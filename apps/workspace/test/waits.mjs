// Waits view of the Server place: mocked /api/live/waits with waits-1.0
// reports replayed from live MySQL 8.4 and MariaDB captures (fixtures/).
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause, SERVER_GROUPS } from "./perf-harness.mjs";

const mysql = await load("waits-mysql-8.4.json");
const off = await load("waits-mariadb-13.0-default.json");
const mariadb = await load("waits-mariadb-11.4.json");
const failure = { status: 400, body: { error: "Could not read wait events: ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:3306'" } };

const h = await open("/api/live/waits", "waits");
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=waits]";
const rows = () => page.$$eval(`${view} [aria-label="Wait events over the window"] tbody tr`, (els) => els.map((e) => e.dataset.event));
try {
  await h.server();
  // Waits sits in the Workload group of the sub-navigation.
  assert.deepEqual(await texts(".source-list .source-group:nth-child(2) .source-label"), SERVER_GROUPS.Workload);
  await h.view("waits");
  assert.match(await text(`${view} .view-empty`), /Where server threads spent their waiting time/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");
  assert.deepEqual(await page.$$eval(`${view} .perf-window option`, (els) => els.map((e) => e.value)), ["2", "5", "10", "15", "30", "60"]);
  assert.equal(await page.$eval(`${view} .perf-window select`, (e) => e.value), "10");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0", "auto-refresh is off");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.disabled), true);

  // Keyboard: arrows move along the sub-navigation (selection follows), Home/End reach the ends.
  await page.focus("#server-nav-waits");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-io");
  await page.waitForSelector("#server-nav-io[aria-current=page]");
  await page.keyboard.press("End");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-accounts");
  await page.keyboard.press("Home");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-health");
  await h.view("waits");

  // Load with a 5 s window: loading state, then the report.
  await page.select(`${view} .perf-window select`, "5");
  state.delay = 400;
  await button("Load wait events", view + " ");
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Sampling wait events for 5 s/);
  await page.waitForSelector(`${view} .data-table`);
  await h.idle(view);
  state.delay = 0;
  assert.equal(state.seen.length, 1);
  assert.equal(state.seen[0].interval, 5);
  assert.equal(state.seen[0].connection.password || "", "", "no password was typed");
  assert.match(await text(`${view} .fact-strip`), /MySQL 8\.4\.11/);
  assert.match(await text(`${view} .summary-line`), /events waited over 5\.\d s · 6 classes not instrumented/);
  const events = await rows();
  assert.equal(events[0], "wait/lock/metadata/sql/mdl", "the metadata lock wait leads");
  const mdl = `${view} tr[data-event="wait/lock/metadata/sql/mdl"]`;
  assert.match(await text(mdl), /Metadata locks/);
  assert.match(await text(`${view} thead th[data-col=wait]`), /Total wait \(ms\)/);
  assert.match(await text(`${view} thead th[data-col=rate]`), /ms\/s/);
  const waitMs = mysql.window.events.find((e) => e.event === "wait/lock/metadata/sql/mdl").wait_ms;
  assert.equal(await page.$eval(`${mdl} td[data-col=wait]`, (e) => Number((e.querySelector("abbr")?.title || e.textContent).replace(/,/g, ""))), waitMs);
  assert.ok(await page.$(`${view} .view-toolbar [aria-label="Time range"]`), "time range is a toolbar control");
  // Sortable: Waits, largest first; then back.
  await page.click(`${view} th:nth-child(2) button`);
  assert.equal(await page.$eval(`${view} th:nth-child(2)`, (e) => e.getAttribute("aria-sort")), "descending");
  // Large counts are compact ("10.4M"); the exact value is the tooltip.
  const byCount = await page.$$eval(`${view} tbody tr td:nth-child(2)`, (els) =>
    els.map((e) => Number((e.querySelector("abbr")?.title || e.textContent).replace(/[^\d.]/g, ""))));
  assert.deepEqual(byCount, [...byCount].sort((a, b) => b - a));
  // Nothing adds the classes up or shows a share of a total; the reason is behind ⓘ.
  const panel = await text(view);
  assert.doesNotMatch(panel, /% of|percent|Total of all/i);
  assert.match(await h.popover("About Waits"), /never added up or shown as a share of a total/);
  // A lock wait opens Locks from the inspector; file I/O opens I/O.
  await h.select(mdl);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "wait/lock/metadata/sql/mdl");
  assert.match(await h.inspector(), /Over the window.*Waits.*Total wait.*Per second/s);
  await page.locator(".view-inspector button ::-p-text(Open Locks)").click();
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-locks");
  await h.view("waits");
  await page.waitForSelector(`${view} .data-table`);
  assert.equal(state.seen.length, 1, "switching views keeps the report");
  assert.equal(await page.$eval(`${mdl}`, (e) => e.getAttribute("aria-current")), "true", "and its selection");
  // Classes, one by one; since server start with the longest single wait.
  await page.click(`${view} .segmented button[data-value=classes]`);
  assert.ok((await page.$$(`${view} [aria-label="Wait classes over the window"] tbody tr`)).length > 0);
  await page.click(`${view} .segmented button[data-value=since]`);
  assert.match(await text(`${view} [aria-label="Wait classes since server start"] thead`), /Events/);
  // A class in the inspector: the range on screen first, each range in its own labelled section.
  await h.select(`${view} [aria-label="Wait classes since server start"] tr[data-class="wait/io/table"]`);
  assert.deepEqual(await page.$$eval(".view-inspector .inspector-block h4", (els) => els.map((e) => e.textContent)),
    ["What it is", "Since server start", "Over the window"]);
  assert.match(await h.inspector(), /Since server start\s*Events\s*1\s*Waits\s*12,324,105.*Over the window\s*Events\s*1\s*Waits\s*10,407,087/s);
  await page.click(`${view} .segmented button[data-value=events]`);
  const sinceTable = `${view} table[aria-label="Wait events since server start"]`;
  assert.equal(await page.$(`${sinceTable} th[data-col=max]`), null, "longest wait is a detail column beside the inspector");
  await page.click(`${sinceTable} .table-columns-button`);
  await page.click('.popover input[data-col=max]');
  await page.keyboard.press("Escape");
  assert.match(await text(`${sinceTable} th[data-col=max]`), /Longest \(ms\)/);
  await page.click(`${view} .segmented button[data-value=window]`);

  // Not instrumented: collapsed; counts, the enable statements to copy, and the caveat in the inspector.
  assert.equal(await page.$eval(`${view} .waits-off`, (e) => e.open), false);
  await page.click(`${view} .waits-off summary`);
  assert.deepEqual(await page.$$eval(`${view} .waits-off li[data-class]`, (els) => els.map((e) => e.dataset.class)),
    ["wait/synch/mutex", "wait/synch/rwlock", "wait/synch/sxlock", "wait/synch/prlock", "wait/synch/cond", "wait/io/socket"]);
  const mutex = `${view} .waits-off li[data-class="wait/synch/mutex"]`;
  assert.match(await text(mutex), /0 of 236 on/);
  await page.focus(`${mutex} button`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => /wait\/synch\/mutex/.test(document.querySelector(".view-inspector .inspector-meta")?.textContent || ""));
  assert.equal(await text(".view-inspector .perf-sql pre"),
    "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', TIMED = 'YES' WHERE NAME LIKE 'wait/synch/mutex/%';");
  assert.match(await h.inspector(), /performance-schema-instrument = 'wait\/synch\/mutex\/%=ON'/);
  assert.match(await h.inspector(), /lasts until the server restarts/);
  await page.keyboard.press("Escape");
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "waits.png") });

  // Automatic refresh: opt-in, on the chosen period, while the view is visible.
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
  await pause(400);
  assert.equal(state.seen.length, stopped, "Off stops automatic refresh");

  // Error: an alert, the last report stays.
  state.queue.push(failure);
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.match(await text(`${view} [role=alert]`), /ERROR 2003/);
  assert.ok(await page.$(`${view} .data-table`));

  // Performance Schema off (MariaDB default): one sentence and one action, no tables.
  state.queue.push({ status: 200, body: off });
  await h.refresh();
  await page.waitForSelector(`${view} .gate`);
  await h.idle(view);
  assert.match(await text(`${view} .gate`), /performance_schema=ON under \[mysqld\].*restart/);
  assert.ok(await page.$(`${view} .gate [data-instruments-row=ps]`), "the gate links to the Instruments row");
  assert.equal(await page.$(`${view} .data-table`), null);
  assert.match(await text(`${view} .fact-strip`), /MariaDB 13\.0\.2/);

  // MariaDB with Performance Schema on: its MDL note is behind the notes button.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 11\.4/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  assert.match(await h.notes(), /MariaDB turns the metadata lock instrument/);

  // Narrow layout: tables scroll inside themselves.
  await h.narrow("waits-390.png");

  // Session restore keeps the view and the window, never the report.
  await page.select(`${view} .perf-window select`, "30");
  const saved = await h.savedSlow((s) => s.kind === "waits" && s.waits_interval === 30);
  assert.equal(JSON.stringify(saved.state).includes("waits-1.0"), false, "reports are not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-waits[aria-current=page]");
  assert.equal(await page.$eval(`${view} .perf-window select`, (e) => e.value), "30");
  assert.match(await text(`${view} .view-empty`), /Load wait events/);

  assert.deepEqual(h.errors, []);
  console.log("waits view: ok");
} finally {
  await h.close();
}
