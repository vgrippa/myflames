// Action feedback harness, shared by test/action-feedback.mjs (the suite) and
// screenshot runs: the real workspace server on a temporary, seeded database,
// mocked live endpoints whose replies can be held, an in-page probe of the
// click and the first visible change, and the scenarios (how to reach each
// action, the endpoint it calls, and what each reply must show).
import assert from "node:assert/strict";
import { batchBScenarios } from "./action-feedback-batch-b.mjs";
import { batchC } from "./action-feedback-batch-c.mjs";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection, chooseView, openConnectionPicker } from "./chrome-helpers.mjs";
import { operationScenarios } from "./action-feedback-operations.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));
export const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));
/** The longest a visible change may take after the click, measured in the page. */
export const FEEDBACK_MS = 150;
/** How far the page clock moves on for a request that never answers. */
export const SLOW_SHIFT_MS = 65_000;

// ---- Mocked answers ---------------------------------------------------------------

const F = {};
for (const [p, file] of [
  ["/api/live/health", "health-mysql-8.4-full.json"], ["/api/live/innodb", "innodb-mysql-8.4.json"],
  ["/api/live/error-log", "errorlog-mysql-8.4.json"], ["/api/live/locks", "locks-mysql-8.4.json"],
  ["/api/live/indexes", "indexes-mysql-8.4.json"], ["/api/live/tables", "tables-mysql-8.4.json"],
  ["/api/live/io", "io-mysql-8.4.json"], ["/api/live/waits", "waits-mysql-8.4.json"],
  ["/api/live/config", "parameters-mysql.json"], ["/api/live/instruments", "instruments-mysql-8.4.json"],
  ["/api/live/sessions", "sessions-mysql-8.4.json"], ["/api/live/report", "report-mysql-8.4.json"],
  ["/api/live/replication", "replication-mysql-8.4-gtid-error.json"], ["/api/live/cluster", "cluster-ic-8.4-healthy.json"],
  ["/api/live/proxysql", "proxysql-lab.json"], ["/api/live/schemas", "schemas-mysql-8.4.json"],
  ["/api/lifecycle/tools", "lifecycle-tools-mysql.json"],
]) F[p] = await load(file);
F["/api/live/sessions"].server_identity = { server_uuid: "feedback-server-uuid" };
const LOCKS_DDL = await load("locks-mysql-26.7-ddl.json");
LOCKS_DDL.server_identity = { server_uuid: "feedback-locks-uuid" };
const OPERATION_FIXTURES = {
  variable: await load("variable-mysql-8.4-global.json"), parameters: F["/api/live/config"],
  replication: F["/api/live/replication"], skipped: await load("replication-mysql-8.4-skip.json"),
  running: await load("replication-loop-running.json"), stopped: await load("replication-loop-stopped.json"),
};

let runN = 0;
function editorSession(over = {}) {
  return {
    schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
    connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
    engine: "mysql", server_version: "8.4.3", connection_id: 42, autocommit: true, in_transaction: false,
    transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY", backslash_escapes: true,
    safe_updates: true, safe_updates_default: true, read_only_transactions: false, started_at: "2026-10-15T12:00:00+00:00",
    idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null, message: null, notes: [], run: null, ...over,
  };
}
function editorRun(sql = "SELECT 1") {
  return {
    schema_version: "editor-run-1.0", id: `run-${++runN}`, kind: "run", state: "complete", scope: "selection",
    started_at: "2026-10-15T12:00:00+00:00", finished_at: "2026-10-15T12:00:02+00:00", duration_ms: 2000, limit: 1000,
    statements: [{ index: 0, text: sql, start: 0, end: sql.length, verb: "select", kind: "read", category: "read", status: "ok", duration_ms: 3 }],
    results: [{ type: "rows", statement: 0, columns: [{ name: "1", hex: false, type: null }], columns_known: true, rows: [["1"]], row_count: 1,
      truncated: false, limit: 1000, stopped: null, label: null, server_rows: 1, warnings: 0, duration_ms: 3 }],
    error: null, stopped: null, message: null,
    counts: { statements: 1, run: 1, rows: 1, affected_rows: 0, warnings: 0, errors: 0 }, audit_id: null, history_id: null,
  };
}
const preview = () => ({ status: "ready", statements: [{ index: 0, kind: "read", action_keys: [], state: "unknown", missing: [], reason: "" }],
  aggregate: { state: "unknown", missing: [], reason: "", warning: "" }, policy: { action: "run", text: "" } });
const hours = (h) => new Date(Date.now() - h * 3600e3).toISOString().replace(/\.\d+Z$/, "Z");
const NEWS = () => ({
  schema_version: "news-1.0", enabled: true, fetched_at: hours(0), stale: false,
  items: [{ title: "Troubleshooting MySQL with OpenTelemetry Metrics", link: "https://blogs.oracle.com/mysql/otel", engine: "mysql",
    published: hours(5), summary: "s", source: "Planet MySQL", source_id: "planet-mysql" }],
  sources: [{ id: "planet-mysql", name: "Planet MySQL", engine: "mysql", url: "https://planet.mysql.com/rss20.xml", fetched_at: hours(0), checked_at: hours(0), items: 1 }],
});
const ACCOUNTS = {
  schema_version: "accounts-1.0", engine: "mysql", version: "8.4.11", complete: true, warnings: [],
  accounts: [
    { user: "app", host: "%", kind: "user", locked: false, password_expired: false, plugin: "caching_sha2_password", protected: false },
    { user: "reporting", host: "10.%", kind: "user", locked: false, password_expired: false, plugin: "caching_sha2_password", protected: false },
  ],
  role_grants: [], default_roles: [],
};
const confirmation = (sql) => ({ token: "c1", expires_in: 300, title: "Run?", text: "Making the index invisible changes plans that use it.", tone: "danger",
  kind: "risky", category: "ddl", reasons: [], warnings: [], in_transaction: false, statements: [sql], count: 1,
  connection_type: "production", connection: { name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "" } });
/** The reviewed SQL runner: a review first, then the confirmed run. */
const reviewedRun = (b) => {
  if (b.decline) return { status: "declined", session: editorSession() };
  if (!b.confirmed) return { status: "needs_confirmation", session: editorSession(), confirmation: confirmation(b.sql) };
  const run = editorRun(b.sql);
  run.results = [{ type: "affected", statement: 0, affected_rows: 0, warnings: 0, duration_ms: 3 }];
  return { status: "complete", session: editorSession(), run };
};
const KILL_OK = { schema_version: "kill-1.0", status: "killed", connection_id: 53, mode: "connection", statement: "KILL CONNECTION 53",
  verified: ["user", "host", "not this session"], after: { present: false },
  message: "Ended connection 53. Its open transaction was rolled back and its locks were released." };
const SAMPLE_RAW = JSON.stringify({ query: "SELECT * FROM orders", operation: "Table scan on orders", access_type: "table",
  table_name: "orders", estimated_rows: 1000, estimated_total_cost: 101 });

// Table data: a two-row grid whose Apply runs in the background (status polls).
const tdText = (value) => ({ kind: "text", value });
const TABLE_PAGE = { snapshot_id: "snapshot", schema: "shop", table: "orders", columns: [
  { name: "id", type: "bigint unsigned", kind: "text", nullable: false }, { name: "name", type: "varchar(120)", kind: "text", nullable: false }],
  rows: [{ id: "first", values: [tdText("1"), tdText("Alice")] }, { id: "second", values: [tdText("2"), tdText("Bruno")] }],
  editable: true, reason: "", key: ["id"], offset: 0, limit: 50, has_more: false, expires_in: 300 };
const TABLE_MOCKS = {
  "/api/table-data/open": () => TABLE_PAGE,
  "/api/table-data/close": () => ({ status: "closed" }),
  "/api/table-data/prepare": () => ({ snapshot_id: "snapshot", review_token: "one-use", privileges: [], connection_type: "production",
    statements: ["LOCK TABLES `shop`.`orders` WRITE", "UPDATE `shop`.`orders` SET `name` = 'Bob' WHERE `id` = 1 LIMIT 1", "UNLOCK TABLES"],
    connection: { host: "127.0.0.1", port: 3306, user: "root" }, warnings: [] }),
  "/api/table-data/decline": () => ({ status: "ready" }),
  "/api/table-data/apply": () => ({ status: "running", snapshot_id: "snapshot", message: "", audit_id: null, affected_rows: 0 }),
  "/api/table-data/status": () => ({ snapshot_id: "snapshot", status: "complete", message: "Changes committed. Reload the table.", audit_id: "a1", affected_rows: 1 }),
};
// Users and roles: one user, its grants, a Lock plan and its applied change.
const ACCOUNT_LIST = { schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, warnings: [],
  accounts: [{ name: "app", host: "%", plugin: "caching_sha2_password", locked: false, expired: false, role: false }], role_grants: [], default_roles: [] };
const accountDetail = () => ({ schema_version: "account-1.0", name: "app", host: "%", engine: "mysql", version: "8.4.0", exists: true, fingerprint: "fp-1",
  grants: ["GRANT SELECT ON *.* TO `app`@`%`"], attributes: { locked: false, expired: false, plugin: "caching_sha2_password" },
  model: { global: ["SELECT"], dynamic: [], global_grantable: [], static_grant_option: false, schemas: [], tables: [], columns: [], routines: [], proxies: [], restrictions: [], roles: [], unparsed: [] },
  roles: [], default_roles: [], defaults_checked: true, warnings: [] });
const ACCOUNT_MOCKS = {
  "/api/accounts/list": () => ACCOUNT_LIST,
  "/api/accounts/read": () => accountDetail(),
  "/api/accounts/plan": (b) => ({ schema_version: "account-plan-1.0", plan_id: "plan-1", change_set_id: "set-1", name: b.user, host: b.host, role: false,
    operation: b.operation, account: b.user, engine: "mysql", version: "8.4.0", request: { operation: b.operation }, before: accountDetail(), generates_password: false,
    expect: "fp-1", status: "ready", statements: ["ALTER USER `app`@`%` ACCOUNT LOCK"], warnings: [], guards: { blocked: [], typed_name: "" },
    privileges: { state: "allowed", missing: [], reason: "" }, policy: { action: "confirm", text: "Production: confirm this account change." } }),
  "/api/accounts/apply": () => ({ schema_version: "account-change-1.0", change_set_id: "set-1", status: "applied", message: "Statements accepted.",
    before: accountDetail(), after: { ...accountDetail(), fingerprint: "fp-2", attributes: { locked: true, expired: false, plugin: "caching_sha2_password" } },
    diff: { added: [], removed: [] }, statements: [{ index: 0, statement: "ALTER USER `app`@`%` ACCOUNT LOCK", status: "ok" }], warnings: [], audit_ids: ["audit-1"] }),
};
// Connections with a saved password for Lab primary (Forget password).
const PROFILES_WITH_PASSWORD = () => ({ secret_store: { backend: "vault", label: "encrypted on this computer", available: true }, items: [
  { id: "p-lab", name: "Lab primary", host: "127.0.0.1", port: 3306, user: "root", database: "", ssl_mode: "", type: "development",
    has_saved_password: true, secret_backend: "vault", secret_backend_label: "encrypted on this computer" },
  { id: "p-other", name: "Billing replica", host: "10.20.0.9", port: 3306, user: "app", database: "", ssl_mode: "", type: "production", has_saved_password: false }] });

// The same connections after Lab primary was made read-only elsewhere (another tab or the CLI).
const PROFILES_LAB_READ_ONLY = () => ({ secret_store: { backend: "vault", label: "encrypted on this computer", available: true }, items: [
  { id: "p-lab", name: "Lab primary", host: "127.0.0.1", port: 3306, user: "root", database: "", ssl_mode: "", type: "development", read_only: true, has_saved_password: false },
  { id: "p-other", name: "Billing replica", host: "10.20.0.9", port: 3306, user: "app", database: "", ssl_mode: "", type: "production", has_saved_password: false }] });

/** Path -> (body) => response body (200), or PASS for the real server. */
export const PASS = Symbol("pass");
export const DEFAULTS = {
  ...Object.fromEntries(Object.entries(F).map(([p, v]) => [p, () => v])),
  "/api/live/top": () => ({ schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: "total", limit: 25, schema: null,
    notes: [], table: null, coverage: { uptime_s: 1, first_seen: null, digest_lost: 0, overflow: null }, statements: [] }),
  "/api/live/privileges": () => ({ schema_version: "privileges-1.0", account: "root@localhost", actions: {} }),
  "/api/live/replication/skip-loop/status": () => ({ schema_version: "replication-skip-loop-1.0", loops: [] }),
  "/api/lifecycle/jobs": () => ({ schema_version: "lifecycle-jobs-1.0", jobs: [] }),
  "/api/accounts/list": () => ACCOUNTS,
  "/api/news": () => NEWS(),
  "/api/app/update/check": () => ({ schema_version: "app-update-1.0", current_version: "3.1.0", latest_version: "3.1.0", update_available: false }),
  "/api/editor/status": (b) => (b.run_id ? { status: "complete", session: editorSession(), run: editorRun() } : { status: "none", session: null, run: null }),
  "/api/editor/preview": () => preview(),
  "/api/editor/run": (b) => ({ status: "complete", session: editorSession(), run: editorRun(b.sql) }),
  "/api/editor/stop": () => ({ stopped: true, stop_requested: true, message: "KILL QUERY sent.", session: editorSession({ state: "running" }) }),
  "/api/editor/close": () => ({ closed: true, session: null }),
  "/api/editor/open": () => ({ status: "idle", session: editorSession() }),
  "/api/editor/history": (b) => (b.action === "clear" ? { cleared: 0 } : { items: [] }),
  "/api/editor/export": (b) => ({ format: b.format, content: "a,b\r\n", mime: "text/csv", filename: `myflames-result.${b.format}` }),
  "/api/connection/test": () => ({ version: "8.4.11", schemas: ["shop", "mftab"] }),
  "/api/live/catalog": (b) => (b.table
    ? { schema_version: "catalog-1.0", level: "table", schema: b.schema, table: b.table,
      columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "", generated: false }],
      indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }] }
    : { schema_version: "catalog-1.0", level: "tables", schema: b.schema, truncated: false, limit: 200, prefix: null, after: null,
      next_after: null, tables: [{ name: "orders", type: "table" }, { name: "customers", type: "table" }] }),
  "/api/audit/declined": () => ({ audit_id: "declined-1", status: "declined" }),
  "/api/live/kill": () => KILL_OK,
  "/api/live/kill/bulk": (b) => ({ schema_version: "kill-bulk-1.0", total: b.targets.length, killed: b.targets.length, gone: 0, refused: 0,
    denied: 0, errors: 0, message: `Kill sessions finished: ${b.targets.length} killed`, results: [] }),
};
// The real server answers these (session, connections, plans, investigations, Learn, audit, kill reviews).
const REAL = [/^\/api\/auth\//, /^\/api\/session\//, /^\/api\/profiles\//, /^\/api\/(analyze|render|report|compare)$/,
  /^\/api\/investigations\//, /^\/api\/teach\//, /^\/api\/audit\/(list|export|settings)$/, /^\/api\/news\/settings$/,
  /^\/api\/app\/update\/status$/, /^\/api\/report\/export$/, /^\/api\/analysis\//, /^\/api\/live\/kill\/(bulk\/)?prepare$/];

// ---- In-page probe: the click, the first visible change, the page clock ----------------

function installProbe() {
  // The page clock can be moved on (a request that never answers becomes slow).
  const realNow = Date.now.bind(Date);
  window.__clockOffset = 0;
  Date.now = () => realNow() + window.__clockOffset;
  const P = (window.__af = { armed: false, tClick: null, tFirst: null, first: "", tBusy: null });
  const shown = (el) => !!el && el.isConnected && !el.closest("[hidden]") && el.getClientRects().length > 0;
  document.addEventListener("click", () => { if (P.armed && P.tClick === null) P.tClick = performance.now(); }, true);
  new MutationObserver((list) => {
    if (!P.armed || P.tClick === null) return;
    const t = performance.now();
    for (const m of list) {
      const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (m.type === "attributes" && m.attributeName === "aria-busy" && target.getAttribute("aria-busy") === "true" && P.tBusy === null) P.tBusy = t;
      // Only what the eye sees: added or removed nodes, text, and attributes
      // that change the look; accessibility-only attributes do not count.
      const silent = m.type === "attributes" && /^(aria-|role$|tabindex$|title$)/.test(m.attributeName || "");
      const visible = !silent && (m.type === "childList" ? shown(target) && (m.addedNodes.length > 0 || m.removedNodes.length > 0) : shown(target));
      if (visible && P.tFirst === null) {
        P.tFirst = t;
        P.first = `${m.type}${m.attributeName ? `[${m.attributeName}]` : ""} ${target.tagName.toLowerCase()}.${String(target.className).split(" ").slice(0, 2).join(".")}`;
      }
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
}

// ---- Workspace and page -------------------------------------------------------------

export async function startWorkspace(name) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), `myflames-action-feedback-${name}-`));
  const store = path.join(directory, "workspace.sqlite3");
  const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault", MYFLAMES_TEST_UPDATE: "99.0.0" };
  for (const script of ["action-feedback-seed.py", "audit-seed.py"]) {
    const r = spawnSync(process.env.PYTHON || "python3", [path.join(root, "apps/workspace/test", script), store], { cwd: root, env });
    if (r.status !== 0) throw Error(`${script}: ${r.stderr}`);
  }
  const proc = spawn(process.env.PYTHON || "python3",
    ["-c", "import sys; from myflames.ui import WorkspaceServer; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()", store],
    { cwd: root, env });
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(`server did not start: ${text}`)), 30000);
    proc.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on("error", reject);
  });
  return { url, directory, async close() { proc.kill(); await fs.rm(directory, { recursive: true, force: true }); } };
}

/** A page whose /api requests are mocked; paths in ctx.hold wait in ctx.held. */
export async function openPage(browser, url, viewport = { width: 1440, height: 900 }) {
  const page = await browser.newPage();
  const ctx = { page, hold: new Set(), held: [], log: [], mocks: {}, errors: [] };
  page.on("pageerror", (e) => ctx.errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/")) return request.continue().catch(() => {});
    let body = {};
    try { body = JSON.parse(request.postData() || "{}"); } catch { body = {}; }
    ctx.log.push({ path: p, body });
    if (ctx.hold.has(p)) { ctx.held.push({ path: p, body, request }); return; }
    answer(ctx, p, body, request);
  });
  await page.evaluateOnNewDocument(installProbe);
  await page.setViewport(viewport);
  await page.goto(url, { waitUntil: "networkidle0" });
  return ctx;
}
export function respond(request, status, body) {
  return request.respond({ status, contentType: "application/json", body: JSON.stringify(body) }).catch(() => {});
}
export function answer(ctx, p, body, request) {
  const mock = ctx.mocks[p] || DEFAULTS[p];
  if (mock) {
    const r = mock(body);
    if (r === PASS) return request.continue().catch(() => {});
    if (r && r.__status) return respond(request, r.__status, r.body);
    return respond(request, 200, r);
  }
  if (REAL.some((r) => r.test(p))) return request.continue().catch(() => {});
  return respond(request, 400, { error: `Not mocked in the action feedback test: ${p}` });
}

// ---- Page helpers ----------------------------------------------------------------------

export async function goPlace(page, place) {
  await page.click(`.main-nav button[data-place="${place}"]`);
  await pause(400);
}
export async function goView(page, place, kind) {
  await goPlace(page, place);
  await chooseView(page, kind);
  await pause(600);
}
const isShown = (e) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
/** The first visible element for *selector*, waiting for it. */
export async function visible(page, selector, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    for (const h of await page.$$(selector)) if (await h.evaluate(isShown)) return h;
    await pause(60);
  }
  throw Error(`not visible: ${selector}`);
}
/** The visible control (button, menu item, tab) named *text*. */
export const named = (text, scope = "") => async (page) => {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    for (const h of await page.$$(`${scope} :is(button,[role=menuitem],[role=tab],a)`)) {
      const ok = await h.evaluate((e, t) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0 &&
        (e.textContent.replace(/\s+/g, " ").trim() === t || e.getAttribute("aria-label") === t), text);
      if (ok) return h;
    }
    await pause(80);
  }
  throw Error(`no visible control named ${text}`);
};
export const click = async (page, text, scope = "") => (await named(text, scope)(page)).click();
/** The confirm (last) button of the open sheet, once enabled. */
export const confirmButton = (scope) => async (page) => {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    const buttons = await page.$$(`${scope} .dialog-actions button`);
    const last = buttons[buttons.length - 1];
    if (last && !(await last.evaluate((e) => e.disabled || e.getAttribute("aria-disabled") === "true"))) return last;
    await pause(80);
  }
  throw Error(`confirm never enabled in ${scope}`);
};
/** Wait until a visible element for *selector* has text matching *pattern*. */
export async function waitText(page, selector, pattern, timeout = 8000) {
  await page.waitForFunction((sel, source, flags) => {
    const re = new RegExp(source, flags);
    return [...document.querySelectorAll(sel)].some((e) => e.isConnected && !e.closest("[hidden]") &&
      e.getClientRects().length > 0 && re.test(e.textContent.replace(/\s+/g, " ").trim()));
  }, { timeout }, selector, pattern.source, pattern.flags).catch(async () => {
    const seen = await page.$$eval(selector, (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim().slice(0, 200)));
    throw Error(`no ${selector} matching ${pattern}; saw ${JSON.stringify(seen)}`);
  });
}
/** Wait until no element for *selector* is aria-busy. */
export async function waitNotBusy(page, selector, timeout = 8000) {
  await page.waitForFunction((sel) => ![...document.querySelectorAll(sel)].some((e) => e.getAttribute("aria-busy") === "true"),
    { timeout }, selector).catch(() => { throw Error(`still aria-busy: ${selector}`); });
}
const typeInto = async (page, selector, text) => {
  const field = await visible(page, selector);
  await field.click({ clickCount: 3 });
  await field.type(text);
};
const escapeRe = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** Use another saved connection; returns the label of the one that was in
 * use, as views write it ("Name (user@host:port)"). */
async function switchConnection(page) {
  const from = await page.$eval(".window-toolbar .connection-pill", (pill) => {
    const name = pill.querySelector(".connection-pill-name")?.textContent.trim() || "";
    const target = (pill.querySelector(".picker-target")?.textContent || "").replace(/^\s*·\s*/, "").trim();
    return name && target ? `${name} (${target})` : name || target;
  });
  await openConnectionPicker(page);
  const to = await page.$eval('.window-toolbar select[aria-label="Connection"]',
    (select) => [...select.options].find((o) => o.value && o.value !== select.value)?.value || "");
  assert.ok(to, "another saved connection to switch to");
  await page.keyboard.press("Escape");
  await chooseConnection(page, to);
  await pause(300);
  return from;
}
export async function heldFor(ctx, paths, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const h = ctx.held.find((e) => paths.includes(e.path));
    if (h) return h;
    await pause(25);
  }
  throw Error(`no request to ${paths.join(" or ")}`);
}

// ---- Scenarios ------------------------------------------------------------------------
//
// name, setup(ctx), target(page) -> handle (or a selector), paths (held),
// busy: selector that carries aria-busy while waiting (default: the target),
// ok / error / hang: async (ctx) => checks after the answer (or, for hang,
// after the page clock moved on). double: false for controls a click removes
// (menu items); focus: false where the owner lane still disables the control.

const REFRESH = (view) => `.server-view[data-view="${view}"] [data-action=refresh]`;
const STATUS = (view) => `.server-view[data-view="${view}"] .view-status`;
export const failure = (p) => `Simulated failure: ${p.replace("/api/", "")} (action feedback test).`;

/** A view's Refresh: ViewLayout's ActionButton and ViewStatus. */
function viewRefresh(view, place, apiPath, extra = {}) {
  return {
    name: `${view}-refresh`, label: `${extra.label || view} Refresh`, paths: [apiPath],
    setup: async ({ page }) => { if (extra.go) await extra.go(page); else await goView(page, place, view); await visible(page, REFRESH(view)); },
    target: REFRESH(view),
    ok: async ({ page }) => {
      await waitNotBusy(page, REFRESH(view));
      await waitText(page, `${STATUS(view)} [role=status]`, /^Updated at /);
      assert.equal(await page.$(`${STATUS(view)} [role=alert]`), null, "no error after a good read");
    },
    error: async ({ page }) => {
      await waitText(page, `${STATUS(view)} [role=alert]`, /Simulated failure/);
      await visible(page, `${STATUS(view)} .view-status-retry`);
      await waitNotBusy(page, REFRESH(view));
    },
    hang: async ({ page }) => {
      await waitText(page, `${STATUS(view)} .capture-status`, /Still waiting for the server\./);
      await waitText(page, `${STATUS(view)} .action-elapsed`, /^\d{2,} s so far$/);
      await (await visible(page, `${STATUS(view)} .view-status-stop`)).click();
      await waitNotBusy(page, REFRESH(view));
      await waitText(page, `${STATUS(view)} [role=status]`, /Stopped waiting/);
      // Stop went away with the line: focus is on the view's Refresh, not the page.
      await page.waitForFunction((sel) => document.activeElement === document.querySelector(sel), { timeout: 2000 }, REFRESH(view))
        .catch(() => { throw Error("focus did not return to Refresh after Stop"); });
    },
    ...extra,
  };
}

const LIVE_VIEWS = ["/api/live/health", "/api/live/innodb", "/api/live/error-log", "/api/live/locks", "/api/live/indexes",
  "/api/live/tables", "/api/live/config", "/api/live/instruments", "/api/live/sessions", "/api/live/waits", "/api/live/io",
  "/api/live/top", "/api/live/replication", "/api/live/cluster", "/api/live/proxysql", "/api/accounts/list",
  "/api/lifecycle/tools", "/api/schema-diagram/read", "/api/live/report"];

export const SCENARIOS = [
  ...operationScenarios({ click, named, visible, confirmButton, goPlace, goView, waitText, waitNotBusy,
    pause, respond, heldFor, switchConnection, escapeRe, viewRefresh }, OPERATION_FIXTURES),
  ...batchC({ goPlace, goView, visible, named, click, confirmButton, waitText, waitNotBusy, pause, respond, answer, viewRefresh }),
  // ---- Server, Workload and Cluster views -------------------------------------------
  viewRefresh("health", "server", "/api/live/health", { label: "Health" }),
  viewRefresh("innodb", "server", "/api/live/innodb", { label: "InnoDB" }),
  viewRefresh("locks", "server", "/api/live/locks", { label: "Locks" }),
  viewRefresh("indexes", "server", "/api/live/indexes", { label: "Indexes" }),
  viewRefresh("tables", "server", "/api/live/tables", { label: "Tables" }),
  viewRefresh("instruments", "server", "/api/live/instruments", { label: "Instruments" }),
  viewRefresh("waits", "workload", "/api/live/waits", { label: "Waits" }),
  viewRefresh("io", "workload", "/api/live/io", { label: "I/O" }),
  viewRefresh("cluster", "cluster", "/api/live/cluster", { label: "Cluster", go: async (page) => { await goPlace(page, "cluster"); await pause(800); } }),
  viewRefresh("accounts", "server", "/api/accounts/list", {
    name: "accounts-load", label: "Users and roles: Refresh",
    setup: async ({ page }) => {
      await goView(page, "server", "accounts");
      // Opening reads automatically; measure a later explicit Refresh.
      await visible(page, ".accounts-view tbody tr[data-row-key]");
      await waitNotBusy(page, REFRESH("accounts"));
    },
  }),
  {
    name: "tuning-run-full-analysis", label: "Tuning: Run full analysis", paths: ["/api/live/report"],
    setup: async ({ page }) => { await goPlace(page, "tuning"); await pause(600); },
    target: "[data-action=run-analysis]",
    // Its live region says what runs once; the seconds are drawn, never announced.
    afterClick: async ({ page, ctx }) => {
      await waitText(page, `${STATUS("tuning")} > [role=status]`, /^Running every Server view once, about 10 to 20 s in all\. It only reads\.$/);
      ctx.announced = await page.$eval(`${STATUS("tuning")} > [role=status]`, (e) => e.textContent);
    },
    ok: async ({ page }) => { await waitNotBusy(page, "[data-action=run-analysis]"); await waitText(page, `${STATUS("tuning")} [role=status]`, /^Updated at /); },
    error: async ({ page }) => { await waitText(page, `${STATUS("tuning")} [role=alert]`, /Simulated failure/); await visible(page, `${STATUS("tuning")} .view-status-retry`); },
    hang: async ({ page, ctx }) => {
      await waitText(page, `${STATUS("tuning")} .capture-status`, /Longer than the usual 10 to 20 s\./);
      await waitText(page, `${STATUS("tuning")} .capture-status .action-elapsed`, /^\d{2,} s so far$/);
      assert.equal(await page.$eval(`${STATUS("tuning")} > [role=status]`, (e) => e.textContent), ctx.announced,
        "the live region is not updated with the seconds");
      (await visible(page, `${STATUS("tuning")} .view-status-stop`)).click();
      await waitNotBusy(page, "[data-action=run-analysis]");
    },
  },
  {
    // Refresh all turns into Stop in the same spot: a double click must not stop it.
    name: "server-refresh-all", label: "Refresh all", paths: LIVE_VIEWS, busy: ".server-place .refresh-all-stop", oneRequest: false,
    setup: async ({ page }) => goView(page, "server", "health"),
    target: ".server-place [data-action=refresh-all]",
    afterClick: async ({ page, ctx }) => {
      assert.ok(await page.$(".server-place .refresh-all-stop"), "a double click on Refresh all keeps it running (Stop is not armed yet)");
      await waitText(page, ".server-place .refresh-all-status", /^Refreshing 1 of \d+ · /);
      void ctx;
    },
    ok: async ({ page, ctx }) => {
      ctx.hold.clear();
      for (const h of ctx.held.splice(0)) answer(ctx, h.path, h.body, h.request);
      await waitText(page, ".server-place .refresh-all-status", /^\d+ of \d+ views refreshed$/, 20000);
    },
    error: async ({ page, ctx }) => {
      ctx.hold.clear();
      for (const h of ctx.held.splice(0)) respond(h.request, 400, { error: failure(h.path) });
      for (const p of LIVE_VIEWS) ctx.mocks[p] = () => ({ __status: 400, body: { error: failure(p) } });
      await waitText(page, ".server-place .refresh-all-status", /failed$/, 20000);
    },
    hang: async ({ page }) => {
      await waitText(page, ".server-place .refresh-all-status .action-elapsed", /^\d{2,} s so far$/);
      await page.click(".server-place .refresh-all-stop");
      await waitText(page, ".server-place .refresh-all-status", /stopped$/);
    },
    skipAnswer: true,
  },
  // ---- Home --------------------------------------------------------------------------
  {
    name: "home-test-connection", label: "Test connection (form)", paths: ["/api/connection/test"],
    setup: async ({ page }) => { await goPlace(page, "home"); await click(page, "Add connection"); await pause(300); },
    target: named("Test connection", ".connection-panel"),
    ok: async ({ page }) => waitText(page, ".connection-panel .connections-status", /Connected · 8\.4\.11/),
    error: async ({ page }) => waitText(page, ".connection-panel [role=alert]", /Simulated failure/),
    hang: async ({ page }) => {
      await waitText(page, ".connection-panel .connections-progress .action-elapsed", /^\d{2,} s so far$/);
      await click(page, "Stop waiting", ".connection-panel");
      await waitText(page, ".connection-panel .connections-status", /Stopped waiting for the test/);
    },
  },
  {
    name: "home-save-connection", label: "Save connection", paths: ["/api/profiles/save"],
    setup: async ({ page }) => {
      await goPlace(page, "home"); await click(page, "Add connection"); await pause(300);
      await typeInto(page, ".connection-panel .connection-fields input", "Feedback server");
    },
    target: named("Save connection", ".connection-panel"),
    respondOk: () => ({ id: "p-new", name: "Feedback server", host: "127.0.0.1", port: 3306, user: "", type: "production", has_saved_password: false }),
    ok: async ({ page }) => waitText(page, ".connections-status", /Connection "Feedback server" saved/),
    error: async ({ page }) => waitText(page, ".connection-panel [role=alert]", /Simulated failure/),
    hang: async ({ page }) => waitText(page, ".connection-panel .button.primary[aria-busy=true]", /^Saving…$/),
  },
  {
    name: "home-refresh-news", label: "Refresh news", paths: ["/api/news"],
    setup: async ({ page }) => { await goPlace(page, "home"); await visible(page, ".home-news .news-item"); await waitNotBusy(page, ".home-news"); },
    target: '.home-news button[aria-label="Refresh news"]',
    ok: async ({ page }) => waitText(page, ".home-news .news-state", /^Headlines updated\.$/),
    error: async ({ page }) => waitText(page, ".home-news .news-state", /^News could not be loaded\.$/),
    hang: async ({ page }) => waitText(page, ".home-news .news-state", /^Refreshing news…$/),
  },
  {
    name: "home-delete-connection", label: "Delete connection (sheet)", paths: ["/api/profiles/delete"], sheet: true,
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await page.click('button[aria-label="Actions for Billing replica"]'); await pause(250);
      await page.click('[role="menu"] button[aria-label="Delete Billing replica…"]');
      await page.waitForSelector("dialog.confirm-sheet[open]");
    },
    target: confirmButton("dialog.confirm-sheet[open]"),
    ok: async ({ page }) => waitText(page, ".connections-status", /1 connection removed/),
    error: async ({ page }) => waitText(page, ".connection-delete-error", /Simulated failure/),
    hang: async ({ page }) => {
      await waitText(page, "dialog.confirm-sheet[open] .sheet-progress", /^No reply yet/);
      await waitText(page, "dialog.confirm-sheet[open] .sheet-leave-note", /^Still deleting\. Close does not stop it/);
      await click(page, "Close", "dialog.confirm-sheet[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet[open]"));
      await waitText(page, ".connections-progress", /Deleting the chosen connections/);
    },
  },
  {
    name: "home-forget-password", label: "Forget saved password (connection form)", paths: ["/api/profiles/forget-password"],
    mocks: { "/api/profiles/list": PROFILES_WITH_PASSWORD },
    setup: async ({ page }) => {
      await goPlace(page, "home");
      // The list is read again on focus, now with the saved password.
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await pause(600);
      await page.click('button[aria-label="Actions for Lab primary"]'); await pause(250);
      await page.click('[role="menu"] button[aria-label="Edit Lab primary"]');
      await visible(page, ".connection-panel");
    },
    target: named("Forget saved password", ".connection-panel"),
    respondOk: () => ({ forgotten: true }),
    ok: async ({ page }) => waitText(page, ".connection-panel .connections-status", /Saved password forgotten/),
    error: async ({ page }) => waitText(page, ".connection-panel [role=alert]", /Could not forget the saved password: Simulated failure/),
    hang: async ({ page }) => waitText(page, '.connection-panel button[aria-busy="true"]', /^Forgetting…$/),
  },
  // ---- Investigations and Plans ---------------------------------------------------------
  {
    name: "investigation-open-all", label: "Open all", paths: ["/api/investigations/get"],
    setup: async ({ page }) => { await goPlace(page, "investigations"); await visible(page, ".library-actions"); },
    target: named("Open all", ".library-actions"),
    respondOk: PASS,
    ok: async ({ page }) => page.waitForFunction(() => document.querySelector('.main-nav button[data-place="plans"]')?.getAttribute("aria-current") === "page", { timeout: 10000 }),
    error: async ({ page }) => { await waitText(page, ".library-status", /Could not open “Checkout slowdown”: Simulated failure/); await named("Try again", ".library-status")(page); },
    hang: async ({ page }) => waitText(page, ".library-status .action-elapsed", /^\d{2,} s so far$/),
  },
  {
    name: "investigation-compare", label: "Compare to baseline", paths: ["/api/investigations/get"],
    setup: async ({ page }) => { await goPlace(page, "investigations"); await visible(page, ".library-actions"); },
    target: named("Compare to baseline", ".library-actions"),
    respondOk: PASS,
    ok: async ({ page }) => visible(page, ".library-comparison iframe", 15000),
    error: async ({ page }) => waitText(page, ".library [role=alert]", /Simulated failure/),
    hang: async ({ page }) => {
      await waitText(page, ".library-compare-status .action-elapsed", /^\d{2,} s so far$/);
      await click(page, "Stop", ".library-compare-status");
      await waitNotBusy(page, ".library-actions button");
    },
  },
  {
    name: "investigation-export", label: "Export (investigation)", paths: ["/api/investigations/get"],
    setup: async ({ page }) => { await goPlace(page, "investigations"); await visible(page, ".library-actions"); },
    target: named("Export", ".library-actions"),
    respondOk: PASS,
    ok: async ({ page }) => waitText(page, ".library-status", /Exported “Checkout slowdown” as Checkout-slowdown\.json\./),
    // Try again under the failed export, after another investigation was
    // selected, exports the one that failed.
    error: async ({ page, ctx }) => {
      await waitText(page, ".library-status", /Could not export “Checkout slowdown”/);
      await page.click('.library-item[data-row-key="inv-0"]');
      await waitText(page, ".library-detail-head h2", /^Nightly report$/);
      const before = ctx.log.length;
      await click(page, "Try again", ".library-status");
      await waitText(page, ".library-status", /Exported “Checkout slowdown” as Checkout-slowdown\.json\./);
      assert.deepEqual(ctx.log.slice(before).filter((e) => e.path === "/api/investigations/get").map((e) => e.body.id), ["inv-1"],
        "Try again exports the investigation that failed");
    },
    hang: async ({ page }) => waitText(page, ".library-status .action-elapsed", /^\d{2,} s so far$/),
  },
  {
    name: "investigation-delete", label: "Delete permanently (investigation)", paths: ["/api/investigations/delete"], sheet: true,
    setup: async ({ page }) => {
      await goPlace(page, "investigations"); await visible(page, ".library-actions");
      await click(page, "Delete", ".library-actions"); await page.waitForSelector("dialog.confirm-sheet[open]");
    },
    target: confirmButton("dialog.confirm-sheet[open]"),
    respondOk: PASS,
    ok: async ({ page }) => waitText(page, ".library-status", /Deleted “Checkout slowdown” from this computer\./),
    error: async ({ page }) => waitText(page, "dialog.confirm-sheet[open] [role=alert]", /Simulated failure/),
    hang: async ({ page }) => {
      await click(page, "Close", "dialog.confirm-sheet[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet[open]"));
      await waitText(page, ".library-status", /Deleting “Checkout slowdown”…/);
    },
  },
  {
    name: "plans-import-paste", label: "Import plan (paste)", paths: ["/api/analyze"],
    setup: async ({ page }) => {
      await goPlace(page, "plans");
      await page.click(".plans-import"); await pause(300);
      const area = await visible(page, "dialog.import-dialog textarea");
      await area.evaluate((e, raw) => { Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(e, raw);
        e.dispatchEvent(new Event("input", { bubbles: true })); }, SAMPLE_RAW);
      await pause(200);
    },
    target: named("Analyze plan", "dialog.import-dialog"),
    respondOk: PASS,
    ok: async ({ page }) => page.waitForFunction(() => !document.querySelector("dialog.import-dialog"), { timeout: 10000 }),
    error: async ({ page }) => waitText(page, "dialog.import-dialog [role=alert]", /Simulated failure/),
    hang: async ({ page }) => waitText(page, "dialog.import-dialog .action-status .action-elapsed", /^\d{2,} s so far$/),
  },
  // ---- Query editor ------------------------------------------------------------------
  {
    // The first run preserves the action's focus and shows progress in results.
    name: "editor-first-run", label: "Run (first run in a tab)", paths: ["/api/editor/run"], replyDelayMs: 2000,
    setup: async ({ page }) => { await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(400); },
    target: "[data-action=run]",
    ok: async ({ page }) => waitText(page, ".query-lab .results-tabs [role=tab]", /Result 1/),
    error: async ({ page }) => waitText(page, ".query-lab [role=alert]", /Simulated failure/),
    hang: async ({ page }) => waitText(page, ".query-lab .results-first-run .action-elapsed", /^\d{2,} s so far$/),
  },
  {
    // A tab that already has a result: the run is announced at its end, and a
    // run refused with an error never announces the previous run's counts.
    name: "editor-run-again", label: "Run (a tab with a result)", paths: ["/api/editor/run"], replyDelayMs: 2000,
    setup: async ({ page }) => {
      await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(300);
      await page.click("[data-action=run]");
      await waitText(page, ".query-lab .results-tabs [role=tab]", /Result 1/);
      await pause(300);
    },
    target: "[data-action=run]",
    ok: async ({ page }) => waitText(page, ".query-lab .results-pane > [role=status]", /^Run finished: 1 statement, 1 row\.$/),
    error: async ({ page }) => {
      await waitText(page, ".query-lab [role=alert]", /Simulated failure/);
      await waitNotBusy(page, ".query-lab .results-pane");
      assert.equal(await page.$eval(".query-lab .results-pane > [role=status]", (e) => e.textContent), "", "a refused run announces no old counts");
    },
    hang: async ({ page }) => waitText(page, ".query-lab .results-running .action-elapsed", /^\d{2,} s so far$/),
  },
  {
    name: "editor-load-schemas", label: "Load schemas", paths: ["/api/live/schemas"], busy: ".schema-browser .browser-head [aria-busy]",
    setup: async ({ page }) => { await goPlace(page, "query"); await pause(300); },
    target: named("Load schemas", ".schema-browser"),
    ok: async ({ page }) => visible(page, ".schema-browser .browser-tree [role=treeitem]"),
    error: async ({ page }) => { await waitText(page, ".schema-browser .action-status", /Simulated failure/); await named("Try again", ".schema-browser")(page); },
    hang: async ({ page }) => waitText(page, ".schema-browser .action-status .action-elapsed", /^\d{2,} s so far$/),
    double: false,
  },
  {
    name: "editor-history", label: "Show the run history", paths: ["/api/editor/history"], busy: ".history-panel",
    setup: async ({ page }) => { await goPlace(page, "query"); await pause(300); },
    target: 'button[aria-label="Show the run history"]',
    ok: async ({ page }) => waitText(page, ".history-panel .browser-status", /Runs appear here\./),
    error: async ({ page }) => { await waitText(page, ".history-panel .action-status", /Simulated failure/); await named("Try again", ".history-panel")(page); },
    hang: async ({ page }) => waitText(page, ".history-panel .action-status .action-elapsed", /^\d{2,} s so far$/),
    double: false,
  },
  {
    name: "editor-export-csv", label: "Export result (CSV)", paths: ["/api/editor/export"],
    setup: async ({ page }) => {
      await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(300);
      await page.click("[data-action=run]");
      await waitText(page, ".query-lab .results-tabs [role=tab]", /Result 1/);
      await click(page, "Export", ".results-tabs-actions"); await pause(250);
    },
    target: named("CSV", ".export-actions"),
    ok: async ({ page }) => waitText(page, ".query-lab", /Result 1 exported as myflames-result\.csv\./),
    error: async ({ page }) => waitText(page, ".query-lab", /Simulated failure: editor\/export/),
    hang: async ({ page }) => waitText(page, ".export-status .action-elapsed", /^\d{2,} s so far$/),
  },
  // ---- Writes behind a review -------------------------------------------------------
  {
    name: "table-data-apply", label: "Table data: Apply changes (confirm)", paths: ["/api/table-data/apply"], sheet: true,
    mocks: TABLE_MOCKS,
    setup: async ({ page }) => {
      await goPlace(page, "query"); await pause(300);
      await click(page, "Load schemas", ".schema-browser");
      await (await visible(page, ".tree-row.kind-schema")).click();
      const table = await visible(page, ".tree-row.kind-table");
      await table.click({ button: "right" });
      await click(page, "Edit table data…");
      await visible(page, ".table-data-panel input");
      await page.locator('input[aria-label="name for first"]').fill("Bob");
      await click(page, "Review 1 changes", ".table-data-panel");
      await page.waitForSelector("dialog.confirm-sheet[open]");
    },
    target: confirmButton("dialog.confirm-sheet[open]"),
    ok: async ({ page }) => waitText(page, ".table-data-panel .table-data-live", /Changes committed/),
    // A refusal before anything ran is a failure in the server's words, not an unknown outcome.
    error: async ({ page }) => {
      await waitText(page, ".table-data-panel [role=alert]", /^Apply failed: Simulated failure/);
      assert.equal(await page.$(".table-data-panel button ::-p-text(Check status)"), null, "no status check after a refusal");
    },
    hang: async ({ page }) => {
      await click(page, "Close", "dialog.confirm-sheet[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet[open]"));
      await waitText(page, ".table-data-panel .table-data-step", /Applying the reviewed changes/);
    },
  },
  {
    name: "accounts-apply", label: "Users and roles: Apply (review sheet)", paths: ["/api/accounts/apply"], sheet: true,
    mocks: ACCOUNT_MOCKS,
    setup: async ({ page }) => {
      await goView(page, "server", "accounts");
      // Wait for the automatic first read before asking Refresh to read again.
      await visible(page, ".accounts-view tbody tr[data-row-key]");
      await waitNotBusy(page, REFRESH("accounts"));
      await (await visible(page, REFRESH("accounts"))).click();
      await waitNotBusy(page, REFRESH("accounts"));
      const row = await visible(page, ".accounts-view tbody tr[data-row-key]");
      await row.click();
      await click(page, "Lock", ".view-inspector");
      await page.waitForSelector("dialog.account-review[open]");
    },
    target: confirmButton("dialog.account-review[open]"),
    ok: async ({ page }) => waitText(page, ".accounts-view .account-result", /1 group applied/),
    error: async ({ page }) => waitText(page, ".accounts-view", /Simulated failure/),
    hang: async ({ page }) => {
      await click(page, "Close", "dialog.account-review[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.account-review[open]"));
      await waitText(page, `${STATUS("accounts")} .capture-status`, /Applying account changes…/);
    },
  },
  {
    name: "locks-kill", label: "Kill blocker (confirm)", paths: ["/api/live/kill"], sheet: true,
    mocks: { "/api/live/locks": () => LOCKS_DDL },
    setup: async ({ page }) => {
      await goView(page, "server", "locks");
      const row = await visible(page, '.server-view[data-view="locks"] li[data-blocker="53"]');
      await ((await row.$("[data-row-key]")) || row).click();
      await (await visible(page, '.view-inspector [aria-label="Kill blocker: connection 53"]')).click();
      await page.waitForSelector("dialog.kill-dialog[open]");
    },
    target: confirmButton("dialog.kill-dialog[open]"),
    ok: async ({ page }) => waitText(page, ".server-view[data-view=locks] .kill-result", /^Done\./),
    error: async ({ page }) => waitText(page, ".server-view[data-view=locks] .kill-result", /Could not end it\. Simulated failure/),
    hang: async ({ page, ctx }) => {
      await waitText(page, "dialog.kill-dialog[open] .sheet-progress", /^No reply yet/);
      await waitText(page, "dialog.kill-dialog[open] .sheet-leave-note", /^The KILL may still be running/);
      await click(page, "Close", "dialog.kill-dialog[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog[open]"));
      await waitText(page, ".server-view[data-view=locks] .view-status .action-status", /Waiting for the reply to the KILL of connection 53\./);
      // Another connection in use: the waiting line and the late answer name
      // the connection the KILL was sent on, and offer no Refresh of this one.
      const sentOn = await switchConnection(page);
      await waitText(page, ".server-view[data-view=locks] .view-status .action-status",
        new RegExp(`Waiting for the reply to the KILL of connection 53 on ${escapeRe(sentOn)}\\.`));
      const kill = ctx.held.splice(ctx.held.findIndex((h) => h.path === "/api/live/kill"), 1)[0];
      respond(kill.request, 200, KILL_OK);
      await waitText(page, ".server-view[data-view=locks] .kill-result", /^Done\. Ended connection 53/);
      await waitText(page, ".server-view[data-view=locks] .kill-result .kill-result-on",
        new RegExp(`^Sent on ${escapeRe(sentOn)}, not the connection in use\\.$`));
      assert.equal(await page.$(".server-view[data-view=locks] .kill-result ::-p-text(Refresh)"), null,
        "no Refresh in a note about another connection");
    },
  },
  {
    name: "kill-sessions-review", label: "Kill sessions…: Kill N (prepares the review)", paths: ["/api/live/kill/bulk/prepare"],
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await click(page, "Runaway queries (> 30s)"); await pause(250);
    },
    target: ".mass-kill-footer-actions .action-button",
    respondOk: PASS,
    ok: async ({ page }) => page.waitForSelector("dialog.mass-kill-confirm[open]"),
    error: async ({ page }) => waitText(page, ".mass-kill-error[role=alert]", /Could not prepare the review: Simulated failure.*Nothing was sent\./),
    hang: async ({ page }) => {
      await waitText(page, ".mass-kill-footer .action-elapsed", /^\d{2,} s so far$/);
      await click(page, "Stop waiting", ".mass-kill-footer");
      await waitText(page, ".mass-kill-error", /Stopped waiting for the review\. Nothing was sent\./);
    },
  },
  {
    name: "kill-sessions-send", label: "Kill sessions…: Send N statements (confirm)", paths: ["/api/live/kill/bulk"], sheet: true,
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await click(page, "Runaway queries (> 30s)"); await pause(250);
      await page.click(".mass-kill-footer-actions .action-button");
      await page.waitForSelector("dialog.mass-kill-confirm[open]");
    },
    target: confirmButton("dialog.mass-kill-confirm[open]"),
    afterClick: async ({ ctx }) => {
      ctx.hold.add("/api/live/sessions");
      ctx.massKillReads = ctx.log.filter((entry) => entry.path === "/api/live/sessions").length;
    },
    ok: async ({ page, ctx }) => {
      await waitText(page, ".server-view[data-view=sessions] .kill-result", /^Done\. Kill sessions finished: \d+ killed/);
      await focusKillOutcome(page);
      await heldFor(ctx, ["/api/live/sessions"]);
    },
    error: async ({ page }) => waitText(page, ".mass-kill-error[role=alert]", /Could not send the KILL statements: Simulated failure/),
    hang: async ({ page, ctx }) => {
      await waitText(page, "dialog.mass-kill-confirm[open] .sheet-progress", /^No reply yet/);
      await waitText(page, "dialog.mass-kill-confirm[open] .sheet-leave-note", /^The KILLs may still be running/);
      await click(page, "Close", "dialog.mass-kill-confirm[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"));
      await waitText(page, ".server-view[data-view=sessions] .kill-result", /^Waiting for the reply\./);
      assert.equal(ctx.log.filter((entry) => entry.path === "/api/live/sessions").length, ctx.massKillReads,
        "closing a pending bulk send does not reread Processlist");
      // One bulk KILL at a time: a dialog opened meanwhile says so and sends nothing.
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await waitText(page, ".mass-kill-earlier", /has not answered yet/);
      await click(page, "Runaway queries (> 30s)"); await pause(200);
      assert.equal(await page.$eval(".mass-kill-footer-actions .action-button", (e) => e.disabled), true, "Kill N waits for the earlier send");
      // The earlier reply arrives: it shows in this dialog, which stays open,
      // and reaches Processlist when this one closes. The connection is still
      // the same, so the note must not suggest it belongs to another one.
      const bulk = ctx.held.splice(ctx.held.findIndex((h) => h.path === "/api/live/kill/bulk"), 1)[0];
      respond(bulk.request, 200, DEFAULTS["/api/live/kill/bulk"](bulk.body));
      await waitText(page, ".mass-kill-earlier.is-answered", /^The earlier Kill sessions request on .+ answered: Done\. Kill sessions finished: \d+ killed/);
      await pause(300);
      assert.ok(await page.$("dialog.mass-kill-dialog[open]"), "the late reply does not close the dialog opened since");
      await page.waitForFunction(() => document.querySelector(".mass-kill-footer-actions .action-button")?.disabled === false, { timeout: 3000 })
        .catch(() => { throw Error("Kill N stays off after the earlier send answered"); });
      await click(page, "Cancel", ".mass-kill-footer-actions");
      await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"));
      await waitText(page, ".server-view[data-view=sessions] .kill-result", /^Done\. Kill sessions finished: \d+ killed/);
      assert.equal(await page.$(".server-view[data-view=sessions] .kill-result .kill-result-on"), null,
        "same-connection late note does not repeat its origin");
    },
  },
  {
    // Its owner closes the dialog while the send waits (the connection was
    // made read-only in another tab): the outcome is not lost, Processlist
    // says it waits, and the late reply never closes a dialog opened since.
    name: "kill-sessions-unmounted", label: "Kill sessions…: dialog closed by a connection change during the send", paths: ["/api/live/kill/bulk"],
    sheet: true, modes: ["ok", "error"], skipAnswer: true,
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await chooseConnection(page, "p-lab");
      await readSessionsHere(page);
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await click(page, "Runaway queries (> 30s)"); await pause(250);
      await page.click(".mass-kill-footer-actions .action-button");
      await page.waitForSelector("dialog.mass-kill-confirm[open]");
    },
    target: confirmButton("dialog.mass-kill-confirm[open]"),
    ok: async (run) => unmountedSend(run, 200),
    error: async (run) => unmountedSend(run, 400),
  },
  {
    name: "reviewed-sql-prepare", label: "Set invisible… (prepares the SQL review)", paths: ["/api/editor/run"],
    busy: '.server-view[data-view="indexes"] [data-reviewed-status]', mocks: { "/api/editor/run": reviewedRun }, double: false,
    setup: async ({ page }) => {
      await goView(page, "server", "indexes");
      await page.click('button[aria-label="Actions for mfshop.orders.k_status"]'); await pause(250);
    },
    target: named("Set invisible…"),
    ok: async ({ page }) => page.waitForSelector("dialog.confirm-sheet[open]"),
    error: async ({ page }) => waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status]', /Simulated failure/),
    hang: async ({ page }) => {
      await waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status] .action-elapsed', /^\d{2,} s so far$/);
      await click(page, "Stop waiting", '.server-view[data-view="indexes"] [data-reviewed-status]');
      await waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status]', /Stopped waiting for the review\. Nothing was sent\./);
    },
  },
  {
    name: "reviewed-sql-confirm", label: "Reviewed SQL confirm (Set invisible)", paths: ["/api/editor/run"], sheet: true,
    mocks: { "/api/editor/run": reviewedRun },
    setup: async ({ page }) => {
      await goView(page, "server", "indexes");
      await page.click('button[aria-label="Actions for mfshop.orders.k_status"]'); await pause(250);
      await click(page, "Set invisible…");
      await page.waitForSelector("dialog.confirm-sheet[open]");
    },
    target: confirmButton("dialog.confirm-sheet[open]"),
    ok: async ({ page }) => waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status]', /completed/),
    // A refusal (HTTP 400) is a failure in the server's words, never "outcome unknown".
    error: async ({ page }) => {
      await waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status]', /failed: Simulated failure/);
      assert.doesNotMatch(await page.$eval('.server-view[data-view="indexes"] [data-reviewed-status]', (e) => e.textContent), /unknown/i);
    },
    hang: async ({ page }) => {
      await click(page, "Close", "dialog.confirm-sheet[open]");
      await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet[open]"));
      await waitText(page, '.server-view[data-view="indexes"] [data-reviewed-status]', /Sending the reviewed statements/);
    },
  },
  {
    name: "audit-export-json", label: "Audit log: Export JSON", paths: ["/api/audit/export"],
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await click(page, "Settings"); await pause(250);
      await click(page, "Audit log"); await pause(900);
      await click(page, "Export the audit log"); await pause(250);
    },
    target: '[role=dialog][aria-label="Export the audit log"] button[data-format=json]',
    respondOk: PASS,
    ok: async ({ page }) => waitText(page, ".export-status", /^Saved myflames-audit-/),
    error: async ({ page }) => waitText(page, ".export-status", /Simulated failure/),
    hang: async ({ page }) => waitText(page, ".export-status .action-elapsed", /^\d{2,} s so far$/),
  },
  ...batchBScenarios({ goPlace, goView, visible, named, click, confirmButton, waitText, waitNotBusy, pause,
    viewRefresh, editorSession, editorRun, switchConnection, respond }),
];

/** After a switch the Processlist report is greyed until it is read on the
 * connection in use (Refresh, unless a read already ran). */
async function readSessionsHere(page) {
  const view = ".server-view[data-view=sessions]";
  await pause(400);
  if (await page.$eval(view, (e) => e.dataset.otherConnection === "true")) await page.click(`${view} [data-action=refresh]`);
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection !== "true" &&
    document.querySelector("[data-action=kill-sessions]")?.disabled === false, { timeout: 8000 }, view);
}
async function focusKillOutcome(page) {
  await page.waitForFunction(() => document.activeElement === document.querySelector(".server-view[data-view=sessions] .kill-result"),
    { timeout: 3000 }).catch(() => { throw Error("Kill outcome does not have focus"); });
}
/** kill-sessions-unmounted: the connection is made read-only elsewhere while the send waits. */
async function unmountedSend({ page, ctx }, status) {
  ctx.mocks["/api/profiles/list"] = PROFILES_LAB_READ_ONLY;
  await page.evaluate(() => { window.__clockOffset += 2000; window.dispatchEvent(new Event("focus")); });
  await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"), { timeout: 8000 })
    .catch(() => { throw Error("the connection change did not close Kill sessions"); });
  await waitText(page, ".server-view[data-view=sessions] .kill-result", /^Waiting for the reply\. Waiting for the reply to \d+ KILL statements on Lab primary/);
  // A new dialog, on the connection as it is now, waits for the earlier send.
  await readSessionsHere(page);
  await page.click("[data-action=kill-sessions]");
  await page.waitForSelector("dialog.mass-kill-dialog[open]");
  await waitText(page, ".mass-kill-earlier", /has not answered yet/);
  const bulk = ctx.held.splice(ctx.held.findIndex((h) => h.path === "/api/live/kill/bulk"), 1)[0];
  if (status === 200) respond(bulk.request, 200, DEFAULTS["/api/live/kill/bulk"](bulk.body));
  else respond(bulk.request, 400, { error: failure(bulk.path) });
  const outcome = status === 200 ? /Done\. Kill sessions finished: \d+ killed/ : /Could not end it\. Simulated failure: live\/kill\/bulk/;
  await waitText(page, ".mass-kill-earlier.is-answered", new RegExp(`^The earlier Kill sessions request on Lab primary .+ answered: ${outcome.source}`));
  await pause(300);
  assert.ok(await page.$("dialog.mass-kill-dialog[open]"), "the late reply does not close the dialog opened since");
  await click(page, "Cancel", ".mass-kill-footer-actions");
  await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"));
  await waitText(page, ".server-view[data-view=sessions] .kill-result", new RegExp(`^${outcome.source}`));
}

// ---- One run --------------------------------------------------------------------------

export async function runOne(browser, s, mode, shots) {
  const ws = await startWorkspace(s.name);
  let ctx;
  try {
    ctx = await openPage(browser, ws.url);
    Object.assign(ctx.mocks, s.mocks || {});
    ctx.mode = mode;
    const { page } = ctx;
    await s.setup(ctx);
    await pause(250);
    const handle = typeof s.target === "string" ? await visible(page, s.target) : await s.target(page);
    for (const p of s.paths) ctx.hold.add(p);
    const before = ctx.log.length;
    await page.evaluate(() => { const P = window.__af; P.tClick = null; P.tFirst = null; P.first = ""; P.tBusy = null; P.armed = true; });
    // A double click: two clicks on the same spot, as fast as the browser takes them.
    const box = await handle.boundingBox();
    assert.ok(box, "the control is on screen");
    const x = box.x + box.width / 2, y = box.y + box.height / 2;
    const clickedAt = Date.now();
    await page.mouse.click(x, y);
    if (s.double !== false) await page.mouse.click(x, y);
    if (!s.local) await heldFor(ctx, s.paths);
    // Busy while it waits: on the control (or the region named by s.busy).
    const busySelector = s.busy || null;
    if (s.local) await pause(50); // A synchronous download has no pending network request.
    else if (busySelector) await page.waitForFunction((sel) => [...document.querySelectorAll(sel)].some((e) => e.getAttribute("aria-busy") === "true"), { timeout: 3000 }, busySelector)
      .catch(() => { throw Error(`not aria-busy while waiting: ${busySelector}`); });
    else {
      // The busy state can render a moment after the request leaves (a read
      // started from an effect): wait for it, briefly.
      const busy = await page.waitForFunction((e) => e.getAttribute("aria-busy") === "true", { timeout: 2000 }, handle).then(() => true, () => false);
      const html = await handle.evaluate((e) => (e.isConnected ? "" : "(detached) ") + e.outerHTML.slice(0, 160));
      assert.ok(busy, `the control is aria-busy while waiting: ${html}`);
    }
    const probe = await page.evaluate(() => ({ ...window.__af }));
    assert.ok(probe.tClick !== null && probe.tFirst !== null, `a visible change after the click (${JSON.stringify(probe)})`);
    const firstMs = Math.round(probe.tFirst - probe.tClick);
    assert.ok(firstMs <= FEEDBACK_MS, `first visible change ${firstMs} ms after the click (${probe.first}); at most ${FEEDBACK_MS} ms`);
    if (s.focus !== false) {
      const focus = await page.evaluate(() => document.activeElement === document.body || !document.activeElement ? "body" : document.activeElement.outerHTML.slice(0, 80));
      assert.notEqual(focus, "body", "focus stays on a control, never the page");
    }
    if (s.afterClick) await s.afterClick({ page, ctx, mode });
    await pause(s.replyDelayMs ?? s.delayMs ?? 350);
    if (s.oneRequest !== false && !s.local) {
      const sent = ctx.log.slice(before).filter((e) => s.paths.includes(e.path)).length;
      assert.equal(sent, 1, `one request for ${s.double === false ? "the click" : "a double click"}`);
    }
    if (mode !== "hang" && s.replyDelayMs) await pause(Math.max(0, s.replyDelayMs - (Date.now() - clickedAt)));
    // A scenario can also hold the read after a successful write. Release only
    // the action's paths; its result must show while that follow-up still waits.
    const releaseAction = () => {
      for (const p of s.paths) ctx.hold.delete(p);
      const action = ctx.held.filter((entry) => s.paths.includes(entry.path));
      ctx.held = ctx.held.filter((entry) => !s.paths.includes(entry.path));
      return action;
    };
    if (mode === "ok") {
      if (!s.skipAnswer) {
        for (const h of releaseAction()) {
          if (s.respondOk === PASS) answer(ctx, h.path, h.body, h.request);
          else if (s.respondOk) respond(h.request, 200, s.respondOk(h.body));
          else answer(ctx, h.path, h.body, h.request);
        }
      }
      await s.ok({ page, ctx });
    } else if (mode === "error") {
      if (!s.skipAnswer) {
        for (const h of releaseAction()) {
          if (s.respondError) await s.respondError(h.request);
          else respond(h.request, s.errorStatus ?? 400, { error: failure(h.path) });
        }
      }
      await s.error({ page, ctx });
    } else {
      await page.evaluate((ms) => { window.__clockOffset += ms; }, s.slowShiftMs ?? SLOW_SHIFT_MS);
      await pause(1300);
      await s.hang({ page, ctx });
    }
    assert.deepEqual(ctx.errors, [], "no page errors");
    if (shots) await page.screenshot({ path: path.join(shots, `${s.name}-${mode}.png`) });
    return { name: s.name, mode, firstMs };
  } catch (e) {
    if (ctx) {
      const dir = shots || await fs.mkdtemp(path.join(os.tmpdir(), "action-feedback-fail-"));
      await ctx.page.screenshot({ path: path.join(dir, `${s.name}-${mode}-FAILED.png`) }).catch(() => {});
      e.message += ` (screenshot in ${dir})`;
    }
    throw e;
  } finally {
    if (ctx) {
      for (const h of ctx.held.splice(0)) await h.request.abort().catch(() => {});
      await ctx.page.close().catch(() => {});
    }
    await ws.close();
  }
}
