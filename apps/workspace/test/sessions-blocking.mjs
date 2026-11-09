// Synthetic chain plus no-wait reports. The harness starts an isolated
// workspace server and intercepts every live read; no real database is used.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open } from "./perf-harness.mjs";

const chain = await load("sessions-blocking-chain.json");
const calm = await load("sessions-no-waits.json");
const attack = "SELECT '</script><img src=x onerror=alert(1)> אבג'";
const h = await open("/api/live/sessions", "sessions-blocking", {
  "/api/live/sessions/history": body => ({ status: 200, body: {
    id: body.id, available: body.id !== 102, limit: 10,
    reason: body.id === 102 ? "Recent statements need events_statements_history enabled." : null,
    statements: body.id === 102 ? [] : [{ event_id: "92", sql: attack, duration_ms: 1234,
      rows_examined: 5, rows_sent: 0, errors: 1, errno: 1205, message: "Lock wait timeout exceeded" }],
  } }),
  "/api/audit/declined": { status: 200, body: { audit_id: "declined", status: "declined" } },
}, { list: "docked" });
const { page, state } = h;
state.fallback = { status: 200, body: chain };
const view = '.server-view[data-view="sessions"]';
const row = id => `${view} tr[data-session="${id}"]`;
const historyReads = () => state.other.filter(x => x.path === "/api/live/sessions/history");
const diagnostics = [];
const failedRequests = [];
const httpErrors = [];
page.on("console", m => { if (["error", "warn"].includes(m.type())) diagnostics.push(m.text()); });
page.on("requestfailed", r => failedRequests.push({ path: new URL(r.url()).pathname, error: r.failure()?.errorText }));
page.on("response", r => { if (r.status() >= 400) httpErrors.push({ path: new URL(r.url()).pathname, status: r.status() }); });
try {
  await h.workload();
  await h.view("sessions");
  await page.waitForSelector(row(101));
  await page.waitForSelector(`${view} th[data-col="lock"]`);
  assert.equal(await page.$(`${view} th[data-col="blocked_by"], ${view} th[data-col="blocks"]`), null);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForFunction(v => {
    const table = document.querySelector(`${v} .data-table-wrap`);
    return table && table.scrollWidth === table.clientWidth;
  }, {}, view);
  assert.equal(await page.$eval(`${view} .data-table-wrap`, e => e.scrollWidth - e.clientWidth), 0,
    "no sideways scroll at 1440 px with the chain fixture");
  assert.equal(historyReads().length, 0, "history waits until the inspector opens");
  await page.click(`${view} .segmented [data-value="locked"]`);
  await page.waitForFunction(v => document.querySelector(`${v} tbody tr[data-session]`)?.dataset.session === "101", {}, view);
  assert.deepEqual(await page.$$eval(`${view} tbody tr[data-session]`, rows => rows.map(r => r.dataset.session)), ["101", "102", "103"]);
  assert.doesNotMatch(await h.text(`${row(101)} td[data-col="status"]`), /Root blocker/);
  assert.match(await h.text(`${row(101)} td[data-col="lock"] .status-pill`), /Root blocker/);
  assert.match(await h.text(`${row(101)} td[data-col="lock"]`), /^Root blocker · Blocks 2/);
  assert.equal(await page.$eval(`${row(101)} td[data-col="lock"] > span`, e => e.title),
    "Blocks 2 sessions; longest wait 22 s");
  assert.match(await h.text(`${row(102)} td[data-col="lock"]`), /^Waits on 101 · blocks 1 \(8 s\)/);
  assert.match(await h.text(`${row(103)} td[data-col="lock"]`), /^Waits on 102 \(22 s\)/);
  assert.equal(await page.$(`${view} td[data-col="lock"] button, ${view} td[data-col="lock"] a`), null,
    "table blocker IDs remain text");
  // Sorting the existing cell preserves the Locked order.
  await page.click(`${view} th[data-col="lock"] .sort-button`);
  assert.deepEqual(await page.$$eval(`${view} tbody tr[data-session]`, rows => rows.map(r => r.dataset.session)),
    ["101", "102", "103"]);

  await h.select(row(101));
  await page.waitForSelector('.view-inspector .session-history');
  assert.match(await h.inspector(), /Blocks 2 sessions \(longest 22 s\)/);
  assert.match(await h.inspector(), /1\.23 s/);
  assert.equal(await h.text('.session-history bdi'), attack);
  assert.equal(await page.$('.session-history img, .session-history script'), null, "server SQL remains text");
  assert.equal(historyReads().length, 1);
  assert.equal(historyReads()[0].body.id, 101);
  assert.equal(historyReads()[0].body.connection.host, state.seen[0].connection.host);
  await page.click('.view-inspector [aria-label="Kill: connection 101"]');
  await page.waitForSelector('dialog.kill-dialog[open]');
  assert.equal(await page.$eval('dialog.kill-dialog input[value="query"]', e => e.checked), true);
  assert.match(await h.text('dialog.kill-dialog .kill-blocking'), /Blocks 2 sessions now \(longest wait 22 s\)\./);
  assert.match(await h.text('dialog.kill-dialog .kill-blocking'), /Stopping the statement leaves the connection open; an open transaction keeps its locks unless the session runs in autocommit mode, so sessions may keep waiting\./);
  assert.doesNotMatch(await h.text('dialog.kill-dialog .kill-blocking'), /Ending the connection|Ends the wait/);
  await page.click('dialog.kill-dialog input[value="connection"]');
  assert.match(await h.text('dialog.kill-dialog .kill-blocking'), /Ending the connection rolls back its open transaction and releases its locks when the rollback finishes \(a prepared XA transaction stays prepared\); another blocker can keep sessions waiting\./);
  assert.doesNotMatch(await h.text('dialog.kill-dialog .kill-blocking'), /Stopping the statement|Ends the wait/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('dialog.kill-dialog[open]'));
  assert.equal(state.other.filter(x => x.path === "/api/live/kill").length, 0);

  const wait = page.waitForResponse(r => new URL(r.url()).pathname === "/api/live/sessions/history");
  await page.click('.view-inspector .session-block-links button');
  assert.equal((await wait).status(), 200);
  await page.waitForFunction(() => /Connection 102/.test(document.querySelector('.view-inspector')?.textContent || ""));
  await page.waitForSelector('.view-inspector .gate');
  assert.match(await h.text('.view-inspector .gate'), /events_statements_history.*Open Instruments/s);
  assert.equal(historyReads().length, 2);

  for (const width of [1440, 720]) {
    await page.setViewport({ width, height: 900 });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await page.evaluate(() => document.fonts.ready);
      await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
      await page.screenshot({ path: path.join(h.shots, `sessions-blocking-${width}-${scheme}.png`), fullPage: true });
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.keyboard.press("Escape");

  const filtered = structuredClone(chain);
  filtered.sessions = filtered.sessions.filter(s => s.id !== 102);
  filtered.filters.min_seconds = 10;
  filtered.matched = filtered.sessions.length;
  state.fallback = { status: 200, body: filtered };
  const minimum = `${view} input[aria-label="Running at least (seconds)"]`;
  await page.$eval(minimum, input => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, "10");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await h.savedSlow(s => s.min_seconds === 10);
  await h.refresh();
  await page.waitForFunction(v => !document.querySelector(`${v} tr[data-session="102"]`), {}, view);
  assert.equal(state.seen.at(-1).min_seconds, 10);
  await h.select(row(101));
  await page.waitForSelector('.view-inspector .session-history');
  assert.match(await h.inspector(), /Blocks 2 sessions/);
  assert.match(await h.text('.session-block-links'), /Connection 102 \(not in this list\)/);
  assert.deepEqual(await page.$$eval('.session-block-links button', buttons => buttons.map(b => b.textContent.trim())),
    ["Connection 103"], "the inspector follows the chain through hidden session 102");
  assert.equal(await page.$('.session-block-links span button'), null);
  await page.keyboard.press("Escape");

  // A waiter with several direct blockers keeps the same one-line cell.
  const shared = structuredClone(filtered);
  shared.sessions.find(s => s.id === 103).blocked_by = [101, 102];
  Object.assign(shared.sessions.find(s => s.id === 101), { blocks: 2, blocks_ids: [102, 103] });
  shared.blocking_edges.push({ waiter: 103, blocker: 101 });
  state.fallback = { status: 200, body: shared };
  await h.refresh();
  await page.waitForFunction(v => /Waits on 101 \+1/.test(document.querySelector(`${v} tr[data-session="103"] td[data-col="lock"]`)?.textContent || ""), {}, view);

  state.fallback = { status: 200, body: calm };
  await page.click(`${view} .segmented [data-value="all"]`);
  await page.waitForFunction(v => !/Root blocker/.test(document.querySelector(`${v} tbody`)?.textContent || ""), {}, view);
  assert.equal(await page.$(`${view} th[data-col="blocked_by"]`), null);
  assert.doesNotMatch(await h.text(`${view} tbody`), /Root blocker/);
  assert.equal(historyReads().length, 3, "closing details does not fetch another history");
  // Top statements and the schema picker, mounted beside Processlist in the
  // Workload place, read on mount; the harness refuses unmocked live reads
  // with 400, so only those refusals (and their console lines) are expected.
  const sibling = new Set(["/api/live/top", "/api/live/schemas"]);
  let tolerated = httpErrors.filter(e => sibling.has(e.path) && e.status === 400).length;
  const unexpected = diagnostics.filter(m => !(tolerated > 0 && /status of 400 \(Bad Request\)/.test(m) && tolerated--));
  assert.deepEqual(h.errors, []);
  assert.deepEqual(unexpected, []);
  assert.deepEqual(failedRequests.filter(e => !(e.path === "/api/live/kill/prepare" && e.error === "net::ERR_ABORTED")), []);
  assert.deepEqual(httpErrors.filter(e => !(sibling.has(e.path) && e.status === 400)), []);
  console.log("sessions blocking and recent statements: ok");
} finally {
  await h.close();
}
