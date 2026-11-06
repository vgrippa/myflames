// The Files browser (FileBrowser.tsx, FilePane.tsx, Transfers.tsx) in a real
// browser against an in-memory backend kept here: a remote and a local tree
// (listings, New folder, Rename, Delete change what the next listing shows),
// uploads and downloads as scripted jobs that land their files when they
// finish. Every request is answered and recorded, and the bodies are
// asserted. Screenshots go to test/shots-files-browser/.
// Run: node test/files-browser.mjs
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.join(here, '../src');
const shots = path.join(here, 'shots-files-browser');
const ORIGIN = 'http://localhost:5199';
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';
const MiB = 2 ** 20;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const bundle = await build({
  stdin: {
    resolveDir: src,
    loader: 'tsx',
    contents: `
      import React, { useState } from 'react';
      import { createRoot } from 'react-dom/client';
      import { FileBrowser } from './FileBrowser';
      const w = window;
      w.__hostKeyCalls = 0;
      w.__edits = 0;
      function App() {
        const [connection, setConnection] = useState(w.__connection);
        w.__setConnection = setConnection;
        return <div className="harness">
          <aside className="harness-sidebar" />
          <div className="harness-main">
            <div className="harness-topbar" />
            <div className="files-place">
              <nav className="harness-list" aria-label="SSH connections">SSH connections</nav>
              <section className="files-place-main" aria-label="Files">
                <FileBrowser connection={connection}
                  onHostKeyUnknown={(retry) => { w.__hostKeyCalls += 1; w.__retry = retry; }}
                  onEditConnection={() => { w.__edits += 1; }} />
              </section>
            </div>
          </div>
        </div>;
      }
      createRoot(document.getElementById('root')).render(<App />);
    `,
  },
  bundle: true,
  platform: 'browser',
  format: 'iife',
  write: false,
  jsx: 'automatic',
  loader: { '.css': 'empty' },
});
// The app's cascade order (styles.ts): kit first, style.css last.
const css = (await Promise.all(['kit.css', 'files-place.css', 'files.css', 'style.css']
  .map((file) => readFile(path.join(src, file), 'utf8')))).join('\n');
if (process.argv.includes('--compile-only')) {
  console.log('files-browser fixture compiled; interactions not run');
  process.exit(0);
}
const harnessCss = `
  html, body { margin: 0; height: 100%; }
  .harness { display: grid; grid-template-columns: var(--sidebar-w) minmax(0, 1fr); height: 100dvh; }
  .harness-sidebar { background: var(--surface-muted); border-right: 1px solid var(--line); }
  .harness-main { min-width: 0; overflow: auto; }
  .harness-topbar { box-sizing: border-box; height: var(--topbar-height); border-bottom: 1px solid var(--line); }
  .harness-list { padding: var(--space-3) var(--space-4); color: var(--ink-muted); font-size: var(--text-sm); font-weight: 600; }
`;
const html = `<!doctype html><html><head><meta charset="utf-8"><style>${css}\n${harnessCss}</style></head><body><div id="root"></div></body></html>`;

// ---- The fake backend --------------------------------------------------------------
const T0 = 1762300000;
const dir = (children = {}, mtime = T0) => ({ kind: 'dir', mtime, mode: 0o040755, children: new Map(Object.entries(children)) });
const file = (size, mtime = T0) => ({ kind: 'file', size, mtime, mode: 0o100644 });
const link = (target, isDir) => ({ kind: 'link', target, isDir, size: target.length, mtime: T0, mode: 0o120777 });
const join = (d, name) => (d.endsWith('/') ? d + name : `${d}/${name}`);
const base = (p) => p.split('/').filter(Boolean).pop() || p;
const count = (n) => 1 + (n.kind === 'dir' ? [...n.children.values()].reduce((s, c) => s + count(c), 0) : 0);
const clone = (n) => (n.kind === 'dir' ? { ...n, children: new Map([...n.children].map(([k, v]) => [k, clone(v)])) } : { ...n });

class Tree {
  constructor(root, home) { this.root = root; this.home = home; }
  parts(p) { return p.split('/').filter(Boolean); }
  norm(p) { return `/${this.parts(p).join('/')}`; }
  get(p) {
    let node = this.root;
    let parent = null;
    for (const part of this.parts(p)) {
      if (!node || node.kind !== 'dir') return null;
      parent = node;
      node = node.children.get(part);
      if (node && node.kind === 'link' && node.isDir) node = parent.children.get(node.target);
    }
    return node || null;
  }
  parent(p) {
    const parts = this.parts(p);
    return { dir: this.get(`/${parts.slice(0, -1).join('/')}`), name: parts[parts.length - 1] };
  }
  put(p, node) { this.parent(p).dir.children.set(this.parent(p).name, node); }
  drop(p) { const { dir: d, name } = this.parent(p); d.children.delete(name); }
}
const entry = (name, n) => ({
  name, kind: n.kind, is_dir: n.kind === 'dir' || (n.kind === 'link' && n.isDir),
  size: n.kind === 'dir' ? 4096 : n.size, mtime: n.mtime, mode: n.mode,
});

const bigFolder = {};
for (let i = 0; i < 5000; i++) bigFolder[`f${String(i).padStart(4, '0')}.dat`] = file(1000 + i, T0 - i * 60);
const remoteTree = new Tree(dir({
  home: dir({
    lab: dir({
      data: dir({
        logs: dir({ 'a.log': file(120), 'b.log': file(240) }),
        archive: dir({ 'x1.tar': file(MiB), 'x2.tar': file(2 * MiB), 'x3.tar': file(3 * MiB) }),
        'link-to-logs': link('logs', true),
        'a.txt': file(10, T0 - 3600),
        'big.bin': file(64 * MiB, T0 - 86400),
        'dump.sql': file(Math.round(1.2 * 2 ** 30), T0 - 7200),
        'readme.md': file(2048, T0 - 600),
        '.hidden': file(5),
        '.config': dir({}),
      }),
      big: dir(bigFolder),
      notes: dir({}),
      secret: dir({ 'key.txt': file(12) }),
      slow: dir({ 'late.txt': file(1) }),
    }),
  }),
  srv: dir({}),
}), '/home/lab');
const localTree = new Tree(dir({
  Users: dir({
    me: dir({
      Downloads: dir({ 'old.zip': file(5 * MiB) }),
      logs: dir({ 'c.log': file(10) }),
      'notes.txt': file(2048),
      'report.pdf': file(300 * 1024),
      'big.bin': file(MiB, T0 - 400000),
      'todo.md': file(512),
      'plan.txt': file(640),
      '.zshrc': file(100),
    }),
  }),
  Volumes: dir({ Backup: dir({ 'dump-2026.sql': file(9 * MiB) }) }),
}), '/Users/me');

const NAMES = { c1: 'lab-box', c2: 'prod-db-1', c3: 'new-box' };
const backend = {
  failures: new Map(), // "remote:<path>" → {error, code}
  delays: new Map(), // "remote:<path>" → ms
  jobs: [], // { view, steps, auto, land }
  seq: 0,
  job(over) {
    return {
      id: `j${++this.seq}`, ssh: 'c1', ssh_name: 'lab-box', direction: 'upload', state: 'queued', phase: null,
      on_exists: 'fail', target_dir: '/', sources: [], files_total: 0, files_done: 0, files_skipped: 0, files_failed: 0,
      bytes_total: 0, bytes_done: 0, rate_bps: 0, eta_s: null, started_at: null, finished_at: null, error: null,
      current: null, items: [], items_more: 0, failed_items: [], ...over,
    };
  },
  advance(record) {
    const patch = record.steps.shift();
    if (!patch) return;
    Object.assign(record.view, patch);
    if (record.view.state === 'finished' && record.land) {
      record.land();
      record.land = null;
    }
  },
  find(id) { return this.jobs.find((r) => r.view.id === id); },
  autoJob(view, land = null) {
    const now = new Date().toISOString();
    const record = {
      view, auto: true, land,
      steps: [
        { state: 'running', phase: 'transferring', started_at: now, bytes_done: Math.floor(view.bytes_total / 2), rate_bps: MiB, eta_s: 2 },
        {
          state: 'finished', phase: null, bytes_done: view.bytes_total, files_done: view.files_total, rate_bps: 0, eta_s: null,
          finished_at: now, items: view.items.map((i) => ({ ...i, done: i.bytes, state: 'finished' })),
        },
      ],
    };
    this.jobs.unshift(record);
    return record;
  },
};
backend.jobs.push({
  view: backend.job({ id: 'jc2', ssh: 'c2', ssh_name: 'prod-db-1', direction: 'download', state: 'finished', target_dir: '/Users/me', sources: ['/var/log/mysql/error.log'], files_total: 1, files_done: 1, bytes_total: 4096, bytes_done: 4096 }),
  steps: [], auto: false, land: null,
});

const fail = (error, code) => ({ status: 400, body: code ? { error, code } : { error } });
async function answer(route, body) {
  switch (route) {
    case 'auth/session': return { body: { token: 't' } };
    case 'lifecycle/choose-directory': return { body: { path: '/Volumes/Backup', cancelled: false } };
    case 'files/remote/list':
    case 'files/local/list': {
      const side = route.includes('remote') ? 'remote' : 'local';
      const key = `${side}:${body.path}`;
      if (backend.delays.has(key)) await sleep(backend.delays.get(key));
      if (backend.failures.has(key)) {
        const f = backend.failures.get(key);
        return fail(f.error, f.code);
      }
      const tree = side === 'remote' ? remoteTree : localTree;
      const want = body.path === '' ? tree.home : body.path;
      const node = tree.get(want);
      if (!node || node.kind !== 'dir') return fail(`No such folder: ${want}`);
      return { body: { path: tree.norm(want), home: tree.home, entries: [...node.children].map(([n, c]) => entry(n, c)) } };
    }
    case 'files/remote/mkdir':
    case 'files/local/mkdir': {
      const tree = route.includes('remote') ? remoteTree : localTree;
      if (tree.get(body.path)) return fail(`${body.path} already exists.`);
      tree.put(body.path, dir({}));
      return { body: { path: body.path } };
    }
    case 'files/remote/rename':
    case 'files/local/rename': {
      const tree = route.includes('remote') ? remoteTree : localTree;
      const node = tree.get(body.from);
      if (!node) return fail(`No such file: ${body.from}`);
      tree.drop(body.from);
      tree.put(body.to, node);
      return { body: { path: body.to } };
    }
    case 'files/remote/remove': {
      let removed = 0;
      for (const p of body.paths) {
        const node = remoteTree.get(p);
        if (node) {
          removed += count(node);
          remoteTree.drop(p);
        }
      }
      return { body: { removed } };
    }
    case 'files/upload':
    case 'files/download': {
      const direction = route === 'files/upload' ? 'upload' : 'download';
      const sources = direction === 'upload' ? body.local_paths : body.remote_paths;
      const target = direction === 'upload' ? body.remote_dir : body.local_dir;
      const [from, to] = direction === 'upload' ? [localTree, remoteTree] : [remoteTree, localTree];
      const sizes = sources.map((s) => from.get(s)?.size || 0);
      const view = backend.job({
        ssh: body.ssh, ssh_name: NAMES[body.ssh], direction, on_exists: body.on_exists, target_dir: target, sources,
        files_total: sources.length, bytes_total: sizes.reduce((a, b) => a + b, 0),
        items: sources.map((s, i) => ({ source: s, target: join(target, base(s)), bytes: sizes[i], done: 0, state: 'queued', error: null })),
      });
      backend.autoJob(view, () => {
        for (const s of sources) {
          const node = from.get(s);
          const dest = join(target, base(s));
          if (node && !(to.get(dest) && body.on_exists === 'skip')) to.put(dest, clone(node));
        }
      });
      return { body: { job: structuredClone(view) } };
    }
    case 'files/jobs': {
      for (const record of backend.jobs) if (record.auto) backend.advance(record);
      return { body: { jobs: backend.jobs.map((r) => structuredClone(r.view)) } };
    }
    case 'files/cancel': {
      const record = backend.find(body.job_id);
      if (!record || !['queued', 'running'].includes(record.view.state)) return fail('This transfer has already ended.');
      record.steps = [];
      Object.assign(record.view, { state: 'cancelled', phase: null, rate_bps: 0, eta_s: null, finished_at: new Date().toISOString() });
      return { body: { job: structuredClone(record.view) } };
    }
    case 'files/retry': {
      const record = backend.find(body.job_id);
      if (!record || record.spent || !['failed', 'cancelled'].includes(record.view.state)) return fail('Nothing to retry in this transfer.');
      const old = record.view;
      const sources = old.failed_items.length ? old.failed_items.map((i) => i.source) : old.sources;
      const view = backend.job({
        ssh: old.ssh, ssh_name: old.ssh_name, direction: old.direction, on_exists: old.on_exists, target_dir: old.target_dir,
        sources, files_total: sources.length, bytes_total: 20,
        items: sources.map((s) => ({ source: s, target: join(old.target_dir, base(s)), bytes: 10, done: 0, state: 'queued', error: null })),
      });
      backend.autoJob(view);
      return { body: { job: structuredClone(view) } };
    }
    case 'files/clear': {
      const before = backend.jobs.length;
      backend.jobs = backend.jobs.filter((r) => !(r.view.state === 'finished' && (!body.ssh || r.view.ssh === body.ssh)));
      return { body: { cleared: before - backend.jobs.length } };
    }
    default: return { status: 404, body: { error: `Unknown route ${route}` } };
  }
}

// ---- Browser -----------------------------------------------------------------------
await mkdir(shots, { recursive: true });
const browser = await puppeteer.launch({ headless: 'shell', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
const requests = [];
const aborted = [];
const errors = [];
let page;
let section = '';
const step = (name) => { section = name; };
try {
  page = await browser.newPage();
  page.on('pageerror', (e) => errors.push(`${section}: ${String(e)}`));
  page.on('console', (msg) => {
    // A 4xx answer is logged by Chrome itself as a failed resource load.
    if (msg.type() === 'error' && !/Failed to load resource/.test(msg.text())) errors.push(`${section}: ${msg.text()}`);
  });
  page.on('requestfailed', (req) => {
    if (req.url().includes('/api/')) aborted.push({ route: new URL(req.url()).pathname.slice(5), body: JSON.parse(req.postData() || '{}') });
  });
  await page.setRequestInterception(true);
  page.on('request', async (req) => {
    const url = new URL(req.url());
    try {
      if (!url.pathname.startsWith('/api/')) {
        await req.respond({ status: 200, contentType: 'text/html', body: html });
        return;
      }
      const route = url.pathname.slice(5);
      const body = JSON.parse(req.postData() || '{}');
      requests.push({ route, body, at: Date.now() });
      const reply = await answer(route, body);
      await req.respond({ status: reply.status || 200, contentType: 'application/json', body: JSON.stringify(reply.body) });
    } catch {
      /* The page aborted the request (Stop, a newer listing, unmount). */
    }
  });
  await page.setViewport({ width: 1440, height: 900 });

  // ---- Helpers ---------------------------------------------------------------------
  const pane = (side) => `[data-side="${side}"]`;
  const row = (side, name) => `${pane(side)} .files-grid-body [data-name="${name}"]`;
  const mark = () => requests.length;
  const since = (m, route) => requests.slice(m).filter((r) => r.route === route);
  async function until(fn, label, timeout = 8000) {
    const start = Date.now();
    for (;;) {
      const value = await fn();
      if (value) return value;
      if (Date.now() - start > timeout) throw new Error(`Timed out: ${label}`);
      await sleep(25);
    }
  }
  /** Exactly one request to *route* since *m* (after a short settle). */
  async function one(m, route, label) {
    await until(() => since(m, route).length, `${label}: request sent`);
    await sleep(200);
    const list = since(m, route);
    assert.equal(list.length, 1, `${label}: exactly one ${route} request`);
    return list[0].body;
  }
  const names = (side) => page.$$eval(`${pane(side)} .files-grid-body [data-name]`, (els) => els.map((e) => e.dataset.name));
  const selected = (side) => page.$$eval(`${pane(side)} .files-grid-body [aria-selected="true"]`, (els) => els.map((e) => e.dataset.name));
  const pathOf = (side) => page.$eval(`${pane(side)} .files-crumbs`, (e) => e.dataset.path);
  const summary = (side) => page.$eval(`${pane(side)} .files-pane-summary`, (e) => e.textContent);
  async function settled(side, p) {
    await page.waitForFunction((s, want) => {
      const crumbs = document.querySelector(`[data-side="${s}"] .files-crumbs`);
      return crumbs && crumbs.dataset.path === want && !document.querySelector(`[data-side="${s}"] .files-grid[aria-busy="true"]`);
    }, { timeout: 8000 }, side, p);
  }
  async function withKeys(keys, fn) {
    for (const k of keys) await page.keyboard.down(k);
    try { await fn(); } finally { for (const k of [...keys].reverse()) await page.keyboard.up(k); }
  }
  const clickRow = (side, name, keys = []) => withKeys(keys, () => page.click(row(side, name)));
  async function chord(keys, key) { await withKeys(keys, () => page.keyboard.press(key)); }
  async function focusGrid(side) {
    await page.focus(`${pane(side)} .files-grid`);
  }
  /** Clicks the button whose visible text is *label* inside *scope*; twice when asked. */
  async function clickButton(scope, label, times = 1) {
    const ok = await page.evaluate((s, text, n) => {
      const button = [...document.querySelectorAll(`${s} button`)].find((b) => b.textContent.trim() === text);
      if (!button) return false;
      for (let i = 0; i < n; i++) button.click();
      return true;
    }, scope, label, times);
    assert.ok(ok, `button "${label}" in ${scope}`);
  }
  const sheet = 'dialog[open]';
  async function sheetOpen() { await page.waitForSelector(sheet); }
  async function sheetClosed() { await page.waitForFunction(() => !document.querySelector('dialog[open]')); }
  async function menuLabels() {
    await page.waitForSelector('.context-menu');
    return page.$$eval('.context-menu [role="menuitem"]', (els) => els.map((e) =>
      [...e.children].filter((c) => !c.classList.contains('context-menu-icon') && !c.classList.contains('context-menu-shortcut'))
        .map((c) => (c.querySelector('span') || c).textContent).join('')));
  }
  async function menuClick(label) {
    await page.waitForSelector('.context-menu');
    await page.evaluate((text) => {
      const item = [...document.querySelectorAll('.context-menu [role="menuitem"]')].find((e) => e.textContent.startsWith(text));
      item.click();
    }, label);
  }
  async function typePath(side, value, key = 'Enter') {
    await focusGrid(side);
    await chord([MOD], 'l');
    await page.waitForSelector(`${pane(side)} .files-path-input`);
    await page.$eval(`${pane(side)} .files-path-input`, (input) => input.select());
    await page.keyboard.type(value);
    await page.keyboard.press(key);
  }
  async function drag(fromSelector, toSelector) {
    await page.evaluate((a, b) => {
      const from = document.querySelector(a);
      const to = document.querySelector(b);
      const dataTransfer = new DataTransfer();
      const fire = (el, type) => el.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer }));
      fire(from, 'dragstart');
      fire(to, 'dragenter');
      fire(to, 'dragover');
      fire(to, 'drop');
      fire(from, 'dragend');
    }, fromSelector, toSelector);
  }
  async function dropFile(toSelector, name) {
    await page.evaluate((b, fileName) => {
      const to = document.querySelector(b);
      const dataTransfer = new DataTransfer();
      dataTransfer.items.add(new File(['x'], fileName, { type: 'application/octet-stream' }));
      for (const type of ['dragenter', 'dragover', 'drop']) to.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer }));
    }, toSelector, name);
  }
  async function load(connection) {
    await page.goto(`${ORIGIN}/`);
    await page.evaluate((c) => { window.__connection = c; }, connection);
    await page.addScriptTag({ content: bundle.outputFiles[0].text });
  }
  const jobRow = (id) => `.files-job[data-job="${id}"]`;
  const jobText = (id, part) => page.$eval(`${jobRow(id)} ${part}`, (e) => e.textContent);

  const c1 = { id: 'c1', name: 'lab-box', host: '127.0.0.1', port: 2222, user: 'lab', auth: 'agent', key_path: '', has_secret: false, remote_dir: '/home/lab/data', local_dir: '', updated: '2026-11-05T10:00:00Z' };

  // ==== Load 1 ========================================================================
  step('first listings');
  let m = mark();
  await load(c1);
  await settled('remote', '/home/lab/data');
  await settled('local', '/Users/me');
  assert.deepEqual(since(m, 'files/remote/list')[0].body, { ssh: 'c1', path: '/home/lab/data' }, 'remote opens remote_dir');
  assert.deepEqual(since(m, 'files/local/list')[0].body, { path: '' }, 'local opens "" (home) when local_dir is empty');
  assert.deepEqual(since(m, 'files/jobs')[0].body, {}, 'transfers are read for every connection');
  assert.equal(await page.$eval(`${pane('local')} .files-crumbs`, (e) => [...e.querySelectorAll('.files-crumb-button')].map((b) => b.textContent).join(' ')), '/ Users me',
    'the answered (resolved) home shows in the path bar');
  assert.equal(await page.$eval('.files-endpoint', (e) => e.textContent), 'lab@127.0.0.1:2222');
  await page.waitForFunction(() => document.querySelector('.files-identity .status-indicator')?.textContent === 'Connected');

  step('folders first and sorting');
  assert.deepEqual(await names('remote'), ['archive', 'link-to-logs', 'logs', 'a.txt', 'big.bin', 'dump.sql', 'readme.md']);
  const sortBy = (label) => page.evaluate((text) => {
    [...document.querySelectorAll('[data-side="remote"] .files-grid-head .sort-button')].find((b) => b.textContent.trim() === text).click();
  }, label);
  const ariaSort = (label) => page.evaluate((text) => [...document.querySelectorAll('[data-side="remote"] [role="columnheader"]')]
    .find((h) => h.textContent.trim() === text).getAttribute('aria-sort'), label);
  assert.equal(await ariaSort('Name'), 'ascending');
  await sortBy('Name');
  assert.deepEqual(await names('remote'), ['logs', 'link-to-logs', 'archive', 'readme.md', 'dump.sql', 'big.bin', 'a.txt'], 'Name descending, folders still first');
  assert.equal(await ariaSort('Name'), 'descending');
  await sortBy('Size');
  assert.deepEqual((await names('remote')).slice(3), ['dump.sql', 'big.bin', 'readme.md', 'a.txt'], 'Size starts largest first');
  assert.equal(await ariaSort('Size'), 'descending');
  assert.equal(await ariaSort('Name'), 'none');
  await sortBy('Size');
  assert.deepEqual((await names('remote')).slice(3), ['a.txt', 'readme.md', 'big.bin', 'dump.sql'], 'Size toggles to ascending');
  await sortBy('Name');
  assert.deepEqual(await names('remote'), ['archive', 'link-to-logs', 'logs', 'a.txt', 'big.bin', 'dump.sql', 'readme.md']);
  assert.equal(await page.$eval(`${pane('remote')} [data-name="big.bin"] .files-cell-perm`, (e) => e.textContent), '-rw-r--r--');
  assert.equal(await page.$eval(`${pane('remote')} [data-name="logs"] .files-cell-perm`, (e) => e.textContent), 'drwxr-xr-x');
  assert.equal(await page.$eval(`${pane('remote')} [data-name="big.bin"] .files-cell-size`, (e) => e.textContent), '64.0 MiB');
  assert.equal(await page.$(`${pane('local')} .files-cell-perm`), null, 'This computer has no Permissions column');
  const linkTitle = await page.$eval(row('remote', 'link-to-logs'), (e) => [e.querySelector('.files-entry-icon.is-link') !== null, e.querySelector('.files-cell-name').title]);
  assert.ok(linkTitle[0], 'a link shows the link badge');
  assert.match(linkTitle[1], /Symbolic link: opens if it points to a folder; skipped in transfers/);

  step('hidden files');
  assert.equal(await summary('remote'), '7 items · 2 hidden');
  await page.evaluate(() => [...document.querySelectorAll('.files-toolbar .checkbox')].find((l) => l.textContent.includes('Show hidden files')).click());
  await page.waitForSelector(row('remote', '.hidden'));
  assert.ok((await names('remote')).includes('.config'));
  assert.equal(await page.evaluate(() => localStorage.getItem('myflames.files.showHidden')), '1');
  assert.equal(await summary('remote'), '9 items');
  assert.ok((await names('local')).includes('.zshrc'), 'the switch covers both panes');
  await page.evaluate(() => [...document.querySelectorAll('.files-toolbar .checkbox')].find((l) => l.textContent.includes('Show hidden files')).click());
  await page.waitForFunction(() => !document.querySelector('[data-side="remote"] [data-name=".hidden"]'));

  step('mouse selection');
  await clickRow('remote', 'a.txt');
  assert.deepEqual(await selected('remote'), ['a.txt']);
  assert.equal(await summary('remote'), '1 selected · 10 B');
  await clickRow('remote', 'dump.sql', [MOD]);
  assert.deepEqual(await selected('remote'), ['a.txt', 'dump.sql']);
  await clickRow('remote', 'logs', ['Shift']);
  assert.deepEqual(await selected('remote'), ['logs', 'a.txt', 'big.bin', 'dump.sql'], 'Shift-click selects from the anchor');
  await focusGrid('remote');
  await chord([MOD], 'a');
  assert.equal((await selected('remote')).length, 7, `${MOD}+A selects every row`);
  await clickRow('remote', 'big.bin');
  assert.deepEqual(await selected('remote'), ['big.bin'], 'a plain click on a selected row selects it alone');

  step('keyboard');
  await page.keyboard.press('ArrowDown');
  assert.deepEqual(await selected('remote'), ['dump.sql']);
  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  assert.deepEqual(await selected('remote'), ['a.txt']);
  await chord(['Shift'], 'ArrowUp');
  assert.deepEqual(await selected('remote'), ['logs', 'a.txt'], 'Shift+ArrowUp extends the selection');
  await page.keyboard.press('Home');
  assert.deepEqual(await selected('remote'), ['archive']);
  const active = await page.$eval(`${pane('remote')} .files-grid`, (g) => document.getElementById(g.getAttribute('aria-activedescendant'))?.dataset.name);
  assert.equal(active, 'archive', 'aria-activedescendant names the active row');
  m = mark();
  await page.keyboard.press('Enter');
  await settled('remote', '/home/lab/data/archive');
  assert.deepEqual(since(m, 'files/remote/list').map((r) => r.body), [{ ssh: 'c1', path: '/home/lab/data/archive' }], 'Enter opens a folder');
  m = mark();
  await page.keyboard.press('Backspace');
  await settled('remote', '/home/lab/data');
  assert.equal(since(m, 'files/remote/list')[0].body.path, '/home/lab/data', 'Backspace goes up');
  assert.deepEqual(await selected('remote'), ['archive'], 'the folder you came from is selected');
  await clickRow('remote', 'dump.sql');
  await page.keyboard.press('F2');
  await sheetOpen();
  await page.waitForFunction(() => document.activeElement?.matches('.files-name-sheet input'));
  assert.deepEqual(await page.$eval('.files-name-sheet input', (i) => [i.value, i.selectionStart, i.selectionEnd]), ['dump.sql', 0, 4],
    'F2 opens Rename with the name selected without its extension');
  m = mark();
  await page.keyboard.press('Escape');
  await sheetClosed();
  await focusGrid('remote');
  await page.keyboard.press('Delete');
  await sheetOpen();
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), 'Delete 1 item from lab-box?');
  await page.keyboard.press('Escape');
  await sheetClosed();
  assert.equal(since(m, 'files/remote/rename').length + since(m, 'files/remote/remove').length, 0, 'Escape sends nothing');

  step('back and forward');
  m = mark();
  await page.click(`${pane('remote')} button[aria-label="Back"]`);
  await settled('remote', '/home/lab/data/archive');
  await page.click(`${pane('remote')} button[aria-label="Forward"]`);
  await settled('remote', '/home/lab/data');
  assert.deepEqual(since(m, 'files/remote/list').map((r) => r.body.path), ['/home/lab/data/archive', '/home/lab/data']);

  step('path bar');
  m = mark();
  await page.evaluate(() => [...document.querySelectorAll('[data-side="remote"] .files-crumb-button')].find((b) => b.textContent === 'lab').click());
  await settled('remote', '/home/lab');
  await page.evaluate(() => [...document.querySelectorAll('[data-side="remote"] .files-crumb-button')].find((b) => b.textContent === '/').click());
  await settled('remote', '/');
  assert.ok(await page.$eval(`${pane('remote')} button[aria-label="Up one folder"]`, (b) => b.disabled), 'Up is off at the root');
  await page.click(`${pane('remote')} button[aria-label="Home folder"]`);
  await settled('remote', '/home/lab');
  assert.deepEqual(since(m, 'files/remote/list').map((r) => r.body.path), ['/home/lab', '/', '/home/lab'], 'breadcrumbs and Home navigate');
  m = mark();
  await page.click(`${pane('remote')} button[aria-label="Up one folder"]`);
  await settled('remote', '/home');
  assert.equal(since(m, 'files/remote/list')[0].body.path, '/home');
  m = mark();
  await focusGrid('remote');
  await chord([MOD], 'l');
  await page.waitForSelector(`${pane('remote')} .files-path-input`);
  assert.deepEqual(await page.$eval(`${pane('remote')} .files-path-input`, (i) => [i.value, i.selectionStart, i.selectionEnd]), ['/home', 0, 5],
    `${MOD}+L turns the path into a selected input`);
  await page.keyboard.type('/home/lab/data');
  await page.keyboard.press('Enter');
  await settled('remote', '/home/lab/data');
  assert.deepEqual(since(m, 'files/remote/list').map((r) => r.body), [{ ssh: 'c1', path: '/home/lab/data' }], 'Enter lists the typed path');
  m = mark();
  await page.click(`${pane('remote')} button[aria-label="Edit path"]`);
  await page.waitForSelector(`${pane('remote')} .files-path-input`);
  await page.keyboard.type('/nope');
  await page.keyboard.press('Escape');
  await page.waitForSelector(`${pane('remote')} .files-crumbs`);
  assert.equal(await pathOf('remote'), '/home/lab/data', 'Escape restores the breadcrumbs');
  await sleep(150);
  assert.equal(since(m, 'files/remote/list').length, 0, 'Escape lists nothing');

  step('filter');
  await page.type(`${pane('remote')} input[type="search"]`, 'dump');
  await page.waitForFunction(() => document.querySelectorAll('[data-side="remote"] .files-grid-body [data-name]').length === 1);
  assert.deepEqual(await names('remote'), ['dump.sql']);
  assert.equal(await summary('remote'), '1 of 7 items');
  await page.$eval(`${pane('remote')} input[type="search"]`, (input) => input.select());
  await page.keyboard.press('Backspace');
  await page.waitForFunction(() => document.querySelectorAll('[data-side="remote"] .files-grid-body [data-name]').length === 7);

  step('Browse… on This computer');
  m = mark();
  await clickButton(pane('local'), 'Browse…');
  await settled('local', '/Volumes/Backup');
  assert.deepEqual(since(m, 'files/local/list').map((r) => r.body), [{ path: '/Volumes/Backup' }]);
  await page.click(`${pane('local')} button[aria-label="Back"]`);
  await settled('local', '/Users/me');

  step('double-click a file uploads it, the destination refreshes once');
  m = mark();
  await page.click(row('local', 'notes.txt'), { count: 2 });
  const doubleUpload = await one(m, 'files/upload', 'double-click upload');
  assert.deepEqual(doubleUpload, { ssh: 'c1', local_paths: ['/Users/me/notes.txt'], remote_dir: '/home/lab/data', on_exists: 'fail' });
  const firstJob = `j${backend.seq}`;
  await page.waitForSelector(jobRow(firstJob));
  assert.equal(await jobText(firstJob, '.files-job-label'), 'Upload notes.txt to /home/lab/data');
  await page.waitForSelector(`${jobRow(firstJob)}[data-state="finished"]`, { timeout: 8000 });
  await page.waitForSelector(row('remote', 'notes.txt'));
  await sleep(2300);
  assert.equal(since(m, 'files/remote/list').filter((r) => r.body.path === '/home/lab/data').length, 1, 'one refresh of the destination');
  assert.equal(since(m, 'files/local/list').length, 0, 'the source pane is not listed again');
  let polls = mark();
  await sleep(2500);
  assert.equal(since(polls, 'files/jobs').length, 0, 'polling stops when nothing is active');

  step('Upload and Download buttons');
  await clickRow('local', 'report.pdf');
  await clickRow('local', 'Downloads', [MOD]);
  assert.equal(await page.$eval(`${pane('local')} .files-transfer-button`, (b) => b.title), 'Upload 2 items to /home/lab/data');
  m = mark();
  await clickButton(pane('local'), 'Upload →', 2);
  assert.deepEqual(await one(m, 'files/upload', 'Upload button'), {
    ssh: 'c1', local_paths: ['/Users/me/Downloads', '/Users/me/report.pdf'], remote_dir: '/home/lab/data', on_exists: 'fail',
  }, 'Upload sends the selection in screen order, once for a double click');
  await clickRow('remote', 'dump.sql');
  assert.equal(await page.$eval(`${pane('remote')} .files-transfer-button`, (b) => b.title), 'Download dump.sql to /Users/me');
  m = mark();
  await clickButton(pane('remote'), '← Download');
  assert.deepEqual(await one(m, 'files/download', 'Download button'), {
    ssh: 'c1', remote_paths: ['/home/lab/data/dump.sql'], local_dir: '/Users/me', on_exists: 'fail',
  });
  await page.waitForFunction(() => document.querySelectorAll('.files-job[data-state="finished"]').length >= 3, { timeout: 8000 });
  await page.waitForSelector(row('local', 'dump.sql'));

  step('conflicts');
  await clickRow('remote', 'big.bin');
  m = mark();
  await clickButton(pane('remote'), '← Download');
  await sheetOpen();
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), '1 item already exists in me');
  const conflictText = await page.$eval(sheet, (e) => e.textContent);
  assert.match(conflictText, /lab-box: 64\.0 MiB/, 'the source size shows');
  assert.match(conflictText, /This computer: 1\.0 MiB/, 'the existing size shows');
  assert.doesNotMatch(conflictText, /removes the server's copy/, 'the upload warning is for uploads');
  await clickButton(sheet, 'Skip existing');
  await sheetClosed();
  assert.deepEqual(await one(m, 'files/download', 'Skip existing'), { ssh: 'c1', remote_paths: ['/home/lab/data/big.bin'], local_dir: '/Users/me', on_exists: 'skip' });
  m = mark();
  await clickButton(pane('remote'), '← Download');
  await sheetOpen();
  await clickButton(sheet, 'Overwrite');
  await sheetClosed();
  assert.equal((await one(m, 'files/download', 'Overwrite')).on_exists, 'overwrite');
  m = mark();
  await clickButton(pane('remote'), '← Download');
  await sheetOpen();
  await clickButton(sheet, 'Cancel');
  await sheetClosed();
  await sleep(150);
  assert.equal(since(m, 'files/download').length, 0, 'Cancel starts nothing');
  await clickRow('local', 'logs');
  await clickButton(pane('local'), 'Upload →');
  await sheetOpen();
  const folderText = await page.$eval(sheet, (e) => e.textContent);
  assert.match(folderText, /Existing folders are merged: your choice applies to the files inside them\./);
  assert.match(folderText, /Overwriting removes the server's copy first; if the upload stops, the old file cannot be restored\./);
  await page.keyboard.press('Escape');
  await sheetClosed();

  step('drag and drop');
  m = mark();
  await drag(row('remote', 'a.txt'), pane('local'));
  assert.deepEqual(await one(m, 'files/download', 'drag to This computer'), { ssh: 'c1', remote_paths: ['/home/lab/data/a.txt'], local_dir: '/Users/me', on_exists: 'fail' });
  await clickRow('local', 'todo.md');
  await clickRow('local', 'report.pdf', [MOD]);
  await clickRow('local', 'plan.txt', [MOD]);
  await clickRow('local', 'report.pdf', [MOD]);
  m = mark();
  await drag(row('local', 'todo.md'), pane('remote'));
  assert.deepEqual(await one(m, 'files/upload', 'drag a multi-selection to the server'), {
    ssh: 'c1', local_paths: ['/Users/me/plan.txt', '/Users/me/todo.md'], remote_dir: '/home/lab/data', on_exists: 'fail',
  }, 'a drag carries every selected row');
  m = mark();
  await drag(row('remote', 'readme.md'), pane('remote'));
  await sleep(200);
  assert.equal(since(m, 'files/upload').length + since(m, 'files/download').length, 0, 'a drop on its own pane does nothing');
  await dropFile(pane('remote'), 'photo.png');
  await page.waitForFunction(() => document.querySelector('.files-notes')?.textContent.includes('works in the desktop app'));
  assert.match(await page.$eval('.files-notes', (e) => e.textContent),
    /Dropping files from (Finder|File Explorer) works in the desktop app\. Use the This computer pane here\./);
  await page.evaluate(() => { window.myflamesDesktop = { pathForFile: (f) => `/Users/me/Desktop/${f.name}` }; });
  m = mark();
  await dropFile(pane('local'), 'photo.png');
  assert.deepEqual(await one(m, 'files/upload', 'drop from the system'), {
    ssh: 'c1', local_paths: ['/Users/me/Desktop/photo.png'], remote_dir: '/home/lab/data', on_exists: 'fail',
  }, 'files dropped on either pane upload into the remote folder');
  await page.waitForFunction(() => !document.querySelector('.files-job[data-state="queued"], .files-job[data-state="running"]'), { timeout: 10000 });

  step('New folder');
  await page.click(`${pane('remote')} button[aria-label="New folder"]`);
  await sheetOpen();
  await page.waitForFunction(() => document.activeElement?.matches('.files-name-sheet input'));
  assert.deepEqual(await page.$eval('.files-name-sheet input', (i) => [i.value, i.selectionStart, i.selectionEnd]), ['New folder', 0, 10]);
  m = mark();
  await page.$eval('.files-name-sheet input', (i) => i.select());
  await page.keyboard.type('a/b');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.files-name-sheet .form-error')?.textContent.includes('cannot contain'));
  await page.$eval('.files-name-sheet input', (i) => i.select());
  await page.keyboard.type('logs');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.files-name-sheet .form-error')?.textContent.includes('already an item named'));
  await page.$eval('.files-name-sheet input', (i) => i.select());
  await page.keyboard.type('..');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => document.querySelector('.files-name-sheet .form-error')?.textContent.includes('cannot be used as names'));
  assert.equal(since(m, 'files/remote/mkdir').length, 0, 'invalid names are not sent');
  await page.$eval('.files-name-sheet input', (i) => i.select());
  await page.keyboard.type('reports');
  await clickButton('.files-name-sheet', 'Create folder', 2);
  assert.deepEqual(await one(m, 'files/remote/mkdir', 'New folder'), { ssh: 'c1', path: '/home/lab/data/reports' }, 'one mkdir for a double click');
  await sheetClosed();
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] [data-name="reports"]')?.getAttribute('aria-selected') === 'true');
  m = mark();
  await page.click(`${pane('local')} button[aria-label="New folder"]`);
  await sheetOpen();
  await page.waitForFunction(() => document.activeElement?.matches('.files-name-sheet input'));
  await page.keyboard.type('Projects');
  await page.keyboard.press('Enter');
  assert.deepEqual(await one(m, 'files/local/mkdir', 'local New folder'), { path: '/Users/me/Projects' });
  await page.waitForFunction(() => document.querySelector('[data-side="local"] [data-name="Projects"]')?.getAttribute('aria-selected') === 'true');

  step('Rename');
  await clickRow('remote', 'a.txt');
  await page.keyboard.press('F2');
  await sheetOpen();
  await page.waitForFunction(() => document.activeElement?.matches('.files-name-sheet input'));
  await page.keyboard.type('b');
  assert.equal(await page.$eval('.files-name-sheet input', (i) => i.value), 'b.txt', 'typing replaces the stem only');
  m = mark();
  await page.keyboard.press('Enter');
  assert.deepEqual(await one(m, 'files/remote/rename', 'Rename'), { ssh: 'c1', from: '/home/lab/data/a.txt', to: '/home/lab/data/b.txt' });
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] [data-name="b.txt"]')?.getAttribute('aria-selected') === 'true');
  assert.ok(!(await names('remote')).includes('a.txt'));

  step('remote Delete');
  await clickRow('remote', 'archive');
  await clickRow('remote', 'b.txt', [MOD]);
  await focusGrid('remote');
  await page.keyboard.press('Delete');
  await sheetOpen();
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), 'Delete 2 items from lab-box?');
  assert.deepEqual(await page.$$eval(`${sheet} .files-sheet-names li`, (els) => els.map((e) => e.textContent)), ['archive', 'b.txt']);
  assert.match(await page.$eval(sheet, (e) => e.textContent), /Folders are deleted with everything in them\. This cannot be undone\./);
  m = mark();
  await page.evaluate(() => { const b = document.querySelector('dialog[open] .action-button'); b.click(); b.click(); });
  assert.deepEqual(await one(m, 'files/remote/remove', 'Delete'), { ssh: 'c1', paths: ['/home/lab/data/archive', '/home/lab/data/b.txt'] });
  await sheetClosed();
  await page.waitForFunction(() => document.querySelector('.files-notes')?.textContent.includes('Deleted 5 items.'));
  await page.waitForFunction(() => !document.querySelector('[data-side="remote"] [data-name="archive"]'));

  step('context menu');
  await page.click(row('remote', 'logs'), { button: 'right' });
  assert.deepEqual(await menuLabels(), ['Download', 'Open', 'Rename…', 'New folder…', 'Delete…', 'Copy path', 'Refresh']);
  m = mark();
  await menuClick('Open');
  await settled('remote', '/home/lab/data/logs');
  assert.equal(since(m, 'files/remote/list')[0].body.path, '/home/lab/data/logs');
  await page.click(`${pane('remote')} button[aria-label="Back"]`);
  await settled('remote', '/home/lab/data');
  await page.evaluate(() => {
    window.__clip = '';
    if (navigator.clipboard) navigator.clipboard.writeText = async (text) => { window.__clip = text; };
  });
  await page.click(row('remote', 'dump.sql'), { button: 'right' });
  await menuClick('Copy path');
  await page.waitForFunction(() => document.querySelector('.files-notes')?.textContent.includes('Path copied: /home/lab/data/dump.sql'));
  if (await page.evaluate(() => !!navigator.clipboard)) assert.equal(await page.evaluate(() => window.__clip), '/home/lab/data/dump.sql');
  await page.hover(row('remote', 'readme.md'));
  await page.click(`${row('remote', 'readme.md')} .files-row-more`);
  assert.deepEqual(await selected('remote'), ['readme.md'], 'the row button selects its row');
  m = mark();
  await menuClick('Download');
  assert.deepEqual(await one(m, 'files/download', 'menu Download'), { ssh: 'c1', remote_paths: ['/home/lab/data/readme.md'], local_dir: '/Users/me', on_exists: 'fail' });
  await page.evaluate(() => {
    const grid = document.querySelector('[data-side="remote"] .files-grid');
    const box = grid.getBoundingClientRect();
    grid.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: box.left + 40, clientY: box.bottom - 6 }));
  });
  const emptyMenu = await menuLabels();
  assert.ok(emptyMenu.includes('New folder…') && emptyMenu.includes('Refresh') && emptyMenu.includes('Copy path'), 'empty space has a folder menu');
  assert.ok(!emptyMenu.includes('Delete…'));
  m = mark();
  await menuClick('Refresh');
  assert.deepEqual(await one(m, 'files/remote/list', 'menu Refresh'), { ssh: 'c1', path: '/home/lab/data' });
  await settled('remote', '/home/lab/data');
  await page.click(row('local', 'todo.md'), { button: 'right' });
  const localMenu = await menuLabels();
  assert.ok(localMenu.includes('Upload') && !localMenu.includes('Delete…'), 'This computer offers no Delete');
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.context-menu'));

  step('listing errors');
  backend.failures.set('remote:/home/lab/secret', { error: 'Permission denied: /home/lab/secret' });
  const before = await names('remote');
  m = mark();
  await typePath('remote', '/home/lab/secret');
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] .files-pane-error [role="alert"]')?.textContent.includes('Permission denied'));
  assert.ok(await page.$(`${pane('remote')} .files-grid.is-stale`), 'the last listing stays, greyed');
  assert.deepEqual(await names('remote'), before);
  assert.equal(await pathOf('remote'), '/home/lab/data');
  assert.equal(await page.$eval('.files-identity .status-indicator', (e) => e.textContent), 'Connected', 'a folder error is not a connection error');
  await page.click(`${pane('remote')} .view-status-retry`);
  await until(() => since(m, 'files/remote/list').filter((r) => r.body.path === '/home/lab/secret').length === 2, 'Try again lists again');
  backend.failures.delete('remote:/home/lab/secret');
  await page.waitForSelector(`${pane('remote')} .view-status-retry`);
  await page.click(`${pane('remote')} .view-status-retry`);
  await settled('remote', '/home/lab/secret');
  assert.equal(await page.$(`${pane('remote')} .files-grid.is-stale`), null);
  backend.failures.set('remote:/srv', { error: 'Permission denied (publickey).', code: 'auth_failed' });
  await typePath('remote', '/srv');
  await page.waitForFunction(() => [...document.querySelectorAll('[data-side="remote"] .files-pane-error button')].some((b) => b.textContent === 'Edit connection'));
  await clickButton(`${pane('remote')} .files-pane-error`, 'Edit connection');
  assert.equal(await page.evaluate(() => window.__edits), 1, 'auth_failed offers Edit connection');
  assert.equal(await page.$eval('.files-identity .status-indicator', (e) => e.textContent), 'Not connected');
  backend.failures.delete('remote:/srv');
  await typePath('remote', '/home/lab/data');
  await settled('remote', '/home/lab/data');

  step('5000 entries');
  const t0 = Date.now();
  await typePath('remote', '/home/lab/big');
  await page.waitForSelector(row('remote', 'f0000.dat'));
  const firstRowsMs = Date.now() - t0;
  assert.ok(firstRowsMs < 1500, `5000 entries: first rows in ${firstRowsMs} ms (budget 1500 ms)`);
  assert.equal(await summary('remote'), '5,000 items');
  const drawn = await page.$$eval(`${pane('remote')} .files-grid-body [data-name]`, (els) => els.length);
  assert.ok(drawn < 120, `only the rows in view are drawn (${drawn})`);
  const scrollMs = await page.evaluate(async () => {
    const grid = document.querySelector('[data-side="remote"] .files-grid');
    const start = performance.now();
    for (let i = 1; i <= 20; i++) {
      grid.scrollTop = (grid.scrollHeight * i) / 20;
      await new Promise((r) => requestAnimationFrame(r));
    }
    return performance.now() - start;
  });
  await page.waitForSelector(row('remote', 'f4999.dat'));
  assert.ok(scrollMs < 2000, `20 scroll steps over 5000 rows in ${Math.round(scrollMs)} ms`);
  assert.ok((await page.$$eval(`${pane('remote')} .files-grid-body [data-name]`, (els) => els.length)) < 120);
  const stickyGap = await page.evaluate(() => {
    const grid = document.querySelector('[data-side="remote"] .files-grid');
    return Math.abs(grid.querySelector('.files-grid-head').getBoundingClientRect().top - grid.getBoundingClientRect().top);
  });
  assert.ok(stickyGap < 1, 'the header stays at the top of the pane while its rows scroll');
  const scrolledTo = await page.$eval(`${pane('remote')} .files-grid`, (g) => { g.scrollTop = 40000; return g.scrollTop; });
  m = mark();
  await page.click(`${pane('remote')} button[data-action="refresh"]`);
  await one(m, 'files/remote/list', 'Refresh of a long folder');
  await settled('remote', '/home/lab/big');
  assert.equal(await page.$eval(`${pane('remote')} .files-grid`, (g) => g.scrollTop), scrolledTo, 'Refresh keeps the scroll position');
  await focusGrid('remote');
  await page.keyboard.press('Home');
  await page.waitForSelector(row('remote', 'f0000.dat'));
  await page.keyboard.press('End');
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] [data-name="f4999.dat"]')?.getAttribute('aria-selected') === 'true');
  await typePath('remote', '/home/lab/data');
  await settled('remote', '/home/lab/data');

  step('Transfers panel');
  backend.jobs = backend.jobs.filter((r) => r.view.ssh === 'c2');
  const items = (prefix, n) => Array.from({ length: n }, (_, i) => ({ source: `${prefix}/f${i}`, target: `/home/lab/data/f${i}`, bytes: 10, done: i ? 0 : 10, state: i ? 'queued' : 'finished', error: null }));
  backend.jobs.unshift({
    view: backend.job({ id: 'jm1', direction: 'upload', target_dir: '/home/lab/data', sources: ['/Users/me/a', '/Users/me/b', '/Users/me/c'] }),
    auto: false, land: null,
    steps: [
      { state: 'running', phase: 'scanning', started_at: new Date().toISOString(), files_total: 10 },
      {
        phase: 'transferring', bytes_total: 64 * MiB, bytes_done: 13002342, rate_bps: 12 * MiB, eta_s: 4.2, files_done: 3, files_skipped: 1,
        current: { source: '/Users/me/a/f3', target: '/home/lab/data/a/f3', bytes: 2 * MiB, done: MiB, state: 'running', error: null },
        items: items('/Users/me/a', 3), items_more: 7,
      },
    ],
  });
  backend.jobs.unshift({
    view: backend.job({
      id: 'jf1', direction: 'download', state: 'failed', target_dir: '/Users/me', sources: ['/home/lab/data/ro'],
      error: 'Permission denied: /home/lab/data/ro', files_total: 10, files_done: 1, files_failed: 2, bytes_total: 100, bytes_done: 10,
      items: items('/home/lab/data/ro', 3), items_more: 7,
      failed_items: [
        { source: '/home/lab/data/ro/x', target: '/Users/me/ro/x', bytes: 10, done: 0, state: 'failed', error: 'Permission denied' },
        { source: '/home/lab/data/ro/y', target: '/Users/me/ro/y', bytes: 10, done: 0, state: 'failed', error: 'No space left on device' },
      ],
    }),
    steps: [], auto: false, land: null,
  });
  backend.jobs.unshift({
    view: backend.job({ id: 'jk1', direction: 'download', state: 'finished', target_dir: '/Users/me', sources: ['/home/lab/data/logs'], files_total: 3, files_done: 1, files_skipped: 2, bytes_total: 120, bytes_done: 120 }),
    steps: [], auto: false, land: null,
  });
  await page.click('.files-toolbar button[data-action="refresh"]');
  await page.waitForSelector(jobRow('jm1'));
  assert.equal(await jobText('jk1', '.status-pill'), 'Done, some skipped');
  assert.ok(await page.$(`${jobRow('jk1')} .status-pill.is-watch`), 'skipped files turn the pill to watch');
  assert.match(await jobText('jk1', '.files-job-stats'), / · 2 skipped$/);
  await page.waitForFunction(() => document.querySelectorAll('.files-job[data-state="finished"]').length === 1, { timeout: 4000 });
  assert.match(await jobText('jm1', '.files-job-stats'), /Waiting to start/);
  assert.match(await page.$eval('.files-transfers-counts', (e) => e.textContent), /^Queue 1 · Failed 1 · Done 1/);
  backend.advance(backend.find('jm1'));
  await page.waitForFunction(() => document.querySelector('.files-job[data-job="jm1"] .files-job-stats')?.textContent.startsWith('Scanning…'));
  assert.equal(await jobText('jm1', '.files-job-stats'), 'Scanning… · 10 files found');
  assert.equal(await jobText('jm1', '.status-pill'), 'Scanning');
  assert.ok(await page.$(`${jobRow('jm1')} .files-progress.is-indeterminate`), 'scanning shows an indeterminate bar');
  backend.advance(backend.find('jm1'));
  await page.waitForFunction(() => document.querySelector('.files-job[data-job="jm1"] .files-job-stats')?.textContent.includes('%'));
  assert.equal(await jobText('jm1', '.files-job-stats'), '12.4 MiB of 64.0 MiB · 19% · 12.0 MiB/s · ~4 s left · 3 of 10 files · 1 skipped');
  assert.equal(await page.$eval(`${jobRow('jm1')} [role="progressbar"]`, (e) => e.getAttribute('aria-valuenow')), '19');
  assert.equal(await jobText('jm1', '.files-job-label'), 'Upload 3 items to /home/lab/data');
  await page.click(`${jobRow('jm1')} .files-job-expand`);
  assert.match(await jobText('jm1', '.files-job-details'), /Now: \/Users\/me\/a\/f3 → \/home\/lab\/data\/a\/f3/);
  assert.match(await jobText('jm1', '.files-job-details'), /and 7 more/);
  assert.match(await jobText('jf1', '.files-job-error'), /Permission denied: \/home\/lab\/data\/ro/, 'a failed job shows its error');
  await page.click(`${jobRow('jf1')} .files-job-expand`);
  const failedDetails = await jobText('jf1', '.files-job-details');
  assert.match(failedDetails, /and 7 more/);
  assert.match(failedDetails, /Failed/);
  assert.match(failedDetails, /No space left on device/);
  m = mark();
  await clickButton(jobRow('jm1'), 'Cancel', 2);
  assert.deepEqual(await one(m, 'files/cancel', 'Cancel'), { job_id: 'jm1' });
  await page.waitForSelector(`${jobRow('jm1')}[data-state="cancelled"]`);
  m = mark();
  await clickButton(jobRow('jf1'), 'Retry');
  assert.deepEqual(await one(m, 'files/retry', 'Retry'), { job_id: 'jf1' });
  const retried = `j${backend.seq}`;
  await page.waitForSelector(`${jobRow(retried)}[data-state="finished"]`, { timeout: 8000 });
  backend.find('jf1').spent = true;
  m = mark();
  await clickButton(jobRow('jf1'), 'Retry');
  await one(m, 'files/retry', 'Retry refused');
  await page.waitForFunction(() => document.querySelector('.files-job[data-job="jf1"] .action-status')?.textContent.includes('Nothing to retry in this transfer.'));
  m = mark();
  await clickButton(jobRow('jm1'), 'Retry');
  await one(m, 'files/retry', 'Retry a cancelled job');
  const retried2 = `j${backend.seq}`;
  await page.waitForSelector(`${jobRow(retried2)}[data-state="finished"]`, { timeout: 8000 });
  assert.match(await page.$eval('.files-transfers-counts', (e) => e.textContent), /^Queue 0 · Failed 1 · Done 3 · Cancelled 1/);
  await page.click('.segmented button[data-value="all"]');
  await page.waitForSelector(jobRow('jc2'));
  assert.equal(await jobText('jc2', '.files-job-conn'), 'prod-db-1', 'All connections names each job\'s connection');
  assert.match(await page.$eval('.files-transfers-counts', (e) => e.textContent), /^Queue 0 · Failed 1 · Done 4/);
  await page.click('.segmented button[data-value="this"]');
  await page.waitForFunction(() => !document.querySelector('.files-job[data-job="jc2"]'));
  m = mark();
  await clickButton('.files-transfers-head', 'Clear finished', 2);
  assert.deepEqual(await one(m, 'files/clear', 'Clear finished'), { ssh: 'c1' });
  await page.waitForFunction((a, b) => !document.querySelector(`.files-job[data-job="${a}"]`) && !document.querySelector(`.files-job[data-job="${b}"]`)
    && !document.querySelector('.files-job[data-job="jk1"]'), {}, retried, retried2);
  assert.ok(await page.$(jobRow('jf1')) && await page.$(jobRow('jm1')), 'failed and cancelled jobs stay');
  polls = mark();
  await sleep(2500);
  assert.equal(since(polls, 'files/jobs').length, 0, 'no polling while nothing is active');
  await page.click('.files-transfers-toggle');
  assert.equal(await page.evaluate(() => localStorage.getItem('myflames.files.transfersOpen')), '0');
  assert.ok(await page.$eval('.files-transfers-body', (e) => e.hidden));
  await page.click('.files-transfers-toggle');
  assert.equal(await page.evaluate(() => localStorage.getItem('myflames.files.transfersOpen')), '1');

  step('pane Refresh keeps the selection and sends one request');
  await clickRow('remote', 'dump.sql');
  await clickRow('remote', 'readme.md', [MOD]);
  m = mark();
  await page.evaluate(() => { const b = document.querySelector('[data-side="remote"] button[data-action="refresh"]'); b.click(); b.click(); });
  await one(m, 'files/remote/list', 'pane Refresh');
  await settled('remote', '/home/lab/data');
  assert.deepEqual(await selected('remote'), ['dump.sql', 'readme.md'], 'a refresh keeps the selection');
  assert.ok(errors.length === 0, `no console errors so far:\n${errors.join('\n')}`);

  // ==== Load 2: a host that is not trusted yet, Stop on a slow listing ===============
  step('host_key_unknown');
  backend.failures.set('remote:', { error: 'new-box is not a trusted host yet.', code: 'host_key_unknown' });
  const c3 = { ...c1, id: 'c3', name: 'new-box', remote_dir: '', local_dir: '/Users/me/Downloads' };
  m = mark();
  await load(c3);
  await page.waitForFunction(() => window.__hostKeyCalls === 1);
  assert.deepEqual(since(m, 'files/remote/list')[0].body, { ssh: 'c3', path: '' }, 'an empty remote_dir lists the home folder');
  assert.deepEqual(since(m, 'files/local/list')[0].body, { path: '/Users/me/Downloads' }, 'local_dir opens first');
  await page.waitForFunction(() => document.querySelector('.files-identity .status-indicator')?.textContent === 'Not connected');
  await clickButton(`${pane('remote')} .files-pane-error`, 'Trust host…');
  assert.equal(await page.evaluate(() => window.__hostKeyCalls), 2, 'Trust host… asks again');
  backend.failures.delete('remote:');
  m = mark();
  await page.evaluate(() => window.__retry());
  await settled('remote', '/home/lab');
  assert.deepEqual(since(m, 'files/remote/list').map((r) => r.body), [{ ssh: 'c3', path: '' }], 'the retry lists again');
  await page.waitForFunction(() => document.querySelector('.files-identity .status-indicator')?.textContent === 'Connected');

  step('slow listing: seconds, still waiting, Stop');
  backend.delays.set('remote:/home/lab/slow', 14000);
  const shown = await names('remote');
  await typePath('remote', '/home/lab/slow');
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] .files-pane-status')?.textContent.includes('Listing /home/lab/slow…'));
  await page.waitForFunction(() => /\d+ s so far/.test(document.querySelector('[data-side="remote"] .files-pane-status')?.textContent || ''), { timeout: 3000 });
  await page.waitForFunction(() => document.querySelector('[data-side="remote"] .files-pane-status')?.textContent.includes('Still waiting for the server.'), { timeout: 12000 });
  await page.click(`${pane('remote')} .view-status-stop`);
  await page.waitForFunction(() => !document.querySelector('[data-side="remote"] .files-grid[aria-busy="true"]'));
  assert.equal(await pathOf('remote'), '/home/lab', 'Stop keeps the last folder');
  assert.deepEqual(await names('remote'), shown, 'Stop keeps the last listing');
  await until(() => aborted.some((r) => r.route === 'files/remote/list' && r.body.path === '/home/lab/slow'), 'the listing request is aborted', 3000);
  backend.delays.clear();

  // ==== Load 3: remembered folders, screenshots ========================================
  step('remembered folders');
  const remembered = await page.evaluate(() => [localStorage.getItem('myflames.files.c1.remote'), localStorage.getItem('myflames.files.c1.local')]);
  assert.deepEqual(remembered, ['/home/lab/data', '/Users/me']);
  m = mark();
  await load(c1);
  await settled('remote', '/home/lab/data');
  assert.equal(since(m, 'files/remote/list')[0].body.path, '/home/lab/data', 'the last folder opens first');

  step('screenshots and layout');
  backend.jobs = [
    {
      view: backend.job({
        id: 'js1', direction: 'upload', state: 'running', phase: 'transferring', target_dir: '/home/lab/data',
        sources: ['/Users/me/Downloads', '/Users/me/report.pdf', '/Users/me/todo.md'], files_total: 12, files_done: 7,
        bytes_total: 64 * MiB, bytes_done: 41 * MiB, rate_bps: 11.6 * MiB, eta_s: 2.4,
      }),
      steps: [], auto: false, land: null,
    },
    {
      view: backend.job({ id: 'js2', direction: 'download', state: 'finished', target_dir: '/Users/me', sources: ['/home/lab/data/dump.sql'], files_total: 1, files_done: 1, bytes_total: 1288490189, bytes_done: 1288490189 }),
      steps: [], auto: false, land: null,
    },
    {
      view: backend.job({ id: 'js3', direction: 'upload', state: 'failed', target_dir: '/home/lab/data/ro', sources: ['/Users/me/logs'], error: 'Permission denied: /home/lab/data/ro', files_total: 3, files_failed: 3 }),
      steps: [], auto: false, land: null,
    },
  ];
  await page.click('.files-toolbar button[data-action="refresh"]');
  await page.waitForSelector(jobRow('js1'));
  await clickRow('remote', 'dump.sql');
  await clickRow('remote', 'big.bin', [MOD]);
  await clickRow('local', 'Downloads');
  await focusGrid('remote');
  for (const [width, height] of [[1440, 900], [720, 520]]) {
    await page.setViewport({ width, height });
    await sleep(150);
    const layout = await page.evaluate(() => {
      const main = document.querySelector('.harness-main');
      const local = document.querySelector('[data-side="local"]').getBoundingClientRect();
      const remote = document.querySelector('[data-side="remote"]').getBoundingClientRect();
      return {
        pageScroll: document.documentElement.scrollWidth - window.innerWidth,
        mainScroll: main.scrollWidth - main.clientWidth,
        stacked: local.bottom <= remote.top + 1,
        sideBySide: local.right <= remote.left + 1 && Math.abs(local.top - remote.top) < 1,
      };
    });
    assert.ok(layout.pageScroll <= 0 && layout.mainScroll <= 0, `${width} px: nothing scrolls sideways (${JSON.stringify(layout)})`);
    if (width < 900) assert.ok(layout.stacked, '720 px: This computer above the server');
    else assert.ok(layout.sideBySide, '1440 px: panes side by side');
    // The header stays at the top of the pane's own scroll area.
    const sticky = await page.evaluate(() => {
      const grid = document.querySelector('[data-side="remote"] .files-grid');
      if (grid.scrollHeight <= grid.clientHeight) return { scrolls: false, gap: 0 };
      grid.scrollTop = 40;
      const gap = Math.abs(grid.querySelector('.files-grid-head').getBoundingClientRect().top - grid.getBoundingClientRect().top);
      grid.scrollTop = 0;
      return { scrolls: true, gap };
    });
    assert.ok(sticky.gap < 1, `${width} px: the header is sticky in its pane`);
    if (width < 900) assert.ok(sticky.scrolls, '720 px: the pane scrolls inside itself');
    for (const theme of ['light', 'dark']) {
      await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
      await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
      await sleep(120);
      await page.screenshot({ path: path.join(shots, `files-browser-${theme}-${width}.png`) });
      if (width < 900) {
        await page.$eval('.harness-main', (main) => { main.scrollTop = main.scrollHeight; });
        await sleep(80);
        await page.screenshot({ path: path.join(shots, `files-browser-${theme}-${width}-bottom.png`) });
        await page.$eval('.harness-main', (main) => { main.scrollTop = 0; });
      }
    }
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    await page.evaluate(() => { document.documentElement.dataset.theme = 'light'; });
  }

  assert.deepEqual(requests.filter((r) => r.route.startsWith('files/') || r.route.startsWith('ssh/'))
    .map((r) => r.route).filter((route) => !['files/remote/list', 'files/local/list', 'files/remote/mkdir', 'files/local/mkdir',
      'files/remote/rename', 'files/local/rename', 'files/remote/remove', 'files/upload', 'files/download', 'files/jobs',
      'files/cancel', 'files/retry', 'files/clear'].includes(route)), [], 'only Files routes are used');
  assert.deepEqual(errors, [], 'no console errors');
  console.log(`files-browser: ok (${requests.length} requests; 5000 rows first drawn in ${firstRowsMs} ms; screenshots in test/shots-files-browser)`);
} catch (error) {
  console.error(`files-browser failed in "${section}"`);
  if (page) await page.screenshot({ path: path.join(shots, 'failure.png') }).catch(() => {});
  throw error;
} finally {
  await browser.close();
}
