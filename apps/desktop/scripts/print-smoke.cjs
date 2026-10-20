'use strict';
// Isolated Electron export smoke. Uses the production HTML and print helpers,
// but never starts a database backend or opens the user's app data.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawn, execFileSync } = require('node:child_process');
const assert = require('node:assert/strict');
const native = process.argv.includes('--native');
if (!process.versions.electron) {
  (async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), 'myflames-print-smoke-'));
    const output = path.resolve(__dirname, '../../../local/print-smoke-2026-10-16');
    fs.mkdirSync(output, { recursive: true });
    const { build } = require('../../workspace/node_modules/esbuild');
    const entry = path.resolve(__dirname, '../../workspace/src/reportTableExport.ts');
    await build({ stdin: { contents: `import {htmlReport, printReport} from ${JSON.stringify(entry)}; window.exportSmoke={htmlReport,printReport};`, resolveDir: path.dirname(entry) },
      bundle: true, outfile: path.join(home, 'export.js'), platform: 'browser', format: 'iife' });
    const env = { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: 'vault', PRINT_SMOKE_HOME: home, PRINT_SMOKE_OUTPUT: output };
    delete env.ELECTRON_RUN_AS_NODE; delete env.MYFLAMES_VAULT_PASSPHRASE;
    const child = spawn(require('electron'), [__filename, ...(native ? ['--native'] : [])], { env, stdio: 'inherit' });
    const timer = setTimeout(() => child.kill('SIGTERM'), native ? 180000 : 45000);
    try {
      const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); });
      assert.equal(code, 0, 'Electron print smoke exited successfully');
      const pdf = path.join(output, 'report.pdf');
      const details = execFileSync('pdfinfo', [pdf], { encoding: 'utf8' });
      const pages = Number(details.match(/^Pages:\s+(\d+)/m)?.[1]);
      assert.ok(pages > 1, 'the full report spans multiple pages');
      const text = execFileSync('pdftotext', ['-layout', pdf, '-'], { encoding: 'utf8' });
      assert.ok(text.includes('FIRST_ROW_000'), 'first row is in the PDF');
      assert.ok(text.includes('LAST_ROW_250'), 'last row is in the PDF');
      assert.ok(text.includes('数据库'), 'Unicode text survives PDF export');
      const result = JSON.parse(fs.readFileSync(path.join(output, 'result.json'), 'utf8'));
      Object.assign(result, { pages, firstAndLastRows: true, unicode: true });
      fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
      console.log(JSON.stringify(result));
    } finally { clearTimeout(timer); fs.rmSync(home, { recursive: true, force: true }); }
  })().catch(error => { console.error(error); process.exitCode = 1; });
} else {
  const { app, BrowserWindow, session } = require('electron');
  const http = require('node:http');
  const home = process.env.PRINT_SMOKE_HOME, output = process.env.PRINT_SMOKE_OUTPUT;
  app.setName('MyFlames Print Smoke');
  app.setPath('userData', path.join(home, 'app'));
  app.setPath('sessionData', path.join(home, 'chromium'));
  app.enableSandbox();
  let server, window;
  process.on('SIGTERM', () => app.quit());
  app.on('window-all-closed', () => app.quit());
  app.on('will-quit', () => server?.close());
  app.whenReady().then(async () => {
    const assets = fs.readFileSync(path.join(home, 'export.js'));
    let report = '';
    server = http.createServer((req, res) => {
      if (req.url === '/export.js') { res.setHeader('Content-Type', 'text/javascript'); res.end(assets); return; }
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(req.url === '/report' ? report : '<!doctype html><html><head><title>MyFlames Print Smoke</title></head><body><h1>MyFlames isolated print test</h1><button id="print">Open native print dialog</button><script src="/export.js"></script></body></html>');
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const origin = `http://127.0.0.1:${server.address().port}`;
    const isolated = session.fromPartition('print-smoke');
    await isolated.setProxy({ mode: 'direct' });
    isolated.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    isolated.setPermissionCheckHandler(() => false);
    isolated.setDevicePermissionHandler(() => false);
    const sameOrigin = url => { try { return new URL(url).origin === origin; } catch { return false; } };
    isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !(sameOrigin(details.url) || details.url.startsWith(`blob:${origin}/`) ||
      (details.resourceType !== 'mainFrame' && (details.url.startsWith('data:') || details.url === 'about:blank'))) }));
    window = new BrowserWindow({ title: 'MyFlames Print Smoke', width: 1200, height: 800, show: native,
      webPreferences: { session: isolated, sandbox: true, contextIsolation: true, nodeIntegration: false,
        nodeIntegrationInWorker: false, nodeIntegrationInSubFrames: false, webSecurity: true, allowRunningInsecureContent: false, webviewTag: false } });
    const contents = window.webContents;
    contents.on('will-navigate', (event, url) => { if (!sameOrigin(url)) event.preventDefault(); });
    contents.on('will-redirect', (event, url) => { if (!sameOrigin(url)) event.preventDefault(); });
    contents.on('will-attach-webview', event => event.preventDefault());
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    await window.loadURL(origin);
    assert.equal(await contents.executeJavaScript('typeof require'), 'undefined');
    report = await contents.executeJavaScript(`exportSmoke.htmlReport('Tables 数据库', 'Synthetic export smoke', [{label:'Table',value:r=>r.name},{label:'Rows',value:r=>r.number}], Array.from({length:251},(_,n)=>({number:n,name:(n===0?'FIRST_ROW_000':n===250?'LAST_ROW_250':'table_'+n)+' 数据库 '+'long_identifier_'.repeat(7)})))`);
    await window.loadURL(origin + '/report');
    await contents.executeJavaScript('document.fonts.ready.then(() => true)');
    fs.writeFileSync(path.join(output, 'report.pdf'), await contents.printToPDF({ preferCSSPageSize: true, printBackground: true }));
    fs.writeFileSync(path.join(output, 'report.png'), (await contents.capturePage()).toPNG());
    const result = { sandbox: contents.getLastWebPreferences().sandbox, nodeIntegration: false, permissionHandlersDeny: true,
      productionHtmlReport: true, printToPDF: true, nativeRequested: native, nativeDialogVerified: false };
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(result, null, 2));
    if (!native) { app.quit(); return; }
    await window.loadURL(origin);
    await contents.executeJavaScript(`document.querySelector('#print').onclick=()=>exportSmoke.printReport(${JSON.stringify(report)}); true`);
    window.show(); window.focus();
    console.log('PRINT_SMOKE_READY: Click Open native print dialog in MyFlames Print Smoke. Close this test window after inspecting and cancelling the dialog.');
  }).catch(error => { console.error(error); app.exit(1); });
}
