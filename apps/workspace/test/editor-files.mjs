import { chooseConnection, openConnectionPicker } from "./chrome-helpers.mjs";
import { setEditorText } from "./editor-helpers.mjs";
// The Query editor's SQL files and session options: Open SQL file (the tab
// menu and Cmd/Ctrl+O, a mocked chooser endpoint), the edited dot and the
// close prompt (Save / Don't save / Cancel), Save and Save as (Cmd/Ctrl+S,
// Shift+Cmd/Ctrl+S), Run options' On error: Continue summary, and the
// default-database picker (pre-filled from the schema catalog; USE through a
// mocked editor/use). The packaged page and the real Python server; every
// editor API is mocked and nothing may run. Run after `npm run build`:
// node test/editor-files.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-files-"));
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
    database_name: null,
    ...over,
  };
}
function statement(index, text, start, over = {}) {
  return { index, text, start, end: start + text.length, verb: text.split(/\s+/)[0].toLowerCase(), kind: "read", category: "read", status: "ok", duration_ms: 3, ...over };
}
function rowsResult(statementIndex, data) {
  return { type: "rows", statement: statementIndex, columns: [{ name: "1", hex: false, type: null }], columns_known: true,
    rows: data, row_count: data.length, truncated: false, limit: 1000, stopped: null, label: null, server_rows: data.length, warnings: 0, duration_ms: 3 };
}
function runReply(statements, results, over = {}) {
  const errors = results.filter((r) => r.type === "error");
  return {
    schema_version: "editor-run-1.0",
    id: `run-${++runN}`,
    kind: "run",
    state: errors.length ? "error" : "complete",
    scope: "all",
    started_at: "2026-11-09T12:00:00+00:00",
    finished_at: "2026-11-09T12:00:00+00:00",
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
      affected_rows: 0,
      warnings: 0,
      errors: errors.length,
    },
    audit_id: null,
    history_id: null,
    ...over,
  };
}

// Queued endpoint answers the tests set up.
const mock = {
  fileOpen: [],
  fileSave: [],
  close: [],
  use: [],
  run: [],
};
const seen = [];
const calls = (p) => seen.filter((s) => s.path === p).map((s) => s.body);
function reply(p, body) {
  switch (p) {
    case "/api/editor/preview":
      return { status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "This session's privileges are not verified.", warning: "" }, policy: { action: "allow", text: "" } };
    case "/api/live/privileges":
      return { schema_version: "privileges-1.0", account: "app@%", grants: [], current_roles: [], actions: {} };
    case "/api/live/readiness":
      // A connection chosen in the picker checks its readiness (connection readiness contract).
      return { engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.3", read_only: false, super_read_only: false,
        performance_schema: true, replica: false, group_member: false, galera_member: false, privileges: {}, actions: {},
        notes: [], checked_at: "2026-11-09T12:00:00Z" };
    case "/api/editor/status":
      return { status: "none", session: null, run: null };
    case "/api/editor/open":
      return { status: "idle", session: session() };
    case "/api/editor/history":
      return { items: [] };
    case "/api/editor/close":
      return mock.close.shift() || { closed: true, session: null };
    case "/api/editor/file/open":
      return mock.fileOpen.shift();
    case "/api/editor/file/save":
      return mock.fileSave.shift();
    case "/api/editor/use":
      return mock.use.shift() || { status: "idle", database: body.database, session: session({ database_name: body.database }) };
    case "/api/editor/run":
      return mock.run.shift() || { status: "complete", session: session(), run: runReply([statement(0, body.sql, 0)], [rowsResult(0, [["1"]])]) };
    case "/api/live/schemas":
      return {
        schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.3", taken_at: "x", uptime_s: 1, performance_schema: true,
        schemas: [{ name: "app", tables: 2, system: false }, { name: "shop", tables: 3, system: false }, { name: "mysql", tables: 38, system: true }],
        user_tables: 5, all_limit: 2000, all_note: "", system: ["mysql"],
      };
  }
  return undefined;
}

const EDITOR = '[aria-label="SQL editor"]';
const ACTIVE_TAB = ".query-tablist [role=tab][aria-selected=true]";
const DIRTY = `${ACTIVE_TAB} [title="Edited since the last save"]`;
const DATABASE = 'select[data-action="database"]';

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
  page.setDefaultTimeout(10000);
  const errors = [];
  const consoleMessages = [];
  const failedRequests = [];
  const badResponses = [];
  page.on("console", (m) => { if (["error", "warn"].includes(m.type())) consoleMessages.push(m.text()); });
  page.on("requestfailed", (r) => failedRequests.push(new URL(r.url()).pathname));
  page.on("response", (r) => { if (r.status() >= 400) badResponses.push([new URL(r.url()).pathname, r.status()]); });
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/")) return request.continue();
    // Sign-in and the session store are the real server's (a throwaway HOME);
    // every editor and live endpoint is mocked so nothing runs.
    if (!p.startsWith("/api/editor/") && !p.startsWith("/api/live/")) return request.continue();
    seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
    const answer = reply(p, JSON.parse(request.postData() || "{}"));
    if (answer === undefined || answer === null) {
      return request.respond({ status: 500, contentType: "application/json", body: '{"error":"no mock"}' });
    }
    request.respond({ status: answer.httpStatus || 200, contentType: "application/json", body: JSON.stringify(answer.body || answer) });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");

  const tabNames = () => page.$$eval(".query-tablist [role=tab] .query-tab-label", (els) => els.map((e) => e.textContent));
  const sql = () => page.$eval(EDITOR, (e) => e.value);
  async function setText(value) {
    await setEditorText(page, value);
  }
  async function press(key, { shift = false } = {}) {
    await page.$eval(EDITOR, (el) => el.focus());
    await page.keyboard.down(MOD);
    if (shift) await page.keyboard.down("Shift");
    await page.keyboard.press(key);
    if (shift) await page.keyboard.up("Shift");
    await page.keyboard.up(MOD);
  }

  // -- Open SQL file from the tab menu --------------------------------------
  mock.fileOpen.push({ cancelled: false, name: "report.sql", sql: "SELECT 7;", token: "tok1", warning: null });
  await page.locator(`${ACTIVE_TAB}`).click({ button: "right" });
  await page.locator('[role=menu].query-tab-menu button ::-p-text(Open SQL file…)').click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  assert.deepEqual(await tabNames(), ["Query 1", "report"], "the tab takes the file's name");
  assert.equal(await sql(), "SELECT 7;");
  assert.equal(await page.$(DIRTY), null, "a just-opened file is not edited");

  // -- The edited dot and the status line -----------------------------------
  await setText("SELECT 8;");
  await page.waitForSelector(DIRTY);
  await page.waitForFunction(() => (document.querySelector(".window-statusbar")?.textContent || "").includes("Edited"));

  // -- Cmd/Ctrl+S saves back to the file (the token, never a path) -----------
  mock.fileSave.push({ cancelled: false, token: "tok2", name: "report.sql", path: "/tmp/report.sql" });
  await press("s");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, `${ACTIVE_TAB} [title="Edited since the last save"]`);
  const saves = calls("/api/editor/file/save");
  assert.equal(saves.length, 1);
  assert.deepEqual(saves[0], { sql: "SELECT 8;", token: "tok1" }, "Save sends the token, never a path");
  assert.equal(await page.$(DIRTY), null);

  // -- Shift+Save asks where (Save as) ---------------------------------------
  await setText("SELECT 9;");
  await page.waitForSelector(DIRTY);
  mock.fileSave.push({ cancelled: false, token: "tok3", name: "copy.sql", path: "/tmp/copy.sql" });
  await press("s", { shift: true });
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, `${ACTIVE_TAB} [title="Edited since the last save"]`);
  assert.equal(calls("/api/editor/file/save")[1].save_as, true);
  assert.equal(calls("/api/editor/file/save")[1].name, "report.sql", "Save as suggests the file's name");

  // -- Closing an edited file tab asks first ---------------------------------
  await setText("SELECT 10;");
  await page.waitForSelector(DIRTY);
  await page.locator(`${ACTIVE_TAB} ~ .query-tab-close`).click();
  await page.waitForSelector('.confirm-sheet');
  const promptText = await page.$eval('.confirm-sheet', (e) => e.textContent);
  assert.match(promptText, /Save .* before closing/);
  await page.locator('.confirm-sheet button ::-p-text(Don\'t save)').click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);
  assert.deepEqual(await tabNames(), ["Query 1"], "Don't save closes without a save request");
  assert.equal(calls("/api/editor/file/save").length, 2);

  // -- Cmd/Ctrl+O opens the chooser too --------------------------------------
  mock.fileOpen.push({ cancelled: false, name: "bad.sql", sql: "SELECT;", token: "tok4", warning: "Some bytes were not valid UTF-8 and were replaced with the replacement character (U+FFFD); saving writes the replaced text." });
  const opensBefore = calls("/api/editor/file/open").length;
  await press("o");
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  assert.equal((await tabNames())[1], "bad");
  assert.equal(calls("/api/editor/file/open").length, opensBefore + 1, "the backend owns the chooser and the read");
  // Close it again without edits: no prompt.
  await page.locator(`${ACTIVE_TAB} ~ .query-tab-close`).click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);

  // -- On error: Continue ------------------------------------------------------
  await setText("SELECT 1;\nSELECT 2;\nSELECT 3;");
  await page.locator('button[aria-label="Run options"]').click();
  await page.locator('[role=group][aria-label="On error"] button ::-p-text(Continue)').click();
  await page.keyboard.press("Escape");
  const failed = { type: "error", statement: 1, code: 1146, sqlstate: "42S02", message: "Table 'test.nope' doesn't exist", interrupted: false, duration_ms: 2, position: null };
  mock.run.push({
    status: "error",
    session: session(),
    run: runReply(
      [statement(0, "SELECT 1;", 0), statement(1, "SELECT 2;", 10, { status: "error" }), statement(2, "SELECT 3;", 20)],
      [rowsResult(0, [["1"]]), failed, rowsResult(2, [["3"]])],
      { on_error: "continue" },
    ),
  });
  await page.$eval(EDITOR, (el) => el.focus());
  await page.keyboard.down(MOD);
  await page.keyboard.down("Shift");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Shift");
  await page.keyboard.up(MOD);
  await page.waitForSelector(".messages-summary");
  const summary = await page.$eval(".messages-summary", (e) => e.textContent);
  assert.match(summary, /3 statements: 2 succeeded, 1 failed/, `the Continue summary counts outcomes, got: ${summary}`);
  const runCalls = calls("/api/editor/run");
  assert.equal(runCalls[0].on_error, "continue", "the tab's On error choice goes with the run");
  const messages = await page.$eval(".message-list", (e) => e.textContent);
  assert.match(messages, /Table 'test\.nope' doesn't exist/, "Messages lists the failure");

  // -- The default database picker --------------------------------------------
  // The open session's database (from the run reply) fills the picker.
  await page.waitForSelector(DATABASE);
  assert.equal(await page.$eval(DATABASE, (e) => e.value), "", "no database yet");
  mock.run.push({
    status: "complete",
    session: session({ database_name: "app" }),
    run: runReply([statement(0, "SELECT 1;", 0)], [rowsResult(0, [["1"]])]),
  });
  await press("Enter");
  await page.waitForFunction(() => document.querySelector('select[data-action="database"]')?.value === "app");
  // Choosing another database sends USE in the tab's session.
  await page.select(DATABASE, "shop");
  await page.waitForFunction(() => document.querySelector('select[data-action="database"]')?.value === "shop");
  const uses = calls("/api/editor/use");
  assert.equal(uses.length, 1);
  assert.equal(uses[0].database, "shop");
  // The catalog pre-filled the options.
  const options = await page.$$eval(`${DATABASE} option`, (els) => els.map((e) => e.value));
  assert.ok(options.includes("app") && options.includes("shop") && options.includes("mysql"), `catalog options, got ${options}`);

  // Expired file tokens recover inside one save operation, including chooser
  // cancellation and a failed retry. No database or path is sent from the page.
  mock.fileOpen.push({ cancelled: false, name: "expired.sql", sql: "SELECT 7;", token: "expired", warning: null });
  await press("o");
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  await setText("SELECT 70;");
  const saveCount = calls("/api/editor/file/save").length;
  const expired = { httpStatus: 400, body: { error: "File binding expired", code: "unknown_token" } };
  mock.fileSave.push(expired, { cancelled: false, token: "renewed", name: "renewed.sql", path: "/tmp/renewed.sql" });
  await press("s");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, DIRTY);
  assert.deepEqual(calls("/api/editor/file/save").slice(saveCount), [
    { sql: "SELECT 70;", token: "expired" },
    { sql: "SELECT 70;", save_as: true, name: "expired.sql" },
  ], "expired token opens Save as exactly once");
  await page.waitForFunction(() => document.querySelector(".editor-feedback")?.textContent.includes("Saved to /tmp/renewed.sql"));
  await setText("SELECT 71;");
  mock.fileSave.push(expired, { cancelled: true });
  await press("s");
  await page.waitForFunction(() => document.querySelector(".editor-feedback")?.textContent.includes("Save cancelled"));
  assert.ok(await page.$(DIRTY), "cancelled recovery preserves edits");
  mock.fileSave.push(expired, { httpStatus: 400, body: { error: "Disk is full" } });
  await press("s");
  await page.waitForFunction(() => document.querySelector(".editor-feedback")?.textContent.includes("Disk is full"));
  assert.ok(await page.$(DIRTY), "failed recovery preserves edits");

  // Discard applies to this close attempt only. Cancelling the transaction
  // sheet or failing to close must leave the original saved SQL untouched.
  for (const engine of ["mysql", "mariadb"]) {
    mock.close.push({ closed: false, reason: "transaction_open", session: session({ engine, in_transaction: true }) });
    await page.locator(`${ACTIVE_TAB} ~ .query-tab-close`).click();
    await page.locator('.confirm-sheet button ::-p-text(Don\'t save)').click();
    await page.waitForFunction(() => document.querySelector(".confirm-sheet")?.textContent.includes("with a transaction open"));
    await page.locator('.confirm-sheet button ::-p-text(Cancel)').click();
    await page.waitForSelector(".confirm-sheet", { hidden: true });
    assert.ok(await page.$(DIRTY), `${engine}: cancelling transaction close keeps dirty state`);
  }
  mock.close.push({ httpStatus: 400, body: { error: "Close failed" } });
  await page.locator(`${ACTIVE_TAB} ~ .query-tab-close`).click();
  await page.locator('.confirm-sheet button ::-p-text(Don\'t save)').click();
  await page.waitForFunction(() => document.querySelector(".session-action-feedback")?.textContent.includes("Close failed") || document.querySelector(".query-lab")?.textContent.includes("Close failed"));
  assert.ok(await page.$(DIRTY), "failed close keeps dirty state");
  // Retry discard and Roll back: the file prompt must not reopen.
  mock.close.push({ closed: false, reason: "transaction_open", session: session({ in_transaction: true }) });
  await page.locator(`${ACTIVE_TAB} ~ .query-tab-close`).click();
  await page.locator('.confirm-sheet button ::-p-text(Don\'t save)').click();
  await page.waitForFunction(() => document.querySelector(".confirm-sheet")?.textContent.includes("with a transaction open"));
  await page.locator('.confirm-sheet button ::-p-text(Roll back and close)').click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);

  // Continue counts actual terminal statuses, including Stop before any
  // error and a lost connection whose outcome cannot be claimed as success.
  async function runAllReply(value, expected) {
    mock.run.push(value);
    const previous = calls("/api/editor/run").length;
    await press("Enter", { shift: true });
    await page.waitForFunction((expected) => document.querySelector(".messages-summary")?.textContent.includes(expected), {}, expected);
    assert.equal(calls("/api/editor/run").length, previous + 1);
  }
  const planned = [0, 1, 2, 3, 4].map((i) => statement(i, `SELECT ${i};`, i * 10, { status: i === 0 ? "ok" : i === 1 ? "error" : "not_run" }));
  await runAllReply({ status: "stopped", session: session(), run: runReply(planned, [rowsResult(0, [["0"]]), failed], { on_error: "continue", state: "stopped" }) },
    "5 statements: 1 succeeded, 1 failed, 3 not run");
  await runAllReply({ status: "stopped", session: session(), run: runReply([
    statement(0, "SELECT 1;", 0, { status: "stopped" }), statement(1, "SELECT 2;", 10, { status: "not_run" }),
  ], [], { on_error: "continue", state: "stopped" }) }, "2 statements: 0 succeeded, 0 failed, 1 not run, 1 stopped");
  await runAllReply({ status: "session_ended", session: session({ ended: true }), run: runReply([
    statement(0, "SELECT 1;", 0, { status: "session_ended" }), statement(1, "SELECT 2;", 10, { status: "not_run" }),
  ], [], { on_error: "continue", state: "session_ended" }) }, "2 statements: 0 succeeded, 0 failed, 1 not run, 1 outcome unknown");
  await page.locator('[data-banner="ended"] button ::-p-text(Reconnect)').click();
  await page.waitForSelector('[data-banner="ended"]', { hidden: true });
  for (const [engine, version] of [["mysql", "8.4.3"], ["mariadb", "10.11.14"]]) {
    const deadlock = { ...failed, code: 1213, sqlstate: "40001", message: "Deadlock found when trying to get lock; try restarting transaction" };
    await runAllReply({ status: "error", session: session({ engine, server_version: version, in_transaction: false }),
      run: runReply([statement(0, "SELECT 1;", 0, { status: "error" })], [{ ...deadlock, statement: 0 }], { on_error: "continue" }) }, "Error 1213: an InnoDB deadlock rolls back the victim's whole transaction");
    // Clear the preceding result so the next engine must produce its own summary.
    await runAllReply({ status: "error", session: session({ engine, server_version: version, in_transaction: true }),
      run: runReply([statement(0, "SELECT 1;", 0, { status: "error" })], [{ ...failed, code: 1205, statement: 0 }], { on_error: "continue" }) },
      "Error 1205: an InnoDB lock wait timeout rolls back the whole transaction only with innodb_rollback_on_timeout ON");
  }

  // A session on A cannot USE a database catalogued on B. A chosen None is
  // different from an old tab that inherits the saved connection's database.
  const saveProfile = (value) => page.evaluate(async (value) => {
    const auth = await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" });
    const { token } = await auth.json();
    const r = await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify({ value }) });
    return (await r.json()).id;
  }, value);
  const A = await saveProfile({ name: "A", host: "127.0.0.1", port: 3306, user: "a", database: "app" });
  const B = await saveProfile({ name: "B", host: "127.0.0.1", port: 3307, user: "b", database: "shop" });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await openConnectionPicker(page);
  await page.waitForFunction((id) => !!document.querySelector(`.window-toolbar select[aria-label="Connection"] option[value="${id}"]`), {}, B);
  await page.keyboard.press("Escape");
  await chooseConnection(page, B);
  await page.locator('[data-banner="mismatch"] button ::-p-text(Close session)').click();
  await page.waitForSelector('[data-banner="mismatch"]', { hidden: true });
  await chooseConnection(page, A);
  mock.run.push({ status: "complete", session: session({ database_name: "app", connection: { ...session().connection, name: "A", user: "a", profile_id: A } }),
    run: runReply([statement(0, "SELECT 1;", 0)], [rowsResult(0, [["1"]])]) });
  await press("Enter");
  await page.waitForFunction(() => document.querySelector('select[data-action="database"]')?.value === "app");
  await chooseConnection(page, B);
  const usesBeforeMismatch = calls("/api/editor/use").length;
  await page.select(DATABASE, "shop");
  await page.waitForFunction(() => document.querySelector(".editor-feedback")?.textContent.includes("This tab is still connected to A"));
  assert.equal(calls("/api/editor/use").length, usesBeforeMismatch, "database change cannot target the old server");
  assert.equal(await page.$eval(DATABASE, (e) => e.value), "app");
  await chooseConnection(page, A);
  await page.select(DATABASE, "");
  await page.waitForFunction(() => document.querySelector(".editor-feedback")?.textContent.includes("a new session starts with none"));
  await chooseConnection(page, B);
  await page.locator('[data-banner="mismatch"] button ::-p-text(Close session)').click();
  await page.waitForSelector('[data-banner="mismatch"]', { hidden: true });
  await chooseConnection(page, A);
  await page.waitForFunction(() => document.querySelector('select[data-action="database"]')?.value === "");
  // Wait for the real store's autosave, inspect its value, then restore it.
  await page.waitForFunction(async () => {
    const auth = await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" });
    const { token } = await auth.json();
    const r = await fetch("/api/session/load", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}" });
    const saved = await r.json();
    const lab = saved.state?.query_lab;
    return lab?.tabs.find((t) => t.id === lab.tab_id)?.database === "";
  });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(DATABASE, { visible: true });
  assert.equal(await page.$eval(DATABASE, (e) => e.value), "", "explicit None survives normalization and restore");
  const runBeforeNone = calls("/api/editor/run").length;
  const noneRun = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/editor/run");
  await press("Enter");
  await noneRun;
  await page.waitForSelector('.results-tabs [role=tab]');
  assert.equal(calls("/api/editor/run").length, runBeforeNone + 1);
  assert.equal(calls("/api/editor/run").at(-1).connection.database, "", "the next session does not inherit A's app database");

  // -- 720 px: the toolbar keeps every control ---------------------------------
  await page.setViewport({ width: 720, height: 520 });
  await page.waitForSelector(DATABASE);
  await page.waitForSelector('button[data-action="format"]');
  assert.equal(await page.$eval(DATABASE, (e) => e.value), "");
  for (const width of [1440, 720]) for (const theme of ["light", "dark"]) {
    await page.setViewport({ width, height: 900 });
    await page.evaluate(async (theme) => { document.documentElement.dataset.theme = theme; await document.fonts.ready; }, theme);
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    assert.ok(await page.$('button[data-action="format"]'));
    if (process.env.W2A_SHOTS) {
      await fs.mkdir(process.env.W2A_SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.W2A_SHOTS, `editor-files-${width}-${theme}.png`) });
    }
  }
  assert.deepEqual(badResponses, Array.from({ length: 5 }, (_, i) => [i === 4 ? "/api/editor/close" : "/api/editor/file/save", 400]));
  assert.deepEqual(failedRequests, []);
  assert.equal(consoleMessages.length, 5, `only the five intentional 400 responses: ${consoleMessages}`);
  assert.ok(consoleMessages.every((m) => /Failed to load resource.*400/.test(m)));

  assert.deepEqual(errors, [], "no page errors");
  console.log("editor-files: ok");
} finally {
  if (browser) await browser.close();
  server.kill();
  await fs.rm(directory, { recursive: true, force: true });
}
