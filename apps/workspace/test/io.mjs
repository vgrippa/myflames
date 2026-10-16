// I/O view of the Server place: mocked /api/live/io with io-1.0 reports
// replayed from live captures (fixtures/). No automatic refresh; a table's
// inspector links to Index health and Top statements for its schema.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open } from "./perf-harness.mjs";

const mysql = await load("io-mysql-8.4.json");
const off = await load("io-mariadb-13.0-default.json");

const h = await open("/api/live/io", "io");
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=io]";
try {
  await h.server();
  await h.view("io");
  assert.match(await text(`${view} .view-empty`), /tables and files with the most reading and writing/);
  assert.equal(await page.$(`${view} .auto-refresh`), null, "no automatic refresh");
  assert.equal(state.seen.length, 0);
  assert.match(await text(`${view} .perf-schema`), /Schema/);
  assert.equal(await page.$eval("#io-schema", (e) => e.placeholder), "All schemas");

  // Without a schema: no schema sent.
  state.delay = 300;
  await button("Load I/O", view + " ");
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Sampling I\/O for 10 s/);
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  state.delay = 0;
  assert.deepEqual([state.seen[0].interval, state.seen[0].schema], [10, undefined]);
  assert.match(await text(`${view} .summary-line`), /s window · tables from Performance Schema/);
  assert.ok(await page.$(`${view} .view-toolbar [aria-label="Time range"]`), "range lives in the toolbar");
  assert.equal(await page.$eval(`${view} .io-global-rates`, (e) => e.open), false, "global rates are disclosed on demand");
  await page.click(`${view} .io-global-rates > summary`);
  // Server-wide rates: compact tiles; what each measures is in the inspector.
  assert.deepEqual(await page.$$eval(`${view} .tile-item`, (els) => els.map((e) => e.dataset.metric)),
    ["data_read", "data_written", "data_reads", "data_writes", "data_fsyncs", "redo_written", "pages_read"]);
  assert.match(await text(`${view} .tile-item[data-metric="redo_written"]`), /\/s/);
  const tileHeight = await page.$eval(`${view} .tile-item[data-metric="redo_written"] .metric-tile`, (e) => e.getBoundingClientRect().height);
  assert.ok(tileHeight <= 120, `tiles stay compact (${tileHeight}px)`);
  await h.select(`${view} .tile-item[data-metric="data_written"]`);
  await page.waitForFunction(() => /includes redo log writes/.test(document.querySelector(".view-inspector")?.textContent || ""));
  // One table at a time: tables, files, file kinds; over the window or since start.
  assert.deepEqual(await page.$$eval(`${view} .segmented button`, (els) => els.map((e) => e.dataset.value)),
    ["window", "since", "tables", "files", "kinds"]);
  const tables = await page.$$eval(`${view} [aria-label="Tables over the window"] tbody tr`, (els) => els.map((e) => e.dataset.table));
  assert.deepEqual(tables, ["orders", "customers", "odd$name"]);
  const orders = `${view} tr[data-table="orders"]`;
  // Rows read and written are compact ("12.6M") when large.
  assert.match(await text(`${orders} td[data-col=read]`), /M/);
  assert.match(await text(`${view} th[data-col=total]`), /Total time \(ms\)/);
  assert.equal(await page.$(`${view} th[data-col=readms]`), null, "read timing is available through Columns or the inspector");
  assert.match(await h.inspector(), /What it measures/);
  // Files: kinds labeled; temporary files are in the kinds list.
  await page.click(`${view} .segmented button[data-value=files]`);
  const kinds = await page.$$eval(`${view} [aria-label="Files over the window"] tbody tr`, (els) => els.map((e) => e.dataset.kind));
  assert.ok(kinds.includes("redo"));
  await page.click(`${view} .segmented button[data-value=kinds]`);
  assert.match(await text(`${view} [aria-label="File kinds over the window"]`), /wait\/io\/file\/sql\/io_cache/);
  await h.select(`${view} tr[data-event="wait/io/file/sql/io_cache"]`);
  await page.waitForFunction(() => /temporary files that were closed/.test(document.querySelector(".view-inspector")?.textContent || ""));
  // Since server start: tables and files.
  await page.click(`${view} .segmented button[data-value=since]`);
  assert.ok(await page.$(`${view} [aria-label="File kinds since server start"]`));
  await page.click(`${view} .segmented button[data-value=files]`);
  assert.ok(await page.$(`${view} [aria-label="Files since server start"]`));
  await page.click(`${view} .segmented button[data-value=tables]`);
  assert.ok(await page.$(`${view} [aria-label="Tables since server start"]`));
  await page.click(`${view} .segmented button[data-value=window]`);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "io.png") });

  // Links in the inspector: Index health loads that schema; Top statements too.
  await h.select(orders);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "mfshop.orders");
  await page.locator(".view-inspector button ::-p-text(Index health)").click();
  await page.waitForSelector("#server-nav-indexes[aria-current=page]");
  assert.equal(await page.$eval(".server-view .perf-picker-summary", (e) => e.textContent), "mfshop");
  for (let i = 0; i < 50 && !state.other.some((o) => o.path === "/api/live/indexes"); i++) await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(state.other.find((o) => o.path === "/api/live/indexes").body.schemas, ["mfshop"]);
  await h.view("io");
  await h.select(orders);
  await page.locator(".view-inspector button ::-p-text(Top statements)").click();
  await page.waitForSelector("#server-nav-top[aria-current=page]");
  assert.equal(await page.$eval("#top-schema", (e) => e.value), "mfshop");
  await h.view("io");
  // The shared schema now limits the next read.
  assert.equal(await page.$eval("#io-schema", (e) => e.value), "mfshop");
  await page.select(`${view} .perf-window select`, "5");
  await h.refresh();
  await h.idle(view);
  assert.deepEqual([state.seen.at(-1).interval, state.seen.at(-1).schema], [5, "mfshop"]);

  // A gated family is absent in both snapshots; the opt-in keeps the scope
  // and sampling interval, and never implies that the time budget is lifted.
  const gated = structuredClone(mysql);
  gated.collection = { instance_limit: 2000, budget_s: 30, all_instances: false,
    counts: { tables: 2001, files: 12 }, omitted: ["tables"], complete: false };
  gated.window.tables = [];
  gated.since_start.tables = [];
  state.queue.push({ status: 200, body: gated });
  await h.refresh();
  await page.waitForSelector(`${view} .gate`);
  await h.idle(view);
  assert.equal(await text(`${view} .gate p`), "tables omitted by the 2,000 instance limit or 30 s time budget.");
  assert.equal(await page.$(`${view} tr[data-table]`), null);
  assert.match(await text(`${view} .table-empty`), /Table I\/O was not collected/);
  assert.ok(await page.$(`${view} .io-global-rates`), "server counters remain available");
  await h.narrow("io-gate-390.png");
  await page.setViewport({ width: 1440, height: 1000 });
  state.queue.push({ status: 200, body: { ...gated, collection: { ...gated.collection, all_instances: true } } });
  await button("Read all anyway", view + " ");
  await h.idle(view);
  assert.deepEqual([state.seen.at(-1).all_instances, state.seen.at(-1).schema, state.seen.at(-1).interval], [true, "mfshop", 5]);
  assert.equal(await text(`${view} .gate p`), "tables omitted by the 30 s time budget.");
  assert.equal((await texts(`${view} .gate button`)).includes("Read all anyway"), false, "do not offer to bypass the deadline");

  // Performance Schema off: server-wide counters, and one sentence with one action.
  state.queue.push({ status: 200, body: off });
  await h.refresh();
  await page.waitForSelector(`${view} .gate`);
  await h.idle(view);
  assert.equal(state.seen.at(-1).all_instances, undefined, "a normal refresh restores the instance limit");
  assert.match(await text(`${view} .gate`), /performance_schema=ON/);
  assert.ok(await page.$(`${view} .gate [data-instruments-row=ps]`));
  assert.equal(await page.$(`${view} .data-table`), null);
  assert.match(await text(`${view} .io-global-rates > summary`), /Server-wide InnoDB I\/O/);
  assert.equal(await page.$eval(`${view} .io-global-rates`, (e) => e.open), true, "rates stay visible without per-table data");
  await h.select(`${view} .tile-item[data-metric="data_written"]`);
  await page.waitForFunction(() => /redo log writes are not included/.test(document.querySelector(".view-inspector")?.textContent || ""));

  // Error keeps the report.
  state.queue.push({ status: 400, body: { error: "Could not read I/O statistics: ERROR 2013 (HY000): Lost connection" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.ok(await page.$(`${view} .fact-strip`));

  await h.narrow("io-390.png");

  // Session restore keeps the view, schema and window.
  const saved = await h.savedSlow((s) => s.kind === "io" && s.io_interval === 5 && s.schema === "mfshop");
  assert.equal(JSON.stringify(saved.state).includes("io-1.0"), false, "reports are not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-io[aria-current=page]");
  assert.equal(await page.$eval(`${view} .perf-window select`, (e) => e.value), "5");
  assert.equal(await page.$eval("#io-schema", (e) => e.value), "mfshop");

  assert.deepEqual(h.errors, []);
  console.log("io view: ok");
} finally {
  await h.close();
}
