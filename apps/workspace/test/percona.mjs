// Percona Server in the Server views: mocked live reads with reports made
// from the GA matrix's Percona Server 8.0.46-37 and 8.4.11-11 recordings
// (test/fixtures/perf/*-percona-8.*.json, test/test_percona.py). Every
// summary and the window footer name Percona Server, never MySQL; Health
// shows the thread pool under pool-of-threads; Indexes says when user
// statistics kept an index; Tables adds Rows changed; Parameters tags the
// variables Percona adds. Partitioned tables, which user statistics do not
// count reliably, show an unknown Rows changed and, with Performance Schema
// off, their indexes under No data (reports recorded from throwaway Percona
// Server 8.4.11-11 containers). Checked in light and dark at 1440x900 and
// 720x520.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";
import { chooseConnection } from "./chrome-helpers.mjs";

const health = await load("health-percona-8.0.json");
const indexes = await load("indexes-percona-8.4.json");
const tables = await load("tables-percona-8.4.json");
const config = await load("config-percona-8.4.json");
const indexesOff = await load("indexes-percona-8.4-psoff.json");
const tablesParted = await load("tables-percona-8.4-partitioned.json");
// One Performance Schema candidate that user statistics saw read: the backend
// keeps it (test_percona.TestIndexes.test_an_index_userstat_saw_read_is_kept).
const kept = indexes.unused.find((u) => u.verdict === "candidate");
Object.assign(kept, { verdict: "keep", keep_reason: "userstat", userstat_rows_read: 42,
  why: "Performance Schema shows no use, but user statistics count 42 rows read through it; keep it." });
delete kept.drop_sql; delete kept.test_sql; delete kept.caveat;
const schemas = {
  schema_version: "schemas-1.0", engine: "mysql", flavor: "percona", server_product: "Percona Server",
  server_version: "8.4.11-11", taken_at: "", uptime_s: 600, performance_schema: true, notes: [], user_tables: 13,
  all_limit: 2000, all_note: "Reading every user schema is the default up to 2,000 tables.",
  system: ["mysql", "sys", "performance_schema", "information_schema"],
  schemas: [{ name: "review", tables: 8, system: false }, { name: "testdb", tables: 6, system: false }],
};

const live = { indexes, tables };
let digestTablesResponse = { status: 200, body: await load("digest-tables.json") };
// Query digest: statements in two schemas and one without a default schema.
const statement = (digest, schema, total_ms, count) => ({ digest, digest_text: `SELECT * FROM t_${digest}`, schema, count, total_ms,
  avg_ms: total_ms / count, max_ms: total_ms, lock_time_ms: 0.25, rows_examined: 10, rows_sent: 5, tmp_tables: 0, tmp_disk_tables: 0,
  select_scan: 0, select_full_join: 0, sample_text: null, first_seen: null, last_seen: null, flags: [], capture_problem: null });
// The Slow log table source's report, as read from a Percona Server.
const slowTable = { ...(await load("slow-log-table.json")), flavor: "percona", server_product: "Percona Server",
  server_version: "8.4.11-11" };
const digestReport = { engine: "mysql", flavor: "percona", server_product: "Percona Server",
  server_version: "8.4.11-11", notes: [],
  statements: [statement("a1", "review", 1200, 2), statement("b2", "testdb", 59.04, 1), statement("c3", null, 0.5, 1)] };
// Performance Schema rankings read against the whole server: 7 digests, the
// three listed plus 4 others; the CPU consumer off (its default); a1 is New.
const detail = (s, extra = {}) => ({ ...s, cpu_ms: 0, p95_ms: 1258.925, p99_ms: 1258.925, p999_ms: 1258.925, new: false, ...extra });
digestReport.statements[0].digest_text += " JOIN review.order_view v ON v.id=t_a1.id JOIN archive.missing m ON m.id=t_a1.id";
const topReport = { ...digestReport, schema_version: "top-1.0",
  statements: digestReport.statements.map((s) => detail(s, s.digest === "a1" ? { new: true, first_seen: "2026-10-27 08:00:00.123456", last_seen: "2026-10-27 11:59:00" } : {})),
  totals: { digests: 7, total_ms: 2519.08, executions: 16 }, others: { digests: 4, total_ms: 1259.54, executions: 12 },
  cpu: { collected: false }, coverage: { uptime_s: 200000, first_seen: null, digest_lost: 0, overflow: null, counting_since: null } };
// a1's two calls: about 600 ms and 1.2 s (MySQL buckets 240 and 255).
const histogram = { schema_version: "digest-histogram-1.0", engine: "mysql", server_version: "8.4.11-11", flavor: "percona",
  server_product: "Percona Server", schema: "review", digest: "a1", available: true, reason: null,
  buckets: [{ bucket: 240, low_ms: 602.559586, high_ms: 630.957344, count: 1 }, { bucket: 255, low_ms: 1202.264435, high_ms: 1258.925412, count: 1 }] };
// Two saved connections for the histogram race at the end (nothing earlier
// uses them: the connection in use stays the unsaved default until then).
const profile = (id, name, host) => ({ id, name, host, port: 3306, user: "root", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
  has_saved_password: false, secret_backend: "", secret_backend_label: "" });
const profiles = [profile("pa", "Percona A", "db-a"), profile("pb", "Percona B", "db-b")];
// Histogram requests are answered later while *holdHistograms* is set.
let holdHistograms = false;
const heldHistograms = [];
const answer = (request, body) => request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(body) })
  .catch(() => "aborted by the page");
const h = await open("/api/live/health", "percona", {
  "/api/profiles/list": { status: 200, body: { items: profiles, secret_store: { backend: "vault", label: "encrypted vault", available: true } } },
  "/api/live/indexes": () => ({ status: 200, body: live.indexes }),
  "/api/live/tables": () => ({ status: 200, body: live.tables }),
  "/api/live/config": { status: 200, body: config },
  "/api/live/schemas": { status: 200, body: schemas },
  "/api/live/top": { status: 200, body: topReport },
  "/api/live/digest-histogram": (body, request) => {
    if (!holdHistograms) return { status: 200, body: histogram };
    heldHistograms.push(request);
    return null;
  },
  "/api/live/digest-tables": () => digestTablesResponse,
  "/api/live/slow-log": { status: 200, body: slowTable },
}, { fallback: { status: 200, body: health }, list: "docked" });
const { page, errors, text } = h;
const footer = () => page.$$eval('[data-status="engine"]', (els) => els.filter((e) => e.getClientRects().length).map((e) => e.textContent));
const shoot = async (name) => {
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const [w, hgt] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width: w, height: hgt });
      await pause(250);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, `${name}: horizontal scroll of ${overflow}px at ${w} px ${theme}`);
      await page.screenshot({ path: path.join(h.shots, `percona-${name}.${w}.${theme}.png`) });
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
};
try {
  // Health on Percona Server 8.0 with the thread pool.
  await h.server();
  const hv = ".server-view[data-view=health]";
  await page.waitForSelector(`${hv} .summary-line`);
  await h.idle(hv);
  assert.deepEqual(await footer(), ["Percona Server 8.0.46-37"], "the footer names Percona Server");
  const pool = `${hv} li[data-diagnostic="thread_cache"]`;
  await page.waitForSelector(pool);
  assert.match(await text(pool), /Thread pool\s*\d+ threads, \d+ idle/);
  assert.doesNotMatch(await page.$eval(hv, (e) => e.textContent), /MySQL 8\.0\.46/, "never named MySQL");
  await shoot("health");

  // Indexes: Performance Schema with user statistics as evidence.
  await h.view("indexes");
  const iv = ".server-view[data-view=indexes]";
  await page.waitForSelector(`${iv} .fact-strip`);
  await h.idle(iv);
  const facts = await text(`${iv} .fact-strip`);
  assert.match(facts, /Percona Server 8\.4\.11-11/);
  assert.match(facts, /Performance Schema and user statistics/);
  await page.click(`${iv} .segmented button[data-value=unused]`);
  await page.waitForFunction((v) => /Keep: read per user statistics/.test(document.querySelector(v)?.textContent || ""), {}, iv);
  assert.match(await page.$eval(iv, (e) => e.textContent), /Keep: backs a foreign key/);
  await h.select(`${iv} [aria-label="Unused indexes"] tr[data-index="${kept.table}.${kept.index}"]`);
  assert.match(await h.inspector(), /Rows read \(user statistics\)\s*42/);
  await shoot("indexes-userstat");
  await page.click(".detail-back").catch(() => {});

  // Tables: rows changed from TABLE_STATISTICS.
  await h.view("tables");
  const tv = ".server-view[data-view=tables]";
  await page.waitForSelector(`${tv} .fact-strip`);
  await h.idle(tv);
  assert.match(await text(`${tv} .fact-strip`), /Percona Server 8\.4\.11-11/);
  // All tables first: the inventory has the column too.
  const heads = (label) => page.$$eval(`${tv} [aria-label="${label}"] th`, (els) => els.map((e) => e.textContent.trim()));
  assert.ok((await heads("All tables")).some((x) => x.startsWith("Rows changed")), (await heads("All tables")).join(", "));
  await page.click(`${tv} .segmented button[data-value=missing]`);
  await page.waitForSelector(`${tv} [aria-label="Tables without a primary key"] th`);
  const headers = await heads("Tables without a primary key");
  assert.ok(headers.some((x) => x.startsWith("Rows changed")), headers.join(", "));
  await shoot("tables-rows-changed");

  // Partitioned tables: their user statistics are unknown, never zero
  // (test_percona.TestTables.test_partitioned_tables_have_unknown_activity).
  live.tables = tablesParted;
  await h.refresh();
  const missingRow = (t) => `${tv} [aria-label="Tables without a primary key"] tr[data-table="review.${t}"]`;
  await page.waitForSelector(missingRow("parted_nopk"));
  await h.idle(tv);
  const changedCell = async (t) => {
    const index = (await heads("Tables without a primary key")).findIndex((x) => x.startsWith("Rows changed"));
    return page.$eval(missingRow(t), (row, i) => row.children[i].textContent.trim(), index);
  };
  assert.equal(await changedCell("parted_nopk"), "\u2014", "unknown for a partitioned table");
  assert.equal(await changedCell("plain_nopk"), "4");
  await page.click(`${tv} .notes-button`);
  assert.match(await text(".popover"), /User statistics do not count partitioned tables reliably, so rows read and changed are unknown for the 2 partitioned tables in scope/);
  await page.keyboard.press("Escape");
  await h.select(missingRow("parted_nopk"));
  assert.doesNotMatch(await h.inspector(), /TABLE_STATISTICS|no rows changed/, "no inactivity claim");
  await shoot("tables-partitioned");
  await page.click(".detail-back").catch(() => {});

  // Performance Schema off: user statistics are the source, and partitioned
  // tables' indexes are under No data, never unused.
  live.indexes = indexesOff;
  await h.view("indexes");
  await h.refresh();
  await page.waitForFunction((v) => /Usage from user statistics|user statistics \(userstat\)/i.test(document.querySelector(`${v} .fact-strip`)?.textContent || ""), {}, iv);
  await h.idle(iv);
  await page.click(`${iv} .segmented button[data-value=nodata]`);
  const noDataRow = `${iv} [aria-label="Tables with no data"] tr[data-table="parted_pk"]`;
  await page.waitForSelector(noDataRow);
  assert.match(await text(noDataRow), /partitioned, and Performance Schema does not count every read through a partitioned table's indexes/);
  await h.select(noDataRow);
  assert.match(await h.inspector(), /4 indexes, partitioned, and Performance Schema does not count every read through a partitioned table's indexes\. Never listed as unused/);
  await shoot("indexes-partitioned");
  await page.click(".detail-back").catch(() => {});

  // Parameters: the Percona tag on Percona's variables only.
  await h.view("config");
  const cv = ".server-view[data-view=config]";
  await page.waitForSelector(`${cv} tr[data-row-key="thread_pool_size"]`);
  const tag = (name) => page.$eval(`${cv} tr[data-row-key="${name}"]`, (e) => !!e.querySelector(".pill")).catch(() => null);
  assert.equal(await tag("thread_pool_size"), true);
  assert.equal(await tag("userstat"), true);
  assert.equal(await tag("innodb_buffer_pool_size"), false);
  assert.equal(await page.$eval(`${cv} tr[data-row-key="thread_pool_size"] .pill`, (e) => e.getAttribute("title")),
    "A variable Percona Server adds to MySQL");
  assert.deepEqual(await footer(), ["Percona Server 8.4.11-11"]);
  await shoot("parameters-tag");
  // Query digest projects both report shapes (Statement digests and the Slow
  // log table); neither may discard the product.
  await page.click('.main-nav button[data-place="digest"]');
  const qd = '[data-view="query-digest"]';
  for (const table of [false, true]) {
    if (table) {
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/live/slow-log');
      await page.select(`${qd} select[aria-label="Source"]`, 'table');
      await response;
    }
    await page.waitForFunction((tableSource) => {
      const refresh = document.querySelector('[data-view="query-digest"] button[data-action="refresh"]');
      const label = [...document.querySelectorAll('[data-status="engine"]')].find(e => e.getClientRects().length)?.textContent;
      const summary = document.querySelector('[data-view="query-digest"] .summary-line .summary-text')?.textContent || "";
      return refresh && refresh.getAttribute('aria-busy') !== 'true' && document.querySelector('.qd-summary') &&
        label === 'Percona Server 8.4.11-11' && /^mysql\.slow_log · /.test(summary) === tableSource;
    }, {}, table);
    await page.click('button[aria-label="Export query digest"]');
    await page.click('.export-menu-items button[data-format="text"]');
    const preview = 'dialog[open] pre[aria-label="text report"]';
    await page.waitForSelector(preview);
    assert.match(await text(preview), /^# myflames query digest$/m);
    assert.match(await text(preview), /^# Server: Percona Server 8\.4\.11-11$/m);
    assert.match(await text(preview), table ? /^# Rank by: Total time$/m : /^# Filter mode: time$/m);
    await page.keyboard.press('Escape');
    await page.waitForFunction(() => !document.querySelector('dialog[open] pre[aria-label="text report"]'));
  }
  // The slow log's share is of every statement logged in the range, and the
  // summary line is the numbers and the range, with no counting window.
  assert.match(await page.$eval(`${qd} th[data-col="share"]`, (e) => e.title), /every statement in the range/);
  assert.doesNotMatch(await text(`${qd} .summary-line .summary-text`), /since/);
  assert.equal(await page.$(`${qd} select[aria-label="Rank by"] option[value="slow_queries"]`), null, "the 1 s ranking is retired");

  // Back to Statement digests: Performance Schema digests ranked against the whole server.
  const topRead = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/live/top");
  await page.select(`${qd} select[aria-label="Source"]`, "pfs");
  await topRead;
  await page.waitForSelector(`${qd} .qd-others`);
  assert.deepEqual(await page.$$eval(`${qd} select[aria-label="Rank by"] option`, (os) => os.map((o) => o.textContent)),
    ["Total time", "Full scans", "Joins without an index", "Temp tables on disk", "Lock time"]);
  // The Schema choice lists the loaded statements' schemas and filters them on screen.
  const schemaChoice = `${qd} select[aria-label="Schema"]`;
  const qdRows = () => page.$$eval(`${qd} tbody tr[data-row-key]`, (rows) => rows.length);
  assert.deepEqual(await page.$$eval(`${schemaChoice} option`, (os) => os.map((o) => o.textContent)),
    ["All schemas", "review", "testdb", "No default schema"]);
  assert.equal(await qdRows(), 3);
  const digestReads = h.state.other.filter((o) => o.path === "/api/live/top").length;
  await page.select(schemaChoice, "testdb");
  await page.waitForFunction((q) => document.querySelectorAll(`${q} tbody tr[data-row-key]`).length === 1, {}, qd);
  // Latencies read like Top statements: milliseconds, the exact value on hover.
  assert.deepEqual(await page.$eval(`${qd} tbody tr[data-row-key] td[data-col="total"] span`, (e) => [e.textContent, e.title]),
    ["59.04 ms", "59.04 ms"]);
  await page.select(schemaChoice, "review");
  await page.waitForFunction((q) => document.querySelector(`${q} tbody tr[data-row-key] td[data-col="total"]`)?.textContent === "1.20 s", {}, qd);
  assert.equal(await page.$eval(`${qd} tbody tr[data-row-key] td[data-col="total"] span`, (e) => e.title), "1,200 ms");
  assert.equal(await page.$eval(`${qd} tbody tr[data-row-key] td[data-col="avg"]`, (e) => e.textContent), "600 ms");
  await page.select(schemaChoice, await page.$eval(`${schemaChoice} option:last-child`, (o) => o.value));
  await page.waitForFunction((q) => document.querySelector(`${q} tbody tr[data-row-key] td[data-col="schema"]`)?.textContent === "—", {}, qd);
  assert.equal(await qdRows(), 1);
  assert.match(await text(`${qd} .summary-line`), /3 statements .* · 1 shown/);
  await page.select(schemaChoice, "");
  await page.waitForFunction((q) => document.querySelectorAll(`${q} tbody tr[data-row-key]`).length === 3, {}, qd);
  assert.equal(h.state.other.filter((o) => o.path === "/api/live/top").length, digestReads, "the Schema choice never reads");
  // Share of total time is of the server's 2,519.08 ms, not the loaded 1,259.54 ms.
  assert.match(await page.$eval(`${qd} th[data-col="share"]`, (e) => e.title), /server's total statement time/);
  const a1 = `${qd} tbody tr[data-row-key]:first-child`;
  assert.equal(await page.$eval(`${a1} td[data-col="share"]`, (e) => e.textContent.trim()), "47.6%");
  assert.equal(await page.$eval(`${a1} td[data-col="share"] progress`, (e) => e.getAttribute("aria-label")),
    "47.6% of the server's total statement time");
  assert.equal(await text(`${qd} .qd-others`), "4 other statements · 50.0% of total time · 12 calls");
  assert.equal(await page.$(`${qd} .qd-others[data-row-key], ${qd} tr.qd-others`), null, "the closing row is not a selectable row");
  // The summary line leads with the numbers and keeps the period short; the
  // full sentence is on hover, the source sentence in the ⓘ beside the title.
  const summaryText = `${qd} .summary-line .summary-text`;
  assert.equal(await text(summaryText), "3 statements · 1.26 s total · 4 executions · since 2 d 7 h ago");
  assert.ok((await text(summaryText)).length <= 120, "the summary line fits the 120-character budget");
  assert.equal(await page.$eval(`${summaryText} span[title]`, (e) => e.title),
    "Counted since the server started 2 d 7 h ago, or since the digest table was last reset.");
  await page.click(`${qd} button[aria-label="About Query digest"]`);
  await page.waitForSelector(".popover[role=dialog]");
  assert.match(await text(".popover[role=dialog]"),
    /reads performance_schema\.events_statements_summary_by_digest: Performance Schema statement digests counted since the server started 2 d 7 h ago or the digest table was last reset\./);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".popover[role=dialog]"));
  // New: first seen in the last 24 hours, decided on the server.
  assert.deepEqual(await page.$$eval(`${qd} tbody tr[data-row-key] .pill`, (els) => els.map((e) => [e.firstChild.textContent, e.title])),
    [["New", "First seen 2026-10-27 08:00:00"]]);
  // CPU time is a column off by default (Columns menu) and an inspector fact.
  assert.equal(await page.$(`${qd} th[data-col="cpu"]`), null);
  await page.click(`${qd} button[aria-label="Columns of Query digest statements"]`);
  await page.click(`input[data-col="cpu"]`);
  await page.waitForSelector(`${qd} th[data-col="cpu"]`);
  assert.equal(await page.$eval(`${a1} td[data-col="cpu"]`, (e) => e.textContent.trim()), "Not collected");
  await page.click(`input[data-col="cpu"]`);
  await page.waitForFunction((q) => !document.querySelector(`${q} th[data-col="cpu"]`), {}, qd);
  await page.keyboard.press("Escape");
  const histogramReads = () => h.state.other.filter((o) => o.path === "/api/live/digest-histogram");
  assert.equal(histogramReads().length, 0, "nothing reads a histogram before a statement is shown");
  const tableReads = () => h.state.other.filter((o) => o.path === "/api/live/digest-tables");
  assert.equal(tableReads().length, 0, "no table read before a statement is opened");
  // Control only the Tables section's observer, so the on-screen contract
  // is deterministic across viewport sizes and native inspector scrolling.
  await page.evaluate(() => {
    const NativeObserver = window.IntersectionObserver;
    const targets = new Map();
    let visible = false;
    window.__showDigestTables = (shown) => {
      visible = shown;
      targets.forEach((callback) => callback([{ isIntersecting: shown }]));
    };
    window.IntersectionObserver = class {
      constructor(callback, options) { this.callback = callback; this.native = new NativeObserver(callback, options); }
      observe(element) {
        if (element.matches(".qd-tables")) { targets.set(this, this.callback); this.callback([{ isIntersecting: visible }]); }
        else this.native.observe(element);
      }
      unobserve(element) { targets.delete(this); this.native.unobserve(element); }
      disconnect() { targets.delete(this); this.native.disconnect(); }
      takeRecords() { return this.native.takeRecords(); }
    };
  });
  await page.click(`${a1} td[data-col="statement"]`);
  await page.waitForSelector(`${qd} .view-inspector .qd-latency`);
  const inspector = await page.$eval(`${qd} .view-inspector`, (e) => e.textContent);
  assert.match(inspector, /CPU timeNot collectedevents_statements_cpu is off\. How to turn it on/);
  assert.ok(await page.$(`${qd} .view-inspector [data-instruments-row="consumer:events_statements_cpu"]`));
  assert.match(inspector, /P951\.26 sP991\.26 sP99\.91\.26 s/);
  // One aligned list: the Latency rows share the Statement rows' columns.
  const aligned = await page.evaluate((q) => {
    const firstRow = (f) => {
      const row = f.querySelector(":scope > div");
      const dt = row?.querySelector("dt")?.getBoundingClientRect();
      const dd = row?.querySelector("dd")?.getBoundingClientRect();
      return dt && dd ? [Math.round(dt.left), Math.round(dd.left)] : null;
    };
    const lists = [...document.querySelectorAll(`${q} .view-inspector .inspector-fields`)].map(firstRow).filter(Boolean);
    return lists.length > 1 && lists.every(([dt, dd]) => dt === lists[0][0] && dd === lists[0][1]);
  }, qd);
  assert.ok(aligned, "every fact list shares one label column and one value column");
  // The digest hash is one line with the full value on hover and in Copy.
  assert.deepEqual(await page.$eval(`${qd} .view-inspector .qd-digest code`, (e) => [e.textContent, e.title]), ["a1", "a1"]);
  assert.ok(await page.$(`${qd} .view-inspector .qd-digest button`), "the digest has a Copy button");
  // First seen and Last seen: the server's own clock as a short date and
  // time (never shifted to this computer's zone); the exact value on hover.
  const seen = await page.$$eval(`${qd} .view-inspector .inspector-fields time`, (els) => els.map((e) => [e.textContent, e.title]));
  assert.deepEqual(seen, [["Oct 27, 2026, 8:00 AM", "Server time: 2026-10-27 08:00:00.123456"], ["Oct 27, 2026, 11:59 AM", "Server time: 2026-10-27 11:59:00"]]);
  // CPU time: "Not collected" on one line, the reason and its link on one
  // muted line below.
  const cpuNote = await page.evaluate((q) => {
    const fields = document.querySelector(`${q} .view-inspector .inspector-fields`);
    const rows = [...fields.children].map((r) => [r.querySelector("dt"), r.querySelector("dd")]);
    const rect = (e) => { const r = e?.getBoundingClientRect(); return r ? { top: r.top, bottom: r.bottom, right: r.right, height: r.height } : null; };
    const value = rect(rows.find(([dt]) => dt?.textContent === "CPU time")?.[1]);
    const note = rect(fields.querySelector(".qd-cpu-note"));
    const box = rect(document.querySelector(`${q} .view-inspector .inspector-scroll`));
    return { value, note, right: box?.right };
  }, qd);
  assert.ok(cpuNote.value && cpuNote.note, "the CPU note is rendered");
  assert.ok(cpuNote.note.top >= cpuNote.value.bottom - 1, "the reason sits below Not collected");
  assert.ok(Math.round(cpuNote.note.height) <= 19, `the reason and its link fit on one line (${cpuNote.note.height})`);
  assert.ok(cpuNote.note.right <= cpuNote.right + 1, "nothing overflows the inspector");
  // The histogram reads once for the statement on screen, then draws.
  const chart = `${qd} .view-inspector .qd-hist svg[role=img]`;
  await page.waitForSelector(chart);
  assert.deepEqual(histogramReads().map((o) => [o.body.schema, o.body.digest]), [["review", "a1"]]);
  assert.equal(await page.$$eval(`${chart} .qd-hist-bar`, (els) => els.length), 2);
  assert.equal(await page.$eval(chart, (e) => e.getAttribute("aria-label")),
    "Latency histogram. Most calls took 501 ms to 1.3 s; the slowest took 1.20 s.");
  assert.equal(await text(`${qd} .qd-hist-summary`), "Most calls took 501 ms to 1.3 s; the slowest took 1.20 s.");
  assert.deepEqual(await page.$$eval(`${chart} .qd-hist-hit title`, (els) => els.map((e) => e.textContent)),
    ["501 to 631 ms: 1 call (50.0%)", "1 to 1.3 s: 1 call (50.0%)"]);
  await page.$eval(`${qd} .view-inspector .qd-latency`, (e) => e.scrollIntoView({ block: "center" }));
  await shoot("query-digest");
  assert.equal(histogramReads().length, 1, "resizing and theme changes never read again");
  const tableSection = `${qd} .view-inspector .qd-tables`;
  assert.equal(tableReads().length, 0, "Tables waits while its section is below the fold");
  await page.$eval(tableSection, (e) => e.scrollIntoView({ block: "center" }));
  await page.evaluate(() => window.__showDigestTables(true));
  await page.waitForSelector(`${tableSection} .qd-index-list`);
  assert.deepEqual(tableReads().map((o) => [o.body.schema, o.body.text]), [["review", topReport.statements[0].digest_text]]);
  assert.match(await text(tableSection), /Found in the statement text/);
  assert.match(await text(tableSection), /Rows \(estimate\)/);
  assert.match(await text(tableSection), /PRIMARY \(id\).*unique/);
  assert.match(await text(tableSection), /idx_customer \(customer_id, created_at\).*invisible/);
  assert.match(await text(tableSection), /Views have no indexes of their own/);
  assert.equal(await page.$$eval(`${tableSection} button`, (es) => es.filter((e) => e.textContent === "Open in Indexes").length), 1);
  await shoot("query-digest-tables");
  assert.equal(tableReads().length, 1, "resizing and theme changes reuse table metadata");
  await page.evaluate(() => window.__showDigestTables(false));
  // A different row, while the whole place is hidden, also waits.
  await page.click(`${qd} button[aria-label="Close inspector"]`);
  await page.click(`${qd} tbody tr:nth-child(2) td[data-col="statement"]`);
  await h.server();
  await page.evaluate(() => window.__showDigestTables(true));
  await pause(100);
  assert.equal(tableReads().length, 1, "a hidden Query digest never reads table metadata");
  digestTablesResponse = { status: 400, body: { error: "Statement metadata is unavailable." } };
  await page.click('.main-nav button[data-place="digest"]');
  await page.waitForFunction((q) => document.querySelector(`${q} .qd-tables`)?.textContent.includes("Statement metadata is unavailable."), {}, qd);
  assert.equal((await text(tableSection)).match(/Statement metadata is unavailable\./g)?.length, 1, "the read error appears once");
  assert.equal(tableReads().length, 2);
  await pause(100);
  assert.equal(tableReads().length, 2, "a failed read waits for Try again");
  // Restore the first statement. Its cached tables need no new read.
  await page.click(`${qd} button[aria-label="Close inspector"]`);
  await page.click(`${a1} td[data-col="statement"]`);
  await page.waitForSelector(`${tableSection} .qd-index-list`);
  assert.equal(tableReads().length, 2);
  // Its link opens Server > Instruments at the consumer row.
  await page.click(`${qd} .view-inspector [data-instruments-row="consumer:events_statements_cpu"]`);
  await page.waitForSelector('.server-view[data-view="instruments"]', { visible: true });

  // A latency histogram read belongs to the connection it was sent on: a
  // switch to another connection stops it, and its late answer is dropped
  // even after switching back (A, B, A); the section reads again on A instead
  // of waiting forever for an answer it already threw away.
  await page.evaluate(() => window.__showDigestTables(false));
  holdHistograms = true;
  await chooseConnection(page, "pa");
  const topReads = () => h.state.other.filter((o) => o.path === "/api/live/top").length;
  const topBefore = topReads();
  await page.click('.main-nav button[data-place="digest"]');
  // Opening the place on A reads A's statements (the default connection's report is replaced).
  for (let i = 0; i < 100 && topReads() === topBefore; i++) await pause(20);
  await page.waitForFunction((q) => document.querySelector(q)?.dataset.otherConnection === undefined &&
    document.querySelector(`${q} tbody tr[data-row-key]`), {}, qd);
  const firstRead = histogramReads().length;
  await page.click(`${a1} td[data-col="statement"]`);
  for (let i = 0; i < 100 && heldHistograms.length < 1; i++) await pause(20);
  assert.equal(heldHistograms.length, 1, "the statement on screen reads its histogram on A");
  assert.equal(histogramReads().length, firstRead + 1);
  await page.waitForFunction((q) => /Reading the latency histogram/.test(document.querySelector(`${q} .qd-latency`)?.textContent || ""), {}, qd);
  // B while A's read is out: the section waits for A; A's answer arrives now.
  await chooseConnection(page, "pb");
  await page.waitForFunction((q) => /From Percona A/.test(document.querySelector(`${q} .qd-latency`)?.textContent || ""), {}, qd);
  await answer(heldHistograms[0], { ...histogram, buckets: histogram.buckets.slice(0, 1) });
  await pause(100);
  // Back on A: a new read, not an endless "Reading…".
  await chooseConnection(page, "pa");
  for (let i = 0; i < 100 && heldHistograms.length < 2; i++) await pause(20);
  assert.equal(heldHistograms.length, 2, "switching back to A reads the histogram again");
  // A, B, A again while the second read is out: its late answer is dropped too.
  await chooseConnection(page, "pb");
  await chooseConnection(page, "pa");
  for (let i = 0; i < 100 && heldHistograms.length < 3; i++) await pause(20);
  assert.equal(heldHistograms.length, 3, "each return to A starts one fresh read");
  await answer(heldHistograms[1], { ...histogram, buckets: histogram.buckets.slice(0, 1) });
  await pause(150);
  assert.equal(await page.$(`${qd} .view-inspector .qd-hist svg[role=img]`), null, "an answer read before the switch is not drawn");
  assert.match(await text(`${qd} .qd-latency`), /Reading the latency histogram/);
  await answer(heldHistograms[2], histogram);
  await page.waitForSelector(chart);
  assert.equal(await page.$$eval(`${chart} .qd-hist-bar`, (els) => els.length), 2, "only the latest read on A is drawn");
  assert.equal(histogramReads().length, firstRead + 3);
  holdHistograms = false;
  assert.deepEqual(errors, []);
  console.log("percona: ok");
} finally {
  await h.close();
}
