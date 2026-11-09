"""Baselines: baselines.py, WorkspaceStore.baseline_* and /api/baselines/*.

Storage runs on a temporary HOME with the vault backend. The server identity
read (baselines.IDENTITY_SQL, three statements in one client run) is answered
by a fake connection with the text the mysql client prints in batch mode:
MySQL keeps @@server_uuid (mysql-server sql/sys_vars.cc Sys_server_uuid);
MariaDB has none (no server_uuid in mariadb-server sql/sys_vars.cc), so it is
named by @@hostname and @@port. Health readings are computed from the
two-sample live captures in fixtures/health (MySQL 5.7, 8.4, 26.7, Percona
8.4, MariaDB 11.4 and 13.0). Query digest readings are the recorded MySQL 5.7
(fixtures/perf) and 8.4 (apps/workspace/test/fixtures) lab reports; no
MariaDB top-1.0 reading is recorded, so the MariaDB one is hand-made in the
shape its digest table gives: MD5 digests of 32 hex characters and FIRST_SEEN
as TIMESTAMP(0) (mariadb-server storage/perfschema/table_esms_by_digest.cc:88;
MySQL has TIMESTAMP(6), mysql-server table_esms_by_digest.cc:87).
"""
import copy
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from myflames import baselines, health, ui, workspace_store
from myflames.connector import ConnectorError, PartialReadError
from myflames.workspace_store import (BASELINE_MAX_BYTES, BASELINE_MAX_PER_SERVER, BaselineLimit,
                                      WorkspaceStore)

HERE = Path(__file__).resolve().parent
UUID = '3e11fa47-71ca-11e1-9e33-c80aa9429562'
OTHER_UUID = '8f0c2b6e-0a43-11ef-8d43-0242ac120002'
WRITE_WORDS = re.compile(r'\b(UPDATE|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|TRUNCATE|SET)\b',
                         re.I)


def health_report(server):
    with open(str(HERE / 'fixtures' / 'health' / (server + '.json')), encoding='utf-8') as handle:
        data = json.load(handle)
    first, second = data['samples']
    return health.compute_health(first, second, data['variables'], 5)


def digest_report(server):
    if server == 'mysql-8.4':
        path = HERE.parent / 'apps' / 'workspace' / 'test' / 'fixtures' / 'top-mysql-8.4-lab.json'
        with open(str(path), encoding='utf-8') as handle:
            report = json.load(handle)
    elif server == 'mysql-5.7':
        with open(str(HERE / 'fixtures' / 'perf' / 'top-mysql-5.7.json'), encoding='utf-8') as handle:
            report = json.load(handle)['report']
    else:
        report = {
            'schema_version': 'top-1.0', 'engine': 'mariadb', 'server_version': '11.4.13-MariaDB-ubu2404',
            'server_product': 'MariaDB', 'order': 'total', 'limit': 50, 'notes': [], 'statements': [
                {'digest': '1c7f6b1f3f3a1e6b8a1c2d3e4f5a6b7c', 'digest_text': 'SELECT * FROM `orders` WHERE `id` = ? ',
                 'schema': 'shop', 'count': 1200, 'total_ms': 960.5, 'avg_ms': 0.8, 'max_ms': 12.0,
                 'rows_examined': 1200, 'rows_sent': 1200, 'sample_text': None, 'first_seen': '2026-10-14 16:00:01',
                 'last_seen': '2026-10-14 16:26:30'},
                {'digest': '9a8b7c6d5e4f30211203f4e5d6c7b8a9', 'digest_text': 'UPDATE `stock` SET `qty` = `qty` - ? ',
                 'schema': 'shop', 'count': 40, 'total_ms': 88.0, 'avg_ms': 2.2, 'max_ms': 9.1,
                 'rows_examined': 40, 'rows_sent': 0, 'sample_text': None, 'first_seen': '2026-10-14 16:10:00',
                 'last_seen': '2026-10-14 16:26:00'}],
            'coverage': {'uptime_s': 1600, 'first_seen': '2026-10-14 16:00:01', 'digest_lost': 0,
                         'counting_since': None, 'overflow': None},
            'totals': {'digests': 2, 'total_ms': 1048.5, 'executions': 1240}}
    report = copy.deepcopy(report)
    report['read_at'] = '2026-10-31T12:00:00.000Z'
    return report


def identity_values(report, uptime=None, server_uuid=UUID, hostname='db1', port='3306', now='2026-11-08 14:02:31.123456'):
    """The server the reading came from, as the identity read prints it."""
    mariadb = report['engine'] == 'mariadb'
    values = [('hostname', hostname), ('port', port), ('version', report['server_version']),
              ('version_comment', 'mariadb.org binary distribution' if mariadb else 'MySQL Community Server - GPL')]
    if server_uuid and not mariadb:
        values.insert(2, ('server_uuid', server_uuid))
    if uptime is None:
        uptime = report.get('uptime_s') if report['schema_version'] == 'health-1.0' else report['coverage']['uptime_s']
        uptime = int(uptime or 0) + 60
    values.append(('Uptime', str(uptime)))
    values.append(('now', now))
    return values


def with_reading(report, values):
    """The reading as /api/live/health and /api/live/top answer it with
    with_identity: true: reading_server, the identity read with its data
    (from the identity read's *values*). The key it carries is not trusted:
    the server computes its own from the server's fields."""
    v = dict((name.lower(), value) for name, value in values)
    mariadb = report['engine'] == 'mariadb'
    server = {'engine': report['engine'], 'flavor': report['engine'], 'product': 'MariaDB' if mariadb else 'MySQL',
              'version': v['version'], 'server_uuid': v.get('server_uuid'), 'hostname': v['hostname'],
              'port': int(v['port'])}
    out = dict(report)
    out['reading_server'] = {'verified': True, 'server_key': 'not-trusted', 'server': server, 'uptime_s': int(v['uptime']),
                             'server_time': v['now']}
    return out


class FakeConnection:
    """MySQLConnection's run() with the identity read's answer."""

    calls = []

    def __init__(self, text, error=None, **options):
        self.text, self.error, self.options = text, error, options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, sql, extra_flags=None, timeout=None, tail_timeout=None):
        FakeConnection.calls.append((sql, list(extra_flags or []), self.options))
        if self.error:
            raise self.error
        return self.text


def answer(values, error=None):
    text = ''.join('%s\t%s\n' % pair for pair in values)
    FakeConnection.calls = []
    return mock.patch.object(baselines, 'MySQLConnection',
                             side_effect=lambda **options: FakeConnection(text, error, **options))


def fill(value):
    return value


class Isolated(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patch = mock.patch.dict(os.environ, {'HOME': tmp.name, 'USERPROFILE': tmp.name,
                                            'MYFLAMES_SECRET_BACKEND': 'vault'})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.home = Path(tmp.name)
        self.store = WorkspaceStore(self.home / '.myflames' / 'workspace.sqlite3')
        # One created_at per save, in order: the list order is then exact.
        ticks = iter('2026-11-08T14:%02d:%02d.000000+00:00' % divmod(n, 60) for n in range(3600))
        clock = mock.patch.object(workspace_store, '_now', side_effect=lambda: next(ticks))
        clock.start()
        self.addCleanup(clock.stop)

    def save(self, kind='health', report=None, name='Before deploy', connection=None, values=None, reading=None):
        """Save *report* as read from the server named by *reading* (identity
        values; by default the server the connection reaches at save time,
        *values*)."""
        report = report or (health_report('mysql-8.4') if kind == 'health' else digest_report('mysql-8.4'))
        values = values or identity_values(report)
        if 'reading_server' not in report:
            report = with_reading(report, reading or values)
        with answer(values):
            return baselines.response('/api/baselines/save', {
                'connection': connection or {'host': '10.0.0.5', 'port': 3306, 'user': 'app'},
                'kind': kind, 'name': name, 'report': report}, self.store, fill)


class TestStorage(Isolated):
    def test_round_trip_lists_newest_first_and_keeps_the_payload(self):
        for n in range(3):
            self.store.baseline_save('mysql/uuid/a', 'health', 'Reading %d' % n, {'status': 'ok'}, {'n': n})
        items = self.store.baseline_list('mysql/uuid/a', 'health')
        self.assertEqual([i['name'] for i in items], ['Reading 2', 'Reading 1', 'Reading 0'],
                         'newest first, so the picker pre-fills the newest')
        got = self.store.baseline_get(items[-1]['id'])
        self.assertEqual(got['payload'], {'n': 0})
        self.assertEqual((got['kind'], got['status']), ('health', 'ok'))
        self.assertTrue(self.store.baseline_delete(items[-1]['id']))
        self.assertIsNone(self.store.baseline_get(items[-1]['id']))
        self.assertEqual(len(self.store.baseline_list('mysql/uuid/a', 'health')), 2)
        self.assertEqual(self.store.baseline_list('mysql/uuid/a', 'digest'), [], 'kinds never mix')

    def test_twenty_per_server_and_kind_then_refused_without_deleting(self):
        for n in range(BASELINE_MAX_PER_SERVER):
            self.store.baseline_save('mysql/uuid/a', 'health', 'Reading %d' % n, {}, {'n': n})
        self.assertEqual(BASELINE_MAX_PER_SERVER, 20)
        with self.assertRaises(BaselineLimit) as refused:
            self.store.baseline_save('mysql/uuid/a', 'health', 'One more', {}, {})
        self.assertEqual(str(refused.exception), 'Could not save the baseline: 20 Health baselines of this server are '
                         'already kept on this computer, the most allowed. Delete one in Compare with baseline, then '
                         'save again.')
        names = [i['name'] for i in self.store.baseline_list('mysql/uuid/a', 'health')]
        self.assertEqual(len(names), 20)
        self.assertIn('Reading 0', names, 'the oldest is never deleted to make room')
        # The limit is per server and kind.
        self.store.baseline_save('mysql/uuid/a', 'digest', 'Digests', {}, {})
        self.store.baseline_save('mysql/uuid/b', 'health', 'Other server', {}, {})
        oldest = self.store.baseline_list('mysql/uuid/a', 'health')[-1]
        self.assertTrue(self.store.baseline_delete(oldest['id']))
        self.store.baseline_save('mysql/uuid/a', 'health', 'After a delete', {}, {})

    def test_two_mib_is_the_largest_baseline(self):
        # {"x":"..."} is 8 bytes of JSON around the text.
        self.store.baseline_save('k', 'digest', 'At the limit', {}, {'x': 'a' * (BASELINE_MAX_BYTES - 8)})
        with self.assertRaises(ValueError) as refused:
            self.store.baseline_save('k', 'digest', 'Over the limit', {}, {'x': 'a' * (BASELINE_MAX_BYTES - 7)})
        self.assertIn('a baseline holds at most 2 MiB', str(refused.exception))
        self.assertEqual([i['name'] for i in self.store.baseline_list('k', 'digest')], ['At the limit'])
        self.assertEqual(len(self.store.baseline_get(self.store.baseline_list('k', 'digest')[0]['id'])['payload']['x']),
                         BASELINE_MAX_BYTES - 8)

    def test_names_are_one_short_line(self):
        for name in ('', '   ', 'a' * 81, 'two\nlines', 'tab\there', None, 3):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.baseline_save('k', 'health', name, {}, {})
        item = self.store.baseline_save('k', 'health', '  ' + 'a' * 80 + '  ', {}, {})
        self.assertEqual(item['name'], 'a' * 80)
        with self.assertRaises(ValueError):
            self.store.baseline_save('k', 'plans', 'Unknown kind', {}, {})

    def test_a_damaged_payload_is_refused_not_decoded(self):
        item = self.store.baseline_save('k', 'health', 'Damaged', {}, {'n': 1})
        with sqlite3.connect(str(self.store.path)) as conn:
            conn.execute('UPDATE baselines SET payload = ? WHERE id = ?', (b'not zlib', item['id']))
        with self.assertRaises(Exception):
            self.store.baseline_get(item['id'])
        import zlib
        bomb = zlib.compress(b'"' + b'a' * (BASELINE_MAX_BYTES + 10) + b'"')
        with sqlite3.connect(str(self.store.path)) as conn:
            conn.execute('UPDATE baselines SET payload = ? WHERE id = ?', (bomb, item['id']))
        with self.assertRaises(ValueError):
            self.store.baseline_get(item['id'])

    def test_reading_creates_nothing_and_unknown_ids_are_answered(self):
        self.assertEqual(self.store.baseline_list('k', 'health'), [])
        self.assertFalse(self.store.path.exists(), 'listing never creates the workspace file')
        self.store.save('profile', {'name': 'prod', 'host': 'db.internal', 'port': 3306, 'user': 'app'})
        # A workspace made before baselines: the table appears on first use only.
        with sqlite3.connect(str(self.store.path)) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertNotIn('baselines', tables)
        for key in ('zz', '0' * 32, None, 'A' * 32):
            self.assertIsNone(self.store.baseline_get(key))
            self.assertFalse(self.store.baseline_delete(key))
        self.assertEqual(len(self.store.list('profile')), 1)


class TestIdentity(Isolated):
    def identity(self, **values):
        base = {'version': '8.4.3', 'version_comment': 'MySQL Community Server - GPL', 'hostname': 'db1',
                'port': '3306', 'server_uuid': UUID}
        base.update(values)
        return baselines.identity_from({k: v for k, v in base.items() if v is not None})

    def test_mysql_is_named_by_server_uuid_whatever_the_address(self):
        a = self.identity(hostname='db1', port='3306')
        b = self.identity(hostname='replica-alias', port='13306', server_uuid=UUID.upper())
        self.assertEqual(baselines.server_key(a), baselines.server_key(b))
        self.assertEqual(baselines.server_key(a), 'mysql/uuid/' + UUID)
        self.assertNotEqual(baselines.server_key(a), baselines.server_key(self.identity(server_uuid=OTHER_UUID)))
        # An upgraded server keeps its uuid, so its baselines still match.
        self.assertEqual(baselines.server_key(a), baselines.server_key(self.identity(version='9.7.0')))
        five_seven = self.identity(version='5.7.44-log')
        self.assertEqual(baselines.server_key(five_seven), 'mysql/uuid/' + UUID)
        percona = self.identity(version='8.4.11-11', version_comment='Percona Server (GPL), Release 11')
        self.assertEqual((percona['flavor'], percona['product']), ('percona', 'Percona Server'))

    def test_mariadb_is_named_by_hostname_and_port(self):
        maria = dict(version='11.4.13-MariaDB-ubu2404', version_comment='mariadb.org binary distribution',
                     server_uuid=None)
        a = self.identity(hostname='db2', port='3306', **maria)
        self.assertEqual(a['engine'], 'mariadb')
        self.assertIsNone(a['server_uuid'])
        self.assertEqual(baselines.server_key(a), 'mariadb/host/db2:3306')
        self.assertEqual(baselines.server_key(a), baselines.server_key(self.identity(hostname='DB2.', port='3306', **maria)))
        self.assertNotEqual(baselines.server_key(a), baselines.server_key(self.identity(hostname='db2', port='3307', **maria)))
        # The engine is part of the name: a MySQL that reports no uuid on the same host and port is another server.
        mysql = self.identity(hostname='db2', port='3306', server_uuid='not-a-uuid')
        self.assertIsNone(mysql['server_uuid'], 'a malformed server_uuid is not used')
        self.assertNotEqual(baselines.server_key(a), baselines.server_key(mysql))

    def test_a_server_without_a_name_is_refused(self):
        with self.assertRaises(ValueError):
            self.identity(server_uuid=None, hostname=None)
        with self.assertRaises(ValueError):
            self.identity(server_uuid=None, port='0')
        with self.assertRaises(ValueError):
            baselines.identity_from({'hostname': 'db1', 'port': '3306'})

    def test_identity_is_one_tagged_read_only_client_run(self):
        report = health_report('mariadb-11.4')
        with answer(identity_values(report, uptime=99, hostname='maria\\tbox')):
            identity = baselines.read_server({'host': '10.0.0.9', 'port': 3306, 'user': 'app'}, fill)
        self.assertEqual(len(FakeConnection.calls), 1, 'one client run')
        sql, flags, options = FakeConnection.calls[0]
        self.assertEqual(flags, ['--comments', '--skip-reconnect'], 'the tag reaches the server, so Top statements leaves it out')
        statements = [s.strip() for s in sql.split(';')]
        self.assertEqual(len(statements), 3)
        for statement in statements:
            self.assertTrue(statement.startswith('/*myflames:baselines*/ '), statement)
            self.assertRegex(statement, r'^/\*myflames:baselines\*/ (SHOW GLOBAL (VARIABLES|STATUS) WHERE|SELECT )')
        self.assertIsNone(WRITE_WORDS.search(re.sub(r"'[^']*'", "''", sql)), sql)
        self.assertEqual(options['query_timeout'], baselines.QUERY_TIMEOUT)
        self.assertEqual((identity['hostname'], identity['uptime_s'], identity['engine']), ('maria\tbox', 99, 'mariadb'))
        self.assertEqual(identity['server_time'], '2026-11-08 14:02:31.123456')

    def test_reading_time_is_the_server_clock_at_the_reading(self):
        identity = self.identity()
        identity.update(uptime_s=1000, server_time='2026-11-08 14:02:31.123456')
        self.assertEqual(baselines.reading_time(identity, 940), '2026-11-08 14:01:31.123456')
        self.assertEqual(baselines.reading_time(identity, 1001), '2026-11-08 14:02:31.123456',
                         'Uptime has one-second resolution: a second ahead is the same moment')
        with self.assertRaises(ValueError) as restarted:
            baselines.reading_time(identity, 5000)
        self.assertIn('the server restarted after this reading', str(restarted.exception))
        self.assertIsNone(baselines.reading_time(identity, None))
        identity['server_time'] = None
        self.assertIsNone(baselines.reading_time(identity, 940))


class TestKeptPayload(Isolated):
    SECRETS = ('alice@example.com', 'card-4111', 'hunter2')

    def test_digest_literals_are_hidden_and_samples_are_never_kept(self):
        for server in ('mysql-8.4', 'mysql-5.7', 'mariadb-11.4'):
            with self.subTest(server=server):
                report = digest_report(server)
                first = report['statements'][0]
                first['sample_text'] = "SELECT * FROM users WHERE email = 'alice@example.com'"
                first['analysis_sql'] = "SELECT * FROM users WHERE password = 'hunter2'"
                # Older servers and cut digest text can carry a literal.
                first['digest_text'] = "SELECT * FROM `cards` WHERE `note` = 'card-4111' AND `n` = 4111"
                kept = baselines.clean_digests(report)
                text = json.dumps(kept)
                for secret in self.SECRETS + ('4111',):
                    self.assertNotIn(secret, text)
                self.assertEqual(kept['statements'][0]['digest_text'], 'SELECT * FROM `cards` WHERE `note` = ? AND `n` = ?')
                for row in kept['statements']:
                    self.assertEqual(set(row), {'digest', 'schema', 'digest_text', 'count', 'total_ms', 'avg_ms',
                                                'max_ms', 'rows_examined', 'rows_sent', 'first_seen', 'last_seen'})
                source = report['statements'][1]
                row = kept['statements'][1]
                self.assertEqual((row['digest'], row['schema'], row['count'], row['total_ms'], row['first_seen']),
                                 (source['digest'], source['schema'], source['count'], source['total_ms'],
                                  source['first_seen']), 'numbers and names are kept exactly')
                self.assertEqual(row['digest_text'], source['digest_text'], 'normalized text keeps its ? marks')

    def test_hidden_through_the_endpoint_and_stored_file(self):
        report = digest_report('mysql-8.4')
        report['statements'][0]['sample_text'] = "SELECT 'alice@example.com'"
        report['statements'][0]['digest_text'] = "SELECT 'card-4111'"
        saved = self.save('digest', report)
        got = baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)
        self.assertEqual(got['payload']['statements'][0]['digest_text'], 'SELECT ?')
        raw = self.store.path.read_bytes()
        import zlib
        with sqlite3.connect(str(self.store.path)) as conn:
            blob = conn.execute('SELECT payload FROM baselines').fetchone()[0]
        for secret in self.SECRETS:
            self.assertNotIn(secret.encode(), raw)
            self.assertNotIn(secret.encode(), zlib.decompress(blob))

    def test_health_keeps_numbers_statuses_and_headlines_on_every_engine(self):
        for server in ('mysql-5.7', 'mysql-8.4', 'mysql-26.7', 'percona-8.4', 'mariadb-11.4', 'mariadb-13.0'):
            with self.subTest(server=server):
                report = health_report(server)
                kept = baselines.clean_health(report)
                self.assertEqual([(m['key'], m['value'], m['status'], m['unit']) for m in kept['metrics']],
                                 [(m['key'], m['value'], m['status'], m['unit']) for m in report['metrics']])
                self.assertEqual([(d['key'], d['status'], d['state'], d['headline']) for d in kept['diagnostics']],
                                 [(d['key'], d['status'], d['state'], d['headline']) for d in report['diagnostics']])
                self.assertEqual((kept['uptime_s'], kept['taken_at'], kept['engine'], kept['server_version']),
                                 (report['uptime_s'], report['taken_at'], report['engine'], report['server_version']))
                self.assertEqual(kept['summary']['status'], report['summary']['status'])
                for row in kept['metrics'] + kept['diagnostics']:
                    self.assertFalse({'explain', 'why', 'rule', 'verdict', 'recommendation', 'sources'} & set(row))
                self.assertLess(len(json.dumps(kept)), len(json.dumps(report)) / 2)

    def test_malformed_readings_are_refused(self):
        good = health_report('mysql-8.4')
        cases = []
        for change in (lambda r: r.update(schema_version='health-2.0'), lambda r: r.update(engine='oracle'),
                       lambda r: r['metrics'][0].update(value=float('nan')), lambda r: r['metrics'][0].update(value='12'),
                       lambda r: r['metrics'][0].update(status='great'), lambda r: r['metrics'][0].update(key='Bad Key'),
                       lambda r: r['metrics'].append(dict(r['metrics'][0])), lambda r: r.update(taken_at=None),
                       lambda r: r['diagnostics'][0].update(title=None), lambda r: r.update(metrics='all')):
            report = copy.deepcopy(good)
            change(report)
            cases.append(('health', report))
        digests = digest_report('mysql-8.4')
        for change in (lambda r: r['statements'][0].update(digest='not-hex!'),
                       lambda r: r['statements'][0].update(first_seen='yesterday'),
                       lambda r: r['statements'][0].update(count=-1),
                       lambda r: r['statements'].extend([dict(r['statements'][0], digest='%064x' % n)
                                                         for n in range(baselines.MAX_STATEMENTS)]),
                       lambda r: r.update(read_at=None), lambda r: r.update(schema_version='slow-log-1.0')):
            report = copy.deepcopy(digests)
            change(report)
            cases.append(('digest', report))
        for i, (kind, report) in enumerate(cases):
            with self.subTest(case=i, kind=kind), self.assertRaises(ValueError):
                (baselines.clean_health if kind == 'health' else baselines.clean_digests)(report)


class TestEndpoints(Isolated):
    def test_save_list_get_delete_on_mysql_and_mariadb(self):
        for server in ('mysql-8.4', 'mariadb-13.0'):
            for kind in ('health', 'digest'):
                with self.subTest(server=server, kind=kind):
                    report = health_report(server) if kind == 'health' else digest_report(
                        'mysql-8.4' if server.startswith('mysql') else 'mariadb-11.4')
                    values = identity_values(report, hostname='host-' + server)
                    saved = self.save(kind, report, name='Before %s' % server, values=values)
                    self.assertEqual((saved['kind'], saved['name'], saved['max']), (kind, 'Before %s' % server, 20))
                    with answer(values):
                        listed = baselines.response('/api/baselines/list', {
                            'connection': {'host': 'elsewhere'}, 'kind': kind}, self.store, fill)
                    self.assertEqual(listed['schema_version'], 'baseline-list-1.0')
                    self.assertEqual([i['id'] for i in listed['items']], [saved['id']])
                    self.assertEqual(listed['server']['engine'], report['engine'])
                    self.assertEqual(listed['server']['server_uuid'], None if report['engine'] == 'mariadb' else UUID)
                    got = baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)
                    self.assertEqual(got['payload']['engine'], report['engine'])
                    self.assertEqual(got['payload']['server']['hostname'], 'host-' + server)
                    if kind == 'health':
                        self.assertEqual((got['taken_at'], got['status']), (report['taken_at'], report['summary']['status']))
                    else:
                        self.assertEqual(got['statements'], len(report['statements']))
                    self.assertEqual(baselines.response('/api/baselines/delete', {'id': saved['id']}, self.store),
                                     {'deleted': True, 'id': saved['id']})
                    with self.assertRaises(ValueError):
                        baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)

    def test_list_follows_the_server_not_the_address(self):
        report = health_report('mysql-8.4')
        saved = self.save('health', report, connection={'host': '10.0.0.5', 'port': 3306})
        with answer(identity_values(report, hostname='other-name', port='13306')):
            same = baselines.response('/api/baselines/list', {'connection': {'host': 'db-via-tunnel', 'port': 13306},
                                                              'kind': 'health'}, self.store, fill)
        self.assertEqual([i['id'] for i in same['items']], [saved['id']])
        with answer(identity_values(report, server_uuid=OTHER_UUID)):
            other = baselines.response('/api/baselines/list', {'connection': {'host': '10.0.0.5', 'port': 3306},
                                                               'kind': 'health'}, self.store, fill)
        self.assertEqual(other['items'], [], 'another server_uuid at the same address is another server')

    def test_saved_password_fills_the_identity_read_and_never_leaks(self):
        report = health_report('mysql-8.4')
        filled = []

        def with_saved(value):
            filled.append(value['connection'].get('profile_id'))
            return {'connection': dict(value['connection'], password='needle-Pa55')}
        with answer(identity_values(report)):
            baselines.response('/api/baselines/list', {'connection': {'host': 'db', 'profile_id': 'p1'},
                                                       'kind': 'health'}, self.store, with_saved)
        self.assertEqual(filled, ['p1'])
        self.assertEqual(FakeConnection.calls[0][2]['password'], 'needle-Pa55')
        error = ConnectorError("mysql client exited 1: ERROR 1045 (28000): Access denied for user 'app' "
                               "(using password: needle-Pa55)")
        with answer([], error=error), self.assertRaises(ValueError) as failed:
            baselines.response('/api/baselines/list', {'connection': {'host': 'db', 'profile_id': 'p1'},
                                                       'kind': 'health'}, self.store, with_saved)
        self.assertNotIn('needle-Pa55', str(failed.exception))
        self.assertIn('Access denied', str(failed.exception))

    def test_a_reading_from_another_server_or_before_a_restart_is_refused(self):
        report = health_report('mysql-8.4')
        upgraded = identity_values(report)
        upgraded[3] = ('version', '9.7.0')
        with self.assertRaises(ValueError) as other:
            self.save('health', report, values=upgraded, reading=identity_values(report))
        self.assertEqual(str(other.exception), 'Could not save the baseline: this reading came from MySQL 8.4.11, but '
                                               'the connection now reaches MySQL 9.7.0. Refresh, then save again.')
        with self.assertRaises(ValueError) as restarted:
            self.save('health', report, values=identity_values(report, uptime=3))
        self.assertIn('the server restarted after this reading', str(restarted.exception))
        self.assertEqual(self.store.baseline_list(baselines.server_key(baselines.identity_from(
            {'version': '8.4.11', 'server_uuid': UUID, 'hostname': 'db1', 'port': '3306'})), 'health'), [])

    def test_digest_baselines_carry_the_server_clock_of_their_reading(self):
        report = digest_report('mysql-8.4')
        uptime = report['coverage']['uptime_s']
        saved = self.save('digest', report, values=identity_values(report, uptime=uptime + 75,
                                                                   now='2026-10-31 10:20:00.500000'))
        got = baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)
        self.assertEqual(got['payload']['server_time'], '2026-10-31 10:18:45.500000')
        self.assertEqual(got['server_time'], '2026-10-31 10:18:45.500000')
        self.assertEqual(got['payload']['coverage']['uptime_s'], uptime)
        self.assertEqual(got['executions'], sum(s['count'] for s in report['statements']))

    def test_bad_requests_are_refused_before_any_connection(self):
        report = health_report('mysql-8.4')
        cases = [
            ('/api/baselines/list', {'kind': 'plans', 'connection': {'host': 'db'}}),
            ('/api/baselines/save', {'kind': 'health', 'name': '', 'report': report, 'connection': {'host': 'db'}}),
            ('/api/baselines/save', {'kind': 'health', 'name': 'x' * 81, 'report': report, 'connection': {'host': 'db'}}),
            ('/api/baselines/save', {'kind': 'health', 'name': 'ok', 'report': {'schema_version': 'health-1.0'},
                                     'connection': {'host': 'db'}}),
            ('/api/baselines/save', {'kind': 'digest', 'name': 'ok', 'report': report, 'connection': {'host': 'db'}}),
            ('/api/baselines/get', {'id': '../etc'}), ('/api/baselines/delete', {'id': 7}),
            ('/api/baselines/delete', {'id': '0' * 32}), ('/api/baselines/rename', {}),
        ]
        for path, payload in cases:
            with self.subTest(path=path, payload=sorted(payload)), \
                    mock.patch.object(baselines, 'MySQLConnection') as connect, self.assertRaises(ValueError):
                baselines.response(path, payload, self.store, fill)
            connect.assert_not_called()

    def test_the_limit_reaches_the_api_with_its_reason(self):
        report = health_report('mariadb-11.4')
        for n in range(20):
            self.save('health', report, name='Reading %d' % n)
        with self.assertRaises(BaselineLimit) as refused:
            self.save('health', report, name='One too many')
        self.assertIn('Delete one in Compare with baseline', str(refused.exception))
        self.assertEqual(ui.request_error_text(refused.exception), str(refused.exception),
                         'the reason is shown as it is, under no generic prefix')


class TestReadingServer(Isolated):
    """The server a reading came from travels with it (reading_server, read
    in the same request), and a save files it only under that server."""

    def test_an_endpoint_now_on_another_server_of_the_same_version_is_refused(self):
        """A tunnel or virtual IP that moved to another server of the same
        version: the identity read at save time names server B, the reading
        came from server A. Before the fix A's reading was filed under B."""
        cases = (
            ('mysql-8.4', dict(server_uuid=OTHER_UUID), 'MySQL {v} (server_uuid %s)' % UUID,
             'MySQL {v} (server_uuid %s)' % OTHER_UUID),
            ('mariadb-11.4', dict(hostname='db-b'), 'MariaDB {v} at db1:3306', 'MariaDB {v} at db-b:3306'),
            ('mariadb-11.4', dict(port='3307'), 'MariaDB {v} at db1:3306', 'MariaDB {v} at db1:3307'),
        )
        for server, now, was, reaches in cases:
            for kind in ('health', 'digest'):
                with self.subTest(server=server, kind=kind, now=now):
                    report = health_report(server) if kind == 'health' else digest_report(server)
                    was_text, reaches_text = (t.format(v=report['server_version']) for t in (was, reaches))
                    with self.assertRaises(ValueError) as refused:
                        self.save(kind, report, reading=identity_values(report), values=identity_values(report, **now))
                    self.assertEqual(str(refused.exception), 'Could not save the baseline: this reading came from %s, '
                                     'but the connection now reaches %s. Refresh, then save again.'
                                     % (was_text, reaches_text))
                    self.assertEqual(self.store.baseline_list(baselines.server_key(baselines.identity_from(
                        dict((k.lower(), v) for k, v in identity_values(report, **now)))), kind), [],
                        "nothing is filed under the server reached now")
        # The same server through another address is still the same server.
        report = health_report('mysql-8.4')
        saved = self.save('health', report, reading=identity_values(report, hostname='db1', port='3306'),
                          values=identity_values(report, hostname='replica-alias', port='13306'))
        self.assertEqual(saved['server_key'], 'mysql/uuid/' + UUID)

    def test_a_reading_without_its_server_identity_is_refused_before_connecting(self):
        report = health_report('mysql-8.4')
        failed = dict(report, reading_server={'error': 'Could not read the server identity.'})
        forged = with_reading(report, identity_values(report))
        forged['reading_server']['server'] = dict(forged['reading_server']['server'], engine='mariadb')
        for case in (report, failed, dict(report, reading_server={'server': {'engine': 'mysql'}}), forged):
            with self.subTest(case=sorted(case.get('reading_server') or {})), \
                    mock.patch.object(baselines, 'MySQLConnection') as connect, self.assertRaises(ValueError) as refused:
                baselines.response('/api/baselines/save', {'connection': {'host': 'db'}, 'kind': 'health', 'name': 'x',
                                                           'report': case}, self.store, fill)
            connect.assert_not_called()
            self.assertTrue(str(refused.exception).startswith('Could not save the baseline: '))
        self.assertEqual(self._refusal(failed), "Could not save the baseline: the server's identity was not read "
                                                "with this reading. Refresh, then save again.")

    def _refusal(self, report):
        try:
            baselines.reading_server(report)
        except ValueError as exc:
            return str(exc)
        return ''

    def test_the_key_is_computed_from_the_server_never_taken_from_the_reading(self):
        report = health_report('mysql-8.4')
        reading = with_reading(report, identity_values(report, server_uuid=OTHER_UUID))
        reading['reading_server']['server_key'] = 'mysql/uuid/' + UUID
        with self.assertRaises(ValueError) as refused:
            self.save('health', reading, values=identity_values(report))
        self.assertIn('this reading came from MySQL 8.4.11 (server_uuid %s)' % OTHER_UUID, str(refused.exception))

    def test_items_carry_their_server_key(self):
        report = health_report('mariadb-13.0')
        saved = self.save('health', report)
        self.assertEqual(saved['server_key'], 'mariadb/host/db1:3306')
        with answer(identity_values(report)):
            listed = baselines.response('/api/baselines/list', {'connection': {'host': 'db'}, 'kind': 'health'},
                                        self.store, fill)
        self.assertEqual(listed['server_key'], 'mariadb/host/db1:3306')
        self.assertEqual([i['server_key'] for i in listed['items']], ['mariadb/host/db1:3306'])
        self.assertEqual(baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)['server_key'],
                         'mariadb/host/db1:3306')

    def test_a_digest_baseline_takes_the_server_clock_of_the_identity_read_with_it(self):
        report = digest_report('mysql-8.4')
        uptime = report['coverage']['uptime_s']
        # Read with the reading: 46 s of Uptime later, NOW(6) 10:18:52. At save
        # time, much later: 1,000 s more, 10:35:38.
        reading = identity_values(report, uptime=uptime + 46, now='2026-10-31 10:18:52.000000')
        saved = self.save('digest', report, reading=reading,
                          values=identity_values(report, uptime=uptime + 1046, now='2026-10-31 10:35:38.400000'))
        got = baselines.response('/api/baselines/get', {'id': saved['id']}, self.store)
        self.assertEqual(got['payload']['server_time'], '2026-10-31 10:18:06.000000')

    def test_unverified_or_legacy_identity_cannot_be_saved(self):
        for verified in (None, False):
            report = with_reading(health_report('mysql-8.4'), identity_values(health_report('mysql-8.4')))
            report['reading_server']['verified'] = verified
            with answer(identity_values(report)), self.assertRaisesRegex(ValueError, 'identity was not read'):
                baselines.response('/api/baselines/save', {'connection': {'host': 'db'}, 'kind': 'health',
                                   'name': 'Unsafe', 'report': report}, self.store, fill)
            self.assertEqual(FakeConnection.calls, [], 'refuse before opening another connection')

    def test_whole_table_first_seen_is_kept_independently_of_display_threshold(self):
        for server in ('mysql-8.4', 'mariadb-11.4'):
            with self.subTest(server=server):
                report = digest_report(server)
                report['coverage'].update(oldest_first_seen='2026-10-14 16:00:00', counting_since=None)
                saved = self.save('digest', report)
                got = self.store.baseline_get(saved['id'])['payload']['coverage']
                self.assertEqual(got['oldest_first_seen'], '2026-10-14 16:00:00')
                self.assertIsNone(got['counting_since'])
                report['coverage']['oldest_first_seen'] = 'not a timestamp'
                with self.assertRaisesRegex(ValueError, 'oldest_first_seen'):
                    baselines.clean_digests(report)

    def test_standalone_identity_timeout_is_three_seconds(self):
        conn = mock.Mock()
        conn.run.return_value = ''.join('%s\t%s\n' % pair for pair in identity_values(health_report('mysql-8.4')))
        baselines.read_identity(conn)
        self.assertEqual(conn.run.call_args[1]['timeout'], 3)
        self.assertEqual(baselines.QUERY_TIMEOUT, 3)

    def test_identity_batch_framing_does_not_accept_server_text_as_a_marker(self):
        # A name containing a marker and a newline is data, never another result set.
        text = 'myflames_reading_0\nname\tmyflames_reading_1\\nspoof\nmyflames_reading_1\n'
        text += ''.join('%s\t%s\n' % pair for pair in identity_values(health_report('mysql-8.4')))
        conn = FakeConnection(text)
        groups, reading = baselines.read_with_identity(conn, ['SELECT name, value FROM fixed_table'])
        self.assertEqual(groups, [[['name', 'myflames_reading_1\nspoof']]])
        self.assertTrue(reading['verified'])
        for broken in ('', 'name\tvalue\n', 'myflames_reading_0\nname\tvalue\n'):
            with self.subTest(broken=broken), self.assertRaisesRegex(ValueError, 'incomplete reading'):
                baselines.read_with_identity(FakeConnection(broken), ['SELECT name, value FROM fixed_table'])

    def test_incomplete_identity_keeps_data_but_is_explicitly_unverified(self):
        groups, reading = baselines.read_with_identity(FakeConnection(
            'myflames_reading_0\nname\tvalue\nmyflames_reading_1\nversion\t8.4.3\n'), ['SELECT name, value FROM fixed_table'])
        self.assertEqual(groups, [[['name', 'value']]])
        self.assertEqual(reading['verified'], False)
        self.assertIn('error', reading)

    def test_failed_batch_never_gets_an_identity_from_another_invocation(self):
        conn = mock.Mock()
        conn.run.side_effect = ConnectorError('Lost connection')
        with self.assertRaisesRegex(ConnectorError, 'Lost connection'):
            baselines.read_with_identity(conn, ['SHOW GLOBAL STATUS'])
        self.assertEqual(conn.run.call_count, 1)
        self.assertIn('--skip-reconnect', conn.run.call_args[1]['extra_flags'])

    def test_identity_tail_failure_keeps_completed_data_and_refuses_save(self):
        for engine in ('mysql-8.4', 'mariadb-11.4'):
            for message in ('ERROR 1227: Access denied', 'Query timed out after 3s.'):
                with self.subTest(engine=engine, failure=message):
                    # Even complete identity fields must be discarded after a
                    # trailing failure, such as NOW(6) losing its connection.
                    prefix = 'myflames_reading_0\nname\tvalue\nmyflames_reading_1\n'
                    fields = ''.join('%s\t%s\n' % p for p in identity_values(health_report(engine)))
                    conn = mock.Mock()
                    conn.run.side_effect = PartialReadError(message, prefix + fields)
                    groups, identity = baselines.read_with_identity(conn, ['SHOW GLOBAL STATUS'])
                    self.assertEqual(groups, [[['name', 'value']]])
                    self.assertFalse(identity['verified'])
                    self.assertNotIn('server', identity)
                    self.assertEqual(conn.run.call_count, 1)
                    self.assertEqual(conn.run.call_args[1]['tail_timeout'], ('myflames_reading_1', 3))
                    report = dict(health_report(engine), reading_server=identity)
                    with mock.patch.object(baselines, 'read_server') as connect, self.assertRaisesRegex(
                            ValueError, 'identity was not read'):
                        baselines.response('/api/baselines/save', {'connection': {'host': 'db'}, 'kind': 'health',
                                           'name': 'Unverified', 'report': report}, self.store, fill)
                    connect.assert_not_called()

    def test_failed_required_data_and_spoofed_or_unterminated_markers_are_not_salvaged(self):
        for output in ('', 'myflames_reading_0\nname\tvalue\n',
                       'myflames_reading_0\nname\tvalue\nmyflames_reading_1',
                       'myflames_reading_0\nname\tmyflames_reading_1\\nspoof\n'):
            with self.subTest(output=output):
                conn = mock.Mock()
                error = PartialReadError('Required statement failed', output)
                conn.run.side_effect = error
                with self.assertRaises(PartialReadError) as raised:
                    baselines.read_with_identity(conn, ['SHOW GLOBAL STATUS'])
                self.assertIs(raised.exception, error)
                self.assertEqual(conn.run.call_count, 1)


class TestBaselineHTTP(unittest.TestCase):
    """The four paths pass the local server's checks and reach the store."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'HOME': cls.home.name, 'USERPROFILE': cls.home.name,
                                              'MYFLAMES_SECRET_BACKEND': 'vault'})
        cls.env.start()
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        try:
            cls.server = ui.WorkspaceServer()
            cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
            cls.thread.start()
        except Exception:
            cls.env.stop()
            cls.home.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
        finally:
            cls.env.stop()
            cls.home.cleanup()

    def request(self, path, payload, token=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=10)
        headers = {'Content-Type': 'application/json', 'X-Myflames-Token': token or self.server.token,
                   'Origin': self.server.origin, 'Cookie': self.server.cookie_name + '=' + self.server.session_secret}
        try:
            conn.request('POST', path, body=json.dumps(payload), headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b'{}')
        finally:
            conn.close()

    def test_paths_are_allowed_behind_the_session_checks(self):
        self.assertEqual(set(ui._BASELINE_PATHS), set(baselines.PATHS))
        status, body = self.request('/api/baselines/get', {'id': 'nope'}, token='wrong')
        self.assertEqual(status, 403)
        status, body = self.request('/api/baselines/get', {'id': 'nope'})
        self.assertEqual((status, body), (400, {'error': 'Could not complete the request: Choose a saved baseline.'}))
        report = health_report('mysql-8.4')
        with answer(identity_values(report)):
            status, saved = self.request('/api/baselines/save', {'connection': {'host': 'db'}, 'kind': 'health',
                                                                 'name': 'Over HTTP',
                                                                 'report': with_reading(report, identity_values(report))})
        self.assertEqual(status, 200, saved)
        with answer(identity_values(report)):
            status, listed = self.request('/api/baselines/list', {'connection': {'host': 'db'}, 'kind': 'health'})
        self.assertEqual([i['name'] for i in listed['items']], ['Over HTTP'])
        status, deleted = self.request('/api/baselines/delete', {'id': saved['id']})
        self.assertEqual((status, deleted), (200, {'deleted': True, 'id': saved['id']}))


if __name__ == '__main__':
    unittest.main()
