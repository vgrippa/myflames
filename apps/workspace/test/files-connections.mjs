// The Files place SSH connection list, New/Edit sheet and host-key trust
// sheet (src/SshConnections.tsx) in a real browser, without a workspace
// server: every request is intercepted and answered with fixtures, and the
// request bodies are recorded for assertions. Run: node test/files-connections.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const here = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({ stdin: { resolveDir: path.join(here, '../src'), loader: 'tsx', contents: `
  import { useCallback, useEffect, useState } from 'react';
  import { createRoot } from 'react-dom/client';
  import { SshConnectionList, SshConnectionSheet, HostKeySheet } from './SshConnections';
  import { sshConnections } from './filesApi';
  import { api } from './api';

  window.__events = [];
  window.__listDone = 0;
  const record = (...args) => { window.__events.push(args); };

  function Demo() {
    const [gen, setGen] = useState(0);
    const [connections, setConnections] = useState([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [selectedId, setSelectedId] = useState('');
    const [profiles, setProfiles] = useState([]);
    const [sheet, setSheet] = useState(null);
    const [hostKey, setHostKey] = useState(null);
    const load = useCallback(async () => {
      setLoading(true);
      setError('');
      try {
        const r = await sshConnections();
        setConnections(r.connections);
      } catch (e) {
        setError(e && e.message ? e.message : String(e));
      } finally {
        setLoading(false);
        window.__listDone += 1;
      }
    }, []);
    useEffect(() => { void load(); }, [load, gen]);
    useEffect(() => { api('profiles/list', {}).then((r) => setProfiles(r.items || []), () => {}); }, []);
    return (
      <div className="demo-frame">
        <SshConnectionList
          key={gen}
          connections={connections}
          loading={loading}
          error={error}
          selectedId={selectedId}
          onSelect={(id) => { record('select', id); setSelectedId(id); }}
          onNew={() => { record('new'); setSheet({}); }}
          onEdit={(c) => { record('edit', c.id); setSheet({ initial: c }); }}
          onRetry={() => { record('retry'); void load(); }}
        />
        <div id="demo-controls">
          <button id="reload" type="button" onClick={() => setGen((g) => g + 1)}>Reload</button>
          <button id="open-hostkey" type="button" onClick={() => { record('hostkey-open'); setHostKey({ id: 'aa11' }); }}>Host key sheet</button>
        </div>
        {sheet && (
          <SshConnectionSheet
            initial={sheet.initial}
            profiles={profiles}
            existingNames={connections.map((c) => c.name)}
            onClose={() => { record('close'); setSheet(null); }}
            onSaved={(c) => {
              record('saved', c.id, c.name);
              setConnections((all) => {
                const at = all.findIndex((x) => x.id === c.id);
                if (at < 0) return [...all, c];
                const next = [...all];
                next[at] = c;
                return next;
              });
            }}
            onDeleted={(id) => { record('deleted', id); setSheet(null); }}
          />
        )}
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
const css = (await Promise.all(['style.css', 'kit.css', 'ssh-connections.css'].map(file => readFile(path.join(here, '../src', file), 'utf8')))).join('\n') + `
  .demo-frame .ssh-connections { width: 280px; border-right: 1px solid var(--line); }
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
  const setInput = async (selector, text) => {
    await page.click(selector, { clickCount: 3 });
    if (text) await page.type(selector, text);
    else await page.keyboard.press('Backspace');
  };
  const escape = async () => { await page.keyboard.press('Escape'); };
  // Rows remount when a load resolves; wait for a fresh load to complete, so
  // a later click never lands on a node the reload is about to replace.
  const waitRows = () => page.waitForFunction(() => {
    const status = document.querySelector('.ssh-connections .view-status');
    return !!status && status.classList.contains('is-idle') && document.querySelectorAll('.ssh-connection-main').length > 0;
  });
  const reloadList = async () => {
    const done = await page.evaluate(() => window.__listDone);
    await page.click('#reload');
    await page.waitForFunction((d) => window.__listDone > d, {}, done);
    await waitRows();
  };

  // ---- List: loading, rows, selection --------------------------------------
  await page.waitForSelector('.capture-status');
  assert.match(await page.$eval('.capture-status', (el) => el.textContent || ''), /Loading SSH connections…/, 'the list announces its read');
  await waitRows();
  assert.deepEqual(await page.$$eval('.ssh-connection-target', (els) => els.map((el) => el.textContent)),
    ['lab@127.0.0.1:2222', 'ops@10.0.0.5'], 'rows show user@host:port, omitting empty parts');
  assert.deepEqual(await page.$$eval('.ssh-connection-name', (els) => els.map((el) => el.textContent)), ['lab-box', 'prod-db-1']);
  await page.click('.ssh-connection-rows li:nth-child(2) .ssh-connection-main');
  assert.ok((await events()).some((e) => e[0] === 'select' && e[1] === 'bb22'), 'selection calls onSelect');
  assert.equal(await page.$eval('.ssh-connection-rows li:nth-child(2) .ssh-connection-main', (el) => el.getAttribute('aria-current')), 'page');

  // ---- List: error with Try again -------------------------------------------
  route('/api/ssh/connections/list', { status: 400, body: { error: 'Could not read SSH connections.' } });
  await page.click('#reload');
  await page.waitForSelector('.view-status-error');
  assert.match(await page.$eval('.view-status-error', (el) => el.textContent || ''), /Could not read SSH connections\./);
  route('/api/ssh/connections/list', okList);
  await page.click('.view-status-retry');
  await waitRows();
  assert.ok((await events()).some((e) => e[0] === 'retry'), 'Try again calls onRetry');

  // ---- List: empty state -----------------------------------------------------
  route('/api/ssh/connections/list', { body: { connections: [], known_hosts_file: '/x' } });
  await page.click('#reload');
  await page.waitForSelector('.view-empty');
  assert.match(await page.$eval('.view-empty', (el) => el.textContent || ''),
    /Save an SSH connection to move files between this computer and a server\./, 'the empty state sentence');
  await page.click('.view-empty .button');
  await page.waitForSelector('#ssh-conn-sheet-title');
  assert.ok((await events()).some((e) => e[0] === 'new'), 'the empty state button is New');
  await escape();
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });
  assert.equal(await page.evaluate(() => (document.activeElement && document.activeElement.textContent) || ''), 'New SSH connection',
    'Escape returns focus to the control that opened the sheet');
  route('/api/ssh/connections/list', okList);
  await reloadList();

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
  // Key file: the pick list from /api/ssh/keys, with a typed fallback.
  await setInput('#ssh-conn-name', 'new-box');
  await page.click('.ssh-auth button[data-value="key"]');
  await page.waitForFunction(() => [...document.querySelectorAll('select#ssh-conn-key option')].some((el) => (el.textContent || '').includes('/Users/me/.ssh/id_ed25519')));
  const keyOptions = await page.$$eval('select#ssh-conn-key option', (els) => els.map((el) => el.textContent));
  assert.ok(keyOptions.some((o) => o && o.includes('/Users/me/.ssh/id_ed25519')), 'the keys endpoint feeds the pick list');
  assert.ok(keyOptions.includes('Type a path…'), 'a typed fallback is offered');
  await page.select('select#ssh-conn-key', '__type');
  await page.waitForSelector('input#ssh-conn-key');
  await page.type('input#ssh-conn-key', '/keys/other.pem');
  await page.type('#ssh-conn-secret', 'secret-pass');
  // Save: one request, the exact body, and the secret leaves the field.
  await page.click('.ssh-save-button');
  await page.waitForFunction(() => document.querySelector('.ssh-save-button').textContent.includes('Saving…'));
  await page.click('.ssh-save-button');
  await page.waitForFunction(() => window.__events.some((e) => e[0] === 'saved' && e[1] === 'cc33'));
  const saves = requestsTo('/api/ssh/connections/save');
  assert.equal(saves.length, 1, 'repeated clicks send one save');
  assert.deepEqual(saves[0].body, {
    connection: { name: 'new-box', host: '127.0.0.1', port: 2222, user: 'lab', auth: 'key', key_path: '/keys/other.pem', remote_dir: '', local_dir: '' },
    secret: 'secret-pass',
  }, 'Save sends exactly the contract body');
  assert.equal(await page.$eval('#ssh-conn-secret', (el) => el.value), '', 'the secret leaves the field once the save resolves');
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
  await page.waitForFunction(() => window.__events.some((e) => e[0] === 'saved' && e[2] === 'alias-box'));
  const passwordSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.deepEqual(passwordSave.connection, { name: 'alias-box', host: 'alias-box', port: null, user: '', auth: 'password', key_path: '', remote_dir: '', local_dir: '' },
    'an empty port is sent as null');
  assert.ok(!('secret' in passwordSave), 'an empty secret field sends no secret key');
  assert.ok(!('forget_secret' in passwordSave), 'no forget flag without Forget');
  await escape();

  // ---- Sheet (edit, has_secret): Saved securely, Forget, Replace, Test ------
  await page.click('.ssh-connection-rows li:nth-child(2) .ssh-connection-edit');
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
  await page.waitForFunction(() => window.__events.filter((e) => e[0] === 'saved' && e[1] === 'bb22').length >= 1);
  const forgetSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.equal(forgetSave.forget_secret, true, 'Forget sends forget_secret');
  assert.ok(!('secret' in forgetSave), 'Forget sends no secret');
  await escape();
  // Reload from the fixtures so the connection has a secret again.
  await reloadList();
  await page.click('.ssh-connection-rows li:nth-child(2) .ssh-connection-edit');
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
  const tests = requestsTo('/api/ssh/connections/test');
  assert.equal(tests.length, 1, 'repeated clicks send one test');
  assert.equal(tests[0].body.connection.id, 'bb22', 'a test from the edit sheet sends the draft connection');
  assert.equal(tests[0].body.secret, 'brand-new-pass', 'the typed secret goes with the draft test');
  assert.equal(await page.$eval('#ssh-conn-secret', (el) => el.value), '', 'the secret leaves the field once the test resolves');
  // After the test cleared the field, a save carries no secret and no forget.
  await page.click('.ssh-save-button');
  await page.waitForFunction(() => window.__events.filter((e) => e[0] === 'saved' && e[1] === 'bb22').length >= 2);
  const plainSave = requestsTo('/api/ssh/connections/save').pop().body;
  assert.ok(!('secret' in plainSave) && !('forget_secret' in plainSave), 'a save after the test sends no secret keys');

  // ---- Test: host_key_unknown opens HostKeySheet; trust retries the test ----
  route('/api/ssh/connections/test',
    { status: 400, body: { error: '10.0.0.5 is not a trusted host yet.', code: 'host_key_unknown' }, delay: 60 },
    { body: { ok: true, home: '/home/ops', elapsed_ms: 300 } });
  route('/api/ssh/hostkey/scan',
    { body: { host: '10.0.0.5', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:abcdef123456' }], token: 'tok-1' }, delay: 250 });
  route('/api/ssh/hostkey/trust', { body: { trusted: true, host: '10.0.0.5' }, delay: 150 });
  // A typed password must survive the prompt: the retest after trust sends it
  // again (it used to go out empty, so a password draft failed after trust).
  await page.type('#ssh-conn-secret', 'typed-before-trust');
  await page.click('.ssh-test-button');
  await page.waitForSelector('dialog.hostkey-sheet');
  assert.match(await bodyText(), /Reading the server's host key…/, 'the scan announces itself');
  await page.waitForSelector('code.hostkey-fingerprint');
  assert.equal(await page.$eval('.hostkey-sheet h2', (el) => el.textContent), 'Trust 10.0.0.5?');
  assert.match(await page.$eval('.hostkey-sheet .sheet-lead', (el) => el.textContent || ''),
    /This is the first connection to 10\.0\.0\.5 from myflames\. Compare the fingerprint with the one your server administrator gives you\./);
  assert.equal(await page.$eval('.hostkey-kind', (el) => el.textContent), 'ED25519 (256 bits)');
  assert.equal(await page.$eval('code.hostkey-fingerprint', (el) => el.textContent), 'SHA256:abcdef123456');
  assert.ok(await page.$('.hostkey-copy[aria-label="Copy the ED25519 fingerprint"]'), 'each fingerprint has a Copy button');
  assert.ok(!requestsTo('/api/ssh/hostkey/scan')[0].body.secret, 'the host-key scan never carries a secret');
  await page.click('.hostkey-trust-button');
  await page.waitForFunction(() => document.querySelector('.hostkey-trust-button').textContent.includes('Trusting…'), 'busy label while trusting');
  assert.deepEqual(requestsTo('/api/ssh/hostkey/trust')[0].body, { token: 'tok-1' }, 'trust sends the token');
  await page.waitForSelector('dialog.hostkey-sheet', { hidden: true });
  await page.waitForFunction(() => document.body.textContent.includes('Connected in 0.3 s. Home folder: /home/ops'),
    'after trust the test runs again on its own and succeeds');
  assert.equal(requestsTo('/api/ssh/connections/test').length, 3, 'the retried test is a second request from this sheet');
  const [beforeTrust, afterTrust] = requestsTo('/api/ssh/connections/test').slice(1);
  assert.equal(beforeTrust.body.secret, 'typed-before-trust');
  assert.equal(afterTrust.body.secret, 'typed-before-trust', 'the retest after trust carries the typed password');
  assert.equal(await page.$eval('#ssh-conn-secret', (el) => el.value), '', 'and the field is empty again');
  // Copy puts the fingerprint on the clipboard for comparison (checked above
  // through the button's presence; the clipboard is not readable headless).

  // ---- Test: host_key_changed shows the error, no trust button --------------
  route('/api/ssh/connections/test', { status: 400, body: { error: 'The host key of 10.0.0.5 differs from the trusted one.', code: 'host_key_changed' }, delay: 60 });
  await page.click('.ssh-test-button');
  await page.waitForFunction(() => document.body.textContent.includes('The host key of 10.0.0.5 differs from the trusted one.'), 'the error line');
  assert.equal(await page.$('dialog.hostkey-sheet'), null, 'host_key_changed opens no trust sheet');
  assert.ok(!(await bodyText()).includes('Trust and connect'), 'host_key_changed offers no trust button');

  // ---- Delete (edit): confirm sheet, {id}, onDeleted -------------------------
  await page.click('.ssh-delete-button');
  await page.waitForFunction(() => document.body.textContent.includes('Delete prod-db-1?'));
  assert.match(await bodyText(), /Its saved password or passphrase is removed from this computer\./, 'the confirm sheet says what happens');
  await page.click('.confirm-sheet .sheet-buttons .danger-solid');
  await page.waitForFunction(() => window.__events.some((e) => e[0] === 'deleted' && e[1] === 'bb22'));
  assert.deepEqual(requestsTo('/api/ssh/connections/delete')[0].body, { id: 'bb22' }, 'delete sends the id');
  await page.waitForSelector('#ssh-conn-sheet-title', { hidden: true });

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

  // ---- Screenshots: light and dark, 1440 and 720 px ---------------------------
  const shots = path.join(here, 'shots-files-connections');
  await mkdir(shots, { recursive: true });
  route('/api/ssh/connections/list', okList);
  await reloadList();
  await page.addStyleTag({ content: '#demo-controls{display:none!important}' });
  const shot = (name) => page.screenshot({ path: path.join(shots, name) });
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await shot('list-light-1440.png');
  await page.evaluate(() => document.querySelector('.ssh-connections-head .button').click());
  await page.waitForSelector('#ssh-conn-sheet-title');
  await shot('sheet-new-light-1440.png');
  await escape();
  await page.setViewport({ width: 720, height: 900 });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await shot('list-dark-720.png');
  await page.evaluate(() => document.querySelector('.ssh-connection-edit').click());
  await page.waitForSelector('#ssh-conn-sheet-title');
  await shot('sheet-edit-dark-720.png');
  await escape();
  route('/api/ssh/hostkey/scan', { body: { host: '127.0.0.1', keys: [{ type: 'ED25519', bits: 256, fingerprint: 'SHA256:abcdef123456' }], token: 'tok-shot' } });
  await page.evaluate(() => document.getElementById('open-hostkey').click());
  await page.waitForSelector('code.hostkey-fingerprint');
  await shot('hostkey-dark-720.png');
  await escape();

  assert.deepEqual(errors, [], 'no console errors');
  console.log('files-connections: ok');
} finally {
  await browser.close();
}
