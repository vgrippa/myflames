'use strict';
const { app, BrowserWindow, dialog, session, shell, Menu } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { backendEnvironment, startBackend, sameOrigin, externalLink, dataPath, isolatedBackendHome } = require('./backend.cjs');

app.setName('myflames');
process.on('SIGTERM', () => app.quit());
process.on('SIGINT', () => app.quit());
app.enableSandbox();
const dataDirectory = dataPath(process.env.MYFLAMES_DESKTOP_DATA ||
  path.join(app.getPath('appData'), app.isPackaged ? 'myflames-desktop' : 'myflames-desktop-development'), os.homedir());
fs.mkdirSync(dataDirectory, { recursive: true, mode: 0o700 });
app.setPath('userData', dataDirectory);
app.setPath('sessionData', path.join(dataDirectory, 'chromium'));
let backend, launching, window, quitting = false, fatal = false, blockedSmokeRequests = 0;
const smoke = process.argv.includes('--smoke') && (!app.isPackaged || process.env.MYFLAMES_DESKTOP_SMOKE === '1');
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { if (window) { if (window.isMinimized()) window.restore(); window.focus(); } });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault(); quitting = true;
    Promise.resolve(backend ? backend.stop() : launching?.stop()).catch(() => { fatal = true;
      if (!smoke) dialog.showErrorBox('Backend shutdown incomplete', 'The desktop could not confirm that all owned backend processes stopped. Check your running database operations.');
    }).finally(() => app.exit(fatal ? 1 : 0));
  });
  app.whenReady().then(start).catch(fail);
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
  fs.mkdirSync(env.HOME, { recursive: true, mode: 0o700 });
  const root = path.resolve(__dirname, '../..');
  const binary = path.join(process.resourcesPath, 'backend', 'myflames-backend' + (process.platform === 'win32' ? '.exe' : ''));
  launching = startBackend({
    command: app.isPackaged ? binary : (process.env.MYFLAMES_DESKTOP_PYTHON || (process.platform === 'win32' ? 'python' : 'python3')),
    args: [...(app.isPackaged ? [] : ['-m', 'myflames']), 'ui', '--port', '0', '--no-browser'],
    cwd: app.isPackaged ? process.resourcesPath : root, env,
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
  browserSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  browserSession.setPermissionCheckHandler(() => false);
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
    webPreferences: { session: browserSession, sandbox: true, contextIsolation: true,
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
      if (!externalBlocked || blockedSmokeRequests < 1) throw new Error('Desktop origin restriction failed.');
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
      fs.writeFileSync(path.join(dataDirectory, 'smoke.json'), JSON.stringify({ ready: true, packaged: app.isPackaged, backendExecutable: backend.child.spawnfile, shellNavigation: true, completion: true, authenticated: true, reloadAuthenticated: reloaded, sandbox: window.webContents.getLastWebPreferences().sandbox, nodeIntegration: false, backendPid: backend.child.pid }));
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
