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
const digestReport = { engine: "mysql", flavor: "percona", server_product: "Percona Server",
  server_version: "8.4.11-11", statements: [] };
const h = await open("/api/live/health", "percona", {
  "/api/live/indexes": () => ({ status: 200, body: live.indexes }),
  "/api/live/tables": () => ({ status: 200, body: live.tables }),
  "/api/live/config": { status: 200, body: config },
  "/api/live/schemas": { status: 200, body: schemas },
  "/api/live/top": { status: 200, body: { ...digestReport, schema_version: "top-1.0" } },
  "/api/live/slow-queries": { status: 200, body: { ...digestReport, schema_version: "slow-queries-1.0", source: "slow_log" } },
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
  // Query digest projects both report shapes; neither may discard the product.
  await page.click('.main-nav button[data-place="digest"]');
  for (const slow of [false, true]) {
    if (slow) {
      const response = page.waitForResponse(r => new URL(r.url()).pathname === '/api/live/slow-queries');
      await page.click('.query-digest-pill-tabs [role="tab"]:last-child');
      await response;
    }
    await page.waitForFunction(() => {
      const refresh = document.querySelector('button[title="Refresh digest profile"]');
      const label = [...document.querySelectorAll('[data-status="engine"]')].find(e => e.getClientRects().length)?.textContent;
      return refresh && !refresh.disabled && label === 'Percona Server 8.4.11-11';
    });
    await page.click('button[title="Export a pt-query-digest style text report or CSV"]');
    await page.waitForSelector('.query-digest-preview-pre');
    assert.match(await text('.query-digest-preview-pre'), /^# Server: Percona Server 8\.4\.11-11$/m);
    assert.match(await text('.query-digest-preview-pre'), slow ? /^# Filter mode: slow_queries$/m : /^# Filter mode: time$/m);
    await page.click('.query-digest-modal-header button');
    await page.waitForFunction(() => !document.querySelector('.query-digest-preview-pre'));
  }
  assert.deepEqual(errors, []);
  console.log("percona: ok");
} finally {
  await h.close();
}
