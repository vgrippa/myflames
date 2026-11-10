// Core CodeMirror 6 Query editor checks in the real workspace page:
// 1. Per-tab undo isolation
// 2. Mod-[ Back navigation while the editor has focus
// 3. Shift-Mod-K deleteLine without opening dialog.command-palette
// 4. 1 MB document paste and 20-keystroke typing budget
// 5. IME composition via CDP without opening .sql-completion
// 6. Hidden mount line-height measurement after reload from Home
// 7. Refused .sql file drop on .cm-content
// 8. Multi-cursor Escape during an active run vs second Escape Stop
// Run after `npm run build`: node test/editor-core.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import {
  EDITOR,
  assertEditorText,
  editorUndo,
  selectEditor,
  setEditorText,
  waitForEditorText,
} from "./editor-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-core-"));
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const { MYFLAMES_VAULT_PASSPHRASE: _ignoredPassphrase, ...baseEnv } = process.env;
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import myflames.ui as ui; import os,sys; from pathlib import Path; ui.ASSET_DIR=Path(os.environ.get('WORKSPACE_ASSETS', str(ui.ASSET_DIR))); s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
  { cwd: root, env: { ...baseEnv, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

// ---- The mocked editor API (copied from test/editor.mjs) --------------------
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
function previewReply(state, warning = "") {
  const missing = state === "denied" ? ["SYSTEM_VARIABLES_ADMIN", "SUPER"] : [];
  const reason = state === "denied" ? "Needs SYSTEM_VARIABLES_ADMIN or SUPER; app@% has neither." :
    state === "allowed" ? "Allowed through SYSTEM_VARIABLES_ADMIN." : "This session's privileges are not verified.";
  return {
    status: "ready",
    statements: [{ index: 0, kind: "risky", action_keys: ["set_global"], state, missing, reason }],
    aggregate: { state, missing, reason, warning },
    policy: { action: "confirm", text: "Review before running." },
  };
}

const ddlFixture = JSON.parse(await fs.readFile(new URL("./fixtures/ddl-progress.json", import.meta.url), "utf8"));
const mock = {
  run: [],
  status: [],
  stopReply: null,
  schemas: [{ name: "testdb", tables: 2, system: false }, { name: "mysql", tables: 38, system: true }],
  commit: [],
  rollback: [],
  close: [],
  open: [],
  holdOpen: false,
  history: { items: [] },
  holdStatus: false,
  holdRun: false,
  holdTable: false,
  catalogColumns: null,
  privilegeActions: {},
  preview: null,
  holdPreview: false,
  holdProbe: false,
  probe: [],
  ddl: ddlFixture,
};
const seen = [];
const calls = (p) => seen.filter((s) => s.path === p).map((s) => s.body);

function reply(p, body) {
  switch (p) {
    case "/api/live/ddl-progress":
      return mock.ddl;
    case "/api/editor/preview":
      if (mock.holdPreview) return null;
      return mock.preview ? mock.preview(body) : previewReply("unknown");
    case "/api/live/privileges":
      return {
        schema_version: "privileges-1.0",
        account: `${body.connection?.user || "app"}@%`,
        grants: ["GRANT SELECT ON testdb.* TO a@%"],
        current_roles: [{ name: "reader", host: "%", active: true }],
        actions: mock.privilegeActions,
      };
    case "/api/editor/status":
      if (!body.run_id) {
        if (mock.holdProbe) return null;
        return mock.probe.shift() || { status: "none", session: null, run: null };
      }
      if (mock.holdStatus) return null;
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
      if (mock.holdOpen) return null;
      return mock.open.shift() || { status: "idle", session: session() };
    case "/api/editor/history":
      return body.action === "clear" ? { cleared: mock.history.items.length } : mock.history;
    case "/api/editor/export":
      return { format: body.format, content: "a,b\r\n", mime: "text/csv", filename: `myflames-result.${body.format}` };
    case "/api/live/schemas":
      return {
        schema_version: "schemas-1.0",
        engine: "mysql",
        server_version: "8.4.3",
        taken_at: "x",
        uptime_s: 1,
        performance_schema: true,
        schemas: mock.schemas,
        user_tables: 2,
        all_limit: 2000,
        all_note: "",
        system: ["mysql"],
      };
    case "/api/live/catalog":
      if (body.table && mock.holdTable) return null;
      if (!body.table) {
        const names = ["orders", "users", "archive_history", "%_literal"];
        const filtered = body.prefix ? names.filter((name) => name.startsWith(body.prefix)) : null;
        return {
          schema_version: "catalog-1.0",
          level: "tables",
          schema: body.schema,
          truncated: !filtered && !body.after,
          limit: 200,
          prefix: body.prefix || null,
          after: body.after || null,
          next_after: !filtered && !body.after ? "orders" : null,
          tables: (filtered || (body.after ? ["users"] : ["orders"])).map((name) => ({ name, type: "table" })),
        };
      }
      return {
        schema_version: "catalog-1.0",
        level: "table",
        schema: body.schema,
        table: body.table,
        columns: mock.catalogColumns || [
          { name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false },
          { name: "status", type: "varchar(20)", nullable: true, key: "MUL", default: null, extra: "", generated: false },
        ],
        indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }],
      };
  }
  return undefined;
}

const heldStatus = [];
const held = [];
function answerHeld(entry, body, status = 200) {
  try {
    void entry.request.respond({ status, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
  } catch {
    /* the page gave up on it */
  }
}

const completedSteps = [];
function stepDone(label) {
  completedSteps.push(label);
  console.log(`PASS ${label}`);
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

  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [] });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (p.startsWith("/api/editor/saved/")) {
      seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
      return request.continue();
    }
    if (!p.startsWith("/api/editor/") && p !== "/api/live/schemas" && p !== "/api/live/catalog" && p !== "/api/live/privileges" && p !== "/api/live/ddl-progress") {
      if (p.startsWith("/api/live/") || p.startsWith("/api/connection/")) {
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
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });

  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";

  async function chord(key, shift = false) {
    await page.keyboard.down(MOD);
    if (shift) await page.keyboard.down("Shift");
    await page.keyboard.press(key);
    if (shift) await page.keyboard.up("Shift");
    await page.keyboard.up(MOD);
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

  const idle = () => page.waitForFunction(() => !document.querySelector(".results-running") && !document.querySelector("[data-action=run]")?.disabled);
  const openPlace = async (name) => {
    await page.locator(`.main-nav button ::-p-text(${name})`).click();
  };
  const activePlace = () =>
    page.$eval('.main-nav button[aria-current="page"], .main-nav button.is-active', (el) => el.textContent?.trim() || "");

  // Startup opens Home first; open Query editor for Check 1.
  assert.match(await activePlace(), /^Home\b/, "workspace opens Home at startup");
  await openPlace("Query editor");
  await page.waitForSelector(`.query-lab ${EDITOR}`, { visible: true });

  // ---- Step 1: Per-tab undo -------------------------------------------------
  {
    await setEditorText(page, "SELECT 1;");
    await selectEditor(page, "SELECT 1;".length);
    await page.keyboard.type(" -- tab1");
    await assertEditorText(page, "SELECT 1; -- tab1", "step 1: tab 1 has initial paste plus typed suffix");

    // Open tab 2 via the New query tab button.
    await page.click('button.query-tab-add[aria-label="New query tab"]');
    await until(
      () => page.$$eval(".query-tablist [role=tab]", (tabs) => tabs.length === 2 && tabs[1].getAttribute("aria-selected") === "true"),
      "step 1: tab 2 did not open and become active",
    );

    await setEditorText(page, "SELECT 2;");
    await selectEditor(page, "SELECT 2;".length);
    await page.keyboard.type(" -- tab2");
    await assertEditorText(page, "SELECT 2; -- tab2", "step 1: tab 2 has initial paste plus typed suffix");

    // Switch back to tab 1 and undo once.
    await (await page.$$(".query-tablist [role=tab]"))[0].click();
    await until(
      () => page.$$eval(".query-tablist [role=tab]", (tabs) => tabs[0]?.getAttribute("aria-selected") === "true"),
      "step 1: tab 1 did not become active",
    );
    await assertEditorText(page, "SELECT 1; -- tab1", "step 1: tab 1 preserved its full text across tab switch");
    await editorUndo(page);
    await waitForEditorText(page, "SELECT 1;");
    await assertEditorText(page, "SELECT 1;", "step 1: undo in tab 1 undid only tab 1's last edit");

    // Switch to tab 2: text must be untouched, and undo there must undo tab 2's last edit.
    await (await page.$$(".query-tablist [role=tab]"))[1].click();
    await until(
      () => page.$$eval(".query-tablist [role=tab]", (tabs) => tabs[1]?.getAttribute("aria-selected") === "true"),
      "step 1: tab 2 did not become active again",
    );
    await assertEditorText(page, "SELECT 2; -- tab2", "step 1: tab 1 undo left tab 2 text unchanged");
    await editorUndo(page);
    await waitForEditorText(page, "SELECT 2;");
    await assertEditorText(page, "SELECT 2;", "step 1: undo in tab 2 undid tab 2's last edit");
    stepDone("1. Per-tab undo");
  }

  // ---- Step 2: Mod-[ while the editor has focus navigates Back --------------
  {
    await openPlace("Home");
    await until(async () => /^Home\b/.test(await activePlace()), "step 2: failed to navigate to Home first");
    await openPlace("Query editor");
    await page.waitForSelector(`.query-lab ${EDITOR}`, { visible: true });
    await until(async () => /^Query editor\b/.test(await activePlace()), "step 2: failed to return to Query editor");

    // Indented text catches an accidental CodeMirror indentLess binding on Mod-[.
    const sqlBeforeBack = "  SELECT 1;";
    await setEditorText(page, sqlBeforeBack);
    await selectEditor(page, 4);
    assert.equal(
      await page.$eval(EDITOR, (el) => el.contains(document.activeElement)),
      true,
      "step 2: editor has focus before pressing Mod-[",
    );

    await chord("[");
    await until(
      async () => /^Home\b/.test(await activePlace()),
      "step 2: Mod-[ with focus in the editor did not navigate Back to Home",
    );

    await chord("]");
    await page.waitForSelector(`.query-lab ${EDITOR}`, { visible: true });
    await assertEditorText(page, sqlBeforeBack, "step 2: Mod-[ navigated Back without changing or outdenting editor text");
    stepDone("2. Mod-[ Back navigation from focused editor");
  }

  // ---- Step 3: Shift-Mod-K deletes line without opening command palette -----
  {
    const threeLines = "SELECT 1;\nSELECT 2;\nSELECT 3;";
    await setEditorText(page, threeLines);
    await selectEditor(page, "SELECT 1;\n".length + 3);

    await chord("KeyK", true);
    await waitForEditorText(page, "SELECT 1;\nSELECT 3;");
    await assertEditorText(page, "SELECT 1;\nSELECT 3;", "step 3: Shift-Mod-K deleted the current line");
    await pause(80);
    const paletteOpen = await page.evaluate(() => {
      const dlg = document.querySelector("dialog.command-palette");
      return Boolean(dlg && (dlg.open || dlg.hasAttribute("open") || dlg.getClientRects().length > 0));
    });
    assert.equal(paletteOpen, false, "step 3: Shift-Mod-K must not open dialog.command-palette");
    stepDone("3. Shift-Mod-K deletes line without opening command palette");
  }

  // ---- Step 4: 1 MB document paste and typing budget ------------------------
  {
    const unit = "SELECT id, status FROM testdb.orders WHERE id = 1;\n"; // 51 chars
    const bigDoc = unit.repeat(20000); // 1,020,000 chars (~1 MB)
    const typedTail = "SELECT 123456789012;"; // 20 chars, no bracket pairs
    assert.equal(typedTail.length, 20, "step 4: typed suffix is 20 characters");

    await setEditorText(page, bigDoc);
    await selectEditor(page, bigDoc.length);
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r())));

    const t0 = performance.now();
    await page.keyboard.type(typedTail);
    await waitForEditorText(page, bigDoc + typedTail);
    const elapsedMs = performance.now() - t0;

    await assertEditorText(page, bigDoc + typedTail, "step 4: 1 MB document plus 20 typed characters matches expected text");
    assert.ok(
      elapsedMs < 1500,
      `step 4: typing 20 characters at the end of a 1 MB document took ${Math.round(elapsedMs)} ms (budget < 1500 ms)`,
    );

    // Reset document size before later checks and session persistence.
    await setEditorText(page, "SELECT 1;");
    stepDone(`4. 1 MB document (${bigDoc.length} chars, 20 keystrokes in ${Math.round(elapsedMs)} ms)`);
  }

  // ---- Step 5: IME composition via CDP --------------------------------------
  {
    await setEditorText(page, "SELECT ");
    await selectEditor(page, "SELECT ".length);
    await pause(50);
    assert.equal(
      await page.evaluate(() => Boolean(document.querySelector(".sql-completion"))),
      false,
      "step 5: no .sql-completion popup is open before IME composition starts",
    );

    await page.evaluate(() => {
      window.__imeCompletionOpened = false;
      const check = () => {
        const el = document.querySelector(".sql-completion");
        if (el && el.getClientRects().length > 0) window.__imeCompletionOpened = true;
      };
      const obs = new MutationObserver(check);
      obs.observe(document.body, { childList: true, subtree: true, attributes: true });
      window.__imeCompletionObserver = obs;
    });

    await client.send("Input.imeSetComposition", { text: "に", selectionStart: 1, selectionEnd: 1 });
    await waitForEditorText(page, "SELECT に");
    await pause(120);
    const openDuringComposition = await page.evaluate(() => {
      const live = document.querySelector(".sql-completion");
      return Boolean(window.__imeCompletionOpened || (live && live.getClientRects().length > 0));
    });
    assert.equal(openDuringComposition, false, "step 5: .sql-completion must not open during IME composition");

    await client.send("Input.insertText", { text: "日本" });
    await page.evaluate(() => {
      window.__imeCompletionObserver?.disconnect();
      delete window.__imeCompletionObserver;
    });
    await waitForEditorText(page, "SELECT 日本");
    await assertEditorText(page, "SELECT 日本", "step 5: committed IME text landed in the editor");
    stepDone("5. IME composition via CDP without completion popup");
  }

  // ---- Step 6: Hidden mount line-height check -------------------------------
  {
    await setEditorText(page, "SELECT 1;\nSELECT 2;\nSELECT 3;");
    await openPlace("Home");
    await until(async () => /^Home\b/.test(await activePlace()), "step 6: failed to open Home before reload");
    // Let session autosave flush so reload starts on Home with saved query text.
    await pause(500);
    await page.waitForNetworkIdle({ idleTime: 200, timeout: 5000 });

    await page.reload({ waitUntil: "networkidle0" });
    assert.match(await activePlace(), /^Home\b/, "step 6: reload opened Home first, with Query editor not shown");
    const visibleBeforeOpen = await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      return Boolean(el && el.getClientRects().length > 0);
    }, EDITOR);
    assert.equal(visibleBeforeOpen, false, "step 6: Query editor is not visible while Home is shown");

    await openPlace("Query editor");
    await page.waitForSelector(`.query-lab ${EDITOR}`, { visible: true });
    await page.evaluate(async () => {
      await document.fonts?.ready;
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });

    const heights = await page.evaluate(() => {
      const lines = [...document.querySelectorAll(".cm-content .cm-line")].map((el) => el.getBoundingClientRect().height);
      // Ignore CodeMirror's hidden height:0 width-spacer element inside .cm-lineNumbers.
      const gutters = [...document.querySelectorAll(".cm-lineNumbers .cm-gutterElement")]
        .filter((el) => getComputedStyle(el).visibility !== "hidden")
        .map((el) => el.getBoundingClientRect().height);
      return { lines, gutters };
    });
    assert.ok(heights.lines.length >= 1, "step 6: content rendered at least one line");
    assert.equal(heights.gutters.length, heights.lines.length, "step 6: visible gutter line count matches content line count");
    for (let i = 0; i < heights.lines.length; i++) {
      assert.ok(heights.lines[i] > 0, `step 6: content line ${i + 1} height must be positive (got ${heights.lines[i]})`);
      assert.ok(heights.gutters[i] > 0, `step 6: gutter line ${i + 1} height must be positive, not zero from hidden mount (got ${heights.gutters[i]})`);
      assert.ok(
        Math.abs(heights.gutters[i] - heights.lines[i]) < 0.5,
        `step 6: gutter line ${i + 1} height (${heights.gutters[i]}) must equal content line height (${heights.lines[i]})`,
      );
    }
    stepDone(`6. Hidden mount gutter height (${heights.gutters[0]}px == content ${heights.lines[0]}px across ${heights.lines.length} lines)`);
  }

  // ---- Step 7: Refused file drop --------------------------------------------
  {
    const beforeDrop = "SELECT 'keep_me';";
    await setEditorText(page, beforeDrop);

    await page.$eval(".cm-content", async (el) => {
      const rect = el.getBoundingClientRect();
      const file = new File(["DROP TABLE t"], "x.sql", { type: "application/sql" });
      const dt = new DataTransfer();
      dt.items.add(file);
      el.dispatchEvent(
        new DragEvent("drop", {
          bubbles: true,
          cancelable: true,
          clientX: rect.left + 16,
          clientY: rect.top + 10,
          dataTransfer: dt,
        }),
      );
      // Drain a full FileReader cycle and two frames so an async FileReader drop would have landed.
      await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onloadend = () => resolve();
        reader.readAsText(new File(["DROP TABLE t"], "probe.sql"));
      });
      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
    });
    await pause(80);
    await assertEditorText(page, beforeDrop, 'step 7: dropping File "x.sql" ("DROP TABLE t") on .cm-content leaves editor text unchanged');
    stepDone("7. Refused .sql file drop on .cm-content");
  }

  // ---- Step 8: Escape with two cursors during an active run -----------------
  {
    const multiRun = run([statement(0, "SELECT SLEEP(9)", 0, { status: "running" })], [], { state: "running", id: "run-multi" });
    mock.run.push({ status: "running", session: session({ state: "running" }), run: multiRun });
    mock.holdStatus = true;

    await setEditorText(page, "SELECT SLEEP(9)");
    await selectEditor(page, 0);
    await chord("Enter");
    await page.waitForSelector(".results-running");

    const runLine = await page.$(".cm-content .cm-line");
    const runBox = await runLine.boundingBox();
    await page.keyboard.down("Alt");
    await page.mouse.click(runBox.x + runBox.width - 8, runBox.y + runBox.height / 2);
    await page.keyboard.up("Alt");
    await until(() => page.$(".cm-cursor-secondary"), "step 8: Alt+click did not add a second cursor");

    const stopsBefore = calls("/api/editor/stop").length;
    await page.keyboard.press("Escape");
    await until(async () => !(await page.$(".cm-cursor-secondary")), "step 8: first Escape did not collapse the second cursor");
    await pause(150);
    assert.equal(
      calls("/api/editor/stop").length,
      stopsBefore,
      "step 8: first Escape collapsed multiple cursors and did not call /api/editor/stop",
    );
    assert.ok(await page.$(".results-running"), "step 8: run remains active after collapsing cursors with Escape");

    await page.keyboard.press("Escape");
    await until(
      () => calls("/api/editor/stop").length === stopsBefore + 1,
      "step 8: second Escape with a single cursor did not call /api/editor/stop",
    );

    mock.holdStatus = false;
    const stoppedMulti = {
      ...multiRun,
      state: "stopped",
      stopped: { by: "user", interrupted: true, message: "Stopped." },
      statements: multiRun.statements.map((s) => ({ ...s, status: "stopped" })),
    };
    for (const entry of heldStatus.splice(0)) {
      answerHeld(entry, { status: "idle", session: session(), run: stoppedMulti });
    }
    await idle();
    stepDone("8. Escape collapses two cursors without stopping run, second Escape stops run");
  }

  assert.equal(completedSteps.length, 8, "all 8 editor-core checks completed");
  assert.deepEqual(errors, [], "no uncaught page errors occurred");
  console.log(`Editor core checks passed (${completedSteps.length}/8).`);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
  await fs.rm(directory, { recursive: true, force: true }).catch(() => {});
}
