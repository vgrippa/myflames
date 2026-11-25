"""Socket cancellation and concurrent live reads through authenticated HTTP.

Sampling tests replay captured MySQL replies through the real report functions.
The key-isolation tests block only the read callback: connection consent,
request dispatch, cancellation and the single-flight registry stay real.
"""
import copy
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from _perf_replay import batch, replay
from myflames import connector, report, ui


class _HealthConnection(object):
    def __init__(self, factory, options):
        self.factory, self.options = factory, options
        self.samples = 0
        self.closed = threading.Event()
        self.password = options.get('password')

    def run(self, sql, extra_flags=None, timeout=None):
        with self.factory.lock:
            self.factory.calls.append(sql)
        if 'SHOW GLOBAL VARIABLES' in sql:
            return batch(sorted(self.factory.data['variables'].items()))
        if 'SHOW GLOBAL STATUS' in sql:
            sample = self.factory.data['samples'][min(self.samples, 1)]['status']
            self.samples += 1
            self.factory.first_sample.set()
            return batch(sorted(sample.items()))
        raise AssertionError('Unexpected Health read: ' + sql)

    def close(self):
        self.closed.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _HealthFactory(object):
    def __init__(self):
        source = Path(__file__).parent / 'fixtures' / 'health' / 'mysql-8.4.json'
        self.data = json.loads(source.read_text(encoding='utf-8'))
        self.calls, self.connections = [], []
        self.lock = threading.Lock()
        self.first_sample = threading.Event()

    def __call__(self, **options):
        conn = _HealthConnection(self, options)
        with self.lock:
            self.connections.append(conn)
        return conn


class _BlockedRead(object):
    """Hold every run until the test has observed joining or separation."""
    def __init__(self, error=None):
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.calls = []
        self.error = error

    def __call__(self, path, payload, workspace=None):
        with self.lock:
            self.calls.append((path, copy.deepcopy(payload), workspace))
            number = len(self.calls)
        if not self.release.wait(5):
            raise RuntimeError('Test did not release the live read')
        if self.error is not None:
            raise self.error
        return {'run': number}


class _ObservedConnection(object):
    """Observe a real sampler's replay transport without supplying cancellation."""
    def __init__(self, inner, is_sample):
        self.inner, self.is_sample = inner, is_sample
        self.first_sample, self.closed = threading.Event(), threading.Event()
        self.samples = 0

    def run(self, sql, extra_flags=None, timeout=None):
        result = self.inner.run(sql, extra_flags=extra_flags, timeout=timeout)
        if self.is_sample(sql):
            self.samples += 1
            self.first_sample.set()
        return result

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()


class TestLiveCancellationHTTP(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Tool discovery belongs to startup tests, not request concurrency.
        warmup = patch('myflames.client_tools.discover', return_value={})
        warmup.start()
        self.addCleanup(warmup.stop)
        self.servers = []
        self.clients = []
        self.server = self.make_server('workspace.sqlite3')
        self.connection = {'host': '127.0.0.1', 'port': 3306, 'user': 'reader'}

    def make_server(self, filename):
        server = ui.WorkspaceServer(0, store_path=Path(self.temp.name) / filename)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
        thread.start()
        self.servers.append((server, thread))
        return server

    def tearDown(self):
        for client in self.clients:
            client.close()
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def open_request(self, path, payload=None, server=None):
        server = server or self.server
        client = http.client.HTTPConnection(*server.server_address, timeout=6)
        self.clients.append(client)
        value = {'connection': self.connection, 'interval': 1} if payload is None else payload
        client.request('POST', path, json.dumps(value), {
            'Content-Type': 'application/json', 'X-Myflames-Token': server.token,
            'Origin': server.origin, 'Cookie': server.cookie_name + '=' + server.session_secret,
        })
        return client

    def answer(self, client):
        response = client.getresponse()
        return response.status, json.loads(response.read().decode('utf-8'))

    def abort(self, client):
        if client.sock is not None:
            client.sock.shutdown(socket.SHUT_RDWR)
        client.close()

    def join_request(self, path, payload=None):
        # Observe two distinct request threads waiting on the same flight.
        # This avoids relying on a sleep to mean the second caller joined.
        with report._FLIGHTS_LOCK:
            flights = list(report._FLIGHTS.values())
        self.assertEqual(len(flights), 1)
        done = flights[0].done
        original_wait = done.wait
        joined = threading.Event()
        lock, callers = threading.Lock(), set()

        def wait(*args, **kwargs):
            with lock:
                callers.add(threading.get_ident())
                if len(callers) == 2:
                    joined.set()
            return original_wait(*args, **kwargs)

        with patch.object(done, 'wait', side_effect=wait):
            client = self.open_request(path, payload)
            self.assertTrue(joined.wait(2), 'second request never joined the active read')
        return client

    def test_disconnected_waits_skips_second_sample_and_frees_worker_within_500ms(self):
        first_sample, closed = threading.Event(), threading.Event()

        def observe(sql, text, index):
            if 'events_waits_summary_global_by_event_name' in sql:
                first_sample.set()
            return text

        conn, _ = replay('waits', 'mysql-8.4', edit=observe)
        conn.close = closed.set
        with patch('myflames.connector.MySQLConnection', return_value=conn):
            client = self.open_request('/api/live/waits', {'connection': self.connection, 'interval': 2})
            self.assertTrue(first_sample.wait(2))
            started = time.monotonic()
            self.abort(client)
            self.assertTrue(self.until(lambda: self.server._worker_slots._value ==
                                       self.server.MAX_REQUEST_WORKERS, 0.5))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(closed.wait(0.5), 'the abandoned read still owns its connection')
        samples = [sql for sql, _ in conn.calls if 'events_waits_summary_global_by_event_name' in sql]
        self.assertEqual(len(samples), 1)

    def test_identical_health_requests_share_one_read_sequence(self):
        factory = _HealthFactory()
        with patch('myflames.connector.MySQLConnection', side_effect=factory):
            first = self.open_request('/api/live/health')
            self.assertTrue(factory.first_sample.wait(2))
            second = self.join_request('/api/live/health')
            one, two = self.answer(first), self.answer(second)
        self.assertEqual(one, two)
        self.assertEqual(one[0], 200)
        self.assertEqual(one[1]['schema_version'], 'health-1.0')
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(len(factory.calls), 3)

    def test_disconnected_io_and_memory_skip_their_second_samples(self):
        from myflames import memory
        from test_io_stats import replay as io_replay
        from test_memory import FixtureConnection, fixture
        io_conn, io_data = io_replay('io', 'mysql-8.4')
        cases = (
            ('io', io_conn, lambda sql: 'FROM performance_schema.file_summary_by_event_name ' in sql,
             {'schema': io_data['args'].get('schema')}),
            ('memory', FixtureConnection(fixture('mysql-8.4')),
             lambda sql: sql == memory.TAG + memory.CATEGORY_SQL, {}),
        )
        for endpoint, inner, is_sample, extra in cases:
            with self.subTest(endpoint=endpoint):
                conn = _ObservedConnection(inner, is_sample)
                payload = dict({'connection': self.connection, 'interval': 2}, **extra)
                with patch('myflames.connector.MySQLConnection', return_value=conn):
                    client = self.open_request('/api/live/' + endpoint, payload)
                    self.assertTrue(conn.first_sample.wait(2))
                    self.abort(client)
                    self.assertTrue(self.until(lambda: self.server._worker_slots._value ==
                                               self.server.MAX_REQUEST_WORKERS, 0.5))
                    self.assertTrue(conn.closed.wait(0.5))
                self.assertEqual(conn.samples, 1)

    def test_joined_health_caller_survives_first_socket_disconnect(self):
        factory = _HealthFactory()
        with patch('myflames.connector.MySQLConnection', side_effect=factory):
            first = self.open_request('/api/live/health')
            self.assertTrue(factory.first_sample.wait(2))
            second = self.join_request('/api/live/health')
            started = time.monotonic()
            self.abort(first)
            self.assertTrue(self.until(lambda: self.server._worker_slots._value ==
                                       self.server.MAX_REQUEST_WORKERS - 1, 0.5))
            self.assertLess(time.monotonic() - started, 0.5)
            status, result = self.answer(second)
        self.assertEqual((status, result['schema_version']), (200, 'health-1.0'))
        self.assertEqual(len(factory.connections), 1)
        self.assertEqual(len(factory.calls), 3)

    def test_different_health_parameter_or_connection_runs_twice(self):
        for changed in ({'interval': 2}, {'connection': dict(self.connection, port=3307)}):
            with self.subTest(changed=changed):
                factory = _HealthFactory()
                with patch('myflames.connector.MySQLConnection', side_effect=factory):
                    first = self.open_request('/api/live/health')
                    self.assertTrue(factory.first_sample.wait(2))
                    payload = dict({'connection': self.connection, 'interval': 1}, **changed)
                    second = self.open_request('/api/live/health', payload)
                    self.assertEqual(self.answer(first)[0], 200)
                    self.assertEqual(self.answer(second)[0], 200)
                self.assertEqual(len(factory.connections), 2)
                self.assertEqual(len(factory.calls), 6)

    def test_all_health_callers_disconnecting_cancels_the_run_and_releases_its_key(self):
        factory = _HealthFactory()
        payload = {'connection': self.connection, 'interval': 2}
        with patch('myflames.connector.MySQLConnection', side_effect=factory):
            first = self.open_request('/api/live/health', payload)
            self.assertTrue(factory.first_sample.wait(2))
            second = self.join_request('/api/live/health', payload)
            self.abort(first)
            self.abort(second)
            self.assertTrue(self.until(lambda: self.server._worker_slots._value ==
                                       self.server.MAX_REQUEST_WORKERS, 0.5))
            self.assertTrue(factory.connections[0].closed.wait(0.5))
            self.assertEqual(len(factory.calls), 2)
            self.assertEqual(self.answer(self.open_request('/api/live/health'))[0], 200)
        self.assertEqual(len(factory.connections), 2)
        self.assertEqual(len(factory.calls), 5)

    def separated(self, first_payload, second_payload, first_path='/api/live/health',
                  second_path='/api/live/health', second_server=None):
        read = _BlockedRead()
        with patch.object(ui, '_discover_response', side_effect=read):
            first = self.open_request(first_path, first_payload)
            try:
                self.assertTrue(self.until(lambda: len(read.calls) == 1))
                second = self.open_request(second_path, second_payload, server=second_server)
                self.assertTrue(self.until(lambda: len(read.calls) == 2),
                                'requests with different identities or parameters shared a read')
            finally:
                read.release.set()
            one, two = self.answer(first), self.answer(second)
        self.assertEqual((one[0], two[0]), (200, 200))
        self.assertNotEqual(one[1], two[1])

    def test_connection_credentials_tls_and_profile_never_share_a_result(self):
        for change in ({'password': 'different-test-password'}, {'user': 'other'}, {'host': 'db.example'},
                       {'port': 3307}, {'database': 'different_db'}, {'ssl_mode': 'REQUIRED'},
                       {'ssl_ca': '/test-ca.pem'}, {'ssh_host': 'ssh.example', 'ssh_user': 'reader'},
                       {'type': 'development'}, {'read_only': True}):
            with self.subTest(fields=sorted(change)):
                self.separated({'connection': self.connection},
                               {'connection': dict(self.connection, **change)})
        saved = [self.server.store.save('profile', dict(self.connection, name=name))
                 for name in ('One', 'Two')]
        self.separated({'connection': dict(self.connection, profile_id=saved[0]['id'])},
                       {'connection': dict(self.connection, profile_id=saved[1]['id'])})

    def test_every_request_parameter_and_endpoint_is_part_of_the_key(self):
        cases = (
            ('health', {'interval': 2}), ('health', {'with_identity': True}),
            ('waits', {'limit': 11}), ('io', {'all_instances': True}), ('io', {'schema': 'db'}),
            ('memory', {'interval': 2}), ('locks', {'waits_only': True}), ('locks', {'limit': 5}),
            ('indexes', {'schemas': ['db']}), ('indexes', {'all_schemas': True}),
            ('tables', {'fresh': True}), ('tables', {'limit': 7}),
            ('health', {'future_parameter': {'nested': [1, True, None]}}),
        )
        for endpoint, change in cases:
            with self.subTest(endpoint=endpoint, fields=sorted(change)):
                path = '/api/live/' + endpoint
                self.separated({'connection': self.connection},
                               dict({'connection': self.connection}, **change), path, path)
        self.separated({'connection': self.connection}, {'connection': self.connection},
                       '/api/live/health', '/api/live/memory')

    def test_each_requested_server_view_shares_identical_concurrent_reads(self):
        for endpoint in ('health', 'waits', 'io', 'memory', 'locks', 'indexes', 'tables'):
            with self.subTest(endpoint=endpoint):
                path = '/api/live/' + endpoint
                read = _BlockedRead()
                with patch.object(ui, '_discover_response', side_effect=read):
                    first = self.open_request(path)
                    try:
                        self.assertTrue(self.until(lambda: len(read.calls) == 1))
                        second = self.join_request(path)
                    finally:
                        read.release.set()
                    self.assertEqual(self.answer(first), (200, {'run': 1}))
                    self.assertEqual(self.answer(second), (200, {'run': 1}))
                self.assertEqual(len(read.calls), 1)

    def test_topology_shares_matching_sections_and_separates_different_sections(self):
        saved = self.server.store.save('profile', dict(self.connection, name='Member'))
        self.server.store.save_connection_group('Topology', [saved['id']])
        connection = dict(self.connection, profile_id=saved['id'], group_name='Topology')
        sections = ('map', 'members', 'parameters', 'accounts', 'plugins')
        for index, section in enumerate(sections):
            with self.subTest(section=section):
                read = _BlockedRead()

                def read_group(store, group_name, requested, *args, **kwargs):
                    return read(requested, {}, store)

                payload = {'connection': connection, 'section': section}
                with patch('myflames.topology_group.read_group', side_effect=read_group):
                    first = self.open_request('/api/live/topology', payload)
                    try:
                        self.assertTrue(self.until(lambda: len(read.calls) == 1))
                        second = self.join_request('/api/live/topology', payload)
                        different = dict(payload, section=sections[(index + 1) % len(sections)])
                        third = self.open_request('/api/live/topology', different)
                        self.assertTrue(self.until(lambda: len(read.calls) == 2))
                    finally:
                        read.release.set()
                    self.assertEqual(self.answer(first), (200, {'run': 1}))
                    self.assertEqual(self.answer(second), (200, {'run': 1}))
                    self.assertEqual(self.answer(third), (200, {'run': 2}))

    def test_identical_reads_from_different_workspaces_do_not_share(self):
        other = self.make_server('other-workspace.sqlite3')
        self.separated({'connection': self.connection}, {'connection': self.connection}, second_server=other)

    def test_rotated_group_member_credentials_start_a_separate_topology_read(self):
        # Keep public timestamps equal so a changed secret reference is the
        # only identity difference. Both saves use the real temporary vault.
        with patch('myflames.workspace_store._now', return_value='2026-11-23T10:00:00+00:00'):
            active = self.server.store.save('profile', dict(self.connection, name='Active'))
            member = self.server.store.save('profile', dict(self.connection, name='Peer', port=3307),
                                            password='credential-fixture-one', remember=True)
            self.server.store.save_connection_group('Credential rotation', [active['id'], member['id']])
            connection = dict(self.connection, profile_id=active['id'], group_name='Credential rotation')
            before = self.server.store.get('profile', member['id'])
            old_ref = self.server.store._profile(member['id'])['secret_ref']
            read = _BlockedRead()

            def read_group(store, group_name, section, *args, **kwargs):
                return read(section, {}, store)

            payload = {'connection': connection, 'section': 'accounts'}
            with patch('myflames.topology_group.read_group', side_effect=read_group), \
                    patch.object(self.server.store, 'connection_with_saved_password',
                                 wraps=self.server.store.connection_with_saved_password) as resolve_secret:
                first = self.open_request('/api/live/topology', payload)
                try:
                    self.assertTrue(self.until(lambda: len(read.calls) == 1))
                    self.server.store.save('profile', dict(before), member['id'],
                                           password='credential-fixture-two', remember=True)
                    self.assertNotEqual(old_ref, self.server.store._profile(member['id'])['secret_ref'])
                    self.assertEqual(before, self.server.store.get('profile', member['id']))
                    second = self.open_request('/api/live/topology', payload)
                    self.assertTrue(self.until(lambda: len(read.calls) == 2),
                                    'rotated member credentials joined the earlier topology read')
                finally:
                    read.release.set()
                self.assertEqual(self.answer(first), (200, {'run': 1}))
                self.assertEqual(self.answer(second), (200, {'run': 2}))
                resolve_secret.assert_not_called()

    def test_recreated_group_generation_does_not_join_old_consent(self):
        saved = self.server.store.save('profile', dict(self.connection, name='Member'))
        self.server.store.save_connection_group('Group', [saved['id']])
        connection = dict(self.connection, profile_id=saved['id'], group_name='Group')
        old_generation = self.server.store.get('profile', saved['id'])['group_generation']
        read = _BlockedRead()
        with patch.object(ui, '_discover_response', side_effect=read):
            first = self.open_request('/api/live/health', {'connection': connection})
            try:
                self.assertTrue(self.until(lambda: len(read.calls) == 1))
                self.server.store.delete_connection_group('Group')
                self.server.store.save_connection_group('Group', [saved['id']])
                self.assertNotEqual(old_generation,
                                    self.server.store.get('profile', saved['id'])['group_generation'])
                second = self.open_request('/api/live/health', {'connection': connection})
                self.assertTrue(self.until(lambda: len(read.calls) == 2))
            finally:
                read.release.set()
            self.assertEqual(self.answer(first)[0], 400)
            self.assertEqual(self.answer(second), (200, {'run': 2}))

    def test_group_recreated_during_guard_construction_cannot_run_under_stale_key(self):
        from myflames import accounts_api
        saved = self.server.store.save('profile', dict(self.connection, name='Member'))
        self.server.store.save_connection_group('Guard race', [saved['id']])
        connection = dict(self.connection, profile_id=saved['id'], group_name='Guard race')
        old_generation = self.server.store.get('profile', saved['id'])['group_generation']
        original_guard, changed = accounts_api._group_login_guard, []

        def recreate(store, supplied, identity=None):
            if not changed:
                changed.append(True)
                store.delete_connection_group('Guard race')
                store.save_connection_group('Guard race', [saved['id']])
            # Run the real consent guard, including the supplied incarnation.
            return original_guard(store, supplied, identity=identity)

        with patch.object(accounts_api, '_group_login_guard', side_effect=recreate), \
                patch.object(ui, '_discover_response', return_value={'wrong_scope': True}) as read:
            status, _ = self.answer(self.open_request('/api/live/health', {'connection': connection}))
        self.assertEqual(status, 400)
        read.assert_not_called()
        self.assertNotEqual(old_generation, self.server.store.get('profile', saved['id'])['group_generation'])
        with patch.object(ui, '_discover_response', return_value={'new_scope': True}):
            self.assertEqual(self.answer(self.open_request('/api/live/health', {'connection': connection})),
                             (200, {'new_scope': True}))

    def test_write_policy_changes_do_not_revoke_shared_read_consent(self):
        from myflames.live import connection_options
        for policy in ({'connection_type': 'production'}, {'read_only': True}):
            with self.subTest(policy=policy):
                saved = self.server.store.save('profile', dict(self.connection, name='Member',
                                                              type='development', read_only=False))
                group = 'Read policy ' + next(iter(policy))
                self.server.store.save_connection_group(group, [saved['id']])
                connection = dict(self.connection, profile_id=saved['id'], group_name=group)
                generation = self.server.store.get('profile', saved['id'])['group_generation']
                blocked = _BlockedRead()

                def read(path, payload, workspace=None):
                    result = blocked(path, payload, workspace)
                    # A policy edit must allow the next read's login check,
                    # as well as delivery of the shared result to both callers.
                    conn = ui._group_guarded_connection(connection_options(connection), connection, workspace.store)
                    conn._check_login_consent()
                    return result

                payload = {'connection': connection}
                with patch.object(ui, '_discover_response', side_effect=read):
                    first = self.open_request('/api/live/health', payload)
                    try:
                        self.assertTrue(self.until(lambda: len(blocked.calls) == 1))
                        second = self.join_request('/api/live/health', payload)
                        self.server.store.set_profile_safety(saved['id'], **policy)
                        self.assertEqual(generation,
                                         self.server.store.get('profile', saved['id'])['group_generation'])
                    finally:
                        blocked.release.set()
                    self.assertEqual(self.answer(first), (200, {'run': 1}))
                    self.assertEqual(self.answer(second), (200, {'run': 1}))
                self.assertEqual(len(blocked.calls), 1)

    def test_work_keeps_original_consent_when_a_new_connection_captures_recreated_group(self):
        from myflames.live import connection_options
        saved = self.server.store.save('profile', dict(self.connection, name='Member'))
        self.server.store.save_connection_group('Transport race', [saved['id']])
        connection = dict(self.connection, profile_id=saved['id'], group_name='Transport race')

        def read(path, payload, workspace=None):
            workspace.store.delete_connection_group('Transport race')
            workspace.store.save_connection_group('Transport race', [saved['id']])
            # This fresh connection captures the new group's own guard. The
            # request's original guard must still refuse before client spawn.
            conn = ui._group_guarded_connection(connection_options(connection), connection, workspace.store)
            result = conn._run_client(['fixture-client'], timeout=1)
            return {'unexpected_spawn': result.returncode}

        with patch.object(ui, '_discover_response', side_effect=read), \
                patch.object(connector, '_run_bounded', return_value=subprocess.CompletedProcess([], 0)) as run:
            status, _ = self.answer(self.open_request('/api/live/health', {'connection': connection}))
        self.assertEqual(status, 400)
        run.assert_not_called()

    def test_topology_pool_propagates_cancellation_and_drops_queued_member_reads(self):
        from myflames import read_cancel, topology_group
        saved = [self.server.store.save('profile', dict(self.connection, name='Member %d' % i, port=3306 + i))
                 for i in range(topology_group.MAX_CONCURRENCY + 1)]
        self.server.store.save_connection_group('Topology cancellation', [item['id'] for item in saved])
        connection = dict(self.connection, profile_id=saved[0]['id'], group_name='Topology cancellation')
        contexts, closed = [], []
        ready, lock = threading.Event(), threading.Lock()

        @contextmanager
        def open_member(*args, **kwargs):
            with lock:
                contexts.append(read_cancel.current())
                if len(contexts) == topology_group.MAX_CONCURRENCY:
                    ready.set()
            try:
                read_cancel.wait(2)
                raise AssertionError('The abandoned member read did not cancel')
                yield  # Makes this a context manager without supplying a transport.
            finally:
                with lock:
                    closed.append(1)

        with patch.object(topology_group, 'open_group_member', side_effect=open_member):
            client = self.open_request('/api/live/topology', {'connection': connection, 'section': 'members'})
            self.assertTrue(ready.wait(2))
            self.abort(client)
            self.assertTrue(self.until(lambda: self.server._worker_slots._value ==
                                       self.server.MAX_REQUEST_WORKERS, 0.5))
        self.assertEqual(len(contexts), topology_group.MAX_CONCURRENCY)
        self.assertEqual(len(closed), topology_group.MAX_CONCURRENCY)
        self.assertTrue(all(event is contexts[0] and event.is_set() for event in contexts))

    def test_identical_read_error_reaches_waiters_then_releases_key(self):
        read = _BlockedRead(ValueError('fixture refusal'))
        with patch.object(ui, '_discover_response', side_effect=read):
            first = self.open_request('/api/live/health')
            try:
                self.assertTrue(self.until(lambda: len(read.calls) == 1))
                second = self.join_request('/api/live/health')
            finally:
                read.release.set()
            one, two = self.answer(first), self.answer(second)
            self.assertEqual(one, two)
            self.assertEqual(one[0], 400)
            self.assertEqual(len(read.calls), 1)
            read.error = None
            self.assertEqual(self.answer(self.open_request('/api/live/health')), (200, {'run': 2}))

    def test_completed_result_is_not_cached_for_later_reads(self):
        read = _BlockedRead()
        read.release.set()
        with patch.object(ui, '_discover_response', side_effect=read):
            self.assertEqual(self.answer(self.open_request('/api/live/health')), (200, {'run': 1}))
            self.assertEqual(self.answer(self.open_request('/api/live/health')), (200, {'run': 2}))

    def test_read_watcher_error_does_not_cancel_connected_caller(self):
        for phase in ('create', 'register', 'select'):
            for error_type in (ValueError, OSError):
                with self.subTest(phase=phase, error=error_type.__name__):
                    observed = threading.Event()

                    def fail(*args):
                        observed.set()
                        raise error_type('watcher unavailable')

                    def read(path, payload, workspace=None):
                        self.assertTrue(observed.wait(2), 'watcher did not check the socket')
                        return {'still_here': True}

                    with patch.object(ui.selectors, 'DefaultSelector') as factory, \
                            patch.object(ui, '_discover_response', side_effect=read):
                        selector = factory.return_value.__enter__.return_value
                        if phase == 'create':
                            factory.side_effect = fail
                        else:
                            getattr(selector, phase).side_effect = fail
                        self.assertEqual(self.answer(self.open_request('/api/live/health')),
                                         (200, {'still_here': True}))

    def test_read_watcher_peeks_without_consuming_extra_request_bytes(self):
        entered, release = threading.Event(), threading.Event()
        sockets, observed = [], []
        original_process = self.server.process_request
        marker = b'bytes-for-the-next-request'

        def process(sock, address):
            sockets.append(sock)
            return original_process(sock, address)

        def read(path, payload, workspace=None):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test did not send the extra bytes')
            if not select.select([sockets[0]], [], [], 0)[0]:
                raise AssertionError('Watcher consumed the extra request bytes')
            observed.append(sockets[0].recv(len(marker), socket.MSG_PEEK))
            return {'still_here': True}

        with patch.object(self.server, 'process_request', side_effect=process), \
                patch.object(ui, '_discover_response', side_effect=read):
            client = self.open_request('/api/live/health')
            try:
                self.assertTrue(entered.wait(2))
                client.sock.sendall(marker)
                time.sleep(0.3)  # Allow multiple watcher polls while bytes are readable.
            finally:
                release.set()
            self.assertEqual(self.answer(client), (200, {'still_here': True}))
        self.assertEqual(observed, [marker])

    def test_reviewed_write_finishes_after_disconnect_without_read_cancellation(self):
        from myflames import read_cancel
        from test_locks import KILL_TAG, KillScenario, bound_kill_expect
        scenario = KillScenario()
        connection = dict(self.connection, user='app')
        payload = {'connection': connection, 'connection_id': 41, 'mode': 'connection', 'confirmed': True,
                   'expect': bound_kill_expect(scenario.conn, connection=connection)}
        prepared = self.answer(self.open_request('/api/live/kill/prepare', payload))
        self.assertEqual(prepared[0], 200)
        payload['review_token'] = prepared[1]['review_token']
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        original_kill, contexts, results = ui._kill_response, [], []

        def kill(value, workspace):
            contexts.append(read_cancel.current())
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test did not release the reviewed write')
            read_cancel.check()
            results.append(original_kill(value, workspace))
            completed.set()
            return results[-1]

        with patch('myflames.connector.MySQLConnection', return_value=scenario.conn), \
                patch.object(ui, '_kill_response', side_effect=kill):
            client = self.open_request('/api/live/kill', payload)
            try:
                self.assertTrue(entered.wait(2))
                self.abort(client)
                time.sleep(0.2)
            finally:
                release.set()
            self.assertTrue(completed.wait(3))
            self.assertTrue(self.until(lambda: self.server._worker_slots._value == self.server.MAX_REQUEST_WORKERS))
        self.assertEqual(contexts, [None])
        self.assertEqual(results[0]['status'], 'killed')
        self.assertEqual(scenario.kills(), [KILL_TAG + 'KILL CONNECTION 41'])
        self.assertEqual(self.server.store.audit_records()[0]['status'], 'ok')


class TestReadCancellationPrimitives(unittest.TestCase):
    def test_full_report_pool_propagates_read_cancellation_to_all_views(self):
        from myflames import read_cancel
        cancelled, ready = threading.Event(), threading.Event()
        contexts, errors = [], []
        lock = threading.Lock()

        def view(conn):
            with lock:
                contexts.append(read_cancel.current())
                if len(contexts) == 2:
                    ready.set()
            read_cancel.wait(2)
            return {'unexpected': 'uncancelled'}

        def run():
            try:
                with read_cancel.scope(cancelled):
                    report._run_all(object(), [('one', view), ('two', view), ('queued', view)],
                                    2, threading.Event(), None)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        try:
            self.assertTrue(ready.wait(2))
            cancelled.set()
            worker.join(0.5)
            self.assertFalse(worker.is_alive())
        finally:
            cancelled.set()
            worker.join(5)
        self.assertEqual(contexts, [cancelled, cancelled])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], read_cancel.CancelledRead)

    def test_cancellation_between_samples_is_checked_even_without_remaining_wait(self):
        from myflames import perf_common, read_cancel
        cancelled, samples = threading.Event(), []

        def take(clock):
            samples.append(1)
            cancelled.set()
            return {'t': 0, 'taken_at': 'fixture'}

        with self.assertRaises(read_cancel.CancelledRead), read_cancel.scope(cancelled):
            perf_common.sample_twice(take, 1, clock=lambda: 10)
        self.assertEqual(samples, [1])

    def test_cancelled_client_process_is_killed_and_reaped(self):
        from myflames import read_cancel
        cancelled, launched = threading.Event(), threading.Event()
        processes, errors = [], []
        original_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            proc = original_popen(*args, **kwargs)
            processes.append(proc)
            launched.set()
            return proc

        def run():
            try:
                with read_cancel.scope(cancelled):
                    connector._run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=35)
            except BaseException as exc:
                errors.append(exc)

        with patch.object(connector.subprocess, 'Popen', side_effect=spawn):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(launched.wait(3))
                started = time.monotonic()
                cancelled.set()
                worker.join(0.5)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - started, 0.5)
            finally:
                cancelled.set()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
                worker.join(3)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], read_cancel.CancelledRead)
        self.assertTrue(all(proc.poll() is not None for proc in processes))

    def test_pre_cancelled_read_starts_no_client_process(self):
        from myflames import read_cancel
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(connector.subprocess, 'Popen') as spawn:
            with self.assertRaises(read_cancel.CancelledRead), read_cancel.scope(cancelled):
                connector._run_bounded([sys.executable, '-c', 'pass'], timeout=1)
        spawn.assert_not_called()

    def test_scope_restores_outer_cancellation_and_does_not_leak_into_writes(self):
        from myflames import read_cancel
        outer, inner = threading.Event(), threading.Event()
        self.assertIsNone(read_cancel.current())
        with read_cancel.scope(outer):
            self.assertIs(read_cancel.current(), outer)
            with read_cancel.scope(inner):
                self.assertIs(read_cancel.current(), inner)
            self.assertIs(read_cancel.current(), outer)
        self.assertIsNone(read_cancel.current())


class TestFirstContactCancellation(unittest.TestCase):
    def setUp(self):
        # Reuse only fixture builders, not the other module's test classes.
        # Selection and path trust stay real; no executable reaches a server.
        from test_connector import TestClientSelection, TestVersionAwareClients
        from myflames import client_tools
        TestClientSelection.setUp(self)
        self.binary = lambda **kwargs: TestClientSelection.binary(self, **kwargs)
        TestVersionAwareClients.layout(self)
        self.discovered = client_tools.discover(names=client_tools.QUERY_DISCOVERY_NAMES)
        self.keys, self.errors = client_tools.choice_keys(self.discovered, {})

    def resolve(self, conn, cancel, results, errors, name):
        from myflames import read_cancel
        try:
            with read_cancel.scope(cancel):
                results[name] = conn._first_contact(self.discovered, self.keys, {}, self.errors)
        except BaseException as exc:
            errors[name] = exc

    def joined_wait(self, conn, joined):
        with connector._CONTACT_LOCK:
            done = connector._CONTACT_FLIGHTS[conn._endpoint_key()]['done']
        original_wait = done.wait

        def wait(*args, **kwargs):
            joined.set()
            return original_wait(*args, **kwargs)

        return patch.object(done, 'wait', side_effect=wait)

    def test_cancelled_contact_owner_does_not_cancel_a_write_context_waiter(self):
        from myflames import read_cancel
        cancelled, entered, joined = threading.Event(), threading.Event(), threading.Event()
        owner = connector.MySQLConnection(host='cancelled-contact-owner', user='read-user')
        waiter = connector.MySQLConnection(host=owner.host, user='write-user')
        results, errors, calls, contexts = {}, {}, [], []

        def read(conn, sql, binary, **kwargs):
            self.assertEqual(sql, 'SELECT @@version, @@version_comment')
            calls.append(conn.user)
            contexts.append(read_cancel.current())
            if conn is owner:
                entered.set()
                read_cancel.wait(2)
            return '8.4.14\tMySQL Community Server\n'

        first = threading.Thread(target=self.resolve, args=(owner, cancelled, results, errors, 'owner'))
        second = threading.Thread(target=self.resolve, args=(waiter, None, results, errors, 'waiter'))
        with patch.object(connector.MySQLConnection, '_run_with_binary', new=read):
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.joined_wait(owner, joined):
                    second.start()
                    self.assertTrue(joined.wait(2))
                    cancelled.set()
                    first.join(3)
                    second.join(3)
            finally:
                cancelled.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(set(errors), {'owner'})
        self.assertIsInstance(errors['owner'], read_cancel.CancelledRead)
        self.assertEqual(results['waiter'], {'engine': 'mysql', 'version': '8.4.14'})
        self.assertEqual(calls, ['read-user', 'write-user'])
        self.assertEqual(contexts, [cancelled, None])
        self.assertNotIn(owner._endpoint_key(), connector._CONTACT_FLIGHTS)

    def test_cancelled_contact_waiter_leaves_promptly_and_owner_finishes(self):
        from myflames import read_cancel
        cancelled, entered, joined, release = (threading.Event() for _ in range(4))
        owner = connector.MySQLConnection(host='cancelled-contact-waiter', user='owner')
        waiter = connector.MySQLConnection(host=owner.host, user='waiter')
        results, errors, calls = {}, {}, []

        def read(conn, sql, binary, **kwargs):
            calls.append(conn.user)
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test did not release first-contact owner')
            return '8.4.14\tMySQL Community Server\n'

        first = threading.Thread(target=self.resolve, args=(owner, None, results, errors, 'owner'))
        second = threading.Thread(target=self.resolve, args=(waiter, cancelled, results, errors, 'waiter'))
        with patch.object(connector.MySQLConnection, '_run_with_binary', new=read):
            first.start()
            try:
                self.assertTrue(entered.wait(3))
                with self.joined_wait(owner, joined):
                    second.start()
                    self.assertTrue(joined.wait(2))
                    cancelled.set()
                    second.join(0.5)
                    self.assertFalse(second.is_alive())
                    self.assertTrue(first.is_alive(), 'waiter cancellation interrupted the owner')
            finally:
                cancelled.set()
                release.set()
                first.join(3)
                if second.ident is not None:
                    second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(set(errors), {'waiter'})
        self.assertIsInstance(errors['waiter'], read_cancel.CancelledRead)
        self.assertEqual(results['owner'], {'engine': 'mysql', 'version': '8.4.14'})
        self.assertEqual(calls, ['owner'])
        self.assertNotIn(owner._endpoint_key(), connector._CONTACT_FLIGHTS)


if __name__ == '__main__':
    unittest.main()
