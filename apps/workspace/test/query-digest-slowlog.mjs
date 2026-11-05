// Query digest: the time range picker and the slow log sources, on mocked
// replies recorded from throwaway lab servers (fixtures/top-mysql-8.4-lab.json
// and slow-log-table.json from MySQL 8.4.13 with log_timestamps=SYSTEM at
// UTC-03:00; slow-log-file.json is slowlog.py's reading of
// test/fixtures/slowlog/mariadb-11.4.13-slow.log).
//
// Performance Schema: the range filters the loaded rows only (no read) and
// says the numbers are totals. Slow log table: Source, the range, the user
// and Rank by are part of the read. Slow log file: the empty state, the
// native chooser (cancelled, then a file), a typed path with its error, the
// read with its progress and Stop, the inspector without live reads, and the
// text export. Screenshots at 1440 and 720 px, light and dark, with no
// horizontal page scroll (QDIGEST_SHOTS names their folder).
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const top = await load("top-mysql-8.4-lab.json");
const table = await load("slow-log-table.json");
const fileReport = await load("slow-log-file.json");
const fileInfo = { file_id: "file-id-1", name: "db1-slow.log", size: 14404, modified: "2026-10-31T12:55:00Z" };
const shotsDir = process.env.QDIGEST_SHOTS || null;
if (shotsDir) await fs.mkdir(shotsDir, { recursive: true });

const chooseQueue = [];
const heldReads = [];
let holdRead = false;
let failTable = false;
const progress = { schema_version: "slow-log-progress-1.0", phase: "reading", bytes_read: 5000, bytes_total: 14404,
  percent: 34.7, elapsed_s: 0.6, stop_requested: false };
const json = (request, body, status = 200) => request.respond({ status, contentType: "application/json", body: JSON.stringify(body) })
  .catch(() => "aborted by the page");
const h = await open("/api/live/top", "qdigest-slowlog", {
  "/api/live/slow-log": () => failTable
    ? { status: 400, body: { error: "This user cannot read mysql.slow_log. Grant it with: GRANT SELECT ON mysql.slow_log TO <user>;" } }
    : { status: 200, body: table },
  "/api/slow-log/choose": () => chooseQueue.shift() || { status: 200, body: { cancelled: true } },
  "/api/slow-log/open-path": (body) => body.path === "/var/log/missing.log"
    ? { status: 400, body: { error: "No file exists at /var/log/missing.log." } } : { status: 200, body: fileInfo },
  "/api/slow-log/read": (body, request) => {
    if (holdRead) { heldReads.push(request); return null; }
    return { status: 200, body: fileReport };
  },
  "/api/slow-log/progress": () => ({ status: 200, body: progress }),
  "/api/slow-log/stop": () => ({ status: 200, body: { stop_requested: true, message: "Stopping the read." } }),
  "/api/live/digest-tables": () => ({ status: 200, body: { schema_version: "digest-tables-1.0", method: "statement text", tables: [], notes: [] } }),
}, { fallback: { status: 200, body: top } });
const { page, errors, text, state } = h;
const qd = '[data-view="query-digest"]';
const calls = (p) => state.other.filter((o) => o.path === p);
const rowCount = () => page.$$eval(`${qd} tbody tr[data-row-key]`, (rows) => rows.length);
async function setInput(selector, value) {
  await page.$eval(selector, (e, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, v);
    e.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
}
async function openRange() {
  await page.click(`${qd} button[aria-label="Time range"]`);
  await page.waitForSelector('[role=dialog][aria-label="Time range"]');
}
const dialog = '[role=dialog][aria-label="Time range"]';
const shoot = async (name) => {
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const [w, hgt] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width: w, height: hgt });
      await pause(300);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, `${name}: horizontal scroll of ${overflow}px at ${w} px ${theme}`);
      if (w === 720) assert.ok(await toolbarRows() <= 2, `${name}: the toolbar takes ${await toolbarRows()} control rows at 720 px`);
      if (shotsDir) await page.screenshot({ path: path.join(shotsDir, `query-digest-${name}.${w}.${theme}.png`) });
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(200);
};
/** The rows the toolbar's controls take (centers 36 px or more apart). */
const toolbarRows = () => page.$eval(`${qd} .qd-filter-bar`, (bar) => {
  const centers = [];
  for (const el of bar.children) {
    if (!el.getClientRects().length || el.classList.contains("qd-toolbar-break")) continue;
    const box = el.getBoundingClientRect();
    const center = (box.top + box.bottom) / 2;
    if (!centers.some((c) => Math.abs(c - center) < 18)) centers.push(center);
  }
  return centers.length;
});

try {
  await page.click('.main-nav button[data-place="digest"]');
  await page.waitForSelector(`${qd} .qd-summary`);
  assert.deepEqual(await page.$$eval(`${qd} select[aria-label="Source"] option`, (os) => os.map((o) => o.textContent)),
    ["Statement digests", "Slow log table", "Slow log file"]);
  assert.equal(await text(`${qd} .qd-range-label`), "Any time");
  const all = await rowCount();
  assert.equal(all, top.statements.length);
  const topReads = state.seen.length;

  // ---- Performance Schema: the range filters the loaded rows, never a read.
  await openRange();
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("type")), "datetime-local", "From has focus");
  assert.match(await text(dialog), /Times are the server's clock \(FIRST_SEEN and LAST_SEEN\)/);
  assert.match(await text(dialog), /totals since counting began/);
  // From after To is refused beside the fields; the dialog stays open.
  await setInput(`${dialog} input[type=datetime-local]:first-of-type`, "2026-10-31T09:50:00");
  const inputs = await page.$$(`${dialog} input[type=datetime-local]`);
  const [fromId, toId] = await Promise.all(inputs.map((i) => i.evaluate((e) => e.id)));
  await setInput(`[id="${toId}"]`, "2026-10-31T09:40:00");
  await page.click(`${dialog} button[type=submit]`);
  assert.equal(await text(`${dialog} .form-error`), "From must be before To.");
  // A real range: statements first seen before To and last seen after From.
  const from = "2026-10-31T09:47:58", to = "2026-10-31T09:47:59";
  await setInput(`[id="${fromId}"]`, from);
  await setInput(`[id="${toId}"]`, to);
  await shoot("range-dialog");
  await page.focus(`[id="${toId}"]`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Time range"]'));
  const key = (v) => v.replace("T", " ");
  const expected = top.statements.filter((s) => s.first_seen <= key(to) + ".999999" && s.last_seen >= key(from)).length;
  assert.ok(expected > 0 && expected < all, `the range keeps some statements (${expected} of ${all})`);
  await page.waitForFunction((q, n) => document.querySelectorAll(`${q} tbody tr[data-row-key]`).length === n, {}, qd, expected);
  assert.match(await text(`${qd} .qd-range-note`), /Numbers are totals since counting began, not only this range/);
  assert.match(await text(`${qd} .summary-line`), new RegExp(`· ${expected} shown`));
  assert.equal(await page.$eval(`${qd} .qd-range-button`, (e) => e.classList.contains("is-active")), true);
  assert.equal(state.seen.length, topReads, "a range on Performance Schema never reads");
  assert.match(await h.popover("About time ranges and Performance Schema"), /digest table does not record when each call ran/);
  await shoot("pfs-range");
  // Quick ranges end at the newest last seen; Any time clears.
  await openRange();
  await page.locator(`${dialog} button ::-p-text(Last 24 hours)`).click();
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Time range"]'));
  assert.equal(await text(`${qd} .qd-range-label`), "Last 24 hours");
  assert.equal(await rowCount(), all);
  await openRange();
  await setInput(`${dialog} input[type=datetime-local]`, "2026-10-01T00:00:00");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Time range"]'));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Time range", "Escape returns focus");
  await openRange();
  assert.notEqual(await page.$eval(`${dialog} input[type=datetime-local]`, (e) => e.value), "2026-10-01T00:00:00",
    "an edit left unapplied is dropped when the dialog opens again");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Time range"]'));
  await openRange();
  await page.locator(`${dialog} button ::-p-text(Any time)`).click();
  await page.waitForFunction((q) => document.querySelector(`${q} .qd-range-label`)?.textContent === "Any time", {}, qd);
  assert.equal(await page.$(`${qd} .qd-range-note`), null);

  // ---- Slow log table: Source, range, user and ranking are read on the server.
  await page.select(`${qd} select[aria-label="Source"]`, "table");
  await page.waitForFunction((q) => /^mysql\.slow_log · /.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""), {}, qd);
  let reads = calls("/api/live/slow-log");
  assert.equal(reads.length, 1);
  assert.deepEqual({ ...reads[0].body, connection: undefined }, { connection: undefined, order: "total", limit: 50, from: null, to: null, user: null });
  // The report says the range the server applied (the fixture was read from 09:00 on).
  assert.match(await text(`${qd} .summary-line .summary-text`), /executions · From .+/);
  assert.deepEqual(await page.$$eval(`${qd} select[aria-label="Rank by"] option`, (os) => os.map((o) => o.textContent)),
    ["Total time", "Calls", "Average time", "Slowest call", "P95", "Lock time", "Rows examined", "Rows sent"]);
  assert.ok(await page.$(`${qd} th[data-col="p95"]`), "P95 column");
  const users = await page.$$eval(`${qd} select[aria-label="User"] option`, (os) => os.map((o) => o.textContent));
  assert.equal(users[0], "All users");
  assert.equal(users.length, table.users.length + 1);
  // Last hour ends at the server's NOW() the report carries.
  await openRange();
  assert.match(await text(dialog), new RegExp(`Times are ${table.clock.label.replace(/[()+]/g, "\\$&")}`));
  await page.locator(`${dialog} button ::-p-text(Last hour)`).click();
  for (let i = 0; i < 50 && calls("/api/live/slow-log").length < 2; i++) await pause(50);
  reads = calls("/api/live/slow-log");
  const anchor = table.anchor.slice(0, 19).replace(" ", "T");
  assert.equal(reads[1].body.to, anchor);
  const hourBefore = new Date(Date.parse(anchor + "Z") - 3600_000).toISOString().slice(0, 19);
  assert.equal(reads[1].body.from, hourBefore);
  await page.waitForFunction((q) => !document.querySelector(`${q} .spinner`), {}, qd);
  await page.select(`${qd} select[aria-label="User"]`, table.users[0].name);
  for (let i = 0; i < 50 && calls("/api/live/slow-log").length < 3; i++) await pause(50);
  assert.equal(calls("/api/live/slow-log")[2].body.user, table.users[0].name);
  await page.select(`${qd} select[aria-label="Rank by"]`, "avg");
  for (let i = 0; i < 50 && calls("/api/live/slow-log").length < 4; i++) await pause(50);
  assert.equal(calls("/api/live/slow-log")[3].body.order, "avg");
  await page.waitForFunction((q) => !document.querySelector(`${q} .spinner`), {}, qd);
  await shoot("table-list");
  // The inspector: latency from the report (no histogram read), who ran it, the tables read on the connection.
  await h.select(`${qd} tbody tr[data-row-key]:first-child`);
  const inspector = await h.inspector();
  assert.match(inspector, /Latency/);
  assert.match(inspector, /Who ran it/);
  assert.match(inspector, /Sample \(the slowest call\)/);
  assert.match(inspector, /Fingerprint/);
  assert.ok(await page.$(".view-inspector .qd-hist svg"), "the histogram is drawn from the report");
  assert.equal(calls("/api/live/digest-histogram").length, 0, "no histogram read for a slow log");
  await shoot("table");
  await page.keyboard.press("Escape");

  // A failed Slow log table read never leaves Statement digests' rows on screen.
  failTable = true;
  await page.select(`${qd} select[aria-label="Source"]`, "pfs");
  await page.waitForFunction((q) => !/^mysql\.slow_log/.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || "x")
    && document.querySelectorAll(`${q} tbody tr[data-row-key]`).length > 0, {}, qd);
  await page.select(`${qd} select[aria-label="Source"]`, "table");
  await page.waitForFunction((q) => /GRANT SELECT ON mysql\.slow_log/.test(document.querySelector(q)?.textContent || ""), {}, qd);
  assert.equal(await rowCount(), 0, "the Performance Schema statements are not shown as the table's");
  failTable = false;

  // ---- Slow log file: nothing is read until a file is chosen.
  const topBefore = state.seen.length;
  await page.select(`${qd} select[aria-label="Source"]`, "file");
  await page.waitForSelector(`${qd} .qd-file-empty`);
  assert.match(await text(`${qd} .qd-file-empty`), /Open a slow query log/);
  assert.equal(await page.$eval(`${qd} button[aria-label="Time range"]`, (e) => e.disabled), true);
  assert.equal(await page.$eval(`${qd} button[data-action=refresh]`, (e) => e.disabled), true);
  await shoot("file-empty");
  // A typed path that does not exist: the error under the field.
  await h.button("Type a path instead");
  await page.waitForSelector(`${qd} .qd-path input`);
  assert.equal(await page.evaluate(() => document.activeElement?.closest(".qd-path") !== null), true);
  await page.type(`${qd} .qd-path input`, "/var/log/missing.log");
  await page.keyboard.press("Enter");
  await page.waitForFunction((q) => document.querySelector(`${q} .qd-path .form-error`)?.textContent === "No file exists at /var/log/missing.log.", {}, qd);
  // The chooser cancelled, then a file chosen: its read starts and reports progress.
  await h.button("Choose file…");
  await page.waitForFunction((q) => /No file chosen\./.test(document.querySelector(`${q} .qd-file-line`)?.textContent || ""), {}, qd);
  chooseQueue.push({ status: 200, body: fileInfo });
  holdRead = true;
  await h.button("Choose file…");
  for (let i = 0; i < 60 && !heldReads.length; i++) await pause(50);
  assert.equal(heldReads.length, 1);
  const read = calls("/api/slow-log/read")[0].body;
  assert.equal(read.file_id, "file-id-1");
  assert.match(read.progress_id, /^[0-9a-f]{24}$/);
  // Rank by stays as chosen for the table; the range and the user start over with a new source.
  assert.deepEqual([read.order, read.limit, read.from, read.to, read.user], ["avg", 50, null, null, null]);
  await page.waitForSelector(`${qd} .qd-file-progress progress`);
  assert.match(await text(`${qd} .qd-file-progress`), /35% · 4\.9 KiB of 14\.1 KiB/);
  assert.match(await text(`${qd} .view-status`), /Reading db1-slow\.log…/);
  holdRead = false;
  await json(heldReads.shift(), fileReport);
  await page.waitForFunction((q) => /^Slow log file: db1-slow\.log · /.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""), {}, qd);
  assert.equal(await rowCount(), fileReport.statements.length);
  assert.equal(state.seen.length, topBefore, "a file source sends nothing to the server");
  await shoot("file-list");
  // The inspector: no Tables section (no server), the sample's origin, the latency histogram.
  await h.select(`${qd} tbody tr[data-row-key]:first-child`);
  const fileInspector = await h.inspector();
  assert.doesNotMatch(fileInspector, /Found in the statement text/);
  assert.match(fileInspector, /Sample \(the slowest call\)/);
  assert.match(await text(".view-inspector .qd-sample-meta"), /line \d/);
  assert.equal(await page.$eval('.view-inspector button[title*="comes from a file"]', (e) => e.disabled), false);
  assert.equal(calls("/api/live/digest-tables").length, 1, "only the table source's statement read its tables");
  await shoot("file");
  await page.keyboard.press("Escape");
  // The text export names the file and its clock.
  await page.click(`${qd} button[aria-label="Export query digest"]`);
  await page.click('.export-menu-items button[data-format="text"]');
  await page.waitForSelector('dialog[open] pre[aria-label="text report"]');
  const exported = await text('dialog[open] pre[aria-label="text report"]');
  assert.match(exported, /^# Source: Slow log file: db1-slow\.log \(slow log file\)$/m);
  assert.match(exported, /^# Clock: Times are the server's local time, as the log writes them/m);
  assert.match(exported, /^# Fingerprint: [0-9a-f]{16}$/m);
  assert.doesNotMatch(exported, /^# Connection:/m, "a file is not a connection's report");
  await page.keyboard.press("Escape");
  // Today ends at the file's last entry; the read carries the range.
  await openRange();
  assert.match(await text(dialog), /Server local time \(UTC\+05:30\)/);
  await page.locator(`${dialog} button ::-p-text(Today)`).click();
  for (let i = 0; i < 50 && calls("/api/slow-log/read").length < 2; i++) await pause(50);
  const today = calls("/api/slow-log/read")[1].body;
  assert.deepEqual([today.from, today.to], [`${fileReport.anchor.slice(0, 10)}T00:00:00`, fileReport.anchor.slice(0, 19).replace(" ", "T")]);
  // Stop: a slow read can be stopped; the server is told and the view keeps its report.
  await page.waitForFunction((q) => !document.querySelector(`${q} .spinner`), {}, qd);
  holdRead = true;
  await h.refresh();
  await page.waitForSelector(`${qd} .view-status-stop`, { timeout: 8000 });
  await page.click(`${qd} .view-status-stop`);
  for (let i = 0; i < 40 && !calls("/api/slow-log/stop").length; i++) await pause(50);
  assert.equal(calls("/api/slow-log/stop").length, 1);
  assert.equal(calls("/api/slow-log/stop")[0].body.progress_id, calls("/api/slow-log/read").at(-1).body.progress_id);
  await page.waitForFunction((q) => /Stopped reading the file/.test(document.querySelector(`${q}`)?.textContent || ""), {}, qd);
  assert.equal(await rowCount(), fileReport.statements.length, "the view keeps what it showed before");
  holdRead = false;
  heldReads.splice(0).forEach((r) => json(r, fileReport));
  await page.waitForFunction((q) => !document.querySelector(`${q} .spinner`), {}, qd);
  // A Statement digests read that lands after the user went back to the file
  // never clears the file's selection or its inspector.
  const seenBefore = state.seen.length;
  state.delay = 2500;
  await page.select(`${qd} select[aria-label="Source"]`, "pfs");
  for (let i = 0; i < 60 && state.seen.length === seenBefore; i++) await pause(50);
  assert.equal(state.seen.length, seenBefore + 1, "Statement digests were read again");
  state.delay = 0;
  await page.select(`${qd} select[aria-label="Source"]`, "file");
  await page.waitForFunction((q) => /^Slow log file: /.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""), {}, qd);
  await h.select(`${qd} tbody tr[data-row-key]:first-child`);
  await h.inspector();
  await pause(3000);
  assert.ok(await page.$(".view-inspector"), "the late reply left the file's inspector open");
  assert.ok(await page.$(`${qd} tbody tr[data-row-key][aria-current="true"]`), "and its row selected");
  assert.deepEqual(errors, []);
  console.log("query-digest-slowlog: ok");
} finally {
  await h.close();
}
