import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
// Full analysis (report-1.0 from myflames/report.py) and Refresh all in the
// Server place. /api/live/report answers with a live run on the demo database
// (fixtures/report-mysql-8.4.json, MySQL 8.4) given a problem finding and the
// statements behind its numbers; the other live views answer from their own
// fixtures, and can be held to watch the concurrency cap. Covers: nothing read
// until asked, the run with its estimate, the header and summary, Look at
// first, findings ranked as one-line rows, Not judged with the reason, the
// sections, the inspector (verdict, numbers, rule, statements and my.cnf lines
// to copy, the statements behind a number with Explain and Run analysis), the
// notes, export through /api/report/export with a download, links to Top
// statements, the report greyed after a connection switch, session restore
// (that it ran, never the report), 390 px; and Refresh all: unopened views
// included, analysis in its own final phase, at most three view reads
// at once with the sampling views first, single flight (⌥⌘R while running does
// nothing), a view's own Refresh joined, failures per view (badge, list, note
// above the view, previous report kept), Escape stops, a connection switch
// stops it and drops late answers. Run: npm run test:report
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

// Socket-free coverage of the shell-owned run lifecycle and actual Tuning
// rendering. The browser path below continues to cover focus and downloads.
if (process.argv.includes("--unit")) {
  const { build, transform } = await import("esbuild");
  const { createRequire } = await import("node:module");
  const { fileURLToPath } = await import("node:url");
  const React = await import("react");
  const { renderToStaticMarkup } = await import("react-dom/server");
  const source = await fs.readFile(new URL("../src/Server.tsx", import.meta.url), "utf8");
  const helper = source.slice(source.indexOf("function refreshDiagramDatabase("), source.indexOf("/** Below this width"));
  const helperCode = (await transform(`${helper}\nreturn refreshDiagramDatabase;`, { loader: "ts" })).code;
  const database = new Function(helperCode)();
  const documents = [null, { source: { kind: "import" } }, { source: { kind: "server", key: "other", database: "shop" } },
    { source: { kind: "server", key: "current", database: "shop" } }];
  assert.deepEqual(documents.map(doc => database(doc, " selected ", "current")), ["selected", "", "", "shop"]);
  assert.equal(database(null, "  ", "current"), "");
  assert.equal(database({ source: { kind: "server", key: "current" } }, "shop", "current"), "");
  assert.equal((source.match(/refreshTasks\(\)/g) || []).length, 2, "tasks are built only on Refresh all, never to render a count");
  const tasks = source.slice(source.indexOf("  function refreshTasks()"), source.indexOf("  /** Refresh every Server section"));
  const viewsCode = (await transform(await fs.readFile(new URL("../src/serverViews.ts", import.meta.url), "utf8"), { loader: "ts", format: "cjs" })).code;
  const viewModule = { exports: {} };
  new Function("module", viewsCode)(viewModule);
  const { ALL_SERVER_VIEWS } = viewModule.exports;
  const taskCode = (await transform(tasks, { loader: "ts" })).code;
  for (const diagram of documents) {
    const scope = { latestConnection: { current: {} }, connectionKey: () => "current", lastViewRead: () => null,
      readRunning: () => false, ALL_SERVER_VIEWS, READ_LABELS: {}, healthInterval: 5, waitsInterval: 10,
      ioInterval: 10, schema: "", order: "total", topStatements: { table: null },
      topRead: () => ({ params: "top", body: () => ({}) }), sessionsScope: "active", minSeconds: 0,
      locks: null, tables: null, indexesSchemas: [], tablesSchemas: [], ERROR_LOG_READ: "error-log",
      errorLogBody: () => ({}), diagram, diagramDatabase: "selected", refreshDiagramDatabase: database,
      WINDOWED: ["health", "waits", "io"] };
    const actual = new Function("scope", `with (scope) { ${taskCode}; return refreshTasks(); }`)(scope);
    assert.equal(actual.length, ALL_SERVER_VIEWS.length + 3 + (database(diagram, "selected", "current") ? 1 : 0));
  }
  assert.doesNotMatch(source, /badges\.(replication|report|tuning)/);
  assert.doesNotMatch(source, /click Load or Refresh|Top statements, Sessions|Kill in Sessions/);
  const reportSource = await fs.readFile(new URL("../src/Report.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(reportSource, /ReportView|runStarts/);
  const sidebar = await fs.readFile(new URL("../src/Sidebar.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(sidebar, /onNewSession|onOpenQuery|onRemoveQuery|activeQueryId|Open queries/);

  const callback = source.match(/onRunReport=\{(async \(\) => \{[\s\S]*?)\}\n\s+onOpenSql=/);
  assert.ok(callback, "shell-owned analysis callback exists");
  const callbackCode = (await transform(`const run = ${callback[1]};`, { loader: "ts" })).code;
  const pending = [];
  let active = "first";
  let started = null;
  const scope = { disabled: false, running: false, connection: {},
    setAnalysisStarted: value => { started = typeof value === "function" ? value(started) : value; },
    analysisReads: { currentKey: () => active, running: () => pending.some(p => p.key === active && !p.done),
      run: () => new Promise(resolve => pending.push({ key: active, resolve, done: false })) } };
  const run = new Function("scope", `with (scope) { ${callbackCode}; return run; }`)(scope);
  const now = Date.now;
  const originalError = console.error;
  const here = path.dirname(fileURLToPath(import.meta.url));
  const directory = await fs.mkdtemp(path.join(here, ".report-unit-"));
  try {
    Date.now = () => 100000;
    const first = run();
    assert.deepEqual(started, { key: "first", at: 100000 });
    await run();
    assert.equal(pending.length, 1, "duplicate starts preserve the first timestamp");
    const output = await build({ entryPoints: [fileURLToPath(new URL("../src/Tuning.tsx", import.meta.url))],
      bundle: true, platform: "node", format: "cjs", write: false, jsx: "automatic", loader: { ".css": "empty" }, external: ["react", "react-dom"] });
    const filename = path.join(directory, "tuning.cjs");
    await fs.writeFile(filename, output.outputFiles[0].text);
    const { TuningView } = createRequire(import.meta.url)(filename);
    console.error = (...args) => {
      if (String(args[0]).includes("useLayoutEffect does nothing on the server")) return;
      originalError(...args);
    };
    const render = extra => renderToStaticMarkup(React.createElement(TuningView, {
      disabled: false, selection: { key: "", select() {} }, reads: { key: "first", loading: "Reading", error: "" },
      startedAt: started?.at, ...extra,
    }));
    Date.now = () => 107000;
    assert.match(render(), /7 s so far/);
    Date.now = () => 112000;
    assert.match(render(), /12 s so far/, "a remounted Tuning keeps the shell start time");
    assert.match(render(), /aria-label="Export the tuning scorecard"[^>]*aria-haspopup="menu"/);
    const analysis = await load("report-mysql-8.4.json");
    assert.match(render({ tabRequest: { tab: "analysis", n: 1 }, report: { key: "first", label: "first", report: analysis } }),
      /aria-label="Export the full analysis"[^>]*aria-haspopup="menu"/);
    active = "second";
    const second = run();
    assert.deepEqual(started, { key: "second", at: 112000 });
    pending[0].done = true;
    pending[0].resolve(null);
    await first;
    assert.deepEqual(started, { key: "second", at: 112000 }, "old completion leaves the new connection's timer intact");
    pending[1].done = true;
    pending[1].resolve(null);
    await second;
    assert.equal(started, null);

    // Refresh all owns a separate read path. Execute its actual worker with
    // only the report task and hold the response while Tuning is first opened.
    const refreshSource = source.slice(source.indexOf("  async function refreshAll()"), source.indexOf("  /** Stop Refresh all:"));
    const refreshCode = (await transform(refreshSource, { loader: "ts" })).code;
    let refresh = null;
    let generation = 0;
    const globalPending = [];
    const refreshScope = {
      refreshControl: { current: null }, disabled: false, latestConnection: { current: { key: "first" } },
      // Stop arming is exercised by the browser path; this worker test owns
      // no UI timer, but provides the merged action-feedback dependencies.
      stopArmedAt: { current: 0 }, setStopArmed() {}, STOP_ARM_MS: 600, window: { setTimeout() {} },
      connectionKey: c => c.key, connectionLabel: c => c.key, refreshSeq: { current: 0 },
      refreshTasks: () => [{ view: "report", label: "Full analysis", params: "report", request() {}, accept() {} }],
      setRefresh: value => { refresh = typeof value === "function" ? value(refresh) : value; },
      analysisStarted: null, readRunning: () => false, nextGeneration: () => ++generation, latestGeneration: () => generation,
      sharedRead: () => new Promise(resolve => globalPending.push(resolve)), reportOfRef: { current: () => null }, REFRESH_CONCURRENCY: 3,
    };
    const refreshAll = new Function("scope", `with (scope) { ${refreshCode}; return refreshAll; }`)(refreshScope);
    Date.now = () => 200000;
    const globalFirst = refreshAll();
    await Promise.resolve();
    assert.equal(refresh.views[0].state, "loading");
    assert.equal(refresh.views[0].startedAt, 200000, "the report phase records its read start");
    const globalProps = () => ({ reads: { key: refresh.key, loading: "", error: "" },
      refreshRunning: refresh.state === "running" && refresh.views[0].state === "loading", startedAt: refresh.views[0].startedAt });
    Date.now = () => 207000;
    assert.match(render(globalProps()), /7 s so far/, "opening Tuning during Refresh all displays the running analysis");
    Date.now = () => 212000;
    assert.match(render(globalProps()), /12 s so far/, "remounting during Refresh all keeps its phase's start");
    // A connection switch stops the old run and permits a new one. Its late
    // completion cannot clear or relabel the new run's elapsed time.
    refreshScope.refreshControl.current.controller.abort();
    refreshScope.refreshControl.current = null;
    refreshScope.latestConnection.current = { key: "second" };
    const globalSecond = refreshAll();
    await Promise.resolve();
    globalPending[0]({});
    await globalFirst;
    assert.equal(refresh.key, "second");
    assert.equal(refresh.views[0].state, "loading");
    assert.equal(refresh.views[0].startedAt, 212000);
    globalPending[1]({});
    await globalSecond;
    assert.doesNotMatch(render(globalProps()), /s so far/, "a completed report phase stops the timer");
    // Joining a manually started read copies its original timestamp; the
    // waiter may clear manual state before the Refresh all waiter completes.
    refreshScope.analysisStarted = { key: "second", at: 300000 };
    refreshScope.readRunning = () => true;
    Date.now = () => 305000;
    const joined = refreshAll();
    assert.equal(refresh.views[0].startedAt, 300000);
    refreshScope.analysisStarted = null;
    assert.match(render(globalProps()), /5 s so far/);
    globalPending[2]({});
    await joined;
  } finally {
    Date.now = now;
    console.error = originalError;
    await fs.rm(directory, { recursive: true, force: true });
  }
  console.log("Report unit checks passed: refresh count/scope, manual and Refresh all timing across remounts, stale completion and shared export menus.");
  process.exit(0);
}

const smoke = await load("report-mysql-8.4.json");
const health = await load("health-mysql-8.4.json");
const config = await load("config-mysql-8.4.json");
const waits = await load("waits-mysql-8.4.json");
const locks = await load("locks-mysql-8.4.json");

const A = "a".repeat(64);
const B = "b".repeat(64);
const C = "c".repeat(64);
const statement = (digest, sample, extra = {}) => ({
  digest, digest_text: "SELECT ?", schema: "shop", count: 120, total_ms: 5400.5, avg_ms: 45, max_ms: 90,
  rows_examined: 360000, rows_sent: 1200, examined_per_sent: 300, no_index_used: 120, no_good_index_used: 0,
  tmp_disk_tables: 0, sort_merge_passes: 0, sample_text: sample, sample_truncated: false,
  first_seen: "2026-10-12 10:00:00", last_seen: "2026-10-14 10:00:00", flags: ["no_index_used"], capture_problem: null,
  select_full_join: 0, select_range_check: 0, select_scan: 0, ...extra,
});
// The smoke run found no join without an index and no statement naming a
// scanned table: give one finding a problem and statements, and one view a
// failure, so ranking, attribution and Not judged are all on screen.
const rich = structuredClone(smoke);
const joins = rich.findings.find((f) => f.id === "health.select_full_join");
Object.assign(joins, { status: "problem", headline: "12/s", verdict: "120 joins in 10 s (12 per second).", impact: 12 });
rich.findings = [joins, ...rich.findings.filter((f) => f !== joins)];
rich.attribution.full_joins.statements = [
  statement(A, "SELECT * FROM orders o JOIN customers c ON c.note = o.note", { select_full_join: 120 }),
  statement(B, "SELECT COUNT(*) FROM items i JOIN prices p ON p.sku = i.sku", { count: 8, total_ms: 80, select_full_join: 8, select_range_check: 2 }),
];
rich.attribution.full_scans[0].statements = [
  statement(C, "SELECT * FROM employees WHERE hire_date > '2000-01-01'", {
    schema: "employees", select_scan: 31, table_match: { schema: "employees", table: "employees", kind: "unqualified", label: "text match, not proof" },
  }),
];
rich.look_first = joins.id;
rich.summary = { ...rich.summary, problem: 1, ok: rich.summary.ok - 1, status: "problem",
  text: "1 problem, 3 to watch, 4 unknown, 31 ok; look first at Joins without an index" };
rich.sections.find((s) => s.key === "temp_sorts").status = "problem";
rich.header.failures = [{ view: "tables", label: "Tables", status: "failed",
  error: "Could not read the tables: ERROR 1142 (42000): SELECT command denied to user 'demo'" }];
rich.header.views.find((v) => v.key === "tables").status = "failed";

// Other live views: answered from fixtures, or held until the test lets them go.
const extraFixtures = Object.fromEntries(await Promise.all([
  ["/api/live/io", "io-mysql-8.4.json"], ["/api/live/innodb", "innodb-mysql-8.4.json"],
  ["/api/live/error-log", "errorlog-mysql-8.4.json"],
  ["/api/live/sessions", "sessions-mysql-8.4.json"], ["/api/live/indexes", "indexes-mysql-8.4.json"],
  ["/api/live/tables", "tables-mysql-8.4.json"], ["/api/live/instruments", "instruments-mysql-8.4.json"],
  ["/api/live/replication", "replication-mysql-8.4-healthy.json"], ["/api/lifecycle/tools", "lifecycle-tools-mysql.json"],
  ["/api/live/cluster", "cluster-mysql-8.4-standalone.json"],
].map(async ([endpoint, fixture]) => [endpoint, await load(fixture)])));
const replies = {
  ...Object.fromEntries(Object.entries(extraFixtures).map(([endpoint, fixture]) => [endpoint, () => fixture])),
  "/api/accounts/list": () => ({schema_version:"accounts-1.0", engine:"mysql", version:"8.4.11", complete:true,
    warnings:[], accounts:[], role_grants:[], default_roles:[]}),
  "/api/live/health": () => health,
  "/api/live/config": () => config,
  "/api/live/waits": () => waits,
  "/api/live/locks": () => locks,
  "/api/live/top": (body) => ({
    schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: body.order, limit: body.limit,
    schema: body.schema || null, notes: ["Counters accumulate since the server started or the digest table was last truncated.",
      "The digest table is full: 5,000 more executions (1.2 s) were summed into one overflow row without a digest, so they are not listed."],
    statements: rich.attribution.full_joins.statements, table: body.table || null,
    coverage: { uptime_s: 192385, first_seen: "2026-10-12 10:00:00", digest_lost: 0, overflow: { executions: 5000, total_ms: 1200 } },
  }),
};
const hold = new Set();
const held = [];
/** path -> error text, or a function of the request body returning one (or null). */
const fail = new Map();
const traffic = { inflight: 0, peak: 0 };
function answer(entry, override) {
  traffic.inflight -= 1;
  const rule = fail.get(entry.path);
  const failure = typeof rule === "function" ? rule(entry.body) : rule;
  const [status, body] = override || (failure ? [400, { error: failure }] : [200, replies[entry.path](entry.body)]);
  entry.request.respond({ status, contentType: "application/json", body: JSON.stringify(body) }).catch(() => undefined);
}
const responder = (p) => (body, request) => {
  traffic.inflight += 1;
  traffic.peak = Math.max(traffic.peak, traffic.inflight);
  const entry = { path: p, body, request };
  if (hold.has(p)) held.push(entry);
  else answer(entry);
  return null;
};
/** Answer the oldest held request for *p* (any path when omitted). */
function release(p, override) {
  const at = held.findIndex((e) => !p || e.path === p);
  assert.ok(at >= 0, `no held request for ${p || "any path"}`);
  const [entry] = held.splice(at, 1);
  answer(entry, override);
}
const exports = [];
// passExport: the real server generates the file (the downloaded content is checked).
const flags = { passExport: false, capturing: false };
let holdDiagram = false, pendingDiagram = null;
const diagramReply = (body, expanded = false) => ({schema_version: "schema-diagram-1.0", tables: [
  {id: "shop.orders", schema: body.database, name: "orders", columns: [{name: "id", data_type: "bigint", nullable: false, primary_key: true}]},
  ...(expanded ? [{id: "shop.shipments", schema: body.database, name: "shipments", columns: [{name: "id", data_type: "bigint", nullable: false, primary_key: true}]}] : []),
], relationships: [], warnings: []});
const extra = {
  "/api/live/schemas": {status: 200, body: {schema_version: "schemas-1.0", schemas: [{name: "shop", tables: 1, system: false}], user_tables: 1, all_limit: 2000}},
  "/api/schema-diagram/read": (body, request) => {
    if (holdDiagram) { pendingDiagram = {body, request}; return null; }
    return {status: 200, body: diagramReply(body)};
  },
  ...Object.fromEntries(Object.keys(replies).map((p) => [p, responder(p)])),
  // A capture in the Query editor: running while flags.capturing, then cancelled.
  "/api/live/start": (body) => (flags.capturing
    ? { status: 200, body: { id: "job-busy", state: "queued" } }
    : { status: 400, body: { error: "Not mocked in this suite." } }),
  "/api/live/status": (body) => ({ status: 200, body: { id: body.id, state: flags.capturing ? "running" : "cancelled", progress: 0, repeat: 1, error: "", result: null } }),
  "/api/live/cancel": (body) => ({ status: 200, body: { id: body.id, state: "cancelled" } }),
  "/api/report/export": (body, request) => {
    exports.push(body);
    if (flags.passExport) {
      request.continue();
      return null;
    }
    const ext = { html: "html", markdown: "md", json: "json", text: "txt" }[body.format];
    return { status: 200, body: { format: body.format, content: `${body.format} export`, mime: "text/plain", filename: `myflames-report-20261015010720.${ext}` } };
  },
};

// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/report", "report", extra, { list: "docked" });
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: rich };
const view = ".server-view[data-view=tuning]";
const inspectorTitle = (title) => page.waitForFunction((t) => document.querySelector(".view-inspector h3")?.textContent === t, {}, title);
const calls = (p) => state.other.filter((o) => o.path === p);
const until = async (check, message) => {
  for (let i = 0; i < 200; i++) {
    if (await check()) return;
    await pause(25);
  }
  assert.fail(message);
};
const refreshAll = ".server-place [data-action=refresh-all]";
/** The place toolbar scrolls under the app's top bar: back to the top first. */
const clickRefreshAll = async () => {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(refreshAll);
};
/** The open view's own Refresh (its toolbar can scroll under the app's top bar too). */
const refreshView = async () => {
  await page.evaluate(() => window.scrollTo(0, 0));
  await h.refresh();
};
const statusText = () => page.$eval(".server-place .refresh-all-status", (e) => e.textContent).catch(() => "");
const downloads = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-report-downloads-"));
try {
  // Saved connections to switch to later (Staging, Archive); the page starts on the unsaved default.
  await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    for (const [name, host, port] of [["Staging", "db.staging", 3307], ["Archive", "db.archive", 3308]])
      await fetch("/api/profiles/save", {
        method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: JSON.stringify({ value: { name, host, port, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } }),
      });
  });
  await page.reload({ waitUntil: "networkidle0" });
  const cdp = await page.createCDPSession();
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });

  // Full analysis sits in Overview after Health and InnoDB; nothing is read until asked.
  await h.server();
  assert.deepEqual(await texts(".source-list .source-group:first-child .source-label"), ["Health", "InnoDB", "Error log"]);
  assert.equal(await page.$("#server-nav-report"), null, "the Full analysis is no Server view");
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), false, "Refresh all works before any view is loaded");
  assert.match(await page.$eval(refreshAll, (e) => e.title), /Refresh all 17 sections/);
  await h.fullAnalysis();
  assert.match(await text(`${view} .view-empty`), /Runs every Server view once and ranks what to look at first, about 10 to 20 s\. It only reads\./);
  assert.match(await h.popover("About Tuning"), /never applies them/);
  assert.equal(state.seen.length, 0, "nothing runs until asked");
  assert.equal(await page.$(`${view} .report-export`), null, "no export before a report");

  // Run: the progress line says what runs (announced once) and counts the
  // seconds beside it (drawn, not announced); then the report.
  state.delay = 1300;
  await page.locator(`${view} .view-empty button ::-p-text(Run full analysis)`).click();
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Running every Server view once, about 10 to 20 s in all\. It only reads\./);
  await page.waitForFunction((v) => document.querySelector(`${v} .view-status > [role=status]`)?.textContent ===
    "Running every Server view once, about 10 to 20 s in all. It only reads.", { timeout: 3000 }, view);
  // While the seconds count, the live region still holds the same words.
  const announcedWhileCounting = await page.waitForFunction((v) =>
    /[1-9] s so far/.test(document.querySelector(`${v} .capture-status`)?.textContent || "") &&
      document.querySelector(`${v} .view-status > [role=status]`)?.textContent, {}, view).then((h) => h.jsonValue());
  assert.equal(announcedWhileCounting, "Running every Server view once, about 10 to 20 s in all. It only reads.",
    "the seconds are never announced");
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  state.delay = 0;
  assert.equal(state.seen.length, 1);
  assert.equal(state.seen[0].connection.host, "127.0.0.1");
  assert.equal(state.seen[0].all_schemas, undefined, "never all_schemas");
  assert.match(await text(`${view} .summary-line`), /Problem\s*1 problem, 3 to watch, 4 unknown, 31 ok; look first at Joins without an index/);
  const facts = await text(`${view} .fact-strip`);
  assert.match(facts, /ServerMySQL 8\.4\.11/);
  assert.match(facts, /HostLocal MySQL \(127\.0\.0\.1:3306\)/);
  assert.match(facts, /Uptime2 d 5 h/);
  assert.match(facts, /Taken.*, in 10\.8 s/);
  assert.match(facts, /Databasesevery user schema/);
  assert.match(facts, /Views read9 of 10/);
  // The Full analysis status surfaces on the Tuning scorecard (the place has no Server list badge).
  assert.equal(await page.$("#server-nav-report"), null);
  await page.click("#tuning-tab-scorecard");
  await page.waitForSelector("#tuning-panel-scorecard .tuning-score");
  assert.match(await text(".tuning-score-coverage"), /1 problem/);
  assert.ok(await page.$(".tuning-score .status-dot.is-problem"), "the scorecard shows the bound report's problem status");
  await h.fullAnalysis();

  // Look at first, then the findings to look at, ranked as the report ranks them.
  assert.deepEqual(await page.$$eval(`${view} .report-first li`, (els) => els.map((e) => e.dataset.finding)), ["health.select_full_join"]);
  const look = await page.$$eval(`${view} .report-look li[data-finding]`, (els) => els.map((e) => e.dataset.finding));
  assert.deepEqual(look, rich.findings.filter((f) => f.id !== rich.look_first && (f.status === "problem" || f.status === "watch")).map((f) => f.id));
  assert.deepEqual(look, ["config.buffer_pool", "indexes.redundant", "config.slow_log"]);
  assert.match(await text(`${view} .report-look .grouped-head`), /0 problems, 3 to watch/);
  const first = await text(`${view} .report-look li[data-finding="config.buffer_pool"]`);
  assert.match(first, /Watch\s*Buffer pool size\s*128\.0 MiB/);
  const heights = await page.$$eval(`${view} .report-look .grouped-row`, (els) => els.map((e) => e.getBoundingClientRect().height));
  assert.ok(heights.every((x) => x <= 40), `one-line rows (${heights.join(", ")})`);
  // Not judged: the failed view and the unknown checks, each with its reason.
  const unknown = await page.$$eval(`${view} .report-unknown li`, (els) => els.map((e) => e.dataset.viewFailure || e.dataset.finding));
  assert.deepEqual(unknown, ["tables", "checks.replication", "checks.anonymous", "checks.no_password", "checks.root_any_host"]);
  assert.match(await text(`${view} .report-unknown li[data-finding="checks.anonymous"]`), /Not judged\s*Anonymous accounts\s*Could not read the accounts.*SELECT on mysql\.user/);
  assert.match(await text(`${view} .report-unknown li[data-view-failure="tables"]`), /Failed\s*Tables\s*Could not read the tables/);
  // Sections: one row each, in the report's order.
  assert.deepEqual(await page.$$eval(`${view} .report-sections li`, (els) => els.map((e) => e.dataset.section)), rich.sections.map((s) => s.key));
  assert.deepEqual(await page.$$eval(`${view} .report-sections li`, (els) => els.map((e) => [
    e.querySelector(".row-title")?.textContent, e.querySelector(".row-value")?.textContent,
    e.querySelector(".row-value > span")?.title,
  ])), rich.sections.map((section) => [section.title, section.summary, section.summary]),
  "section summaries use the same value slot as findings and keep the full text");
  assert.match(await text(`${view} .report-sections li[data-section="query_cache"]`), /MySQL 8\.0 and later have no query cache/);
  // For information: collapsed.
  assert.equal(await page.$eval(`${view} .report-info`, (e) => e.open), false);
  // No explanatory paragraph on screen: the notes are one control.
  assert.equal(await page.$$eval(`${view} .view-main p`, (els) => els.filter((e) => !e.closest(".summary-line, .gate") && e.offsetParent && e.textContent.length > 80).length), 0);
  const notes = await h.notes();
  assert.match(notes, /myflames never applies a recommendation/);
  assert.match(notes, /Statement counters are cumulative since the server started or the digest table was truncated/);
  assert.match(notes, /Server uptime 2 d 5 h/);
  assert.match(notes, /text match, not proof/);
  assert.match(notes, /Not checked: OS memory, swap, CPU and disk\. Not readable over SQL\./);

  // The inspector: verdict, numbers, rule, why, and the statements behind the number.
  await h.select(`${view} .report-first li`);
  await inspectorTitle("Joins without an index");
  let body = await h.inspector();
  assert.match(body, /Problem\s*12\/s/);
  assert.match(body, /Verdict\s*120 joins in 10 s \(12 per second\)\./);
  assert.match(body, /Rule of thumb: watch when any occur in the window, problem at 10 per second/);
  assert.match(body, /Why it matters\s*Joins that read a later table with a full scan/);
  assert.match(body, /Statements behind it/);
  assert.match(body, /SELECT \* FROM orders o JOIN customers c ON c\.note = o\.note/);
  assert.match(body, /120 calls · 5\.40 s total · 120 joins without an index · shop/);
  assert.match(body, /8 calls · 80 ms total · 10 joins without an index/);
  assert.match(await text(".view-inspector .inspector-actions-bar"), /Open Health.*Open Top statements by joins without an index.*Lesson: Block nested loop and hash join/);
  // Run analysis on a statement asks first (the Top statements confirmation).
  await page.click('.view-inspector [aria-label="Run analysis on statement 1 with joins without an index"]');
  await page.waitForSelector(".view-inspector .run-confirm");
  assert.match(await text(".view-inspector .run-confirm"), /executes this statement on 127\.0\.0\.1:3306: 1 run inside a read-only transaction/);
  await page.locator(".view-inspector .run-confirm button ::-p-text(Execute and analyze)").click();
  await page.waitForFunction(() => document.querySelector(".main-nav [aria-current=page]")?.textContent.includes("Query editor"));
  await until(() => calls("/api/live/start").length === 1, "Run analysis reaches the capture engine");
  const start = calls("/api/live/start")[0].body;
  assert.deepEqual([start.mode, start.query, start.source?.kind, start.source?.digest], ["analyze", "SELECT * FROM orders o JOIN customers c ON c.note = o.note", "digest", A]);
  await h.fullAnalysis();
  await page.waitForSelector(`${view} .summary-line`);

  // A watched setting: what to do, the statement and the my.cnf line to copy (never applied), the caveat.
  await h.select(`${view} .report-look li[data-finding="config.buffer_pool"]`);
  await inspectorTitle("Buffer pool size");
  body = await h.inspector();
  assert.match(body, /innodb_buffer_pool_size \(128\.0 MiB\) is much smaller than the InnoDB tablespace files/);
  assert.match(body, /Numbersinnodb_buffer_pool_size128\.0 MiB/);
  assert.ok(await page.$('.view-inspector dl[aria-label="Buffer pool size: numbers"] dt > code'), "identifier labels occupy their own row");
  assert.match(body, /SET GLOBAL innodb_buffer_pool_size = <bytes>;/);
  assert.match(body, /innodb_buffer_pool_size = <size>  # under \[mysqld\]/);
  assert.match(body, /Suggested only: myflames never runs or applies it\./);
  assert.match(body, /Caveat: The data on disk includes cold rows and free space/);
  assert.equal(await page.$$eval(".view-inspector .perf-sql button", (els) => els.map((e) => e.textContent)).then((t) => t.length), 2, "both are copyable");
  // Keyboard: Down moves to the next finding and details open on Enter.
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.matches('.report-look [data-finding="config.buffer_pool"] .grouped-row'));
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await inspectorTitle("Redundant indexes");
  // Not judged: the reason is the verdict; a failed view says why it failed.
  await h.select(`${view} .report-unknown li[data-finding="checks.no_password"]`);
  await inspectorTitle("Accounts without a password");
  assert.match(await h.inspector(), /Not judged.*Why it was not judged\s*Could not read the accounts.*SELECT on mysql\.user/);
  await h.select(`${view} .report-unknown li[data-view-failure="tables"]`);
  await inspectorTitle("Tables");
  assert.match(await h.inspector(), /Why it failed\s*Could not read the tables: ERROR 1142/);
  // A section lists its findings; choosing one opens it.
  await h.select(`${view} .report-sections li[data-section="indexes"]`);
  await inspectorTitle("Indexes");
  assert.deepEqual(await page.$$eval(".view-inspector li[data-finding]", (els) => els.map((e) => e.dataset.finding)), ["indexes.redundant", "indexes.full_scans", "indexes.unused"]);
  // Enter on a finding there: the inspector shows it, and focus moves to its title (not lost to the page).
  await page.focus('.view-inspector li[data-finding="indexes.full_scans"] button');
  await page.keyboard.press("Enter");
  await inspectorTitle("Tables read mostly without an index");
  await page.waitForFunction(() => document.activeElement?.matches(".view-inspector .inspector-title h3"));
  body = await h.inspector();
  assert.match(body, /Statements that name each table/);
  assert.match(body, /employees\.employees\s*text match, not proof/);
  assert.match(body, /SELECT \* FROM employees WHERE hire_date > '2000-01-01'/);
  assert.match(body, /sakila\.category\s*text match, not proof\s*No recorded statement names it and reads without an index\./);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "report.png") });

  // Export: generated on the server from the report JSON, saved as a download.
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(`${view} .detail-back`);
  await page.click(`${view} .report-export button.button`);
  await page.waitForSelector(".popover .export-menu-items");
  assert.deepEqual(await page.$$eval(".popover .export-menu-items button", (els) => els.map((e) => e.textContent)), ["HTML", "Markdown", "JSON", "Text"]);
  await page.click('.popover .export-menu-items button[data-format="html"]');
  await page.waitForFunction(() => /Saved myflames-report-20261015010720\.html/.test(document.querySelector(".tuning-panel")?.textContent || ""));
  assert.equal(exports.length, 1);
  assert.equal(exports[0].format, "html");
  assert.equal(exports[0].report.schema_version, "report-1.0");
  assert.equal(exports[0].report.look_first, "health.select_full_join", "the report as read, not screen state");
  assert.deepEqual([exports[0].source.label, exports[0].source.host, exports[0].source.port], ["Local MySQL (127.0.0.1:3306)", "127.0.0.1", 3306],
    "the export names the connection it was read from");
  assert.equal(JSON.stringify(exports[0].source).includes("password"), false);
  await until(async () => (await fs.readdir(downloads)).some((f) => f.endsWith(".html")), "the HTML file is downloaded");
  assert.equal(await fs.readFile(path.join(downloads, "myflames-report-20261015010720.html"), "utf8"), "html export");
  for (const format of ["markdown", "json", "text"]) {
    await page.waitForFunction(s => !document.querySelector(s)?.disabled, {}, `${view} .report-export button.button`);
    await page.click(`${view} .report-export button.button`);
    await page.click(`.popover .export-menu-items button[data-format="${format}"]`);
    await until(() => exports.some(item => item.format === format), `${format} export requested`);
  }
  assert.deepEqual(exports.map((e) => e.format), ["html", "markdown", "json", "text"]);
  await page.keyboard.press("Escape");

  // While the Query editor is capturing, links that read the server are off (and read nothing).
  flags.capturing = true;
  await h.button("Query editor", ".main-nav ");
  await page.locator(".editor-actions button ::-p-text(Explain)").click();
  await until(() => calls("/api/live/status").length > 0, "the capture runs");
  await h.server();
  await h.fullAnalysis();
  await h.select(`${view} .report-first li`);
  await inspectorTitle("Joins without an index");
  const topLink = ".view-inspector .inspector-actions-bar button[title='Open Top statements by joins without an index']";
  await page.waitForFunction((s) => document.querySelector(s)?.disabled === true, {}, topLink);
  assert.equal(await page.$eval(".view-inspector .inspector-actions-bar button[title='Open Health']", (e) => e.disabled), false, "a link that only opens a view stays on");
  await page.$eval(topLink, (e) => e.click());
  await pause(200);
  assert.equal(calls("/api/live/top").length, 0, "nothing is read during the capture");
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, (e) => e.disabled), true);
  flags.capturing = false;
  await page.waitForFunction((s) => document.querySelector(s)?.disabled === false, { timeout: 15000 }, topLink);

  // A link to Top statements: ranked by joins without an index.
  await h.select(`${view} .report-first li`);
  await inspectorTitle("Joins without an index");
  await page.locator(".view-inspector .inspector-actions-bar button ::-p-text(Open Top statements by joins without an index)").click();
  await page.waitForSelector("#server-nav-top[aria-current=page]");
  await until(() => calls("/api/live/top").length === 1, "the link reads Top statements");
  assert.deepEqual([calls("/api/live/top")[0].body.order, calls("/api/live/top")[0].body.table], ["full_join", undefined]);
  assert.equal(await page.$eval(".server-view[data-view=top] .toolbar-field select", (e) => e.value), "full_join");
  await page.waitForSelector('.server-view[data-view=top] tr[data-rank="1"]');
  assert.match(await text(".server-view[data-view=top] .summary-line"), /2 statements · 2 flagged · with joins without an index/);
  const topNotes = await h.notes();
  assert.match(topNotes, /The digest table is full: 5,000 more executions/);
  assert.match(topNotes, /Server uptime 2 d 5 h; the oldest statement listed was first seen 2026-10-12 10:00:00\./);

  // ---- Refresh all -------------------------------------------------------------
  // Load Health, Waits, Locks and Parameters (Top statements is loaded already;
  // Parameters reads on open, so it has no Load button).
  for (const [kind, label] of [["health", "Load health"], ["waits", "Load wait events"], ["locks", "Load locks"], ["config", ""]]) {
    await h.view(kind);
    // A view reads once on its own when it first appears on screen (Health after the Tuning
    // place, Waits, Locks), so its Load button may be gone or vanish while we look: the
    // summary line below is the check that the report arrived.
    if (label) {
      await page.locator(`.server-view[data-view=${kind}] .view-empty button ::-p-text(${label})`).setTimeout(1500).click().catch(() => {});
    }
    await page.waitForFunction((k) => !!document.querySelector(`.server-view[data-view=${k}] .summary-line`), {}, kind);
    await h.idle(`.server-view[data-view=${kind}]`);
  }
  const refreshed = Object.keys(replies);
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), false);
  assert.match(await page.$eval(refreshAll, (e) => e.title), /Refresh all 17 sections for this connection \(⌥⌘R\)/);
  // Hold every read: three start (the sampling views first), the rest wait.
  for (const p of refreshed) hold.add(p);
  const before = state.other.length;
  const reportRuns = state.seen.length;
  traffic.inflight = 0;
  traffic.peak = 0;
  state.delay = 1000;
  await clickRefreshAll();
  await until(() => held.length === 3, "three reads start");
  await pause(300);
  assert.equal(held.length, 3, "never more than three at once");
  assert.equal(state.seen.length, reportRuns, "analysis must not overlap the per-view reads");
  assert.deepEqual(held.map((e) => e.path), ["/api/live/health", "/api/live/waits", "/api/live/io"]);
  assert.equal(held[0].body.interval, 5, "Health keeps its window");
  assert.equal(held[2].body.interval, 10, "unopened I/O uses its configured window");
  // It names the views it reads now, so a slow one is never hidden behind a count.
  assert.match(await statusText(), /^Refreshing 1 of 17 · Health, Waits, I\/O…$/);
  assert.equal(await page.$$eval(".source-list .source-refreshing", (els) => els.length), 10); // the Server list: 10 views (Database diagram and the Full analysis are places of their own)
  await h.workload();
  assert.equal(await page.$$eval(".source-list .source-refreshing", (els) => els.length), 4);
  await h.server();
  assert.ok(await page.$(".server-place .refresh-all-stop"), "the button becomes Stop");
  await h.fullAnalysis();
  assert.equal(await page.$eval('.server-view[data-view="tuning"] .toolbar-refresh', e => e.disabled), true,
    "Full analysis cannot start its own worker pool beside the global view queue");
  await h.view("config");
  // Single flight: ⌥⌘R while it runs starts nothing.
  await page.keyboard.down("Alt");
  await page.keyboard.down("Meta");
  await page.keyboard.press("KeyR");
  await page.keyboard.up("Meta");
  await page.keyboard.up("Alt");
  await pause(200);
  assert.equal(held.length, 3);
  // Locks fails; the others answer.
  fail.set("/api/live/locks", "Could not read locks: ERROR 2013 (HY000): Lost connection to MySQL server during query");
  release("/api/live/health");
  await until(() => held.length === 3 && held.some((e) => e.path === "/api/live/innodb"), "the next view starts");
  while (held.length || state.other.length - before < refreshed.length) {
    if (held.length) release();
    await pause(30);
  }
  await until(() => state.seen.length === reportRuns + 1, "the final analysis phase starts");
  assert.equal(await page.$eval('.server-view[data-view="config"] .toolbar-refresh', e => e.disabled), true,
    "individual reads cannot overlap the final analysis worker pool");
  state.delay = 0;
  await page.waitForSelector(`${refreshAll}`);
  const run = state.other.slice(before).map((o) => o.path);
  assert.deepEqual([...run].sort(), [...refreshed].sort(), "every section is read once, including unopened sections");
  assert.equal(state.seen.length, reportRuns + 1, "Full analysis refreshes in the final phase");
  assert.equal(calls("/api/live/top").at(-1).body.order, "full_join", "Top statements keeps its order");
  assert.equal(calls("/api/live/error-log").at(-1).body.limit, 200, "Error log uses the same bounded read as its own Refresh");
  assert.equal(calls("/api/live/indexes").at(-1).body.all_schemas, undefined, "unopened Indexes never grants large-schema consent");
  assert.equal(calls("/api/live/tables").at(-1).body.all_schemas, undefined, "unopened Tables never grants large-schema consent");
  assert.equal(traffic.peak, 3, "at most three reads at once");
  await page.waitForFunction(() => /^16 of 17 views refreshed; Locks failed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  // The failure, per view: a mark in the list of views, the list, a note above the view.
  assert.ok(await page.$("#server-nav-locks .source-stale[data-refresh=failed]"));
  // The compact status marker has its own screen-reader text before the
  // refresh failure. Check all the screen-reader text, not the first span.
  assert.match(await page.$$eval("#server-nav-locks .visually-hidden", els => els.map(e => e.textContent).join(" ")),
    /not refreshed: Could not read locks/);
  assert.equal(await page.$("#server-nav-health .source-stale"), null);
  await page.click(".server-place .refresh-all-status");
  const list = await page.$$eval(".popover .refresh-all-list li", (els) => els.map((e) => [e.dataset.view, e.dataset.state, e.textContent]));
  assert.equal(list.length, 17);
  assert.equal(list.findIndex(x => x[0] === "errorlog"), list.findIndex(x => x[0] === "innodb") + 1);
  assert.deepEqual(list.filter(x => x[1] !== "ok").map(x => [x[0], x[1]]), [["locks", "failed"]]);
  assert.equal(list.at(-1)[0], "report", "analysis is scheduled after individual sections");
  assert.match(list[0][2], /Refreshed\s*Health\s*\d+:\d+:\d+.*, \d+ s ago/);
  assert.match(list.find(x => x[0] === "locks")[2], /Failed\s*Locks\s*Could not read locks: ERROR 2013.* The previous report stays\./);
  await page.keyboard.press("Escape");
  await h.view("locks");
  assert.match(await text(".refresh-note[data-view=locks]"), /Refresh all could not refresh Locks: Could not read locks: ERROR 2013.*This is the previous report\./);
  assert.ok(await page.$(".server-view[data-view=locks] .summary-line"), "the previous report stays");
  fail.clear();
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "refresh-all.png") });
  // Locks' own Refresh then succeeds: the mark and the note go; the run's list keeps what happened.
  hold.clear();
  await refreshView();
  await page.waitForFunction(() => !document.querySelector(".refresh-note"));
  assert.equal(await page.$("#server-nav-locks .source-stale"), null);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(".server-place .refresh-all-status");
  assert.match(await page.$eval('.popover .refresh-all-list li[data-view="locks"]', (e) => e.textContent),
    /Failed\s*Locks\s*Could not read locks: ERROR 2013.*Refreshed since by its own Refresh\./);
  await page.keyboard.press("Escape");

  // Locks' own Refresh with Waits only, then Refresh all: joined, never a second read with the old setting.
  await page.click(".server-view[data-view=locks] .lock-check input");
  hold.add("/api/live/locks");
  const locksBefore = calls("/api/live/locks").length;
  await refreshView();
  await until(() => held.length === 1, "Locks' own read starts");
  assert.equal(held[0].body.waits_only, true);
  traffic.peak = traffic.inflight;
  await clickRefreshAll();
  await page.waitForFunction(() => /^Refreshing/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  await until(() => calls("/api/live/config").length >= 3, "the other views refresh");
  await pause(200);
  assert.equal(calls("/api/live/locks").length, locksBefore + 1, "one Locks read for both, with Waits only");
  assert.ok(traffic.peak <= 3, "already running individual reads occupy refresh worker slots");
  release("/api/live/locks");
  hold.clear();
  await page.waitForFunction(() => /^17 of 17 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  await page.click(".server-view[data-view=locks] .lock-check input");

  // A newer read of a view that fails, then the older Refresh all answer: not counted as refreshed.
  hold.add("/api/live/health");
  await clickRefreshAll();
  await until(() => held.some((e) => e.path === "/api/live/health"), "Refresh all reads Health");
  await h.view("health");
  fail.set("/api/live/health", (body) => (body.interval === 10 ? "Could not read the server status: ERROR 2003 (HY000)" : null));
  hold.delete("/api/live/health");
  await page.select(".server-view[data-view=health] .health-window select", "10");
  await refreshView();
  await page.waitForSelector(".server-view[data-view=health] [role=alert]");
  release("/api/live/health");
  await page.waitForFunction(() => /^16 of 17 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  await page.click(".server-place .refresh-all-status");
  assert.match(await page.$eval('.popover .refresh-all-list li[data-view="health"]', (e) => `${e.dataset.state} ${e.textContent}`),
    /^replaced Replaced\s*Health\s*A newer read of this view was asked for/);
  await page.keyboard.press("Escape");
  assert.ok(await page.$("#server-nav-health .source-stale"), "Health still shows its previous report");
  assert.match(await text(".refresh-note[data-view=health]"), /a newer read replaced it/);
  fail.clear();
  await page.select(".server-view[data-view=health] .health-window select", "5");
  await refreshView();
  await page.waitForFunction(() => !document.querySelector(".refresh-note"));

  // A view's own Refresh already reading is joined, not requested again.
  await h.view("health");
  hold.clear();
  hold.add("/api/live/health");
  const healthBefore = calls("/api/live/health").length;
  await refreshView();
  await until(() => held.length === 1, "Health's own read starts");
  await clickRefreshAll();
  await page.waitForFunction(() => /^Refreshing/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  await until(() => calls("/api/live/config").length >= 3, "the other views refresh");
  await pause(200);
  assert.equal(calls("/api/live/health").length, healthBefore + 1, "one Health read for both");
  release("/api/live/health");
  await page.waitForFunction(() => /^17 of 17 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(await page.$$eval(".source-list .source-stale", (els) => els.length), 0, "no view is marked any more");
  assert.equal(await page.$(".refresh-note"), null);

  // Escape stops it: queued views are never read and keep their reports.
  for (const p of refreshed) hold.add(p);
  const stopBefore = state.other.length;
  await clickRefreshAll();
  await until(() => held.length === 3, "three reads start");
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Escape");
  await page.waitForSelector(refreshAll);
  while (held.length) release();
  await pause(300);
  assert.equal(state.other.length - stopBefore, 3, "nothing more starts after Escape");
  await page.waitForFunction(() => /^0 of 17 views refreshed; stopped$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(await page.$$eval(".source-list .source-stale[data-refresh=stopped]", (els) => els.length), 10);
  await h.workload();
  assert.equal(await page.$$eval(".source-list .source-stale[data-refresh=stopped]", (els) => els.length), 4);
  await h.server();
  assert.ok(await page.$(".server-view[data-view=health] .summary-line"), "Health keeps its report");

  // A connection switch stops a run and drops its late answers.
  const switchBefore = state.other.length;
  await clickRefreshAll();
  await until(() => held.length === 3, "three reads start");
  const staging = await connectionOption(page, "Staging");
  await chooseConnection(page, staging);
  await page.waitForSelector(".server-view[data-view=health][data-other-connection=true]");
  const late = structuredClone(health);
  late.server_version = "8.4.99";
  release("/api/live/health", [200, late]);
  while (held.length) release();
  await pause(300);
  // The switch also reads the new connection's privileges once (not a view).
  assert.equal(state.other.slice(switchBefore).filter((o) => o.path !== "/api/live/privileges").length, 3, "nothing more starts after the switch");
  assert.doesNotMatch(await text(".server-view[data-view=health] .fact-strip"), /8\.4\.99/, "a late answer for the old connection is dropped");
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), false, "new connection can refresh without opening each section");
  assert.equal(await page.$(".server-place .refresh-all-status"), null, "the run belongs to the other connection");
  hold.clear();

  // The Full analysis from the other connection: greyed, server actions off, export still offered.
  await h.fullAnalysis();
  assert.match(await text(`${view} .other-connection`), /From Local MySQL \(127\.0\.0\.1:3306\), refresh to load this server\./);
  await page.click("#tuning-tab-scorecard");
  await page.waitForSelector("#tuning-panel-scorecard .tuning-score");
  // The scorecard has no Refresh: its note offers the read that loads this
  // server (Run full analysis), and its export is off until then.
  assert.match(await text(`${view} .other-connection`), /From Local MySQL \(127\.0\.0\.1:3306\)\. Run full analysis to load this server\./, "the scorecard names the server the report came from");
  assert.equal(await page.$eval(`${view} .other-connection .gate-action button`, (e) => e.textContent), "Run full analysis");
  assert.equal(await page.$eval(`${view} .report-export button.button`, (e) => e.disabled), true, "the scorecard of another server is not exported");
  // A check's full-width details keep the note and its action.
  await h.select(`${view} .tuning-check[data-check]`);
  await page.waitForFunction((v) => [...document.querySelectorAll(`${v} .other-connection .gate-action button`)].some((b) => b.offsetParent && b.textContent === "Run full analysis"), {}, view);
  await page.click(`${view} .detail-back`);
  await h.fullAnalysis();
  await h.select(`${view} .report-first li`);
  await inspectorTitle("Joins without an index");
  assert.equal(await page.$eval('.view-inspector [aria-label="Explain statement 1 with joins without an index"]', (e) => e.disabled), true);
  assert.equal(await page.$eval(".view-inspector .inspector-actions-bar button:nth-child(2)", (e) => e.disabled), true, "Open Top statements is off");
  assert.equal(await page.$eval(`${view} .report-export button.button`, (e) => e.disabled), false);
  // Its export (made by the real server) names the connection it was read from, not the one in use.
  flags.passExport = true;
  for (const f of await fs.readdir(downloads)) await fs.rm(path.join(downloads, f));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(`${view} .detail-back`);
  await page.click(`${view} .report-export button.button`);
  await page.click('.popover .export-menu-items button[data-format="markdown"]');
  await until(async () => (await fs.readdir(downloads)).some((f) => f.endsWith(".md")), "the Markdown file is downloaded");
  const markdown = await fs.readFile(path.join(downloads, (await fs.readdir(downloads)).find((f) => f.endsWith(".md"))), "utf8");
  assert.match(markdown, /^Connection: Local MySQL \(127\.0\.0\.1:3306\)/m);
  assert.doesNotMatch(markdown, /db\.staging/);
  flags.passExport = false;
  await page.keyboard.press("Escape");

  // Session restore keeps that the analysis ran, never the report. The place is
  // Tuning, not a Server view: the saved Server view stays a list view, never "report" or "tuning".
  const saved = await h.savedSlow((s) => s.report_run === true);
  assert.ok(saved.state.query_lab.slow.kind !== "report" && saved.state.query_lab.slow.kind !== "tuning", "the Full analysis is never the saved Server view");
  assert.equal(saved.state.query_lab.slow.order, "full_join");
  assert.equal(JSON.stringify(saved.state).includes("report-1.0"), false, "the report is not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.fullAnalysis();
  assert.match(await text(`${view} .view-empty`), /A full analysis ran in your last session\. Reports are not saved: run it again, about 10 to 20 s\./);

  // 390 px: no horizontal page scroll; details use the full work area.
  await chooseConnection(page, staging);
  await page.locator(`${view} .view-empty button ::-p-text(Run full analysis)`).click();
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  await h.narrow("report-390.png");
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  await page.waitForSelector(`${view} .report-first li`, { visible: true });
  await page.screenshot({ path: path.join(h.shots, "report-view-390.png") });
  // Open the first recommendation in the full work area. Escape returns to
  // the retained list; choosing the row again preserves the recommendation.
  await h.select(`${view} .report-first li`);
  await page.waitForSelector(".view-inspector.is-detail");
  await inspectorTitle("Joins without an index");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await h.select(`${view} .report-first li`);
  await page.waitForSelector(".view-inspector.is-detail");
  await page.screenshot({ path: path.join(h.shots, "report-inspector-390.png") });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 390 px`);
  // Enter on a finding inside a section moves focus to its detail title.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await h.select(`${view} .report-sections li[data-section="indexes"]`);
  await page.waitForSelector(".view-inspector.is-detail li[data-finding] button");
  await page.focus('.view-inspector li[data-finding="indexes.redundant"] button');
  await page.keyboard.press("Enter");
  await inspectorTitle("Redundant indexes");
  await page.waitForFunction(() => document.activeElement?.matches(".view-inspector.is-detail .inspector-title h3"));
  await page.keyboard.press("Escape");
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(200);

  // Staging -> Archive -> Staging while a run is on its way: its answer, read
  // before the switch, is never shown (and the run can start again).
  const archive = await connectionOption(page, "Archive");
  // connectionOption leaves the picker open; close it before clicking Refresh under it.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  const lateReport = structuredClone(rich);
  lateReport.summary = { ...lateReport.summary, text: "Late answer from before the switch" };
  state.queue.push({ status: 200, body: lateReport });
  state.delay = 1500;
  await refreshView();
  await page.waitForSelector(`${view} .capture-status`);
  await chooseConnection(page, archive);
  await page.waitForSelector(`${view}[data-other-connection=true]`);
  await chooseConnection(page, staging);
  await page.waitForSelector(`${view}:not([data-other-connection])`);
  await pause(2000);
  state.delay = 0;
  assert.doesNotMatch(await text(`${view} .summary-line`), /Late answer/);
  assert.equal(await page.$(`${view} .capture-status`), null, "nothing is loading any more");

  // A database chosen in the diagram picker participates before any diagram
  // exists. Switching views preserves that choice for the global refresh.
  await h.view("diagram");
  // The Database field is a combobox (input + datalist) since the diagram picker change.
  await page.waitForFunction(() => Array.from(document.querySelectorAll("#diagram-databases option")).some(e => e.value === "shop"));
  await page.click('.diagram-source input[aria-label="Database"]', { clickCount: 3 });
  await page.type('.diagram-source input[aria-label="Database"]', "shop");
  await h.view("health");
  await h.view("diagram");
  await page.waitForFunction(() => document.querySelector('.diagram-source input[aria-label="Database"]')?.value === "shop");
  const diagramReads = calls("/api/schema-diagram/read").length;
  // Database diagram is a place of its own with no Refresh all: Server's
  // Refresh all reads it too, and the place shows that answer.
  assert.equal(await page.$('.server-place.is-diagram-place [data-action=refresh-all]'), null, "no Refresh all in the diagram place");
  await h.server();
  await clickRefreshAll();
  await page.waitForFunction(() => /^18 of 18 views refreshed$/.test(document.querySelector(".refresh-all-status")?.textContent || ""));
  assert.equal(calls("/api/schema-diagram/read").length, diagramReads + 1);
  assert.equal(calls("/api/schema-diagram/read").at(-1).body.database, "shop");
  await h.view("diagram");
  await page.waitForSelector('.diagram-table[aria-label="Table shop.orders"]');

  // Move the arrangement while global metadata refresh is still reading.
  // Its response must retain that new position and place new tables below it.
  holdDiagram = true;
  await h.server();
  await clickRefreshAll();
  await until(() => !!pendingDiagram, "global diagram read is held");
  await h.view("diagram");
  await page.focus('[data-table-id="shop.orders"]');
  await page.keyboard.press("ArrowRight");
  await page.keyboard.press("ArrowDown");
  const moved = await page.$eval('.diagram-table[aria-label="Table shop.orders"]', e => ({x: e.style.left, y: e.style.top}));
  const pending = pendingDiagram; pendingDiagram = null; holdDiagram = false;
  await pending.request.respond({status: 200, contentType: "application/json", body: JSON.stringify(diagramReply(pending.body, true))});
  await page.waitForSelector('.diagram-table[aria-label="Table shop.shipments"]');
  assert.deepEqual(await page.$eval('.diagram-table[aria-label="Table shop.orders"]', e => ({x: e.style.left, y: e.style.top})), moved);
  assert.equal(await page.evaluate(() => {
    const existing = document.querySelector('.diagram-table[aria-label="Table shop.orders"]');
    const added = document.querySelector('.diagram-table[aria-label="Table shop.shipments"]');
    return parseFloat(added.style.top) >= parseFloat(existing.style.top) + parseFloat(existing.style.height);
  }), true, "global refresh places new tables below the latest arrangement");
  await h.server();
  await page.waitForFunction(() => /^18 of 18 views refreshed$/.test(document.querySelector(".refresh-all-status")?.textContent || ""));

  assert.deepEqual(h.errors, []);
  console.log("full analysis and refresh all: ok");
} finally {
  await h.close();
  await fs.rm(downloads, { recursive: true, force: true });
}
