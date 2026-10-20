'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const path = require('node:path');
const { parseLaunchLine, sameOrigin, externalLink, backendEnvironment, startBackend } = require('../backend.cjs');
const url = 'http://127.0.0.1:54321/#launch=' + 'S'.repeat(43);
function fixture() {
  const child = new EventEmitter(); child.pid = 12345;
  child.stdout = new PassThrough(); child.stderr = new PassThrough();
  const calls = [], signals = [];
  return { child, calls, signals, dependencies: {
    platform: 'darwin', spawn: (...args) => { calls.push(args); return child; },
    killGroup: (pid, signal) => { signals.push([pid, signal]); if (signal === 'SIGINT') setImmediate(() => child.emit('exit', 0)); },
  } };
}
const settings = { command: 'python3', args: ['-m', 'myflames', 'ui', '--port', '0', '--no-browser'], cwd: '/repo', env: {}, timeout: 100, grace: 10 };
test('launch parsing refuses alternate hosts, credentials, paths and extra parameters', () => {
  assert.equal(parseLaunchLine('myflames UI: ' + url), url);
  for (const value of [url.replace('127.0.0.1', 'localhost'), url.replace('127.0.0.1', '127.1'), url.replace('127.0.0.1', 'evil.test'), url.replace('54321', '65536'), url.replace('54321', '0'), url.replace('/#', '/secret#'), url + '&other=1', url.replace('http:', 'https:'), url.replace('//', '//user@'), url + ' ', url.replace('S'.repeat(43), 'short')]) {
    assert.equal(parseLaunchLine('myflames UI: ' + value), null);
  }
  assert.equal(parseLaunchLine(url), null);
});
test('navigation compares exact origins; external links require permitted HTTPS hosts', () => {
  assert.equal(sameOrigin('http://127.0.0.1:54321/path', 'http://127.0.0.1:54321'), true);
  for (const value of ['http://127.0.0.1:54322/', 'http://127.0.0.1.evil.test:54321/', 'http://user@127.0.0.1:54321/', 'file:///tmp/x', 'javascript:alert(1)']) assert.equal(sameOrigin(value, 'http://127.0.0.1:54321'), false);
  assert.equal(externalLink('https://dev.mysql.com/downloads/shell/'), 'https://dev.mysql.com/downloads/shell/');
  for (const value of ['https://dev.mysql.com.evil.test/', 'https://user@dev.mysql.com/', 'https://dev.mysql.com:123/', 'http://dev.mysql.com/', 'file:///etc/passwd', 'ms-settings:privacy', 'javascript:alert(1)']) assert.equal(externalLink(value), null);
});
test('backend environment isolates HOME and strips injected paths and credentials', () => {
  const env = backendEnvironment('/desktop', { HOME: '/real', USERPROFILE: '/real', PATH: '/tools', PYTHONPATH: '/evil', MYSQL_PWD: 'secret', MYFLAMES_VAULT_PASSPHRASE: 'secret', MYFLAMES_SECRET_BACKEND: 'keychain' });
  assert.equal(env.HOME, path.join('/desktop', 'backend-home')); assert.equal(env.USERPROFILE, env.HOME);
  assert.equal(env.MYFLAMES_SECRET_BACKEND, 'vault'); assert.equal(env.PATH.split(path.delimiter)[0], '/tools');
  for (const key of ['PYTHONPATH', 'MYSQL_PWD', 'MYFLAMES_VAULT_PASSPHRASE']) assert.equal(env[key], undefined);
});
test('Finder client discovery appends fixed locations without shell execution or duplicate entries', () => {
  const env = backendEnvironment('/desktop', { PATH: '/usr/bin:/opt/homebrew/bin' }, 'darwin');
  assert.equal(env.PATH.split(':')[0], '/usr/bin');
  assert.equal(env.PATH.split(':').filter(value => value === '/opt/homebrew/bin').length, 1);
  assert.ok(env.PATH.split(':').includes('/usr/local/mysql/bin'));
  assert.ok(env.PATH.split(':').includes('/opt/homebrew/opt/mysql-client/bin'));
  assert.ok(backendEnvironment('/desktop', {}, 'darwin').PATH.startsWith('/usr/bin:/bin:'));
  assert.equal(backendEnvironment('/desktop', { PATH: '/custom' }, 'linux').PATH, '/custom');
});
test('startup tolerates fragmented output and shutdown owns only its child process group', async () => {
  const f = fixture(), ready = startBackend(settings, f.dependencies);
  f.child.stderr.write('Other bounded startup text\nmyflames UI: ' + url.slice(0, 20));
  f.child.stderr.write(url.slice(20) + '\nPress Ctrl+C to stop.\n');
  const backend = await ready;
  assert.equal(backend.url, url);
  assert.equal(f.calls[0][2].detached, true); assert.deepEqual(f.calls[0][2].stdio, ['ignore', 'pipe', 'pipe']);
  await Promise.all([backend.stop(), backend.stop()]);
  assert.deepEqual(f.signals, [[12345, 'SIGINT'], [12345, 'SIGKILL']]);
});
test('startup timeout cleans up and never includes secret output in diagnostics', async () => {
  const f = fixture(), ready = startBackend({ ...settings, timeout: 10 }, f.dependencies);
  f.child.stderr.write('password=private\n');
  await assert.rejects(ready, error => /did not become ready/.test(error.message) && !error.message.includes('private'));
  assert.equal(f.signals[0][0], 12345);
});
test('spawn failure and early exit produce useful bounded errors', async () => {
  const f = fixture(), ready = startBackend(settings, f.dependencies); f.child.pid = undefined;
  f.child.emit('error', new Error('secret executable path'));
  await assert.rejects(ready, /Check the configured Python executable/);
  const g = fixture(), early = startBackend(settings, g.dependencies);
  g.child.emit('exit', 2);
  await assert.rejects(early, /exit 2/);
});
test('oversized output is rejected and cleaned up', async () => {
  const f = fixture(), ready = startBackend(settings, f.dependencies);
  f.child.stderr.write('x'.repeat(70000)); await assert.rejects(ready, /too much startup output/);
  assert.ok(f.signals.length);
});
test('quit during startup has an immediate owned stop handle', async () => {
  const f = fixture(), ready = startBackend(settings, f.dependencies);
  const rejection = assert.rejects(ready, /stopped before startup/);
  await ready.stop(); await rejection;
});
test('Windows shutdown invokes only the owned process tree once', async () => {
  const f = fixture(), killed = [];
  const ready = startBackend(settings, { ...f.dependencies, platform: 'win32', killTree: async pid => { killed.push(pid); f.child.emit('exit', 0); } });
  f.child.stderr.write('myflames UI: ' + url + '\n'); const backend = await ready;
  await Promise.all([backend.stop(), backend.stop()]);
  assert.deepEqual(killed, [12345]); assert.equal(f.calls[0][2].detached, false);
});
test('data paths reject symlinks into the protected CLI store', () => {
  const fs = require('node:fs'), os = require('node:os');
  const { dataPath } = require('../backend.cjs');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-paths-'));
  try {
    fs.mkdirSync(path.join(directory, '.myflames'));
    fs.symlinkSync(path.join(directory, '.myflames'), path.join(directory, 'alias'), 'junction');
    assert.throws(() => dataPath(path.join(directory, '.myflames'), directory), /separate/);
    assert.throws(() => dataPath(path.join(directory, 'alias', 'new-child'), directory), /separate/);
    assert.equal(dataPath(path.join(directory, 'desktop'), directory), path.join(fs.realpathSync(directory), 'desktop'));
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
test('Windows tree cleanup failure is reported even when direct-child fallback exits', async () => {
  const f = fixture(); f.child.kill = () => { f.child.emit('exit', 0); return true; };
  const ready = startBackend(settings, { ...f.dependencies, platform: 'win32', killTree: async () => { throw new Error('taskkill failed'); } });
  f.child.stderr.write('myflames UI: ' + url + '\n'); const backend = await ready;
  await assert.rejects(backend.stop(), /taskkill failed/);
});
test('Windows shutdown refuses to claim success while the child remains alive', async () => {
  const f = fixture(); f.child.kill = () => false;
  const ready = startBackend(settings, { ...f.dependencies, platform: 'win32', killTree: async () => {} });
  f.child.stderr.write('myflames UI: ' + url + '\n'); const backend = await ready;
  await assert.rejects(backend.stop(), /did not stop/);
});
test('actual backend HOME and store cannot escape through nested symlinks', () => {
  const fs = require('node:fs'), os = require('node:os');
  const { dataPath, isolatedBackendHome } = require('../backend.cjs');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-home-'));
  const data = path.join(directory, 'desktop'); fs.mkdirSync(data);
  fs.mkdirSync(path.join(directory, '.myflames'));
  try {
    const canonical = dataPath(data, directory);
    fs.symlinkSync(directory, path.join(data, 'backend-home'), 'junction');
    assert.throws(() => isolatedBackendHome(canonical, directory), /separate|isolated/);
    fs.unlinkSync(path.join(data, 'backend-home')); fs.mkdirSync(path.join(data, 'backend-home'));
    fs.symlinkSync(path.join(directory, '.myflames'), path.join(data, 'backend-home', '.myflames'), 'junction');
    assert.throws(() => isolatedBackendHome(canonical, directory), /separate|isolated/);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
