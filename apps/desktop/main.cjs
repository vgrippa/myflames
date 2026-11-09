'use strict';
const { app, BrowserWindow, dialog, session, shell, Menu, ipcMain, autoUpdater } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { backendEnvironment, startBackend, sameOrigin, externalLink, dataPath, isolatedBackendHome, toolGroupsFile } = require('./backend.cjs');
const updater = require('./updater.cjs');

app.setName('myflames');
process.on('SIGTERM', () => app.quit());
process.on('SIGINT', () => app.quit());
app.enableSandbox();
const dataDirectory = dataPath(process.env.MYFLAMES_DESKTOP_DATA ||
  path.join(app.getPath('appData'), app.isPackaged ? 'myflames-desktop' : 'myflames-desktop-development'), os.homedir());
fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
app.setPath('userData', dataDirectory);
app.setPath('sessionData', path.join(dataDirectory, 'chromium'));
// The backend downloads and checks an update here (MYFLAMES_UPDATES_DIR);
// updater.cjs reads it back from this fixed folder only.
const updatesDirectory = path.join(dataDirectory, 'updates');
fs.mkdirSync(updatesDirectory, { recursive: true, mode: 0o700 });
// mkdir leaves an existing folder's mode alone; updater.checkFolder refuses
// one that is shared, owned by someone else or a link.
try { if (!fs.lstatSync(updatesDirectory).isSymbolicLink()) fs.chmodSync(updatesDirectory, 0o700); } catch { /* checked before use */ }
let backend, launching, window, quitting = false, exiting = false, fatal = false, blockedSmokeRequests = 0;
// The update the person chose to install ({ version, relaunch }), and the
// macOS restart that lets Squirrel.Mac quit the app itself.
let preparedUpdate = null, preparing = null, restartingForUpdate = false;
const smoke = process.argv.includes('--smoke') && (!app.isPackaged || process.env.MYFLAMES_DESKTOP_SMOKE === '1');
if (!app.requestSingleInstanceLock()) app.quit();
else {
  // After the lock: a second launch must not remove an update being staged.
  updater.removeStale(updatesDirectory, app.getVersion());
  // An autoUpdater error with no listener would throw in the main process;
  // prepareMac listens while it works, and this keeps any later one harmless.
  autoUpdater.on('error', () => {});
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.on('window-all-closed', () => { if (!restartingForUpdate) app.quit(); });
  // Every quit waits for the backend and its tools to stop: a second Quit
  // while it waits is held too. Only Squirrel.Mac's own quit (exiting) passes.
  app.on('before-quit', event => {
    if (exiting) return;
    event.preventDefault();
    if (quitting) return;
    quitting = true;
    stopBackend().finally(() => { installOnQuit(); app.exit(fatal ? 1 : 0); });
  });
  app.whenReady().then(start).catch(fail);
}
function stopBackend() {
  return Promise.resolve(backend ? backend.stop() : launching?.stop()).catch(() => { fatal = true;
    if (!smoke) dialog.showErrorBox('Backend shutdown incomplete', 'The desktop could not confirm that all owned backend processes stopped. Check your running database operations.');
  });
}
function bundleWritable() {
  if (process.platform !== 'darwin') return true;
  const bundle = path.resolve(process.execPath, '..', '..', '..'); // myflames.app
  try { fs.accessSync(bundle, fs.constants.W_OK); fs.accessSync(path.dirname(bundle), fs.constants.W_OK); return true; }
  catch { return false; }
}
function updateSupport() {
  const reason = updater.unsupportedReason({ platform: process.platform, packaged: app.isPackaged,
    inApplications: process.platform === 'darwin' && app.isInApplicationsFolder(), bundleWritable: bundleWritable(),
    customDataFolder: !!process.env.MYFLAMES_DESKTOP_DATA });
  return { ok: true, supported: !reason, reason, ready: preparedUpdate ? preparedUpdate.version : null };
}
// Check the staged update again and give it to the platform installer. The
// page passes nothing: the file and its checksum come from the updates folder.
async function prepareUpdate() {
  const reason = updateSupport().reason;
  if (reason) return { ok: false, reason };
  const staged = updater.verifyStaged(updatesDirectory, app.getVersion());
  if (preparedUpdate && preparedUpdate.version === staged.version) return { ok: true, version: staged.version };
  if (process.platform === 'darwin') {
    // A result after the page stopped waiting still makes the update ready.
    await updater.prepareMac(autoUpdater, staged, { onLate: () => { preparedUpdate = { version: staged.version, relaunch: false }; } });
  }
  preparedUpdate = { version: staged.version, relaunch: false };
  return { ok: true, version: staged.version };
}
async function restartToUpdate() {
  if (!preparedUpdate) return { ok: false, reason: 'No update is ready to install.' };
  if (quitting) return { ok: false, reason: 'myflames is already quitting.' };
  if (process.platform === 'win32') { preparedUpdate.relaunch = true; app.quit(); return { ok: true }; }
  // macOS: stop the backend and its tools first, then Squirrel.Mac closes the
  // windows, quits, replaces the app and opens the new version.
  quitting = true; restartingForUpdate = true;
  await stopBackend();
  if (fatal) { app.exit(1); return { ok: false, reason: 'The backend did not stop.' }; }
  autoUpdater.once('error', () => app.exit(0)); // installed at the next start instead
  setTimeout(() => app.exit(0), 15000).unref();
  exiting = true;
  autoUpdater.quitAndInstall();
  return { ok: true };
}
// Windows installs a prepared update when the app quits, after the backend
// stopped: silently, and starting the new version after Restart to update.
function installOnQuit() {
  if (process.platform !== 'win32' || !preparedUpdate || fatal) return;
  try {
    const staged = updater.verifyStaged(updatesDirectory, app.getVersion());
    if (staged.version === preparedUpdate.version) updater.launchWindowsInstaller(staged.file, preparedUpdate.relaunch);
  } catch { /* offered again at the next start */ }
}
function fail(error) {
  if (quitting) return;
  fatal = true;
  const message = error instanceof Error ? error.message : 'The desktop workspace could not start.';
  // Never include the launch URL or backend stderr in errors or logs.
  if (smoke) process.stderr.write('Desktop smoke failed: ' + message.replace(/http:\/\/\S+/g, '[private launch URL]') + '\n');
  else dialog.showErrorBox('Could not open myflames', message.replace(/http:\/\/\S+/g, '[private launch URL]'));
  app.quit();
}
async function start() {
  const env = backendEnvironment(dataDirectory);
  env.HOME = isolatedBackendHome(dataDirectory, os.homedir());
  env.USERPROFILE = env.HOME;
  // ssh reads ~/.ssh from the user's real home (see backendEnvironment).
  env.MYFLAMES_SSH_HOME = os.homedir();
  // A downloaded update goes to the user's real Downloads folder, not the isolated HOME.
  env.MYFLAMES_DOWNLOADS_DIR = app.getPath('downloads');
  env.MYFLAMES_UPDATES_DIR = updatesDirectory;
  // The packaged backend entry stops itself if this process dies uncleanly.
  env.MYFLAMES_DESKTOP_PARENT = String(process.pid);
  fs.mkdirSync(env.HOME, { recursive: true, mode: 0o700 });
  const root = path.resolve(__dirname, '../..');
  const binary = path.join(process.resourcesPath, 'backend', 'myflames-backend' + (process.platform === 'win32' ? '.exe' : ''));
  launching = startBackend({
    command: app.isPackaged ? binary : (process.env.MYFLAMES_DESKTOP_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')),
    args: [...(app.isPackaged ? [] : ['-m', 'myflames']), 'ui', '--port', '0', '--no-browser'],
    cwd: app.isPackaged ? process.resourcesPath : root, env,
    // Where the backend lists the dump, restore and clone tool process groups
    // it owns, so quitting stops them even if the backend must be killed.
    groupsFile: process.platform === 'win32' ? null : toolGroupsFile(dataDirectory),
  });
  backend = await launching;
  if (quitting) { await backend.stop(); return; }
  if (backend.child.exitCode !== null || backend.child.signalCode !== null) throw new Error('The local backend exited during startup.');
  backend.child.once('exit', () => { if (!quitting) fail(new Error('The local backend stopped. Reopen myflames to start a fresh session.')); });
  const launchUrl = backend.url;
  const origin = new URL(launchUrl).origin;
  backend.url = null; // Only the initial load needs this one-use secret.
  const browserSession = session.fromPartition(`desktop-${crypto.randomUUID()}`);
  await browserSession.setProxy({ mode: 'direct' });
  // One permission is granted, and only to the workspace's own page: the
  // Fullscreen API, for Learn's Full screen (the lesson's container is the
  // full-screen element; Escape leaves). Every other request is refused.
  const fullscreenOnly = (permission, url) => permission === 'fullscreen' && sameOrigin(url || '', origin);
  browserSession.setPermissionRequestHandler((_contents, permission, callback, details) =>
    callback(fullscreenOnly(permission, details && details.requestingUrl)));
  browserSession.setPermissionCheckHandler((_contents, permission, requestingOrigin) => fullscreenOnly(permission, requestingOrigin));
  browserSession.setDevicePermissionHandler(() => false);
  browserSession.webRequest.onBeforeRequest((details, callback) => {
    // The startup smoke exercises local keyword completion only. Never let its
    // synthetic default connection trigger real catalog reads on localhost.
    if (smoke && sameOrigin(details.url, origin) && new URL(details.url).pathname.startsWith('/api/live/')) {
      callback({ cancel: true }); return;
    }
    const allowed = sameOrigin(details.url, origin) || details.url.startsWith(`blob:${origin}/`) ||
      (details.resourceType !== 'mainFrame' && (details.url.startsWith('data:') || details.url === 'about:blank'));
    if (smoke && !allowed && details.url === 'http://127.0.0.1:65534/') blockedSmokeRequests += 1;
    callback({ cancel: !allowed });
  });
  browserSession.on('will-download', (event, item) => {
    if (!sameOrigin(item.getURL(), origin) && !item.getURL().startsWith(`blob:${origin}/`)) event.preventDefault();
    // Electron's normal save dialog asks for an explicit destination.
  });
  window = new BrowserWindow({ width: 1440, height: 960, minWidth: 720, minHeight: 520,
    show: false, title: 'myflames', backgroundColor: '#202124',
    // preload.cjs exposes only the path of a dropped file (the Files place).
    webPreferences: { session: browserSession, sandbox: true, contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs'),
      nodeIntegration: false, nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false, spellcheck: false,
      devTools: !app.isPackaged },
  });
  app.setAboutPanelOptions({ applicationName: 'myflames', applicationVersion: app.getVersion(),
    copyright: 'Apache-2.0', credits: "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs." });
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{ label: 'myflames', submenu: [{ role: 'about' }, { type: 'separator' }, { role: 'quit' }] }] : []),
    { label: 'Edit', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' }, { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: 'View', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }, { role: 'togglefullscreen' }] },
    { label: 'Window', submenu: [{ role: 'minimize' }, { role: 'close' }] },
  ]));
  const contents = window.webContents;
  // The update bridge (preload.cjs): only the workspace's own top frame, no
  // arguments, and a plain { ok: false, reason } instead of a thrown error.
  const handle = (channel, action) => ipcMain.handle(channel, async event => {
    if (!updater.isWorkspaceSender(event, contents, origin)) throw new Error('Refused.');
    try { return await action(); }
    catch (error) { return { ok: false, reason: error instanceof Error ? error.message : 'The update could not be installed.' }; }
  });
  handle('myflames:update-support', async () => updateSupport());
  handle('myflames:update-prepare', () => (preparing ||= prepareUpdate().finally(() => { preparing = null; })));
  handle('myflames:update-restart', restartToUpdate);
  // Restart to update asked about unsaved changes in the page first, and the
  // backend has stopped by now: a beforeunload refusal would only leave a
  // window with no backend until the fallback exit, so it is overridden.
  contents.on('will-prevent-unload', event => { if (restartingForUpdate) event.preventDefault(); });
  contents.on('will-attach-webview', event => event.preventDefault());
  contents.on('will-navigate', (event, url) => { if (!sameOrigin(url, origin)) event.preventDefault(); });
  contents.on('will-redirect', (event, url) => { if (!sameOrigin(url, origin)) event.preventDefault(); });
  contents.setWindowOpenHandler(({ url }) => {
    const safe = externalLink(url);
    if (safe) void shell.openExternal(safe).catch(() => {});
    return { action: 'deny' };
  });
  contents.on('render-process-gone', () => fail(new Error('The workspace window stopped. Reopen myflames to restore the saved workspace.')));
  await window.loadURL(launchUrl);
  window.show();
  if (smoke) await smokeWindow(origin);
}
async function smokeWindow(origin) {
  // Explicit local test mode, never a renderer-controlled IPC channel.
  const deadline = Date.now() + 20000;
  let reloaded = false;
  while (Date.now() < deadline) {
    const status = await window.webContents.executeJavaScript(`({ready: !!document.querySelector('.home-start') && !document.querySelector('.home [role=alert]') && !document.body.innerText.includes('Opening your workspace'), secretGone: !location.hash, node: typeof require, origin: location.origin, text: document.body.innerText})`);
    if (status.ready && status.secretGone) {
      if (status.node !== 'undefined' || status.origin !== origin) throw new Error('Desktop isolation smoke failed.');
      const cookies = await window.webContents.session.cookies.get({ url: origin + '/api/' });
      if (!cookies.some(cookie => cookie.httpOnly)) {
        await new Promise(resolve => setTimeout(resolve, 100));
        continue;
      }
      const authenticated = await window.webContents.executeJavaScript(`fetch('/api/session/load', {method:'POST', headers:{'Content-Type':'application/json', 'X-Myflames-Token':localStorage.getItem('myflames.session-proof') || ''}, body:'{}'}).then(response => response.ok)`);
      if (!authenticated) throw new Error('Authenticated workspace request failed.');
      if (!reloaded) {
        reloaded = true;
        await window.loadURL(origin + '/'); // No launch secret on a normal reload.
        continue;
      }
      const externalBlocked = await window.webContents.executeJavaScript(`fetch('http://127.0.0.1:65534/').then(() => false, () => true)`);
      // The preload is packaged and exposes the Files place's dropped-file
      // paths and the update bridge, which answers from the workspace page.
      const preload = await window.webContents.executeJavaScript(`(async () => { const d = window.myflamesDesktop || {};
        const support = await d.update.support();
        return JSON.stringify(Object.keys(d)) === '["pathForFile","update"]' && JSON.stringify(Object.keys(d.update)) === '["support","prepare","restart"]' &&
          d.pathForFile(new File(['x'], 'x.txt')) === '' && support.ok === true && typeof support.supported === 'boolean'; })()`);
      if (!externalBlocked || blockedSmokeRequests < 1) throw new Error('Desktop origin restriction failed.');
      // A smoke started from a terminal or CI does not make the window key, so
      // Chromium withholds focus events and the editor never opens completion.
      // Emulate page focus for this test mode instead of stealing the user's.
      window.webContents.debugger.attach('1.3');
      await window.webContents.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
      // Exercise the actual shared shell in Electron, not just its HTTP health.
      await window.webContents.executeJavaScript(`document.querySelector('.command-trigger').click()`);
      await waitForSmoke(`!!document.querySelector('dialog.command-palette[open]')`);
      await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('.command-option')).find(element => element.innerText.includes('Query editor')).click()`);
      await waitForSmoke(`!!document.querySelector('textarea.sql-editor') && !document.querySelector('dialog.command-palette')`);
      await window.webContents.executeJavaScript(`
        const ed = document.querySelector('textarea.sql-editor');
        ed.focus();
        ed.select();
        document.execCommand('insertText', false, 'SEL');
      `);
      await waitForSmoke(`!!document.querySelector('.sql-completion [role=option]')`);
      // DOM readiness can precede Chromium's next painted frame after reload.
      // Capture the completed editor, not a cached startup/refusal frame.
      await window.webContents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
      fs.writeFileSync(path.join(dataDirectory, 'smoke.png'), (await window.webContents.capturePage()).toPNG());
      fs.writeFileSync(path.join(dataDirectory, 'smoke.json'), JSON.stringify({ ready: true, packaged: app.isPackaged, backendExecutable: backend.child.spawnfile, shellNavigation: true, completion: true, authenticated: true, reloadAuthenticated: reloaded, preload, sandbox: window.webContents.getLastWebPreferences().sandbox, nodeIntegration: false, backendPid: backend.child.pid }));
      process.stdout.write('Desktop smoke passed: authenticated workspace, sandboxed renderer, isolated data.\n');
      app.quit(); return;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error('The workspace did not finish authenticating.');
}

async function waitForSmoke(expression, label = expression) {
  const deadline = Date.now() + 6000;
  while (Date.now() < deadline) {
    if (await window.webContents.executeJavaScript(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  throw new Error('Desktop workspace interaction timed out: ' + label);
}
