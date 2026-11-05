"""Pipe-based SFTP protocol, credential boundary and file-transfer tests.

No ssh, sockets or real credential stores are used. Every disk write is under
TemporaryDirectory, including askpass helpers and isolated HOME/USERPROFILE.
MySQL/MariaDB parsing is unchanged: this is an engine-independent SSH protocol.
"""
import ast
import collections
import hashlib
import io
import json
import os
import posixpath
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from myflames import sftp, sftp_api as api, sftp_jobs as jobs

SPEC = {'ssh_host': 'example.test', 'ssh_user': 'reader', 'ssh_port': 2222}


def number(value):
    return struct.pack('>I', value)


def string(value):
    data = value.encode('utf-8') if isinstance(value, str) else value
    return number(len(data)) + data


class Reader(object):
    def __init__(self, data):
        self.data = io.BytesIO(data)

    def uint(self):
        return struct.unpack('>I', self.data.read(4))[0]

    def uint64(self):
        return struct.unpack('>Q', self.data.read(8))[0]

    def string(self):
        return self.data.read(self.uint())


class PipeServer(object):
    """Independent SFTP v3 peer with actual pipes and an in-memory filesystem."""

    def __init__(self, files=None, names=None, short_read=None, reorder=False,
                 stall=False, failure=None, status_error=None, raw_version=None):
        request_read, request_write = os.pipe()
        reply_read, reply_write = os.pipe()
        error_read, error_write = os.pipe()
        self.stdin = os.fdopen(request_write, 'wb', buffering=0)
        self.stdout = os.fdopen(reply_read, 'rb', buffering=0)
        self.stderr = os.fdopen(error_read, 'rb', buffering=0)
        self.requests = os.fdopen(request_read, 'rb', buffering=0)
        self.replies = os.fdopen(reply_write, 'wb', buffering=0)
        self.errors = os.fdopen(error_write, 'wb', buffering=0)
        self.files = {p: bytearray(v) for p, v in (files or {}).items()}
        self.modes = {p: 0o100640 for p in self.files}
        self.names = names if names is not None else ['log.txt', 'logs']
        self.handles = {}
        self.dir_read = set()
        self.calls = []
        self.short_read, self.reorder = short_read, reorder
        self.stall, self.failure, self.status_error = stall, failure, status_error
        self.raw_version = raw_version
        self.held = []
        self.max_held = 0
        self.returncode = None
        self.killed = False
        self.problems = []
        self.thread = threading.Thread(target=self.run, name='fake-sftp', daemon=True)
        self.thread.start()

    @staticmethod
    def read_exact(stream, size):
        result = bytearray()
        while len(result) < size:
            part = stream.read(size - len(result))
            if not part:
                raise EOFError()
            result.extend(part)
        return bytes(result)

    def packet(self, data):
        data = memoryview(number(len(data)) + data)
        while data:
            count = self.replies.write(data)
            data = data[count:]

    def status(self, rid, code=0, message='ok'):
        self.packet(bytes([101]) + number(rid) + number(code) + string(message) + string('en'))

    def attrs(self, path, extended=False):
        mode = self.modes.get(path, 0o40750 if path == 'logs' else 0o100640)
        size = len(self.files.get(path, b'abcdef'))
        flags = 15 | (0x80000000 if extended else 0)
        data = number(flags) + struct.pack('>Q', size) + number(501) + number(20) + number(mode)
        data += number(1600000000) + number(1700000000)
        if extended:
            data += number(1) + string('ignored') + string('attribute')
        return data

    def run(self):
        try:
            if self.failure is not None:
                self.errors.write(self.failure.encode('utf-8'))
                return
            while True:
                size = struct.unpack('>I', self.read_exact(self.requests, 4))[0]
                body = self.read_exact(self.requests, size)
                kind, packet = body[0], Reader(body[1:])
                if kind == 1:
                    assert packet.uint() == 3
                    if self.raw_version is not None:
                        self.replies.write(self.raw_version)
                    elif not self.stall:
                        self.packet(bytes([2]) + number(3))
                    continue
                rid = packet.uint()
                self.calls.append((kind, rid))
                if self.status_error is not None:
                    self.status(rid, *self.status_error)
                    continue
                if kind in (3, 11):
                    path = packet.string().decode('utf-8')
                    if kind == 3:
                        flags = packet.uint()
                        if flags & 2:
                            if flags & 32 and path in self.files:
                                self.status(rid, 4, 'File exists')
                                continue
                            if flags & 16 or path not in self.files:
                                self.files[path] = bytearray()
                                self.modes[path] = 0o100600
                        elif path not in self.files:
                            self.status(rid, 2, 'Missing file')
                            continue
                    handle = ('handle%d' % rid).encode('ascii')
                    self.handles[handle] = path
                    self.packet(bytes([102]) + number(rid) + string(handle))
                elif kind == 12:
                    handle = packet.string()
                    if handle in self.dir_read:
                        self.status(rid, 1, 'End of directory')
                        continue
                    self.dir_read.add(handle)
                    data = bytes([104]) + number(rid) + number(len(self.names))
                    for name in self.names:
                        data += string(name) + string('not parsed <longname>') + self.attrs(name, extended=True)
                    self.packet(data)
                elif kind == 4:
                    self.handles.pop(packet.string())
                    self.status(rid)
                elif kind == 5:
                    path = self.handles[packet.string()]
                    offset, amount = packet.uint64(), packet.uint()
                    if self.short_read:
                        amount = min(amount, self.short_read)
                    data = bytes(self.files[path][offset:offset + amount])
                    if not data:
                        self.status(rid, 1, 'End of file')
                        continue
                    reply = bytes([103]) + number(rid) + string(data)
                    if self.reorder:
                        self.held.append(reply)
                        self.max_held = max(self.max_held, len(self.held))
                        if len(self.held) == 8:
                            for held in reversed(self.held):
                                self.packet(held)
                            self.held = []
                            self.reorder = False
                    else:
                        self.packet(reply)
                elif kind == 6:
                    path = self.handles[packet.string()]
                    offset, data = packet.uint64(), packet.string()
                    content = self.files[path]
                    if offset + len(data) > len(content):
                        content.extend(b'\x00' * (offset + len(data) - len(content)))
                    content[offset:offset + len(data)] = data
                    self.status(rid)
                elif kind in (7, 17):
                    path = packet.string().decode('utf-8')
                    if path not in self.files:
                        self.status(rid, 2, 'Missing file')
                    else:
                        self.packet(bytes([105]) + number(rid) + self.attrs(path))
                elif kind == 16:
                    path = posixpath.normpath('/home/reader/' + packet.string().decode('utf-8'))
                    self.packet(bytes([104]) + number(rid) + number(1) + string(path) + string('') + number(0))
                elif kind in (13, 15):
                    path = packet.string().decode('utf-8')
                    if path not in self.files:
                        self.status(rid, 2, 'Missing file')
                    else:
                        del self.files[path]
                        self.status(rid)
                elif kind == 14:
                    path = packet.string().decode('utf-8')
                    self.files[path] = bytearray()
                    self.modes[path] = 0o40700
                    self.status(rid)
                elif kind == 18:
                    source, target = packet.string().decode('utf-8'), packet.string().decode('utf-8')
                    self.files[target] = self.files.pop(source)
                    self.modes[target] = self.modes.pop(source)
                    self.status(rid)
                elif kind == 9:
                    path = packet.string().decode('utf-8')
                    assert packet.uint() == 4
                    self.modes[path] = 0o100000 | packet.uint()
                    self.status(rid)
                else:
                    raise AssertionError('Unexpected SFTP type %d' % kind)
        except (EOFError, BrokenPipeError):
            pass
        except (OSError, ValueError) as exc:
            if not self.killed:
                self.problems.append(exc)
        except BaseException as exc:
            self.problems.append(exc)
        finally:
            for stream in (self.requests, self.replies, self.errors):
                stream.close()
            self.returncode = 255 if self.failure else 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.stdin.close()

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise subprocess.TimeoutExpired('fake-sftp', timeout)
        return self.returncode


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-sftp-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.realpath(self.temp.name)
        self.env = mock.patch.dict(os.environ, {'HOME': self.root, 'USERPROFILE': self.root,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patch = mock.patch.object(tempfile, 'tempdir', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(api.close_sessions)
        self.addCleanup(sftp.close_all)

    def session(self, **kwargs):
        password = kwargs.pop('password', None)
        server = PipeServer(**kwargs)
        self.addCleanup(self.check_server, server)
        session = sftp.SftpSession(SPEC, password=password, _process=server, packet_timeout=1)
        self.addCleanup(session.close)
        return session, server

    def check_server(self, server):
        server.kill()
        server.wait(2)
        for stream in (server.stdin, server.stdout, server.stderr):
            stream.close()
        self.assertFalse(server.problems)

    def local(self, name='source.log', data=b'abc'):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as stream:
            stream.write(data)
        return path


class TestProtocol(Isolated):
    def test_listdir_parses_attrs_ignores_longname_and_dot(self):
        session, server = self.session(names=['.', 'log.txt', 'logs'])
        result = session.listdir('/a/./b/..')
        self.assertEqual([r['name'] for r in result], ['log.txt', 'logs'])
        self.assertEqual(result[0], {'name': 'log.txt', 'size': 6, 'mtime': 1700000000,
                                     'mode': 0o100640, 'is_dir': False})
        self.assertTrue(result[1]['is_dir'])
        self.assertEqual(server.handles, {})

    def test_hostile_readdir_parent_refused_and_handle_closed(self):
        session, server = self.session(names=['..'])
        with self.assertRaisesRegex(sftp.SftpError, 'unsafe directory entry'):
            session.listdir('.')
        self.assertEqual(server.handles, {})

    def test_hostile_readdir_separator_refused(self):
        session, server = self.session(names=['../../secret'])
        with self.assertRaisesRegex(sftp.SftpError, 'unsafe directory entry'):
            session.listdir('.')
        self.assertEqual(server.handles, {})

    def test_get_pipelines_eight_reordered_packets_with_byte_integrity(self):
        data = bytes(range(256)) * (sftp.CHUNK_SIZE * 9 // 256) + b'end'
        session, server = self.session(files={'/log': data}, reorder=True)
        output, progress = io.BytesIO(), []
        self.assertEqual(session.get('/log', output, len(data), progress.append, threading.Event()), len(data))
        self.assertEqual(output.getvalue(), data)
        self.assertEqual(server.max_held, 8)
        self.assertEqual(progress, [sftp.CHUNK_SIZE * i for i in range(1, 10)] + [len(data)])
        self.assertEqual(server.handles, {})

    def test_get_repairs_short_reads_and_early_eof_fails(self):
        data = bytes(range(256)) * 2100
        session, server = self.session(files={'/log': data}, short_read=13000)
        output, progress = io.BytesIO(), []
        session.get('/log', output, len(data), progress.append, threading.Event())
        self.assertEqual(output.getvalue(), data)
        self.assertEqual(progress[-1], len(data))
        with self.assertRaises(sftp.SftpError):
            session.get('/log', io.BytesIO(), len(data) + 1, lambda n: None, threading.Event())
        self.assertEqual(server.handles, {})

    def test_put_integrity_progress_and_exclusive_open(self):
        data = b'\x00\xff\n' * 800000
        session, server = self.session()
        progress = []
        session.put(io.BytesIO(data), '/new', len(data), progress.append, threading.Event())
        self.assertEqual(bytes(server.files['/new']), data)
        self.assertEqual(progress[-1], len(data))
        self.assertEqual(len(progress), 10)
        with self.assertRaises(sftp.SftpError):
            session.put(io.BytesIO(b'bad'), '/new', 3, lambda n: None, threading.Event())
        self.assertEqual(bytes(server.files['/new']), data)
        session.put(io.BytesIO(b'ok'), '/new', 2, lambda n: None, threading.Event(), overwrite=True)
        self.assertEqual(bytes(server.files['/new']), b'ok')

    def test_overwrite_refuses_remote_symlink_without_sending_data(self):
        session, server = self.session(files={'/link': b'keep'})
        server.modes['/link'] = 0o120777
        with self.assertRaisesRegex(sftp.SftpError, 'symbolic link'):
            session.put(io.BytesIO(b'bad'), '/link', 3, lambda n: None, threading.Event(), overwrite=True)
        self.assertEqual(bytes(server.files['/link']), b'keep')
        self.assertEqual([kind for kind, _ in server.calls], [7])

    def test_overwrite_race_after_remove_uses_exclusive_open(self):
        session, server = self.session(files={'/log': b'old'})
        remove = session.remove

        def replace_with_racer(path):
            remove(path)
            server.files[path] = bytearray(b'racer')
        session.remove = replace_with_racer
        with self.assertRaises(sftp.SftpError):
            session.put(io.BytesIO(b'bad'), '/log', 3, lambda n: None, threading.Event(), overwrite=True)
        self.assertEqual(bytes(server.files['/log']), b'racer')

    def test_cancel_get_drains_pipeline_and_reuses_session(self):
        data = b'x' * (sftp.CHUNK_SIZE * 10)
        session, server = self.session(files={'/log': data})
        stop, output, progress = threading.Event(), io.BytesIO(), []

        def report(count):
            progress.append(count)
            stop.set()
        with self.assertRaises(sftp.SftpCancelled):
            session.get('/log', output, len(data), report, stop)
        self.assertEqual(output.getvalue(), data[:sftp.CHUNK_SIZE])
        self.assertEqual(progress, [sftp.CHUNK_SIZE])
        self.assertEqual(server.handles, {})
        self.assertEqual(session.stat('/log')['size'], len(data))

    def test_cancel_put_removes_partial_file(self):
        data = b'Z' * (sftp.CHUNK_SIZE * 10)
        session, server = self.session()
        stop = threading.Event()
        with self.assertRaises(sftp.SftpCancelled):
            session.put(io.BytesIO(data), '/partial', len(data), lambda n: stop.set(), stop)
        self.assertNotIn('/partial', server.files)
        self.assertEqual(server.handles, {})

    def test_cancel_before_open_sends_nothing(self):
        session, server = self.session()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(sftp.SftpCancelled):
            session.put(io.BytesIO(b'abc'), '/x', 3, lambda n: None, stop)
        self.assertFalse(server.calls)

    def test_empty_files_get_and_put_close_handles(self):
        session, server = self.session(files={'/empty': b''})
        output = io.BytesIO()
        session.get('/empty', output, 0, lambda n: self.fail('no chunks'), threading.Event())
        session.put(output, '/new', 0, lambda n: self.fail('no chunks'), threading.Event())
        self.assertEqual(server.files['/new'], b'')
        self.assertEqual(server.handles, {})

    def test_mutations_stat_lstat_realpath_and_modes(self):
        session, server = self.session(files={'/log': b'abc'})
        self.assertEqual(session.realpath('a/../b'), '/home/reader/b')
        self.assertEqual(session.stat('/log')['size'], 3)
        self.assertFalse(session.lstat('/log')['is_dir'])
        session.setstat('/log', 0o640)
        session.rename('/log', '/new')
        session.remove('/new')
        session.mkdir('/dir')
        session.rmdir('/dir')
        self.assertEqual(server.files, {})
        self.assertEqual([kind for kind, _ in server.calls], [16, 17, 7, 9, 18, 13, 14, 15])

    def test_status_mapping_includes_server_message(self):
        session, _ = self.session(status_error=(3, 'No permission for logs'))
        with self.assertRaisesRegex(sftp.SftpError, 'Permission denied: No permission for logs') as caught:
            session.stat('/log')
        self.assertEqual(caught.exception.code, 3)

    def test_timeout_kills_process_and_cleans_readers(self):
        server = PipeServer(stall=True)
        self.addCleanup(self.check_server, server)
        with self.assertRaisesRegex(sftp.SftpError, 'timed out'):
            sftp.SftpSession(SPEC, _process=server, packet_timeout=0.03)
        self.assertTrue(server.killed)
        self.assertFalse(server.thread.is_alive())

    def test_remote_path_normalization_and_invalid_types(self):
        self.assertEqual(sftp.remote_path('/a/../b/./c'), '/b/c')
        for bad in (None, 5, '', 'a\x00b'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sftp.remote_path(bad)

    def test_packet_cursor_rejects_truncation_and_unknown_attrs(self):
        with self.assertRaises(sftp.SftpError):
            sftp._Cursor(number(100) + b'short').string()
        with self.assertRaises(sftp.SftpError):
            sftp._Cursor(number(16)).attrs()

    def test_oversized_frame_and_slow_partial_packet_kill_transport(self):
        for header in (number(sftp.MAX_PACKET + 1), number(5) + b'\x02\x00'):
            server = PipeServer(raw_version=header)
            self.addCleanup(self.check_server, server)
            with self.subTest(header=header), self.assertRaises(sftp.SftpError):
                sftp.SftpSession(SPEC, _process=server, packet_timeout=0.03)
            self.assertTrue(server.killed)

    def test_shrinking_upload_removes_file_and_does_not_report_success(self):
        session, server = self.session()
        with self.assertRaisesRegex(sftp.SftpError, 'became shorter'):
            session.put(io.BytesIO(b'abc'), '/short', 100, lambda n: None, threading.Event())
        self.assertNotIn('/short', server.files)
        self.assertEqual(server.handles, {})

    def test_listing_count_includes_discarded_dot_entries(self):
        session, server = self.session(names=['.', '.', '.'])
        with mock.patch.object(sftp, 'MAX_ENTRIES', 2), self.assertRaisesRegex(sftp.SftpError, 'too large'):
            session.listdir('.')
        self.assertEqual(server.handles, {})

    def test_zero_status_cannot_substitute_for_file_attributes(self):
        session, _ = self.session(status_error=(0, 'success'))
        with self.assertRaisesRegex(sftp.SftpError, 'without the requested data'):
            session.stat('/log')


class TestAuthentication(Isolated):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(api._AUTH_REFS, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.secrets = {}
        self.reads = []
        self.saved_keys = []

        def save(key, password):
            self.secrets[key] = password
            self.saved_keys.append(key)
            return 'fake'

        def get(key, backend=None):
            self.reads.append((key, backend))
            return self.secrets.get(key)

        def delete(key, backend=None):
            self.secrets.pop(key, None)
        for name, function in (('save_secret', save), ('get_secret', get), ('delete_secret', delete)):
            patch = mock.patch.object(api.secret_store, name, side_effect=function)
            patch.start()
            self.addCleanup(patch.stop)

    def test_auth_save_status_clear_are_secret_free_shapes(self):
        self.assertEqual(api.api_ssh_auth_status(SPEC), {'has_password': False})
        self.assertEqual(api.api_ssh_auth_save(SPEC, 'secret-value'), {'saved': True})
        self.assertEqual(api.api_ssh_auth_status(SPEC), {'has_password': True})
        key = self.saved_keys[0]
        fingerprint = hashlib.sha256(b'example.test\x00reader\x002222').hexdigest()[:24]
        self.assertRegex(key, '^sshpw\\.' + fingerprint + '\\.[0-9a-f]{16}$')
        self.assertEqual(api.api_ssh_auth_clear(SPEC), {'cleared': True})
        self.assertEqual(api.api_ssh_auth_status(SPEC), {'has_password': False})
        self.assertEqual(self.secrets, {})

    def test_endpoint_binding_checked_before_secret_store_read(self):
        api.api_ssh_auth_save(SPEC, 'A-only')
        other = dict(SPEC, ssh_host='other.test')
        self.assertEqual(api.api_ssh_auth_status(other), {'has_password': False})
        self.assertEqual(self.reads, [])
        api._AUTH_REFS[api._endpoint(other)] = api._AUTH_REFS[api._endpoint(SPEC)]
        with self.assertRaisesRegex(ValueError, 'different SSH details'):
            api._password_for(other)
        self.assertEqual(self.reads, [])
        for change in ({'ssh_user': 'another'}, {'ssh_port': 22}):
            changed = dict(SPEC, **change)
            self.assertFalse(api._matches(self.saved_keys[0], changed))

    def test_normalized_endpoint_and_config_port_distinction(self):
        api.api_ssh_auth_save(dict(SPEC, ssh_host=' example.test ', ssh_port='2222'), 'saved')
        self.assertEqual(api._password_for(SPEC), 'saved')
        self.assertNotEqual(api._fingerprint({'ssh_host': 'example.test'}),
                            api._fingerprint({'ssh_host': 'example.test', 'ssh_port': 22}))

    def test_replacement_deletes_old_secret_and_drops_cache(self):
        session = mock.Mock()
        api._SESSIONS[api._endpoint(SPEC)] = {'session': session, 'timer': None}
        api.api_ssh_auth_save(SPEC, 'first')
        session.close.assert_called_once()
        first = self.saved_keys[-1]
        api.api_ssh_auth_save(SPEC, 'second')
        self.assertNotIn(first, self.secrets)
        self.assertEqual(api._password_for(SPEC), 'second')

    def test_password_validation_and_backend_error_hygiene(self):
        for bad in (None, True, 123, '', 'x' * 1025, 'two\nlines', 'nul\x00'):
            with self.subTest(bad=type(bad)), self.assertRaises(ValueError):
                api.api_ssh_auth_save(SPEC, bad)
        with mock.patch.object(api.secret_store, 'save_secret', side_effect=ValueError('typed-secret leaked')):
            with self.assertRaises(ValueError) as caught:
                api.api_ssh_auth_save(SPEC, 'typed-secret')
        self.assertNotIn('typed-secret', str(caught.exception))
        self.assertEqual(api._AUTH_REFS, {})

    def test_askpass_modes_exclusive_creation_single_read_and_cleanup(self):
        helper = sftp._Askpass('one-use-123')
        self.addCleanup(helper.close)
        self.assertEqual(stat.S_IMODE(os.stat(helper.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(helper.helper).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(helper.secret_file).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            helper._write(helper.secret_file, b'replaced', 0o600)
        process = subprocess.run([helper.helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b'one-use-123\n')
        self.assertFalse(os.path.exists(helper.secret_file))
        again = subprocess.run([helper.helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        self.assertNotEqual(again.returncode, 0)
        self.assertNotIn(b'one-use-123', again.stdout + again.stderr)
        helper.close()
        self.assertFalse(os.path.exists(helper.directory))

    def test_argv_and_environment_never_contain_password(self):
        seen = {}
        server = PipeServer()
        self.addCleanup(self.check_server, server)

        def spawn(argv, **kwargs):
            seen.update(argv=argv, kwargs=kwargs)
            helper = kwargs['env']['SSH_ASKPASS']
            self.assertTrue(os.path.isfile(helper))
            self.assertNotIn('private-password', json.dumps(argv) + json.dumps(kwargs['env']))
            self.assertNotIn('BatchMode=yes', argv)
            self.assertTrue(kwargs['start_new_session'])
            self.assertEqual(kwargs['stdin'], subprocess.PIPE)
            self.assertEqual(kwargs['stdout'], subprocess.PIPE)
            return server
        with mock.patch.object(sftp.subprocess, 'Popen', side_effect=spawn), \
                mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'):
            session = sftp.SftpSession(SPEC, password='private-password')
        session._owns_group = False
        self.addCleanup(session.close)
        self.assertEqual(seen['argv'][-3:], ['--', 'example.test', 'sftp'])
        self.assertEqual(seen['kwargs']['env']['SSH_ASKPASS_REQUIRE'], 'force')
        self.assertFalse(os.path.exists(os.path.dirname(seen['kwargs']['env']['SSH_ASKPASS'])))
        session.close()

    def test_default_argv_uses_batchmode_and_allowlisted_environment(self):
        server = PipeServer()
        self.addCleanup(self.check_server, server)
        with mock.patch.dict(os.environ, {'MYSQL_PWD': 'db-secret', 'SSH_ASKPASS': '/bad/helper'}), \
                mock.patch.object(sftp.subprocess, 'Popen', return_value=server) as spawn, \
                mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'):
            session = sftp.SftpSession(SPEC)
        session._owns_group = False
        self.addCleanup(session.close)
        argv, kwargs = spawn.call_args[0][0], spawn.call_args[1]
        self.assertIn('BatchMode=yes', argv)
        self.assertNotIn('MYSQL_PWD', kwargs['env'])
        self.assertNotIn('SSH_ASKPASS', kwargs['env'])
        self.assertEqual(argv[argv.index('-p') + 1], '2222')
        self.assertEqual(argv[argv.index('-l') + 1], 'reader')

    def test_stderr_and_status_password_are_redacted(self):
        server = PipeServer(failure='Bad transport for very-private-password')
        self.addCleanup(self.check_server, server)
        with self.assertRaises(sftp.SftpError) as caught:
            sftp.SftpSession(SPEC, password='very-private-password', _process=server)
        self.assertNotIn('very-private-password', str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))
        session, _ = self.session(password='very-private-password', status_error=(4, 'very-private-password rejected'))
        with self.assertRaises(sftp.SftpError) as caught:
            session.stat('/log')
        self.assertNotIn('very-private-password', str(caught.exception))

    def test_key_auth_failure_has_plain_actionable_error(self):
        server = PipeServer(failure='reader@example.test: Permission denied (publickey).')
        self.addCleanup(self.check_server, server)
        with self.assertRaisesRegex(sftp.SftpAuthError, 'Save a password or fix your SSH keys'):
            sftp.SftpSession(SPEC, _process=server)

    def test_frozen_password_mode_fails_clearly_before_spawn(self):
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sftp.ssh_tunnel, '_is_program', return_value=False), \
                mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            with self.assertRaises(sftp.SftpAuthError):
                sftp.SftpSession(SPEC, password='secret')
        spawn.assert_not_called()

    def test_frozen_helper_uses_absolute_system_interpreter(self):
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.dict(os.environ, {'PATH': '.:relative:/usr/bin'}), \
                mock.patch.object(sftp.ssh_tunnel, '_is_program', side_effect=lambda p: p == '/usr/bin/python3'):
            self.assertEqual(sftp._askpass_python(), '/usr/bin/python3')

    def test_windows_keys_spawn_suspended_and_join_job_before_protocol(self):
        from myflames import win_job  # Load ctypes before simulating Windows.
        server = PipeServer()
        self.addCleanup(self.check_server, server)
        with mock.patch.object(os, 'name', 'nt'), \
                mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='C:\\Windows\\ssh.exe'), \
                mock.patch.object(sftp.ssh_tunnel, 'ssh_environment', return_value={}), \
                mock.patch.object(sftp.ssh_tunnel, '_join_job') as join, \
                mock.patch.object(sftp.subprocess, 'Popen', return_value=server) as spawn:
            session = sftp.SftpSession(SPEC)
        session._owns_group = False
        self.addCleanup(session.close)
        join.assert_called_once_with(server)
        self.assertTrue(spawn.call_args[1]['creationflags'] & win_job.CREATE_SUSPENDED)
        self.assertNotIn('start_new_session', spawn.call_args[1])

    def test_windows_password_fails_closed_without_process(self):
        with mock.patch.object(os, 'name', 'nt'), mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(sftp.SftpAuthError, 'Windows'):
                sftp.SftpSession(SPEC, password='secret')
        spawn.assert_not_called()

    def test_askpass_setup_error_never_exposes_secret_or_temp_path(self):
        with mock.patch.object(sftp, '_Askpass', side_effect=OSError('secret /private/hidden/helper')), \
                mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'), \
                mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            with self.assertRaises(sftp.SftpAuthError) as caught:
                sftp.SftpSession(SPEC, password='secret')
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('/private/hidden', str(caught.exception))
        spawn.assert_not_called()


class FakeTransfer(object):
    def __init__(self, data=b'downloaded', entered=None, release=None, stop_after_chunk=False, failure=False):
        self.data, self.entered, self.release = data, entered, release
        self.stop_after_chunk, self.failure = stop_after_chunk, failure
        self.closed = False
        self.uploaded = {}
        self.removed = []
        self.modes = []
        self._password = None

    def safe(self, text):
        return text

    def stat(self, path):
        return {'size': len(self.data), 'mode': 0o100600, 'is_dir': False}

    def lstat(self, path):
        raise sftp.SftpError('Missing', 2)

    def hold(self, stop):
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(3):
            raise RuntimeError('Test did not release worker')
        if stop.is_set():
            raise sftp.SftpCancelled('File transfer cancelled.')

    def get(self, remote, fd, size, progress, stop):
        self.hold(stop)
        os.write(fd, self.data)
        progress(len(self.data))
        if self.stop_after_chunk:
            stop.set()
            raise sftp.SftpCancelled('File transfer cancelled.')
        if self.failure:
            raise sftp.SftpError('Remote read failed')

    def put(self, fd, remote, size, progress, stop, overwrite=False):
        self.hold(stop)
        self.uploaded[remote] = os.read(fd, size)
        progress(size)

    def setstat(self, path, mode):
        self.modes.append((path, mode))

    def remove(self, path):
        self.removed.append(path)

    def close(self):
        self.closed = True
        if self.release:
            self.release.set()


class TestJobs(Isolated):
    def manager(self, session=None, factory=None, clock=None, store=None):
        session = session or FakeTransfer()
        manager = jobs.SftpJobManager(store=store, session_factory=factory or (lambda spec: session),
                                      **({'clock': clock} if clock else {}))
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
        job = manager.start_download(SPEC, ['/var/log/mysql.log'], self.root, False)
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
        final = self.finish(manager, manager.start_upload(SPEC, [path], '/logs', False))
        self.assertEqual(final['state'], 'finished')
        self.assertEqual(session.uploaded['/logs/source.log'], b'upload-payload')
        self.assertEqual(session.modes, [('/logs/source.log', 0o750)])

    def test_local_symlink_component_and_leaf_refused(self):
        actual = os.path.join(self.root, 'actual')
        os.mkdir(actual)
        link = os.path.join(self.root, 'linked')
        os.symlink(actual, link)
        source = self.local()
        leaf = os.path.join(self.root, 'leaf')
        os.symlink(source, leaf)
        manager, _ = self.manager()
        for path in (link, link + '/sub/..'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.start_download(SPEC, ['/log'], path, False)
        with self.assertRaises(ValueError):
            manager.start_upload(SPEC, [leaf], '/', False)
        with self.assertRaises(ValueError):
            manager.start_download(SPEC, ['/leaf'], self.root, True)

    def test_existing_final_requires_overwrite_and_remains_intact(self):
        path = self.local('log', b'original')
        manager, _ = self.manager()
        with self.assertRaisesRegex(ValueError, 'Enable overwrite'):
            manager.start_download(SPEC, ['/log'], self.root, False)
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'original')
        final = self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, True))
        self.assertEqual(final['state'], 'finished')
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), b'downloaded')

    def test_target_traversal_and_windows_names_refused(self):
        manager, _ = self.manager()
        for path in ('/logs/..', '/logs/.', '/logs/..\\outside', '/logs/C:secret', '/logs/NUL', '/logs/name.'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manager.start_download(SPEC, [path], self.root, False)
        self.assertEqual(os.listdir(self.root), [])

    def test_cancel_and_failure_remove_only_owned_partial(self):
        for cancel in (True, False):
            manager, _ = self.manager(FakeTransfer(stop_after_chunk=cancel, failure=not cancel))
            final = self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, False))
            self.assertEqual(final['state'], 'cancelled' if cancel else 'failed')
            self.assertEqual(os.listdir(self.root), [])
        part = self.local('log.myflames-part', b'previous-part')
        manager, _ = self.manager()
        final = self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, False))
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
        final = self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, False))
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
            if len(checks) == 3:
                self.local('log', b'racing-file')
        with mock.patch.object(jobs._LocalDirectory, 'check_final', racing):
            final = self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, False))
        self.assertEqual(final['state'], 'failed')
        with open(os.path.join(self.root, 'log'), 'rb') as stream:
            self.assertEqual(stream.read(), b'racing-file')

    def test_directory_swapped_for_symlink_after_preflight_refused(self):
        target = os.path.join(self.root, 'target')
        outside = os.path.join(self.root, 'outside')
        os.mkdir(target)
        os.mkdir(outside)

        def factory(spec):
            os.rmdir(target)
            os.symlink(outside, target)
            return FakeTransfer()
        manager, _ = self.manager(factory=factory)
        final = self.finish(manager, manager.start_download(SPEC, ['/log'], target, False))
        self.assertEqual(final['state'], 'failed')
        self.assertEqual(os.listdir(outside), [])

    def test_duplicate_targets_and_staging_collisions_refused(self):
        manager, _ = self.manager()
        for paths in (['/a/log', '/b/log'], ['/log', '/log.myflames-part']):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                manager.start_download(SPEC, paths, self.root, False)

    def test_shutdown_prunes_expired_terminal_job_without_error(self):
        now = [0.0]
        manager, _ = self.manager(clock=lambda: now[0])
        self.finish(manager, manager.start_download(SPEC, ['/log'], self.root, False))
        now[0] = 3601
        manager.shutdown()
        self.assertEqual(manager.jobs(), [])

    def test_queue_two_running_rest_queued_and_cancel(self):
        release = threading.Event()
        entered = [threading.Event(), threading.Event()]
        count = []

        def factory(spec):
            index = len(count)
            session = FakeTransfer(entered=entered[index], release=release)
            count.append(session)
            return session
        manager, _ = self.manager(factory=factory)
        path = self.local()
        first = manager.start_upload(SPEC, [path], '/one', False)
        second = manager.start_upload(SPEC, [path], '/two', False)
        self.assertTrue(entered[0].wait(1))
        self.assertTrue(entered[1].wait(1))
        third = manager.start_upload(SPEC, [path], '/three', False)
        self.assertEqual([j['state'] for j in manager.jobs()], ['running', 'running', 'queued'])
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

        def factory(spec):
            index = len(count)
            instance = FakeTransfer(entered=entered[index] if index < 2 else None,
                                    release=release if index < 2 else None)
            count.append(instance)
            return instance
        manager, _ = self.manager(factory=factory)
        path = self.local()
        first = manager.start_upload(SPEC, [path], '/one', False)
        second = manager.start_upload(SPEC, [path], '/two', False)
        self.assertTrue(entered[0].wait(1))
        self.assertTrue(entered[1].wait(1))
        third = manager.start_upload(SPEC, [path], '/three', False)
        release.set()
        self.finish(manager, first)
        self.finish(manager, second)
        self.assertEqual(self.finish(manager, third)['state'], 'finished')
        self.assertEqual(len(count), 3)

    def test_eta_rolling_window_with_fake_clock(self):
        now = [10.0]
        manager, _ = self.manager(clock=lambda: now[0])
        item = {'source': 'x', 'target': 'y', 'bytes': 1000, 'done': 0, 'state': 'running', 'error': None}
        job = jobs._Job('download', SPEC, [item], False, now[0])
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
        job = manager.start_download(SPEC, ['/log'], self.root, False)
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
            self.finish(manager, manager.start_download(SPEC, ['/log%d' % index], self.root, False))
        self.assertEqual([r['outcome'] for r in store.records], ['finished', 'failed', 'cancelled'])
        self.assertEqual([r['status'] for r in store.records], ['ok', 'error', 'error'])
        self.assertTrue(all(r['operation'] == 'sftp_download' for r in store.records))


class TestApi(Isolated):
    def test_connection_missing_host_exact_message_and_strict_types(self):
        with self.assertRaises(ValueError) as caught:
            api.api_listdir({}, '.')
        self.assertEqual(str(caught.exception), 'This connection has no SSH details. Add an SSH host to the saved connection to transfer files.')
        for value in ([], None, {'ssh_host': 1}, {'ssh_host': 'host', 'ssh_user': False},
                      {'ssh_host': 'host', 'ssh_port': True}, {'ssh_host': 'host', 'ssh_port': 2.5},
                      {'ssh_host': 'host', 'ssh_port': '2_222'}, {'ssh_host': 'host', 'ssh_port': '+22'},
                      {'ssh_host': '-oProxyCommand=bad'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api.api_realpath(value, '.')

    def test_transfer_shapes_no_coercion_and_unknown_fields(self):
        valid = {'connection': SPEC, 'local_paths': ['/a'], 'remote_dir': '/logs', 'overwrite': False}
        for changes in ({'overwrite': 1}, {'overwrite': 'true'}, {'local_paths': '/a'}, {'local_paths': [1]},
                        {'local_paths': []}, {'remote_dir': 2}, {'secret_ref': 'external'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                api.api_start_upload(dict(valid, **changes))
        with mock.patch.object(jobs, 'start_upload', return_value={'id': 'job'}) as start:
            self.assertEqual(api.api_start_upload(valid), {'id': 'job'})
        self.assertEqual(start.call_args[0], (SPEC, ['/a'], '/logs', False))
        for job_id in (None, 1, '', '../job'):
            with self.assertRaises(ValueError):
                api.api_cancel(job_id)

    def test_cached_session_reuse_idle_close_and_auth_drop(self):
        session = mock.Mock()
        session._password = None
        session.alive.return_value = True
        session.listdir.return_value = []
        session.safe.side_effect = lambda text: text
        session.realpath.return_value = '/home/reader'
        with mock.patch.object(api, 'open_session', return_value=session) as factory, \
                mock.patch.object(api.time, 'monotonic', return_value=10):
            self.assertEqual(api.api_listdir(SPEC, '.'), [])
            self.assertEqual(api.api_realpath(SPEC, '.'), '/home/reader')
            factory.assert_called_once_with(SPEC)
        key = api._endpoint(SPEC)
        entry = api._SESSIONS[key]
        with mock.patch.object(api.time, 'monotonic', return_value=129):
            api._expire(key, entry)
        session.close.assert_not_called()
        with mock.patch.object(api.time, 'monotonic', return_value=130):
            api._expire(key, entry)
        session.close.assert_called_once()
        self.assertEqual(api._SESSIONS, {})

    def test_new_modules_parse_as_python37_and_only_stdlib_dependencies(self):
        allowed = {'atexit', 'collections', 'contextlib', 'copy', 'datetime', 'hashlib', 'hmac', 'os', 'posixpath',
                   'queue', 're', 'secrets', 'shlex', 'shutil', 'signal', 'stat', 'struct', 'subprocess', 'sys',
                   'tempfile', 'threading', 'time'}
        root = os.path.dirname(os.path.dirname(__file__))
        for module in ('sftp.py', 'sftp_jobs.py', 'sftp_api.py'):
            with open(os.path.join(root, 'myflames', module), encoding='utf-8') as stream:
                source = stream.read()
            tree = ast.parse(source, feature_version=(3, 7)) if sys.version_info >= (3, 8) else ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    self.assertTrue(all(n.name.split('.')[0] in allowed for n in node.names), module)
                if isinstance(node, ast.ImportFrom) and node.level == 0:
                    self.assertIn(node.module.split('.')[0], allowed, module)


if __name__ == '__main__':
    unittest.main()
