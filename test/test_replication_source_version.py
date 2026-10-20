"""The source server's version on each replication channel
(replication.attach_source_versions, ``myflames replication``,
/api/live/replication).

Replays live recordings made on 2026-10-20 (fixtures/perf/replication-*-srcver-*.json)
from throwaway Docker pairs whose replica shares the source's network
namespace, so the replica reports its source as 127.0.0.1:<port> and a saved
connection on this computer can name the same server:

* MySQL 9.7.2 replica 127.0.0.1:33298 of the source 127.0.0.1:33297 (server
  IDs 302 and 301): through the saved connection, from the relay log after a
  receiver connect and after FLUSH RELAY LOGS (the copied event), without
  REPLICATION SLAVE (unknown), and with a saved connection to localhost that
  the client could not reach (falls back to the relay log).
* MariaDB 11.8.9 replica 127.0.0.1:33319 of 127.0.0.1:33318 (402 and 401):
  through the saved connection, from the relay log (SLAVE MONITOR only), after
  FLUSH RELAY LOGS, and after RESET SLAVE with only the SQL thread started (no
  event from the source yet: unknown).

``source_calls`` in a recording are the saved connection's own answers.
Edits marked synthetic below change a recorded answer to reach a branch the
live servers could not show (another server behind a saved connection).
"""
import json
import os
import types
import unittest
from unittest import mock

from _perf_replay import FakeFactory, ReplayConn, fixture as recorded_fixture, run_cli
from myflames import replication as rp, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore
from test_replication import ReplicationCase, fixture

TAG = rp.REPLICATION_TAG
MYSQL_SOURCE = {'id': 'p-mysql', 'name': 'Source 9.7', 'host': '127.0.0.1', 'port': 33297, 'user': 'probe',
                'ssl_mode': 'PREFERRED', 'remember_password': True}
MARIADB_SOURCE = {'id': 'p-maria', 'name': 'Source 11.8', 'host': '127.0.0.1', 'port': 33318, 'user': 'probe',
                  'ssl_mode': 'PREFERRED', 'remember_password': True}


def recording(name):
    return recorded_fixture('replication', name)


def source_conn(name, edit=None):
    calls = [call for calls in recording(name)['source_calls'].values() for call in calls]
    return ReplayConn(calls, edit=edit)


def read(name, profiles=(), open_profile=None, replica_host='127.0.0.1', edit=None):
    conn = ReplayConn(fixture('replication', name)['calls'], edit=edit)
    report = rp.read_status(conn, source_versions=True, profiles=profiles, open_profile=open_profile,
                            replica_host=replica_host)
    return report, conn


def relay_calls(conn):
    return [sql for sql, _flags in conn.calls if 'RELAYLOG' in sql]


class TestRelayLog(ReplicationCase):

    def test_mysql_after_connect(self):
        report, conn = read('mysql-9.7-srcver-relay')
        version = report['channels'][0]['source_version']
        self.assertEqual(version['version'], '9.7.2')
        self.assertEqual(version['engine'], 'mysql')
        self.assertEqual(version['provenance'], 'relay_log')
        self.assertEqual(version['label'], 'as recorded in the relay log')
        self.assertEqual(version['relay_log_file'], '025357c3d20b-relay-bin.000003')
        self.assertTrue(version['verified'])
        self.assertIn('Right after a source upgrade', version['caveat'])
        self.assertIsNone(version['reason'])
        self.assertTrue(report['capabilities']['source_version'])
        self.assertEqual(relay_calls(conn), [TAG + "SHOW RELAYLOG EVENTS IN '025357c3d20b-relay-bin.000003' "
                                                   "LIMIT 4 FOR CHANNEL ''"])

    def test_copied_event_after_flush_relay_logs(self):
        # Live: own header (302), Previous_gtids, the copied source event (301,
        # End_log_pos 0), the next Gtid; MariaDB: own header (402), the copy (401), a Gtid.
        for name, version, engine in (('mysql-9.7-srcver-relay-flushed', '9.7.2', 'mysql'),
                                      ('mariadb-11.8-srcver-relay-flushed', '11.8.9-MariaDB-ubu2404-log', 'mariadb')):
            with self.subTest(name=name):
                report, _ = read(name)
                found = report['channels'][0]['source_version']
                self.assertEqual((found['version'], found['engine'], found['provenance']), (version, engine, 'relay_log'))

    def test_mariadb_with_slave_monitor_only(self):
        report, conn = read('mariadb-11.8-srcver-relay')
        found = report['channels'][0]['source_version']
        self.assertEqual(found['version'], '11.8.9-MariaDB-ubu2404-log')
        self.assertEqual(found['engine'], 'mariadb')
        self.assertEqual(relay_calls(conn), [TAG + "SHOW RELAYLOG '' EVENTS IN 'mysqld-relay-bin.000002' LIMIT 3"])

    def test_off_by_default_and_for_skip_reads(self):
        conn = ReplayConn(fixture('replication', 'mysql-9.7-srcver-relay')['calls'])
        report = rp.read_status(conn)
        self.assertIsNone(report['channels'][0]['source_version'])
        self.assertFalse(report['capabilities']['source_version'])
        self.assertEqual(relay_calls(conn), [])

    def test_read_only_and_tagged(self):
        for name in ('mysql-9.7-srcver-relay', 'mariadb-11.8-srcver-relay', 'mysql-9.7-srcver-profile'):
            with self.subTest(name=name):
                profiles = [MYSQL_SOURCE]
                source = source_conn('mysql-9.7-srcver-profile')
                _, conn = read(name, profiles, lambda p: source)
                for sql, flags in conn.calls + source.calls:
                    self.assertTrue(sql.startswith(TAG), sql)
                    self.assertRegex(sql[len(TAG):], r'^(SELECT|SHOW) ')
                    self.assertIn('--comments', flags)


class TestUnknown(ReplicationCase):

    def test_mysql_without_replication_slave(self):
        report, _ = read('mysql-9.7-srcver-denied')
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertEqual(found['label'], 'Version unknown')
        self.assertEqual(found['reason'], 'Reading the relay log (SHOW RELAYLOG EVENTS) needs the REPLICATION SLAVE '
                                          'privilege.')
        self.assertEqual(found['hint'], 'Add a saved connection for 127.0.0.1:33297 to show its version.')
        self.assertIn('Source version: unknown. Reading the relay log', rp.format_text(report))

    def test_mariadb_before_the_receiver_connects(self):
        report, _ = read('mariadb-11.8-srcver-unknown')
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIn('no format event from the source (server ID 401)', found['reason'])
        self.assertEqual(found['hint'], 'Add a saved connection for 127.0.0.1:33318 to show its version.')

    def test_source_server_id_not_recorded(self):
        # Synthetic: Source_Server_Id 0, as before the receiver ever connected.
        edit = lambda sql, text, n: text.replace('<field name="Source_Server_Id">301</field>',
                                                 '<field name="Source_Server_Id">0</field>') if text else text
        report, _ = read('mysql-9.7-srcver-relay', edit=edit)
        self.assertIn("has not recorded its source's server ID", report['channels'][0]['source_version']['reason'])

    def test_relay_file_gone(self):
        sql = TAG + "SHOW RELAYLOG EVENTS IN '025357c3d20b-relay-bin.000003' LIMIT 4 FOR CHANNEL ''"
        calls = [[sql, None, 'mysql client exited 1: ERROR 1220 (HY000) at line 1: Error when executing command '
                  'SHOW BINLOG EVENTS: Could not find target log'] if call[0] == sql else call
                 for call in fixture('replication', 'mysql-9.7-srcver-relay')['calls']]
        report = rp.read_status(ReplayConn(calls), source_versions=True)
        self.assertIn('Could not read the relay log 025357c3d20b-relay-bin.000003: ERROR 1220',
                      report['channels'][0]['source_version']['reason'])


class TestSavedConnection(ReplicationCase):

    def test_mysql_profile_wins_and_relay_is_not_read(self):
        source = source_conn('mysql-9.7-srcver-profile')
        report, conn = read('mysql-9.7-srcver-profile', [MYSQL_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['version'], '9.7.2')
        self.assertEqual(found['version_comment'], 'MySQL Community Server - GPL')
        self.assertEqual(found['provenance'], 'saved_connection')
        self.assertEqual(found['label'], 'from the saved connection Source 9.7')
        self.assertEqual((found['profile_id'], found['profile_name']), ('p-mysql', 'Source 9.7'))
        self.assertTrue(found['verified'])
        self.assertEqual(relay_calls(conn), [])
        self.assertEqual(source.calls[0][0], TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                                                   "('server_id', 'server_uuid', 'version', 'version_comment')")
        self.assertIn('Source version: 9.7.2 (saved connection Source 9.7)', rp.format_text(report))

    def test_mariadb_profile(self):
        source = source_conn('mariadb-11.8-srcver-profile')
        report, conn = read('mariadb-11.8-srcver-profile', [MARIADB_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual((found['version'], found['engine'], found['provenance']),
                         ('11.8.9-MariaDB-ubu2404-log', 'mariadb', 'saved_connection'))
        self.assertTrue(found['verified'], 'server_id 401 matches Master_Server_Id')
        self.assertEqual(relay_calls(conn), [])

    def test_unreachable_profile_falls_back_to_relay_log(self):
        local = dict(MYSQL_SOURCE, id='p-local', name='Local 9.7', host='localhost')
        source = source_conn('mysql-9.7-srcver-profile-unreachable')
        report, conn = read('mysql-9.7-srcver-profile-unreachable', [local], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['provenance'], 'relay_log')
        self.assertEqual(found['version'], '9.7.2')
        self.assertEqual(len(found['attempts']), 1)
        self.assertIn('The saved connection Local 9.7 could not be read: ERROR 2002', found['attempts'][0])

    def test_profile_reaching_another_server_is_not_used(self):
        # Synthetic: the saved connection answers with another server's IDs.
        for field, value, words in (('server_id', '999', 'reaches server ID 999'),
                                    ('server_uuid', '00000000-0000-0000-0000-000000000000', 'reaches server UUID')):
            with self.subTest(field=field):
                original = dict(line.split('\t') for line in recording('mysql-9.7-srcver-profile')['source_calls']
                                ['127.0.0.1:33297'][0][1].splitlines())
                edit = lambda sql, text, n: text.replace('%s\t%s' % (field, original[field]), '%s\t%s' % (field, value))
                source = source_conn('mysql-9.7-srcver-profile', edit=edit)
                report, _ = read('mysql-9.7-srcver-relay', [MYSQL_SOURCE], lambda p: source)
                found = report['channels'][0]['source_version']
                self.assertEqual(found['provenance'], 'relay_log')
                self.assertIn(words, found['attempts'][0])

    def test_profile_needs_every_identity_the_replica_reports(self):
        # Synthetic: the saved connection's answer lacks server_uuid; then the
        # replica has recorded neither ID (before its receiver connected).
        drop_uuid = lambda sql, text, n: '\n'.join(l for l in text.splitlines() if not l.startswith('server_uuid')) + '\n'
        source = source_conn('mysql-9.7-srcver-profile', edit=drop_uuid)
        report, _ = read('mysql-9.7-srcver-relay', [MYSQL_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['provenance'], 'relay_log')
        self.assertIn('reaches server UUID (none)', found['attempts'][0])
        no_ids = lambda sql, text, n: text.replace('<field name="Source_Server_Id">301</field>',
                                                   '<field name="Source_Server_Id">0</field>').replace(
            '<field name="Source_UUID">716fd176-cc91-11f1-bb1e-ba63c647dbde</field>', '<field name="Source_UUID"></field>') \
            if text else text
        source = source_conn('mysql-9.7-srcver-profile')
        report, _ = read('mysql-9.7-srcver-relay', [MYSQL_SOURCE], lambda p: source, edit=no_ids)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIn('cannot be confirmed as the source', found['attempts'][0])

    def test_saved_password_lookup_failure_falls_back(self):
        import sqlite3

        def open_profile(profile):
            raise sqlite3.OperationalError('database is locked')
        report, _ = read('mysql-9.7-srcver-relay', [MYSQL_SOURCE], open_profile)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['provenance'], 'relay_log')
        self.assertEqual(found['attempts'], ['The saved password for Source 9.7 could not be read: OperationalError'])

    def test_unquotable_relay_names_leave_the_version_unknown(self):
        channels = [{'kind': 'replication', 'name': 'a\\b', 'source': {'host': 'db1', 'port': 3306, 'server_id': 7},
                     'position': {'relay_log_file': 'r.000002'}}]
        report = {'engine': 'mysql', 'capabilities': {}, 'channels': channels}
        rp.attach_source_versions(None, report)
        self.assertIn('Could not read the relay log r.000002', channels[0]['source_version']['reason'])

    def test_profile_without_saved_password_is_not_read(self):
        opened = []
        report, _ = read('mysql-9.7-srcver-relay', [dict(MYSQL_SOURCE, remember_password=False)],
                         lambda p: opened.append(p))
        found = report['channels'][0]['source_version']
        self.assertEqual(opened, [])
        self.assertEqual(found['provenance'], 'relay_log')
        self.assertIn('has no saved password', found['attempts'][0])

    def test_unknown_after_profile_failure_has_no_add_hint(self):
        source = source_conn('mysql-9.7-srcver-profile-unreachable')
        local = dict(MYSQL_SOURCE, host='localhost', name='Local 9.7')
        report, _ = read('mysql-9.7-srcver-denied', [local], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIsNone(found['hint'])
        self.assertEqual(len(found['attempts']), 2)

    def test_password_is_redacted_from_profile_errors(self):
        class Refusing(object):
            password = 'hunter2-not-real'

            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("mysql client exited 1: ERROR 1045 (28000): Access denied using hunter2-not-real")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass
        report, _ = read('mysql-9.7-srcver-relay', [MYSQL_SOURCE], lambda p: Refusing())
        text = json.dumps(report)
        self.assertNotIn('hunter2-not-real', text)
        self.assertIn('[redacted]', report['channels'][0]['source_version']['attempts'][0])

    def test_each_profile_is_read_once_and_reads_are_capped(self):
        opened = []

        def open_profile(profile):
            opened.append(profile['id'])
            raise ConnectorError('ERROR 2003 (HY000): Can\'t connect')
        profiles = [dict(MYSQL_SOURCE, id='p%d' % i, name='S%d' % i, host='127.0.0.1') for i in range(6)]
        report, _ = read('mysql-9.7-srcver-relay', profiles, open_profile)
        self.assertEqual(opened, ['p0', 'p1', 'p2', 'p3'])
        attempts = report['channels'][0]['source_version']['attempts']
        self.assertIn('More than 4 saved connections', attempts[-1])


class TestMatching(unittest.TestCase):
    SOURCE = {'host': 'DB1.example.com.', 'port': 3307}

    def names(self, source, profiles, replica_host='10.0.0.5'):
        found, problem = rp.match_source_profiles(source, profiles, replica_host)
        return [p['name'] for p in found], problem

    def test_host_ignores_case_and_port_must_match(self):
        profiles = [{'name': 'a', 'host': 'db1.EXAMPLE.com', 'port': 3307}, {'name': 'b', 'host': 'db1.example.com'},
                    {'name': 'c', 'host': 'db2.example.com', 'port': 3307}]
        self.assertEqual(self.names(self.SOURCE, profiles), (['a'], None))
        self.assertEqual(self.names({'host': 'db1.example.com', 'port': 3306}, profiles), (['b'], None))
        self.assertEqual(self.names({'host': 'db1.example.com', 'port': None}, profiles), (['b'], None))

    def test_loopback_equivalents_only_when_unambiguous_and_local(self):
        local = {'host': '127.0.0.1', 'port': 3306}
        one = [{'name': 'lh', 'host': 'localhost', 'port': 3306}]
        self.assertEqual(self.names(local, one, '127.0.0.1'), (['lh'], None))
        self.assertEqual(self.names(local, one, 'localhost'), (['lh'], None))
        self.assertEqual(self.names({'host': '::1', 'port': 3306}, one, '::1'), (['lh'], None))
        two = one + [{'name': 'v6', 'host': '::1', 'port': 3306}]
        found, problem = self.names(local, two, '127.0.0.1')
        self.assertEqual(found, [])
        self.assertIn('2 saved connections could be 127.0.0.1:3306', problem)
        # An exact match is not ambiguous.
        exact = two + [{'name': 'ip', 'host': '127.0.0.1', 'port': 3306}]
        self.assertEqual(self.names(local, exact, '127.0.0.1'), (['ip'], None))

    def test_loopback_source_of_a_remote_replica_matches_nothing(self):
        found, problem = self.names({'host': '127.0.0.1', 'port': 3306}, [{'name': 'ip', 'host': '127.0.0.1'}],
                                    'db9.example.com')
        self.assertEqual(found, [])
        self.assertIn('local to the replica, which is not on this computer', problem)

    def test_no_host(self):
        self.assertEqual(self.names({'host': None, 'port': 3306}, [{'name': 'a'}]),
                         ([], "The replica does not report its source's host."))

    def test_parse_skips_the_files_own_header(self):
        own = {'Event_type': 'Format_desc', 'Server_id': '301', 'Info': 'Server ver: 9.7.2, Binlog ver: 4'}
        # The own header carries the replica's ID; sharing it with the source is ambiguous.
        self.assertIsNone(rp.parse_source_fde([own], 301)[0])
        self.assertIn('same server ID', rp.parse_source_fde([own], 301)[1])
        rows = [dict(own, Server_id='302', Info='Server ver: 9.7.9, Binlog ver: 4'),
                {'Event_type': 'Format_desc', 'Server_id': '302', 'Info': 'Server ver: 9.7.9, Binlog ver: 4'},
                {'Event_type': 'Rotate', 'Server_id': '301', 'Info': 'mysql-bin.000003;pos=4'},
                {'Event_type': 'Format_desc', 'Server_id': '301', 'Info': 'Server ver: 8.4.6-log, Binlog ver: 4'}]
        self.assertEqual(rp.parse_source_fde(rows, 301), ('8.4.6-log', None))
        self.assertIsNone(rp.parse_source_fde(rows[:3], 301)[0])

    def test_statements_quote_names(self):
        channel = {'name': "it's", 'position': {'relay_log_file': 'r-relay.000002'}}
        self.assertEqual(rp.source_version_statement('mysql', channel),
                         "SHOW RELAYLOG EVENTS IN 'r-relay.000002' LIMIT 4 FOR CHANNEL 'it''s'")
        self.assertEqual(rp.source_version_statement('mariadb', channel),
                         "SHOW RELAYLOG 'it''s' EVENTS IN 'r-relay.000002' LIMIT 3")
        self.assertIsNone(rp.source_version_statement('mysql', {'name': '', 'position': {}}))

    def test_lost_replica_connection_is_not_waited_for_again(self):
        class Lost(object):
            calls = 0

            def named(self, sql):
                Lost.calls += 1
                raise ConnectorError('mysql client exited 1: ERROR 2013 (HY000): Lost connection to server')
        channels = [{'kind': 'replication', 'name': n, 'source': {'host': 'db1', 'port': 3306, 'server_id': 7},
                     'position': {'relay_log_file': 'r.000002'}} for n in ('a', 'b')]
        report = {'engine': 'mysql', 'capabilities': {}, 'channels': channels}
        rp.attach_source_versions(Lost(), report)
        self.assertEqual(Lost.calls, 1)
        self.assertEqual([c['source_version']['reason'] for c in channels][1], channels[0]['source_version']['reason'])
        self.assertIn('Lost connection', channels[1]['source_version']['reason'])

    def test_group_replication_channels_get_none(self):
        report = {'engine': 'mysql', 'capabilities': {}, 'channels': [{'kind': 'group_replication', 'name': 'x'}]}
        rp.attach_source_versions(None, report)
        self.assertIsNone(report['channels'][0]['source_version'])


class TestSurfaces(ReplicationCase):

    def test_cli_text_and_json(self):
        conn = ReplayConn(fixture('replication', 'mysql-9.7-srcver-relay')['calls'])
        code, out, err, _f = run_cli('replication', [], conn)
        self.assertEqual(code, 0, err)
        self.assertIn('  Source version: 9.7.2 (relay log)\n', out)
        conn = ReplayConn(fixture('replication', 'mariadb-11.8-srcver-relay')['calls'])
        code, out, err, _f = run_cli('replication', ['--json'], conn)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['channels'][0]['source_version']['engine'], 'mariadb')

    def endpoint(self, name, profiles=()):
        store = WorkspaceStore()
        saved = {}
        for profile in profiles:
            value = {k: profile[k] for k in ('name', 'host', 'port', 'user', 'ssl_mode')}
            saved[profile['port']] = store.save('profile', value, None, password='source-secret-1', remember=True)
        replica = ReplayConn(fixture('replication', name)['calls'])
        source = source_conn(name) if profiles else None
        opened = []

        def factory(**options):
            opened.append(options)
            return source if options['port'] in saved else replica
        payload = {'connection': {'host': '127.0.0.1', 'port': 33298, 'user': 'root', 'password': 'replica-secret-1'}}
        with mock.patch('myflames.connector.MySQLConnection', factory):
            report = ui.api_response('/api/live/replication', payload, types.SimpleNamespace(store=store))
        return report, opened, saved

    def test_endpoint_reads_the_matching_saved_connection_with_its_own_password(self):
        report, opened, saved = self.endpoint('mysql-9.7-srcver-profile', [MYSQL_SOURCE])
        found = report['channels'][0]['source_version']
        self.assertEqual(found['label'], 'from the saved connection Source 9.7')
        self.assertEqual(found['profile_id'], saved[33297]['id'])
        source = [o for o in opened if o['port'] == 33297]
        self.assertEqual(len(source), 1)
        self.assertEqual((source[0]['host'], source[0]['user'], source[0]['password']),
                         ('127.0.0.1', 'probe', 'source-secret-1'))
        self.assertEqual((source[0]['connect_timeout'], source[0]['query_timeout']),
                         (rp.SOURCE_CONNECT_TIMEOUT_S, rp.SOURCE_QUERY_TIMEOUT_S))
        self.assertNotIn('source-secret-1', json.dumps(report))
        self.assertNotIn('replica-secret-1', json.dumps(report))

    def test_endpoint_without_profiles_uses_the_relay_log(self):
        report, opened, _ = self.endpoint('mysql-9.7-srcver-relay')
        self.assertEqual(report['channels'][0]['source_version']['provenance'], 'relay_log')
        self.assertEqual([o['port'] for o in opened], [33298])

    def test_profile_opener_needs_a_saved_password(self):
        store = WorkspaceStore()
        saved = store.save('profile', {'name': 'NoPass', 'host': '127.0.0.1', 'port': 33297, 'user': 'probe'}, None)
        with mock.patch('myflames.connector.MySQLConnection', FakeFactory(None)) as _f:
            with self.assertRaisesRegex(ValueError, 'has no saved password'):
                rp.profile_opener(store)(saved)

    def test_saved_profiles_survives_an_unreadable_store(self):
        class Broken(object):
            def list(self, kind):
                raise OSError('locked')
        self.assertEqual(rp.saved_profiles(Broken()), [])


if __name__ == '__main__':
    unittest.main()
