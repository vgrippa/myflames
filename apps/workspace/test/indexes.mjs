// Indexes view of the Server place: mocked /api/live/indexes with indexes-1.0
// reports replayed from live captures (fixtures/). The database picker
// defaults to all user databases while the server has at most 2,000 tables
// (above that a choice is required, with one sentence saying why), there is no
// automatic refresh, one list shows at a time, the why and the statements are
// in the inspector, links to and from Tables focus one table, a full-scan
// table shows the statements that name it on request (Explain, Run analysis
// with its confirmation, Open in Top statements with the table chip), and the
// choice is restored with the workspace.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const mysql = await load("indexes-mysql-8.4.json");
const off = await load("indexes-mariadb-13.0-default.json");
const userstat = await load("indexes-mariadb-11.4-userstat.json");
const tablesReport = await load("tables-mysql-8.4.json");
const small = {
  schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11", taken_at: "", uptime_s: 802,
  performance_schema: true, notes: [], user_tables: 6, all_limit: 2000, all_note: "Reading every user schema is the default up to 2,000 tables.",
  system: ["mysql", "sys", "performance_schema", "information_schema"],
  schemas: [{ name: "mfshop", tables: 4, system: false }, { name: "shop2", tables: 2, system: false },
    { name: "mysql", tables: 38, system: true }],
};
const schemasMock = { status: 200, body: small };
// Statements that name mfshop.orders (top-1.0 with a table filter).
const naming = (digest, sample, extra = {}) => ({
  digest, digest_text: "SELECT * FROM `orders` WHERE `status` = ?", schema: "mfshop", count: 42, total_ms: 1830.5,
  avg_ms: 43.6, max_ms: 90, rows_examined: 420000, rows_sent: 420, examined_per_sent: 1000, no_index_used: 42,
  no_good_index_used: 0, tmp_disk_tables: 0, sort_merge_passes: 0, sample_text: sample, sample_truncated: false,
  first_seen: "2026-10-12 10:00:00", last_seen: "2026-10-14 10:00:00", flags: ["no_index_used"], capture_problem: null,
  select_full_join: 0, select_range_check: 0, select_scan: 42,
  table_match: { schema: "mfshop", table: "orders", kind: "unqualified", label: "text match, not proof" }, ...extra,
});
const topFor = (body) => ({
  schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: body.order, limit: body.limit, schema: null,
  notes: ["Counters accumulate since the server started or the digest table was last truncated."],
  statements: [naming("a".repeat(64), "SELECT * FROM orders WHERE status = 'new'"),
    naming("b".repeat(64), "SELECT COUNT(*) FROM orders", { count: 7, total_ms: 120, no_index_used: 7 })],
  table: body.table || null,
  coverage: { uptime_s: 192385, first_seen: "2026-10-12 10:00:00", digest_lost: 3, overflow: null },
  ...(body.table ? { match: { label: "text match, not proof", candidates: 2, capped: false,
    caveats: ["The table is matched by name in the normalized digest text, so this is a text match, not proof."] } } : {}),
});

// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/indexes", "indexes", {
  "/api/live/schemas": schemasMock,
  "/api/live/tables": { status: 200, body: tablesReport },
  "/api/live/top": (body) => ({ status: 200, body: topFor(body) }),
  "/api/live/start": { status: 400, body: { error: "Not mocked in this suite." } },
}, { list: "docked" });
const { page, state, button, text } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=indexes]";
const refresh = `${view} [data-action=refresh]`;
const list = async (name) => {
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=${name}]`);
};
const ids = (label) => page.$$eval(`${view} [aria-label="${label}"] tbody tr`, (els) => els.map((e) => e.dataset.index || e.dataset.table));
try {
  await h.server();
  await h.view("indexes");
  // A small server reads on open, because the view is on screen.
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  assert.equal(state.seen.length, 1, "opening reads once");
  state.seen.length = 0;
  assert.equal(await text(`${view} .perf-picker-summary`), "All user databases");
  assert.equal(await page.$eval(refresh, (e) => e.disabled), false);
  assert.equal(await page.$(`${view} .auto-refresh`), null, "no automatic refresh");
  // Above 2,000 tables in user databases a choice is required: one sentence says why.
  schemasMock.body = { ...small, user_tables: 12345 };
  await page.click("#indexes-schemas");
  const picker = `${view} .perf-picker-button`;
  await page.waitForSelector('[role=dialog][aria-label="Choose databases"] #indexes-schemas-panel');
  assert.equal(await page.$eval(picker, (e) => e.getAttribute("aria-expanded")), "true");
  // Focus moves to the search once the popover is placed (the next frame).
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Search databases");
  await page.locator("#indexes-schemas-panel button ::-p-text(Reload list)").click();
  await page.waitForFunction((sel) => document.querySelector(sel).disabled, {}, refresh);
  assert.match(await text(`${view} .gate #indexes-load-hint`), /12,345 tables in user databases, more than 2,000: choose databases/);
  await page.click("#indexes-schemas-panel .perf-picker-list input");
  assert.equal(await text(`${view} .perf-picker-summary`), "mfshop");
  assert.equal(await page.$eval(refresh, (e) => e.disabled), false);
  assert.equal(await page.$(`${view} .gate`), null);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Choose databases"]'));
  assert.equal(await page.$eval(picker, (e) => e === document.activeElement), true);
  assert.equal(await page.$eval(picker, (e) => e.getAttribute("aria-expanded")), "false");
  // Opening another kit popover dismisses the picker without losing selection.
  await page.click("#indexes-schemas");
  await page.$eval(`${view} button[aria-label="About Indexes"]`, (e) => e.click());
  await page.waitForFunction(() => !document.querySelector("#indexes-schemas-panel"));
  assert.equal(await text(`${view} .perf-picker-summary`), "mfshop");
  await page.keyboard.press("Escape");
  schemasMock.body = small;

  state.delay = 300;
  await h.refresh();
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Reading index usage for mfshop/);
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  state.delay = 0;
  assert.deepEqual(state.seen.map((s) => [s.schemas, s.all_schemas]), [[["mfshop"], undefined]]);
  assert.match(await text(`${view} .fact-strip`), /Databases\s*mfshop/);
  assert.match(await text(`${view} .fact-strip`), /Usage from\s*Performance Schema/);
  assert.equal(await text(`${view} .perf-summary-line`),
    "5 unused · 1 for foreign keys · 3 redundant · 1 mostly full scans · 1 without data");
  assert.deepEqual(await page.$$eval(`${view} .segmented button`, (els) => els.map((e) => e.textContent)),
    ["Unused6", "Redundant3", "Full scans1", "No data1"]);
  // The badge counts what to review.
  assert.equal(await text("#server-nav-indexes .count-badge"), "8");
  // Row actions are a ⋯ menu in the trailing column (no bordered Actions button
  // per row): hidden at rest, shown on the hovered, focused or selected row.
  assert.equal(await page.$(`${view} th[data-col=actions]`), null);
  await page.mouse.move(5, 890);
  assert.deepEqual([...new Set(await page.$$eval(`${view} tbody td.col-menu .row-more`, (els) => els.map((e) => getComputedStyle(e).opacity)))], ["0"]);
  await page.hover(`${view} tbody tr:first-child td:nth-child(2)`);
  assert.equal(await page.$eval(`${view} tbody tr:first-child .row-more`, (e) => getComputedStyle(e).opacity), "1");
  await page.click(`${view} tbody tr:first-child .row-more`);
  await page.waitForSelector(".row-action-menu [role=menuitem]", { visible: true });
  assert.deepEqual(await page.$$eval(".row-action-menu [role=menuitem]", (els) => els.map((e) => e.textContent.trim())),
    ["Set invisible…", "Drop index…", "Table health"]);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".row-action-menu"));

  // Unused: candidates and the FK-backing index to keep; PRIMARY/UNIQUE never listed.
  const unused = await page.$$eval(`${view} [aria-label="Unused indexes"] tbody tr`,
    (els) => els.map((e) => [e.dataset.index, e.querySelector(".status-pill").textContent]));
  assert.deepEqual(unused, [
    ["customers.k_country_created", "Candidate"], ["odd$name.k_ab", "Candidate"], ["orders.k_note10", "Candidate"],
    ["orders.k_status", "Candidate"], ["orders.k_status_dup", "Candidate"], ["orders.k_customer", "Keep: backs a foreign key"],
  ]);
  await h.select(`${view} tr[data-index="orders.k_customer"]`);
  await page.waitForFunction(() => /backs a foreign key \(child side of fk_orders_customer/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.equal(await page.$(".view-inspector .perf-sql"), null, "no drop statement for an index to keep");
  // Keyboard: Up moves to the candidate above; details open on Enter with the statements.
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector") && document.activeElement?.matches("[data-row-key]"));
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "k_status_dup");
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector") && document.activeElement?.matches("[data-row-key]"));
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "k_status");
  const why = await h.inspector();
  assert.match(why, /COUNT_STAR = 0/);
  assert.match(why, /ALTER TABLE `mfshop`\.`orders` ALTER INDEX `k_status` INVISIBLE;/);
  assert.match(why, /ALTER TABLE `mfshop`\.`orders` DROP INDEX `k_status`;/);
  assert.match(why, /Make it invisible first/);
  // Redundant: the covering index and the reason.
  await list("redundant");
  const redundant = await page.$$eval(`${view} [aria-label="Redundant indexes"] tbody tr`, (els) => els.map((e) => e.textContent));
  assert.equal(redundant.length, 3);
  assert.match(redundant[0], /k_country \(country\)prefix of k_country_created \(country, created_at\)/);
  assert.match(redundant[2], /k_status_dup \(status\)duplicates k_status \(status\)/);
  // Full scans: the flag and Top statements for the schema in the inspector.
  await list("scans");
  const scan = `${view} tr[data-table="orders"]`;
  assert.match(await text(scan), /Mostly full scans/);
  const scanName = await page.$eval(`${scan} td[data-col=table]`, (cell) => {
    const name = cell.querySelector("code");
    return { width: cell.getBoundingClientRect().width, title: name.title, clipped: name.scrollWidth > name.clientWidth };
  });
  assert.ok(scanName.width >= 240, "the primary table column keeps its minimum width");
  assert.equal(scanName.title, "mfshop.orders");
  assert.equal(scanName.clipped, false, "a short table name fits beside its pill");
  assert.match(await text(scan), /97% of read time/);
  await list("nodata");
  assert.match(await text(`${view} [aria-label="Tables with no data"]`), /mfshop\.idle_table\s*2\s*not opened since the counts started/);
  await list("scans");
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "indexes.png") });

  // The statements that name the table: read only on request, labeled a text match.
  const topCalls = () => h.state.other.filter((o) => o.path === "/api/live/top");
  const waitCalls = async (n) => {
    for (let i = 0; i < 100 && topCalls().length < n; i++) await pause(50);
    assert.equal(topCalls().length, n);
  };
  await h.select(scan);
  assert.match(await h.inspector(), /Statements that name it/);
  assert.equal(topCalls().length, 0, "nothing is read until Show statements");
  await page.locator(".view-inspector button ::-p-text(Show statements)").click();
  await page.waitForSelector(".view-inspector .statements-behind-list li");
  await waitCalls(1);
  assert.deepEqual([topCalls()[0].body.order, topCalls()[0].body.limit, topCalls()[0].body.table], ["no_index", 5, { schema: "mfshop", table: "orders" }]);
  let behind = await h.inspector();
  assert.match(behind, /Top 2 statements by calls without an index, since 2026-10-12 10:00:00 · text match, not proof/);
  assert.match(behind, /SELECT \* FROM orders WHERE status = 'new'/);
  assert.match(behind, /42 calls · 1\.83 s total · 42 calls without an index · mfshop/);
  assert.equal(await page.$('.view-inspector [aria-label="Use statement 1 that names mfshop.orders in the editor"]'), null, "compact actions");
  // Coverage and the caveats are behind the notes button.
  await page.click(".view-inspector .statements-behind .notes-button");
  const covered = await page.$eval(".popover", (e) => e.textContent);
  assert.match(covered, /Server uptime 2 d 5 h; the oldest statement listed was first seen 2026-10-12 10:00:00\./);
  assert.match(covered, /Performance_schema_digest_lost is 3/);
  assert.match(covered, /text match, not proof/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".popover"));
  // Run analysis asks first (the Top statements confirmation); Cancel sends nothing.
  await page.click('.view-inspector [aria-label="Run analysis on statement 1 that names mfshop.orders"]');
  await page.waitForSelector(".view-inspector .run-confirm");
  assert.match(await text(".view-inspector .run-confirm"), /Run analysis executes this statement on 127\.0\.0\.1:3306/);
  await page.locator(".view-inspector .run-confirm button ::-p-text(Cancel)").click();
  await page.waitForFunction(() => !document.querySelector(".view-inspector .run-confirm"));
  assert.equal(h.state.other.filter((o) => o.path === "/api/live/start").length, 0, "nothing ran");
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "indexes-statements.png") });

  // Open in Top statements: filtered to the table (a removable chip), ranked by reads without an index.
  await page.locator(".view-inspector button ::-p-text(Open in Top statements)").click();
  await page.waitForSelector("#server-nav-top[aria-current=page]");
  await page.waitForSelector(".server-view[data-view=top] .top-table-chip");
  assert.match(await text(".server-view[data-view=top] .top-table-chip"), /^Table: mfshop\.orders$/);
  assert.equal(await page.$eval(".server-view[data-view=top] .toolbar-field select", (e) => e.value), "no_index");
  await waitCalls(2);
  assert.deepEqual([topCalls()[1].body.order, topCalls()[1].body.table, topCalls()[1].body.schema],
    ["no_index", { schema: "mfshop", table: "orders" }, undefined], "a table filter clears the schema filter");
  await page.waitForSelector('.server-view[data-view=top] tr[data-rank="2"]');
  assert.match(await text(".server-view[data-view=top] .summary-line"),
    /2 statements · 2 flagged · with reads without an index · naming mfshop\.orders \(text match, not proof\)/);
  // Removing the chip reads every statement again (same order).
  await page.click(".server-view[data-view=top] .top-table-chip button");
  await page.waitForFunction(() => !document.querySelector(".server-view[data-view=top] .top-table-chip"));
  await waitCalls(3);
  assert.deepEqual([topCalls()[2].body.order, topCalls()[2].body.table], ["no_index", undefined]);
  await page.waitForFunction(() => !/naming/.test(document.querySelector(".server-view[data-view=top] .summary-line")?.textContent || "naming"));
  await page.type("#top-schema", "mfshop");

  // Back in Indexes: the report is kept (no second read) and the list tab starts again at its
  // default; the statements read earlier are kept, so opening the table shows them without a read.
  await h.view("indexes");
  await page.waitForSelector(`${view} .fact-strip`);
  assert.equal(state.seen.length, 1, "returning keeps the report");
  await list("scans");
  await h.select(scan);
  await page.waitForSelector(".view-inspector .statements-behind-list li");
  assert.equal(topCalls().length, 3, "returning shows the statements read before");
  await page.click('.view-inspector [aria-label="Explain statement 1 that names mfshop.orders"]');
  await page.waitForFunction(() => document.querySelector(".main-nav [aria-current=page]")?.textContent.includes("Query editor"));
  for (let i = 0; i < 100 && !h.state.other.some((o) => o.path === "/api/live/start"); i++) await pause(50);
  const start = h.state.other.find((o) => o.path === "/api/live/start").body;
  assert.deepEqual([start.mode, start.query, start.source?.kind], ["explain", "SELECT * FROM orders WHERE status = 'new'", "digest"]);
  await h.server();
  await h.view("indexes");
  await page.waitForSelector(`${view} .fact-strip`);
  assert.equal(state.seen.length, 1, "switching views keeps the report");

  // Performance Schema off without userstat: the gate on Unused, redundant indexes still listed.
  state.queue.push({ status: 200, body: off });
  await h.refresh();
  await page.waitForFunction(() => /full scans not available/.test(document.querySelector(".server-view .perf-summary-line")?.textContent || ""));
  await h.idle(view);
  await list("redundant");
  assert.equal(await page.$eval(`${view} .segmented button[data-value=redundant]`, (e) => e.getAttribute("aria-pressed")), "true");
  await list("unused");
  assert.match(await text(`${view} .gate`), /SET GLOBAL userstat = ON/);
  assert.ok(await page.$(`${view} .gate .instr-link`));
  assert.match(await text(`${view} .perf-summary-line`), /3 redundant · full scans not available/);
  // MariaDB user statistics: no read times, IGNORED instead of INVISIBLE.
  state.queue.push({ status: 200, body: userstat });
  await h.refresh();
  await page.waitForFunction(() => /Usage fromUser statistics \(userstat\)/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  await list("unused");
  await h.select(`${view} [aria-label="Unused indexes"] tr[data-index="orders.k_status"]`);
  await page.waitForFunction(() => /ALTER INDEX `k_status` IGNORED;/.test(document.querySelector(".view-inspector")?.textContent || ""));
  await list("scans");
  assert.match(await text(`${view} tr[data-table="orders"]`), /of rows/);
  // Partitioned and unresolved tables have no DROP/visibility path, including
  // the inventory, redundant list, inspector, row menu and right-click menu.
  // Keep a plain candidate alongside them as the positive action control.
  for (const engine of ["mysql", "mariadb"]) {
    for (const reason of ["partitioned", "partitioning_unknown"]) {
      const reasonText = reason === "partitioned"
        ? "partitioned, and Performance Schema does not count every read through a partitioned table's indexes"
        : "partitioning could not be read, so index use cannot be judged";
      const plain = { ...mysql.unused[0] };
      const blocked = { schema: "mfshop", table: "parted", index: "kw", columns: "w", unique: false,
        type: "BTREE", visible: true, action_blocked_reason: reasonText };
      const guarded = { ...mysql, engine, server_version: engine === "mysql" ? "8.4.11" : "11.4.13-MariaDB",
        indexes: [blocked, { ...plain, unique: false, type: "BTREE" }],
        unused: [plain],
        redundant: [{ ...mysql.redundant[0], ...blocked, dominant: "kw_more", dominant_columns: "w, id",
          test_sql: "", drop_sql: "", caveat: "" }],
        full_scans: [], no_data: [{ schema: "mfshop", table: "parted", indexes: 2, reason, reason_text: reasonText }],
        summary: { ...mysql.summary, tables: 2, indexes: 3, unused: 1, unused_candidates: 1,
          keep_foreign_key: 0, keep_userstat: 0, redundant: 1, full_scans: 0, mostly_full_scans: 0, no_data: 1 },
        more: { indexes: 0, unused: 0, redundant: 0, full_scans: 0, no_data: 0 } };
      if (await page.$(".detail-back")) await page.click(".detail-back");
      state.queue.push({ status: 200, body: guarded });
      const loaded = page.waitForResponse(response => response.url().endsWith("/api/live/indexes"));
      await h.refresh();
      await loaded;
      await h.idle(view);
      await list("nodata");
      const noDataRow = `${view} tr[data-table="parted"]`;
      await page.waitForSelector(noDataRow);
      assert.ok((await text(noDataRow)).includes(reasonText));
      await h.select(noDataRow);
      assert.ok((await h.inspector()).includes(reasonText));
      assert.doesNotMatch(await h.inspector(), /DROP INDEX|ALTER INDEX/);
      assert.equal(await page.$(".view-inspector .perf-sql"), null);
      assert.ok(!(await page.$$eval(".view-inspector button", els => els.map(e => e.textContent.trim())))
        .some(label => /Drop|Set invisible|Set visible|Set ignored|Set not ignored/.test(label)));
      await list("unused");
      assert.deepEqual(await ids("Unused indexes"), [`${plain.table}.${plain.index}`]);
      await page.click(`${view} tbody tr:first-child .row-more`);
      await page.waitForSelector(".row-action-menu [role=menuitem]", { visible: true });
      assert.match(await text(".row-action-menu"), /Drop index/);
      await page.keyboard.press("Escape");
      await page.waitForSelector(".row-action-menu", { hidden: true });
      for (const group of ["all", "redundant"]) {
        await list(group);
        const row = `${view} tr[data-index="parted.kw"]`;
        await page.waitForSelector(row);
        await page.click(`${row} .row-more`);
        await page.waitForSelector(".row-action-menu [role=menuitem]", { visible: true });
        assert.deepEqual(await page.$$eval(".row-action-menu [role=menuitem]", els => els.map(e => e.textContent.trim())),
          ["Table health"]);
        await page.keyboard.press("Escape");
        await page.waitForSelector(".row-action-menu", { hidden: true });
        await page.click(row, { button: "right" });
        await page.waitForSelector(".context-menu[role=menu]", { visible: true });
        assert.doesNotMatch(await text(".context-menu"), /DROP|Drop|invisible|visible|ignored/);
        await page.keyboard.press("Escape");
        await page.waitForSelector(".context-menu", { hidden: true });
        await h.select(row);
        assert.ok((await h.inspector()).includes(reasonText));
        assert.doesNotMatch(await h.inspector(), /DROP INDEX|ALTER INDEX/);
        assert.equal(await page.$(".view-inspector .perf-sql"), null);
        const actions = await page.$$eval(".view-inspector button", els => els.map(e => e.textContent.trim()));
        assert.ok(!actions.some(label => /Drop|Set invisible|Set visible|Set ignored|Set not ignored/.test(label)));
      }
    }
  }
  // Restore the list the following limit assertions exercise.
  await list("scans");
  // Every list cut at the server's limit says how many rows it leaves out.
  state.queue.push({ status: 200, body: { ...mysql, more: { unused: 1, redundant: 4, full_scans: 2, no_data: 3 } } });
  await h.refresh();
  await page.waitForSelector(`${view} .table-more[data-more=scans]`);
  await h.idle(view);
  for (const [name, n] of [["unused", 1], ["redundant", 4], ["scans", 2], ["nodata", 3]]) {
    await list(name);
    assert.equal(await text(`${view} .table-more[data-more=${name}]`), `${n} more not shown. Choose fewer databases to see them.`);
  }

  // All user databases sends no list (the server applies the 2,000-table rule);
  // an error shows in an alert and keeps the report.
  await page.click("#indexes-schemas");
  await page.click("#indexes-schemas-panel .perf-picker-all input");
  // Still the large server: All user databases cannot be loaded.
  assert.equal(await page.$eval(refresh, (e) => e.disabled), true);
  await page.locator("#indexes-schemas-panel button ::-p-text(Reload list)").click();
  await page.waitForFunction((sel) => !document.querySelector(sel).disabled, {}, refresh);
  await page.keyboard.press("Escape");
  state.queue.push({ status: 400, body: { error: "Could not read index health: ERROR 1142 (42000): SELECT command denied" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.equal(state.seen.at(-1).all_schemas, undefined);
  assert.equal(state.seen.at(-1).schemas, undefined);
  assert.match(await text(`${view} [role=alert]`), /ERROR 1142/);
  assert.ok(await page.$(`${view} .fact-strip`));

  // Indexes -> Tables for one table, and back: each view reads that database and shows only that table.
  state.queue.push({ status: 200, body: mysql });
  await h.refresh();
  await page.waitForFunction(() => !document.querySelector(".server-view [role=alert]"));
  await h.idle(view);
  await list("unused");
  await h.select(`${view} tr[data-index="orders.k_status"]`);
  await page.locator(".view-inspector button ::-p-text(Table health)").click();
  await page.waitForSelector("#server-nav-tables[aria-current=page]");
  await page.waitForSelector(".server-view[data-view=tables] .fact-strip");
  assert.deepEqual(h.state.other.findLast((o) => o.path === "/api/live/tables").body.schemas, ["mfshop"]);
  assert.match(await text(".server-view .perf-focus"), /Showing only\s*mfshop\.orders/);
  assert.equal((await page.$$(".server-view tbody tr")).length, 0, "the fixture has no mfshop.orders row");
  await page.locator(".server-view .perf-focus button ::-p-text(Show all tables)").click();
  assert.ok((await page.$$(".server-view tbody tr")).length > 0);
  await h.select('.server-view tr[data-table="mftab.no_key"]');
  await page.locator(".view-inspector button ::-p-text(Indexes)").click();
  await page.waitForSelector("#server-nav-indexes[aria-current=page]");
  for (let i = 0; i < 50 && state.seen.at(-1)?.schemas?.[0] !== "mftab"; i++) await pause(50);
  assert.deepEqual(state.seen.at(-1).schemas, ["mftab"]);
  await page.waitForSelector(`${view} .perf-focus`);
  assert.match(await text(`${view} .perf-focus`), /Showing only\s*mftab\.no_key/);
  assert.equal((await page.$$(`${view} tbody tr`)).length, 0, "only that table's rows");
  const reads = state.seen.length;
  await h.view("tables");
  await h.view("indexes");
  assert.equal(state.seen.length, reads, "returning to the view does not read again");
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.locator(`${view} .perf-focus button ::-p-text(Show all tables)`).click();
  assert.ok((await page.$$(`${view} tbody tr`)).length > 0);

  await h.narrow("indexes-390.png");

  // Session restore keeps the view and the chosen databases (its own, not Top statements'), never the report.
  await page.click("#indexes-schemas");
  await page.click("#indexes-schemas-panel .perf-picker-all input");
  await page.type("#indexes-schemas-panel .perf-picker-search", "shop9");
  await page.keyboard.press("Enter");
  await page.keyboard.press("Escape");
  const saved = await h.savedSlow((s) => s.kind === "indexes" && JSON.stringify(s.indexes_schemas) === '["shop9"]');
  assert.equal(JSON.stringify(saved.state).includes("indexes-1.0"), false, "reports are not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-indexes[aria-current=page]");
  assert.equal(await text(`${view} .perf-picker-summary`), "shop9");
  // The report is not restored: the view reads again on open, for the restored databases.
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  assert.deepEqual(state.seen.at(-1).schemas, ["shop9"], "the read on open uses the restored databases");
  await h.view("top");
  assert.equal(await page.$eval("#top-schema", (e) => e.value), "mfshop",
    "Top statements keeps its own schema (from the earlier link), not the Indexes choice");

  assert.deepEqual(h.errors, []);
  console.log("indexes view: ok");
} finally {
  await h.close();
}
