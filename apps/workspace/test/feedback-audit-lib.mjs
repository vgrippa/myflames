// Action feedback audit: the real workspace server on a temporary, seeded
// workspace database, with every live, lifecycle, network and editor endpoint
// mocked. A scenario opens the screen that owns an action, then the action's
// endpoints answer one of three ways (after 2 s, an error after 1 s, or never
// within 15 s) while an in-page probe records the first visible change after
// the click, busy and live-region states, requests sent, focus and the result.
// Read-only on the app: nothing here changes src/.
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

export const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));
export const pause = (ms) => new Promise((r) => setTimeout(r, ms));
export const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));

// ---- Mocked answers -----------------------------------------------------------------

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
export const FIX = {
  replicationSkip: await load("replication-mysql-8.4-skip.json"),
  loopRunning: await load("replication-loop-running.json"),
  loopStopped: await load("replication-loop-stopped.json"),
  dumpPlan: await load("lifecycle-plan-dump.json"),
  restorePlan: await load("lifecycle-plan-restore.json"),
  clonePlan: await load("lifecycle-plan-clone.json"),
  jobRunning: await load("lifecycle-job-dump-running.json"),
  jobCancelled: await load("lifecycle-job-dump-cancelled.json"),
  jobDone: await load("lifecycle-job-dump-done.json"),
  upgradeNative: await load("upgrade-native-8.0.46-to-8.4.0.json"),
  variable: await load("variable-mysql-8.4-global.json"),
  clusterPlan: await load("cluster-plan-switchover.json"),
  proxyPlan: await load("proxysql-plan-offline-soft.json"),
  proxyDone: await load("proxysql-result-done.json"),
  instrumentsSel: await load("instruments-selection.json"),
};

const A = "a".repeat(64);
const statementRow = (over) => ({
  digest: A, digest_text: "SELECT ?", schema: "shop", count: 120, total_ms: 5400.5, avg_ms: 45, max_ms: 90,
  rows_examined: 360000, rows_sent: 1200, examined_per_sent: 300, no_index_used: 120, no_good_index_used: 0,
  tmp_disk_tables: 0, sort_merge_passes: 0, sample_text: null, sample_truncated: false,
  first_seen: "2026-10-12 10:00:00", last_seen: "2026-10-12 11:00:00", flags: ["no_index_used"], capture_problem: null, ...over,
});
const TOP = (body = {}) => ({
  schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: body.order || "total", limit: body.limit || 25,
  schema: body.schema || null, notes: [], table: body.table || null,
  coverage: { uptime_s: 192385, first_seen: "2026-10-12 10:00:00", digest_lost: 0, overflow: null },
  statements: [
    statementRow({ digest: A, digest_text: "SELECT * FROM `orders` WHERE STATUS = ? ORDER BY `total` DESC LIMIT ?",
      sample_text: "SELECT * FROM orders WHERE status = 'new' ORDER BY total DESC LIMIT 10" }),
    statementRow({ digest: "b".repeat(64), digest_text: "SELECT `name` FROM `customers` WHERE `country` = ?",
      sample_text: "SELECT name FROM customers WHERE country = 'US'", total_ms: 900 }),
    statementRow({ digest: "c".repeat(64), digest_text: "UPDATE `orders` SET STATUS = ? WHERE `id` = ?",
      sample_text: "UPDATE orders SET status = 'paid' WHERE id = 1", total_ms: 400,
      capture_problem: "Capture accepts one SELECT or WITH … SELECT statement." }),
  ],
});
let runN = 0;
export function editorSession(over = {}) {
  return {
    schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
    connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
    engine: "mysql", server_version: "8.4.3", connection_id: 42, autocommit: true, in_transaction: false,
    transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY", backslash_escapes: true,
    safe_updates: true, safe_updates_default: true, read_only_transactions: false, started_at: "2026-10-15T12:00:00+00:00",
    idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null, message: null, notes: [], run: null, ...over,
  };
}
export function editorRun(sql = "SELECT 1") {
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
const NEWS = {
  schema_version: "news-1.0", enabled: true, fetched_at: hours(0), stale: false,
  items: [{ title: "Troubleshooting MySQL with OpenTelemetry Metrics", link: "https://blogs.oracle.com/mysql/otel", engine: "mysql",
    published: hours(5), summary: "s", source: "Planet MySQL", source_id: "planet-mysql" },
  { title: "MariaDB Foundation Newsletter", link: "https://mariadb.org/newsletter/", engine: "mariadb", published: hours(30),
    summary: "s", source: "MariaDB Foundation", source_id: "mariadb-foundation" }],
  sources: [{ id: "planet-mysql", name: "Planet MySQL", engine: "mysql", url: "https://planet.mysql.com/rss20.xml", fetched_at: hours(0), checked_at: hours(0), items: 1 }],
};
const UPDATE = () => ({
  schema_version: "app-update-1.0", current_version: "3.1.0", latest_version: "3.2.0", update_available: true,
  release_title: "myflames 3.2.0", release_notes: "Notes", published_at: "", html_url: "https://github.com/vgrippa/myflames/releases",
  asset_name: "myflames-3.2.0-arm64.dmg", asset_url: "https://github.com/vgrippa/myflames/releases/download/v3.2.0/x.dmg", asset_size: 1000,
  checked_at: new Date().toISOString(),
  last_check: { checked_at: new Date().toISOString(), outcome: "available", current_version: "3.1.0", latest_version: "3.2.0", reason: null },
});
const ACCOUNTS = {
  schema_version: "accounts-1.0", engine: "mysql", version: "8.4.11", complete: true, warnings: [],
  accounts: [
    { user: "app", host: "%", kind: "user", locked: false, password_expired: false, plugin: "caching_sha2_password", protected: false },
    { user: "reporting", host: "10.%", kind: "user", locked: false, password_expired: false, plugin: "caching_sha2_password", protected: false },
  ],
  role_grants: [], default_roles: [],
};
const DIAGRAM = (body = {}) => ({ schema_version: "schema-diagram-1.0", warnings: [], relationships: [], tables: [
  { id: "shop.orders", schema: body.database || "shop", name: "orders", columns: [{ name: "id", data_type: "bigint", nullable: false, primary_key: true }] },
  { id: "shop.customers", schema: body.database || "shop", name: "customers", columns: [{ name: "id", data_type: "bigint", nullable: false, primary_key: true }] },
] });

const SAMPLE_RAW = JSON.stringify({ query: "SELECT * FROM orders", operation: "Table scan on orders", access_type: "table",
  table_name: "orders", estimated_rows: 1000, estimated_total_cost: 101 });
/** path -> (body) => response body (status 200), or PASS (the real server). */
export const PASS = Symbol("pass");
export const DEFAULTS = {
  ...Object.fromEntries(Object.entries(F).map(([p, v]) => [p, () => v])),
  "/api/live/top": (b) => TOP(b),
  "/api/live/slow-queries": () => ({ schema_version: "slow-queries-1.0", engine: "mysql", statements: [], notes: [] }),
  "/api/live/privileges": () => ({ schema_version: "privileges-1.0", account: "root@localhost", actions: {} }),
  "/api/live/replication/skip-loop/status": () => ({ schema_version: "replication-skip-loop-1.0", loops: [] }),
  "/api/lifecycle/jobs": () => ({ schema_version: "lifecycle-jobs-1.0", jobs: [] }),
  "/api/accounts/list": () => ACCOUNTS,
  "/api/schema-diagram/read": (b) => DIAGRAM(b),
  "/api/news": () => NEWS,
  "/api/app/update/check": () => UPDATE(),
  "/api/editor/status": (b) => (b.run_id ? { status: "complete", session: editorSession(), run: editorRun() } : { status: "none", session: null, run: null }),
  "/api/editor/preview": () => preview(),
  "/api/editor/run": (b) => ({ status: "complete", session: editorSession(), run: editorRun(b.sql) }),
  "/api/editor/stop": () => ({ stopped: true, stop_requested: true, message: "KILL QUERY sent.", session: editorSession({ state: "running" }) }),
  "/api/editor/close": () => ({ closed: true, session: null }),
  "/api/editor/open": () => ({ status: "idle", session: editorSession() }),
  "/api/editor/history": (b) => (b.action === "clear" ? { cleared: 0 } : { items: [] }),
  "/api/editor/export": (b) => ({ format: b.format, content: "a,b\r\n", mime: "text/csv", filename: `myflames-result.${b.format}` }),
  "/api/live/catalog": (b) => (b.table
    ? { schema_version: "catalog-1.0", level: "table", schema: b.schema, table: b.table,
      columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "", generated: false }],
      indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }] }
    : { schema_version: "catalog-1.0", level: "tables", schema: b.schema, truncated: false, limit: 200, prefix: null, after: null,
      next_after: null, tables: [{ name: "orders", type: "table" }, { name: "customers", type: "table" }] }),
  "/api/live/start": () => ({ id: "job-1", state: "queued" }),
  "/api/live/status": (b) => ({ id: b.id, state: "done", progress: 1, repeat: 1, error: "", result: {
    raw: SAMPLE_RAW, query: "SELECT * FROM orders", mode: "explain", engine: "mysql", server_version: "8.4.11",
    captured_at: "2026-10-21T10:00:00Z", runs: [], measurements: { count: 0, median_ms: null, min_ms: null, max_ms: null, stdev_ms: null },
    schema: {}, table_stats: {}, variables: {}, trace: null, warnings: [], conditions: "" } }),
  "/api/connection/test": () => ({ version: "8.4.11", schemas: ["shop", "mftab"] }),
  "/api/live/cancel": (b) => ({ id: b.id, state: "cancelled" }),
  "/api/audit/declined": () => ({ audit_id: "declined-1", status: "declined" }),
  "/api/lifecycle/choose-directory": () => ({ path: "/tmp/myflames-dump-target", cancelled: false }),
};
// Real server: session, profiles, plans, investigations, Learn, audit log.
const REAL = [/^\/api\/auth\//, /^\/api\/session\//, /^\/api\/profiles\//, /^\/api\/(analyze|render|report|compare)$/,
  /^\/api\/investigations\//, /^\/api\/teach\//, /^\/api\/audit\/(list|export|settings)$/, /^\/api\/news\/settings$/,
  /^\/api\/app\/update\/status$/, /^\/api\/report\/export$/, /^\/api\/analysis\//, /^\/api\/live\/kill\/(bulk\/)?prepare$/];

// ---- In-page probe --------------------------------------------------------------------

function installProbe() {
  const P = (window.__probe = { armed: false, t0: 0, muts: [], live: [], fetches: [], focus: [] });
  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input.url;
    P.fetches.push({ t: performance.now(), url: new URL(url, location.href).pathname });
    return origFetch(input, init);
  };
  const shown = (n) => {
    const el = n && (n.nodeType === 1 ? n : n.parentElement);
    if (!el || !el.isConnected || el.closest("[hidden]")) return false;
    return el.getClientRects().length > 0;
  };
  const desc = (el) => {
    if (!el || el.nodeType !== 1) return el ? "#text" : "";
    const cls = typeof el.className === "string" && el.className.trim() ? "." + el.className.trim().split(/\s+/).slice(0, 3).join(".") : "";
    return `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${cls}`;
  };
  P.desc = desc;
  const LIVE = '[role=status],[role=alert],[aria-live]:not([aria-live=off]),[role=log],[role=progressbar],[role=alertdialog]';
  const recordLive = (el, how, t) => {
    const text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 240);
    const role = el.getAttribute("role") || `aria-live=${el.getAttribute("aria-live")}`;
    const last = P.live[P.live.length - 1];
    if (last && last.text === text && last.role === role) return;
    P.live.push({ t: t - P.t0, role, how, text, where: desc(el), visible: shown(el) });
  };
  new MutationObserver((list) => {
    if (!P.armed) return;
    const t = performance.now();
    for (const m of list) {
      const target = m.target.nodeType === 1 ? m.target : m.target.parentElement;
      if (m.type === "childList") {
        for (const n of m.addedNodes) {
          if (n.nodeType !== 1) continue;
          const regions = [...(n.matches(LIVE) ? [n] : []), ...n.querySelectorAll(LIVE)];
          for (const r of regions) if ((r.textContent || "").trim()) recordLive(r, "inserted", t);
        }
      }
      const live = target && target.closest && target.closest(LIVE);
      if (live && live.isConnected) recordLive(live, "updated", t);
      const visibleChange = m.type === "childList"
        ? (shown(m.target) && ([...m.addedNodes].some((n) => n.nodeType !== 1 || shown(n)) || m.removedNodes.length > 0))
        : shown(m.target);
      if (visibleChange && P.muts.length < 400)
        P.muts.push({ t: t - P.t0, type: m.type, attr: m.attributeName || "", target: desc(target) });
    }
  }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true,
    attributeFilter: ["disabled", "aria-busy", "aria-disabled", "hidden", "class", "aria-expanded", "open", "aria-pressed", "value", "style"] });
}

/** What is on screen now (visible text lines, busy markers, focus, regions). */
async function snapshot(page, targetHandle) {
  return page.evaluate((target) => {
    const P = window.__probe;
    const shown = (el) => el && el.isConnected && !el.closest("[hidden]") && el.getClientRects().length > 0;
    const vis = (sel) => [...document.querySelectorAll(sel)].filter(shown);
    const txt = (el) => (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 300);
    const host = (el) => {
      const c = el.closest("dialog, .server-view, [role=dialog], .query-lab, .home, .connections, .view-inspector, section, header, main, aside");
      return c ? P.desc(c) : "";
    };
    const a = document.activeElement;
    return {
      lines: document.body.innerText.split("\n").map((s) => s.trim()).filter(Boolean),
      spinners: vis(".spinner, .source-refreshing, [class*=progress], progress").map((e) => P.desc(e.parentElement || e) + " in " + host(e)).slice(0, 6),
      busy: vis("[aria-busy=true]").map((e) => P.desc(e)).slice(0, 6),
      status: vis("[role=status],[aria-live=polite],[role=progressbar]").map((e) => txt(e)).filter(Boolean).slice(0, 8),
      alerts: vis("[role=alert],[aria-live=assertive]").map((e) => `${txt(e)} @ ${host(e)}`).filter(Boolean).slice(0, 6),
      dialogs: vis("dialog[open], [role=dialog]:not(.popover), [role=alertdialog]").map((e) => P.desc(e) + ": " + txt(e).slice(0, 120)).slice(0, 4),
      focus: a ? `${P.desc(a)}${a.getAttribute("aria-label") ? ` "${a.getAttribute("aria-label")}"` : a.textContent && a.textContent.length < 50 ? ` "${a.textContent.trim()}"` : ""}${a.disabled ? " (disabled)" : ""}${shown(a) || a === document.body ? "" : " (not visible)"}` : "none",
      target: target
        ? { connected: target.isConnected, visible: shown(target), disabled: !!target.disabled,
          aria: target.getAttribute("aria-disabled") || "", busy: target.getAttribute("aria-busy") || "",
          label: txt(target).slice(0, 80) }
        : null,
      cursor: getComputedStyle(document.body).cursor,
    };
  }, targetHandle || null);
}

// ---- Workspace ----------------------------------------------------------------------

export async function startWorkspace(name) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), `myflames-feedback-${name}-`));
  const store = path.join(directory, "workspace.sqlite3");
  const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault", MYFLAMES_TEST_UPDATE: "99.0.0" };
  for (const script of ["feedback-audit-seed.py", "audit-seed.py"]) {
    const r = spawnSync(process.env.PYTHON || "python3", [path.join(root, "apps/workspace/test", script), store], { cwd: root, env });
    if (r.status !== 0) throw Error(`${script}: ${r.stderr}`);
  }
  const proc = spawn(process.env.PYTHON || "python3",
    ["-c", "import sys; from myflames.ui import WorkspaceServer; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()", store],
    { cwd: root, env });
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 15000);
    proc.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on("error", reject);
  });
  return { url, directory, async close() { proc.kill(); await fs.rm(directory, { recursive: true, force: true }); } };
}

/** A page on a fresh workspace. ctx.modes[path] = "ok" | "error" | "hang"
 * changes how *path* answers from then on; ctx.mocks[path] overrides DEFAULTS. */
export async function openPage(browser, url, { width = 1440, height = 900 } = {}) {
  const page = await browser.newPage();
  const ctx = { page, modes: {}, mocks: {}, log: [], failed: [], held: [], errors: [], okDelay: 2000, errorDelay: 1000 };
  page.on("pageerror", (e) => ctx.errors.push(String(e)));
  page.on("requestfailed", (r) => { const p = new URL(r.url()).pathname; if (p.startsWith("/api/")) ctx.failed.push({ t: Date.now(), path: p, why: r.failure()?.errorText }); });
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/")) return request.continue();
    let body = {};
    try { body = JSON.parse(request.postData() || "{}"); } catch { body = {}; }
    ctx.log.push({ t: Date.now(), path: p, body });
    const respond = (status, b) => request.respond({ status, contentType: "application/json", body: JSON.stringify(b) }).catch(() => {});
    const real = REAL.some((r) => r.test(p));
    const answer = () => {
      const mock = ctx.mocks[p] || DEFAULTS[p];
      if (mock) {
        const r = mock(body, request);
        if (r === PASS) return request.continue().catch(() => {});
        if (r && r.__status) return respond(r.__status, r.body);
        return respond(200, r);
      }
      if (real) return request.continue().catch(() => {});
      return respond(400, { error: `Not mocked in the feedback audit: ${p}` });
    };
    const mode = ctx.modes[p];
    if (mode === "hang") { ctx.held.push({ path: p, request }); return; }
    if (mode === "error") return void setTimeout(() => respond(400, { error: `Simulated failure: ${p.replace("/api/", "")} could not reach the server (feedback audit).` }), ctx.errorDelay);
    if (mode === "ok") return void setTimeout(answer, ctx.okDelay);
    answer();
  });
  await page.evaluateOnNewDocument(installProbe);
  await page.setViewport({ width, height });
  await page.goto(url, { waitUntil: "networkidle0" });
  return ctx;
}

const visibleHandle = async (page, selector) => {
  const all = await page.$$(selector);
  for (const h of all) if (await h.evaluate((e) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0)) return h;
  return null;
};
export async function waitVisible(page, selector, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const h = await visibleHandle(page, selector);
    if (h) return h;
    await pause(50);
  }
  throw Error(`not visible: ${selector}`);
}
export async function clickText(page, text, scope = "") {
  await page.locator(`${scope} :is(button,[role=menuitem],[role=tab],a,summary) ::-p-text(${text})`).setTimeout(8000).click();
}

/** Run one measured action: arm the probe, click *target* (selector or
 * async function returning a handle), click again at 300 ms, then look at
 * 150 ms, 1 s, and after the answer (ok/error) or at 15 s (hang). */
export async function measure(ctx, { target, paths, mode, shots, name, after, settle, noSecondClick }) {
  const { page } = ctx;
  for (const p of paths) ctx.modes[p] = mode;
  const handle = typeof target === "function" ? await target(page) : await waitVisible(page, target);
  if (!handle) throw Error("no target");
  const before = await snapshot(page, handle);
  const beforeLines = new Set(before.lines);
  const logStart = ctx.log.length;
  await page.evaluate(() => { const P = window.__probe; P.muts = []; P.live = []; P.fetches = []; P.t0 = performance.now(); P.armed = true; });
  const clickAt = Date.now();
  await handle.click().catch(async () => { await handle.evaluate((e) => e.click()); });
  await pause(150);
  const at150 = await snapshot(page, handle);
  if (shots) await page.screenshot({ path: path.join(shots, `${name}-${mode}-150ms.png`) });
  await pause(150);
  // Second click on the same control (if it is still there).
  const sentBeforeSecond = ctx.log.slice(logStart).filter((e) => paths.includes(e.path)).length;
  let second = "not attempted";
  if (noSecondClick) second = "skipped";
  else if (await handle.evaluate((e) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0).catch(() => false)) {
    second = (await handle.evaluate((e) => e.disabled || e.getAttribute("aria-disabled") === "true")) ? "control disabled" : "clicked";
    if (second === "clicked") await handle.click().catch(() => {});
  } else second = "control gone";
  await pause(700);
  const at1s = await snapshot(page, handle);
  const wait = mode === "ok" ? ctx.okDelay + 1200 : mode === "error" ? ctx.errorDelay + 1200 : 15000;
  await pause(wait - 1000);
  if (settle) await settle(page).catch(() => {});
  const end = await snapshot(page, handle);
  if (shots) await page.screenshot({ path: path.join(shots, `${name}-${mode}-end.png`) });
  const probe = await page.evaluate(() => { const P = window.__probe; P.armed = false; return { muts: P.muts, live: P.live, fetches: P.fetches }; });
  const sent = ctx.log.slice(logStart).filter((e) => paths.includes(e.path));
  const firstVisible = probe.muts.find((m) => m.t >= 0);
  const result = {
    name, mode, firstChangeMs: firstVisible ? Math.round(firstVisible.t) : null, firstChange: firstVisible ? `${firstVisible.type}${firstVisible.attr ? `[${firstVisible.attr}]` : ""} ${firstVisible.target}` : "none",
    requests: sent.length, requestsBeforeSecondClick: sentBeforeSecond, second,
    at150: pick(at150, beforeLines), at1s: pick(at1s, beforeLines), end: pick(end, beforeLines),
    live: probe.live.slice(0, 12), failedRequests: ctx.failed.filter((f) => f.t >= clickAt).map((f) => `${f.path}: ${f.why}`),
    otherRequests: [...new Set(ctx.log.slice(logStart).map((e) => e.path))].filter((p) => !paths.includes(p)),
  };
  if (after) result.after = await after(page, ctx).catch((e) => `after failed: ${e.message}`);
  return result;
}
function pick(s, beforeLines) {
  return {
    newText: s.lines.filter((l) => !beforeLines.has(l)).slice(0, 14),
    spinners: s.spinners, busy: s.busy, status: s.status, alerts: s.alerts, dialogs: s.dialogs, focus: s.focus, target: s.target,
  };
}

/** After a hung action: go to *place* and back, then report what is shown. */
export async function switchAway(page, back, away = "home") {
  await page.click(`.main-nav button[data-place="${away}"]`);
  await pause(600);
  await page.click(`.main-nav button[data-place="${back}"]`);
  await pause(800);
  const s = await snapshot(page, null);
  return { spinners: s.spinners, status: s.status, alerts: s.alerts, busy: s.busy };
}
/** After a hung action: choose another saved connection in the window toolbar. */
export async function switchConnection(page) {
  const trigger = await visibleHandle(page, ".window-toolbar .connection-pill");
  if (!trigger) return "no connection picker on screen";
  await trigger.click();
  await page.waitForSelector(".window-toolbar .picker-panel", { visible: true, timeout: 4000 });
  const value = await page.$$eval('.window-toolbar select[aria-label="Connection"] option', (os) =>
    (os.find((o) => /Billing replica/.test(o.textContent)) || {}).value);
  if (!value) return "no other connection to choose";
  await page.select('.window-toolbar select[aria-label="Connection"]', value);
  await page.keyboard.press("Escape");
  await pause(900);
  const s = await snapshot(page, null);
  return { spinners: s.spinners, status: s.status, alerts: s.alerts, busy: s.busy, newText: s.lines.filter((l) => /Billing|From |another|refresh/i.test(l)).slice(0, 6) };
}

/** With a request held: can the person get out of the open sheet? */
export async function tryDismiss(page) {
  const open = () => page.evaluate(() => [...document.querySelectorAll("dialog[open]")].map((d) => (d.className || "dialog") + ": " +
    [...d.querySelectorAll(".dialog-actions button, .mass-kill-footer-actions button")].map((b) => `${b.textContent.trim()}${b.disabled ? " (disabled)" : ""}`).join(" | ")));
  const before = await open();
  if (!before.length) return { sheet: "none open" };
  await page.keyboard.press("Escape");
  await pause(400);
  const afterEscape = await open();
  let afterCancel = afterEscape;
  if (afterEscape.length) {
    await page.evaluate(() => { const b = [...document.querySelectorAll("dialog[open] button")].find((x) => x.textContent.trim() === "Cancel"); if (b) b.click(); });
    await pause(400);
    afterCancel = await open();
  }
  return { before, afterEscape, afterCancel };
}

export async function launch() {
  return puppeteer.launch({ headless: "shell", args: ["--no-sandbox"] });
}
export { snapshot, visibleHandle };
