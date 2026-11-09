'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { PassThrough } = require('node:stream');
const path = require('node:path');
const fs = require('node:fs');
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
test('Parameters documentation links open only the official manuals', () => {
  // myflames/sysvar_docs.py: the MySQL Reference Manual, MariaDB's documentation, Percona's manuals.
  for (const value of ['https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_buffer_pool_size',
    'https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables#innodb_buffer_pool_size',
    'https://docs.percona.com/percona-server/8.4/innodb-corrupt-table-action.html#innodb_corrupt_table_action',
    'https://docs.percona.com/percona-xtradb-cluster/8.4/wsrep-system-index.html#pxc_strict_mode'])
    assert.equal(externalLink(value), value);
  for (const value of ['http://docs.percona.com/percona-server/8.4/x.html', 'https://docs.percona.com.evil.test/x',
    'https://user@docs.percona.com/x', 'https://docs.percona.com:8443/x', 'https://percona.com/x', 'https://forums.percona.com/x'])
    assert.equal(externalLink(value), null);
});
test('Home news links open in the default browser, never in the app window', () => {
  // Headline links from myflames/news.py: the MySQL team's posts and mariadb.org.
  for (const value of ['https://blogs.oracle.com/mysql/troubleshooting-mysql-with-opentelemetry-metrics', 'https://mariadb.org/amazic-joins-mariadb-foundation-as-a-silver-sponsor/'])
    assert.equal(externalLink(value), value);
  for (const value of ['http://blogs.oracle.com/mysql/x', 'https://blogs.oracle.com.evil.test/mysql/x', 'https://user@blogs.oracle.com/mysql/x', 'https://blogs.oracle.com:8443/mysql/x', 'https://www.percona.com/blog/x', 'https://planet.mysql.com.evil.test/'])
    assert.equal(externalLink(value), null);
  // target=_blank reaches the window-open handler: it opens a permitted HTTPS
  // link with the system browser and always denies a new app window; any
  // navigation of the app window away from the workspace origin is cancelled.
  const main = require('node:fs').readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
  assert.match(main, /setWindowOpenHandler\(\(\{ url \}\) => \{\s*const safe = externalLink\(url\);\s*if \(safe\) void shell\.openExternal\(safe\)[^\n]*\n\s*return \{ action: 'deny' \};/);
  assert.match(main, /on\('will-navigate', \(event, url\) => \{ if \(!sameOrigin\(url, origin\)\) event\.preventDefault\(\); \}\)/);
  assert.match(main, /on\('will-redirect', \(event, url\) => \{ if \(!sameOrigin\(url, origin\)\) event\.preventDefault\(\); \}\)/);
});
test('Safe updates documentation links open in the default browser', () => {
  // The ⓘ next to Safe updates in the Query editor (SafeUpdatesInfo.tsx).
  const links = ['https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_sql_safe_updates',
    'https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables#sql_safe_updates'];
  for (const value of links) assert.equal(externalLink(value), value);
  // Every link in the source is one of them, so a changed URL cannot be
  // silently refused by the allowlist.
  const source = require('node:fs').readFileSync(path.join(__dirname, '..', '..', 'workspace', 'src', 'SafeUpdatesInfo.tsx'), 'utf8');
  assert.deepEqual([...source.matchAll(/https:\/\/[^"'\s`]+/g)].map((m) => m[0]).sort(), [...links].sort());
  for (const value of ['http://mariadb.com/docs/server/', 'https://mariadb.com.evil.test/docs/', 'https://docs.mariadb.com/', 'https://mariadb.com:8443/docs/'])
    assert.equal(externalLink(value), null);
});
test('backend environment isolates HOME and strips injected paths and credentials', () => {
  const env = backendEnvironment('/desktop', { HOME: '/real', USERPROFILE: '/real', PATH: '/tools', PYTHONPATH: '/evil', MYSQL_PWD: 'secret', MYFLAMES_VAULT_PASSPHRASE: 'secret', MYFLAMES_SECRET_BACKEND: 'keychain' });
  assert.equal(env.HOME, path.join('/desktop', 'backend-home')); assert.equal(env.USERPROFILE, env.HOME);
  assert.equal(env.MYFLAMES_SECRET_BACKEND, 'vault'); assert.equal(env.PATH.split(path.delimiter)[0], '/tools');
  for (const key of ['PYTHONPATH', 'MYSQL_PWD', 'MYFLAMES_VAULT_PASSPHRASE']) assert.equal(env[key], undefined);
});
test('ssh gets the real home and agent socket, kept apart from the isolated HOME', () => {
  const env = backendEnvironment('/desktop', { HOME: '/Users/jane', PATH: '/usr/bin', SSH_AUTH_SOCK: '/private/tmp/com.apple.launchd.x/Listeners' }, 'darwin');
  assert.equal(env.HOME, path.join('/desktop', 'backend-home'));
  assert.equal(env.MYFLAMES_SSH_HOME, '/Users/jane');
  assert.equal(env.MYFLAMES_SSH_AUTH_SOCK, '/private/tmp/com.apple.launchd.x/Listeners');
  // The backend passes these to ssh only; the variable ssh reads is never set for the backend itself.
  assert.equal(env.SSH_AUTH_SOCK, undefined);
  const relative = backendEnvironment('/desktop', { HOME: 'relative', SSH_AUTH_SOCK: 'agent.sock' }, 'linux');
  assert.equal(relative.MYFLAMES_SSH_HOME, undefined);
  assert.equal(relative.MYFLAMES_SSH_AUTH_SOCK, undefined);
  // Windows: the profile folder; the agent is a named pipe and needs no variable.
  const windows = backendEnvironment('C:\\desktop', { USERPROFILE: 'C:\\Users\\jane', SSH_AUTH_SOCK: 'C:\\x' }, 'win32', { readdirSync: () => { throw Object.assign(new Error('missing'), { code: 'ENOENT' }); } });
  assert.equal(windows.MYFLAMES_SSH_HOME, 'C:\\Users\\jane');
  assert.equal(windows.MYFLAMES_SSH_AUTH_SOCK, undefined);
});
test('Finder client discovery appends fixed locations without shell execution or duplicate entries', () => {
  const env = backendEnvironment('/desktop', { PATH: '/usr/bin:/opt/homebrew/bin' }, 'darwin');
  assert.equal(env.PATH.split(':')[0], '/usr/bin');
  assert.equal(env.PATH.split(':').filter(value => value === '/opt/homebrew/bin').length, 1);
  assert.ok(env.PATH.split(':').includes('/usr/local/mysql/bin'));
  assert.ok(env.PATH.split(':').includes('/opt/homebrew/opt/mysql-client/bin'));
  assert.ok(backendEnvironment('/desktop', {}, 'darwin').PATH.startsWith('/usr/bin:/bin:'));
  assert.ok(backendEnvironment('/desktop', { HOME: '/Users/jane' }, 'darwin').PATH.split(':')
    .includes('/Users/jane/Library/Application Support/Herd/bin'));
  assert.ok(!backendEnvironment('/desktop', { HOME: 'relative' }, 'darwin').PATH.includes('Herd'));
  assert.ok(backendEnvironment('/desktop', {}, 'darwin').PATH.endsWith(':/Applications/MySQLWorkbench.app/Contents/MacOS'));
});
test('Finder client discovery finds keg-only versioned Homebrew clients, newest first', () => {
  const fileSystem = { readdirSync(dir) {
    if (dir === '/opt/homebrew/opt') return ['mysql-client@8.0', 'mysql@8.4', 'mysql-client@8.4', 'mariadb@11.4', 'mysql@10.0-rc', 'node@22'];
    throw new Error('ENOENT');
  } };
  const parts = backendEnvironment('/desktop', { PATH: '/usr/bin' }, 'darwin', fileSystem).PATH.split(':');
  const versioned = parts.filter(value => value.includes('@'));
  assert.deepEqual(versioned, ['/opt/homebrew/opt/mariadb@11.4/bin', '/opt/homebrew/opt/mysql@8.4/bin',
    '/opt/homebrew/opt/mysql-client@8.4/bin', '/opt/homebrew/opt/mysql-client@8.0/bin']);
  assert.ok(parts.indexOf('/opt/homebrew/opt/mysql-client/bin') < parts.indexOf(versioned[0]));
  assert.equal(backendEnvironment('/desktop', { PATH: '/custom' }, 'linux').PATH, '/custom');
});
test('Windows client discovery appends installer folders after PATH, newest first', () => {
  // A fake file system, so this runs on any OS: Program Files as the installers leave it.
  const dirs = new Set([
    'C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin', 'C:\\Program Files\\MySQL\\MySQL Server 8.4\\bin',
    'C:\\Program Files\\MySQL\\MySQL Shell 8.4\\bin', 'C:\\Program Files\\MariaDB 11.8\\bin',
    'C:\\Program Files\\MariaDB 10.11\\bin', 'C:\\Program Files (x86)\\MySQL\\MySQL Server 5.7\\bin',
  ]);
  const listing = {
    'C:\\Program Files\\MySQL': ['MySQL Server 8.0', 'MySQL Server 8.4', 'MySQL Shell 8.4', 'MySQL Workbench 8.0', 'MySQL Server 9.1'],
    'C:\\Program Files': ['MariaDB 10.11', 'MariaDB 11.8', 'MySQL', 'Git'],
    'C:\\Program Files (x86)\\MySQL': ['MySQL Server 5.7'],
  };
  // Like Windows, the fake compares paths without case.
  const lowerDirs = new Set([...dirs].map(dir => dir.toLowerCase()));
  const lowerListing = Object.fromEntries(Object.entries(listing).map(([dir, names]) => [dir.toLowerCase(), names]));
  const fileSystem = {
    readdirSync: dir => { const names = lowerListing[dir.toLowerCase()]; if (!names) throw Object.assign(new Error('missing'), { code: 'ENOENT' }); return names; },
    statSync: dir => { if (!lowerDirs.has(dir.toLowerCase())) throw Object.assign(new Error('missing'), { code: 'ENOENT' }); return { isDirectory: () => true }; },
  };
  const inherited = { Path: 'C:\\Windows\\System32;C:\\tools\\mysql\\bin', ProgramFiles: 'C:\\Program Files',
    'ProgramFiles(x86)': 'C:\\Program Files (x86)', ProgramW6432: 'C:\\Program Files' };
  const env = backendEnvironment('C:\\desktop', { ...inherited, PATH: inherited.Path }, 'win32', fileSystem);
  assert.deepEqual(env.PATH.split(';'), [
    'C:\\Windows\\System32', 'C:\\tools\\mysql\\bin',
    'C:\\Program Files\\MySQL\\MySQL Server 8.4\\bin', 'C:\\Program Files\\MySQL\\MySQL Server 8.0\\bin',
    'C:\\Program Files (x86)\\MySQL\\MySQL Server 5.7\\bin',
    'C:\\Program Files\\MariaDB 11.8\\bin', 'C:\\Program Files\\MariaDB 10.11\\bin',
    'C:\\Program Files\\MySQL\\MySQL Shell 8.4\\bin',
  ]);
  // Environment names are case-insensitive on Windows; nothing installed leaves PATH alone.
  const lower = backendEnvironment('C:\\desktop', { PATH: 'C:\\x', programfiles: 'C:\\Program Files' }, 'win32', fileSystem);
  assert.ok(lower.PATH.split(';').includes('C:\\Program Files\\MariaDB 11.8\\bin'));
  const none = { readdirSync: () => { throw new Error('missing'); }, statSync: () => { throw new Error('missing'); } };
  assert.equal(backendEnvironment('C:\\desktop', { PATH: 'C:\\x', ProgramFiles: 'C:\\Program Files' }, 'win32', none).PATH, 'C:\\x');
  // A folder already on PATH, in any case or with a trailing backslash, is not added twice.
  const dup = backendEnvironment('C:\\desktop', { PATH: 'c:\\program files\\mariadb 11.8\\bin\\', ProgramFiles: 'C:\\Program Files',
    ProgramW6432: 'C:\\PROGRAM FILES' }, 'win32', fileSystem);
  assert.equal(dup.PATH.split(';').filter(value => value.toLowerCase().startsWith('c:\\program files\\mariadb 11.8')).length, 1);
  assert.ok(dup.PATH.startsWith('c:\\program files\\mariadb 11.8\\bin\\;'));
});
test('Windows client discovery falls back to the mysql.exe bundled with MySQL Workbench, last', () => {
  // A server with only Workbench installed: mysql.exe sits in the Workbench folder itself, not in bin.
  const files = new Set(['c:\\program files\\mysql\\mysql workbench 8.0 ce\\mysql.exe']);
  const fileSystem = {
    readdirSync: dir => {
      if (dir.toLowerCase() === 'c:\\program files\\mysql') return ['MySQL Workbench 8.0 CE', 'MySQL Workbench 6.3 CE', 'MySQL Server 8.4'];
      throw Object.assign(new Error('missing'), { code: 'ENOENT' });
    },
    statSync: file => {
      if (file.toLowerCase() === 'c:\\program files\\mysql\\mysql server 8.4\\bin') return { isDirectory: () => true, isFile: () => false };
      if (files.has(file.toLowerCase())) return { isDirectory: () => false, isFile: () => true };
      throw Object.assign(new Error('missing'), { code: 'ENOENT' });
    },
  };
  const env = backendEnvironment('C:\\desktop', { PATH: 'C:\\Windows\\System32', ProgramFiles: 'C:\\Program Files' }, 'win32', fileSystem);
  // Server first; Workbench 6.3 CE has no mysql.exe here, so it is not added.
  assert.deepEqual(env.PATH.split(';'), ['C:\\Windows\\System32', 'C:\\Program Files\\MySQL\\MySQL Server 8.4\\bin',
    'C:\\Program Files\\MySQL\\MySQL Workbench 8.0 CE']);
});
test('macOS: the bundled MySQL client is searched last, after PATH and every installed client folder', () => {
  const resources = '/Applications/myflames.app/Contents/Resources';
  const bundled = resources + '/mysql-client/bin';
  const fileSystem = { readdirSync(dir) {
    if (dir === '/opt/homebrew/opt') return ['mysql-client@8.4', 'mariadb@11.4'];
    throw new Error('ENOENT');
  } };
  const env = backendEnvironment('/desktop', { HOME: '/Users/jane', PATH: '/usr/bin:/opt/homebrew/bin' }, 'darwin', fileSystem, resources);
  const parts = env.PATH.split(':');
  assert.equal(parts.at(-1), bundled);
  assert.equal(parts.filter(value => value === bundled).length, 1);
  assert.equal(env.MYFLAMES_BUNDLED_CLIENT_DIR, bundled);
  // Inherited PATH, the standard folders, keg-only Homebrew, Herd and Workbench (PR #16) all come first.
  for (const installed of ['/usr/bin', '/opt/homebrew/bin', '/usr/local/mysql/bin', '/opt/homebrew/opt/mysql-client/bin',
    '/opt/homebrew/opt/mariadb@11.4/bin', '/opt/homebrew/opt/mysql-client@8.4/bin',
    '/Users/jane/Library/Application Support/Herd/bin', '/Applications/MySQLWorkbench.app/Contents/MacOS']) {
    assert.ok(parts.indexOf(installed) >= 0 && parts.indexOf(installed) < parts.indexOf(bundled), installed);
  }
  assert.equal(parts.at(-2), '/Applications/MySQLWorkbench.app/Contents/MacOS');
  // Already on the inherited PATH: moved to the end, never searched earlier.
  const moved = backendEnvironment('/desktop', { PATH: bundled + ':/usr/bin' }, 'darwin', fileSystem, resources).PATH.split(':');
  assert.equal(moved[0], '/usr/bin');
  assert.equal(moved.at(-1), bundled);
  assert.equal(moved.filter(value => value === bundled).length, 1);
});
test('the bundled client is added only for a packaged app with an absolute resources folder', () => {
  for (const env of [backendEnvironment('/desktop', { PATH: '/usr/bin' }, 'darwin'),
    backendEnvironment('/desktop', { PATH: '/usr/bin' }, 'darwin', fs, null),
    backendEnvironment('/desktop', { PATH: '/usr/bin' }, 'darwin', fs, 'relative/Resources')]) {
    assert.ok(env.PATH.endsWith(':/Applications/MySQLWorkbench.app/Contents/MacOS'));
    assert.ok(!env.PATH.includes('Resources'));
    assert.equal(env.MYFLAMES_BUNDLED_CLIENT_DIR, undefined);
  }
  // Linux is not a packaged target: its PATH stays exactly as inherited.
  const linux = backendEnvironment('/desktop', { PATH: '/custom' }, 'linux', fs, '/opt/myflames/resources');
  assert.equal(linux.PATH, '/custom');
  assert.equal(linux.MYFLAMES_BUNDLED_CLIENT_DIR, undefined);
  // A folder name holding the PATH separator cannot go on PATH; the backend still gets it by name.
  const colon = backendEnvironment('/desktop', { PATH: '/usr/bin' }, 'darwin', fs, '/Users/jane/a:b/myflames.app/Contents/Resources');
  assert.ok(!colon.PATH.includes('a:b'));
  assert.equal(colon.MYFLAMES_BUNDLED_CLIENT_DIR, '/Users/jane/a:b/myflames.app/Contents/Resources/mysql-client/bin');
});
test('Windows: the bundled MySQL client is searched last, after PATH, the installer folders and Workbench', () => {
  const files = new Set(['c:\\program files\\mysql\\mysql workbench 8.0 ce\\mysql.exe']);
  const fileSystem = {
    readdirSync: dir => {
      if (dir.toLowerCase() === 'c:\\program files\\mysql') return ['MySQL Workbench 8.0 CE', 'MySQL Server 8.4'];
      if (dir.toLowerCase() === 'c:\\program files') return ['MariaDB 11.8', 'MySQL'];
      throw Object.assign(new Error('missing'), { code: 'ENOENT' });
    },
    statSync: file => {
      const lower = file.toLowerCase();
      if (lower === 'c:\\program files\\mysql\\mysql server 8.4\\bin' || lower === 'c:\\program files\\mariadb 11.8\\bin') return { isDirectory: () => true, isFile: () => false };
      if (files.has(lower)) return { isDirectory: () => false, isFile: () => true };
      throw Object.assign(new Error('missing'), { code: 'ENOENT' });
    },
  };
  const resources = 'C:\\Users\\jane\\AppData\\Local\\Programs\\myflames-desktop\\resources';
  const bundled = resources + '\\mysql-client\\bin';
  const env = backendEnvironment('C:\\desktop', { PATH: 'C:\\Windows\\System32;C:\\tools\\mysql\\bin', ProgramFiles: 'C:\\Program Files' }, 'win32', fileSystem, resources);
  assert.deepEqual(env.PATH.split(';'), ['C:\\Windows\\System32', 'C:\\tools\\mysql\\bin',
    'C:\\Program Files\\MySQL\\MySQL Server 8.4\\bin', 'C:\\Program Files\\MariaDB 11.8\\bin',
    'C:\\Program Files\\MySQL\\MySQL Workbench 8.0 CE', bundled]);
  assert.equal(env.MYFLAMES_BUNDLED_CLIENT_DIR, bundled);
  // On the inherited PATH in another case or with a trailing backslash: moved to the end once.
  const moved = backendEnvironment('C:\\desktop', { PATH: bundled.toUpperCase() + '\\;C:\\Windows\\System32', ProgramFiles: 'C:\\Program Files' }, 'win32', fileSystem, resources);
  const parts = moved.PATH.split(';');
  assert.equal(parts[0], 'C:\\Windows\\System32');
  assert.equal(parts.at(-1), bundled);
  assert.equal(parts.filter(value => value.toLowerCase().replace(/\\+$/, '') === bundled.toLowerCase()).length, 1);
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
    // dataPath uses the native realpath, which also expands Windows 8.3 short names.
    assert.equal(dataPath(path.join(directory, 'desktop'), directory), path.join(fs.realpathSync.native(directory), 'desktop'));
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
function runtimeFile() {
  const fs = require('node:fs'), os = require('node:os');
  const { toolGroupsFile } = require('../backend.cjs');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-groups-'));
  return { directory, file: toolGroupsFile(directory, 'test'), cleanup: () => fs.rmSync(directory, { recursive: true, force: true }) };
}
function writeGroups(file, groups) {
  require('node:fs').writeFileSync(file, JSON.stringify({ schema_version: 'tool-groups-1.0', groups }), { mode: 0o600 });
}
// A backend whose cleanup outlasts the grace period (SIGINT is ignored), with
// tool process groups that SIGKILL ends.
function stuckFixture(alive) {
  const f = fixture();
  f.dependencies.killGroup = (pid, signal) => {
    if (pid !== 12345) {
      if (!alive.has(pid)) throw Object.assign(new Error('gone'), { code: 'ESRCH' });
      if (signal === 0) return;
      f.signals.push([pid, signal]);
      if (signal === 'SIGKILL' && !f.immortal?.has(pid)) alive.delete(pid);
      return;
    }
    f.signals.push([pid, signal]);
    if (signal === 'SIGKILL') { f.onKill?.(); setImmediate(() => f.child.emit('exit', null)); }
  };
  return f;
}
test('force stop kills recorded tool groups before and after killing the backend', async () => {
  const fs = require('node:fs');
  const r = runtimeFile(), alive = new Set([4001, 4002]);
  try {
    writeGroups(r.file, [4001]);
    const f = stuckFixture(alive);
    // The backend recorded one more tool just before it was killed.
    f.onKill = () => writeGroups(r.file, [4001, 4002]);
    const ready = startBackend({ ...settings, killWait: 1000, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    const backend = await ready;
    assert.equal(f.calls[0][2].env.MYFLAMES_TOOL_GROUPS_FILE, r.file);
    await Promise.all([backend.stop(), backend.stop()]);
    assert.deepEqual(f.signals, [[12345, 'SIGINT'], [4001, 'SIGKILL'], [12345, 'SIGKILL'], [4002, 'SIGKILL']]);
    assert.equal(alive.size, 0);
    assert.equal(fs.existsSync(r.file), false);
  } finally { r.cleanup(); }
});
test('a normal backend exit still stops a tool group it left listed', async () => {
  const r = runtimeFile(), alive = new Set([4003]);
  try {
    writeGroups(r.file, [4003]);
    const f = stuckFixture(alive);
    const kill = f.dependencies.killGroup;
    f.dependencies.killGroup = (pid, signal) => { kill(pid, signal); if (pid === 12345 && signal === 'SIGINT') setImmediate(() => f.child.emit('exit', 0)); };
    const ready = startBackend({ ...settings, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    await (await ready).stop();
    assert.deepEqual(f.signals, [[12345, 'SIGINT'], [12345, 'SIGKILL'], [4003, 'SIGKILL']]);
  } finally { r.cleanup(); }
});
test('stop is bounded and refuses to claim success while a tool group survives', async () => {
  const r = runtimeFile(), alive = new Set([4004]);
  try {
    writeGroups(r.file, [4004]);
    const f = stuckFixture(alive); f.immortal = new Set([4004]);
    const ready = startBackend({ ...settings, killWait: 100, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    const backend = await ready, started = Date.now();
    await assert.rejects(backend.stop(), /Could not confirm that every owned dump, restore or clone tool stopped/);
    assert.ok(Date.now() - started < 2000);
    assert.ok(f.signals.some(([pid, signal]) => pid === 12345 && signal === 'SIGKILL'));
  } finally { r.cleanup(); }
});
// The backend lists at most MAX_TOOL_GROUPS groups (myflames.lifecycle
// ToolGroups.MAX_GROUPS refuses another tool or tunnel), and a full list is
// read: tunnels and tools together never make a forced quit kill nothing.
test('a full list of tool groups is read, one more is not', { skip: process.platform === 'win32' }, () => {
  const { readToolGroups, MAX_TOOL_GROUPS } = require('../backend.cjs');
  assert.equal(MAX_TOOL_GROUPS, 16);
  const r = runtimeFile();
  try {
    const full = Array.from({ length: MAX_TOOL_GROUPS }, (_, i) => 70000 + i);
    writeGroups(r.file, full);
    assert.deepEqual(readToolGroups(r.file, [process.pid]), { groups: full, readable: true });
    writeGroups(r.file, [...full, 70000 + MAX_TOOL_GROUPS]);
    assert.equal(readToolGroups(r.file, [process.pid]).readable, false);
  } finally { r.cleanup(); }
});
// Tool group records are POSIX only: Windows launches never pass one (below).
test('unexpected tool group records are never signalled and fail the shutdown check', { skip: process.platform === 'win32' }, async () => {
  const fs = require('node:fs');
  const { readToolGroups } = require('../backend.cjs');
  const r = runtimeFile();
  try {
    for (const groups of [[1], [0], [-4005], [process.pid], [12345], ['4005'], [4005.5], 'all', Array(17).fill(4005)]) {
      writeGroups(r.file, groups);
      assert.deepEqual(readToolGroups(r.file, [process.pid, 12345]), { groups: [], readable: false }, JSON.stringify(groups));
    }
    fs.writeFileSync(r.file, '{"schema_version":"other","groups":[4005]}');
    assert.equal(readToolGroups(r.file).readable, false);
    fs.writeFileSync(r.file, 'not json');
    assert.equal(readToolGroups(r.file).readable, false);
    fs.rmSync(r.file);
    assert.deepEqual(readToolGroups(r.file), { groups: [], readable: true });
    const elsewhere = path.join(r.directory, 'elsewhere.json'); writeGroups(elsewhere, [4005]);
    fs.symlinkSync(elsewhere, r.file);
    assert.equal(readToolGroups(r.file).readable, false);
    fs.rmSync(r.file); writeGroups(r.file, [-1]);
    const f = stuckFixture(new Set());
    const ready = startBackend({ ...settings, killWait: 50, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    await assert.rejects((await ready).stop(), /Could not confirm/);
    assert.deepEqual(f.signals, [[12345, 'SIGINT'], [12345, 'SIGKILL']]);
  } finally { r.cleanup(); }
});
test('each launch gets a fresh record in a private runtime folder; earlier records are dropped', { skip: process.platform === 'win32' }, () => {
  const fs = require('node:fs'), os = require('node:os');
  const { toolGroupsFile } = require('../backend.cjs');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'desktop-runtime-'));
  try {
    const first = toolGroupsFile(directory);
    writeGroups(first, [4006]);
    fs.writeFileSync(path.join(directory, 'runtime', '.tool-groups-abc_12'), '{}');
    fs.writeFileSync(path.join(directory, 'runtime', 'keep.txt'), 'other');
    const second = toolGroupsFile(directory);
    assert.notEqual(first, second);
    assert.deepEqual(fs.readdirSync(path.join(directory, 'runtime')), ['keep.txt']);
    assert.equal(fs.statSync(path.join(directory, 'runtime')).mode & 0o777, 0o700);
    fs.rmSync(path.join(directory, 'runtime'), { recursive: true });
    fs.mkdirSync(path.join(directory, 'real'));
    fs.symlinkSync(path.join(directory, 'real'), path.join(directory, 'runtime'));
    assert.throws(() => toolGroupsFile(directory), /private folder/);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
test('Windows launches never pass a tool group record', async () => {
  const f = fixture();
  const ready = startBackend({ ...settings, groupsFile: '/tmp/never.json' }, { ...f.dependencies, platform: 'win32', killTree: async () => { f.child.emit('exit', 0); } });
  f.child.stderr.write('myflames UI: ' + url + '\n'); const backend = await ready;
  assert.equal(f.calls[0][2].env.MYFLAMES_TOOL_GROUPS_FILE, undefined);
  await backend.stop();
});
test('real processes: a stuck backend and its detached tool group are both gone when stop resolves', { skip: process.platform === 'win32' }, async () => {
  const fs = require('node:fs');
  const r = runtimeFile();
  const script = path.join(r.directory, 'fake-backend.cjs');
  // Ignores SIGINT (cleanup that never finishes); its tool ignores SIGTERM
  // and runs in its own process group, like a dump started by lifecycle.py.
  fs.writeFileSync(script, `
    const { spawn } = require('node:child_process'), fs = require('node:fs');
    process.on('SIGINT', () => {});
    const tool = spawn(process.execPath, ['-e', "process.on('SIGTERM', () => {}); setInterval(() => {}, 1000)"], { detached: true, stdio: 'ignore' });
    fs.writeFileSync(process.env.MYFLAMES_TOOL_GROUPS_FILE, JSON.stringify({ schema_version: 'tool-groups-1.0', groups: [tool.pid] }));
    process.stderr.write('myflames UI: ${url}\\n');
    setInterval(() => {}, 1000);
  `);
  let tool;
  try {
    const backend = await startBackend({ command: process.execPath, args: [script], cwd: r.directory, env: { PATH: process.env.PATH },
      timeout: 10000, grace: 300, killWait: 3000, groupsFile: r.file });
    tool = JSON.parse(fs.readFileSync(r.file, 'utf8')).groups[0];
    assert.ok(Number.isSafeInteger(tool) && tool > 1);
    process.kill(-tool, 0);
    await backend.stop();
    for (const id of [tool, backend.child.pid]) {
      assert.throws(() => process.kill(-id, 0), error => error.code === 'ESRCH' || error.code === 'EPERM');
    }
    assert.equal(backend.child.signalCode, 'SIGKILL');
  } finally {
    if (tool) { try { process.kill(-tool, 'SIGKILL'); } catch { /* already gone */ } }
    r.cleanup();
  }
});
test('after launch, a missing tool group record is unconfirmed; before launch it lists nothing', async () => {
  const r = runtimeFile();
  try {
    const f = fixture();
    const ready = startBackend({ ...settings, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    // The backend removes its record only when it could not rewrite it.
    await assert.rejects((await ready).stop(), /Could not confirm/);
    const g = fixture(), early = startBackend({ ...settings, groupsFile: r.file }, g.dependencies);
    const rejection = assert.rejects(early, /stopped before startup/);
    await early.stop(); await rejection;
  } finally { r.cleanup(); }
});
test('a group seen gone is never signalled again, even if a stale list names it', async () => {
  const r = runtimeFile(), alive = new Set([4007]);
  try {
    writeGroups(r.file, [4007]);
    const f = stuckFixture(alive);
    // The stuck backend never unlisted it, and the id now names a new group.
    f.onKill = () => { writeGroups(r.file, [4007]); alive.add(4007); };
    const ready = startBackend({ ...settings, killWait: 500, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    await (await ready).stop();
    assert.deepEqual(f.signals, [[12345, 'SIGINT'], [4007, 'SIGKILL'], [12345, 'SIGKILL']]);
    assert.ok(alive.has(4007));
  } finally { r.cleanup(); }
});
test('a FIFO in place of the record never blocks shutdown', { skip: process.platform === 'win32' }, () => {
  const { readToolGroups } = require('../backend.cjs');
  const r = runtimeFile();
  try {
    require('node:child_process').execFileSync('mkfifo', [r.file]);
    assert.deepEqual(readToolGroups(r.file, [], true), { groups: [], readable: false });
  } finally { r.cleanup(); }
});
test('a group that vanishes between the check and SIGKILL is never signalled again', async () => {
  const r = runtimeFile(), alive = new Set([4008]);
  try {
    writeGroups(r.file, [4008]);
    const f = stuckFixture(alive), kill = f.dependencies.killGroup;
    let reused = false;
    f.dependencies.killGroup = (pid, signal) => {
      // Gone by the time SIGKILL arrives; the id is then reused by another group.
      if (pid === 4008 && signal === 'SIGKILL' && !reused) { reused = true; throw Object.assign(new Error('gone'), { code: 'ESRCH' }); }
      return kill(pid, signal);
    };
    f.onKill = () => writeGroups(r.file, [4008]);
    const ready = startBackend({ ...settings, killWait: 300, groupsFile: r.file }, f.dependencies);
    f.child.stderr.write('myflames UI: ' + url + '\n');
    await (await ready).stop();
    assert.deepEqual(f.signals, [[12345, 'SIGINT'], [12345, 'SIGKILL']]);
    assert.ok(alive.has(4008));
  } finally { r.cleanup(); }
});
