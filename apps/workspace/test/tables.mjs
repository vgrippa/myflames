// Tables view of the Server place: mocked /api/live/tables with schema-1.0
// reports recomputed from live captures (fixtures/), and the database picker's
// /api/live/schemas. All user databases is the default, Fresh numbers is
// MySQL only, rows sort and filter inside a scrolling region, the why and the
// fix statements are in the inspector, Tables and Indexes link to each other
// for one table, and the chosen databases are restored with the workspace.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const fresh = await load("tables-mysql-8.4.json");
const cached = await load("tables-mysql-8.4-cached.json");
const maria = await load("tables-mariadb-13.0.json");
const catalog = await load("schemas-mysql-8.4.json");
const largeCatalog = { ...catalog, user_tables: 5000,
  schemas: catalog.schemas.map((x) => (x.name === "mftab" ? { ...x, tables: 4999 } : x)) };

const schemasMock = { status: 200, body: catalog };
const tableColumns = (body) => ({ status: 200, body: { schema: body.schema, table: body.table, columns: [
  { name: "a", type: "int", nullable: true, default: null, key: "", extra: "" },
  { name: "b", type: "varchar(20)", nullable: false, default: "x", key: "MUL", extra: "" },
] } });
// Review and apply hands the statements to the reviewed-SQL sheet: the run
// asks for confirmation, and a decline sends nothing.
const editorRun = (body) => body.decline
  ? { status: 200, body: { status: "declined" } }
  : { status: 200, body: { status: "needs_confirmation", confirmation: { token: "t1", text: "Review the statements.", tone: "primary",
      connection: { user: "root", host: "127.0.0.1", port: 3306 }, connection_type: "development", warnings: [],
      statements: body.sql.split(";\n"), typed_target: null } } };
// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/tables", "tables", { "/api/live/schemas": schemasMock, "/api/live/table-columns": tableColumns,
  "/api/editor/preview": { status: 400, body: { error: "Not checked in this test." } }, "/api/editor/run": editorRun,
  "/api/editor/close": { status: 200, body: { closed: true, session: null } } }, { list: "docked" });
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: fresh };
const view = ".server-view[data-view=tables]";
const rows = (label) => page.$$eval(`${view} [aria-label="${label}"] tbody tr`, (els) => els.map((e) => e.dataset.table));
const missingLabel = "Tables without a primary key";
const freeLabel = "Free space by table";
const whyPk = async () => {
  await page.click('button[aria-label="About Tables"]');
  await page.locator('[role=dialog][aria-label="About Tables"] button ::-p-text(Why a primary key matters)').click();
};
const inspectorHas = (pattern) =>
  page.waitForFunction((p) => new RegExp(p).test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);
const reloadCatalog = async (body, gated) => {
  schemasMock.body = body;
  await page.click("#tables-schemas");
  await page.locator("#tables-schemas-panel button ::-p-text(Reload list)").click();
  await page.waitForFunction((expected) => !!document.querySelector("#tables-load-hint") === expected, {}, gated);
  await page.keyboard.press("Escape");
};
const assertRefreshBlocked = async () => {
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, (e) => e.disabled), true);
  const before = state.seen.length;
  await h.refresh();
  await pause(100);
  assert.equal(state.seen.length, before, "gated Refresh sends no table read");
};
try {
  await h.server();
  // Tables follows Indexes in the Schema group (Database diagram is a place of its own).
  assert.deepEqual(await texts(".source-list .source-group:nth-child(3) .source-label"), ["Indexes", "Tables"]);
  await h.view("tables");
  // A small server reads on open, because the view is on screen.
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  assert.equal(state.seen.length, 1, "opening reads once");
  state.seen.length = 0;
  assert.equal(await text(`${view} .perf-picker-summary`), "All user databases");

  // The picker: user databases with table counts, system ones hidden until asked, search, typed names.
  await page.click("#tables-schemas");
  await page.waitForSelector('[role=dialog][aria-label="Choose databases"] #tables-schemas-panel');
  // Focus moves to the search once the popover is placed (the next frame).
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Search databases");
  assert.equal(await page.$eval(`${view} .perf-picker-button`, (e) => e.getAttribute("aria-expanded")), "true");
  await page.waitForFunction(() => document.querySelectorAll("#tables-schemas-panel .perf-picker-list li").length === 2);
  assert.deepEqual(await texts("#tables-schemas-panel .perf-picker-list li"), ["mfother1 table", "mftab18 tables"]);
  assert.match(await text("#tables-schemas-panel .perf-picker-all"), /All user databases\s*19 tables/);
  assert.equal(await page.$eval("#tables-schemas-panel .perf-picker-all input", (e) => e.checked), true);
  await page.locator("#tables-schemas-panel label ::-p-text(Show system databases)").click();
  assert.equal((await texts("#tables-schemas-panel .perf-picker-list li")).length, 6);
  await page.locator("#tables-schemas-panel label ::-p-text(Show system databases)").click();
  assert.equal(await page.evaluate(() => document.activeElement?.type), "checkbox");
  await page.focus("#tables-schemas-panel .perf-picker-search");
  await page.keyboard.type("tab");
  assert.deepEqual(await texts("#tables-schemas-panel .perf-picker-list li"), ["mftab18 tables"]);
  await page.click("#tables-schemas-panel .perf-picker-list input");
  assert.equal(await page.$eval("#tables-schemas-panel .perf-picker-all input", (e) => e.checked), false);
  // A name the list does not have can be typed and used (Enter).
  await page.click("#tables-schemas-panel .perf-picker-search", { clickCount: 3 });
  await page.keyboard.type("archive_db");
  assert.match(await text("#tables-schemas-panel .perf-picker-foot"), /Use “archive_db”/);
  await page.keyboard.press("Enter");
  assert.equal(await text(`${view} .perf-picker-summary`), "mftab, archive_db");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#tables-schemas-panel"));
  assert.equal(await page.$eval(`${view} .perf-picker-button`, (e) => e === document.activeElement), true, "focus returns to the picker");
  // Done and a click outside dismiss the floating list without clearing choices.
  await page.click("#tables-schemas");
  await page.locator("#tables-schemas-panel button ::-p-text(Done)").click();
  await page.waitForFunction(() => !document.querySelector("#tables-schemas-panel"));
  assert.equal(await page.$eval(`${view} .perf-picker-button`, (e) => e === document.activeElement), true);
  await page.click("#tables-schemas");
  await page.click(`${view} .view-title h2`);
  await page.waitForFunction(() => !document.querySelector("#tables-schemas-panel"));
  assert.equal(await text(`${view} .perf-picker-summary`), "mftab, archive_db");
  assert.equal(state.other.filter((o) => o.path === "/api/live/schemas").length, 1, "the list is read once");

  // Before any report exists, both normal read actions respect the selected scope's cap.
  await reloadCatalog(largeCatalog, true);
  assert.match(await text("#tables-load-hint"), /2,000 table limit; choose fewer databases/);
  await assertRefreshBlocked();
  await reloadCatalog(catalog, false);
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, (e) => e.disabled), false);

  // Load: the chosen databases, fresh off by default.
  state.delay = 300;
  await h.refresh();
  await page.waitForSelector(`${view} .capture-status`);
  assert.match(await text(`${view} .capture-status`), /Reading tables in mftab, archive_db/);
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  state.delay = 0;
  assert.deepEqual(state.seen.at(-1).schemas, ["mftab", "archive_db"]);
  assert.equal(state.seen.at(-1).fresh, false);
  assert.match(await text(`${view} .fact-strip`), /Statistics\s*fresh from InnoDB/);
  assert.match(await text(`${view} .perf-summary-line`),
    /^10 tables without a usable key, 2 with only a unique NOT NULL key · 2 tables with reusable free space$/);
  assert.ok((await text(`${view} .summary-line p`)).length <= 120, "one summary line, at most 120 characters");
  assert.deepEqual(await page.$$eval(`${view} .segmented button`, (els) => els.map((e) => e.dataset.value)), ["missing", "free"]);
  assert.equal(await page.$eval("#server-nav-tables .status-dot", (e) => e.className), "status-dot is-problem");

  // Missing primary keys: worst first, sortable, filterable; why a key matters in the inspector.
  const missing = await rows(missingLabel);
  assert.equal(missing.length, 12);
  assert.deepEqual(missing.slice(-2).sort(), ["mftab.notnull_uk", "mftab.two_uk"], "watch rows after problems");
  // Why a primary key matters is a topic reached from the view's ⓘ (and the empty inspector).
  await whyPk();
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Why a primary key matters");
  assert.equal(await page.$('[role=dialog][aria-label="About Tables"]'), null, "the popover closes");
  const why = await h.inspector();
  assert.match(why, /up to one full table scan per event/);
  assert.match(why, /global dictionary mutex/);
  assert.match(why, /does not comply with the requirements by an external plugin/);
  assert.match(why, /pt-online-schema-change and gh-ost/);
  const size = `${view} [aria-label="${missingLabel}"] th:nth-child(5)`;
  // With the inspector open the table scrolls sideways inside its frame, and
  // Size sits under the sticky Columns cell: scroll it into view first.
  await page.$eval(size, (e) => e.scrollIntoView({ block: "nearest", inline: "center" }));
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${size} button`);
  assert.equal(await page.$eval(size, (e) => e.getAttribute("aria-sort")), "descending");
  assert.equal((await rows(missingLabel))[0], "mftab.two_uk", "largest first");
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${size} button`);
  assert.equal(await page.$eval(size, (e) => e.getAttribute("aria-sort")), "ascending");
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-pk-status select`, "watch");
  assert.deepEqual((await rows(missingLabel)).sort(), ["mftab.notnull_uk", "mftab.two_uk"]);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-pk-status select`, "all");
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-engine select`, "other");
  assert.deepEqual(await rows(missingLabel), ["mftab.myisam_nopk"]);
  assert.match(await text(`${view} tr[data-table="mftab.myisam_nopk"]`), /not InnoDB/);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-engine select`, "all");
  await page.type(`${view} input[aria-label="Filter tables"]`, "uk");
  assert.ok((await rows(missingLabel)).every((t) => t.includes("uk")));
  await page.click(`${view} input[aria-label="Filter tables"]`, { clickCount: 3 });
  await page.keyboard.press("Backspace");

  // Row details in the inspector: why, the NULL check and the fix to copy, the caveat.
  const nuk = `${view} tr[data-table="mftab.nullable_uk"]`;
  assert.match(await text(nuk), /hidden row ID/);
  await h.select(nuk);
  await inspectorHas(/uk_a \(a\): a allows NULL/);
  const detail = await h.inspector();
  assert.match(detail, /SELECT COUNT\(\*\) FROM `mftab`\.`nullable_uk` WHERE `a` IS NULL;/);
  assert.match(detail, /ALTER TABLE `mftab`\.`nullable_uk` ADD PRIMARY KEY \(`a`\), DROP INDEX `uk_a`, ALGORITHM=INPLACE, LOCK=NONE;/);
  assert.match(detail, /LOCK=SHARED/);
  assert.match(detail, /queue behind that wait/);
  await page.evaluate(() => {
    window.__copied = [];
    navigator.clipboard.writeText = (t) => (window.__copied.push(t), Promise.resolve());
  });
  await page.click(".view-inspector .perf-sql button");
  assert.deepEqual(await page.evaluate(() => window.__copied), ["SELECT COUNT(*) FROM `mftab`.`nullable_uk` WHERE `a` IS NULL;"]);
  assert.match(await text(".view-inspector .perf-sql [role=status]"), /Run it yourself/);
  const two = `${view} tr[data-table="mftab.two_uk"]`;
  assert.match(await text(two), /Watch/);
  await h.select(two);
  await inspectorHas(/uk_b/);
  assert.match(await text(`${view} .tables-gipk`), /mftab\.gipk_t/);

  // Free space is informational; positive reusable extents are listed by default.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=free]`);
  assert.deepEqual(await rows(freeLabel), ["mftab.frag_fpt", "mftab.ae_t"]);
  const fpt = `${view} tr[data-table="mftab.frag_fpt"]`;
  assert.match(await text(fpt), /Reusable space/);
  assert.match(await text(fpt), /275\.0 MiB/);
  await h.select(fpt);
  await inspectorHas(/OPTIMIZE TABLE `mftab`\.`frag_fpt`;/);
  assert.match(await h.inspector(), /ALTER TABLE `mftab`\.`frag_fpt` FORCE, ALGORITHM=INPLACE, LOCK=NONE;/);
  assert.match(await h.inspector(), /Optional maintenance; review and confirm/);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-free-show select`, "shared");
  assert.deepEqual((await rows(freeLabel)).sort(), ["mftab.frag_gen", "mftab.gen_small", "mftab.sys_small"]);
  await h.select(`${view} tr[data-table="mftab.frag_gen"]`);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "mftab.frag_gen");
  assert.equal(await page.$(".view-inspector .perf-sql"), null, "no per-table fix");
  assert.match(await text(`${view} .tables-shared`), /mf_gen \(general\)/);
  assert.match(await text(`${view} .tables-shared`), /the file does not shrink/);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.select(`${view} .tables-free-show select`, "info");
  // The tables scroll inside their region with the header in view.
  const overflow = await page.$eval(`${view} [aria-label="${freeLabel}"]`, (e) => getComputedStyle(e).overflowY);
  assert.equal(overflow, "auto");
  assert.equal(await page.$eval(`${view} [aria-label="${freeLabel}"] th`, (e) => getComputedStyle(e).position), "sticky");
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=missing]`);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "tables.png") });

  // Fresh numbers: MySQL only, sent with the read, explained (behind ⓘ) as this connection only.
  await page.click(`${view} .tables-fresh input`);
  assert.match(await h.popover("About Fresh numbers"), /own connection only, not the server/);
  // A backend cap rejection remains visible even if the catalog count is stale.
  const limitError = "This scope needs an explicit all_schemas / --all-schemas override";
  state.queue.push({ status: 400, body: { error: limitError } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  await h.idle(view);
  assert.equal(await text(`${view} [role=alert]`), limitError);
  await assertRefreshBlocked();
  // The selected scope is gated, even with cached MySQL numbers.
  schemasMock.body = largeCatalog;
  await page.click("#tables-schemas");
  await page.locator("#tables-schemas-panel button ::-p-text(Reload list)").click();
  await page.waitForFunction(() => /4,999 tables/.test(document.querySelector("#tables-schemas-panel")?.textContent || ""));
  await page.keyboard.press("Escape");
  await assertRefreshBlocked();
  state.queue.push({ status: 200, body: cached });
  await button("Read all anyway", view + " ");
  await page.waitForFunction(() => /cached, up to 1 d 0 h old/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  assert.equal(state.seen.at(-1).fresh, true);
  assert.equal(state.seen.at(-1).all_schemas, true, "only the explicit action opts out");
  assert.deepEqual(state.seen.at(-1).schemas, ["mftab", "archive_db"], "override preserves the selected databases");
  await assertRefreshBlocked();
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "tables-gate.png") });
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=free]`);
  assert.deepEqual(await rows(freeLabel), ["mftab.ae_t", "mftab.frag_fpt"], "cached positive free space remains informational");
  assert.match(await h.notes(), /information_schema_stats_expiry/);

  // Narrowing below the cap enables normal reads again.
  await reloadCatalog(catalog, false);
  // MariaDB: no Fresh numbers (no cache), the system tablespace is shared.
  state.queue.push({ status: 200, body: maria });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  assert.equal(state.seen.at(-1).all_schemas, undefined, "a normal refresh restores the limit");
  assert.equal(await page.$(`${view} .tables-fresh`), null);
  assert.match(await text(`${view} .fact-strip`), /read now \(MariaDB has no cache\)/);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=missing]`);
  await whyPk();
  await inspectorHas(/Galera/);
  assert.match(await h.inspector(), /until it finds the row/);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=free]`);
  assert.match(await text(`${view} .tables-shared`), /innodb_system \(system\)/);

  // An error keeps the report and shows in an alert.
  state.queue.push({ status: 400, body: { error: "Could not read table health: ERROR 1142 (42000): SELECT command denied" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.equal(state.seen.at(-1).fresh, false, "fresh is never sent once the server is known to be MariaDB");
  assert.match(await text(`${view} [role=alert]`), /ERROR 1142/);
  assert.ok(await page.$(`${view} .fact-strip`));

  // Tables -> Indexes for one table: the Indexes view reads that database and shows only that table.
  state.queue.push({ status: 200, body: fresh });
  await h.refresh();
  await page.waitForFunction(() => !document.querySelector(".server-view[data-view=tables] [role=alert]"));
  await h.idle(view);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.click(`${view} .segmented button[data-value=missing]`);
  await h.select(`${view} tr[data-table="mftab.no_key"]`);

  // Add column and Remove column are modal sheets: labelled by their title,
  // focus moves in and stays inside, Escape and Cancel close them and return
  // focus to the control that opened them, and Cancel and the review button
  // stay in view in the 720x520 window (492 px of content).
  const sheet = "dialog.table-alter-dialog";
  const sheetState = () => page.$eval(sheet, (d) => ({
    modal: d.open && d.matches(":modal"),
    role: d.getAttribute("role"),
    title: document.getElementById(d.getAttribute("aria-labelledby") || "")?.textContent || "",
    described: !!document.getElementById(d.getAttribute("aria-describedby") || "")?.textContent,
    focused: d.contains(document.activeElement) ? (document.activeElement.closest("label")?.querySelector(".field-label")?.textContent || document.activeElement.textContent) : null,
    actions: Array.from(d.querySelectorAll(".sheet-actions button"), (b) => {
      const r = b.getBoundingClientRect();
      return [b.textContent, r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth];
    }),
  }));
  const tabStaysInside = async (presses) => {
    for (let i = 0; i < presses; i++) {
      await page.keyboard.press("Tab");
      assert.equal(await page.$eval(sheet, (d) => d.contains(document.activeElement)), true, "Tab stays in the sheet");
    }
    await page.keyboard.down("Shift");
    for (let i = 0; i < 3; i++) {
      await page.keyboard.press("Tab");
      assert.equal(await page.$eval(sheet, (d) => d.contains(document.activeElement)), true, "Shift+Tab stays in the sheet");
    }
    await page.keyboard.up("Shift");
  };
  const addButton = ".view-inspector button ::-p-text(Add column)";
  await page.locator(addButton).click();
  await page.waitForSelector(sheet);
  await page.waitForFunction((s) => document.querySelector(s)?.contains(document.activeElement), {}, sheet);
  let alter = await sheetState();
  assert.equal(alter.modal, true, "Add column is a modal dialog");
  assert.equal(alter.role, "dialog");
  assert.equal(alter.title, "Add column to mftab.no_key");
  assert.equal(alter.described, true);
  assert.equal(alter.focused, "Column name", "focus starts in the column name");
  await tabStaysInside(10);
  await page.setViewport({ width: 720, height: 492 });
  await page.waitForFunction((s) => document.querySelector(s)?.getBoundingClientRect().height <= innerHeight, {}, sheet);
  assert.deepEqual((await sheetState()).actions, [["Cancel", true], ["Review and apply…", true]], "actions stay in view at 720x492");
  await page.setViewport({ width: 1440, height: 1000 });
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Add column", "Escape returns focus to Add column");

  // Remove column: Cancel first (a destructive change), actions in view at 720x492.
  await page.locator(".view-inspector button ::-p-text(Remove column)").click();
  await page.waitForSelector(`${sheet} .col-info-card`);
  alter = await sheetState();
  assert.equal(alter.modal, true, "Remove column is a modal dialog");
  assert.equal(alter.role, "alertdialog");
  assert.equal(alter.title, "Remove column from mftab.no_key");
  assert.equal(alter.focused, "Cancel", "Cancel is focused first");
  await tabStaysInside(6);
  await page.setViewport({ width: 720, height: 492 });
  await page.waitForFunction((s) => document.querySelector(s)?.getBoundingClientRect().height <= innerHeight, {}, sheet);
  alter = await sheetState();
  assert.deepEqual(alter.actions, [["Cancel", true], ["Review and drop column…", true]], "actions stay in view at 720x492");
  await (await page.$(sheet)).screenshot({ path: path.join(h.shots, "tables-remove-column-720.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.locator(`${sheet} button ::-p-text(Cancel)`).click();
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Remove column", "Cancel returns focus to Remove column");

  // Review and apply closes the column sheet and hands the exact statements to
  // the reviewed-SQL sheet, which takes focus; declining it returns focus to
  // Add column and sends nothing.
  await page.locator(addButton).click();
  await page.waitForFunction((s) => document.querySelector(s + " input")?.matches(":focus"), {}, sheet);
  await page.keyboard.type("is_active");
  await page.locator(`${sheet} button ::-p-text(Review and apply)`).click();
  await page.waitForFunction(() => /ADD COLUMN `is_active`/.test(document.querySelector("dialog.confirm-sheet .sql-preview")?.textContent || ""));
  assert.equal(await page.$(sheet), null, "the column sheet closes");
  const sent = state.other.filter((o) => o.path === "/api/editor/run").at(-1).body;
  assert.equal(sent.sql, "SET SESSION lock_wait_timeout = 5;\nALTER TABLE `mftab`.`no_key` ADD COLUMN `is_active` VARCHAR(255) NULL");
  assert.equal(sent.require_confirmation, true);
  await page.waitForFunction(() => document.querySelector("dialog.confirm-sheet")?.contains(document.activeElement));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"));
  assert.equal(state.other.filter((o) => o.path === "/api/editor/run").at(-1).body.decline, true, "Escape declines the review");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Add column", "focus returns to Add column after the review");

  // From the row's Actions menu, focus returns to the menu button (the menu
  // closes with the choice). The table shows once the row's details close.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  // The row's ⋯ menu (shown on the hovered, focused or selected row).
  const trigger = `${view} tr[data-table="mftab.no_key"] .row-more`;
  await page.waitForSelector(trigger);
  await page.$eval(trigger, (b) => { b.scrollIntoView({ block: "nearest", inline: "nearest" }); b.focus(); b.click(); });
  await page.waitForSelector(".row-action-menu [role=menuitem]");
  await page.$$eval(".row-action-menu [role=menuitem]", (items) => {
    const add = items.find((i) => /^Add column/.test((i.textContent || "").trim()));
    add.focus(); add.click();
  });
  await page.waitForSelector(sheet);
  assert.equal((await sheetState()).title, "Add column to mftab.no_key");
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
  assert.equal(await page.$eval(trigger, (b) => b === document.activeElement), true, "focus returns to the row's ⋯ button");
  await h.select(`${view} tr[data-table="mftab.no_key"]`);

  await page.locator(".view-inspector button ::-p-text(Indexes)").click();
  await page.waitForSelector("#server-nav-indexes[aria-current=page]");
  for (let i = 0; i < 50 && !state.other.some((o) => o.path === "/api/live/indexes"); i++) await pause(50);
  const indexesCall = state.other.find((o) => o.path === "/api/live/indexes");
  assert.deepEqual(indexesCall.body.schemas, ["mftab"]);
  assert.equal(await text(".server-view .perf-picker-summary"), "mftab");
  await h.view("tables");
  await page.waitForSelector(`${view} .fact-strip`);

  await h.narrow("tables-390.png");

  // Session restore keeps the view and its databases (per view), never the report.
  // Wait for the save made after the Indexes visit: an earlier one (kind
  // tables, same databases) can still be the stored one under load.
  const saved = await h.savedSlow((s) => s.kind === "tables" && JSON.stringify(s.tables_schemas) === '["mftab","archive_db"]' &&
    JSON.stringify(s.indexes_schemas) === '["mftab"]');
  assert.deepEqual(saved.state.query_lab.slow.indexes_schemas, ["mftab"]);
  assert.equal(JSON.stringify(saved.state).includes("schema-1.0"), false, "reports are not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-tables[aria-current=page]");
  assert.equal(await text(`${view} .perf-picker-summary`), "mftab, archive_db");
  await h.view("indexes");
  assert.equal(await text(".server-view .perf-picker-summary"), "mftab");

  assert.deepEqual(h.errors, []);
  console.log("tables view: ok");
} finally {
  await h.close();
}
