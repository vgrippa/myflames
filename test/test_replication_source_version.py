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
  REPLICATION SLAVE (unknown from the relay log), and with a saved connection
  to localhost that the client could not reach.
* MariaDB 11.8.9 replica 127.0.0.1:33319 of 127.0.0.1:33318 (402 and 401):
  through the saved connection, from the relay log (SLAVE MONITOR only), after
  FLUSH RELAY LOGS, and after RESET SLAVE with only the SQL thread started (no
  event from the source yet: unknown from the relay log).

The relay log is read first; a saved connection only when it gives no
version. ``source_calls`` in a recording are the saved connection's own
answers. Edits marked synthetic below change a recorded answer to reach a
branch the live servers could not show. Every recording predates the
@@hostname reads: the replica's answer and the ``hostname`` row of the saved
connection's answer are synthetic (the same name on both, as for two servers
on one computer), and the saved connection's statement is today's.
"""
import json
import sqlite3
import threading
import time
import types
import unittest
from unittest import mock

from _perf_replay import ReplayConn, fixture as recorded_fixture, run_cli
from myflames import replication as rp, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore
from test_replication import ReplicationCase, fixture

TAG = rp.REPLICATION_TAG
MYSQL_SOURCE = {'id': 'p-mysql', 'name': 'Source 9.7', 'host': '127.0.0.1', 'port': 33297, 'user': 'probe',
                'ssl_mode': 'PREFERRED', 'remember_password': True}
MARIADB_SOURCE = {'id': 'p-maria', 'name': 'Source 11.8', 'host': '127.0.0.1', 'port': 33318, 'user': 'probe',
                  'ssl_mode': 'PREFERRED', 'remember_password': True}
#: Synthetic @@hostname: the MySQL replica's default relay log name is <hostname>-relay-bin.
HOST = '025357c3d20b'
HOSTNAME_SQL = TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name = 'hostname'"
RECORDED_SOURCE_SQL = TAG + ("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                             "('server_id', 'server_uuid', 'version', 'version_comment')")
SOURCE_SQL = TAG + ("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                    "('hostname', 'server_id', 'server_uuid', 'version', 'version_comment')")
NO_UUID = lambda sql, text, n: text.replace(  # noqa: E731
    '<field name="Source_UUID">716fd176-cc91-11f1-bb1e-ba63c647dbde</field>', '<field name="Source_UUID"></field>') \
    if text else text


def recording(name):
    return recorded_fixture('replication', name)


def source_conn(name, edit=None, hostname=HOST):
    """The saved connection's recorded answer under today's statement, with a
    synthetic ``hostname`` row (none when *hostname* is None)."""
    calls = []
    for call in [call for calls in recording(name)['source_calls'].values() for call in calls]:
        call = list(call)
        if call[0] == RECORDED_SOURCE_SQL:
            call[0] = SOURCE_SQL
            if call[1] is not None and hostname is not None:
                call[1] = 'hostname\t%s\n' % hostname + call[1]
        calls.append(call)
    return ReplayConn(calls, edit=edit)


def replica_calls(name, hostname=HOST):
    """The replica's recorded calls plus a synthetic @@hostname answer
    (a refusal when *hostname* is an exception text)."""
    calls = list(fixture('replication', name)['calls'])
    if hostname is not None:
        calls.append([HOSTNAME_SQL, None, hostname] if hostname.startswith('mysql client') else
                     [HOSTNAME_SQL, 'hostname\t%s\n' % hostname])
    return calls


def read(name, profiles=(), open_profile=None, replica_host='127.0.0.1', edit=None, hostname=HOST):
    conn = ReplayConn(replica_calls(name, hostname), edit=edit)
    report = rp.read_status(conn, source_versions=True, profiles=profiles, open_profile=open_profile,
                            replica_host=replica_host)
    return report, conn


def relay_calls(conn):
    return [sql for sql, _flags in conn.calls if 'RELAYLOG' in sql]


class Opener(object):
    """``open_profile`` that counts opens and hands out *conn* (or raises it)."""

    def __init__(self, conn, clock=None, takes=0):
        self.conn, self.opened, self.clock, self.takes = conn, [], clock, takes

    def __call__(self, profile):
        self.opened.append(profile.get('id'))
        if self.clock is not None:
            self.clock.now += self.takes
        if isinstance(self.conn, Exception):
            raise self.conn
        return self.conn(profile) if callable(self.conn) and not hasattr(self.conn, 'run') else self.conn


class Source(object):
    """A saved connection's server answering SHOW GLOBAL VARIABLES."""

    def __init__(self, **values):
        self.values, self.timeouts, self.password, self.closed = values, [], 'saved-secret-1', False

    def run(self, sql, extra_flags=None, timeout=None):
        assert sql == SOURCE_SQL, sql
        self.timeouts.append(timeout)
        return ''.join('%s\t%s\n' % kv for kv in sorted(self.values.items()))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True


class FakeReader(object):
    """The replica for synthetic reports: the relay log head and @@hostname."""

    def __init__(self, relay=None, hostname='replica-host'):
        self.relay, self.hostname, self.calls, self.timeouts = relay, hostname, [], []

    def named(self, sql):
        self.calls.append(sql)
        if isinstance(self.relay, Exception):
            raise self.relay
        return self.relay or []

    def rows(self, sql, timeout=None):
        self.calls.append(sql)
        self.timeouts.append(timeout)
        if isinstance(self.hostname, Exception):
            raise self.hostname
        return [['hostname', self.hostname]]


def wait_until(predicate, timeout=5.0):
    """Poll *predicate* in real time; True once it holds."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class Clock(object):
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


DENIED = ConnectorError('mysql client exited 1: ERROR 1227 (42000) at line 1: Access denied; you need (at least one '
                        'of) the REPLICATION SLAVE privilege(s) for this operation')


def channel(name='', host='db1.example.com', port=3306, server_id=7, uuid=None, relay_file='r.000002'):
    return {'kind': 'replication', 'name': name,
            'source': {'host': host, 'port': port, 'server_id': server_id, 'uuid': uuid},
            'position': {'relay_log_file': relay_file}}


def attach(channels, profiles=(), open_profile=None, reader=None, engine='mysql', replica_host='127.0.0.1'):
    report = {'engine': engine, 'capabilities': {}, 'channels': channels}
    rp.attach_source_versions(reader or FakeReader(DENIED), report, profiles, open_profile, replica_host)
    return [c['source_version'] for c in channels]


def profile(pid, host='db1.example.com', port=3306, **extra):
    return dict({'id': pid, 'name': 'Saved %s' % pid, 'host': host, 'port': port, 'user': 'probe',
                 'remember_password': True, 'updated_at': '2026-10-20T10:00:00'}, **extra)


class Fresh(object):
    """Every test starts with an empty saved-connection cache."""

    def setUp(self):
        rp.reset_source_cache()
        super(Fresh, self).setUp()

    def tearDown(self):
        rp.reset_source_cache()
        super(Fresh, self).tearDown()


class TestRelayLog(Fresh, ReplicationCase):

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
        for name in ('mysql-9.7-srcver-relay', 'mariadb-11.8-srcver-relay', 'mysql-9.7-srcver-denied'):
            with self.subTest(name=name):
                rp.reset_source_cache()
                source = source_conn('mysql-9.7-srcver-profile')
                _, conn = read(name, [MYSQL_SOURCE], lambda p: source)
                for sql, flags in conn.calls + source.calls:
                    self.assertTrue(sql.startswith(TAG), sql)
                    self.assertRegex(sql[len(TAG):], r'^(SELECT|SHOW) ')
                    self.assertIn('--comments', flags)
                self.assertEqual(bool(source.calls), name == 'mysql-9.7-srcver-denied')


class TestUnknown(Fresh, ReplicationCase):

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

    def test_a_loopback_source_of_a_remote_replica_gets_no_add_hint(self):
        found = attach([channel(host='127.0.0.1')], [profile('a', host='127.0.0.1')], Opener(Source()),
                       replica_host='db9.example.com')[0]
        self.assertIsNone(found['hint'])
        self.assertIn('needs the REPLICATION SLAVE privilege. The source address 127.0.0.1:3306 is local to the '
                      'replica', found['reason'])


class TestRelayFirst(Fresh, ReplicationCase):
    """1. The relay log first; a saved connection only when it gives no version."""

    def test_a_matching_saved_connection_is_not_opened_when_the_relay_log_answers(self):
        # Live: this replica's relay log names the source's version, and the
        # saved connection to localhost:33297 could not be reached.
        local = dict(MYSQL_SOURCE, id='p-local', name='Local 9.7', host='localhost')
        opener = Opener(source_conn('mysql-9.7-srcver-profile-unreachable'))
        report, conn = read('mysql-9.7-srcver-profile-unreachable', [local], opener)
        found = report['channels'][0]['source_version']
        self.assertEqual((found['provenance'], found['version'], found['attempts']), ('relay_log', '9.7.2', []))
        self.assertEqual(opener.opened, [])
        self.assertNotIn(HOSTNAME_SQL, [sql for sql, _ in conn.calls])

    def test_the_saved_connection_is_read_only_after_the_relay_log(self):
        opener = Opener(source_conn('mysql-9.7-srcver-profile'))
        report, conn = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], opener)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['provenance'], 'saved_connection')
        self.assertEqual(found['attempts'], ['Reading the relay log (SHOW RELAYLOG EVENTS) needs the REPLICATION '
                                             'SLAVE privilege.'])
        sent = [sql for sql, _ in conn.calls]
        self.assertLess(sent.index(relay_calls(conn)[0]), sent.index(HOSTNAME_SQL))
        self.assertEqual(opener.opened, ['p-mysql'])


class TestSavedConnection(Fresh, ReplicationCase):

    def test_mysql_profile(self):
        source = source_conn('mysql-9.7-srcver-profile')
        report, conn = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['version'], '9.7.2')
        self.assertEqual(found['version_comment'], 'MySQL Community Server - GPL')
        self.assertEqual(found['provenance'], 'saved_connection')
        self.assertEqual(found['label'], 'from the saved connection Source 9.7')
        self.assertEqual((found['profile_id'], found['profile_name']), ('p-mysql', 'Source 9.7'))
        self.assertTrue(found['verified'], 'server_id and server_uuid both match')
        self.assertIsNone(found['caveat'])
        self.assertEqual(source.calls[0][0], SOURCE_SQL)
        self.assertIn('Source version: 9.7.2 (saved connection Source 9.7)\n', rp.format_text(report))

    def test_profile_reaching_another_server_is_not_used(self):
        # Synthetic: the saved connection answers with another server's IDs.
        for field, value, words in (('server_id', '999', 'reaches server ID 999'),
                                    ('server_uuid', '00000000-0000-0000-0000-000000000000', 'reaches server UUID')):
            with self.subTest(field=field):
                rp.reset_source_cache()
                original = dict(line.split('\t') for line in recording('mysql-9.7-srcver-profile')['source_calls']
                                ['127.0.0.1:33297'][0][1].splitlines())
                edit = lambda sql, text, n: text.replace('%s\t%s' % (field, original[field]), '%s\t%s' % (field, value))
                source = source_conn('mysql-9.7-srcver-profile', edit=edit)
                report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source)
                found = report['channels'][0]['source_version']
                self.assertIsNone(found['version'])
                self.assertIn(words, found['attempts'][1])
                self.assertIn(words, found['reason'])

    def test_profile_needs_every_identity_the_replica_reports(self):
        # Synthetic: the saved connection's answer lacks server_uuid; then the
        # replica has recorded neither ID (before its receiver connected).
        drop_uuid = lambda sql, text, n: '\n'.join(l for l in text.splitlines() if not l.startswith('server_uuid')) + '\n'
        source = source_conn('mysql-9.7-srcver-profile', edit=drop_uuid)
        report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIn('reaches server UUID (none)', found['attempts'][1])
        rp.reset_source_cache()
        no_ids = lambda sql, text, n: NO_UUID(sql, text, n).replace(
            '<field name="Source_Server_Id">301</field>', '<field name="Source_Server_Id">0</field>') if text else text
        source = source_conn('mysql-9.7-srcver-profile')
        report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source, edit=no_ids)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIn('cannot be confirmed as the source', found['attempts'][1])

    def test_saved_password_lookup_failure_is_reported_and_not_kept(self):
        opener = Opener(sqlite3.OperationalError('database is locked'))
        for _ in range(2):
            report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], opener)
            found = report['channels'][0]['source_version']
            self.assertIsNone(found['version'])
            self.assertEqual(found['attempts'][1], 'The saved password for Source 9.7 could not be read: '
                                                   'OperationalError')
        # No login was sent, so nothing is remembered against the source.
        self.assertEqual(opener.opened, ['p-mysql', 'p-mysql'])

    def test_unquotable_relay_names_leave_the_version_unknown(self):
        channels = [{'kind': 'replication', 'name': 'a\\b', 'source': {'host': 'db1', 'port': 3306, 'server_id': 7},
                     'position': {'relay_log_file': 'r.000002'}}]
        report = {'engine': 'mysql', 'capabilities': {}, 'channels': channels}
        rp.attach_source_versions(None, report)
        self.assertIn('Could not read the relay log r.000002', channels[0]['source_version']['reason'])

    def test_profile_without_saved_password_is_not_read(self):
        opener = Opener(None)
        report, _ = read('mysql-9.7-srcver-denied', [dict(MYSQL_SOURCE, remember_password=False)], opener)
        found = report['channels'][0]['source_version']
        self.assertEqual(opener.opened, [])
        self.assertIsNone(found['version'])
        self.assertIn('has no saved password', found['attempts'][1])

    def test_unknown_after_profile_failure_has_no_add_hint(self):
        source = source_conn('mysql-9.7-srcver-profile-unreachable')
        local = dict(MYSQL_SOURCE, host='localhost', name='Local 9.7')
        report, _ = read('mysql-9.7-srcver-denied', [local], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIsNone(found['hint'])
        self.assertEqual(len(found['attempts']), 2)
        self.assertIn('The saved connection Local 9.7 could not be read: ERROR 2002', found['attempts'][1])
        # The reason says why the relay log gave none and how the saved connection went.
        self.assertTrue(found['reason'].startswith('Reading the relay log'))
        self.assertIn('Local 9.7 could not be read', found['reason'])

    def test_password_is_redacted_from_profile_errors(self):
        class Refusing(object):
            password = 'hunter2-not-real'

            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("mysql client exited 1: ERROR 1045 (28000): Access denied using hunter2-not-real")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass
        report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: Refusing())
        text = json.dumps(report)
        self.assertNotIn('hunter2-not-real', text)
        self.assertIn('[redacted]', report['channels'][0]['source_version']['attempts'][1])

    def test_each_profile_is_read_once_and_reads_are_capped(self):
        opener = Opener(ConnectorError('ERROR 2003 (HY000): Can\'t connect'))
        profiles = [dict(MYSQL_SOURCE, id='p%d' % i, name='S%d' % i, host='127.0.0.1') for i in range(6)]
        report, _ = read('mysql-9.7-srcver-denied', profiles, opener)
        self.assertEqual(opener.opened, ['p0', 'p1', 'p2', 'p3'])
        attempts = report['channels'][0]['source_version']['attempts']
        self.assertIn('More than 4 saved connections', attempts[-1])


class TestServerIdOnly(Fresh, ReplicationCase):
    """5. A server ID alone does not verify the saved connection."""

    def test_mariadb_saved_connection_is_not_verified(self):
        # Live: after RESET SLAVE the relay log has no event from the source
        # yet; the saved connection to 127.0.0.1:33318 answers server_id 401.
        source = source_conn('mariadb-11.8-srcver-profile')
        report, _ = read('mariadb-11.8-srcver-unknown', [MARIADB_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertEqual((found['version'], found['engine'], found['provenance']),
                         ('11.8.9-MariaDB-ubu2404-log', 'mariadb', 'saved_connection'))
        self.assertFalse(found['verified'], 'MariaDB reports no source UUID: the server ID alone was compared')
        self.assertIn('Source version: 11.8.9-MariaDB-ubu2404-log (saved connection Source 11.8; only its server '
                      'ID could be compared)\n', rp.format_text(report))

    def test_mysql_before_source_uuid_is_known_is_not_verified(self):
        # Synthetic: Source_UUID empty, as before the receiver read it.
        source = source_conn('mysql-9.7-srcver-profile')
        report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source, edit=NO_UUID)
        found = report['channels'][0]['source_version']
        self.assertEqual(found['provenance'], 'saved_connection')
        self.assertFalse(found['verified'])
        self.assertIn('; only its server ID could be compared)', rp.format_text(report))

    def test_a_reported_uuid_verifies(self):
        found = attach([channel(uuid='AB-1')], [profile('a')], Opener(Source(
            server_id='7', server_uuid='ab-1', version='8.4.6', hostname='elsewhere')))[0]
        self.assertTrue(found['verified'])
        self.assertEqual(rp.source_version_text(found), 'Source version: 8.4.6 (saved connection Saved a)')


class TestLoopbackHost(Fresh, ReplicationCase):
    """4. A loopback source counts only on the replica's own host."""

    def test_tunnelled_mariadb_replica_and_an_unrelated_local_server(self):
        # The finding: a MariaDB replica reached through an SSH tunnel
        # replicates from 127.0.0.1:3306 on its own host (default server_id
        # 1); this computer's saved "local dev" is 127.0.0.1:3306 too.
        local_dev = profile('dev', host='127.0.0.1', name='local dev')
        unrelated = Source(server_id='1', version='11.4.2-MariaDB', hostname='laptop')
        opener = Opener(unrelated)
        reader = FakeReader(relay=[], hostname='db-host-7')
        found = attach([channel(host='127.0.0.1', server_id=1)], [local_dev], opener, reader, engine='mariadb')[0]
        self.assertIsNone(found['version'])
        self.assertEqual(opener.opened, ['dev'])
        self.assertIn("The saved connection local dev reaches the host laptop, not the replica's host db-host-7",
                      found['attempts'][-1])
        # With the source's event in the relay log, the saved connection is never opened.
        rp.reset_source_cache()
        rows = [{'Event_type': 'Format_desc', 'Server_id': '2', 'Info': 'Server ver: 10.11.9-MariaDB, Binlog ver: 4'},
                {'Event_type': 'Format_desc', 'Server_id': '1', 'Info': 'Server ver: 10.11.9-MariaDB, Binlog ver: 4'}]
        opener = Opener(unrelated)
        found = attach([channel(host='127.0.0.1', server_id=1)], [local_dev], opener, FakeReader(relay=rows),
                       engine='mariadb')[0]
        self.assertEqual((found['version'], found['provenance']), ('10.11.9-MariaDB', 'relay_log'))
        self.assertEqual(opener.opened, [])

    def test_same_host_is_accepted_ignoring_case_and_a_trailing_dot(self):
        reader = FakeReader(hostname='box.local')
        found = attach([channel(host='localhost', server_id=9)], [profile('a', host='::1')],
                       Opener(Source(server_id='9', version='8.4.6', hostname='Box.Local.')), reader)[0]
        self.assertEqual((found['version'], found['provenance']), ('8.4.6', 'saved_connection'))
        # The replica's host name is read once, within the bound of one source.
        self.assertEqual(reader.timeouts, [rp.SOURCE_QUERY_TIMEOUT_S])

    def test_every_loopback_spelling_needs_the_replicas_host(self):
        # Each is an address of the replica's own computer, so a server on
        # this computer answering with another @@hostname is refused.
        for host in ('127.0.0.1', 'localhost', 'LocalHost.', '::1', '[::1]', '0:0:0:0:0:0:0:1', '127.0.0.2',
                     '::ffff:127.0.0.1', '0.0.0.0', 'db.localhost'):
            with self.subTest(host=host):
                rp.reset_source_cache()
                opener = Opener(Source(server_id='1', version='11.4.2-MariaDB', hostname='laptop'))
                reader = FakeReader(relay=[], hostname='db-host-7')
                found = attach([channel(host=host, server_id=1)], [profile('dev', host=host, name='local dev')],
                               opener, reader, engine='mariadb')[0]
                self.assertEqual(opener.opened, ['dev'])
                self.assertIn(rp._HOSTNAME_SQL, reader.calls)
                self.assertIsNone(found['version'])
                self.assertIn("The saved connection local dev reaches the host laptop, not the replica's host "
                              "db-host-7", found['attempts'][-1])
                # The same server on the replica's host is used (server ID only: not verified).
                rp.reset_source_cache()
                found = attach([channel(host=host, server_id=1)], [profile('dev', host=host, name='local dev')],
                               Opener(Source(server_id='1', version='11.4.2-MariaDB', hostname='db-host-7')),
                               FakeReader(relay=[], hostname='db-host-7'), engine='mariadb')[0]
                self.assertEqual((found['version'], found['verified']), ('11.4.2-MariaDB', False))

    def test_a_loopback_source_of_a_remote_replica_opens_nothing(self):
        for host in ('localhost', '::1', '0:0:0:0:0:0:0:1', '127.0.0.2', '::ffff:127.0.0.1', '0.0.0.0'):
            with self.subTest(host=host):
                opener = Opener(Source(server_id='1', version='11.4.2-MariaDB', hostname='db-host-7'))
                reader = FakeReader(relay=[], hostname='db-host-7')
                found = attach([channel(host=host, server_id=1)], [profile('dev', host=host)], opener, reader,
                               engine='mariadb', replica_host='db9.example.com')[0]
                self.assertEqual(opener.opened, [])
                self.assertNotIn(rp._HOSTNAME_SQL, reader.calls)
                self.assertIn('is local to the replica, which is not on this computer', found['reason'])

    def test_saved_connection_without_a_hostname_is_not_used(self):
        found = attach([channel(host='127.0.0.1', server_id=9)], [profile('a', host='127.0.0.1')],
                       Opener(Source(server_id='9', version='8.4.6')))[0]
        self.assertIn('reaches the host (none)', found['reason'])

    def test_unreadable_replica_hostname_opens_nothing(self):
        refused = ConnectorError('mysql client exited 1: ERROR 1142 (42000): SHOW command denied')
        for reader in (FakeReader(hostname=refused), FakeReader(hostname='')):
            with self.subTest(hostname=reader.hostname):
                opener = Opener(Source(server_id='9', version='8.4.6', hostname='x'))
                found = attach([channel(host='127.0.0.1', server_id=9)], [profile('a', host='127.0.0.1')], opener,
                               reader)[0]
                self.assertEqual(opener.opened, [])
                self.assertIn("counts only when it reaches the replica's own host, so Saved a was not read",
                              found['attempts'][-1])

    def test_remote_source_reads_no_hostname(self):
        reader = FakeReader(hostname=AssertionError('not asked'))
        found = attach([channel()], [profile('a')], Opener(Source(server_id='7', version='8.4.6')), reader)[0]
        self.assertEqual(found['version'], '8.4.6')
        self.assertEqual(len(reader.calls), 1, 'only the relay log head')

    def test_live_replica_with_another_hostname(self):
        # Synthetic hostnames over the live MySQL pair: the saved connection
        # answers from another computer's server with the same IDs.
        source = source_conn('mysql-9.7-srcver-profile', hostname='other-box')
        report, _ = read('mysql-9.7-srcver-denied', [MYSQL_SOURCE], lambda p: source)
        found = report['channels'][0]['source_version']
        self.assertIsNone(found['version'])
        self.assertIn("reaches the host other-box, not the replica's host 025357c3d20b", found['reason'])


class TestProcessCache(Fresh, ReplicationCase):
    """2. Opened at most once per process per saved connection and password."""

    def test_refused_login_is_not_sent_again_until_the_profile_changes(self):
        opener = Opener(ConnectorError('mysql client exited 1: ERROR 1045 (28000): Access denied for user'))
        saved = profile('a')
        for _ in range(3):
            found = attach([channel()], [saved], opener)[0]
        self.assertEqual(opener.opened, ['a'])
        self.assertIn('ERROR 1045', found['attempts'][-1])
        self.assertIn('Not tried again until the saved connection changes.', found['attempts'][-1])
        # Saved again (a new password or settings): one more attempt, then none.
        edited = dict(saved, updated_at='2026-10-20T11:00:00')
        for _ in range(2):
            attach([channel()], [edited], opener)
        self.assertEqual(opener.opened, ['a', 'a'])

    def test_a_version_read_is_kept_and_says_so(self):
        opener = Opener(Source(server_id='7', version='8.4.6'))
        first = attach([channel()], [profile('a')], opener)[0]
        second = attach([channel()], [profile('a')], opener)[0]
        self.assertEqual(opener.opened, ['a'])
        self.assertIsNone(first['caveat'])
        self.assertEqual((second['version'], second['caveat']), ('8.4.6', rp.EARLIER_READ_CAVEAT))

    def test_one_open_for_two_channels_in_one_read(self):
        opener = Opener(Source(server_id='7', version='8.4.6'))
        found = attach([channel('a'), channel('b')], [profile('a')], opener)
        self.assertEqual(opener.opened, ['a'])
        self.assertEqual([(f['version'], f['caveat']) for f in found], [('8.4.6', None), ('8.4.6', None)])

    def test_identity_is_checked_against_the_kept_answer_per_channel(self):
        opener = Opener(Source(server_id='7', version='8.4.6'))
        attach([channel()], [profile('a')], opener)
        found = attach([channel(server_id=8)], [profile('a')], opener)[0]
        self.assertIn('reaches server ID 7', found['reason'])
        self.assertEqual(opener.opened, ['a'])

    def test_fingerprint_covers_settings_and_the_saved_password(self):
        store = WorkspaceStore()
        saved = store.save('profile', {'name': 'Src', 'host': 'db1.example.com', 'port': 3306, 'user': 'probe'}, None,
                           password='first-secret-1', remember=True)
        prepare = rp.profile_opener(store).prepare
        listed = lambda: [p for p in store.list('profile') if p['id'] == saved['id']][0]  # noqa: E731
        from myflames import secret_store
        # The fingerprint comes from the workspace store, never the secret store.
        with mock.patch.object(secret_store, 'get_secret', side_effect=AssertionError('secret read')):
            first, _ = prepare(listed())
            self.assertEqual(prepare(listed())[0], first)
        self.assertNotIn('first-secret-1', first)
        stored = store._profile(saved['id'])
        self.assertTrue(stored.get('secret_ref'))
        self.assertNotIn(stored['secret_ref'], first)
        # Only the saved password's reference changes: same settings, same save time.
        same = listed()
        with mock.patch.object(store, '_profile', return_value=dict(stored, secret_ref=stored['secret_ref'] + 'x')):
            self.assertNotEqual(prepare(same)[0], first)
        self.assertEqual(prepare(same)[0], first)
        # Only a setting changes.
        self.assertNotEqual(prepare(dict(same, port=3307))[0], first)
        # Saving a new password writes a new reference.
        store.save('profile', {'name': 'Src', 'host': 'db1.example.com', 'port': 3306, 'user': 'probe'}, saved['id'],
                   password='second-secret-2', remember=True)
        self.assertNotEqual(store._profile(saved['id'])['secret_ref'], stored['secret_ref'])
        self.assertNotEqual(prepare(listed())[0], first)

    def test_the_cache_is_bounded_and_replaces_a_changed_profile(self):
        def done(key, failed=False):
            entry, _ = rp._claim(key)
            entry['values'], entry['problem'] = (None, 'refused') if failed else ({'version': '8.4.6'}, None)
            entry['done'].set()
        pending, _ = rp._claim(('pending', 'f'))
        # A second claim of an open in progress waits for it, never opens again.
        self.assertEqual(rp._claim(('pending', 'f')), (pending, False))
        done(('failed', 'f'), failed=True)
        for i in range(rp.SOURCE_CACHE_SIZE + 5):
            done(('p%d' % i, 'f'))
        self.assertEqual(len(rp._source_cache), rp.SOURCE_CACHE_SIZE)
        # Answers go first: a kept failure and a pending read stay.
        self.assertIn(('failed', 'f'), rp._source_cache)
        self.assertIn(('pending', 'f'), rp._source_cache)
        self.assertNotIn(('p0', 'f'), rp._source_cache)
        rp._claim(('p9', 'g'))
        self.assertNotIn(('p9', 'f'), rp._source_cache)
        self.assertIn(('p9', 'g'), rp._source_cache)

    def test_cli_opens_the_source_once_per_run(self):
        store = WorkspaceStore()
        store.save('profile', {'name': 'Source 9.7', 'host': '127.0.0.1', 'port': 33297, 'user': 'probe'}, None,
                   password='source-secret-1', remember=True)
        replica = ReplayConn(replica_calls('mysql-9.7-srcver-denied'))
        source = source_conn('mysql-9.7-srcver-profile')
        opened = []

        def factory(**options):
            opened.append(options['port'])
            return source if options.get('port') == 33297 else replica
        code, out, err, _f = run_cli('replication', [], replica,
                                     patches=[mock.patch('myflames.connector.MySQLConnection', factory)])
        self.assertEqual(code, 0, err)
        self.assertIn('  Source version: 9.7.2 (saved connection Source 9.7)\n', out)
        self.assertEqual(opened.count(33297), 1)


class TestTimeBudget(Fresh, ReplicationCase):
    """3. A short timeout per source and a budget per read."""

    def three_sources(self):
        return [channel(n, host='db%s.example.com' % n) for n in '123'], \
            [profile(n, host='db%s.example.com' % n) for n in '123']

    def test_slow_sources_leave_the_rest_not_checked_until_a_later_read(self):
        clock = Clock()
        opener = Opener(ConnectorError('mysql client exited 1: ERROR 2003 (HY000): Can\'t connect (110)'),
                        clock, takes=rp.SOURCE_QUERY_TIMEOUT_S)
        with mock.patch.object(rp, '_monotonic', clock):
            channels, profiles = self.three_sources()
            found = attach(channels, profiles, opener)
            self.assertEqual(opener.opened, ['1'])
            for later in found[1:]:
                self.assertIn('Not checked: this read already spent its 5 s on other saved connections',
                              later['reason'])
            # Each later read opens the next source; the failed one is not waited for again.
            channels, profiles = self.three_sources()
            attach(channels, profiles, opener)
            channels, profiles = self.three_sources()
            attach(channels, profiles, opener)
        self.assertEqual(opener.opened, ['1', '2', '3'])

    def test_fast_sources_all_fit(self):
        opener = Opener(lambda p: Source(server_id='7', version='8.4.6'))
        with mock.patch.object(rp, '_monotonic', Clock()):
            channels, profiles = self.three_sources()
            found = attach(channels, profiles, opener)
        self.assertEqual(opener.opened, ['1', '2', '3'])
        self.assertEqual([f['version'] for f in found], ['8.4.6'] * 3)

    def test_another_reads_open_is_waited_for_only_within_the_budget(self):
        saved = profile('a')
        entry, owner = rp._claim(('a', rp._settings_fingerprint(saved)))
        self.assertTrue(owner)
        clock = Clock()
        opener = Opener(Source(server_id='7', version='8.4.6'))

        def late():
            clock.now += rp.SOURCE_READ_BUDGET_S
            return clock.now
        with mock.patch.object(rp, '_monotonic', late):
            found = attach([channel()], [saved], opener)[0]
        self.assertEqual(opener.opened, [])
        self.assertIn('Not checked: another read of the saved connection Saved a had not finished', found['reason'])
        # Once that read finishes, its answer is used.
        entry['values'] = {'server_id': '7', 'version': '8.4.7'}
        entry['done'].set()
        self.assertEqual(attach([channel()], [saved], opener)[0]['version'], '8.4.7')

    def test_concurrent_reads_open_once(self):
        # Both reads find no result and claim at the same moment.
        gate, opened, claims, results = threading.Event(), [], [], []
        barrier, claim = threading.Barrier(2, timeout=5), rp._claim

        def racing_claim(key):
            barrier.wait()
            entry, owner = claim(key)
            claims.append(owner)
            return entry, owner

        def slow(profile):
            opened.append(profile['id'])
            gate.wait(5)
            return Source(server_id='7', version='8.4.6')
        threads = [threading.Thread(target=lambda: results.append(attach([channel()], [profile('a')], slow)[0]))
                   for _ in range(2)]
        try:
            with mock.patch.object(rp, '_claim', racing_claim):
                for thread in threads:
                    thread.start()
                # The open is released only after both claims returned.
                self.assertTrue(wait_until(lambda: len(claims) == 2), claims)
                gate.set()
                for thread in threads:
                    thread.join(5)
        finally:
            gate.set()
        self.assertEqual(sorted(claims), [False, True])
        self.assertEqual(opened, ['a'])
        self.assertEqual(sorted(r['version'] for r in results), ['8.4.6', '8.4.6'])

    def test_a_read_waits_for_an_open_in_progress(self):
        gate, in_open, waiting, opened, results = (threading.Event(), threading.Event(), threading.Event(), [],
                                                   [])

        class Watched(threading.Event):
            def wait(self, timeout=None):
                waiting.set()
                return super(Watched, self).wait(timeout)

        def slow(profile):
            opened.append(profile['id'])
            in_open.set()
            gate.wait(5)
            return Source(server_id='7', version='8.4.6')

        def one_read():
            results.append(attach([channel()], [profile('a')], slow)[0])
        first, second = threading.Thread(target=one_read), threading.Thread(target=one_read)
        try:
            first.start()
            self.assertTrue(in_open.wait(5))
            # The first read is inside its open: watch the result it will set.
            (entry,) = rp._source_cache.values()
            entry['done'] = Watched()
            second.start()
            # Released only once the second read waits for that open.
            self.assertTrue(waiting.wait(5), 'the second read waits for the open in progress')
            self.assertEqual(opened, ['a'])
            gate.set()
            first.join(5)
            second.join(5)
        finally:
            gate.set()
        self.assertEqual(opened, ['a'])
        self.assertEqual([(r['version'], r['caveat']) for r in results],
                         [('8.4.6', None), ('8.4.6', rp.EARLIER_READ_CAVEAT)])

    def test_an_open_in_progress_is_waited_for_with_the_time_left(self):
        # Too little time left to open a saved connection is still time to
        # wait for another read's open of it.
        saved = profile('a')
        entry, _ = rp._claim(('a', rp._settings_fingerprint(saved)))
        waited = []

        class Finishing(object):
            def is_set(self):
                return bool(waited)

            def wait(self, timeout=None):
                waited.append(timeout)
                entry['values'] = {'server_id': '7', 'version': '8.4.7'}
                return True

            def set(self):
                pass
        entry['done'] = Finishing()
        clock = Clock()
        # An earlier source of this read used 2.5 s of its 5 s.
        opener = Opener(ConnectorError("mysql client exited 1: ERROR 2003 (HY000): Can't connect (110)"), clock,
                        takes=2.5)
        with mock.patch.object(rp, '_monotonic', clock):
            found = attach([channel('x', host='db2.example.com'), channel('y')],
                           [profile('b', host='db2.example.com'), saved], opener)
        self.assertEqual(opener.opened, ['b'])
        self.assertEqual(waited, [2.5])
        self.assertEqual(found[1]['version'], '8.4.7')

    def test_a_slow_saved_password_lookup_sends_nothing_after_the_budget(self):
        clock = Clock()
        source = Source(server_id='7', version='8.4.6')
        # The lookup (inside the open) leaves 2.5 s of the 5 s: less than one source's 3 s.
        opener = Opener(source, clock, takes=rp.SOURCE_READ_BUDGET_S - rp.SOURCE_QUERY_TIMEOUT_S + 0.5)
        with mock.patch.object(rp, '_monotonic', clock):
            found = attach([channel()], [profile('a')], opener)[0]
            self.assertEqual(opener.opened, ['a'])
            self.assertEqual(source.timeouts, [], 'nothing was sent')
            self.assertTrue(source.closed)
            self.assertIsNone(found['version'])
            self.assertIn('Not checked: looking up the saved password for Saved a took the rest of the 5 s',
                          found['reason'])
            # Not kept: the next read opens it, and a lookup that leaves a
            # full bound sends the query with that bound.
            opener.takes = rp.SOURCE_READ_BUDGET_S - rp.SOURCE_QUERY_TIMEOUT_S
            found = attach([channel()], [profile('a')], opener)[0]
        self.assertEqual(opener.opened, ['a', 'a'])
        self.assertEqual(source.timeouts, [rp.SOURCE_QUERY_TIMEOUT_S])
        self.assertEqual((found['version'], found['caveat']), ('8.4.6', None))

    def test_a_slow_lookup_through_the_store_opener_sends_nothing(self):
        # The same through profile_opener's prepare(): a saved password
        # lookup that blocks (the Keychain) and returns after the budget.
        store = WorkspaceStore()
        saved = store.save('profile', {'name': 'Src', 'host': 'db1.example.com', 'port': 3306, 'user': 'probe'},
                           None, password='source-secret-1', remember=True)
        listed = [p for p in store.list('profile') if p['id'] == saved['id']]
        clock, made = Clock(), []
        lookup = store.connection_with_saved_password

        def slow_lookup(value):
            clock.now += rp.SOURCE_READ_BUDGET_S
            return lookup(value)

        def factory(**options):
            made.append(Source(server_id='7', version='8.4.6'))
            return made[-1]
        with mock.patch.object(rp, '_monotonic', clock), \
                mock.patch.object(store, 'connection_with_saved_password', slow_lookup), \
                mock.patch('myflames.connector.MySQLConnection', factory):
            found = attach([channel()], listed, rp.profile_opener(store))[0]
        self.assertEqual([(m.timeouts, m.closed) for m in made], [([], True)])
        self.assertIn('Not checked: looking up the saved password for Src', found['reason'])
        self.assertEqual(rp._source_cache, {})

    def test_each_source_read_is_bounded(self):
        source = Source(server_id='7', version='8.4.6')
        attach([channel()], [profile('a')], Opener(source))
        self.assertEqual(source.timeouts, [rp.SOURCE_QUERY_TIMEOUT_S])
        self.assertLessEqual(rp.SOURCE_CONNECT_TIMEOUT_S, rp.SOURCE_QUERY_TIMEOUT_S)
        self.assertLessEqual(rp.SOURCE_QUERY_TIMEOUT_S, rp.SOURCE_READ_BUDGET_S)


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
        for host in ('127.0.0.1', '127.0.0.2', 'localhost', '0:0:0:0:0:0:0:1', '::ffff:127.0.0.1'):
            found, problem = self.names({'host': host, 'port': 3306}, [{'name': 'ip', 'host': host}],
                                        'db9.example.com')
            self.assertEqual(found, [], host)
            self.assertIn('local to the replica, which is not on this computer', problem)

    def test_loopback_addresses_in_any_spelling(self):
        for host in ('127.0.0.1', '127.0.0.2', '127.255.255.254', '::1', '0:0:0:0:0:0:0:1', '[::1]',
                     '::ffff:127.0.0.1', 'localhost', 'LocalHost.', 'db.localhost', '0.0.0.0', '::'):
            self.assertTrue(rp._is_loopback(host), host)
        for host in ('128.0.0.1', '10.0.0.5', '::2', '::ffff:10.0.0.5', 'fe80::1', 'localhost.example.com',
                     'db1', '', None):
            self.assertFalse(rp._is_loopback(host), host)

    def test_addresses_compare_in_canonical_form(self):
        ip = [{'name': 'ip', 'host': '127.0.0.1', 'port': 3306}]
        v6 = [{'name': 'v6', 'host': '::1', 'port': 3306}]
        self.assertEqual(self.names({'host': '0:0:0:0:0:0:0:1', 'port': 3306}, v6, '127.0.0.1'), (['v6'], None))
        self.assertEqual(self.names({'host': '::ffff:127.0.0.1', 'port': 3306}, ip, '127.0.0.1'), (['ip'], None))
        # 127.0.0.2 can be another server on this computer: an exact match only.
        self.assertEqual(self.names({'host': '127.0.0.2', 'port': 3306}, ip, '127.0.0.1'), ([], None))
        self.assertEqual(self.names({'host': '127.0.0.2', 'port': 3306}, ip + [{'name': 'two', 'host': '127.0.0.2'}],
                                    '127.0.0.1'), (['two'], None))
        # A replica reached on any loopback address is on this computer.
        self.assertEqual(self.names({'host': '127.0.0.1', 'port': 3306}, ip, '127.0.0.2'), (['ip'], None))

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


class TestSurfaces(Fresh, ReplicationCase):

    def test_cli_text_and_json(self):
        conn = ReplayConn(fixture('replication', 'mysql-9.7-srcver-relay')['calls'])
        code, out, err, _f = run_cli('replication', [], conn)
        self.assertEqual(code, 0, err)
        self.assertIn('  Source version: 9.7.2 (relay log)\n', out)
        conn = ReplayConn(fixture('replication', 'mariadb-11.8-srcver-relay')['calls'])
        code, out, err, _f = run_cli('replication', ['--json'], conn)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['channels'][0]['source_version']['engine'], 'mariadb')

    def save(self, store, profiles):
        saved = {}
        for item in profiles:
            value = {k: item[k] for k in ('name', 'host', 'port', 'user', 'ssl_mode')}
            saved[item['port']] = store.save('profile', value, None, password='source-secret-1', remember=True)
        return saved

    def endpoint(self, store, name, source=None, saved=()):
        replica = ReplayConn(replica_calls(name))
        opened = []

        def factory(**options):
            opened.append(options)
            return source if options['port'] in saved else replica
        payload = {'connection': {'host': '127.0.0.1', 'port': 33298, 'user': 'root', 'password': 'replica-secret-1'}}
        with mock.patch('myflames.connector.MySQLConnection', factory):
            report = ui.api_response('/api/live/replication', payload, types.SimpleNamespace(store=store))
        return report, opened

    def test_endpoint_reads_the_matching_saved_connection_with_its_own_password(self):
        store = WorkspaceStore()
        saved = self.save(store, [MYSQL_SOURCE])
        report, opened = self.endpoint(store, 'mysql-9.7-srcver-denied', source_conn('mysql-9.7-srcver-profile'), saved)
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

    def test_endpoint_does_not_log_in_again_after_a_refusal(self):
        store = WorkspaceStore()
        saved = self.save(store, [MYSQL_SOURCE])
        refused = ReplayConn([[SOURCE_SQL, None, 'mysql client exited 1: ERROR 1045 (28000): Access denied for user '
                                                 "'probe'@'localhost' (using password: YES)"]])
        logins = 0
        for _ in range(3):
            report, opened = self.endpoint(store, 'mysql-9.7-srcver-denied', refused, saved)
            logins += len([o for o in opened if o['port'] == 33297])
        self.assertEqual(logins, 1)
        self.assertIn('Not tried again until the saved connection changes.',
                      report['channels'][0]['source_version']['reason'])

    def test_endpoint_with_a_readable_relay_log_opens_no_saved_connection(self):
        store = WorkspaceStore()
        saved = self.save(store, [MYSQL_SOURCE])
        report, opened = self.endpoint(store, 'mysql-9.7-srcver-relay', source_conn('mysql-9.7-srcver-profile'), saved)
        self.assertEqual(report['channels'][0]['source_version']['provenance'], 'relay_log')
        self.assertEqual([o['port'] for o in opened], [33298])

    def test_profile_opener_needs_a_saved_password(self):
        store = WorkspaceStore()
        saved = store.save('profile', {'name': 'NoPass', 'host': '127.0.0.1', 'port': 33297, 'user': 'probe'}, None)
        with self.assertRaisesRegex(ValueError, 'has no saved password'):
            rp.profile_opener(store)(saved)
        _fingerprint, connect = rp.profile_opener(store).prepare(saved)
        with self.assertRaisesRegex(ValueError, 'has no saved password'):
            connect()

    def test_saved_profiles_survives_an_unreadable_store(self):
        class Broken(object):
            def list(self, kind):
                raise OSError('locked')
        self.assertEqual(rp.saved_profiles(Broken()), [])


if __name__ == '__main__':
    unittest.main()
