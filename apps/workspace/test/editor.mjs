import { openConnectionPicker, chooseConnection, openAccountSheet, closeAccountSheet } from "./chrome-helpers.mjs";
// Query editor: running SQL in the tab's session, results, Messages, Stop,
// row limit and Load more, transactions, confirmations, export, history, the
// schema browser, a session that ended, and restore. The packaged page and
// the real Python server (session autosave, profiles); every /api/editor/*
// request and the schema browser's reads are answered by this test, so no
// database is needed. Run after `npm run build`: node test/editor.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-"));
const shots = process.env.EDITOR_SHOTS || directory;
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

// ---- The mocked editor API ---------------------------------------------------
let runN = 0;
function session(over = {}) {
  return {
    schema_version: "editor-session-1.0",
    tab_id: "1",
    state: "idle",
    connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
    engine: "mysql",
    server_version: "8.4.3",
    connection_id: 42,
    autocommit: true,
    in_transaction: false,
    transaction_source: "performance_schema",
    transaction_note: null,
    sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
    backslash_escapes: true,
    safe_updates: true,
    safe_updates_default: true,
    read_only_transactions: false,
    started_at: "2026-10-15T12:00:00+00:00",
    idle_seconds: 0,
    idle_timeout_seconds: 1800,
    ended: false,
    ended_reason: null,
    message: null,
    notes: [],
    run: null,
    ...over,
  };
}
function statement(index, text, start, over = {}) {
  return { index, text, start, end: start + text.length, verb: text.split(/\s+/)[0].toLowerCase(), kind: "read", category: "read", status: "ok", duration_ms: 3, ...over };
}
function rows(statementIndex, columns, data, over = {}) {
  return {
    type: "rows",
    statement: statementIndex,
    columns: columns.map((c) => (typeof c === "string" ? { name: c, hex: false, type: null } : c)),
    columns_known: columns.length > 0,
    rows: data,
    row_count: data.length,
    truncated: false,
    limit: 1000,
    stopped: null,
    label: null,
    server_rows: data.length,
    warnings: 0,
    duration_ms: 3,
    ...over,
  };
}
function run(statements, results, over = {}) {
  const errors = results.filter((r) => r.type === "error" && !r.interrupted);
  return {
    schema_version: "editor-run-1.0",
    id: `run-${++runN}`,
    kind: "run",
    state: errors.length ? "error" : "complete",
    scope: "selection",
    started_at: "2026-10-15T12:00:00+00:00",
    finished_at: "2026-10-15T12:00:00+00:00",
    duration_ms: 9,
    limit: 1000,
    statements,
    results,
    error: errors[0] || null,
    stopped: null,
    message: null,
    counts: {
      statements: statements.length,
      run: statements.filter((s) => s.status !== "not_run").length,
      rows: results.filter((r) => r.type === "rows").reduce((a, r) => a + r.row_count, 0),
      affected_rows: results.filter((r) => r.type === "ok").reduce((a, r) => a + (r.affected_rows || 0), 0),
      warnings: 0,
      errors: errors.length,
    },
    audit_id: null,
    history_id: null,
    ...over,
  };
}
/** What each editor endpoint answers: a function of the request body. */
const mock = {
  run: [], // queued replies for editor/run (else `runDefault`)
  status: [], // queued replies for editor/status with a run_id
  stopReply: null,
  commit: [],
  rollback: [],
  close: [],
  open: [],
  history: { items: [] },
  holdStatus: false,
  holdRun: false,
  holdTable: false,
  privilegeActions: {},
  preview: null,
  holdPreview: false,
  holdProbe: false,
  probe: [],
};
const seen = [];
const calls = (p) => seen.filter((s) => s.path === p).map((s) => s.body);
function reply(p, body) {
  switch (p) {
    case "/api/editor/preview":
      if (mock.holdPreview) return null;
      return mock.preview ? mock.preview(body) : previewReply("unknown");
    case "/api/live/privileges":
      return { schema_version: "privileges-1.0", account: `${body.connection.user}@%`, grants: ["GRANT SELECT ON testdb.* TO a@%"], current_roles: [{ name: "reader", host: "%", active: true }], actions: mock.privilegeActions };
    case "/api/editor/status":
      if (!body.run_id) {
        if (mock.holdProbe) return null; // a restore probe answered later by the test
        return mock.probe.shift() || { status: "none", session: null, run: null };
      }
      if (mock.holdStatus) return null; // answered later by the test
      return mock.status.shift() || { status: "idle", session: session(), run: null };
    case "/api/editor/run":
      if (mock.holdRun) return null;
      return mock.run.shift() || { status: "complete", session: session(), run: run([statement(0, body.sql, 0)], [rows(0, ["1"], [["1"]])]) };
    case "/api/editor/stop":
      return mock.stopReply || { stopped: true, stop_requested: true, message: "KILL QUERY sent.", session: session({ state: "running" }) };
    case "/api/editor/commit":
      return mock.commit.shift();
    case "/api/editor/rollback":
      return mock.rollback.shift();
    case "/api/editor/close":
      return mock.close.shift() || { closed: true, session: null };
    case "/api/editor/open":
      return mock.open.shift() || { status: "idle", session: session() };
    case "/api/editor/history":
      return body.action === "clear" ? { cleared: mock.history.items.length } : mock.history;
    case "/api/editor/export":
      return { format: body.format, content: "a,b\r\n", mime: "text/csv", filename: `myflames-result.${body.format}` };
    case "/api/live/schemas":
      return {
        schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.3", taken_at: "x", uptime_s: 1, performance_schema: true,
        schemas: [{ name: "testdb", tables: 2, system: false }, { name: "mysql", tables: 38, system: true }],
        user_tables: 2, all_limit: 2000, all_note: "", system: ["mysql"],
      };
    case "/api/live/catalog":
      if (body.table && mock.holdTable) return null;
      if (!body.table) {
        const names = ["orders", "users", "archive_history", "%_literal"];
        const filtered = body.prefix ? names.filter((name) => name.startsWith(body.prefix)) : null;
        return { schema_version: "catalog-1.0", level: "tables", schema: body.schema,
          truncated: !filtered && !body.after, limit: 200, prefix: body.prefix || null, after: body.after || null,
          next_after: !filtered && !body.after ? "orders" : null,
          tables: (filtered || (body.after ? ["users"] : ["orders"])).map((name) => ({ name, type: "table" })) };
      }
      return { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table,
        columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false },
          { name: "status", type: "varchar(20)", nullable: true, key: "MUL", default: null, extra: "", generated: false }],
        indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }] };
  }
  return undefined;
}
function previewReply(state, warning = "") {
  const missing = state === "denied" ? ["SYSTEM_VARIABLES_ADMIN", "SUPER"] : [];
  const reason = state === "denied" ? "Needs SYSTEM_VARIABLES_ADMIN or SUPER; app@% has neither." :
    state === "allowed" ? "Allowed through SYSTEM_VARIABLES_ADMIN." : "This session's privileges are not verified.";
  return { status: "ready", statements: [{ index: 0, kind: "risky", action_keys: ["set_global"], state, missing, reason }],
    aggregate: { state, missing, reason, warning }, policy: { action: "confirm", text: "Review before running." } };
}
const heldStatus = [];
const held = [];
/** Answer a held request (it may have been aborted by the page meanwhile). */
function answerHeld(entry, body, status = 200) {
  try {
    void entry.request.respond({ status, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
  } catch {
    /* the page gave up on it */
  }
}

let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    server.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/editor/") && p !== "/api/live/schemas" && p !== "/api/live/catalog" && p !== "/api/live/privileges") {
      if (p.startsWith("/api/live/") || p.startsWith("/api/connection/")) {
        // Recorded, so a test can prove nothing was captured.
        seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
        return request.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
      }
      return request.continue();
    }
    const body = JSON.parse(request.postData() || "{}");
    seen.push({ path: p, body });
    const answer = reply(p, body);
    if (answer === null) return void (p === "/api/editor/status" && body.run_id ? heldStatus : held).push({ path: p, body, request });
    if (answer === undefined) return request.respond({ status: 500, contentType: "application/json", body: '{"error":"no mock"}' });
    if (answer.http) return request.respond({ status: answer.http, contentType: "application/json", body: JSON.stringify({ error: answer.error }) });
    request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: directory });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  const EDITOR = '[aria-label="SQL editor"]';
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const texts = (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent));
  async function chord(key, shift = false) {
    await page.keyboard.down(MOD);
    if (shift) await page.keyboard.down("Shift");
    await page.keyboard.press(key);
    if (shift) await page.keyboard.up("Shift");
    await page.keyboard.up(MOD);
  }
  async function select(start, end = start) {
    await page.$eval(EDITOR, (el, start, end) => {
      el.focus();
      el.setSelectionRange(start, end);
    }, start, end);
  }
  async function setText(sql) {
    if (sql) return void (await page.locator(EDITOR).fill(sql));
    await page.$eval(EDITOR, (el) => {
      el.focus();
      el.select();
    });
    await page.keyboard.press("Backspace");
  }
  async function until(check, message, timeout = 8000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = await check();
      if (value) return value;
      await pause(25);
    }
    assert.fail(message);
  }
  const ran = async (n) => until(() => calls("/api/editor/run").length >= n && calls("/api/editor/run")[n - 1], `editor/run #${n} was not sent`);
  const idle = () => page.waitForFunction(() => !document.querySelector(".results-running") && !document.querySelector("[data-action=run]")?.disabled);
  const resultTabs = () => texts(".results-tabs [role=tab]");
  /** Unverified privileges add no note (the server decides); each action
   * keeps its reason in its accessible description. */
  async function quietUnverified() {
    await page.waitForFunction(() => document.querySelector("[data-action=run]")?.dataset.privilegeState === "unknown");
    await pause(450);
    assert.equal(await page.$(".editor-privilege-note"), null, "an unverified statement shows no note");
    for (const action of ["run", "run-all"])
      assert.match(await page.$eval(`[data-action=${action}]`, e => document.getElementById(e.getAttribute("aria-describedby").split(" ")[0])?.textContent || ""), /^Unverified/);
  }
  async function sharedPrivilegeNotice(expected) {
    await page.waitForSelector(".editor-privilege-note", { visible: true });
    assert.equal(await page.$$eval(".editor-privilege-note", els => els.length), 1);
    // In the toolbar row: a late answer never pushes the editor down.
    assert.equal(await page.$eval(".editor-privilege-note", e => !!e.closest(".editor-toolbar")), true);
    assert.equal(await text(".editor-privilege-note"), expected);
    for (const action of ["run", "run-all", "explain", "analyze"]) {
      const selector = `[data-action=${action}]`;
      assert.doesNotMatch(await text(selector), /Unverified/);
      assert.equal(await page.$eval(selector, e => e.querySelector(".privilege-unverified")), null);
      assert.equal(await page.$eval(selector, e => {
        const id = e.getAttribute("aria-describedby").split(" ")[0];
        const note = document.getElementById(id);
        return note?.matches(".editor-privilege-note") && note.getClientRects().length > 0;
      }), true, "the visible explanation also describes each action");
    }
  }
  async function onePhoneStatusBar() {
    assert.equal(await page.$(".editor-statusbar"), null, "the editor has no status bar of its own");
    assert.deepEqual(await page.$$eval(".window-statusbar", els => els
      .filter(e => e.getClientRects().length > 0).map(e => e.getAttribute("aria-label"))), ["Workspace status"]);
  }

  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  assert.equal(await page.$eval("#query-editor-title", (e) => e.classList.contains("visually-hidden")), true, "the top bar supplies the visible title");
  await page.waitForSelector(".query-lab .editor-toolbar");
  assert.equal(await page.$(".editor-heading, .editor-title"), null, "the editor has no duplicate heading or title row");
  assert.equal(await page.$('[aria-label="Query name"]'), null, "query names edit inline on demand");
  const analysisOptions = '[aria-label="Explain and Run analysis options"]';
  const runOptions = 'button[aria-label="Run options"]';
  assert.equal(await page.$eval(`button${analysisOptions}`, (e) => e.getAttribute("aria-expanded")), "false");
  await page.click(`button${analysisOptions}`);
  await page.waitForSelector(`[role=dialog]${analysisOptions}:not([hidden])`);
  assert.ok(await page.$(`[role=dialog]${analysisOptions} .plan-options`), "analysis settings belong to the popover");
  await page.keyboard.press("Escape");
  assert.equal(await page.$eval(`button${analysisOptions}`, (e) => e === document.activeElement && e.getAttribute("aria-expanded") === "false"), true, "Escape closes options and restores focus");
  await page.click(runOptions);
  assert.ok(await page.$('[role=dialog][aria-label="Run options"]:not([hidden]) [aria-label="Row limit"]'));
  await page.click(`button${analysisOptions}`);
  assert.equal(await page.$eval(runOptions, (e) => e.getAttribute("aria-expanded")), "false", "only one options popover stays open");
  // Click the editor beside the popover (it overlaps the editor's right half).
  await page.click(EDITOR, { offset: { x: 24, y: 24 } });
  assert.equal(await page.$eval(`button${analysisOptions}`, (e) => e.getAttribute("aria-expanded")), "false", "an outside click dismisses options");
  await page.focus('.query-tablist [role=tab][aria-selected=true]');
  await page.keyboard.press("F2");
  await page.locator('[aria-label="Query name"]').fill("Cancelled name");
  await page.keyboard.press("Escape");
  assert.equal(await text('.query-tablist [aria-selected=true]'), "Query 1", "Escape cancels a tab rename");
  // Opening the editor asks once whether the tab already has a session (a reload keeps it).
  await until(() => calls("/api/editor/status").length === 1, "no session probe");
  // No session yet: the window status bar says so; the editor shows no transaction state.
  const statusField = (name) => page.$eval(`.window-statusbar [data-status="${name}"]`, (e) => e.textContent).catch(() => null);
  await page.waitForFunction(() => document.querySelector('.window-statusbar [data-status="session"]')?.textContent === "No session");
  assert.equal(await page.$(".tx-state"), null);
  assert.equal(await page.$(".editor-statusbar"), null, "one status region: the window's");
  await quietUnverified();
  await setText("");
  assert.equal(await page.$(".editor-privilege-note"), null, "empty SQL has no preview note");

  // 1. Run: the statement at the cursor, the selection, or everything.
  const SQL = "SELECT 1;\nSELECT 2;\nSELECT 3";
  await setText(SQL);
  await select(SQL.indexOf("2"));
  // The statement at the cursor is tinted.
  await page.waitForFunction(() => [...document.querySelectorAll(".sql-highlight .sql-current")].map((e) => e.textContent).join("") === "SELECT 2");
  await chord("Enter");
  let body = await ran(1);
  assert.equal(body.tab_id, "1");
  assert.equal(body.sql, SQL);
  // Only the caret: the server chooses the statement with its own splitter.
  assert.equal(body.cursor, SQL.indexOf("2"));
  assert.equal(body.selection, undefined);
  assert.equal(body.limit, 1000);
  assert.equal(body.connection.host, "127.0.0.1");
  assert.equal(body.connection.type, "production");
  assert.equal(await page.$eval(EDITOR, (e) => e.value), SQL, "the shortcut inserted no text");
  await idle();
  await select(0, SQL.indexOf("SELECT 3"));
  await page.waitForFunction(() => document.querySelector("[data-action=run]")?.textContent.includes("Run selection"));
  await page.click("[data-action=run]");
  body = await ran(2);
  assert.deepEqual(body.selection, { start: 0, end: SQL.indexOf("SELECT 3") });
  await idle();
  await select(3);
  await chord("Enter", true);
  body = await ran(3);
  assert.equal(body.selection, undefined, "Run all sends the whole text");
  assert.equal(body.cursor, undefined);
  await idle();
  assert.equal(await text(".tx-state .status-pill"), "Auto-commit");
  // Server, session and last run are in the one window status bar; the
  // transaction state stays in the toolbar and rows and time in the result
  // tab, each said once.
  assert.equal(await statusField("engine"), "MySQL 8.4.3");
  assert.equal(await statusField("session"), "Session open · connection 42");
  assert.equal(await statusField("transaction"), null);
  assert.equal(await statusField("rows"), null);
  assert.equal(await statusField("duration"), null);
  assert.ok(await page.$('.window-statusbar [data-status="last-run"] time[datetime]'), "the last run includes its completion time");
  assert.equal(await page.$$eval('[aria-label="Workspace status"]', (els) => els.length), 1, "exactly one Workspace status region");

  // 2. Several result sets, one tab each, then Messages; NULL, '' and 'NULL'.
  mock.run.push({
    status: "complete",
    session: session(),
    run: run(
      [statement(0, "SELECT 1", 0), statement(1, "SELECT a, b, c", 10), statement(2, "UPDATE t SET a = 1 WHERE id = 2", 26, { kind: "write", verb: "update" })],
      [
        rows(0, ["one"], [["1"], ["2"]]),
        rows(1, ["a", "b", "c", { name: "data", hex: true, type: null }], [[null, "", "NULL", "0xCAFE"]], { duration_ms: 1200 }),
        { type: "ok", statement: 2, affected_rows: 3, info: "Rows matched: 3  Changed: 3  Warnings: 0", warnings: 0, duration_ms: 2 },
      ],
    ),
  });
  await page.click("[data-action=run-all]");
  await ran(4);
  await page.waitForFunction(() => document.querySelectorAll(".results-tabs [role=tab]").length === 3);
  assert.deepEqual(await resultTabs(), ["Result 1 · 2 rows · 3 ms", "Result 2 · 1 row · 1.20 s", "Messages"]);
  await page.click(".results-tabs [role=tab]:nth-child(2)");
  await page.waitForSelector(".result-table td[data-col=c0] .sql-null");
  assert.equal(await text(".result-table td[data-col=c0]"), "NULL");
  assert.ok(await page.$(".result-table td[data-col=c1] .sql-empty"), "an empty string is an empty cell");
  assert.equal(await page.$(".result-table td[data-col=c2] .sql-null"), null, "the string 'NULL' is text");
  assert.equal(await text(".result-table td[data-col=c2]"), "NULL");
  assert.match(await page.$eval(".result-table td[data-col=c3] .sql-hex", (e) => e.title), /2 bytes/);
  await page.click(".results-tabs [role=tab]:nth-child(3)");
  await page.waitForSelector(".message-row");
  const messages = await texts(".message-row .message-head");
  assert.equal(messages.length, 3);
  assert.match(messages[2], /Statement 3 · UPDATE.*3 rows affected · Rows matched: 3/);

  // Context menus return focus to the exact opener, including nested buttons.
  async function dismissMenuTo(opener) {
    await page.waitForSelector(".context-menu[role=menu]");
    await page.waitForFunction(() => document.activeElement?.getAttribute("role") === "menuitem");
    await page.keyboard.press("Escape");
    await page.waitForSelector(".context-menu", { hidden: true });
    await page.waitForFunction((el) => document.activeElement === el, {}, opener);
  }
  async function keyboardMenuTo(opener, key) {
    await opener.focus();
    if (key === "F10") await page.keyboard.down("Shift");
    try { await page.keyboard.press(key); }
    finally { if (key === "F10") await page.keyboard.up("Shift"); }
    await dismissMenuTo(opener);
  }
  await page.click(".results-tabs [role=tab]:nth-child(2)");
  const resultMore = await page.$('.result-table button[aria-label="More actions for row 1"]');
  await resultMore.click();
  await dismissMenuTo(resultMore);
  const resultRow = await page.$('.result-table tr[data-row-key="0"]');
  for (const key of ["F10", "ContextMenu"]) await keyboardMenuTo(resultRow, key);
  // Cells are normally reached through their row. Preserve a focused cell if
  // an assistive tool or grid navigation gives it focus before opening a menu.
  const resultCell = await page.$('.result-table tr[data-row-key="0"] td[data-col="c1"]');
  await resultCell.evaluate((el) => el.tabIndex = -1);
  for (const key of ["F10", "ContextMenu"]) await keyboardMenuTo(resultCell, key);
  await resultCell.evaluate((el) => el.removeAttribute("tabindex"));

  // 3. Export: the result's typed values, the binary choice, SQL with a table name.
  await page.click(".results-tabs [role=tab]:nth-child(2)");
  await page.locator(".result-export button ::-p-text(Export)").click();
  await page.locator(".popover button ::-p-text(CSV)").click();
  body = await until(() => calls("/api/editor/export")[0], "no CSV export");
  assert.equal(body.format, "csv");
  assert.deepEqual(body.result.rows, [[null, "", "NULL", "0xCAFE"]]);
  assert.deepEqual(body.result.columns.map((c) => [c.name, c.hex, c.binary]), [["a", false, false], ["b", false, false], ["c", false, false], ["data", true, false]]);
  assert.deepEqual(body.options, { header: true, formula_guard: false });
  assert.equal(JSON.stringify(body).includes("password"), false, "no credentials in an export");
  await page.locator(".result-export button ::-p-text(Export)").click();
  await page.waitForSelector(".popover .export-binary");
  await page.locator(".popover label ::-p-text(Treat data as binary)").click();
  await page.type(".popover .export-sql input", "orders_copy");
  await page.locator(".popover button ::-p-text(Download SQL)").click();
  body = await until(() => calls("/api/editor/export")[1], "no SQL export");
  assert.equal(body.format, "sql");
  assert.equal(body.table, "orders_copy");
  assert.equal(body.result.columns[3].binary, true, "marked binary only after the user chose it");
  await page.locator(".result-export button ::-p-text(Export)").click();
  await page.locator(".popover button ::-p-text(Copy as TSV)").click();
  body = await until(() => calls("/api/editor/export")[2], "no TSV export");
  assert.equal(body.format, "tsv");
  await page.waitForFunction(() => /copied|did not allow copying/.test(document.querySelector(".statusbar-note")?.textContent || ""));
  await page.setViewport({ width: 390, height: 844 });
  await onePhoneStatusBar();
  assert.equal(await page.$eval(".editor-feedback", e => e.getClientRects().length > 0 && !e.closest(".editor-statusbar")), true,
    "copy feedback stays visible outside the hidden status bar");
  assert.match(await text('.window-statusbar[aria-label="Workspace status"]'), /Session open|connection 42/);
  assert.ok(await page.$('.window-statusbar[aria-label="Workspace status"] [data-status="last-run"] time[datetime]'));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.setViewport({ width: 1440, height: 1000 });

  // 4. An error: Messages, the underline, and Go to line.
  const BAD = "SELECT 1;\nSELEC 2 FROM t";
  await setText(BAD);
  mock.run.push({
    status: "error",
    session: session(),
    run: run(
      [statement(0, "SELECT 1", 0), statement(1, "SELEC 2 FROM t", 10, { status: "error", verb: "selec" })],
      [rows(0, ["1"], [["1"]]), { type: "error", statement: 1, code: 1064, sqlstate: "42000", message: "You have an error in your SQL syntax; check the manual near 'SELEC 2 FROM t' at line 1", interrupted: false, duration_ms: 1, position: { offset: 10, line: 2, column: 1, exact: true } }],
    ),
  });
  await page.click("[data-action=run-all]");
  await ran(5);
  await page.waitForSelector(".message-error");
  assert.equal(await page.$eval(".results-tabs [aria-selected=true]", (e) => e.textContent), "Messages1", "an error opens Messages");
  assert.match(await text(".message-error"), /ERROR 1064 \(42000\): You have an error/);
  assert.equal(await text(".sql-highlight .sql-error-mark"), "SELEC");
  assert.ok(await page.$(".sql-gutter-lines .is-error"));
  await select(0);
  await page.locator(".message-error button ::-p-text(Go to line 2, column 1)").click();
  assert.deepEqual(await page.$eval(EDITOR, (e) => [e.selectionStart, e.selectionEnd, document.activeElement === e]), [10, 10, true]);

  // 5. Stop: KILL QUERY for the running statement; Escape stops too.
  await setText("SELECT SLEEP(10)");
  const running = run([statement(0, "SELECT SLEEP(10)", 0, { status: "running" })], [], { state: "running" });
  mock.run.push({ status: "running", session: session({ state: "running" }), run: running });
  mock.holdStatus = true;
  await chord("Enter");
  await ran(6);
  await page.waitForSelector(".results-running");
  assert.equal(await page.$eval("[data-action=stop]", (e) => e.disabled), false);
  await until(() => heldStatus.length > 0, "no status poll while running");
  await page.click("[data-action=stop]");
  body = await until(() => calls("/api/editor/stop")[0], "Stop sent nothing");
  assert.equal(body.tab_id, "1");
  assert.equal(body.connection.host, "127.0.0.1");
  mock.holdStatus = false;
  const stopped = run(
    [statement(0, "SELECT SLEEP(10)", 0, { status: "stopped" })],
    [{ type: "error", statement: 0, code: 1317, sqlstate: "70100", message: "Query execution was interrupted", interrupted: true, duration_ms: 900, position: null }],
    { id: running.id, state: "stopped", stopped: { by: "user", interrupted: true, message: "Stopped with KILL QUERY. It ended the statement and undid what that statement changed; an open transaction is still open." } },
  );
  for (const entry of heldStatus.splice(0)) answerHeld(entry, { status: "idle", session: session(), run: stopped });
  await idle();
  assert.match(await text(".messages"), /Stopped with KILL QUERY/);
  assert.match(await text(".message-row"), /Stopped/);
  // Escape in the editor stops a run as well.
  mock.run.push({ status: "running", session: session({ state: "running" }), run: { ...running, id: "run-esc" } });
  mock.holdStatus = true;
  await page.focus(EDITOR);
  await chord("Enter");
  await ran(7);
  await page.waitForSelector(".results-running");
  await page.focus(EDITOR);
  await page.keyboard.press("Escape");
  await until(() => calls("/api/editor/stop").length === 2, "Escape did not stop");
  mock.holdStatus = false;
  for (const entry of heldStatus.splice(0)) answerHeld(entry, { status: "idle", session: session(), run: { ...stopped, id: "run-esc" } });
  await idle();

  // 6. Row limit and Load more (it runs the statement again, with a note).
  await setText("SELECT * FROM orders");
  await page.click(runOptions);
  await page.$eval('[aria-label="Row limit"]', (input) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, "2");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.keyboard.press("Escape");
  await page.focus(EDITOR);
  mock.run.push({
    status: "complete",
    session: session(),
    run: run([statement(0, "SELECT * FROM orders", 0)], [rows(0, ["id"], [["1"], ["2"]], { stopped: "row_limit", label: "stopped after 2 rows", truncated: true, limit: 2 })]),
  });
  await chord("Enter");
  body = await ran(8);
  assert.equal(body.limit, 2);
  await page.waitForSelector(".result-cut");
  assert.equal(await text(".result-cut"), "stopped after 2 rows");
  mock.run.push({
    status: "complete",
    session: session(),
    run: run([statement(0, "SELECT * FROM orders", 0)], [rows(0, ["id"], [["1"], ["2"], ["3"], ["4"], ["5"]], { limit: 1002 })]),
  });
  await page.locator(".result-actions button ::-p-text(Load more)").click();
  body = await ran(9);
  assert.equal(body.limit, 1002);
  // The run's own text with that statement selected, in the same session.
  assert.equal(body.sql, "SELECT * FROM orders");
  assert.deepEqual(body.selection, { start: 0, end: 20 });
  await page.waitForFunction(() => /rows may differ/.test(document.querySelector(".result-note")?.textContent || ""));
  assert.equal((await page.$$(".result-table tbody tr[data-row-key]")).length, 5);

  // 7. Transactions: the indicator, Roll back, Commit, and closing a tab.
  mock.run.push({ status: "complete", session: session({ in_transaction: true, autocommit: true }), run: run([statement(0, "START TRANSACTION", 0, { kind: "write" })], [{ type: "ok", statement: 0, affected_rows: 0, info: null, warnings: 0, duration_ms: 1 }]) });
  await setText("START TRANSACTION");
  await chord("Enter");
  await ran(10);
  await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Transaction open");
  assert.ok(await page.$('.query-tabs .tab-transaction[aria-label="Transaction open"]'), "the tab shows its open transaction");
  const action = (verb) => ({ status: "complete", session: session({ in_transaction: false }), run: run([statement(0, verb, 0, { kind: verb === "COMMIT" ? "write" : "read" })], [{ type: "ok", statement: 0, affected_rows: 0, info: null, warnings: 0, duration_ms: 1 }], { kind: "action" }) });
  mock.rollback.push(action("ROLLBACK"));
  await page.click("[data-action=rollback]");
  await until(() => calls("/api/editor/rollback").length === 1, "Roll back sent nothing");
  await page.waitForFunction(() => document.querySelector(".statusbar-note")?.textContent === "Rolled back.");
  assert.equal(await text(".tx-state .status-pill"), "Auto-commit");
  assert.match(await text(".message-row"), /START TRANSACTION/, "an action keeps the run's results");
  // Commit on a Production connection asks first (the server says so).
  mock.run.push({ status: "complete", session: session({ in_transaction: true }), run: run([statement(0, "BEGIN", 0, { kind: "write" })], [{ type: "ok", statement: 0, affected_rows: 0, info: null, warnings: 0, duration_ms: 1 }]) });
  await setText("BEGIN");
  await chord("Enter");
  await ran(11);
  await page.waitForSelector("[data-action=commit]");
  const confirmation = (token, statements, over = {}) => ({
    token, expires_in: 300, title: "Production connection", text: "This is a Production connection, so myflames asks before every write.",
    tone: "primary", kind: "write", category: "transaction", reasons: [], warnings: [], in_transaction: true,
    statements, count: statements.length, connection_type: "production",
    connection: { name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "" }, ...over,
  });
  mock.commit.push({ status: "needs_confirmation", confirmation: confirmation("c1", ["/*myflames:editor*/ COMMIT"]), session: session({ in_transaction: true }) });
  mock.commit.push(action("COMMIT"));
  await page.click("[data-action=commit]");
  await page.waitForSelector(".editor-confirm");
  assert.match(await text(".editor-confirm h2"), /Commit the transaction on Local MySQL\?/);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel", "Cancel is focused first");
  await page.locator(".editor-confirm button ::-p-text(Commit)").click();
  await until(() => calls("/api/editor/commit").length === 2, "the confirmed Commit was not sent");
  assert.deepEqual([calls("/api/editor/commit")[1].confirmed, calls("/api/editor/commit")[1].confirm_token], [true, "c1"]);
  await page.waitForFunction(() => document.querySelector(".statusbar-note")?.textContent === "Committed.");
  // Closing a tab whose transaction is open asks Commit, Roll back or Cancel.
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  const second = await page.$eval(".query-tablist [aria-selected=true]", (e) => e.textContent);
  assert.equal(second, "Query 2");
  mock.run.push({ status: "complete", session: session({ tab_id: "2", in_transaction: null, transaction_note: "The transaction instrument is off." }), run: run([statement(0, "UPDATE t SET a = 1 WHERE id = 1", 0, { kind: "write" })], [{ type: "ok", statement: 0, affected_rows: 1, info: null, warnings: 0, duration_ms: 1 }]) });
  await setText("UPDATE t SET a = 1 WHERE id = 1");
  await chord("Enter");
  await ran(12);
  await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Transaction unknown");
  // The footer leaves the transaction to the toolbar, except in Query digest,
  // which hides the toolbar: an open or unknown transaction stays visible.
  assert.equal(await page.$('.window-statusbar[aria-label="Workspace status"] [data-status="transaction"]'), null);
  await page.click('.query-mode-switch [role=tab]:nth-child(2)');
  await page.waitForFunction(() => document.querySelector('.window-statusbar[aria-label="Workspace status"] [data-status="transaction"]')?.textContent === "Transaction unknown");
  await page.click('.query-mode-switch [role=tab]:nth-child(1)');
  await page.waitForFunction(() => !document.querySelector('.window-statusbar[aria-label="Workspace status"] [data-status="transaction"]') && !!document.querySelector(".tx-state"));
  mock.close.push({ closed: false, reason: "transaction_unknown", message: "A transaction may be open.", session: session({ in_transaction: null }) });
  mock.close.push({ closed: true, session: session({ ended: true, ended_reason: "closed", state: "ended" }) });
  await page.click(".query-tab:has([aria-selected=true]) .query-tab-close");
  await page.waitForSelector(".confirm-sheet");
  assert.match(await text(".confirm-sheet h2"), /Close Query 2 with a transaction that may be open\?/);
  await page.locator(".confirm-sheet button ::-p-text(Roll back and close)").click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);
  const closes = calls("/api/editor/close");
  assert.equal(closes.at(-1).discard, true);
  assert.equal(closes.at(-2).discard, undefined, "the first close never discards");

  // 8. A write on Production: the confirmation lists the statements and the
  // implicit commit; a token that expired asks again; Cancel is recorded as declined.
  const WRITE = "ALTER TABLE t ADD COLUMN x INT";
  await setText(WRITE);
  mock.run.push({ status: "needs_confirmation", session: session({ in_transaction: true }),
    confirmation: confirmation("t1", [WRITE], { tone: "danger", kind: "risky", category: "ddl", expires_in: 0,
      warnings: ["Statement 1 (alter) commits the open transaction first."], reasons: ["DDL changes the schema."] }) });
  mock.run.push({ status: "needs_confirmation", session: session({ in_transaction: true }),
    confirmation: confirmation("t2", [WRITE], { tone: "danger", kind: "risky", category: "ddl" }) });
  mock.run.push({ status: "declined", message: "Nothing was sent.", audit_id: "a1", session: session() });
  await chord("Enter");
  await page.waitForSelector(".editor-confirm .sheet-statements");
  assert.equal(await text(".editor-confirm .sheet-statements"), WRITE + ";");
  assert.match(await text(".editor-confirm .sheet-warnings"), /commits the open transaction first/);
  assert.match(await text(".editor-confirm .eyebrow"), /ALTER/);
  await page.locator(".editor-confirm button ::-p-text(Run statement)").click();
  await page.waitForFunction(() => /expired/.test(document.querySelector(".editor-confirm .sheet-again")?.textContent || ""));
  let sent = calls("/api/editor/run");
  assert.deepEqual([sent.at(-1).confirmed, sent.at(-1).confirm_token], [true, "t1"]);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".editor-confirm"));
  await page.waitForFunction(() => /Nothing was sent/.test(document.querySelector(".statusbar-note")?.textContent || ""));
  sent = calls("/api/editor/run");
  assert.equal(sent.at(-1).decline, true, "Cancel tells the server, which records the decline");
  assert.equal(sent.at(-1).confirm_token, undefined);

  // Typed destructive target is exact, resets for each token, and travels with the run.
  await setText("DROP DATABASE Camel");
  mock.run.push({ status: "needs_confirmation", session: session(),
    confirmation: confirmation("drop1", ["DROP DATABASE Camel"], { typed_target: "Camel", tone: "danger" }) });
  mock.run.push({ status: "complete", session: session(), run: run([statement(0, "DROP DATABASE Camel", 0)], []) });
  await chord("Enter");
  const targetInput = '.editor-confirm input[aria-label="Database name to confirm DROP"]';
  await page.waitForSelector(targetInput);
  for (const width of [1440, 390]) {
    await page.setViewport({ width, height: 900 });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      // Let the sheet's entrance and the color transitions finish before the evidence shot.
      await new Promise((r) => setTimeout(r, 400));
      await page.screenshot({ path: path.join(shots, `drop-database-${width}-${scheme}.png`), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  // Keyboard: Tab from the sheet's first focus reaches the typed-name field.
  let reached = false;
  for (let i = 0; i < 6 && !reached; i++) {
    await page.keyboard.press("Tab");
    reached = await page.$eval(targetInput, (e) => e === document.activeElement);
  }
  assert.ok(reached, "Tab reaches the typed-name field");
  const disabledConfirm = () => page.$eval(".editor-confirm button.button.danger-solid", e => e.disabled);
  assert.equal(await disabledConfirm(), true);
  await page.type(targetInput, "camel");
  assert.equal(await disabledConfirm(), true);
  await page.click(targetInput, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.type(targetInput, "Camel");
  assert.equal(await disabledConfirm(), false);
  await page.locator(".editor-confirm button ::-p-text(Drop database Camel)").click();
  // The connection's policy is read again before sending, so wait for the request itself.
  await until(() => calls("/api/editor/run").some((c) => c.confirm_token === "drop1"), "the confirmed DROP was not sent");
  await idle();
  assert.equal(calls("/api/editor/run").at(-1).typed_target, "Camel");

  // ALTER review includes the guard, table evidence and an optional (never executed) rewrite.
  const guard = "/*myflames:editor*/ SET SESSION lock_wait_timeout = 5";
  const suggestion = WRITE + ", ALGORITHM=INPLACE, LOCK=NONE";
  await setText(WRITE);
  mock.run.push({ status: "needs_confirmation", session: session(),
    confirmation: confirmation("alter-review", [guard, WRITE], { tone: "danger", category: "ddl",
      alter_preflight: { schema: "app", table: "t", table_engine: "InnoDB", engine: "mysql", version: "8.4.11",
        approx_bytes: 123456, metadata_known: true, transactions_known: true, metadata_locks: [
          { connection_id: 81, lock_type: "SHARED_READ", status: "GRANTED" }],
        transactions: [{ connection_id: 81, age_s: 120, query: "SELECT * FROM t" }],
        unattributed_transactions: [], suggestion } }) });
  mock.run.push({ status: "declined", message: "Nothing was sent.", session: session() });
  await chord("Enter");
  await page.waitForSelector('[aria-label="ALTER TABLE preflight"]');
  assert.match(await text('[aria-label="ALTER TABLE preflight"]'), /123,456 bytes/);
  assert.match(await text('[aria-label="ALTER TABLE preflight"]'), /5 seconds/);
  assert.equal(await text('[aria-label="Statements that will be sent"]'), guard + ";\n\n" + WRITE + ";");
  assert.equal(await text('[aria-label="Suggested ALTER SQL"]'), suggestion + ";");
  for (const width of [1440, 390]) {
    await page.setViewport({ width, height: 900 });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      // Let the sheet's entrance and the color transitions finish before the evidence shot.
      await new Promise((r) => setTimeout(r, 400));
      await page.screenshot({ path: path.join(shots, `alter-review-${width}-${scheme}.png`), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  const runsBefore = calls("/api/editor/run").length;
  const declinedBefore = calls("/api/audit/declined").length;
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".editor-confirm"));
  assert.equal(calls("/api/editor/run").at(-1).decline, true);
  assert.equal(calls("/api/editor/run").length - runsBefore, 1, "one Cancel sends one decline");
  assert.equal(calls("/api/audit/declined").length, declinedBefore, "and no second decline record");

  // 9. History: the list, search, and click to insert at the cursor.
  mock.history.items = [
    { at: new Date().toISOString(), tab_id: "1", connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306 }, engine: "mysql", kind: "run", scope: "all",
      sql: "SELECT * FROM customers WHERE id = 7", sql_truncated: false, statements: 1, statements_run: 1, duration_ms: 4, rows: 1, affected_rows: 0, status: "complete", error_code: null },
    { at: new Date().toISOString(), tab_id: "1", connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306 }, engine: "mysql", kind: "run", scope: "all",
      sql: "DELETE FROM t", sql_truncated: false, statements: 1, statements_run: 1, duration_ms: 1, rows: 0, affected_rows: 0, status: "error", error_code: 1175 },
  ];
  await setText("");
  await page.click('button[aria-label="Show the run history"]');
  await page.waitForSelector(".history-item");
  assert.equal((await page.$$(".history-item")).length, 2);
  await page.type('.history-panel input[type=search]', "customers");
  await page.waitForFunction(() => document.querySelectorAll(".history-item").length === 1);
  await page.click(".history-item");
  await page.waitForFunction((sel) => document.querySelector(sel).value === "SELECT * FROM customers WHERE id = 7", {}, EDITOR);
  await page.click('button[aria-label="Hide the history"]');

  // 10. Schema browser: nothing is read until asked; insert names; SELECT … LIMIT 100 runs.
  assert.equal(seen.filter((s) => s.path === "/api/live/schemas").length, 0, "no read before Load schemas");
  await page.locator(".schema-browser button ::-p-text(Load schemas)").click();
  const treeRow = async (kind, label) => {
    await page.waitForFunction((kind, label) => [...document.querySelectorAll(`.tree-row.kind-${kind}`)].some((r) => r.querySelector(".tree-label")?.textContent === label), {}, kind, label);
    for (const handle of await page.$$(`.tree-row.kind-${kind}`))
      if ((await handle.$eval(".tree-label", (e) => e.textContent)) === label) return handle;
  };
  await (await treeRow("schema", "testdb")).click();
  await treeRow("status", "Showing the first 1 tables");
  const more = await treeRow("more", "Load more");
  await more.focus();
  await page.keyboard.press("Enter");
  const users = await treeRow("table", "users");
  assert.equal(calls("/api/live/catalog")[0].schema, "testdb");
  assert.equal(calls("/api/live/catalog")[1].after, "orders");
  assert.equal(calls("/api/live/catalog")[1].prefix, "");
  assert.equal(await page.$(".tree-row.kind-more"), null, "no continuation after the last page");
  assert.equal(await page.$eval(".schema-browser", e => /Showing the first/.test(e.textContent)), false, "the cap notice goes away on the last page");
  assert.equal((await page.$$(".tree-row.kind-table")).length, 2, "pages are appended");
  await setText("SELECT * FROM ");
  await select("SELECT * FROM ".length);
  // A double-click inserts the qualified name at the cursor (its two clicks leave the branch as it was).
  await users.click({ count: 2 });
  await page.waitForFunction((sel) => document.querySelector(sel).value === "SELECT * FROM testdb.users", {}, EDITOR);
  // Expanding the table lists its columns (keys and types) and indexes.
  await (await treeRow("table", "users")).click();
  await page.waitForSelector(".tree-row.kind-column");
  assert.match(await text(".tree-row.kind-column"), /idint · PK · not null/);
  const names = await page.$$eval(".tree-row.kind-column", (els) => els.map((row) => {
    const name = row.querySelector(".tree-label"), type = row.querySelector(".tree-meta");
    return { name: name.textContent, nameTitle: name.title, type: type.textContent, typeTitle: type.title, clipped: name.scrollWidth > name.clientWidth };
  }));
  assert.ok(names.every((n) => n.name === n.nameTitle && n.type === n.typeTitle), "full names and types remain in tooltips");
  assert.ok(names.every((n) => !n.clipped), "short column names take priority over types");
  // One line per column: the actions button shares the row and shows only on
  // the row being pointed at or focused.
  const columnRows = await page.$$eval(".tree-row.kind-column", (els) => els.map((row) => {
    const box = row.getBoundingClientRect(), more = row.querySelector(".row-more")?.getBoundingClientRect();
    return { height: box.height, inline: !!more && more.top >= box.top && more.bottom <= box.bottom + 0.5,
      shown: getComputedStyle(row.querySelector(".row-more")).visibility === "visible", selected: row.getAttribute("aria-selected") === "true" };
  }));
  assert.ok(columnRows.every((r) => r.height <= 30 && r.inline), `column rows stay on one line: ${JSON.stringify(columnRows)}`);
  assert.ok(columnRows.filter((r) => !r.selected).every((r) => !r.shown), "unfocused rows hide their actions button");
  assert.equal(calls("/api/live/catalog").at(-1).table, "users");
  const schemaOpener = await treeRow("table", "orders");
  const schemaMore = await schemaOpener.$('button[aria-label="More actions for orders"]');
  await schemaMore.click();
  await dismissMenuTo(schemaMore);
  for (const key of ["F10", "ContextMenu"]) await keyboardMenuTo(schemaOpener, key);
  await (await treeRow("table", "orders")).click({ button: "right" });
  await page.waitForSelector(".context-menu[role=menu]");
  assert.deepEqual(await texts(".context-menu [role=menuitem]"), ["Insert name", "SELECT * … LIMIT 100", "Show CREATE TABLE", "Edit table data…", "Open in Indexes", "Open in Tables"]);
  await page.locator(".context-menu [role=menuitem] ::-p-text(SELECT * … LIMIT 100)").click();
  body = await until(() => calls("/api/editor/run").find((b) => b.sql?.includes("LIMIT 100")), "SELECT … LIMIT 100 did not run");
  assert.equal(body.sql, "SELECT * FROM testdb.users;\n\nSELECT * FROM testdb.orders LIMIT 100;");
  assert.equal(body.cursor, body.sql.indexOf("SELECT * FROM testdb.orders"), "the caret on the added statement");
  await idle();
  // Hiding the pane keeps what it read.
  await page.click('.schema-browser button[aria-label="Hide the schema browser"]');
  await page.waitForFunction(() => document.querySelector(".schema-browser-slot")?.hidden === true);
  await page.click('.editor-toolbar button[aria-label="Show the schema browser"]');
  await page.waitForFunction(() => document.querySelector(".schema-browser-slot")?.hidden === false);
  assert.ok(await page.$(".tree-row.kind-column"), "the tree is still there");
  assert.equal(calls("/api/live/schemas").length, 1, "and nothing was read again");

  // A prefix is sent to the server, including literal SQL wildcard characters;
  // it can find a table outside the pages already loaded in the tree.
  const prefixField = '.schema-browser input[aria-label="Table name prefix"]';
  await page.type(prefixField, "archive");
  await treeRow("table", "archive_history");
  assert.equal(calls("/api/live/catalog").at(-1).prefix, "archive");
  assert.equal(calls("/api/live/catalog").at(-1).after, null, "a new prefix starts at the first page");
  assert.equal((await page.$$(".tree-row.kind-table")).length, 1);
  await page.click(prefixField, { clickCount: 3 });
  await page.keyboard.type("%_");
  await treeRow("table", "%_literal");
  assert.equal(calls("/api/live/catalog").at(-1).prefix, "%_");
  await page.click(prefixField, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await treeRow("table", "users");
  assert.equal((await page.$$(".tree-row.kind-table")).length, 2, "clearing the prefix restores all loaded pages");

  // 11. The session ended: what was lost, and Reconnect (never automatic).
  mock.run.push({ status: "session_ended", session: session({ ended: true, ended_reason: "connection_lost", state: "ended", message: "Session ended. Session settings were lost and any open transaction was rolled back." }),
    run: run([statement(0, "SELECT 1", 0, { status: "session_ended" })], [], { state: "session_ended", message: "Session ended." }) });
  await setText("SELECT 1");
  await chord("Enter");
  await page.waitForSelector('[data-banner="ended"]');
  assert.match(await text('[data-banner="ended"]'), /Session ended: the connection to the server was lost\. Session settings were lost and any open transaction was rolled back\./);
  const before = calls("/api/editor/run").length;
  await chord("Enter");
  await pause(150);
  assert.equal(calls("/api/editor/run").length, before, "no run on an ended session");
  assert.match(await text(".query-lab [role=alert]"), /Choose Reconnect/);
  await page.locator('[data-banner="ended"] button ::-p-text(Reconnect)').click();
  body = await until(() => calls("/api/editor/open").at(-1), "Reconnect sent nothing");
  assert.equal(body.tab_id, "1");
  await page.waitForFunction(() => !document.querySelector('[data-banner="ended"]'));

  // 12. Safe updates off on Production asks first (editor/open with a token).
  await page.click(runOptions);
  mock.open.push({ status: "needs_confirmation", session: session(), confirmation: confirmation("s1", ["SET SESSION sql_safe_updates = 0"]) });
  mock.open.push({ status: "idle", session: session({ safe_updates: false }) });
  await page.locator(".safe-updates input").click();
  await page.waitForSelector(".editor-confirm");
  assert.match(await text(".editor-confirm h2"), /Turn safe updates off/);
  await page.locator(".editor-confirm button ::-p-text(Turn safe updates off)").click();
  await until(() => calls("/api/editor/open").at(-1)?.confirm_token === "s1", "the confirmed open was not sent");
  assert.equal(calls("/api/editor/open").at(-1).safe_updates, false);
  await page.waitForFunction(() => document.querySelector(".safe-updates input")?.checked === false);

  // 13. Restore: the text, the caret, the row limit, the results tab and the
  // editor height come back; results and credentials never do.
  await setText("SELECT 'restored';\nSELECT 2");
  await select(5, 9);
  await page.focus('[aria-label="Height of the SQL editor"]');
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.click(".results-tabs [role=tab]:last-child");
  await page.focus(EDITOR);
  await page.waitForFunction(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}" });
    const lab = (await r.json()).state?.query_lab;
    const tab = lab?.tabs?.[0];
    return tab?.selection?.start === 5 && tab?.selection?.end === 9 && lab?.editor_layout?.height === 292;
  }, { timeout: 15000, polling: 250 });
  const stored = await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}" });
    return (await r.json()).state.query_lab;
  });
  assert.equal(stored.tabs[0].limit, 2);
  assert.equal(JSON.stringify(stored).includes("0xCAFE"), false, "no results are saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved", { timeout: 15000 });
  await page.reload({ waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");
  assert.equal(await page.$eval(EDITOR, (e) => e.value), "SELECT 'restored';\nSELECT 2");
  assert.deepEqual(await page.$eval(EDITOR, (e) => [e.selectionStart, e.selectionEnd]), [5, 9]);
  assert.equal(await page.$eval('[aria-label="Row limit"]', (e) => e.value), "2");
  assert.equal(await page.$eval(".sql-editor-wrap", (e) => e.style.height), "292px");
  assert.equal(await page.$(".result-table"), null, "results are not restored");

  // 14. Narrow window: no horizontal scroll; the schema browser opens as a sheet.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await onePhoneStatusBar();
  assert.equal(await page.$eval(".schema-browser-slot", (e) => e.hidden), true, "closed until asked for on a narrow window");
  await page.click('button[aria-label="Show the schema browser"]');
  await page.waitForFunction(() => document.querySelector(".schema-browser-slot")?.hidden === false);

  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: path.join(shots, "editor-390.png"), fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });

  // ---- Review gate regressions (2026-10-15) ----------------------------------
  const tabCount = () => page.$$eval(".query-tablist [role=tab]", (els) => els.length);
  async function newTab() {
    const n = await tabCount();
    await page.click('button[aria-label="New query tab"]');
    await page.waitForFunction((n) => document.querySelectorAll(".query-tablist [role=tab]").length === n + 1, {}, n);
    return page.$eval(".query-tablist [aria-selected=true]", (e) => e.textContent);
  }
  const tabIdNow = async () => calls("/api/editor/status").filter((b) => !b.run_id).at(-1).tab_id;

  // R1. Numbers sort exactly: BIGINT past 2^53 and long DECIMAL values.
  await newTab();
  mock.run.push({ status: "complete", session: session(), run: run([statement(0, "SELECT big, dec FROM n", 0)],
    [rows(0, ["big", "dec"], [["9007199254740992", "0.3"], ["-1.55", "-1.5"], ["9007199254740993", "0.30000000000000001"]])]) });
  await setText("SELECT big, dec FROM n");
  await chord("Enter");
  await page.waitForSelector(".result-table td[data-col=c0]");
  const column = (key) => page.$$eval(`.result-table tbody td[data-col=${key}]`, (els) => els.map((e) => e.textContent));
  await page.click(".result-table th[data-col=c0] .sort-button");
  assert.deepEqual(await column("c0"), ["9007199254740993", "9007199254740992", "-1.55"], "largest first, exactly");
  await page.click(".result-table th[data-col=c0] .sort-button");
  assert.deepEqual(await column("c0"), ["-1.55", "9007199254740992", "9007199254740993"]);
  await page.click(".result-table th[data-col=c1] .sort-button");
  assert.deepEqual(await column("c1"), ["0.30000000000000001", "0.3", "-1.5"]);

  // R2. Escape that ends an input method's composition does not stop a run.
  const imeRun = run([statement(0, "SELECT SLEEP(3)", 0, { status: "running" })], [], { state: "running", id: "run-ime" });
  mock.run.push({ status: "running", session: session({ state: "running" }), run: imeRun });
  mock.holdStatus = true;
  await setText("SELECT SLEEP(3)");
  const stopsBefore = calls("/api/editor/stop").length;
  await chord("Enter");
  await page.waitForSelector(".results-running");
  await page.$eval(EDITOR, (el) => el.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, isComposing: true })));
  await pause(150);
  assert.equal(calls("/api/editor/stop").length, stopsBefore, "Escape while composing is not Stop");

  // R3. A failed status read keeps the run active: Stop stays, polling retries.
  mock.holdStatus = false;
  mock.status.push({ http: 500, error: "Could not complete the request: try again." });
  mock.status.push({ status: "idle", session: session(), run: { ...imeRun, state: "complete", statements: [statement(0, "SELECT SLEEP(3)", 0)], results: [rows(0, ["s"], [["0"]])] } });
  for (const entry of heldStatus.splice(0)) answerHeld(entry, { error: "Server unavailable." }, 503);
  await page.waitForFunction(() => /Trying again/.test(document.querySelector(".statusbar-note")?.textContent || ""));
  assert.equal(await page.$eval("[data-action=stop]", (e) => e.disabled), false, "Stop stays while the state is unknown");
  await idle();
  assert.equal(await page.$eval("[data-action=stop]", (e) => e.disabled), true);

  // R4. Commit and close, with a COMMIT that is still running at first (polled).
  mock.run.push({ status: "complete", session: session({ in_transaction: true }), run: run([statement(0, "BEGIN", 0, { kind: "write" })], [{ type: "ok", statement: 0, affected_rows: 0, info: null, warnings: 0, duration_ms: 1 }]) });
  await setText("BEGIN");
  await chord("Enter");
  await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Transaction open");
  const tabsBefore = await tabCount();
  mock.close.push({ closed: false, reason: "transaction_open", message: "A transaction is open.", session: session({ in_transaction: true }) });
  mock.close.push({ closed: true, session: null });
  const commitRun = run([statement(0, "COMMIT", 0, { kind: "write", status: "running" })], [], { kind: "action", state: "running", id: "run-commit" });
  mock.commit.push({ status: "running", session: session({ in_transaction: true, state: "running" }), run: commitRun });
  mock.status.push({ status: "idle", session: session({ in_transaction: false }), run: { ...commitRun, state: "complete", statements: [statement(0, "COMMIT", 0, { kind: "write" })] } });
  await page.click(".query-tab:has([aria-selected=true]) .query-tab-close");
  await page.waitForSelector(".confirm-sheet");
  await page.locator(".confirm-sheet button ::-p-text(Commit and close)").click();
  await page.waitForFunction((n) => document.querySelectorAll(".query-tablist [role=tab]").length === n - 1, { timeout: 8000 }, tabsBefore);
  assert.equal(calls("/api/editor/close").at(-1).discard, undefined, "closed after the commit, not discarded");

  // R5. A restore probe cut short by a tab switch is asked again; a late
  // answer never replaces a run started meanwhile.
  mock.holdProbe = true;
  await newTab();
  const probeTab = await tabIdNow();
  await page.click(".query-tabs .query-tab:first-child [role=tab]");
  await page.waitForFunction(() => document.querySelector(".query-tabs .query-tab:first-child [role=tab]")?.getAttribute("aria-selected") === "true");
  await page.click(".query-tabs .query-tab:last-child [role=tab]");
  await until(() => calls("/api/editor/status").filter((b) => !b.run_id && b.tab_id === probeTab).length === 2, "the probe was not asked again");
  mock.holdProbe = false;
  const newRun = run([statement(0, "SELECT 9", 0, { status: "running" })], [], { state: "running", id: "run-new" });
  mock.run.push({ status: "running", session: session({ tab_id: probeTab, state: "running", connection_id: 55 }), run: newRun });
  mock.holdStatus = true;
  await setText("SELECT 9");
  await chord("Enter");
  await until(() => heldStatus.some((e) => e.body.run_id === "run-new"), "the new run is not polled");
  const late = held.filter((e) => e.body.tab_id === probeTab && !e.body.run_id).at(-1);
  answerHeld(late, { status: "running", session: session({ tab_id: probeTab, connection_id: 55 }),
    run: run([statement(0, "SELECT old", 0, { status: "running" })], [], { state: "running", id: "run-old" }) });
  await pause(300);
  assert.equal(calls("/api/editor/status").filter((b) => b.run_id === "run-old").length, 0, "the late probe started no poll");
  assert.ok(await page.$(".results-running"), "the new run is still followed");
  mock.holdStatus = false;
  for (const entry of heldStatus.splice(0))
    answerHeld(entry, { status: "idle", session: session({ connection_id: 55 }), run: { ...newRun, state: "complete", statements: [statement(0, "SELECT 9", 0)], results: [rows(0, ["9"], [["9"]])] } });
  await idle();
  assert.match((await texts(".results-tabs [role=tab]"))[0], /Result 1 · 1 row/);

  // R6. Two saved connections, A and B: Stop uses the connection the run was
  // sent with; Explain and Run analysis wait like Run; Load more only for the
  // session and connection the results came from.
  const saveProfile = (value) => page.evaluate(async (value) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify({ value }) });
    return (await r.json()).id;
  }, value);
  const A = await saveProfile({ name: "A", host: "127.0.0.1", port: 3306, user: "a" });
  const B = await saveProfile({ name: "B", host: "127.0.0.1", port: 3307, user: "b" });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await openConnectionPicker(page);
  await page.waitForFunction((B) => !!document.querySelector(`.window-toolbar .connection-picker option[value="${B}"]`), {}, B);
  // The open picker covers the tab strip's + button: close it first.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  await newTab();
  const abTab = await tabIdNow();
  await chooseConnection(page, A);

  // A table detail read on A must be aborted on A -> B -> A. Even a late
  // successful reply may not populate A's cache after returning to it.
  if (await page.$eval(".schema-browser-slot", e => e.hidden)) await page.click('button[aria-label="Show the schema browser"]');
  await page.locator(".schema-browser button ::-p-text(Load schemas)").click();
  const schemaA = await treeRow("schema", "testdb");
  if ((await schemaA.evaluate(e => e.getAttribute("aria-expanded"))) !== "true") await schemaA.click();
  const tableA = await treeRow("table", "orders");
  if ((await tableA.evaluate(e => e.getAttribute("aria-expanded"))) === "true") await tableA.click();
  mock.holdTable = true;
  await tableA.click();
  const oldTable = await until(() => held.find(e => e.path === "/api/live/catalog" && e.body.table === "orders"), "table detail was not requested");
  held.splice(held.indexOf(oldTable), 1);
  assert.equal(oldTable.body.connection.profile_id, A);
  await chooseConnection(page, B);
  assert.equal(await page.$(".tree-row.kind-column"), null, "no A columns on B");
  await chooseConnection(page, A);
  const staleTable = reply("/api/live/catalog", { ...oldTable.body, table: null });
  // Use a distinct column name to expose an accepted stale response.
  answerHeld(oldTable, { ...staleTable, level: "table", table: "orders", columns: [{ name: "stale_from_A", type: "int", nullable: true, default: null }], indexes: [] });
  await pause(100);
  assert.equal(await page.$eval(".schema-browser", e => e.textContent.includes("stale_from_A")), false);
  mock.holdTable = false;
  const returnedTable = await treeRow("table", "orders");
  if ((await returnedTable.evaluate(e => e.getAttribute("aria-expanded"))) === "true") await returnedTable.click();
  const beforeRetry = calls("/api/live/catalog").filter(b => b.table === "orders").length;
  await returnedTable.click();
  await until(() => calls("/api/live/catalog").filter(b => b.table === "orders").length === beforeRetry + 1, "aborted details could not be requested again");
  await page.waitForSelector(".tree-row.kind-column");
  assert.equal(await page.$eval(".schema-browser", e => e.textContent.includes("stale_from_A")), false);

  await setText("SELECT * FROM big");
  mock.holdRun = true;
  await chord("Enter");
  await until(() => held.some((e) => e.path === "/api/editor/run" && e.body.tab_id === abTab), "the run was not sent");
  await chooseConnection(page, B);
  await page.click("[data-action=stop]");
  body = await until(() => calls("/api/editor/stop").find((b) => b.tab_id === abTab), "Stop sent nothing");
  assert.equal(body.connection.profile_id, A, "Stop goes to the connection the run was sent with");
  mock.holdRun = false;
  const heldRun = held.find((e) => e.path === "/api/editor/run" && e.body.tab_id === abTab);
  held.splice(held.indexOf(heldRun), 1);
  answerHeld(heldRun, { status: "complete", session: session({ tab_id: abTab, connection_id: 77, connection: { profile_id: A, name: "A", host: "127.0.0.1", port: 3306, user: "a", type: "production", read_only: false } }),
    run: run([statement(0, "SELECT * FROM big", 0)], [rows(0, ["id"], [["1"], ["2"]], { stopped: "row_limit", label: "stopped after 2 rows", truncated: true, limit: 2 })]) });
  await page.waitForSelector('[data-banner="mismatch"]');
  assert.equal(await page.$(".result-actions button ::-p-text(Load more)"), null, "no Load more on another connection");
  const startsBefore = calls("/api/live/start").length;
  await page.locator("[data-action=explain]").click();
  await page.waitForFunction(() => /session is on A/.test(document.querySelector(".query-lab [role=alert]")?.textContent || ""));
  await page.focus(EDITOR);
  await chord("e", true);
  await pause(150);
  assert.equal(calls("/api/live/start").length, startsBefore, "Explain and Run analysis send nothing to B");
  await chooseConnection(page, A);
  await page.waitForFunction(() => !document.querySelector('[data-banner="mismatch"]'));
  await page.waitForSelector(".result-actions button ::-p-text(Load more)");
  await chooseConnection(page, B);
  mock.close.push({ closed: true, session: null });
  await page.locator('[data-banner="mismatch"] button ::-p-text(Close session)').click();
  await page.waitForFunction(() => !document.querySelector('[data-banner="mismatch"]'));
  await chooseConnection(page, A);
  await pause(150);
  assert.equal(await page.$(".result-actions button ::-p-text(Load more)"), null, "no Load more after the session closed");


  // Stage B: exact capture denials remain focusable, including the keyboard
  // paths. New-session write denials never stand in for this tab's roles.
  const refreshPrivileges = async () => {
    const before = calls("/api/live/privileges").length;
    await openAccountSheet(page);
    await page.locator('dialog.account-sheet button ::-p-text(Refresh privileges)').click();
    await until(() => calls("/api/live/privileges").length > before, "privileges did not refresh");
    await page.waitForFunction(() => [...document.querySelectorAll('dialog.account-sheet button')].some(b => b.textContent === "Refresh privileges" && !b.disabled));
    assert.match(await text('dialog.account-sheet'), /Connected as a@%.*Active roles: reader@%/s);
    assert.match(await text('dialog.account-sheet pre'), /GRANT SELECT ON testdb/);
    await closeAccountSheet(page);
  };
  const deniedAnswer = { state: "denied", grey_out: true, missing: ["TEST_GLOBAL_PRIVILEGE"], reason: "Definitive global denial." };
  mock.privilegeActions = { explain: deniedAnswer, run_analysis: deniedAnswer, set_global: deniedAnswer };
  await refreshPrivileges();
  await setText("SELECT 1");
  const beforeDeniedCapture = calls("/api/live/start").length;
  for (const action of ["explain", "analyze"]) {
    const selector = `[data-action=${action}]`;
    assert.equal(await page.$eval(selector, e => e.disabled), false, "privilege denial stays focusable");
    assert.equal(await page.$eval(selector, e => e.getAttribute("aria-disabled")), "true");
    await page.focus(selector);
    assert.equal(await page.$eval(selector, e => e === document.activeElement), true);
    assert.match(await page.$eval(selector, e => e.getAttribute("aria-describedby").split(" ").map(id => document.getElementById(id)?.textContent).join(" ")), /TEST_GLOBAL_PRIVILEGE/);
    await page.click(selector);
  }
  await page.focus(EDITOR);
  await chord("e");
  await chord("e", true);
  assert.equal(calls("/api/live/start").length, beforeDeniedCapture, "buttons and shortcuts send no denied capture");
  assert.equal((await text(".editor-privilege-note")).match(/TEST_GLOBAL_PRIVILEGE/g)?.length, 1,
    "identical capture denials appear once");
  mock.privilegeActions = { set_global: deniedAnswer };
  await refreshPrivileges();
  // Run's answer comes from the planned statements, independent of the
  // provider's generic action keys. Every denied button remains focusable.
  mock.preview = () => previewReply("denied");
  await setText("SET GLOBAL max_connections = 123");
  await select(0);
  await page.waitForFunction(() => ["run", "run-all"].every(a => document.querySelector(`[data-action=${a}]`)?.getAttribute("aria-disabled") === "true"));
  await sharedPrivilegeNotice(previewReply("denied").aggregate.reason);
  assert.equal(await page.$eval(".editor-privilege-note", e => e.classList.contains("is-warning")), true);
  await page.setViewport({ width: 390, height: 844 });
  await sharedPrivilegeNotice(previewReply("denied").aggregate.reason);
  await onePhoneStatusBar();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.setViewport({ width: 1440, height: 1000 });
  const beforeDeniedRun = calls("/api/editor/run").length;
  for (const action of ["run", "run-all"]) {
    const selector = `[data-action=${action}]`;
    assert.equal(await page.$eval(selector, e => e.disabled), false);
    assert.equal(await page.$eval(selector, e => e.classList.contains("primary")), false, "denied buttons use the shared grey appearance");
    await page.focus(selector);
    assert.equal(await page.$eval(selector, e => e === document.activeElement), true);
    assert.match(await page.$eval(selector, e => e.title), /SYSTEM_VARIABLES_ADMIN/);
    assert.match(await page.$eval(selector, e => document.getElementById(e.getAttribute("aria-describedby"))?.textContent), /SYSTEM_VARIABLES_ADMIN/);
    await page.keyboard.press("Enter");
    await page.click(selector);
  }
  await page.focus(EDITOR);
  await chord("Enter");
  await chord("Enter", true);
  assert.equal(calls("/api/editor/run").length, beforeDeniedRun, "neither buttons nor shortcuts bypass a denial");

  const mixedSql = "SELECT 1;\nSET GLOBAL max_connections = 123";
  const mixedWarning = `Some statements are denied: Statement 2: ${previewReply("denied").aggregate.reason}`;
  mock.preview = body => body.selection ? previewReply("denied") : body.cursor !== undefined ? previewReply("allowed") :
    previewReply("unknown", mixedWarning);
  await setText(mixedSql);
  await select(0);
  await page.waitForFunction(() => document.querySelector('[data-action=run]')?.dataset.privilegeState === "allowed");
  assert.equal(await page.$eval('[data-action=run-all]', e => e.getAttribute("aria-disabled")), "false");
  await sharedPrivilegeNotice(mixedWarning);
  await select(mixedSql.indexOf("SET"), mixedSql.length);
  await page.waitForFunction(() => document.querySelector('[data-action=run]')?.getAttribute("aria-disabled") === "true");
  assert.match(await text('[data-action=run]'), /Run selection/);
  await sharedPrivilegeNotice(mixedWarning);
  const scopedRequest = calls('/api/editor/preview').filter(b => b.selection).at(-1);
  assert.deepEqual(scopedRequest.selection, { start: mixedSql.indexOf("SET"), end: mixedSql.length });
  const mixedBefore = calls('/api/editor/run').length;
  await chord("Enter");
  assert.equal(calls('/api/editor/run').length, mixedBefore);
  await chord("Enter", true);
  await ran(mixedBefore + 1);
  await idle();

  // An old reply cannot disable different text, even if it arrives late.
  mock.holdPreview = true;
  await setText("SET GLOBAL max_connections = 124");
  await until(() => held.some(r => r.path === '/api/editor/preview' && r.body.sql.endsWith("124")), "preview was not debounced and sent");
  assert.equal(await page.$(".editor-privilege-note"), null, "pending previews do not show an unknown note");
  const oldPreviews = held.filter(r => r.path === '/api/editor/preview');
  mock.holdPreview = false;
  mock.preview = () => previewReply("allowed");
  await setText("SELECT 2");
  await page.waitForFunction(() => document.querySelector('[data-action=run]')?.dataset.privilegeState === "allowed");
  for (const entry of oldPreviews) answerHeld(entry, previewReply("denied"));
  await pause(100);
  assert.equal(await page.$eval('[data-action=run]', e => e.getAttribute("aria-disabled")), "false");
  assert.equal(await page.$(".editor-privilege-note"), null, "allowed Run stays quiet even when capture privileges are unknown");

  // An unknown Run all preview alone must not label an allowed current statement.
  mock.preview = body => previewReply(body.cursor !== undefined ? "allowed" : "unknown");
  await setText("SELECT 3; SELECT 4");
  await select(0);
  await page.waitForFunction(() => document.querySelector('[data-action=run]')?.dataset.privilegeState === "allowed");
  assert.equal(await page.$(".editor-privilege-note"), null);

  // The connection policy refuses a write independently of allowed grants.
  mock.preview = () => ({ ...previewReply("allowed"), policy: { action: "refuse", text: "This connection is read-only." } });
  await setText("SET GLOBAL max_connections = 125");
  await page.waitForFunction(() => document.querySelector('[data-action=run]')?.title.includes("read-only"));
  assert.equal(await page.$eval('[data-action=run]', e => e.getAttribute("aria-disabled")), "true");
  await sharedPrivilegeNotice("This connection is read-only.");
  mock.preview = null;
  // An unknown persistent session (including SET ROLE) stays runnable.
  for (const sql of ["SET ROLE NONE", "SET GLOBAL max_connections = 123"]) {
    await setText(sql);
    await page.waitForFunction(() => document.querySelector('[data-action=run]')?.dataset.privilegeState === "unknown");
    const before = calls("/api/editor/run").length;
    await chord("Enter");
    await ran(before + 1);
    await idle();
  }
  await quietUnverified();
  assert.equal(await page.$eval('[data-action=run]', e => e.getAttribute("aria-disabled")), "false");
  assert.equal(await page.$eval('[data-action=run]', e => e.classList.contains("primary")), true);
  assert.equal(await page.$eval('[data-action=run-all]', e => e.getAttribute("aria-disabled")), "false");
  // Missing object grants keep captures enabled; an access-denied response
  // invalidates the shared model without closing the editor session.
  mock.privilegeActions = {};
  await refreshPrivileges();
  assert.equal(await page.$eval("[data-action=explain]", e => e.getAttribute("aria-disabled")), "false");
  const beforeDenialRead = calls("/api/live/privileges").length;
  mock.run.push({ status: "error", session: session(), run: run([statement(0, "SELECT 1", 0, { status: "error" })], [], {
    state: "error", error: { type: "error", statement: 0, code: 1227, message: "Access denied", interrupted: false, position: null },
  }) });
  await setText("SELECT 1");
  await chord("Enter");
  await idle();
  await until(() => calls("/api/live/privileges").length > beforeDenialRead, "denial did not invalidate privileges");
  // Successful reconnect on the same endpoint refreshes the provider.
  mock.run.push({ status: "session_ended", session: session({ ended: true, ended_reason: "connection_lost", state: "ended" }),
    run: run([statement(0, "SELECT 1", 0, { status: "session_ended" })], [], { state: "session_ended" }) });
  await chord("Enter");
  await page.waitForSelector('[data-banner="ended"]');
  const beforeReconnect = calls("/api/live/privileges").length;
  await page.locator('[data-banner="ended"] button ::-p-text(Reconnect)').click();
  await until(() => calls("/api/live/privileges").length > beforeReconnect, "reconnect did not refresh privileges");

  assert.deepEqual(errors, []);
  console.log("Query editor checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
