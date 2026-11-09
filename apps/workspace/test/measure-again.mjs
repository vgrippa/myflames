// Suggested index -> reviewed editor write -> confirmed source measurement.
// All database endpoints are mocked. The real isolated Python server handles
// SQL preparation, plan parsing and workspace persistence. This does not
// replace live SQL or write-policy tests.
// Run after npm run build: node test/measure-again.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { chooseConnection, chooseView } from "./chrome-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-measure-again-"));
// Keep screenshots separate from the disposable HOME, which is removed below.
const shots = process.env.PERF_SHOTS || await fs.mkdtemp(path.join(os.tmpdir(), "myflames-measure-again-shots-"));
await fs.mkdir(shots, { recursive: true });
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" };
delete env.MYFLAMES_VAULT_PASSPHRASE;
const server = spawn(process.env.PYTHON || "python3", ["-c",
  "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
  path.join(directory, "workspace.sqlite3")], { cwd: root, env });

const SQL = "SELECT * FROM orders WHERE status = 'new'";
const DDL = "CREATE INDEX `idx_orders_status` ON `shop`.`orders` (`status`);";
let topRow = {
  digest: "a".repeat(64), digest_text: "SELECT * FROM `orders` WHERE `status` = ?", schema: "shop",
  // prepare-query removes this delimiter; the measured result must still
  // belong to this source statement after the backend normalizes its SQL.
  sample_text: `${SQL};`, sample_truncated: false, capture_problem: null,
  count: 20, total_ms: 900, avg_ms: 45, max_ms: 80, rows_examined: 6000, rows_sent: 200,
  examined_per_sent: 30, no_index_used: 20, no_good_index_used: 0,
  tmp_disk_tables: 0, sort_merge_passes: 0, flags: ["no_index_used", "low_selectivity"],
  first_seen: "2026-11-09 10:00:00", last_seen: "2026-11-09 10:10:00",
};
const seen = [];
const starts = [];
const applied = [];
const sessions = new Map();
const jobs = new Map();
const reviews = new Map();
let missingStats = false;
let nextOutcome = "complete";
let runNumber = 0;
function session(body) {
  if (!sessions.has(body.tab_id)) {
    const c = body.connection;
    sessions.set(body.tab_id, { schema_version: "editor-session-1.0", tab_id: body.tab_id, state: "idle",
      connection: { profile_id: c.profile_id, name: c.name, host: c.host, port: c.port, user: c.user,
        type: "production", read_only: false }, engine: "mysql", server_version: "8.4.3",
      connection_id: 100 + sessions.size, autocommit: true, in_transaction: false, sql_mode: "",
      backslash_escapes: true, safe_updates: true, safe_updates_default: true,
      read_only_transactions: false, ended: false, notes: [], run: null });
  }
  return sessions.get(body.tab_id);
}
function editorRun(body, outcome) {
  const error = outcome === "error" ? { type: "error", statement: 0, code: 1061, sqlstate: "42000",
    message: "Duplicate key name", interrupted: false, duration_ms: 1, position: null } : null;
  const stopped = outcome === "stopped" ? { by: "user", interrupted: true, message: "Stopped" } : null;
  return { schema_version: "editor-run-1.0", id: `run-${++runNumber}`, kind: "run", state: outcome,
    scope: "statement", started_at: "2026-11-09T13:00:00Z", finished_at: "2026-11-09T13:00:01Z",
    duration_ms: 1, limit: 1000, statements: [{ index: 0, text: body.sql, sent: body.sql, start: 0,
      end: body.sql.length, verb: "CREATE", kind: "write", category: "ddl",
      status: outcome === "complete" ? "ok" : outcome, duration_ms: 1 }],
    results: error ? [error] : outcome === "complete" ? [{ type: "ok", statement: 0, affected_rows: 0,
      info: null, warnings: 0, duration_ms: 1 }] : [], error, stopped, message: null,
    counts: { statements: 1, run: 1, rows: 0, affected_rows: 0, warnings: 0, errors: error ? 1 : 0 },
    audit_id: null, history_id: null };
}
function captureFor(body, noStats) {
  const raw = JSON.stringify({ query: body.query, operation: "Index lookup on orders using idx_orders_status (status='new')",
    access_type: "index", table_name: "orders", index_name: "idx_orders_status", estimated_rows: 10,
    actual_rows: 2, actual_loops: 1, actual_first_row_ms: 0.1, actual_last_row_ms: 1.5 });
  return { schema_version: "capture-1.0", raw, query: body.query, mode: "analyze", engine: "mysql",
    server_version: "8.4.3", captured_at: "2026-11-09T13:00:00Z", conditions: "", warnings: [],
    // Deliberately disagree with iterator and summary times: the UI must use
    // the final statement event's 12.5 ms and 8 rows examined.
    runs: [{ total_time_ms: 1.5, statistics: noStats ? null : { timer_wait_ps: 12500000000, rows_examined: 8 },
      session_status_delta: {}, plan: raw }],
    measurements: { count: 1, median_ms: 1.5, min_ms: 1.5, max_ms: 1.5, stdev_ms: 0 },
    schema: {}, table_stats: {}, variables: {}, trace: null, source: body.source };
}
function respond(url, body) {
  if (url === "/api/live/readiness") return [200, { engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.3",
    read_only: false, super_read_only: false, performance_schema: true, replica: false, group_member: false,
    galera_member: false, privileges: {}, actions: {}, notes: [], checked_at: "2026-11-09T13:00:00Z" }];
  if (url === "/api/live/privileges") return [200, { schema_version: "privileges-1.0", account: "tester@%", actions: {} }];
  if (url === "/api/live/top") return [200, { schema_version: "top-1.0", engine: "mysql", server_version: "8.4.3",
    order: "total", notes: [], statements: [topRow] }];
  if (url === "/api/live/index-suggestion") return [200, { status: "suggestion", ddl: DDL,
    reason: "Full scan on orders with filter on status.", caveats: "Indexes add write cost." }];
  if (url === "/api/editor/preview") return [200, { status: "ready", statements: [{ index: 0, kind: "write",
    action_keys: [], state: "unknown", missing: [], reason: "Privileges are unverified." }],
    aggregate: { state: "unknown", missing: [], reason: "Privileges are unverified.", warning: "" },
    policy: { action: "confirm", text: "Review before running." } }];
  if (url === "/api/editor/status") return [200, { status: sessions.has(body.tab_id) ? "idle" : "none",
    session: sessions.get(body.tab_id) || null, run: null }];
  if (url === "/api/editor/open") return [200, { status: "idle", session: session(body) }];
  if (url === "/api/editor/close") { sessions.delete(body.tab_id); return [200, { closed: true, session: null }]; }
  if (url === "/api/editor/history") return [200, { items: [] }];
  if (url === "/api/editor/run") {
    const current = session(body);
    if (body.decline) return [200, { status: "declined", message: "Nothing was sent.", session: current }];
    if (!body.confirmed) {
      const token = `review-${reviews.size + 1}`;
      reviews.set(token, { sql: body.sql, tab_id: body.tab_id });
      return [200, { status: "needs_confirmation", session: current, confirmation: {
        token, expires_in: 300, title: "Production connection", text: "Review this CREATE INDEX before it runs.",
        tone: "primary", kind: "write", category: "ddl", reasons: [], warnings: [], in_transaction: false,
        statements: [body.sql], count: 1, connection_type: "production", connection: current.connection } }];
    }
    const reviewed = reviews.get(body.confirm_token);
    // This is a UI transport assertion, not a substitute for real dbops.
    assert.deepEqual(reviewed, { sql: body.sql, tab_id: body.tab_id }, "confirmation preserves the reviewed SQL and tab");
    const outcome = nextOutcome;
    nextOutcome = "complete";
    applied.push(body);
    return [200, { status: outcome, session: current, run: editorRun(body, outcome) }];
  }
  if (url === "/api/live/start") {
    starts.push(body);
    const id = `job-${starts.length}`;
    jobs.set(id, { body, noStats: missingStats });
    return [200, { id, state: "queued" }];
  }
  if (url === "/api/live/status") {
    const job = jobs.get(body.id);
    assert.ok(job, "status refers to a job started after confirmation");
    return [200, { id: body.id, state: "complete", progress: 1, repeat: 1, error: "", result: captureFor(job.body, job.noStats) }];
  }
  if (url === "/api/live/schemas") return [200, { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.3",
    taken_at: "x", uptime_s: 1, performance_schema: true, schemas: [{ name: "shop", tables: 1, system: false }],
    user_tables: 1, all_limit: 2000, all_note: "", system: [] }];
  if (url === "/api/live/catalog") return [200, { schema_version: "catalog-1.0", level: "tables", schema: body.schema,
    tables: [{ name: "orders", type: "table" }], truncated: false, limit: 200, prefix: null, after: null, next_after: null }];
  return null;
}

let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error(`Workspace did not start: ${output}`)), 10000);
    server.stderr.on("data", chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    server.once("error", error => { clearTimeout(timer); reject(error); });
    server.once("exit", code => { clearTimeout(timer); reject(new Error(`Workspace exited (${code}): ${output}`)); });
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  const consoleProblems = [];
  const requestFailures = [];
  const pendingRequests = new Set();
  const switchAborts = new WeakSet();
  const intentionalAbortUrls = new Set();
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => {
    if (["error", "warn", "warning"].includes(message.type())) {
      consoleProblems.push({ type: message.type(), text: message.text(), url: message.location().url || "" });
    }
  });
  page.on("requestfinished", request => pendingRequests.delete(request));
  page.on("requestfailed", request => {
    pendingRequests.delete(request);
    const failure = { url: request.url(), error: request.failure()?.errorText || "Unknown request failure" };
    const expected = switchAborts.has(request) && failure.error === "net::ERR_ABORTED";
    if (expected) intentionalAbortUrls.add(failure.url);
    requestFailures.push({ ...failure, expected });
  });
  await page.setRequestInterception(true);
  page.on("request", async request => {
    pendingRequests.add(request);
    const pathname = new URL(request.url()).pathname;
    try {
      const body = JSON.parse(request.postData() || "{}");
      if (pathname.startsWith("/api/")) seen.push({ path: pathname, body });
      const reply = respond(pathname, body);
      if (reply) return await request.respond({ status: reply[0], contentType: "application/json", body: JSON.stringify(reply[1]) });
      if (pathname.startsWith("/api/live/") || pathname.startsWith("/api/connection/") ||
        (pathname.startsWith("/api/editor/") && !pathname.startsWith("/api/editor/saved/"))) {
        return await request.respond({ status: 400, contentType: "application/json", body: JSON.stringify({ error: `Not mocked: ${pathname}` }) });
      }
      await request.continue();
    } catch (error) {
      errors.push(String(error));
      if (!request.isInterceptResolutionHandled()) await request.abort();
    }
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const profiles = await page.evaluate(async () => {
    const bootstrap = await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json",
      "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" });
    const token = (await bootstrap.json()).token;
    const ids = [];
    for (const [name, host] of [["Measure MySQL", "127.0.0.1"], ["Other MySQL", "127.0.0.2"]]) {
      const response = await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: JSON.stringify({ value: { name, host, port: 3306, user: "tester", database: "active_schema", ssl_mode: "PREFERRED" } }) });
      if (!response.ok) throw new Error(await response.text());
      ids.push((await response.json()).id);
    }
    return ids;
  });
  await page.reload({ waitUntil: "networkidle0" });
  const text = selector => page.$eval(selector, element => element.textContent);
  const nav = label => page.locator(`.main-nav button ::-p-text(${label})`).click();
  const selectedTab = () => page.$eval('.query-tablist [role="tab"][aria-selected="true"]', element => element.dataset.tabId);
  const selectTab = id => page.click(`.query-tablist [data-tab-id="${id}"]`);
  const absentMeasure = async () => assert.equal(await page.$('[data-action="measure-again"]'), null);
  async function switchConnection(id) {
    // Only reads already in flight on the previous connection may abort.
    // Capture, write and asset failures are always unexpected diagnostics.
    const cancellable = new Set(["/api/live/privileges", "/api/live/schemas", "/api/live/catalog",
      "/api/live/index-suggestion", "/api/editor/preview", "/api/editor/status"]);
    for (const request of pendingRequests) {
      if (cancellable.has(new URL(request.url()).pathname)) switchAborts.add(request);
    }
    await chooseConnection(page, id);
  }
  async function openSuggestion() {
    await nav("Workload");
    await chooseView(page, "top");
    const back = await page.$(".view-inspector.is-detail .detail-back");
    if (back) await back.click();
    await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === "/api/live/top"),
      page.click('.server-view[data-view="top"] [data-action="refresh"]'),
    ]);
    await page.waitForFunction(() => !document.querySelector('.server-view[data-view="top"] .spinner'));
    await page.waitForFunction(sql => document.querySelector('.server-view[data-view="top"] .cell-sql')?.textContent.includes(sql), {}, topRow.sample_text);
    await page.click('.server-view[data-view="top"] tbody tr[data-rank="1"]');
    await page.waitForFunction(() => document.querySelector('.view-inspector [data-action="read-index-suggestion"], .view-inspector [data-action="open-index-suggestion"]'));
    const readSuggestion = await page.$('.view-inspector [data-action="read-index-suggestion"]');
    if (readSuggestion) await readSuggestion.click();
    await page.locator('.view-inspector [data-action="open-index-suggestion"]').click();
    await page.waitForFunction(sql => document.querySelector('[aria-label="SQL editor"]')?.value === sql, {}, DDL);
    await absentMeasure();
    return selectedTab();
  }
  async function applyIndex() {
    const before = applied.length;
    await page.click('[data-action="run"]');
    await page.waitForSelector(".editor-confirm[open]");
    await absentMeasure();
    assert.equal(applied.length, before, "a write review is not an applied index");
    assert.match(await text(".editor-confirm .sheet-statements"), /CREATE INDEX/);
    await page.locator('.editor-confirm button ::-p-text(Run statement)').click();
    await page.waitForFunction(() => !document.querySelector(".editor-confirm") && !document.querySelector(".results-running"));
  }
  async function measure() {
    const before = starts.length;
    await page.click('[data-action="measure-again"]');
    await page.waitForSelector(".confirm-sheet[open] .confirm-sql");
    assert.match(await text(".confirm-sheet h2"), /Run analysis on/);
    assert.equal(starts.length, before, "Measure again opens review without executing the SELECT");
    assert.equal(await page.$eval('[aria-label="SQL editor"]', element => element.value), topRow.sample_text);
    assert.equal(await text(".confirm-sheet .confirm-sql"), topRow.sample_text);
    await page.locator('.confirm-sheet button ::-p-text(Run analysis)').click();
    await page.waitForSelector("[data-measure-result]");
    assert.equal(starts.length, before + 1);
    assert.equal(starts.at(-1).query, topRow.sample_text.replace(/;$/, ""), "preparation strips the optional final delimiter");
    assert.equal(starts.at(-1).mode, "analyze");
    assert.equal(starts.at(-1).connection.profile_id, profiles[0]);
    assert.equal(starts.at(-1).connection.database, topRow.schema || "", "source schema overrides the profile's active_schema, including no default schema");
    assert.deepEqual(starts.at(-1).source, { kind: "digest", digest: topRow.digest, schema: topRow.schema });
  }

  await nav("Query editor");
  await switchConnection(profiles[0]);
  const ddlTab = await openSuggestion();
  assert.equal(starts.length, 0, "opening suggested SQL executes neither the DDL nor its source query");
  await applyIndex();
  await page.waitForSelector('[data-action="measure-again"]');
  assert.equal(applied.length, 1);
  assert.equal(applied[0].sql.trim(), DDL);

  // Switching connections keeps the origin visible but prevents a rerun on
  // the new server. The original successful DDL does not grant that action.
  await switchConnection(profiles[1]);
  assert.equal(await page.$eval('[data-action="measure-again"]', element => element.disabled || element.getAttribute("aria-disabled") === "true"), true);
  await page.click('[data-action="measure-again"]');
  assert.equal(starts.length, 0);
  assert.equal(await page.$(".confirm-sheet[open] .confirm-sql"), null);
  await switchConnection(profiles[0]);

  // Cancel the measurement review: no live/start, and the source SQL stays
  // available in its own tab for the user to inspect or run later.
  await page.click('[data-action="measure-again"]');
  await page.waitForSelector(".confirm-sheet[open] .confirm-sql");
  assert.equal(await page.$eval('[aria-label="SQL editor"]', element => element.value), `${SQL};`);
  assert.notEqual(await selectedTab(), ddlTab);
  assert.equal(starts.length, 0);
  await page.locator('.confirm-sheet button ::-p-text(Cancel)').click();
  await page.waitForFunction(() => !document.querySelector(".confirm-sheet[open]"));
  assert.equal(starts.length, 0, "cancel sends no capture request");

  // Editing that source tab makes it an ordinary analysis. Its result must
  // not claim to measure the digest whose suggested index opened the tab.
  await page.locator('[aria-label="SQL editor"]').fill("SELECT * FROM orders WHERE status = 'edited'");
  await page.click('[data-action="analyze"]');
  await page.waitForSelector(".confirm-sheet[open] .confirm-sql");
  await page.locator('.confirm-sheet button ::-p-text(Run analysis)').click();
  await page.waitForFunction(() => document.querySelector(".plan-pane")?.textContent.includes("Measured:"));
  assert.equal(starts.length, 1);
  assert.equal(await page.$("[data-measure-result]"), null, "edited source SQL cannot retain the digest before/after attribution");
  assert.equal(await page.$('[data-action="compare-measurement"]'), null);

  await selectTab(ddlTab);
  await measure();
  const measuredText = await text("[data-measure-result]");
  assert.match(measuredText, /Digest average before/);
  assert.match(measuredText, /45(?:\.0+)?\s*ms/);
  assert.match(measuredText, /after/i);
  assert.match(measuredText, /12\.5(?:0)?\s*ms/);
  assert.match(measuredText, /300/);
  assert.match(measuredText, /\b8\b/);
  assert.match(measuredText, /this run; caches may be warm/);
  assert.equal(await page.$('[data-action="compare-measurement"]'), null, "a digest average is not an earlier captured plan");
  await selectTab(ddlTab);
  await measure();
  await page.waitForSelector('[data-action="compare-measurement"]');

  async function settleLayout(width, theme) {
    await page.setViewport({ width, height: 1000 });
    await page.evaluate(async scheme => {
      document.documentElement.dataset.theme = scheme;
      await document.fonts.ready;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await Promise.allSettled(document.getAnimations().filter(animation =>
        Number.isFinite(animation.effect?.getComputedTiming().endTime)).map(animation => animation.finished));
    }, theme);
  }
  for (const width of [1440, 720]) {
    for (const theme of ["light", "dark"]) {
      await settleLayout(width, theme);
      const layout = await page.evaluate(() => {
        const result = document.querySelector("[data-measure-result]");
        const line = result?.querySelector(".editor-insight-text");
        const pane = result?.closest(".plan-pane");
        const compare = pane?.querySelector('[data-action="compare-measurement"]');
        if (!line || !pane || !compare) return { missing: true };
        const bounds = pane.getBoundingClientRect();
        const inside = rect => rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1 &&
          rect.top >= bounds.top - 1 && rect.bottom <= bounds.bottom + 1;
        const range = document.createRange();
        range.selectNodeContents(line);
        return {
          missing: false,
          theme: document.documentElement.dataset.theme,
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
          lineVisible: line.checkVisibility(),
          lineInside: [...range.getClientRects()].every(inside),
          compareVisible: compare.checkVisibility(),
          compareInside: inside(compare.getBoundingClientRect()),
        };
      });
      assert.deepEqual(layout, { missing: false, theme, horizontalOverflow: false,
        lineVisible: true, lineInside: true, compareVisible: true, compareInside: true },
      `${width}px ${theme}: the measurement and comparison action remain visible inside the result pane`);
      await page.screenshot({ path: path.join(shots, `measure-again-${width}-${theme}.png`), fullPage: true });
    }
  }
  await settleLayout(1440, "light");
  await page.click('[data-action="compare-measurement"]');
  await page.waitForSelector(".compare-screen", { visible: true });
  const pair = await page.$$eval(".compare-selectors select", selects => selects.map(select => select.value));
  assert.equal(pair.length, 2);
  assert.ok(pair[0] && pair[1] && pair[0] !== pair[1], "comparison selects two distinct captured plans");

  // A new literal value has the same digest, but no matching source capture.
  topRow = { ...topRow, sample_text: "SELECT * FROM orders WHERE status = 'paid'" };
  await openSuggestion();
  await applyIndex();
  await page.waitForSelector('[data-action="measure-again"]');
  missingStats = true;
  await measure();
  assert.match(await text("[data-measure-result]"), /Not collected/);
  assert.doesNotMatch(await text("[data-measure-result]"), /after\s+1\.5\s*ms/i, "iterator timing must not impersonate a statement timer");
  assert.equal(await page.$('[data-action="compare-measurement"]'), null, "another literal value must not inherit the prior plan");
  missingStats = false;

  // Likewise, exact SQL under another default schema has no previous plan.
  topRow = { ...topRow, schema: "billing" };
  await openSuggestion();
  await applyIndex();
  await page.waitForSelector('[data-action="measure-again"]');
  await measure();
  assert.equal(await page.$('[data-action="compare-measurement"]'), null, "another default database is another statement thread");

  // A digest recorded with no default database never inherits the profile's
  // active_schema. The source can still use an explicitly qualified table.
  topRow = { ...topRow, schema: null, sample_text: "SELECT * FROM shop.orders WHERE status = 'paid'" };
  await openSuggestion();
  await applyIndex();
  await page.waitForSelector('[data-action="measure-again"]');
  await measure();
  assert.equal(starts.at(-1).connection.database, "");
  assert.equal(await page.$('[data-action="compare-measurement"]'), null);

  // An edited statement may complete successfully. It is not the suggested
  // index and must not produce the follow-up action.
  await openSuggestion();
  await page.locator('[aria-label="SQL editor"]').fill(DDL.replace("`status`", "`customer_id`"));
  await applyIndex();
  await absentMeasure();
  for (const outcome of ["error", "stopped"]) {
    await openSuggestion();
    nextOutcome = outcome;
    await applyIndex();
    await absentMeasure();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(requestFailures.filter(failure => !failure.expected), [], "unexpected request failures");
  assert.deepEqual(consoleProblems.filter(message => !(intentionalAbortUrls.has(message.url) &&
    message.text === "Failed to load resource: net::ERR_ABORTED")), [], "unexpected console warnings or errors");
  console.log(`Measure again workflow: ok. Screenshots: ${shots}`);
} finally {
  try {
    if (browser) await browser.close();
  } finally {
    if (server.exitCode === null) {
      const exited = once(server, "exit");
      server.kill("SIGINT");
      const force = setTimeout(() => server.kill("SIGKILL"), 5000);
      try { await exited; } finally { clearTimeout(force); }
    }
    await fs.rm(directory, { recursive: true, force: true });
  }
}
