// DataTable (kit.tsx) through the Indexes and Sessions views: every column
// resizes with the pointer and the keyboard (a left column here), hides and
// shows from the Columns menu (never the last visible one), Reset restores
// the defaults, the layout is kept with the workspace across a reload (and a
// corrupt stored layout is ignored), sorting does not depend on which
// columns are visible, large counts are compact with the exact value as the
// tooltip, and a 2,000-row table draws only the rows in view while the
// keyboard still reaches every row.
// Run: npm run test:datatable (builds first) or node test/datatable.mjs
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const indexes = await load("indexes-mysql-8.4.json");
// The maintainer's screenshot: a table read through billions of rows.
indexes.full_scans[0] = { ...indexes.full_scans[0], rows_without_index: 158955464549, rows_with_index: 1782861982 };
const catalog = await load("schemas-mysql-8.4.json");
const sessions = await load("sessions-mysql-8.4-active.json");
const ROWS = 2000;
const base = sessions.sessions.find((s) => s.status === "ok") || sessions.sessions[0];
const many = {
  ...sessions,
  sessions: Array.from({ length: ROWS }, (_, i) => ({
    ...base,
    id: 10000 + i,
    time_s: i,
    status: "ok",
    flags: [],
    trx: null,
    lock_wait: null,
  })),
};
let sessionReport = many;

const h = await open("/api/live/indexes", "datatable", {
  "/api/live/schemas": { status: 200, body: catalog },
  "/api/live/sessions": () => ({ status: 200, body: sessionReport }),
});
const { page, state } = h;
state.fallback = { status: 200, body: indexes };
const view = ".server-view[data-view=indexes]";
// Column work on the Unused list (Verdict, Table, Index: Index takes the
// room left); numbers on the Full scans list.
const unused = `${view} [aria-label="Unused indexes"]`;
const scans = `${view} [aria-label="Tables read without an index"]`;
const header = (key, t = unused) => `${t} th[data-col="${key}"]`;
const width = (key, t = unused) => page.$eval(header(key, t), (e) => Math.round(e.getBoundingClientRect().width));
const separator = (key, t = unused) => `${header(key, t)} [role=separator]`;
const visibleKeys = (t = unused) => page.$$eval(`${t} thead th[data-col]`, (els) => els.map((e) => e.dataset.col));
const order = (t = unused) => page.$$eval(`${t} tbody tr[data-row-key]`, (els) => els.map((e) => e.dataset.rowKey));
const dialog = '[role=dialog][aria-label="Columns of Unused indexes"]';
const menu = async () => {
  await page.click(`${unused} .table-columns-button`);
  await page.waitForSelector(dialog);
};
const closeMenu = async () => {
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label^="Columns of"]'));
};
const toggle = (key) => page.click(`${dialog} input[data-col="${key}"]`);
const list = (name) => page.click(`${view} .segmented button[data-value=${name}]`);
const loadIndexes = async () => {
  await h.server();
  await h.view("indexes");
  await h.refresh();
  await page.waitForSelector(`${view} .fact-strip`);
  await h.idle(view);
  await list("unused");
  await page.waitForSelector(`${unused} tbody tr`);
};

try {
  await loadIndexes();

  // Compact numbers: billions read as "159.0B"; the exact value is the tooltip
  // and the accessible name; the inspector shows the exact value too.
  await list("scans");
  const cell = `${scans} tr[data-table="orders"] td[data-col="rows"] abbr`;
  await page.waitForSelector(cell);
  assert.equal(await page.$eval(cell, (e) => e.textContent), "159.0B");
  assert.equal(await page.$eval(cell, (e) => e.title), "158,955,464,549");
  assert.equal(await page.$eval(`${scans} tr[data-table="orders"] td[data-col="with"] abbr`, (e) => e.textContent), "1.78B");
  const nameBefore = await width("table", scans);
  await h.select(`${scans} tr[data-table="orders"]`);
  assert.match(await h.inspector(), /158,955,464,549/);
  // Return to the list before measuring its columns. Names keep 220 px
  // beside flags (or their whole width when less); number columns scroll
  // sideways inside the frame instead.
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  const named = await page.$eval(scans, (t) => {
    let flags = 0;
    for (const cell of t.querySelectorAll('tbody td[data-col="table"]')) {
      let sum = 0;
      cell.querySelectorAll(".pill").forEach((p) => (sum += p.getBoundingClientRect().width + 8));
      flags = Math.max(flags, sum);
    }
    // The name beside the flag in the widest row.
    const names = [...t.querySelectorAll('tbody td[data-col="table"] .cell-name > :not(.pill)')];
    return {
      width: Math.round(t.querySelector('th[data-col="table"]').getBoundingClientRect().width),
      flags: Math.ceil(flags),
      clipped: names.filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => [e.textContent, e.clientWidth]),
    };
  });
  assert.ok(named.flags > 0, "a row has the Mostly full scans flag");
  // Every name is whole, or has at least 220 px of column beside its flags.
  assert.ok(!named.clipped.length || named.width >= 220 + named.flags - 1, JSON.stringify({ ...named, nameBefore }));
  await page.keyboard.press("Escape");
  // Name and flag stay on one line: every row has the same height.
  const heights = await page.$$eval(`${scans} tbody tr[data-row-key]`, (els) => els.map((e) => Math.round(e.getBoundingClientRect().height)));
  assert.equal(new Set(heights).size, 1, `one row height, got ${heights}`);
  // Pointer: drag the handle of a number column 60 px to the right.
  const rowsBefore = await width("rows", scans);
  const box = await (await page.$(separator("rows", scans))).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 30, box.y + box.height / 2, { steps: 3 });
  await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2, { steps: 3 });
  await page.mouse.up();
  assert.equal(await width("rows", scans), rowsBefore + 60);
  // Numbers never truncate: Home takes the column to its minimum, the widest value.
  await page.focus(separator("rows", scans));
  await page.keyboard.press("Home");
  assert.ok((await page.$$eval(`${scans} td[data-col="rows"]`, (els) => Math.max(...els.map((e) => e.scrollWidth - e.clientWidth)))) <= 0, "numbers fit");
  await list("unused");
  await page.waitForSelector(`${unused} tbody tr`);

  // Every header has a resize handle with its value, minimum and maximum.
  const handles = await page.$$eval(`${unused} thead [role=separator]`, (els) =>
    els.map((e) => [e.getAttribute("aria-label"), +e.getAttribute("aria-valuenow"), +e.getAttribute("aria-valuemin"), +e.getAttribute("aria-valuemax"), e.tabIndex]));
  assert.deepEqual(handles.map((x) => x[0]), ["Width of Verdict", "Width of Table", "Width of Index"]);
  for (const [label, now, min, max, tab] of handles)
    assert.ok(min <= now && now <= max && tab === 0, `${label}: ${now} in [${min}, ${max}]`);

  // Keyboard on the left column: Right +16, Shift+Right +64, Left -16, Home to the minimum.
  const start = await width("verdict");
  await page.focus(separator("verdict"));
  await page.keyboard.press("ArrowRight");
  assert.equal(await width("verdict"), start + 16);
  await page.keyboard.down("Shift");
  await page.keyboard.press("ArrowRight");
  await page.keyboard.up("Shift");
  assert.equal(await width("verdict"), start + 80);
  await page.keyboard.press("ArrowLeft");
  assert.equal(await width("verdict"), start + 64);
  assert.equal(await page.$eval(separator("verdict"), (e) => +e.getAttribute("aria-valuenow")), start + 64);
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "separator", "focus stays on the handle");
  const min = await page.$eval(separator("verdict"), (e) => +e.getAttribute("aria-valuemin"));
  await page.keyboard.press("Home");
  assert.equal(await width("verdict"), min);
  await page.keyboard.press("ArrowRight");
  assert.equal(await width("verdict"), min + 16);
  // The column that takes the room left (Index) still fills the frame.
  const frame = await page.$eval(`${view} .data-table-wrap[aria-label="Unused indexes"]`, (e) => e.clientWidth);
  assert.equal(await page.$eval(`${unused} table`, (e) => Math.round(e.getBoundingClientRect().width)), frame);

  // The layout is saved with the workspace, under its table id.
  const saved = await h.savedSlow((s) => s.columns_v1?.["indexes.unused"]?.widths?.verdict === min + 16);
  assert.equal(saved.state.query_lab.slow.columns_v1["indexes.scans"].widths.rows >= 48, true);

  // Sort by Table, then hide Table: the order stays.
  await page.click(`${header("table")} .sort-button`);
  assert.equal(await page.$eval(header("table"), (e) => e.getAttribute("aria-sort")), "ascending");
  await page.click(`${header("table")} .sort-button`);
  assert.equal(await page.$eval(header("table"), (e) => e.getAttribute("aria-sort")), "descending");
  const sorted = await order();
  await menu();
  const names = await page.$$eval(`${dialog} label`, (els) => els.map((e) => e.textContent.trim()));
  // Row actions are the ⋯ menu in the trailing column, not a column to hide.
  assert.deepEqual(names, ["Verdict", "Table", "Index"]);
  await toggle("table");
  assert.deepEqual(await visibleKeys(), ["verdict", "index"]);
  assert.deepEqual(await order(), sorted, "hiding the sorted column keeps the order");
  // The hidden column is still in the menu, unchecked, and comes back.
  assert.equal(await page.$eval(`${dialog} input[data-col="table"]`, (e) => e.checked), false);
  await toggle("table");
  assert.deepEqual(await visibleKeys(), ["verdict", "table", "index"]);
  // The last visible column cannot be hidden.
  await toggle("table");
  await toggle("index");
  assert.deepEqual(await visibleKeys(), ["verdict"]);
  assert.equal(await page.$eval(`${dialog} input[data-col="verdict"]`, (e) => e.disabled), true);
  await page.$eval(`${dialog} input[data-col="verdict"]`, (e) => e.click());
  assert.deepEqual(await visibleKeys(), ["verdict"], "the last column stays");
  await closeMenu();
  const hiddenSaved = await h.savedSlow((s) => (s.columns_v1?.["indexes.unused"]?.hidden || []).length === 2);
  assert.deepEqual(hiddenSaved.state.query_lab.slow.columns_v1["indexes.unused"].hidden.sort(), ["index", "table"]);

  // Kept across a reload: the hidden columns and the Verdict width come back.
  await page.reload({ waitUntil: "networkidle0" });
  await loadIndexes();
  assert.deepEqual(await visibleKeys(), ["verdict"]);
  await menu();
  await toggle("table");
  await toggle("index");
  assert.deepEqual(await visibleKeys(), ["verdict", "table", "index"]);
  assert.equal(await width("verdict"), min + 16);
  // Reset: default widths and every column, and the stored entry is gone.
  await page.click(`${dialog} .columns-reset`);
  await closeMenu();
  await h.savedSlow((s) => !s.columns_v1?.["indexes.unused"]);
  assert.equal(await width("verdict"), start, "the default width again");

  // A corrupt stored layout: unknown tables and columns are dropped, widths
  // clamped, non-numbers ignored, and hiding every column shows them all.
  const token = await page.evaluate(async () => (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.evaluate(async (t) => {
    const call = (route, body) => fetch(route, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(body) }).then((r) => r.json());
    const loaded = await call("/api/session/load", {});
    const s = loaded.state;
    s.query_lab.slow.kind = "indexes";
    s.query_lab.slow.columns_v1 = {
      "indexes.unused": { widths: { verdict: 99999, table: "wide", nope: 300, index: -5 }, hidden: ["verdict", "table", "index", "actions", "ghost"] },
      "not.a.table": { widths: { a: 100 } },
      constructor: { hidden: ["table"] },
    };
    await call("/api/session/save", { revision: loaded.revision, client_id: "corrupt-layout", seq: 1, state: s });
  }, token);
  await page.reload({ waitUntil: "networkidle0" });
  await loadIndexes();
  assert.deepEqual(await visibleKeys(), ["verdict", "table", "index"], "zero visible columns is never restored");
  assert.equal(await page.$eval(separator("verdict"), (e) => +e.getAttribute("aria-valuenow")), 1200, "clamped to the maximum");
  // The table scrolls sideways inside its frame; the page never does.
  assert.ok(await page.$eval(`${view} .data-table-wrap[aria-label="Unused indexes"]`, (e) => e.scrollWidth > e.clientWidth));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), 0);
  // The page keeps working: sort.
  await page.click(`${header("table")} .sort-button`);
  assert.equal(await page.$eval(header("table"), (e) => e.getAttribute("aria-sort")), "ascending");
  await page.focus(separator("verdict"));
  await page.keyboard.press("Home");
  const normalized = await h.savedSlow((s) => s.columns_v1?.["indexes.unused"]?.widths?.verdict < 1200);
  assert.deepEqual(Object.keys(normalized.state.query_lab.slow.columns_v1), ["indexes.unused"], "unknown table ids are dropped");
  await page.screenshot({ path: path.join(h.shots, "datatable-indexes.png") });
  await menu();
  await page.click(`${dialog} .columns-reset`);
  await closeMenu();

  // 2,000 sessions: only the rows in view are drawn; the keyboard reaches every row.
  const sv = ".server-view[data-view=sessions]";
  await h.view("sessions");
  await page.click(`${sv} [data-action=refresh]`);
  await page.waitForSelector(`${sv} tbody tr[data-session]`);
  const drawn = await page.$$eval(`${sv} tbody tr[data-session]`, (els) => els.length);
  assert.ok(drawn > 10 && drawn < 200, `windowed: ${drawn} rows drawn of ${ROWS}`);
  assert.equal(await page.$eval(`${sv} table.data-table`, (e) => e.getAttribute("aria-rowcount")), String(ROWS + 1));
  const first = await page.$eval(`${sv} tbody tr[data-session]`, (e) => e.dataset.session);
  const retainedTable = await page.$(`${sv} table.data-table`);
  const frameBefore = await page.$eval(`${sv} .data-table-wrap`, (e) => e.clientWidth);
  const idWidthBefore = await width("id", sv);
  await page.click(`${sv} tbody tr[data-session="${first}"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.equal(await page.$eval(`${sv} .data-table-wrap`, (e) => e.clientWidth), 0, "full-width details hide the retained table");
  // A refresh changes the other rows while the selected row keeps details
  // open. Returning at the same window width must still grow numeric minima.
  sessionReport = { ...many, sessions: many.sessions.map((s, i) => ({ ...s, id: String(s.id) === first ? s.id : 9000000000000000 - i })) };
  await page.click(`${sv} [data-action=refresh]`);
  await page.waitForFunction((sel) => [...document.querySelectorAll(`${sel} tbody td[data-col="id"]`)]
    .some((e) => /\d{16}/.test(e.textContent)), {}, sv);
  assert.equal(await page.$eval(`${sv} .data-table-wrap`, (e) => e.clientWidth), 0, "refresh leaves details open");
  await page.click(".detail-back");
  await page.waitForFunction((sel, before) => document.querySelector(`${sel} th[data-col="id"]`).getBoundingClientRect().width > before, {}, sv, idWidthBefore);
  assert.equal(await page.evaluate((table, sel) => table === document.querySelector(`${sel} table.data-table`), retainedTable, sv), true, "the table was not remounted");
  assert.equal(await page.$eval(`${sv} .data-table-wrap`, (e) => e.clientWidth), frameBefore, "same frame width after Back");
  assert.ok(await page.$$eval(`${sv} tbody td[data-col="id"]`, (cells) => cells.every((e) => e.scrollWidth <= e.clientWidth)), "larger IDs fit after hidden refresh");
  await retainedTable.dispose();
  sessionReport = many;
  await page.click(`${sv} [data-action=refresh]`);
  await page.waitForFunction((sel) => [...document.querySelectorAll(`${sel} tbody td[data-col="id"]`)]
    .every((e) => !/\d{16}/.test(e.textContent)), {}, sv);
  await page.focus(`${sv} tbody tr[data-session="${first}"]`);
  await page.keyboard.press("End");
  const ids = many.sessions.map((s) => s.id).sort((a, b) => a - b);
  const expectedLast = first === String(ids[0]) ? String(ids.at(-1)) : String(ids[0]);
  await page.waitForFunction((k) => document.activeElement?.dataset?.session === k, {}, expectedLast);
  const lastKey = await page.evaluate(() => document.activeElement.dataset.session);
  assert.equal(lastKey, expectedLast, `End reaches the opposite end (${lastKey})`);
  assert.equal(await page.evaluate(() => document.activeElement.getAttribute("aria-current")), "true", "selection follows focus");
  await page.keyboard.press("Enter");
  assert.match(await h.inspector(), new RegExp(`Connection ${lastKey}`));
  await page.click(".detail-back");
  await page.waitForFunction((k) => document.activeElement?.dataset?.session === k, {}, lastKey);
  await page.keyboard.press("Home");
  await page.waitForFunction((k) => document.activeElement?.dataset?.session === k, {}, first);
  // PageDown and ArrowDown move through rows that were not drawn a moment ago.
  for (let i = 0; i < 5; i++) await page.keyboard.press("PageDown");
  await page.keyboard.press("ArrowDown");
  const deep = await page.evaluate(() => +document.activeElement.getAttribute("aria-rowindex"));
  assert.ok(deep > 60, `keyboard moved down to row ${deep}`);
  assert.ok(await page.$$eval(`${sv} tbody tr[data-session]`, (els) => els.length) < 200, "still windowed");
  // Enter opens the inspector for the focused row.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.focus(`${sv} [data-action=refresh]`);
  assert.equal(await page.$$eval(`${sv} tr.is-selected`, (els) => els.length), 1, "other tables retain their existing selection fill without focus or an inspector");
  await page.focus(`${sv} tbody tr[tabindex="0"]`);
  await page.keyboard.press("Enter");
  await page.waitForSelector(".view-inspector .inspector-title");

  // 390 px: no page scroll sideways.
  await h.narrow("datatable-390.png");
  assert.deepEqual(h.errors, []);
  console.log("datatable: ok");
} finally {
  await h.close();
}
