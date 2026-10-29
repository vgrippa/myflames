"""Unit tests for schema_versions.py: capture, validation, compare, store and API.

Live reads replay the recorded client calls in test/fixtures/schema_versions
(_schema_replay.ReplayConn); nothing contacts a server. Store and API tests
run against a throwaway HOME with the vault secret backend, like test_ui.py.
"""
import copy
import inspect
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _schema_replay as replay  # noqa: E402

from myflames import schema_versions, schema_versions_api, ui  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

TAG = schema_versions.TAG
PRIVILEGES_TAG = '/*myflames:privileges*/ '
ROUTINE_TYPES = ('procedure', 'function', 'package', 'package_body')


def snap(server, which):
    return replay.snapshot(server, which)


def entry(report, type_, name):
    for item in report['objects']:
        if item['type'] == type_ and item['name'] == name:
            return item
    raise AssertionError('%s %s not in the comparison' % (type_, name))


def changes(report, type_, name):
    return [(c['part'], c.get('name'), c['change']) for c in
            entry(report, type_, name).get('changes', [])]


class IsolatedStoreTest(unittest.TestCase):
    """setUp/tearDown copied from test/test_ui.py: never the real ~/.myflames."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name,
                                               'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)


class TestCaptureDeterministic(unittest.TestCase):
    def test_capture_replays_to_the_stored_checksum(self):
        # Every recording reproduces its stored snapshot exactly: capture is
        # deterministic and the fixtures are current.
        for server in replay.SERVERS:
            for which in replay.WHICH:
                with self.subTest(server=server, which=which):
                    record = replay.recording(server, which)
                    captured = schema_versions.capture(
                        replay.ReplayConn(record), record['database'], record['source'])
                    fixture = replay.load('%s-%s' % (server, which))
                    self.assertEqual(captured['checksum'], fixture['checksum'])
                    self.assertEqual(captured['database'], fixture['database'])
                    self.assertEqual(captured['server']['engine'],
                                     fixture['server']['engine'])


class TestCaptureStatements(unittest.TestCase):
    def test_capture_sends_only_tagged_reads(self):
        # The capture is: one tagged SHOW GLOBAL VARIABLES read, the tagged
        # information_schema inventory SELECTs, the privileges read, and one
        # run_script of USE + SET SESSION + SHOW CREATE statements.
        forbidden = re.compile(r'\b(INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|GRANT|KILL)\b')
        for server in replay.SERVERS:
            with self.subTest(server=server):
                record = replay.recording(server, 'before')
                conn = replay.ReplayConn(record)
                schema_versions.capture(conn, record['database'], record['source'])
                scripts = [sent for sent in conn.sent if sent[0] == 'run_script']
                runs = [sent for sent in conn.sent if sent[0] == 'run']
                self.assertEqual(len(scripts), 1)
                for method, sql, flags in conn.sent:
                    self.assertTrue(
                        sql.startswith(TAG) or sql.startswith(PRIVILEGES_TAG),
                        'statement without a tag: %r' % sql[:120])
                    self.assertIsNone(forbidden.search(sql.replace('SHOW CREATE', '')),
                                      'write keyword in: %r' % sql[:120])
                variables = [sql for _, sql, _ in runs
                             if sql.startswith(TAG) and 'SHOW GLOBAL VARIABLES' in sql]
                self.assertEqual(len(variables), 1)
                self.assertTrue(variables[0].startswith(TAG))
                inventory = [sql for _, sql, _ in runs if "SELECT 'S'" in sql]
                self.assertEqual(len(inventory), 1)
                self.assertTrue(inventory[0].startswith(TAG))
                for table in ('SCHEMATA', 'TABLES', 'ROUTINES', 'TRIGGERS',
                              'EVENTS', 'COLUMNS'):
                    self.assertIn('information_schema.%s' % table, inventory[0])
                privileges = [sql for _, sql, _ in runs
                              if sql.startswith(PRIVILEGES_TAG)]
                self.assertTrue(privileges)
                # ...and nothing else: every other read is the variables read
                # or the inventory SELECTs.
                others = [sql for _, sql, _ in runs
                          if not sql.startswith(PRIVILEGES_TAG)
                          and sql not in variables and sql not in inventory]
                self.assertEqual(others, [])
                # The single script: USE, then SET SESSION lines, then SHOW CREATE.
                lines = scripts[0][1].rstrip('\n').split('\n')
                self.assertEqual(lines[0], TAG + 'USE `information_schema`;')
                show_at = next(i for i, line in enumerate(lines)
                               if 'SHOW CREATE' in line)
                for line in lines[1:show_at]:
                    self.assertTrue(line.startswith(TAG + 'SET SESSION '), line)
                self.assertTrue(show_at > 1)
                for line in lines[show_at:]:
                    self.assertTrue(line.startswith(TAG + 'SHOW CREATE '), line)


class TestLimitedSnapshots(unittest.TestCase):
    def test_limited_account_keeps_routines_with_a_reason(self):
        # An account with only SELECT, SHOW VIEW and EXECUTE lists tables and
        # routines but cannot read routine bodies, triggers or events.
        for server in replay.SERVERS:
            with self.subTest(server=server):
                snapshot = snap(server, 'limited')
                routines = [obj for obj in snapshot['objects']
                            if obj['type'] in ROUTINE_TYPES]
                self.assertTrue(routines)
                for obj in routines:
                    self.assertIsNone(obj['ddl'])
                    if server == 'mysql-8.4':
                        self.assertIn('SHOW_ROUTINE', obj['reason'])
                    else:
                        self.assertIn('SHOW CREATE ROUTINE', obj['reason'])
                coverage = snapshot['coverage']
                self.assertEqual(coverage['tables']['state'], 'complete')
                self.assertEqual(coverage['routines']['state'], 'complete')
                self.assertEqual(coverage['triggers']['state'], 'unknown')
                self.assertEqual(coverage['events']['state'], 'unknown')
                kinds = [obj['type'] for obj in snapshot['objects']]
                self.assertNotIn('trigger', kinds)
                self.assertNotIn('event', kinds)


class TestCaptureErrors(unittest.TestCase):
    """ReplayConn's edit hook rewrites recorded answers into failure states."""

    server, which = 'mysql-8.4', 'before'

    def setUp(self):
        self.record = replay.recording(self.server, self.which)
        script = [c for c in self.record['calls'] if c['method'] == 'run_script'][0]
        self.customers_line = next(
            i for i, line in enumerate(script['sql'].split('\n'), 1)
            if 'SHOW CREATE TABLE `sv_m`.`customers`' in line)

    def capture(self, edit):
        conn = replay.ReplayConn(self.record, edit=edit)
        return schema_versions.capture(conn, self.record['database'], self.record['source'])

    def edit_script(self, call, stderr='', drop_prefix=None):
        if drop_prefix:
            call['stdout'] = '\n'.join(
                line for line in call['stdout'].split('\n')
                if not line.startswith(drop_prefix))
        if stderr:
            call['stderr'] = (call.get('stderr') or '') + stderr
        return call

    def test_missing_database(self):
        def edit(method, sql, call):
            if method == 'run' and "SELECT 'S'" in sql:
                call['stdout'] = '\n'.join(
                    line for line in call['stdout'].split('\n')
                    if not line.startswith('S\t'))
            return call
        with self.assertRaisesRegex(ValueError, 'sv_m'):
            self.capture(edit)

    def test_information_schema_refused_target_any_read(self):
        def edit(method, sql, call):
            if method == 'run_script':
                return self.edit_script(call, 'ERROR 1044 (42000) at line 1: '
                                               'Access denied for user')
            return call
        with self.assertRaisesRegex(ValueError, 'Could not prepare the session'):
            self.capture(edit)

    def test_object_dropped_while_reading_is_left_out_with_a_note(self):
        def edit(method, sql, call):
            if method == 'run_script':
                return self.edit_script(
                    call, "ERROR 1146 (42S02) at line %d: Table 'sv_m.customers' "
                          "doesn't exist" % self.customers_line, 'customers\t')
            return call
        snapshot = self.capture(edit)
        names = [obj['name'] for obj in snapshot['objects']]
        self.assertNotIn('customers', names)
        self.assertTrue(any('dropped while the database was read' in note
                            for note in snapshot['notes']), snapshot['notes'])

    def test_unreadable_object_is_kept_with_a_reason(self):
        def edit(method, sql, call):
            if method == 'run_script':
                return self.edit_script(
                    call, 'ERROR 1142 (42000) at line %d: SELECT command denied'
                          % self.customers_line, 'customers\t')
            return call
        snapshot = self.capture(edit)
        customers = next(obj for obj in snapshot['objects']
                         if obj['name'] == 'customers')
        self.assertIsNone(customers['ddl'])
        self.assertIn('ERROR 1142', customers['reason'])

    def test_lost_session_is_a_connector_error(self):
        def edit(method, sql, call):
            if method == 'run_script':
                return self.edit_script(
                    call, 'ERROR 2013 (HY000) at line %d: Lost connection'
                          % self.customers_line)
            return call
        with self.assertRaises(ConnectorError):
            self.capture(edit)

    def test_fewer_rows_than_statements_is_rejected(self):
        def edit(method, sql, call):
            if method == 'run_script':
                return self.edit_script(call, drop_prefix='customers\t')
            return call
        with self.assertRaisesRegex(ValueError, 'returned 13 definitions for 14'):
            self.capture(edit)


class TestValidateSnapshot(unittest.TestCase):
    def test_accepts_every_fixture(self):
        for server in replay.SERVERS:
            for which in replay.WHICH:
                with self.subTest(server=server, which=which):
                    clean = schema_versions.validate_snapshot(
                        replay.load('%s-%s' % (server, which)))
                    self.assertEqual(clean['schema_version'],
                                     schema_versions.SNAPSHOT_VERSION)

    def test_rejects_wrong_schema_version(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        value['schema_version'] = 'schema-snapshot-2.0'
        with self.assertRaises(ValueError):
            schema_versions.validate_snapshot(value)

    def test_rejects_unknown_object_type(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        value['objects'][1]['type'] = 'widget'
        with self.assertRaisesRegex(ValueError, 'Invalid object'):
            schema_versions.validate_snapshot(value)

    def test_rejects_duplicate_table_and_view_name(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        view = copy.deepcopy(value['objects'][1])
        view['type'] = 'view'
        value['objects'].append(view)
        with self.assertRaisesRegex(ValueError, 'twice'):
            schema_versions.validate_snapshot(value)

    def test_rejects_non_integer_trigger_order(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        for obj in value['objects']:
            if obj['type'] == 'trigger':
                obj['order'] = 'first'
        with self.assertRaisesRegex(ValueError, 'trigger order'):
            schema_versions.validate_snapshot(value)

    def test_rejects_missing_engine(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        del value['server']['engine']
        with self.assertRaisesRegex(ValueError, 'engine'):
            schema_versions.validate_snapshot(value)

    def test_rejects_nul_in_a_name(self):
        value = copy.deepcopy(replay.load('mysql-8.4-before'))
        value['objects'][1]['name'] = 'bad\x00name'
        with self.assertRaisesRegex(ValueError, 'name'):
            schema_versions.validate_snapshot(value)

    def test_recomputes_counts_and_checksum(self):
        fixture = replay.load('mysql-8.4-before')
        value = copy.deepcopy(fixture)
        value['checksum'] = 'sha256:bogus'
        value['counts'] = {}
        clean = schema_versions.validate_snapshot(value)
        self.assertEqual(clean['checksum'], fixture['checksum'])
        self.assertEqual(clean['counts'], fixture['counts'])


class TestChecksum(unittest.TestCase):
    def test_changes_when_one_definition_changes(self):
        fixture = replay.load('mysql-8.4-before')
        changed = copy.deepcopy(fixture)
        changed['objects'][1]['ddl'] += ' '
        self.assertNotEqual(schema_versions.checksum(changed),
                            schema_versions.checksum(fixture))

    def test_same_objects_in_another_order_match(self):
        fixture = replay.load('mysql-8.4-before')
        shuffled = copy.deepcopy(fixture)
        shuffled['objects'] = list(reversed(shuffled['objects']))
        # finish() normalizes the order, like every stored or compared read.
        self.assertEqual(schema_versions.finish(shuffled)['checksum'],
                         schema_versions.checksum(fixture))


class TestCompareIdentical(unittest.TestCase):
    def test_target_against_itself(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                target = snap(server, 'before')
                report = schema_versions.compare(target, target)
                self.assertTrue(report['identical'])
                self.assertTrue(all(item['status'] == 'identical'
                                    for item in report['objects']))
                sync = report['sync']
                self.assertEqual(sync['statements'], [])
                self.assertEqual(sync['summary']['statements'], 0)
                self.assertIn('-- No differences to apply.', sync['script'])


class TestCompareBeforeAfter(unittest.TestCase):
    def setUp(self):
        self.expected_summary = {
            'mysql-8.4': {'different': 10, 'only_in_source': 4, 'only_in_target': 2,
                          'not_compared': 0, 'identical': 2, 'definer_only': 0},
            'mariadb-11.4': {'different': 13, 'only_in_source': 4, 'only_in_target': 2,
                             'not_compared': 0, 'identical': 3, 'definer_only': 0},
        }

    def test_summary_and_object_changes(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'after'),
                                                 snap(server, 'before'))
                self.assertFalse(report['identical'])
                self.assertEqual(report['summary'], self.expected_summary[server])
                customers = changes(report, 'table', 'customers')
                self.assertIn(('column', 'status', 'moved'), customers)
                self.assertIn(('column', 'email', 'different'), customers)
                self.assertIn(('column', 'phone', 'only_in_source'), customers)
                self.assertIn(('column', 'score', 'only_in_target'), customers)
                self.assertIn(('index', 'idx_name', 'different'), customers)
                self.assertIn(('index', 'idx_status', 'only_in_source'), customers)
                self.assertIn(('check', 'chk_score', 'only_in_target'), customers)
                self.assertIn(('options', None, 'different'), customers)
                self.assertIn(('foreign_key', 'fk_orders_customer', 'different'),
                              changes(report, 'table', 'orders'))
                self.assertEqual(entry(report, 'table', 'logs')['status'], 'different')
                # body and title only print CHARACTER SET differently: their
                # effective character set is unchanged, so only the table
                # options (the new default) are listed.
                notes_changes = [c for c in
                                 entry(report, 'table', 'notes').get('changes', [])
                                 if c['part'] == 'column']
                self.assertEqual(notes_changes, [])
                self.assertEqual(entry(report, 'trigger', 'trg_bi0')['status'], 'only_in_source')
                self.assertEqual(entry(report, 'trigger', 'trg_bi2')['status'], 'only_in_target')
                self.assertEqual(changes(report, 'database', 'sv_b'),
                                 [('options', None, 'different')])
                if server == 'mariadb-11.4':
                    self.assertEqual(entry(report, 'sequence', 'seq_ids')['status'],
                                     'different')
                    self.assertEqual(entry(report, 'package_body', 'pkg_util')['status'],
                                     'different')
                self.assertTrue(any('different names (sv_m and sv_b)' in note
                                    for note in report['notes']), report['notes'])

    def test_unchanged_objects(self):
        unchanged = {'mysql-8.4': {'f_double', 'trg_bi'},
                     'mariadb-11.4': {'f_double', 'pkg_util', 'trg_bi'}}
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'after'),
                                                 snap(server, 'before'))
                names = {item['name'] for item in report['objects']
                         if item['status'] == 'identical'}
                self.assertEqual(names, unchanged[server])


class TestCompareLimited(unittest.TestCase):
    def test_routines_not_compared_and_missing_objects_removed(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'limited'),
                                                 snap(server, 'after'))
                for type_, name in (('procedure', 'p_touch'), ('function', 'f_double'),
                                    ('function', 'f_triple')):
                    item = entry(report, type_, name)
                    self.assertEqual(item['status'], 'not_compared')
                    self.assertTrue(item['reason'].startswith('Source:'), item['reason'])
                if server == 'mariadb-11.4':
                    for name in ('pkg_util',):
                        for type_ in ('package', 'package_body'):
                            item = entry(report, type_, name)
                            self.assertEqual(item['status'], 'not_compared')
                            self.assertTrue(item['reason'].startswith('Source:'),
                                            item['reason'])
                # Triggers and events exist in Source but the limited account
                # could not list them, so they compare as removed.
                for type_, name in (('trigger', 'trg_bi'), ('trigger', 'trg_bi0'),
                                    ('event', 'ev_cleanup')):
                    self.assertEqual(entry(report, type_, name)['status'], 'only_in_target')
                self.assertTrue(any('Source may not list every trigger' in note
                                    for note in report['notes']), report['notes'])
                self.assertTrue(any('Source may not list every event' in note
                                    for note in report['notes']), report['notes'])


class TestIgnoreDefiners(unittest.TestCase):
    def modified_source(self):
        source = copy.deepcopy(replay.load('mysql-8.4-after'))
        for obj in source['objects']:
            if (obj['type'], obj['name']) in (('function', 'f_double'),
                                              ('trigger', 'trg_bi')):
                obj['ddl'] = obj['ddl'].replace('DEFINER=`root`@`%`',
                                                'DEFINER=`other`@`%`', 1)
        return schema_versions.validate_snapshot(source)

    def test_definer_change_is_reported_as_such(self):
        report = schema_versions.compare(self.modified_source(),
                                         snap('mysql-8.4', 'before'))
        for name in ('f_double', 'trg_bi'):
            item = entry(report, 'function' if name == 'f_double' else 'trigger', name)
            self.assertEqual(item['status'], 'different')
            self.assertTrue(item['definer_only'])
        self.assertEqual(report['summary']['definer_only'], 2)

    def test_ignore_definers_leaves_them_unchanged(self):
        report = schema_versions.compare(self.modified_source(),
                                         snap('mysql-8.4', 'before'), ignore_definers=True)
        for name in ('f_double', 'trg_bi'):
            item = entry(report, 'function' if name == 'f_double' else 'trigger', name)
            self.assertEqual(item['status'], 'identical')
        self.assertEqual(report['summary']['definer_only'], 0)
        self.assertTrue(any('Definers are ignored' in note for note in report['notes']))


class TestCaseRename(unittest.TestCase):
    def test_rename_to_another_letter_case_pairs_by_name(self):
        renamed = copy.deepcopy(replay.load('mysql-8.4-before'))
        for obj in renamed['objects']:
            if obj['type'] == 'table' and obj['name'] == 'items':
                obj['name'] = 'ITEMS'
        report = schema_versions.compare(schema_versions.validate_snapshot(renamed),
                                         snap('mysql-8.4', 'before'))
        item = entry(report, 'table', 'ITEMS')
        self.assertEqual(item['status'], 'different')
        self.assertEqual(item['target_name'], 'items')
        self.assertEqual([(c['part'], c['change']) for c in item['changes']],
                         [('name', 'different')])
        removed = [i['name'] for i in report['objects'] if i['status'] == 'only_in_target']
        self.assertNotIn('items', removed)
        # The sync cannot rename by letter case.
        skipped = {(s['type'], s['name']) for s in report['sync']['skipped']}
        self.assertIn(('table', 'items'), skipped)


class TestFormat(unittest.TestCase):
    def test_summary_text(self):
        target = snap('mysql-8.4', 'before')
        identical = schema_versions.compare(target, target)
        self.assertEqual(schema_versions.summary_text(identical),
                         'No differences: 14 objects compared.')
        changed = schema_versions.compare(snap('mysql-8.4', 'after'), target)
        self.assertEqual(schema_versions.summary_text(changed),
                         '10 different, 4 only in source, 2 only in target.')

    def test_format_text_lists_changed_objects_and_the_sync(self):
        source, target = snap('mysql-8.4', 'after'), snap('mysql-8.4', 'before')
        comparison = schema_versions.compare(source, target, sync=False)
        report = schema_versions.compare(source, target, chosen_keys=[
            '%s:%s' % (schema_versions.NAMESPACE.get(item['type'], item['type']), item['name'])
            for item in comparison['objects']])
        text = schema_versions.format_text(report)
        self.assertIn('table customers', text)
        self.assertIn('view v_orders', text)
        self.assertIn('procedure p_touch', text)
        self.assertIn('trigger trg_bi0', text)
        self.assertIn('Sync script: 36 statements that would change Target to '
                      'match Source', text)
        self.assertIn('column status at position 2 in source', text)

    def test_format_markdown_has_a_sql_block_with_the_script(self):
        report = schema_versions.compare(snap('mysql-8.4', 'after'),
                                         snap('mysql-8.4', 'before'))
        markdown = schema_versions.format_markdown(report)
        self.assertIn('### table `customers`', markdown)
        self.assertIn('```sql', markdown)
        self.assertIn('ALTER TABLE `customers`', markdown)
        self.assertIn('myflames does not run them', markdown)

    def test_identical_sides_say_no_differences(self):
        target = snap('mysql-8.4', 'before')
        report = schema_versions.compare(target, target)
        self.assertIn('No differences', schema_versions.format_text(report))
        self.assertIn('No differences', schema_versions.format_markdown(report))
        self.assertNotIn('```sql', schema_versions.format_markdown(report))


class TestSourceTargetContract(unittest.TestCase):
    def test_direction_and_diff_are_from_target_to_source(self):
        source, target = snap('mysql-8.4', 'after'), snap('mysql-8.4', 'before')
        report = schema_versions.compare(source=source, target=target,
                                         source_meta={'label': 'desired'}, target_meta={'label': 'deployed'})
        self.assertEqual(list(report)[2:4], ['source', 'target'])
        self.assertEqual(tuple(report['summary']), schema_versions.STATUSES + ('definer_only',))
        self.assertEqual(schema_versions.STATUSES,
                         ('different', 'only_in_source', 'only_in_target', 'not_compared', 'identical'))
        customer = entry(report, 'table', 'customers')
        self.assertIn(('column', 'phone', 'only_in_source'), changes(report, 'table', 'customers'))
        self.assertIn(('column', 'score', 'only_in_target'), changes(report, 'table', 'customers'))
        self.assertTrue(any(op == '+' and '`phone`' in line for op, line in customer['diff']))
        self.assertTrue(any(op == '-' and '`score`' in line for op, line in customer['diff']))
        self.assertEqual(report['sync']['database'], target['database'])
        self.assertEqual(report['sync']['statements'][0]['sql'], 'USE `sv_m`')
        self.assertEqual(report['sync']['script'].splitlines()[:3],
                         ['-- myflames schema sync', '-- Changes the target: deployed',
                          '-- To match the source: desired'])
        text = schema_versions.format_text(report)
        self.assertLess(text.index('Source: desired'), text.index('Target: deployed'))
        markdown = schema_versions.format_markdown(report)
        self.assertLess(markdown.index('**Source:** desired'), markdown.index('**Target:** deployed'))
        self.assertIn('## Sync script', markdown)
        self.assertNotIn('sync', schema_versions.compare(source, target, sync=False))

    def test_change_text_names_sides_and_source_positions(self):
        cases = [
            ({'part': 'column', 'name': 'phone', 'change': 'only_in_source',
              'source': '`phone` varchar(20) DEFAULT NULL'},
             'column phone only in source: `phone` varchar(20) DEFAULT NULL'),
            ({'part': 'column', 'name': 'score', 'change': 'only_in_target',
              'target': "`score` decimal(10,2) DEFAULT '0.00'"},
             "column score only in target: `score` decimal(10,2) DEFAULT '0.00'"),
            ({'part': 'column', 'name': 'email', 'change': 'different',
              'source': '`email` varchar(255) NOT NULL', 'target': '`email` varchar(100) NOT NULL'},
             'column email: source `email` varchar(255) NOT NULL; target `email` varchar(100) NOT NULL'),
            ({'part': 'column', 'name': 'status', 'change': 'moved', 'position': 1},
             'column status at position 2 in source'),
            ({'part': 'definition', 'name': None, 'change': 'different'}, 'definition differs'),
            ({'part': 'partitioning', 'name': None, 'change': 'different', 'source': 'a\nb', 'target': 'c'},
             'partitioning differs'),
        ]
        for change, expected in cases:
            with self.subTest(change=change):
                self.assertEqual(schema_versions.change_text(change), expected)
        # The workspace's Element column: what each change is about.
        self.assertEqual([schema_versions.change_label(c) for c, _ in cases],
                         ['column phone', 'column score', 'column email', 'column status', 'definition', 'partitioning'])
        self.assertEqual(schema_versions.change_label({'part': 'options', 'name': None}, 'database'), 'database options')
        change = dict(cases[2][0], moved=True, position=2,
                      collation={'source': ['utf8mb4', 'utf8mb4_bin'], 'target': ['latin1', 'latin1_bin']})
        self.assertEqual(schema_versions.change_text(change), cases[2][1] +
                         ' (character set and collation: source utf8mb4/utf8mb4_bin; target latin1/latin1_bin)'
                         ', at position 3 in source')

    def test_options_only_name_differing_values(self):
        cases = [
            ("ENGINE=InnoDB COMMENT='Customer accounts'", "ENGINE=InnoDB COMMENT='Customers'",
             "table options: source COMMENT='Customer accounts'; target COMMENT='Customers'"),
            ('ENGINE=InnoDB', '', 'table options: ENGINE=InnoDB only in source'),
            ('', 'ENGINE=InnoDB', 'table options: ENGINE=InnoDB only in target'),
            ("COMMENT='x' ENGINE=InnoDB", "ENGINE=InnoDB COMMENT='x'", 'table options: reordered'),
        ]
        for source, target, expected in cases:
            with self.subTest(source=source, target=target):
                self.assertEqual(schema_versions.change_text({'part': 'options', 'name': None,
                                 'change': 'different', 'source': source, 'target': target}, 'table'), expected)

    def test_entry_summary_uses_side_labels(self):
        changes_ = [{'part': part, 'change': change} for part, change in
                    [('column', 'different'), ('column', 'moved'), ('column', 'only_in_source'),
                     ('column', 'only_in_target'), ('index', 'only_in_source'), ('options', 'different')]]
        self.assertEqual(schema_versions.entry_summary({'type': 'table', 'changes': changes_}),
                         '1 column different, 1 column moved, 1 column only in source, '
                         '1 column only in target, 1 key only in source, table options differ')


class TestComparisonKeysAndRisks(unittest.TestCase):
    """Risk facts describe the target-to-source change, even before selection."""

    def table_report(self, target_lines, source_lines, server='mysql-8.4'):
        def snapshot(lines):
            value = snap(server, 'before')
            database = next(item for item in value['objects'] if item['type'] == 'database')
            value['objects'] = [database, {
                'type': 'table', 'name': 'risk_table', 'columns': {},
                'ddl': 'CREATE TABLE `risk_table` (\n  %s\n) ENGINE=InnoDB' % ',\n  '.join(lines)}]
            return schema_versions.validate_snapshot(value)
        return schema_versions.compare(snapshot(source_lines), snapshot(target_lines))

    def assert_shape(self, risks):
        self.assertEqual(set(risks), {'destructive', 'data_dependent', 'rebuild'})
        self.assertIsNone(risks['rebuild'])
        for kind in ('destructive', 'data_dependent'):
            self.assertIsInstance(risks[kind], list)
            for sentence in risks[kind]:
                self.assertIsInstance(sentence, str)
                self.assertTrue(sentence.strip())
                self.assertNotIn('\n', sentence)

    def test_keys_are_namespaced_unique_and_independent_of_sync_selection(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                source, target = snap(server, 'after'), snap(server, 'before')
                report = schema_versions.compare(source, target, sync=False)
                selected = schema_versions.compare(source, target, chosen_keys=[])['objects']
                keys = [item['key'] for item in report['objects']]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertEqual(keys, [item['key'] for item in selected])
                for item in report['objects']:
                    self.assertEqual(item['key'], '%s:%s' % (
                        schema_versions.NAMESPACE.get(item['type'], item['type']), item['name']))
                self.assertIn('relation:customers', keys)
                self.assertIn('relation:v_orders', keys)
                if server == 'mariadb-11.4':
                    self.assertIn('relation:seq_ids', keys)
                    self.assertIn('package:pkg_util', keys)
                    self.assertIn('package_body:pkg_util', keys)

    def test_all_entries_and_changes_have_roundtrippable_risk_details(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = schema_versions.compare(snap(server, 'after'), snap(server, 'before'), sync=False)
                self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)
                for item in report['objects']:
                    self.assert_shape(item['risks'])
                    for change in item.get('changes', []):
                        self.assert_shape(change['risks'])
                        for kind in ('destructive', 'data_dependent'):
                            self.assertTrue(set(change['risks'][kind]).issubset(item['risks'][kind]))

    def test_dropped_column_is_destructive_on_entry_change_and_statement(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                report = self.table_report(['`id` int NOT NULL', '`old_value` int'],
                                           ['`id` int NOT NULL'], server)
                item = entry(report, 'table', 'risk_table')
                change = next(c for c in item['changes'] if c['name'] == 'old_value')
                risks = change['risks']['destructive']
                self.assertTrue(risks)
                self.assertTrue(any('old_value' in sentence for sentence in risks))
                for sentence in risks:
                    self.assertIn(sentence, item['risks']['destructive'])
                    self.assertIn('-- WARNING: ' + sentence, report['sync']['script'])
                self.assertEqual(item['risks']['data_dependent'], [])

    def test_primary_unique_foreign_and_check_additions_require_data_checks(self):
        additions = [
            ('index', 'PRIMARY KEY (`id`)'),
            ('index', 'UNIQUE KEY `u_value` (`value`)'),
            ('foreign_key', 'CONSTRAINT `fk_parent` FOREIGN KEY (`value`) REFERENCES `parent` (`id`)'),
            ('check', 'CONSTRAINT `positive` CHECK ((`value` > 0))'),
        ]
        columns = ['`id` int NOT NULL', '`value` int DEFAULT NULL']
        for server in replay.SERVERS:
            for part, clause in additions:
                with self.subTest(server=server, clause=clause):
                    report = self.table_report(columns, columns + [clause], server)
                    item = entry(report, 'table', 'risk_table')
                    change = next(c for c in item['changes'] if c['part'] == part)
                    self.assertTrue(change['risks']['data_dependent'])
                    self.assertEqual(change['risks']['destructive'], [])
                    for sentence in change['risks']['data_dependent']:
                        self.assertIn(sentence, item['risks']['data_dependent'])
                        self.assertIn('-- CHECK: ' + sentence, report['sync']['script'])

    def test_narrowing_and_nullability_require_data_checks(self):
        cases = [
            ('int DEFAULT NULL', 'int NOT NULL'),
            ('bigint NOT NULL', 'int NOT NULL'),
            ('int NOT NULL', 'smallint NOT NULL'),
            ('varchar(100) DEFAULT NULL', 'varchar(20) DEFAULT NULL'),
            ('decimal(10,2) NOT NULL', 'decimal(8,2) NOT NULL'),
            # A larger floating-point range still loses negative values when
            # UNSIGNED is added (mysql-server sql/field.cc:Field_real::truncate).
            ('float NOT NULL', 'double unsigned NOT NULL'),
            ("enum('A') DEFAULT NULL", "enum('a') DEFAULT NULL"),
        ]
        for server in replay.SERVERS:
            for before, after in cases:
                with self.subTest(server=server, before=before, after=after):
                    report = self.table_report(['`value` ' + before], ['`value` ' + after], server)
                    item = entry(report, 'table', 'risk_table')
                    change = next(c for c in item['changes'] if c['name'] == 'value')
                    self.assertTrue(change['risks']['data_dependent'])
                    self.assertEqual(change['risks']['destructive'], [])
                    self.assertTrue(any('value' in sentence for sentence in change['risks']['data_dependent']))

    def test_widening_comments_and_literal_keywords_are_not_narrowing(self):
        cases = [
            (['`value` int NOT NULL'], ['`value` bigint NOT NULL']),
            (['`value` varchar(20) DEFAULT NULL'], ['`value` varchar(100) DEFAULT NULL']),
            (['`value` decimal(8,2) NOT NULL'], ['`value` decimal(10,2) NOT NULL']),
            (['`value` float NOT NULL'], ['`value` double NOT NULL']),
            (['`value` float unsigned NOT NULL'], ['`value` double NOT NULL']),
            (["`value` enum('A') DEFAULT NULL"], ["`value` enum('A','B') DEFAULT NULL"]),
            (["`value` varchar(100) DEFAULT NULL COMMENT 'old'"],
             ["`value` varchar(100) DEFAULT NULL COMMENT 'NOT NULL UNIQUE CHECK DROP COLUMN bigint varchar(1)'"]),
            (["`value` varchar(100) DEFAULT 'old'"],
             ["`value` varchar(100) DEFAULT 'NOT NULL UNIQUE CHECK DROP COLUMN'"]),
            (['`value` int DEFAULT NULL'], ['`value` int DEFAULT NULL /* NOT NULL */']),
            (['`value` int DEFAULT NULL'], ['`value` int DEFAULT NULL # NOT NULL']),
            (['`value` int DEFAULT NULL'], ['`value` int DEFAULT NULL -- NOT NULL']),
            (['`value` int'], ['`value` int', "KEY `plain` (`value`) COMMENT 'UNIQUE PRIMARY KEY'"]),
            (["`value` enum('NOT NULL','UNIQUE') DEFAULT NULL"],
             ["`value` enum('NOT NULL','UNIQUE','CHECK') DEFAULT NULL"]),
        ]
        for server in replay.SERVERS:
            for before, after in cases:
                with self.subTest(server=server, before=before, after=after):
                    report = self.table_report(before, after, server)
                    item = entry(report, 'table', 'risk_table')
                    self.assertEqual(item['risks']['destructive'], [])
                    self.assertEqual(item['risks']['data_dependent'], [])
                    self.assertEqual(report['sync']['summary']['warnings'], 0)

    def test_removing_any_object_is_destructive_even_when_default_omits_it(self):
        for server in replay.SERVERS:
            target = snap(server, 'before')
            source = copy.deepcopy(target)
            source['objects'] = [item for item in source['objects'] if item['type'] == 'database']
            report = schema_versions.compare(schema_versions.validate_snapshot(source), target)
            with self.subTest(server=server):
                self.assertEqual(report['sync']['statements'], [])
                for item in report['objects']:
                    if item['type'] != 'database':
                        self.assertEqual(item['status'], 'only_in_target')
                        self.assertTrue(item['risks']['destructive'], item)

    def test_relation_type_replacement_keeps_key_and_warns_about_drop(self):
        for server in replay.SERVERS:
            target = snap(server, 'before')
            target['objects'] = [item for item in target['objects']
                                 if item['type'] == 'database' or item['name'] == 'notes']
            source = copy.deepcopy(target)
            source['objects'][-1] = {'type': 'view', 'name': 'notes',
                                    'ddl': 'CREATE VIEW `notes` AS SELECT 1 AS `id`'}
            report = schema_versions.compare(schema_versions.validate_snapshot(source),
                                             schema_versions.validate_snapshot(target))
            with self.subTest(server=server):
                item = entry(report, 'view', 'notes')
                self.assertEqual(item['key'], 'relation:notes')
                self.assertTrue(item['risks']['destructive'])
                type_change = next(c for c in item['changes'] if c['part'] == 'type')
                self.assertTrue(type_change['risks']['destructive'])
                for sentence in item['risks']['destructive']:
                    self.assertIn('-- WARNING: ' + sentence, report['sync']['script'])

    def test_mysql_routine_recreation_is_destructive(self):
        report = schema_versions.compare(snap('mysql-8.4', 'after'), snap('mysql-8.4', 'before'),
                                         chosen_keys=['procedure:p_touch'])
        item = entry(report, 'procedure', 'p_touch')
        self.assertTrue(item['risks']['destructive'])
        drop = next(s for s in report['sync']['statements'] if s['sql'].startswith('DROP PROCEDURE'))
        self.assertEqual(drop['risks'], ['drops_privileges'])
        for sentence in item['risks']['destructive']:
            self.assertIn(sentence, drop['risk_details']['destructive'])
            self.assertIn('-- WARNING: ' + sentence, report['sync']['script'])


class TestStore(IsolatedStoreTest):
    def setUp(self):
        super().setUp()
        self.store = WorkspaceStore()
        self.target = snap('mysql-8.4', 'before')
        self.source = snap('mysql-8.4', 'after')

    def test_save_numbers_versions_per_source(self):
        first = self.store.schema_version_save(self.target, 'first')
        second = self.store.schema_version_save(self.target)
        self.assertEqual(first['number'], 1)
        self.assertEqual(second['number'], 2)
        self.assertEqual(first['source_key'], '127.0.0.1:33584/sv_m')
        other = copy.deepcopy(replay.load('mysql-8.4-before'))
        other['database'] = 'sv_other'
        other['objects'][0]['name'] = 'sv_other'
        third = self.store.schema_version_save(
            schema_versions.validate_snapshot(other))
        self.assertEqual(third['number'], 1)
        self.assertEqual(third['source_key'], '127.0.0.1:33584/sv_other')

    def test_same_as_set_only_when_checksum_matches_latest(self):
        first = self.store.schema_version_save(self.target, 'first')
        same = self.store.schema_version_save(self.target)
        self.assertEqual(same['same_as'],
                         {'id': first['id'], 'number': 1, 'name': 'first'})
        self.assertIsNone(first['same_as'])
        different = self.store.schema_version_save(self.source)
        self.assertIsNone(different['same_as'])

    def test_list_is_newest_first_and_never_holds_a_snapshot(self):
        self.store.schema_version_save(self.target, 'one')
        self.store.schema_version_save(self.target, 'two')
        result = self.store.schema_version_list(
            source='127.0.0.1:33584/sv_m')
        self.assertEqual(result['schema_version'], 'schema-version-list-1.0')
        self.assertEqual([item['name'] for item in result['items']], ['two', 'one'])
        for item in result['items']:
            self.assertNotIn('snapshot', item)

    def test_get_returns_the_validated_snapshot(self):
        saved = self.store.schema_version_save(self.target, 'keep')
        item = self.store.schema_version_get(saved['id'])
        self.assertEqual(item['snapshot']['checksum'], self.target['checksum'])
        self.assertEqual(item['name'], 'keep')
        self.assertIsNone(self.store.schema_version_get('f' * 32))

    def test_update_renames_and_rejects_bad_names(self):
        saved = self.store.schema_version_save(self.target, 'old')
        renamed = self.store.schema_version_update(saved['id'], 'new')
        self.assertEqual(renamed['name'], 'new')
        for bad in ('two\nlines', 'x' * 201):
            with self.assertRaisesRegex(ValueError, 'name'):
                self.store.schema_version_update(saved['id'], bad)

    def test_delete(self):
        saved = self.store.schema_version_save(self.target)
        self.assertEqual(self.store.schema_version_delete(saved['id']),
                         {'deleted': True})
        self.assertIsNone(self.store.schema_version_get(saved['id']))

    def test_find_by_id_prefix_name_and_latest(self):
        first = self.store.schema_version_save(self.target, 'named')
        second = self.store.schema_version_save(self.target, 'other')
        self.assertEqual(self.store.schema_version_find(first['id'])['id'],
                         first['id'])
        self.assertEqual(self.store.schema_version_find(first['id'][:6])['id'],
                         first['id'])
        self.assertEqual(self.store.schema_version_find('named')['id'], first['id'])
        latest = self.store.schema_version_find('latest', source=first['source_key'])
        self.assertEqual(latest['id'], second['id'])
        # A name shared by two versions is ambiguous; an unknown name and an
        # unknown ID prefix match nothing.
        self.store.schema_version_update(second['id'], 'named')
        with self.assertRaisesRegex(ValueError, 'match'):
            self.store.schema_version_find('named')
        with self.assertRaisesRegex(ValueError, 'match'):
            self.store.schema_version_find('no-such-version')
        with self.assertRaisesRegex(ValueError, 'match'):
            self.store.schema_version_find(first['id'][:6] + 'ffff')

    def test_file_is_owner_only_on_posix(self):
        self.store.schema_version_save(self.target)
        if os.name == 'posix':
            self.assertEqual(os.stat(self.store.path).st_mode & 0o077, 0)


class TestSshTarget(IsolatedStoreTest):
    """A read through an SSH tunnel records its SSH target in the snapshot's
    server: 127.0.0.1:3306 behind SSH host A is another server than behind
    SSH host B or read directly (its history, label and live matching)."""

    def setUp(self):
        super().setUp()
        self.record = replay.recording('mysql-8.4', 'before')

        class Workspace(object):
            store = WorkspaceStore()
        self.workspace = Workspace()
        patcher = mock.patch('myflames.schema_versions_api.MySQLConnection',
                             side_effect=lambda **options: replay.ReplayConn(self.record))
        patcher.start()
        self.addCleanup(patcher.stop)

    def through(self, **ssh):
        snapshot = copy.deepcopy(replay.load('mysql-8.4-before'))
        snapshot['server'].update(ssh)
        return schema_versions.validate_snapshot(snapshot)

    def test_capture_records_the_ssh_target_only_through_ssh(self):
        direct = schema_versions.capture(replay.ReplayConn(self.record), self.record['database'],
                                         self.record['source'])
        self.assertFalse(set(schema_versions.SSH_FIELDS) & set(direct['server']), 'a direct read is unchanged')
        source = dict(self.record['source'], ssh_host='bastion', ssh_user='deploy', ssh_port=2222)
        through = schema_versions.capture(replay.ReplayConn(self.record), self.record['database'], source)
        self.assertEqual({key: through['server'][key] for key in schema_versions.SSH_FIELDS},
                         {'ssh_host': 'bastion', 'ssh_user': 'deploy', 'ssh_port': 2222})
        self.assertEqual(through['checksum'], direct['checksum'], 'the definitions are compared, not the route')
        self.assertEqual(schema_versions.validate_snapshot(through)['server'], through['server'])

    def test_read_source_carries_the_ssh_fields_of_the_options(self):
        from myflames.live import connection_options
        options = connection_options({'host': '127.0.0.1', 'port': 3306, 'user': 'app',
                                      'ssh_host': ' bastion ', 'ssh_port': 2222})
        self.assertEqual(schema_versions.read_source(options),
                         {'host': '127.0.0.1', 'port': 3306, 'ssh_host': 'bastion', 'ssh_port': 2222})
        direct = connection_options({'host': '127.0.0.1', 'port': 3306, 'user': 'app'})
        self.assertEqual(schema_versions.read_source(direct), {'host': '127.0.0.1', 'port': 3306})

    def test_validation_keeps_and_checks_the_ssh_target(self):
        kept = self.through(ssh_host='bastion', ssh_user='deploy', ssh_port=2222)
        self.assertEqual((kept['server']['ssh_host'], kept['server']['ssh_user'], kept['server']['ssh_port']),
                         ('bastion', 'deploy', 2222))
        for bad in ({'ssh_host': '-oProxyCommand=x'}, {'ssh_user': 'deploy'}, {'ssh_host': ''},
                    {'ssh_host': 'bastion', 'ssh_port': 70000}, {'ssh_host': 'bastion', 'ssh_port': True},
                    {'ssh_host': 'bastion', 'ssh_port': '22'}, {'ssh_host': ['bastion']}):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'Invalid SSH target'):
                self.through(**bad)

    def test_source_key_and_side_label_name_the_ssh_target(self):
        direct = replay.snapshot('mysql-8.4', 'before')
        self.assertEqual(schema_versions.source_key(direct), '127.0.0.1:33584/sv_m', 'unchanged without SSH')
        through = self.through(ssh_host='bastion', ssh_user='deploy', ssh_port=2222)
        self.assertEqual(schema_versions.source_key(through), '127.0.0.1:33584 via deploy@bastion:2222/sv_m')
        self.assertEqual(schema_versions.source_key(self.through(ssh_host='bastion')), '127.0.0.1:33584 via bastion/sv_m')
        self.assertIn('sv_m on 127.0.0.1:33584 via deploy@bastion:2222',
                      schema_versions.describe_side(through, {'kind': 'live'})['label'])
        self.assertIn('at 127.0.0.1:33584 via deploy@bastion:2222,', schema_versions.format_snapshot_text(through))

    def test_two_ssh_hosts_keep_two_histories_and_latest_matches_the_route(self):
        store = self.workspace.store
        a = store.schema_version_save(self.through(ssh_host='bastion-a'))
        b = store.schema_version_save(self.through(ssh_host='bastion-b'))
        direct = store.schema_version_save(replay.snapshot('mysql-8.4', 'before'))
        self.assertEqual([a['number'], b['number'], direct['number']], [1, 1, 1])
        self.assertEqual(len({a['source_key'], b['source_key'], direct['source_key']}), 3)
        latest = store.schema_version_find('latest', source=schema_versions.source_key(self.through(ssh_host='bastion-a')))
        self.assertEqual(latest['id'], a['id'])

    def test_api_reads_through_ssh_record_their_target(self):
        connection = {'host': '127.0.0.1', 'port': 33584, 'user': 'root', 'ssh_host': 'bastion-a'}
        saved = ui.api_response('/api/schema-versions/save', {'connection': connection, 'database': 'sv_m',
                                                              'name': '', 'note': ''}, self.workspace)
        self.assertEqual(saved['server']['ssh_host'], 'bastion-a')
        self.assertEqual(saved['source_key'], '127.0.0.1:33584 via bastion-a/sv_m')
        report = ui.api_response('/api/schema-versions/compare', {
            'source': {'version': saved['id']},
            'target': {'live': {'connection': dict(connection, ssh_host='bastion-b'), 'database': 'sv_m'}}},
            self.workspace)
        self.assertEqual(report['source']['server']['ssh_host'], 'bastion-a')
        self.assertEqual(report['target']['server']['ssh_host'], 'bastion-b')
        self.assertIn('via bastion-b', report['target']['label'])


class TestApi(IsolatedStoreTest):
    def setUp(self):
        super().setUp()

        class Workspace(object):
            store = WorkspaceStore()
        self.workspace = Workspace()
        self.record = replay.recording('mysql-8.4', 'before')
        patcher = mock.patch('myflames.schema_versions_api.MySQLConnection',
                             side_effect=lambda **options: replay.ReplayConn(self.record))
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, path, payload):
        return ui.api_response(path, payload, self.workspace)

    def save_target(self, name=''):
        return self.call('/api/schema-versions/save',
                         {'connection': {'host': '127.0.0.1', 'port': 33584,
                                         'user': 'root'},
                          'database': 'sv_m', 'name': name, 'note': 'n'})

    def test_paths_are_registered_in_the_ui_allowlist(self):
        # ui.py imports PATHS as _SCHEMA_VERSION_PATHS and adds that name to
        # the do_POST allowlist expression; the concrete path strings live in
        # schema_versions_api.py, so the alias must stand for exactly PATHS.
        self.assertEqual(set(schema_versions_api.PATHS), set(ui._SCHEMA_VERSION_PATHS))
        self.assertIn('/api/schema-versions/sync', schema_versions_api.PATHS)
        self.assertNotIn('/api/schema-versions/sync', schema_versions_api.LIVE_PATHS)
        source = inspect.getsource(ui)
        self.assertIn('_SCHEMA_VERSION_PATHS', source)
        post = source[source.index('def do_POST'):]
        self.assertIn('_SCHEMA_VERSION_PATHS', post)

    def test_list_save_get_update_export_import_delete(self):
        saved = self.save_target('v one')
        self.assertEqual(saved['number'], 1)
        self.assertEqual(saved['database'], 'sv_m')
        listed = self.call('/api/schema-versions/list', {})
        self.assertEqual([item['id'] for item in listed['items']], [saved['id']])
        by_source = self.call('/api/schema-versions/list',
                              {'source_key': saved['source_key']})
        self.assertEqual(len(by_source['items']), 1)
        got = self.call('/api/schema-versions/get', {'id': saved['id']})
        self.assertEqual(got['snapshot']['checksum'],
                         replay.load('mysql-8.4-before')['checksum'])
        renamed = self.call('/api/schema-versions/update',
                            {'id': saved['id'], 'name': 'renamed'})
        self.assertEqual(renamed['name'], 'renamed')
        exported = self.call('/api/schema-versions/export', {'id': saved['id']})
        self.assertEqual(exported['filename'], 'schema-sv_m-v1.json')
        parsed = schema_versions.parse_file(exported['text'])
        self.assertEqual(parsed['checksum'], got['snapshot']['checksum'])
        self.assertEqual(parsed['name'], 'renamed')
        imported = self.call('/api/schema-versions/import',
                             {'text': exported['text']})
        self.assertEqual(imported['number'], 2)
        self.assertEqual(imported['name'], 'renamed')
        self.assertEqual(self.call('/api/schema-versions/delete',
                                   {'id': saved['id']}), {'deleted': True})
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.call('/api/schema-versions/get', {'id': saved['id']})

    def test_compare_two_saved_versions(self):
        saved = self.save_target()
        report = self.call('/api/schema-versions/compare',
                           {'target': {'version': saved['id']},
                            'source': {'version': saved['id']}})
        self.assertTrue(report['identical'])
        self.assertEqual(report['schema_version'], 'schema-compare-1.0')
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.call('/api/schema-versions/compare',
                      {'target': {'version': 'f' * 32},
                       'source': {'version': saved['id']}})

    def test_compare_against_a_live_read(self):
        saved = self.save_target()
        report = self.call('/api/schema-versions/compare',
                           {'target': {'version': saved['id']},
                            'source': {'live': {'connection': {
                                'host': '127.0.0.1', 'port': 33584,
                                'user': 'root'}, 'database': 'sv_m'}}})
        self.assertTrue(report['identical'])
        self.assertEqual(report['source']['kind'], 'live')

    def test_rejects_a_side_that_is_neither_version_nor_live(self):
        saved = self.save_target()
        with self.assertRaisesRegex(ValueError, 'saved version or a live database'):
            self.call('/api/schema-versions/compare',
                      {'target': {'version': saved['id']}, 'source': {'other': 1}})

    def test_compare_requires_source_and_target_payload_keys(self):
        saved = self.save_target()
        for payload in ({}, {'source': {'version': saved['id']}},
                        {'target': {'version': saved['id']}},
                        {'before': {'version': saved['id']}, 'after': {'version': saved['id']}}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, r'Choose what to compare as source and target\.'):
                    self.call('/api/schema-versions/compare', payload)

    def test_rejects_ignore_definers_that_is_not_a_bool(self):
        saved = self.save_target()
        with self.assertRaisesRegex(ValueError, 'ignore_definers'):
            self.call('/api/schema-versions/compare',
                      {'ignore_definers': 'yes',
                       'target': {'version': saved['id']},
                       'source': {'version': saved['id']}})

    def test_sync_regenerates_cached_comparison_without_reading_sides(self):
        target = self.save_target()
        source = self.workspace.store.schema_version_save(snap('mysql-8.4', 'after'))
        report = self.call('/api/schema-versions/compare', {
            'target': {'version': target['id']}, 'source': {'version': source['id']}})
        comparison_id = report['comparison_id']
        self.assertIsInstance(comparison_id, str)
        self.assertTrue(comparison_id)
        with mock.patch('myflames.schema_versions_api.MySQLConnection') as connect, \
                mock.patch.object(self.workspace.store, 'schema_version_get') as read_saved:
            selected = self.call('/api/schema-versions/sync', {
                'comparison_id': comparison_id, 'chosen_keys': ['relation:legacy']})
            empty = self.call('/api/schema-versions/sync', {
                'comparison_id': comparison_id, 'chosen_keys': []})
        connect.assert_not_called()
        read_saved.assert_not_called()
        self.assertEqual(selected['comparison_id'], comparison_id)
        # Rebuild judgments describe emitted ALTERs, so a new selection clears
        # only those judgments while preserving every captured difference.
        expected = copy.deepcopy(report['objects'])
        for entry in expected:
            entry['risks']['rebuild'] = None
            for change in entry.get('changes', []):
                change['risks']['rebuild'] = None
        self.assertEqual(selected['objects'], expected)
        self.assertEqual(selected['sync']['chosen_keys'], ['relation:legacy'])
        self.assertIn('DROP TABLE `legacy`', selected['sync']['script'])
        self.assertEqual(empty['sync']['statements'], [])
        self.assertEqual(empty['objects'], expected)
        self.assertNotIn('DROP TABLE `legacy`', report['sync']['script'])

    def test_sync_uses_cached_live_read_after_saved_version_is_deleted(self):
        source = self.workspace.store.schema_version_save(snap('mysql-8.4', 'after'))
        report = self.call('/api/schema-versions/compare', {
            'source': {'version': source['id']},
            'target': {'live': {'connection': {'host': '127.0.0.1', 'port': 33584, 'user': 'root'},
                                'database': 'sv_m'}}})
        self.workspace.store.schema_version_delete(source['id'])
        with mock.patch('myflames.schema_versions_api.MySQLConnection') as connect:
            selected = self.call('/api/schema-versions/sync', {
                'comparison_id': report['comparison_id'], 'chosen_keys': ['relation:notes']})
        connect.assert_not_called()
        self.assertEqual(selected['target'], report['target'])
        self.assertEqual(selected['source'], report['source'])
        self.assertIn('ALTER TABLE `notes`', selected['sync']['script'])

    def test_sync_refuses_unknown_and_invalid_comparison_ids(self):
        for comparison_id in (None, '', 'missing', 1, [], {}):
            with self.subTest(comparison_id=comparison_id):
                with self.assertRaises(ValueError):
                    self.call('/api/schema-versions/sync', {
                        'comparison_id': comparison_id, 'chosen_keys': []})

    def test_sync_refuses_keys_absent_from_its_cached_report(self):
        saved = self.save_target()
        report = self.call('/api/schema-versions/compare', {
            'source': {'version': saved['id']}, 'target': {'version': saved['id']}})
        for chosen in (['relation:missing'], ['relation:notes', 'relation:notes'],
                       'relation:notes', [None], [1]):
            with self.subTest(chosen=chosen):
                with self.assertRaises(ValueError):
                    self.call('/api/schema-versions/sync', {
                        'comparison_id': report['comparison_id'], 'chosen_keys': chosen})

    def test_comparison_id_is_scoped_to_its_workspace(self):
        saved = self.save_target()
        report = self.call('/api/schema-versions/compare', {
            'source': {'version': saved['id']}, 'target': {'version': saved['id']}})
        other = type('Workspace', (), {})()
        other.store = WorkspaceStore(os.path.join(self.temp.name, 'other.sqlite3'))
        with self.assertRaises(ValueError):
            ui.api_response('/api/schema-versions/sync', {
                'comparison_id': report['comparison_id'], 'chosen_keys': []}, other)

    def test_sync_expiry_requires_compare_again_without_rereading(self):
        saved = self.save_target()
        with mock.patch.object(schema_versions_api.time, 'monotonic', return_value=100):
            report = self.call('/api/schema-versions/compare', {
                'source': {'version': saved['id']}, 'target': {'version': saved['id']}})
        with mock.patch.object(schema_versions_api.time, 'monotonic', return_value=701), \
                mock.patch('myflames.schema_versions_api.MySQLConnection') as connect:
            with self.assertRaisesRegex(ValueError, 'Compare again'):
                self.call('/api/schema-versions/sync', {
                    'comparison_id': report['comparison_id'], 'chosen_keys': []})
        connect.assert_not_called()

    def test_comparison_cache_evicts_oldest_and_keeps_latest_four(self):
        saved = self.save_target()
        reports = [self.call('/api/schema-versions/compare', {
            'source': {'version': saved['id']}, 'target': {'version': saved['id']}})
            for _ in range(5)]
        with self.assertRaisesRegex(ValueError, 'Compare again'):
            self.call('/api/schema-versions/sync', {
                'comparison_id': reports[0]['comparison_id'], 'chosen_keys': []})
        for report in reports[1:]:
            selected = self.call('/api/schema-versions/sync', {
                'comparison_id': report['comparison_id'], 'chosen_keys': []})
            self.assertEqual(selected['comparison_id'], report['comparison_id'])
            self.assertEqual(selected['sync']['statements'], [])


if __name__ == '__main__':
    unittest.main()
