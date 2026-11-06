"""Recursive transfer contracts with an in-memory SFTP tree, never SSH."""
import ast
import json
import os
import pathlib
import posixpath
import stat
import tempfile
import threading
import unittest
from unittest import mock

from myflames import sftp
from myflames import sftp_jobs as jobs

LABEL = {'ssh': 'a' * 32, 'name': 'Test SSH'}


class FakeTransfer(object):
    """LSTAT-based remote tree; failures distinguish status from transport loss."""

    def __init__(self, data=b'downloaded', entered=None, release=None, stop_after_chunk=False,
                 failure=False, tree=None):
        self.data, self.entered, self.release = data, entered, release
        self.stop_after_chunk, self.failure = stop_after_chunk, failure
        self.closed = False
        self.uploaded, self.removed, self.modes = {}, [], []
        self.mode_failures = set()
        self.calls, self.failures, self.listings = [], {}, {}
        self.tree = tree if tree is not None else {}
        if tree is None:
            for path in ('/', '/logs', '/one', '/two', '/three', '/var', '/var/log'):
                self.directory(path)
            for path in ('/log', '/log0', '/log1', '/log2', '/leaf', '/var/log/mysql.log'):
                self.file(path, data)

    def directory(self, path):
        self.tree[path] = {'mode': stat.S_IFDIR | 0o700, 'size': 0}

    def file(self, path, data=b'abc'):
        self.tree[path] = {'mode': stat.S_IFREG | 0o600, 'size': len(data), 'data': data}

    def link(self, path, target):
        self.tree[path] = {'mode': stat.S_IFLNK | 0o777, 'size': 0, 'link': target}

    def safe(self, text):
        return text.replace('private-secret', '[redacted]')

    def lstat(self, path):
        self.calls.append(('lstat', path))
        if path not in self.tree:
            raise sftp.SftpError('Missing', 2)
        return dict(self.tree[path])

    def stat(self, path):
        info = self.lstat(path)
        if stat.S_ISLNK(info['mode']):
            return self.stat(info['link'])
        return info

    def listdir(self, path):
        self.calls.append(('listdir', path))
        if path in self.listings:
            return self.listings[path]
        result = []
        for name, info in self.tree.items():
            if name != path and posixpath.dirname(name) == path:
                row = dict(info)
                row.update(name=posixpath.basename(name), is_dir=stat.S_ISDIR(info['mode']))
                result.append(row)
        return result

    def mkdir(self, path):
        self.calls.append(('mkdir', path))
        if path in self.tree:
            raise sftp.SftpError('Already exists', 4)
        if posixpath.dirname(path) not in self.tree:
            raise sftp.SftpError('Missing parent', 2)
        self.directory(path)

    def hold(self, stop):
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(3):
            raise RuntimeError('Test did not release worker')
        if stop.is_set():
            raise sftp.SftpCancelled('File transfer cancelled.')

    def get(self, remote, fd, size, progress, stop):
        self.calls.append(('get', remote))
        self.hold(stop)
        if remote in self.failures:
            raise self.failures[remote]
        data = self.stat(remote)['data']
        os.write(fd, data)
        progress(len(data))
        if self.stop_after_chunk:
            stop.set()
            raise sftp.SftpCancelled('File transfer cancelled.')
        if self.failure:
            raise sftp.SftpError('Remote read failed')

    def put(self, fd, remote, size, progress, stop, overwrite=False, mode=None):
        self.calls.append(('put', remote))
        self.hold(stop)
        if remote in self.failures:
            raise self.failures[remote]
        if remote in self.tree and not overwrite:
            raise sftp.SftpError('Already exists', 4)
        data = os.read(fd, size)
        self.uploaded[remote] = data
        self.file(remote, data)
        progress(len(data))
        if mode is not None:
            # Like SftpSession.put: FSETSTAT on the handle, before CLOSE.
            if remote in self.mode_failures:
                self.remove(remote)
                raise sftp.SftpError('Cannot set permissions', 3)
            self.modes.append((remote, mode))
            self.tree[remote]['mode'] = stat.S_IFREG | mode
        if self.stop_after_chunk or self.failure:
            self.remove(remote)
            if self.stop_after_chunk:
                stop.set()
                raise sftp.SftpCancelled('File transfer cancelled.')
            raise sftp.SftpError('Remote write failed')

    def setstat(self, path, mode):
        self.modes.append((path, mode))
        self.tree[path]['mode'] = stat.S_IFREG | mode

    def remove(self, path):
        self.removed.append(path)
        self.tree.pop(path, None)

    def close(self):
        self.closed = True
        if self.release:
            self.release.set()


class TestJobs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = os.path.realpath(temporary.name)
        patch = mock.patch.dict(os.environ, {'HOME': self.root, 'USERPROFILE': self.root,
                                            'MYFLAMES_SECRET_BACKEND': 'vault'})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def local(self, name='source.log', data=b'abc'):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as stream:
            stream.write(data)
        return path

    def manager(self, session=None, factory=None, clock=None, store=None):
        session = session or FakeTransfer()
        manager = jobs.SftpJobManager(store=store, **({'clock': clock} if clock else {}))
        manager.opener = factory or (lambda: session)
        self.addCleanup(manager.shutdown)
        return manager, session

    def finish(self, manager, job):
        thread = manager._jobs[job['id']].thread
        if thread:
            thread.join(3)
            self.assertFalse(thread.is_alive(), 'Transfer worker did not terminate')
        return next(j for j in manager.jobs() if j['id'] == job['id'])

    def test_download_atomic_publish_and_permissions(self):
        manager, session = self.manager()
        job = manager.start_download(manager.opener, LABEL, ['/var/log/mysql.log'], self.root, 'fail')
        final = self.finish(manager, job)
        path = os.path.join(self.root, 'mysql.log')
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'downloaded')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(path + '.myflames-part'))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(final['bytes_done'], 10)
        self.assertTrue(session.closed)
        json.dumps(final, allow_nan=False)

    def test_upload_preserves_bytes_and_strips_special_mode_bits(self):
        path = self.local(data=b'upload-payload')
        os.chmod(path, 0o4750)
        manager, session = self.manager()
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [path], '/logs', 'fail'))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(session.uploaded['/logs/source.log'], b'upload-payload')
        self.assertEqual(session.modes, [('/logs/source.log', 0o750)])

    def test_chosen_folder_link_owned_by_another_user_is_refused(self):
        # Security review: resolving a chosen folder must not follow a link
        # another user planted (in /tmp, /Users/Shared); root's and our own are fine.
        actual = os.path.join(self.root, 'actual')
        os.mkdir(actual)
        link = os.path.join(self.root, 'planted')
        os.symlink(actual, link)
        real_lstat = os.lstat

        def foreign(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == link:
                values = list(info)
                values[4] = 4242  # st_uid
                return os.stat_result(values)
            return info
        manager, _ = self.manager()
        with mock.patch.object(jobs.os, 'lstat', side_effect=foreign):
            with self.assertRaisesRegex(ValueError, 'owned by another user'):
                manager.start_download(manager.opener, LABEL, ['/log'], link, 'fail')
            with self.assertRaisesRegex(ValueError, 'owned by another user'):
                manager.start_upload(manager.opener, LABEL, [os.path.join(link, 'x')], '/', 'fail')
        self.assertEqual(jobs._trusted_realpath(link), os.path.realpath(actual))
        if os.path.islink('/tmp'):
            self.assertEqual(jobs._trusted_realpath('/tmp'), os.path.realpath('/tmp'))

    def test_without_getuid_transfers_say_unavailable_not_attribute_error(self):
        # Windows has no os.getuid: the platform message, never a raw error.
        manager, _ = self.manager()
        with mock.patch.object(jobs.os, 'getuid', create=True) as getuid:
            del jobs.os.getuid
            try:
                for start in (lambda: manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'),
                              lambda: manager.start_upload(manager.opener, LABEL, [self.local()], '/', 'fail')):
                    with self.assertRaisesRegex(ValueError, 'unavailable on this platform'):
                        start()
            finally:
                jobs.os.getuid = getuid

    def test_chosen_folder_resolved_once_and_symlink_leaf_refused(self):
        # A chosen folder under a link (macOS /tmp, /var) is resolved when the
        # transfer starts; later opens never follow links (see the swap test).
        actual = os.path.join(self.root, 'actual')
        os.mkdir(actual)
        link = os.path.join(self.root, 'linked')
        os.symlink(actual, link)
        source = self.local()
        leaf = os.path.join(self.root, 'leaf')
        os.symlink(source, leaf)
        manager, _ = self.manager()
        job = manager.start_download(manager.opener, LABEL, ['/log'], link, 'fail')
        self.assertEqual(job['target_dir'], os.path.realpath(actual))
        self.assertEqual(self.finish(manager, job)['state'], 'finished')
        self.assertTrue(os.path.isfile(os.path.join(actual, 'log')))
        with self.assertRaises(ValueError):
            manager.start_download(manager.opener, LABEL, ['/log'], link + '/sub/..', 'fail')
        with self.assertRaises(ValueError):
            manager.start_upload(manager.opener, LABEL, [leaf], '/', 'fail')
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/leaf'], self.root, 'overwrite'))
        self.assertEqual(final['state'], 'failed')

    def test_existing_final_requires_overwrite_and_remains_intact(self):
        path = self.local('log', b'original')
        manager, _ = self.manager()
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(final['items'][0]['error'], 'Already exists.')
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'original')
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'overwrite'))
        self.assertEqual(final['state'], 'finished')
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'downloaded')

    def test_target_traversal_and_windows_names_refused(self):
        manager, _ = self.manager()
        for path in ('/logs/..', '/logs/.', '/logs/..\\outside', '/logs/C:secret', '/logs/NUL', '/logs/name.'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.start_download(manager.opener, LABEL, [path], self.root, 'fail')
        self.assertEqual(os.listdir(self.root), [])

    def test_cancel_and_failure_remove_only_owned_partial(self):
        for cancel in (True, 'fail'):
            manager, _ = self.manager(FakeTransfer(stop_after_chunk=cancel, failure=not cancel))
            final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
            self.assertEqual(final['state'], 'cancelled' if cancel else 'failed')
            self.assertEqual(os.listdir(self.root), [])
        part = self.local('log.myflames-part', b'previous-part')
        manager, _ = self.manager()
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(final['state'], 'failed')
        with open(part, 'rb') as stream:
            self.assertEqual(stream.read(), b'previous-part')

    def test_racing_final_file_is_not_overwritten_without_flag(self):
        session = FakeTransfer()
        original_get = session.get

        def racing(*args):
            original_get(*args)
            self.local('log', b'racer')
        session.get = racing
        manager, _ = self.manager(session)
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(final['state'], 'failed')
        with open(os.path.join(self.root, 'log'), 'rb') as stream:
            self.assertEqual(stream.read(), b'racer')
        self.assertFalse(os.path.exists(os.path.join(self.root, 'log.myflames-part')))

    def test_final_creation_after_last_preflight_still_refuses_overwrite(self):
        manager, _ = self.manager()
        original = jobs._LocalDirectory.check_final
        checks = []

        def racing(parent, name, overwrite):
            original(parent, name, overwrite)
            checks.append(name)
            if len(checks) == 2:
                self.local('log', b'racing-file')
        with mock.patch.object(jobs._LocalDirectory, 'check_final', racing):
            final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(final['state'], 'failed')
        with open(os.path.join(self.root, 'log'), 'rb') as stream:
            self.assertEqual(stream.read(), b'racing-file')

    def test_directory_swapped_for_symlink_after_preflight_refused(self):
        target = os.path.join(self.root, 'target')
        outside = os.path.join(self.root, 'outside')
        os.mkdir(target)
        os.mkdir(outside)

        def factory():
            os.rmdir(target)
            os.symlink(outside, target)
            return FakeTransfer()
        manager, _ = self.manager(factory=factory)
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], target, 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(os.listdir(outside), [])

    def test_duplicate_targets_and_staging_collisions_refused(self):
        manager, _ = self.manager()
        for paths in (['/a/log', '/b/log'], ['/log', '/log.myflames-part']):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                manager.start_download(manager.opener, LABEL, paths, self.root, 'fail')

    def test_shutdown_prunes_expired_terminal_job_without_error(self):
        now = [0.0]
        manager, _ = self.manager(clock=lambda: now[0])
        self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        now[0] = 3601
        manager.shutdown()
        self.assertEqual(manager.jobs(), [])

    def test_queue_two_running_rest_queued_and_cancel(self):
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        count = []

        def factory():
            index = len(count)
            session = FakeTransfer(entered=entered[index], release=release)
            count.append(session)
            return session
        manager, _ = self.manager(factory=factory)
        path = self.local()
        first = manager.start_upload(manager.opener, LABEL, [path], '/one', 'fail')
        second = manager.start_upload(manager.opener, LABEL, [path], '/two', 'fail')
        self.assertTrue(entered[0].wait(1))
        self.assertTrue(entered[1].wait(1))
        third = manager.start_upload(manager.opener, LABEL, [path], '/three', 'fail')
        self.assertEqual([j['state'] for j in manager.jobs()], ['queued', 'running', 'running'])
        self.assertIsNone(manager._jobs[third['id']].thread)
        self.assertEqual(manager.cancel(third['id'])['state'], 'cancelled')
        manager.cancel(first['id'])
        release.set()
        self.assertEqual(self.finish(manager, first)['state'], 'cancelled')
        self.assertEqual(self.finish(manager, second)['state'], 'finished')
        self.assertEqual(len(count), 2)

    def test_queue_starts_next_when_slot_frees(self):
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        count = []

        def factory():
            index = len(count)
            instance = FakeTransfer(entered=entered[index] if index < 2 else None,
                                    release=release if index < 2 else None)
            count.append(instance)
            return instance
        manager, _ = self.manager(factory=factory)
        path = self.local()
        first = manager.start_upload(manager.opener, LABEL, [path], '/one', 'fail')
        second = manager.start_upload(manager.opener, LABEL, [path], '/two', 'fail')
        self.assertTrue(entered[0].wait(1))
        self.assertTrue(entered[1].wait(1))
        third = manager.start_upload(manager.opener, LABEL, [path], '/three', 'fail')
        release.set()
        self.finish(manager, first)
        self.finish(manager, second)
        self.assertEqual(self.finish(manager, third)['state'], 'finished')
        self.assertEqual(len(count), 3)

    def test_eta_rolling_window_with_fake_clock(self):
        now = [10.0]
        manager, _ = self.manager(clock=lambda: now[0])
        item = {'source': 'x', 'target': 'y', 'bytes': 1000, 'done': 0, 'state': 'running', 'error': None}
        job = jobs._Job('download', manager.opener, LABEL, ['x'], self.root, 'fail', now[0])
        job.items = [item]
        job.public['bytes_total'] = 1000
        job.started = now[0]
        job.public['state'] = 'running'
        job.samples.append((10.0, 0))
        self.assertIsNone(job.public['eta_s'])
        now[0] = 12
        manager._progress(job, item, 200)
        self.assertEqual(job.public['rate_bps'], 100)
        self.assertEqual(job.public['eta_s'], 8)
        now[0] = 17
        manager._progress(job, item, 700)
        self.assertEqual(job.public['rate_bps'], 100)
        self.assertEqual(job.public['eta_s'], 3)
        job.metrics(23)
        self.assertEqual(job.public['rate_bps'], 0)
        self.assertIsNone(job.public['eta_s'])

    def test_terminal_ttl_and_snapshot_copy(self):
        now = [0.0]
        manager, _ = self.manager(clock=lambda: now[0])
        job = manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail')
        final = self.finish(manager, job)
        final['items'][0]['source'] = 'changed'
        self.assertEqual(manager.jobs()[0]['items'][0]['source'], '/log')
        now[0] = 3599
        self.assertEqual(len(manager.jobs()), 1)
        now[0] = 3600
        self.assertEqual(manager.jobs(), [])

    def test_audit_actual_store_protocol_for_success_failure_cancel(self):
        class Store(object):
            def __init__(self):
                self.records = []

            def audit_insert(self, row):
                self.records.append(dict(row))

            def audit_complete(self, key, fields):
                next(row for row in self.records if row['id'] == key).update(fields)
        store = Store()
        for index, session in enumerate((FakeTransfer(), FakeTransfer(failure=True), FakeTransfer(stop_after_chunk=True))):
            manager, _ = self.manager(session, store=store)
            self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log%d' % index], self.root, 'fail'))
        self.assertEqual([r['outcome'] for r in store.records], ['finished', 'failed', 'cancelled'])
        self.assertEqual([r['status'] for r in store.records], ['ok', 'error', 'error'])
        self.assertTrue(all(r['operation'] == 'sftp_download' for r in store.records))


    def test_folder_upload_mirrors_nested_files_and_empty_folders(self):
        folder = os.path.join(self.root, 'tree')
        os.makedirs(os.path.join(folder, 'deep', 'empty'))
        self.local('tree/first', b'\x00\xfffirst')
        self.local('tree/deep/second', b'next\x00payload')
        manager, remote = self.manager()
        remote.directory('/logs/tree')
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [folder], '/logs', 'fail'))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(final['files_total'], 2)
        self.assertEqual(final['files_done'], 2)
        self.assertEqual(remote.tree['/logs/tree/first']['data'], b'\x00\xfffirst')
        self.assertEqual(remote.tree['/logs/tree/deep/second']['data'], b'next\x00payload')
        self.assertTrue(stat.S_ISDIR(remote.tree['/logs/tree/deep/empty']['mode']))
        self.assertNotIn(('mkdir', '/logs/tree'), remote.calls)

    def test_folder_download_mirrors_nested_files_and_reuses_folders(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.directory('/tree/deep')
        remote.directory('/tree/deep/empty')
        remote.file('/tree/first', b'\x00\xfffirst')
        remote.file('/tree/deep/second', b'next\x00payload')
        os.mkdir(os.path.join(self.root, 'tree'))
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(final['files_done'], 2)
        self.assertEqual(pathlib.Path(self.root, 'tree/first').read_bytes(), b'\x00\xfffirst')
        self.assertEqual(pathlib.Path(self.root, 'tree/deep/second').read_bytes(), b'next\x00payload')
        self.assertTrue(pathlib.Path(self.root, 'tree/deep/empty').is_dir())
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.root, 'tree/deep')).st_mode), 0o777 & ~umask)
        self.assertFalse(list(pathlib.Path(self.root).rglob('*.myflames-part')))

    def test_upload_links_and_pipes_skipped_at_multiple_depths(self):
        folder = os.path.join(self.root, 'tree')
        os.makedirs(os.path.join(folder, 'deep'))
        self.local('outside', b'secret')
        self.local('tree/deep/good', b'good')
        for prefix in ('tree', 'tree/deep'):
            os.symlink(os.path.join(self.root, 'outside'), os.path.join(self.root, prefix, 'link'))
            os.symlink(folder, os.path.join(self.root, prefix, 'cycle'))
            os.mkfifo(os.path.join(self.root, prefix, 'pipe'))
        manager, remote = self.manager()
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [folder], '/logs', 'fail'))
        self.assertEqual(final['state'], 'finished')
        skipped = [i for i in final['items'] if i['state'] == 'skipped']
        self.assertEqual(len(skipped), 6)
        self.assertEqual([i['error'] for i in skipped].count('Symbolic link not transferred.'), 4)
        self.assertEqual([i['error'] for i in skipped].count('Not a regular file.'), 2)
        self.assertEqual(remote.uploaded, {'/logs/tree/deep/good': b'good'})

    def test_download_links_and_special_files_skipped_at_multiple_depths(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.directory('/tree/deep')
        remote.file('/outside', b'secret')
        remote.file('/tree/deep/good', b'good')
        for prefix in ('/tree', '/tree/deep'):
            remote.link(prefix + '/link', '/outside')
            remote.link(prefix + '/cycle', '/tree')
            remote.tree[prefix + '/pipe'] = {'mode': stat.S_IFIFO | 0o600, 'size': 0}
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['files_skipped'], 6)
        self.assertEqual(final['files_done'], 1)
        self.assertEqual(final['state'], 'finished')
        self.assertEqual([i['error'] for i in final['items']].count('Symbolic link not transferred.'), 4)
        self.assertEqual([i['error'] for i in final['items']].count('Not a regular file.'), 2)
        self.assertEqual([p for kind, p in remote.calls if kind == 'get'], ['/tree/deep/good'])

    def test_top_remote_folder_link_followed_but_file_link_skipped(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.file('/tree/data', b'bytes')
        remote.link('/linked', '/tree')
        # The protocol resolves an ancestor link when accessing its children.
        remote.file('/linked/data', b'bytes')
        remote.link('/file-link', '/log')
        remote.listings['/linked'] = [dict(remote.tree['/tree/data'], name='data', is_dir=False)]
        final = self.finish(manager, manager.start_download(manager.opener, LABEL,
                                                           ['/linked', '/file-link'], self.root, 'fail'))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(final['files_done'], 1)
        self.assertEqual(final['files_skipped'], 1)
        self.assertEqual(pathlib.Path(self.root, 'linked/data').read_bytes(), b'bytes')

    def test_nonfolder_destination_fails_only_its_subtree_both_directions(self):
        for direction in ('upload', 'download'):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory(dir=self.root) as target:
                manager, remote = self.manager()
                if direction == 'upload':
                    os.makedirs(os.path.join(target, 'tree', 'blocked'))
                    self.local(os.path.relpath(target, self.root) + '/tree/good', b'yes')
                    self.local(os.path.relpath(target, self.root) + '/tree/blocked/bad', b'no')
                    remote.directory('/logs/tree')
                    remote.file('/logs/tree/blocked', b'keep')
                    result = manager.start_upload(manager.opener, LABEL, [target + '/tree'], '/logs', 'overwrite')
                else:
                    remote.directory('/tree')
                    remote.directory('/tree/blocked')
                    remote.file('/tree/good', b'yes')
                    remote.file('/tree/blocked/bad', b'no')
                    os.mkdir(target + '/tree')
                    pathlib.Path(target, 'tree/blocked').write_bytes(b'keep')
                    result = manager.start_download(manager.opener, LABEL, ['/tree'], target, 'overwrite')
                final = self.finish(manager, result)
                self.assertEqual(final['state'], 'failed')
                self.assertEqual(final['files_done'], 1)
                self.assertEqual(final['files_failed'], 1)
                self.assertEqual([i['state'] for i in final['items'] if i['source'].endswith('/bad')], ['failed'])
                if direction == 'upload':
                    self.assertEqual(remote.tree['/logs/tree/good']['data'], b'yes')
                    self.assertEqual(remote.tree['/logs/tree/blocked']['data'], b'keep')
                else:
                    self.assertEqual(pathlib.Path(target, 'tree/good').read_bytes(), b'yes')
                    self.assertEqual(pathlib.Path(target, 'tree/blocked').read_bytes(), b'keep')

    def test_per_file_exists_policies_both_directions(self):
        for direction in ('upload', 'download'):
            for policy in ('fail', 'skip', 'overwrite'):
                with self.subTest(direction=direction, policy=policy), tempfile.TemporaryDirectory(dir=self.root) as target:
                    manager, remote = self.manager()
                    if direction == 'upload':
                        pathlib.Path(target, 'a').write_bytes(b'new')
                        pathlib.Path(target, 'b').write_bytes(b'fresh')
                        remote.file('/logs/a', b'old')
                        job = manager.start_upload(manager.opener, LABEL, [target + '/a', target + '/b'], '/logs', policy)
                    else:
                        pathlib.Path(target, 'a').write_bytes(b'old')
                        remote.file('/a', b'new')
                        remote.file('/b', b'fresh')
                        job = manager.start_download(manager.opener, LABEL, ['/a', '/b'], target, policy)
                    final = self.finish(manager, job)
                    self.assertEqual(final['state'], 'failed' if policy == 'fail' else 'finished')
                    self.assertEqual(final['items'][0]['state'], {'fail': 'failed', 'skip': 'skipped', 'overwrite': 'finished'}[policy])
                    if policy == 'fail':
                        self.assertEqual(final['items'][0]['error'], 'Already exists.')
                    self.assertEqual(final['items'][1]['state'], 'finished')
                    actual = remote.tree['/logs/a']['data'] if direction == 'upload' else pathlib.Path(target, 'a').read_bytes()
                    self.assertEqual(actual, b'new' if policy == 'overwrite' else b'old')

    def test_overwrite_never_replaces_links_or_special_targets(self):
        for kind in ('link', 'pipe', 'folder'):
            for direction in ('upload', 'download'):
                with self.subTest(kind=kind, direction=direction), tempfile.TemporaryDirectory(dir=self.root) as target:
                    manager, remote = self.manager()
                    if direction == 'upload':
                        pathlib.Path(target, 'data').write_bytes(b'new')
                        if kind == 'link':
                            remote.link('/logs/data', '/log')
                        else:
                            remote.tree['/logs/data'] = {'mode': (stat.S_IFIFO if kind == 'pipe' else stat.S_IFDIR) | 0o600, 'size': 0}
                        job = manager.start_upload(manager.opener, LABEL, [target + '/data'], '/logs', 'overwrite')
                    else:
                        remote.file('/data', b'new')
                        if kind == 'link':
                            os.symlink(target + '/outside', target + '/data')
                        elif kind == 'pipe':
                            os.mkfifo(target + '/data')
                        else:
                            os.mkdir(target + '/data')
                        job = manager.start_download(manager.opener, LABEL, ['/data'], target, 'overwrite')
                    final = self.finish(manager, job)
                    self.assertEqual(final['state'], 'failed')
                    self.assertFalse([c for c in remote.calls if c[0] in ('put', 'get')])

    def test_one_status_failure_does_not_stop_other_file(self):
        manager, remote = self.manager()
        remote.failures['/log'] = sftp.SftpError('Denied', 3)
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log', '/log1'], self.root, 'fail'))
        self.assertEqual([i['state'] for i in final['items']], ['failed', 'finished'])
        self.assertEqual(final['files_failed'], 1)
        self.assertEqual(final['files_done'], 1)
        self.assertEqual(final['error'], 'Denied')

    def test_transport_failure_fails_all_remaining_items_with_redacted_error(self):
        manager, remote = self.manager()
        remote.failures['/log'] = sftp.SftpError('Disconnected private-secret')
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log', '/log1'], self.root, 'fail'))
        self.assertEqual([i['state'] for i in final['items']], ['failed', 'failed'])
        self.assertEqual([i['error'] for i in final['items']], ['Disconnected [redacted]'] * 2)
        self.assertEqual([c for c in remote.calls if c[0] == 'get'], [('get', '/log')])
        self.assertEqual(os.listdir(self.root), [])

    def test_cancel_mid_folder_cancels_remaining_files_and_retry_only_cancelled(self):
        manager, remote = self.manager(FakeTransfer(stop_after_chunk=True))
        remote.directory('/tree')
        remote.file('/tree/a', b'aaa')
        remote.file('/tree/b', b'bbb')
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['state'], 'cancelled')
        self.assertEqual([i['state'] for i in final['items']], ['cancelled', 'cancelled'])
        self.assertEqual(list(pathlib.Path(self.root, 'tree').iterdir()), [])
        fresh = FakeTransfer(tree=remote.tree)
        manager._jobs[final['id']].opener = lambda: fresh
        retried = self.finish(manager, manager.retry(final['id']))
        self.assertNotEqual(retried['id'], final['id'])
        self.assertEqual(retried['sources'], ['/tree/a', '/tree/b'])
        self.assertEqual(retried['files_done'], 2)
        self.assertEqual(retried['state'], 'finished')
        self.assertEqual(retried['target_dir'], self.root)

    def test_retry_preserves_mirrored_targets_and_excludes_success_and_skipped(self):
        remote = FakeTransfer()
        remote.directory('/tree')
        remote.directory('/tree/deep')
        remote.file('/tree/good', b'yes')
        remote.file('/tree/deep/bad', b'fix')
        remote.link('/tree/link', '/log')
        remote.failures['/tree/deep/bad'] = sftp.SftpError('Denied', 3)
        sessions = [remote, FakeTransfer(tree=remote.tree)]
        opener = lambda: sessions.pop(0)
        manager, _ = self.manager(factory=opener)
        final = self.finish(manager, manager.start_download(opener, LABEL, ['/tree'], self.root, 'fail'))
        retried = self.finish(manager, manager.retry(final['id']))
        self.assertEqual(retried['sources'], ['/tree/deep/bad'])
        self.assertEqual(retried['items'][0]['target'], self.root + '/tree/deep/bad')
        self.assertEqual(retried['files_total'], 1)
        self.assertEqual(retried['files_done'], 1)
        self.assertEqual(pathlib.Path(self.root, 'tree/deep/bad').read_bytes(), b'fix')
        with self.assertRaisesRegex(ValueError, 'no failed or cancelled'):
            manager.retry(retried['id'])

    def test_active_retry_refused_and_clear_keeps_active_jobs(self):
        entered, release = threading.Event(), threading.Event()
        manager, _ = self.manager(FakeTransfer(entered=entered, release=release))
        job = manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail')
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(ValueError, 'finish'):
            manager.retry(job['id'])
        self.assertEqual(manager.clear(), 0)
        current = manager.jobs()[0]
        self.assertEqual(current['phase'], 'transferring')
        self.assertEqual(current['current']['source'], '/log')
        release.set()
        self.finish(manager, job)
        self.assertEqual(manager.clear(), 1)

    def test_clear_and_ssh_filter_and_newest_first(self):
        manager, _ = self.manager()
        other = {'ssh': 'b' * 32, 'name': 'Other'}
        first = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        second = self.finish(manager, manager.start_download(lambda: FakeTransfer(), other, ['/log1'], self.root, 'fail'))
        self.assertEqual([j['id'] for j in manager.jobs()], [second['id'], first['id']])
        self.assertEqual([j['id'] for j in manager.jobs(LABEL['ssh'])], [first['id']])
        self.assertEqual(manager.clear(LABEL['ssh']), 1)
        self.assertEqual([j['id'] for j in manager.jobs()], [second['id']])
        self.assertEqual(manager.clear(), 1)
        self.assertEqual(manager.clear(), 0)

    def test_local_error_fails_one_file_and_the_job_goes_on(self):
        first, second = self.local('first', b'one'), self.local('second', b'two')
        session = FakeTransfer()
        put = session.put

        def shrinking(fd, remote, size, progress, cancel, overwrite=False, mode=None):
            if remote.endswith('/first'):
                raise sftp.SftpLocalError('The local file became shorter during upload.')
            return put(fd, remote, size, progress, cancel, overwrite=overwrite, mode=mode)
        session.put = shrinking
        manager, _ = self.manager(session)
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [first, second], '/', 'fail'))
        states = {posixpath.basename(i['target']): i['state'] for i in final['items']}
        self.assertEqual(states, {'first': 'failed', 'second': 'finished'})
        self.assertEqual(final['state'], 'failed')

    def test_retry_after_a_cancelled_scan_starts_the_selection_again(self):
        entered, release = threading.Event(), threading.Event()
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.file('/tree/a', b'a')
        remote.file('/tree/b', b'b')
        listdir = remote.listdir

        def slow_listdir(path):
            entered.set()
            release.wait(2)
            return listdir(path)
        remote.listdir = slow_listdir
        job = manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail')
        self.assertTrue(entered.wait(2))
        manager.cancel(job['id'])
        release.set()
        cancelled = self.finish(manager, job)
        self.assertEqual(cancelled['state'], 'cancelled')
        remote.listdir = listdir
        again = self.finish(manager, manager.retry(job['id']))
        self.assertEqual(again['sources'], ['/tree'])
        self.assertEqual((again['state'], again['files_done']), ('finished', 2))

    def test_whole_job_failure_names_its_ssh_reason(self):
        for error, code in ((sftp.SftpAuthError('not accepted'), 'auth_failed'),
                            (sftp.SftpHostKeyUnknown('not trusted'), 'host_key_unknown'),
                            (ValueError('The SSH connection changed.'), None)):
            with self.subTest(code=code):
                manager = jobs.SftpJobManager()
                self.addCleanup(manager.shutdown)

                def opener(error=error):
                    raise error
                final = self.finish(manager, manager.start_download(opener, LABEL, ['/log'], self.root, 'fail'))
                self.assertEqual((final['state'], final['error_code']), ('failed', code))
                self.assertIn(str(error), final['error'])

    def test_clear_keeps_failed_and_cancelled_jobs_for_retry(self):
        manager, _ = self.manager()
        open(os.path.join(self.root, 'log'), 'wb').close()
        failed = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(manager.clear(), 0)
        self.assertEqual([j['id'] for j in manager.jobs()], [failed['id']])

    def test_item_cap_before_any_destination_write(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.listings['/tree'] = [{'name': 'file%d' % n, 'mode': stat.S_IFREG | 0o600,
                                     'size': 1, 'is_dir': False} for n in range(10001)]
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(final['error'], 'This transfer has more than 10000 files. Transfer fewer files at a time.')
        self.assertEqual(final['files_total'], 10000)
        self.assertEqual(final['files_failed'], 10000)
        self.assertEqual(os.listdir(self.root), [])
        self.assertFalse([c for c in remote.calls if c[0] in ('get', 'put', 'mkdir')])

    def test_upload_cap_before_any_destination_write(self):
        os.mkdir(self.root + '/tree')
        for n in range(4):
            self.local('tree/file%d' % n)
        manager, remote = self.manager()
        with mock.patch.object(jobs, 'MAX_ITEMS', 3):
            final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [self.root + '/tree'], '/logs', 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertIn('more than 10000 files', final['error'])
        self.assertFalse(remote.uploaded)
        self.assertFalse([c for c in remote.calls if c[0] == 'mkdir'])

    def test_depth_cap_both_directions_before_writing_any_bytes(self):
        for direction in ('upload', 'download'):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory(dir=self.root) as target:
                manager, remote = self.manager()
                if direction == 'upload':
                    directory = target + '/tree'
                    os.mkdir(directory)
                    pathlib.Path(directory, 'first').write_bytes(b'first')
                    for n in range(65):
                        directory += '/d'
                        os.mkdir(directory)
                    job = manager.start_upload(manager.opener, LABEL, [target + '/tree'], '/logs', 'fail')
                else:
                    remote.directory('/tree')
                    remote.file('/tree/first', b'first')
                    directory = '/tree'
                    for n in range(65):
                        directory += '/d'
                        remote.directory(directory)
                    job = manager.start_download(manager.opener, LABEL, ['/tree'], target, 'fail')
                final = self.finish(manager, job)
                self.assertEqual(final['state'], 'failed')
                self.assertIn('64 levels', final['error'])
                self.assertEqual(final['bytes_done'], 0)
                self.assertFalse([c for c in remote.calls if c[0] in ('get', 'put', 'mkdir')])
                if direction == 'download':
                    self.assertEqual(os.listdir(target), [])

    def test_public_item_and_failure_lists_truncated_but_retry_retains_all(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        for n in range(205):
            remote.file('/tree/f%03d' % n, b'x')
            remote.failures['/tree/f%03d' % n] = sftp.SftpError('Denied', 3)
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['files_total'], 205)
        self.assertEqual(final['files_failed'], 205)
        self.assertEqual(len(final['items']), 200)
        self.assertEqual(final['items_more'], 5)
        self.assertEqual(len(final['failed_items']), 200)
        self.assertEqual(final['items'][-1]['source'], '/tree/f199')
        fresh = FakeTransfer(tree=remote.tree)
        manager._jobs[final['id']].opener = lambda: fresh
        retried = self.finish(manager, manager.retry(final['id']))
        self.assertEqual(retried['files_done'], 205)
        self.assertTrue(pathlib.Path(self.root, 'tree/f204').exists())

    def test_unsafe_readdir_names_failed_individually_without_creating_paths(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.file('/tree/good', b'good')
        remote.listings['/tree'] = [{'name': name, 'mode': stat.S_IFREG | 0o600, 'size': 1}
                                    for name in ('..', 'a/b', 'CON', 'a\\b')]
        remote.listings['/tree'].append(dict(remote.tree['/tree/good'], name='good'))
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual([i['state'] for i in final['items']], ['failed'] * 4 + ['finished'])
        self.assertTrue(all('single safe file name' in i['error'] for i in final['failed_items']))
        self.assertEqual(os.listdir(self.root + '/tree'), ['good'])
        self.assertEqual([c for c in remote.calls if c[0] == 'get'], [('get', '/tree/good')])

    def test_nested_staging_collision_refused_before_transfer(self):
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.file('/tree/log', b'one')
        remote.file('/tree/log.myflames-part', b'two')
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(final['files_failed'], 1)
        self.assertEqual(pathlib.Path(self.root, 'tree/log.myflames-part').read_bytes(), b'two')
        self.assertFalse(pathlib.Path(self.root, 'tree/log').exists())

    def test_public_shape_matches_typescript_and_has_utc_terminal_timestamp(self):
        manager, _ = self.manager()
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(set(final), set('id ssh ssh_name direction state phase on_exists target_dir sources files_total files_done files_skipped files_failed bytes_total bytes_done rate_bps eta_s started_at finished_at error error_code current items items_more failed_items'.split()))
        self.assertEqual(set(final['items'][0]), set('source target bytes done state error'.split()))
        self.assertEqual(final['ssh'], LABEL['ssh'])
        self.assertEqual(final['ssh_name'], LABEL['name'])
        self.assertTrue(final['started_at'].endswith('+00:00'))
        self.assertTrue(final['finished_at'].endswith('+00:00'))
        self.assertIsNone(final['phase'])
        self.assertIsNone(final['current'])
        json.dumps(final, allow_nan=False)

    def test_invalid_policy_and_unsupported_platform_fail_before_opening_session(self):
        opener = mock.Mock()
        manager, _ = self.manager()
        source = self.local()
        for policy in (None, False, True, 'replace', 1, [], {}):
            for direction in ('upload', 'download'):
                with self.subTest(policy=policy, direction=direction), self.assertRaises(ValueError):
                    if direction == 'upload':
                        manager.start_upload(opener, LABEL, [source], '/', policy)
                    else:
                        manager.start_download(opener, LABEL, ['/log'], self.root, policy)
        with mock.patch.object(os, 'name', 'nt'):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                manager.start_upload(opener, LABEL, [source], '/', 'fail')
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                manager.start_download(opener, LABEL, ['/log'], self.root, 'fail')
        opener.assert_not_called()

    def test_python37_syntax(self):
        for filename in ('myflames/sftp_jobs.py', 'test/test_sftp_jobs.py'):
            with self.subTest(filename=filename):
                source = pathlib.Path(filename).read_text()
                try:
                    ast.parse(source, filename=filename, feature_version=(3, 7))
                except TypeError:  # Python 3.7's parser uses its own grammar.
                    ast.parse(source, filename=filename)

    def test_depth_64_folder_accepts_its_file_children(self):
        for direction in ('upload', 'download'):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory(dir=self.root) as target:
                manager, remote = self.manager()
                if direction == 'upload':
                    directory = target + '/tree'
                    os.mkdir(directory)
                    for n in range(64):
                        directory += '/d'
                        os.mkdir(directory)
                    pathlib.Path(directory, 'file').write_bytes(b'deep')
                    job = manager.start_upload(manager.opener, LABEL, [target + '/tree'], '/logs', 'fail')
                else:
                    directory = '/tree'
                    remote.directory(directory)
                    for n in range(64):
                        directory += '/d'
                        remote.directory(directory)
                    remote.file(directory + '/file', b'deep')
                    job = manager.start_download(manager.opener, LABEL, ['/tree'], target, 'fail')
                final = self.finish(manager, job)
                self.assertEqual(final['state'], 'finished')
                self.assertEqual(final['files_done'], 1)
                self.assertEqual(final['bytes_done'], 4)

    def test_top_level_file_count_cap_is_a_scanning_failure(self):
        manager, remote = self.manager()
        paths = [self.local('file%d' % n) for n in range(4)]
        with mock.patch.object(jobs, 'MAX_ITEMS', 3):
            final = self.finish(manager, manager.start_upload(manager.opener, LABEL, paths, '/logs', 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertIn('more than 10000 files', final['error'])
        self.assertFalse(remote.uploaded)
        self.assertFalse([c for c in remote.calls if c[0] == 'mkdir'])

    def test_retry_scanning_file_failure_refreshes_remote_size(self):
        remote, fresh = FakeTransfer(), FakeTransfer()
        remote.lstat = mock.Mock(side_effect=sftp.SftpError('Denied', 3))
        fresh.file('/log', b'new-size')
        sessions = [remote, fresh]
        opener = lambda: sessions.pop(0)
        manager, _ = self.manager(factory=opener)
        final = self.finish(manager, manager.start_download(opener, LABEL, ['/log'], self.root, 'fail'))
        self.assertEqual(final['files_failed'], 1)
        self.assertEqual(final['error'], 'Denied')
        retried = self.finish(manager, manager.retry(final['id']))
        self.assertEqual(retried['files_done'], 1)
        self.assertEqual(retried['bytes_total'], 8)
        self.assertEqual(retried['bytes_done'], 8)
        self.assertEqual(pathlib.Path(self.root, 'log').read_bytes(), b'new-size')
        self.assertIs(manager._jobs[retried['id']].opener, opener)

    def test_closed_session_fails_remaining_files_even_for_oserror(self):
        manager, remote = self.manager()

        def close_then_fail(*args):
            remote.close()
            raise OSError('Connection closed')
        remote.get = close_then_fail
        final = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log', '/log1'], self.root, 'fail'))
        self.assertEqual([i['state'] for i in final['items']], ['failed', 'failed'])
        self.assertEqual([i['error'] for i in final['items']], ['Connection closed'] * 2)
        self.assertEqual(os.listdir(self.root), [])

    def test_remote_folder_symlink_is_not_reused_as_upload_destination(self):
        os.mkdir(self.root + '/tree')
        self.local('tree/file', b'private')
        manager, remote = self.manager()
        remote.link('/logs/tree', '/outside')
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [self.root + '/tree'], '/logs', 'overwrite'))
        self.assertEqual(final['files_failed'], 1)
        self.assertEqual(final['state'], 'failed')
        self.assertFalse(remote.uploaded)
        self.assertTrue(stat.S_ISLNK(remote.tree['/logs/tree']['mode']))

    def test_upload_source_folder_swapped_for_link_after_scan_refused(self):
        os.mkdir(self.root + '/tree')
        os.mkdir(self.root + '/outside')
        self.local('tree/file', b'intended')
        self.local('outside/file', b'private')
        manager, remote = self.manager()
        mkdir = remote.mkdir

        def swapped(path):
            os.rename(self.root + '/tree', self.root + '/moved')
            os.symlink(self.root + '/outside', self.root + '/tree')
            mkdir(path)
        remote.mkdir = swapped
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [self.root + '/tree'], '/logs', 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertFalse(remote.uploaded)
        self.assertEqual(pathlib.Path(self.root, 'outside/file').read_bytes(), b'private')

    def test_upload_cancel_removes_partial_and_cancels_remaining_folder_files(self):
        os.mkdir(self.root + '/tree')
        self.local('tree/a', b'first')
        self.local('tree/b', b'second')
        manager, remote = self.manager(FakeTransfer(stop_after_chunk=True))
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [self.root + '/tree'], '/logs', 'fail'))
        self.assertEqual(final['state'], 'cancelled')
        self.assertEqual([i['state'] for i in final['items']], ['cancelled', 'cancelled'])
        self.assertNotIn('/logs/tree/a', remote.tree)
        self.assertNotIn('/logs/tree/b', remote.tree)
        self.assertEqual(remote.removed, ['/logs/tree/a'])

    def test_upload_mode_failure_removes_created_file_and_continues(self):
        paths = [self.local('a'), self.local('b')]
        manager, remote = self.manager()
        remote.mode_failures.add('/logs/a')
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, paths, '/logs', 'fail'))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual([i['state'] for i in final['items']], ['failed', 'finished'])
        self.assertNotIn('/logs/a', remote.tree)
        self.assertEqual(remote.tree['/logs/b']['data'], b'abc')

    def test_scanning_phase_never_starts_destination_writes_early(self):
        entered, release = threading.Event(), threading.Event()
        manager, remote = self.manager()
        remote.directory('/tree')
        remote.file('/tree/file', b'data')
        listdir = remote.listdir

        def hold_scan(path):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test did not release scan')
            return listdir(path)
        remote.listdir = hold_scan
        self.addCleanup(release.set)
        job = manager.start_download(manager.opener, LABEL, ['/tree'], self.root, 'fail')
        self.assertTrue(entered.wait(1))
        current = manager.jobs()[0]
        self.assertEqual(current['phase'], 'scanning')
        self.assertIsNone(current['current'])
        self.assertEqual(os.listdir(self.root), [])
        release.set()
        self.assertEqual(self.finish(manager, job)['state'], 'finished')

    def test_empty_folder_creation_failure_still_fails_job(self):
        os.mkdir(self.root + '/empty')
        manager, remote = self.manager()
        remote.file('/logs/empty', b'keep')
        final = self.finish(manager, manager.start_upload(manager.opener, LABEL, [self.root + '/empty'], '/logs', 'skip'))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(final['files_total'], 0)
        self.assertIn('not a folder', final['error'])
        self.assertEqual(remote.tree['/logs/empty']['data'], b'keep')

    def test_job_limit_and_missing_ids_fail_with_plain_errors(self):
        manager, _ = self.manager()
        first = self.finish(manager, manager.start_download(manager.opener, LABEL, ['/log'], self.root, 'fail'))
        with mock.patch.object(jobs, 'MAX_JOBS', 1):
            with self.assertRaisesRegex(ValueError, 'Too many file transfer jobs'):
                manager.start_download(manager.opener, LABEL, ['/log1'], self.root, 'fail')
        for action in (manager.cancel, manager.retry):
            with self.assertRaisesRegex(ValueError, 'No file transfer job'):
                action('missing')
        self.assertEqual(manager.cancel(first['id'])['state'], 'finished')

    def test_upload_validates_every_top_source_synchronously(self):
        manager, _ = self.manager()
        opener = mock.Mock()
        good = self.local()
        os.symlink(good, self.root + '/link')
        os.mkfifo(self.root + '/pipe')
        for bad in ('relative', self.root + '/x/../source.log', self.root + '/missing',
                    self.root + '/link', self.root + '/pipe'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                manager.start_upload(opener, LABEL, [good, bad], '/logs', 'fail')
        self.assertEqual(manager.jobs(), [])
        opener.assert_not_called()
