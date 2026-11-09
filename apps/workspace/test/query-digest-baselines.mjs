// Query digest: Save baseline and Compare with baseline on Performance Schema
// statement digests (fixtures/top-mysql-8.4-lab.json, recorded from a MySQL
// 8.4 lab server). The two actions are items of the Refresh menu; comparing
// lists the statements new since the baseline and the ones whose average
// latency or calls per second grew by more than 50%, before → after, with the
// rule in an ⓘ popover. /api/baselines/* answers from an in-memory store with
// the contract of baselines.py (test_baselines.py covers the real one); every
// other live read is refused by the harness.
// Run: npm run test:baselines
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const recorded = await load("top-mysql-8.4-lab.json");
// The baseline's reading on the server's clock: the server had been up 1,854 s
// since about 09:48 (coverage), so it was read at about 10:18:52.
const SERVER_TIME = "2026-10-31 10:18:52.000000";
const SERVER = { engine: "mysql", flavor: "mysql", product: "MySQL", version: recorded.server_version, server_uuid: "3e11fa47-71ca-11e1-9e33-c80aa9429562",
  hostname: "lab", port: 3306 };
const KEY = `mysql/uuid/${SERVER.server_uuid}`;
// The digests as live/top answers them with with_identity: true.
const top = { ...recorded, coverage: { ...recorded.coverage, oldest_first_seen: "2026-10-31 09:48:00.000000" }, reading_server: { verified: true, server_key: KEY, server: SERVER, uptime_s: recorded.coverage.uptime_s + 1, server_time: SERVER_TIME } };
const baselines = [];
let clock = Date.UTC(2026, 10, 8, 14, 2);
const keepDigests = (r) => ({
  schema_version: r.schema_version, engine: r.engine, server_version: r.server_version, server_product: r.server_product ?? null,
  order: r.order, limit: r.limit, read_at: r.read_at, server_time: SERVER_TIME,
  coverage: { uptime_s: r.coverage.uptime_s, counting_since: r.coverage.counting_since ?? null, oldest_first_seen: r.coverage.oldest_first_seen ?? null, digest_lost: r.coverage.digest_lost },
  totals: r.totals ?? null,
  statements: r.statements.map(({ digest, schema, digest_text, count, total_ms, avg_ms, max_ms, first_seen, last_seen }) =>
    ({ digest, schema, digest_text, count, total_ms, avg_ms, max_ms, first_seen, last_seen })),
});
const baselineMocks = {
  "/api/baselines/list": (body) => ({ status: 200, body: { schema_version: "baseline-list-1.0", kind: body.kind, server: SERVER,
    server_key: KEY, max: 20, items: baselines.filter((b) => b.item.kind === body.kind).map((b) => b.item).reverse() } }),
  "/api/baselines/save": (body) => {
    if (body.report.reading_server?.verified !== true || body.report.reading_server?.server_key !== KEY) {
      return { status: 400, body: { error: "Could not save the baseline: the server's identity was not read with this reading. Refresh, then save again." } };
    }
    clock += 60_000;
    const item = { id: (baselines.length + 1).toString(16).padStart(32, "0"), kind: body.kind, name: body.name.trim(),
      created_at: new Date(clock).toISOString(), server: SERVER, server_key: KEY, read_at: body.report.read_at, server_time: SERVER_TIME,
      uptime_s: body.report.coverage.uptime_s, order: body.report.order, limit: body.report.limit, statements: body.report.statements.length };
    baselines.push({ item, payload: keepDigests(body.report) });
    return { status: 200, body: { ...item, max: 20 } };
  },
  "/api/baselines/get": (body) => {
    const found = baselines.find((b) => b.item.id === body.id);
    return found ? { status: 200, body: { ...found.item, payload: found.payload } }
      : { status: 400, body: { error: "Could not open the baseline: it is no longer saved on this computer." } };
  },
  "/api/baselines/delete": (body) => {
    const index = baselines.findIndex((b) => b.item.id === body.id);
    if (index < 0) return { status: 400, body: { error: "Could not delete the baseline: it is no longer saved on this computer." } };
    baselines.splice(index, 1);
    return { status: 200, body: { deleted: true, id: body.id } };
  },
};

// 600 s later on the same run (uptime 1,854 s, then 2,454 s):
const later = structuredClone(top);
later.coverage.uptime_s = 2454;
const byDigest = (r, prefix) => r.statements.find((s) => s.digest.startsWith(prefix));
// 2f09595d41: 153 calls in 1,847 s before (0.08/s); 600 more in 600 s (1.00/s): calls per second +1,107%.
Object.assign(byDigest(later, "2f09595d41"), { count: 753, total_ms: 6.789 + 600 * 0.05 });
// 2dd7bae4c1: 0.0946 ms on average before; 30 more calls at 0.5 ms each: average latency +428%.
Object.assign(byDigest(later, "2dd7bae4c1"), { count: 93, total_ms: 5.961 + 30 * 0.5 });
const NEW = "e".repeat(64);
later.statements.push(
  { ...byDigest(top, "2dd7bae4c1"), digest: NEW, digest_text: "SELECT * FROM `audit_log` WHERE `ts` > ?", count: 120, total_ms: 360,
    avg_ms: 3, first_seen: "2026-10-31 10:25:00.000000", last_seen: "2026-10-31 10:28:00.000000", sample_text: null, new: false },
  // Listed now, not in the baseline, first seen before it: not judged new.
  { ...byDigest(top, "2dd7bae4c1"), digest: "f".repeat(64), digest_text: "SELECT `id` FROM `refunds`", count: 5, total_ms: 1,
    avg_ms: 0.2, first_seen: "2026-10-31 09:50:00.000000", last_seen: "2026-10-31 10:28:00.000000", sample_text: null, new: false },
);
// A restart since the baseline (uptime 100 s).
const restarted = structuredClone(later);
restarted.coverage.uptime_s = 100;

const h = await open("/api/live/top", "qdigest-baselines", {
  ...baselineMocks,
  "/api/live/digest-tables": () => ({ status: 200, body: { schema_version: "digest-tables-1.0", method: "statement text", tables: [], notes: [] } }),
  "/api/live/digest-histogram": () => ({ status: 200, body: { schema_version: "digest-histogram-1.0", engine: "mysql", server_version: top.server_version,
    schema: "shop", digest: "x", available: false, reason: "No histogram in this suite.", buckets: [] } }),
}, { fallback: { status: 200, body: top } });
const { page, errors, text, state } = h;
const qd = '[data-view="query-digest"]';
const calls = (p) => state.other.filter((o) => o.path === `/api/baselines/${p}`);
const sheet = "dialog.baseline-sheet";
const rowCount = () => page.$$eval(`${qd} tbody tr[data-row-key]`, (rows) => rows.length);
const cells = (col) => page.$$eval(`${qd} tbody tr[data-row-key] td[data-col="${col}"]`, (els) => els.map((e) => e.textContent));
async function openMenu() {
  await page.click(`${qd} button[aria-label="Refresh options"]`);
  await page.waitForSelector(`${qd} .view-refresh-menu [data-action=save-baseline]`, { visible: true });
}
const confirmSheet = () => page.click(`${sheet} .sheet-buttons .action-button`);
async function refreshWith(report, statements) {
  state.fallback = { status: 200, body: report };
  await h.refresh();
  await page.waitForFunction((q, n) => new RegExp(`^${n} statement`).test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""),
    {}, qd, statements);
  await page.waitForFunction((q) => !document.querySelector(`${q} .spinner`), {}, qd);
}

try {
  await page.click('.main-nav button[data-place="digest"]');
  await page.waitForSelector(`${qd} .qd-summary`);
  assert.equal(await rowCount(), top.statements.length);
  // The two actions are the Refresh menu's items here (no Refresh every view outside Server); no toolbar button.
  assert.equal(await page.$$eval(`${qd} .view-toolbar button`, (els) => els.filter((e) => /baseline/i.test(e.textContent)).length), 0);
  await openMenu();
  assert.deepEqual(await page.$$eval(`${qd} .view-refresh-menu [role=menuitem]`, (els) => els.map((e) => [e.dataset.action, e.disabled])),
    [["save-baseline", false], ["compare-baseline", false]]);

  // Save baseline: the statements on screen; the server keeps them with literal values hidden.
  await page.click(`${qd} [data-action=save-baseline]`);
  await page.waitForSelector(sheet);
  assert.equal(await text(`${sheet} h2`), "Save a Query digest baseline");
  assert.match(await text(sheet), /literal values hidden and no samples/);
  assert.match(await page.$eval(`${sheet} input`, (e) => e.value), /^Baseline, \d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  await page.$eval(`${sheet} input`, (e) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, "Before deploy");
    e.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await confirmSheet();
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
  await page.waitForFunction((q) => /Saved the baseline ⁨Before deploy⁩\./.test(document.querySelector(`${q} .baseline-status`)?.textContent || ""), {}, qd);
  assert.equal(state.seen.at(-1).with_identity, true, "the digests ask for the server's identity");
  const sent = calls("save").at(-1).body;
  assert.deepEqual([sent.kind, sent.name, sent.report.schema_version, sent.report.engine, sent.report.order, sent.report.limit,
    sent.report.coverage.uptime_s, sent.report.statements.length], ["digest", "Before deploy", "top-1.0", "mysql", "time", 50, 1854, 30]);
  assert.ok(!Number.isNaN(Date.parse(sent.report.read_at)), "the time this computer received the reading");
  assert.equal(sent.report.coverage.oldest_first_seen, "2026-10-31 09:48:00.000000",
    "Save preserves the whole-table minimum even while counting_since is hidden");
  assert.equal(baselines[0].payload.coverage.oldest_first_seen, sent.report.coverage.oldest_first_seen);
  assert.equal(sent.connection.host, "127.0.0.1");

  // Compare with a reading 600 s later on the same run.
  await refreshWith(later, later.statements.length);
  await openMenu();
  await page.click(`${qd} [data-action=compare-baseline]`);
  await page.waitForSelector(`${sheet} .baseline-choices`);
  assert.equal(calls("list").at(-1).body.kind, "digest");
  assert.match(await text(`${sheet} .baseline-choice .hint`), /MySQL 8\.4\.\d+ · 30 statements$/);
  await confirmSheet();
  await page.waitForSelector(`${qd} [data-baseline-changes]`);
  assert.match(await text(`${qd} .baseline-bar`), /Baseline\s*Before deploy · \d{4}-\d{2}-\d{2} \d{2}:\d{2}\s*→\s*Now/);
  assert.match(await text(`${qd} [data-baseline-summary]`), /· 1 new, 2 grew by more than 50% since Before deploy,\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(await rowCount(), 3, "the new statement and the two that grew; the rest is unchanged");
  assert.deepEqual(await cells("change"), ["New", "Calls per second +1,107%", "Avg latency +428%"]);
  assert.deepEqual(await cells("rate"), ["— → 0.20/s", "0.08/s → 1.00/s", "0.03/s → 0.05/s"]);
  const avg = await cells("avg");
  assert.equal(avg[0], "— → 3 ms");
  assert.match(avg[2], /^94\.\d µs → 500\.0 µs$/);
  assert.deepEqual(await cells("calls"), ["120", "600", "30"]);
  assert.ok(await page.$(`${qd} tbody tr[data-row-key]:first-child .pill`), "the new statement carries the New pill");
  assert.match(await text(`${qd} .baseline-footnote`), /^1 listed statement is not in the baseline and not judged new\.$/);
  const rule = await h.popover("How statements are compared with the baseline");
  assert.match(rule, /New: not in the baseline, and first seen after it was read \(FIRST_SEEN, on the server's clock\)\./);
  assert.match(rule, /Grew: average latency or calls per second more than 50% above the baseline, once 10 or more calls ran since it\./);
  // The inspector of a changed statement says before → after.
  await h.select(`${qd} tbody tr[data-row-key]:nth-child(3)`);
  await page.waitForFunction(() => /Since the baseline/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.match(await h.inspector(), /BaselineBefore deployChangeAvg latency \+428%Avg latency94\.\d µs → 500\.0 µsCalls per second0\.03\/s → 0\.05\/sCalls since the baseline30/);
  const sharedHeadings = await page.$$eval('[data-statement-inspector] > .inspector-block > h4', nodes => nodes.map(n => n.textContent.trim()));
  assert.deepEqual(sharedHeadings.filter(t => t !== "Suggested index").slice(0, 6), ["Statement", "Latency", "Rows", "Flags", "Actions", "Source notes"]);
  assert.match(await h.inspector(), /Examined per execution/);
  assert.match(await h.inspector(), /Run analysis/);
  await page.keyboard.press("Escape");
  // The view's filter applies to the changes too.
  await page.$eval(`${qd} input[type=search]`, (e) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, "audit_log");
    e.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction((q) => document.querySelectorAll(`${q} tbody tr[data-row-key]`).length === 1, {}, qd);
  assert.deepEqual(await cells("change"), ["New"]);
  await page.$eval(`${qd} input[type=search]`, (e) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, "");
    e.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction((q) => document.querySelectorAll(`${q} tbody tr[data-row-key]`).length === 3, {}, qd);

  // 1440 and 720 px, light and dark: nothing scrolls sideways and the bar fits.
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const [width, height] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width, height });
      await pause(250);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), 0,
        `no sideways scroll at ${width} px (${theme})`);
      const fits = await page.$eval(`${qd} .baseline-bar`, (e) => {
        const r = e.getBoundingClientRect();
        const v = e.closest(".server-view").getBoundingClientRect();
        return r.left >= v.left - 1 && r.right <= v.right + 1;
      });
      assert.ok(fits, `the baseline bar fits at ${width} px (${theme})`);
      await page.screenshot({ path: path.join(h.shots, `query-digest-baseline-${width}-${theme}.png`) });
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await page.setViewport({ width: 1440, height: 1000 });

  // Within the first hour, a reset can replace every top-N digest. The
  // whole-table oldest FIRST_SEEN detects it even without Counting since.
  const reset = structuredClone(later);
  reset.coverage.counting_since = null;
  reset.coverage.oldest_first_seen = "2026-10-31 10:25:00.000000";
  reset.statements = reset.statements.filter((s) => s.digest === NEW);
  await refreshWith(reset, reset.statements.length);
  await page.waitForFunction((q) => /The digest table was reset since the baseline/.test(document.querySelector(`${q} .baseline-bar`)?.textContent || ""), {}, qd);
  assert.match(await text(`${qd} [data-baseline-changes]`), /Nothing new or grown since the baseline/);
  assert.match(await text(`${qd} .baseline-footnote`), /^1 listed statement is not in the baseline and not judged new\.$/);

  // After a restart nothing is judged new and calls per second are not
  // compared; average latency covers the calls since the restart.
  await refreshWith(restarted, restarted.statements.length);
  await page.waitForFunction((q) => /The server restarted since the baseline/.test(document.querySelector(`${q} .baseline-bar`)?.textContent || ""), {}, qd);
  assert.deepEqual(await cells("change"), ["Avg latency +138%"]);
  assert.deepEqual(await cells("rate"), ["Not compared"]);
  assert.match(await text(`${qd} .baseline-footnote`), /^2 listed statements are not in the baseline and not judged new\.$/);

  // Another source: the actions say why they are off; back on Statement
  // digests the comparison goes on.
  await page.select(`${qd} select[aria-label="Source"]`, "table");
  await openMenu();
  assert.match(await text(`${qd} [data-action=save-baseline]`), /Baselines keep Performance Schema statement digests: choose Statement digests\./);
  assert.equal(await page.$eval(`${qd} [data-action=compare-baseline]`, (e) => e.disabled), true);
  await page.keyboard.press("Escape");
  await page.select(`${qd} select[aria-label="Source"]`, "pfs");
  await page.waitForSelector(`${qd} [data-baseline-changes]`);

  // Stop comparing: the ranking is back.
  await page.click(`${qd} [data-action=stop-comparing]`);
  await page.waitForFunction((q) => !document.querySelector(`${q} .baseline-bar`), {}, qd);
  assert.equal(await page.$(`${qd} [data-baseline-changes]`), null);
  assert.equal(await rowCount(), restarted.statements.length);
  assert.equal(await page.$(`${qd} [data-baseline-summary]`), null);

  // Deleting the last baseline leaves nothing chosen: Compare is off.
  await openMenu();
  await page.click(`${qd} [data-action=compare-baseline]`);
  await page.waitForSelector(`${sheet} .baseline-choices`);
  assert.equal(await page.$eval(`${sheet} .sheet-buttons .action-button`, (e) => e.disabled), false);
  await page.click(`${sheet} button[aria-label="Delete baseline Before deploy"]`);
  await page.click(`${sheet} [data-action=confirm-delete-baseline]`);
  await page.waitForSelector(`${sheet} [data-baselines-empty]`);
  assert.equal(calls("delete").at(-1).body.id, "1".padStart(32, "0"));
  assert.equal(await page.$(`${sheet} .baseline-choices`), null);
  assert.equal(await page.$eval(`${sheet} .sheet-buttons .action-button`, (e) => e.disabled), true, "Compare is off with nothing to choose");
  const gets = calls("get").length;
  await page.$eval(`${sheet} .sheet-buttons .action-button`, (e) => e.click());
  await pause(200);
  assert.equal(calls("get").length, gets, "nothing is opened");
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
  assert.deepEqual(errors, []);
  console.log("query digest baselines: ok");
} finally {
  await h.close();
}
