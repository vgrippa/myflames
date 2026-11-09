// README screenshots (docs/screenshots): Home, Visual Explain, Query editor
// and Server Health at 1440x900 in the light theme, plus manifest.json
// ({version, commit, captured_at}).
//
// It builds nothing: it opens the built workspace (myflames/ui_assets, from
// `npm run build`) through the perf harness, with the real Python server on a
// throwaway HOME and store. Every database read is answered from fixed
// fixtures, nothing is requested outside 127.0.0.1, and the page clock, time
// zone, locale, theme and motion are fixed, so two runs on one machine write
// the same PNGs (only manifest.json changes).
//
//   npm run build && npm run docs:screenshots        (writes docs/screenshots)
//   node test/readme-screenshots.mjs --out DIR       (writes DIR instead)
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const outFlag = process.argv.indexOf("--out");
const out = path.resolve(outFlag > 0 ? process.argv[outFlag + 1] : path.join(root, "docs/screenshots"));
const SIZE = { width: 1440, height: 900 };
// The frozen page clock: just after the Health fixture's sample (taken_at
// 16:25:51 UTC), so "Refreshed" and "window at" read as one moment.
const CLOCK = "2026-10-14T16:26:00Z";
const QUERY = "SELECT id, customer_id, status, total, created_at FROM orders ORDER BY id LIMIT 24;";

await fs.access(path.join(root, "myflames/ui_assets/index.html")).catch(() => {
  throw new Error("No built workspace in myflames/ui_assets: run `npm run build` in apps/workspace first.");
});
await fs.mkdir(out, { recursive: true });

// ---- fixtures -------------------------------------------------------------
const health = await load("health-mysql-8.4-full.json");
const schemas = await load("schemas-mysql-8.4.json");
schemas.schemas = [{ name: "shop", tables: 4, system: false }, ...schemas.schemas.filter((s) => s.system)];
schemas.user_tables = 4;
const catalog = (body) => body.table
  ? { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table,
    columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false }], indexes: [] }
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
const editorRun = (sql) => {
  const columns = ["id", "customer_id", "status", "total", "created_at"].map((name) => ({ name, hex: false, type: null }));
  const rows = Array.from({ length: 24 }, (_, i) => [String(i + 1), String(100 + i), i % 3 ? "paid" : "new",
    (19.5 + i * 3.25).toFixed(2), `2026-10-${String(1 + (i % 28)).padStart(2, "0")} 10:00:00`]);
  const statement = { index: 0, text: sql, start: 0, end: sql.length, verb: "select", kind: "read", category: "read", status: "ok", duration_ms: 12 };
  return {
    schema_version: "editor-run-1.0", id: "run-1", kind: "run", state: "complete", scope: "selection",
    started_at: CLOCK, finished_at: CLOCK, duration_ms: 12, limit: 1000, statements: [statement],
    results: [{ type: "rows", statement: 0, columns, columns_known: true, rows, row_count: rows.length, truncated: false,
      limit: 1000, stopped: null, label: null, server_rows: rows.length, warnings: 0, duration_ms: 12 }],
    error: null, stopped: null, message: null, audit_id: null, history_id: null,
    counts: { statements: 1, run: 1, rows: rows.length, affected_rows: 0, warnings: 0, errors: 0 },
  };
};
const ok = (body) => ({ status: 200, body });
const extra = {
  "/api/live/schemas": ok(schemas),
  "/api/live/catalog": (body) => ok(catalog(body)),
  "/api/editor/open": ok({ status: "idle", session: session() }),
  "/api/editor/status": ok({ status: "none", session: null, run: null }),
  "/api/editor/run": (body) => ok({ status: "complete", session: session(), run: editorRun(body.sql || QUERY) }),
  "/api/editor/close": ok({ closed: true, session: null }),
  "/api/editor/history": ok({ items: [] }),
  // Privileges unverified, as the editor shows them quietly.
  "/api/editor/preview": ok({ status: "ready", statements: [{ index: 0, kind: "read", action_keys: [], state: "unknown", missing: [], reason: "" }],
    aggregate: { state: "unknown", missing: [], reason: "", warning: "" }, policy: { action: "run", text: "" } }),
  // Choosing the connection checks its readiness once.
  "/api/live/readiness": ok({ engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.11", read_only: false, super_read_only: false,
    performance_schema: true, replica: false, group_member: false, galera_member: false, privileges: {}, actions: {}, notes: [], checked_at: CLOCK }),
  // Home news, as when the feeds cannot be read: no headline is invented.
  "/api/news": ok({ schema_version: "news-1.0", enabled: true, items: [], fetched_at: null, stale: true, offline: true, sources: [] }),
  // Never asked without a click; refused if it ever is.
  "/api/app/update/check": { status: 500, body: { error: "Not expected while capturing screenshots." } },
};
const seed = `from myflames.workspace_store import WorkspaceStore
import json, sys
store = WorkspaceStore(sys.argv[1]); ids = {}
for name, host, kind in (("Dev MySQL", "dev.example", "development"), ("Prod MySQL", "prod.example", "production")):
    saved = store.save("profile", dict(name=name, host=host, port=3306, user="dba", database="shop", ssl_mode="REQUIRED", type=kind, read_only=False), password=None, remember=False)
    ids[name] = saved["id"]
print(json.dumps(ids))`;

async function fixPage(page) {
  // A frozen clock: every "now" in the page is CLOCK; dates passed in are kept.
  await page.evaluateOnNewDocument((fixed) => {
    const Real = Date;
    function Frozen(...args) {
      if (!new.target) return new Real(fixed).toString();
      return args.length ? new Real(...args) : new Real(fixed);
    }
    Frozen.prototype = Real.prototype;
    Frozen.now = () => fixed;
    Frozen.parse = Real.parse;
    Frozen.UTC = Real.UTC;
    globalThis.Date = Frozen;
    try { localStorage.setItem("myflames.theme", "light"); } catch {}
  }, Date.parse(CLOCK));
  await page.emulateTimezone("UTC");
  const cdp = await page.createCDPSession();
  await cdp.send("Emulation.setLocaleOverride", { locale: "en-US" });
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
}

const h = await open("/api/live/health", "readme-screenshots", extra, {
  fallback: ok(health), seed, beforeGoto: fixPage, viewport: SIZE,
  // Editor calls that would reach a server are refused unless mocked above.
  refuse: /^\/api\/editor\/(?!saved\/)/,
});
const { page } = h;
const remote = [];
page.on("request", (request) => {
  const url = new URL(request.url());
  if (!["127.0.0.1", "localhost"].includes(url.hostname) && !["data:", "blob:"].includes(url.protocol)) remote.push(url.href);
});

/** Wait until nothing loads or animates, then write *file*. */
async function shoot(file) {
  await page.addStyleTag({ content: "*, *::before, *::after { transition: none !important; animation: none !important; caret-color: transparent !important; }" });
  await page.waitForFunction(() => !document.querySelector(".spinner, [aria-busy=true]"), { timeout: 10000 }).catch(() => {});
  // The session autosave has settled (the sidebar footer).
  await page.waitForFunction(() => document.body.innerText.includes("All changes saved"));
  // The pointer rests on the empty middle of the status bar: no hover state, no tooltip.
  await page.mouse.move(1100, SIZE.height - 7);
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await pause(600);
  await page.screenshot({ path: path.join(out, file) });
  console.log("wrote", path.relative(root, path.join(out, file)));
}
const clickButton = async (text, scope = "") => {
  const handle = await page.waitForFunction((text, scope) => [...document.querySelectorAll(`${scope} button`)]
    .find((b) => b.textContent.trim() === text && b.getClientRects().length && !b.disabled), {}, text, scope);
  await handle.asElement().click();
  await handle.dispose();
};

try {
  // Home: saved connections, quick actions, Recent work, News, version footer.
  await page.waitForFunction(() => /Not checked yet/.test(document.querySelector(".home-version")?.textContent || ""));
  await page.waitForFunction(() => /Dev MySQL/.test(document.body.textContent) && /Prod MySQL/.test(document.body.textContent));
  await page.waitForFunction(() => /unavailable offline/.test(document.querySelector(".home-news")?.textContent || ""));
  await shoot("home.png");

  // Plans: the bundled sample plan in Visual Explain.
  await clickButton("Explore sample plan", ".home-pane");
  await page.waitForFunction(() => document.querySelector(".plan-heading h1")?.textContent === "sample-plan.json");
  await page.waitForSelector(".plan-screen svg", { visible: true });
  await shoot("plan-visual-explain.png");

  // Query editor on the Development connection, after running a SELECT.
  await clickButton("Query editor", ".main-nav");
  await chooseConnection(page, h.seeded["Dev MySQL"]);
  await page.waitForSelector('[aria-label="SQL editor"]');
  await page.locator('[aria-label="SQL editor"]').fill(QUERY);
  await page.click("[data-action=run]");
  await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
  await shoot("query-editor.png");

  // Server > Health on the same connection.
  await clickButton("Server", ".main-nav");
  await h.onView("health");
  await page.waitForFunction(() => /Buffer pool: full or warming/.test(document.querySelector('.server-view[data-view="health"]')?.textContent || ""));
  await shoot("server-health.png");

  assert.deepEqual(h.errors, [], "page errors");
  assert.deepEqual(remote, [], "requests outside 127.0.0.1");
  const mocked = new Set(Object.keys(extra));
  const refused = [...new Set(h.state.other.map((o) => o.path).filter((p) => !mocked.has(p)))];
  if (refused.length) console.log("refused (not mocked):", refused.join(", "));

  const desktop = JSON.parse(await fs.readFile(path.join(root, "apps/desktop/package.json"), "utf8"));
  const commit = execFileSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" }).trim();
  const manifest = { version: desktop.version, commit, captured_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z") };
  await fs.writeFile(path.join(out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log("wrote", path.relative(root, path.join(out, "manifest.json")), JSON.stringify(manifest));
} finally {
  await h.close();
}
