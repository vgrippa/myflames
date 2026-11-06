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

from myflames import sftp, sftp_api as api, sftp_jobs as jobs, ssh_connections as ssh
from myflames.workspace_store import WorkspaceStore

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
                 stall=False, failure=None, status_error=None, raw_version=None, limits=None):
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
        self.pid = 2 ** 22 + 7  # Popen's pid, which the askpass helper is bound to.
        # (read, write) data limits advertised like OpenSSH's limits@openssh.com,
        # or None for a server without the extension.
        self.limits = limits
        self.largest_write = 0
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
                        extension = string('limits@openssh.com') + string('1') if self.limits else b''
                        self.packet(bytes([2]) + number(3) + extension)
                    continue
                rid = packet.uint()
                self.calls.append((kind, rid))
                if self.status_error is not None:
                    self.status(rid, *self.status_error)
                    continue
                if kind == 200:
                    assert packet.string() == b'limits@openssh.com'
                    read, write = self.limits
                    self.packet(bytes([201]) + number(rid) + struct.pack('>QQQQ', 256 * 1024, read, write, 0))
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
                    self.largest_write = max(self.largest_write, len(data))
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
                elif kind == 10:
                    path = self.handles[packet.string()]
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
                                               'MYFLAMES_SECRET_BACKEND': 'vault', 'MYFLAMES_SSH_HOME': self.root})
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
        session = sftp.SftpSession(SPEC, auth='password' if password else 'agent', secret=password, _process=server, packet_timeout=1)
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

    def test_readdir_dot_entries_skipped_and_handle_closed(self):
        # OpenSSH's sftp-server lists "." and ".." in every folder (found live:
        # refusing ".." made every real listing fail).
        session, server = self.session(names=['.', '..', 'log.txt'])
        self.assertEqual([entry['name'] for entry in session.listdir('.')], ['log.txt'])
        self.assertEqual(server.handles, {})

    def test_hostile_readdir_separator_refused(self):
        session, server = self.session(names=['../../secret'])
        with self.assertRaisesRegex(sftp.SftpError, 'unsafe directory entry'):
            session.listdir('.')
        self.assertEqual(server.handles, {})

    def test_get_pipelines_eight_reordered_packets_with_byte_integrity(self):
        data = bytes(range(256)) * (sftp.CHUNK_SIZE * 9 // 256) + b'end'
        session, server = self.session(files={'/log': data}, reorder=True, limits=(sftp.CHUNK_SIZE, sftp.CHUNK_SIZE))
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

    def test_upload_mode_is_set_on_the_handle_before_close_never_by_path(self):
        # Security review: SETSTAT by path after CLOSE follows a link another
        # remote user swaps in, so the mode goes on the open handle instead.
        session, server = self.session()
        session.put(io.BytesIO(b'data'), '/up', 4, lambda n: None, threading.Event(), mode=0o640)
        kinds = [kind for kind, _ in server.calls]
        self.assertNotIn(9, kinds, 'no SETSTAT by path')
        self.assertLess(kinds.index(10), len(kinds) - 1 - kinds[::-1].index(4), 'FSETSTAT before the last CLOSE')
        self.assertEqual(kinds[kinds.index(10) + 1], 4, 'CLOSE follows the FSETSTAT')
        self.assertEqual(server.modes['/up'], 0o100640)
        with self.assertRaises(ValueError):
            session.put(io.BytesIO(b'x'), '/bad', 1, lambda n: None, threading.Event(), mode=0o4755)

    def test_chunks_follow_stated_limits_else_the_safe_32_kib(self):
        data = os.urandom(300 * 1024)
        for limits, expected in ((None, sftp.SAFE_CHUNK), ((261120, 261120), 261120), ((8192, 4096), 4096)):
            with self.subTest(limits=limits):
                session, server = self.session(files={'/up': b''}, limits=limits)
                self.assertEqual(session.write_chunk, expected)
                local = self.local('payload', data)
                with open(local, 'rb') as stream:
                    session.put(stream, '/new', len(data), lambda done: None, threading.Event())
                self.assertEqual(bytes(server.files['/new']), data)
                self.assertEqual(server.largest_write, expected)

    def test_put_integrity_progress_and_exclusive_open(self):
        data = b'\x00\xff\n' * 800000
        session, server = self.session(limits=(sftp.CHUNK_SIZE, sftp.CHUNK_SIZE))
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
        session, server = self.session(files={'/log': data}, limits=(sftp.CHUNK_SIZE, sftp.CHUNK_SIZE))
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
        with self.assertRaisesRegex(sftp.SftpLocalError, 'became shorter') as caught:
            session.put(io.BytesIO(b'abc'), '/short', 100, lambda n: None, threading.Event())
        # A local problem, not a transport failure: the session goes on.
        self.assertEqual(caught.exception.code, 0)
        self.assertNotIn('/short', server.files)
        self.assertEqual(server.handles, {})
        session.put(io.BytesIO(b'next'), '/next', 4, lambda n: None, threading.Event())
        self.assertEqual(bytes(server.files['/next']), b'next')

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
    def test_askpass_modes_exclusive_creation_single_read_and_cleanup(self):
        helper = sftp._Askpass('one-use-123')
        self.addCleanup(helper.close)
        self.assertEqual(stat.S_IMODE(os.stat(helper.directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(helper.helper).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(helper.secret_file).st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            helper._write(helper.secret_file, b'replaced', 0o600)
        # Bound to this process (the helper's parent below), as sftp.py binds
        # it to the ssh it starts. A grandchild (a ProxyJump ssh) gets nothing
        # and the secret stays for the real parent.
        helper.bind(os.getpid())
        self.assertEqual(stat.S_IMODE(os.stat(helper.pid_file).st_mode), 0o600)
        proxy = subprocess.run(['/bin/sh', '-c', '/bin/sh "$0"; exit $?', helper.helper],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        self.assertNotEqual(proxy.returncode, 0)
        self.assertNotIn(b'one-use-123', proxy.stdout + proxy.stderr)
        self.assertTrue(os.path.exists(helper.secret_file), 'a refused caller does not consume the secret')
        process = subprocess.run(['/bin/sh', helper.helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b'one-use-123\n')
        self.assertFalse(os.path.exists(helper.secret_file))
        again = subprocess.run(['/bin/sh', helper.helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2)
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
            session = sftp.SftpSession(SPEC, auth='password', secret='private-password')
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
            sftp.SftpSession(SPEC, auth='password', secret='very-private-password', _process=server)
        self.assertNotIn('very-private-password', str(caught.exception))
        self.assertIn('[redacted]', str(caught.exception))
        session, _ = self.session(password='very-private-password', status_error=(4, 'very-private-password rejected'))
        with self.assertRaises(sftp.SftpError) as caught:
            session.stat('/log')
        self.assertNotIn('very-private-password', str(caught.exception))

    def test_key_auth_failure_has_plain_actionable_error(self):
        server = PipeServer(failure='reader@example.test: Permission denied (publickey).')
        self.addCleanup(self.check_server, server)
        with self.assertRaisesRegex(sftp.SftpAuthError, 'your keys or agent'):
            sftp.SftpSession(SPEC, _process=server)

    def test_all_auth_argv_and_quoted_known_hosts(self):
        key = self.local('key with spaces')
        hosts = os.path.join(self.root, 'trusted hosts')
        for method, secret in (('agent', None), ('key', None), ('key', 'passphrase'), ('password', 'password-value')):
            server = PipeServer()
            self.addCleanup(self.check_server, server)
            with self.subTest(method=method, secret=bool(secret)), \
                    mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'), \
                    mock.patch.object(sftp.subprocess, 'Popen', return_value=server) as spawn, \
                    mock.patch.object(sftp, '_knows_use_keychain', return_value=True), \
                    mock.patch.object(sftp, 'known_hosts_config',
                                      return_value=([], ['/etc/ssh/ssh_known_hosts', '/etc/ssh/ssh_known_hosts2'])), \
                    mock.patch.object(sys, 'frozen', True, create=True):
                session = sftp.SftpSession(SPEC, auth=method, key_path=key if method == 'key' else '',
                                           secret=secret, known_hosts=hosts)
                session._owns_group = False
                self.addCleanup(session.close)
                argv = spawn.call_args[0][0]
                env = spawn.call_args[1]['env']
                self.assertIn('GlobalKnownHostsFile="/etc/ssh/ssh_known_hosts" "/etc/ssh/ssh_known_hosts2" "' + hosts + '"', argv)
                self.assertEqual('BatchMode=yes' in argv, secret is None)
                if secret:
                    self.assertNotIn(secret, repr(argv) + repr(env))
                    self.assertIn('NumberOfPasswordPrompts=1', argv)
                    self.assertIn('BatchMode=no', argv)
                if method == 'key':
                    self.assertIn('IdentityFile="' + key + '"', argv)
                    self.assertIn('IdentitiesOnly=yes', argv)
                    self.assertIn('PreferredAuthentications=publickey', argv)
                # Only a passphrase myflames supplies is kept from the agent and Keychain.
                self.assertEqual('AddKeysToAgent=no' in argv, method == 'key' and bool(secret))
                self.assertEqual('UseKeychain=no' in argv, method == 'key' and bool(secret))
                if method == 'password':
                    self.assertIn('PreferredAuthentications=password', argv)
                    self.assertIn('PubkeyAuthentication=no', argv)
                for option in ('StrictHostKeyChecking=yes', 'UpdateHostKeys=no', 'PermitLocalCommand=no',
                               'ControlMaster=no', 'ControlPath=none', 'ClearAllForwardings=yes',
                               'ForwardAgent=no', 'ForwardX11=no'):
                    self.assertIn(option, argv)
                session.close()

    def test_silent_drop_before_sign_in_is_explained(self):
        server = PipeServer(failure='')
        self.addCleanup(self.check_server, server)
        with self.assertRaisesRegex(sftp.SftpError, 'closed the connection before sign-in.*wait a moment'):
            sftp.SftpSession(SPEC, _process=server)

    def test_known_hosts_config_reads_paths_with_spaces_and_custom_globals(self):
        # Review: ssh -G prints paths unquoted; splitting at every space broke
        # a home folder with a space, and sessions ignored a custom global file.
        out = ('user lab\nuserknownhostsfile /Users/John Smith/.ssh/known_hosts /Users/John Smith/.ssh/known_hosts2\n'
               'globalknownhostsfile /opt/team keys/known_hosts\n')
        with mock.patch.object(sftp.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, out.encode(), b'')):
            user, global_files = sftp.known_hosts_config('/usr/bin/ssh', {'ssh_host': 'db', 'ssh_port': 2222})
        self.assertEqual(user, ['/Users/John Smith/.ssh/known_hosts', '/Users/John Smith/.ssh/known_hosts2'])
        self.assertEqual(global_files, ['/opt/team keys/known_hosts'])
        with mock.patch.object(sftp.subprocess, 'run', side_effect=OSError('no ssh')):
            user, global_files = sftp.known_hosts_config('/usr/bin/ssh', {'ssh_host': 'db'})
        self.assertTrue(user[0].endswith('/.ssh/known_hosts'))
        self.assertEqual(global_files, ['/etc/ssh/ssh_known_hosts', '/etc/ssh/ssh_known_hosts2'])
        # A session keeps that custom global file and adds myflames' own.
        server = PipeServer()
        self.addCleanup(self.check_server, server)
        hosts = os.path.join(self.root, 'trusted hosts')
        with mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'), \
                mock.patch.object(sftp, 'known_hosts_config', return_value=([], ['/opt/team keys/known_hosts'])), \
                mock.patch.object(sftp.subprocess, 'Popen', return_value=server) as spawn:
            session = sftp.SftpSession(SPEC, known_hosts=hosts)
            session._owns_group = False
            self.addCleanup(session.close)
        self.assertIn('GlobalKnownHostsFile="/opt/team keys/known_hosts" "%s"' % hosts, spawn.call_args[0][0])

    def test_known_hosts_paths_split_by_what_exists_and_relative_paths_kept(self):
        # Review: "/team /known_hosts" is one path when that file exists, and
        # a relative UserKnownHostsFile is resolved, not dropped.
        team = os.path.join(self.root, 'team ')
        os.mkdir(team)
        spaced = os.path.join(team, 'known_hosts')
        open(spaced, 'w').close()
        plain = os.path.join(self.root, 'plain_hosts')
        open(plain, 'w').close()
        self.assertEqual(sftp._paths_of('%s %s' % (spaced, plain)), [spaced, plain])
        # Without the spaced folder, the same text is two paths.
        self.assertEqual(sftp._paths_of('%s/a %s/b' % (self.root, self.root)),
                         [os.path.join(self.root, 'a'), os.path.join(self.root, 'b')])
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)
        self.assertEqual(sftp._paths_of('plain_hosts'), [os.path.abspath('plain_hosts')])

    def test_use_keychain_probe_is_cached_and_fails_closed(self):
        sftp._KEYCHAIN_OPTION.clear()
        self.addCleanup(sftp._KEYCHAIN_OPTION.clear)
        with mock.patch.object(sftp.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(sftp._knows_use_keychain('/usr/bin/ssh'))
            self.assertTrue(sftp._knows_use_keychain('/usr/bin/ssh'))
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0], ['/usr/bin/ssh', '-o', 'UseKeychain=no', '-G', 'localhost'])
        with mock.patch.object(sftp.subprocess, 'run', return_value=mock.Mock(returncode=255)):
            self.assertFalse(sftp._knows_use_keychain('/opt/other/ssh'))
        with mock.patch.object(sftp.subprocess, 'run', side_effect=OSError('missing')):
            self.assertFalse(sftp._knows_use_keychain('/missing/ssh'))

    def test_quotes_and_controls_in_ssh_paths_are_refused_before_spawn(self):
        with mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            for bad in ('relative', '/key" -o ProxyCommand=bad', '/key\nvalue'):
                for kwargs in ({'known_hosts': bad}, {'auth': 'key', 'key_path': bad}):
                    with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                        sftp.SftpSession(SPEC, **kwargs)
        spawn.assert_not_called()

    def test_crlf_failure_reasons_and_changed_precedes_unknown(self):
        cases = [('No ED25519 host key is known for example.test\r\nHost key verification failed.\r\n',
                  'host_key_unknown'),
                 ('REMOTE HOST IDENTIFICATION HAS CHANGED!\r\nHost key verification failed.\r\n',
                  'host_key_changed'),
                 ('Host key for example.test has changed\r', 'host_key_changed'),
                 ('Permission denied (publickey).\r\n', 'auth_failed'),
                 ('Authentication failed\r', 'auth_failed')]
        for message, reason in cases:
            server = PipeServer(failure=message)
            self.addCleanup(self.check_server, server)
            with self.subTest(reason=reason), self.assertRaises(sftp.SftpError) as caught:
                sftp.SftpSession(SPEC, _process=server)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn('\r', str(caught.exception))
            self.assertNotIn('\\u000d', str(caught.exception))
            self.assertIn('example.test', str(caught.exception))
        with mock.patch.object(sftp.ssh_tunnel, 'find_ssh', side_effect=sftp.ssh_tunnel.TunnelError('missing')):
            with self.assertRaises(sftp.SftpSshMissing) as caught:
                sftp.SftpSession(SPEC)
        self.assertEqual(caught.exception.reason, 'ssh_missing')

    def test_windows_passphrase_fails_closed(self):
        key = self.local('id_key')
        with mock.patch.object(os, 'name', 'nt'), mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(sftp.SftpAuthError, 'Windows'):
                sftp.SftpSession(SPEC, auth='key', key_path=key, secret='passphrase')
        spawn.assert_not_called()

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
                sftp.SftpSession(SPEC, auth='password', secret='secret')
        spawn.assert_not_called()

    def test_askpass_setup_error_never_exposes_secret_or_temp_path(self):
        with mock.patch.object(sftp, '_Askpass', side_effect=OSError('secret /private/hidden/helper')), \
                mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/usr/bin/ssh'), \
                mock.patch.object(sftp.subprocess, 'Popen') as spawn:
            with self.assertRaises(sftp.SftpAuthError) as caught:
                sftp.SftpSession(SPEC, auth='password', secret='secret')
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('/private/hidden', str(caught.exception))
        spawn.assert_not_called()


class TestApi(Isolated):
    def setUp(self):
        super().setUp()
        self.store = WorkspaceStore(os.path.join(self.root, 'store', 'workspace.sqlite3'))
        self.draft = dict(name='Logs', host='example.test', port=2222, user='reader', auth='agent',
                          key_path='', remote_dir='', local_dir='')
        self.connection = ssh.save(self.store, {'connection': self.draft})['connection']
        self.key = self.connection['id']

    def fake_session(self):
        session = mock.Mock()
        session._password = None
        session.alive.return_value = True
        session.listdir.return_value = []
        session.safe.side_effect = lambda text: text
        session.realpath.side_effect = lambda path: '/home/reader' if path == '.' else posixpath.normpath(path)
        return session

    def test_remote_home_empty_path_and_full_modes(self):
        session, server = self.session(names=['log.txt', 'logs'])
        with mock.patch.object(ssh, 'open_session', return_value=session):
            result = api.api_listdir(self.store, self.key, '')
        self.assertEqual(result['path'], '/home/reader')
        self.assertEqual(result['home'], '/home/reader')
        self.assertEqual(result['entries'], [
            dict(name='log.txt', kind='file', is_dir=False, size=6, mtime=1700000000, mode=0o100640),
            dict(name='logs', kind='dir', is_dir=True, size=6, mtime=1700000000, mode=0o40750)])
        self.assertEqual([kind for kind, _ in server.calls].count(sftp.REALPATH), 1)

    def test_remote_resolved_path_and_link_stat_cap(self):
        session = self.fake_session()
        session.realpath.side_effect = ['/home/reader', '/resolved']
        session.listdir.return_value = [dict(name='link%d' % i, mode=0o120777, size=3, mtime=1) for i in range(205)]
        session.stat.return_value = {'mode': 0o40700}
        with mock.patch.object(ssh, 'open_session', return_value=session):
            result = api.api_listdir(self.store, self.key, '/unresolved')
        self.assertEqual(result['path'], '/resolved')
        self.assertEqual(result['home'], '/home/reader')
        self.assertEqual(session.stat.call_count, 200)
        self.assertTrue(all(e['is_dir'] and e['kind'] == 'link' for e in result['entries'][:200]))
        self.assertFalse(any(e['is_dir'] for e in result['entries'][200:]))
        session.stat.assert_any_call('/resolved/link199')

    def test_broken_link_is_not_directory_but_transport_errors_propagate(self):
        session = self.fake_session()
        session.listdir.return_value = [dict(name='broken', mode=0o120777)]
        session.stat.side_effect = sftp.SftpError('missing', 2)
        with mock.patch.object(ssh, 'open_session', return_value=session):
            result = api.api_listdir(self.store, self.key, '')
            self.assertFalse(result['entries'][0]['is_dir'])
            session.stat.side_effect = sftp.SftpError('transport ended')
            with self.assertRaisesRegex(sftp.SftpError, 'transport ended'):
                api.api_listdir(self.store, self.key, '')

    def test_cached_session_reused_and_expired(self):
        session = self.fake_session()
        with mock.patch.object(ssh, 'open_session', return_value=session) as factory, \
                mock.patch.object(api.time, 'monotonic', return_value=10):
            api.api_listdir(self.store, self.key, '')
            api.api_listdir(self.store, self.key, '/x')
            self.assertEqual(factory.call_count, 1)
        key = next(iter(api._SESSIONS))
        entry = api._SESSIONS[key]
        with mock.patch.object(api.time, 'monotonic', return_value=129):
            api._expire(key, entry)
        session.close.assert_not_called()
        with mock.patch.object(api.time, 'monotonic', return_value=130):
            api._expire(key, entry)
        session.close.assert_called_once()
        self.assertEqual(api._SESSIONS, {})

    def test_saved_edit_invalidates_browsing_session(self):
        first, second = self.fake_session(), self.fake_session()
        with mock.patch.object(ssh, 'open_session', side_effect=[first, second]) as factory:
            api.api_listdir(self.store, self.key, '')
            ssh.save(self.store, {'connection': dict(self.draft, id=self.key, host='other.test')})
            first.close.assert_called_once()
            api.api_listdir(self.store, self.key, '')
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(factory.call_args[0][0]['host'], 'other.test')

    def test_local_home_and_link_shapes(self):
        os.mkdir(os.path.join(self.root, 'folder'))
        source = self.local('file')
        os.symlink('folder', os.path.join(self.root, 'folder-link'))
        os.symlink('missing', os.path.join(self.root, 'broken'))
        result = api.api_local_list('')
        self.assertEqual((result['path'], result['home']), (self.root, self.root))
        items = {item['name']: item for item in result['entries']}
        self.assertEqual(items['folder-link']['kind'], 'link')
        self.assertTrue(items['folder-link']['is_dir'])
        self.assertFalse(items['broken']['is_dir'])
        self.assertEqual(items['file']['mode'], os.lstat(source).st_mode)
        self.assertIsInstance(items['file']['mtime'], int)
        result = api.api_local_list(os.path.join(self.root, 'folder-link'))
        self.assertEqual(result['path'], os.path.join(self.root, 'folder'))
        for bad in (None, 1, '\x00', 'relative'):
            with self.assertRaises(ValueError):
                api.api_local_list(bad)

    def test_local_mkdir_rename_and_refusals(self):
        folder = os.path.join(self.root, 'new')
        self.assertEqual(api.api_local_mkdir(folder), {'path': folder})
        target = folder + '-renamed'
        self.assertEqual(api.api_local_rename(folder, target), {'path': target})
        self.assertTrue(os.path.isdir(target))
        source = self.local()
        existing = self.local('existing', b'keep')
        broken = os.path.join(self.root, 'broken')
        os.symlink('missing', broken)
        for destination in (existing, broken, source):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, 'already exists'):
                api.api_local_rename(source, destination)
        for bad in ('', 'relative', self.root + '/.', self.root + '/..', self.root + '/a\x00b',
                    self.root + '/a\nb', self.root + '/a\rb', self.root + '/a\x7fb', self.root + '/missing/child'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    api.api_local_mkdir(bad)
                with self.assertRaises(ValueError):
                    api.api_local_rename(source, bad)
        with open(existing, 'rb') as stream:
            self.assertEqual(stream.read(), b'keep')
        with self.assertRaises(ValueError):
            api.api_local_mkdir(target)

    def test_local_mkdir_permission_error_is_plain(self):
        with mock.patch.object(api, '_absent', side_effect=PermissionError('private path')):
            with self.assertRaisesRegex(ValueError, 'could not be created') as caught:
                api.api_local_mkdir(self.root + '/new')
        self.assertNotIn('private path', str(caught.exception))

    def tree_session(self):
        session = self.fake_session()
        modes = {'/tree': 0o40700, '/tree/child': 0o40700, '/tree/child/data': 0o100600,
                 '/tree/link': 0o120777}
        listings = {'/tree': [{'name': 'child'}, {'name': 'link'}], '/tree/child': [{'name': 'data'}]}
        session.lstat.side_effect = lambda path: {'mode': modes[path]}
        session.listdir.side_effect = lambda path: listings[path]
        return session

    def test_local_reveal_runs_the_file_manager_with_an_argv_list_only(self):
        target = self.local('report.sql', b'x')
        with mock.patch.object(api.subprocess, 'run') as run, mock.patch.object(api.sys, 'platform', 'darwin'):
            answer = api.api_local_reveal(target)
        self.assertEqual(answer, {'revealed': os.path.realpath(target)})
        argv = run.call_args[0][0]
        self.assertEqual(argv, ['/usr/bin/open', '-R', os.path.realpath(target)])
        self.assertNotIn('shell', run.call_args[1])
        # A folder with a trailing separator reveals that folder.
        folder = os.path.join(self.root, 'logs')
        os.mkdir(folder)
        with mock.patch.object(api.subprocess, 'run') as run, mock.patch.object(api.sys, 'platform', 'darwin'):
            self.assertEqual(api.api_local_reveal(folder + os.sep), {'revealed': os.path.realpath(folder)})
        import ntpath, posixpath
        for given, expected in (('C:\\', 'C:\\'), ('C:\\logs\\', 'C:\\logs'), ('\\\\srv\\share\\', '\\\\srv\\share\\'),
                                ('\\\\srv\\share\\d\\', '\\\\srv\\share\\d')):
            self.assertEqual(api._trim_separators(given, ntpath), expected)
        self.assertEqual(api._trim_separators('/', posixpath), '/')
        self.assertEqual(api._trim_separators('/a/b//', posixpath), '/a/b')
        with mock.patch.object(api.sys, 'platform', 'win32'), mock.patch.object(api.os, 'name', 'nt'), \
                mock.patch.dict(api.os.environ, {'SystemRoot': 'C:\\Windows'}):
            self.assertEqual(api._reveal_argv('C:\\data\\a b.sql'),
                             [os.path.join('C:\\Windows', 'explorer.exe'), '/select,C:\\data\\a b.sql'])
        for bad in ('relative/file', '', os.path.join(self.root, 'missing.sql'), 'x\x00y'):
            with self.subTest(bad=bad), mock.patch.object(api.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    api.api_local_reveal(bad)
                run.assert_not_called()
        from myflames import ui
        self.assertIn('/api/files/local/reveal', ui._FILES_PATHS)
        self.assertIn('transfers', api.api_local_list(self.root))

    def test_local_listing_and_operations_refuse_a_link_another_user_owns(self):
        # Review: the listing answered the link's target, so a transfer started
        # from it never saw the foreign link. Both now follow the same rule.
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
        with mock.patch.object(jobs.os, 'lstat', side_effect=foreign):
            for call in (lambda: api.api_local_list(link), lambda: api.api_local_mkdir(os.path.join(link, 'new')),
                         lambda: api.api_local_reveal(os.path.join(link, 'x'))):
                with self.assertRaisesRegex(ValueError, 'owned by another user'):
                    call()
        self.assertEqual(api.api_local_list(link)['path'], os.path.realpath(actual))

    def test_recursive_remove_counts_first_and_unlinks_links(self):
        session = self.tree_session()
        with mock.patch.object(ssh, 'open_session', return_value=session):
            result = api.api_remove(self.store, self.key, ['/tree', '/tree/child'])
        self.assertEqual(result, {'removed': 4})
        self.assertEqual(session.listdir.call_args_list, [mock.call('/tree'), mock.call('/tree/child')])
        self.assertEqual(session.remove.call_args_list, [mock.call('/tree/child/data'), mock.call('/tree/link')])
        self.assertEqual(session.rmdir.call_args_list, [mock.call('/tree/child'), mock.call('/tree')])
        calls = session.mock_calls
        first_delete = next(i for i, call in enumerate(calls) if call[0] in ('remove', 'rmdir'))
        last_list = max(i for i, call in enumerate(calls) if call[0] == 'listdir')
        self.assertGreater(first_delete, last_list)

    def test_recursive_remove_caps_do_not_delete_anything(self):
        for cap, value in (('MAX_REMOVE_ENTRIES', 3), ('MAX_REMOVE_DEPTH', 1)):
            session = self.tree_session()
            with mock.patch.object(api, cap, value), mock.patch.object(ssh, 'open_session', return_value=session):
                with self.assertRaisesRegex(ValueError, 'Nothing was removed'):
                    api.api_remove(self.store, self.key, ['/tree'])
            session.remove.assert_not_called()
            session.rmdir.assert_not_called()

    def test_recursive_remove_symlink_swap_during_scan_fails_before_delete(self):
        session = self.tree_session()
        original = session.lstat.side_effect
        counts = collections.Counter()

        def swapped(path):
            counts[path] += 1
            if path == '/tree/child' and counts[path] >= 2:
                return {'mode': 0o120777}
            return original(path)
        session.lstat.side_effect = swapped
        with mock.patch.object(ssh, 'open_session', return_value=session):
            with self.assertRaisesRegex(ValueError, 'no longer a directory'):
                api.api_remove(self.store, self.key, ['/tree'])
        session.remove.assert_not_called()
        session.rmdir.assert_not_called()

    def test_recursive_remove_stops_when_an_ancestor_becomes_a_link_between_deletions(self):
        # Found by review: a check shared across deletions let the entries
        # after the swap resolve through the link. Each deletion re-checks.
        session = self.tree_session()
        original = session.lstat.side_effect
        state = {'swapped': False}

        def lstat(path):
            if state['swapped'] and path == '/tree':
                return {'mode': 0o120777}
            return original(path)

        def remove(path):
            state['swapped'] = True  # The swap lands right after the first deletion.
        session.lstat.side_effect = lstat
        session.remove.side_effect = remove
        with mock.patch.object(ssh, 'open_session', return_value=session):
            with self.assertRaisesRegex(ValueError, 'no longer a directory'):
                api.api_remove(self.store, self.key, ['/tree'])
        self.assertEqual(session.remove.call_args_list, [mock.call('/tree/child/data')])
        session.rmdir.assert_not_called()

    def test_recursive_remove_hostile_entry_and_root_are_refused(self):
        for bad in ('../outside', '/absolute', 'x\x00y'):
            session = self.tree_session()
            session.listdir.side_effect = None
            session.listdir.return_value = [{'name': bad}]
            with mock.patch.object(ssh, 'open_session', return_value=session):
                with self.assertRaises(sftp.SftpError):
                    api.api_remove(self.store, self.key, ['/tree'])
            session.remove.assert_not_called()
        with self.assertRaises(ValueError):
            api.api_remove(self.store, self.key, ['/'])

    def test_transfer_shapes_validation_and_private_openers(self):
        payload = {'ssh': self.key, 'local_paths': [self.root + '/file'], 'remote_dir': '/logs', 'on_exists': 'fail'}
        for changes in ({'on_exists': True}, {'on_exists': 'true'}, {'local_paths': '/a'}, {'local_paths': [1]},
                        {'local_paths': []}, {'local_paths': ['relative']}, {'remote_dir': 2}, {'secret_ref': 'external'},
                        {'local_paths': ['/x/.']}, {'local_paths': ['/a\n']}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                api.api_start_upload(self.store, dict(payload, **changes))
        with mock.patch.object(jobs, 'start_upload', return_value={'id': 'job'}) as start:
            self.assertEqual(api.api_start_upload(self.store, payload), {'id': 'job'})
        opener, label, paths, directory, policy = start.call_args[0]
        self.assertEqual((label, paths, directory, policy),
                         ({'ssh': self.key, 'name': 'Logs'}, [self.root + '/file'], '/logs', 'fail'))
        first, second = self.fake_session(), self.fake_session()
        with mock.patch.object(ssh, 'open_session', side_effect=[first, second]) as factory:
            self.assertIs(opener(), first)
            self.assertIs(opener(), second)
            self.assertEqual(factory.call_count, 2)
        self.assertEqual(api._SESSIONS, {})
        ssh.save(self.store, {'connection': dict(self.draft, id=self.key, host='changed.test')})
        with mock.patch.object(ssh, 'open_session') as factory:
            with self.assertRaisesRegex(ValueError, 'connection changed'):
                opener()
        factory.assert_not_called()

    def test_all_files_routes_shapes_through_ui(self):
        from myflames import ui
        workspace = mock.Mock(store=self.store)
        session = self.fake_session()
        session.lstat.return_value = {'mode': 0o100600}
        job = {'id': 'a' * 32}
        folder = self.root + '/created'
        source = self.local()
        cases = [
            ('remote/list', {'ssh': self.key, 'path': ''}, {'path': '/home/reader', 'home': '/home/reader', 'entries': []}),
            ('remote/mkdir', {'ssh': self.key, 'path': '/new'}, {'path': '/new'}),
            ('remote/rename', {'ssh': self.key, 'from': '/old', 'to': '/new'}, {'path': '/new'}),
            ('remote/remove', {'ssh': self.key, 'paths': ['/old']}, {'removed': 1}),
            ('local/mkdir', {'path': folder}, {'path': folder}),
            ('local/rename', {'from': source, 'to': source + '2'}, {'path': source + '2'}),
            ('upload', {'ssh': self.key, 'local_paths': [source + '2'], 'remote_dir': '/', 'on_exists': 'skip'}, {'job': job}),
            ('download', {'ssh': self.key, 'remote_paths': ['/x'], 'local_dir': self.root, 'on_exists': 'overwrite'}, {'job': job}),
            ('jobs', {'ssh': self.key}, {'jobs': [job]}),
            ('cancel', {'job_id': job['id']}, {'job': job}),
            ('retry', {'job_id': job['id']}, {'job': job}),
            ('clear', {'ssh': self.key}, {'cleared': 1})]
        with mock.patch.object(ssh, 'open_session', return_value=session), \
                mock.patch.object(jobs, 'start_upload', return_value=job), \
                mock.patch.object(jobs, 'start_download', return_value=job), \
                mock.patch.object(jobs, 'jobs', return_value=[job]) as listing, \
                mock.patch.object(jobs, 'cancel', return_value=job), \
                mock.patch.object(jobs, 'retry', return_value=job, create=True), \
                mock.patch.object(jobs, 'clear', return_value=1, create=True) as clear:
            for route, body, answer in cases:
                with self.subTest(route=route):
                    self.assertEqual(ui.api_response('/api/files/' + route, body, workspace), answer)
            listing.assert_called_once_with(ssh=self.key)
            clear.assert_called_once_with(ssh=self.key)
        answer = ui.api_response('/api/files/local/list', {'path': ''}, workspace)
        self.assertEqual(set(answer), {'path', 'home', 'entries', 'transfers'})
        self.assertEqual(answer['home'], self.root)
        for path in ui._FILES_PATHS + ui._SSH_CONNECTION_PATHS:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'running workspace'):
                ui.api_response(path, {})

    def test_new_modules_parse_as_python37_and_only_stdlib_dependencies(self):
        allowed = {'atexit', 'collections', 'contextlib', 'copy', 'ctypes', 'datetime', 'errno', 'hashlib', 'hmac',
                   'os', 'posixpath',
                   'queue', 're', 'secrets', 'shlex', 'shutil', 'signal', 'stat', 'struct', 'subprocess', 'sys',
                   'tempfile', 'threading', 'time'}
        root = os.path.dirname(os.path.dirname(__file__))
        for module in ('sftp.py', 'sftp_jobs.py', 'sftp_api.py', 'ssh_connections.py'):
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
