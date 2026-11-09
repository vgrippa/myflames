'use strict';
// In-place updates (updater.cjs): the staged update is read only from its
// folder, checked again (name, platform, newer version, size, SHA-256), and
// handed to Squirrel.Mac through a one-file loopback feed or to the NSIS
// installer with fixed arguments. The bridge answers only the workspace frame.
const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const updater = require('../updater.cjs');

const bytes = Buffer.from('signed update bytes');
const digest = crypto.createHash('sha256').update(bytes).digest('hex');
function folder() { const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'myflames-updates-')); fs.chmodSync(dir, 0o700); return dir; }
function stage(dir, overrides = {}, platform = 'darwin') {
  const name = platform === 'darwin' ? 'myflames-3.4.0-macos-arm64.zip' : 'myflames-3.4.0-windows-x64-setup.exe';
  const record = { schema_version: 'staged-update-1.0', version: '3.4.0', platform, file: name,
    size: bytes.length, sha256: digest, staged_at: '2026-11-09T12:00:00Z', ...overrides };
  fs.writeFileSync(path.join(dir, name), bytes);
  fs.writeFileSync(path.join(dir, updater.MANIFEST), JSON.stringify(record));
  return path.join(dir, name);
}

test('versions compare by major, minor and patch only', () => {
  assert.equal(updater.isNewer('3.4.0', '3.3.3'), true);
  assert.equal(updater.isNewer('3.3.10', '3.3.9'), true);
  assert.equal(updater.isNewer('3.3.3', '3.3.3'), false);
  assert.equal(updater.isNewer('3.3.2', '3.3.3'), false);
  assert.equal(updater.isNewer('not a version', '3.3.3'), false);
});

test('a staged update that matches its record is accepted', () => {
  const dir = folder();
  const file = stage(dir);
  assert.deepEqual(updater.verifyStaged(dir, '3.3.3', 'darwin'), { version: '3.4.0', file, size: bytes.length, sha256: digest });
  const win = folder();
  const installer = stage(win, {}, 'win32');
  assert.equal(updater.verifyStaged(win, '3.3.3', 'win32').file, installer);
});

test('no staged update reads as none, not as an error, until it is verified', () => {
  const dir = folder();
  assert.equal(updater.readManifest(dir, 'darwin'), null);
  assert.throws(() => updater.verifyStaged(dir, '3.3.3', 'darwin'), /No update has been downloaded/);
});

test('changed, older or mismatched updates are refused', () => {
  let dir = folder(); let file = stage(dir);
  fs.appendFileSync(file, 'x');
  assert.throws(() => updater.verifyStaged(dir, '3.3.3', 'darwin'), /wrong size/);
  dir = folder(); file = stage(dir); fs.writeFileSync(file, Buffer.from('signed update bytez'));
  assert.throws(() => updater.verifyStaged(dir, '3.3.3', 'darwin'), /checksum/);
  dir = folder(); stage(dir);
  assert.throws(() => updater.verifyStaged(dir, '3.4.0', 'darwin'), /not newer/);
  assert.throws(() => updater.verifyStaged(dir, '3.5.0', 'darwin'), /not newer/);
});

test('the record must name a plain release file for this platform and version', () => {
  const bad = [
    { file: '../myflames-3.4.0-macos-arm64.zip' }, { file: 'sub/myflames-3.4.0-macos-arm64.zip' },
    { file: 'myflames-3.4.0-macos-arm64.zip.sh' }, { file: 'evil-3.4.0-macos-arm64.zip' },
    { file: 'myflames-3.4.0-windows-x64-setup.exe' }, { version: '3.4.1' }, { platform: 'win32' },
    { sha256: digest.toUpperCase() }, { sha256: 'abc' }, { size: 0 }, { size: '19' }, { schema_version: 'staged-update-2.0' },
  ];
  for (const overrides of bad) {
    const dir = folder(); stage(dir, overrides);
    assert.throws(() => updater.verifyStaged(dir, '3.3.3', 'darwin'), /not valid/, JSON.stringify(overrides));
  }
  const dir = folder(); stage(dir, { file: 'myflames-3.4.0-macos-arm64.zip' }, 'win32');
  assert.throws(() => updater.verifyStaged(dir, '3.3.3', 'win32'), /not valid/, 'a macOS zip on Windows');
});

test('links in the updates folder are never followed', () => {
  const dir = folder(); const outside = folder();
  const real = stage(outside);
  fs.symlinkSync(path.join(outside, updater.MANIFEST), path.join(dir, updater.MANIFEST));
  assert.throws(() => updater.readManifest(dir, 'darwin'), /not a plain file/);
  const linked = folder();
  fs.writeFileSync(path.join(linked, updater.MANIFEST), fs.readFileSync(path.join(outside, updater.MANIFEST)));
  fs.symlinkSync(real, path.join(linked, path.basename(real)));
  assert.throws(() => updater.verifyStaged(linked, '3.3.3', 'darwin'), /not a plain file/);
  const big = folder();
  fs.writeFileSync(path.join(big, updater.MANIFEST), ' '.repeat(5000));
  assert.throws(() => updater.readManifest(big, 'darwin'), /too large/);
});

test('a shared or linked updates folder is never read or emptied', { skip: process.platform === 'win32' }, () => {
  // Security review S1/S2: files another user can plant are never installed,
  // and a link to the user's own files is never emptied.
  const shared = folder(); stage(shared); fs.chmodSync(shared, 0o777);
  assert.throws(() => updater.verifyStaged(shared, '3.3.3', 'darwin'), /not private/);
  assert.equal(updater.removeStale(shared, '3.5.0', 'darwin'), false);
  assert.equal(fs.readdirSync(shared).length, 2);
  const documents = folder(); fs.writeFileSync(path.join(documents, 'notes.txt'), 'mine');
  fs.writeFileSync(path.join(documents, updater.MANIFEST), '{');
  const link = path.join(folder(), 'updates'); fs.symlinkSync(documents, link);
  assert.throws(() => updater.readManifest(link, 'darwin'), /not a plain folder/);
  assert.equal(updater.removeStale(link, '3.3.3', 'darwin'), false);
  assert.deepEqual(fs.readdirSync(documents).sort(), ['notes.txt', updater.MANIFEST].sort());
  assert.equal(updater.readManifest(path.join(folder(), 'absent'), 'darwin'), null);
});

test('cleanup removes only the files the updater writes', () => {
  const dir = folder(); stage(dir);
  fs.writeFileSync(path.join(dir, '.myflames-stage-1.part'), 'x');
  fs.writeFileSync(path.join(dir, 'notes.txt'), 'mine');
  assert.equal(updater.removeStale(dir, '3.4.0', 'darwin'), true);
  assert.deepEqual(fs.readdirSync(dir), ['notes.txt']);
});

test('an installed or unreadable staged update is removed at start; a newer one stays', () => {
  let dir = folder(); stage(dir);
  assert.equal(updater.removeStale(dir, '3.3.3', 'darwin'), false);
  assert.equal(fs.readdirSync(dir).length, 2);
  assert.equal(updater.removeStale(dir, '3.4.0', 'darwin'), true);
  assert.deepEqual(fs.readdirSync(dir), []);
  dir = folder(); fs.writeFileSync(path.join(dir, updater.MANIFEST), '{');
  fs.writeFileSync(path.join(dir, '.myflames-stage-1.part'), 'x');
  updater.removeStale(dir, '3.3.3', 'darwin');
  assert.deepEqual(fs.readdirSync(dir), []);
});

test('only packaged apps update in place, and on macOS only from Applications', () => {
  const base = { platform: 'darwin', packaged: true, inApplications: true, bundleWritable: true };
  assert.equal(updater.unsupportedReason(base), null);
  assert.match(updater.unsupportedReason({ ...base, packaged: false }), /Development builds/);
  assert.match(updater.unsupportedReason({ ...base, inApplications: false }), /Applications folder/);
  assert.match(updater.unsupportedReason({ ...base, bundleWritable: false }), /cannot replace itself/);
  assert.equal(updater.unsupportedReason({ platform: 'win32', packaged: true }), null);
  // A data folder chosen with MYFLAMES_DESKTOP_DATA may be shared, and Windows owners are not checked.
  assert.match(updater.unsupportedReason({ platform: 'win32', packaged: true, customDataFolder: true }), /MYFLAMES_DESKTOP_DATA/);
  assert.equal(updater.unsupportedReason({ ...base, customDataFolder: true }), null, 'macOS checks the folder owner instead');
  assert.match(updater.unsupportedReason({ platform: 'linux', packaged: true }), /does not update in place/);
});

test('the bridge answers only the workspace page in its top frame', () => {
  const origin = 'http://127.0.0.1:54321';
  const mainFrame = { url: origin + '/' };
  const contents = { mainFrame };
  assert.equal(updater.isWorkspaceSender({ sender: contents, senderFrame: mainFrame }, contents, origin), true);
  assert.equal(updater.isWorkspaceSender({ sender: contents, senderFrame: { url: origin + '/' } }, contents, origin), false, 'a subframe');
  assert.equal(updater.isWorkspaceSender({ sender: {}, senderFrame: mainFrame }, contents, origin), false, 'another page');
  assert.equal(updater.isWorkspaceSender({ sender: contents, senderFrame: { ...mainFrame, url: 'https://evil.test/' } }, { mainFrame: { url: 'https://evil.test/' } }, origin), false);
  assert.equal(updater.isWorkspaceSender({ sender: contents, senderFrame: null }, contents, origin), false);
  assert.equal(updater.isWorkspaceSender(null, contents, origin), false);
});

test('the loopback feed serves its zip once under an unguessable path, then closes', async () => {
  const dir = folder(); const file = stage(dir);
  const feed = await updater.serveOnce(file, bytes.length);
  try {
    const parsed = new URL(feed.url);
    assert.equal(parsed.hostname, '127.0.0.1');
    assert.match(parsed.pathname, /^\/[0-9a-f]{64}\/feed\.json$/);
    const answer = await (await fetch(feed.url)).json();
    assert.equal(new URL(answer.url).origin, parsed.origin);
    assert.equal(Buffer.compare(Buffer.from(await (await fetch(answer.url)).arrayBuffer()), bytes), 0);
    assert.equal((await fetch(answer.url)).status, 404, 'served once');
    assert.equal((await fetch(parsed.origin + '/feed.json')).status, 404);
    assert.equal((await fetch(answer.url.replace(/[0-9a-f]{64}/, '0'.repeat(64)))).status, 404);
  } finally { feed.close(); }
  await assert.rejects(fetch(feed.url));
});

function raw(port, text) {
  const net = require('node:net');
  return new Promise((resolve) => {
    const socket = net.connect(port, '127.0.0.1', () => socket.end(text));
    let data = '';
    socket.on('data', (chunk) => { data += chunk; });
    socket.on('close', () => resolve(data));
    socket.on('error', () => resolve(data));
  });
}

test('malformed requests and other Host names get 400, and the zip is still served once', async () => {
  // Security review S3: a request target URL cannot parse must not throw in the main process.
  const dir = folder(); const file = stage(dir);
  const feed = await updater.serveOnce(file, bytes.length);
  const thrown = [];
  const onError = (error) => thrown.push(error);
  process.on('uncaughtException', onError);
  try {
    const port = Number(new URL(feed.url).port);
    for (const target of ['//', 'http://[', 'http://a:99999/']) {
      const answer = await raw(port, `GET ${target} HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\nConnection: close\r\n\r\n`);
      assert.match(answer, /^HTTP\/1\.1 (400|404)/, target);
    }
    const rebound = await raw(port, `GET ${new URL(feed.url).pathname} HTTP/1.1\r\nHost: evil.test:${port}\r\nConnection: close\r\n\r\n`);
    assert.match(rebound, /^HTTP\/1\.1 400/, 'a rebound Host name is refused');
    const answer = await (await fetch(feed.url)).json();
    assert.equal(Buffer.compare(Buffer.from(await (await fetch(answer.url)).arrayBuffer()), bytes), 0);
    assert.deepEqual(thrown, []);
  } finally { process.off('uncaughtException', onError); feed.close(); }
});

function fakeUpdater(behaviour) {
  const emitter = new EventEmitter();
  emitter.setFeedURL = options => { emitter.options = options; };
  emitter.checkForUpdates = () => { setImmediate(() => behaviour(emitter)); };
  return emitter;
}

test('Squirrel.Mac gets the JSON feed and the verified zip, and the feed closes after', async () => {
  const dir = folder(); const staged = updater.verifyStaged((stage(dir), dir), '3.3.3', 'darwin');
  let received;
  const auto = fakeUpdater(async emitter => {
    const answer = await (await fetch(emitter.options.url)).json();
    received = Buffer.from(await (await fetch(answer.url)).arrayBuffer());
    emitter.emit('update-downloaded');
  });
  await updater.prepareMac(auto, staged);
  assert.equal(auto.options.serverType, 'json');
  assert.equal(Buffer.compare(received, bytes), 0);
  assert.equal(auto.listenerCount('error') + auto.listenerCount('update-downloaded') + auto.listenerCount('update-not-available'), 0);
  await assert.rejects(fetch(auto.options.url));
});

test('a refused update (for example a different signature) is reported, not installed', async () => {
  const dir = folder(); const staged = updater.verifyStaged((stage(dir), dir), '3.3.3', 'darwin');
  // Squirrel.Mac's own words, from a zip signed by another identity (seen in the release spike).
  const refused2 = fakeUpdater(emitter => emitter.emit('error', new Error('Code signature at URL file:///Users/x/Library/Caches/com.myflames.desktop.ShipIt/update.oRbwvU1/myflames.app/ did not pass validation: code failed to satisfy specified code requirement(s)')));
  await assert.rejects(updater.prepareMac(refused2, staged), (error) => {
    assert.equal(error.message, 'The update is not signed by the developer of this copy of myflames, so it was not installed.');
    return true;
  });
  // Squirrel.Mac's words for a lower version (ElectronSquirrelPreventDowngrades), seen in the release spike.
  const older = fakeUpdater(emitter => emitter.emit('error', new Error("Cannot update to a bundle with a lower version number. The application has ElectronSquirrelPreventDowngrades enabled and is trying to update from '3.3.95' to '3.3.91' which appears to be a downgrade")));
  await assert.rejects(updater.prepareMac(older, staged), { message: 'The update is older than this copy of myflames, so it was not installed.' });
  const other = fakeUpdater(emitter => emitter.emit('error', new Error('Could not read file:///private/var/x/update.zip: disk full')));
  await assert.rejects(updater.prepareMac(other, staged), (error) => {
    assert.doesNotMatch(error.message, /file:\/\//);
    assert.match(error.message, /^The update could not be installed: /);
    return true;
  });
  const none = fakeUpdater(emitter => emitter.emit('update-not-available'));
  await assert.rejects(updater.prepareMac(none, staged), /could not be installed/);
  const silent = fakeUpdater(() => {});
  await assert.rejects(updater.prepareMac(silent, staged, { lifetime: 50, cap: 200 }), /too long/);
});

test('Windows runs the installer silently with fixed arguments and no shell', () => {
  assert.deepEqual(updater.windowsInstallerArgs(false), ['--updated', '/S']);
  assert.deepEqual(updater.windowsInstallerArgs(true), ['--updated', '/S', '--force-run']);
  const calls = [];
  const child = updater.launchWindowsInstaller('C:\\data\\updates\\myflames-3.4.0-windows-x64-setup.exe', true,
    (...args) => { calls.push(args); return { unref() { calls.push('unref'); } }; });
  assert.ok(child);
  assert.deepEqual(calls[0], ['C:\\data\\updates\\myflames-3.4.0-windows-x64-setup.exe', ['--updated', '/S', '--force-run'],
    { cwd: path.dirname('C:\\data\\updates\\myflames-3.4.0-windows-x64-setup.exe'), detached: true, stdio: 'ignore', windowsHide: true, shell: false }]);
  assert.equal(calls[1], 'unref');
});

test('a result after the page stopped waiting is still followed (Codex review C3)', async () => {
  const dir = folder(); const staged = updater.verifyStaged((stage(dir), dir), '3.3.3', 'darwin');
  // Late success: Squirrel.Mac staged the update after the wait ended; the app records it.
  let late = 0;
  const slow = fakeUpdater(() => {});
  await assert.rejects(updater.prepareMac(slow, staged, { lifetime: 30, cap: 2000, onLate: () => { late += 1; } }), /too long/);
  assert.equal(slow.listenerCount('update-downloaded'), 1, 'still listening after the timeout');
  const answer = await (await fetch(slow.options.url)).json();
  assert.equal((await fetch(answer.url)).status, 200, 'the feed still serves the zip');
  slow.emit('update-downloaded');
  assert.equal(late, 1);
  assert.equal(slow.listenerCount('update-downloaded') + slow.listenerCount('error'), 0);
  await assert.rejects(fetch(slow.options.url), undefined, 'the feed closed once it settled');
  // Late error: absorbed by the listener prepareMac keeps (no throw from emit).
  const failing = fakeUpdater(() => {});
  await assert.rejects(updater.prepareMac(failing, staged, { lifetime: 30, cap: 2000, onLate: () => { late += 1; } }), /too long/);
  assert.doesNotThrow(() => failing.emit('error', new Error('late failure')));
  assert.equal(late, 1, 'a late error never marks the update ready');
});
