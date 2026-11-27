"""Request-only redaction, including pooled and shared live reads."""
from concurrent.futures import ThreadPoolExecutor
import io
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from myflames import read_cancel, report, request_secrets, secret_store, ui
from myflames.workspace_store import WorkspaceStore


class TestRequestSecrets(unittest.TestCase):
    def test_scope_restores_parent_and_clears_on_success_or_failure(self):
        request_secrets.capture('outside-a-request')
        self.assertEqual(request_secrets.values(), set())
        with request_secrets.scope() as outer:
            request_secrets.capture_many(('outer', '', None, 42))
            with request_secrets.scope(outer):
                request_secrets.capture('borrowed')
            self.assertEqual(request_secrets.values(), {'outer', 'borrowed'})
            with self.assertRaises(RuntimeError):
                with request_secrets.scope() as inner:
                    request_secrets.capture('inner')
                    self.assertEqual(request_secrets.values(), {'inner'})
                    raise RuntimeError('end this request')
            self.assertEqual(inner.values(), set())
            self.assertIs(request_secrets.current(), outer)
            with request_secrets.scope(None):
                request_secrets.capture('unscoped-worker')
                self.assertEqual(request_secrets.values(), set())
            self.assertEqual(request_secrets.values(), {'outer', 'borrowed'})
        self.assertIsNone(request_secrets.current())
        self.assertEqual(outer.values(), set())
        # A retained worker reference cannot extend a completed request.
        with request_secrets.scope(outer):
            request_secrets.capture('late-worker-password')
        self.assertEqual(outer.values(), set())

    def test_pool_inherits_only_explicit_context_and_restores_reused_thread(self):
        with request_secrets.scope() as registry, ThreadPoolExecutor(max_workers=1) as pool:
            request_secrets.capture('request')
            self.assertEqual(pool.submit(request_secrets.values).result(), set())

            def worker():
                with request_secrets.scope(registry):
                    request_secrets.capture('worker')
                    return request_secrets.values()

            self.assertEqual(pool.submit(worker).result(), {'request', 'worker'})
            self.assertEqual(pool.submit(request_secrets.values).result(), set())
            self.assertEqual(request_secrets.values(), {'request', 'worker'})

    def test_concurrent_requests_do_not_share_passwords(self):
        barrier = threading.Barrier(2, timeout=5)

        def request(password):
            with request_secrets.scope():
                request_secrets.capture(password)
                barrier.wait()
                return request_secrets.values()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(request, 'first-request')
            second = pool.submit(request, 'second-request')
            self.assertEqual(first.result(), {'first-request'})
            self.assertEqual(second.result(), {'second-request'})

    def test_reraise_keeps_original_exception_context_redacted(self):
        password = 'already-used-password'
        output = io.StringIO()
        with request_secrets.scope(), mock.patch.object(ui.sys, 'stderr', output):
            try:
                try:
                    raise RuntimeError('original error with ' + password)
                except RuntimeError as exc:
                    ui._reraise_redacted(exc, password)
            except RuntimeError:
                ui._log_unexpected_error('/api/live/replication/skip', sys.exc_info())
        logged = output.getvalue()
        self.assertIn('During handling of the above exception', logged)
        self.assertIn('original error with [redacted]', logged)
        self.assertNotIn(password, logged)

    def test_report_pool_propagates_passwords_to_parent(self):
        def view(connection):
            self.assertIn('parent-password', request_secrets.values())
            request_secrets.capture('view-password')
            return {'read': True}

        with request_secrets.scope():
            request_secrets.capture('parent-password')
            result = report._run_all(object(), [('view', view)], 1, threading.Event(), None)
            self.assertIsNone(result['view']['error'])
            self.assertEqual(request_secrets.values(), {'parent-password', 'view-password'})

    def test_shared_read_transfers_passwords_before_success_or_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail), request_secrets.scope():
                request_secrets.capture('request-password')
                registries = []

                def work():
                    registries.append(request_secrets.current())
                    self.assertIn('request-password', request_secrets.values())
                    request_secrets.capture('shared-password')
                    if fail:
                        raise RuntimeError('shared-password')
                    return {'ok': True}

                if fail:
                    with self.assertRaises(RuntimeError):
                        report.run_single_flight('secret-transfer', (), work)
                else:
                    self.assertEqual(report.run_single_flight('secret-transfer', (), work), {'ok': True})
                self.assertEqual(request_secrets.values(), {'request-password', 'shared-password'})
                self.assertEqual(registries[0].values(), set())
                self.assertNotIn('secret-transfer', report._FLIGHTS)

    def test_shared_read_survives_first_caller_clearing_its_registry(self):
        started, release, joined = threading.Event(), threading.Event(), threading.Event()
        cancelled = threading.Event()
        outcomes, contexts, failures = {}, {}, []
        key = 'secret-transfer-after-disconnect'

        def work():
            contexts['flight'] = request_secrets.current()
            request_secrets.capture('first-member-password')
            started.set()
            if not release.wait(5):
                raise AssertionError('shared read was not released')
            request_secrets.capture('later-member-password')
            raise RuntimeError('first-member-password later-member-password')

        def caller(name, cancel):
            try:
                with request_secrets.scope() as registry:
                    contexts[name] = registry
                    try:
                        report.run_single_flight(key, (), work, cancel=cancel)
                    except read_cancel.CancelledRead:
                        outcomes[name] = 'cancelled'
                    except RuntimeError:
                        output = io.StringIO()
                        with mock.patch.object(ui.sys, 'stderr', output):
                            ui._log_unexpected_error('/api/live/topology', sys.exc_info())
                        outcomes[name] = output.getvalue()
            except BaseException as exc:
                failures.append(exc)

        first = threading.Thread(target=caller, args=('first', cancelled))
        second = threading.Thread(target=caller, args=('second', threading.Event()))
        first.start()
        try:
            self.assertTrue(started.wait(5))
            done = report._FLIGHTS[key].done
            wait = done.wait

            def observe_wait(timeout):
                if threading.current_thread() is second:
                    joined.set()
                return wait(timeout)

            with mock.patch.object(done, 'wait', side_effect=observe_wait):
                second.start()
                self.assertTrue(joined.wait(5))
                cancelled.set()
                first.join(5)
                self.assertFalse(first.is_alive())
                self.assertEqual(outcomes.get('first'), 'cancelled')
                self.assertEqual(contexts['first'].values(), set())
                self.assertIn('first-member-password', contexts['flight'].values())
                release.set()
                second.join(5)
        finally:
            cancelled.set()
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertIn('RuntimeError: [redacted] [redacted]', outcomes['second'])
        self.assertNotIn('first-member-password', outcomes['second'])
        self.assertNotIn('later-member-password', outcomes['second'])
        self.assertTrue(all(registry.values() == set() for registry in contexts.values()))
        self.assertNotIn(key, report._FLIGHTS)

    def test_shared_read_thread_construction_failure_clears_unpublished_secrets(self):
        flights = []
        create_flight = report._Flight
        key = 'secret-thread-construction-failure'

        def create(options):
            flight = create_flight(options)
            flights.append(flight)
            return flight

        with request_secrets.scope():
            request_secrets.capture('request-password')
            with mock.patch.object(report, '_Flight', side_effect=create), \
                    mock.patch.object(report.threading, 'Thread', side_effect=RuntimeError('no thread')):
                with self.assertRaisesRegex(RuntimeError, 'no thread'):
                    report.run_single_flight(key, (), lambda: None)
            self.assertEqual(flights[0].secrets.values(), set())
            self.assertNotIn(key, report._FLIGHTS)
            self.assertEqual(request_secrets.values(), {'request-password'})


class TestSavedPasswordCapture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        environment = mock.patch.dict(os.environ, {'HOME': self.home.name, 'USERPROFILE': self.home.name,
                                                   'MYFLAMES_SECRET_BACKEND': 'vault'})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = WorkspaceStore()
        self.profile = self.store.save('profile', {'name': 'saved', 'host': 'db.example',
                                       'port': 3306, 'user': 'dba', 'group': 'G'},
                                       password='saved-password', remember=True)
        self.connection = {'profile_id': self.profile['id'], 'host': 'db.example',
                           'port': 3306, 'user': 'dba', 'group_name': 'G'}

    def test_captures_saved_and_typed_password_only_during_request(self):
        with request_secrets.scope() as registry:
            saved = self.store.connection_with_saved_password(self.connection)
            typed = self.store.connection_with_saved_password(dict(self.connection, password='typed-password'))
            unsaved = self.store.connection_with_saved_password({'password': 'unsaved-password'})
            self.assertEqual(request_secrets.values(), {saved['password'], typed['password'], unsaved['password']})
        self.assertEqual(registry.values(), set())
        self.assertIsNone(request_secrets.current())
        self.store.connection_with_saved_password(self.connection)
        self.assertEqual(request_secrets.values(), set())

    def test_refused_login_never_retrieves_or_captures_saved_password(self):
        for changes in ({'host': 'other.example'}, {'group_name': 'other-group'}):
            with self.subTest(changes=changes), request_secrets.scope(), \
                    mock.patch.object(secret_store, 'get_secret') as lookup:
                with self.assertRaises(ValueError):
                    self.store.connection_with_saved_password(dict(self.connection, **changes))
                lookup.assert_not_called()
                self.assertEqual(request_secrets.values(), set())


if __name__ == '__main__':
    unittest.main()
