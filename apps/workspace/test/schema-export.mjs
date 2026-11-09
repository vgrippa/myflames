// Export as SQL (Schema versions) and Table structure (Server > Tables) on
// mocked endpoints. The replies are made by the Python package from the
// recorded lab fixtures in test/fixtures/schema_export (schema_export.build
// and schema_browser.table_structure replaying recorded client output), so
// the suite shows what the backend really sends. No server is contacted.
//
// Export as SQL: from a saved version's inspector and, for a database read
// now, from the Saved versions toolbar; the options (Database name in the
// script, Create the database, Keep definers) rebuild the script, a database
// read now is read once (later changes send its export_id), Copy script,
// Download .sql, Open in Query editor (nothing runs), errors once with Try
// again, Escape returns focus. Table structure: the inspector's action bar,
// the row's ⋯ menu and its context menu open one sheet that reads once from
// the list's connection; the Columns, Indexes, Foreign keys and CREATE TABLE
// tabs; Copy CREATE TABLE and Download .sql (<schema>.<table>.sql); an error
// once with Try again. Both sheets at 1440 and 720 px, light and dark, with
// no horizontal page overflow (screenshots in SQLEXPORT_SHOTS when set).
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { load, open, pause } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const made = JSON.parse(execFileSync("python3", ["-c", `
import json, sys
sys.path.insert(0, 'test')
from myflames import schema_versions as sv, schema_export, schema_browser
from test_table_structure import Replay, recorded
snap = sv.validate_snapshot(json.load(open('test/fixtures/schema_export/xp_src-mysql-8.4.json')))
item = {'id': 'e' * 32, 'saved_at': '2026-10-31T12:00:00Z', 'source_key': sv.source_key(snap), 'number': 1, 'name': 'release',
        'note': ''}
item.update(sv.version_summary(snap))
exports = {}
for kind in ('version', 'live'):
    meta = {'kind': 'version', 'id': item['id'], 'number': 1, 'name': 'release'} if kind == 'version' else {'kind': 'live'}
    for database in ('xp_src', 'copy'):
        for create in (False, True):
            for keep in (False, True):
                out = schema_export.build(snap, database=database, create_database=create, keep_definers=keep, meta=meta,
                                          generated_at='2026-10-31T12:00:00+00:00')
                out.pop('statements')
                out['kind'], out['server'] = kind, snap['server']
                if kind == 'live':
                    out['export_id'] = 'f' * 32
                exports['%s:%s:%s:%s' % (kind, database, create, keep)] = out
structure = schema_browser.table_structure(Replay(recorded('mysql-8.4')['customers']['calls']), 'xp_src', 'customers')
print(json.dumps({'item': item, 'snapshot': snap, 'exports': exports, 'structure': structure}))
`], { cwd: root }).toString());

const shotsDir = process.env.SQLEXPORT_SHOTS || null;
if (shotsDir) await fs.mkdir(shotsDir, { recursive: true });
const catalog = { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.13", user_tables: 12, all_limit: 2000, all_note: "", system: [],
  schemas: [{ name: "xp_src", tables: 11, system: false }, { name: "mftab", tables: 6, system: false }, { name: "mysql", tables: 38, system: true }] };
const tables = await load("tables-mysql-8.4.json");
const replies = { exportSql: [], structure: [] };
const exportReply = (body) => {
  const kind = body.id ? "version" : "live";
  return replies.exportSql.shift() || { status: 200, delay: 150,
    body: made.exports[`${kind}:${body.database}:${body.create_database ? "True" : "False"}:${body.keep_definers ? "True" : "False"}`] };
};
const structureReply = (body) => replies.structure.shift() || { status: 200, delay: 150,
  body: { ...made.structure, schema: body.schema, table: body.table, filename: `${body.schema}.${body.table}.sql`,
    foreign_keys: made.structure.foreign_keys.map((k) => ({ ...k, referenced_schema: body.schema })) } };

const h = await open("/api/live/tables", "schema-export", {
  "/api/live/schemas": { status: 200, body: catalog },
  "/api/profiles/list": { status: 200, body: { items: [], secret_store: { backend: "vault", label: "encrypted vault", available: true } } },
  "/api/schema-versions/list": { status: 200, body: { schema_version: "schema-version-list-1.0", items: [made.item] } },
  "/api/schema-versions/get": { status: 200, body: { ...made.item, snapshot: made.snapshot } },
  "/api/schema-versions/lint": { status: 200, body: { schema_version: "schema-lint-1.0", findings: [], not_judged: [] } },
  "/api/schema-versions/export-sql": exportReply,
  "/api/live/table-structure": structureReply,
}, { list: "docked", fallback: { status: 200, body: tables } });
const { page, state, errors } = h;
const sent = (pathname) => state.other.filter((r) => r.path === pathname).map((r) => r.body);
const sheet = "dialog.text-sheet[open]";
const sheetText = () => page.$eval(sheet, (e) => e.textContent);
const closed = () => page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
const idleSheet = () => page.waitForFunction((s) => {
  const d = document.querySelector(s);
  return d && !d.querySelector(".action-status[data-phase=pending]");
}, {}, sheet);

/** Both appearances at the desktop window and the 720 px minimum: the sheet
 * stays inside the window and the page never scrolls sideways. */
async function review(name) {
  const viewport = page.viewport();
  for (const [width, height] of [[1440, 900], [720, 600]]) for (const theme of ["light", "dark"]) {
    await page.setViewport({ width, height });
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(150);
    const box = await page.$eval(sheet, (d) => {
      const r = d.getBoundingClientRect();
      const actions = d.querySelector(".sheet-actions").getBoundingClientRect();
      return { left: r.left, right: r.right, bottom: r.bottom, actionsBottom: actions.bottom,
        overflow: document.documentElement.scrollWidth - innerWidth, inner: innerWidth, innerHeight };
    });
    assert.equal(box.overflow, 0, `${name} ${width} ${theme}: no page overflow`);
    assert.ok(box.left >= 0 && box.right <= box.inner, `${name} ${width}: the sheet fits the window`);
    assert.ok(box.actionsBottom <= box.innerHeight, `${name} ${width}: the actions stay in view`);
    if (shotsDir) await page.screenshot({ path: path.join(shotsDir, `${name}-${width}-${theme}.png`) });
  }
  await page.setViewport(viewport);
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

/** The view itself (no sheet) at both widths and appearances: no page overflow. */
async function reviewPage(name, selector) {
  const viewport = page.viewport();
  for (const [width, height] of [[1440, 900], [720, 600]]) for (const theme of ["light", "dark"]) {
    await page.setViewport({ width, height });
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(150);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, `${name} ${width}: no page overflow`);
    if (await page.$(selector)) {
      assert.equal(await page.$eval(selector, (e) => { const r = e.getBoundingClientRect(); return r.right <= innerWidth + 0.5; }), true,
        `${name} ${width}: ${selector} inside the window`);
    }
    if (shotsDir) await page.screenshot({ path: path.join(shotsDir, `${name}-${width}-${theme}.png`) });
  }
  await page.setViewport(viewport);
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
}

try {
  // ---- Export as SQL from a saved version -----------------------------------
  await page.click('.main-nav button[data-place="schema"]');
  await page.waitForSelector(`.schema-versions-view tr[data-row-key="${made.item.id}"]`);
  await page.click(`.schema-versions-view tr[data-row-key="${made.item.id}"] td[data-col="database"]`);
  const inspectorExport = '.view-inspector .schema-inspector-actions [data-action="schema-export-sql"]';
  await page.waitForSelector(inspectorExport);
  assert.equal(sent("/api/schema-versions/export-sql").length, 0, "nothing is exported until asked");
  await reviewPage("saved-versions", inspectorExport);
  await page.waitForSelector(inspectorExport, { visible: true });
  await page.click(inspectorExport);
  await page.waitForSelector(sheet);
  await page.waitForFunction((s) => /-- myflames schema export/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  assert.deepEqual(sent("/api/schema-versions/export-sql")[0],
    { id: made.item.id, database: "xp_src", create_database: false, keep_definers: false });
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "xp_src · Version 1 · release");
  assert.match(await page.$eval(`${sheet} .schema-export-summary`, (e) => e.textContent),
    /^\d+ statements for xp_src · 11 tables, 3 views, 1 procedure, 1 function, 3 triggers, 1 event · no warnings$/);
  assert.equal(await page.$eval(`${sheet} #schema-export-name`, (e) => e.value), "xp_src");
  assert.equal(await page.$(`${sheet} #schema-export-source`), null, "a saved version has no database to read");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Close", "Close is focused first");
  const script = () => page.$eval(`${sheet} .schema-script`, (e) => e.textContent);
  assert.doesNotMatch(await script(), /CREATE DATABASE/);
  assert.match(await script(), /USE `xp_src`;/);
  await review("export-version");

  // Create the database: the script is rebuilt with CREATE DATABASE.
  await page.locator(`${sheet} label.checkbox ::-p-text(Create the database)`).click();
  await page.waitForFunction((s) => /CREATE DATABASE `xp_src`/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  assert.equal(sent("/api/schema-versions/export-sql").at(-1).create_database, true);
  // Another name: applied on Enter, views are written for it.
  await page.click(`${sheet} #schema-export-name`, { clickCount: 3 });
  await page.type(`${sheet} #schema-export-name`, "copy");
  await page.keyboard.press("Enter");
  await page.waitForFunction((s) => /USE `copy`;/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  assert.deepEqual(sent("/api/schema-versions/export-sql").at(-1),
    { id: made.item.id, database: "copy", create_database: true, keep_definers: false });
  assert.match(await script(), /VIEW `copy`\.`v_orders`/);
  // Keep definers.
  await page.locator(`${sheet} label.checkbox ::-p-text(Keep definers)`).click();
  await page.waitForFunction((s) => /DEFINER=`root`@`%`/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  await idleSheet();
  // Review before running holds the notes Python wrote.
  assert.match(await page.$eval(`${sheet} .disclosure`, (e) => e.textContent), /keep their DEFINER/);
  // Copy script and Download .sql say what happened beside the actions.
  await page.locator(`${sheet} .sheet-actions button ::-p-text(Copy script)`).click();
  await page.waitForFunction((s) => /Copied\.|Select the text/.test(document.querySelector(`${s} .text-sheet-status`)?.textContent || ""), {}, sheet);
  await page.locator(`${sheet} .sheet-actions button ::-p-text(Download .sql)`).click();
  await page.waitForFunction((s) => document.querySelector(`${s} .text-sheet-status`)?.textContent === "Saved schema-xp_src-v1.sql.", {}, sheet);
  // A failed rebuild: the error once, with Try again; the old script greyed.
  replies.exportSql.push({ status: 400, body: { error: "Could not complete the request: test failure." } });
  await page.locator(`${sheet} label.checkbox ::-p-text(Keep definers)`).click();
  await page.waitForSelector(`${sheet} .action-status[data-phase=failed]`);
  assert.equal(await page.$$eval(`${sheet} .action-status[data-phase=failed]`, (els) => els.length), 1);
  assert.match(await page.$eval(`${sheet} .action-status`, (e) => e.textContent), /test failure\.Try again/);
  assert.equal(await page.$eval(`${sheet} .schema-export-result`, (e) => e.classList.contains("is-stale")), true);
  assert.equal(await page.$eval(`${sheet} .sheet-actions button.primary`, (b) => b.disabled), true, "a stale script cannot be opened");
  await page.locator(`${sheet} .action-status button ::-p-text(Try again)`).click();
  await page.waitForFunction((s) => !/DEFINER=`root`/.test(document.querySelector(`${s} .schema-script`)?.textContent || "x")
    && !document.querySelector(`${s} .schema-export-result.is-stale`), {}, sheet);
  // Escape closes the sheet and returns focus to Export as SQL….
  await page.keyboard.press("Escape");
  await closed();
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.action), "schema-export-sql");

  // ---- Export as SQL of a database read now --------------------------------
  const before = sent("/api/schema-versions/export-sql").length;
  await page.click('.schema-versions-view .view-toolbar [data-action="schema-export-sql"]');
  await page.waitForSelector(sheet);
  await page.waitForFunction((s) => /-- myflames schema export/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  const live = sent("/api/schema-versions/export-sql")[before];
  assert.equal(live.live.database, "xp_src", "the database the connection in use offers first");
  assert.equal(live.live.connection.host, "127.0.0.1", "read through the connection in use");
  assert.deepEqual(await page.$$eval(`${sheet} #schema-export-source option`, (os) => os.map((o) => o.value)), ["xp_src", "mftab"],
    "no system databases");
  assert.match(await page.$eval(`${sheet} .sheet-lead`, (e) => e.textContent), /Reads xp_src through .*, read-only\./);
  await page.locator(`${sheet} label.checkbox ::-p-text(Keep definers)`).click();
  await page.waitForFunction((s) => /DEFINER=`root`@`%`/.test(document.querySelector(`${s} .schema-script`)?.textContent || ""), {}, sheet);
  const again = sent("/api/schema-versions/export-sql").at(-1);
  assert.deepEqual(again, { export_id: "f".repeat(32), database: "xp_src", create_database: false, keep_definers: true },
    "a changed option rebuilds from the kept read, never reading the server again");
  await review("export-live");
  // Open in Query editor: a new tab with the script, nothing runs.
  await page.locator(`${sheet} .sheet-actions button.primary ::-p-text(Open in Query editor)`).click();
  await closed();
  await page.waitForFunction(() => /-- myflames schema export/.test(document.querySelector("textarea.sql-editor")?.value || ""));
  assert.equal(state.other.filter((r) => r.path.startsWith("/api/editor/run")).length, 0, "nothing runs");

  // ---- Table structure (Server > Tables) -----------------------------------
  await h.server();
  await h.view("tables");
  const view = ".server-view[data-view=tables]";
  await page.waitForSelector(`${view} tr[data-table="mftab.no_key"]`);
  await h.select(`${view} tr[data-table="mftab.no_key"]`);
  const action = `.view-inspector [data-action="table-structure"]`;
  await page.waitForSelector(action);
  await reviewPage("tables-inspector", action);
  if (!(await page.$(action))) await h.select(`${view} tr[data-table="mftab.no_key"]`);
  assert.equal(sent("/api/live/table-structure").length, 0, "nothing is read until asked");
  await page.click(action);
  await page.waitForSelector(sheet);
  await page.waitForSelector(`${sheet} .table-structure-table`);
  assert.deepEqual(sent("/api/live/table-structure")[0].schema, "mftab");
  assert.deepEqual(sent("/api/live/table-structure")[0].table, "no_key");
  assert.equal(sent("/api/live/table-structure")[0].connection.host, "127.0.0.1");
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "mftab.no_key");
  assert.match(await page.$eval(`${sheet} .sheet-lead`, (e) => e.textContent),
    /^InnoDB · utf8mb4_bin · 5 columns, 3 indexes, 1 foreign key · Read from .*\. Nothing on the server changes\.$/);
  const cells = (row) => page.$$eval(`${sheet} .table-structure-table tbody tr:nth-child(${row}) :is(th, td)`, (els) => els.map((e) => e.textContent));
  assert.deepEqual(await cells(2), ["email", "varchar(255)", "No", "—", "—", "utf8mb4 / utf8mb4_bin", "login ação"]);
  assert.deepEqual(await cells(1), ["idPK", "int unsigned", "No", "—", "auto_increment", "—", "—"]);
  await review("structure-columns");
  await page.click("#table-structure-tab-indexes");
  await page.waitForFunction((s) => /uq_email/.test(document.querySelector(`${s} .table-structure-panel`)?.textContent || ""), {}, sheet);
  assert.deepEqual(await cells(1), ["PRIMARY", "id", "Primary key", "BTREE", "Yes", "—"]);
  await page.click("#table-structure-tab-foreign_keys");
  await page.waitForFunction((s) => /fk_referrer/.test(document.querySelector(`${s} .table-structure-panel`)?.textContent || ""), {}, sheet);
  assert.deepEqual(await cells(1), ["fk_referrer", "referrer_id", "customers (id)", "NO ACTION", "SET NULL"]);
  await page.click("#table-structure-tab-create");
  await page.waitForSelector(`${sheet} pre.table-structure-ddl`);
  assert.match(await page.$eval(`${sheet} pre.table-structure-ddl`, (e) => e.textContent), /^CREATE TABLE `customers` \(/);
  await review("structure-create");
  await page.locator(`${sheet} .sheet-actions button ::-p-text(Copy CREATE TABLE)`).click();
  await page.waitForFunction((s) => /Copied\.|Select the text/.test(document.querySelector(`${s} .text-sheet-status`)?.textContent || ""), {}, sheet);
  await page.locator(`${sheet} .sheet-actions button.primary ::-p-text(Download .sql)`).click();
  await page.waitForFunction((s) => document.querySelector(`${s} .text-sheet-status`)?.textContent === "Saved mftab.no_key.sql.", {}, sheet);
  assert.equal(sent("/api/live/table-structure").length, 1, "one read per sheet");
  await page.keyboard.press("Escape");
  await closed();
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.action), "table-structure", "focus returns to Table structure");

  // The row's ⋯ menu.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  const trigger = `${view} tr[data-table="mftab.func_uk"] .row-more`;
  await page.waitForSelector(trigger);
  await page.$eval(trigger, (b) => { b.scrollIntoView({ block: "nearest", inline: "nearest" }); b.focus(); b.click(); });
  await page.waitForSelector(".row-action-menu [role=menuitem]");
  assert.equal(await page.$eval(".row-action-menu [role=menuitem]", (e) => e.textContent.trim()), "Table structure",
    "Table structure leads the row menu");
  await page.$eval(".row-action-menu [role=menuitem]", (e) => { e.focus(); e.click(); });
  await page.waitForSelector(sheet);
  await page.waitForFunction((s) => document.querySelector(`${s} h2`)?.textContent === "mftab.func_uk", {}, sheet);
  await idleSheet();
  await page.keyboard.press("Escape");
  await closed();
  assert.equal(await page.$eval(trigger, (b) => b === document.activeElement), true, "focus returns to the row's ⋯ button");

  // The context menu, and a failed read with Try again.
  replies.structure.push({ status: 400, body: { error: "Could not read mftab.mixed_uk: denied." } });
  const row = await page.$(`${view} tr[data-table="mftab.mixed_uk"]`);
  await row.click({ button: "right" });
  await page.waitForSelector("[role=menu] [role=menuitem]");
  await page.locator("[role=menu] [role=menuitem] ::-p-text(Table structure)").click();
  await page.waitForSelector(`${sheet} .action-status[data-phase=failed]`);
  assert.match(await sheetText(), /Could not read mftab\.mixed_uk: denied\./);
  assert.equal(await page.$eval(`${sheet} .sheet-actions button.primary`, (b) => b.disabled), true);
  await page.locator(`${sheet} .action-status button ::-p-text(Try again)`).click();
  await page.waitForSelector(`${sheet} .table-structure-table`);
  await page.keyboard.press("Escape");
  await closed();

  assert.deepEqual(errors, [], "no page errors");
  console.log("schema-export: Export as SQL (saved version and database read now, options, kept read, Copy, Download, Open in Query editor, failure and Try again) and Table structure (inspector, row menu, context menu, tabs, Copy, Download, failure and Try again) at 1440 and 720 px, light and dark, passed.");
} finally {
  await h.close();
}
