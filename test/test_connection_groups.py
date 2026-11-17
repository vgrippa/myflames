"""Group consent is an authorization boundary, not a topology heuristic.

No test here opens a network socket or reads real saved credentials. Engine
reports use the existing captured/synthetic fixtures; new identity and client
attribute cases are explicitly synthetic.
"""
import copy
import inspect
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import _cluster_fixtures as CF
import test_replication_source_version as SV
from _perf_replay import run_cli
from test_topology_links import FakeProbeConn
from test_readyset import FakeReadyset
from test_proxysql import FakeAdmin
from test_chart_data import validate
from myflames import cluster, connection_groups as cg, connection_import, proxysql, readyset
from myflames import connector, dbops, editor_session, mcp_server, replication as rp, topology_links as tl, ui
from myflames.connector import ConnectorError
from myflames.workspace_store import ENDPOINT_FIELDS, WorkspaceStore

ROOT = Path(__file__).resolve().parent.parent
EMPTY = {'name': '', 'read': [], 'suggestions': [], 'join': [], 'suggested_name': ''}


class LocalDatabase(FakeProbeConn):
    """Synthetic standalone database answering the existing read-only probes."""
    def __init__(self, **options):
        super(LocalDatabase, self).__init__('database', hostname='current-db', uuid='uuid-current')
        for key in ENDPOINT_FIELDS:
            setattr(self, key, options.get(key))

    def run(self, sql, extra_flags=None, timeout=None):
        return super(LocalDatabase, self).run(sql.replace(rp.REPLICATION_TAG, ''), extra_flags, timeout)


class GroupCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name,
                   MYFLAMES_SECRET_BACKEND='vault', PYTHONDONTWRITEBYTECODE='1')
        env.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = WorkspaceStore()
        tl._reset_cache_for_tests()
        rp.reset_source_cache()

    def save(self, pid, group='', **fields):
        value = dict(name=pid, host=pid + '.example', port=3306, user='dba', group=group)
        value.update(fields)
        return self.store.save('profile', value, pid, password='test-password', remember=True)

    def request(self, current):
        return dict({k: current.get(k) for k in ENDPOINT_FIELDS}, profile_id=current['id'])


class TestGroupStore(GroupCase):
    def test_atomic_assignment_and_validation_through_api_dispatch(self):
        first, second = self.save('one'), self.save('two')
        before = self.store.list('profile')
        workspace = types.SimpleNamespace(store=self.store)
        for ids, group in ((['one', 'missing', 'two'], 'G'), (['one'], 'x' * 81),
                           (['one'], 'G\n'), (['one'], 'G\x7f'), (['one'], 'G\u0085'),
                           (['one'], 'G\u200b'), ([], 'G'), (['one'] * 101, 'G'),
                           (['one', 2], 'G'), (['one'], None)):
            with self.subTest(ids=ids, group=group):
                with self.assertRaises(ValueError) as error:
                    ui.api_response('/api/profiles/group', {'profile_ids': ids, 'group': group}, workspace)
                if 'missing' in ids:
                    self.assertIn('missing', str(error.exception))
                self.assertEqual(self.store.list('profile'), before)
        result = ui.api_response('/api/profiles/group', {'profile_ids': ['one', 'two'], 'group': '  G  '}, workspace)
        self.assertEqual(result, {'updated': ['one', 'two'], 'group': 'G'})
        self.assertEqual({p['group'] for p in self.store.list('profile')}, {'G'})
        self.assertEqual(self.store.assign_profile_group(['one'], '')['group'], '')
        # A new write uses the same authenticated POST allowlist and dispatcher.
        self.assertIn('"/api/profiles/group"', inspect.getsource(ui))

    def test_group_changes_preserve_password_binding_and_legacy_defaults(self):
        saved = self.save('one')
        raw = self.store._profile('one')
        secret = raw['secret_ref']
        self.store.assign_profile_group(['one'], 'G')
        self.assertEqual(self.store._profile('one')['secret_ref'], secret)
        self.assertEqual(self.store.connection_with_saved_password(self.request(saved))['password'], 'test-password')
        value = {k: saved[k] for k in ('host', 'port', 'user', 'name')}
        self.assertEqual(self.store.save('profile', value, 'one')['group'], 'G')
        self.assertEqual(self.store.save('profile', dict(value, group=' H '), 'one')['group'], 'H')
        with self.assertRaises(ValueError):
            self.store.save('profile', dict(value, host='other.example'), 'one')
        conn = self.store._connect()
        try:
            with conn:
                raw.pop('group', None)
                conn.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(raw), 'one'))
        finally:
            conn.close()
        self.assertEqual(self.store.get('profile', 'one')['group'], '')

    def test_import_cannot_supply_consent_or_identity(self):
        data = {'connections': {'db': {'provider': 'mysql', 'driver': 'mysql8', 'name': 'Imported',
            'group': 'production', 'topology_identity': {'server_uuid': 'forged'},
            'configuration': {'host': 'db.example', 'port': '3306', 'user': 'dba', 'group': 'production'}}}}
        path = Path(self.temp.name) / 'data-sources.json'
        path.write_text(json.dumps(data))
        with mock.patch.object(connection_import, 'default_path', return_value=path):
            result = ui.api_response('/api/connections/import', {'tool': 'dbeaver'})
        self.assertEqual(len(result['items']), 1)
        self.assertNotIn('group', result['items'][0])
        self.assertNotIn('topology_identity', result['items'][0])
        saved = connection_import.save_imported(self.store, result['items'])[0]
        self.assertEqual(saved['group'], '')
        with self.assertRaises(ValueError):
            ui.api_response('/api/connections/import', {'tool': 'dbeaver', 'group': 'production'})

    def test_identity_touches_only_opened_profile_and_never_accepts_save_payload(self):
        current, other = self.save('current', 'G'), self.save('other', 'G')
        before_current, before_other = self.store._profile('current'), self.store._profile('other')
        report = CF.report('ic-8.4-healthy')
        profiles, selected = cg.request_profiles(self.store, self.request(current))
        cg.finish_read(self.store, report, self.request(current), profiles, selected)
        identity = self.store._profile('current')['topology_identity']
        self.assertEqual(identity['cluster_id'], report['metadata']['cluster_id'])
        self.assertEqual(identity['gr_group_name'], report['cluster']['uuid'])
        self.assertEqual(identity['server_uuid'], report['server']['server_uuid'])
        self.assertIn('recorded_at', identity)
        after = self.store._profile('current')
        after.pop('topology_identity')
        self.assertEqual(after, before_current)
        self.assertEqual(self.store._profile('other'), before_other)
        self.assertNotIn('topology_identity', report)
        forged = dict(other, topology_identity={'server_uuid': 'forged'})
        self.store.save('profile', forged, 'other')
        self.assertNotIn('topology_identity', self.store._profile('other'))
        # An endpoint edit in flight invalidates the evidence, as well as consent.
        self.store.save('profile', dict(current, host='replacement'), 'current', password='replacement', remember=True)
        cg.finish_read(self.store, report, self.request(current), profiles, selected)
        self.assertNotIn('topology_identity', self.store._profile('current'))

    def test_endpoint_and_group_edits_revoke_prepared_login(self):
        for field, value in (('host', 'new-host'), ('group', 'other-group')):
            with self.subTest(field=field):
                current = self.save('current', 'G')
                peer = self.save('peer', 'G')
                opener = rp.profile_opener(self.store, 'current', current)
                fingerprint, connect = opener.prepare(peer)
                self.assertTrue(fingerprint)
                changed = dict(current)
                changed[field] = value
                self.store.save('profile', changed, 'current', password='changed', remember=True)
                with mock.patch('myflames.connector.MySQLConnection') as factory, self.assertRaises(ValueError):
                    connect()
                factory.assert_not_called()
        profiles, selected = cg.request_profiles(self.store, dict(self.request(current), host='edited'))
        self.assertIsNone(selected)
        self.assertEqual(cg.members(profiles, selected), [])


class TestEvidence(GroupCase):
    def test_all_evidence_types_ranked_with_join_without_logins(self):
        current = self.save('current', 'mine')
        report = {'kind': 'innodb_cluster', 'server': {'server_uuid': 'uuid-current'},
                  'cluster': {'uuid': 'gr', 'name': 'mfcluster'}, 'metadata': {'cluster_id': 'ic'},
                  'clusterset': {'id': 'cs', 'name': 'set'},
                  'channels': [{'source': {'uuid': 'source'}}],
                  'members': [{'host': 'address.example', 'port': 3306}]}
        identities = [('same_group_replication_group', {'gr_group_name': 'gr'}),
                      ('same_innodb_cluster', {'cluster_id': 'ic'}),
                      ('same_clusterset', {'clusterset_id': 'cs'}),
                      ('same_galera_cluster', {'wsrep_cluster_uuid': 'galera'}),
                      ('replication_source', {'server_uuid': 'source'}),
                      ('replica_of_this_server', {'source_uuids': ['uuid-current']}),
                      ('same_server', {'server_uuid': 'uuid-current'}),
                      ('address_named_by_this_server', {})]
        profiles = []
        for i, (evidence, identity) in enumerate(identities):
            profiles.append(dict(id=str(i), name='peer-' + str(i), type='production',
                                 host='address.example' if i == 7 else 'peer-' + str(i), port=3306,
                                 group='existing' if i in (1, 2) else '', topology_identity=identity))
        # Mixed identity is synthetic, solely to exercise the specified ranking.
        with mock.patch.object(rp, 'profile_opener', side_effect=AssertionError('suggestion login')):
            group = cg.describe(report, list(reversed(profiles)), current,
                                identity={'wsrep_cluster_uuid': 'galera'})
        self.assertEqual([p['evidence'] for p in group['suggestions']], [e for e, _ in identities])
        self.assertEqual(group['join'], [{'group': 'existing', 'evidence': 'same_innodb_cluster',
                                         'evidence_text': 'Same InnoDB Cluster (ic).'}])
        self.assertEqual(group['suggested_name'], 'mfcluster')
        self.assertEqual(group['read'], [])
        # An already-consenting peer and the current profile are not suggestions.
        self.assertEqual(cg.describe(report, [current, dict(profiles[0], group='mine')], current)['suggestions'], [])
        self.assertEqual(cg.describe(report, profiles, dict(current, group=''))['join'][0]['group'], 'existing')

    def test_names_by_kind_and_clusterset_id_stays_internal(self):
        cases = [({'kind': 'innodb_cluster', 'cluster': {'name': 'cluster'}, 'clusterset': {'name': 'set'}}, 'cluster'),
                 ({'clusterset': {'name': 'set'}}, 'set'),
                 ({'kind': 'galera', 'cluster': {'name': 'galera'}}, 'galera'),
                 ({'kind': 'pxc', 'cluster': {'name': 'pxc'}}, 'pxc'),
                 ({'channels': [{'source': {'host': 'source-db'}}]}, 'source-db replication'),
                 ({'is_proxysql': True}, 'Saved proxy'), ({'is_readyset': True}, 'Saved proxy'), ({}, '')]
        for report, name in cases:
            with self.subTest(report=report):
                self.assertEqual(cg.suggested_name(report, {'name': 'Saved proxy'}), name)
        identity = {}
        report = cluster.read_status(CF.conn('ic-8.4-clusterset-primary'), _identity=identity)
        self.assertTrue(identity['clusterset_id'])
        self.assertNotIn('id', report['clusterset'])
        self.assertNotIn('topology_identity', report)

    def test_addresses_routers_sources_replicas_proxy_readyset_and_loopback(self):
        fields = [({'members': [{'host': 'peer', 'port': 3306}]}),
                  {'metadata': {'instances': [{'endpoint': 'peer:3306'}]}},
                  {'routers': [{'hostname': 'peer', 'ports': {'rw': 3306}}]},
                  {'channels': [{'source': {'host': 'peer', 'port': 3306}}]},
                  {'replicas': [{'host': 'peer', 'port': 3306}]},
                  {'servers': [{'hostname': 'peer', 'port': 3306}]},
                  {'upstreams': [{'host': 'peer', 'port': 3306}]}]
        profile = dict(id='peer', name='peer', host='peer', port=3306, group='')
        for report in fields:
            with self.subTest(report=report):
                self.assertEqual(cg.describe(report, [profile])['suggestions'][0]['evidence'],
                                 'address_named_by_this_server')
        report = {'members': [{'host': '127.0.0.1', 'port': 3306}]}
        local = dict(profile, host='localhost')
        self.assertEqual(len(cg.describe(report, [local], observer_host='127.0.0.1')['suggestions']), 1)
        self.assertEqual(cg.describe(report, [local], observer_host='remote')['suggestions'], [])
        self.assertEqual(cg.describe(report, [dict(local, ssh_host='remote')], observer_host='127.0.0.1')['suggestions'], [])

    def test_client_footprint_and_performance_schema_off(self):
        conn = FakeProbeConn('database', connect_attrs=[['proxy-host:40123', 'program_name', 'ProxySQL'],
                                                       ['rs-host:49999', 'program_name', 'READYSET']])
        footprint = tl.read_live_client_footprint(conn)
        self.assertEqual(footprint['clients'], [{'program': 'ProxySQL', 'host': 'proxy-host:40123'},
                                                {'program': 'READYSET', 'host': 'rs-host:49999'}])
        self.assertIn('t.PROCESSLIST_ID = a.PROCESSLIST_ID', tl._LIVE_ATTRS_SQL)
        self.assertNotIn('a.THREAD_ID', tl._LIVE_ATTRS_SQL)
        profiles = [dict(id='px', name='ProxySQL', host='proxy-host', port=6032),
                    dict(id='rs', name='Readyset', host='rs-host', port=6036)]
        group = cg.describe({'topology_tiers': {'live_footprint': footprint}}, profiles)
        self.assertEqual([p['id'] for p in group['suggestions']], ['px', 'rs'])
        with mock.patch.object(conn, 'run', wraps=conn.run) as run:
            off = tl.read_live_client_footprint(conn, performance_schema=False)
        self.assertFalse(off['known'])
        self.assertEqual(len(off['notes']), 1)
        self.assertFalse(any('session_connect_attrs' in call[0][0] for call in run.call_args_list))


class TestReadConsent(GroupCase):
    def profiles(self, group='G'):
        current = dict(id='current', name='current', host='current-db', port=3306, group=group)
        inside = dict(id='inside', name='Database on its default port', host='db1.example.com', port=3306,
                      group='G', type='production', remember_password=True)
        outside = dict(id='outside', name='proxy', host='production.example', port=6033,
                       group='production', type='production', remember_password=True)
        same_address_outside = dict(inside, id='source-outside', group='production')
        return current, [outside, same_address_outside, inside, current]

    def run_path(self, path, profiles, current, opener):
        kwargs = dict(profiles=profiles, open_profile=opener, current_profile_id=current['id'] if current else None)
        if path == 'cluster':
            report = cluster.read_status(CF.conn('mysql-8.4-standalone'))
            return cluster.attach_topology_tiers(LocalDatabase(), report, **kwargs)
        if path == 'replication':
            return rp.read_status(LocalDatabase(), topology_tiers=True, **kwargs)
        if path == 'source_versions':
            report = {'engine': 'mysql', 'channels': [SV.channel()], 'capabilities': {}}
            return rp.attach_source_versions(SV.FakeReader(SV.DENIED), report, **kwargs)
        if path == 'proxysql':
            conn = FakeAdmin()
            try:
                return proxysql.read_status(conn, **kwargs)
            finally:
                conn.db.close()
        return readyset.read_status(FakeReadyset(), **kwargs)

    def test_all_readers_enforce_consent_even_for_production_proxy_port(self):
        for path in ('cluster', 'replication', 'source_versions', 'proxysql', 'readyset'):
            for mode in ('group', 'no_group', 'no_profile'):
                with self.subTest(path=path, mode=mode):
                    tl._reset_cache_for_tests()
                    rp.reset_source_cache()
                    current, profiles = self.profiles('' if mode == 'no_group' else 'G')
                    opened = []
                    def opener(profile):
                        self.assertEqual(profile['id'], 'inside', 'OUTSIDE GROUP LOGIN')
                        self.assertEqual(profile['group'], current['group'])
                        opened.append(profile['id'])
                        return SV.Source(server_id='7', version='8.4.6') if path == 'source_versions' else FakeProbeConn('database')
                    report = self.run_path(path, profiles, None if mode == 'no_profile' else current, opener)
                    self.assertEqual(opened, ['inside'] if mode == 'group' else [])
                    self.assertEqual([r['id'] for r in report['connection_group']['read']], opened)
                    self.assertEqual(report['connection_group']['name'], 'G' if mode == 'group' else '')
                    if opened:
                        self.assertEqual(report['connection_group']['read'][0]['outcome'], 'read')
                    self.assertNotIn('topology_identity', report)

    def test_budget_outcomes_and_notes_do_not_copy_sql_or_passwords(self):
        current, profiles = self.profiles()
        peers = [dict(profiles[2], id='peer-%d' % i, host='peer-%d' % i) for i in range(8)]
        peers.append(dict(profiles[2], id='no-password', remember_password=False))
        opened = []
        def opener(profile):
            opened.append(profile['id'])
            if profile['id'] == 'peer-1':
                raise ConnectorError('Query timed out after 2s: /*myflames:topology*/ SHOW GLOBAL VARIABLES secret')
            return FakeProbeConn('database')
        report = readyset.read_status(FakeReadyset(), profiles=[current] + peers, open_profile=opener,
                                     current_profile_id=current['id'])
        rows = {p['id']: p for p in report['connection_group']['read']}
        self.assertEqual(len(opened), tl.PROFILE_READ_LIMIT)
        self.assertEqual(rows['peer-0']['outcome'], 'read')
        self.assertEqual(rows['peer-1']['outcome'], 'failed')
        self.assertEqual(rows['peer-6']['outcome'], 'not_reached')
        self.assertEqual(rows['no-password']['outcome'], 'not_reached')
        self.assertEqual(rows['peer-1']['message'], 'Saved connection Database on its default port in group G could not be read: timed out after 2 s.')
        notes = json.dumps(rows) + json.dumps(report['topology']['notes'])
        for forbidden in ('SHOW GLOBAL VARIABLES', '/*myflames:', 'secret'):
            self.assertNotIn(forbidden, notes)
        tl._reset_cache_for_tests()
        clock = [0.0]
        opened[:] = []
        def slow(profile):
            opened.append(profile['id'])
            clock[0] += tl.PROFILE_BUDGET_S
            return FakeProbeConn('database')
        with mock.patch.object(tl.time, 'monotonic', side_effect=lambda: clock[0]):
            self.run_path('readyset', [current] + peers, current, slow)
        self.assertEqual(len(opened), 1)

    def test_source_version_budget_outcomes_and_client_error_sanitization(self):
        current, profiles = self.profiles()
        peers = [dict(profiles[2], id='peer-%d' % i) for i in range(rp.SOURCE_PROFILE_READS + 1)]
        peers.append(dict(profiles[2], id='no-password', remember_password=False))
        opened = []
        def opener(profile):
            opened.append(profile['id'])
            raise ConnectorError('Query timed out after 2s: SHOW GLOBAL VARIABLES password=secret')
        report = self.run_path('source_versions', [current] + peers, current, opener)
        rows = {p['id']: p for p in report['connection_group']['read']}
        self.assertEqual(len(opened), rp.SOURCE_PROFILE_READS)
        self.assertEqual(rows['peer-0']['outcome'], 'failed')
        self.assertEqual(rows['peer-%d' % rp.SOURCE_PROFILE_READS]['outcome'], 'not_reached')
        self.assertEqual(rows['no-password']['outcome'], 'not_reached')
        self.assertIn('timed out after 2 s.', rows['peer-0']['message'])
        self.assertNotIn('SHOW GLOBAL VARIABLES', json.dumps(report))
        self.assertNotIn('secret', json.dumps(report))

    def test_missing_unreadable_and_mock_store_cannot_authorize_logins(self):
        broken = mock.Mock()
        broken.list.side_effect = OSError('locked')
        for store in (None, broken, mock.Mock()):
            with self.subTest(store=store):
                self.assertEqual(cg.request_profiles(store, {'profile_id': 'current'}), ([], None))
                for path in ('cluster', 'replication', 'proxysql', 'readyset'):
                    conn = {'cluster': lambda: CF.conn('mysql-8.4-standalone'),
                            'replication': LocalDatabase, 'proxysql': FakeAdmin, 'readyset': FakeReadyset}[path]()
                    workspace = types.SimpleNamespace(store=store)
                    with mock.patch('myflames.connector.MySQLConnection', return_value=conn), \
                            mock.patch.object(rp, 'profile_opener', side_effect=AssertionError('saved login')):
                        report = ui.api_response('/api/live/' + path, {'connection': {'host': 'current-db'}}, workspace)
                    self.assertEqual(report['connection_group'], EMPTY)
                    if path == 'proxysql':
                        conn.db.close()

    def test_cli_replication_uses_only_selected_saved_group(self):
        current = self.save('current', 'G', host='current-db')
        inside = self.save('inside', 'G', host='inside.example')
        self.save('outside', 'production', name='proxy', host='production.example', port=6033)
        for mode in ('group', 'no_group', 'no_profile', 'edited'):
            with self.subTest(mode=mode):
                tl._reset_cache_for_tests()
                self.store.assign_profile_group(['current'], '' if mode == 'no_group' else 'G')
                opened = []
                def factory(**options):
                    if options['host'] in ('current-db', 'edited-db'):
                        return LocalDatabase(**options)
                    self.assertEqual(options['host'], 'inside.example', 'OUTSIDE GROUP CLI LOGIN')
                    opened.append(options['host'])
                    return FakeProbeConn('database')
                args = ['--json', '--profile', current['id']] if mode != 'no_profile' else ['--json', '--host', 'current-db']
                if mode == 'edited':
                    args += ['--host', 'edited-db', '--password', 'explicit']
                code, out, err, unused = run_cli('replication', args, LocalDatabase(), patches=[
                    mock.patch('myflames.connector.MySQLConnection', factory)])
                self.assertEqual(code, 0, err)
                self.assertEqual(opened, ['inside.example'] if mode == 'group' else [])
                report = json.loads(out)
                self.assertEqual([r['id'] for r in report['connection_group']['read']], ['inside'] if opened else [])
                self.assertNotIn('topology_identity', report)

    def test_place_requests_scope_openers_and_record_only_direct_identities(self):
        """Exercise request wiring as well as the lower-level scope guards."""
        current = self.save('current', 'G', host='current-db')
        self.save('inside', 'G', host='inside.example')
        self.save('outside', 'production', name='proxy', host='production.example', port=6033)
        workspace = types.SimpleNamespace(store=self.store)
        original_opener = rp.profile_opener
        for path in ('cluster', 'replication', 'proxysql', 'readyset'):
            for mode in ('group', 'no_group', 'no_profile', 'edited'):
                with self.subTest(path=path, mode=mode):
                    tl._reset_cache_for_tests()
                    self.store.assign_profile_group(['current'], '' if mode == 'no_group' else 'G')
                    current = self.store.get('profile', 'current')
                    connection = self.request(current)
                    if mode == 'no_profile':
                        connection.pop('profile_id')
                    if mode == 'edited':
                        connection.update(host='edited-db', password='explicit')
                    opened = []
                    local = {'cluster': lambda: CF.conn('mysql-8.4-standalone'),
                             'replication': LocalDatabase, 'proxysql': FakeAdmin, 'readyset': FakeReadyset}[path]()
                    def factory(**options):
                        if options['host'] in ('current-db', 'edited-db'):
                            return local
                        self.assertEqual(options['host'], 'inside.example', 'OUTSIDE GROUP REQUEST LOGIN')
                        opened.append(options['host'])
                        return FakeProbeConn('database')
                    def spy(store, pid, expected=None):
                        actual = original_opener(store, pid, expected)
                        def open_profile(profile):
                            self.assertEqual(profile['id'], 'inside', 'OUTSIDE GROUP OPENER')
                            return actual(profile)
                        return open_profile
                    try:
                        with mock.patch('myflames.connector.MySQLConnection', factory), \
                                mock.patch.object(rp, 'profile_opener', spy), \
                                mock.patch.object(self.store, 'record_topology_identity', wraps=self.store.record_topology_identity) as record:
                            report = ui.api_response('/api/live/' + path, {'connection': connection}, workspace)
                        self.assertEqual(opened, ['inside.example'] if mode == 'group' else [])
                        self.assertEqual([c[0][0] for c in record.call_args_list],
                                         ['current', 'inside'] if mode == 'group' else
                                         ['current'] if mode == 'no_group' else [])
                        self.assertNotIn('topology_identity', report)
                        if mode == 'group':
                            self.assertEqual(self.store._profile('inside')['topology_identity']['server_uuid'],
                                             '11111111-1111-1111-1111-111111111111')
                        self.assertNotIn('topology_identity', self.store._profile('outside'))
                    finally:
                        if path == 'proxysql':
                            local.db.close()

    def test_identity_write_failure_does_not_fail_place_read(self):
        current = self.save('current')
        with mock.patch('myflames.connector.MySQLConnection', return_value=LocalDatabase()), \
                mock.patch.object(self.store, 'record_topology_identity', side_effect=OSError('disk full')):
            report = ui.api_response('/api/live/replication', {'connection': self.request(current)},
                                     types.SimpleNamespace(store=self.store))
        self.assertTrue(report['available'])
        self.assertEqual(report['connection_group'], EMPTY)

    def test_store_becomes_unreadable_before_peer_login(self):
        current, peer = self.save('current', 'G'), self.save('peer', 'G')
        opener = rp.profile_opener(self.store, current['id'], current)
        with mock.patch.object(self.store, 'get', side_effect=OSError('locked')), \
                mock.patch('myflames.connector.MySQLConnection') as factory:
            report = readyset.read_status(FakeReadyset(), profiles=[current, peer],
                                         open_profile=opener, current_profile_id=current['id'])
        factory.assert_not_called()
        self.assertEqual(report['connection_group']['read'][0]['outcome'], 'not_reached')

    def test_replica_status_survives_missing_policy_store_without_a_write_token(self):
        from test_replication import fixture
        from _reviewed_replay import ReplayConn
        for workspace in (None, types.SimpleNamespace(store=None)):
            conn = ReplayConn(fixture('replication', 'mysql-8.4-gtid-error')['calls'])
            original = conn.run
            def run(sql, extra_flags=None, timeout=None):
                # The captured replica predates the optional reconnect read.
                if sql == rp.REPLICATION_TAG + rp._RECEIVER_SQL:
                    raise ConnectorError('ERROR 1142: SELECT command denied')
                return original(sql, extra_flags, timeout)
            conn.run = run
            with mock.patch('myflames.connector.MySQLConnection', return_value=conn):
                report = ui._replication_response('/api/live/replication', {'connection': {}}, workspace)
            self.assertTrue(report['replica'])
            self.assertEqual(report['connection_group']['name'], '')
            plans = [ch['skip_plan'] for ch in report['channels'] if ch.get('skip_plan')]
            self.assertTrue(plans)
            for plan in plans:
                self.assertFalse(plan['available'])
                self.assertNotIn('review_token', plan)

    def test_replication_identity_sink_and_strict_report_schemas(self):
        identity = {}
        report = rp.read_status(LocalDatabase(), topology_tiers=True, _identity=identity)
        self.assertEqual(identity['server_uuid'], 'uuid-current')
        self.assertNotIn('topology_identity', report)
        for name, report in (('replication', report), ('cluster', CF.report('ic-8.4-healthy')),
                             ('readyset', readyset.read_status(FakeReadyset()))):
            schema = json.loads((ROOT / 'docs' / 'schemas' / (name + '-1.0.json')).read_text())
            validate(report, schema)
            for change in ({'extra': True}, {'read': [{'id': 'x', 'name': 'x', 'type': 'production',
                                                     'message': '', 'outcome': 'unreviewed'}]}):
                bad = copy.deepcopy(report)
                bad['connection_group'].update(change)
                with self.assertRaises(AssertionError):
                    validate(bad, schema)


class TestPeerIdentityRecording(GroupCase):
    """Own-session evidence survives the existing bounded group probes."""
    def setUp(self):
        super(TestPeerIdentityRecording, self).setUp()
        self.profiles = [self.save('db-%d' % (i + 1), 'G', type='development') for i in range(3)]
        scenario = copy.deepcopy(CF.SCENARIOS['gr-8.4-two-members'])
        scenario['variables'].update(server_uuid=CF.SERVER_UUIDS[0], hostname='db-1')
        scenario['tables']['performance_schema.replication_group_members'] = [
            CF._gr_member(i, host='db-%d' % (i + 1)) for i in range(3)]
        scenario['tables']['performance_schema.replication_group_member_stats'] = [CF._gr_stats(i) for i in range(3)]
        self.local = CF.ScenarioConn(scenario)
        self.peers = {p['host']: FakeProbeConn('database', hostname=p['name'], uuid=CF.SERVER_UUIDS[i],
                                             gr_group='group', gr_members=[
                                                 [CF.SERVER_UUIDS[j], 'db-%d' % (j + 1), '3306', 'ONLINE', 'SECONDARY']
                                                 for j in range(3)])
                      for i, p in enumerate(self.profiles) if i}
        self.connection = dict(self.request(self.profiles[0]), group_name='G')
        self.workspace = types.SimpleNamespace(store=self.store)
        self.opened = []

    def read(self):
        def factory(**options):
            self.opened.append(options['host'])
            return self.local if options['host'] == self.profiles[0]['host'] else self.peers[options['host']]
        with mock.patch('myflames.connector.MySQLConnection', side_effect=factory):
            return ui.api_response('/api/live/cluster', {'connection': self.connection}, self.workspace)

    def test_group_cluster_read_records_both_peers_and_enables_routes_without_extra_probes(self):
        before = {p['id']: self.store._profile(p['id'], include_updated=True) for p in self.profiles}
        with mock.patch.object(self.store, 'record_topology_identity', wraps=self.store.record_topology_identity) as record:
            report = self.read()
        self.assertEqual(self.opened[0], self.profiles[0]['host'])
        self.assertCountEqual(self.opened, [p['host'] for p in self.profiles])
        self.assertEqual(len(tl._probe_attempts), 2)
        for i, profile in enumerate(self.profiles):
            after = self.store._profile(profile['id'], include_updated=True)
            self.assertEqual(after['topology_identity']['server_uuid'], CF.SERVER_UUIDS[i])
            after.pop('topology_identity')
            self.assertEqual(after, before[profile['id']])
        calls = {call.args[0]: call for call in record.call_args_list}
        for profile in self.profiles[1:]:
            self.assertEqual(calls[profile['id']].kwargs['expected'], profile)
        for index in (1, 2):
            member = next(m for m in report['members'] if m['uuid'] == CF.SERVER_UUIDS[index])
            action = next(a for a in member['actions'] if a['id'] == 'stop_member')
            self.assertTrue(action['enabled'], action)
            self.assertEqual(action['target']['profile_id'], self.profiles[index]['id'])
        # The evidence stays internal and cached reads do not open peers again.
        self.assertNotIn('topology_identity', json.dumps(report))
        self.assertFalse(any('identity' in read for read in report['connection_group']['read']))
        self.read()
        self.assertEqual(self.opened[-1], self.profiles[0]['host'])
        self.assertCountEqual(self.opened, [p['host'] for p in self.profiles] + [self.profiles[0]['host']])
        self.assertEqual(len(tl._probe_attempts), 2)

    def test_cluster_member_endpoints_need_no_probe_and_offer_identity_check_on_review(self):
        self.profiles = [self.store.save('profile', dict(p, host='db-%d' % (i + 1)), p['id'],
                                       password='test-password', remember=True)
                         for i, p in enumerate(self.profiles)]
        self.connection = dict(self.request(self.profiles[0]), group_name='G')
        report = self.read()
        # Tier discovery intentionally skips the cluster's own addresses.
        # Rendering their actions must not add any convenience logins.
        self.assertEqual(self.opened, ['db-1'])
        self.assertEqual(len(tl._probe_attempts), 0)
        for index in (1, 2):
            profile = self.profiles[index]
            self.assertNotIn('topology_identity', self.store._profile(profile['id']))
            member = next(m for m in report['members'] if m['uuid'] == CF.SERVER_UUIDS[index])
            action = next(a for a in member['actions'] if a['id'] == 'stop_member')
            self.assertTrue(action['enabled'], action)
            self.assertEqual(action['target']['profile_id'], profile['id'])
            self.assertIs(action['target']['confirm'], True)
            self.assertEqual(action['target']['note'], 'Review checks that %s (%s:3306) is this member.' %
                             (profile['name'], profile['host']))
            validate(report, json.loads((ROOT / 'docs/schemas/cluster-1.0.json').read_text(encoding='utf-8')))

    def test_wrong_backend_uuid_belongs_only_to_its_profile_and_duplicates_fail_closed(self):
        self.peers[self.profiles[1]['host']].cfg['uuid'] = CF.SERVER_UUIDS[2]
        report = self.read()
        self.assertEqual(self.store._profile('db-2')['topology_identity']['server_uuid'], CF.SERVER_UUIDS[2])
        self.assertEqual(self.store._profile('db-3')['topology_identity']['server_uuid'], CF.SERVER_UUIDS[2])
        self.assertEqual(self.store._profile('db-1')['topology_identity']['server_uuid'], CF.SERVER_UUIDS[0])
        for index in (1, 2):
            member = next(m for m in report['members'] if m['uuid'] == CF.SERVER_UUIDS[index])
            action = next(a for a in member['actions'] if a['id'] == 'stop_member')
            self.assertFalse(action['enabled'])
            self.assertIsNone(action['target']['profile_id'])
            if index == 2:
                self.assertEqual(action['why_not'], 'db-2 and db-3 both reach this member.')
                self.assertEqual(action['fix'],
                                 'A MySQL Router or ProxySQL connection reaches whichever server is primary. '
                                 'Connect to db-2 to act on this member, or keep only one of them in the group.')
            else:
                self.assertEqual(action['why_not'], 'Add a saved connection for db-2 to this group.')
        self.assertEqual(len(self.opened), 3)

    def test_ungrouped_read_never_records_peer_identities_or_opens_peers(self):
        self.store.assign_profile_group(['db-1'], '')
        self.connection.pop('group_name')
        self.read()
        self.assertEqual(self.opened, [self.profiles[0]['host']])
        for profile in self.profiles[1:]:
            self.assertNotIn('topology_identity', self.store._profile(profile['id']))
        self.assertEqual(len(tl._probe_attempts), 0)

    def test_galera_and_pxc_record_only_the_own_gcomm_uuid(self):
        for kind in ('galera', 'pxc'):
            with self.subTest(kind=kind):
                tl._reset_cache_for_tests()
                peer = self.peers[self.profiles[1]['host']]
                peer.cfg.update(wsrep=True, gcomm_uuid=CF.SERVER_UUIDS[1],
                                version_comment='Percona XtraDB Cluster' if kind == 'pxc' else 'MariaDB Server',
                                wsrep_members=[[CF.SERVER_UUIDS[2], 'other node', 'other:3306']])
                with mock.patch.object(peer, 'run', wraps=peer.run) as run:
                    self.read()
                identity = self.store._profile('db-2')['topology_identity']
                self.assertEqual(identity['wsrep_node_uuid'], CF.SERVER_UUIDS[1])
                self.assertNotIn('server_uuid', identity)
                self.assertEqual(sum(tl._WSREP_STATUS_SQL in call.args[0] for call in run.call_args_list), 1)
                self.assertEqual(len(tl._probe_attempts), 2)

    def test_endpoint_edit_or_group_removal_after_probe_prevents_persistence(self):
        for change in ('endpoint', 'group', 'active_group'):
            with self.subTest(change=change):
                tl._reset_cache_for_tests()
                for profile in self.profiles:
                    self.store.save('profile', profile, profile['id'], password='test-password', remember=True)
                original = cg.finish_read
                def finishing(store, report, connection, profiles, current, identity=None):
                    if change == 'endpoint':
                        profile = self.profiles[1]
                        store.save('profile', dict(profile, host='changed'), profile['id'], password='new', remember=True)
                    else:
                        store.assign_profile_group(['db-1' if change == 'active_group' else 'db-2'], '')
                    return original(store, report, connection, profiles, current, identity)
                with mock.patch.object(cg, 'finish_read', side_effect=finishing):
                    self.read()
                self.assertNotIn('topology_identity', self.store._profile('db-2'))

    def test_public_read_rows_cannot_supply_identity_evidence(self):
        current = self.profiles[0]
        forged = {'connection_group': {'read': [dict(id='db-2', name='db-2', type='development', outcome='read',
                                                    identity={'server_uuid': CF.SERVER_UUIDS[1]})]}}
        cg.finish_read(self.store, forged, self.connection, self.profiles, current)
        self.assertNotIn('topology_identity', self.store._profile('db-2'))


class TestReviewRegressions(GroupCase):
    """Regression and abuse cases for the 3.4.3 connection-group review."""

    def finish(self, profile, report, identity=None):
        profiles, current = cg.request_profiles(self.store, self.request(profile))
        cg.finish_read(self.store, report, self.request(profile), profiles, current, identity)
        return self.store._profile(profile['id']).get('topology_identity', {})

    def test_cluster_read_clears_departed_cluster_evidence(self):
        current = self.save('current')
        clustered = CF.report('ic-8.4-clusterset-primary')
        old = self.finish(current, clustered, {'clusterset_id': 'old-set'})
        self.assertTrue(old['cluster_id'])
        standalone = CF.report('mysql-8.4-standalone')
        standalone['server']['server_uuid'] = old['server_uuid']
        stored = self.finish(current, standalone)
        for field in ('cluster_kind', 'cluster_name', 'cluster_id', 'gr_group_name',
                      'wsrep_cluster_uuid', 'clusterset_id', 'clusterset_name'):
            self.assertTrue(field not in stored, field)
        suggestions = cg.describe(clustered, [self.store.get('profile', 'current')])['suggestions']
        self.assertFalse(any(row['evidence'] in ('same_group_replication_group',
                         'same_innodb_cluster', 'same_clusterset') for row in suggestions))

    def test_each_identity_view_preserves_uninspected_fields(self):
        current = self.save('current')
        clustered = CF.report('ic-8.4-healthy')
        initial = self.finish(current, clustered)
        replication = {'schema_version': 'replication-1.0', 'channels': [{'source': {'uuid': 'source'}}]}
        stored = self.finish(current, replication, {'server_uuid': initial['server_uuid']})
        self.assertEqual(stored['cluster_id'], initial['cluster_id'])
        self.assertEqual(stored['gr_group_name'], initial['gr_group_name'])
        self.assertEqual(stored['source_uuids'], ['source'])
        stored = self.finish(current, clustered)
        self.assertEqual(stored['source_uuids'], ['source'])
        stored = self.finish(current, dict(replication, channels=[]))
        self.assertEqual(stored.get('source_uuids', []), [])
        self.assertEqual(stored['cluster_id'], initial['cluster_id'])
        self.assertEqual(stored['gr_group_name'], initial['gr_group_name'])

    def test_changed_server_uuid_invalidates_uninspected_identity_fields(self):
        # MySQL-only: InnoDB Cluster and Group Replication identify the old server.
        current = self.save('current')
        clustered = CF.report('ic-8.4-clusterset-primary')
        initial = self.finish(current, clustered)
        self.assertTrue(initial['cluster_id'])
        self.assertTrue(initial['gr_group_name'])
        replication = {'schema_version': 'replication-1.0', 'channels': [{'source': {'uuid': 'new-source'}}]}
        stored = self.finish(current, replication, {'server_uuid': 'new-server-uuid'})
        self.assertEqual({key: value for key, value in stored.items() if key != 'recorded_at'},
                         {'server_uuid': 'new-server-uuid', 'source_uuids': ['new-source']})
        self.assertEqual(cg.describe(clustered, [self.store.get('profile', 'current')])['suggestions'], [])

    def test_pxc_binding_ignores_first_server_uuid_record_and_preserves_node_identity(self):
        current = self.save('current', group='G')
        report = CF.report('pxc-8.4-healthy')
        node_identity = cluster.target_server_identity(report)
        self.assertEqual(set(node_identity), {'wsrep_node_uuid'})
        self.assertTrue(node_identity['wsrep_node_uuid'])
        self.store.record_topology_identity(current['id'], node_identity)
        connection = dict(self.request(current), group_name=current['group'])
        before = ui._action_target_binding(self.store.get('profile', current['id']), connection, report['kind'])
        self.assertEqual(before['server_identity'], node_identity)
        self.store.record_topology_identity(current['id'], {'server_uuid': '11111111-2222-3333-4444-555555555555'})
        after = self.store.get('profile', current['id'])
        self.assertEqual(after['topology_identity']['wsrep_node_uuid'], node_identity['wsrep_node_uuid'])
        self.assertEqual(after['topology_identity']['server_uuid'], '11111111-2222-3333-4444-555555555555')
        self.assertEqual(ui._action_target_binding(after, connection, report['kind']), before)

    def test_read_replica_identity_uses_cluster_not_group_replication(self):
        # MySQL-only: MariaDB does not expose InnoDB Cluster read replicas.
        current = self.save('current')
        report = CF.report('ic-8.4-read-replica-local')
        self.assertEqual(report['metadata']['local_copy'], 'read_replica')
        stored = self.finish(current, report)
        self.assertNotIn('gr_group_name', stored)
        self.assertEqual(stored['cluster_id'], report['metadata']['cluster_id'])
        member = CF.report('ic-8.4-healthy')
        # The read-replica fixture is synthetic; the healthy recording has a
        # different metadata ID. Model a member of this replica's cluster.
        member['metadata']['cluster_id'] = report['metadata']['cluster_id']
        group = cg.describe(member, [self.store.get('profile', 'current')])
        self.assertEqual(group['suggestions'][0]['evidence'], 'same_innodb_cluster')

    def test_replication_does_not_record_configured_gr_group(self):
        # Loaded/configured GR alone does not prove membership. Cluster owns it.
        conn = LocalDatabase()
        conn.cfg['gr_group'] = 'configured-but-never-joined'
        identity = {}
        rp.read_status(conn, topology_tiers=True, _identity=identity)
        self.assertEqual(identity['server_uuid'], 'uuid-current')
        self.assertNotIn('gr_group_name', identity)

    def test_proxy_reports_never_store_backend_identity(self):
        hints = ({'is_proxysql': True}, {'is_readyset': True},
                 {'server_product': 'ProxySQL Admin'}, {'server_product': 'Readyset'},
                 {'flavor': 'readyset'}, {'readyset_hint': True})
        for i, hint in enumerate(hints):
            for schema in ('cluster-1.0', 'replication-1.0'):
                with self.subTest(hint=hint, schema=schema):
                    current = self.save('proxy-%s-%s' % (i, schema))
                    report = dict(schema_version=schema, server={'server_uuid': 'backend-uuid'},
                                  channels=[{'source': {'uuid': 'backend-source'}}],
                                  kind='innodb_cluster', cluster={'uuid': 'backend-group'}, **hint)
                    stored = self.finish(current, report, {'server_uuid': 'backend-uuid'})
                    self.assertEqual(stored, {})

    def test_oversized_identity_fields_and_source_list_are_refused(self):
        current = self.save('current')
        huge = 'x' * 200000
        report = {'schema_version': 'cluster-1.0', 'kind': 'innodb_cluster',
                  'server': {'server_uuid': huge}, 'cluster': {'name': huge, 'uuid': huge},
                  'metadata': {'cluster_id': huge}, 'clusterset': {'id': huge, 'name': huge},
                  'channels': [{'source': {'uuid': 'source-%d' % i}} for i in range(50)]}
        stored = self.finish(current, report, {'wsrep_cluster_uuid': huge})
        for field in ('server_uuid', 'cluster_name', 'cluster_id', 'gr_group_name',
                      'clusterset_id', 'clusterset_name', 'wsrep_cluster_uuid', 'source_uuids'):
            self.assertNotIn(field, stored, field)
        self.assertLess(len(json.dumps(stored)), 512)

    def test_identity_limits_do_not_truncate_or_keep_previous_invalid_values(self):
        current = self.save('current')
        accepted = {field: 'a' * 128 for field in cg.IDENTITY_FIELDS}
        accepted['source_uuids'] = ['uuid-%d' % i for i in range(32)]
        self.store.record_topology_identity(current['id'], accepted)
        stored = self.store._profile(current['id'])['topology_identity']
        for field, value in accepted.items():
            self.assertEqual(stored[field], value)
        for sources in (['x' * 129], ['uuid-%d' % i for i in range(33)], 'not-a-list', None):
            invalid = {field: 'a' * 129 for field in cg.IDENTITY_FIELDS}
            invalid['source_uuids'] = sources
            self.store.record_topology_identity(current['id'], invalid)
            stored = self.store._profile(current['id'])['topology_identity']
            self.assertFalse(set(stored) & set(accepted), stored)

    def test_each_probe_login_rechecks_consent_endpoint_and_password(self):
        for change in ('peer_group', 'current_group', 'host', 'user', 'ssh_host', 'password', 'store_error'):
            with self.subTest(change=change):
                tl._reset_cache_for_tests()
                current, peer = self.save('current', 'G'), self.save('peer', 'G')
                scope = cg.GroupScope([current, peer], current)
                opener = scope.opener(rp.profile_opener(self.store, current['id'], current))
                calls, store_error = [], []
                fake = FakeProbeConn('database')
                def invoke(argv, **kwargs):
                    sql = argv[argv.index('-e') + 1]
                    calls.append(sql)
                    answer = fake.run(sql)
                    if len(calls) == 1:
                        if change.endswith('_group'):
                            self.store.assign_profile_group([change.split('_')[0]], 'revoked')
                        elif change == 'store_error':
                            patcher = mock.patch.object(self.store, '_profile', side_effect=OSError('unreadable'))
                            patcher.start()
                            store_error.append(patcher)
                        else:
                            changed = dict(peer)
                            if change != 'password':
                                changed[change] = 'changed'
                            self.store.save('profile', changed, peer['id'], password='new-password', remember=True)
                    return types.SimpleNamespace(returncode=0, stdout=answer.encode(), stderr=b'')
                try:
                    with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                            mock.patch.object(connector, '_run_bounded', side_effect=invoke):
                        probed, notes = tl.probe_saved_profiles([peer], opener, skip_profile_id=current['id'], current=current)
                finally:
                    for patcher in store_error:
                        patcher.stop()
                self.assertEqual(len(calls), 1, 'a revoked probe started another client login')
                self.assertEqual(probed, [])
                self.assertEqual(scope.read[0]['outcome'], 'failed')
                self.assertEqual(len(tl._probe_attempts), 1, 'a sent login must retain its attempt slot')
                self.assertTrue(notes)
                for forbidden in ('SELECT', 'SHOW', 'test-password', 'new-password'):
                    self.assertNotIn(forbidden, json.dumps(notes))

    def test_setup_revocation_refuses_probe_login(self):
        current, peer = self.save('current', 'G'), self.save('peer', 'G')
        scope = cg.GroupScope([current, peer], current)
        opener = scope.opener(rp.profile_opener(self.store, current['id'], current))
        create = connector.MySQLConnection._create_defaults_file
        def revoke(conn, *args, **kwargs):
            path = create(conn, *args, **kwargs)
            self.store.assign_profile_group(['peer'], 'revoked')
            return path
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector.MySQLConnection, '_create_defaults_file', revoke), \
                mock.patch.object(connector, '_run_bounded') as invoke:
            probed, notes = tl.probe_saved_profiles([peer], opener, skip_profile_id=current['id'], current=current)
        invoke.assert_not_called()
        self.assertEqual(probed, [])
        self.assertEqual(scope.read[0]['outcome'], 'not_reached')
        self.assertEqual(tl._probe_attempts, set())
        self.assertTrue(notes)

    def test_source_first_contact_revocation_is_failed_after_one_login(self):
        for engine, version, comment in (('mysql', '8.4.11', 'MySQL Community Server - GPL'),
                                         ('mariadb', '11.4.8-MariaDB', 'MariaDB Server')):
            with self.subTest(engine=engine):
                rp.reset_source_cache()
                current = self.save('current', 'G')
                peer = self.save('peer', 'G', host='db1.example.com')
                scope = cg.GroupScope([current, peer], current)
                opener = scope.opener(rp.profile_opener(self.store, current['id'], current))
                # Client selection may log in before the source-variable read.
                # Exercise both logins through the real connector consent checks.
                def resolve(conn):
                    conn._run_with_binary('SELECT @@version, @@version_comment', '/nonexistent/mysql')
                    return '/nonexistent/mysql'
                def invoke(argv, **kwargs):
                    self.assertEqual(argv[argv.index('-e') + 1], 'SELECT @@version, @@version_comment')
                    self.store.assign_profile_group(['peer'], 'revoked')
                    return types.SimpleNamespace(returncode=0,
                        stdout=('%s\t%s\n' % (version, comment)).encode(), stderr=b'')
                with mock.patch.object(connector.MySQLConnection, '_resolve_binary', resolve), \
                        mock.patch.object(connector, '_run_bounded', side_effect=invoke) as client:
                    report = rp.attach_source_versions(SV.FakeReader(SV.DENIED),
                        {'engine': engine, 'channels': [SV.channel()], 'capabilities': {}},
                        profiles=[current, peer], open_profile=opener, current_profile_id=current['id'])
                self.assertEqual(client.call_count, 1, 'a revoked source started another client login')
                self.assertEqual(report['connection_group']['read'][0]['outcome'], 'failed')
                self.assertIn(peer['id'], scope.attempted)
                self.assertEqual(len(rp._source_attempts), 1, 'a sent login must retain its attempt slot')
                self.assertIsNone(report['channels'][0]['source_version']['version'])
                self.assertFalse(report['channels'][0]['source_version']['verified'])

    def test_consented_mysql_and_mariadb_probes_complete(self):
        for version, comment in (('8.4.11', 'MySQL Community Server - GPL'),
                                 ('11.4.8-MariaDB', 'MariaDB Server')):
            with self.subTest(version=version):
                tl._reset_cache_for_tests()
                current, peer = self.save('current', 'G'), self.save('peer', 'G')
                scope = cg.GroupScope([current, peer], current)
                opener = scope.opener(rp.profile_opener(self.store, current['id'], current))
                fake = FakeProbeConn('database', version=version, version_comment=comment)
                def invoke(argv, **kwargs):
                    answer = fake.run(argv[argv.index('-e') + 1])
                    return types.SimpleNamespace(returncode=0, stdout=answer.encode(), stderr=b'')
                with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                        mock.patch.object(connector, '_run_bounded', side_effect=invoke) as client:
                    probed, notes = tl.probe_saved_profiles([peer], opener, skip_profile_id=current['id'], current=current)
                self.assertEqual(probed[0]['version'], version)
                self.assertEqual(scope.read[0]['outcome'], 'read')
                self.assertGreater(client.call_count, 1)
                self.assertEqual(notes, [])

    def test_client_script_and_write_paths_also_recheck_group_consent(self):
        # No SQL runs: exercise all connector entry points at the login boundary.
        for method in ('run', 'run_script', 'run_write'):
            with self.subTest(method=method):
                current, peer = self.save('current', 'G'), self.save('peer', 'G')
                conn = rp.profile_opener(self.store, current['id'], current)(peer)
                self.store.assign_profile_group(['peer'], 'revoked')
                with conn, mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                        mock.patch.object(connector, '_run_bounded', return_value=types.SimpleNamespace(
                            returncode=0, stdout=b'', stderr=b'')) as invoke:
                    with self.assertRaises(cg.ProfileReadError):
                        getattr(conn, method)('SELECT 1')
                invoke.assert_not_called()

    def test_setup_revocation_refuses_source_login(self):
        current = self.save('current', 'G')
        peer = self.save('peer', 'G', host='db1.example.com')
        opener = rp.profile_opener(self.store, current['id'], current)
        create = connector.MySQLConnection._create_defaults_file
        def revoke(conn, *args, **kwargs):
            path = create(conn, *args, **kwargs)
            self.store.assign_profile_group(['peer'], 'revoked')
            return path
        with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector.MySQLConnection, '_create_defaults_file', revoke), \
                mock.patch.object(connector, '_run_bounded') as invoke:
            report = rp.attach_source_versions(SV.FakeReader(SV.DENIED),
                {'engine': 'mysql', 'channels': [SV.channel()], 'capabilities': {}},
                profiles=[current, peer], open_profile=opener, current_profile_id=current['id'])
        invoke.assert_not_called()
        self.assertEqual(report['connection_group']['read'][0]['outcome'], 'not_reached')
        self.assertEqual(rp._source_attempts, set())

    def test_pending_probe_checks_consent_before_waiting(self):
        self.check_pending_consent('topology')

    def test_pending_source_checks_consent_before_waiting(self):
        self.check_pending_consent('source')

    def check_pending_consent(self, path):
        current = self.save('current', 'G')
        peer = self.save('peer', 'G', host='db1.example.com')
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        results, errors = {}, []
        base = SV.Source(server_id='7', version='8.4.6') if path == 'source' else FakeProbeConn('database')
        original_run = base.run
        def block(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test did not release the first probe')
            return original_run(*args, **kwargs)
        base.run = block
        def read(which):
            try:
                opener = rp.profile_opener(self.store, current['id'], current)
                if path == 'source':
                    report = {'engine': 'mysql', 'channels': [SV.channel()], 'capabilities': {}}
                    results[which] = rp.attach_source_versions(SV.FakeReader(SV.DENIED), report,
                        profiles=[current, peer], open_profile=opener, current_profile_id=current['id'])['connection_group']
                else:
                    scope = cg.GroupScope([current, peer], current)
                    tl.probe_saved_profiles([peer], scope.opener(opener), skip_profile_id=current['id'], current=current)
                    results[which] = {'read': scope.read}
            except BaseException as exc:
                errors.append(exc)
            finally:
                if which == 'second':
                    second_done.set()
        first = threading.Thread(target=read, args=('first',))
        second = threading.Thread(target=read, args=('second',))
        with mock.patch.object(connector, 'MySQLConnection', return_value=base) as factory:
            first.start()
            try:
                self.assertTrue(entered.wait(2), 'first probe never entered the client')
                self.store.assign_profile_group(['peer'], 'revoked')
                second.start()
                self.assertTrue(second_done.wait(2), 'revoked reader waited for an unauthorized pending probe')
                self.assertEqual(results['second']['read'][0]['outcome'], 'not_reached')
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
            self.assertEqual(factory.call_count, 1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])

    def test_group_reassigned_during_password_lookup_never_constructs_client(self):
        current, peer = self.save('current', 'G'), self.save('peer', 'G')
        original = self.store.connection_with_saved_password
        def revoke(value):
            result = original(value)
            self.store.assign_profile_group(['peer'], 'revoked')
            return result
        opener = rp.profile_opener(self.store, current['id'], current)
        with mock.patch.object(self.store, 'connection_with_saved_password', side_effect=revoke), \
                mock.patch.object(connector, 'MySQLConnection') as factory:
            with self.assertRaises(cg.ProfileReadError):
                opener(peer)
        factory.assert_not_called()

    def test_payload_cannot_forge_group_or_current_profile_consent(self):
        current = self.save('current', '')
        self.save('peer', 'G')
        forged = dict(current, group='G')
        workspace = types.SimpleNamespace(store=self.store)
        for path in ('cluster', 'replication', 'proxysql', 'readyset'):
            for key in ('group', 'connection_current', 'top_current', 'all'):
                with self.subTest(path=path, key=key):
                    connection = self.request(current)
                    payload = {'connection': connection}
                    if key in ('group', 'all'):
                        connection['group'] = 'G'
                    if key in ('connection_current', 'all'):
                        connection['current_profile'] = forged
                    if key in ('top_current', 'all'):
                        payload['current_profile'] = forged
                    conn = {'cluster': lambda: CF.conn('mysql-8.4-standalone'),
                            'replication': LocalDatabase, 'proxysql': FakeAdmin, 'readyset': FakeReadyset}[path]()
                    try:
                        with mock.patch.object(connector, 'MySQLConnection', return_value=conn) as factory, \
                                mock.patch.object(rp, 'profile_opener', side_effect=AssertionError('forged consent')):
                            report = ui.api_response('/api/live/' + path, payload, workspace)
                        self.assertEqual(factory.call_count, 1, 'forged consent opened a saved peer')
                        self.assertEqual(report['connection_group']['read'], [])
                        self.assertEqual(report['connection_group']['name'], '')
                    finally:
                        if path == 'proxysql':
                            conn.db.close()

    def test_user_or_ssh_host_mismatch_cannot_borrow_group(self):
        current = self.save('current', 'G', ssh_host='jump.example')
        self.save('peer', 'G')
        for field in ('user', 'ssh_host'):
            with self.subTest(field=field):
                edited = self.request(current)
                edited[field] = 'different'
                profiles, selected = cg.request_profiles(self.store, edited)
                self.assertIsNone(selected)
                self.assertEqual(cg.members(profiles, selected), [])

    def test_group_api_refuses_non_profile_id_atomically(self):
        self.save('current', 'G')
        from test_workspace_store import bundle
        record = self.store.save('investigation', bundle(), 'not-a-profile')
        profiles = self.store.list('profile')
        with self.assertRaises(ValueError):
            ui.api_response('/api/profiles/group', {'profile_ids': ['current', record['id']], 'group': 'changed'},
                            types.SimpleNamespace(store=self.store))
        self.assertEqual(self.store.list('profile'), profiles)
        self.assertEqual(self.store.get('investigation', record['id']), record)

    def test_mcp_topology_tools_open_only_selected_connection(self):
        current = self.save('current', 'G')
        self.save('peer', 'G')
        for name, conn in (('cluster', CF.conn('mysql-8.4-standalone')),
                           ('replication', LocalDatabase()), ('readyset', FakeReadyset())):
            with self.subTest(name=name):
                with mock.patch.object(connector, 'MySQLConnection', return_value=conn) as factory, \
                        mock.patch.object(rp, 'profile_opener', side_effect=AssertionError('MCP peer login')):
                    report = getattr(mcp_server, name + '_status_tool')(current['id'], detail='full')
                self.assertEqual(factory.call_count, 1)
                self.assertEqual(factory.call_args[1]['host'], current['host'])
                self.assertEqual(report['connection_group']['read'], [])

    def test_readyset_cli_reads_only_selected_group_members(self):
        current = self.save('current', 'G', host='current-rs')
        self.save('inside', 'G', host='inside.example')
        self.save('outside', 'production', host='outside.example')
        for mode in ('group', 'no_group', 'no_profile', 'edited'):
            with self.subTest(mode=mode):
                tl._reset_cache_for_tests()
                self.store.assign_profile_group(['current'], '' if mode == 'no_group' else 'G')
                opened = []
                def factory(**options):
                    if options['host'] in ('current-rs', 'edited-rs'):
                        conn = FakeReadyset()
                        for key in ENDPOINT_FIELDS:
                            setattr(conn, key, options.get(key))
                        return conn
                    opened.append(options['host'])
                    self.assertEqual(options['host'], 'inside.example', 'OUTSIDE GROUP CLI LOGIN')
                    return FakeProbeConn('database')
                args = ['--json', '--profile', current['id']] if mode != 'no_profile' else ['--json', '--host', 'current-rs']
                if mode == 'edited':
                    args += ['--host', 'edited-rs', '--password', 'explicit']
                code, out, err, unused = run_cli('readyset', args, FakeReadyset(), patches=[
                    mock.patch.object(connector, 'MySQLConnection', side_effect=factory)])
                self.assertEqual(code, 0, err)
                self.assertEqual(opened, ['inside.example'] if mode == 'group' else [])
                report = json.loads(out)
                self.assertEqual([r['id'] for r in report['connection_group']['read']], ['inside'] if opened else [])

    def test_save_and_delete_connection_group_rename_add_remove_and_cap(self):
        from myflames import secret_store
        p1 = self.save('prof-a')
        p2 = self.save('prof-b')
        p3 = self.save('prof-c', type='development')
        other = self.save('prof-other', 'other-group')
        workspace = types.SimpleNamespace(store=self.store)

        # Create group with two members.
        res = ui.api_response('/api/profiles/groups/save',
                              {'name': '  alpha  ', 'profile_ids': ['prof-a', 'prof-b']}, workspace)
        self.assertEqual(res['name'], 'alpha')
        self.assertEqual(res['profile_ids'], ['prof-a', 'prof-b'])
        groups = {g['name']: g for g in res['groups']}
        self.assertEqual(groups['alpha']['profile_ids'], ['prof-a', 'prof-b'])
        self.assertEqual(groups['alpha']['type'], 'production')
        self.assertEqual(groups['other-group']['profile_ids'], ['prof-other'])
        for blob in (json.dumps(res['groups']), json.dumps(res['profiles'])):
            self.assertNotIn('test-password', blob)
            self.assertNotIn('secret_ref', blob)

        # Edit group (add prof-c, remove prof-b) and rename alpha -> beta.
        res = ui.api_response('/api/profiles/groups/save',
                              {'name': 'beta', 'previous_name': 'alpha', 'profile_ids': ['prof-a', 'prof-c']},
                              workspace)
        self.assertEqual(res['name'], 'beta')
        groups = {g['name']: g for g in res['groups']}
        self.assertNotIn('alpha', groups)
        self.assertEqual(groups['beta']['profile_ids'], ['prof-a', 'prof-c'])
        self.assertEqual(self.store.get('profile', 'prof-a')['group'], 'beta')
        self.assertEqual(self.store.get('profile', 'prof-b')['group'], '')
        self.assertEqual(self.store.get('profile', 'prof-c')['group'], 'beta')

        # Renaming or creating with a name already used by another group is refused.
        with self.assertRaisesRegex(ValueError, 'already exists'):
            ui.api_response('/api/profiles/groups/save',
                            {'name': 'other-group', 'previous_name': 'beta', 'profile_ids': ['prof-a']}, workspace)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            ui.api_response('/api/profiles/groups/save',
                            {'name': 'other-group', 'profile_ids': ['prof-b']}, workspace)
        # Saving the same group name (with or without previous_name) succeeds.
        res = ui.api_response('/api/profiles/groups/save',
                              {'name': 'beta', 'previous_name': 'beta', 'profile_ids': ['prof-a', 'prof-b']}, workspace)
        self.assertEqual({p['id'] for p in res['profiles'] if p['group'] == 'beta'}, {'prof-a', 'prof-b'})
        res = ui.api_response('/api/profiles/groups/save',
                              {'name': 'beta', 'profile_ids': ['prof-a', 'prof-c']}, workspace)
        self.assertEqual({p['id'] for p in res['profiles'] if p['group'] == 'beta'}, {'prof-a', 'prof-c'})

        # 16-member cap: 16 succeeds, 17 or 20 is refused atomically.
        many_ids = []
        for i in range(20):
            pid = 'member-%02d' % i
            self.save(pid)
            many_ids.append(pid)
        res16 = ui.api_response('/api/profiles/groups/save',
                                {'name': 'cap-group', 'profile_ids': many_ids[:16]}, workspace)
        self.assertEqual(len(res16['profile_ids']), 16)
        for bad_count in (17, 20):
            with self.subTest(bad_count=bad_count):
                with self.assertRaisesRegex(ValueError, '16'):
                    ui.api_response('/api/profiles/groups/save',
                                    {'name': 'cap-group', 'previous_name': 'cap-group',
                                     'profile_ids': many_ids[:bad_count]}, workspace)
        self.assertEqual({p['id'] for p in self.store.list('profile') if p['group'] == 'cap-group'},
                         set(many_ids[:16]))

        # Delete group: clears group on members, keeps every profile and its saved password.
        del_res = ui.api_response('/api/profiles/groups/delete', {'name': 'beta'}, workspace)
        self.assertTrue(del_res['deleted'])
        self.assertNotIn('beta', {g['name'] for g in del_res['groups']})
        for pid, orig in (('prof-a', p1), ('prof-b', p2), ('prof-c', p3)):
            self.assertEqual(self.store.get('profile', pid)['group'], '')
            self.assertTrue(self.store.get('profile', pid)['has_saved_password'])
            self.assertEqual(self.store.connection_with_saved_password(self.request(orig))['password'], 'test-password')
        self.assertEqual(self.store.get('profile', other['id'])['group'], 'other-group')

    def test_abuse_f3_group_endpoints_strict_schema_and_allowlist(self):
        import http.client
        prof_a = self.save('prof-a', 'g')
        raw_before = self.store._profile('prof-a')
        workspace = types.SimpleNamespace(store=self.store)

        # Direct API calls with id or value keys are refused and leave prof-a and its password untouched.
        bad_payloads = [
            ('/api/profiles/groups/delete', {'name': 'g', 'id': 'prof-a'}),
            ('/api/profiles/groups/delete', {'id': 'prof-a'}),
            ('/api/profiles/groups/delete', {}),
            ('/api/profiles/groups/delete', {'name': ''}),
            ('/api/profiles/groups/delete', {'name': 'x' * 81}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': ['prof-a'], 'id': 'prof-a'}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': ['prof-a'], 'value': {'host': 'evil.example'}}),
            ('/api/profiles/groups/save', {'id': 'prof-a', 'value': {'host': 'evil.example'}}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': 'prof-a'}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': []}),
            ('/api/profiles/groups/save', {'name': 'x' * 81, 'profile_ids': ['prof-a']}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': ['prof-a'], 'previous_name': 123}),
            ('/api/profiles/groups/save', {'name': 'g', 'profile_ids': ['prof-a'], 'previous_name': 'x' * 81}),
        ]
        for path, payload in bad_payloads:
            with self.subTest(path=path, payload=payload):
                with self.assertRaises(ValueError):
                    ui.api_response(path, payload, workspace)
                self.assertEqual(self.store._profile('prof-a'), raw_before)
                self.assertEqual(self.store.connection_with_saved_password(self.request(prof_a))['password'],
                                 'test-password')

        # HTTP server dispatch and allowlist verification.
        server = ui.WorkspaceServer(store_path=str(self.store.path))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, payload):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request('POST', path, body=json.dumps(payload),
                             headers={'Content-Type': 'application/json', 'X-Myflames-Token': server.token,
                                      'Origin': server.origin,
                                      'Cookie': server.cookie_name + '=' + server.session_secret})
                response = conn.getresponse()
                return response.status, json.loads(response.read().decode('utf-8'))
            finally:
                conn.close()

        status, body = post('/api/profiles/groups/delete', {'name': 'g', 'id': 'prof-a'})
        self.assertEqual(status, 400, body)
        status, body = post('/api/profiles/groups/save',
                            {'name': 'g', 'profile_ids': ['prof-a'], 'id': 'prof-a', 'value': {'host': 'evil'}})
        self.assertEqual(status, 400, body)
        self.assertEqual(self.store._profile('prof-a'), raw_before)
        self.assertEqual(self.store.connection_with_saved_password(self.request(prof_a))['password'], 'test-password')

        for unallowed in ('/api/profiles/groups', '/api/profiles/groups/other',
                          '/api/profiles/other/save', '/api/profiles/other/delete'):
            with self.subTest(unallowed=unallowed):
                status, body = post(unallowed, {'name': 'g'})
                self.assertEqual(status, 404, body)

    def test_abuse_f4_per_login_consent_flips_and_second_store_instance(self):
        # 1. Group field flips between prepare and connect (including with group_name).
        for edit_kind in ('save_group_remove', 'delete_group', 'flip_adapter'):
            with self.subTest(stage='prepare_to_connect', edit_kind=edit_kind):
                current = self.save('current', 'G')
                peer = self.save('peer', 'G')
                req_conn = dict(self.request(current), group_name='G')
                opener = rp.profile_opener(self.store, current['id'], current, group_name='G', connection=req_conn)
                fingerprint, connect = opener.prepare(peer)
                self.assertTrue(fingerprint)
                if edit_kind == 'save_group_remove':
                    self.store.save_connection_group('G', [current['id']], previous_name='G')
                elif edit_kind == 'delete_group':
                    self.store.delete_connection_group('G')
                else:
                    real_get = self.store.get
                    self.store.get = lambda kind, key: (
                        dict(real_get(kind, key), group='') if key == 'peer' else real_get(kind, key))
                try:
                    with mock.patch.object(connector, 'MySQLConnection') as factory, \
                            self.assertRaises(cg.ProfileReadError):
                        connect()
                    factory.assert_not_called()
                finally:
                    if edit_kind == 'flip_adapter':
                        del self.store.get

        # 2. Group field flips between two statements of one probe.
        for edit_kind in ('save_group_remove', 'delete_group', 'second_store_instance'):
            with self.subTest(stage='mid_probe', edit_kind=edit_kind):
                tl._reset_cache_for_tests()
                current = self.save('current', 'G')
                peer = self.save('peer', 'G')
                second_store = WorkspaceStore(self.store.path)
                req_conn = dict(self.request(current), group_name='G')
                scope = cg.GroupScope([current, peer], current)
                opener = scope.opener(rp.profile_opener(
                    self.store, current['id'], current, group_name='G', connection=req_conn))
                calls = []
                fake = FakeProbeConn('database')

                def invoke(argv, **kwargs):
                    sql = argv[argv.index('-e') + 1]
                    calls.append(sql)
                    answer = fake.run(sql)
                    if len(calls) == 1:
                        if edit_kind == 'save_group_remove':
                            self.store.save_connection_group('G', [current['id']], previous_name='G')
                        elif edit_kind == 'delete_group':
                            self.store.delete_connection_group('G')
                        else:
                            second_store.save_connection_group('G', [current['id']], previous_name='G')
                    return types.SimpleNamespace(returncode=0, stdout=answer.encode(), stderr=b'')

                with mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                        mock.patch.object(connector, '_run_bounded', side_effect=invoke):
                    probed, notes = tl.probe_saved_profiles([peer], opener, skip_profile_id=current['id'], current=current)
                self.assertEqual(len(calls), 1, 'revoked member logged in for a second statement')
                self.assertEqual(probed, [])
                self.assertEqual(scope.read[0]['outcome'], 'failed')
                self.assertTrue(notes)

        # 3. Second WorkspaceStore instance on the same file removes a member; next login in first instance is refused.
        current = self.save('current', 'G')
        peer = self.save('peer', 'G')
        store1 = WorkspaceStore(self.store.path)
        store2 = WorkspaceStore(self.store.path)
        req_conn = dict(self.request(current), group_name='G')
        opener1 = rp.profile_opener(store1, current['id'], current, group_name='G', connection=req_conn)
        conn1 = opener1(peer)
        store2.save_connection_group('G', [current['id']], previous_name='G')
        with conn1, mock.patch.object(connector.MySQLConnection, '_resolve_binary', return_value='/nonexistent/mysql'), \
                mock.patch.object(connector, '_run_bounded') as invoke:
            with self.assertRaises(cg.ProfileReadError):
                conn1.run('SELECT 1')
            invoke.assert_not_called()
        with mock.patch.object(connector, 'MySQLConnection') as factory, \
                self.assertRaises(cg.ProfileReadError):
            opener1(peer)
        factory.assert_not_called()

    def test_abuse_f9_refuses_invalid_targets_and_edited_active_member_with_zero_password_lookups(self):
        from myflames import secret_store
        from test_workspace_store import bundle
        current = self.save('current', 'G')
        valid_peer = self.save('peer', 'G')
        other_group_peer = self.save('other-peer', 'H')
        no_pass_peer = self.store.save(
            'profile', {'name': 'no-pass', 'host': 'nopass.example', 'port': 3306, 'user': 'dba', 'group': 'G'},
            'no-pass', remember=False)
        investigation = self.store.save('investigation', bundle(), 'inv-record')

        lookups = []
        real_get_secret = secret_store.get_secret

        def counting_get_secret(key, backend=None):
            lookups.append((key, backend))
            return real_get_secret(key, backend)

        with mock.patch.object(secret_store, 'get_secret', side_effect=counting_get_secret):
            # 1. Active member with edited host + valid target_profile_id -> refused, 0 lookups.
            edited_conn = dict(self.request(current), host='edited.example', group_name='G')
            _, req_cur = cg.request_profiles(self.store, edited_conn)
            self.assertIsNone(req_cur)
            bad_opener = rp.profile_opener(self.store, current['id'], edited_conn, group_name='G', connection=edited_conn)
            for target in (valid_peer, valid_peer['id']):
                with self.assertRaises(cg.ProfileReadError):
                    bad_opener(target)
            self.assertEqual(lookups, [])

            # 2. Target in another group, investigation record id, or member with no saved password -> refused, 0 lookups.
            good_conn = dict(self.request(current), group_name='G')
            opener = rp.profile_opener(self.store, current['id'], current, group_name='G', connection=good_conn)
            scope = cg.GroupScope([current, valid_peer, no_pass_peer], current)
            scoped_opener = scope.opener(opener)

            for bad_target in (other_group_peer, other_group_peer['id'],
                               {'id': investigation['id'], 'group': 'G'}, investigation['id'],
                               no_pass_peer, no_pass_peer['id'],
                               'unknown-id', 'x' * 201, ''):
                with self.subTest(bad_target=bad_target):
                    with self.assertRaises(cg.ProfileReadError):
                        opener(bad_target)
                    with self.assertRaises(cg.ProfileReadError):
                        scoped_opener(bad_target)
            with self.assertRaises(ValueError):
                self.store.connection_with_saved_password(
                    {'profile_id': investigation['id'], 'host': '127.0.0.1', 'port': 3306, 'user': 'dba'})
            with self.assertRaises(ValueError):
                self.store.connection_with_saved_password(
                    dict(self.request(other_group_peer), group_name='G'))
            self.assertEqual(lookups, [], 'refused target triggered a secret_store password lookup')

            # Positive control: valid member in group G with a saved password looks up its password once.
            conn = opener(valid_peer['id'])
            self.assertEqual(len(lookups), 1)
            self.assertEqual(conn.host, valid_peer['host'])


class TestGroupEffectiveSafety(GroupCase):
    """F5: one server-side rule for a group connection's safety type.

    The strictest type of the active member, the target and every current
    member of the group (production > staging > development > local > unset)
    is computed from the store at every call; read-only stays per member.
    """

    def test_strictest_type_ordering(self):
        self.assertEqual(cg.strictest_type(['local', 'staging', 'development']), 'staging')
        self.assertEqual(cg.strictest_type(['development', 'production']), 'production')
        self.assertEqual(cg.strictest_type(['development', None, '']), 'development')
        self.assertIsNone(cg.strictest_type([None, '', 'bogus']))
        profiles = [{'id': 'a', 'group': 'G', 'type': 'development'},
                    {'id': 'b', 'group': 'G', 'type': 'production'},
                    {'id': 'c', 'group': 'H', 'type': 'staging'},
                    {'id': 'd', 'group': 'G'}]
        self.assertEqual(cg.group_effective_safety(profiles, 'G'), 'production')
        self.assertEqual(cg.group_effective_safety(profiles, 'H'), 'staging')
        self.assertIsNone(cg.group_effective_safety(profiles, 'missing'))

    def test_editor_tab_pinned_to_a_member_uses_the_group_type(self):
        dev = self.save('dev-a', group='G', type='development')
        self.save('prod-b', group='G')
        payload = {'connection': dict(self.request(dev), group_name='G')}
        _, identity, safety, _ = editor_session._connection_facts(payload, self.store)
        self.assertEqual(safety['type'], 'production')
        self.assertTrue(safety['endpoint_matches'])
        self.assertFalse(safety['read_only'], 'read-only stays each member\'s own flag')
        statement = 'INSERT INTO t VALUES (1)'
        decision = dbops.decide(dbops.classify(statement), safety['type'], safety['read_only'],
                                safety['profile_name'])
        self.assertEqual(dbops.gate(decision, confirmed=False)[0], 'needs_confirmation')
        # Removing the Production member lowers the group back to its members.
        self.store.assign_profile_group(['prod-b'], '')
        _, identity_after, safety_after, _ = editor_session._connection_facts(payload, self.store)
        self.assertEqual(safety_after['type'], 'development')
        decision = dbops.decide(dbops.classify(statement), safety_after['type'], safety_after['read_only'])
        self.assertIsNone(dbops.gate(decision, confirmed=False))
        self.assertNotEqual(identity, identity_after,
                            'the identity a session or token is bound to follows the group')

    def test_a_production_member_joining_voids_review_tokens(self):
        dev = self.save('dev-a', group='G', type='development')
        workspace = types.SimpleNamespace(store=self.store)
        conn = dict(self.request(dev), group_name='G')
        details = {'connection_id': 7, 'mode': 'connection', 'expect': {}, 'statements': ['KILL CONNECTION 7']}
        identity = ui._review_identity(conn, workspace)
        token = ui._review_tokens(workspace).issue('kill', identity, details)
        self.save('prod-b', group='G')
        with self.assertRaises(ValueError):
            ui._review_tokens(workspace).consumer(token, 'kill', ui._review_identity(conn, workspace), details)

    def test_action_target_requires_and_uses_the_group(self):
        dev = self.save('dev-a', group='G', type='development')
        self.save('prod-b', group='G')
        outsider = self.save('other-c', group='H')
        workspace = types.SimpleNamespace(store=self.store)
        conn = self.request(dev)
        # A target without the group connection is refused before any connection opens.
        for payload in ({'connection': conn, 'target_profile_id': 'prod-b'},
                        {'connection': dict(conn, group_name='G'), 'target_profile_id': outsider['id']},
                        {'connection': dict(conn, group_name='G'), 'target_profile_id': 'missing'}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    ui.api_response('/api/live/cluster/plan', payload, workspace)
        # A target is a cluster concept only.
        with self.assertRaises(ValueError):
            ui.api_response('/api/live/proxysql/plan',
                            {'connection': dict(conn, group_name='G'), 'target_profile_id': 'prod-b'}, workspace)
        target = ui._action_target({'target_profile_id': 'prod-b'}, dict(conn, group_name='G'), 'cluster',
                                   self.store)
        self.assertEqual(target['id'], 'prod-b')
        safety = dbops.connection_safety(dev, dict(conn, group_name='G'), store=self.store,
                                         target_profile=target)
        self.assertEqual(safety['type'], 'production')

    def test_group_safety_fails_closed(self):
        dev = self.save('dev-a', group='G', type='development')
        conn = dict(self.request(dev), group_name='G')
        with mock.patch.object(self.store, 'list', side_effect=sqlite3.Error('locked')):
            safety = dbops.connection_safety(dev, conn, store=self.store)
        self.assertEqual(safety['type'], 'production')
        bad = dbops.connection_safety(dev, dict(conn, group_name='G\x00'), store=self.store)
        self.assertEqual(bad['type'], 'production')


class _FakeMemberConn(object):
    """Counts and bounds member logins for /api/connect without a network."""
    lock = threading.Lock()
    logins = []
    active = 0
    peak = 0
    uuids = {}
    fail = frozenset()
    pause = 0.0

    @classmethod
    def reset(cls, uuids=None, fail=(), pause=0.0):
        with cls.lock:
            cls.logins, cls.active, cls.peak = [], 0, 0
        cls.uuids, cls.fail, cls.pause = dict(uuids or {}), frozenset(fail), pause

    def __init__(self, **options):
        self.host = options.get('host')
        with _FakeMemberConn.lock:
            _FakeMemberConn.logins.append(self.host)

    def __enter__(self):
        if self.host in _FakeMemberConn.fail:
            raise ConnectorError('Access denied for user')
        with _FakeMemberConn.lock:
            _FakeMemberConn.active += 1
            _FakeMemberConn.peak = max(_FakeMemberConn.peak, _FakeMemberConn.active)
        if _FakeMemberConn.pause:
            time.sleep(_FakeMemberConn.pause)
        return self

    def __exit__(self, *args):
        with _FakeMemberConn.lock:
            _FakeMemberConn.active -= 1
        return False

    def server_version_comment(self):
        return 'MySQL Community Server (GPL)'

    def server_version(self):
        return '8.0.36'

    def query_rows(self, sql):
        if sql == 'SHOW DATABASES':
            return [('app',)]
        if sql == 'SELECT @@global.server_uuid':
            uuid = _FakeMemberConn.uuids.get(self.host)
            return [(uuid,)] if uuid else []
        if 'replication_group_members' in sql:
            return [('PRIMARY', 'ONLINE')]
        return []


class TestGroupConnect(GroupCase):
    """F8/F10: POST /api/connect opens a group as one Group connection."""

    UUIDS = {'dev-a.example': 'aaaaaaaa-0000-4000-8000-000000000001',
             'prod-b.example': 'bbbbbbbb-0000-4000-8000-000000000002',
             'replica-c.example': 'cccccccc-0000-4000-8000-000000000003'}

    def connect(self, payload, workspace):
        with mock.patch.object(connector, 'MySQLConnection', _FakeMemberConn):
            return ui.api_response('/api/connect', payload, workspace)

    def setUp(self):
        super(TestGroupConnect, self).setUp()
        _FakeMemberConn.reset(uuids=self.UUIDS)
        self.workspace = types.SimpleNamespace(store=self.store)

    def test_group_name_validation_and_member_choice(self):
        self.save('dev-a', group='G', type='development')
        self.save('prod-b', group='G')
        for payload in ({}, {'group_name': ''}, {'group_name': 'G\x00'}, {'group_name': 'x' * 81},
                        {'group_name': 'missing'}, {'group_name': None},
                        {'group_name': 'G', 'active_member_profile_id': 'x' * 201},
                        {'group_name': 'G', 'active_member_profile_id': 'missing'}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.connect(payload, self.workspace)
        self.assertEqual(_FakeMemberConn.logins, [], 'a refused request never logs in')
        reply = self.connect({'group_name': 'G', 'active_member_profile_id': 'prod-b'}, self.workspace)
        self.assertEqual(reply['group_connection']['active_member_profile_id'], 'prod-b')
        self.assertEqual(_FakeMemberConn.logins, ['prod-b.example'])

    def test_active_member_defaults_to_the_stored_source_then_the_first(self):
        self.save('dev-a', group='G', type='development')
        source = self.save('prod-b', group='G')
        replica = self.save('replica-c', group='G', type='development')
        self.store.record_topology_identity(source['id'], {'server_uuid': self.UUIDS['prod-b.example']})
        self.store.record_topology_identity(replica['id'],
                                            {'server_uuid': self.UUIDS['replica-c.example'],
                                             'source_uuids': [self.UUIDS['prod-b.example']]})
        reply = self.connect({'group_name': 'G'}, self.workspace)
        group = reply['group_connection']
        self.assertEqual(group['active_member_profile_id'], 'prod-b')
        self.assertEqual(group['primary_profile_id'], 'prod-b')
        # Without probe_members only the active member is logged in to.
        self.assertEqual(_FakeMemberConn.logins, ['prod-b.example'])
        self.assertEqual(group['effective_safety'], {'type': 'production', 'read_only': False})
        by_id = {m['profile_id']: m for m in group['members']}
        self.assertEqual(set(by_id), {'dev-a', 'prod-b', 'replica-c'})
        self.assertIs(by_id['prod-b']['reachable'], True)
        self.assertEqual(by_id['prod-b']['role'], 'PRIMARY')
        self.assertIsNone(by_id['dev-a']['reachable'])
        self.assertIsNone(by_id['dev-a']['role'])
        # The stored identity stands in for the uuid until a probe reads it live.
        self.assertEqual(by_id['replica-c']['server_uuid'], self.UUIDS['replica-c.example'])
        # The reply is the usual discovery plus the group block, never a secret.
        self.assertEqual(reply['engine'], 'mysql')
        self.assertNotIn('test-password', json.dumps(reply))

    def test_probe_members_is_bounded_and_group_only(self):
        profiles = [self.save('member-%02d' % i, group='G') for i in range(16)]
        self.save('outsider', group='H')
        _FakeMemberConn.reset(uuids=self.UUIDS, pause=0.02, fail=('member-07.example',))
        reply = self.connect({'group_name': 'G', 'probe_members': True}, self.workspace)
        with _FakeMemberConn.lock:
            logins, peak = list(_FakeMemberConn.logins), _FakeMemberConn.peak
        self.assertEqual(len(logins), 16, 'every member of a 16-member group is probed once')
        self.assertLessEqual(peak, 4, 'at most 4 member logins at a time')
        self.assertGreater(peak, 1, 'probes overlap')
        allowed = {p['id'] + '.example' for p in profiles}
        self.assertEqual(set(logins), allowed, 'only current members of the group are logged in to')
        by_id = {m['profile_id']: m for m in reply['group_connection']['members']}
        self.assertIs(by_id['member-07']['reachable'], False)
        self.assertNotIn('outsider', by_id)
        # An explicit open without the probe flag touches the active member only.
        _FakeMemberConn.reset(uuids=self.UUIDS)
        self.connect({'group_name': 'G'}, self.workspace)
        self.assertEqual(len(_FakeMemberConn.logins), 1)

    def test_topology_labels_never_rename_a_member(self):
        victim = self.save('dev-a', group='G', name='dev-a-name', type='development')
        other = self.save('prod-b', group='G', name='prod-db-2')
        for label in ('prod-db-2</b>', 'prod-db-2'):
            with self.subTest(label=label):
                raw = self.store._profile(victim['id'])
                raw['topology_identity'] = {'server_uuid': self.UUIDS['dev-a.example'],
                                            'label': label, 'name': other['name'], 'role': 'PRIMARY</b>'}
                conn = self.store._connect()
                try:
                    with conn:
                        conn.execute("UPDATE records SET payload=? WHERE id=? AND kind='profile'",
                                     (json.dumps(raw), victim['id']))
                finally:
                    conn.close()
                reply = self.connect({'group_name': 'G'}, self.workspace)
                by_id = {m['profile_id']: m for m in reply['group_connection']['members']}
                self.assertEqual(by_id['dev-a']['profile_name'], 'dev-a-name')
                self.assertEqual(by_id['prod-b']['profile_name'], 'prod-db-2')
                self.assertEqual(by_id['dev-a']['host'], 'dev-a.example')
                self.assertNotIn('</b>', json.dumps(reply))
                self.assertIsNone(reply['group_connection']['primary_profile_id'],
                                  'an injected role label is not topology evidence')
                first = reply['group_connection']['members'][0]
                self.assertEqual(reply['group_connection']['active_member_profile_id'], first['profile_id'],
                                 'with no primary evidence the active member is the first member')


if __name__ == '__main__':
    unittest.main()
