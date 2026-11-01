"""Schema lint uses synthetic SHOW CREATE cases and recorded snapshots.

No database writes or network: the live adapter below records bounded reads.
Both engines are covered; KEYWORDS and standard-parent-key defaults are
intentionally MySQL-only rules.
"""
import copy
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chart_data import validate  # noqa: E402
from myflames import schema_lint, schema_versions_api, schema_versions_cli  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / 'docs/schemas/schema-lint-1.0.json').read_text())


def table(name='orders', lines=None, options='ENGINE=InnoDB DEFAULT CHARSET=utf8mb4', columns=None):
    return {'type': 'table', 'name': name, 'columns': columns or {},
            'ddl': 'CREATE TABLE `%s` (\n  %s\n) %s' %
                   (name.replace('`', '``'), ',\n  '.join(lines or ['`id` int NOT NULL']), options)}


def routine(name='p', kind='procedure', definer='`owner`@`localhost`'):
    ddl = 'CREATE DEFINER=%s %s `%s` () SELECT 1' % (definer, kind.upper(), name)
    obj = {'type': kind, 'name': name, 'ddl': ddl}
    if kind == 'trigger':
        obj.update(table='orders', timing='BEFORE', event='INSERT', order=1)
    return obj


def snapshot(*objects, **options):
    engine = options.get('engine', 'mysql')
    return {'schema_version': 'schema-snapshot-1.0', 'database': 'app',
            'taken_at': '2026-10-27T00:00:00Z',
            'server': {'engine': engine, 'server_version': options.get('version', '8.4.11' if engine == 'mysql' else '11.4.9-MariaDB'),
                       'host': '127.0.0.1', 'port': 3306, 'lower_case_table_names': '0'},
            'objects': [{'type': 'database', 'name': 'app', 'ddl': 'CREATE DATABASE `app`'}] + list(objects)}


def findings(report, rule):
    return [item for item in report['findings'] if item['rule'] == rule]


def unknown(report, rule):
    return [item for item in report['not_judged'] if item['rule'] == rule]


class Connection(object):
    def __init__(self, version='8.4.11', comment='MySQL Community Server', words='SELECT\nORDER\nRANK\n'):
        self.version, self.comment, self.words = version, comment, words
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, extra_flags, timeout))
        if '@@version' in sql:
            return self.version + '\t' + self.comment + '\n'
        if 'information_schema.KEYWORDS' in sql:
            return self.words
        if 'FROM mysql.user ' in sql:
            return 'owner\tlocalhost\tcaching_sha2_password\tN\tN\t2026-01-01\tNULL\t0\t0\n'
        if 'FROM mysql.role_edges ' in sql or 'FROM mysql.default_roles ' in sql:
            return ''
        raise AssertionError('Unexpected SQL: ' + sql)


def live(conn=None, host='127.0.0.1', port=3306):
    return {'connection': conn or Connection(), 'host': host, 'port': port}


class OfflineRules(unittest.TestCase):
    def test_money_types_and_name_heuristic_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            obj = table(lines=['`unit_price` float', '`AMOUNT` double', '`total` real',
                               '`cash` double', '`cost` decimal(10,2)', '`tax_note` varchar(80)'])
            result = schema_lint.lint(snapshot(obj, engine=engine))
            self.assertEqual([i['column'] for i in findings(result, 'money_float')],
                             ['unit_price', 'AMOUNT', 'total'])
            self.assertEqual(findings(result, 'money_float')[0]['text'],
                             'FLOAT is approximate; money usually needs DECIMAL.')

    def test_every_requested_money_name(self):
        for name in ('price', 'amount', 'cost', 'total', 'balance', 'salary', 'fee',
                     'tax', 'payment', 'money', 'revenue'):
            result = schema_lint.lint(snapshot(table(lines=['`x_%s_x` float' % name])))
            self.assertEqual(len(findings(result, 'money_float')), 1, name)

    def test_utf8_metadata_and_engine_wording(self):
        for engine in ('mysql', 'mariadb'):
            obj = table(lines=['`name` varchar(30)', '`label` varchar(30)', '`safe` varchar(30)'],
                        options='ENGINE=InnoDB DEFAULT CHARSET=utf8',
                        columns={'name': ['utf8mb3', 'utf8mb3_general_ci'],
                                 'label': ['utf8', 'utf8_general_ci'],
                                 'safe': ['utf8mb4', 'utf8mb4_general_ci']})
            rows = findings(schema_lint.lint(snapshot(obj, engine=engine)), 'utf8mb3')
            self.assertEqual([i['column'] for i in rows], [None, 'name', 'label'])
            self.assertTrue(all(('deprecates' in i['text']) == (engine == 'mysql') for i in rows))

    def test_effective_charset_wins_over_configurable_utf8_alias(self):
        obj = table(lines=['`name` varchar(30) CHARACTER SET utf8'],
                    columns={'name': ['utf8mb4', 'utf8mb4_general_ci']})
        self.assertFalse(findings(schema_lint.lint(snapshot(obj, engine='mariadb')), 'utf8mb3'))

    def test_column_charset_without_metadata(self):
        obj = table(lines=['`name` varchar(30) CHARACTER SET utf8mb3'])
        self.assertEqual(findings(schema_lint.lint(snapshot(obj)), 'utf8mb3')[0]['column'], 'name')

    def test_myisam(self):
        for engine in ('mysql', 'mariadb'):
            out = schema_lint.lint(snapshot(table(options='ENGINE=MyISAM'), engine=engine))
            self.assertEqual(len(findings(out, 'myisam')), 1)
            self.assertIn('Table repair', findings(out, 'myisam')[0]['detail'])

    def test_zero_date_default_both_engines(self):
        obj = table(lines=["`d` date DEFAULT '0000-00-00'", "`dt` datetime DEFAULT '0000-00-00 00:00:00'",
                           "`ts` timestamp(6) DEFAULT ('0000-00-00 00:00:00.000000')",
                           "`n` date DEFAULT 0", "`valid` date DEFAULT '2026-01-01'",
                           "`missing` date DEFAULT NULL", "`text` varchar(30) DEFAULT '0000-00-00'"])
        for engine in ('mysql', 'mariadb'):
            rows = findings(schema_lint.lint(snapshot(obj, engine=engine)), 'zero_date')
            self.assertEqual([i['column'] for i in rows], ['d', 'dt', 'ts', 'n'])
            expected = 'A zero date is not a real date.'
            if engine == 'mysql':
                expected += " MySQL's default sql_mode (NO_ZERO_DATE with strict mode) rejects it."
            self.assertTrue(all(i['text'] == expected for i in rows))

    def test_literals_comments_and_identifiers_are_not_syntax(self):
        obj = table(lines=["`normal` float COMMENT 'price total tax'",
                           "`d` date COMMENT 'DEFAULT ''0000-00-00'''",
                           '`DEFAULT` date',
                           "`n` date /* DEFAULT '0000-00-00' */ DEFAULT NULL",
                           "`g` date GENERATED ALWAYS AS (if(`id`, '0000-00-00', '2026-01-01')) VIRTUAL",
                           "`s` varchar(30) COMMENT 'CHARACTER SET utf8mb3'"],
                    options="ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ENGINE=MyISAM CHARSET=utf8'")
        out = schema_lint.lint(snapshot(obj))
        self.assertEqual(out['findings'], [])

    def test_comment_escapes_do_not_expose_fake_syntax(self):
        obj = table(lines=["`d` date COMMENT 'a \\' DEFAULT \\'0000-00-00\\''",
                           "`n` varchar(20) COMMENT 'a '' CHARACTER SET utf8mb3'"])
        self.assertEqual(schema_lint.lint(snapshot(obj))['findings'], [])

    def test_unreadable_and_unrecognized_ddl_are_not_judged(self):
        for ddl in (None, 'CREATE TABLE t (id int)'):
            obj = table()
            obj['ddl'] = ddl
            obj['reason'] = 'No SHOW CREATE privilege.'
            report = schema_lint.lint(snapshot(obj))
            self.assertEqual(report['findings'], [])
            for rule in schema_lint._TABLE_RULES:
                self.assertTrue(unknown(report, rule), rule)

    def test_unknown_inventory_coverage_is_preserved(self):
        snap = snapshot(table())
        snap['coverage'] = {'tables': {'state': 'unknown', 'reason': 'Limited account.'}}
        report = schema_lint.lint(snap)
        for rule in schema_lint._TABLE_RULES:
            self.assertIn('Limited account.', unknown(report, rule)[0]['reason'])


class ForeignKeys(unittest.TestCase):
    def build(self, child_key, parent_key='PRIMARY KEY (`id`)', **options):
        child = table('child', ['`id` int', '`other` int', child_key,
                               'CONSTRAINT `fk` FOREIGN KEY (`id`) REFERENCES `parent` (`id`)'])
        parent = table('parent', ['`id` int', '`other` int', parent_key])
        return schema_lint.lint(snapshot(child, parent, **options))

    def test_leftmost_full_child_key(self):
        for key in ('KEY `k` (`id`)', 'KEY `k` (`id`,`other`)', 'PRIMARY KEY (`id` DESC)',
                    'UNIQUE KEY `k(` (`id`)'):
            with self.subTest(key=key):
                self.assertFalse(findings(self.build(key), 'fk_index'))
        for key in ('KEY `k` (`other`,`id`)', 'KEY `k` (`id`(4))',
                    'KEY `k` ((abs(`other`)),`id`)', 'FULLTEXT KEY `k` (`id`)'):
            with self.subTest(key=key):
                self.assertEqual(len(findings(self.build(key), 'fk_index')), 1)

    def test_expression_after_leftmost_key_does_not_hide_full_column(self):
        self.assertFalse(findings(self.build('KEY `k` (`id`,(abs(`other`)))'), 'fk_index'))

    def test_composite_child_keeps_order(self):
        for key, expected in [('KEY `k` (`a`,`b`,`c`)', False), ('KEY `k` (`b`,`a`)', True),
                              ('KEY `k` (`a`,`b`(5))', True)]:
            obj = table('child', ['`a` int', '`b` int', '`c` int', key,
                                 'CONSTRAINT `fk` FOREIGN KEY (`a`,`b`) REFERENCES `elsewhere`.`p` (`x`,`y`)'])
            self.assertEqual(bool(findings(schema_lint.lint(snapshot(obj)), 'fk_index')), expected)

    def test_parent_requires_exact_full_unique_key_with_engine_specific_detail(self):
        for key in ('PRIMARY KEY (`id`)', 'UNIQUE KEY `u` (`id`)'):
            self.assertFalse(findings(self.build('KEY `k` (`id`)', key), 'fk_parent_key'))
        for key in ('KEY `k` (`id`)', 'UNIQUE KEY `u` (`id`,`other`)', 'UNIQUE KEY `u` (`id`(4))'):
            current = findings(self.build('KEY `k` (`id`)', key), 'fk_parent_key')
            self.assertEqual(len(current), 1)
            self.assertIn('restrict_fk_on_non_standard_key', current[0]['detail'])
            for options in ({'version': '8.0.46'}, {'engine': 'mariadb'}):
                rows = findings(self.build('KEY `k` (`id`)', key, **options), 'fk_parent_key')
                self.assertEqual(len(rows), 1)
                self.assertNotIn('restrict_fk_on_non_standard_key', rows[0]['detail'])

    def test_external_missing_and_unreadable_parent_are_not_judged(self):
        for schema, include_parent in [('other.', True), ('', False), ('', True)]:
            child = table('child', ['`id` int', 'KEY `k` (`id`)',
                                   'CONSTRAINT `fk` FOREIGN KEY (`id`) REFERENCES %s`parent` (`id`)' % schema])
            parent = table('parent')
            parent['ddl'] = None
            for engine in ('mysql', 'mariadb'):
                out = schema_lint.lint(snapshot(child, *([parent] if include_parent else []), engine=engine))
                self.assertFalse(findings(out, 'fk_parent_key'))
                self.assertTrue(unknown(out, 'fk_parent_key'))

    def test_constraint_names_containing_keywords_do_not_break_parse(self):
        child = table('child', ['`id` int', 'KEY `k` (`id`)',
                               'CONSTRAINT `name FOREIGN KEY odd` FOREIGN KEY (`id`) REFERENCES `parent` (`id`)'])
        parent = table('parent', ['`id` int', 'PRIMARY KEY (`id`)'])
        report = schema_lint.lint(snapshot(child, parent))
        self.assertFalse(findings(report, 'fk_index'))
        self.assertFalse(unknown(report, 'fk_index'))
        self.assertFalse(findings(report, 'fk_parent_key'))

    def test_table_name_case_follows_captured_setting(self):
        child = table('child', ['`id` int', 'KEY `k` (`id`)',
                               'CONSTRAINT `fk` FOREIGN KEY (`id`) REFERENCES `PARENT` (`id`)'])
        snap = snapshot(child, table('parent', ['`id` int', 'PRIMARY KEY (`id`)']))
        self.assertTrue(unknown(schema_lint.lint(snap), 'fk_parent_key'))
        snap['server']['lower_case_table_names'] = '1'
        self.assertFalse(unknown(schema_lint.lint(snap), 'fk_parent_key'))


class ReservedWords(unittest.TestCase):
    def test_keywords_available_from_mysql8013(self):
        out = schema_lint.lint(snapshot(table('order'), version='8.0.13'), live(Connection('8.0.13')))
        self.assertTrue(findings(out, 'reserved_word'))
        self.assertFalse(unknown(out, 'reserved_word'))

    def test_live_table_column_and_routine_names(self):
        conn = Connection()
        report = schema_lint.lint(snapshot(table('order', ['`rank` int']), routine('select')), live(conn))
        rows = findings(report, 'reserved_word')
        self.assertEqual([(i['object'], i['column']) for i in rows],
                         [('table order', None), ('table order', 'rank'), ('procedure select', None)])
        self.assertFalse(unknown(report, 'reserved_word'))

    def test_future_names_are_offline_and_compared_to_snapshot_version(self):
        snap = snapshot(table('intersect', ['`qualify` int', '`id` int']), version='8.0.30')
        rows = findings(schema_lint.lint(snap), 'reserved_word')
        self.assertEqual(len(rows), 2)
        self.assertIn('8.0.31', rows[0]['detail'])
        self.assertIn('8.3.0', rows[1]['detail'])
        snap['server']['server_version'] = '8.4.11'
        self.assertFalse(findings(schema_lint.lint(snap), 'reserved_word'))

    def test_known_transition_takes_priority_over_newer_live_keywords(self):
        for captured, transition in [('8.0.46', True), ('8.4.0', False)]:
            with self.subTest(captured=captured):
                report = schema_lint.lint(snapshot(table('qualify'), version=captured),
                                          live(Connection('8.4.11', words='SELECT\nQUALIFY\n')))
                rows = findings(report, 'reserved_word')
                self.assertEqual(len(rows), 1)
                self.assertFalse(unknown(report, 'reserved_word'))
                if transition:
                    self.assertIn('becomes reserved in MySQL 8.3.0', rows[0]['detail'])
                    self.assertIn('later MySQL release', rows[0]['text'])
                else:
                    self.assertIn('reserved by the connected MySQL 8.4.11', rows[0]['detail'])
                    self.assertNotIn('becomes reserved', rows[0]['text'] + rows[0]['detail'])

    def test_mariadb_and_old_actual_mysql_do_not_query_keywords(self):
        for engine, conn in [('mariadb', Connection('11.4.9-MariaDB')),
                             ('mysql', Connection('8.0.10')),
                             ('mysql', Connection('11.4.9', 'MariaDB Server'))]:
            report = schema_lint.lint(snapshot(table(), engine=engine), live(conn))
            self.assertTrue(unknown(report, 'reserved_word'))
            self.assertFalse(any('KEYWORDS' in call[0] for call in conn.calls))

    def test_null_keyword_row_is_skipped(self):
        """Seen live on MySQL 8.4: KEYWORDS has one RESERVED row whose WORD is NULL."""
        report = schema_lint.lint(snapshot(table('rank')), live(Connection(words='SELECT\nNULL\nRANK\n')))
        self.assertEqual(len(findings(report, 'reserved_word')), 1)
        self.assertFalse(unknown(report, 'reserved_word'))

    def test_unreadable_keywords_give_one_reason(self):
        report = schema_lint.lint(snapshot(table('rank', lines=['`order` int', '`rank` int'])), live(Connection(words='')))
        self.assertEqual(len(unknown(report, 'reserved_word')), 1)

    def test_empty_excessive_and_failed_keyword_reads_not_judged(self):
        for words in ('', 'SELECT\t1', 'SELECT\n' * (schema_lint.MAX_KEYWORDS + 1)):
            report = schema_lint.lint(snapshot(table()), live(Connection(words=words)))
            self.assertTrue(unknown(report, 'reserved_word'))
        conn = Connection()
        conn.run = mock.Mock(side_effect=ConnectorError('Access denied'))
        self.assertIn('Access denied', unknown(schema_lint.lint(snapshot(table()), live(conn)), 'reserved_word')[0]['reason'])

    def test_only_bounded_tagged_reads_and_single_metadata_lookup(self):
        conn = Connection()
        report = schema_lint.lint(snapshot(table('order'), routine()), live(conn))
        self.assertFalse(findings(report, 'definer_missing'))
        self.assertEqual(sum('@@version' in sql for sql, _, _ in conn.calls), 1)
        self.assertTrue(any('WHERE RESERVED = 1 LIMIT 10001' in sql for sql, _, _ in conn.calls))
        for sql, flags, timeout in conn.calls:
            self.assertTrue(sql.startswith(('/*myflames:schema-lint*/ SELECT', '/*myflames:accounts*/ SELECT')))
            self.assertEqual(flags, ['--comments'])
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, schema_lint.READ_SECONDS)


class Definers(unittest.TestCase):
    def test_mariadb_numeric_version_uses_actual_comment_and_complete_account_list(self):
        for name, missing in [('owner', False), ('missing', True)]:
            def read(conn):
                self.assertEqual(conn.server_version(), '11.4.9-MariaDB')
                return {'complete': True, 'accounts': [{'name': 'owner', 'host': 'localhost'}]}
            with mock.patch.object(schema_lint.accounts, 'list_accounts', side_effect=read):
                out = schema_lint.lint(snapshot(routine(definer='`%s`@`localhost`' % name), engine='mariadb'),
                                       live(Connection('11.4.9', 'MariaDB Server')))
            self.assertEqual(bool(findings(out, 'definer_missing')), missing)
            self.assertFalse(unknown(out, 'definer_missing'))

    def test_engine_mismatch_does_not_read_accounts(self):
        with mock.patch.object(schema_lint.accounts, 'list_accounts') as read:
            out = schema_lint.lint(snapshot(routine()), live(Connection('11.4.9', 'MariaDB Server')))
        read.assert_not_called()
        self.assertTrue(unknown(out, 'definer_missing'))
        self.assertFalse(findings(out, 'definer_missing'))

    def test_complete_accounts_missing_and_present_all_object_types(self):
        for kind in ('view', 'procedure', 'function', 'trigger', 'event'):
            for account, expected in [('owner', False), ('missing', True)]:
                with self.subTest(kind=kind, account=account):
                    obj = routine(kind=kind, definer='`%s`@`localhost`' % account)
                    report = schema_lint.lint(snapshot(obj), live())
                    self.assertEqual(bool(findings(report, 'definer_missing')), expected)
                    self.assertFalse(unknown(report, 'definer_missing'))

    def test_account_match_keeps_username_case_and_folds_host_case(self):
        for definer, missing in [('`owner`@`LOCALHOST`', False), ('`Owner`@`localhost`', True),
                                 ('`owner`@`%`', True), ("'owner'@'localhost'", False)]:
            out = schema_lint.lint(snapshot(routine(definer=definer)), live())
            self.assertEqual(bool(findings(out, 'definer_missing')), missing)

    def test_partial_and_failed_reads_never_assert_absence(self):
        obj = routine(definer='`missing`@`localhost`')
        for response in ({'complete': False, 'accounts': []}, {'complete': True, 'accounts': [{}]}):
            with mock.patch.object(schema_lint.accounts, 'list_accounts', return_value=response):
                report = schema_lint.lint(snapshot(obj), live())
            self.assertFalse(findings(report, 'definer_missing'))
            self.assertTrue(unknown(report, 'definer_missing'))
        with mock.patch.object(schema_lint.accounts, 'list_accounts', side_effect=ConnectorError('Account read failed')):
            report = schema_lint.lint(snapshot(obj), live())
        self.assertIn('Account read failed', unknown(report, 'definer_missing')[0]['reason'])

    def test_offline_mismatch_unknown_endpoint_no_account_reads(self):
        for endpoint in (None, live(host='another'), live(port=3307)):
            with mock.patch.object(schema_lint.accounts, 'list_accounts') as read:
                report = schema_lint.lint(snapshot(routine()), endpoint)
            read.assert_not_called()
            self.assertTrue(unknown(report, 'definer_missing'))
        snap = snapshot(routine())
        snap['server']['host'] = None
        with mock.patch.object(schema_lint.accounts, 'list_accounts') as read:
            report = schema_lint.lint(snap, live())
        read.assert_not_called()
        self.assertTrue(unknown(report, 'definer_missing'))

    def test_another_ssh_target_is_another_server(self):
        # 127.0.0.1:3306 through SSH host A is not 127.0.0.1:3306 through B,
        # nor a direct connection: no account list is read for the definers.
        through_a = snapshot(routine())
        through_a['server'].update(ssh_host='bastion-a', ssh_user='deploy')
        cases = [
            (through_a, live(), 'read via deploy@bastion-a and the live connection goes directly'),
            (through_a, dict(live(), ssh_host='bastion-b', ssh_user='deploy'), 'goes via deploy@bastion-b'),
            (through_a, dict(live(), ssh_host='bastion-a'), 'goes via bastion-a;'),
            (snapshot(routine()), dict(live(), ssh_host='bastion-a'), 'read directly and the live connection goes via bastion-a'),
        ]
        for snap, endpoint, reason in cases:
            with self.subTest(reason=reason):
                with mock.patch.object(schema_lint.accounts, 'list_accounts') as read:
                    report = schema_lint.lint(snap, endpoint)
                read.assert_not_called()
                self.assertFalse(findings(report, 'definer_missing'))
                self.assertIn(reason, unknown(report, 'definer_missing')[0]['reason'])
        # The same SSH target is the same server: its accounts are read.
        report = schema_lint.lint(through_a, dict(live(), ssh_host='bastion-a', ssh_user='deploy'))
        self.assertFalse(unknown(report, 'definer_missing'))
        validate(report, SCHEMA)

    def test_view_only_mismatched_endpoint_reads_nothing(self):
        conn = Connection()
        schema_lint.lint(snapshot(routine(kind='view')), live(conn, host='other'))
        self.assertEqual(conn.calls, [])

    def test_missing_definition_is_not_a_missing_definer(self):
        obj = routine()
        obj['ddl'] = None
        obj['reason'] = 'The definition could not be read.'
        report = schema_lint.lint(snapshot(obj), live())
        self.assertFalse(findings(report, 'definer_missing'))
        self.assertIn('could not be read', unknown(report, 'definer_missing')[0]['reason'])

    def test_failed_connection_keeps_offline_findings_with_reason(self):
        endpoint = {'connection': None, 'host': '127.0.0.1', 'port': 3306,
                    'error': 'Could not read live server: connection refused.'}
        out = schema_lint.lint(snapshot(table(lines=['`price` float']), routine()), endpoint)
        self.assertTrue(findings(out, 'money_float'))
        for rule in ('reserved_word', 'definer_missing'):
            self.assertIn('connection refused', unknown(out, rule)[0]['reason'])


class Contract(unittest.TestCase):
    def test_rejects_invalid_snapshot_before_connecting(self):
        conn = Connection()
        for value in (None, {}, {'schema_version': 'schema-snapshot-999'}):
            with self.assertRaises(ValueError):
                schema_lint.lint(value, live(conn))
        self.assertEqual(conn.calls, [])

    def test_rejects_bad_live_descriptor(self):
        for endpoint in ({}, {'connection': object()}, live(host='a b'), live(port=True), live(port=0)):
            with self.assertRaises(ValueError):
                schema_lint.lint(snapshot(table()), endpoint)

    def test_json_roundtrip_schema_and_no_input_mutation(self):
        snap = snapshot(table(lines=['`price` float', "`d` date DEFAULT '0000-00-00'"], options='ENGINE=MyISAM DEFAULT CHARSET=utf8'), routine())
        before = copy.deepcopy(snap)
        result = schema_lint.lint(snap)
        self.assertEqual(snap, before)
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)
        validate(result, SCHEMA)
        with self.assertRaises(AssertionError):
            validate(dict(result, password='must never be present'), SCHEMA)

    def test_every_recorded_mysql_and_mariadb_snapshot_validates(self):
        for path in (ROOT / 'test/fixtures/schema_versions').glob('*.json'):
            data = json.loads(path.read_text())
            if data.get('schema_version') != 'schema-snapshot-1.0':
                continue
            with self.subTest(path=path.name):
                validate(schema_lint.lint(data), SCHEMA)

    def test_deadline_expires_without_another_read(self):
        conn = Connection()
        with mock.patch.object(schema_lint.time, 'monotonic', side_effect=[0, 31]):
            report = schema_lint.lint(snapshot(table()), live(conn))
        self.assertEqual(conn.calls, [])
        self.assertIn('time limit', unknown(report, 'reserved_word')[0]['reason'])


class CliAndApi(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.path = Path(self.temp.name) / 'schema.json'
        self.path.write_text(json.dumps(snapshot(table(lines=['`price` float']))))

    def cli(self, *args):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', io.StringIO()):
            try:
                schema_versions_cli.cmd_schema(list(args))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_file_json_lint_is_offline_by_default(self):
        with mock.patch.object(schema_versions_cli, 'MySQLConnection') as factory:
            code, text, err = self.cli('lint', 'file:' + str(self.path), '--json')
        factory.assert_not_called()
        self.assertEqual((code, err), (0, ''))
        result = json.loads(text)
        validate(result, SCHEMA)
        self.assertTrue(findings(result, 'money_float'))
        self.assertTrue(unknown(result, 'reserved_word'))

    def test_fail_on_watch_exits_one_for_findings_and_zero_for_only_not_judged(self):
        code, text, err = self.cli('lint', str(self.path), '--json', '--fail-on', 'watch')
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(text)['findings'])
        self.assertIn('--fail-on watch', err)
        self.path.write_text(json.dumps(snapshot(table())))
        code, text, err = self.cli('lint', str(self.path), '--json', '--fail-on', 'watch')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(text)['findings'], [])
        self.assertTrue(json.loads(text)['not_judged'])

    def test_invalid_input_and_gate_exit_two_without_stdout_or_output_changes(self):
        output = Path(self.temp.name) / 'keep.json'
        output.write_text('keep')
        for argv in [('lint', str(self.path), '--fail-on', 'error'),
                     ('lint', 'file:/no/such/schema.json', '--json', '-o', str(output))]:
            code, text, err = self.cli(*argv)
            self.assertEqual((code, text), (2, ''))
            self.assertTrue(err)
            self.assertEqual(output.read_text(), 'keep')
        self.path.write_text('{}')
        code, text, _ = self.cli('lint', str(self.path), '--json', '-o', str(output))
        self.assertEqual((code, text, output.read_text()), (2, '', 'keep'))

    def test_json_file_and_text_output(self):
        output = Path(self.temp.name) / 'lint.json'
        code, text, err = self.cli('lint', str(self.path), '--json', '-o', str(output))
        self.assertEqual((code, text), (0, ''))
        self.assertIn('Written to', err)
        validate(json.loads(output.read_text()), SCHEMA)
        code, text, err = self.cli('lint', str(self.path))
        self.assertEqual(code, 0, err)
        self.assertIn('FLOAT is approximate', text)
        self.assertIn('Not judged:', text)

    def test_file_with_explicit_connection_reads_once_and_failure_stays_offline(self):
        conn = Connection()
        with mock.patch.object(schema_versions_cli, 'MySQLConnection', return_value=conn) as factory:
            code, text, err = self.cli('lint', str(self.path), '--json', '-h', '127.0.0.1')
        self.assertEqual(code, 0, err)
        factory.assert_called_once()
        self.assertFalse(unknown(json.loads(text), 'reserved_word'))
        with mock.patch.object(schema_versions_cli, 'MySQLConnection', side_effect=ConnectorError('bad s3cret')):
            code, text, err = self.cli('lint', str(self.path), '--json', '-ps3cret')
        self.assertEqual(code, 0, err)
        self.assertNotIn('s3cret', text + err)
        self.assertTrue(findings(json.loads(text), 'money_float'))
        self.assertIn('[redacted]', text)

    def test_api_saved_and_latest_remain_offline(self):
        old = self.store.schema_version_save(snapshot(table()), 'old', '')
        self.store.schema_version_save(snapshot(table(lines=['`price` float'])), 'new', '')
        with mock.patch.object(schema_versions_api, 'MySQLConnection') as factory:
            clean = schema_versions_api.response(schema_versions_api.LINT_PATH, {'id': old['id']}, self.store)
            latest = schema_versions_api.response(schema_versions_api.LINT_PATH, {'id': 'latest'}, self.store)
        factory.assert_not_called()
        self.assertFalse(clean['findings'])
        self.assertTrue(findings(latest, 'money_float'))
        validate(latest, SCHEMA)

    def test_api_saved_connection_failure_is_redacted_and_retains_findings(self):
        saved = self.store.schema_version_save(snapshot(table(lines=['`price` float']), routine()), '', '')
        payload = {'id': saved['id'], 'connection': {'host': '127.0.0.1', 'port': 3306, 'password': 's3cret'}}
        with mock.patch.object(schema_versions_api, 'MySQLConnection', side_effect=ConnectorError('bad s3cret')):
            report = schema_versions_api.response(schema_versions_api.LINT_PATH, payload, self.store, lambda data: data)
        self.assertTrue(findings(report, 'money_float'))
        self.assertNotIn('s3cret', json.dumps(report))
        self.assertIn('[redacted]', unknown(report, 'definer_missing')[0]['reason'])

    def test_api_and_cli_connected_read_errors_redact_passwords(self):
        conn = Connection()
        conn.run = mock.Mock(side_effect=ConnectorError('read failed for s3cret'))
        saved = self.store.schema_version_save(snapshot(table(lines=['`price` float']), routine()), '', '')
        payload = {'id': saved['id'], 'connection': {'host': '127.0.0.1', 'port': 3306, 'password': 's3cret'}}
        with mock.patch.object(schema_versions_api, 'MySQLConnection', return_value=conn):
            report = schema_versions_api.response(schema_versions_api.LINT_PATH, payload, self.store, lambda data: data)
        self.assertNotIn('s3cret', json.dumps(report))
        self.assertIn('[redacted]', unknown(report, 'reserved_word')[0]['reason'])
        with mock.patch.object(schema_versions_cli, 'MySQLConnection', return_value=conn):
            code, text, err = self.cli('lint', str(self.path), '--json', '-ps3cret')
        self.assertEqual(code, 0, err)
        self.assertNotIn('s3cret', text + err)
        self.assertIn('[redacted]', text)

    def test_api_live_read_does_not_save_a_version(self):
        payload = {'id': 'live', 'database': 'app', 'connection': {'host': '127.0.0.1', 'port': 3306}}
        with mock.patch.object(schema_versions_api, 'read_live', return_value=snapshot(table())) as capture, \
                mock.patch.object(schema_versions_api, 'MySQLConnection', return_value=Connection()):
            report = schema_versions_api.response(schema_versions_api.LINT_PATH, payload, self.store, lambda data: data)
        capture.assert_called_once()
        self.assertEqual(self.store.schema_version_list()['items'], [])
        validate(report, SCHEMA)


if __name__ == '__main__':
    unittest.main()
