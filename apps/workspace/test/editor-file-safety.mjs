// Real authenticated file endpoints and real temporary files. Only the native
// chooser is replaced in Python; no database connection or SQL execution.
// Exemplar: editor-files.mjs. Run after npm run build.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
import { setEditorText } from './editor-helpers.mjs';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-file-safety-'));
const selected = path.join(directory, 'selected');
const moved = path.join(directory, 'moved');
const outside = path.join(directory, 'outside');
await fs.mkdir(selected);
await fs.mkdir(outside);
await fs.writeFile(path.join(selected, 'query.sql'), 'SELECT 1;');
await fs.writeFile(path.join(directory, 'report.sql'), 'keep');
await fs.writeFile(path.join(directory, 'large.sql'), Buffer.alloc(5 * 1024 * 1024 + 1, 120));
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' };
delete env.MYFLAMES_VAULT_PASSPHRASE;
const server = spawn(process.env.PYTHON || 'python3', ['-c', `
import os, sys
from myflames import file_picker, connector
from myflames.ui import WorkspaceServer
root = sys.argv[1]
opens = iter([os.path.join(root, 'selected', 'query.sql'), os.path.join(root, 'large.sql')])
saves = iter([os.path.join(root, 'report'), os.path.join(root, 'report.sql')])
# Like the real chooser, answer with check_file's canonical path.
file_picker.choose_sql_file = lambda: {'path': file_picker.check_file(next(opens)), 'cancelled': False}
file_picker.choose_save_file = lambda name: {'path': next(saves), 'cancelled': False}
def no_database(*args, **kwargs):
    raise AssertionError('File operations must not open a database connection')
connector.MySQLConnection.__init__ = no_database
server = WorkspaceServer(store_path=os.path.join(root, 'workspace.sqlite3'))
print(server.launch_url, file=sys.stderr, flush=True)
server.serve_forever()
`, directory], { cwd: root, env });
const exited = once(server, 'exit');
let stderr = '';
server.stderr.on('data', data => { stderr += data; });
let browser;
let page;
const diagnostics = { errors: [], console: [], failed: [], unexpected: [], responses: [] };
const requests = [];
try {
  const url = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error(stderr)), 10000);
    server.stderr.on('data', () => {
      const found = stderr.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (found) { clearTimeout(timer); resolve(found[0]); }
    });
    server.on('error', error => { clearTimeout(timer); reject(error); });
    server.on('exit', code => { clearTimeout(timer); reject(Error(`Server exited ${code}: ${stderr}`)); });
  });
  browser = await puppeteer.launch({ headless: 'shell' });
  page = await browser.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => diagnostics.errors.push(String(error)));
  page.on('console', message => {
    if (['error', 'warn'].includes(message.type())) diagnostics.console.push(message.text());
  });
  page.on('requestfailed', request => diagnostics.failed.push(request.url()));
  page.on('response', response => {
    if (response.status() >= 400) diagnostics.responses.push({ path: new URL(response.url()).pathname, status: response.status() });
  });
  await page.setRequestInterception(true);
  page.on('request', request => {
    const endpoint = new URL(request.url()).pathname;
    if (endpoint.startsWith('/api/editor/file/')) {
      requests.push({ path: endpoint, body: JSON.parse(request.postData() || '{}') });
      return request.continue();
    }
    const replies = {
      '/api/live/privileges': { schema_version: 'privileges-1.0', account: 'app@%', grants: [], current_roles: [], actions: {} },
      '/api/live/schemas': { schemas: [], system: [], user_tables: 0, all_limit: 2000, all_note: '' },
      '/api/editor/status': { status: 'none', session: null, run: null },
      '/api/editor/preview': { status: 'ready', statements: [], aggregate: { state: 'unknown', missing: [], reason: '', warning: '' }, policy: { action: 'allow', text: '' } },
      '/api/news/list': { items: [] },
    };
    if (Object.hasOwn(replies, endpoint)) return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(replies[endpoint]) });
    if (endpoint.startsWith('/api/live/') || endpoint.startsWith('/api/connection/')) {
      diagnostics.unexpected.push(endpoint);
      return request.respond({ status: 400, contentType: 'application/json', body: '{"error":"Unexpected database request"}' });
    }
    return request.continue();
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  await page.locator('.main-nav button ::-p-text(Query editor)').click();
  const editor = '[aria-label="SQL editor"]';
  const dirty = '.query-tablist [role=tab][aria-selected=true] [title="Edited since the last save"]';
  await page.waitForSelector(editor, { visible: true });
  const modifier = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? 'Meta' : 'Control');
  async function press(key, shift = false) {
    await page.focus(editor);
    await page.keyboard.down(modifier);
    if (shift) await page.keyboard.down('Shift');
    await page.keyboard.press(key);
    if (shift) await page.keyboard.up('Shift');
    await page.keyboard.up(modifier);
  }
  async function action(endpoint, key, shift = false) {
    const response = page.waitForResponse(value => new URL(value.url()).pathname === endpoint);
    await press(key, shift);
    const result = await response;
    return { status: result.status(), body: await result.json() };
  }
  async function edit(text) {
    await setEditorText(page, text);
    await page.waitForSelector(dirty);
  }
  const opened = await action('/api/editor/file/open', 'o');
  assert.equal(opened.status, 200);
  await page.waitForFunction(() => document.querySelector('[aria-label="SQL editor"]')?.value === 'SELECT 1;');
  await edit('SELECT 2;');
  const saved = await action('/api/editor/file/save', 's');
  assert.equal(saved.status, 200);
  await page.waitForFunction(selector => !document.querySelector(selector), {}, dirty);
  assert.equal(await fs.readFile(path.join(selected, 'query.sql'), 'utf8'), 'SELECT 2;');

  await edit('SELECT 3;');
  await fs.rename(selected, moved);
  await fs.symlink(outside, selected, 'dir');
  const swapped = await action('/api/editor/file/save', 's');
  assert.equal(swapped.status, 400);
  assert.match(swapped.body.error, /folder.*changed/);
  await page.waitForFunction(() => document.body.textContent.includes("folder has changed"));
  await page.waitForSelector(dirty);
  assert.equal(await fs.readFile(path.join(moved, 'query.sql'), 'utf8'), 'SELECT 2;');
  assert.deepEqual(await fs.readdir(outside), []);

  const collision = await action('/api/editor/file/save', 's', true);
  assert.equal(collision.status, 400);
  assert.match(collision.body.error, /choose.*again/);
  await page.waitForFunction(() => document.body.textContent.includes('Adding .sql selects a file that already exists.'));
  assert.equal(await fs.readFile(path.join(directory, 'report.sql'), 'utf8'), 'keep');
  await page.waitForSelector(dirty);

  // Error feedback and the dirty buffer remain usable at the supported sizes.
  for (const width of [1440, 720]) for (const theme of ['light', 'dark']) {
    await page.setViewport({ width, height: 900 });
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => document.documentElement.scrollWidth <= window.innerWidth);
    assert.equal(await page.$eval(editor, element => element.value), 'SELECT 3;');
    if (process.env.EDITOR_FILE_SHOTS) {
      await fs.mkdir(process.env.EDITOR_FILE_SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.EDITOR_FILE_SHOTS, `${width}-${theme}.png`) });
    }
  }
  const approved = await action('/api/editor/file/save', 's', true);
  assert.equal(approved.status, 200);
  await page.waitForFunction(selector => !document.querySelector(selector), {}, dirty);
  assert.equal(await fs.readFile(path.join(directory, 'report.sql'), 'utf8'), 'SELECT 3;');
  const count = await page.$$eval('.query-tablist [role=tab]', elements => elements.length);
  const oversized = await action('/api/editor/file/open', 'o');
  assert.equal(oversized.status, 400);
  assert.match(oversized.body.error, /5 MiB/);
  await page.waitForFunction(() => document.body.textContent.includes('larger than 5 MiB'));
  assert.equal(await page.$$eval('.query-tablist [role=tab]', elements => elements.length), count);
  assert.equal(await page.$eval(editor, element => element.value), 'SELECT 3;');
  assert.equal(requests.length, 6);
  for (const request of requests) assert.equal(Object.hasOwn(request.body, 'path'), false);
  assert.deepEqual(diagnostics.responses, [
    { path: '/api/editor/file/save', status: 400 },
    { path: '/api/editor/file/save', status: 400 },
    { path: '/api/editor/file/open', status: 400 },
  ]);
  assert.deepEqual(diagnostics.errors, []);
  assert.deepEqual(diagnostics.failed, []);
  assert.deepEqual(diagnostics.unexpected, []);
  assert.equal(diagnostics.console.length, 3);
  for (const message of diagnostics.console) assert.match(message, /^Failed to load resource: the server responded with a status of 400 /);
  console.log('editor-file-safety: ok (real file endpoints, 1440/720 px, light/dark)');
} catch (error) {
  console.error(JSON.stringify({ diagnostics, requests, stderr }));
  throw error;
} finally {
  if (browser) await browser.close();
  server.kill();
  await exited;
  await fs.rm(directory, { recursive: true, force: true });
}
