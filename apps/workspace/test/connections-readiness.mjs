// Coordinator: npm --prefix apps/workspace run build, then run this outside
// the socket sandbox. No real database is contacted by this suite.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { connectionAction, connectionRow } from './connection-helpers.mjs';
import { chooseConnection, openConnectionPicker } from './chrome-helpers.mjs';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-readiness-browser-'));
const database = path.join(directory, 'workspace.sqlite3');
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' };
delete env.MYFLAMES_VAULT_PASSPHRASE;
const seed = spawnSync(process.env.PYTHON || 'python3', ['-c', `
import json,sys
from myflames.workspace_store import WorkspaceStore
s=WorkspaceStore(sys.argv[1])
items=[s.save('profile', dict(name=name, host=host, user='reader', port=3306, ssl_mode='PREFERRED', database='',type='production'), remember=False) for name,host in [('Primary','db.example'),('Other','other.example')]]
print(json.dumps(items))`, database], { cwd: root, env, encoding: 'utf8' });
assert.equal(seed.status, 0, seed.stderr);
const profiles = JSON.parse(seed.stdout);
const primary = profiles[0];
const key = JSON.stringify([primary.id, 'db.example', 3306, 'reader', 'PREFERRED', '']);
const facts = { product: 'MySQL', version: '8.4.3', read_only: false, super_read_only: false,
  performance_schema: true, replica: true, group_member: false, galera_member: false, checked_at: new Date(Date.now() - 7200000).toISOString() };
const report = { ...facts, engine: 'mysql', flavor: 'mysql', notes: [], privileges: { PROCESS: false },
  actions: { kill: { allowed: false, need: 'CONNECTION_ADMIN or SUPER', note: '' } }, password: 'NEVER_CACHE_THIS' };
const imported = { name: 'Migrated SSH', host: 'mysql.internal', port: 3307, user: 'app', database: 'shop',
  ssh_host: 'bastion.example', ssh_user: 'deploy', ssh_port: 2222 };
const hostile = { name: '<img src=x onerror="window.injected=true">', host: 'hostile.example', port: 3306, user: 'reader', database: '' };
const server = spawn(process.env.PYTHON || 'python3', ['-c',
  'from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()', database], { cwd: root, env });
let browser;
const readinessRequests = [], imports = [], saves = [], diagnostics = [], failed = [], httpErrors = [];
let offline = false;
try {
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error(output || 'Server did not start')), 10000);
    server.stderr.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    server.once('error', e => { clearTimeout(timer); reject(e); });
  });
  browser = await puppeteer.launch({ headless: 'shell' });
  const page = await browser.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', e => diagnostics.push(String(e)));
  const consoleErrors = [];
  page.on('console', msg => { if (['error', 'warning'].includes(msg.type())) consoleErrors.push(msg.text()); });
  page.on('requestfailed', req => failed.push({ url: req.url(), error: req.failure()?.errorText }));
  page.on('response', response => { if (response.status() >= 400) httpErrors.push({ path: new URL(response.url()).pathname, status: response.status() }); });
  await page.evaluateOnNewDocument((key, facts) => {
    const name = 'myflames.connectionReadiness.v1';
    if (!localStorage.getItem(name)) localStorage.setItem(name, JSON.stringify({ [key]: { facts } }));
  }, key, facts);
  await page.setRequestInterception(true);
  page.on('request', request => {
    const route = new URL(request.url()).pathname;
    const body = JSON.parse(request.postData() || '{}');
    const respond = (value, status = 200) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (route === '/api/live/readiness') {
      readinessRequests.push(body);
      return respond(offline ? { error: 'Synthetic offline check' } : report, offline ? 400 : 200);
    }
    if (route === '/api/connection/test') return respond({ version: '8.4.3', databases: ['shop'], product: 'MySQL' });
    if (route === '/api/connections/import') {
      imports.push(body);
      if (body.tool === 'dbeaver') return respond(body.choose ? { found: true, cancelled: false, items: [{ ...imported, name: 'DBeaver MariaDB', host: 'maria.internal' }] } : { found: false, cancelled: false, items: [] });
      return respond({ found: true, cancelled: false, items: [
        { ...imported, name: 'Primary' },
        { name: 'Endpoint duplicate', host: 'db.example', port: 3306, user: 'reader', database: '' }, imported, hostile,
      ] });
    }
    if (route === '/api/profiles/save') saves.push(body);
    // Every other server access is explicitly refused, including background
    // privilege reads. No request can accidentally reach a real database.
    if (route.startsWith('/api/live/') || route.startsWith('/api/editor/'))
      return respond({ error: 'Synthetic database access refusal' }, 400);
    return request.continue();
  });
  const exact = async (selector, text) => {
    await page.waitForFunction((s, value) => [...document.querySelectorAll(s)].some(e => e.textContent.trim() === value), {}, selector, text);
    const handle = await page.evaluateHandle((s, value) => [...document.querySelectorAll(s)].find(e => e.textContent.trim() === value), selector, text);
    await handle.click(); await handle.dispose();
  };
  const noOverflow = async () => {
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
  };
  const home = async () => { await page.locator('.main-nav [data-place="home"]').click(); };
  const menu = async () => {
    await page.locator('.connections-header-actions button[aria-label="Connections menu"]').click();
    await exact('[role="menu"] button', 'Import from MySQL Workbench or DBeaver…');
    await page.waitForSelector('dialog.connection-import-sheet[open]');
  };
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.waitForSelector(connectionRow('Primary'));
  assert.equal(readinessRequests.length, 0, 'Production is not checked on startup');
  await page.waitForFunction(selector => document.querySelector(selector)?.textContent.includes('MySQL 8.4.3') && document.querySelector(selector)?.textContent.includes('stale'), {}, `${connectionRow('Primary')} [data-col=server]`);
  assert.match(await page.$eval(`${connectionRow('Primary')} [data-col=server]`, e => e.textContent), /replica.*Performance Schema on.*seen/);
  await connectionAction(page, 'Primary', 'Check now');
  await page.waitForFunction(selector => !document.querySelector(selector)?.textContent.includes('stale') && !document.querySelector(selector)?.textContent.includes('Checking'), {}, `${connectionRow('Primary')} [data-col=server]`);
  assert.equal(readinessRequests.length, 1);
  assert.equal(readinessRequests[0].connection.profile_id, primary.id);
  offline = true;
  await connectionAction(page, 'Primary', 'Check now');
  await page.waitForFunction(selector => document.querySelector(selector)?.textContent.includes('Synthetic offline check') && document.querySelector(selector)?.textContent.includes('stale'), {}, `${connectionRow('Primary')} [data-col=server]`);
  assert.match(await page.$eval(`${connectionRow('Primary')} [data-col=server]`, e => e.textContent), /MySQL 8.4.3/);
  offline = false;
  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: 900 });
      await noOverflow();
      await page.screenshot({ path: path.join(directory, `connections-${theme}-${width}.png`) });
      await menu();
      await page.waitForFunction(() => document.querySelector('.connection-import-preview')?.textContent.includes('Migrated SSH'));
      const checkboxes = await page.$$eval('.connection-import-preview input[type=checkbox]', inputs => inputs.map(e => ({ checked: e.checked, disabled: e.disabled })));
      assert.deepEqual(checkboxes, [{ checked: false, disabled: true }, { checked: false, disabled: true }, { checked: true, disabled: false }, { checked: true, disabled: false }]);
      assert.equal(await page.$('.connection-import-preview img'), null);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      assert.match(await page.$eval('.connection-import-preview', e => e.textContent), /bastion.example:2222/);
      await noOverflow();
      await page.screenshot({ path: path.join(directory, `import-${theme}-${width}.png`) });
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('dialog.connection-import-sheet'));
      await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Connections menu');
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  await menu();
  await exact('dialog.connection-import-sheet button', 'Import 2 connections');
  await page.waitForFunction(() => document.querySelector('dialog.connection-import-sheet')?.textContent.includes('Imported 2 connections.'));
  await exact('dialog.connection-import-sheet button', 'Done');
  await page.waitForSelector(connectionRow('Migrated SSH'));
  assert.equal(saves.length, 2);
  for (const saved of saves) {
    assert.equal(saved.value.type, 'production');
    assert.equal(saved.remember_password, false);
    assert.equal(saved.password, undefined);
    assert.equal(saved.value.password, undefined);
  }
  assert.equal(saves[0].value.ssh_host, 'bastion.example');
  await menu();
  await page.waitForSelector('dialog.connection-import-sheet select[aria-label="Import from"]:not(:disabled)');
  await page.select('dialog.connection-import-sheet select[aria-label="Import from"]', 'dbeaver');
  await page.waitForFunction(() => document.querySelector('dialog.connection-import-sheet')?.textContent.includes('Default file not found'));
  await exact('dialog.connection-import-sheet button', 'Choose file…');
  await page.waitForFunction(() => document.querySelector('.connection-import-preview')?.textContent.includes('DBeaver MariaDB'));
  assert.deepEqual(imports.at(-1), { tool: 'dbeaver', choose: true });
  await exact('dialog.connection-import-sheet button', 'Import 1 connections');
  await page.waitForFunction(() => document.querySelector('dialog.connection-import-sheet')?.textContent.includes('Imported 1 connections.'));
  await exact('dialog.connection-import-sheet button', 'Done');
  await page.waitForSelector(connectionRow('DBeaver MariaDB'));
  // A picker choice sends exactly one readiness read; reloading that active
  // Production connection sends none. The facts still show while offline.
  const beforeChoice = readinessRequests.length;
  await page.locator('.main-nav [data-place="query"]').click();
  await page.waitForSelector('.window-toolbar .connection-pill');
  await chooseConnection(page, primary.id);
  await openConnectionPicker(page);
  await page.waitForFunction(() => {
    const text = document.querySelector('.window-toolbar .picker-panel .connection-readiness')?.textContent || '';
    return text.includes('MySQL 8.4.3') && !text.includes('stale') && !text.includes('Checking');
  });
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.window-toolbar .picker-panel'));
  assert.equal(readinessRequests.length, beforeChoice + 1);
  const beforeReload = readinessRequests.length;
  await page.reload({ waitUntil: 'networkidle0' });
  assert.equal(readinessRequests.length, beforeReload);
  await home();
  await page.waitForSelector(connectionRow('Primary'));
  assert.match(await page.$eval(`${connectionRow('Primary')} [data-col=server]`, e => e.textContent), /MySQL 8.4.3.*stale/);
  assert.ok(!(await page.evaluate(() => localStorage.getItem('myflames.connectionReadiness.v1'))).includes('NEVER_CACHE_THIS'));
  // Test connection loads readiness as well as the database list and uses
  // one concise privilege hint in the Add/Edit sheet.
  await connectionAction(page, 'Primary', 'Edit');
  const beforeTest = readinessRequests.length;
  await exact('.connection-panel button', 'Test connection');
  await page.waitForFunction(() => document.querySelector('.connection-panel')?.textContent.includes('Kill needs CONNECTION_ADMIN or SUPER.'));
  assert.equal(readinessRequests.length, beforeTest + 1);
  assert.match(await page.$eval('.connection-panel', e => e.textContent), /Connected · MySQL 8.4.3 · replica · Performance Schema on/);
  await exact('.connection-panel .button-row button', 'Cancel');
  await connectionAction(page, 'Other', 'Edit');
  await page.waitForFunction(() => document.querySelector('.connection-panel .connection-readiness')?.textContent.includes('Not checked'));
  assert.deepEqual(diagnostics, []);
  assert.deepEqual(failed, []);
  assert.ok(httpErrors.every(r => r.status === 400 && ['/api/live/', '/api/editor/'].some(prefix => r.path.startsWith(prefix))), JSON.stringify(httpErrors));
  assert.ok(consoleErrors.every(message => /^Failed to load resource: the server responded with a status of 400/.test(message)), JSON.stringify(consoleErrors));
  console.log(`Connections readiness and import browser checks passed. Screenshots: ${directory}`);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) { server.kill('SIGINT'); await once(server, 'exit'); }
}
