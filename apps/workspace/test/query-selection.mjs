// Native Explain uses the editor session API; only Explore starts a capture.
// The page and workspace server are real. Database responses are fixtures.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-selection-"));
const PLAN = await fs.readFile(path.join(root, "test/mysql-explain-json-sample.json"), "utf8");
const server = spawn(process.env.PYTHON || "python3", ["-c",
  "from myflames.ui import WorkspaceServer; import myflames.ui as ui; import os,sys; from pathlib import Path; ui.ASSET_DIR=Path(os.environ.get('WORKSPACE_ASSETS', str(ui.ASSET_DIR))); s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
  path.join(directory, "workspace.sqlite3")],
{ cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } });
const seen = [];
const calls = p => seen.filter(x => x.path === p).map(x => x.body);
let currentSession = null, nativeText = "SELECT 1", nativeError = null, holdRun = false, heldRun;
let nativeTable = { columns: ['id', 'select_type'], rows: [['1', 'SIMPLE']] };
let runningNative = false, pendingNative = null, heldStatus;
const nativeJson = JSON.stringify({ query_block: { select_id: 1, message: 'No tables used' } }, null, 2);
const nativeTree = '-> Rows fetched before execution  (cost=0..0 rows=1)\n';
let browser;
const session = () => ({ schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
  connection: { host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
  engine: "mysql", server_version: "8.4.3", connection_id: 42, autocommit: true, in_transaction: false,
  sql_mode: "", backslash_escapes: true, safe_updates: true, safe_updates_default: true,
  read_only_transactions: false, ended: false, notes: [], run: null });
function nativeReply(body) {
  currentSession ||= session();
  // Let the request assertions diagnose a missing format rather than crashing
  // the fixture before the safety regression can be reported.
  const format = body.explain_format ?? 'table';
  const prefix = body.explain_format == null ? '' : format === "table" ? "EXPLAIN FORMAT=TRADITIONAL " : `EXPLAIN FORMAT=${format.toUpperCase()} `;
  const start = body.selection?.start ?? body.sql.indexOf(nativeText);
  // JSON and Tree are one textual EXPLAIN column, not traditional-plan columns.
  const data = format === 'table' ? nativeTable
    : { columns: ['EXPLAIN'], rows: [[format === 'json' ? nativeJson : nativeTree]] };
  const limit = body.limit ?? 1000;
  const rows = data.rows.slice(0, limit);
  const truncated = rows.length < data.rows.length;
  const result = nativeError ? { type: "error", statement: 0, code: 1064, sqlstate: "42000", message: nativeError, interrupted: false }
    : { type: "rows", statement: 0, columns: data.columns.map(name => ({ name, hex: false, type: null })),
      columns_known: true, rows, row_count: rows.length, truncated, limit,
      stopped: truncated ? 'row_limit' : null, label: truncated ? `stopped after ${limit} rows` : null,
      server_rows: data.rows.length, warnings: 0, duration_ms: 3 };
  return { status: "complete", session: currentSession, run: { schema_version: "editor-run-1.0", id: `run-${calls('/api/editor/run').length}`, kind: "run",
    state: nativeError ? "error" : "complete", scope: body.selection ? "selection" : "statement", duration_ms: 3,
    statements: [{ index: 0, text: nativeText, sent: prefix + nativeText, start, end: start + nativeText.length, verb: "EXPLAIN",
      kind: "read", category: "read", status: nativeError ? "error" : "ok", duration_ms: 3 }], results: [result], error: nativeError ? result : null,
    counts: { statements: 1, run: 1, rows: nativeError ? 0 : rows.length, errors: nativeError ? 1 : 0, affected_rows: 0, warnings: 0 } } };
}
function capture(body) {
  return { raw: PLAN, query: body.query, mode: body.mode, engine: "mysql", server_version: "8.4.3", captured_at: "2026-10-12T12:00:00Z",
    runs: [], measurements: { count: 1, median_ms: body.mode === "analyze" ? 3 : null, min_ms: 3, max_ms: 3, stdev_ms: 0 },
    schema: {}, table_stats: {}, variables: {}, trace: null, warnings: [], conditions: "" };
}
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", c => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    server.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [], diagnostics = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (["error", "warning"].includes(m.type())) diagnostics.push(m.text()); });
  page.on("requestfailed", r => { if (r.failure()?.errorText !== "net::ERR_ABORTED") diagnostics.push(r.failure()?.errorText); });
  await page.setRequestInterception(true);
  page.on("request", request => {
    const pathname = new URL(request.url()).pathname;
    const body = JSON.parse(request.postData() || "{}");
    const json = data => request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(data) });
    if (pathname.startsWith("/api/")) seen.push({ path: pathname, body });
    if (pathname === "/api/editor/status") {
      if (body.run_id && pendingNative) {
        assert.equal(body.run_id, pendingNative.run.id, 'poll follows the native Explain run');
        heldStatus = json;
        return;
      }
      return json({ status: currentSession ? "idle" : "none", session: currentSession, run: null });
    }
    if (pathname === "/api/editor/preview") return json({ status: "ok", statements: [], aggregate: { state: "unknown", missing: [], reason: "Unverified", warning: "" } });
    if (pathname === "/api/live/privileges") return json({ schema_version: "privileges-1.0", account: "app@%", engine: "mysql", version: "8.4.3", actions: {} });
    if (pathname === "/api/live/schemas") return json({ schemas: [{ name: "testdb", tables: 1, system: false }], user_tables: 1, system: [] });
    if (pathname === "/api/editor/run") {
      if (holdRun) { heldRun = () => json(nativeReply(body)); return; }
      if (runningNative) {
        pendingNative = nativeReply(body);
        currentSession = { ...currentSession, state: 'running' };
        pendingNative = { ...pendingNative, status: 'running', session: currentSession,
          run: { ...pendingNative.run, state: 'running', results: [],
            statements: pendingNative.run.statements.map(statement => ({ ...statement, status: 'running' })),
            counts: { ...pendingNative.run.counts, run: 0, rows: 0 } } };
        return json(pendingNative);
      }
      return json(nativeReply(body));
    }
    if (pathname === "/api/editor/stop") return json({ stopped: true, message: "KILL QUERY sent.", session: currentSession });
    if (pathname === "/api/live/start") return json({ id: `job-${calls(pathname).length}`, state: "queued" });
    if (pathname === "/api/live/status") return json({ id: body.id, state: "complete", progress: 1, repeat: 1, error: "", result: capture(calls('/api/live/start').at(-1)) });
    request.continue();
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  assert.equal(calls('/api/live/schemas').length, 0, "hidden editor reads no schemas");
  const editorPlace = () => page.locator(".main-nav button ::-p-text(Query editor)").click();
  await editorPlace();
  const EDITOR = '[aria-label="SQL editor"]';
  await page.waitForSelector('.tree-row.kind-schema');
  assert.equal(calls('/api/live/schemas').length, 1, "visible editor reads once");
  const select = (a, b = a) => page.$eval(EDITOR, (el, a, b) => { el.focus(); el.setSelectionRange(a, b); }, a, b);
  const idle = () => page.waitForFunction(() => !document.querySelector('.results-running') && document.querySelector('[data-action=explain]')?.getAttribute('aria-busy') !== 'true');
  const requested = (pathname, match = () => true) => page.waitForRequest(request =>
    new URL(request.url()).pathname === pathname && match(JSON.parse(request.postData() || '{}')), { timeout: 5000 });
  const sql = "SELECT 1;\nUPDATE t SET name = REPLACE(name, 'a', 'b');\nSELECT 3";
  await page.locator(EDITOR).fill(sql);
  nativeText = "UPDATE t SET name = REPLACE(name, 'a', 'b')";
  const at = sql.indexOf('UPDATE');
  await select(at, at + nativeText.length);
  holdRun = true;
  const firstRequest = requested('/api/editor/run');
  await page.click('[data-action=explain]');
  await firstRequest;
  await page.waitForFunction(() => document.querySelector('[data-action=explain]')?.textContent.includes('Explaining…'));
  assert.equal(typeof heldRun, 'function', 'interception has installed the pending reply');
  assert.equal(calls('/api/editor/run').length, 1);
  assert.equal(calls('/api/editor/run')[0].sql, sql);
  assert.deepEqual(calls('/api/editor/run')[0].selection, { start: at, end: at + nativeText.length });
  assert.equal(calls('/api/editor/run')[0].explain_format, 'table');
  await page.$eval('[data-action=explain]', e => e.click());
  assert.equal(calls('/api/editor/run').length, 1, "pending Explain cannot send twice");
  holdRun = false;
  await heldRun();
  await page.waitForSelector('.results-tabs [data-key="result:0"]');
  await idle();
  assert.match(await page.$eval('.results-tabs [data-key="result:0"]', e => e.textContent), /^Explain/);
  assert.equal(calls('/api/analysis/prepare-query').length, 0);
  assert.equal(calls('/api/live/start').length, 0);
  assert.equal(await page.$eval(EDITOR, e => e.value), sql, "DML stays exactly as typed");
  assert.equal(await page.$eval('.main-nav button[aria-current=page]', e => e.textContent.trim()), 'Query editor');

  // The normal Export consumes the Explain rows; its label wraps at both widths.
  for (const width of [1440, 720]) {
    await page.setViewport({ width, height: 1000 });
    await page.evaluate(() => document.fonts.ready);
    for (const theme of ['light', 'dark']) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      await page.click('button[aria-label="Export result 1"]');
      await page.waitForSelector('.export-guard', { visible: true });
      assert.equal(await page.$eval('.export-guard', e => getComputedStyle(e).whiteSpace), 'normal');
      assert.equal(await page.$eval('.export-guard', e => e.scrollWidth <= e.clientWidth + 1), true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(directory, `explain-${width}-${theme}.png`) });
      await page.keyboard.press('Escape');
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.click('button[aria-label="Export result 1"]');
  const exported = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/export');
  await page.locator('.export-actions button ::-p-text(JSON)').click();
  assert.equal((await exported).status(), 200);
  assert.deepEqual(calls('/api/editor/export').at(-1).result.rows, [['1', 'SIMPLE']]);

  // Load more must explain the original UPDATE again, even after the viewer
  // chooses another format. Dropping explain_format would request the DML itself.
  const updateSql = 'SELECT 0;\nUPDATE a JOIN b USING (id) SET a.v = b.v;\nSELECT 9';
  nativeText = 'UPDATE a JOIN b USING (id) SET a.v = b.v';
  nativeTable = { columns: ['id', 'select_type', 'table'], rows: [['1', 'SIMPLE', 'a'], ['1', 'SIMPLE', 'b']] };
  const updateStart = updateSql.indexOf('UPDATE');
  await page.click('button[aria-label="Run options"]');
  await page.$eval('[aria-label="Row limit"]', input => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, '1');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await page.keyboard.press('Escape');
  await page.locator(EDITOR).fill(updateSql);
  await select(updateStart, updateStart + nativeText.length);
  const limitedReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/run');
  await page.click('[data-action=explain]');
  assert.equal((await (await limitedReply).json()).run.results[0].truncated, true);
  await page.waitForSelector('.result-cut');
  await idle();
  assert.equal(calls('/api/editor/run').at(-1).limit, 1);
  await page.select('[aria-label="Explain format"]', 'json');
  const beforeMore = calls('/api/editor/run').length;
  const moreReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/run');
  await page.locator('.result-actions button ::-p-text(Load more)').click();
  const loaded = await (await moreReply).json();
  assert.equal(calls('/api/editor/run').length, beforeMore + 1);
  const more = calls('/api/editor/run').at(-1);
  assert.equal(more.sql, updateSql);
  assert.deepEqual(more.selection, { start: updateStart, end: updateStart + nativeText.length });
  assert.equal(more.tab_id, calls('/api/editor/run')[0].tab_id);
  assert.equal(more.explain_format, 'table', 'Load more retains the result format, not the new viewer choice');
  assert.ok(more.limit > 1, 'Load more raises the result limit');
  assert.equal(loaded.run.statements[0].sent, 'EXPLAIN FORMAT=TRADITIONAL ' + nativeText);
  await page.waitForFunction(() => document.querySelector('.result-note')?.textContent.includes('Ran again') &&
    document.querySelector('.result-table tr[data-row-key="1"] td[data-col="c2"]')?.textContent === 'b');
  await idle();
  assert.equal(await page.$('.result-cut'), null);
  assert.match(await page.$eval('.results-tabs [data-key="result:0"]', e => e.textContent), /^Explain.*2 rows/);
  assert.equal(await page.$('.confirm-sheet'), null);
  assert.equal(calls('/api/live/start').length, 0);

  // Format persists per viewer. Keyboard Explain uses the server's cursor selection.
  await page.locator(EDITOR).fill(sql);
  await page.select('[aria-label="Explain format"]', 'json');
  nativeText = 'SELECT 3';
  const third = sql.indexOf(nativeText) + 3;
  await select(third);
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  async function chord(shift = false) {
    await page.keyboard.down(mac ? 'Meta' : 'Control');
    if (shift) await page.keyboard.down('Shift');
    await page.keyboard.press('e');
    if (shift) await page.keyboard.up('Shift');
    await page.keyboard.up(mac ? 'Meta' : 'Control');
  }
  const jsonReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/run');
  await chord();
  const jsonResult = (await (await jsonReply).json()).run.results[0];
  assert.deepEqual(jsonResult.columns.map(column => column.name), ['EXPLAIN']);
  assert.deepEqual(JSON.parse(jsonResult.rows[0][0]), { query_block: { select_id: 1, message: 'No tables used' } });
  await page.waitForFunction(() => document.querySelector('.result-table')?.textContent.includes('No tables used'));
  await idle();
  assert.equal(calls('/api/editor/run').at(-1).cursor, third);
  assert.equal(calls('/api/editor/run').at(-1).explain_format, 'json');
  assert.equal(await page.evaluate(() => localStorage.getItem('myflames.editor.explainFormat')), 'json');
  await page.select('[aria-label="Explain format"]', 'tree');
  const treeReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/run');
  await chord();
  const treeResult = (await (await treeReply).json()).run.results[0];
  assert.deepEqual(treeResult.columns.map(column => column.name), ['EXPLAIN']);
  assert.equal(treeResult.rows[0][0], nativeTree);
  await page.waitForFunction(() => document.querySelector('.result-table')?.textContent.includes('Rows fetched before execution'));
  await idle();
  assert.equal(calls('/api/editor/run').at(-1).explain_format, 'tree');

  // Explore is the only navigation. It uses the captured statement, even after editing.
  await page.locator(EDITOR).fill('SELECT 99');
  await page.locator('.results-tabs-actions button ::-p-text(Explore)').click();
  await page.waitForSelector('.plan-heading h1');
  assert.equal(calls('/api/live/start').length, 1);
  assert.equal(calls('/api/live/start')[0].query, 'SELECT 3');
  assert.equal(calls('/api/live/start')[0].session_tab, calls('/api/editor/run')[0].tab_id);
  assert.equal(calls('/api/analysis/prepare-query').length, 0);
  await editorPlace();

  // Server errors go to Messages. A :name with no value in Parameters is sent
  // as typed: nothing is invented for it.
  nativeText = 'SELECT :missing'; nativeError = "Syntax error near ':missing' at line 1";
  await page.locator(EDITOR).fill(nativeText);
  await page.click('[data-action=explain]');
  await page.waitForSelector('.message-error');
  assert.match(await page.$eval('.message-error', e => e.textContent), /ERROR 1064 \(42000\).*:missing/);
  assert.equal(calls('/api/editor/run').at(-1).sql, nativeText);
  assert.equal(calls('/api/editor/run').at(-1).parameters, undefined, 'empty Parameters add no values');
  nativeError = null;

  // A long-running Explain is polled and stays busy until the stopped state
  // arrives. A successful KILL request alone is not the terminal run outcome.
  nativeText = 'SELECT 1'; runningNative = true; heldStatus = undefined;
  await page.locator(EDITOR).fill(nativeText);
  const runningReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/run');
  const statusRequest = requested('/api/editor/status', body => !!body.run_id);
  await page.click('[data-action=explain]');
  const started = await (await runningReply).json();
  assert.equal(started.run.state, 'running');
  await statusRequest;
  assert.equal(typeof heldStatus, 'function', 'the native Explain status poll is pending');
  await page.waitForFunction(() => document.querySelector('[data-action=explain]')?.getAttribute('aria-busy') === 'true');
  assert.match(await page.$eval('.results-running', e => e.textContent), /Explaining/);
  const stopped = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/stop');
  await page.click('[data-action=stop]');
  await stopped;
  assert.equal(calls('/api/editor/stop').at(-1).tab_id, calls('/api/editor/run')[0].tab_id);
  assert.equal(await page.$eval('[data-action=explain]', e => e.getAttribute('aria-busy')), 'true',
    'Stop acknowledgement does not finish the Explain run');
  currentSession = { ...currentSession, state: 'idle' };
  const interrupted = { type: 'error', statement: 0, code: 1317, sqlstate: '70100',
    message: 'Query execution was interrupted', interrupted: true, duration_ms: 900, position: null };
  const stoppedRun = { ...pendingNative.run, state: 'stopped', results: [interrupted], error: null,
    statements: pendingNative.run.statements.map(statement => ({ ...statement, status: 'stopped' })),
    stopped: { by: 'user', interrupted: true, message: 'Stopped with KILL QUERY.' },
    counts: { ...pendingNative.run.counts, run: 1 } };
  const terminalReply = page.waitForResponse(r => new URL(r.url()).pathname === '/api/editor/status' &&
    JSON.parse(r.request().postData() || '{}').run_id === started.run.id);
  runningNative = false;
  await heldStatus({ status: 'idle', session: currentSession, run: stoppedRun });
  pendingNative = null;
  assert.equal((await (await terminalReply).json()).run.state, 'stopped');
  await page.waitForSelector('.message-row.is-stopped');
  await idle();
  assert.match(await page.$eval('.message-error', e => e.textContent), /^Stopped: Query execution was interrupted/);
  assert.match(await page.$eval('.messages', e => e.textContent), /Stopped with KILL QUERY/);
  assert.equal(await page.$eval('[data-action=explain]', e => e.disabled), false);
  assert.equal(await page.$eval('[data-action=stop]', e => e.disabled), true);

  // Analysis still scopes and binds parameters, and refuses several statements.
  await page.locator(EDITOR).fill(sql);
  await select(0, sql.length);
  await chord(true);
  await page.waitForFunction(() => document.querySelector('.editor-panel .error')?.textContent.includes('holds 3 statements'));
  assert.equal(calls('/api/live/start').length, 1);
  await page.click('button[aria-label="Run analysis options"]');
  await page.click('.editor-parameters summary');
  await page.locator('[aria-label="Query parameters"]').fill('{"c": 42}');
  const analysisSql = 'SELECT 0;\nSELECT :c;\nSELECT 2';
  await page.locator(EDITOR).fill(analysisSql);
  await select(analysisSql.indexOf('SELECT :c'), analysisSql.indexOf('SELECT :c') + 9);
  await chord(true);
  await page.waitForSelector('.confirm-sheet');
  assert.match(await page.$eval('.confirm-sheet', e => e.textContent), /runs the statement/);
  assert.equal(calls('/api/live/start').length, 1, "nothing executes before confirmation");
  await page.locator('.confirm-sheet button ::-p-text(Run analysis)').click();
  await page.waitForFunction(() => document.querySelector('.plan-pane')?.textContent.includes('Measured:'));
  assert.equal(calls('/api/live/start').at(-1).mode, 'analyze');
  assert.equal(calls('/api/live/start').at(-1).query, 'SELECT :c');
  assert.deepEqual(calls('/api/live/start').at(-1).parameters, { c: 42 });
  assert.equal(await page.$eval('.main-nav button[aria-current=page]', e => e.textContent.trim()), 'Query editor');
  assert.match(await page.$eval('.results-tabs [data-key=plan]', e => e.textContent), /Analysis/);
  await page.locator('.plan-pane button ::-p-text(Explore)').click();
  await page.waitForFunction(() => document.querySelector('.main-nav button[aria-current=page]')?.textContent.trim() === 'Plans');
  assert.equal(calls('/api/live/start').length, 2, "opening analysis never executes it again");
  await editorPlace();
  assert.equal(calls('/api/live/schemas').length, 1, "returning reuses the schema list");
  // Explore added a plan: the page asks before leaving until its save is
  // confirmed (beforeunload), so reload only after the session has saved.
  await page.waitForFunction(() => document.querySelector('.session-status')?.dataset.state === 'saved', { timeout: 20000 });
  await page.reload({ waitUntil: 'networkidle0' });
  await editorPlace();
  await page.waitForFunction(() => document.querySelector('[aria-label="Explain format"]')?.value === 'tree');
  assert.deepEqual(errors, []);
  assert.deepEqual(diagnostics, []);
  console.log('Native Explain and query selection passed. ' + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) { server.kill('SIGINT'); await once(server, 'exit'); }
}
