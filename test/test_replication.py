"""Replication status, skipping one failed transaction, and the skip loop
(myflames/replication.py, ``myflames replication``, /api/live/replication*).

Replays live captures in fixtures/perf/replication-*.json, recorded on
2026-10-15 from throwaway Docker replicas: MySQL 8.4.11 with gtid_mode ON
(four workers, a second channel ``ch80`` from a MySQL 8.0 source), MySQL
8.4.11 with gtid_mode OFF (file and position), MySQL 8.0.46 with GTID, and
MariaDB 11.4.13 with GTID (a named connection ``src1``, then the default
one). Each ``*-error`` fixture is the status after a duplicate key conflict
(a row inserted on the replica, then the same key on the source); the
matching ``*-skip`` fixture is skip_one's re-read, script and read after.
``mysql-8.0.21-slave-*`` is the 8.0.46 replica answering SHOW SLAVE STATUS
with the version rewritten to 8.0.21, for the naming before 8.0.22.
"""
import contextlib
import io
import json
import os
import re
import tempfile
import types
import unittest
from unittest import mock

from _perf_replay import FakeFactory, FakeTime, ReplayConn, fixture as recorded_fixture, run_cli
from myflames import dbops, replication as rp, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import WorkspaceStore

TAG = rp.REPLICATION_TAG
REAL_RECEIVER_READER = rp.read_receiver_identity
STATUS_FIXTURES = ('mysql-8.4-healthy', 'mysql-8.4-gtid-error', 'mysql-8.4-filepos-error', 'mysql-8.4-filepos-healthy',
                   'mysql-8.4-multi-error', 'mysql-8.4-on-permissive-error', 'mysql-8.4-io-stopped',
                   'mysql-8.4-sql-stopped', 'mysql-8.4-delayed', 'mysql-8.4-source', 'mysql-8.4-denied',
                   'mysql-8.4-skip-denied-error', 'mysql-8.0-healthy', 'mysql-8.0-error', 'mysql-8.0.21-slave-error',
                   'mariadb-11.4-healthy', 'mariadb-11.4-error', 'mariadb-11.4-default-error',
                   'mariadb-11.4-parallel-domains-error', 'mariadb-11.4-io-stopped', 'mariadb-11.4-source',
                   'mariadb-11.4-denied')
#: (status fixture, skip fixture, channel, method)
SKIPS = (('mysql-8.4-gtid-error', 'mysql-8.4-gtid-skip', '', 'empty_gtid_transaction'),
         ('mysql-8.4-filepos-error', 'mysql-8.4-filepos-skip', '', 'skip_counter'),
         ('mysql-8.4-on-permissive-error', 'mysql-8.4-on-permissive-skip', '', 'empty_gtid_transaction'),
         ('mysql-8.4-multi-error', 'mysql-8.4-multi-skip', 'ch80', 'empty_gtid_transaction'),
         ('mysql-8.0-error', 'mysql-8.0-skip', '', 'empty_gtid_transaction'),
         ('mysql-8.0.21-slave-error', 'mysql-8.0.21-slave-skip', '', 'empty_gtid_transaction'),
         ('mariadb-11.4-error', 'mariadb-11.4-skip', 'src1', 'skip_counter'),
         ('mariadb-11.4-default-error', 'mariadb-11.4-default-skip', '', 'skip_counter'))
WRITE_WORDS = re.compile(r'\b(UPDATE|SET|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|TRUNCATE|START|'
                         r'STOP|CHANGE|RESET|PURGE|BEGIN|COMMIT)\b', re.I)


def fixture(view, name):
    """Extend captured reads with explicitly synthetic safety-preflight evidence.

    The original recordings predate the global-counter read and the extra
    identity reads. Duplicate the stopped snapshot, not a later healthy one.
    Grants and receiver epochs are separately injected by ReplicationCase.
    Recordings made before source versions (2026-10-20) have no answer for
    the relay log head the status endpoint and CLI now read: they get an
    explicitly synthetic refusal, so those channels show "Version unknown".
    """
    data = recorded_fixture(view, name)
    calls, block = [], []
    for original in data['calls']:
        call = list(original)
        if 'SHOW GLOBAL VARIABLES' in call[0] and 'sql_replica_skip_counter' not in call[0]:
            call[0] = call[0].replace("'super_read_only'", "'sql_replica_skip_counter', 'sql_slave_skip_counter', 'super_read_only'")
            if call[1] is not None:
                call[1] += 'sql_replica_skip_counter\t0\nsql_slave_skip_counter\t0\n'
        if call[0].startswith(rp.SKIP_TAG):
            calls.extend(block * (2 if '-loop-' in name else 1))
            calls.append(call)
            block = []
        else:
            if 'SHOW GLOBAL VARIABLES' in call[0]:
                block = []
            block.append(call)
            calls.append(call)
    data['calls'] = calls
    _synthetic_relay_heads(data)
    # Recordings predate the cluster hint a server that is not a replica now
    # reads: an explicitly synthetic empty answer (no Galera provider, no
    # Group Replication configured).
    if not any(call[0] == TAG + rp.CLUSTER_HINT_SQL for call in data['calls']):
        data['calls'].append([TAG + rp.CLUSTER_HINT_SQL, ''])
    return data


SYNTHETIC_RELAY_HEAD = ('mysql client exited 1: ERROR 1220 (HY000) at line 1: synthetic: this recording predates '
                        'the relay log head read')


def _synthetic_relay_heads(data):
    if any('LIMIT %d' % n in call[0] and 'RELAYLOG' in call[0] for call in data['calls']
           for n in rp.SOURCE_VERSION_EVENTS.values()):
        return
    try:
        report = rp.read_status(ReplayConn(data['calls']))
    except (AssertionError, ValueError, ConnectorError):
        return
    for item in report.get('channels') or ():
        sql = rp.source_version_statement(report['engine'], item)
        if sql:
            data['calls'].append([TAG + sql, None, SYNTHETIC_RELAY_HEAD])


def replay(view, name, edit=None, error=None):
    data = fixture(view, name)
    return ReplayConn(data['calls'], edit=edit, error=error), data


@contextlib.contextmanager
def private_home():
    """A temporary HOME and the vault backend: audit records go to
    ~/.myflames/workspace.sqlite3, which must never be the real one."""
    with tempfile.TemporaryDirectory() as home, \
            mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home, 'MYFLAMES_SECRET_BACKEND': 'vault'}):
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        yield home


class NoConn(object):
    """A connection that must not be used."""

    def run(self, sql, extra_flags=None, timeout=None):
        raise AssertionError('no SQL may be sent: ' + sql)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class StrictReplay(ReplayConn):
    """ReplayConn that fails once a statement's recorded answers run out, so
    a loop test ends with an error instead of polling the last answer forever."""

    def run(self, sql, extra_flags=None, timeout=None):
        if self.seen.get(sql, 0) >= len(self.answers.get(sql, ())):
            raise AssertionError('no recorded answer left for: ' + sql)
        return ReplayConn.run(self, sql, extra_flags, timeout)


def first_read(calls):
    """The calls of the first read_status in a recording."""
    seen, out = set(), []
    for call in calls:
        if call[0] in seen or call[0].startswith(rp.SKIP_TAG):
            break
        seen.add(call[0])
        out.append(call)
    return out


def cli_conn(name):
    """A recording replayed for a CLI command, which reads the status once
    itself (to show the plan) before the write path reads it again."""
    calls = fixture('replication', name)['calls']
    return StrictReplay(first_read(calls) + calls)


def status(name, edit=None):
    conn, _data = replay('replication', name, edit=edit)
    return rp.read_status(conn), conn


def channel(report, name=''):
    return rp.find_channel(report, name)


def sent_scripts(conn):
    return [(sql, flags) for sql, flags in conn.calls if sql.startswith(rp.SKIP_TAG)]


def xml_edit(field, old, new, sql_word='STATUS'):
    """Rewrite one field of the SHOW ... STATUS XML answer (every read)."""
    def edit(sql, text, n):
        if sql_word in sql and text and '<resultset' in text:
            return text.replace('<field name="%s">%s</field>' % (field, old), '<field name="%s">%s</field>'
                                % (field, new))
        return text
    return edit


def variables_edit(name, old, new):
    def edit(sql, text, n):
        if 'SHOW GLOBAL VARIABLES' in sql and text:
            return text.replace('%s\t%s\n' % (name, old), '%s\t%s\n' % (name, new))
        return text
    return edit


def chain(*edits):
    def edit(sql, text, n):
        for one in edits:
            text = one(sql, text, n)
        return text
    return edit


class ReplicationCase(unittest.TestCase):
    maxDiff = None

    def run(self, result=None):
        from myflames import privileges
        # These old live recordings did not capture grants or reconnect stages.
        # Inject unknown grants (the server still decides), and a stable synthetic
        # epoch. Dedicated review-gate tests exercise the real readers/decisions.
        with private_home() as home, \
                mock.patch.object(privileges, 'read_privileges', return_value=privileges.parse_grants([], checked=False)), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=((1, 'version-check', 1),)):
            self.home = home
            return super(ReplicationCase, self).run(result)

    def fresh_store(self):
        """A new audit database for this test or subtest."""
        self._stores = getattr(self, '_stores', 0) + 1
        self.store = WorkspaceStore(os.path.join(self.home, 'audit-%d.sqlite3' % self._stores))
        return self.store

    def audit(self, operation=rp.OPERATION_SKIP, read_only=False, surface='cli'):
        safety = dbops.connection_safety(None, {'read_only': True} if read_only else {})
        if getattr(self, 'store', None) is None:
            self.fresh_store()
        return dbops.AuditTrail(self.store, operation, surface, connection={'host': 'db', 'port': 3306, 'user': 'u'},
                                safety=safety, password='hunter2-not-real')

    def records(self):
        return dbops.audit_report(self.store)['records']


# ---- reading ------------------------------------------------------------------------

class TestStatusReplays(ReplicationCase):

    def test_every_fixture_reads_only_and_is_json(self):
        for name in STATUS_FIXTURES:
            with self.subTest(fixture=name):
                report, conn = status(name)
                self.assertEqual(report['schema_version'], 'replication-1.0')
                json.dumps(report, allow_nan=False)
                self.assertTrue(conn.calls)
                for sql, flags in conn.calls:
                    self.assertTrue(sql.startswith(TAG), sql)
                    self.assertIn('--comments', flags)
                    body = sql[len(TAG):]
                    self.assertRegex(body, r'^(SELECT|SHOW) ', sql)
                    self.assertNotRegex(body.replace('SHOW GLOBAL', '').replace('SHOW REPLICA STATUS', '')
                                        .replace('SHOW SLAVE STATUS', '').replace('SHOW ALL SLAVES STATUS', ''),
                                        r'\b(UPDATE|INSERT|DELETE|KILL|STOP|START|CHANGE|RESET)\b')
                    self.assertNotIn('@@', body)
                for c in report['channels']:
                    self.assertIn(c['status'], ('ok', 'watch', 'problem', 'unknown'))

    def test_status_is_read_by_name_from_xml(self):
        report, conn = status('mysql-8.4-healthy')
        show = [flags for sql, flags in conn.calls if sql.endswith('SHOW REPLICA STATUS')]
        self.assertEqual(show, [['--comments', '--xml']])
        self.assertEqual([c['name'] for c in report['channels']], ['', 'ch80'])
        default, ch80 = report['channels']
        self.assertEqual(default['source']['host'], 'mf-repl-g-src')
        self.assertEqual(ch80['source']['host'], 'mf-repl-80-src')
        self.assertEqual((default['io']['running'], default['sql']['running']), ('Yes', 'Yes'))
        self.assertEqual(report['gtid']['mode'], 'ON')
        self.assertEqual(report['settings']['parallel_workers'], 4)
        self.assertEqual(report['settings']['preserve_commit_order'], 'ON')
        self.assertEqual(default['agat'], 'OFF')
        self.assertEqual(default['lag_signals']['heartbeat_interval_s'], 30.0)
        self.assertEqual(default['worker']['workers'], 4)
        self.assertTrue(report['capabilities']['worker_timestamps'])
        self.assertEqual(report['summary']['judged'], 2)
        self.assertEqual(report['summary']['ok'], 2)
        self.assertNotIn('skip_plan', default)
        # A worker that never applied anything has zero timestamps: shown as None.
        self.assertIsNone(ch80['worker']['last_applied_end_apply'])
        self.assertIsNone(ch80['lag_signals']['last_queued_age_s'])

    def test_mysql_gtid_error(self):
        report, _ = status('mysql-8.4-gtid-error')
        c = channel(report)
        self.assertEqual(c['status'], 'problem')
        self.assertEqual(c['sql']['error']['errno'], 1062)
        self.assertRegex(c['failing_gtid'], r'^7256d98a-c8b1-11f1-be59-aa9276ea306a:\d+$')
        # The coordinator text has no table; the failing worker's does (live 8.4.11).
        self.assertNotIn('on table', c['sql']['error']['message'])
        self.assertEqual(c['sql']['error']['table'], 'shop.t')
        self.assertEqual(c['worker']['failing_applying_transaction'], c['failing_gtid'])
        self.assertTrue(c['relay']['complete'])
        self.assertEqual(c['relay']['gtid'], c['failing_gtid'])
        self.assertIn(c['sql']['error']['end_log_pos'], c['relay']['event_end_positions'])
        self.assertEqual(report['summary']['failed'], 1)
        self.assertIsNone(c['lag_s'])
        self.assertIn('NULL because the SQL thread is not running', c['lag_notes'][0])

    def test_mysql_before_8_0_22_uses_slave_names(self):
        report, conn = status('mysql-8.0.21-slave-error')
        self.assertEqual(report['statement'], 'SHOW SLAVE STATUS')
        self.assertTrue(any(sql.endswith('SHOW SLAVE STATUS') for sql, _ in conn.calls))
        self.assertFalse(any('replication_applier_configuration' in sql for sql, _ in conn.calls),
                         'AGAT exists from 8.0.23 only')
        c = channel(report)
        self.assertEqual((c['io']['running'], c['sql']['running']), ('Yes', 'No'))
        self.assertEqual(c['source']['host'], 'mf-repl-80-src')
        self.assertEqual(c['agat'], 'OFF')
        self.assertEqual(c['position']['exec_file'], 'binlog.000003')
        self.assertIn('Seconds_Behind_Master', ' '.join(report['notes']))

    def test_mariadb_named_connection(self):
        report, conn = status('mariadb-11.4-error')
        self.assertEqual(report['engine'], 'mariadb')
        self.assertEqual(report['statement'], 'SHOW ALL SLAVES STATUS')
        c = channel(report, 'src1')
        self.assertEqual(c['position']['using_gtid'], 'Slave_Pos')
        self.assertEqual(c['sql']['error']['table'], 'shop.t')
        # MariaDB 11.4 names no GTID in the error text and end_log_pos 0 means unknown.
        self.assertNotIn('Gtid', c['sql']['error']['message'])
        self.assertIsNone(c['sql']['error']['end_log_pos'])
        self.assertEqual(c['failing_gtid'], c['relay']['gtid'])
        self.assertRegex(c['failing_gtid'], r'^0-11-\d+$')
        self.assertTrue(any("SHOW RELAYLOG 'src1' EVENTS IN" in sql for sql, _ in conn.calls))
        self.assertEqual(report['gtid']['slave_pos_domains'][0]['domain'], 0)
        # The Gtid_Slave_Pos column is gtid_current_pos, not gtid_slave_pos.
        self.assertIn('gtid_current_pos', c['position'])
        self.assertNotIn('gtid_slave_pos', c['position'])

    def test_not_a_replica(self):
        for name, word in (('mysql-8.4-source', 'channel'), ('mariadb-11.4-source', 'connection')):
            with self.subTest(fixture=name):
                report, _ = status(name)
                self.assertTrue(report['available'])
                self.assertFalse(report['replica'])
                self.assertEqual(report['channels'], [])
                self.assertIn('not a replica', report['message'])
                self.assertIn(word, report['message'])

    def test_privilege_gate(self):
        report, conn = status('mysql-8.4-denied')
        self.assertFalse(report['available'])
        self.assertIn('REPLICATION CLIENT or SUPER', report['message'])
        self.assertIn('1227', report['message'])
        self.assertEqual(len(conn.calls), 3)
        report, _ = status('mariadb-11.4-denied')
        self.assertFalse(report['available'])
        self.assertIn('SLAVE MONITOR privilege', report['message'])

    def test_connection_failure_raises(self):
        conn, _ = replay('replication', 'mysql-8.4-healthy', error=ConnectorError('ERROR 2013 (HY000): Lost connection'))
        with self.assertRaises(ConnectorError):
            rp.read_status(conn)

    def test_other_status_errors_raise_value_error(self):
        def edit(sql, text, n):
            if sql.endswith('SHOW REPLICA STATUS'):
                from _perf_replay import _Refused
                return _Refused('ERROR 1064 (42000): You have an error in your SQL syntax')
            return text
        conn, _ = replay('replication', 'mysql-8.4-healthy', edit=edit)
        with self.assertRaises(ValueError):
            rp.read_status(conn)


class TestLagSemantics(ReplicationCase):

    def test_null_lag_with_receiver_stopped_mysql(self):
        report, _ = status('mysql-8.4-io-stopped')
        c = channel(report)
        self.assertIsNone(c['lag_s'])
        self.assertEqual(c['status'], 'problem')
        self.assertIn('The receiver (IO thread) is not running.', c['status_why'])
        self.assertIn('once the applier has caught up', c['lag_notes'][0])

    def test_null_lag_mariadb(self):
        report, _ = status('mariadb-11.4-io-stopped')
        c = channel(report)
        self.assertIsNone(c['lag_s'])
        self.assertIn('NULL unless the IO thread is connected', c['lag_notes'][0])

    def test_null_lag_while_both_threads_run_is_unknown_not_ok(self):
        report, _ = status('mysql-8.4-filepos-healthy', edit=xml_edit('Seconds_Behind_Source', '0', ''))
        c = channel(report)
        self.assertIsNone(c['lag_s'])
        self.assertEqual(c['status'], 'unknown')
        self.assertIn('lag cannot be told', c['status_why'][0])

    def test_zero_lag_is_not_proof(self):
        report, _ = status('mysql-8.4-filepos-healthy')
        self.assertIn('far behind the source', channel(report)['lag_notes'][0])
        report, _ = status('mariadb-11.4-healthy')
        self.assertIn('while the applier is idle', channel(report)['lag_notes'][0])

    def test_lag_over_the_delay(self):
        report, _ = status('mysql-8.4-delayed')
        c = channel(report)
        self.assertEqual(c['sql']['delay_s'], 30)
        self.assertEqual(c['status'], 'ok')
        self.assertTrue(any('SQL_Delay' in n for n in c['lag_notes']))
        edit = xml_edit('Seconds_Behind_Source', str(c['lag_s']), '200')
        report, _ = status('mysql-8.4-delayed', edit=edit)
        c = channel(report)
        self.assertEqual(c['status'], 'watch')
        self.assertIn('SQL_Delay is 30 s', c['status_why'][0])
        report, _ = status('mysql-8.4-delayed', edit=xml_edit('Seconds_Behind_Source', str(c['lag_s'] or 9), '80'))
        self.assertEqual(channel(report)['status'], 'ok', '80 s minus a 30 s delay is under 60 s')

    def test_idle_link_uses_newer_of_heartbeat_and_last_queued(self):
        """The source sends a heartbeat only after heartbeat_period without
        any event, so an old heartbeat alone does not mean a broken link."""
        report, _ = status('mysql-8.4-delayed')
        signals = channel(report)['lag_signals']
        self.assertGreater(signals['heartbeat_age_s'], 30)
        self.assertEqual(signals['receiver_idle_s'], signals['last_queued_age_s'])
        self.assertEqual(channel(report)['status'], 'ok')

        def ages(heartbeat_us, queued_us):
            def edit(sql, text, n):
                if 'replication_connection_status' in sql and text:
                    cells = text.rstrip('\n').split('\t')
                    cells[6], cells[7] = heartbeat_us, queued_us
                    return '\t'.join(cells) + '\n'
                return text
            return edit
        report, _ = status('mysql-8.4-delayed', edit=ages('500000000', '8000000'))
        self.assertEqual(channel(report)['status'], 'ok')
        report, _ = status('mysql-8.4-delayed', edit=ages('500000000', '400000000'))
        c = channel(report)
        self.assertEqual(c['status'], 'watch')
        self.assertIn('not even a heartbeat', c['status_why'][-1])


class TestGroupReplication(ReplicationCase):

    def edit(self):
        return xml_edit('Channel_Name', '', 'group_replication_applier')

    def test_group_member_is_not_a_replica(self):
        # A group member whose only channel is the group's: not a replica (a
        # neutral state, never unknown), and the message points to Cluster.
        report, _ = status('mysql-8.4-gtid-error', edit=self.edit())
        self.assertTrue(report['available'])
        self.assertFalse(report['replica'])
        self.assertEqual(report['channels'], [])
        self.assertEqual(report['group_replication_channels'], ['group_replication_applier'])
        self.assertIsNone(channel(report, 'group_replication_applier'))
        self.assertEqual(report['summary']['channels'], 0)
        self.assertRegex(report['message'], r'not a replica: SHOW REPLICA STATUS returned no asynchronous channel\. '
                                            r'Its group_replication_applier channel belongs to Group Replication: Cluster '
                                            r'\(myflames cluster\) shows the group\.')
        self.assertEqual(rp.format_text(report), report['message'] + '\n')
        with self.assertRaisesRegex(ValueError, 'no replication channel named'):
            rp.plan_skip(report, 'group_replication_applier')

    def test_group_channels_apart_from_asynchronous_ones(self):
        # A member that also replicates asynchronously: only that channel is
        # listed and judged; the group's channel is named in a note.
        edit = xml_edit('Channel_Name', 'ch80', 'group_replication_recovery')
        report, _ = status('mysql-8.4-multi-error', edit=edit)
        self.assertTrue(report['replica'])
        self.assertEqual([c['name'] for c in report['channels']], [''])
        self.assertEqual(report['group_replication_channels'], ['group_replication_recovery'])
        self.assertEqual(report['summary']['channels'], 1)
        self.assertEqual(report['summary']['judged'], 1)
        self.assertIn(rp.GR_CHANNELS_NOTE % 'group_replication_recovery', report['notes'])

    def test_both_group_channels_are_named(self):
        def edit(sql, text, n):
            if 'STATUS' in sql and text and '<resultset' in text:
                text = text.replace('<field name="Channel_Name">ch80</field>',
                                    '<field name="Channel_Name">group_replication_recovery</field>')
                return text.replace('<field name="Channel_Name"></field>',
                                    '<field name="Channel_Name">group_replication_applier</field>')
            return text
        report, _ = status('mysql-8.4-multi-error', edit=edit)
        self.assertFalse(report['replica'])
        self.assertEqual(report['group_replication_channels'], ['group_replication_applier', 'group_replication_recovery'])
        self.assertIn('Its group_replication_applier and group_replication_recovery channels belong to Group '
                      'Replication', report['message'])

    def test_not_a_replica_hints_at_the_cluster_place(self):
        # A server with no channel: the hint says where its own replication
        # is shown, from two variables; nothing when neither is set.
        report, conn = status('mysql-8.4-source')
        self.assertFalse(report['replica'])
        self.assertIsNone(report['cluster_hint'])
        self.assertEqual(report['message'], 'This server is not a replica: SHOW REPLICA STATUS returned no channel.')
        self.assertIn(TAG + rp.CLUSTER_HINT_SQL, [sql for sql, _flags in conn.calls])

        def answer(text):
            def edit(sql, original, n):
                return text if sql == TAG + rp.CLUSTER_HINT_SQL else original
            return edit
        report, _ = status('mysql-8.4-source', edit=answer('group_replication_group_name\t8a94f357-aab4-11df-86ab-c80aa9429562\n'))
        self.assertEqual(report['cluster_hint'], 'group_replication')
        self.assertTrue(report['message'].endswith(rp.GR_CONFIGURED_NOT_REPLICA))
        report, _ = status('mariadb-11.4-source', edit=answer('wsrep_provider\t/usr/lib/galera/libgalera_smm.so\n'))
        self.assertEqual(report['cluster_hint'], 'galera')
        self.assertTrue(report['message'].endswith(rp.GALERA_NOT_REPLICA))
        report, _ = status('mariadb-11.4-source', edit=answer('wsrep_provider\tnone\n'))
        self.assertIsNone(report['cluster_hint'])
        # A refused hint read is no hint, never a failed status read.
        conn, _data = replay('replication', 'mysql-8.4-source')
        refused = ReplayConn([c if c[0] != TAG + rp.CLUSTER_HINT_SQL else [c[0], None, 'ERROR 1142 (42000): denied']
                              for c in _data['calls']])
        self.assertIsNone(rp.read_status(refused)['cluster_hint'])
        # Group Replication's own channel sets the hint without the read.
        report, conn = status('mysql-8.4-gtid-error', edit=xml_edit('Channel_Name', '', 'group_replication_applier'))
        self.assertEqual(report['cluster_hint'], 'group_replication')
        self.assertNotIn(TAG + rp.CLUSTER_HINT_SQL, [sql for sql, _flags in conn.calls])

    def test_mariadb_names_no_group_channels(self):
        report, _ = status('mariadb-11.4-healthy')
        self.assertEqual(report['group_replication_channels'], [])
        self.assertTrue(report['replica'])


# ---- parsers ------------------------------------------------------------------------

class TestParsers(unittest.TestCase):

    def test_gtid_in_set(self):
        uuid = '7256d98a-c8b1-11f1-be59-aa9276ea306a'
        text = '%s:1-13:15,\n726f0649-c8b1-11f1-a91e-f6c08cc89039:1-9' % uuid.upper()
        self.assertTrue(rp.gtid_in_set(uuid + ':13', text))
        self.assertFalse(rp.gtid_in_set(uuid + ':14', text))
        self.assertTrue(rp.gtid_in_set(uuid + ':15', text))
        self.assertTrue(rp.gtid_in_set(uuid + ':tag1:3', '%s:1-2:tag1:1-4' % uuid))
        self.assertFalse(rp.gtid_in_set(uuid + ':3', '%s:tag1:1-4' % uuid))
        self.assertFalse(rp.gtid_in_set('ANONYMOUS', text))

    def test_mariadb_gtids(self):
        self.assertEqual(rp.parse_mariadb_gtids('0-11-12,1-11-1'),
                         [{'domain': 0, 'server': 11, 'seq': 12}, {'domain': 1, 'server': 11, 'seq': 1}])
        self.assertEqual(rp.parse_mariadb_gtids(''), [])

    def rows(self, *events):
        return [{'Event_type': e, 'End_log_pos': str(p), 'Info': i} for e, p, i in events]

    def test_relay_transaction_mysql(self):
        trx = rp.parse_relay_transaction(self.rows(
            ('Gtid', 1462, "SET @@SESSION.GTID_NEXT= 'u:5'"), ('Query', 1533, 'BEGIN'),
            ('Table_map', 1588, 'table_id: 88 (shop.t)'), ('Write_rows', 1640, 'flags: STMT_END_F'),
            ('Xid', 1671, 'COMMIT /* xid=34 */'), ('Gtid', 1750, "SET @@SESSION.GTID_NEXT= 'u:6'")))
        self.assertEqual((trx['gtid'], trx['end_log_pos'], trx['complete'], trx['tables']),
                         ('u:5', 1671, True, ['shop.t']))
        self.assertEqual(trx['event_end_positions'], [1462, 1533, 1588, 1640, 1671])

    def test_relay_transaction_anonymous_ddl_and_meta(self):
        trx = rp.parse_relay_transaction(self.rows(
            ('Format_desc', 126, 'Server ver'), ('Previous_gtids', 157, ''),
            ('Anonymous_Gtid', 236, "SET @@SESSION.GTID_NEXT= 'ANONYMOUS'"), ('Query', 380, 'CREATE TABLE x (a int)'),
            ('Anonymous_Gtid', 459, '')))
        self.assertTrue(trx['anonymous'])
        self.assertIsNone(trx['gtid'])
        self.assertEqual((trx['end_log_pos'], trx['events']), (380, 2))

    def test_relay_transaction_mariadb(self):
        trx = rp.parse_relay_transaction(self.rows(
            ('Gtid', 1225, 'BEGIN GTID 0-11-6'), ('Query', 0, 'INSERT INTO t VALUES (1)'),
            ('Table_map', 0, 'table_id: 18 (shop.t)'), ('Xid', 1419, 'COMMIT /* xid=32 */')))
        self.assertEqual((trx['gtid'], trx['end_log_pos'], trx['complete']), ('0-11-6', 1419, True))
        ddl = rp.parse_relay_transaction(self.rows(('Gtid', 900, 'GTID 0-11-7'), ('Query', 1000, 'CREATE TABLE y (a int)')))
        self.assertEqual((ddl['gtid'], ddl['end_log_pos']), ('0-11-7', 1000))
        partial = rp.parse_relay_transaction(self.rows(('Gtid', 900, 'BEGIN GTID 0-11-8'), ('Write_rows_v1', 0, '')))
        self.assertFalse(partial['complete'])
        self.assertIsNone(partial['end_log_pos'])

    def test_failing_gtid(self):
        uuid = '7256d98a-c8b1-11f1-be59-aa9276ea306a'
        c = {'sql': {'error': {'message': "Worker 1 failed executing transaction 'ANONYMOUS' at source log"}},
             'worker': {}}
        self.assertIsNone(rp.failing_gtid(c))
        c['worker'] = {'error': {'message': "Worker 2 failed executing transaction '%s:9' at" % uuid}}
        self.assertEqual(rp.failing_gtid(c), uuid + ':9')
        c = {'sql': {'error': {'message': 'x'}}, 'worker': {'failing_applying_transaction': uuid + ':3'}}
        self.assertEqual(rp.failing_gtid(c), uuid + ':3')
        c = {'sql': {'error': {'message': 'Could not execute ...; Gtid 0-1-42, Internal MariaDB error'}}}
        self.assertEqual(rp.failing_gtid(c), '0-1-42')

    def test_error_details(self):
        self.assertEqual(rp._error_details("event on table shop.t; Duplicate entry; end_log_pos 1640"),
                         ('shop.t', 1640))
        self.assertEqual(rp._error_details('the event\'s master log x, end_log_pos 0'), (None, None))

    def test_quoting_refuses_odd_names(self):
        self.assertEqual(rp._qtext("it's"), "it''s")
        with self.assertRaises(ValueError):
            rp._qtext('a\\b')


# ---- planning -----------------------------------------------------------------------

class TestPlanSkip(ReplicationCase):

    def plan(self, name, channel_name='', edit=None, change=None):
        report, _ = status(name, edit=edit)
        if change:
            change(report, channel(report, channel_name))
        return rp.plan_skip(report, channel_name), report

    def test_gtid_on_empty_transaction(self):
        plan, report = self.plan('mysql-8.4-gtid-error')
        gtid = channel(report)['failing_gtid']
        self.assertTrue(plan['available'])
        self.assertEqual(plan['method'], 'empty_gtid_transaction')
        self.assertEqual(plan['statements'], ["SET GTID_NEXT = '%s'" % gtid, 'BEGIN', 'COMMIT',
                                              "SET GTID_NEXT = 'AUTOMATIC'", "START REPLICA SQL_THREAD FOR CHANNEL ''"])
        self.assertEqual(plan['identity']['gtid'], gtid)
        self.assertEqual(plan['identity']['method'], 'empty_gtid_transaction')
        self.assertEqual(plan['identity']['source']['host'], 'mf-repl-g-src')
        self.assertEqual(plan['skip']['table'], 'shop.t')
        self.assertIn('REPLICATION_APPLIER', plan['privileges'])
        self.assertEqual(plan['drift_warning'], rp.DRIFT_WARNING)
        self.assertEqual(plan, channel(report)['skip_plan'])

    def test_named_channel(self):
        plan, _ = self.plan('mysql-8.4-multi-error', 'ch80')
        self.assertEqual(plan['statements'][-1], "START REPLICA SQL_THREAD FOR CHANNEL 'ch80'")
        with self.assertRaisesRegex(ValueError, 'no failed transaction'):
            self.plan('mysql-8.4-multi-error', '')

    def test_file_position_counter(self):
        plan, report = self.plan('mysql-8.4-filepos-error')
        c = channel(report)
        self.assertEqual(plan['method'], 'skip_counter')
        self.assertEqual(plan['statements'], ['SET GLOBAL sql_replica_skip_counter = 1',
                                              "START REPLICA SQL_THREAD FOR CHANNEL ''"])
        self.assertIsNone(plan['identity']['gtid'])
        self.assertEqual(plan['skip']['end_log_pos'], c['relay']['end_log_pos'])
        self.assertIn(plan['skip']['error_end_log_pos'], c['relay']['event_end_positions'])
        self.assertIn('SYSTEM_VARIABLES_ADMIN', plan['privileges'])

    def test_counter_names_before_8_0_26_and_8_0_22(self):
        def anonymous(report, c):
            report['gtid']['mode'] = 'OFF'
            c['failing_gtid'] = None
            c['relay'] = None
        plan, _ = self.plan('mysql-8.0.21-slave-error', change=anonymous)
        self.assertEqual(plan['statements'], ['SET GLOBAL sql_slave_skip_counter = 1',
                                              "START SLAVE SQL_THREAD FOR CHANNEL ''"])

        def v8024(report, c):
            anonymous(report, c)
            report['server_version'] = '8.0.24'
        plan, _ = self.plan('mysql-8.0.21-slave-error', change=v8024)
        self.assertEqual(plan['statements'], ['SET GLOBAL sql_slave_skip_counter = 1',
                                              "START REPLICA SQL_THREAD FOR CHANNEL ''"])
        plan, _ = self.plan('mysql-8.0.21-slave-error')
        self.assertEqual(plan['statements'][-1], "START SLAVE SQL_THREAD FOR CHANNEL ''")

    def test_on_permissive_with_a_gtid_uses_the_empty_transaction(self):
        plan, report = self.plan('mysql-8.4-on-permissive-error')
        self.assertEqual(report['gtid']['mode'], 'ON_PERMISSIVE')
        self.assertEqual(plan['method'], 'empty_gtid_transaction')
        self.assertTrue(any('would not enter gtid_executed' in n for n in plan['notes']))

    def test_permissive_anonymous_uses_the_counter(self):
        def anonymous(report, c):
            c['failing_gtid'] = None
            c['relay']['gtid'] = None
        plan, _ = self.plan('mysql-8.4-on-permissive-error', change=anonymous)
        self.assertEqual(plan['method'], 'skip_counter')
        self.assertTrue(any('ON_PERMISSIVE honors the counter' in n for n in plan['notes']))

    def test_gtid_on_without_a_gtid_is_unavailable(self):
        def lose(report, c):
            c['failing_gtid'] = None
            c['relay'] = None
        plan, _ = self.plan('mysql-8.4-gtid-error', change=lose)
        self.assertFalse(plan['available'])
        self.assertEqual(plan['statements'], [])
        self.assertIn('skip nothing', plan['message'])

    def test_agat_channel_uses_the_counter(self):
        def agat(report, c):
            c['agat'] = 'LOCAL'
        plan, _ = self.plan('mysql-8.4-gtid-error', change=agat)
        self.assertEqual(plan['method'], 'skip_counter')
        self.assertTrue(any('AGAT LOCAL' in n for n in plan['notes']))

    def test_unknown_agat_or_gtid_mode_is_unavailable(self):
        def no_agat(report, c):
            c['agat'] = None
        plan, _ = self.plan('mysql-8.4-gtid-error', change=no_agat)
        self.assertFalse(plan['available'])
        self.assertIn('assigns GTIDs to anonymous', plan['message'])

        def no_mode(report, c):
            report['gtid']['mode'] = None
        plan, _ = self.plan('mysql-8.4-gtid-error', change=no_mode)
        self.assertFalse(plan['available'])

    def test_counter_refused_when_the_relay_log_shows_another_transaction(self):
        def other(report, c):
            c['relay']['event_end_positions'] = [10, 20, 30]
        plan, _ = self.plan('mysql-8.4-filepos-error', change=other)
        self.assertFalse(plan['available'])
        self.assertIn('did not fail', plan['message'])

        def incomplete(report, c):
            c['relay']['event_end_positions'] = [10]
            c['relay']['complete'] = False
        plan, _ = self.plan('mysql-8.4-filepos-error', change=incomplete)
        self.assertFalse(plan['available'])
        self.assertIn('could not confirm', plan['message'])

    def test_multithreaded_without_commit_order_note(self):
        def unordered(report, c):
            report['settings']['preserve_commit_order'] = 'OFF'
        plan, _ = self.plan('mysql-8.4-filepos-error', change=unordered)
        self.assertTrue(any('replica_preserve_commit_order OFF' in n for n in plan['notes']))

    def test_tagged_gtid_privileges(self):
        def tagged(report, c):
            c['failing_gtid'] = '7256d98a-c8b1-11f1-be59-aa9276ea306a:mytag:5'
        plan, _ = self.plan('mysql-8.4-gtid-error', change=tagged)
        self.assertEqual(plan['statements'][0], "SET GTID_NEXT = '7256d98a-c8b1-11f1-be59-aa9276ea306a:mytag:5'")
        self.assertIn('TRANSACTION_GTID_TAG', plan['privileges'])

    def test_mariadb_statements(self):
        plan, _ = self.plan('mariadb-11.4-error', 'src1')
        self.assertEqual(plan['statements'], ["SET default_master_connection = 'src1'",
                                              'SET GLOBAL sql_slave_skip_counter = 1', "START SLAVE 'src1' SQL_THREAD"])
        self.assertEqual(plan['method'], 'skip_counter')
        self.assertRegex(plan['skip']['gtid'], r'^0-11-\d+$')
        plan, _ = self.plan('mariadb-11.4-default-error')
        self.assertEqual(plan['statements'], ["SET default_master_connection = ''",
                                              'SET GLOBAL sql_slave_skip_counter = 1', "START SLAVE '' SQL_THREAD"])

    def test_mariadb_parallel_with_two_domains_is_unavailable(self):
        """Live: SET GLOBAL sql_slave_skip_counter = 1 failed with 1966 on this replica."""
        plan, report = self.plan('mariadb-11.4-parallel-domains-error')
        self.assertEqual(report['settings']['parallel_threads'], 4)
        self.assertEqual(len(report['gtid']['slave_pos_domains']), 2)
        self.assertFalse(plan['available'])
        self.assertIn('1966', plan['message'])
        self.assertIn('gtid_slave_pos', plan['message'])
        self.assertIsNone(channel(report)['relay'], 'parallel GTID replicas report the relay log start')
        self.assertTrue(any('does not start with the failed transaction' in n for n in report['notes']))

        def one_domain(rep, c):
            rep['gtid']['slave_pos_domains'] = rep['gtid']['slave_pos_domains'][:1]
        plan, _ = self.plan('mariadb-11.4-parallel-domains-error', change=one_domain)
        self.assertFalse(plan['available'], 'One domain does not identify the failed relay transaction.')

    def test_mariadb_counter_allowed_without_parallel_threads(self):
        """Parallel_Mode says optimistic by default, but parallel replication
        is off while slave_parallel_threads is 0, so 1966 cannot happen."""
        def two_domains(report, c):
            report['gtid']['slave_pos_domains'] = [{'domain': 0, 'server': 11, 'seq': 7},
                                                   {'domain': 1, 'server': 11, 'seq': 1}]
        plan, report = self.plan('mariadb-11.4-error', 'src1', change=two_domains)
        self.assertEqual(channel(report, 'src1')['position']['parallel_mode'], 'optimistic')
        self.assertEqual(report['settings']['parallel_threads'], 0)
        self.assertTrue(plan['available'])

    def test_bad_input(self):
        report, _ = status('mysql-8.4-gtid-error')
        with self.assertRaises(ValueError):
            rp.plan_skip({}, '')
        with self.assertRaisesRegex(ValueError, 'no replication channel'):
            rp.plan_skip(report, 'nope')


# ---- skipping one transaction --------------------------------------------------------

class TestSkipOne(ReplicationCase):

    def confirmed(self, error_fixture, channel_name):
        report, _ = status(error_fixture)
        return channel(report, channel_name)['skip_plan']

    def test_skip_replays(self):
        for error_fixture, skip_fixture, name, method in SKIPS:
            with self.subTest(fixture=skip_fixture):
                plan = self.confirmed(error_fixture, name)
                self.fresh_store()
                audit = self.audit()
                conn, _ = replay('replication', skip_fixture)
                result = rp.skip_one(conn, name, plan['identity'], audit=audit, confirmed=True,
                                     statements=plan['statements'], settle_s=0)
                self.assertEqual(result['status'], 'skipped', result['message'])
                self.assertEqual(result['method'], method)
                self.assertTrue(result['after']['passed'])
                self.assertEqual(result['after']['sql_running'], 'Yes')
                scripts = sent_scripts(conn)
                self.assertEqual(len(scripts), 1)
                script, flags = scripts[0]
                self.assertEqual(script, ';\n'.join(rp.SKIP_TAG + s for s in plan['statements']) + ';')
                self.assertEqual(flags, list(dbops.WRITE_CLIENT_FLAGS))
                if method == 'empty_gtid_transaction':
                    self.assertTrue(result['after']['gtid_executed_has_skipped'])
                if skip_fixture.startswith('mariadb'):
                    domain, _server, seq = (int(p) for p in plan['skip']['gtid'].split('-'))
                    self.assertEqual(result['after']['gtid_slave_pos'], '0-11-%d' % seq)
                if plan['skip'].get('end_log_pos'):
                    self.assertEqual(result['after']['exec_pos'], plan['skip']['end_log_pos'])
                self.assertIn('the error number', result['verified'])
                records = self.records()
                self.assertEqual([(r['status'], r['outcome'], r['operation']) for r in records],
                                 [('ok', 'skipped', rp.OPERATION_SKIP)])
                self.assertEqual(records[0]['statements'], [rp.SKIP_TAG + s for s in plan['statements']])
                self.assertEqual(records[0]['target']['last_sql_errno'], 1062)
                self.assertNotIn('hunter2', json.dumps(records))

    def test_refuses_a_changed_error(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        for key, value in (('last_sql_errno', 1032), ('last_sql_error', 'something else'),
                           ('last_sql_error_timestamp', '261015 00:00:00'),
                           ('gtid', '7256d98a-c8b1-11f1-be59-aa9276ea306a:1'), ('method', 'skip_counter')):
            with self.subTest(changed=key):
                self.fresh_store()
                conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
                expect = dict(plan['identity'], **{key: value})
                result = rp.skip_one(conn, '', expect, audit=self.audit(), confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'refused')
                self.assertIn('changed since you confirmed', result['message'])
                self.assertEqual(sent_scripts(conn), [])
                self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'identity')])

    def test_refuses_a_moved_position_or_source(self):
        plan = self.confirmed('mysql-8.4-filepos-error', '')
        for key, change in (('position', {'relay_log_pos': 4}), ('source', {'host': 'elsewhere'})):
            with self.subTest(changed=key):
                conn, _ = replay('replication', 'mysql-8.4-filepos-skip')
                expect = dict(plan['identity'], **{key: dict(plan['identity'][key], **change)})
                result = rp.skip_one(conn, '', expect, confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'refused')
                self.assertEqual(sent_scripts(conn), [])

    def test_refuses_when_the_failure_is_gone(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        conn, _ = replay('replication', 'mysql-8.4-healthy')
        result = rp.skip_one(conn, '', plan['identity'], audit=self.audit(), confirmed=True, settle_s=0)
        self.assertEqual(result['status'], 'refused')
        self.assertIn('no failed transaction', result['message'])
        self.assertEqual(sent_scripts(conn), [])

    def test_refuses_when_the_applier_runs_again(self):
        report, _ = status('mysql-8.4-gtid-error')
        c = channel(report)
        plan = c['skip_plan']
        running = dict(c, sql=dict(c['sql'], running='Yes'))
        verified, problem = rp.verify_identity(running, plan, plan['identity'])
        self.assertIn('the applier is running again', problem)
        verified, problem = rp.verify_identity(c, plan, plan['identity'])
        self.assertIsNone(problem)
        self.assertIn('the source UUID', verified)

    def test_refuses_a_missing_channel(self):
        plan = self.confirmed('mysql-8.4-multi-error', 'ch80')
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
        result = rp.skip_one(conn, 'ch80', plan['identity'], audit=self.audit(), confirmed=True, settle_s=0)
        self.assertEqual(result['status'], 'refused')
        self.assertIn('gone', result['message'])
        self.assertEqual(self.records()[0]['outcome'], 'gone')

    def test_refuses_other_statements_than_confirmed(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
        result = rp.skip_one(conn, '', plan['identity'], confirmed=True, settle_s=0,
                             statements=plan['statements'][:-1])
        self.assertEqual(result['status'], 'refused')
        self.assertIn('differ from the ones you confirmed', result['message'])

    def test_read_only_refused_before_any_sql(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        decision = dbops.decide(dbops.classify(';'.join(plan['statements'])), 'production', True)
        result = rp.skip_one(NoConn(), '', plan['identity'], audit=self.audit(read_only=True), decision=decision,
                             confirmed=True, statements=plan['statements'])
        self.assertEqual(result['status'], 'refused')
        self.assertIn('these 5 statements were not sent', result['message'])
        records = self.records()
        self.assertEqual([(r['status'], r['outcome']) for r in records], [('refused', 'read_only')])
        self.assertTrue(records[0]['connection']['read_only'])

    def test_unconfirmed_needs_confirmation_on_both_types(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        for ctype in ('production', 'development'):
            with self.subTest(type=ctype):
                decision = dbops.decide(dbops.classify(';'.join(plan['statements'])), ctype, False)
                result = rp.skip_one(NoConn(), '', plan['identity'], decision=decision, statements=plan['statements'])
                self.assertEqual(result['status'], 'needs_confirmation')

    def test_denied_by_the_server(self):
        plan = self.confirmed('mysql-8.4-skip-denied-error', '')
        conn, _ = replay('replication', 'mysql-8.4-skip-denied-skip')
        result = rp.skip_one(conn, '', plan['identity'], audit=self.audit(), confirmed=True, settle_s=0)
        self.assertEqual(result['status'], 'denied')
        self.assertIn('REPLICATION_APPLIER', result['message'])
        self.assertIn('Nothing ran before it', result['message'])
        self.assertEqual(result['after']['sql_running'], 'No')
        self.assertEqual([(r['status'], r['outcome'], r['error_code']) for r in self.records()],
                         [('error', 'denied', 1227)])

    def test_server_error_mapping(self):
        statements = ['SET default_master_connection = \'\'', 'SET GLOBAL sql_slave_skip_counter = 1',
                      "START SLAVE '' SQL_THREAD"]
        status_, message = rp._server_error(ConnectorError('mysql client exited 1: ERROR 1198 (HY000) at line 2: '
                                                           'This operation cannot be performed'), statements, 'p')
        self.assertEqual(status_, 'refused_by_server')
        self.assertIn("These statements ran before it: SET default_master_connection = ''", message)
        self.assertEqual(rp._server_error(ConnectorError('ERROR 1966 (HY000) at line 2: x'), statements, 'p')[0],
                         'refused_by_server')
        self.assertEqual(rp._server_error(ConnectorError('ERROR 1105 (HY000) at line 3: x'), statements, 'p')[0],
                         'failed')
        # A tagged GTID without TRANSACTION_GTID_TAG is ER_SPECIFIC_ACCESS_DENIED (6038), not 1227.
        self.assertEqual(rp._server_error(ConnectorError('ERROR 6038 (42000) at line 1: Access denied; you need '
                                                         'the TRANSACTION_GTID_TAG'), statements, 'p')[0], 'denied')

    def test_lost_connection_is_unknown_in_the_audit(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')

        def edit(sql, text, n):
            if sql.startswith(rp.SKIP_TAG):
                from _perf_replay import _Refused
                return _Refused('mysql client exited 1: ERROR 2013 (HY000): Lost connection to server during query')
            return text
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip', edit=edit)
        with self.assertRaisesRegex(ConnectorError, 'outcome is unknown'):
            rp.skip_one(conn, '', plan['identity'], audit=self.audit(), confirmed=True, settle_s=0)
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('unknown', 'timeout')])

    def test_audit_failure_sends_nothing(self):
        plan = self.confirmed('mysql-8.4-gtid-error', '')
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
        audit = self.audit()
        with mock.patch.object(audit, 'attempt', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(ValueError, 'nothing was sent'):
                rp.skip_one(conn, '', plan['identity'], audit=audit, confirmed=True, settle_s=0)
        self.assertEqual(sent_scripts(conn), [])

    def test_not_passed_when_the_same_transaction_fails_again(self):
        report, _ = status('mysql-8.4-filepos-error')
        plan = channel(report)['skip_plan']
        after = rp._after_view(report, channel(report), plan)
        self.assertFalse(after['passed'])
        self.assertIn('did not pass', rp._skip_message(plan, after))
        self.assertTrue(after['present'])

    def test_expect_validation(self):
        for bad in (None, {}, {'channel': '', 'last_sql_errno': True, 'gtid': 'x'},
                    {'channel': '', 'last_sql_errno': 1062}, {'channel': 3, 'last_sql_errno': 1062, 'gtid': 'x'},
                    {'channel': '', 'last_sql_errno': 1062, 'gtid': 'x', 'position': 'p'}):
            with self.subTest(expect=bad):
                with self.assertRaises(ValueError):
                    rp.check_expect(bad)
        with self.assertRaisesRegex(ValueError, 'for channel'):
            rp.skip_one(NoConn(), 'other', self.confirmed('mysql-8.4-gtid-error', '')['identity'])


# ---- skip until stopped ----------------------------------------------------------------

U = '7256d98a-c8b1-11f1-be59-aa9276ea306a'


def fake_report(errno=0, running='No', gtid=U + ':1', uptime=100, source='src', mode='ON', name='', pos=100, kind=None):
    """A minimal replication-1.0 report for the loop logic."""
    error = {'errno': errno, 'message': ('Error %d on table shop.t' % errno) if errno else None, 'timestamp': None,
             'table': 'shop.t' if errno else None, 'end_log_pos': None}
    c = {'name': name, 'kind': kind or 'replication', 'judged': kind != 'group_replication',
         'source': {'host': source, 'port': 3306, 'uuid': None},
         'io': {'running': 'Yes', 'state': None, 'error': {'errno': 0, 'message': None, 'timestamp': None}},
         'sql': {'running': 'Yes' if not errno and running == 'Yes' else running, 'state': None, 'error': error},
         'lag_s': 0, 'position': {'exec_file': 'b.1', 'exec_pos': pos, 'relay_log_file': 'r.1', 'relay_log_pos': pos, 'skip_counter': 0},
         'agat': 'OFF', 'failing_gtid': gtid if errno else None, 'relay': None, 'status': 'problem' if errno else 'ok',
         'worker': {}}
    return {'schema_version': rp.SCHEMA_VERSION, 'engine': 'mysql', 'server_version': '8.4.11', 'available': True,
            'replica': True, 'uptime_s': uptime, 'global_skip_counter': 0, 'gtid': {'mode': mode}, 'settings': {}, 'channels': [c],
            'notes': []}


class LoopHarness(object):
    """read_status from a scripted list (the last one repeats); skip_one recorded."""

    def __init__(self, reports, skip_status='skipped'):
        self.reports = list(reports)
        self.reads = 0
        self.skips = []
        self.skip_status = skip_status

    def read_status(self, conn):
        report = self.reports[min(self.reads, len(self.reports) - 1)]
        self.reads += 1
        if isinstance(report, Exception):
            raise report
        if hasattr(self, 'clock'):
            report = dict(report, uptime_s=report['uptime_s'] + self.clock.now - 1000.0)
        return report

    def skip_one(self, conn, channel_name, expect, **kwargs):
        self.skips.append((channel_name, expect, kwargs))
        return {'status': self.skip_status, 'message': 'x', 'after': {'passed': True}, 'audit_id': None}

    def run(self, codes=(1062,), max_skips=5, max_duration_s=None, poll_s=1.0, on_event=None, clock=None):
        clock = clock or FakeTime()
        self.clock = clock
        loop = rp.SkipLoop(NoConn(), '', list(codes), max_skips, max_duration_s=max_duration_s, poll_s=poll_s,
                           clock=clock.monotonic, sleep=clock.sleep, on_event=on_event, settle_s=0)
        with mock.patch.object(rp, 'read_status', self.read_status), mock.patch.object(rp, 'skip_one', self.skip_one):
            final = loop.run()
        return final, clock, loop


class TestSkipLoop(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(rp, 'read_receiver_identity', return_value=((1, 'version-check', 1),))
        patch.start()
        self.addCleanup(patch.stop)

    def test_skips_both_then_stops_at_the_cap(self):
        h = LoopHarness([fake_report(1062, gtid=U + ':5', pos=1), fake_report(1062, gtid=U + ':6', pos=2),
                         fake_report(1062, gtid=U + ':7', pos=3)])
        final, clock, _ = h.run(max_skips=2)
        self.assertEqual((final['state'], final['stop_reason'], final['skips_done']), ('stopped', 'max_skips', 2))
        self.assertEqual([s['gtid'] for s in final['skips']], [U + ':5', U + ':6'])
        self.assertFalse(final['needs_attention'])
        self.assertEqual(len(h.skips), 2)
        self.assertTrue(all(k['confirmed'] is True and k['statements'] for _c, _e, k in h.skips))
        self.assertTrue(all(s <= 0.1 or s == 1.0 for s in clock.slept), 'no real sleeps')

    def test_stops_on_an_unselected_error(self):
        h = LoopHarness([fake_report(1062, gtid=U + ':5'), fake_report(1032, gtid=U + ':6')])
        final, _, _ = h.run(codes=[1062])
        self.assertEqual((final['stop_reason'], final['skips_done']), ('unselected_error', 1))
        self.assertTrue(final['needs_attention'])
        self.assertIn('Error 1032', final['error'])

    def test_no_progress(self):
        h = LoopHarness([fake_report(1062, gtid=U + ':5')])
        final, _, _ = h.run()
        self.assertEqual((final['stop_reason'], final['skips_done']), ('no_progress', 1))

    def test_duration(self):
        h = LoopHarness([fake_report(0, running='Yes')])
        final, clock, _ = h.run(max_duration_s=10, poll_s=2)
        self.assertEqual(final['stop_reason'], 'duration')
        self.assertGreaterEqual(clock.now - 1000.0, 10)
        self.assertEqual(h.skips, [])

    def test_stop_takes_effect_before_the_next_skip(self):
        h = LoopHarness([fake_report(1062, gtid=U + ':5', pos=1), fake_report(1062, gtid=U + ':6', pos=2)])
        holder = {}

        def on_event(event):
            if event.get('event') == 'skip':
                holder['loop'].stop()
        clock = FakeTime()
        loop = rp.SkipLoop(NoConn(), '', [1062], 5, clock=clock.monotonic, sleep=clock.sleep, on_event=on_event,
                           settle_s=0)
        holder['loop'] = loop
        with mock.patch.object(rp, 'read_status', h.read_status), mock.patch.object(rp, 'skip_one', h.skip_one):
            final = loop.run()
        self.assertEqual((final['stop_reason'], final['skips_done']), ('user_stop', 1))
        self.assertFalse(final['needs_attention'])

    def test_stop_wakes_a_waiting_loop(self):
        """Without an injected sleep the loop waits on its stop event, so Stop
        ends a long wait at once (the 60 s backoff never blocks a Stop)."""
        h = LoopHarness([fake_report(0, running='Yes')])
        loop = rp.SkipLoop(NoConn(), '', [1062], 5, poll_s=60, settle_s=0)
        with mock.patch.object(rp, 'read_status', h.read_status), mock.patch.object(rp, 'skip_one', h.skip_one):
            loop.start()
            for _ in range(200):
                if h.reads:
                    break
                import time
                time.sleep(0.005)
            loop.stop()
            loop.join(5)
        self.assertEqual(loop.status()['stop_reason'], 'user_stop')

    def test_server_restart_source_mode_channel_changes(self):
        cases = (
            ('server_restarted', [fake_report(0, running='Yes', uptime=100), fake_report(0, running='Yes', uptime=5)]),
            ('config_changed', [fake_report(0, running='Yes'), fake_report(0, running='Yes', source='other')]),
            ('config_changed', [fake_report(0, running='Yes'), fake_report(0, running='Yes', mode='ON_PERMISSIVE')]),
            ('config_changed', [fake_report(0, running='Yes', kind='group_replication')]),
            ('channel_gone', [fake_report(0, running='Yes', name='other')]),
            ('applier_stopped', [fake_report(0, running='No')]),
            ('connection_lost', [ConnectorError('ERROR 2013 (HY000): Lost connection')]),
        )
        for reason_key, reports in cases:
            with self.subTest(reason=reason_key):
                h = LoopHarness(reports)
                final, _, _ = h.run()
                self.assertEqual(final['stop_reason'], reason_key)
                self.assertEqual(h.skips, [])
                self.assertTrue(final['needs_attention'])

    def test_refused_or_denied_skip_stops(self):
        for skip_status, reason_key in (('refused', 'refused'), ('denied', 'denied'), ('failed', 'failed'),
                                        ('refused_by_server', 'refused')):
            with self.subTest(status=skip_status):
                h = LoopHarness([fake_report(1062, gtid=U + ':5')], skip_status=skip_status)
                final, _, _ = h.run()
                self.assertEqual((final['stop_reason'], final['skips_done']), (reason_key, 0))

    def test_backoff_doubles_while_skips_keep_coming(self):
        reports = [fake_report(1062, gtid=U + ':%d' % i, pos=i) for i in range(1, 9)]
        h = LoopHarness(reports)
        final, clock, _ = h.run(max_skips=8, poll_s=5)
        self.assertEqual(final['skips_done'], 8)
        self.assertEqual(clock.slept, [5, 10, 20, 40, 60, 60, 60])

    def test_running_applier_resets_the_backoff(self):
        reports = [fake_report(1062, gtid=U + ':1', pos=1), fake_report(0, running='Yes'),
                   fake_report(1062, gtid=U + ':2', pos=2), fake_report(1032, gtid=U + ':3', pos=3)]
        h = LoopHarness(reports)
        final, clock, _ = h.run(poll_s=3)
        self.assertEqual(clock.slept, [3, 3, 3])
        self.assertEqual(final['stop_reason'], 'unselected_error')

    def test_validation(self):
        for kwargs in ({'codes': []}, {'codes': None}, {'codes': [0]}, {'codes': [True]}, {'max_skips': 0},
                       {'max_skips': None}, {'max_skips': rp.LOOP_MAX_SKIPS + 1}, {'poll_s': 0.5},
                       {'max_duration_s': 0}):
            with self.subTest(kwargs=kwargs):
                args = dict({'codes': [1062], 'max_skips': 2}, **kwargs)
                with self.assertRaises(ValueError):
                    rp.SkipLoop(NoConn(), '', args.pop('codes'), args.pop('max_skips'), **args)
        with self.assertRaisesRegex(ValueError, 'None are preselected'):
            rp.check_codes([])

    def test_status_message(self):
        h = LoopHarness([fake_report(1062, gtid=U + ':5', pos=1), fake_report(1032, gtid=U + ':6', pos=2)])
        final, _, loop = h.run()
        self.assertEqual(final['schema_version'], rp.LOOP_SCHEMA_VERSION)
        self.assertIn('Skipped 1 on channel', final['message'])
        self.assertEqual(final['skips'][0]['table'], 'shop.t')
        self.assertIsNone(final['remaining_s'])
        self.assertEqual(final['drift_warning'], rp.DRIFT_WARNING)


class TestSkipLoopReplays(ReplicationCase):
    """The live loop runs: two conflicts with 1062 selected and a cap of 2,
    then a 1032 conflict with only 1062 selected."""

    def run_loop(self, name, codes, max_skips):
        data = fixture('replication', name)
        conn = StrictReplay(data['calls'])
        self.assertEqual(data['args']['codes'], codes)
        clock = FakeTime()
        self.fresh_store()
        trails = []

        def factory():
            trail = self.audit(rp.OPERATION_LOOP)
            trails.append(trail)
            return trail
        loop = rp.SkipLoop(conn, '', codes, max_skips, clock=clock.monotonic, sleep=clock.sleep, settle_s=1.0,
                           poll_s=1, audit_factory=factory)
        return loop.run(), conn

    def test_loop_skips_both_and_stops_at_the_cap(self):
        for name, pattern in (('mysql-8.4-loop-max', r'^7256d98a-c8b1-11f1-be59-aa9276ea306a:\d+$'),
                              ('mariadb-11.4-loop-max', r'^0-11-\d+$')):
            with self.subTest(fixture=name):
                final, conn = self.run_loop(name, [1062], 2)
                self.assertEqual((final['stop_reason'], final['skips_done']), ('max_skips', 2))
                gtids = [s['gtid'] for s in final['skips']]
                for gtid in gtids:
                    self.assertRegex(gtid, pattern)
                self.assertNotEqual(gtids[0], gtids[1])
                self.assertTrue(all(s['passed'] for s in final['skips']))
                self.assertEqual(len(sent_scripts(conn)), 2)
                records = self.records()
                self.assertEqual([(r['operation'], r['status']) for r in records],
                                 [(rp.OPERATION_LOOP, 'ok'), (rp.OPERATION_LOOP, 'ok')])

    def test_loop_stops_on_an_unselected_error(self):
        for name in ('mysql-8.4-loop-unselected', 'mariadb-11.4-loop-unselected'):
            with self.subTest(fixture=name):
                final, conn = self.run_loop(name, [1062], 5)
                self.assertEqual((final['stop_reason'], final['skips_done']), ('unselected_error', 0))
                self.assertIn('1032', final['error'])
                self.assertEqual(sent_scripts(conn), [])


class TestWorkerStoppedBehindAFailure(unittest.TestCase):
    """Recorded live (MySQL 8.4.11, 4 workers, replica_preserve_commit_order
    ON): worker 1 failed with 1062 on :15, worker 2 then stopped with 3030 on
    :16 (rolled back, not committed), and SHOW REPLICA STATUS reported the 3030
    and :16. The plan must skip :15, never the transaction that stopped."""

    U3 = 'e5572236-c8c2-11f1-b861-9a23772a56c6'

    def test_plan_skips_the_earlier_failure(self):
        report, conn = status('mysql-8.4-mta-3030-error')
        c = channel(report)
        self.assertEqual(c['sql']['error']['errno'], rp.WORKER_STOPPED_BY_EARLIER)
        self.assertIn("'%s:16'" % self.U3, c['sql']['error']['message'])
        self.assertEqual((c['worker']['error']['errno'], c['worker']['failing_worker_id']), (1062, 1))
        self.assertEqual(c['failing_gtid'], self.U3 + ':15')
        self.assertEqual((c['sql']['error']['table'], c['sql']['error']['end_log_pos']), ('shop.t', 2793))
        plan = c['skip_plan']
        self.assertTrue(plan['available'], plan['message'])
        self.assertEqual(plan['method'], 'empty_gtid_transaction')
        self.assertEqual(plan['statements'][0], "SET GTID_NEXT = '%s:15'" % self.U3)
        self.assertNotIn(':16', ' '.join(plan['statements']))
        self.assertEqual(plan['identity']['last_sql_errno'], 3030, 'the re-read compares the status as it is')
        self.assertEqual(plan['identity']['gtid'], self.U3 + ':15')
        self.assertEqual(plan['error']['errno'], 1062)
        self.assertIn('Duplicate entry', plan['error']['message'])
        self.assertTrue(any('3030' in n for n in plan['notes']))

    def test_no_plan_when_only_the_stopped_worker_is_visible(self):
        def edit(sql, text, n):
            # Worker 1's row as if it had no error: only the 3030 remains.
            if 'replication_applier_status_by_worker' in sql:
                return '\n'.join(line for line in text.split('\n') if '\t1062\t' not in line)
            return text
        report, conn = status('mysql-8.4-mta-3030-error', edit=edit)
        c = channel(report)
        self.assertIsNone(rp.effective_error(c))
        self.assertIsNone(c['failing_gtid'])
        self.assertFalse(c['skip_plan']['available'])
        self.assertIn('cannot tell which transaction to skip', c['skip_plan']['message'])
        self.assertEqual(c['skip_plan']['statements'], [])

    def test_loop_skips_the_selected_error_behind_a_3030(self):
        patch = mock.patch.object(rp, 'read_receiver_identity', return_value=((1, 'version-check', 1),))
        patch.start()
        self.addCleanup(patch.stop)
        report, conn = status('mysql-8.4-mta-3030-error')
        healthy, unused = status('mysql-8.4-healthy')
        h = LoopHarness([report, healthy])
        final, unused_clock, unused_loop = h.run(codes=(1062,), max_skips=1)
        self.assertEqual((final['stop_reason'], final['skips_done']), ('max_skips', 1))
        self.assertEqual(final['skips'][0]['errno'], 1062)
        self.assertEqual(final['skips'][0]['gtid'], self.U3 + ':15')
        self.assertEqual(h.skips[0][1]['gtid'], self.U3 + ':15')
        h = LoopHarness([report])
        final, unused_clock, unused_loop = h.run(codes=(3030,), max_skips=1)
        self.assertEqual(final['stop_reason'], 'unselected_error', 'the error that failed is 1062, not 3030')
        self.assertEqual(h.skips, [])


class TestRegistry(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(rp, 'read_receiver_identity', return_value=((1, 'version-check', 1),))
        patch.start()
        self.addCleanup(patch.stop)

    def loop(self, name=''):
        h = LoopHarness([fake_report(0, running='Yes')])
        loop = rp.SkipLoop(NoConn(), name, [1062], 2, poll_s=60, settle_s=0)
        return loop, h

    def test_keyed_by_connection_identity(self):
        registry = rp.SkipLoopRegistry()
        loop, h = self.loop()
        with mock.patch.object(rp, 'read_status', h.read_status):
            started = registry.start('server-a', loop)
            self.assertIn(started['state'], ('starting', 'running'))
            with self.assertRaisesRegex(ValueError, 'already running'):
                registry.start('server-a', self.loop()[0])
            with self.assertRaisesRegex(ValueError, 'another connection'):
                registry.status('server-b', loop.job_id)
            with self.assertRaisesRegex(ValueError, 'another connection'):
                registry.stop('server-b', loop.job_id)
            self.assertEqual(registry.status('server-b')['loops'], [])
            self.assertEqual(len(registry.status('server-a')['loops']), 1)
            other, _ = self.loop()
            registry.start('server-b', other)
            registry.stop('server-a', loop.job_id)
            registry.stop_all()
            loop.join(5)
            other.join(5)
        self.assertEqual(loop.status()['stop_reason'], 'user_stop')
        self.assertEqual(registry.stop('server-a', loop.job_id)['state'], 'stopped')

    def test_a_policy_change_keeps_status_and_stop(self):
        """The identity carries the connection's type and read-only flag
        (dbops.connection_identity); changing them while a loop runs must not
        strand it: status and Stop still answer, a second loop on the channel
        is still refused, and another endpoint still sees nothing."""
        from myflames import dbops
        connection = {'host': 'db1', 'port': 3306, 'user': 'dba', 'profile_id': 'p1'}
        development = dbops.connection_identity(connection, {'type': 'development', 'read_only': False})
        read_only = dbops.connection_identity(connection, {'type': 'development', 'read_only': True})
        production = dbops.connection_identity(connection, {'type': 'production', 'read_only': False})
        other = dbops.connection_identity(dict(connection, host='db2'), {'type': 'development'})
        self.assertNotEqual(development, production)
        registry = rp.SkipLoopRegistry()
        loop, h = self.loop()
        with mock.patch.object(rp, 'read_status', h.read_status):
            registry.start(development, loop)
            for identity in (production, read_only):
                self.assertEqual([j['job_id'] for j in registry.status(identity)['loops']], [loop.job_id])
                self.assertEqual(registry.status(identity, loop.job_id)['job_id'], loop.job_id)
            with self.assertRaisesRegex(ValueError, 'already running'):
                registry.start(production, self.loop()[0])
            self.assertEqual(registry.status(other)['loops'], [])
            with self.assertRaisesRegex(ValueError, 'another connection'):
                registry.stop(other, loop.job_id)
            self.assertIn(registry.stop(read_only, loop.job_id)['state'], ('stopping', 'stopped'))
            loop.join(5)
        self.assertEqual(loop.status()['stop_reason'], 'user_stop')

    def test_a_new_registry_never_resumes(self):
        registry = rp.SkipLoopRegistry()
        self.assertEqual(registry.status('server-a')['loops'], [])
        with self.assertRaisesRegex(ValueError, 'never survive a myflames restart'):
            registry.status('server-a', 'f' * 32)


# ---- CLI ----------------------------------------------------------------------------

class TTYStdin(io.StringIO):
    def isatty(self):
        return True


class TestCli(ReplicationCase):

    def test_status_json_and_text(self):
        conn, _ = replay('replication', 'mysql-8.4-healthy')
        code, out, err, _f = run_cli('replication', ['--json'], conn)
        self.assertEqual(code, 0, err)
        self.assertEqual([c['name'] for c in json.loads(out)['channels']], ['', 'ch80'])
        conn, _ = replay('replication', 'mysql-8.4-gtid-error')
        code, out, err, _f = run_cli('replication', [], conn)
        self.assertEqual(code, 0, err)
        self.assertIn("myflames replication skip --channel ''", out)
        conn, _ = replay('replication', 'mysql-8.4-healthy')
        code, out, err, _f = run_cli('replication', ['--channel', 'ch80', '--json'], conn)
        self.assertEqual([c['name'] for c in json.loads(out)['channels']], ['ch80'])

    def test_status_bad_channel_and_denied_exit_2(self):
        conn, _ = replay('replication', 'mysql-8.4-healthy')
        code, out, err, _f = run_cli('replication', ['--channel', 'nope'], conn)
        self.assertEqual(code, 2)
        self.assertIn("no replication channel named 'nope'", err)
        conn, _ = replay('replication', 'mysql-8.4-denied')
        code, out, err, _f = run_cli('replication', [], conn)
        self.assertEqual(code, 2)
        self.assertIn('REPLICATION CLIENT', err)

    def skip(self, argv, fixture='mysql-8.4-gtid-skip', stdin=None):
        conn = cli_conn(fixture)
        patches = [mock.patch('sys.stdin', stdin if stdin is not None else io.StringIO('')),
                   mock.patch.object(rp, 'SETTLE_S', 0), mock.patch('myflames.replication.time.sleep')]
        code, out, err, factory = run_cli('replication', ['skip'] + argv, conn, patches=patches)
        return code, out, err, conn, factory

    def test_skip_needs_yes_without_a_terminal(self):
        code, out, err, conn, _ = self.skip(['--channel', ''])
        self.assertEqual(code, 2)
        self.assertIn('stdin is not a terminal', err)
        self.assertIn('SET GTID_NEXT', err)
        self.assertEqual(sent_scripts(conn), [])
        self.store = WorkspaceStore()
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'no_terminal')])

    def test_skip_yes(self):
        code, out, err, conn, _ = self.skip(['--channel', '', '--yes', '--json'])
        self.assertEqual(code, 0, out + err)
        result = json.loads(out)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(sent_scripts(conn)), 1)
        self.store = WorkspaceStore()
        self.assertEqual([(r['status'], r['surface']) for r in self.records()], [('ok', 'cli')])

    def test_skip_declined_at_the_prompt(self):
        code, out, err, conn, _ = self.skip(['--channel', ''], stdin=TTYStdin('n\n'))
        self.assertEqual(code, 2)
        self.assertIn('Skip this transaction? [y/N]', err)
        self.assertEqual(sent_scripts(conn), [])
        self.store = WorkspaceStore()
        self.assertEqual([r['status'] for r in self.records()], ['declined'])

    def test_skip_confirmed_at_the_prompt(self):
        code, out, err, conn, _ = self.skip(['--channel', ''], stdin=TTYStdin('y\n'))
        self.assertEqual(code, 0, out + err)
        self.assertIn('Skipped transaction', out)

    def test_skip_nothing_to_skip(self):
        code, out, err, conn, _ = self.skip(['--channel', 'ch80', '--yes'], fixture='mysql-8.4-healthy')
        self.assertEqual(code, 2)
        self.assertIn('no failed transaction', err)

    def test_skip_read_only_profile_refuses_before_connecting(self):
        store = WorkspaceStore()
        store.save('profile', {'name': 'prod', 'host': 'db', 'port': 3306, 'user': 'u', 'read_only': True})
        conn, _ = replay('replication', 'mysql-8.4-gtid-skip')
        code, out, err, factory = run_cli('replication', ['skip', '--profile', 'prod', '--channel', '', '--yes'],
                                          conn)
        self.assertEqual(code, 2)
        self.assertIsNone(factory.options, 'no connection was opened')
        self.assertIn('read-only', err)
        self.store = store
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'read_only')])

    def test_skip_requires_channel(self):
        code, out, err, conn, _ = self.skip(['--yes'])
        self.assertEqual(code, 2)
        self.assertIn('--channel', err)

    def loop(self, argv, fixture):
        conn = cli_conn(fixture)
        clock = FakeTime()

        def sampled_time(sql, text, n):
            # Playback time follows the captured Uptime, not test wall time.
            match = re.search(r'Uptime\t(\d+)', text or '') if "'Uptime'" in sql else None
            if match:
                clock.now = max(clock.now, 1000.0 + int(match.group(1)))
            return text

        conn.edit = sampled_time
        patches = [mock.patch('sys.stdin', io.StringIO('')), mock.patch.object(rp.SkipLoop, '_wait'),
                   mock.patch('myflames.replication.time.sleep'),
                   mock.patch('myflames.replication.time.monotonic', side_effect=clock.monotonic)]
        return run_cli('replication', ['skip-loop'] + argv, conn, patches=patches) + (conn,)

    def test_skip_loop_cap(self):
        code, out, err, _f, conn = self.loop(['--channel', '', '--errors', '1062', '--max', '2', '--yes'],
                                             'mysql-8.4-loop-max')
        self.assertEqual(code, 0, err)
        # Live events go to stderr; stdout (or -o) carries only the final result.
        events = [line for line in err.splitlines() if re.search(r'  skip \d+: error ', line)]
        self.assertEqual(len(events), 2)
        self.assertNotRegex(out, r'  skip \d+: error ')
        self.assertIn('Reached the maximum number of skips', out)
        self.assertEqual(len(sent_scripts(conn)), 2)

    def test_skip_loop_unselected_exits_1(self):
        code, out, err, _f, conn = self.loop(['--channel', '', '--errors', '1062', '--yes', '--json'],
                                             'mysql-8.4-loop-unselected')
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)['stop_reason'], 'unselected_error')
        self.assertIn('not in your list', err)

    def test_skip_loop_receiver_guard_confirmation_and_json(self):
        # Coverage is synthetic; the loop and confirmation use the same reader.
        for engine, recording, instrument in (
                ('mysql', 'mysql-8.4-loop-max', 'stage/sql/Checking source version'),
                ('mariadb', 'mariadb-11.4-loop-max', 'stage/sql/Checking master version')):
            for enabled in (True, False):
                with self.subTest(engine=engine, enabled=enabled), \
                        mock.patch.object(rp, 'read_receiver_identity',
                                          return_value=((1, 'check', 1),) if enabled else None):
                    code, out, err, _f, conn = self.loop(
                        ['--channel', '', '--errors', '1062', '--max', '2', '--yes', '--json'], recording)
                    self.assertEqual(code, 0, out + err)
                    result = json.loads(out)
                    self.assertEqual(result['receiver_guard'], 'on' if enabled else 'off')
                    self.assertEqual(result['receiver_guard_instrument'], instrument)
                    if enabled:
                        self.assertIn('\n  Stops if a receiver reconnects.\n', err)
                        self.assertIsNone(result['receiver_guard_reason'])
                    else:
                        self.assertIn('\n  ' + result['receiver_guard_note'] + '\n', err)
                        self.assertIn(instrument, result['receiver_guard_note'])
                        self.assertTrue(result['receiver_guard_reason'])
                        self.assertNotIn('Stops if a receiver reconnects.', err)
                    self.assertNotIn('Stops if a receiver reconnects.', out)
                    self.assertEqual(len(sent_scripts(conn)), 2, 'missing coverage does not block Start')

    def test_skip_loop_quiet_keeps_receiver_guard_json(self):
        with mock.patch.object(rp, 'read_receiver_identity', return_value=None):
            code, out, err, _f, _conn = self.loop(
                ['--channel', '', '--errors', '1062', '--max', '2', '--yes', '--json', '--quiet'],
                'mysql-8.4-loop-max')
        self.assertEqual(code, 0, out + err)
        self.assertEqual(err, '')
        self.assertEqual(json.loads(out)['receiver_guard'], 'off')

    def test_skip_loop_needs_yes_and_codes(self):
        code, out, err, _f, conn = self.loop(['--channel', '', '--errors', '1062'], 'mysql-8.4-loop-max')
        self.assertEqual(code, 2)
        self.assertIn('stdin is not a terminal', err)
        self.assertEqual(sent_scripts(conn), [])
        for bad in (['--channel', '', '--yes'], ['--channel', '', '--errors', 'abc', '--yes'],
                    ['--channel', '', '--errors', '1062', '--max', '0', '--yes']):
            with self.subTest(argv=bad):
                code, out, err, _f, conn = self.loop(bad, 'mysql-8.4-loop-max')
                self.assertEqual(code, 2)
                self.assertEqual(conn.calls, [])

    def test_dispatched_by_main(self):
        from _perf_replay import run_main
        conn, _ = replay('replication', 'mysql-8.4-healthy')
        code, out, err = run_main(['myflames', 'replication', '--json'], conn)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['schema_version'], 'replication-1.0')


# ---- workspace API ----------------------------------------------------------------------

class TestEndpoints(ReplicationCase):

    def setUp(self):
        self.store = WorkspaceStore()
        self.workspace = types.SimpleNamespace(store=self.store)
        self.connection = {'host': 'db', 'port': 3306, 'user': 'u', 'password': 'hunter2-not-real'}

    def call(self, path, payload, fixture=None, edit=None):
        payload = dict(payload)
        if path == '/api/live/replication/skip' and isinstance(payload.get('expect'), dict):
            payload['review_token'] = ui._review_tokens(self.workspace).issue('replication_skip',
                ui._review_identity(payload.get('connection', self.connection), self.workspace),
                {k: payload.get(k) for k in ('channel', 'expect', 'statements')})
        conn = None
        factory = FakeFactory(NoConn())
        if fixture:
            conn, _ = replay('replication', fixture, edit=edit)
            factory = FakeFactory(conn)
        with mock.patch('myflames.connector.MySQLConnection', factory), \
                mock.patch('myflames.replication.time.sleep'), mock.patch.object(rp, 'SETTLE_S', 0):
            return ui.api_response(path, dict({'connection': self.connection}, **payload), self.workspace), conn, \
                factory

    def test_paths(self):
        self.assertIn('/api/live/replication', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/live/replication', ui._WRITE_PATHS)
        for path in ('/api/live/replication/skip', '/api/live/replication/skip-loop/start'):
            self.assertIn(path, ui._WRITE_PATHS)
            self.assertIn(path, ui._CONNECTION_PATHS)
        for path in ('/api/live/replication/skip-loop/status', '/api/live/replication/skip-loop/stop'):
            self.assertNotIn(path, ui._CONNECTION_PATHS)
            self.assertIn(path, ui._REPLICATION_PATHS)
        for path in ui._REPLICATION_PATHS:
            self.assertNotIn(path, ui._SERVER_VIEW_PATHS)

    def test_status(self):
        report, conn, factory = self.call('/api/live/replication', {}, 'mysql-8.4-healthy')
        self.assertEqual(report['schema_version'], 'replication-1.0')
        self.assertEqual(factory.options['query_timeout'], 30)

    def test_loop_confirmation_carries_receiver_guard_coverage(self):
        for fixture_name, instrument in (('mysql-8.4-gtid-error', 'stage/sql/Checking source version'),
                                          ('mariadb-11.4-error', 'stage/sql/Checking master version')):
            for enabled in (True, False):
                with self.subTest(fixture=fixture_name, enabled=enabled), \
                        mock.patch.object(rp, 'read_receiver_identity',
                                          return_value=((7, instrument, 1),) if enabled else None):
                    report, _, _ = self.call('/api/live/replication', {}, fixture_name)
                plan = next(c['skip_plan'] for c in report['channels'] if c.get('skip_plan'))
                self.assertEqual(plan['receiver_guard'], 'on' if enabled else 'off')
                self.assertEqual(plan['receiver_guard_instrument'], instrument)
                self.assertEqual(plan['receiver_guard_note'], report['receiver_guard_note'])
                if not enabled:
                    self.assertIn('Receiver reconnects are not detected', plan['receiver_guard_note'])
                    self.assertTrue(plan['receiver_guard_reason'])

    def test_loop_keeps_guard_requested_by_confirmation(self):
        body = {'channel': '', 'codes': [1062], 'max_skips': 1, 'confirmed': True, 'receiver_guard': 'on'}
        with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=None), \
                mock.patch.object(rp, 'skip_one') as skip:
            started, _, _ = self.call('/api/live/replication/skip-loop/start', body)
            for _, loop in self.workspace.replication_loops._jobs:
                loop.join(5)
            self.addCleanup(self.workspace.replication_loops.stop_all, wait=True)
            final, _, _ = self.call('/api/live/replication/skip-loop/status', {'job_id': started['job_id']})
        self.assertEqual(final['stop_reason'], 'receiver_unverified')
        skip.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'receiver_guard'):
            self.call('/api/live/replication/skip-loop/start', dict(body, receiver_guard='maybe'))

    def plan(self):
        report, _ = status('mysql-8.4-gtid-error')
        return channel(report)['skip_plan']

    def test_skip_policy_and_confirmation(self):
        plan = self.plan()
        body = {'channel': '', 'expect': plan['identity'], 'statements': plan['statements']}
        result, _, factory = self.call('/api/live/replication/skip', body)
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertIsNone(factory.options)
        self.connection['read_only'] = True
        result, _, factory = self.call('/api/live/replication/skip', dict(body, confirmed=True))
        self.assertEqual(result['status'], 'refused')
        self.assertIsNone(factory.options, 'read-only refuses before opening a connection')
        del self.connection['read_only']
        result, conn, factory = self.call('/api/live/replication/skip', dict(body, confirmed=True),
                                          'mysql-8.4-gtid-skip')
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(sent_scripts(conn)), 1)
        records = self.records()
        self.assertEqual([(r['status'], r['outcome'], r['surface']) for r in records],
                         [('ok', 'skipped', 'ui'), ('refused', 'read_only', 'ui'),
                          ('refused', 'needs_confirmation', 'ui')])
        self.assertNotIn('hunter2', json.dumps(records))

    def test_skip_bad_input(self):
        for body in ({'channel': 1}, {'channel': '', 'expect': {}}, {'channel': '', 'expect': self.plan()['identity'],
                                                                     'statements': 'x'}):
            with self.subTest(body=body):
                with self.assertRaises(ValueError):
                    self.call('/api/live/replication/skip', body)

    def test_saved_password_only_after_the_policy(self):
        profile = self.store.save('profile', {'name': 'ro', 'host': 'db', 'port': 3306, 'user': 'u',
                                              'read_only': True})
        self.connection = {'profile_id': profile['id'], 'host': 'db', 'port': 3306, 'user': 'u'}
        plan = self.plan()
        with mock.patch.object(self.store, 'connection_with_saved_password') as saved:
            result, _, factory = self.call('/api/live/replication/skip', {
                'channel': '', 'expect': plan['identity'], 'statements': plan['statements'], 'confirmed': True})
        self.assertEqual(result['status'], 'refused')
        saved.assert_not_called()

    def test_loop_start_status_stop(self):
        body = {'channel': '', 'codes': [1062], 'max_skips': 2, 'poll_s': 60}
        result, _, factory = self.call('/api/live/replication/skip-loop/start', body)
        self.assertEqual(result['state'], 'needs_confirmation')
        self.connection['read_only'] = True
        result, _, factory = self.call('/api/live/replication/skip-loop/start', dict(body, confirmed=True))
        self.assertEqual(result['state'], 'refused')
        self.assertIsNone(factory.options)
        del self.connection['read_only']
        with self.assertRaises(ValueError):
            self.call('/api/live/replication/skip-loop/start', dict(body, codes=[], confirmed=True))
        h = LoopHarness([fake_report(0, running='Yes')])
        with mock.patch.object(rp, 'read_status', h.read_status):
            started, _, factory = self.call('/api/live/replication/skip-loop/start', dict(body, confirmed=True),
                                            'mysql-8.4-healthy')
            self.assertEqual(factory.options['query_timeout'], 30)
            job = started['job_id']
            listing, _, _ = self.call('/api/live/replication/skip-loop/status', {})
            self.assertEqual([loop['job_id'] for loop in listing['loops']], [job])
            other = dict(self.connection, port=3307)
            with self.assertRaisesRegex(ValueError, 'another connection'):
                ui.api_response('/api/live/replication/skip-loop/stop', {'connection': other, 'job_id': job},
                                self.workspace)
            stopped, _, _ = self.call('/api/live/replication/skip-loop/stop', {'job_id': job})
            self.assertIn(stopped['state'], ('stopping', 'stopped'))
            self.workspace.replication_loops.stop_all()
            for key, loop in self.workspace.replication_loops._jobs:
                loop.join(5)
        final, _, _ = self.call('/api/live/replication/skip-loop/status', {'job_id': job})
        self.assertEqual(final['stop_reason'], 'user_stop')
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()],
                         [('refused', 'unsent'), ('refused', 'read_only'), ('refused', 'needs_confirmation')])


class TestReadOnlyText(unittest.TestCase):

    def test_plural(self):
        self.assertIn('these 2 statements were not sent',
                      dbops.decide(dbops.classify('SET GLOBAL a = 1; START REPLICA'), 'production', True)['text'])
        self.assertIn('this statement was not sent', dbops.decide(dbops.classify('KILL 5'), 'production', True)['text'])


class TestReviewGateRegressions(ReplicationCase):
    """Safety boundaries use real plans/audits and synthetic status transitions.

    Database reads and the clock are injected; no test connects to a server or
    waits for replication. The grants parser and permission decision stay real.
    """

    def setUp(self):
        from myflames import privileges
        self.privileges = privileges
        self.grants = mock.patch.object(privileges, 'read_privileges',
                                        return_value=privileges.parse_grants([], checked=False))
        self.read_grants = self.grants.start()
        self.addCleanup(self.grants.stop)

    @staticmethod
    def copy(value):
        return json.loads(json.dumps(value))

    @staticmethod
    def connection():
        conn = mock.Mock(spec=NoConn)
        conn.run.return_value = ''
        return conn

    def loop(self, conn, clock, **kwargs):
        options = {'clock': clock.monotonic, 'sleep': clock.sleep, 'settle_s': 0,
                   'poll_s': 1, 'max_skips': 1}
        options.update(kwargs)
        return rp.SkipLoop(conn, '', [1062], **options)

    def test_counter_requires_complete_relay_evidence_on_both_engines(self):
        for fixture_name, name in (('mysql-8.4-filepos-error', ''), ('mariadb-11.4-error', 'src1')):
            report, _ = status(fixture_name)
            confirmed = rp.plan_skip(report, name)['identity']
            for incomplete in (None, {}, dict(channel(report, name)['relay'], complete=False)):
                with self.subTest(engine=report['engine'], relay=incomplete):
                    current = self.copy(report)
                    channel(current, name)['relay'] = incomplete
                    plan = rp.plan_skip(current, name)
                    self.assertFalse(plan['available'], 'An unproved counter target must never be planned.')
                    conn = self.connection()
                    with mock.patch.object(rp, 'read_status', return_value=current):
                        result = rp.skip_one(conn, name, confirmed, confirmed=True, settle_s=0)
                    self.assertEqual(result['status'], 'refused')
                    conn.run.assert_not_called()

    def test_expect_requires_every_identity_source_and_position_key(self):
        expect = rp.plan_skip(fake_report(1062), '')['identity']
        self.assertEqual(rp.check_expect(expect), expect)
        cases = [(None, key) for key in expect]
        cases += [('source', key) for key in ('host', 'port', 'uuid')]
        cases += [('position', key) for key in ('relay_log_file', 'relay_log_pos', 'exec_file', 'exec_pos')]
        for group, key in cases:
            with self.subTest(group=group, missing=key):
                truncated = self.copy(expect)
                del (truncated[group] if group else truncated)[key]
                with self.assertRaises(ValueError):
                    rp.check_expect(truncated)

    def test_expect_requires_a_valid_gtid_or_a_complete_file_position_target(self):
        expect = rp.plan_skip(fake_report(1062), '')['identity']
        for gtid in (None, '', 'not-a-gtid', U + ':0', U + ':1-4'):
            with self.subTest(gtid=gtid):
                malformed = self.copy(expect)
                malformed['gtid'] = gtid
                malformed['position'] = dict.fromkeys(expect['position'])
                malformed['position']['exec_pos'] = 4
                with self.assertRaises(ValueError):
                    rp.check_expect(malformed)
        report, _ = status('mysql-8.4-filepos-error')
        file_target = rp.plan_skip(report, '')['identity']
        self.assertEqual(rp.check_expect(file_target), file_target)
        for key in ('relay_log_file', 'relay_log_pos', 'exec_file', 'exec_pos'):
            with self.subTest(null_position=key):
                malformed = self.copy(file_target)
                malformed['position'][key] = None
                with self.assertRaises(ValueError):
                    rp.check_expect(malformed)
        maria, _ = status('mariadb-11.4-error')
        maria_target = rp.plan_skip(maria, 'src1')['identity']
        self.assertEqual(rp.check_expect(maria_target), maria_target)

    def test_explicit_null_identity_values_are_compared(self):
        report = fake_report(1062)
        expect = rp.plan_skip(report, '')['identity']
        for field in ('timestamp', 'source_uuid'):
            with self.subTest(field=field):
                current = self.copy(report)
                if field == 'timestamp':
                    channel(current)['sql']['error']['timestamp'] = '261015 10:00:00'
                else:
                    channel(current)['source']['uuid'] = U
                conn = self.connection()
                with mock.patch.object(rp, 'read_status', return_value=current):
                    result = rp.skip_one(conn, '', expect, confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'refused')
                self.assertIn('changed', result['message'])
                conn.run.assert_not_called()

    def test_unreadable_after_state_is_unverified_in_result_and_audit(self):
        report = fake_report(1062)
        expect = rp.plan_skip(report, '')['identity']
        unavailable = dict(fake_report(0, running='Yes'), available=False, message='Status denied.')
        missing = dict(fake_report(0, running='Yes'), channels=[])
        for after in (ConnectorError('ERROR 2013 (HY000): Lost connection'), unavailable, missing):
            with self.subTest(after=type(after).__name__, available=after.get('available')
                              if isinstance(after, dict) else None):
                self.fresh_store()
                conn = self.connection()

                def read(_conn):
                    if not conn.run.called:
                        return report
                    if isinstance(after, Exception):
                        raise after
                    return after

                with mock.patch.object(rp, 'read_status', side_effect=read):
                    result = rp.skip_one(conn, '', expect, audit=self.audit(), confirmed=True, settle_s=0)
                self.assertEqual(conn.run.call_count, 1)
                self.assertEqual(result['status'], 'unverified')
                self.assertIsNone((result.get('after') or {}).get('passed'))
                self.assertEqual([(r['status'], r['outcome']) for r in self.records()],
                                 [('unknown', 'unverified')])
                self.assertNotIn('The skipped transaction is missing on this replica now', result['message'])

    def test_grants_preflight_requires_whole_script_privileges_before_any_statement(self):
        cases = (('mysql-8.4-gtid-error', '', 'SESSION_VARIABLES_ADMIN'),
                 ('mysql-8.4-filepos-error', '', 'SYSTEM_VARIABLES_ADMIN'),
                 ('mariadb-11.4-error', 'src1', 'REPLICATION SLAVE ADMIN'))
        for fixture_name, name, grant in cases:
            with self.subTest(fixture=fixture_name):
                report, _ = status(fixture_name)
                plan = rp.plan_skip(report, name)
                self.read_grants.return_value = self.privileges.parse_grants(
                    ['GRANT %s ON *.* TO `operator`@`%%`' % grant], engine=report['engine'],
                    version=report['server_version'])
                action = 'skip_gtid' if plan['method'] == 'empty_gtid_transaction' else 'skip_counter'
                self.assertIs(self.privileges.can(self.read_grants.return_value, action)['allowed'], False)
                conn = self.connection()
                with mock.patch.object(rp, 'read_status', return_value=report):
                    result = rp.skip_one(conn, name, plan['identity'], confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'denied')
                self.assertTrue(self.read_grants.called)
                conn.run.assert_not_called()

    def test_mariadb_super_only_is_unknown_and_defers_to_the_server(self):
        """The existing grant model cannot resolve legacy SUPER's split grants."""
        report, _ = status('mariadb-11.4-error')
        plan = rp.plan_skip(report, 'src1')
        self.read_grants.return_value = self.privileges.parse_grants(
            ['GRANT SUPER ON *.* TO `operator`@`%`'], engine='mariadb', version=report['server_version'])
        permission = self.privileges.can(self.read_grants.return_value, 'skip_counter')
        self.assertEqual(permission['state'], 'unknown')
        self.assertIsNone(permission['allowed'])
        conn = self.connection()
        conn.run.side_effect = ConnectorError(
            'ERROR 1227 (42000) at line 3: Access denied; you need the REPLICATION SLAVE ADMIN privilege')
        with mock.patch.object(rp, 'read_status', return_value=report):
            result = rp.skip_one(conn, 'src1', plan['identity'], confirmed=True, settle_s=0)
        self.assertEqual(conn.run.call_count, 1, 'Unknown privileges must defer the decision to the server.')
        self.assertIsNone(result['privilege_check']['allowed'])
        self.assertEqual(result['status'], 'denied')
        self.assertIn('REPLICATION SLAVE ADMIN', result['message'])

    def test_tagged_gtid_requires_the_tag_privilege_before_any_statement(self):
        report = fake_report(1062, gtid=U + ':orders:4')
        plan = rp.plan_skip(report, '')
        self.read_grants.return_value = self.privileges.parse_grants([
            'GRANT SESSION_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO `operator`@`%`'])
        conn = self.connection()
        with mock.patch.object(rp, 'read_status', return_value=report):
            result = rp.skip_one(conn, '', plan['identity'], confirmed=True, settle_s=0)
        self.assertEqual(result['status'], 'denied')
        self.assertIn('TRANSACTION_GTID_TAG', result['message'])
        conn.run.assert_not_called()

    def test_loop_rechecks_read_only_and_connection_type_before_sending(self):
        initial = dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), 'development', False)
        for ctype, read_only in (('development', True), ('production', False)):
            with self.subTest(type=ctype, read_only=read_only):
                conn, clock = self.connection(), FakeTime()
                current = [initial]
                changed = dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), ctype, read_only)

                def read(_conn):
                    current[0] = changed
                    return fake_report(1062)

                loop = self.loop(conn, clock, decision=initial, decision_reader=lambda: current[0])
                with mock.patch.object(rp, 'read_status', side_effect=read):
                    final = loop.run()
                self.assertEqual((final['stop_reason'], final['skips_done']), ('policy_changed', 0))
                conn.run.assert_not_called()

    def test_stop_or_deadline_during_audit_attempt_finishes_record_as_unsent(self):
        for boundary in ('stop', 'deadline'):
            with self.subTest(boundary=boundary):
                self.fresh_store()
                conn, clock = self.connection(), FakeTime()
                audit = self.audit(rp.OPERATION_LOOP)
                loop = self.loop(conn, clock, audit_factory=lambda: audit, max_duration_s=1)
                original_attempt = audit.attempt

                def attempt(*args, **kwargs):
                    audit_id = original_attempt(*args, **kwargs)
                    if boundary == 'stop':
                        loop.stop()
                    else:
                        clock.now += 1
                    return audit_id

                with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)), \
                        mock.patch.object(audit, 'attempt', side_effect=attempt):
                    final = loop.run()
                self.assertEqual(final['stop_reason'], 'user_stop' if boundary == 'stop' else 'duration')
                self.assertEqual(final['skips_done'], 0)
                conn.run.assert_not_called()
                self.assertTrue(audit.finished)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assertNotEqual(records[0]['status'], 'pending')
                self.assertEqual(records[0]['outcome'], 'unsent')

    def test_stop_or_deadline_during_audit_factory_never_sends(self):
        for boundary in ('stop', 'deadline'):
            with self.subTest(boundary=boundary):
                self.fresh_store()
                conn, clock = self.connection(), FakeTime()
                audit = self.audit(rp.OPERATION_LOOP)
                holder = {}

                def factory():
                    if boundary == 'stop':
                        holder['loop'].stop()
                    else:
                        clock.now += 1
                    return audit

                loop = self.loop(conn, clock, audit_factory=factory, max_duration_s=1)
                holder['loop'] = loop
                with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)):
                    final = loop.run()
                self.assertEqual(final['stop_reason'], 'user_stop' if boundary == 'stop' else 'duration')
                self.assertEqual(final['skips_done'], 0)
                conn.run.assert_not_called()
                self.assertTrue(audit.finished)
                records = self.records()
                self.assertEqual(len(records), 1)
                self.assertNotEqual(records[0]['status'], 'pending')
                self.assertEqual(records[0]['outcome'], 'unsent')

    def test_failed_progress_takes_priority_over_the_one_skip_cap(self):
        conn, clock = self.connection(), FakeTime()
        loop = self.loop(conn, clock)
        with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)):
            final = loop.run()
        self.assertEqual(conn.run.call_count, 1)
        self.assertEqual((final['stop_reason'], final['skips_done']), ('no_progress', 1))
        self.assertTrue(final['needs_attention'])
        self.assertIs(final['skips'][0]['passed'], False)

    def test_connector_error_during_loop_write_is_connection_lost(self):
        conn, clock = self.connection(), FakeTime()
        conn.run.side_effect = ConnectorError('ERROR 2013 (HY000): Lost connection to server during query')
        loop = self.loop(conn, clock, audit_factory=lambda: self.audit(rp.OPERATION_LOOP))
        with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)):
            final = loop.run()
        self.assertEqual(conn.run.call_count, 1)
        self.assertEqual((final['stop_reason'], final['skips_done']), ('connection_lost', 0))
        self.assertEqual(self.records()[0]['status'], 'unknown')

    def test_supplied_report_never_bypasses_a_fresh_identity_read(self):
        confirmed = fake_report(1062)
        plan = rp.plan_skip(confirmed, '')
        for changed in (fake_report(1062, gtid=U + ':2'), fake_report(1062, source='other'), fake_report(1032)):
            with self.subTest(identity=rp.plan_skip(changed, '')['identity']):
                conn = self.connection()
                with mock.patch.object(rp, 'read_status', return_value=changed) as read:
                    result = rp.skip_one(conn, '', plan['identity'], report=confirmed, confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'refused')
                read.assert_called_once_with(conn)
                conn.run.assert_not_called()

    def test_loop_checks_source_and_selected_error_on_its_final_fresh_read(self):
        initial = fake_report(1062)
        for changed, reason_key in ((fake_report(1062, source='other'), 'config_changed'),
                                    (fake_report(1032), 'unselected_error')):
            with self.subTest(reason=reason_key):
                conn, clock = self.connection(), FakeTime()
                loop = self.loop(conn, clock)
                with mock.patch.object(rp, 'read_status', side_effect=[initial, changed]):
                    final = loop.run()
                self.assertEqual((final['stop_reason'], final['skips_done']), (reason_key, 0))
                conn.run.assert_not_called()

    def test_loop_fresh_guard_skips_root_failure_from_recorded_3030(self):
        # MySQL's preserve-commit-order workers can report 3030 above 1062.
        # Keep skip_one real: the earlier LoopHarness test replaces this guard.
        report, _ = status('mysql-8.4-mta-3030-error')
        after = self.copy(report)
        channel(after)['position']['executed_gtid_set'] = TestWorkerStoppedBehindAFailure.U3 + ':1-15'
        conn, clock = self.connection(), FakeTime()
        loop = self.loop(conn, clock)
        with mock.patch.object(rp, 'read_status', side_effect=[report, report, report, after]), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=None):
            final = loop.run()
        self.assertEqual((final['stop_reason'], final['skips_done']), ('max_skips', 1), final)
        self.assertEqual(final['skips'][0]['errno'], 1062)
        self.assertEqual(final['skips'][0]['gtid'], TestWorkerStoppedBehindAFailure.U3 + ':15')
        sent = '\n'.join(call[0][0] for call in conn.run.call_args_list)
        self.assertIn("SET GTID_NEXT = '%s:15'" % TestWorkerStoppedBehindAFailure.U3, sent)
        self.assertNotIn(':16', sent)

    def test_loop_fresh_guard_stops_when_only_3030_remains(self):
        report, _ = status('mysql-8.4-mta-3030-error')
        fresh = self.copy(report)
        channel(fresh)['worker']['error'] = {'errno': 3030, 'message': 'Earlier worker stopped'}
        self.assertIsNone(rp.effective_error(channel(fresh)))
        conn, clock = self.connection(), FakeTime()
        loop = self.loop(conn, clock)
        with mock.patch.object(rp, 'read_status', side_effect=[report, fresh]), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=None):
            final = loop.run()
        self.assertEqual((final['stop_reason'], final['skips_done']), ('refused', 0), final)
        self.assertIn('cannot tell which transaction', final['error'])
        conn.run.assert_not_called()

    def test_policy_change_during_audit_attempt_is_checked_at_the_send_boundary(self):
        initial = dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), 'development', False)
        for ctype, read_only in (('development', True), ('production', False)):
            with self.subTest(type=ctype, read_only=read_only):
                self.fresh_store()
                conn, clock = self.connection(), FakeTime()
                current = [initial]
                audit = self.audit(rp.OPERATION_LOOP)
                loop = self.loop(conn, clock, decision=initial, decision_reader=lambda: current[0],
                                 audit_factory=lambda: audit)
                original_attempt = audit.attempt

                def attempt(*args, **kwargs):
                    audit_id = original_attempt(*args, **kwargs)
                    current[0] = dbops.decide(dbops.classify('START REPLICA SQL_THREAD'), ctype, read_only)
                    return audit_id

                with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)), \
                        mock.patch.object(audit, 'attempt', side_effect=attempt):
                    final = loop.run()
                self.assertEqual((final['stop_reason'], final['skips_done']), ('policy_changed', 0))
                conn.run.assert_not_called()
                self.assertTrue(audit.finished)
                self.assertEqual([(r['status'], r['outcome']) for r in self.records()],
                                 [('refused', 'unsent')])


class TestReviewGateSurfaces(ReplicationCase):
    """Write surfaces retain refusals and consume the core's verified result."""

    def setUp(self):
        from myflames import privileges
        grants = privileges.parse_grants([
            'GRANT SESSION_VARIABLES_ADMIN, REPLICATION_SLAVE_ADMIN ON *.* TO `operator`@`%`'],
            engine='mysql', version='8.4.11')
        patch = mock.patch.object(privileges, 'read_privileges', return_value=grants)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def connection():
        conn = mock.MagicMock(spec=NoConn)
        conn.__enter__.return_value = conn
        conn.run.return_value = ''
        return conn

    def test_ui_and_cli_preflight_failures_are_audited_without_sending(self):
        original = status('mysql-8.4-gtid-error')[0]
        plan = channel(original)['skip_plan']
        for surface in ('ui', 'cli'):
            for operation in ('skip', 'skip-loop'):
                for failure in ('denied', 'missing', 'group', 'connection'):
                    with self.subTest(surface=surface, operation=operation, failure=failure), private_home():
                        self.store = WorkspaceStore()
                        workspace = types.SimpleNamespace(store=self.store)
                        conn = self.connection()
                        report = json.loads(json.dumps(original))
                        if failure == 'denied':
                            report.update(available=False, message='Status privilege denied.')
                        elif failure == 'missing':
                            report['channels'] = []
                        elif failure == 'group':
                            channel(report)['kind'] = 'group_replication'
                        connection = {'host': 'db', 'port': 3306, 'user': 'u', 'password': 'hunter2-not-real'}
                        body = {'connection': connection, 'channel': '', 'confirmed': True,
                                'expect': plan['identity'], 'statements': plan['statements'],
                                'codes': [1062], 'max_skips': 2}
                        factory = mock.patch('myflames.connector.MySQLConnection', return_value=conn)
                        if failure == 'connection':
                            factory = mock.patch('myflames.connector.MySQLConnection', side_effect=ConnectorError(
                                'ERROR 2003 (HY000): connection failed hunter2-not-real'))
                        with mock.patch.object(rp, 'read_status', return_value=report):
                            if surface == 'ui':
                                if operation == 'skip':
                                    body['review_token'] = ui._review_tokens(workspace).issue('replication_skip',
                                        ui._review_identity(connection, workspace),
                                        {k: body.get(k) for k in ('channel', 'expect', 'statements')})
                                path = '/api/live/replication/' + ('skip' if operation == 'skip'
                                                                  else 'skip-loop/start')
                                with factory:
                                    if operation == 'skip' and failure != 'connection':
                                        result = ui._replication_response(path, body, workspace)
                                        self.assertEqual(result['status'], 'refused')
                                    else:
                                        error = ConnectorError if failure == 'connection' else ValueError
                                        with self.assertRaises(error):
                                            ui._replication_response(path, body, workspace)
                            else:
                                argv = [operation, '--channel', '', '--yes', '-p', 'hunter2-not-real']
                                if operation == 'skip-loop':
                                    argv += ['--errors', '1062']
                                code, out, err, _ = run_cli('replication', argv, conn, patches=[factory])
                                self.assertEqual(code, 2 if operation == 'skip' and failure == 'group' else 2,
                                                 out + err)
                                self.assertNotIn('hunter2-not-real', out + err)
                        conn.run.assert_not_called()
                        records = self.records()
                        self.assertEqual(len(records), 1)
                        self.assertEqual(records[0]['status'], 'refused')
                        self.assertEqual(records[0]['target']['channel'], '')
                        self.assertTrue(records[0]['finished_at'])
                        self.assertNotIn('hunter2-not-real', json.dumps(records))

    def test_ui_validation_and_profile_failures_are_audited_before_connecting(self):
        plan = channel(status('mysql-8.4-gtid-error')[0])['skip_plan']
        cases = (('skip', {'channel': 1}), ('skip', {'expect': {}}),
                 ('skip', {'statements': [42]}), ('skip-loop/start', {'channel': []}),
                 ('skip-loop/start', {'codes': []}), ('skip-loop/start', {'max_skips': 0}),
                 ('skip-loop/start', {'max_minutes': True}), ('skip-loop/start', {'poll_s': 0}),
                 ('skip', {'profile_id': 'missing-profile'}),
                 ('skip-loop/start', {'profile_id': 'missing-profile'}),
                 ('skip', {'profile_id': 42}), ('skip-loop/start', {'profile_id': 42}))
        for path, invalid in cases:
            with self.subTest(path=path, invalid=invalid), private_home():
                self.store = WorkspaceStore()
                workspace = types.SimpleNamespace(store=self.store)
                body = {'connection': {'host': 'db', 'port': 3306, 'user': 'u',
                                       'password': 'hunter2-not-real'},
                        'channel': '', 'expect': plan['identity'], 'statements': plan['statements'],
                        'codes': [1062], 'max_skips': 2, 'confirmed': True}
                if 'profile_id' in invalid:
                    body['connection'].update(invalid)
                else:
                    body.update(invalid)
                with mock.patch('myflames.connector.MySQLConnection') as connect:
                    with self.assertRaises(ValueError):
                        ui._replication_response('/api/live/replication/' + path, body, workspace)
                connect.assert_not_called()
                records = self.records()
                self.assertEqual([(r['status'], r['outcome']) for r in records], [('refused', 'unsent')])
                self.assertTrue(records[0]['finished_at'])
                self.assertNotIn('hunter2-not-real', json.dumps(records))

    def test_duplicate_loop_start_is_an_audited_unsent_refusal(self):
        self.store = WorkspaceStore()
        registry = rp.SkipLoopRegistry()
        workspace = types.SimpleNamespace(store=self.store, replication_loops=registry)
        connection = {'host': 'db', 'port': 3306, 'user': 'u'}
        safety = dbops.connection_safety(None, connection)
        identity = dbops.connection_identity(connection, safety)
        existing = rp.SkipLoop(NoConn(), '', [1062], 2)
        # Register an active job without starting a thread or waiting.
        with mock.patch.object(existing, 'start', return_value=existing):
            registry.start(identity, existing)
        conn = self.connection()
        with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                mock.patch.object(rp, 'read_status', return_value=fake_report(0, running='Yes')):
            with self.assertRaisesRegex(ValueError, 'already running'):
                ui._replication_response('/api/live/replication/skip-loop/start',
                                         {'connection': connection, 'channel': '', 'codes': [1062],
                                          'max_skips': 2, 'confirmed': True}, workspace)
        conn.run.assert_not_called()
        conn.close.assert_called_once_with()
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'unsent')])
        self.assertEqual(len(registry.status(identity)['loops']), 1)

    def test_surface_callbacks_reload_saved_connection_policy_by_id(self):
        from myflames import workspace_cli
        for ctype, read_only in (('development', True), ('production', False)):
            with self.subTest(connection_type=ctype, read_only=read_only), private_home():
                store = WorkspaceStore()
                profile = store.save('profile', {'name': 'replica', 'host': 'db', 'port': 3306, 'user': 'u',
                                                  'type': 'development'})
                connection = {'profile_id': profile['id'], 'host': 'db', 'port': 3306, 'user': 'u'}
                safety = dbops.connection_safety(profile, connection)
                decision = dbops.decide('risky', safety['type'], safety['read_only'])
                _, ui_state = ui._replication_audit(types.SimpleNamespace(store=store), rp.OPERATION_LOOP,
                                                   connection, safety, decision)
                args = types.SimpleNamespace(profile=profile['id'], password=None, host=None, port=None,
                                             user=None, database=None, ssl_mode=None, ssl_ca=None)
                _, _, cli_state = workspace_cli._replication_policy(args, rp.OPERATION_LOOP)
                for state in (ui_state, cli_state):
                    self.assertEqual(state['decision_reader']()['connection_type'], 'development')
                    self.assertIs(state['decision_reader']()['read_only'], False)
                store.save('profile', dict(profile, name='renamed', type=ctype, read_only=read_only),
                           key=profile['id'])
                for state in (ui_state, cli_state):
                    current = state['decision_reader']()
                    self.assertEqual(current['connection_type'], ctype)
                    self.assertIs(current['read_only'], read_only)

    def test_loop_reloads_policy_between_skips_and_on_profile_deletion(self):
        for change in ('read_only', 'type', 'delete', 'endpoint'):
            with self.subTest(change=change), private_home():
                store = WorkspaceStore()
                workspace = types.SimpleNamespace(store=store)
                profile = store.save('profile', {'name': 'replica', 'host': 'db', 'port': 3306, 'user': 'u',
                                                  'type': 'production'})
                connection = {'profile_id': profile['id'], 'host': 'db', 'port': 3306, 'user': 'u'}
                safety = dbops.connection_safety(profile, connection)
                decision = dbops.decide('risky', safety['type'], False)
                factory, state = ui._replication_audit(workspace, rp.OPERATION_LOOP, connection, safety, decision)
                clock = FakeTime()
                loop = rp.SkipLoop(NoConn(), '', [1062], 2, decision=decision, poll_s=1, settle_s=0,
                                   clock=clock.monotonic, sleep=clock.sleep, audit_factory=factory,
                                   decision_reader=state['decision_reader'],
                                   receiver_reader=lambda conn, report: ((1, 'stage', 1),))
                def skipped(*args, **kwargs):
                    if change == 'delete':
                        store.delete('profile', profile['id'])
                    else:
                        updates = {'read_only': True} if change == 'read_only' else \
                                  {'type': 'development'} if change == 'type' else {'host': 'moved'}
                        store.save('profile', dict(profile, **updates), profile['id'])
                    return {'status': 'skipped', 'after': {'passed': True}}
                with mock.patch.object(rp, 'read_status', return_value=fake_report(1062, gtid=U + ':5')), \
                        mock.patch.object(rp, 'skip_one', side_effect=skipped) as skip, \
                        mock.patch.object(rp, 'read_receiver_identity', return_value=((1, 'stage', 1),)):
                    final = loop.run()
                self.assertEqual((final['stop_reason'], final['skips_done']), ('policy_changed', 1))
                self.assertEqual(skip.call_count, 1)

    def test_cli_requires_verified_progress_and_maps_loop_disconnect_to_exit_2(self):
        report = fixture('replication', 'mysql-8.4-gtid-error')['report']
        report['global_skip_counter'] = 0
        for after in (None, {}, {'passed': None}, {'passed': False}, {'passed': True}):
            with self.subTest(after=after), private_home():
                result = {'status': 'skipped', 'channel': '', 'after': after, 'message': 'Checked.',
                          'statements': [], 'sent': True}
                with mock.patch.object(rp, 'read_status', return_value=report), \
                        mock.patch.object(rp, 'skip_one', return_value=result) as skip:
                    code, out, err, _ = run_cli('replication', ['skip', '--channel', '', '--yes', '--json'],
                                               NoConn())
                self.assertEqual(code, 0 if after and after.get('passed') is True else 1, out + err)
                self.assertTrue(callable(skip.call_args[1]['decision_reader']))
        self.store = WorkspaceStore()
        conn = self.connection()
        conn.run.side_effect = ConnectorError('ERROR 2013 (HY000): Lost connection to server during query')
        with mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp, 'read_receiver_identity', return_value=('receiver', 1)):
            code, out, err, _ = run_cli('replication', ['skip-loop', '--channel', '', '--errors', '1062',
                                                       '--yes', '--json'], conn)
        self.assertEqual(code, 2, out + err)
        self.assertEqual(json.loads(out)['stop_reason'], 'connection_lost')
        self.assertEqual(conn.run.call_count, 1)
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('unknown', 'timeout')])


class TestReceiverGuardFallback(ReplicationCase):

    def test_start_snapshot_has_real_receiver_coverage_before_worker_runs(self):
        for fixture_name, instrument in (('mysql-8.4-gtid-error', 'stage/sql/Checking source version'),
                                          ('mariadb-11.4-error', 'stage/sql/Checking master version')):
            for enabled in (True, False):
                for problem in (None, ConnectorError('ERROR 1142 (42000): SELECT denied')):
                    if enabled and problem:
                        continue
                    with self.subTest(engine=fixture_name, enabled=enabled, problem=str(problem)):
                        report, _ = status(fixture_name)
                        report['capabilities']['performance_schema'] = True
                        conn = mock.Mock(spec=NoConn)
                        conn.run.return_value = self.stage_row(instrument, enabled='YES' if enabled else 'NO')
                        if problem:
                            conn.run.side_effect = problem
                        loop = rp.SkipLoop(conn, report['channels'][0]['name'], [1062], 1,
                                           receiver_reader=REAL_RECEIVER_READER)

                        def assert_ready():
                            snapshot = loop.status()
                            self.assertEqual(snapshot['receiver_guard'], 'on' if enabled else 'off')
                            self.assertEqual(snapshot['receiver_guard_instrument'], instrument)
                            self.assertEqual(snapshot['receiver_guard_reason'] is None, enabled)
                            self.assertNotIn('not been checked', snapshot['receiver_guard_note'])
                            self.assertNotIn('not been checked', snapshot['receiver_guard_reason'] or '')

                        # Hold the worker entirely: readiness cannot depend on
                        # the scheduler completing its first poll after Start.
                        with mock.patch.object(rp, 'read_status', return_value=report), \
                                mock.patch.object(rp.threading, 'Thread') as thread:
                            thread.return_value.start.side_effect = assert_ready
                            self.assertIs(loop.start(), loop)
                            assert_ready()
                            thread.return_value.start.assert_called_once_with()
                        self.assertEqual(conn.run.call_count, 1)
                        self.assertIn('events_stages_summary_by_thread_by_event_name', conn.run.call_args[0][0])

    def test_start_connection_failure_has_a_real_reason_and_closes_without_worker(self):
        conn = mock.Mock(spec=NoConn)
        loop = rp.SkipLoop(conn, '', [1062], 1, close_conn=True)
        with mock.patch.object(rp, 'read_status', side_effect=ConnectorError('Connection timed out')), \
                mock.patch.object(rp.threading, 'Thread') as thread:
            loop.start()
        snapshot = loop.status()
        self.assertEqual(snapshot['stop_reason'], 'connection_lost')
        self.assertEqual(snapshot['receiver_guard'], 'off')
        self.assertEqual(snapshot['receiver_guard_reason'], 'Connection timed out')
        self.assertEqual(snapshot['receiver_guard_note'], 'Connection timed out')
        thread.assert_not_called()
        conn.close.assert_called_once_with()

    def run_with_stage_rows(self, fixture_name, rows, ps_off=False):
        report, _ = status(fixture_name)
        # Stage responses below are synthetic; the MariaDB status recording
        # used the engine's default of Performance Schema off.
        report['capabilities']['performance_schema'] = not ps_off
        name = report['channels'][0]['name']
        conn = mock.Mock(spec=NoConn)
        answers = iter(rows)

        def run(sql, **kwargs):
            if 'events_stages_summary_by_thread_by_event_name' in sql:
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            self.assertTrue(sql.startswith(rp.SKIP_TAG), sql)
            return ''

        conn.run.side_effect = run
        clock = FakeTime()
        loop = rp.SkipLoop(conn, name, [1062], 1, clock=clock.monotonic, sleep=clock.sleep,
                           receiver_reader=REAL_RECEIVER_READER, settle_s=0)
        with mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp, '_read_after', return_value={'present': True, 'passed': True,
                                                                 'skip_counter': 0, 'global_skip_counter': 0}):
            result = loop.run()
        writes = [call for call in conn.run.call_args_list if call[0][0].startswith(rp.SKIP_TAG)]
        return result, writes, conn

    def stage_row(self, instrument, enabled='YES', global_on='YES', thread_on='YES', count=1):
        return '7\tYES\tWaiting for source to send event\t%s\t%d\t%s\t%s\t%s\n' % (
            instrument, count, enabled, global_on, thread_on)

    def test_default_disabled_absent_denied_and_ps_off_allow_skip(self):
        for fixture_name, instrument in (('mysql-8.4-gtid-error', 'stage/sql/Checking source version'),
                                          ('mariadb-11.4-error', 'stage/sql/Checking master version')):
            cases = (('stage off', self.stage_row(instrument, enabled='NO')),
                     ('global off', self.stage_row(instrument, global_on='NO')),
                     ('thread off', self.stage_row(instrument, thread_on='NO')),
                     ('absent', ''),
                     ('denied', ConnectorError('ERROR 1142 (42000): SELECT command denied')),
                     ('missing table', ConnectorError('ERROR 1146 (42S02): Table does not exist')))
            for case, answer in cases:
                with self.subTest(engine=fixture_name, case=case):
                    final, writes, _ = self.run_with_stage_rows(fixture_name, [answer])
                    self.assertEqual((final['stop_reason'], final['skips_done']), ('max_skips', 1))
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(final['receiver_guard'], 'off')
                    self.assertTrue(final['receiver_guard_reason'])
                    self.assertIn(instrument, final['receiver_guard_note'])
            with self.subTest(engine=fixture_name, case='ps off'):
                final, writes, conn = self.run_with_stage_rows(fixture_name, [], ps_off=True)
                self.assertEqual((final['stop_reason'], len(writes)), ('max_skips', 1))
                self.assertEqual(final['receiver_guard_reason'], 'Performance Schema is off.')
                self.assertFalse(any('setup_instruments' in call[0][0] for call in conn.run.call_args_list))

    def test_enabled_guard_is_preserved_at_the_send_boundary(self):
        for fixture_name, instrument in (('mysql-8.4-gtid-error', 'stage/sql/Checking source version'),
                                          ('mariadb-11.4-error', 'stage/sql/Checking master version')):
            row = self.stage_row(instrument)
            for answer, expected in ((row, 'max_skips'),
                                     (self.stage_row(instrument, enabled='NO'), 'receiver_unverified'),
                                     ('', 'receiver_unverified'),
                                     (ConnectorError('ERROR 1142 (42000): SELECT denied'), 'receiver_unverified'),
                                     (self.stage_row(instrument, count=2), 'receiver_reconnected'),
                                     (ConnectorError('ERROR 2013 (HY000): Lost connection'), 'connection_lost')):
                with self.subTest(engine=fixture_name, expected=expected, answer=str(answer)):
                    final, writes, _ = self.run_with_stage_rows(fixture_name, [row, answer, answer])
                    self.assertEqual(final['stop_reason'], expected)
                    self.assertEqual(len(writes), 1 if expected == 'max_skips' else 0)
                    if expected == 'max_skips':
                        self.assertEqual(final['receiver_guard'], 'on')

    def test_unavailable_guard_does_not_hide_connection_failure(self):
        final, writes, _ = self.run_with_stage_rows(
            'mysql-8.4-gtid-error', [ConnectorError('ERROR 2013 (HY000): Lost connection')])
        self.assertEqual(final['stop_reason'], 'connection_lost')
        self.assertEqual(writes, [])

    def test_other_loop_guards_remain_active_without_receiver_telemetry(self):
        cases = (('unselected_error', [fake_report(1032)]),
                 ('no_progress', [fake_report(1062), fake_report(1062)]),
                 ('server_restarted', [fake_report(0, running='Yes'), fake_report(1062, uptime=5)]),
                 ('config_changed', [fake_report(0, running='Yes'), fake_report(1062, source='other')]),
                 ('channel_gone', [fake_report(0, running='Yes'), fake_report(1062, name='other')]))
        for expected, reports in cases:
            with self.subTest(expected=expected), mock.patch.object(rp, 'read_receiver_identity', return_value=None):
                result, _, _ = LoopHarness(reports).run()
                self.assertEqual(result['stop_reason'], expected)
                self.assertEqual(result['receiver_guard'], 'off')


class TestReviewGateConcurrencyAndMonitoring(ReplicationCase):
    """Injected concurrency, partial execution, uptime and receiver evidence."""

    def test_both_mysql_counters_must_be_known_zero_for_every_method(self):
        for fixture_name in ('mysql-8.4-filepos-error', 'mysql-8.4-gtid-error'):
            report, _ = status(fixture_name)
            for key in ('global', 'active'):
                for value in (None, 1):
                    with self.subTest(fixture=fixture_name, counter=key, value=value):
                        changed = json.loads(json.dumps(report))
                        if key == 'global':
                            changed['global_skip_counter'] = value
                        else:
                            channel(changed)['position']['skip_counter'] = value
                        plan = rp.plan_skip(changed, '')
                        self.assertFalse(plan['available'])
                        self.assertIn('counter', plan['message'])

    def test_counter_position_must_include_the_source_filename(self):
        report, _ = status('mysql-8.4-filepos-error')
        channel(report)['position']['exec_file'] = 'binlog.000002'
        plan = rp.plan_skip(report, '')
        self.assertFalse(plan['available'])
        self.assertIn('different source log files', plan['message'])

    def test_increasing_uptime_still_detects_restart(self):
        clock = FakeTime()
        reads = [fake_report(0, running='Yes', uptime=1), fake_report(1062, uptime=30)]
        loop = rp.SkipLoop(NoConn(), '', [1062], 1, clock=clock.monotonic, sleep=clock.sleep,
                           poll_s=60, settle_s=0)
        with mock.patch.object(rp, 'read_status', side_effect=reads), mock.patch.object(rp, 'skip_one') as skip:
            final = loop.run()
        self.assertEqual(final['stop_reason'], 'server_restarted')
        skip.assert_not_called()

    def test_receiver_reconnect_and_missing_telemetry_stop_before_write(self):
        for epochs, expected in (([((7, 'check', 1),), ((7, 'check', 2),)], 'receiver_reconnected'),
                                 ([((7, 'check', 1),), None], 'receiver_unverified')):
            with self.subTest(expected=expected):
                clock = FakeTime()
                reader = mock.Mock(side_effect=epochs)
                loop = rp.SkipLoop(NoConn(), '', [1062], 1, clock=clock.monotonic, sleep=clock.sleep,
                                   receiver_reader=reader, settle_s=0)
                with mock.patch.object(rp, 'read_status', return_value=fake_report(1062)), \
                        mock.patch.object(rp.privileges, 'read_privileges', return_value={'checked': False}):
                    final = loop.run()
                self.assertEqual(final['stop_reason'], expected)
                self.assertEqual(final['skips_done'], 0)

    def test_receiver_reader_requires_enabled_stable_covered_threads(self):
        for event in ('stage/sql/Checking source version', 'stage/sql/Checking master version'):
            row = ['7', 'YES', 'Waiting for source to send event', event, '2', 'YES', 'YES', 'YES']
            conn = mock.Mock(spec=NoConn)
            conn.run.return_value = '\t'.join(row) + '\n'
            self.assertEqual(REAL_RECEIVER_READER(conn, fake_report(1062)), (('7', event, 2),))
            self.assertIn('events_stages_summary_by_thread_by_event_name', conn.run.call_args[0][0])
            for col, value in ((1, 'NO'), (2, 'Connecting to source'), (4, 'NULL'),
                               (5, 'NO'), (6, 'NO'), (7, 'NO')):
                with self.subTest(event=event, column=col):
                    changed = list(row)
                    changed[col] = value
                    conn.run.return_value = '\t'.join(changed) + '\n'
                    with self.assertRaises(ValueError):
                        REAL_RECEIVER_READER(conn, fake_report(1062))
            conn.run.return_value = '\t'.join(row) + '\n'
            report = fake_report(1062)
            channel(report)['io']['running'] = 'No'
            with self.assertRaises(ValueError):
                REAL_RECEIVER_READER(conn, report)
            report = fake_report(1062)
            report['channels'].append(fake_report(1062, name='unmonitored')['channels'][0])
            with self.assertRaises(ValueError):
                REAL_RECEIVER_READER(conn, report)

    def test_partial_start_failure_reports_committed_gtid_and_armed_counter(self):
        for fixture_name, error_line, field in (('mysql-8.4-gtid-error', 5, 'gtid_committed'),
                                                ('mysql-8.4-filepos-error', 2, 'counter_armed'),
                                                ('mariadb-11.4-error', 3, 'counter_armed')):
            with self.subTest(fixture=fixture_name):
                self.fresh_store()
                name = 'src1' if fixture_name.startswith('mariadb') else ''
                report, _ = status(fixture_name)
                plan = rp.plan_skip(report, name)
                after = json.loads(json.dumps(report))
                if field == 'counter_armed':
                    if report['engine'] == 'mysql':
                        after['global_skip_counter'] = 1
                    else:
                        channel(after, name)['position']['skip_counter'] = 1
                conn = mock.Mock(spec=NoConn)
                conn.run.side_effect = ConnectorError('ERROR 1227 (42000) at line %d: START denied' % error_line)
                with mock.patch.object(rp, 'read_status', side_effect=lambda _c: after if conn.run.called else report):
                    result = rp.skip_one(conn, name, plan['identity'], audit=self.audit(), confirmed=True, settle_s=0)
                self.assertEqual(result['status'], 'denied')
                self.assertIs(result['residual_state'][field], True)
                self.assertIs(self.records()[0]['after']['residual_state'][field], True)
                self.assertEqual(conn.run.call_count, 1, 'Do not automatically undo residual state.')
        plan = rp.plan_skip(status('mysql-8.4-filepos-error')[0], '')
        state = rp._residual_state(ConnectorError('ERROR 1227 at line 2: denied'), plan,
                                   {'global_skip_counter': 0, 'skip_counter': 1})
        self.assertTrue(state['counter_armed'], 'A consumed global counter can still be active on the channel.')

    def test_unexpected_post_send_exception_never_audits_unsent(self):
        report = fake_report(1062)
        conn = mock.Mock(spec=NoConn)
        with mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(rp, '_read_after', side_effect=RuntimeError('verification parser failed')):
            with self.assertRaisesRegex(RuntimeError, 'verification parser'):
                rp.skip_one(conn, '', rp.plan_skip(report, '')['identity'], audit=self.audit(), confirmed=True)
        self.assertEqual(conn.run.call_count, 1)
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('unknown', 'unverified')])

    def test_counter_consumption_without_progress_stops_with_attention(self):
        report, _ = status('mysql-8.4-filepos-error')
        clock, conn = FakeTime(), mock.Mock(spec=NoConn)
        loop = rp.SkipLoop(conn, '', [1062], 1, clock=clock.monotonic, sleep=clock.sleep, settle_s=0)
        with mock.patch.object(rp, 'read_status', return_value=report):
            final = loop.run()
        self.assertEqual(final['stop_reason'], 'no_progress')
        self.assertTrue(final['needs_attention'])
        self.assertEqual(conn.run.call_count, 1)

    def test_two_channels_cannot_interleave_counter_scripts(self):
        import threading
        report, _ = status('mysql-8.4-filepos-error')
        first_sent, release, second_at_lock = threading.Event(), threading.Event(), threading.Event()
        real_lock = threading.Lock()
        attempts = [0]

        class ObservedLock(object):
            def __enter__(self):
                attempts[0] += 1
                if attempts[0] == 2:
                    second_at_lock.set()
                real_lock.acquire()

            def __exit__(self, *exc):
                real_lock.release()

        first, second = mock.Mock(spec=NoConn), mock.Mock(spec=NoConn)
        plans, results, errors = [], [], []
        for conn, name in ((first, ''), (second, 'other')):
            conn.report = json.loads(json.dumps(report))
            channel(conn.report)['name'] = name
            plan = rp.plan_skip(conn.report, name)
            plans.append(plan)
            conn.after = json.loads(json.dumps(conn.report))
            current = channel(conn.after, name)
            current['sql']['running'] = 'Yes'
            current['sql']['error']['errno'] = 0
            current['position']['exec_pos'] = plan['skip']['end_log_pos']

        def send_first(*args, **kwargs):
            first_sent.set()
            if not release.wait(1):
                raise AssertionError('test did not release the first send')
            first.report = first.after

        def send_second(*args, **kwargs):
            second.report = second.after

        first.run.side_effect, second.run.side_effect = send_first, send_second

        def skip(conn, plan):
            try:
                results.append(rp.skip_one(conn, plan['channel'], plan['identity'], confirmed=True, settle_s=0))
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=skip, args=(first, plans[0])),
                   threading.Thread(target=skip, args=(second, plans[1]))]
        with mock.patch.object(rp, '_WRITE_LOCK', ObservedLock()), \
                mock.patch.object(rp, 'read_status', side_effect=lambda conn: conn.report):
            try:
                workers[0].start()
                self.assertTrue(first_sent.wait(0.1))
                workers[1].start()
                self.assertTrue(second_at_lock.wait(0.1))
                second.run.assert_not_called()
            finally:
                release.set()
                for worker in workers:
                    if worker.ident is not None:
                        worker.join(1)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual([r['status'] for r in results], ['skipped', 'skipped'])
        self.assertEqual(first.run.call_count, 1)
        self.assertEqual(second.run.call_count, 1)

    def test_lock_is_visible_to_another_cli_process(self):
        import subprocess
        import sys
        probe = '''import os
path = os.path.expanduser('~/.myflames/replication-write.lock')
with open(path, 'a+b') as handle:
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('blocked')
    else:
        print('acquired')
'''
        def run_probe():
            return subprocess.check_output([sys.executable, '-c', probe], universal_newlines=True, timeout=5).strip()
        with rp._serialize_skips():
            self.assertEqual(run_probe(), 'blocked')
        self.assertEqual(run_probe(), 'acquired')

    def test_loop_refusal_before_a_plan_still_has_an_unsent_audit(self):
        report = dict(fake_report(1062), available=False, message='Status denied.')
        clock = FakeTime()
        loop = rp.SkipLoop(NoConn(), '', [1062], 1, clock=clock.monotonic, sleep=clock.sleep,
                           audit_factory=lambda: self.audit(rp.OPERATION_LOOP), settle_s=0)
        with mock.patch.object(rp, 'read_status', return_value=report):
            final = loop.run()
        self.assertEqual(final['stop_reason'], 'refused')
        self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'unsent')])

    def test_grants_are_reloaded_after_blocking_audit_preparation(self):
        from myflames import privileges
        initial = privileges.parse_grants(['GRANT SUPER ON *.* TO operator@localhost'])
        revoked = privileges.parse_grants(['GRANT SESSION_VARIABLES_ADMIN ON *.* TO operator@localhost'])
        current = [initial]
        report, conn = fake_report(1062), mock.Mock(spec=NoConn)
        audit = self.audit()
        original = audit.attempt

        def attempt(*args, **kwargs):
            audit_id = original(*args, **kwargs)
            current[0] = revoked
            return audit_id

        with mock.patch.object(rp, 'read_status', return_value=report), \
                mock.patch.object(audit, 'attempt', side_effect=attempt), \
                mock.patch.object(privileges, 'read_privileges', side_effect=lambda _c: current[0]):
            result = rp.skip_one(conn, '', rp.plan_skip(report, '')['identity'], audit=audit, confirmed=True)
        self.assertEqual(result['status'], 'denied')
        conn.run.assert_not_called()
        self.assertEqual(self.records()[0]['outcome'], 'unsent')

    def test_variable_read_latency_is_not_mistaken_for_a_restart(self):
        clock = FakeTime()
        loop = rp.SkipLoop(NoConn(), '', [1062], 1, clock=clock.monotonic, sleep=clock.sleep)
        first = fake_report(0, running='Yes', uptime=100)

        def slow(_conn):
            clock.now += 10  # Uptime was sampled before slow P_S/relay reads.
            return first

        with mock.patch.object(rp, 'read_status', side_effect=slow):
            report = loop._read_status(NoConn())
        self.assertIsNone(loop._changed(report, channel(report)))
        second = fake_report(0, running='Yes', uptime=110)
        with mock.patch.object(rp, 'read_status', return_value=second):
            report = loop._read_status(NoConn())
        self.assertIsNone(loop._changed(report, channel(report)))

    def test_cli_missing_profile_is_audited_before_connection_setup(self):
        for operation in ('skip', 'skip-loop'):
            with self.subTest(operation=operation), private_home():
                self.store = WorkspaceStore()
                argv = [operation, '--profile', 'missing', '--channel', '', '--yes']
                if operation == 'skip-loop':
                    argv += ['--errors', '1062']
                code, _out, err, factory = run_cli('replication', argv, NoConn())
                self.assertEqual(code, 2, err)
                self.assertEqual([(r['status'], r['outcome']) for r in self.records()], [('refused', 'unsent')])
                self.assertIsNone(factory.options)


if __name__ == '__main__':
    unittest.main()
