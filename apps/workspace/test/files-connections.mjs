// The File transfer place (src/FilesPlace.tsx) with its SSH connection list,
// New/Edit sheet and host-key trust step (src/SshConnections.tsx) in a real
// browser, without a workspace server: every request is intercepted and
// answered with fixtures, and the request bodies are recorded for assertions.
// Run: node test/files-connections.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const here = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({ stdin: { resolveDir: path.join(here, '../src'), loader: 'tsx', contents: `
  import { useEffect, useState } from 'react';
  import { createRoot } from 'react-dom/client';
  import { FilesPlace } from './FilesPlace';
  import { HostKeySheet } from './SshConnections';
  import { api } from './api';

  window.__events = [];
  const record = (...args) => { window.__events.push(args); };

  function Demo() {
    const [gen, setGen] = useState(0);
    const [profiles, setProfiles] = useState([]);
    const [hostKey, setHostKey] = useState(null);
    useEffect(() => { api('profiles/list', {}).then((r) => setProfiles(r.items || []), () => {}); }, []);
    return (
      <div className="demo-frame">
        <FilesPlace key={gen} profiles={profiles} />
        <div id="demo-controls">
          <button id="reload" type="button" onClick={() => setGen((g) => g + 1)}>Reload</button>
          <button id="open-hostkey" type="button" onClick={() => { record('hostkey-open'); setHostKey({ id: 'aa11' }); }}>Host key sheet</button>
        </div>
        {hostKey && (
          <HostKeySheet
            target={hostKey}
            onTrusted={() => { record('trusted'); setHostKey(null); }}
            onCancel={() => { record('hostkey-cancel'); setHostKey(null); }}
          />
        )}
      </div>
    );
  }
  createRoot(document.getElementById('root')).render(<Demo />);
` }, bundle: true, platform: 'browser', format: 'iife', write: false, jsx: 'automatic', loader: { '.css': 'empty' } });
const css = (await Promise.all(['style.css', 'kit.css', 'ssh-connections.css', 'files-place.css', 'files.css', 'ops.css'].map(file => readFile(path.join(here, '../src', file), 'utf8')))).join('\n') + `
  .demo-frame { display: flex; flex-direction: column; height: 100vh; }
  .demo-frame .files-place { flex: 1; min-height: 0; height: auto; }
  #demo-controls { display: flex; gap: 8px; padding: 8px; }
`;
if (process.argv.includes('--compile-only')) { console.log('files-connections browser fixture compiled; interactions not run'); process.exit(0); }

// Fixtures. Route entries are consumed in order until one is left, which is
// then repeated; an entry may carry a delay in ms, or be a function of the
// request body.
const CONNS = [
  { id: 'aa11', name: 'lab-box', host: '127.0.0.1', port: 2222, user: 'lab', auth: 'agent', key_path: '', has_secret: false, remote_dir: '', local_dir: '', updated: '2026-11-01T10:00:00Z' },
  { id: 'bb22', name: 'prod-db-1', host: '10.0.0.5', port: null, user: 'ops', auth: 'password', key_path: '', has_secret: true, remote_dir: '/home/ops', local_dir: '', updated: '2026-11-02T10:00:00Z' },
];
const PROFILES = { items: [
  { id: 'p1', name: 'lab-db', host: '127.0.0.1', port: 3306, user: 'root', database: '', ssl_mode: 'preferred', ssh_host: '127.0.0.1', ssh_user: 'lab', ssh_port: 2222 },
  { id: 'p2', name: 'plain-db', host: 'db.internal', port: 3306, user: 'root', database: '', ssl_mode: 'preferred' },
  { id: 'p3', name: 'local-db', host: 'localhost', port: 3306, user: 'root', database: '', ssl_mode: 'preferred' },
] };
const requests = [];
const requestsTo = (p) => requests.filter((r) => r.path === p);
const routes = new Map();
const route = (p, ...entries) => routes.set(p, entries.flat());
const okList = { body: { connections: CONNS, known_hosts_file: '/x/ssh_known_hosts' } };
const echoSave = (body) => ({
  status: 200,
  delay: 120,
  body: { connection: { ...body.connection, id: body.connection.id || 'cc33', has_secret: body.forget_secret ? false : !!body.secret, updated: '2026-11-05T00:00:00Z' } },
});
route('/api/ssh/connections/list', { ...okList, delay: 120 });
route('/api/profiles/list', { body: PROFILES });
route('/api/ssh/hosts', { body: { hosts: ['prod-ssh', 'alias-box'], ssh_found: true, message: '' } });
route('/api/ssh/keys', { body: { keys: [{ path: '/Users/me/.ssh/id_ed25519', type: 'ED25519', comment: 'me@laptop' }] } });
route('/api/ssh/connections/save', echoSave);
route('/api/ssh/connections/delete', { body: { deleted: true } });
// The panes and the transfer queue of a mounted browser.
route('/api/files/local/list', { body: { path: '/', home: '/', entries: [], transfers: true } });
route('/api/files/remote/list', { body: { path: '/home/lab', home: '/home/lab', entries: [] } });
route('/api/files/jobs', { body: { jobs: [] } });

// --no-sandbox: Chrome cannot start without it in some CI containers.
const browser = await puppeteer.launch({ headless: 'shell', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  // Chrome logs each intercepted 4xx fixture as a network resource error; only
  // real console.error calls and page errors count.
  page.on('console', (m) => { if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(m.text()); });
  await page.setViewport({ width: 1000, height: 900 });
  await page.setRequestInterception(true);
  page.on('request', (request) => {
    const url = new URL(request.url());
    const answer = (status, body) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname === '/api/auth/session') { void answer(200, { token: 't' }); return; }
    if (url.pathname.startsWith('/api/')) {
      let body = {};
      try { body = JSON.parse(request.postData() || '{}'); } catch { /* recorded as {} */ }
      requests.push({ path: url.pathname, body });
      const entries = routes.get(url.pathname) || [];
      if (!entries.length) { void answer(404, { error: `No fixture for ${url.pathname}` }); return; }
      const entry = entries.length > 1 ? entries.shift() : entries[0];
      const resolved = typeof entry === 'function' ? entry(body) : entry;
      const send = () => void answer(resolved.status || 200, resolved.body ?? {});
      if (resolved.delay) setTimeout(send, resolved.delay);
      else send();
      return;
    }
    void request.respond({ status: 200, contentType: 'text/html', body: `<html><head><style>${css}</style></head><body><div id="root"></div></body></html>` });
  });
  await page.goto('http://files.test/');
  await page.addScriptTag({ content: bundle.outputFiles[0].text });

  const events = () => page.evaluate(() => window.__events);
  const bodyText = () => page.evaluate(() => document.body.textContent || '');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const setInput = async (selector, text) => {
    await page.click(selector, { clickCount: 3 });
    if (text) await page.type(selector, text);
    else await page.keyboard.press('Backspace');
  };
  const escape = async () => { await page.keyboard.press('Escape'); };
  const listReads = () => requestsTo('/api/ssh/connections/list').length;
  const remoteLists = () => requestsTo('/api/files/remote/list');
  // Rows remount when a load resolves; wait for a fresh load to complete, so
  // a later click never lands on a node the reload is about to replace.
  const waitRows = () => page.waitForFunction(() => {
    const status = document.querySelector('.ssh-connections .view-status');
    return !!status && status.classList.contains('is-idle') && document.querySelectorAll('.ssh-connection-main').length > 0;
  });
  const reloadList = async () => {
    const before = listReads();
    await page.click('#reload');
    // The remount sends a fresh list request; wait for it, then for its reply.
    await waitFor(() => listReads() > before);
    await page.waitForFunction(() => {
      const status = document.querySelector('.ssh-connections .view-status');
      return !!status && status.classList.contains('is-idle');
    });
  };
  const row = (n) => `.ssh-connection-rows li:nth-child(${n}) .ssh-connection-main`;
  const waitFor = async (fn, timeout = 15000) => {
    const start = Date.now();
    while (!fn()) {
      if (Date.now() - start > timeout) throw new Error('waitFor timed out');
      await sleep(50);
    }
  };
  // The menu item text without its icon and shortcut spans.
  const menuItems = () => page.$$eval('.context-menu [role="menuitem"]', (els) => els.map((el) => {
    const clone = el.cloneNode(true);
    clone.querySelectorAll('.context-menu-icon, .context-menu-shortcut').forEach((n) => n.remove());
    return (clone.textContent || '').trim();
  }));
  const clickMenuItem = async (label) => {
    await page.waitForFunction((l) => [...document.querySelectorAll('.context-menu [role="menuitem"]')].some((el) => {
      const clone = el.cloneNode(true);
      clone.querySelectorAll('.context-menu-icon, .context-menu-shortcut').forEach((n) => n.remove());
      return (clone.textContent || '').trim() === l;
    }), {}, label);
    await page.evaluate((l) => {
      const item = [...document.querySelectorAll('.context-menu [role="menuitem"]')].find((el) => {
        const clone = el.cloneNode(true);
        clone.querySelectorAll('.context-menu-icon, .context-menu-shortcut').forEach((n) => n.remove());
        return (clone.textContent || '').trim() === l;
      });
      item.click();
    }, label);
  };
  const clickMore = (n) => page.evaluate((sel) => document.querySelector(sel).click(), `.ssh-connection-rows li:nth-child(${n}) .ssh-connection-more`);

  // ---- List: loading, rows --------------------------------------------------
  await page.waitForSelector('.capture-status');
  assert.match(await page.$eval('.capture-status', (el) => el.textContent || ''), /Loading SSH connections…/, 'the list announces its read');
  await waitRows();
  assert.deepEqual(await page.$$eval('.ssh-connection-target', (els) => els.map((el) => el.textContent)),
    ['lab@127.0.0.1:2222', 'ops@10.0.0.5'], 'rows show user@host:port, omitting empty parts');
  assert.deepEqual(await page.$$eval('.ssh-connection-name', (els) => els.map((el) => el.textContent)), ['lab-box', 'prod-db-1']);
  // The remembered-or-first connection opens on its own and lists the server.
  await page.waitForFunction(() => document.querySelector('.ssh-connection-main[aria-current="page"]'));
  assert.equal(await page.$eval('.ssh-connection-main[aria-current="page"]', (el) => el.dataset.rowKey), 'aa11',
    'the first connection opens on the first visit');

  // ---- Selection: arrows move focus without connecting ----------------------
  const remoteBefore = remoteLists().length;
  await page.focus(row(1));
  await page.keyboard.press('ArrowDown');
  assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.dataset.rowKey), 'bb22',
    'ArrowDown moves focus to the next row');
  await page.keyboard.press('ArrowUp');
  assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.dataset.rowKey), 'aa11');
  await sleep(300);
  assert.equal(remoteLists().length, remoteBefore, 'arrow keys never open a connection');
  assert.equal(await page.$eval('.ssh-connection-main[aria-current="page"]', (el) => el.dataset.rowKey), 'aa11',
    'focus movement does not move the open connection');
  await page.focus(row(2));
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.ssh-connection-main[aria-current="page"]')?.dataset.rowKey === 'bb22');
  assert.ok(remoteLists().some((r) => r.body.ssh === 'bb22'), 'Enter opens the focused row and lists its server');

  // ---- Row menu: right-click, ⋯ button, keyboard ---------------------------
  const remoteBeforeMenu = remoteLists().length;
  await page.click(row(1), { button: 'right' });
  await page.waitForSelector('.context-menu');
  assert.deepEqual(await menuItems(), ['Open', 'Edit…', 'Duplicate', 'Test connection', 'Copy address', 'Delete…'],
    'the row menu items in order');
  assert.equal(await page.$eval('.context-menu', (el) => el.getAttribute('aria-label')), 'Actions for lab-box');
  await sleep(300);
  assert.equal(remoteLists().length, remoteBeforeMenu, 'right-click never opens the connection');
  assert.equal(await page.$eval('.ssh-connection-main[aria-current="page"]', (el) => el.dataset.rowKey), 'bb22',
    'right-click does not move the open connection');
  await escape();
  await page.waitForSelector('.context-menu', { hidden: true });
  assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.dataset.rowKey), 'aa11',
    'Escape closes the menu and returns focus to the row');

  // Shift+F10 on the focused row opens the same menu.
  await page.keyboard.down('Shift');
  await page.keyboard.press('F10');
  await page.keyboard.up('Shift');
  await page.waitForSelector('.context-menu');
  assert.deepEqual(await menuItems(), ['Open', 'Edit…', 'Duplicate', 'Test connection', 'Copy address', 'Delete…']);
  await escape();
  await page.waitForSelector('.context-menu', { hidden: true });

  // The ⋯ button opens the same menu and says so.
  await clickMore(2);
  await page.waitForSelector('.context-menu');
  assert.equal(await page.$eval('.context-menu', (el) => el.getAttribute('aria-label')), 'Actions for prod-db-1');
  assert.equal(await page.$eval('.ssh-connection-rows li:nth-child(2) .ssh-connection-more', (el) => el.getAttribute('aria-expanded')), 'true');
  await escape();
  await page.waitForSelector('.context-menu', { hidden: true });

  // ---- Copy address -----------------------------------------------------------
  await clickMore(1);
  await clickMenuItem('Copy address');
  await page.waitForFunction(() =>
    (document.querySelector('.ssh-connection-rows li:nth-child(1) .ssh-connection-status') || {}).textContent?.includes('Copied lab@127.0.0.1:2222.'));
  assert.ok(true, 'Copy address reports inline on the row');

  // ---- Test connection from the row menu -------------------------------------
  route('/api/ssh/connections/test', { body: { ok: true, home: '/home/lab', elapsed_ms: 180 }, delay: 60 });
  await clickMore(1);
  await clickMenuItem('Test connection');
  const rowStatus = '.ssh-connection-rows li:nth-child(1) .ssh-connection-status';
  await page.waitForFunction((s) => (document.querySelector(s) || {}).textContent?.includes('Testing lab-box…'), {}, rowStatus);
  await page.waitForFunction((s) => (document.querySelector(s) || {}).textContent?.includes('Connected in 0.2 s.'), {}, rowStatus);
  const rowTests = requestsTo('/api/ssh/connections/test');
  assert.deepEqual(rowTests[0].body, { id: 'aa11' }, 'a row test sends the saved id, never a secret');

  // host_key_unknown from the row menu opens the trust sheet, then retries.
  route('/api/ssh/connections/test',
    { status: 400, body: { error: '127.0.0.1 is not a trusted host yet.', code: 'host_key_unknown' }, delay: 60 },
    { body: { ok: true, home: '/home/lab', elapsed_ms: 90 } });
  route('/api/ssh/hostkey/scan', { body: { host: '127.0.0.1', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:rowkey' }], token: 'tok-row' }, delay: 60 });
  route('/api/ssh/hostkey/trust', { body: { trusted: true, host: '127.0.0.1' }, delay: 60 });
  await clickMore(1);
  await clickMenuItem('Test connection');
  await page.waitForSelector('dialog.hostkey-sheet code.hostkey-fingerprint');
  assert.equal(await page.$eval('.hostkey-sheet h2', (el) => el.textContent), 'Trust 127.0.0.1?');
  await escape();
  await page.waitForSelector('dialog.hostkey-sheet', { hidden: true });
  await page.waitForFunction((s) => !(document.querySelector(s) || {}).textContent?.includes('not a trusted host yet'), {}, rowStatus,
    );

  // ---- Duplicate ---------------------------------------------------------------
  await clickMore(1);
  await clickMenuItem('Duplicate');
  await page.waitForSelector('#ssh-conn-sheet-title');
  assert.equal(await page.$eval('#ssh-conn-sheet-title', (el) => el.textContent), 'New SSH connection');
  assert.equal(await page.$eval('#ssh-conn-name', (el) => el.value), 'lab-box copy', 'Duplicate names the copy');
  assert.equal(await page.$eval('#ssh-conn-host', (el) => el.value), '127.0.0.1', 'Duplicate fills the host');
  assert.equal(await page.$eval('#ssh-conn-port', (el) => el.value), '2222', 'Duplicate fills the port');
  await page.click('.ssh-save-button');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  const dupSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.equal(dupSave.connection.id, undefined, 'a duplicate carries no id');
  assert.ok(!('secret' in dupSave) && !('forget_secret' in dupSave), 'a duplicate carries no secret');
  assert.equal(dupSave.connection.name, 'lab-box copy');
  await waitRows();
  assert.ok((await page.$$eval('.ssh-connection-name', (els) => els.map((el) => el.textContent))).includes('lab-box copy'),
    'the copy joins the list');

  // ---- Delete… from the row menu: confirm, remove, no accidental connect ------
  // prod-db-1 is the third row now (lab-box, lab-box copy, prod-db-1).
  await page.focus(row(3));
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.ssh-connection-main[aria-current="page"]')?.dataset.rowKey === 'bb22');
  const remoteBeforeDelete = remoteLists().length;
  await clickMore(3);
  await clickMenuItem('Delete…');
  await page.waitForFunction(() => document.body.textContent.includes('Delete prod-db-1?'));
  assert.match(await bodyText(), /Its saved password or passphrase is removed from this computer\./, 'the confirm sheet says what happens');
  await page.click('.confirm-sheet .sheet-buttons .danger-solid');
  await page.waitForFunction(() => document.querySelectorAll('.ssh-connection-main').length === 2);
  assert.deepEqual(requestsTo('/api/ssh/connections/delete')[0].body, { id: 'bb22' }, 'delete sends the id');
  await sleep(300);
  assert.equal(remoteLists().length, remoteBeforeDelete, 'deleting the open connection never opens another one');
  await page.waitForSelector('.files-place-main .view-empty');
  assert.match(await page.$eval('.files-place-main .view-empty', (el) => el.textContent || ''), /Choose an SSH connection/,
    'the main area asks for a connection after the delete');
  assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.dataset.rowKey), 'cc33',
    'focus moves to the nearest surviving row');
  route('/api/ssh/connections/list', okList);
  await reloadList();
  await waitRows();

  // ---- List: error with Try again -------------------------------------------
  route('/api/ssh/connections/list', { status: 400, body: { error: 'Could not read SSH connections.' } });
  await page.click('#reload');
  await page.waitForSelector('.view-status-error');
  assert.match(await page.$eval('.view-status-error', (el) => el.textContent || ''), /Could not read SSH connections\./);
  route('/api/ssh/connections/list', okList);
  const listBeforeRetry = listReads();
  await page.click('.ssh-connections .view-status-retry');
  await waitRows();
  assert.ok(listReads() > listBeforeRetry, 'Try again reads the list again');

  // ---- Empty state: one New action, in the main area ---------------------------
  route('/api/ssh/connections/list', { body: { connections: [], known_hosts_file: '/x' } });
  await page.click('#reload');
  await page.waitForSelector('.files-place-main .view-empty');
  assert.match(await page.$eval('.files-place-main .view-empty', (el) => el.textContent || ''),
    /Save an SSH connection to move files between this computer and a server\./, 'the main area explains the place in one sentence');
  assert.match(await page.$eval('.files-place-main .view-empty h3', (el) => el.textContent || ''), /No SSH connections/);
  assert.equal(await page.$('.ssh-connections-head .button'), null, 'no duplicate New in the list header while empty');
  assert.match(await page.$eval('.ssh-connections-empty', (el) => el.textContent || ''), /None saved yet\./);
  assert.equal(await page.$eval('.files-place-main', (el) => el.getAttribute('aria-label')), 'File transfer');
  await page.click('.files-place-main .view-empty .button');
  await page.waitForSelector('#ssh-conn-sheet-title');
  assert.equal(await page.$eval('#ssh-conn-sheet-title', (el) => el.textContent), 'New SSH connection');
  await escape();
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  assert.equal(await page.evaluate(() => (document.activeElement && document.activeElement.textContent) || ''), 'New SSH connection',
    'Escape returns focus to the control that opened the sheet');
  route('/api/ssh/connections/list', okList);
  await reloadList();
  await waitRows();

  // ---- Sheet (new): Start from, validation, auth switch, exact save body ----
  await page.click('.ssh-connections-head .button');
  await page.waitForSelector('#ssh-conn-sheet-title');
  assert.equal(await page.$eval('#ssh-conn-sheet-title', (el) => el.textContent), 'New SSH connection');
  // The default authentication (SSH agent or keys) shows no secret fields.
  assert.equal(await page.$('#ssh-conn-secret'), null, 'agent auth has no secret input');
  assert.equal(await page.$('#ssh-conn-key'), null, 'agent auth has no key field');
  // Start from offers database connections (their SSH tunnel host, or else
  // their own host) and config aliases; this computer is left out.
  await page.waitForFunction(() => [...document.querySelectorAll('#ssh-conn-start option')].some((el) => el.textContent === 'prod-ssh'),
    {}, );
  const startOptions = await page.$$eval('#ssh-conn-start option', (els) => els.map((el) => el.textContent));
  assert.ok(startOptions.includes('lab-db · lab@127.0.0.1 (SSH tunnel)'), 'a database connection with ssh_host is offered');
  assert.ok(startOptions.includes('prod-ssh'), 'a ~/.ssh/config alias is offered');
  assert.ok(startOptions.includes('plain-db · db.internal'), 'a direct database host is offered');
  assert.ok(!startOptions.some((o) => o && o.startsWith('local-db')), 'a database on this computer is not offered');
  await page.select('#ssh-conn-start', 'profile:p1');
  assert.equal(await page.$eval('#ssh-conn-host', (el) => el.value), '127.0.0.1', 'Start from fills Host');
  assert.equal(await page.$eval('#ssh-conn-port', (el) => el.value), '2222', 'Start from fills Port');
  assert.equal(await page.$eval('#ssh-conn-user', (el) => el.value), 'lab', 'Start from fills User');
  assert.equal(await page.$eval('#ssh-conn-name', (el) => el.value), 'lab-db', 'Start from fills an empty Name');
  // A direct database host fills Host only (SSH user and port are its own).
  await page.select('#ssh-conn-start', 'profile:p2');
  assert.equal(await page.$eval('#ssh-conn-host', (el) => el.value), 'db.internal');
  assert.equal(await page.$eval('#ssh-conn-port', (el) => el.value), '');
  assert.equal(await page.$eval('#ssh-conn-user', (el) => el.value), '');
  // Pasting user@host:port/path splits it; Name follows user@host until typed.
  await page.select('#ssh-conn-start', 'profile:p1');
  await setInput('#ssh-conn-name', '');
  await page.$eval('#ssh-conn-name', (el) => el.blur());
  await setInput('#ssh-conn-host', 'ops@db7.example:2200/var/log/mysql');
  await page.$eval('#ssh-conn-host', (el) => el.blur());
  assert.equal(await page.$eval('#ssh-conn-host', (el) => el.value), 'db7.example');
  assert.equal(await page.$eval('#ssh-conn-user', (el) => el.value), 'ops');
  assert.equal(await page.$eval('#ssh-conn-port', (el) => el.value), '2200');
  assert.equal(await page.$eval('#ssh-conn-remote', (el) => el.value), '/var/log/mysql');
  await setInput('#ssh-conn-remote', '');
  await page.select('#ssh-conn-start', 'profile:p1');
  await setInput('#ssh-conn-name', 'lab-db');
  // Name is required and unique (case-insensitive); nothing is sent meanwhile.
  await setInput('#ssh-conn-name', '');
  const savesBefore = requestsTo('/api/ssh/connections/save').length;
  await page.click('.ssh-save-button');
  await page.waitForSelector('.form-row.has-error .form-error');
  assert.match(await page.$eval('.form-row.has-error .form-error', (el) => el.textContent || ''), /Enter a name\./);
  await setInput('#ssh-conn-name', 'LAB-BOX');
  await page.click('.ssh-save-button');
  await page.waitForFunction(() => document.body.textContent.includes('already exists'));
  assert.match(await page.$eval('#ssh-conn-name', (el) => el.closest('.form-row').textContent || ''), /already exists\./,
    'a case-insensitive duplicate name is refused');
  assert.equal(requestsTo('/api/ssh/connections/save').length, savesBefore, 'invalid saves send nothing');
  // Key file: the pick list from /api/ssh/keys leads with the file name.
  await setInput('#ssh-conn-name', 'new-box');
  await page.click('.ssh-auth button[data-value="key"]');
  await page.waitForFunction(() => [...document.querySelectorAll('select#ssh-conn-key option')].some((el) => (el.textContent || '').includes('id_ed25519')));
  const keyOptions = await page.$$eval('select#ssh-conn-key option', (els) => els.map((el) => el.textContent));
  const keyOption = keyOptions.find((o) => o && o.includes('id_ed25519'));
  assert.equal(keyOption, 'id_ed25519 (ed25519) · me@laptop · /Users/me/.ssh',
    'a key option leads with the file name; the folder follows as context');
  assert.ok(keyOptions.includes('Other key file…'), 'a typed fallback is offered');
  assert.ok(!keyOptions.some((o) => o && o.startsWith('/Users')), 'no option is cut at the start by a long folder');
  await page.select('select#ssh-conn-key', '/Users/me/.ssh/id_ed25519');
  assert.equal(await page.$eval('select#ssh-conn-key', (el) => el.title), '/Users/me/.ssh/id_ed25519',
    'the select shows the full path on hover');
  await page.select('select#ssh-conn-key', '__type');
  await page.waitForSelector('input#ssh-conn-key');
  await setInput('input#ssh-conn-key', '/keys/other.pem');
  await page.type('#ssh-conn-secret', 'secret-pass');
  // Save: one request, the exact body, and the secret leaves the field.
  await page.click('.ssh-save-button');
  await page.waitForFunction(() => document.querySelector('.ssh-save-button').textContent.includes('Saving…'));
  await page.click('.ssh-save-button');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  const saves = requestsTo('/api/ssh/connections/save');
  assert.equal(saves.length, savesBefore + 1, 'repeated clicks send one save');
  assert.deepEqual(saves[saves.length - 1].body, {
    connection: { name: 'new-box', host: '127.0.0.1', port: 2222, user: 'lab', auth: 'key', key_path: '/keys/other.pem', remote_dir: '', local_dir: '' },
    secret: 'secret-pass',
  }, 'Save sends exactly the contract body');
  await waitRows();
  assert.ok((await page.$$eval('.ssh-connection-name', (els) => els.map((el) => el.textContent))).includes('new-box'),
    'the saved connection joins the list');

  // Escape returns focus to the New button.
  await page.click('.ssh-connections-head .button');
  await page.waitForSelector('#ssh-conn-sheet-title');
  await escape();
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  assert.equal(await page.evaluate(() => (document.activeElement && document.activeElement.textContent) || ''), 'New',
    'Escape returns focus to the New button');

  // Sheet (new), password auth, empty port and empty secret.
  await page.click('.ssh-connections-head .button');
  await page.waitForSelector('#ssh-conn-sheet-title');
  await page.waitForFunction(() => [...document.querySelectorAll('#ssh-conn-start option')].some((el) => el.textContent === 'alias-box'));
  await page.select('#ssh-conn-start', 'host:alias-box');
  assert.equal(await page.$eval('#ssh-conn-host', (el) => el.value), 'alias-box', 'a config alias fills Host');
  assert.equal(await page.$eval('#ssh-conn-name', (el) => el.value), 'alias-box', 'a config alias fills an empty Name');
  await page.click('.ssh-auth button[data-value="password"]');
  await page.waitForSelector('#ssh-conn-secret');
  assert.equal(await page.$('#ssh-conn-key'), null, 'password auth has no key field');
  await page.click('.ssh-save-button');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  const passwordSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.deepEqual(passwordSave.connection, { name: 'alias-box', host: 'alias-box', port: null, user: '', auth: 'password', key_path: '', remote_dir: '', local_dir: '' },
    'an empty port is sent as null');
  assert.ok(!('secret' in passwordSave), 'an empty secret field sends no secret key');
  assert.ok(!('forget_secret' in passwordSave), 'no forget flag without Forget');

  // ---- Sheet (edit, has_secret): Saved securely, Forget, Replace, Test ------
  // prod-db-1 is the third row now (alias-box, lab-box, prod-db-1).
  await clickMore(3);
  await clickMenuItem('Edit…');
  await page.waitForSelector('#ssh-conn-sheet-title');
  assert.equal(await page.$eval('#ssh-conn-sheet-title', (el) => el.textContent), 'Edit prod-db-1');
  assert.equal(await page.$eval('.ssh-auth button[data-value="password"]', (el) => el.getAttribute('aria-pressed')), 'true',
    'the saved authentication is selected');
  assert.match(await page.$eval('.ssh-secret-saved', (el) => el.textContent || ''), /Saved securely/);
  assert.equal(await page.$('#ssh-conn-secret'), null, 'a saved secret shows no input until Replace');
  // Forget stages forget_secret on the next save.
  const secretRow = (await page.$$('.ssh-secret-saved .text-button'));
  await secretRow[1].click();
  await page.waitForFunction(() => document.body.textContent.includes('is removed when you save'));
  await page.click('.ssh-save-button');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  const forgetSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.equal(forgetSave.forget_secret, true, 'Forget sends forget_secret');
  assert.ok(!('secret' in forgetSave), 'Forget sends no secret');
  // Reload from the fixtures so the connection has a secret again.
  await reloadList();
  await waitRows();
  await clickMore(2);
  await clickMenuItem('Edit…');
  await page.waitForSelector('#ssh-conn-sheet-title');
  // Replace reveals the input; a draft test sends {connection, secret}.
  await page.waitForSelector('.ssh-secret-saved .text-button');
  await page.click('.ssh-secret-saved .text-button');
  await page.waitForSelector('#ssh-conn-secret');
  await page.type('#ssh-conn-secret', 'brand-new-pass');
  route('/api/ssh/connections/test', { body: { ok: true, home: '/home/ops', elapsed_ms: 420 }, delay: 200 });
  await page.click('.ssh-test-button');
  await page.waitForFunction(() => document.querySelector('.ssh-test-button').textContent.includes('Testing…'), 'busy label while testing');
  await page.click('.ssh-test-button');
  await page.waitForFunction(() => document.body.textContent.includes('Connected in 0.4 s. Home folder: /home/ops'), 'the success line');
  const tests = requestsTo('/api/ssh/connections/test').filter((r) => r.body.connection);
  assert.equal(tests.length, 1, 'repeated clicks send one test');
  assert.equal(tests[0].body.connection.id, 'bb22', 'a test from the edit sheet sends the draft connection');
  assert.equal(tests[0].body.secret, 'brand-new-pass', 'the typed secret goes with the draft test');
  assert.equal(await page.$eval('#ssh-conn-secret', (el) => el.value), '', 'the secret leaves the field once the test resolves');
  // After the test cleared the field, a save carries no secret and no forget.
  await page.click('.ssh-save-button');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  const plainSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.ok(!('secret' in plainSave) && !('forget_secret' in plainSave), 'a save after the test sends no secret keys');

  // ---- Test: host_key_unknown turns the sheet into the trust step ------------
  await reloadList();
  await waitRows();
  await clickMore(2);
  await clickMenuItem('Edit…');
  await page.waitForSelector('#ssh-conn-sheet-title');
  await page.waitForSelector('.ssh-secret-saved .text-button');
  await page.click('.ssh-secret-saved .text-button');
  await page.waitForSelector('#ssh-conn-secret');
  // Three failing tests (open, Escape-as-Back retry, pre-trust retry) consume
  // the 400 entries one by one; the retest after trust gets the success.
  route('/api/ssh/connections/test',
    { status: 400, body: { error: '10.0.0.5 is not a trusted host yet.', code: 'host_key_unknown' }, delay: 60 },
    { status: 400, body: { error: '10.0.0.5 is not a trusted host yet.', code: 'host_key_unknown' }, delay: 60 },
    { status: 400, body: { error: '10.0.0.5 is not a trusted host yet.', code: 'host_key_unknown' }, delay: 60 },
    { body: { ok: true, home: '/home/ops', elapsed_ms: 300 } });
  route('/api/ssh/hostkey/scan',
    { body: { host: '10.0.0.5', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:abcdef123456' }], token: 'tok-1' }, delay: 250 });
  route('/api/ssh/hostkey/trust', { body: { trusted: true, host: '10.0.0.5' }, delay: 150 });
  // A typed password must survive the prompt: the retest after trust sends it
  // again (it used to go out empty, so a password draft failed after trust).
  await page.type('#ssh-conn-secret', 'typed-before-trust');
  await page.click('.ssh-test-button');
  // One dialog at a time: the sheet itself becomes the trust step.
  await page.waitForFunction(() => document.querySelector('#ssh-conn-sheet-title')?.textContent === 'Trust 10.0.0.5?');
  assert.equal(await page.$$('dialog[open]').then((d) => d.length), 1, 'the trust step never stacks a second modal');
  assert.equal(await page.$('dialog.hostkey-sheet'), null, 'no stacked host-key sheet over the connection sheet');
  assert.equal(await page.$('.ssh-sheet .ssh-test-button'), null, 'the form actions are replaced while trusting');
  assert.match(await bodyText(), /Reading the server's host key…|This is the first connection/, 'the scan announces itself');
  await page.waitForSelector('code.hostkey-fingerprint');
  assert.equal(await page.$eval('.ssh-sheet h2', (el) => el.textContent), 'Trust 10.0.0.5?');
  assert.match(await page.$eval('.ssh-sheet .sheet-lead', (el) => el.textContent || ''),
    /This is the first connection to 10\.0\.0\.5 from myflames\. Compare the fingerprint with the one your server administrator gives you\./);
  assert.equal(await page.$eval('.hostkey-kind', (el) => el.textContent), 'ED25519 (256 bits)');
  assert.equal(await page.$eval('code.hostkey-fingerprint', (el) => el.textContent), 'SHA256:abcdef123456');
  assert.ok(await page.$('.hostkey-copy[aria-label="Copy the ED25519 fingerprint"]'), 'each fingerprint has a Copy button');
  assert.ok(!requestsTo('/api/ssh/hostkey/scan')[0].body.secret, 'the host-key scan never carries a secret');

  // Back returns to the form with its typed values, focus on Test connection.
  await page.click('.ssh-hostkey-back');
  await page.waitForFunction(() => document.querySelector('#ssh-conn-sheet-title')?.textContent === 'Edit prod-db-1');
  assert.equal(await page.$eval('#ssh-conn-name', (el) => el.value), 'prod-db-1', 'Back keeps the form');
  // Focus moves on the next frame (requestAnimationFrame in backFromTrust).
  await page.waitForFunction(() => document.activeElement && document.activeElement.className.includes('ssh-test-button'), { timeout: 2000 })
    .catch(() => assert.fail('Back returns focus to Test connection'));

  // Escape during the trust step acts as Back, not as closing the sheet.
  await page.type('#ssh-conn-secret', 'typed-before-trust');
  await page.click('.ssh-test-button');
  await page.waitForSelector('code.hostkey-fingerprint');
  await escape();
  await page.waitForFunction(() => document.querySelector('#ssh-conn-sheet-title')?.textContent === 'Edit prod-db-1');
  assert.ok(await page.$('dialog.ssh-sheet'), 'Escape in the trust step never closes the sheet');
  // Back moves focus to Test connection on the next frame; type only after it,
  // or the later keystrokes land on the button.
  await page.waitForFunction(() => document.activeElement && document.activeElement.classList.contains('ssh-test-button'));

  // Trust and connect runs the test again on its own, with the same secret.
  await page.type('#ssh-conn-secret', 'typed-before-trust');
  await page.click('.ssh-test-button');
  await page.waitForSelector('code.hostkey-fingerprint');
  await page.click('.hostkey-trust-button');
  await page.waitForFunction(() => document.querySelector('.hostkey-trust-button').textContent.includes('Trusting…'), 'busy label while trusting');
  assert.deepEqual(requestsTo('/api/ssh/hostkey/trust')[0].body, { token: 'tok-1' }, 'trust sends the token');
  await page.waitForFunction(() => document.querySelector('#ssh-conn-sheet-title')?.textContent === 'Edit prod-db-1',
    'after trust the sheet returns to the form');
  await page.waitForFunction(() => document.body.textContent.includes('Connected in 0.3 s. Home folder: /home/ops'),
    'after trust the test runs again on its own and succeeds');
  const allTests = requestsTo('/api/ssh/connections/test');
  const beforeTrustReq = allTests[allTests.length - 2];
  const afterTrustReq = allTests[allTests.length - 1];
  assert.equal(beforeTrustReq.body.secret, 'typed-before-trust');
  assert.equal(afterTrustReq.body.secret, 'typed-before-trust', 'the retest after trust carries the typed password');
  assert.equal(await page.$eval('#ssh-conn-secret', (el) => el.value), '', 'and the field is empty again');

  // ---- A late scan of an earlier host never answers for a newer one -----------
  // Host A's scan is slow; Back, then a test of host B: B's keys and token are
  // the ones shown and trusted, also after A's answer arrives.
  route('/api/ssh/connections/test',
    { status: 400, body: { error: 'Not a trusted host yet.', code: 'host_key_unknown' }, delay: 30 },
    { status: 400, body: { error: 'Not a trusted host yet.', code: 'host_key_unknown' }, delay: 30 },
    { body: { ok: true, home: '/home/ops', elapsed_ms: 300 } });
  route('/api/ssh/hostkey/scan',
    { body: { host: '10.0.0.5', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:late-a' }], token: 'tok-a' }, delay: 1200 },
    { body: { host: 'host-b', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:fresh-b' }], token: 'tok-b' }, delay: 50 });
  await page.click('.ssh-test-button');
  await page.waitForFunction(() => /^Trust /.test(document.querySelector('#ssh-conn-sheet-title')?.textContent || ''));
  await page.click('.ssh-hostkey-back');
  await page.waitForFunction(() => document.activeElement && document.activeElement.className.includes('ssh-test-button'));
  await setInput('#ssh-conn-host', 'host-b');
  await page.click('.ssh-test-button');
  await page.waitForSelector('code.hostkey-fingerprint');
  assert.equal(await page.$eval('code.hostkey-fingerprint', (el) => el.textContent), 'SHA256:fresh-b');
  await sleep(1400);
  assert.deepEqual(await page.$$eval('code.hostkey-fingerprint', (els) => els.map((el) => el.textContent)), ['SHA256:fresh-b'],
    "host A's late scan never replaces host B's keys");
  const trustsBefore = requestsTo('/api/ssh/hostkey/trust').length;
  await page.click('.hostkey-trust-button');
  await waitFor(() => requestsTo('/api/ssh/hostkey/trust').length > trustsBefore);
  assert.deepEqual(requestsTo('/api/ssh/hostkey/trust').pop().body, { token: 'tok-b' }, 'Trust sends the token of the host on screen');
  await page.waitForFunction(() => document.querySelector('#ssh-conn-sheet-title')?.textContent === 'Edit prod-db-1');
  await setInput('#ssh-conn-host', '10.0.0.5');

  // ---- Test: host_key_changed shows the error, no trust step ------------------
  route('/api/ssh/connections/test', { status: 400, body: { error: 'The host key of 10.0.0.5 differs from the trusted one.', code: 'host_key_changed' }, delay: 60 });
  await page.click('.ssh-test-button');
  await page.waitForFunction(() => document.body.textContent.includes('The host key of 10.0.0.5 differs from the trusted one.'), 'the error line');
  assert.equal(await page.$eval('#ssh-conn-sheet-title', (el) => el.textContent), 'Edit prod-db-1', 'host_key_changed opens no trust step');
  assert.ok(!(await bodyText()).includes('Trust and connect'), 'host_key_changed offers no trust button');

  // ---- Delete (edit): confirm sheet, {id}, the sheet closes --------------------
  await page.click('.ssh-delete-button');
  await page.waitForFunction(() => document.body.textContent.includes('Delete prod-db-1?'));
  assert.match(await bodyText(), /Its saved password or passphrase is removed from this computer\./, 'the confirm sheet says what happens');
  await page.click('.confirm-sheet .sheet-buttons .danger-solid');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  assert.deepEqual(requestsTo('/api/ssh/connections/delete').pop().body, { id: 'bb22' }, 'delete sends the id');

  // ---- HostKeySheet alone: scan error, Try again, Cancel, focus --------------
  route('/api/ssh/hostkey/scan',
    { status: 400, body: { error: 'Could not read the host key.' }, delay: 60 },
    { body: { host: 'alias-box', keys: [
      { type: 'ED25519', bits: 256, fingerprint: 'SHA256:one' },
      { type: 'RSA', bits: 3072, fingerprint: 'SHA256:two' },
    ], token: 'tok-2' } });
  await page.focus('#open-hostkey');
  await page.click('#open-hostkey');
  await page.waitForSelector('dialog.hostkey-sheet');
  await page.waitForFunction(() => document.body.textContent.includes('Could not read the host key.'), 'the scan error shows in the sheet');
  await page.click('.hostkey-retry');
  await page.waitForFunction(() => document.querySelectorAll('code.hostkey-fingerprint').length === 2, 'Try again reads the keys');
  assert.equal(await page.$eval('.hostkey-sheet h2', (el) => el.textContent), 'Trust alias-box?');
  await escape();
  await page.waitForSelector('dialog.hostkey-sheet', { hidden: true });
  assert.ok((await events()).some((e) => e[0] === 'hostkey-cancel'), 'Cancel calls onCancel');
  assert.equal(await page.evaluate(() => (document.activeElement && document.activeElement.id) || ''), 'open-hostkey',
    'Escape returns focus to the control that opened the host-key sheet');

  // ---- Deleting the connection the fallback opened opens no other ----------
  // No remembered selection: the first connection opens on its own; deleting
  // it from its menu must not open the next one.
  await page.evaluate(() => localStorage.removeItem('myflames.files.selected'));
  route('/api/ssh/connections/list', okList);
  await reloadList();
  await page.waitForFunction(() => document.querySelector('.ssh-connection-main[aria-current="page"]')?.dataset.rowKey === 'aa11');
  await waitFor(() => remoteLists().length > 0 && true);
  const remoteBeforeFallbackDelete = remoteLists().length;
  await clickMore(1);
  await clickMenuItem('Delete…');
  await page.waitForFunction(() => document.body.textContent.includes('Delete lab-box?'));
  route('/api/ssh/connections/list', { body: { connections: CONNS.filter((c) => c.id !== 'aa11'), known_hosts_file: '/x' } });
  await page.click('.confirm-sheet .sheet-buttons .danger-solid');
  await page.waitForFunction(() => document.querySelectorAll('.ssh-connection-main').length === 1);
  await sleep(300);
  assert.equal(remoteLists().length, remoteBeforeFallbackDelete, 'deleting the fallback-opened connection opens no other');
  await page.waitForSelector('.files-place-main .view-empty');
  assert.match(await page.$eval('.files-place-main .view-empty', (el) => el.textContent || ''), /Choose an SSH connection/);
  route('/api/ssh/connections/list', okList);
  await reloadList();

  // ---- Screenshots: light and dark, 1440 and 720 px ---------------------------
  const shots = path.join(here, 'shots-files-connections');
  await mkdir(shots, { recursive: true });
  route('/api/ssh/connections/list', okList);
  await reloadList();
  await waitRows();
  await page.addStyleTag({ content: '#demo-controls{display:none!important}' });
  const shot = (name) => page.screenshot({ path: path.join(shots, name) });
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await shot('list-light-1440.png');
  await clickMore(1);
  await page.waitForSelector('.context-menu');
  await shot('row-menu-light-1440.png');
  await escape();
  await page.evaluate(() => document.querySelector('.ssh-connections-head .button').click());
  await page.waitForSelector('#ssh-conn-sheet-title');
  await shot('sheet-new-light-1440.png');
  await escape();
  await page.setViewport({ width: 720, height: 900 });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await shot('list-dark-720.png');
  await clickMore(1);
  await clickMenuItem('Edit…');
  await page.waitForSelector('#ssh-conn-sheet-title');
  await shot('sheet-edit-dark-720.png');
  await escape();
  route('/api/ssh/hostkey/scan', { body: { host: '127.0.0.1', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:abcdef123456' }], token: 'tok-shot' } });
  await page.evaluate(() => document.getElementById('open-hostkey').click());
  await page.waitForSelector('code.hostkey-fingerprint');
  await shot('hostkey-dark-720.png');
  await escape();
  // The first-run empty state lives in the main area.
  route('/api/ssh/connections/list', { body: { connections: [], known_hosts_file: '/x' } });
  await page.evaluate(() => document.getElementById('reload').click());
  await page.waitForSelector('.files-place-main .view-empty');
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await shot('empty-light-1440.png');
  await page.setViewport({ width: 720, height: 520 });
  await shot('empty-light-720.png');

  assert.deepEqual(errors, [], 'no console errors');
  console.log('files-connections: ok');
} finally {
  await browser.close();
}
