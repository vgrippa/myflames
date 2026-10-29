"""Import CSV endpoints (myflames/csv_import_api.py and the ui.py wiring)
with a fake server session: policy refusals, Read-only, the review token
against file and metadata changes, privileges, Stop, failures, unknown
outcomes, the audit record (never row data) and the HTTP routes.

No real server: every statement goes to FakeServer, which answers like the
mysql client session would (rows, Query OK with affected rows and warnings,
an error with its number, or no answer). scripts/lab-csv-import.py runs the
same paths against MySQL 8.4 and 9.7 and MariaDB 10.11 and 11.4.
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from email.message import Message
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from myflames import csv_import as ci  # noqa: E402
from myflames import csv_import_api as api  # noqa: E402
from myflames import dbops, privileges, ui  # noqa: E402

ORIGINAL_SEND = api._send
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402
from test_chart_data import validate  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def _schema(name):
    with open(os.path.join(ROOT, 'docs', 'schemas', name), encoding='utf-8') as handle:
        return json.load(handle)


SCHEMAS = {path: _schema(name)
           for path, name in (('/api/import/csv/inspect', 'csv-inspect-1.0.json'),
                              ('/api/import/csv/plan', 'csv-import-plan-1.0.json'),
                              ('/api/import/csv/apply', 'csv-import-result-1.0.json'),
                              ('/api/import/csv/progress', 'csv-import-progress-1.0.json'))}

SECRET_CELL = 'SECRETCELLVALUE'
COLUMNS = [
    {'COLUMN_NAME': 'id', 'DATA_TYPE': 'int', 'COLUMN_TYPE': 'int', 'IS_NULLABLE': 'NO', 'COLUMN_DEFAULT': None,
     'EXTRA': '', 'CHARACTER_SET_NAME': None, 'COLLATION_NAME': None, 'CHARACTER_MAXIMUM_LENGTH': None,
     'NUMERIC_PRECISION': '10', 'NUMERIC_SCALE': '0', 'DATETIME_PRECISION': None},
    {'COLUMN_NAME': 'name', 'DATA_TYPE': 'varchar', 'COLUMN_TYPE': 'varchar(20)', 'IS_NULLABLE': 'YES',
     'COLUMN_DEFAULT': None, 'EXTRA': '', 'CHARACTER_SET_NAME': 'utf8mb4', 'COLLATION_NAME': 'utf8mb4_0900_ai_ci',
     'CHARACTER_MAXIMUM_LENGTH': '20', 'NUMERIC_PRECISION': None, 'NUMERIC_SCALE': None, 'DATETIME_PRECISION': None},
    {'COLUMN_NAME': 'bits', 'DATA_TYPE': 'bit', 'COLUMN_TYPE': 'bit(3)', 'IS_NULLABLE': 'YES',
     'COLUMN_DEFAULT': None, 'EXTRA': '', 'CHARACTER_SET_NAME': None, 'COLLATION_NAME': None,
     'CHARACTER_MAXIMUM_LENGTH': None, 'NUMERIC_PRECISION': '3', 'NUMERIC_SCALE': None, 'DATETIME_PRECISION': None},
]


def rows_entry(rows):
    keys = list(rows[0]) if rows else ['x']
    return {'type': 'rows', 'columns': [{'name': k} for k in keys], 'rows': [[r[k] for k in keys] for r in rows],
            'truncated': False}


class Step(object):
    def __init__(self, entries=None, errors=None):
        self.entries = entries or []
        self.errors = errors or []

    def real_errors(self):
        return list(self.errors)


class FakeServer(object):
    """What one server says, shared by every session the API opens."""

    def __init__(self, engine='mysql'):
        self.engine = engine
        self.version = '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB'
        self.backslash = True
        self.exists = True
        self.schema_exists = True
        self.table_engine = 'InnoDB'
        self.columns = [dict(c) for c in COLUMNS]
        self.triggers = []
        self.local_infile = 'ON'
        self.variables = {'max_allowed_packet': '67108864', 'sql_require_primary_key': 'OFF', 'time_zone': 'SYSTEM',
                          'system_time_zone': 'UTC', 'read_only': 'OFF', 'super_read_only': 'OFF'}
        self.sent = []
        self.sessions = []
        self.table = []
        self.pending = []
        self.loaded = []
        self.warning_rows = []
        self.hook = None
        self.start_error = None
        self.kills = []
        self.session_mode = None

    def writes(self):
        return [s for s in self.sent if re.match(r'^(/\*myflames:import_csv\*/ )?(INSERT|LOAD|CREATE|START|COMMIT|'
                                                 r'ROLLBACK|SET)', s)]

    def answer(self, session, sql, progress=None, options=None):
        self.sent.append(sql)
        if self.hook is not None:
            override = self.hook(sql, self, progress)
            if override is not None:
                if override == 'unknown':
                    session.ended = True
                    return None, 'unknown'
                return Step(errors=[override]), override
        body = sql.replace(ci.TAG, '')
        if body.startswith('SHOW SESSION VARIABLES'):
            return Step([rows_entry([{'Variable_name': k, 'Value': v} for k, v in self.variables.items()])]), None
        if body.startswith('SELECT @@SESSION.sql_mode'):
            mode = self.session_mode if self.session_mode is not None else (
                'STRICT_ALL_TABLES' + ('' if self.backslash else ',NO_BACKSLASH_ESCAPES'))
            return Step([rows_entry([{'sql_mode': mode}])]), None
        if body.startswith('SHOW GLOBAL VARIABLES'):
            return Step([rows_entry([{'Variable_name': 'local_infile', 'Value': self.local_infile}])]), None
        if 'information_schema.TABLES WHERE' in body:
            rows = [{'TABLE_TYPE': 'BASE TABLE', 'ENGINE': self.table_engine, 'TABLE_ROWS': '10'}] if self.exists else []
            return Step([rows_entry(rows)]), None
        if 'information_schema.SCHEMATA' in body:
            return Step([rows_entry([{'SCHEMA_NAME': 'db'}] if self.schema_exists else [])]), None
        if 'information_schema.COLUMNS' in body:
            return Step([rows_entry(self.columns)]), None
        if 'information_schema.STATISTICS' in body:
            return Step([rows_entry([{'COLUMN_NAME': 'id'}])]), None
        if 'information_schema.TRIGGERS' in body:
            return Step([rows_entry([{'TRIGGER_NAME': t} for t in self.triggers])]), None
        if 'REFERENTIAL_CONSTRAINTS' in body:
            return Step([rows_entry([{'n': '0'}])]), None
        if 'CHECK_CONSTRAINTS' in body:
            return Step([rows_entry([])]), None
        if body.startswith('SHOW WARNINGS'):
            return Step([rows_entry(self.warning_rows)]), None
        if body.startswith('SELECT COUNT(*)'):
            return Step([rows_entry([{'n': str(len(self.table))}])]), None
        if body.startswith('INSERT INTO'):
            values = body.split(' VALUES ', 1)[1]
            count = values.count('), (') + 1
            self.pending.extend([body] * count)
            return Step([{'type': 'ok', 'affected_rows': count, 'warnings': 0, 'info': None}]), None
        if body.startswith('LOAD DATA'):
            path = re.search(r"INFILE '([^']*)'", body).group(1)
            with open(path, 'rb') as handle:
                data = handle.read()
            self.loaded.append(data)
            count = data.count(b'\n')
            self.pending.extend([path] * count)
            return Step([{'type': 'ok', 'affected_rows': count - getattr(self, 'load_short', 0),
                          'warnings': len(self.warning_rows), 'info': None}]), None
        if body.startswith('COMMIT'):
            self.table.extend(self.pending)
            self.pending = []
        if body.startswith('ROLLBACK'):
            self.pending = []
        return Step([{'type': 'ok', 'affected_rows': 0, 'warnings': 0, 'info': None}]), None


class FakeSession(object):
    def __init__(self, server, load_dir):
        self.server = server
        self.engine = server.engine
        self.server_version = server.version
        self.server_product = 'MySQL' if server.engine == 'mysql' else 'MariaDB'
        self.backslash_escapes = server.backslash
        self.client_flags = ('--local-infile=0', '--load-data-local-dir=' + load_dir) if load_dir else ()
        self.ended = False
        self._conn = None

    def end(self, *args, **kwargs):
        self.ended = True

    def _kill_query(self, options, step):
        self.server.kills.append(options.get('password'))
        return 'sent', None


def model(grants=None):
    return privileges.parse_grants(grants or ['GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION'],
                                   'mysql', '8.4.11')


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': self.home.name, 'USERPROFILE': self.home.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.ws = SimpleNamespace(store=WorkspaceStore())
        self.server = FakeServer()
        self.opened = []

        def open_session(full, identity, safety, label, load_dir=None):
            if self.server.start_error:
                raise ConnectorError(self.server.start_error)
            session = FakeSession(self.server, load_dir)
            self.opened.append((full, load_dir))
            return session

        patches = [mock.patch.object(api, '_open_session', open_session),
                   mock.patch.object(api, '_send', lambda session, sql, timeout=None, progress=None, options=None:
                                     session.server.answer(session, sql, progress, options)),
                   mock.patch.object(api, '_actor', lambda session: self.actor),
                   # The local client's --help is not read in these tests.
                   mock.patch.object(api, 'limits_local_files', lambda binary: self.limits),
                   mock.patch.object(api, 'client_binary', lambda full: '/usr/bin/mysql'),
                   # Checked copies go to this test's folder; the real temporary
                   # folder is neither written nor swept.
                   mock.patch.object(api, 'chunk_folder', lambda: tempfile.mkdtemp(
                       prefix='myflames-csv-%d-' % os.getpid(), dir=self.home.name)),
                   mock.patch.object(api, 'sweep_chunk_folders', lambda root=None, now=None: [])]
        self.limits = True
        self.actor = model()
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        # Nothing here may open a real socket.
        for name in ('socket.socket', 'socket.create_connection'):
            guard = mock.patch(name, side_effect=AssertionError('tests must not open sockets'))
            guard.start()
            self.addCleanup(guard.stop)
        self.connection = {'host': 'db.internal', 'port': 3306, 'user': 'admin', 'password': 'conn-secret'}

    def call(self, path, payload):
        """Every answer is checked against its JSON Schema in docs/schemas."""
        answer = ui.api_response(path, payload, self.ws)
        if path in SCHEMAS:
            validate(json.loads(json.dumps(answer)), SCHEMAS[path])
        return answer

    def csv(self, text, name='data.csv', encoding='utf-8'):
        path = os.path.join(self.home.name, name)
        with open(path, 'wb') as handle:
            handle.write(text.encode(encoding))
        return path

    def open(self, text=None, **kwargs):
        path = self.csv(text if text is not None else
                        'id,name,bits\n1,Ann,1\n2,"Bob, Jr",7\n3,%s,\\N\n4,,0\n' % SECRET_CELL, **kwargs)
        return self.call('/api/import/csv/open-path', {'path': path})

    def target(self, **values):
        target = {'mode': 'existing', 'schema': 'db', 'table': 't',
                  'mapping': [{'csv_index': 0, 'column': 'id'}, {'csv_index': 1, 'column': 'name'},
                              {'csv_index': 2, 'column': 'bits'}]}
        target.update(values)
        return target

    def plan(self, opened=None, connection=None, **values):
        opened = opened or self.open()
        payload = {'connection': connection or dict(self.connection), 'file_id': opened['file_id'], 'format': {},
                   'target': self.target(), 'method': 'insert', 'transaction': {'mode': 'all'}, 'on_error': 'stop'}
        payload.update(values)
        return self.call('/api/import/csv/plan', payload)

    def apply(self, plan, connection=None, progress_id='apply-progress-0001'):
        return self.call('/api/import/csv/apply', {'connection': connection or dict(self.connection),
                                                   'token': plan['token'], 'progress_id': progress_id})

    def audits(self):
        return dbops.audit_report(self.ws.store)['records']


class FileTest(Base):
    def test_open_path_answers_without_the_path(self):
        opened = self.open()
        self.assertEqual(set(opened), {'file_id', 'name', 'size', 'modified'})
        self.assertEqual(opened['name'], 'data.csv')
        self.assertNotIn(self.home.name, json.dumps(opened))

    def test_open_path_refusals(self):
        folder = os.path.join(self.home.name, 'folder')
        os.mkdir(folder)
        for path, words in ((folder, 'folder'), (os.path.join(self.home.name, 'none.csv'), 'No file'),
                            ('relative.csv', 'full path'), ('', 'full path')):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, words):
                self.call('/api/import/csv/open-path', {'path': path})
        if hasattr(os, 'mkfifo'):
            fifo = os.path.join(self.home.name, 'pipe.csv')
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, 'not a regular file'):
                self.call('/api/import/csv/open-path', {'path': fifo})

    def test_unknown_file_id(self):
        with self.assertRaisesRegex(ValueError, 'no longer open'):
            self.call('/api/import/csv/inspect', {'file_id': 'nope'})

    def test_choose_uses_the_native_chooser(self):
        path = self.csv('a\n1\n')
        with mock.patch('myflames.file_picker.choose_file', return_value={'path': path, 'cancelled': False}):
            self.assertEqual(self.call('/api/import/csv/choose', {})['name'], 'data.csv')
        with mock.patch('myflames.file_picker.choose_file', return_value={'path': None, 'cancelled': True}):
            self.assertEqual(self.call('/api/import/csv/choose', {}), {'cancelled': True})


class InspectTest(Base):
    def test_detected_format_columns_and_preview(self):
        report = self.call('/api/import/csv/inspect', {'file_id': self.open()['file_id']})
        self.assertEqual(report['schema_version'], 'csv-inspect-1.0')
        self.assertEqual(report['detected']['separator'], ',')
        self.assertTrue(report['detected']['header'])
        self.assertEqual([c['name'] for c in report['columns']], ['id', 'name', 'bits'])
        self.assertEqual(report['columns'][0]['inferred'], {'sql_type': 'INT', 'nullable': False, 'max_length': 1})
        self.assertTrue(report['columns'][1]['inferred']['nullable'])
        self.assertEqual(report['preview']['rows'][2], ['3', SECRET_CELL, None])
        self.assertEqual(report['preview']['lines'], [2, 3, 4, 5])
        self.assertEqual((report['rows'], report['rows_counted'], report['rows_estimate']), (4, True, None))

    def test_problems_and_custom_separator(self):
        opened = self.open('id^name\n1^a\n2^b^c\n3^"x"y\n')
        report = self.call('/api/import/csv/inspect', {'file_id': opened['file_id'], 'format': {'separator': '^'}})
        self.assertTrue(report['format']['header'])
        self.assertEqual([p['line'] for p in report['problems']], [3, 4])
        self.assertEqual(report['problems_total'], 2)

    def test_bad_encoding_is_reported_with_its_line(self):
        path = os.path.join(self.home.name, 'bad.csv')
        with open(path, 'wb') as handle:
            handle.write(b'a,b\n1,2\n3,\xff\n')
        opened = self.call('/api/import/csv/open-path', {'path': path})
        report = self.call('/api/import/csv/inspect', {'file_id': opened['file_id'], 'format': {'encoding': 'utf-8'}})
        self.assertEqual(report['problems'][-1]['line'], 3)
        self.assertFalse(report['rows_counted'])


class PlanTest(Base):
    def test_ready_plan_reads_only(self):
        plan = self.plan()
        self.assertEqual(plan['schema_version'], 'csv-import-plan-1.0')
        self.assertTrue(plan['ready'], plan['blockers'])
        self.assertTrue(plan['token'])
        self.assertEqual(plan['rows'], {'total': 4, 'valid': 4, 'invalid': 0, 'skipped': 0})
        self.assertEqual(plan['batches'], 1)
        self.assertTrue(plan['statements']['data_sample'].startswith(
            "/*myflames:import_csv*/ INSERT INTO `db`.`t` (`id`, `name`, `bits`) VALUES (1, 'Ann', 1)"))
        self.assertIn('4 rows in 1 statement from data.csv', plan['statements']['data_summary'])
        self.assertIsNone(plan['statements']['create'])
        self.assertEqual(plan['policy']['action'], 'confirm')
        self.assertEqual(plan['privileges'], [{'action': 'insert', 'state': 'allowed',
                                               'reason': plan['privileges'][0]['reason']}])
        self.assertEqual(plan['target']['primary_key'], ['id'])
        self.assertEqual(plan['method']['local_infile'], True)
        self.assertEqual(plan['transaction'], {'mode': 'all', 'every': None, 'commits': 1})
        self.assertTrue(all(s.replace(ci.TAG, '').startswith(('SELECT', 'SHOW')) for s in self.server.sent),
                        self.server.sent)

    def test_bad_rows_block_or_skip(self):
        opened = self.open('id,name,bits\n1,a,1\nx,b,1\n3,%s,9\n4,d,1\n' % ('n' * 30))
        plan = self.plan(opened)
        self.assertFalse(plan['ready'])
        self.assertIsNone(plan['token'])
        self.assertEqual([(p['line'], p['column']) for p in plan['problems']], [(3, 'id'), (4, 'name'), (4, 'bits')])
        self.assertIn('2 rows have problems', plan['blockers'][0])
        plan = self.plan(opened, on_error='skip')
        self.assertTrue(plan['ready'])
        self.assertEqual(plan['rows'], {'total': 4, 'valid': 2, 'invalid': 2, 'skipped': 2})

    def test_myisam_refuses_all_or_nothing(self):
        self.server.table_engine = 'MyISAM'
        plan = self.plan()
        self.assertFalse(plan['ready'])
        self.assertIn('MyISAM is not transactional', ' '.join(plan['blockers']))
        self.assertTrue(self.plan(transaction={'mode': 'every', 'every': 2})['ready'])

    def test_load_data_needs_local_infile(self):
        self.server.local_infile = 'OFF'
        plan = self.plan(method='load_data')
        self.assertFalse(plan['ready'])
        self.assertEqual((plan['method']['load_data_available'], plan['method']['local_infile']), (False, False))
        self.assertIn("MySQL's default", plan['method']['why'])
        self.assertTrue(self.plan()['ready'])

    def test_unmapped_required_column_and_unsupported_column(self):
        target = self.target(mapping=[{'csv_index': 1, 'column': 'name'}, {'csv_index': None, 'column': 'id'}])
        plan = self.plan(target=target)
        self.assertIn('Column id has no default', ' '.join(plan['blockers']))
        self.server.columns.append(dict(COLUMNS[0], COLUMN_NAME='g', EXTRA='VIRTUAL GENERATED'))
        target = self.target()
        target['mapping'].append({'csv_index': 1, 'column': 'g'})
        with self.assertRaisesRegex(ValueError, 'mapped twice'):
            self.plan(target=target)

    def test_new_table(self):
        target = {'mode': 'new', 'schema': 'db', 'table': 'fresh', 'add_id_key': True,
                  'new_columns': [{'name': 'n', 'sql_type': 'INT', 'nullable': False},
                                  {'name': 'who', 'sql_type': 'VARCHAR(20)', 'nullable': True}],
                  'mapping': [{'csv_index': 0, 'column': 'n'}, {'csv_index': 1, 'column': 'who'}]}
        self.server.exists = False
        plan = self.plan(target=target)
        self.assertTrue(plan['ready'], plan['blockers'])
        self.assertIn('CREATE TABLE `db`.`fresh`', plan['statements']['create'])
        self.assertEqual(plan['drop_hint'], 'DROP TABLE `db`.`fresh`;')
        self.assertEqual([p['action'] for p in plan['privileges']], ['create_table', 'insert'])
        self.server.exists = True
        self.assertIn('already exists', ' '.join(self.plan(target=target)['blockers']))
        self.server.exists = False
        self.server.variables['sql_require_primary_key'] = 'ON'
        self.assertIn('requires a primary key', ' '.join(self.plan(target=dict(target, add_id_key=False))['blockers']))
        bad = dict(target, new_columns=[{'name': 'id', 'sql_type': 'INT', 'nullable': True}],
                   mapping=[{'csv_index': 0, 'column': 'id'}])
        with self.assertRaisesRegex(ValueError, 'already named id'):
            self.plan(target=bad)
        with self.assertRaisesRegex(ValueError, 'not a type'):
            self.plan(target=dict(target, new_columns=[{'name': 'n', 'sql_type': 'INT); DROP TABLE x; --',
                                                        'nullable': True}],
                                  mapping=[{'csv_index': 0, 'column': 'n'}]))

    def test_read_only_plan_has_no_token(self):
        plan = self.plan(connection=dict(self.connection, read_only=True))
        self.assertEqual(plan['policy']['action'], 'refuse')
        self.assertIsNone(plan['token'])
        self.assertFalse(plan['ready'])

    def test_request_validation(self):
        for values, words in (({'method': 'copy'}, 'INSERT batches'), ({'transaction': {'mode': 'every', 'every': 0}},
                                                                       'every N'),
                              ({'on_error': 'ignore'}, 'skipped'), ({'format': {'separator': ''}}, 'separator'),
                              ({'target': self.target(mapping=[])}, 'Map at least')):
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, words):
                self.plan(**values)

    def test_connection_error_is_redacted(self):
        self.server.start_error = 'ERROR 1045: Access denied using conn-secret'
        with self.assertRaises(ValueError) as caught:
            self.plan()
        self.assertNotIn('conn-secret', str(caught.exception))


class ApplyTest(Base):
    def test_insert_all_or_nothing(self):
        plan = self.plan()
        result = self.apply(plan)
        self.assertEqual(result['schema_version'], 'csv-import-result-1.0')
        self.assertEqual(result['outcome'], 'done', result['message'])
        self.assertEqual(result['rows'], {'total': 4, 'sent': 4, 'committed': 4, 'skipped': 0})
        self.assertEqual(result['last_committed_line'], 5)
        self.assertIsNone(result['next_line'])
        self.assertEqual(result['table_rows_after'], 4)
        writes = [s.replace(ci.TAG, '') for s in self.server.writes()]
        self.assertEqual(writes[:3], api.SETUP)
        self.assertEqual(writes[3], 'START TRANSACTION')
        self.assertTrue(writes[4].startswith("INSERT INTO `db`.`t` (`id`, `name`, `bits`) VALUES (1, 'Ann', 1), "
                                             "(2, 'Bob, Jr', 7), (3, '%s', NULL), (4, NULL, 0)" % SECRET_CELL))
        self.assertEqual(writes[5:], ['COMMIT'])
        record = self.audits()[0]
        self.assertEqual((record['operation'], record['status'], record['outcome'], record['rows_affected']),
                         ('import_csv', 'ok', 'imported', 4))
        self.assertIn('4 rows in 1 statement from data.csv', json.dumps(record))
        self.assertNotIn(SECRET_CELL, json.dumps(record))
        self.assertNotIn('conn-secret', json.dumps(record))
        self.assertEqual(self.opened[0][0]['password'], 'conn-secret')
        self.assertFalse(self.opened[-1][1])

    def test_token_is_used_once(self):
        plan = self.plan()
        self.apply(plan)
        with self.assertRaisesRegex(ValueError, 'expired, changed or was already used'):
            self.apply(plan)

    def test_read_only_refuses_before_any_connection_and_audits(self):
        plan = self.plan()
        opened = len(self.opened)
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.apply(plan, connection=dict(self.connection, read_only=True))
        self.assertEqual(len(self.opened), opened)
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'read_only'))
        # The plan stays for the connection it was made on.
        self.assertEqual(self.apply(plan)['outcome'], 'done')

    def test_policy_change_to_read_only_profile_refuses(self):
        profile = self.ws.store.save('profile', {'name': 'prod', 'host': 'db.internal', 'port': 3306, 'user': 'admin'})
        connection = dict(self.connection, profile_id=profile['id'])
        plan = self.plan(connection=connection)
        self.ws.store.save('profile', dict(profile, read_only=True), profile['id'])
        with self.assertRaisesRegex(ValueError, 'read-only'):
            self.apply(plan, connection=connection)
        self.assertFalse(any(s.replace(ci.TAG, '').startswith(('INSERT', 'START', 'SET SESSION sql_mode'))
                             for s in self.server.sent))

    def test_development_connection_still_needs_the_review(self):
        profile = self.ws.store.save('profile', {'name': 'dev', 'host': 'db.internal', 'port': 3306, 'user': 'admin',
                                                 'type': 'development'})
        plan = self.plan(connection=dict(self.connection, profile_id=profile['id']))
        self.assertEqual((plan['policy']['action'], plan['policy']['connection_type']), ('confirm', 'development'))

    def test_file_change_refuses_and_nothing_is_sent(self):
        plan = self.plan()
        path = os.path.join(self.home.name, 'data.csv')
        with open(path, 'ab') as handle:
            handle.write(b'5,Eve,1\n')
        opened = len(self.opened)
        with self.assertRaisesRegex(ValueError, 'changed after the review'):
            self.apply(plan)
        self.assertEqual(len(self.opened), opened)
        self.assertEqual(self.audits()[0]['outcome'], 'file_changed')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.apply(plan)

    def test_same_size_and_mtime_but_other_bytes_refuses(self):
        plan = self.plan()
        path = os.path.join(self.home.name, 'data.csv')
        info = os.stat(path)
        with open(path, 'r+b') as handle:
            handle.seek(info.st_size - 3)
            handle.write(b'9\n\n')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'changed after the review'):
            self.apply(plan)

    def test_metadata_change_refuses_with_reads_only(self):
        plan = self.plan()
        self.server.columns[1]['COLUMN_TYPE'] = 'varchar(10)'
        before = len(self.server.sent)
        with self.assertRaisesRegex(ValueError, 'definition changed'):
            self.apply(plan)
        sent = [s.replace(ci.TAG, '') for s in self.server.sent[before:]]
        self.assertTrue(sent and all(s.startswith(('SELECT', 'SHOW')) for s in sent), sent)
        self.assertEqual(self.audits()[0]['outcome'], 'changed')

    def test_sql_mode_change_refuses(self):
        plan = self.plan()
        self.server.backslash = False
        with self.assertRaisesRegex(ValueError, 'NO_BACKSLASH_ESCAPES'):
            self.apply(plan)

    def test_denied_privilege_refuses_and_unknown_goes_on(self):
        plan = self.plan()
        denied = {'state': 'denied', 'allowed': False, 'reason': 'Needs INSERT; app@% does not have it.'}
        with mock.patch.object(api.privileges, 'can', return_value=dict(denied, missing=['INSERT'])):
            with self.assertRaisesRegex(ValueError, 'Needs INSERT'):
                self.apply(plan)
        self.actor = {'checked': False, 'grants': [], 'roles': [], 'engine': 'mysql', 'version': '8.4.11'}
        plan = self.plan()
        self.assertEqual(plan['privileges'][0]['state'], 'unknown')
        self.assertTrue(plan['ready'])
        self.assertEqual(self.apply(plan)['outcome'], 'done')

    def test_server_error_rolls_back_all_or_nothing(self):
        plan = self.plan()
        self.server.hook = lambda sql, server, progress: ({'code': 1062, 'message': "Duplicate entry '3' for key "
                                                                                 "'t.PRIMARY'"}
                                                          if sql.startswith(ci.TAG + 'INSERT') else None)
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['rows']['committed'], 0)
        self.assertEqual(result['server_error']['code'], 1062)
        self.assertEqual(result['failed_at']['line'], 2)
        self.assertEqual(result['next_line'], 2)
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'ROLLBACK')
        self.assertEqual(self.audits()[0]['status'], 'error')
        self.assertIn('Nothing was kept', result['message'])

    def test_every_n_keeps_committed_rows_and_names_the_line(self):
        plan = self.plan(transaction={'mode': 'every', 'every': 2})
        self.assertEqual(plan['transaction']['commits'], 2)
        calls = []

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT'):
                calls.append(sql)
                if len(calls) == 2:
                    return {'code': 1366, 'message': "Incorrect integer value: 'x' for column 'id' at row 2"}
            return None
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual((result['rows']['committed'], result['last_committed_line'], result['next_line']), (2, 3, 4))
        self.assertEqual(result['failed_at']['line'], 5)
        self.assertIn('2 rows were committed before it', result['message'])
        self.assertEqual(len(self.server.table), 2)

    def test_skip_mode_sends_rows_one_by_one_after_a_row_error(self):
        plan = self.plan(on_error='skip')

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT') and ("(2, 'Bob, Jr', 7)" in sql):
                return {'code': 1062, 'message': "Duplicate entry '2' for key 't.PRIMARY'"}
            return None
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'done')
        self.assertEqual(result['rows'], {'total': 4, 'sent': 3, 'committed': 3, 'skipped': 1})
        self.assertEqual([p['line'] for p in result['problems']], [3])
        inserts = [s for s in self.server.writes() if 'INSERT' in s]
        self.assertEqual(len(inserts), 5)

    def test_stop_between_statements(self):
        opened = self.open('id,name,bits\n' + ''.join('%d,n%d,1\n' % (i, i) for i in range(1, 11)))
        plan = self.plan(opened, transaction={'mode': 'every', 'every': 2})
        count = []

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT'):
                count.append(sql)
                if len(count) == 2:
                    progress_stop()
            return None

        def progress_stop():
            self.call('/api/import/csv/stop', {'progress_id': 'apply-progress-0001'})
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'stopped')
        self.assertEqual((result['rows']['committed'], result['last_committed_line'], result['next_line']), (2, 3, 4))
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'ROLLBACK')
        self.assertEqual(len(self.server.table), 2)
        self.assertEqual(self.audits()[0]['outcome'], 'stopped')
        snap = self.call('/api/import/csv/progress', {'progress_id': 'apply-progress-0001'})
        self.assertEqual((snap['phase'], snap['stop_requested'], snap['rows_committed']), ('finished', True, 2))

    def test_stop_during_the_last_statement_rolls_back_instead_of_committing(self):
        plan = self.plan()

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT'):
                self.call('/api/import/csv/stop', {'progress_id': 'apply-progress-0001'})
            return None
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'stopped')
        self.assertNotIn(ci.TAG + 'COMMIT', self.server.sent)
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'ROLLBACK')
        self.assertEqual((len(self.server.table), result['rows']['committed'], result['next_line']), (0, 0, 2))

    def test_commit_without_a_reply_is_unknown_and_never_retried(self):
        plan = self.plan()
        self.server.hook = lambda sql, server, progress: 'unknown' if sql == ci.TAG + 'COMMIT' else None
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('unknown', result['message'])
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'COMMIT')
        self.assertEqual(sum(1 for s in self.server.sent if s == ci.TAG + 'COMMIT'), 1)
        self.assertIsNone(result['table_rows_after'])
        self.assertEqual(self.audits()[0]['status'], 'unknown')

    def test_lost_session_mid_statement_is_unknown_without_commit(self):
        plan = self.plan()
        self.server.hook = lambda sql, server, progress: 'unknown' if sql.startswith(ci.TAG + 'INSERT') else None
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('rolls back a transaction that was never committed', result['message'])
        self.assertNotIn(ci.TAG + 'COMMIT', self.server.sent)
        self.assertEqual(self.audits()[0]['status'], 'unknown')


    def test_new_table_left_empty_offers_the_drop(self):
        self.server.exists = False
        target = {'mode': 'new', 'schema': 'db', 'table': 'fresh', 'add_id_key': False,
                  'new_columns': [{'name': 'n', 'sql_type': 'INT', 'nullable': True},
                                  {'name': 'who', 'sql_type': 'VARCHAR(20)', 'nullable': True}],
                  'mapping': [{'csv_index': 0, 'column': 'n'}, {'csv_index': 1, 'column': 'who'}]}
        plan = self.plan(target=target)
        self.server.hook = lambda sql, server, progress: ({'code': 1205, 'message': 'Lock wait timeout exceeded'}
                                                          if sql.startswith(ci.TAG + 'INSERT') else None)
        result = self.apply(plan)
        self.assertEqual((result['outcome'], result['table_created']), ('failed', True))
        self.assertEqual(result['drop_hint'], 'DROP TABLE `db`.`fresh`;')
        self.assertIn('the new table stays, empty', result['message'])
        self.assertTrue(any(s.startswith(ci.TAG + 'CREATE TABLE') for s in self.server.sent))
        record = self.audits()[0]
        self.assertTrue(any('CREATE TABLE `db`.`fresh`' in s for s in record['statements']))

    def test_load_data_reads_a_checked_copy(self):
        opened = self.open('id,name,bits\n1,"tab\there",1\n2,back\\slash,7\n3,\\N,\n')
        plan = self.plan(opened, method='load_data')
        self.assertTrue(plan['ready'], plan['blockers'])
        self.assertIn("LOAD DATA LOCAL INFILE '", plan['statements']['data_sample'])
        self.assertIn('(`id`, `name`, @c3) SET `bits` = CAST(@c3 AS UNSIGNED)', plan['statements']['data_sample'])
        folder = os.path.dirname(re.search(r"INFILE '([^']*)'", plan['statements']['data_sample']).group(1))
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'done', result['message'])
        self.assertTrue(self.opened[-1][1])
        self.assertEqual(self.server.loaded, [b'1\ttab\\there\t1\n2\tback\\\\slash\t7\n3\t\\N\t\\N\n'])
        self.assertFalse(os.path.exists(folder))

    def test_load_data_count_or_warning_mismatch_rolls_back(self):
        plan = self.plan(method='load_data')
        self.server.load_short = 1
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIn('loaded 3 of 4 rows', result['message'])
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'ROLLBACK')
        self.server.load_short = 0
        self.server.warning_rows = [{'Level': 'Warning', 'Code': '1062', 'Message': "Duplicate entry '1'"}]
        result = self.apply(self.plan(method='load_data'))
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['warnings']['items'][0]['code'], 1062)

    def test_load_data_stop_sends_kill_query(self):
        progress = api.Progress('apply')
        progress.set(phase='sending')
        calls = []
        progress.killer(lambda: calls.append('kill') or ('sent', None))
        self.assertIn('KILL QUERY was sent', progress.request_stop())
        self.assertEqual(calls, ['kill'])
        self.assertTrue(progress.stop.is_set())
        finished = api.Progress('apply')
        finished.set(phase='finished')
        self.assertIn('already finished', finished.request_stop())

    def test_progress_and_stop_for_unknown_ids(self):
        self.assertEqual(self.call('/api/import/csv/progress', {'progress_id': 'x' * 20})['phase'], 'unknown')
        self.assertFalse(self.call('/api/import/csv/stop', {'progress_id': 'x' * 20})['stop_requested'])
        with self.assertRaisesRegex(ValueError, 'progress_id'):
            self.call('/api/import/csv/progress', {'progress_id': 'bad id'})

    def test_two_applies_of_one_plan(self):
        plan = self.plan()
        record = api.registry(self.ws).plan(plan['token'])
        self.assertTrue(api.registry(self.ws).claim(record))
        with self.assertRaisesRegex(ValueError, 'already being applied'):
            self.apply(plan)


class VersionRuleTest(unittest.TestCase):
    def test_timestamp_range_follows_the_server(self):
        self.assertEqual(api.timestamp_max_year('mysql', '9.7.2'), 2038)
        self.assertEqual(api.timestamp_max_year('mariadb', '11.4.13-MariaDB'), 2038)
        self.assertEqual(api.timestamp_max_year('mariadb', '11.5.2-MariaDB'), 2106)
        self.assertEqual(api.timestamp_max_year('mariadb', '12.0.1-MariaDB'), 2106)


class SessionFlagTest(unittest.TestCase):
    def test_local_files_only_for_load_data_and_only_from_the_folder(self):
        started = []

        class Session(object):
            client_flags = ()

            def __init__(self, *args):
                pass

            def start(self, options):
                started.append(self.client_flags)

        with mock.patch.object(api.ed, 'EditorSession', Session), \
                mock.patch.object(api, 'client_binary', lambda full: '/x/mysql'):
            with mock.patch.object(api, 'limits_local_files', lambda binary: True):
                api._open_session({}, 'id', {'type': 'production'}, 'x', load_dir='/tmp/myflames-csv-1-a')
            with mock.patch.object(api, 'limits_local_files', lambda binary: False):
                api._open_session({}, 'id', {'type': 'production'}, 'x', load_dir='/tmp/myflames-csv-1-a')
            api._open_session({}, 'id', {'type': 'production'}, 'x')
        self.assertEqual(started, [('--local-infile=0', '--load-data-local-dir=/tmp/myflames-csv-1-a'),
                                   ('--local-infile=1',), ()])

    def test_client_help_decides(self):
        api._LOAD_DIR_SUPPORT.clear()
        self.addCleanup(api._LOAD_DIR_SUPPORT.clear)
        mysql = SimpleNamespace(stdout=b'  --load-data-local-dir=name \n                      Directory path safe\n')
        maria = SimpleNamespace(stdout=b'  --local-infile      Enable LOAD DATA LOCAL INFILE.\n')
        with mock.patch.object(api.subprocess, 'run', return_value=mysql) as run:
            self.assertTrue(api.limits_local_files('/a/mysql'))
            self.assertTrue(api.limits_local_files('/a/mysql'))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args[0][0], ['/a/mysql', '--no-defaults', '--help'])
        with mock.patch.object(api.subprocess, 'run', return_value=maria):
            self.assertFalse(api.limits_local_files('/a/mariadb'))
        with mock.patch.object(api.subprocess, 'run', side_effect=OSError('gone')):
            self.assertFalse(api.limits_local_files('/a/missing'))


class HttpTest(Base):
    def setUp(self):
        Base.setUp(self)
        self.ws.origin = 'http://127.0.0.1:8765'
        self.ws.token = 'test-session-token'
        self.ws.cookie_name = 'myflames_test'
        self.ws.session_secret = 'test-cookie-secret'

    def post(self, path, payload):
        body = json.dumps(payload).encode('utf-8')
        handler = object.__new__(ui.WorkspaceHandler)
        handler.server = self.ws
        handler.connection = mock.Mock()
        handler.path, handler.command = path, 'POST'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST %s HTTP/1.1' % path
        handler.headers = Message()
        for key, value in {'Host': '127.0.0.1:8765', 'Origin': self.ws.origin,
                           'Cookie': 'myflames_test=test-cookie-secret', 'X-Myflames-Token': self.ws.token,
                           'Content-Type': 'application/json', 'Content-Length': str(len(body))}.items():
            handler.headers[key] = value
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        handler.do_POST()
        head, content = handler.wfile.getvalue().split(b'\r\n\r\n', 1)
        return int(head.split(b' ')[1]), json.loads(content)

    def test_routes_are_allowlisted_and_apply_is_a_write_path(self):
        for path in api.PATHS:
            self.assertIn(path, ui._CSV_IMPORT_PATHS)
        self.assertIn('/api/import/csv/apply', ui._WRITE_PATHS)
        self.assertNotIn('/api/import/csv/apply', ui._CONNECTION_PATHS)
        path = self.csv('a,b\n1,2\n')
        status, opened = self.post('/api/import/csv/open-path', {'path': path})
        self.assertEqual(status, 200)
        status, report = self.post('/api/import/csv/inspect', {'file_id': opened['file_id']})
        self.assertEqual((status, report['rows']), (200, 1))

    def test_apply_with_a_bad_token_is_a_400_and_sends_nothing(self):
        status, body = self.post('/api/import/csv/apply', {'connection': self.connection, 'token': 'nope',
                                                           'progress_id': 'apply-progress-0001'})
        self.assertEqual(status, 400)
        self.assertIn('nothing was sent', body['error'])
        self.assertEqual(self.server.sent, [])

    def test_apply_over_http(self):
        status, opened = self.post('/api/import/csv/open-path', {'path': self.csv(
            'id,name,bits\n1,a,1\n')})
        status, plan = self.post('/api/import/csv/plan', {
            'connection': self.connection, 'file_id': opened['file_id'], 'format': {}, 'target': self.target(),
            'method': 'insert', 'transaction': {'mode': 'all'}, 'on_error': 'stop'})
        self.assertEqual(status, 200)
        status, result = self.post('/api/import/csv/apply', {'connection': self.connection, 'token': plan['token'],
                                                             'progress_id': 'apply-progress-0002'})
        self.assertEqual((status, result['outcome']), (200, 'done'))


LOST = {'code': 2013, 'message': 'Lost connection to MySQL server during query'}


class LostConnectionTest(Base):
    """A client-printed lost connection (editor_session.LOST_CONNECTION_CODES)
    is an unknown outcome wherever a statement may have run; never retried."""

    def lost_on(self, prefix):
        def hook(sql, server, progress):
            return LOST if sql.startswith(ci.TAG + prefix) else None
        return hook

    def test_send_maps_lost_codes_to_unknown_and_ends_the_session(self):
        ended = []
        step = SimpleNamespace(marker=True, closed=False, unfinished=False, real_errors=lambda: [dict(LOST)])
        session = SimpleNamespace(_send=lambda *a, **k: step, _wait=lambda predicate, timeout: True, ended=False,
                                  end=lambda *a, **k: ended.append(a))
        self.assertEqual(ORIGINAL_SEND(session, 'x')[1], 'unknown')
        self.assertEqual(ended[0][0], 'connection_lost')
        step2 = SimpleNamespace(marker=True, closed=False, unfinished=False,
                                real_errors=lambda: [{'code': 1062, 'message': 'dup'}])
        session2 = SimpleNamespace(_send=lambda *a, **k: step2, _wait=lambda predicate, timeout: True, ended=False,
                                   end=lambda *a, **k: None)
        self.assertEqual(ORIGINAL_SEND(session2, 'x')[1]['code'], 1062)

    def real_send(self):
        patcher = mock.patch.object(api, '_send', lambda session, sql, timeout=None, progress=None, options=None:
                                    self.translate(session.server.answer(session, sql, progress, options), session))
        patcher.start()
        self.addCleanup(patcher.stop)

    def translate(self, answer, session):
        step, error = answer
        if isinstance(error, dict) and error.get('code') in api.ed.LOST_CONNECTION_CODES:
            session.ended = True
            return step, 'unknown'
        return answer

    def test_commit(self):
        self.real_send()
        plan = self.plan()
        self.server.hook = lambda sql, server, progress: LOST if sql == ci.TAG + 'COMMIT' else None
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('COMMIT got no reply', result['message'])
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome']), ('unknown', 'unknown'))
        self.assertEqual(sum(1 for s in self.server.sent if s == ci.TAG + 'COMMIT'), 1)

    def test_create_table_says_the_table_may_exist(self):
        self.real_send()
        self.server.exists = False
        target = {'mode': 'new', 'schema': 'db', 'table': 'fresh', 'add_id_key': True,
                  'new_columns': [{'name': 'n', 'sql_type': 'INT', 'nullable': True}],
                  'mapping': [{'csv_index': 0, 'column': 'n'}]}
        plan = self.plan(target=target)
        self.server.hook = self.lost_on('CREATE TABLE')
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('whether table `db`.`fresh` exists is unknown', result['message'])
        self.assertEqual(result['drop_hint'], 'DROP TABLE `db`.`fresh`;')
        self.assertFalse(any(s.startswith(ci.TAG + 'INSERT') for s in self.server.sent))
        self.assertEqual(self.audits()[0]['status'], 'unknown')

    def test_non_transactional_statement(self):
        self.real_send()
        self.server.table_engine = 'MyISAM'
        plan = self.plan(transaction={'mode': 'every', 'every': 2})
        self.server.hook = self.lost_on('INSERT')
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertIn('non-transactional', result['message'])

    def test_load_data_chunk(self):
        self.real_send()
        plan = self.plan(method='load_data')
        self.server.hook = self.lost_on('LOAD DATA')
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'unknown')
        self.assertNotIn(ci.TAG + 'COMMIT', self.server.sent)


class ReviewFixesTest(Base):
    def test_server_text_with_row_values_stays_out_of_the_audit(self):
        plan = self.plan()
        self.server.hook = lambda sql, server, progress: (
            {'code': 1062, 'message': "Duplicate entry '%s' for key 't.name'" % SECRET_CELL}
            if sql.startswith(ci.TAG + 'INSERT') else None)
        result = self.apply(plan)
        self.assertIn(SECRET_CELL, result['message'])
        self.assertIn(SECRET_CELL, result['server_error']['message'])
        record = self.audits()[0]
        self.assertNotIn(SECRET_CELL, json.dumps(record))
        self.assertIn('error 1062', record['message'])
        self.assertEqual(record['error_code'], 1062)

    def test_exact_failing_line_is_found_and_nothing_kept(self):
        opened = self.open('id,name,bits\n1,a,1\n2,b,1\n3,c,1\n4,d,1\n')
        plan = self.plan(opened)

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT') and "(3, 'c', 1)" in sql:
                return {'code': 1062, 'message': "Duplicate entry '3' for key 't.PRIMARY'"}
            return None
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual((result['failed_at']['line'], result['failed_at']['first_line'],
                          result['failed_at']['last_line']), (4, 2, 5))
        self.assertIn("The server refused line 4 (in the statement with lines 2 to 5): Duplicate entry '3' for key "
                      "'t.PRIMARY'. Nothing was kept.", result['message'])
        self.assertEqual(self.server.writes()[-1], ci.TAG + 'ROLLBACK')
        self.assertEqual(self.server.table, [])
        singles = [s for s in self.server.sent if s.startswith(ci.TAG + 'INSERT') and '), (' not in s]
        self.assertEqual(len(singles), 3)

    def test_non_transactional_failure_reports_the_range(self):
        self.server.table_engine = 'MyISAM'
        opened = self.open('id,name,bits\n1,a,1\n2,b,1\n3,c,1\n')
        plan = self.plan(opened, transaction={'mode': 'every', 'every': 10})
        self.server.hook = lambda sql, server, progress: ({'code': 1062, 'message': "Duplicate entry '2' for key 'PRIMARY'"}
                                                          if sql.startswith(ci.TAG + 'INSERT') else None)
        result = self.apply(plan)
        self.assertEqual((result['failed_at']['line'], result['failed_at']['last_line']), (2, 4))
        self.assertIn('the statement with lines 2 to 4', result['message'])
        self.assertEqual(sum(1 for s in self.server.sent if s.startswith(ci.TAG + 'INSERT')), 1)
        self.assertIn('not transactional', result['message'])

    def test_stopped_imports_are_audited_as_stopped(self):
        opened = self.open('id,name,bits\n' + ''.join('%d,n%d,1\n' % (i, i) for i in range(1, 7)))
        plan = self.plan(opened, transaction={'mode': 'every', 'every': 2})
        count = []

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT'):
                count.append(1)
                if len(count) == 2:
                    self.call('/api/import/csv/stop', {'progress_id': 'apply-progress-0001'})
            return None
        self.server.hook = hook
        result = self.apply(plan)
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome'], record['rows_affected']), ('stopped', 'stopped', 2))
        self.assertEqual(result['rows']['committed'], 2)

    def test_audit_statements_show_the_import_first(self):
        self.server.exists = False
        target = {'mode': 'new', 'schema': 'db', 'table': 'fresh', 'add_id_key': True,
                  'new_columns': [{'name': 'n', 'sql_type': 'INT', 'nullable': True}],
                  'mapping': [{'csv_index': 0, 'column': 'n'}]}
        self.apply(self.plan(target=target))
        record = self.audits()[0]
        self.assertTrue(record['statements'][0].startswith(ci.TAG + 'CREATE TABLE `db`.`fresh`'))
        self.assertTrue(record['statements'][1].startswith('INSERT INTO `db`.`fresh` (`n`) VALUES ...'))
        self.assertFalse(any('SET SESSION' in s for s in record['statements']))
        self.assertIn('session_setup', json.dumps(record['target']))

    def test_expired_review_is_recorded(self):
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.call('/api/import/csv/apply', {'connection': dict(self.connection), 'token': 'gone',
                                                'progress_id': 'apply-progress-0009'})
        record = self.audits()[0]
        self.assertEqual((record['status'], record['outcome'], record['operation']), ('refused', 'expired',
                                                                                      'import_csv'))

    def test_unreadable_or_deleted_file(self):
        plan = self.plan()
        path = os.path.join(self.home.name, 'data.csv')
        if hasattr(os, 'getuid') and os.getuid() != 0:
            os.chmod(path, 0)
            try:
                opened = len(self.opened)
                with self.assertRaisesRegex(ValueError, 'cannot be read now'):
                    self.apply(plan)
                self.assertEqual(len(self.opened), opened)
                self.assertEqual(self.audits()[0]['outcome'], 'file_changed')
            finally:
                os.chmod(path, 0o600)
        opened = self.open(name='gone.csv')
        os.unlink(os.path.join(self.home.name, 'gone.csv'))
        for path_ in ('/api/import/csv/inspect', '/api/import/csv/plan'):
            payload = {'file_id': opened['file_id'], 'connection': dict(self.connection), 'format': {},
                       'target': self.target(), 'method': 'insert', 'transaction': {'mode': 'all'}}
            with self.subTest(path=path_), self.assertRaisesRegex(ValueError, 'cannot be read now'):
                ui.api_response(path_, payload, self.ws)

    def test_malformed_header_row_blocks_and_is_never_data(self):
        opened = self.open('"title" x,y,z\nid,name,bits\n1,Ann,1\n2,Bob,7\n')
        plan = self.plan(opened, on_error='skip', format={'header': True, 'separator': ','})
        self.assertFalse(plan['ready'])
        self.assertIn('Line 1, the header row, cannot be read', ' '.join(plan['blockers']))
        report = self.call('/api/import/csv/inspect', {'file_id': opened['file_id'],
                                                       'format': {'header': True, 'separator': ','}})
        self.assertEqual(report['problems'][0]['line'], 1)
        self.assertEqual(report['preview']['lines'][0], 2)
        self.assertEqual(report['columns'][0]['header'], '')

    def test_check_stops_without_waiting_for_2000_rows(self):
        imports = api.registry(self.ws)
        progress = imports.start_progress('plan-progress-00001', 'plan')
        progress.stop.set()
        entry = imports.file(self.open()['file_id'])
        fmt = ci.check_format({})
        mapped = [api._Mapped(0, ci.parse_type('INT'))]
        mapped[0].column.name = 'id'
        with self.assertRaisesRegex(ValueError, 'stopped'):
            api._check_rows(entry, fmt, mapped, 3, {'schema': 'db', 'table': 't'}, 'insert',
                            {'mode': 'all', 'every': None}, 'stop', True, 1 << 20, 1 << 20, progress)

    def test_long_line_is_a_blocker(self):
        opened = self.open('id,name,bits\n1,%s,1\n' % ('n' * 3000))
        with mock.patch.object(ci, 'LINE_LIMIT', 1000):
            plan = self.plan(opened)
        self.assertFalse(plan['ready'])
        self.assertIn('Line 2 is longer than', ' '.join(plan['blockers']))

    def test_myisam_load_data_reports_what_was_kept(self):
        self.server.table_engine = 'MyISAM'
        plan = self.plan(method='load_data', transaction={'mode': 'every', 'every': 10})
        self.server.load_short = 1
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertEqual(result['rows']['committed'], 3)
        self.assertIn('loaded 3 of 4 rows', result['message'])
        self.assertIn('3 rows are in the table (it is not transactional, so nothing was rolled back)',
                      result['message'])
        self.assertNotIn('transaction was rolled back', result['message'])

    def test_triggers_are_unknown_without_the_trigger_privilege(self):
        self.actor = model(['GRANT SELECT, INSERT ON `db`.* TO `app`@`%`'])
        plan = self.plan()
        self.assertIsNone(plan['target']['triggers'])
        self.assertIn('TRIGGER privilege', ' '.join(plan['warnings']))
        self.actor = model()
        self.assertEqual(self.plan()['target']['triggers'], 0)
        # MariaDB lists triggers to an account with INSERT, UPDATE or DELETE too.
        self.server.engine = 'mariadb'
        self.actor = privileges.parse_grants(['GRANT SELECT, INSERT ON `db`.* TO `app`@`%`'], 'mariadb', '11.4.13')
        self.assertEqual(self.plan()['target']['triggers'], 0)

    def test_last_commit_waits_for_the_whole_file_hash(self):
        rows = ''.join('%d,%s,1\n' % (i, 'v' * 15) for i in range(1, 6001))
        opened = self.open('id,name,bits\n' + rows)
        plan = self.plan(opened, transaction={'mode': 'every', 'every': 2000})
        self.assertTrue(plan['ready'], plan['blockers'])
        path = os.path.join(self.home.name, 'data.csv')
        info = os.stat(path)
        changed = []

        def hook(sql, server, progress):
            if sql.startswith(ci.TAG + 'INSERT') and not changed:
                changed.append(1)
                with open(path, 'r+b') as handle:  # same size, same mtime, other bytes near the end
                    handle.seek(info.st_size - 12)  # inside the last row's name: still a valid row
                    handle.write(b'w' * 5)
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            return None
        self.server.hook = hook
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIn('file changed while it was being imported', result['message'])
        self.assertEqual(result['rows']['committed'], 4000)
        self.assertEqual(sum(1 for s in self.server.sent if s == ci.TAG + 'COMMIT'), 2)
        self.assertEqual(len(self.server.table), 4000)

    def test_session_mode_is_checked_before_rows(self):
        plan = self.plan()
        self.server.session_mode = 'STRICT_ALL_TABLES,EMPTY_STRING_IS_NULL'
        result = self.apply(plan)
        self.assertEqual(result['outcome'], 'failed')
        self.assertIn("sql_mode is not what the import needs", result['message'])
        self.assertFalse(any(s.startswith(ci.TAG + 'INSERT') for s in self.server.sent))
        self.assertIn("REPLACE(CONCAT(',', @@SESSION.sql_mode, ',STRICT_ALL_TABLES,'), ',EMPTY_STRING_IS_NULL,', ',')",
                      api.SETUP[0])


class PlanWarningsTest(Base):
    def test_ndb_refuses_skip_and_report(self):
        self.server.table_engine = 'ndbcluster'
        plan = self.plan(on_error='skip')
        self.assertIn('On NDB a refused statement rolls back the whole transaction', ' '.join(plan['blockers']))
        self.assertTrue(self.plan()['ready'])

    def test_read_only_texts(self):
        self.server.variables['super_read_only'] = 'ON'
        self.assertIn('refuses every write, from every account', ' '.join(self.plan()['warnings']))
        self.server.variables.update(super_read_only='OFF', read_only='ON')
        self.assertIn('CONNECTION_ADMIN or SUPER', ' '.join(self.plan()['warnings']))
        self.server.engine = 'mariadb'
        self.assertIn('READ ONLY ADMIN', ' '.join(self.plan()['warnings']))

    def test_mariadb_local_infile_advice(self):
        self.server.engine = 'mariadb'
        self.server.local_infile = 'OFF'
        why = self.plan(method='load_data')['method']['why']
        self.assertIn('SET GLOBAL local_infile = ON', why)
        self.assertIn('option file', why)

    def test_client_without_a_folder_limit_is_named(self):
        plan = self.plan(method='load_data')
        self.assertTrue(plan['method']['client_limits_files'])
        self.limits = False
        plan = self.plan(method='load_data')
        self.assertFalse(plan['method']['client_limits_files'])
        self.assertIn('no --load-data-local-dir', ' '.join(plan['warnings']))
        self.assertIsNone(self.plan()['method']['client_limits_files'])

    def new_target(self, **values):
        target = {'mode': 'new', 'schema': 'db', 'table': 'fresh', 'add_id_key': False,
                  'new_columns': [{'name': 'n', 'sql_type': 'INT', 'nullable': True},
                                  {'name': 'at', 'sql_type': 'TIMESTAMP', 'nullable': False}],
                  'mapping': [{'csv_index': 0, 'column': 'n'}, {'csv_index': 2, 'column': 'at'}]}
        target.update(values)
        return target

    def test_invisible_primary_key_and_timestamp_defaults(self):
        self.server.exists = False
        opened = self.open('id,name,at\n1,a,2024-01-01 00:00:00\n')
        self.server.variables.update(sql_require_primary_key='ON', sql_generate_invisible_primary_key='ON')
        plan = self.plan(opened, target=self.new_target())
        self.assertFalse(any('requires a primary key' in b for b in plan['blockers']))
        self.assertIn('invisible primary key', ' '.join(plan['notes']))
        self.server.variables['sql_generate_invisible_primary_key'] = 'OFF'
        self.assertIn('requires a primary key', ' '.join(self.plan(opened, target=self.new_target())['blockers']))
        self.server.variables.update(sql_require_primary_key='OFF', explicit_defaults_for_timestamp='OFF')
        self.assertIn('explicit_defaults_for_timestamp is OFF', ' '.join(
            self.plan(opened, target=self.new_target())['warnings']))

    def test_engine_type_rules_for_new_tables(self):
        self.server.exists = False
        target = self.new_target(new_columns=[{'name': 'n', 'sql_type': 'DECIMAL(10,2) UNSIGNED', 'nullable': True}],
                                 mapping=[{'csv_index': 0, 'column': 'n'}], add_id_key=True)
        with self.assertRaisesRegex(ValueError, 'MySQL deprecates UNSIGNED'):
            self.plan(target=target)
        self.server.engine = 'mariadb'
        plan = self.plan(target=target)
        self.assertIn('`n` DECIMAL(10,2) UNSIGNED NULL', plan['statements']['create'])

    def test_size_caveats(self):
        self.server.table_engine = 'rocksdb'
        self.server.variables['rocksdb_max_row_locks'] = '4'
        self.assertIn('rocksdb_max_row_locks', ' '.join(self.plan()['warnings']))
        self.server.table_engine = 'InnoDB'
        self.server.variables.update(group_replication_group_name='aaaa-bbbb',
                                     group_replication_transaction_size_limit='50', wsrep_on='ON',
                                     wsrep_max_ws_size='50')
        warnings = ' '.join(self.plan()['warnings'])
        self.assertIn('group_replication_transaction_size_limit', warnings)
        self.assertIn('wsrep_max_ws_size', warnings)
        self.assertNotIn('wsrep_max_ws_size', ' '.join(self.plan(transaction={'mode': 'every', 'every': 2})[
            'warnings']))

    def test_notes_name_strict_mode_for_insert_only_and_empty_string_is_null(self):
        self.server.variables['sql_mode'] = 'STRICT_TRANS_TABLES,EMPTY_STRING_IS_NULL'
        notes = ' '.join(self.plan()['notes'])
        self.assertIn('INSERT batches run with STRICT_ALL_TABLES', notes)
        self.assertIn('EMPTY_STRING_IS_NULL', notes)

    def test_names_compare_exactly_when_lower_case_table_names_is_0(self):
        self.plan()
        reads = [s for s in self.server.sent if 'information_schema.TABLES' in s]
        self.assertIn('COLLATE utf8mb4_bin', reads[-1])
        self.server.variables['lower_case_table_names'] = '1'
        self.plan()
        reads = [s for s in self.server.sent if 'information_schema.TABLES' in s]
        self.assertNotIn('COLLATE utf8mb4_bin', reads[-1])


class SweepTest(unittest.TestCase):
    def test_only_stale_folders_of_ours_are_removed(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        import subprocess as sp
        dead = sp.Popen([sys.executable, '-c', 'pass'])
        dead.wait()

        def folder(name, files=('chunk-000001.tsv',)):
            path = os.path.join(root, name)
            os.mkdir(path)
            for file in files:
                open(os.path.join(path, file), 'w').close()
            return path
        stale = folder('myflames-csv-%d-abc123' % dead.pid)
        live = folder('myflames-csv-%d-abc123' % os.getppid())
        mine = folder('myflames-csv-%d-abc123' % os.getpid())
        other = folder('myflames-csv-%d-xyz' % dead.pid, ('notes.txt',))
        foreign = folder('someone-else-%d' % dead.pid)
        removed = api.sweep_chunk_folders(root)
        if os.name == 'nt':
            self.assertEqual(removed, [])
            return
        self.assertEqual(removed, [stale])
        for path in (live, mine, other, foreign):
            self.assertTrue(os.path.isdir(path))


if __name__ == '__main__':
    unittest.main()
