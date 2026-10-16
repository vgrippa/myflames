// Health view of the Server place: mocked /api/live/health with health-1.0
// reports computed from live two-sample captures (fixtures/), the real Python
// server for everything else (session autosave, the Learn catalog). One
// summary line, diagnostics as one-line rows sorted by severity, compact
// tiles, and every explanation in the inspector. Joins without an index, the
// first-table scans and rows read by full scans show the statements behind
// them on request (/api/live/top mocked), with the Performance Schema gate.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause, SERVER_GROUPS, SLOW_DEFAULTS } from "./perf-harness.mjs";

const mysql = await load("health-mysql-8.4.json");
const mariadb = await load("health-mariadb-13.0.json");
// A live capture with a 64 MiB buffer pool and random reads of a larger table.
const full = await load("health-mysql-8.4-full.json");
// A second MySQL sample with a different rate, so the sparklines have two points.
const later = structuredClone(mysql);
later.taken_at = "2026-10-13T16:29:40+00:00";
for (const m of later.metrics) if (m.key === "questions") m.value = m.rate = 120.5;
const trendOf = (report, key) => report.diagnostics.find((d) => d.key === key).trend;
trendOf(later, "buffer_pool").value = 1048576;
trendOf(later, "redo").value = 2 * 1048576;
const failure = { status: 400, body: { error: "Could not read the server status: ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:3306'" } };
const rank = { problem: 0, watch: 1, off: 2, unknown: 3, info: 4, ok: 5 };

// The statements behind a number: top-1.0 for an attribution order (scan answers Performance Schema off).
const joinStatement = (digest, sample, joins) => ({
  digest, digest_text: "SELECT ?", schema: "shop", count: 60, total_ms: 2400, avg_ms: 40, max_ms: 80, rows_examined: 90000,
  rows_sent: 600, examined_per_sent: 150, no_index_used: 60, no_good_index_used: 0, tmp_disk_tables: 0, sort_merge_passes: 0,
  sample_text: sample, sample_truncated: false, first_seen: "2026-10-13 08:00:00", last_seen: "2026-10-13 16:00:00",
  flags: ["no_index_used"], capture_problem: null, select_full_join: joins, select_range_check: 0, select_scan: 0,
});
const topFor = (body) => body.order === "scan"
  ? { status: 400, body: { error: "Performance Schema is off on this server (performance_schema=OFF), so there are no statement digests." } }
  : { status: 200, body: {
    schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: body.order, limit: body.limit, schema: null,
    notes: ["Counters accumulate since the server started or the digest table was last truncated."],
    statements: [joinStatement("a".repeat(64), "SELECT * FROM orders o JOIN customers c ON c.note = o.note", 60)], table: null,
    coverage: { uptime_s: 3600, first_seen: "2026-10-13 08:00:00", digest_lost: 0, overflow: null } } };

// holdTop: Show statements requests wait in heldTop until answered by the test.
let holdTop = false;
const heldTop = [];
const h = await open("/api/live/health", "health", {
  "/api/live/top": (body, request) => (holdTop ? (heldTop.push({ body, request }), null) : topFor(body)),
});
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=health]";
const tile = (key) => `${view} .tile-item[data-metric="${key}"]`;
const diag = (key) => `${view} li[data-diagnostic="${key}"]`;
const inspectorTitle = (title) => page.waitForFunction((t) => document.querySelector(".view-inspector h3")?.textContent === t, {}, title);
try {
  // Health is the first view and opens by default; nothing is read until asked.
  await h.server();
  assert.deepEqual(await texts(".source-list .source-group h3"), Object.keys(SERVER_GROUPS));
  assert.deepEqual(await texts(".source-list .source-group:first-child .source-label"), ["Health", "InnoDB", "Full analysis"]);
  assert.equal(await page.$eval("#server-nav-health", (e) => e.getAttribute("aria-current")), "page");
  assert.match(await text(`${view} .view-empty`), /Samples SHOW GLOBAL STATUS twice/);
  assert.match(await h.popover("About Health"), /Performance Schema is not needed/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.disabled), true);
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0", "auto-refresh is off by default");
  assert.deepEqual(await page.$$eval(`${view} .health-window option`, (els) => els.map((e) => e.value)), ["1", "2", "5", "10", "15", "30", "60"]);
  assert.equal(await page.$eval(`${view} .health-window select`, (e) => e.value), "5");
  // The place's intro is behind the ⓘ next to its title.
  assert.match(await h.popover("About Server"), /changes the server only when you choose an action and confirm it/);

  // Load with a 10 s window: loading state, then the report.
  await page.select(`${view} .health-window select`, "10");
  state.delay = 400;
  await button("Load health", view + " ");
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Sampling the server for 10 s/);
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  state.delay = 0;
  assert.equal(state.seen.length, 1);
  assert.equal(state.seen[0].interval, 10);
  assert.equal(state.seen[0].connection.host, "127.0.0.1");
  assert.equal(state.seen[0].connection.password || "", "", "no password was typed");
  const summary = await text(`${view} .summary-line`);
  assert.match(summary, /Problem\s*3 problems, 1 to watch, 12 ok · diagnostics: 1 problem, 1 to watch/);
  assert.match(summary, /10\.0 s window at/);
  const facts = await text(`${view} .fact-strip`);
  assert.match(facts, /MySQL 8\.4\.11/);
  assert.match(facts, /Host\s*127\.0\.0\.1:3306/);
  assert.match(facts, /Buffer pool\s*128\.0 MiB/);
  // The badge in the sub-navigation carries the worst status.
  assert.equal(await page.$eval("#server-nav-health .status-dot", (e) => e.className), "status-dot is-problem");
  // Notes are one control; no explanatory paragraph is on screen.
  assert.match(await h.notes(), /Notes about this read/);
  assert.equal(await page.$$eval(`${view} .view-main p`, (els) => els.filter((e) => !e.closest(".summary-line") && e.textContent.length > 80).length), 0);
  assert.deepEqual(await texts(`${view} .view-main h3`),
    ["Diagnostics", "Throughput", "Connections", "InnoDB buffer pool", "Row locks", "Temporary tables and sorts", "Table cache", "Redo and I/O"]);
  // Diagnostics: one row each, problems first, with the headline number.
  const order = await page.$$eval(`${view} li[data-diagnostic]`, (els) => els.map((e) => e.dataset.diagnostic));
  const expected = [...mysql.diagnostics].sort((a, b) => rank[a.status] - rank[b.status]).map((d) => d.key);
  assert.deepEqual(order, expected);
  assert.deepEqual(await page.$$eval(`${view} li[data-diagnostic]`, (els) => els.map((e) => [
    e.dataset.diagnostic, e.querySelector(".row-title")?.textContent,
    e.querySelector(".row-value")?.textContent, e.querySelector(".row-value > span")?.title,
  ])), expected.map((key) => {
    const d = mysql.diagnostics.find((item) => item.key === key);
    return [key, d.title, d.headline, d.headline];
  }), "each diagnostic retains its label, value and full headline for inspection");
  assert.equal(mysql.diagnostics.length, 10);
  assert.match(await text(`${view} .health-diag-count`), /^1 problem, 1 to watch, 6 ok, 2 without a threshold or data$/);
  const rowHeights = await page.$$eval(`${view} li[data-diagnostic] .grouped-row`, (els) => els.map((e) => e.getBoundingClientRect().height));
  assert.ok(rowHeights.every((x) => x <= 40), `one-line rows (${rowHeights.join(", ")})`);
  const bp = mysql.diagnostics.find((d) => d.key === "buffer_pool");
  assert.match(await text(diag("buffer_pool")), new RegExp(bp.headline.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(bp.headline, /^Warming: \d+% full, about .* to full at this rate$/);
  // No threshold while warming: the pill names the state, not a status.
  assert.equal(await text(`${diag("buffer_pool")} .status-pill`), "Warming");
  assert.equal(await page.$eval(`${diag("buffer_pool")} .status-pill`, (e) => e.className), "status-pill is-info");
  // The inspector: verdict, rule, why, numbers (the estimate tagged), settings with defaults, sources.
  await h.select(diag("buffer_pool"));
  await inspectorTitle("Buffer pool: full or warming");
  const bpWhy = await h.inspector();
  assert.match(bpWhy, /Reads from disk are expected while it warms/);
  assert.match(bpWhy, /full when free pages are at or below/);
  assert.doesNotMatch(bpWhy, /What to do/, "no suggestion while warming");
  assert.match(bpWhy, /Trend appears after the next refresh/);
  assert.match(bpWhy, /page cleaner evicts the least recently used pages/);
  assert.match(bpWhy, /Time to full at this ratee?stimate/);
  assert.match(bpWhy, /innodb_lru_scan_depth1024 · default 1024/);
  assert.match(bpWhy, /From Innodb_buffer_pool_pages_total/);
  assert.ok(await page.$('.view-inspector dl[aria-label="Buffer pool: full or warming: numbers"] dt > code'), "wrapped metric identifiers use full-row labels");
  // Keyboard: Down moves to the next diagnostic and the inspector follows; Escape closes it.
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent !== "Buffer pool: full or warming");
  await page.keyboard.press("ArrowUp");
  await inspectorTitle("Buffer pool: full or warming");
  // A watched diagnostic shows what to do and its caveat.
  await h.select(diag("redo"));
  await inspectorTitle(mysql.diagnostics.find((d) => d.key === "redo").title);
  assert.match(await text(`${diag("redo")} .status-pill`), /Watch/);
  const redo = await h.inspector();
  assert.match(redo, /Covers about .* of writes/);
  assert.match(redo, /What to do.*If this rate is typical of busy periods/s);
  assert.match(redo, /Caveat: One short window may be a burst/);
  assert.match(redo, /Capacity coversabout .* of writes at this rate/);
  assert.match(redo, /innodb_redo_log_capacity100\.0 MiB · default 100\.0 MiB/);
  // A problem diagnostic links to the cause and the lesson.
  await h.select(diag("sorts"));
  await page.waitForFunction(() => /Open Top statements.*Lesson: Filesort/.test(document.querySelector(".view-inspector .inspector-actions-bar")?.textContent || ""));
  await h.select(diag("binlog_cache"));
  await page.waitForFunction(() => !/What to do/.test(document.querySelector(".view-inspector")?.textContent || "x What to do"));
  // Tiles: label, value with unit, status dot plus word; compact, no prose.
  assert.equal((await page.$$(`${view} .tile-item`)).length, mysql.metrics.length);
  const join = await text(tile("select_full_join"));
  assert.match(join, /Joins without an index/);
  assert.match(join, /Problem/);
  assert.match(join, /\d+(\.\d+)?\s*\/s/);
  assert.equal(await page.$eval(`${tile("select_full_join")} .status-pill`, (e) => e.className), "status-pill is-problem");
  assert.equal((await page.$$(`${tile("select_full_join")} .status-dot`)).length, 1);
  assert.match(await text(`${tile("bp_dirty")} .tile-value`), /^\d+\.\d%$/);
  assert.match(await text(`${tile("redo_generated")} .tile-value`), /KiB\/s/);
  assert.match(await text(`${tile("questions")} .status-pill`), /No threshold/);
  const tileHeights = await page.$$eval(`${view} .metric-tile`, (els) => els.map((e) => e.getBoundingClientRect().height));
  assert.ok(Math.max(...tileHeights) <= 120, `tiles stay at most 120 px (${Math.max(...tileHeights)})`);
  // The tile's inspector: what it measures, the rule, the source variables. Enter opens it.
  await page.focus(`${tile("select_full_join")} button`);
  await page.keyboard.press("Enter");
  await inspectorTitle("Joins without an index");
  const why = await h.inspector();
  assert.match(why, /Select_full_join/);
  assert.match(why, /Rule of thumb: watch when any occur in the window, problem at 10 per second/);
  assert.match(why, /From Select_full_join/);
  // The statements behind it: read only when asked, cumulative since they were first seen.
  const topCalls = () => h.state.other.filter((o) => o.path === "/api/live/top");
  assert.match(why, /Statements behind it/);
  assert.equal(await page.$$eval(".view-inspector .inspector-actions-bar button", (els) => els.filter((e) => /Open (?:in )?Top statements/.test(e.textContent)).length), 0, "attribution owns the Top statements action");
  assert.equal(topCalls().length, 0, "nothing is read until Show statements");
  await page.locator(".view-inspector button ::-p-text(Show statements)").click();
  await page.waitForSelector(".view-inspector .statements-behind-list li");
  assert.deepEqual([topCalls()[0].body.order, topCalls()[0].body.limit, topCalls()[0].body.table], ["full_join", 5, undefined]);
  const behind = await h.inspector();
  assert.equal(await page.$$eval(".view-inspector button", (els) => els.filter((e) => /Open (?:in )?Top statements/.test(e.textContent)).length), 1);
  assert.match(behind, /1 statement by joins without an index, since 2026-10-13 08:00:00/);
  assert.match(behind, /SELECT \* FROM orders o JOIN customers c ON c\.note = o\.note/);
  assert.match(behind, /60 calls · 2\.40 s total · 60 joins without an index · shop/);
  assert.ok(await page.$('.view-inspector [aria-label="Explain statement 1 with joins without an index"]'));
  assert.ok(await page.$('.view-inspector [aria-label="Run analysis on statement 1 with joins without an index"]'));
  await page.click(".view-inspector .statements-behind .notes-button");
  assert.match(await page.$eval(".popover", (e) => e.textContent), /Server uptime 1 h 00 min; the oldest statement listed was first seen 2026-10-13 08:00:00\./);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".popover"));
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "health-statements.png") });
  // The first-table scans tile opens its inspector on click; Performance Schema off is a gate, not an error.
  await h.select(tile("select_scan"));
  await inspectorTitle("Full scans of the first table");
  await page.locator(".view-inspector button ::-p-text(Show statements)").click();
  await page.waitForSelector(".view-inspector .statements-behind .gate");
  assert.deepEqual(topCalls().map((c) => c.body.order), ["full_join", "scan"]);
  assert.match(await text(".view-inspector .statements-behind .gate"), /Performance Schema is off, so there are no statement digests\./);
  assert.ok(await page.$(".view-inspector .statements-behind .gate .instr-link"));
  assert.equal(await page.$(".view-inspector .statements-behind [role=alert]"), null);
  // Rows read by full scans maps to reads without an index.
  await h.select(diag("full_scans"));
  await page.waitForSelector('.view-inspector .statements-behind[data-order="no_index"]');
  // Leaving it while its read is on its way: the late answer is dropped, never kept for later.
  holdTop = true;
  await page.locator(".view-inspector button ::-p-text(Show statements)").click();
  await page.waitForSelector(".view-inspector .statements-behind .capture-status");
  await h.select(tile("select_full_join"));
  await inspectorTitle("Joins without an index");
  holdTop = false;
  const late = heldTop.shift();
  assert.equal(late.body.order, "no_index");
  await late.request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(topFor(late.body).body) }).catch(() => undefined);
  await pause(300);
  await h.select(diag("full_scans"));
  await page.waitForSelector('.view-inspector .statements-behind[data-order="no_index"]');
  assert.equal(await page.$(".view-inspector .statements-behind-list"), null, "the answer read before leaving is not shown");
  assert.ok(await page.$(".view-inspector .statements-behind button ::-p-text(Show statements)"));
  // Open in Top statements: ranked by joins without an index; back to Rank by total time.
  await h.select(tile("select_full_join"));
  await inspectorTitle("Joins without an index");
  assert.ok(await page.$(".view-inspector .statements-behind-list li"), "the list read before is kept");
  await page.locator(".view-inspector button ::-p-text(Open in Top statements)").click();
  await page.waitForSelector("#server-nav-top[aria-current=page]");
  await page.waitForFunction(() => document.querySelector(".server-view[data-view=top] .toolbar-field select")?.value === "full_join");
  for (let i = 0; i < 100 && topCalls().length < 4; i++) await pause(50);
  assert.deepEqual([topCalls()[3].body.order, topCalls()[3].body.limit], ["full_join", 25]);
  await page.waitForSelector('.server-view[data-view=top] tr[data-rank="1"]');
  await page.select(".server-view[data-view=top] .toolbar-field select", "total");
  await h.view("health");
  await page.waitForSelector(`${view} .summary-line`);
  await h.select(tile("questions"));
  await page.waitForFunction(() => /Trend appears after the next refresh/.test(document.querySelector(".view-inspector")?.textContent || ""));
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "health.png") });

  // Links: a flagged row lock tile opens Locks; the view keeps the report.
  await h.select(tile("row_lock_avg"));
  await page.locator(".view-inspector .inspector-actions-bar button ::-p-text(Open Locks)").click();
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-locks");
  await h.view("health");
  await page.waitForSelector(`${view} .summary-line`);
  assert.equal(state.seen.length, 1, "switching views does not sample again");
  // The lesson link opens Learn on that lesson.
  await h.select(tile("sort_merge_passes"));
  const links = await text(".view-inspector .inspector-actions-bar");
  assert.match(links, /Open Top statements/);
  assert.match(links, /Lesson: Filesort/);
  await page.locator(".view-inspector .inspector-actions-bar button ::-p-text(Lesson: Filesort)").click();
  await page.waitForFunction(() => document.querySelector(".main-nav [aria-current=page]")?.textContent.includes("Learn"));
  await page.waitForFunction(() => /Filesort/i.test(document.querySelector(".teach-workspace h1")?.textContent || ""), { timeout: 15000 });
  await h.server();
  await page.waitForSelector(`${view} .summary-line`);

  // Refresh: a second sample draws the sparklines on the key tiles.
  state.queue.push({ status: 200, body: later });
  await h.refresh();
  await page.waitForFunction(() => !!document.querySelector('.tile-item[data-metric="questions"] svg.sparkline'));
  await h.idle(view);
  const spark = await page.$eval(`${tile("questions")} svg.sparkline`, (e) => [e.getAttribute("aria-label"), e.querySelectorAll("rect").length]);
  const qps = Math.round(mysql.metrics.find((m) => m.key === "questions").value);
  assert.equal(spark[0], `Statements from clients, last 2 samples: low 121/s, high ${qps}/s, latest 121/s`);
  assert.equal(spark[1], 2, "each sample has a hover target");
  for (const key of ["threads_running", "row_lock_waits", "bp_disk_reads"]) assert.ok(await page.$(`${tile(key)} svg.sparkline`), key);
  // Diagnostics trend bytes read from disk and redo bytes per second (in the inspector).
  await h.select(diag("buffer_pool"));
  await page.waitForSelector(".view-inspector svg.sparkline");
  assert.match(await page.$eval(".view-inspector svg.sparkline", (e) => e.getAttribute("aria-label")),
    /^Read from disk, last 2 samples: low [\d.]+ (B|KiB)\/s, high 1\.0 MiB\/s, latest 1\.0 MiB\/s$/);
  assert.ok(trendOf(mysql, "buffer_pool").value < 1048576);
  await h.select(diag("redo"));
  await page.waitForFunction(() => /^Redo generated/.test(document.querySelector(".view-inspector svg.sparkline")?.getAttribute("aria-label") || ""));
  assert.match(await page.$eval(".view-inspector svg.sparkline", (e) => e.getAttribute("aria-label")),
    /^Redo generated, last 2 samples: low [\d.]+ KiB\/s, high 2\.0 MiB\/s, latest 2\.0 MiB\/s$/);
  await h.select(diag("table_cache"));
  await page.waitForFunction(() => !document.querySelector(".view-inspector svg.sparkline"));
  assert.equal(await page.$(`${tile("com_select")} svg.sparkline`), null, "only key rates get a trend");

  // Automatic refresh: opt-in; runs on the chosen period while the view is visible.
  await page.evaluate(() => {
    const original = window.setInterval;
    window.setInterval = (fn, ms, ...rest) => original(fn, ms >= 10000 ? 150 : ms, ...rest);
  });
  const before = state.seen.length;
  await page.select(`${view} .auto-refresh select`, "30");
  await pause(700);
  assert.ok(state.seen.length > before, "auto-refresh sampled again");
  await page.select(`${view} .auto-refresh select`, "0");
  await h.idle(view);
  const stopped = state.seen.length;
  await pause(500);
  assert.equal(state.seen.length, stopped, "Off stops automatic refresh");
  // History is capped at 20 samples.
  for (let i = 0; i < 22; i++) {
    await h.refresh();
    await h.idle(view);
  }
  assert.equal(await page.$eval(`${tile("questions")} svg.sparkline`, (e) => e.querySelectorAll("rect").length), 20);

  // Error: shown in an alert, the last report stays, auto-refresh turns off.
  await page.select(`${view} .auto-refresh select`, "30");
  state.queue.push(failure);
  await h.refresh();
  await page.waitForSelector(`${view} .error`);
  assert.match(await text(`${view} [role=alert]`), /ERROR 2003/);
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0");
  assert.ok(await page.$(`${view} .summary-line`));

  // A full buffer pool with a working set larger than it: the bytes read per second.
  state.queue.push({ status: 200, body: full });
  await h.refresh();
  await page.waitForFunction(() => /read from disk$/.test(document.querySelector('li[data-diagnostic="buffer_pool"] .row-value')?.textContent || ""));
  await h.idle(view);
  const fullBp = full.diagnostics.find((d) => d.key === "buffer_pool");
  assert.match(await text(`${diag("buffer_pool")} .row-value`), new RegExp(`^${fullBp.headline.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`));
  assert.match(fullBp.headline, /^\d+\.\d MiB\/s read from disk$/);
  assert.match(await text(`${diag("buffer_pool")} .status-pill`), /Problem/);
  await h.select(diag("buffer_pool"));
  await page.waitForFunction(() => /The buffer pool is full and InnoDB read/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.match(await h.inspector(), /Caveat: Leave memory for connections/);
  assert.match(await text(".view-inspector .inspector-actions-bar"), /Lesson: How the buffer pool keeps pages/);
  await (await page.$(`${view} .health-diagnostics`)).screenshot({ path: path.join(h.shots, "health-diagnostics.png") });

  // MariaDB report: engine label and engine-specific explanations.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 13\.0\.2/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  assert.equal(await page.$(`${view} .error`), null);
  await h.select(tile("bp_hit_ratio"));
  await page.waitForFunction(() => /already includes read-ahead/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.equal(await text(`${diag("binlog_cache")} .status-pill`), "Not applicable");
  await h.select(diag("binlog_cache"));
  await page.waitForFunction(() => /The binary log is off/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.match(await text(`${diag("tmp_tables")} .status-pill`), /Problem/);
  await h.select(diag("tmp_tables"));
  await page.waitForFunction(() => /go to Aria tables on disk/.test(document.querySelector(".view-inspector")?.textContent || ""));

  // Narrow layout: no horizontal page scroll at 390 px; tiles and rows fit;
  // the inspector is a bottom sheet that Escape closes.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 390 px`);
  assert.ok(await page.$eval(tile("redo_generated"), (e) => e.getBoundingClientRect().right) <= 390, "tiles fit the viewport");
  const rights = await page.$$eval(`${view} li[data-diagnostic]`, (els) => els.map((e) => e.getBoundingClientRect().right));
  assert.ok(rights.every((r) => r <= 390), "diagnostic rows fit the viewport");
  await page.screenshot({ path: path.join(h.shots, "health-390.png"), fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });

  // Session restore keeps the view, the interval and the inspector, never the report.
  await page.select(`${view} .health-window select`, "30");
  let saved = await h.savedSlow((s) => s.health_interval === 30);
  assert.deepEqual(saved.state.query_lab.slow, { ...SLOW_DEFAULTS, kind: "health", health_interval: 30, inspector: true });
  assert.equal(JSON.stringify(saved.state).includes("health-1.0"), false, "reports are not saved");
  assert.equal(JSON.stringify(saved.state).includes("Innodb_"), false, "sample history is not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-health[aria-current=page]");
  assert.equal(await page.$eval(`${view} .health-window select`, (e) => e.value), "30");
  assert.match(await text(`${view} .view-empty`), /No health sample yet/);
  assert.ok(await page.$(".view-inspector .inspector-placeholder"), "the inspector stays open, with nothing selected");
  // A restored Top statements view stays selected too.
  await h.view("top");
  saved = await h.savedSlow((s) => s.kind === "top");
  assert.equal(saved.state.query_lab.slow.health_interval, 30);

  assert.deepEqual(h.errors, []);
  console.log("health view: ok");
} finally {
  await h.close();
}
