'use strict';
// In-place application updates. The backend (myflames/app_update.py) downloads
// the release's update asset into the app's private updates folder, checks it
// against the release's .sha256 and writes staged-update.json. This module
// re-reads that manifest from the fixed folder (never a path from the page),
// hashes the file again and hands it to the platform installer: on macOS
// Electron's autoUpdater (Squirrel.Mac), which installs it only when its code
// signature satisfies the running app's designated requirement; on Windows the
// NSIS installer, run silently after the backend has stopped.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const http = require('node:http');
const { spawn } = require('node:child_process');
const { sameOrigin } = require('./backend.cjs');

const MANIFEST = 'staged-update.json';
const MAX_MANIFEST_BYTES = 4096;
const FEED_LIFETIME = 120000; // Squirrel.Mac reads the feed and the zip within this
const FILE_NAME = /^myflames-([0-9]+\.[0-9]+\.[0-9]+)-(macos|windows)-(arm64|x64|universal)(-setup)?\.(zip|exe)$/i;
const PLATFORM_FILE = { darwin: name => /-macos-[^-]+\.zip$/i.test(name), win32: name => /-windows-[^-]+-setup\.exe$/i.test(name) };

function versionParts(value) {
  const match = typeof value === 'string' && /^v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-.+)?$/.exec(value.trim());
  return match ? match.slice(1, 4).map(Number) : null;
}
function isNewer(candidate, current) {
  const a = versionParts(candidate), b = versionParts(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i += 1) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

// Open a file in the updates folder without following a link at its name.
function openPlain(file) {
  const before = fs.lstatSync(file);
  if (!before.isFile()) throw new Error('The staged update is not a plain file.');
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW || 0));
  const after = fs.fstatSync(fd);
  if (!after.isFile() || after.ino !== before.ino || after.dev !== before.dev) {
    fs.closeSync(fd); throw new Error('The staged update changed while it was read.');
  }
  return { fd, size: after.size };
}

// staged-update-1.0, as app_update.stage_update writes it; null when absent.
function readManifest(folder, platform = process.platform) {
  const file = path.join(folder, MANIFEST);
  try { fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  const { fd, size } = openPlain(file);
  let text;
  try {
    if (size > MAX_MANIFEST_BYTES) throw new Error('The staged update record is too large.');
    text = fs.readFileSync(fd, 'utf8');
  } finally { fs.closeSync(fd); }
  let record;
  try { record = JSON.parse(text); } catch { throw new Error('The staged update record is unreadable.'); }
  const match = record && typeof record.file === 'string' && FILE_NAME.exec(record.file);
  if (!record || record.schema_version !== 'staged-update-1.0' || !match || record.platform !== platform ||
      !PLATFORM_FILE[platform] || !PLATFORM_FILE[platform](record.file) || record.version !== match[1] ||
      !Number.isSafeInteger(record.size) || record.size <= 0 || typeof record.sha256 !== 'string' ||
      !/^[0-9a-f]{64}$/.test(record.sha256)) {
    throw new Error('The staged update record is not valid.');
  }
  return { version: record.version, file: record.file, size: record.size, sha256: record.sha256 };
}

function sha256(fd) {
  const hash = crypto.createHash('sha256');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  let position = 0, read;
  while ((read = fs.readSync(fd, buffer, 0, buffer.length, position)) > 0) {
    hash.update(buffer.subarray(0, read)); position += read;
  }
  return hash.digest('hex');
}

// The staged update, checked again here: a newer version, a plain file of the
// recorded size whose SHA-256 matches. Returns its absolute path and version.
function verifyStaged(folder, currentVersion, platform = process.platform) {
  const record = readManifest(folder, platform);
  if (!record) throw new Error('No update has been downloaded.');
  if (!isNewer(record.version, currentVersion)) throw new Error('The downloaded update is not newer than this version.');
  const file = path.join(folder, record.file);
  const { fd, size } = openPlain(file);
  try {
    if (size !== record.size) throw new Error('The downloaded update has the wrong size.');
    if (sha256(fd) !== record.sha256) throw new Error('The downloaded update does not match its checksum.');
  } finally { fs.closeSync(fd); }
  return { version: record.version, file, size: record.size, sha256: record.sha256 };
}

// Only the workspace's own top frame may use the update bridge.
function isWorkspaceSender(event, contents, origin) {
  return !!event && event.sender === contents && !!event.senderFrame && event.senderFrame === contents.mainFrame &&
    sameOrigin(event.senderFrame.url, origin);
}

// Remove a staged update that is no longer newer (it was installed, or a newer
// version was installed by hand) or whose record cannot be read.
function removeStale(folder, currentVersion, platform = process.platform) {
  let record = null;
  try { record = readManifest(folder, platform); } catch { /* unreadable: remove below */ }
  if (record && isNewer(record.version, currentVersion)) return false;
  let names;
  try { names = fs.readdirSync(folder); } catch { return false; }
  for (const name of names) {
    const file = path.join(folder, name);
    try { if (!fs.lstatSync(file).isDirectory()) fs.unlinkSync(file); } catch { /* already gone */ }
  }
  return names.length > 0;
}

// Why this copy cannot update itself in place, or null when it can.
function unsupportedReason({ platform, packaged, inApplications, bundleWritable }) {
  if (!packaged) return 'Development builds do not update in place.';
  if (platform === 'darwin') {
    if (!inApplications) return 'Move myflames to the Applications folder to update it in place.';
    if (!bundleWritable) return 'myflames cannot replace itself in this folder.';
    return null;
  }
  if (platform === 'win32') return null;
  return 'This system does not update in place.';
}

// Squirrel.Mac reads a JSON feed and then the zip it names. Serve exactly
// those two answers, once, on loopback under an unguessable path.
function serveOnce(file, size, { lifetime = FEED_LIFETIME } = {}) {
  const token = crypto.randomBytes(32).toString('hex');
  let served = false;
  const server = http.createServer((request, response) => {
    const { pathname } = new URL(request.url, 'http://127.0.0.1');
    if (request.method === 'GET' && pathname === `/${token}/feed.json`) {
      const { port } = server.address();
      const body = JSON.stringify({ url: `http://127.0.0.1:${port}/${token}/update.zip` });
      response.writeHead(200, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) });
      response.end(body); return;
    }
    if (request.method === 'GET' && pathname === `/${token}/update.zip` && !served) {
      served = true;
      response.writeHead(200, { 'Content-Type': 'application/zip', 'Content-Length': size });
      fs.createReadStream(file).on('error', () => response.destroy()).pipe(response); return;
    }
    response.writeHead(404); response.end();
  });
  const timer = setTimeout(() => close(), lifetime);
  timer.unref?.();
  function close() { clearTimeout(timer); server.close(); server.closeAllConnections?.(); }
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve({ url: `http://127.0.0.1:${server.address().port}/${token}/feed.json`, close }));
  });
}

// Squirrel.Mac's error in plain words, without the cache paths it names.
function squirrelReason(error) {
  const message = error && typeof error.message === 'string' ? error.message : '';
  if (/code signature|did not pass validation|code requirement/i.test(message)) {
    return 'The update is not signed by the developer of this copy of myflames, so it was not installed.';
  }
  const plain = message.replace(/(?:file|https?):\/\/\S+/g, '').replace(/\s+/g, ' ').trim();
  return plain ? 'The update could not be installed: ' + plain.replace(/[.:]+$/, '') + '.' : 'The update could not be installed.';
}

// Hand a verified zip to Squirrel.Mac; resolves when it has staged the update.
async function prepareMac(autoUpdater, staged, { lifetime = FEED_LIFETIME } = {}) {
  const feed = await serveOnce(staged.file, staged.size, { lifetime });
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error('Installing the update took too long.')), lifetime);
      const onDownloaded = () => finish();
      const onError = error => finish(new Error(squirrelReason(error)));
      const onNone = () => finish(new Error('The update could not be installed.'));
      function finish(error) {
        clearTimeout(timer);
        autoUpdater.removeListener('update-downloaded', onDownloaded);
        autoUpdater.removeListener('error', onError);
        autoUpdater.removeListener('update-not-available', onNone);
        if (error) reject(error); else resolve();
      }
      autoUpdater.on('update-downloaded', onDownloaded);
      autoUpdater.on('error', onError);
      autoUpdater.on('update-not-available', onNone);
      autoUpdater.setFeedURL({ url: feed.url, serverType: 'json' });
      autoUpdater.checkForUpdates();
    });
  } finally { feed.close(); }
}

// The NSIS installer electron-builder makes: /S silent, --updated keeps the
// app's data, --force-run starts the new version when it finishes.
function windowsInstallerArgs(relaunch) {
  return ['--updated', '/S', ...(relaunch ? ['--force-run'] : [])];
}
function launchWindowsInstaller(file, relaunch, run = spawn) {
  const child = run(file, windowsInstallerArgs(relaunch), { detached: true, stdio: 'ignore', windowsHide: true, shell: false });
  child.unref();
  return child;
}

module.exports = { MANIFEST, FILE_NAME, isNewer, readManifest, verifyStaged, removeStale, unsupportedReason, isWorkspaceSender,
  serveOnce, prepareMac, windowsInstallerArgs, launchWindowsInstaller };
