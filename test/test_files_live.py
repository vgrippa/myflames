"""Live tests of the Files place against a real OpenSSH/SFTP server.

Skipped unless MYFLAMES_SSH_LAB is set: start the lab with
``scripts/ssh-lab.sh up`` and source ``local/ssh-lab/lab.env`` (or run
``scripts/ssh-lab.sh`` for up, test and down). Every call goes through the
real WorkspaceServer over HTTP with a throwaway HOME and the vault secret
backend; the lab's host key is trusted through myflames' own known_hosts file,
so the real ~/.ssh is only read, never written (checked).
"""
import hashlib
import http.client
import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

LAB = os.environ.get('MYFLAMES_SSH_LAB', '')
SECRETS = ('MYFLAMES_SSH_LAB_PASSWORD', 'MYFLAMES_SSH_LAB_PASSPHRASE')


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


@unittest.skipUnless(LAB, 'set MYFLAMES_SSH_LAB (scripts/ssh-lab.sh up) to run the live SFTP tests')
class TestFilesLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from myflames.ui import WorkspaceServer
        cls.host, port = LAB.rsplit(':', 1)
        cls.port = int(port)
        cls.user = os.environ['MYFLAMES_SSH_LAB_USER']
        cls.password = os.environ['MYFLAMES_SSH_LAB_PASSWORD']
        cls.key = os.environ['MYFLAMES_SSH_LAB_KEY']
        cls.phrase_key = os.environ['MYFLAMES_SSH_LAB_PHRASE_KEY']
        cls.passphrase = os.environ['MYFLAMES_SSH_LAB_PASSPHRASE']
        cls.container = os.environ.get('MYFLAMES_SSH_LAB_CONTAINER', 'myflames-sshlab')
        cls.home = tempfile.mkdtemp(prefix='myflames-files-live-')
        real_known = os.path.expanduser('~/.ssh/known_hosts')
        cls.real_known = (real_known, _sha256(real_known) if os.path.exists(real_known) else None)
        # An agent that holds only the lab's plain key, for the "agent" method.
        agent_dir = tempfile.mkdtemp(prefix='myflames-agent-')
        cls.agent_dir = agent_dir
        cls.agent_sock = os.path.join(agent_dir, 'agent.sock')
        cls.agent = subprocess.Popen(['ssh-agent', '-D', '-a', cls.agent_sock],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if os.path.exists(cls.agent_sock):
                break
            time.sleep(0.1)
        subprocess.run(['ssh-add', cls.key], env=dict(os.environ, SSH_AUTH_SOCK=cls.agent_sock),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.env = mock.patch.dict(os.environ, {
            'HOME': cls.home, 'USERPROFILE': cls.home, 'MYFLAMES_SECRET_BACKEND': 'vault',
            'MYFLAMES_SSH_HOME': cls.home, 'MYFLAMES_SSH_AUTH_SOCK': cls.agent_sock})
        cls.env.start()
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        os.environ.pop('SSH_AUTH_SOCK', None)
        cls.server = WorkspaceServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.ids = {}

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(timeout=5)
            from myflames import sftp_api
            sftp_api.close_sessions()
        finally:
            cls.env.stop()
            cls.agent.send_signal(signal.SIGTERM)
            cls.agent.wait(5)
            shutil.rmtree(cls.agent_dir, ignore_errors=True)
            shutil.rmtree(cls.home, ignore_errors=True)

    # ---- helpers ---------------------------------------------------------
    def post(self, path, payload):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=120)
        try:
            conn.request('POST', path, body=json.dumps(payload), headers={
                'Content-Type': 'application/json', 'X-Myflames-Token': self.server.token,
                'Origin': self.server.origin, 'Cookie': self.server.cookie_name + '=' + self.server.session_secret})
            response = conn.getresponse()
            raw = response.read().decode('utf-8')
        finally:
            conn.close()
        for name in SECRETS:
            self.assertNotIn(os.environ[name], raw, '%s answered a secret' % path)
        return response.status, json.loads(raw)

    def ok(self, path, payload):
        status, body = self.post(path, payload)
        self.assertEqual(status, 200, '%s: %s' % (path, body))
        return body

    def lab(self, command):
        return subprocess.run(['docker', 'exec', self.container, 'sh', '-c', command], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode('utf-8')

    def draft(self, name, auth, key_path=''):
        return {'name': name, 'host': self.host, 'port': self.port, 'user': self.user, 'auth': auth,
                'key_path': key_path, 'remote_dir': '', 'local_dir': ''}

    def connection(self, name, auth, key_path='', secret=None):
        if name not in self.ids:
            payload = {'connection': self.draft(name, auth, key_path)}
            if secret:
                payload['secret'] = secret
            self.ids[name] = self.ok('/api/ssh/connections/save', payload)['connection']['id']
        return self.ids[name]

    def trusted(self):
        """The lab key, trusted once (test_01 proves the prompt itself)."""
        ssh = self.connection('lab-key', 'key', self.key)
        status, body = self.post('/api/ssh/connections/test', {'id': ssh})
        if status != 200 and body.get('code') == 'host_key_unknown':
            token = self.ok('/api/ssh/hostkey/scan', {'id': ssh})['token']
            self.ok('/api/ssh/hostkey/trust', {'token': token})
        return ssh

    def wait(self, ssh, job_id, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            jobs = self.ok('/api/files/jobs', {'ssh': ssh})['jobs']
            job = [j for j in jobs if j['id'] == job_id][0]
            if job['state'] not in ('queued', 'running'):
                return job
            time.sleep(0.2)
        self.fail('transfer %s did not finish in %d s' % (job_id, timeout))

    # ---- tests -----------------------------------------------------------
    def test_01_unknown_host_key_is_trusted_in_myflames_only(self):
        ssh = self.connection('lab-key', 'key', self.key)
        status, body = self.post('/api/ssh/connections/test', {'id': ssh})
        self.assertEqual(status, 400)
        self.assertEqual(body.get('code'), 'host_key_unknown', body)
        self.assertNotIn('\\u000d', body['error'])
        self.assertNotIn('\r', body['error'])
        scan = self.ok('/api/ssh/hostkey/scan', {'id': ssh})
        self.assertIn(str(self.port), scan['host'])
        # The fingerprint shown is the server's real key, computed independently.
        keyscan = subprocess.run(['ssh-keyscan', '-p', str(self.port), self.host], check=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        independent = subprocess.run(['ssh-keygen', '-l', '-E', 'sha256', '-f', '-'], input=keyscan, check=True,
                                     stdout=subprocess.PIPE).stdout.decode()
        for key in scan['keys']:
            self.assertTrue(key['fingerprint'].startswith('SHA256:'))
            self.assertIn(key['fingerprint'], independent)
        self.assertTrue(self.ok('/api/ssh/hostkey/trust', {'token': scan['token']})['trusted'])
        status, _ = self.post('/api/ssh/hostkey/trust', {'token': scan['token']})
        self.assertEqual(status, 400, 'a scan token is single use')
        listed = self.ok('/api/ssh/connections/list', {})
        known = listed['known_hosts_file']
        self.assertTrue(known.startswith(self.home), known)
        self.assertEqual(stat.S_IMODE(os.stat(known).st_mode), 0o600)
        tested = self.ok('/api/ssh/connections/test', {'id': ssh})
        self.assertEqual(tested['home'], '/home/' + self.user)
        real, digest = self.real_known
        self.assertEqual(_sha256(real) if os.path.exists(real) else None, digest, '~/.ssh/known_hosts changed')

    def test_02_every_sign_in_method(self):
        self.trusted()
        password = self.connection('lab-password', 'password', secret=self.password)
        self.assertEqual(self.ok('/api/ssh/connections/test', {'id': password})['home'], '/home/lab')
        phrase = self.connection('lab-phrase', 'key', self.phrase_key, secret=self.passphrase)
        self.assertEqual(self.ok('/api/ssh/connections/test', {'id': phrase})['home'], '/home/lab')
        agent = self.connection('lab-agent', 'agent')
        self.assertEqual(self.ok('/api/ssh/connections/test', {'id': agent})['home'], '/home/lab')
        # A draft is tested without being saved.
        before = len(self.ok('/api/ssh/connections/list', {})['connections'])
        self.ok('/api/ssh/connections/test', {'connection': self.draft('draft', 'password'), 'secret': self.password})
        self.assertEqual(len(self.ok('/api/ssh/connections/list', {})['connections']), before)
        # Wrong secrets fail as sign-in failures, in plain words.
        for draft, secret in ((self.draft('bad-pw', 'password'), 'not-the-password'),
                              (self.draft('bad-phrase', 'key', self.phrase_key), 'not-the-phrase')):
            status, body = self.post('/api/ssh/connections/test', {'connection': draft, 'secret': secret})
            self.assertEqual((status, body.get('code')), (400, 'auth_failed'), body)
        status, body = self.post('/api/ssh/connections/test', {'connection': self.draft('no-phrase', 'key', self.phrase_key)})
        self.assertEqual((status, body.get('code')), (400, 'auth_failed'), body)
        listed = self.ok('/api/ssh/connections/list', {})['connections']
        saved = {c['name']: c for c in listed}
        self.assertTrue(saved['lab-password']['has_secret'])
        self.assertNotIn('secret_ref', json.dumps(listed))
        # Changing the endpoint drops the saved secret (it was bound to the old one).
        changed = dict(saved['lab-password'], port=self.port + 1)
        for key in ('has_secret', 'updated'):
            changed.pop(key, None)
        self.assertFalse(self.ok('/api/ssh/connections/save', {'connection': changed})['connection']['has_secret'])
        changed['port'] = self.port
        restored = self.ok('/api/ssh/connections/save', {'connection': changed, 'secret': self.password})
        self.assertTrue(restored['connection']['has_secret'])

    def test_03_listings_open_at_home_and_describe_links(self):
        ssh = self.trusted()
        remote = self.ok('/api/files/remote/list', {'ssh': ssh, 'path': ''})
        self.assertEqual((remote['path'], remote['home']), ('/home/lab', '/home/lab'))
        entries = {e['name']: e for e in remote['entries']}
        self.assertEqual((entries['data']['kind'], entries['data']['is_dir']), ('dir', True))
        self.assertTrue(stat.S_ISDIR(entries['data']['mode']))
        self.assertEqual((entries['logs-link']['kind'], entries['logs-link']['is_dir']), ('link', True))
        data = {e['name']: e for e in self.ok('/api/files/remote/list', {'ssh': ssh, 'path': '/home/lab/data'})['entries']}
        self.assertEqual(data['big.bin']['size'], 64 * 1024 * 1024)
        self.assertEqual((data['host-link']['kind'], data['host-link']['is_dir']), ('link', False))
        self.assertIn('.hidden', data)
        through = self.ok('/api/files/remote/list', {'ssh': ssh, 'path': '/home/lab/logs-link'})
        self.assertIn('mysqld.err', [e['name'] for e in through['entries']])
        local = self.ok('/api/files/local/list', {'path': ''})
        self.assertEqual(local['path'], os.path.realpath(self.home))
        status, body = self.post('/api/files/remote/list', {'ssh': ssh, 'path': '/no/such/folder'})
        self.assertEqual(status, 400, body)

    def test_04_folders_round_trip_byte_for_byte(self):
        ssh = self.trusted()
        self.lab('rm -rf /home/lab/roundtrip')  # The lab outlives a run.
        target = os.path.join(self.home, 'downloads')
        os.mkdir(target)
        job = self.ok('/api/files/download', {'ssh': ssh, 'remote_paths': ['/home/lab/data'], 'local_dir': target,
                                              'on_exists': 'fail'})['job']
        done = self.wait(ssh, job['id'])
        self.assertEqual(done['state'], 'finished', done)
        self.assertEqual(done['files_skipped'], 1, 'the link inside the folder is skipped')
        skipped = [i for i in done['items'] if i['state'] == 'skipped']
        self.assertEqual(skipped[0]['error'], 'Symbolic link not transferred.')
        local = os.path.join(target, 'data')
        remote_sum = self.lab('sha256sum /home/lab/data/big.bin').split()[0]
        self.assertEqual(_sha256(os.path.join(local, 'big.bin')), remote_sum)
        with open(os.path.join(local, 'logs', 'archive', 'mysqld.err.1')) as stream:
            self.assertEqual(stream.read(), 'old\n')
        self.assertFalse(os.path.lexists(os.path.join(local, 'host-link')))
        self.assertEqual([n for n in os.listdir(local) if n.endswith('.myflames-part')], [])
        # The same folder back up under another name, then compared on the server.
        os.rename(local, os.path.join(target, 'roundtrip'))
        job = self.ok('/api/files/upload', {'ssh': ssh, 'local_paths': [os.path.join(target, 'roundtrip')],
                                            'remote_dir': '/home/lab', 'on_exists': 'fail'})['job']
        done = self.wait(ssh, job['id'])
        self.assertEqual(done['state'], 'finished', done)
        self.assertEqual(self.lab('sha256sum /home/lab/roundtrip/big.bin').split()[0], remote_sum)
        self.assertEqual(self.lab('cat /home/lab/roundtrip/logs/archive/mysqld.err.1'), 'old\n')
        # The mode is set on the open handle (FSETSTAT) and matches the local file.
        local_mode = stat.S_IMODE(os.stat(os.path.join(target, 'roundtrip', 'hello.txt')).st_mode)
        self.assertEqual(int(self.lab('stat -c %a /home/lab/roundtrip/hello.txt').strip(), 8), local_mode)
        # on_exists per file: fail, skip, overwrite.
        again = lambda policy: self.wait(ssh, self.ok('/api/files/upload', {
            'ssh': ssh, 'local_paths': [os.path.join(target, 'roundtrip', 'hello.txt')],
            'remote_dir': '/home/lab/roundtrip', 'on_exists': policy})['job']['id'])
        failed = again('fail')
        self.assertEqual((failed['state'], failed['files_failed']), ('failed', 1), failed)
        self.assertEqual(again('skip')['files_skipped'], 1)
        self.assertEqual(again('overwrite')['state'], 'finished')
        retry = self.ok('/api/files/retry', {'job_id': failed['id']})['job']
        self.assertEqual(retry['files_total'], 1)
        self.wait(ssh, retry['id'])
        self.assertGreaterEqual(self.ok('/api/files/clear', {'ssh': ssh})['cleared'], 4)

    def test_05_file_operations_never_follow_links(self):
        ssh = self.trusted()
        self.lab('rm -rf /home/lab/scratch /home/lab/scratch2')
        self.ok('/api/files/remote/mkdir', {'ssh': ssh, 'path': '/home/lab/scratch'})
        self.ok('/api/files/remote/mkdir', {'ssh': ssh, 'path': '/home/lab/scratch/inner'})
        self.lab("su lab -c 'printf x > /home/lab/scratch/inner/f && ln -s /home/lab/data/hello.txt /home/lab/scratch/inner/l "
                 "&& ln -s /home/lab/data /home/lab/scratch/dirlink'")
        self.ok('/api/files/remote/rename', {'ssh': ssh, 'from': '/home/lab/scratch', 'to': '/home/lab/scratch2'})
        removed = self.ok('/api/files/remote/remove', {'ssh': ssh, 'paths': ['/home/lab/scratch2']})['removed']
        self.assertEqual(removed, 5)
        self.assertEqual(self.lab('cat /home/lab/data/hello.txt'), 'hello\n')
        self.assertIn('big.bin', self.lab('ls /home/lab/data'))
        self.assertEqual(self.lab('ls /home/lab | grep -c scratch || true').strip(), '0')
        made = self.ok('/api/files/local/mkdir', {'path': os.path.join(self.home, 'made')})['path']
        self.assertTrue(os.path.isdir(made))
        open(os.path.join(self.home, 'taken'), 'w').close()
        status, _ = self.post('/api/files/local/rename', {'from': made, 'to': os.path.join(self.home, 'taken')})
        self.assertEqual(status, 400, 'a local rename never replaces')
        self.ok('/api/files/local/rename', {'from': made, 'to': os.path.join(self.home, 'renamed')})
        self.assertTrue(os.path.isdir(os.path.join(self.home, 'renamed')))

    def test_06_changed_host_key_is_refused(self):
        ssh = self.trusted()
        known = self.ok('/api/ssh/connections/list', {})['known_hosts_file']
        with open(known) as stream:
            original = stream.read()
        other = os.path.join(self.home, 'other_key')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', other], check=True)
        with open(other + '.pub') as stream:
            fake = stream.read().split()[:2]
        host = '[%s]:%d' % (self.host, self.port)
        try:
            with open(known, 'w') as stream:
                stream.write('%s %s %s\n' % (host, fake[0], fake[1]))
            from myflames import sftp_api
            sftp_api.close_sessions()
            status, body = self.post('/api/ssh/connections/test', {'id': ssh})
            self.assertEqual((status, body.get('code')), (400, 'host_key_changed'), body)
        finally:
            with open(known, 'w') as stream:
                stream.write(original)


if __name__ == '__main__':
    unittest.main()
