"""Performance Schema instruments guide: ``myflames instruments``, the
Instruments tab API, the defaults per engine, the presets, and the map of
what each myflames view needs.

Replays live captures (fixtures/perf/instruments-*.json) from MySQL 8.4, 9.7
and 26.7 and MariaDB 11.4 and 13.0 with Performance Schema on and the engine
defaults, MariaDB with its defaults (Performance Schema off), MySQL 8.4 after
changes, MariaDB 13.0 after the Minimal preset, and a user without SELECT on
performance_schema. Every preset and toggle statement was also run on those
servers when the fixtures were recorded (see the CHANGELOG entry).
"""
import inspect
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from _perf_replay import PASSWORD, FakeFactory, ReplayConn, assert_read_only, fixture, replay, run_cli, run_main
from myflames import ps_instruments as m
from myflames import ui
from myflames.connector import ConnectorError
from myflames.perf_common import Server

TAG = '/*myflames:instruments*/ '
MYSQL = ('mysql-8.4', 'mysql-9.7', 'mysql-26.7')
MARIADB_PS = ('mariadb-11.4', 'mariadb-13.0')
MARIADB_OFF = ('mariadb-11.4-default', 'mariadb-13.0-default')
MODULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'myflames')


class RefusingConn(ReplayConn):
    """A replay whose fixture also lists statements the server refused."""

    def __init__(self, data):
        super(RefusingConn, self).__init__(data['calls'])
        self.errors = dict(data.get('errors') or [])

    def run(self, sql, extra_flags=None, timeout=None):
        if sql in self.errors:
            self.calls.append((sql, list(extra_flags or [])))
            raise ConnectorError(self.errors[sql])
        return super(RefusingConn, self).run(sql, extra_flags, timeout)


def replay_conn(name):
    data = fixture('instruments', name)
    return RefusingConn(data) if data.get('errors') else replay('instruments', name)[0]


def report(name):
    conn = replay_conn(name)
    result = m.instruments_report(conn)
    json.dumps(result, allow_nan=False)
    return result, conn


def by(items, key):
    return {item[key]: item for item in items}


def setup_rows(name):
    """(engine, {name: (enabled, timed, properties)}) from the recorded setup_instruments answer."""
    data = fixture('instruments', name)
    for sql, text in data['calls']:
        if 'FROM performance_schema.setup_instruments' in sql:
            rows = [line.split('\t') for line in text.split('\n') if line]
            return ('mysql' if name.startswith('mysql') else 'mariadb',
                    {r[0]: (r[1] == 'YES', None if r[2] == 'NULL' else r[2] == 'YES', r[3] if len(r) > 3 else '')
                     for r in rows})
    raise AssertionError('no setup_instruments read in ' + name)


class TestDefaultsMatchLiveServers(unittest.TestCase):
    """default_state() and default_consumer() reproduce fresh servers exactly."""

    def test_every_instrument_matches_its_engine_default(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                engine, rows = setup_rows(name)
                self.assertGreater(len(rows), 500)
                for instrument, (enabled, timed, props) in rows.items():
                    expected = m.default_state(engine, instrument, props)
                    self.assertIsNotNone(expected, instrument)
                    self.assertEqual(enabled, expected[0], instrument)
                    if expected[1] is not None:
                        self.assertEqual(timed, expected[1], instrument)

    def test_every_consumer_matches_its_engine_default(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, _ = report(name)
                for c in result['consumers']:
                    self.assertEqual(c['enabled'], c['default'], c['name'])
                self.assertEqual('events_statements_cpu' in by(result['consumers'], 'name'), name.startswith('mysql'))

    def test_defaults_preset_reports_in_place_on_fresh_servers(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, _ = report(name)
                presets = by(result['presets'], 'key')
                self.assertTrue(presets['defaults']['applied'])
                self.assertIsNone(presets['defaults']['restore_setup'])
                self.assertEqual(result['actors']['state'], 'default')
                self.assertEqual(result['objects']['state'], 'default')
                self.assertTrue(all(g['changed'] == 0 for g in result['groups'] if g['key'] != 'other'))

    def test_rules(self):
        self.assertEqual(m.default_state('mysql', 'wait/lock/metadata/sql/mdl'), (True, True))
        self.assertEqual(m.default_state('mariadb', 'wait/lock/metadata/sql/mdl'), (False, False))
        self.assertEqual(m.default_state('mysql', 'statement/sp/jump'), (False, False))
        self.assertEqual(m.default_state('mysql', 'statement/sp/stmt'), (True, True))
        self.assertEqual(m.default_state('mariadb', 'statement/sp/jump'), (True, True))
        self.assertEqual(m.default_state('mysql', 'stage/sql/Sending data', 'mutable'), (False, False))
        self.assertEqual(m.default_state('mysql', 'stage/innodb/buffer pool load', 'progress'), (True, True))
        self.assertEqual(m.default_state('mariadb', 'stage/innodb/buffer pool load'), (True, True))
        self.assertEqual(m.default_state('mysql', 'memory/sql/THD::main_mem_root'), (True, None))
        self.assertEqual(m.default_state('mariadb', 'memory/sql/THD::main_mem_root'), (False, None))
        self.assertEqual(m.default_state('mariadb', 'memory/performance_schema/threads'), (True, None))
        self.assertEqual(m.default_state('mysql', 'error'), (True, None))
        self.assertEqual(m.default_state('mysql', 'wait/synch/mutex/innodb/x'), (False, False))
        self.assertEqual(m.default_state('mysql', 'transaction'), (True, True))
        self.assertEqual(m.default_state('mariadb', 'transaction'), (False, False))
        self.assertIsNone(m.default_state('mysql', 'meter/unknown'))
        self.assertTrue(m.default_consumer('mysql', 'events_transactions_history'))
        self.assertFalse(m.default_consumer('mariadb', 'events_statements_current'))

    def test_consumer_hierarchy(self):
        self.assertEqual(m.consumer_chain('events_statements_history'),
                         ['events_statements_current', 'thread_instrumentation', 'global_instrumentation'])
        self.assertEqual(m.consumer_chain('events_waits_history_long'),
                         ['events_waits_current', 'thread_instrumentation', 'global_instrumentation'])
        self.assertEqual(m.consumer_chain('statements_digest'), ['global_instrumentation'])
        self.assertEqual(m.consumer_chain('events_statements_cpu'), ['global_instrumentation'])
        self.assertEqual(m.consumer_chain('global_instrumentation'), [])
        # Stage events check only global_instrumentation (pfs.cc start_stage).
        self.assertEqual(m.consumer_chain('events_stages_history'), ['events_stages_current', 'global_instrumentation'])


class TestIndividualInstruments(unittest.TestCase):
    def test_live_rows_and_defaults_for_both_engines(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, _ = report(name)
                engine, raw = setup_rows(name)
                rows = result['instruments']
                self.assertEqual([r['name'] for r in rows], sorted(raw))
                self.assertEqual(len(rows), len(raw))
                for row in rows:
                    enabled, timed, properties = raw[row['name']]
                    defaults = m.default_state(engine, row['name'], properties)
                    untimed = row['group'] in ('memory', 'error')
                    self.assertEqual(row['group'], m._group_of(row['name']))
                    self.assertEqual(row['enabled'], enabled)
                    self.assertEqual(row['timed'], None if untimed else timed)
                    self.assertEqual(row['timed_applies'], not untimed)
                    self.assertEqual(row['properties'], properties)
                    self.assertEqual(row['default_enabled'], defaults[0])
                    self.assertEqual(row['default_timed'], defaults[1])
                    self.assertEqual(row['builtin'], row['name'].startswith(m.BUILTIN_MEMORY))
                builtins = [r for r in rows if r['builtin']]
                self.assertTrue(builtins)
                self.assertTrue(all(r['enabled'] and r['timed'] is None for r in builtins))

    def test_unknown_defaults_and_untimed_server_flags(self):
        server = Server({'version': '8.4.11', 'performance_schema': 'ON'}, {})
        result = m.compute_instruments(server, {'instruments': [
            ['meter/unknown', 'YES', 'NO'], ['memory/sql/example', 'NO', 'YES'],
            ['error', 'YES', 'YES'], ['stage/sql/progress', 'YES', 'YES', 'progress'],
        ]})
        rows = by(result['instruments'], 'name')
        self.assertIsNone(rows['meter/unknown']['default_enabled'])
        self.assertIsNone(rows['meter/unknown']['default_timed'])
        self.assertTrue(rows['meter/unknown']['timed_applies'])
        self.assertIsNone(rows['memory/sql/example']['timed'])
        self.assertIsNone(rows['error']['timed'])
        self.assertTrue(rows['stage/sql/progress']['default_enabled'])
        self.assertEqual(rows['stage/sql/progress']['properties'], 'progress')

    def test_off_empty_and_unreadable_tables(self):
        for name in MARIADB_OFF + ('mysql-8.4-denied',):
            with self.subTest(server=name):
                result, _ = report(name)
                self.assertEqual(result['instruments'], [])
        server = Server({'version': '8.4.11', 'performance_schema': 'ON'}, {})
        for raw in ({}, {'instruments': []}):
            self.assertEqual(m.compute_instruments(server, raw)['instruments'], [])


class TestLiveReplay(unittest.TestCase):
    def test_read_only_and_shape(self):
        for name in MYSQL + MARIADB_PS + MARIADB_OFF + ('mysql-8.4-changed', 'mariadb-13.0-minimal'):
            with self.subTest(server=name):
                result, conn = report(name)
                assert_read_only(self, conn, TAG)
                self.assertEqual(result['schema_version'], 'instruments-1.0')
                self.assertEqual([p['key'] for p in result['presets']], list(m.PRESET_KEYS))
                self.assertIn('Collecting this report does not change settings', result['read_only_note'])
                for preset in result['presets']:
                    self.assertNotIn('targets', preset)

    def test_successful_instrument_fallback_clears_only_the_failed_read_note(self):
        # Only MySQL requests PROPERTIES; the three-column fallback supports older versions.
        for fallback_fails in (False, True):
            with self.subTest(fallback_fails=fallback_fails):
                def edit(sql, text, n):
                    if 'SELECT NAME, ENABLED, TIMED, PROPERTIES' in sql:
                        raise ConnectorError("ERROR 1054: Unknown column 'PROPERTIES'")
                    if fallback_fails and 'SELECT NAME, ENABLED, TIMED FROM performance_schema.setup_instruments' in sql:
                        raise ConnectorError('ERROR 1146: setup_instruments unavailable')
                    if 'FROM performance_schema.setup_actors' in sql:
                        raise ConnectorError('ERROR 1142: setup_actors denied')
                    return text
                data = fixture('instruments', 'mysql-8.4')
                calls = list(data['calls'])
                sql, text = next((s, t) for s, t in calls if 'FROM performance_schema.setup_instruments' in s)
                fallback = '\n'.join('\t'.join(row.split('\t')[:3]) for row in text.splitlines()) + '\n'
                calls.append((sql.replace(', PROPERTIES', ''), fallback))
                result = m.instruments_report(ReplayConn(calls, edit=edit))
                self.assertTrue(any('Could not read performance_schema.setup_actors:' in n for n in result['notes']))
                failures = [n for n in result['notes'] if n.startswith('Could not read performance_schema.setup_instruments:')]
                self.assertEqual(bool(failures), fallback_fails)
                if not fallback_fails:
                    self.assertTrue(result['available'])
                    self.assertTrue(result['instruments'])

    def test_mysql_defaults(self):
        for name in MYSQL:
            with self.subTest(server=name):
                result, _ = report(name)
                self.assertTrue(result['available'])
                groups = by(result['groups'], 'key')
                self.assertEqual(groups['wait/synch']['state'], 'off')
                self.assertEqual(groups['wait/io/file']['state'], 'on')
                self.assertEqual(groups['wait/lock/metadata']['state'], 'on')
                self.assertEqual(groups['memory']['state'], 'on')
                self.assertEqual(groups['error']['timed'], None)
                self.assertEqual(groups['memory']['builtin'], 79)
                self.assertEqual(groups['stage']['enabled'], 16)
                self.assertEqual([k['kind'] for k in groups['wait/synch']['kinds']],
                                 ['mutex', 'rwlock', 'sxlock', 'prlock', 'cond'])
                self.assertEqual({f['key']: f['status'] for f in result['features']},
                                 {f['key']: 'off' if f['key'] == 'replication_receiver' else 'ready'
                                  for f in m.FEATURES})
                presets = by(result['presets'], 'key')
                self.assertTrue(presets['minimal']['applied'])
                self.assertTrue(presets['locks']['applied'])
                self.assertFalse(presets['waits']['applied'])
                self.assertGreater(result['status']['memory_bytes'], 100 * 2 ** 20)
                self.assertEqual(result['status']['sizes']['performance_schema_events_statements_history_size'], 10)
                self.assertIsNone(result['status']['enable'])

    def test_mariadb_defaults_with_performance_schema_on(self):
        for name in MARIADB_PS:
            with self.subTest(server=name):
                result, _ = report(name)
                groups = by(result['groups'], 'key')
                self.assertNotIn('error', groups)
                self.assertEqual(groups['wait/lock/metadata']['state'], 'off')
                self.assertEqual(groups['transaction']['state'], 'off')
                self.assertEqual(groups['memory']['state'], 'off')
                self.assertEqual(groups['memory']['builtin'], 70)
                self.assertEqual(groups['stage']['enabled'], 8)
                self.assertEqual(groups['memory']['default'], "off (Performance Schema's own memory always counted)")
                features = by(result['features'], 'key')
                for key in ('top', 'waits', 'indexes', 'io'):
                    self.assertEqual(features[key]['status'], 'ready', key)
                for key in ('capture', 'locks_mdl', 'locks_last', 'replication_receiver'):
                    self.assertEqual(features[key]['status'], 'off', key)
                self.assertEqual(features['locks_rows']['status'], 'not_needed')
                self.assertIn('consumer events_statements_history is off', features['capture']['missing'])
                self.assertIn('wait/lock/metadata/sql/mdl is off', features['locks_mdl']['missing'])
                self.assertEqual(features['locks_mdl']['rows'], ['group:wait/lock/metadata'])
                self.assertEqual(features['capture']['rows'],
                                 ['consumer:events_statements_current', 'consumer:events_statements_history'])
                presets = by(result['presets'], 'key')
                self.assertFalse(presets['minimal']['applied'])
                self.assertTrue(presets['defaults']['applied'])
                self.assertTrue(any('MariaDB ships Performance Schema off' in n for n in result['notes']))

    def test_performance_schema_off(self):
        for name in MARIADB_OFF:
            with self.subTest(server=name):
                result, conn = report(name)
                self.assertFalse(result['available'])
                self.assertEqual(result['unavailable'], 'performance_schema_off')
                self.assertFalse(any('performance_schema.' in sql for sql, _ in conn.calls))
                self.assertIn('restart', result['notes'][0])
                self.assertIn("MariaDB's default", result['notes'][0])
                self.assertEqual(result['status']['enable']['config'], '[mysqld]\nperformance_schema = ON')
                self.assertIsNone(result['status']['enable']['persist'])
                self.assertTrue(all(g['state'] == 'unknown' and g['default'] for g in result['groups']))
                self.assertTrue(all(c['enabled'] is None for c in result['consumers']))
                statuses = {f['key']: f['status'] for f in result['features']}
                self.assertEqual(statuses.pop('locks_rows'), 'not_needed')
                self.assertEqual(set(statuses.values()), {'unavailable'})
                self.assertTrue(all(p['applied'] is None for p in result['presets']))
                self.assertIsNone(result['actors'])

    def test_mysql_off_offers_set_persist_only(self):
        server = Server({'version': '8.4.11', 'version_comment': 'MySQL', 'performance_schema': 'OFF'}, {})
        result = m.compute_instruments(server, {})
        self.assertEqual(result['status']['enable']['persist'], 'SET PERSIST_ONLY performance_schema = ON;')
        self.assertIn('PERSIST_RO_VARIABLES_ADMIN', result['status']['enable']['persist_text'])

    def test_changed_server(self):
        result, _ = report('mysql-8.4-changed')
        groups = by(result['groups'], 'key')
        synch = groups['wait/synch']
        self.assertEqual(synch['state'], 'partial')
        self.assertEqual(synch['changed'], synch['enabled'])
        self.assertEqual(by(synch['kinds'], 'kind')['mutex']['enabled'], by(synch['kinds'], 'kind')['mutex']['total'])
        self.assertEqual(groups['wait/io/table']['state'], 'counted')
        self.assertEqual(groups['wait/io/table']['changed'], 1)
        self.assertEqual(groups['wait/lock/metadata']['state'], 'off')
        consumers = by(result['consumers'], 'name')
        history = consumers['events_statements_history']
        self.assertTrue(history['enabled'])
        self.assertFalse(history['effective'])
        self.assertEqual(history['blocked_by'], ['events_statements_current'])
        self.assertTrue(consumers['events_waits_history_long']['enabled'])
        features = by(result['features'], 'key')
        self.assertEqual(features['capture']['status'], 'off')
        self.assertIn('consumer events_statements_history is on but blocked by events_statements_current',
                      features['capture']['missing'])
        self.assertEqual(features['locks_mdl']['status'], 'off')
        self.assertEqual(features['indexes']['status'], 'partial')
        self.assertIn('counted but not timed', features['indexes']['missing'][0])
        self.assertIn('objects', features['indexes']['rows'])
        self.assertEqual(features['top']['status'], 'ready')
        self.assertEqual(result['actors']['state'], 'custom')
        self.assertEqual(result['objects']['state'], 'custom')
        self.assertEqual(result['objects']['disabled_table_schemas'],
                         ['archive', 'information_schema', 'mysql', 'performance_schema'])
        presets = by(result['presets'], 'key')
        self.assertFalse(presets['defaults']['applied'])
        self.assertIn('TRUNCATE TABLE performance_schema.setup_actors;', presets['defaults']['restore_setup'])
        self.assertIn('TRUNCATE TABLE performance_schema.setup_objects;', presets['defaults']['restore_setup'])
        self.assertFalse(presets['minimal']['applied'])
        notes = ' '.join(result['notes'])
        self.assertIn('events_statements_history is on but records nothing while events_statements_current is off',
                      notes)
        self.assertIn('setup_actors is not the default (2 rows)', notes)
        self.assertIn('sessions it does not instrument with HISTORY are not recorded',
                      ' '.join(features['capture']['optional_missing']))
        self.assertNotIn('actors', features['locks_mdl']['rows'])

    def test_empty_setup_actors(self):
        conn, _ = replay('instruments', 'mysql-8.4', edit=lambda sql, text, n: '' if 'setup_actors' in sql else text)
        result = m.instruments_report(conn)
        self.assertEqual(result['actors']['state'], 'empty')
        self.assertIn('TRUNCATE TABLE performance_schema.setup_actors;', result['actors']['restore'])
        features = by(result['features'], 'key')
        for key in ('top', 'capture', 'locks_last', 'indexes'):
            self.assertEqual(features[key]['status'], 'off', key)
            self.assertIn('setup_actors is empty, so no session is recorded', features[key]['missing'])
            self.assertIn('actors', features[key]['rows'])
        # Metadata locks ignore setup_actors; background threads still record file and table waits.
        self.assertEqual(features['locks_mdl']['status'], 'ready')
        self.assertNotIn('actors', features['locks_mdl']['rows'])
        for key in ('waits', 'io'):
            self.assertEqual(features[key]['status'], 'ready', key)
            self.assertIn('background threads still are', ' '.join(features[key]['optional_missing']))

    def test_minimal_preset_lights_up_views_without_optional_receiver_guard(self):
        result, _ = report('mariadb-13.0-minimal')
        self.assertTrue(by(result['presets'], 'key')['minimal']['applied'])
        self.assertFalse(by(result['presets'], 'key')['defaults']['applied'])
        statuses = {f['key']: f['status'] for f in result['features']}
        self.assertEqual(statuses.pop('replication_receiver'), 'off')
        self.assertEqual(statuses.pop('locks_rows'), 'not_needed')
        self.assertEqual(set(statuses.values()), {'ready'})

    def test_denied(self):
        result, conn = report('mysql-8.4-denied')
        self.assertFalse(result['available'])
        self.assertEqual(result['unavailable'], 'denied')
        self.assertEqual(len(conn.calls), 3)
        self.assertIn('SELECT command denied', result['notes'][0])
        self.assertTrue(any('GRANT SELECT ON performance_schema.* TO <user>;' in n for n in result['notes']))
        self.assertEqual({f['status'] for f in result['features'] if f['key'] != 'locks_rows'}, {'unknown'})
        self.assertTrue(all(p['applied'] is None for p in result['presets']))

    def test_connection_errors_become_value_errors(self):
        conn = ReplayConn([], error=ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect"))
        with self.assertRaisesRegex(ValueError, "Could not read the Performance Schema setup: ERROR 2003"):
            m.instruments_report(conn)

    def test_text_output(self):
        result, _ = report('mariadb-13.0')
        text = m.format_text(result)
        self.assertTrue(text.startswith('MariaDB 13.0.2'))
        for heading in ('MYFLAMES VIEWS', 'INSTRUMENTS (setup_instruments)', 'CONSUMERS (setup_consumers)',
                        'PRESETS'):
            self.assertIn(heading, text)
        self.assertRegex(text, r'off\s+Locks: metadata locks\s+wait/lock/metadata/sql/mdl is off')
        self.assertIn('Collecting this report does not change settings', text)
        self.assertIn('GRANT UPDATE ON performance_schema.setup_instruments', text)


class TestStatements(unittest.TestCase):
    """The text a user copies: shape and engine differences."""

    def test_every_statement_is_a_setup_update(self):
        for engine in ('mysql', 'mariadb'):
            for preset in m.presets(engine):
                for block in (preset['runtime'], preset['undo'] or ''):
                    for stmt in filter(None, (s.strip() for s in block.split(';'))):
                        self.assertRegex(stmt, r'^UPDATE performance_schema\.setup_(instruments|consumers)\s')
                self.assertTrue(preset['config'].lstrip('# \n').count('[mysqld]') <= 1)
            for group in m.GROUPS:
                sql = m.group_sql(group)
                self.assertTrue(sql['on']['runtime'].startswith('UPDATE performance_schema.setup_instruments SET '))
                self.assertTrue(sql['on']['config'].startswith("performance-schema-instrument = '"))
                self.assertTrue(sql['off']['config'].endswith("=OFF'"))
        memory = m.group_sql(next(g for g in m.GROUPS if g['key'] == 'memory'))
        self.assertNotIn('TIMED', memory['on']['runtime'])

    def test_consumer_statements_turn_on_what_they_depend_on(self):
        sql = m.consumer_sql('events_statements_history')
        self.assertEqual(sql['on']['runtime'],
                         "UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' WHERE NAME IN "
                         "('global_instrumentation', 'thread_instrumentation', 'events_statements_current', "
                         "'events_statements_history');")
        self.assertIn('performance-schema-consumer-events-statements-current = ON', sql['on']['config'])
        self.assertEqual(sql['off']['config'], 'performance-schema-consumer-events-statements-history = OFF')
        self.assertEqual(m.consumer_sql('global_instrumentation')['on']['runtime'],
                         "UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' WHERE NAME = "
                         "'global_instrumentation';")

    def test_minimal_and_locks_presets(self):
        mysql, mariadb = by(m.presets('mysql'), 'key'), by(m.presets('mariadb'), 'key')
        self.assertIn("NAME NOT LIKE 'statement/sp/%' OR NAME = 'statement/sp/stmt'", mysql['minimal']['runtime'])
        self.assertNotIn('statement/sp/', mariadb['minimal']['runtime'])
        self.assertEqual(mysql['minimal']['config'].split('\n')[:2], ['[mysqld]', 'performance_schema = ON'])
        self.assertNotIn('performance-schema-instrument', mysql['minimal']['config'])
        self.assertEqual(mariadb['minimal']['config'],
                         "[mysqld]\nperformance_schema = ON\n"
                         "performance-schema-instrument = 'wait/lock/metadata/sql/mdl=ON'\n"
                         "performance-schema-consumer-events-statements-current = ON\n"
                         "performance-schema-consumer-events-statements-history = ON")
        self.assertIn("performance-schema-instrument = 'transaction=ON'", mariadb['locks']['config'])
        for preset in ('minimal', 'locks', 'defaults'):
            self.assertIsNone(mysql[preset]['undo'])
        self.assertIn('data_locks', mysql['locks']['notes'][0])

    def test_waits_preset_is_temporary(self):
        waits = by(m.presets('mariadb'), 'key')['waits']
        self.assertEqual(waits['cost'][0], 'high')
        self.assertIn("NAME LIKE 'wait/synch/%' OR NAME LIKE 'wait/io/socket/%'", waits['undo'])
        self.assertTrue(waits['config'].startswith('# Keep this preset temporary.'))
        self.assertTrue(any('MariaDB ships off' in n for n in waits['notes']))
        self.assertEqual(len(by(m.presets('mysql'), 'key')['waits']['notes']), 1)

    def test_defaults_preset_per_engine(self):
        mysql, mariadb = by(m.presets('mysql'), 'key')['defaults'], by(m.presets('mariadb'), 'key')['defaults']
        self.assertIn("FIND_IN_SET('progress', PROPERTIES)", mysql['runtime'])
        self.assertNotIn('PROPERTIES', mariadb['runtime'])
        for stage in m.MARIADB_PROGRESS_STAGES:
            self.assertIn("'%s'" % stage, mariadb['runtime'])
        self.assertIn("'wait/lock/metadata/sql/mdl', 'transaction');", mariadb['runtime'].split('\n')[2])
        self.assertIn("'events_transactions_history'), 'YES', 'NO');", mysql['runtime'])
        self.assertIn("('global_instrumentation', 'thread_instrumentation', 'statements_digest'), 'YES', 'NO');",
                      mariadb['runtime'])
        self.assertTrue(all(line.startswith('#') for line in mysql['config'].split('\n')))

    def test_restore_never_uses_delete(self):
        # MariaDB's DELETE on setup_objects removes the TABLE row with the same
        # schema and name, whatever the OBJECT_TYPE; TRUNCATE resets correctly.
        restore = m.objects_restore()
        self.assertTrue(restore.startswith('TRUNCATE TABLE performance_schema.setup_objects;'))
        self.assertNotIn('DELETE', restore + m.ACTORS_RESTORE)
        self.assertEqual(len(m.default_objects()), 20)
        self.assertEqual(restore.count("'NO', 'NO')"), 15)

    def test_wording(self):
        for engine in ('mysql', 'mariadb'):
            for preset in m.presets(engine):
                text = ' '.join([preset['summary'], preset['gain'], preset['cost'][1]] + preset['notes'])
                self.assertNotIn('—', text)
                self.assertNotIn(' - ', text)
        for group in m.GROUPS:
            for value in (group['gain'], group['lose'], group['cost'][1]):
                self.assertNotIn('—', value)
        for info in m.CONSUMERS.values():
            for value in (info['gain'], info['lose'], info['cost'][1]):
                self.assertNotIn('—', value)
        self.assertEqual(set(m.CONSUMERS), set(m.CONSUMER_ORDER))


INSTRUMENT_LITERAL = re.compile(r"(?<![\w/])((?:wait|statement|stage|memory)/[A-Za-z0-9_/%]*)")
CONSUMER_LITERAL = re.compile(r"\b(global_instrumentation|thread_instrumentation|statements_digest|"
                              r"events_(?:statements|stages|transactions|waits)_(?:current|history_long|history|cpu))\b")
TABLE_LITERAL = re.compile(r"performance_schema\.([a-z_]+)")
VIEW_MODULES = ('discover', 'live', 'locks', 'waits', 'indexes', 'io_stats', 'perf_common', 'config_review', 'health',
                'report', 'server_checks', 'replication', 'variables', 'error_log', 'cluster')


class TestReplicationReceiverFeature(unittest.TestCase):

    def test_defaults_name_only_the_engine_stage(self):
        for name in MYSQL + MARIADB_PS:
            with self.subTest(server=name):
                result, _ = report(name)
                entry = by(result['features'], 'key')['replication_receiver']
                instrument = 'stage/sql/Checking %s version' % ('source' if name.startswith('mysql') else 'master')
                self.assertEqual(entry['status'], 'off')
                self.assertEqual(entry['missing'], [instrument + ' is off'])
                self.assertEqual(entry['rows'], ['group:stage'])
                engine, instruments = setup_rows(name)
                self.assertEqual(instruments[instrument][:2], (False, False))
                self.assertEqual(m.default_state(engine, instrument), (False, False))

    def test_counted_stage_and_consumers_enable_guard_without_actors_or_timing(self):
        for engine, instrument in (('mysql', 'stage/sql/Checking source version'),
                                   ('mariadb', 'stage/sql/Checking master version')):
            server = Server({'version': '8.4.11' if engine == 'mysql' else '11.4.13-MariaDB',
                             'performance_schema': 'ON'}, {})
            consumers = [{'name': n, 'enabled': True, 'effective': True, 'blocked_by': []}
                         for n in ('global_instrumentation', 'thread_instrumentation')]
            for disabled in (None, 'global_instrumentation', 'thread_instrumentation'):
                with self.subTest(engine=engine, disabled=disabled):
                    current = [dict(c, enabled=c['name'] != disabled, effective=c['name'] != disabled)
                               for c in consumers]
                    features = m._features(server, {instrument: (True, False, '')}, current,
                                           {'state': 'empty'}, None)
                    entry = by(features, 'key')['replication_receiver']
                    self.assertEqual(entry['status'], 'off' if disabled else 'ready')
                    self.assertNotIn('actors', entry['rows'])
                    if disabled:
                        self.assertEqual(entry['missing'], ['consumer %s is off' % disabled])
                    else:
                        self.assertEqual(entry['missing'], [])

    def test_stage_group_and_consumers_link_to_replication(self):
        for features in (m._features_for_instrument('stage/%'),
                         m._features_for_consumer('global_instrumentation'),
                         m._features_for_consumer('thread_instrumentation')):
            entry = by(features, 'key')['replication_receiver']
            self.assertEqual(entry['view'], 'replication')
            self.assertTrue(entry['required'])
        self.assertEqual(m.TABLES['events_stages_summary_by_thread_by_event_name'], ('replication_receiver',))


class TestFeatureMapStaysInSync(unittest.TestCase):
    """Fails when a view module starts reading a Performance Schema table, or
    naming an instrument or consumer, that ps_instruments.FEATURES does not map."""

    def source(self, module):
        with open(os.path.join(MODULES_DIR, module + '.py'), encoding='utf-8') as handle:
            return handle.read()

    def module_features(self, module):
        return [f for f in m.FEATURES if module in f['modules']]

    def test_every_table_is_mapped(self):
        for module in VIEW_MODULES:
            for table in set(TABLE_LITERAL.findall(self.source(module))):
                with self.subTest(module=module, table=table):
                    self.assertIn(table, m.TABLES, 'map performance_schema.%s in ps_instruments.TABLES' % table)
                    for key in m.TABLES[table]:
                        feature = next(f for f in m.FEATURES if f['key'] == key)
                        self.assertIn(module, feature['modules'] + ('perf_common',))

    def test_every_instrument_named_is_needed_by_the_module(self):
        for module in VIEW_MODULES:
            needs = []
            for f in self.module_features(module):
                for kind in ('needs', 'optional'):
                    needs.extend(f.get(kind, {}).get('instruments', ()))
            for literal in set(INSTRUMENT_LITERAL.findall(self.source(module))):
                prefix = literal.replace('%', '')
                with self.subTest(module=module, instrument=literal):
                    self.assertTrue(any(n.rstrip('%').startswith(prefix) or prefix.startswith(n.rstrip('%'))
                                        for n in needs),
                                    '%s names %s, which FEATURES does not map' % (module, literal))

    def test_every_consumer_named_is_needed_by_the_module(self):
        for module in VIEW_MODULES:
            needs = set(m.FILTER_CONSUMERS)
            for f in self.module_features(module):
                for kind in ('needs', 'optional'):
                    needs.update(f.get(kind, {}).get('consumers', ()))
            for literal in set(CONSUMER_LITERAL.findall(self.source(module))):
                with self.subTest(module=module, consumer=literal):
                    self.assertIn(literal, needs, '%s names consumer %s, which FEATURES does not map'
                                  % (module, literal))

    def test_every_mapped_table_is_still_read(self):
        for f in m.FEATURES:
            sources = ''.join(self.source(module) for module in f['modules'])
            for table in f['tables']:
                with self.subTest(feature=f['key'], table=table):
                    self.assertIn('performance_schema.' + table, sources)

    def test_groups_and_consumers_name_the_views_they_feed(self):
        groups = by(m.compute_instruments(Server({'version': '8.4.11', 'performance_schema': 'OFF'}, {}),
                                          {})['groups'], 'key')
        self.assertEqual([f['key'] for f in groups['wait/io/table']['features']], ['waits', 'indexes', 'io'])
        self.assertEqual([f['key'] for f in groups['statement']['features']], ['top', 'capture', 'locks_last'])
        self.assertEqual([(f['key'], f['required']) for f in groups['wait/synch']['features']], [('waits', False)])
        self.assertEqual(groups['memory']['features'], [])
        self.assertEqual([f['key'] for f in m._features_for_consumer('statements_digest')], ['top'])


class InstrumentsCLI(unittest.TestCase):
    def call(self, name, *argv, **kw):
        conn = kw.get('conn') or replay_conn(name)
        return run_cli('instruments', argv, conn) + (conn,)

    def test_text_json_and_connection_flags(self):
        code, out, err, factory, conn = self.call('mysql-8.4', '-h', 'db', '-P', '3307', '-u', 'app',
                                                  '-p' + PASSWORD, '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['schema_version'], 'instruments-1.0')
        self.assertEqual((factory.options['host'], factory.options['port']), ('db', 3307))
        self.assertNotIn(PASSWORD, out + err)
        assert_read_only(self, conn, TAG)
        code, out, err, _, _ = self.call('mariadb-13.0')
        self.assertEqual(code, 0)
        self.assertIn('MYFLAMES VIEWS', out)
        self.assertIn('note: MariaDB ships Performance Schema off', err)

    def test_preset_text_and_json(self):
        code, out, err, _, _ = self.call('mariadb-13.0', '--preset', 'minimal')
        self.assertEqual((code, err), (0, ''))
        self.assertTrue(out.startswith('-- Minimal for myflames (MariaDB 13.0.2'))
        self.assertIn("UPDATE performance_schema.setup_consumers SET ENABLED = 'YES'", out)
        self.assertIn("performance-schema-instrument = 'wait/lock/metadata/sql/mdl=ON'", out)
        code, out, _, _, _ = self.call('mysql-8.4', '--preset', 'waits', '--json')
        preset = json.loads(out)
        self.assertEqual((code, preset['key'], preset['engine'], preset['applied']), (0, 'waits', 'mysql', False))
        self.assertIn('wait/synch', preset['undo'])
        code, out, _, _, _ = self.call('mysql-8.4', '--preset', 'defaults')
        self.assertIn('-- This server already matches this preset.', out)

    def test_performance_schema_off_exits_0_with_the_how_to(self):
        code, out, err, _, _ = self.call('mariadb-13.0-default')
        self.assertEqual(code, 0)
        self.assertIn('Performance Schema: OFF', out)
        code, out, err, _, _ = self.call('mariadb-13.0-default', '--preset', 'minimal')
        self.assertIn('-- Performance Schema is off: turn it on in my.cnf and restart first', out)

    def test_output_file_and_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'instruments.sql')
            code, out, err, _, _ = self.call('mysql-8.4', '--preset', 'locks', '-o', path)
            self.assertEqual((code, out), (0, ''))
            self.assertIn('Written to', err)
            with open(path, encoding='utf-8') as handle:
                self.assertIn('Lock troubleshooting', handle.read())
            code, out, err, _, _ = self.call('mysql-8.4', '-o', path, '-q')
            self.assertEqual((code, out, err), (0, '', ''))

    def test_bad_arguments_and_failures_exit_2(self):
        conn = ReplayConn([])
        code, _, err, _ = run_cli('instruments', ['--preset', 'everything'], conn)
        self.assertEqual(code, 2)
        self.assertIn('invalid choice', err)
        self.assertEqual(conn.calls, [])
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied for ' + PASSWORD))
        code, out, err, _ = run_cli('instruments', ['-u', 'app', '-p' + PASSWORD], conn)
        self.assertEqual((code, out), (2, ''))
        self.assertNotIn(PASSWORD, err)
        self.assertIn('myflames: Could not read the Performance Schema setup', err)

    def test_root_help_and_dispatch(self):
        code, out, _ = run_main(['myflames', '--help'])
        self.assertEqual(code, 0)
        self.assertIn('instruments  Performance Schema instruments/consumers', out)
        self.assertIn('myflames instruments -h localhost -u admin -p --preset minimal', out)
        code, out, err = run_main(['myflames', 'instruments', '--json', '-q'], replay('instruments', 'mysql-26.7')[0])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['server_version'], '26.7.0')


class InstrumentsAPI(unittest.TestCase):
    connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'password': PASSWORD}

    def call(self, name, payload, workspace=object()):
        conn, _ = replay('instruments', name)
        factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory):
            result = ui.api_response('/api/live/instruments', payload, workspace=workspace)
        assert_read_only(self, conn, TAG)
        return result, factory

    def test_report(self):
        result, factory = self.call('mariadb-11.4', {'connection': self.connection})
        self.assertEqual(result['schema_version'], 'instruments-1.0')
        self.assertEqual(factory.options['password'], PASSWORD)
        self.assertEqual(result['engine'], 'mariadb')

    def test_errors_never_echo_the_password(self):
        conn = ReplayConn([], error=ConnectorError('mysql client exited 1: ERROR 1045: denied ' + PASSWORD))
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)):
            with self.assertRaises(ValueError) as caught:
                ui.api_response('/api/live/instruments', {'connection': self.connection}, workspace=object())
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_saved_password_and_allowlist(self):
        class Store(object):
            def connection_with_saved_password(self, connection):
                return dict(connection, password='from-keychain')

        class Workspace(object):
            store = Store()
        _, factory = self.call('mysql-8.4', {'connection': {'host': '127.0.0.1', 'profile_id': 'p1'}},
                               workspace=Workspace())
        self.assertEqual(factory.options['password'], 'from-keychain')
        self.assertIn('"/api/live/instruments"', inspect.getsource(ui.WorkspaceHandler.do_POST))
        self.assertIn('/api/live/instruments', ui._CONNECTION_PATHS)
        self.assertIn('/api/live/instruments', ui._SERVER_VIEW_PATHS)


if __name__ == '__main__':
    unittest.main()
