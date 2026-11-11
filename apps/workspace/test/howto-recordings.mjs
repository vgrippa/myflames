// How-to guide recordings (docs/screenshots/howto-<id>.gif): short animated
// GIFs of common tasks, driven through the real built workspace the same way
// readme-screenshots.mjs captures the PNGs (perf harness, throwaway HOME and
// store, fixture-backed reads, frozen clock, nothing outside 127.0.0.1).
//
// While a guide records, a caption near the bottom right names the keys being
// pressed and a dot follows the pointer; both are injected into the page by
// this script only and are never part of the app bundle.
//
// Frames are page screenshots at a fixed rate, encoded with gifski. A GIF
// over 1.5 MB is re-encoded smaller (quality, then width, then fps).
//
//   npm run build && npm run docs:howtos      (writes docs/screenshots)
//   node test/howto-recordings.mjs --only multi-cursor
//   node test/howto-recordings.mjs --out DIR  (writes DIR instead)
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection } from "./chrome-helpers.mjs";
import { EDITOR, editorSelection, setEditorText } from "./editor-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const onlyFlag = process.argv.indexOf("--only");
const only = onlyFlag > 0 ? process.argv[onlyFlag + 1] : "";
const outFlag = process.argv.indexOf("--out");
const out = path.resolve(outFlag > 0 ? process.argv[outFlag + 1] : path.join(root, "docs/screenshots"));
const GIFSKI = "/opt/homebrew/bin/gifski";
const SIZE = { width: 1280, height: 800 };
const FPS = 10;
const MAX_BYTES = 1.5 * 1024 * 1024;
// The same frozen moment as the README screenshots.
const CLOCK = "2026-10-14T16:26:00Z";

await fs.access(GIFSKI).catch(() => {
  throw new Error(`gifski not found at ${GIFSKI}: install it (brew install gifski) to record the how-to GIFs.`);
});
await fs.access(path.join(root, "myflames/ui_assets/index.html")).catch(() => {
  throw new Error("No built workspace in myflames/ui_assets: run `npm run build` in apps/workspace first.");
});
await fs.mkdir(out, { recursive: true });

// ---- fixtures (as readme-screenshots.mjs, plus capture jobs for Explore) ----
const health = await load("health-mysql-8.4-full.json");
const schemas = await load("schemas-mysql-8.4.json");
schemas.schemas = [{ name: "shop", tables: 4, system: false }, ...schemas.schemas.filter((s) => s.system)];
schemas.user_tables = 4;
const ORDERS_COLUMNS = ["id", "customer_id", "status", "total", "created_at"]
  .map((name) => ({ name, type: name === "id" || name === "customer_id" ? "int" : name === "total" ? "decimal(10,2)" : name === "created_at" ? "datetime" : "varchar(20)", nullable: false, key: name === "id" ? "PRI" : "", default: null, extra: "", generated: false }));
const catalog = (body) => body.table
  ? { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table, columns: ORDERS_COLUMNS, indexes: [] }
  : { schema_version: "catalog-1.0", level: "tables", schema: body.schema, truncated: false, limit: 200, prefix: null, after: null,
    next_after: null, tables: [{ name: "customers", type: "table" }, { name: "order_items", type: "table" }, { name: "orders", type: "table" }, { name: "products", type: "table" }] };
const session = () => ({
  schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
  connection: { profile_id: null, name: "Dev MySQL", host: "dev.example", port: 3306, user: "dba", type: "development", read_only: false },
  engine: "mysql", server_version: "8.4.11", connection_id: 42, autocommit: true, in_transaction: false,
  transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
  backslash_escapes: true, safe_updates: true, safe_updates_default: true, read_only_transactions: false,
  started_at: CLOCK, idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null, message: null, notes: [], run: null,
});
const statement = (sql) => ({ index: 0, text: sql, start: 0, end: sql.length, verb: "select", kind: "read", category: "read", status: "ok", duration_ms: 12 });
const runBase = (sql) => ({
  schema_version: "editor-run-1.0", id: "run-1", kind: "run", state: "complete", scope: "selection",
  started_at: CLOCK, finished_at: CLOCK, duration_ms: 12, limit: 1000, statements: [statement(sql)],
  error: null, stopped: null, message: null, audit_id: null, history_id: null,
});
const editorRun = (sql) => {
  const columns = ["id", "customer_id", "status", "total", "created_at"].map((name) => ({ name, hex: false, type: null }));
  const rows = Array.from({ length: 24 }, (_, i) => [String(i + 1), String(100 + i), i % 3 ? "paid" : "new",
    (19.5 + i * 3.25).toFixed(2), `2026-10-${String(1 + (i % 28)).padStart(2, "0")} 10:00:00`]);
  return { ...runBase(sql),
    results: [{ type: "rows", statement: 0, columns, columns_known: true, rows, row_count: rows.length, truncated: false,
      limit: 1000, stopped: null, label: null, server_rows: rows.length, warnings: 0, duration_ms: 12 }],
    counts: { statements: 1, run: 1, rows: rows.length, affected_rows: 0, warnings: 0, errors: 0 } };
};
const explainRun = (sql) => {
  const columns = ["id", "select_type", "table", "partitions", "type", "possible_keys", "key", "key_len", "ref", "rows", "filtered", "Extra"]
    .map((name) => ({ name, hex: false, type: null }));
  const rows = [["1", "SIMPLE", "orders", null, "ALL", "status", null, null, null, "24", "100.00", "Using where"]];
  return { ...runBase(sql),
    results: [{ type: "rows", statement: 0, columns, columns_known: true, rows, row_count: 1, truncated: false,
      limit: 1000, stopped: null, label: null, server_rows: 1, warnings: 0, duration_ms: 3 }],
    counts: { statements: 1, run: 1, rows: 1, affected_rows: 0, warnings: 0, errors: 0 } };
};
const PLAN_RAW = await fs.readFile(path.join(root, "test/fixtures/explain-001-table-scan-users-no-filter.json"), "utf8");
const CAPTURE = { schema_version: "capture-1.0", raw: PLAN_RAW, query: "SELECT id FROM users", mode: "explain", engine: "mysql",
  server_version: "8.4.11", captured_at: CLOCK, conditions: "", warnings: [], runs: [], measurements: { count: 0 },
  schema: {}, table_stats: {}, variables: {}, trace: null };
const ok = (body) => ({ status: 200, body });
const extra = {
  "/api/live/schemas": ok(schemas),
  "/api/live/catalog": (body) => ok(catalog(body)),
  "/api/editor/open": ok({ status: "idle", session: session() }),
  "/api/editor/status": ok({ status: "none", session: null, run: null }),
  "/api/editor/run": (body) => ok({ status: "complete", session: session(),
    run: body.explain_format ? explainRun(body.sql) : editorRun(body.sql) }),
  "/api/editor/close": ok({ closed: true, session: null }),
  "/api/editor/history": ok({ items: [] }),
  "/api/editor/preview": ok({ status: "ready", statements: [{ index: 0, kind: "read", action_keys: [], state: "unknown", missing: [], reason: "" }],
    aggregate: { state: "unknown", missing: [], reason: "", warning: "" }, policy: { action: "run", text: "" } }),
  // Explore from an Explain result captures the plan in the tab's session.
  "/api/live/start": ok({ id: "howto-capture" }),
  "/api/live/status": ok({ id: "howto-capture", state: "complete", progress: 1, repeat: 1, error: "", result: CAPTURE }),
  "/api/live/readiness": ok({ engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.11", read_only: false, super_read_only: false,
    performance_schema: true, replica: false, group_member: false, galera_member: false, privileges: {}, actions: {}, notes: [], checked_at: CLOCK }),
  "/api/news": ok({ schema_version: "news-1.0", enabled: true, items: [], fetched_at: null, stale: true, offline: true, sources: [] }),
  "/api/app/update/check": { status: 500, body: { error: "Not expected while recording." } },
};
const seed = `from myflames.workspace_store import WorkspaceStore
import json, sys
store = WorkspaceStore(sys.argv[1]); ids = {}
store.set_ui_preferences({"theme": "light"})
for name, host, kind in (("Dev MySQL", "dev.example", "development"), ("Prod MySQL", "prod.example", "production")):
    saved = store.save("profile", dict(name=name, host=host, port=3306, user="dba", database="shop", ssl_mode="REQUIRED", type=kind, read_only=False), password=None, remember=False)
    ids[name] = saved["id"]
print(json.dumps(ids))`;

// A clock that starts at CLOCK and ticks normally from there: the editor's
// completion debounce stalls on a fully frozen Date (Tab stops accepting
// suggestions), so only the epoch is fixed. Locale is fixed too; the
// recording overlay (key caption bottom right, pointer dot) is injected only
// while recording.
async function fixPage(page) {
  await page.evaluateOnNewDocument((fixed) => {
    const Real = Date;
    const start = Real.now();
    const now = () => fixed + (Real.now() - start);
    function Frozen(...args) {
      if (!new.target) return new Real(now()).toString();
      return args.length ? new Real(...args) : new Real(now());
    }
    Frozen.prototype = Real.prototype;
    Frozen.now = now;
    Frozen.parse = Real.parse;
    Frozen.UTC = Real.UTC;
    globalThis.Date = Frozen;
  }, Date.parse(CLOCK));
  await page.emulateTimezone("UTC");
  const cdp = await page.createCDPSession();
  await cdp.send("Emulation.setLocaleOverride", { locale: "en-US" });
  await page.evaluateOnNewDocument(() => {
    const install = () => {
      if (document.getElementById("howto-keys")) return;
      const style = document.createElement("style");
      style.textContent = [
        "#howto-keys{position:fixed;right:18px;bottom:18px;z-index:2147483647;pointer-events:none;",
        "font:600 14px/1.4 -apple-system,system-ui,sans-serif;color:#fff;background:rgba(17,17,17,.85);",
        "padding:7px 12px;border-radius:8px;opacity:0;transition:opacity .15s;letter-spacing:.02em;}",
        "#howto-keys.show{opacity:1;}",
        "#howto-dot{position:fixed;left:-100px;top:-100px;width:16px;height:16px;margin:-8px 0 0 -8px;",
        "z-index:2147483647;pointer-events:none;border-radius:50%;background:rgba(234,88,12,.45);",
        "border:2px solid rgba(154,52,18,.95);box-sizing:border-box;}",
      ].join("");
      document.head.appendChild(style);
      const keys = document.createElement("div");
      keys.id = "howto-keys";
      keys.setAttribute("aria-hidden", "true");
      const dot = document.createElement("div");
      dot.id = "howto-dot";
      dot.setAttribute("aria-hidden", "true");
      document.body.append(keys, dot);
      window.addEventListener("mousemove", (e) => { dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px`; }, true);
      window.addEventListener("mousedown", () => { dot.style.background = "rgba(234,88,12,.85)"; }, true);
      window.addEventListener("mouseup", () => { dot.style.background = "rgba(234,88,12,.45)"; }, true);
      window.__howtoKeys = (text) => { keys.textContent = text || ""; keys.classList.toggle("show", !!text); };
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
    else install();
  });
}

// ---- recording --------------------------------------------------------------
/** Page screenshots at FPS into a temp directory; encode with gifski. */
class Recorder {
  constructor(page, directory) {
    this.page = page;
    this.directory = directory;
    this.frames = 0;
    this.chain = Promise.resolve();
    this.busy = false;
    this.timer = null;
  }
  start() {
    this.startedAt = Date.now();
    this.timer = setInterval(() => {
      if (this.busy) return;
      this.busy = true;
      this.chain = this.chain.then(async () => {
        const png = await this.page.screenshot();
        await fs.writeFile(path.join(this.directory, `frame-${String(this.frames++).padStart(6, "0")}.png`), png);
        this.busy = false;
      });
    }, 1000 / FPS);
  }
  async stop() {
    clearInterval(this.timer);
    await this.chain;
    this.seconds = (Date.now() - this.startedAt) / 1000;
  }
  caption(text) {
    return this.page.evaluate((t) => window.__howtoKeys?.(t), text);
  }
  hold(ms) {
    return pause(ms);
  }
}

// ---- page helpers -----------------------------------------------------------
const clickButton = async (page, text, scope = "") => {
  const handle = await page.waitForFunction((text, scope) => [...document.querySelectorAll(`${scope} button`)]
    .find((b) => b.textContent.trim() === text && b.getClientRects().length && !b.disabled), {}, text, scope);
  await handle.asElement().click();
  await handle.dispose();
};
/** The page point of line (1-based), column (0-based) in the editor. */
async function posPoint(page, line, col) {
  return page.$eval(EDITOR, (el, line, col) => {
    const lineEl = el.querySelectorAll(".cm-line")[line - 1];
    if (!lineEl) throw new Error(`no editor line ${line}`);
    let left = col;
    const walker = document.createTreeWalker(lineEl, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.length >= left) {
        const range = document.createRange();
        range.setStart(node, left);
        range.setEnd(node, left);
        const r = range.getBoundingClientRect();
        return { x: r.left + 1, y: r.top + r.height / 2 };
      }
      left -= node.length;
    }
    const r = lineEl.getBoundingClientRect();
    return { x: r.left + 4, y: r.top + r.height / 2 };
  }, line, col);
}
const cursorCount = (page) => page.$eval(`${EDITOR}`, (el) => el.closest(".cm-editor").querySelectorAll(".cm-cursor").length);
async function press(page, ...keys) {
  const holds = keys.slice(0, -1);
  for (const k of holds) await page.keyboard.down(k);
  await page.keyboard.press(keys[keys.length - 1]);
  for (const k of holds.reverse()) await page.keyboard.up(k);
}
/** Drag with fixed steps so the motion reads in the recording. */
async function drag(page, from, to, { steps = 8, keys = [] } = {}) {
  await page.mouse.move(from.x, from.y);
  for (const k of keys) await page.keyboard.down(k);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(from.x + ((to.x - from.x) * i) / steps, from.y + ((to.y - from.y) * i) / steps);
    await pause(70);
  }
  await page.mouse.up();
  for (const k of keys) await page.keyboard.up(k);
}
/** Esc closes suggestions first, then extra cursors: press until one cursor. */
async function escapeToOneCursor(page) {
  for (let i = 0; i < 3; i++) {
    await page.keyboard.press("Escape");
    await pause(120);
    if ((await cursorCount(page)) <= 1) return;
  }
  assert.equal(await cursorCount(page), 1, "Esc kept one cursor");
}
/** Click the visible completion option whose label starts with *text*. */
async function pickCompletion(page, text) {
  const handle = await page.waitForFunction((t) => [...document.querySelectorAll(".cm-tooltip-autocomplete li")]
    .find((li) => li.textContent.trim().startsWith(t) && li.getClientRects().length), { timeout: 8000 }, text);
  await handle.asElement().click();
  await handle.dispose();
}

async function boot() {
  const h = await open("/api/live/health", "howto-recordings", extra, {
    fallback: ok(health), seed, beforeGoto: fixPage, viewport: SIZE,
    refuse: /^\/api\/editor\/(?!saved\/)/,
    offline: true,
    launchArgs: ["--disable-partial-raster", "--force-color-profile=srgb"],
  });
  await h.page.waitForFunction(() => /Dev MySQL/.test(document.body.textContent) && /Prod MySQL/.test(document.body.textContent));
  return h;
}
async function openEditor(h) {
  await clickButton(h.page, "Query editor", ".main-nav");
  await chooseConnection(h.page, h.seeded["Dev MySQL"]);
  await h.page.waitForSelector(EDITOR);
}

// ---- guides -----------------------------------------------------------------
const MULTI_SQL = [
  "SELECT id FROM orders WHERE status = 'new';",
  "SELECT id FROM orders WHERE status = 'paid';",
  "SELECT id FROM orders WHERE status = 'refunded';",
].join("\n");
const RUN_SQL = "SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24;";

const GUIDES = [
  ["multi-cursor", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, MULTI_SQL);
    await rec.hold(500);
    // Three cursors: click line 1, then Option-click lines 2 and 3 at the same column.
    let p = await posPoint(page, 1, 9);
    await page.mouse.click(p.x, p.y);
    await rec.caption("⌥ click");
    await page.keyboard.down("Alt");
    for (const line of [2, 3]) {
      p = await posPoint(page, line, 9);
      await page.mouse.click(p.x, p.y);
      await pause(200);
    }
    await page.keyboard.up("Alt");
    assert.equal(await cursorCount(page), 3, "Option-click added two cursors");
    await rec.hold(300);
    await rec.caption("type once");
    await page.keyboard.type(", customer_id", { delay: 45 });
    await rec.hold(500);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(300);
    // ⌘D twice on "status" (line 1, after the edit at 0-based column 41).
    p = await posPoint(page, 1, 43);
    await page.mouse.click(p.x, p.y);
    await pause(150);
    await rec.caption("⌘D ⌘D");
    await press(page, "Meta", "d");
    await pause(300);
    await press(page, "Meta", "d");
    await pause(300);
    assert.equal(await cursorCount(page), 2, "⌘D twice selected two occurrences");
    await rec.hold(400);
    await rec.caption("type: state");
    await page.keyboard.type("state", { delay: 45 });
    await rec.hold(500);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(250);
    // ⇧⌘L on "orders" selects all three occurrences (column 30 after the edit).
    p = await posPoint(page, 2, 30);
    await page.mouse.click(p.x, p.y);
    await rec.caption("⇧⌘L");
    await press(page, "Meta", "Shift", "l");
    await pause(150);
    assert.ok((await cursorCount(page)) >= 3, "⇧⌘L selected every occurrence");
    await rec.hold(600);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(250);
    // ⇧Option-drag a box over the "id" column of all three lines.
    const from = await posPoint(page, 1, 7);
    const to = await posPoint(page, 3, 9);
    await rec.caption("⇧⌥ drag");
    await drag(page, from, to, { keys: ["Shift", "Alt"] });
    const selection = await editorSelection(page);
    assert.notEqual(selection.start, selection.end, "the box selection is not empty");
    await rec.caption("");
  }],
  ["suggestions", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, "");
    let p = await posPoint(page, 1, 0);
    await page.mouse.click(p.x, p.y);
    await rec.caption("type: SEL");
    await page.keyboard.type("SEL", { delay: 110 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("Tab");
    console.log(`  [suggestions] before Tab value=${JSON.stringify(await page.$eval(EDITOR, (el) => el.value))} tooltip=${!!(await page.$(".cm-tooltip-autocomplete"))} sel=${await page.$eval(".cm-tooltip-autocomplete li[aria-selected=true]", (e) => e.textContent).catch(() => "none")} focus=${await page.$eval(EDITOR, (el) => el.contains(document.activeElement))}`);
    await page.keyboard.press("Tab");
    await pause(300);
    console.log(`  [suggestions] after Tab value=${JSON.stringify(await page.$eval(EDITOR, (el) => el.value))}`);
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value === "SELECT");
    await rec.hold(350);
    await rec.caption("type: FROM o");
    await page.keyboard.type(" FROM o", { delay: 90 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("click orders");
    await pickCompletion(page, "orders");
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value === "SELECT FROM orders");
    await rec.hold(350);
    await rec.caption("type: o");
    await page.keyboard.type(" o", { delay: 90 });
    await rec.hold(250);
    // Back after "SELECT ": complete a column through the alias o.
    p = await posPoint(page, 1, 7);
    await page.mouse.click(p.x, p.y);
    await rec.caption("type: o.");
    await page.keyboard.type("o.", { delay: 110 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("click status");
    await pickCompletion(page, "status");
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value.startsWith("SELECT o.status"));
    await rec.hold(400);
    await rec.caption("Ctrl+Space");
    await press(page, "Control", "Space");
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(600);
    await rec.caption("Esc");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".cm-tooltip-autocomplete"));
    await rec.caption("");
  }],
  ["run-explain", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    await rec.hold(400);
    await rec.caption("⌘Enter");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
    await rec.hold(600);
    await rec.caption("click Explain");
    await page.click("[data-action=explain]");
    await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .some((b) => b.textContent.trim() === "Explore" && b.getClientRects().length), { timeout: 10000 });
    await rec.hold(700);
    await rec.caption("click Explore");
    const explore = await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "Explore" && b.getClientRects().length));
    await explore.asElement().click();
    await explore.dispose();
    await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
    await rec.caption("");
    await rec.hold(400);
  }],
  ["find-replace", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, MULTI_SQL.replace(/FROM orders/g, "FROM orders"));
    await rec.hold(300);
    await rec.caption("⌘F");
    await press(page, "Meta", "f");
    await page.waitForSelector(".sql-find", { visible: true });
    await rec.hold(250);
    await rec.caption("type: status");
    await page.click('.sql-find input[aria-label="Find"]');
    await page.keyboard.type("status", { delay: 70 });
    await rec.hold(500);
    await rec.caption("⌘G");
    await press(page, "Meta", "g");
    await rec.hold(400);
    await rec.caption("⌥⌘F");
    await press(page, "Meta", "Alt", "f");
    await page.waitForSelector('.sql-find input[aria-label="Replace with"]', { visible: true });
    await rec.hold(300);
    await rec.caption("type: state");
    await page.click('.sql-find input[aria-label="Replace with"]');
    await page.keyboard.type("state", { delay: 70 });
    await rec.hold(350);
    await rec.caption("click Replace all");
    await clickButton(page, "Replace all", ".sql-find");
    await page.waitForFunction(() => (document.querySelector(".sql-editor")?.value.match(/state =/g) || []).length === 3);
    await rec.hold(600);
    await rec.caption("⌘Z");
    await page.$eval(EDITOR, (el) => el.focus());
    await press(page, "Meta", "z");
    await page.waitForFunction(() => (document.querySelector(".sql-editor")?.value.match(/status =/g) || []).length === 3);
    await rec.caption("");
    await rec.hold(300);
  }],
  ["settings-theme", async (h, rec) => {
    const { page } = h;
    await rec.hold(300);
    await rec.caption("⌘,");
    await press(page, "Meta", ",");
    await page.waitForSelector('.settings-page [data-theme-id="dark"]', { visible: true });
    await rec.hold(400);
    await rec.caption("click Dark");
    await page.click('[data-theme-id="dark"]');
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await rec.hold(800);
    await rec.caption("click Light");
    await page.click('[data-theme-id="light"]');
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    await rec.hold(800);
    await rec.caption("click Query editor");
    await page.click("#settings-section-editor");
    await page.waitForFunction(() => /Multiple cursors/.test(document.querySelector(".settings-panel")?.textContent || ""));
    await rec.hold(350);
    const toggle = async (on) => {
      await page.locator('.settings-editor-group .check-field:has-text("Multiple cursors")').click();
      await page.waitForFunction((want) => {
        const field = [...document.querySelectorAll(".settings-editor-group .check-field")]
          .find((f) => /Multiple cursors/.test(f.textContent));
        return field?.querySelector("input")?.checked === want;
      }, {}, on);
    };
    await rec.caption("Multiple cursors: off");
    await toggle(false);
    await rec.hold(500);
    await rec.caption("Multiple cursors: on");
    await toggle(true);
    await rec.caption("");
    await rec.hold(300);
  }],
  ["layout", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    await rec.caption("⌘Enter");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
    await rec.hold(500);
    // Sidebar: drag the right edge wider.
    let box = await (await page.waitForSelector(".sidebar-resizer")).boundingBox();
    const resizerPoint = () => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 });
    await rec.caption("drag sidebar edge");
    const from = resizerPoint();
    await drag(page, from, { x: from.x + 80, y: from.y });
    const widened = await page.$eval(".sidebar-resizer", (e) => Number(e.getAttribute("aria-valuenow")));
    assert.ok(widened > 208, `sidebar widened (${widened})`);
    await rec.hold(400);
    // Query editor splitter: drag the editor taller.
    const splitter = await (await page.waitForSelector(".editor-splitter")).boundingBox();
    await rec.caption("drag editor splitter");
    const sfrom = { x: splitter.x + splitter.width / 2, y: splitter.y + splitter.height / 2 };
    await drag(page, sfrom, { x: sfrom.x, y: sfrom.y + 90 });
    const taller = await page.$eval(".editor-splitter", (e) => Number(e.getAttribute("aria-valuenow")));
    assert.ok(taller > 208, `editor grew (${taller})`);
    await rec.hold(400);
    // Double-click the sidebar edge to reset it.
    box = await (await page.waitForSelector(".sidebar-resizer")).boundingBox();
    await rec.caption("double-click to reset");
    const p = resizerPoint();
    await page.mouse.click(p.x, p.y, { clickCount: 2 });
    await page.waitForFunction(() => document.querySelector(".sidebar-resizer")?.getAttribute("aria-valuenow") === "208");
    await rec.caption("");
    await rec.hold(300);
  }],
];

// ---- driver -------------------------------------------------------------------
const chosen = GUIDES.filter(([id]) => !only || id === only);
if (!chosen.length) throw new Error(`Unknown guide "${only}". Known: ${GUIDES.map(([id]) => id).join(", ")}`);
const results = [];
for (const [id, steps] of chosen) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), `myflames-howto-${id}-`));
  const h = await boot();
  const rec = new Recorder(h.page, directory);
  try {
    rec.start();
    await steps(h, rec);
    await rec.hold(1000); // a one-second hold on the final state
  } finally {
    await rec.stop();
  }
  assert.deepEqual(h.errors, [], `${id}: page errors`);
  assert.deepEqual(h.state.blocked, [], `${id}: requests outside the local server (blocked)`);
  await h.close();
  const file = path.join(out, `howto-${id}.gif`);
  const frames = (await fs.readdir(directory)).filter((f) => f.endsWith(".png")).sort()
    .map((f) => path.join(directory, f));
  assert.ok(frames.length >= FPS * 3, `${id}: too few frames (${frames.length})`);
  // The capture loop skips ticks while a screenshot is in flight, so the real
  // rate is below FPS; play back at the measured rate (kept inside 8-12 fps)
  // for real-time motion.
  const playbackFps = Math.min(12, Math.max(8, Math.round(rec.frames / rec.seconds)));
  // Re-encode smaller until the GIF fits; report what it took.
  const attempts = [
    { fps: playbackFps, quality: 90, width: SIZE.width },
    { fps: playbackFps, quality: 70, width: SIZE.width },
    { fps: 8, quality: 70, width: 1024 },
    { fps: 8, quality: 60, width: 960 },
    { fps: 6, quality: 50, width: 854 },
  ];
  let encoded = null;
  for (const a of attempts) {
    execFileSync(GIFSKI, ["-o", file, "--fps", String(a.fps), "--quality", String(a.quality), "--width", String(a.width), ...frames],
      { stdio: ["ignore", "pipe", "pipe"] });
    const { size } = await fs.stat(file);
    encoded = { ...a, size };
    if (size <= MAX_BYTES) break;
  }
  const probe = JSON.parse(execFileSync("ffprobe", ["-v", "error", "-count_frames", "-select_streams", "v:0",
    "-show_entries", "stream=nb_read_frames,duration", "-of", "json", file], { encoding: "utf8" })).streams[0];
  const probedFrames = Number(probe.nb_read_frames);
  const seconds = Number(probe.duration);
  await fs.rm(directory, { recursive: true, force: true });
  results.push({ id, file, ...encoded, gifFrames: probedFrames, seconds });
  console.log(`howto-${id}.gif  ${(encoded.size / 1024).toFixed(0)} KB  ${probedFrames} frames  ${seconds.toFixed(1)} s  (${encoded.fps} fps q${encoded.quality} ${encoded.width}px)`);
  assert.ok(encoded.size <= MAX_BYTES, `${id}: still over 1.5 MB after re-encoding`);
}
console.log(`recorded ${results.length} guide(s) into ${path.relative(root, out)}`);
