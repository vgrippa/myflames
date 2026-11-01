// Home collection behavior against real profile storage and a temporary vault.
// Only database reads are mocked. Run after npm run build, outside the socket sandbox.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { connectionAction, connectionRow } from './connection-helpers.mjs';
import { openConnectionPicker } from './chrome-helpers.mjs';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-connections-'));
const database = path.join(directory, 'workspace.sqlite3');
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' };
delete env.MYFLAMES_VAULT_PASSPHRASE;
const python = code => {
  const result = spawnSync(process.env.PYTHON || 'python3', ['-c', code, database], { cwd: root, env, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
};
const seeded = python(`from myflames.workspace_store import WorkspaceStore
import json,sys
s=WorkspaceStore(sys.argv[1]); items=[]
for i in range(1,11):
 p=s.save('profile', dict(name='Connection %02d'%i,host='db%d.example'%i,port=3306,user='dba',database='shop',ssl_mode='REQUIRED',type='development' if i in (3,4) else 'production',read_only=i==3), password='synthetic-%d'%i if i<=2 else None, remember=i<=2)
 items.append(dict(id=p['id'],name=p['name'],ref=s._profile(p['id']).get('secret_ref')))
print(json.dumps(items))`);
const server = spawn(process.env.PYTHON || 'python3', ['-c',
  'from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()', database], { cwd: root, env });
let browser;
const deletions = [];
let failDelete = '';
let testReply = null;
try {
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(Error(output || 'Server did not start')), 10000);
    server.stderr.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    server.once('error', error => { clearTimeout(timer); reject(error); });
  });
  browser = await puppeteer.launch({ headless: 'shell' });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on('request', request => {
    const route = new URL(request.url()).pathname;
    const body = JSON.parse(request.postData() || '{}');
    if (route === '/api/profiles/delete') {
      deletions.push(body.id);
      if (body.id === failDelete) return request.respond({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'Synthetic locked profile' }) });
    }
    if (route === '/api/connection/test' && testReply) {
      return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(testReply) });
    }
    if (route.startsWith('/api/live/') || route.startsWith('/api/connection/') || route.startsWith('/api/editor/'))
      return request.respond({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'Database access is mocked in this suite.' }) });
    return request.continue();
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.waitForFunction(() => document.querySelectorAll('.connection-row').length === 11);
  const exact = async (selector, label) => {
    await page.waitForFunction((s, text) => [...document.querySelectorAll(s)].some(el => el.textContent.trim() === text), {}, selector, label);
    const button = await page.evaluateHandle((selector, label) => [...document.querySelectorAll(selector)].find(el => el.textContent.trim() === label), selector, label);
    await button.click();
    await button.dispose();
  };
  const checkbox = async name => page.locator(`${connectionRow(name)} input[type=checkbox]`).click();
  const focusedRow = async name => page.waitForFunction(name => document.activeElement?.getAttribute('aria-label') === name, {}, name);
  const noBulkDelete = async () => assert.equal(await page.$$eval('.connections-table-toolbar button', els => els.some(el => /^Delete \(/.test(el.textContent))), false);
  const noOverflow = async () => assert.equal(await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - innerWidth)), 0);
  const geometry = async () => page.evaluate(() => {
    const rect = selector => { const b = document.querySelector(selector).getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height, right: b.right, bottom: b.bottom }; };
    const table = document.querySelector('.connections .data-table-wrap').getBoundingClientRect();
    return { connections: rect('.connections'), recent: rect('.recent-work'), table: rect('.connections .data-table-wrap'),
      visibleRows: [...document.querySelectorAll('.connection-row')].filter(el => { const b = el.getBoundingClientRect(); return b.top >= table.top && b.bottom <= Math.min(table.bottom, innerHeight); }).length };
  });
  // Density and overflow are measured in both appearances, never inferred from CSS.
  for (const scheme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: scheme }]);
    await page.setViewport({ width: 1440, height: 900 });
    await page.waitForSelector('.connections [data-col=type]');
    await noOverflow();
    const desktop = await geometry();
    assert.ok(desktop.visibleRows >= 8, JSON.stringify(desktop));
    assert.ok(desktop.recent.y < 900 && desktop.recent.x >= desktop.connections.right - 1, JSON.stringify(desktop));
    assert.equal(await page.$eval(`${connectionRow('Connection 01')} [data-col=password]`, el => el.textContent), 'Saved');
    assert.equal(await page.$eval(`${connectionRow('Connection 03')} [data-col=password]`, el => el.textContent), 'Not saved');
    assert.match(await page.$eval(`${connectionRow('Connection 01')} [data-col=password] span`, el => el.title), /encrypted on this computer/);
    // Type and Read-only are two compact pills, never one truncating string.
    const pills = await page.$$eval(`${connectionRow('Connection 03')} [data-col=type] .type-tag`, els => els.map(el => {
      const box = el.getBoundingClientRect();
      const cell = el.closest('td').getBoundingClientRect();
      return { text: el.textContent, fits: box.right <= cell.right + 1 };
    }));
    assert.deepEqual(pills, [{ text: 'Development', fits: true }, { text: 'Read-only', fits: true }], 'both pills fit their cell');
    // The address column is the flexible one; the form's word heads the schema
    // column; a connection never used in this window shows — with the reason.
    const headers = await page.$$eval('.connections thead [data-col] .th-label', els => els.map(el => el.textContent));
    assert.ok(headers.includes('Default database'), JSON.stringify(headers));
    assert.ok(!headers.includes('Default schema'), JSON.stringify(headers));
    const lastUsed = await page.$eval(`${connectionRow('Connection 03')} [data-col=last-used] span`, el => ({ text: el.textContent, title: el.title }));
    assert.equal(lastUsed.text.startsWith('—'), true);
    assert.match(lastUsed.title, /Not used in this window\. Earlier use is not recorded\./);
    console.log(`${scheme} 1440x900 ${JSON.stringify(desktop)}`);
    await page.screenshot({ path: path.join(directory, `home-${scheme}-1440.png`) });
    await connectionAction(page, 'Connection 03', 'Edit');
    await page.waitForSelector('.connections .view-inspector:not(.is-sheet)');
    assert.deepEqual(await page.$$eval('.connections thead [data-col]', els => els.map(el => el.dataset.col)), ['select', 'name']);
    assert.equal(await page.$eval('.connections .data-table-wrap', el => el.scrollWidth <= el.clientWidth), true, 'compact table fits beside the inspector');
    await noBulkDelete();
    assert.ok(await page.$('.connection-panel .delete-connection'));
    // The form caps at a readable width, Cancel closes it like Back to
    // connections, and Delete connection carries the destructive style.
    const panelWidth = await page.$eval('.connection-panel', el => el.getBoundingClientRect().width);
    assert.ok(panelWidth <= 560, `form width ${panelWidth}`);
    assert.equal(await page.$eval('.connection-panel .delete-connection', el => el.classList.contains('danger')), true);
    await noOverflow();
    await page.screenshot({ path: path.join(directory, `home-${scheme}-1440-edit.png`) });
    await exact('.connection-panel .button-row button', 'Cancel');
    await page.waitForSelector('.connections [data-col=password]');
    // Wherever the compact table hides the Type column, the type stays in
    // words beside the dot (never its color alone).
    const shortTypes = async () => {
      for (const [name, type, short] of [['Connection 01', 'Production', 'Prod'], ['Connection 04', 'Development', 'Dev'], ['Connection 03', 'Development, read-only', 'Dev']]) {
        const dot = await page.$eval(`${connectionRow(name)} [data-col=name] .type-dot`, el => ({
          label: el.getAttribute('aria-label'), role: el.getAttribute('role'), width: el.getBoundingClientRect().width,
        }));
        assert.equal(dot.label, type);
        assert.equal(dot.role, 'img');
        assert.ok(dot.width > 0, 'type dot remains visible beside the name');
        const label = await page.$eval(`${connectionRow(name)} [data-col=name] .connection-type-short`, el => ({
          text: el.textContent.trim(), hidden: el.getAttribute('aria-hidden'),
          width: el.getBoundingClientRect().width, height: el.getBoundingClientRect().height,
          clipped: el.scrollWidth > el.clientWidth,
          outlined: el.classList.contains('is-read-only') && getComputedStyle(el).borderTopStyle === 'solid',
        }));
        assert.equal(label.text, short);
        assert.equal(label.outlined, type.endsWith('read-only'), 'read-only keeps its type word, outlined');
        assert.equal(label.hidden, 'true', 'the existing dot still provides the screen-reader label');
        assert.ok(label.width > 0 && label.height > 0, 'type is visible without relying on color');
        assert.equal(label.clipped, false, 'the short type label does not truncate');
      }
    };
    await page.setViewport({ width: 720, height: 520 });
    await page.waitForFunction(() => !document.querySelector('.connections thead [data-col=password]') && !!document.querySelector('.connections thead [data-col=type]'));
    await noOverflow();
    assert.equal(await page.$eval('.home .connections > .view-toolbar', bar => {
      const h2 = bar.querySelector('h2').getBoundingClientRect();
      const add = bar.querySelector('button').getBoundingClientRect();
      return Math.abs(h2.top + h2.height / 2 - (add.top + add.height / 2)) <= 8;
    }), true, 'Add connection stays on the heading row at 720 px');
    await page.screenshot({ path: path.join(directory, `home-${scheme}-720.png`) });
    await page.setViewport({ width: 390, height: 844 });
    await page.waitForFunction(() => !document.querySelector('.connections thead [data-col=type]'));
    await noOverflow();
    await shortTypes();
    const phone = await geometry();
    assert.ok(phone.recent.y >= phone.connections.bottom - 1, JSON.stringify(phone));
    console.log(`${scheme} 390x844 ${JSON.stringify(phone)}`);
    await page.screenshot({ path: path.join(directory, `home-${scheme}-390.png`) });
    await connectionAction(page, 'Connection 03', 'Edit');
    await page.waitForSelector('.connections .view-inspector.is-detail');
    await noBulkDelete();
    assert.equal(await page.$eval('.connections .view-inspector', el => el.getAttribute('role')), 'region');
    assert.equal(await page.$eval('.connections .view-inspector', el => el.getAttribute('aria-modal')), null, 'focused details remain part of the page');
    await noOverflow();
    await page.keyboard.press('Tab');
    assert.ok(await page.evaluate(() => !!document.activeElement.closest('.view-inspector')));
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('.connections .view-inspector'));
  }
  await page.setViewport({ width: 1440, height: 900 });
  // The unsaved starter has exactly Save and Remove. Saving opens the inspector.
  await page.locator('[aria-label="Actions for Local MySQL"]').click();
  assert.deepEqual(await page.$$eval('.connection-menu button', els => els.map(el => el.textContent)), ['Save', 'Remove']);
  await page.locator('[aria-label="Save Local MySQL"]').click();
  assert.equal(await page.$eval('.connection-panel input', el => el.value), 'Local MySQL');
  await page.click('.connections .inspector-close');
  await connectionAction(page, 'Local MySQL', 'Remove');
  assert.match(await page.$eval('dialog.confirm-sheet', el => el.textContent), /nothing on disk to delete/);
  await exact('dialog.confirm-sheet button', 'Cancel');
  assert.ok(await page.$(connectionRow('Local MySQL')));
  await connectionAction(page, 'Local MySQL', 'Remove');
  await exact('dialog.confirm-sheet button', 'Remove connection');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Actions for Local MySQL"]'));
  await focusedRow('Connection 10');
  assert.equal(deletions.length, 0, 'removing unsaved settings never calls delete');

  // Menus own arrow navigation; Duplicate opens settings without any secret.
  await page.focus('[aria-label="Actions for Connection 01"]');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === 'Use Connection 01');
  await page.keyboard.press('ArrowDown');
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Edit Connection 01');
  await page.keyboard.press('Escape');
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Actions for Connection 01');
  await connectionAction(page, 'Connection 01', 'Duplicate');
  assert.equal(await page.$eval('.connection-panel input', el => el.value), 'Connection 01 copy');
  assert.equal(await page.$eval('.connection-panel input[type=password]', el => el.value), '');
  assert.equal(await page.$('#saved-password-hint'), null, 'duplicate has no reference to the saved secret');
  await page.click('.connections .inspector-close');

  // Two checked rows, Delete while the checkbox has focus, default Cancel.
  for (const name of ['Connection 01', 'Connection 02']) await checkbox(name);
  await page.keyboard.press('Delete');
  await page.waitForSelector('dialog.confirm-sheet[open]');
  assert.equal(await page.$eval('dialog.confirm-sheet h2', el => el.textContent), 'Delete 2 connections?');
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), 'Cancel');
  assert.deepEqual(new Set(await page.$$eval('.connection-delete-list strong', els => els.map(el => el.textContent))), new Set(['Connection 01', 'Connection 02']));
  assert.equal(await page.$$eval('.connection-delete-list li', els => els.filter(el => el.textContent.includes('Saved password will also be removed.')).length), 2);
  await exact('dialog.confirm-sheet button', 'Cancel');
  assert.equal(deletions.length, 0);
  assert.ok(await page.$(connectionRow('Connection 01')));
  assert.ok(await page.$(connectionRow('Connection 02')));
  await exact('.connections-table-toolbar button', 'Delete (2)');
  await exact('dialog.confirm-sheet button', 'Delete connections');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Actions for Connection 01"]') && !document.querySelector('[aria-label="Actions for Connection 02"]'));
  await focusedRow('Connection 03');
  assert.deepEqual(new Set(deletions), new Set(seeded.slice(0, 2).map(p => p.id)));
  const absent = python(`from myflames.workspace_store import WorkspaceStore
from myflames.secret_store import get_secret
import json,sys
s=WorkspaceStore(sys.argv[1]); items=json.loads(${JSON.stringify(JSON.stringify(seeded.slice(0, 2)))})
print(json.dumps([s._profile(p['id']) is None and get_secret(p['ref'],'vault') is None for p in items]))`);
  assert.deepEqual(absent, [true, true], 'both records and both vault passwords were removed');

  // Arrows select, Enter uses; deletion of that connection explains the picker change.
  await page.focus(`${connectionRow('Connection 03')}`);
  await page.keyboard.press('ArrowDown');
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Connection 04');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.connection-row.is-active [data-col=name] strong')?.textContent === 'Connection 04');
  await page.keyboard.press('Backspace');
  await page.waitForSelector('dialog.confirm-sheet[open]');
  assert.equal(await page.$eval('dialog.confirm-sheet h2', el => el.textContent), 'Delete connection?');
  assert.match(await page.$eval('dialog.confirm-sheet', el => el.textContent), /in use.*picker will switch to No connection/s);
  await exact('dialog.confirm-sheet button', 'Delete connection');
  await page.waitForFunction(() => !document.querySelector('[aria-label="Actions for Connection 04"]'));
  await focusedRow('Connection 05');
  await exact('.main-nav button', 'Query editor');
  await openConnectionPicker(page);
  assert.equal(await page.$eval('.picker-panel select', el => el.selectedOptions[0].textContent), 'No connection');
  assert.equal(await page.$('.picker-password'), null);
  await exact('.picker-panel button', 'Manage connections…');
  await page.waitForFunction(() => document.activeElement?.id === 'connections-table');

  // New connection… in the picker: Home with the Add connection form, once.
  await exact('.main-nav button', 'Query editor');
  await openConnectionPicker(page);
  await exact('.picker-panel button', 'New connection…');
  await page.waitForFunction(() => document.querySelector('#connection-form-title')?.textContent === 'Add connection');
  // A pasted host: spaces dropped and a port after it moved to Port on leaving the field.
  // The Name field auto-fills with user@host (plus :port when port is not 3306) until edited.
  const field = label => page.evaluateHandle(text => [...document.querySelectorAll('.connection-panel label.field')]
    .find(l => l.firstChild?.textContent?.trim() === text)?.querySelector('input'), label);
  const nameField = await field('Connection name');
  // A new form has no user yet, so the name starts as the host.
  assert.equal(await nameField.evaluate(e => e.value), '127.0.0.1');
  const host = await field('Host');
  await host.click({ clickCount: 3 });
  await host.type('  10.0.0.5:3307 ');
  await page.keyboard.press('Tab');
  await page.waitForFunction(() => /Moved the port from Host/.test(document.querySelector('.connection-panel .connections-status')?.textContent || ''));
  assert.equal(await host.evaluate(e => e.value), '10.0.0.5');
  assert.equal(await (await field('Port')).evaluate(e => e.value), '3307');
  assert.equal(await nameField.evaluate(e => e.value), '10.0.0.5:3307');
  const userField = await field('User');
  await userField.click({ clickCount: 3 });
  await userField.type('app');
  assert.equal(await nameField.evaluate(e => e.value), 'app@10.0.0.5:3307');
  const port = await field('Port');
  await port.click({ clickCount: 3 });
  await port.type('3306');
  assert.equal(await nameField.evaluate(e => e.value), 'app@10.0.0.5');
  // Editing Name stops auto-fill; duplicate names (trimmed, case-insensitive) show an inline error and refuse Save.
  await nameField.click({ clickCount: 3 });
  await nameField.type('  connection 05  ');
  await userField.click({ clickCount: 3 });
  await userField.type('dba');
  assert.equal(await nameField.evaluate(e => e.value), '  connection 05  ', 'editing Name stops auto-fill');
  assert.equal(await page.$eval('#connection-name-problem', e => e.textContent.trim()), 'A saved connection named Connection 05 already exists.');
  assert.equal(await page.$eval('.connection-panel .button.primary ::-p-text(Save connection)', e => e.closest('button').disabled), true, 'Save is disabled for a duplicate name');
  // Test connection populates the Default database pick list, hiding system databases behind "Show system databases".
  testReply = {
    version: '8.4.3',
    schemas: ['information_schema', 'mysql', 'orders', 'performance_schema', 'shop', 'sys'],
    databases: ['information_schema', 'mysql', 'orders', 'performance_schema', 'shop', 'sys'],
    databases_truncated: false,
  };
  await exact('.connection-panel button', 'Test connection');
  await page.waitForSelector('.connection-panel select[aria-label="Default database"]');
  assert.deepEqual(
    await page.$$eval('.connection-panel select[aria-label="Default database"] option', els => els.map(e => e.textContent)),
    ['None', 'orders', 'shop', 'Show system databases', 'Type a name…'],
  );
  await page.select('.connection-panel select[aria-label="Default database"]', '__show_system_databases__');
  assert.deepEqual(
    await page.$$eval('.connection-panel select[aria-label="Default database"] option', els => els.map(e => e.textContent)),
    ['None', 'orders', 'shop', 'information_schema', 'mysql', 'performance_schema', 'sys', 'Hide system databases', 'Type a name…'],
  );
  await page.select('.connection-panel select[aria-label="Default database"]', 'orders');
  assert.equal(await page.$eval('.connection-panel select[aria-label="Default database"]', e => e.value), 'orders');
  await page.select('.connection-panel select[aria-label="Default database"]', '__type_database__');
  await page.waitForSelector('.connection-panel input[aria-label="Default database"]');
  // When the database list could not be read, the field stays a text field with a short hint why.
  testReply = {
    version: '8.4.3',
    schemas: [],
    databases: [],
    databases_truncated: false,
    databases_error: 'Access denied for SHOW DATABASES',
  };
  await exact('.connection-panel button', 'Test connection');
  await page.waitForSelector('#connection-schema-hint');
  assert.match(await page.$eval('#connection-schema-hint', e => e.textContent), /Could not read the database list \(Access denied for SHOW DATABASES\)/);
  assert.ok(await page.$('.connection-panel input[aria-label="Default database"]'));
  testReply = null;
  await host.click({ clickCount: 3 });
  await host.type('db 1.example');
  assert.match(await page.$eval('#connection-host-problem', e => e.textContent), /cannot contain spaces/);
  await port.click({ clickCount: 3 });
  await page.keyboard.press('Backspace');
  assert.match(await page.$eval('#connection-port-problem', e => e.textContent), /port from 1 to 65535/);
  await exact('.connection-panel button', 'Use without saving');
  assert.match(await page.$eval('.connection-panel .error', e => e.textContent), /cannot contain spaces/, 'Use refuses a host with a space');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('#connection-form-title'));
  // Editing an existing connection may keep its own name, even with different case, while another saved name is refused.
  await connectionAction(page, 'Connection 05', 'Edit');
  await page.waitForSelector('#connection-form-title');
  assert.equal(await page.$('#connection-name-problem'), null, 'editing an existing connection may keep its own name');
  const editName = await field('Connection name');
  await editName.click({ clickCount: 3 });
  await editName.type('connection 05');
  assert.equal(await page.$('#connection-name-problem'), null, 'case-insensitive match with its own name is allowed');
  await editName.click({ clickCount: 3 });
  await editName.type('connection 06');
  assert.equal(await page.$eval('#connection-name-problem', e => e.textContent.trim()), 'A saved connection named Connection 06 already exists.');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('#connection-form-title'));

  // Cmd/Ctrl and Shift selection follow the table's displayed sort order.
  await page.click(`${connectionRow('Connection 05')} [data-col=name]`);
  await page.keyboard.down('Shift');
  await page.click(`${connectionRow('Connection 07')} [data-col=name]`);
  await page.keyboard.up('Shift');
  assert.deepEqual(await page.$$eval('.connection-row[aria-selected=true] [data-col=name] strong', els => els.map(el => el.textContent)), ['Connection 05', 'Connection 06', 'Connection 07']);
  await page.keyboard.down('Meta');
  await page.click(`${connectionRow('Connection 06')} [data-col=name]`);
  await page.keyboard.up('Meta');
  assert.deepEqual(await page.$$eval('.connection-row[aria-selected=true] [data-col=name] strong', els => els.map(el => el.textContent)), ['Connection 05', 'Connection 07']);
  failDelete = seeded[6].id;
  await page.keyboard.press('Delete');
  await exact('dialog.confirm-sheet button', 'Delete connections');
  await page.waitForSelector('.connection-delete-error');
  assert.match(await page.$eval('.connection-delete-error', el => el.textContent), /Connection 07.*Synthetic locked profile/);
  assert.equal(await page.$(connectionRow('Connection 05')), null);
  assert.ok(await page.$(connectionRow('Connection 07')));
  assert.equal(deletions.filter(id => id === seeded[4].id).length, 1);
  await focusedRow('Connection 06');

  // Focus follows the displayed descending order, including form deletion,
  // the previous row when deleting the last, and Add when none remain.
  failDelete = '';
  await page.click('.connections th[data-col=name] .sort-button');
  await page.waitForFunction(() => document.querySelector('.connections th[data-col=name]')?.getAttribute('aria-sort') === 'descending');
  await connectionAction(page, 'Connection 06', 'Edit');
  await noBulkDelete();
  await exact('.connection-panel button', 'Delete connection');
  assert.equal(await page.$eval('dialog.confirm-sheet h2', el => el.textContent), 'Delete connection?');
  await exact('dialog.confirm-sheet button', 'Delete connection');
  await focusedRow('Connection 03');
  assert.equal(await page.$('.connections .view-inspector'), null);
  await connectionAction(page, 'Connection 03', 'Delete');
  await exact('dialog.confirm-sheet button', 'Delete connection');
  await focusedRow('Connection 07');
  for (const name of ['Connection 07', 'Connection 08', 'Connection 09', 'Connection 10']) await checkbox(name);
  await exact('.connections-table-toolbar button', 'Delete (4)');
  assert.equal(await page.$eval('dialog.confirm-sheet h2', el => el.textContent), 'Delete 4 connections?');
  await exact('dialog.confirm-sheet button', 'Delete connections');
  await page.waitForFunction(() => document.querySelectorAll('.connection-row').length === 0 && document.activeElement?.textContent.trim() === 'Add connection');
  assert.deepEqual(errors, []);
  console.log(`Connections checks passed. Screenshots: ${directory}`);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) { server.kill('SIGINT'); await once(server, 'exit'); }
}
