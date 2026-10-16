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
// (that it ran, never the report), 390 px; and Refresh all: only views already
// loaded for the connection in use, never the analysis, at most three reads
// at once with the sampling views first, single flight (⌥⌘R while running does
// nothing), a view's own Refresh joined, failures per view (badge, list, note
// above the view, previous report kept), Escape stops, a connection switch
// stops it and drops late answers. Run: npm run test:report
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

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
const replies = {
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
const extra = {
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

const h = await open("/api/live/report", "report", extra);
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: rich };
const view = ".server-view[data-view=report]";
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
  assert.deepEqual(await texts(".source-list .source-group:first-child .source-label"), ["Health", "InnoDB", "Full analysis"]);
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), true, "Refresh all waits for a loaded view");
  assert.match(await page.$eval(refreshAll, (e) => e.title), /Load a view first/);
  await h.view("report");
  assert.match(await text(`${view} .view-empty`), /Runs every Server view once and ranks what to look at first, about 10 to 20 s\. It only reads\./);
  assert.match(await h.popover("About Full analysis"), /never applies them/);
  assert.equal(state.seen.length, 0, "nothing runs until asked");
  assert.equal(await page.$(`${view} .report-export`), null, "no export before a report");

  // Run: the progress line counts the seconds; then the report.
  state.delay = 1300;
  await page.locator(`${view} .view-empty button ::-p-text(Run full analysis)`).click();
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Running every Server view once: \d+ s so far, about 10 to 20 s in all\. It only reads\./);
  await page.waitForFunction((v) => /[1-9] s so far/.test(document.querySelector(`${v} .capture-status`)?.textContent || ""), {}, view);
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
  assert.equal(await page.$eval("#server-nav-report .status-dot", (e) => e.className), "status-dot is-problem");

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
  await h.server();
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
  // Keyboard: Down moves to the next finding and the inspector follows.
  await page.keyboard.press("ArrowDown");
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
  await page.click(`${view} .report-export button.button`);
  await page.waitForSelector(".popover .report-export-formats");
  assert.deepEqual(await page.$$eval(".popover .report-export-formats button", (els) => els.map((e) => e.textContent)), ["HTML", "Markdown", "JSON", "Text"]);
  await page.click('.popover .report-export-formats button[data-format="html"]');
  await page.waitForFunction(() => /Saved myflames-report-20261015010720\.html/.test(document.querySelector(".popover")?.textContent || ""));
  assert.equal(exports.length, 1);
  assert.equal(exports[0].format, "html");
  assert.equal(exports[0].report.schema_version, "report-1.0");
  assert.equal(exports[0].report.look_first, "health.select_full_join", "the report as read, not screen state");
  assert.deepEqual([exports[0].source.label, exports[0].source.host, exports[0].source.port], ["Local MySQL (127.0.0.1:3306)", "127.0.0.1", 3306],
    "the export names the connection it was read from");
  assert.equal(JSON.stringify(exports[0].source).includes("password"), false);
  await until(async () => (await fs.readdir(downloads)).some((f) => f.endsWith(".html")), "the HTML file is downloaded");
  assert.equal(await fs.readFile(path.join(downloads, "myflames-report-20261015010720.html"), "utf8"), "html export");
  for (const format of ["markdown", "json", "text"]) await page.click(`.popover .report-export-formats button[data-format="${format}"]`).then(() => pause(150));
  assert.deepEqual(exports.map((e) => e.format), ["html", "markdown", "json", "text"]);
  await page.keyboard.press("Escape");

  // While the Query editor is capturing, links that read the server are off (and read nothing).
  flags.capturing = true;
  await h.button("Query editor", ".main-nav ");
  await page.locator(".editor-actions button ::-p-text(Explain)").click();
  await until(() => calls("/api/live/status").length > 0, "the capture runs");
  await h.server();
  await h.view("report");
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
  // Load Health, Waits, Locks and Configuration (Top statements is loaded already).
  for (const [kind, label] of [["health", "Load health"], ["waits", "Load wait events"], ["locks", "Load locks"], ["config", "Load configuration"]]) {
    await h.view(kind);
    await page.locator(`.server-view[data-view=${kind}] .view-empty button ::-p-text(${label})`).click();
    await page.waitForFunction((k) => !!document.querySelector(`.server-view[data-view=${k}] .summary-line`), {}, kind);
    await h.idle(`.server-view[data-view=${kind}]`);
  }
  const loaded = ["/api/live/health", "/api/live/waits", "/api/live/top", "/api/live/locks", "/api/live/config"];
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), false);
  assert.match(await page.$eval(refreshAll, (e) => e.title), /Refresh the 5 views loaded for this connection \(⌥⌘R\)/);
  // Hold every read: three start (the sampling views first), the rest wait.
  for (const p of loaded) hold.add(p);
  const before = state.other.length;
  const reportRuns = state.seen.length;
  traffic.inflight = 0;
  traffic.peak = 0;
  await clickRefreshAll();
  await until(() => held.length === 3, "three reads start");
  await pause(300);
  assert.equal(held.length, 3, "never more than three at once");
  assert.deepEqual(held.map((e) => e.path), ["/api/live/health", "/api/live/waits", "/api/live/top"]);
  assert.equal(held[0].body.interval, 5, "Health keeps its window");
  assert.equal(held[2].body.order, "full_join", "Top statements keeps its order");
  assert.match(await statusText(), /^Refreshing 1 of 5…$/);
  assert.equal(await page.$$eval(".source-list .source-refreshing", (els) => els.length), 5);
  assert.ok(await page.$(".server-place .refresh-all-stop"), "the button becomes Stop");
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
  await until(() => held.length === 3 && held.some((e) => e.path === "/api/live/locks"), "the next view starts");
  while (held.length || state.other.length - before < 5) {
    if (held.length) release();
    await pause(30);
  }
  await page.waitForSelector(`${refreshAll}`);
  const run = state.other.slice(before).map((o) => o.path);
  assert.deepEqual([...run].sort(), [...loaded].sort(), "only the views already loaded, once each");
  assert.equal(state.seen.length, reportRuns, "Refresh all never runs the analysis");
  assert.equal(traffic.peak, 3, "at most three reads at once");
  await page.waitForFunction(() => /^4 of 5 views refreshed; Locks failed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  // The failure, per view: a mark in the list of views, the list, a note above the view.
  assert.ok(await page.$("#server-nav-locks .source-stale[data-refresh=failed]"));
  assert.match(await text("#server-nav-locks .visually-hidden"), /not refreshed: Could not read locks/);
  assert.equal(await page.$("#server-nav-health .source-stale"), null);
  await page.click(".server-place .refresh-all-status");
  const list = await page.$$eval(".popover .refresh-all-list li", (els) => els.map((e) => [e.dataset.view, e.dataset.state, e.textContent]));
  assert.deepEqual(list.map((x) => [x[0], x[1]]), [["health", "ok"], ["waits", "ok"], ["top", "ok"], ["locks", "failed"], ["config", "ok"]]);
  assert.match(list[0][2], /Refreshed\s*Health\s*\d+:\d+:\d+.*, \d+ s ago/);
  assert.match(list[3][2], /Failed\s*Locks\s*Could not read locks: ERROR 2013.* The previous report stays\./);
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
  await clickRefreshAll();
  await page.waitForFunction(() => /^Refreshing/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  await until(() => calls("/api/live/config").length >= 3, "the other views refresh");
  await pause(200);
  assert.equal(calls("/api/live/locks").length, locksBefore + 1, "one Locks read for both, with Waits only");
  release("/api/live/locks");
  hold.clear();
  await page.waitForFunction(() => /^5 of 5 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
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
  await page.waitForFunction(() => /^4 of 5 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
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
  await page.waitForFunction(() => /^5 of 5 views refreshed$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(await page.$$eval(".source-list .source-stale", (els) => els.length), 0, "no view is marked any more");
  assert.equal(await page.$(".refresh-note"), null);

  // Escape stops it: queued views are never read and keep their reports.
  for (const p of loaded) hold.add(p);
  const stopBefore = state.other.length;
  await clickRefreshAll();
  await until(() => held.length === 3, "three reads start");
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press("Escape");
  await page.waitForSelector(refreshAll);
  while (held.length) release();
  await pause(300);
  assert.equal(state.other.length - stopBefore, 3, "nothing more starts after Escape");
  await page.waitForFunction(() => /^0 of 5 views refreshed; stopped$/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(await page.$$eval(".source-list .source-stale[data-refresh=stopped]", (els) => els.length), 5);
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
  assert.equal(await page.$eval(refreshAll, (e) => e.disabled), true, "nothing is loaded for Staging");
  assert.equal(await page.$(".server-place .refresh-all-status"), null, "the run belongs to the other connection");
  hold.clear();

  // The Full analysis from the other connection: greyed, server actions off, export still offered.
  await h.view("report");
  assert.match(await text(`${view} .other-connection`), /From Local MySQL \(127\.0\.0\.1:3306\), refresh to load this server\./);
  assert.equal(await page.$("#server-nav-report .status-dot"), null, "no badge from another connection");
  await h.select(`${view} .report-first li`);
  await inspectorTitle("Joins without an index");
  assert.equal(await page.$eval('.view-inspector [aria-label="Explain statement 1 with joins without an index"]', (e) => e.disabled), true);
  assert.equal(await page.$eval(".view-inspector .inspector-actions-bar button:nth-child(2)", (e) => e.disabled), true, "Open Top statements is off");
  assert.equal(await page.$eval(`${view} .report-export button.button`, (e) => e.disabled), false);
  // Its export (made by the real server) names the connection it was read from, not the one in use.
  flags.passExport = true;
  for (const f of await fs.readdir(downloads)) await fs.rm(path.join(downloads, f));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(`${view} .report-export button.button`);
  await page.click('.popover .report-export-formats button[data-format="markdown"]');
  await until(async () => (await fs.readdir(downloads)).some((f) => f.endsWith(".md")), "the Markdown file is downloaded");
  const markdown = await fs.readFile(path.join(downloads, (await fs.readdir(downloads)).find((f) => f.endsWith(".md"))), "utf8");
  assert.match(markdown, /^Connection: Local MySQL \(127\.0\.0\.1:3306\)/m);
  assert.doesNotMatch(markdown, /db\.staging/);
  flags.passExport = false;
  await page.keyboard.press("Escape");

  // Session restore keeps the view and that the analysis ran, never the report.
  const saved = await h.savedSlow((s) => s.kind === "report" && s.report_run === true);
  assert.equal(saved.state.query_lab.slow.order, "full_join");
  assert.equal(JSON.stringify(saved.state).includes("report-1.0"), false, "the report is not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-report[aria-current=page]");
  assert.match(await text(`${view} .view-empty`), /A full analysis ran in your last session\. Reports are not saved: run it again, about 10 to 20 s\./);

  // 390 px: no horizontal page scroll; the inspector is a sheet.
  await chooseConnection(page, staging);
  await page.locator(`${view} .view-empty button ::-p-text(Run full analysis)`).click();
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  await h.narrow("report-390.png");
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  if (await page.$(".server-layout.show-list")) await page.click("#server-nav-report");
  await page.waitForSelector(`${view} .report-first li`, { visible: true });
  await page.screenshot({ path: path.join(h.shots, "report-view-390.png") });
  // The new run starts on its Look at first pick and the inspector was restored
  // open: at this width that is a modal sheet over the list (its scrim takes a
  // click on the row and closes it). Escape closes it; choosing the row opens it.
  await page.waitForSelector(".view-inspector.is-sheet");
  await inspectorTitle("Joins without an index");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await h.select(`${view} .report-first li`);
  await page.waitForSelector(".view-inspector.is-sheet");
  await page.screenshot({ path: path.join(h.shots, "report-inspector-390.png") });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 390 px`);
  // Sheet width: Enter on a finding inside a section moves focus to its title in the sheet.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await h.select(`${view} .report-sections li[data-section="indexes"]`);
  await page.waitForSelector(".view-inspector.is-sheet li[data-finding] button");
  await page.focus('.view-inspector li[data-finding="indexes.redundant"] button');
  await page.keyboard.press("Enter");
  await inspectorTitle("Redundant indexes");
  await page.waitForFunction(() => document.activeElement?.matches(".view-inspector.is-sheet .inspector-title h3"));
  await page.keyboard.press("Escape");
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(200);

  // Staging -> Archive -> Staging while a run is on its way: its answer, read
  // before the switch, is never shown (and the run can start again).
  const archive = await connectionOption(page, "Archive");
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

  assert.deepEqual(h.errors, []);
  console.log("full analysis and refresh all: ok");
} finally {
  await h.close();
  await fs.rm(downloads, { recursive: true, force: true });
}
