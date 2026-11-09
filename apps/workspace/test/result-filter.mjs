// Result filter, column chips, filtered totals/export/copy, and pin & compare:
// unit checks for resultFilter.ts plus browser verification in ResultsPane.tsx.
// Run after `npm run build`: node apps/workspace/test/result-filter.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { transform } from "esbuild";
import puppeteer from "puppeteer";

const tsPath = new URL("../src/resultFilter.ts", import.meta.url);
const tsSource = await fs.readFile(tsPath, "utf8");
const { code } = await transform(tsSource, { loader: "ts", format: "esm" });
const {
  MAX_PIN_ROWS,
  compareResults,
  defaultKeyColumn,
  exportFilterLabel,
  filterRows,
  filterSummary,
  matchesFilter,
  pinResult,
  pruneClosedTabPins,
  sameColumns,
  upsertChip,
} = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);

// ---- 1. Pure unit tests for resultFilter.ts ---------------------------------

assert.equal(filterSummary(12, 1000), "12 of 1,000 loaded rows");
assert.equal(exportFilterLabel(12), "Export 12 filtered rows");
assert.equal(exportFilterLabel(1), "Export 1 filtered row");

const sampleRows = [
  ["1", "Alice", null, "paid"],
  ["2", "Bob", "", "paid"],
  ["3", "Charlie", "NULL", "pending"],
  ["4", "alice_2", "note", "refunded"],
];

// Case-insensitive substring across all columns; SQL NULL is never matched as the text "null".
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "ALICE", []).map((r) => r[0]),
  ["1", "4"],
);
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "null", []).map((r) => r[0]),
  ["3"],
  "substring 'null' matches literal string 'NULL', never SQL NULL",
);

// Column-scoped chips: SQL NULL, empty string, and literal 'NULL' stay distinct.
const nullChip = upsertChip([], 2, "note", "show", null);
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "", nullChip).map((r) => r[0]),
  ["1"],
  "NULL chip matches only SQL NULL",
);

const emptyChip = upsertChip([], 2, "note", "show", "");
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "", emptyChip).map((r) => r[0]),
  ["2"],
  "empty-string chip matches only empty string",
);

const hideNullChip = upsertChip([], 2, "note", "hide", null);
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "", hideNullChip).map((r) => r[0]),
  ["2", "3", "4"],
  "Hide NULL keeps empty string and literal 'NULL'",
);

// Chips combine with the substring query.
const paidChip = upsertChip([], 3, "status", "show", "paid");
assert.deepEqual(
  filterRows(sampleRows, (r) => r, "bob", paidChip).map((r) => r[0]),
  ["2"],
);
assert.equal(matchesFilter(["1", "Alice", null, "paid"], "alice", paidChip), true);
assert.equal(matchesFilter(["1", "Alice", null, "pending"], "alice", paidChip), false);

// Pin caps at 5,000 loaded rows in memory.
const overRows = Array.from({ length: 5200 }, (_, i) => [String(i + 1), `row-${i + 1}`]);
const pinnedBig = pinResult({
  type: "rows",
  statement: 0,
  columns: [
    { name: "code", hex: false, type: "varchar" },
    { name: "label", hex: false, type: "varchar" },
  ],
  columns_known: true,
  rows: overRows,
  row_count: overRows.length,
  truncated: false,
  limit: 10000,
  stopped: null,
  label: null,
  server_rows: overRows.length,
  warnings: 0,
  duration_ms: 5,
});
assert.equal(MAX_PIN_ROWS, 5000);
assert.equal(pinnedBig.rows.length, 5000);
assert.equal(pinnedBig.loadedCount, 5200);
assert.equal(pinnedBig.capped, true);
assert.equal("source" in pinnedBig, false, "a pin keeps only the capped copy, never the live result object");

// Pins are pruned by tab id when a query tab closes; nothing closed returns the same object.
const smallResult = {
  type: "rows",
  statement: 0,
  columns: [{ name: "id", hex: false, type: "int" }],
  columns_known: true,
  rows: [["1"]],
  row_count: 1,
  truncated: false,
  limit: 1000,
  stopped: null,
  label: null,
  server_rows: 1,
  warnings: 0,
  duration_ms: 1,
};
const pins = { tabA: pinResult(smallResult, "run-1", 1), tabB: pinResult(smallResult, "run-2", 1) };
assert.deepEqual(Object.keys(pruneClosedTabPins(pins, ["tabA"])), ["tabA"], "a closed tab's pin is dropped");
assert.equal(pruneClosedTabPins(pins, ["tabA", "tabB"]), pins, "nothing closed: the pins object is untouched");

// Key column preselection: 'id' (case-insensitive) else first column (0).
assert.equal(
  defaultKeyColumn([
    { name: "tenant", hex: false, type: null },
    { name: "id", hex: false, type: null },
    { name: "status", hex: false, type: null },
  ]),
  1,
);
assert.equal(
  defaultKeyColumn([
    { name: "code", hex: false, type: null },
    { name: "name", hex: false, type: null },
  ]),
  0,
);
assert.equal(
  sameColumns(
    [{ name: "id", hex: false, type: null }, { name: "status", hex: false, type: null }],
    [{ name: "id", hex: false, type: null }, { name: "status", hex: false, type: null }],
  ),
  true,
);
assert.equal(
  sameColumns(
    [{ name: "id", hex: false, type: null }],
    [{ name: "order_id", hex: false, type: null }],
  ),
  false,
);

// Compare results: added, removed, changed, unchanged, and NULL vs empty string cell diffs.
const cols = [
  { name: "tenant", hex: false, type: "varchar" },
  { name: "id", hex: false, type: "int" },
  { name: "status", hex: false, type: "varchar" },
  { name: "note", hex: false, type: "varchar" },
];
const diff = compareResults(
  {
    columns: cols,
    rows: [
      ["acme", "10", "open", null],
      ["acme", "20", "open", "keep"],
      ["acme", "30", "closed", "old"],
    ],
  },
  {
    columns: cols,
    rows: [
      ["acme", "10", "open", ""],
      ["acme", "20", "open", "keep"],
      ["acme", "40", "new", null],
    ],
  },
);
assert.equal(diff.keyCol, 1, "preselects column named id");
assert.equal(diff.added.length, 1);
assert.equal(diff.added[0].keyValue, "40");
assert.equal(diff.removed.length, 1);
assert.equal(diff.removed[0].keyValue, "30");
assert.equal(diff.changed.length, 1);
assert.equal(diff.changed[0].keyValue, "10");
assert.deepEqual(diff.changed[0].changedCols, [3], "NULL -> empty string is detected as a changed cell");
assert.equal(diff.unchanged, 1);
assert.equal(diff.summary, "1 added · 1 removed · 1 changed");
assert.equal(diff.ambiguous.length, 0);

// Duplicate keys: identical rows inside the repeated-key group pair off first,
// so row order is never reported as a change (the old FIFO pairing did).
const dupCols = [
  { name: "id", hex: false, type: "int" },
  { name: "v", hex: false, type: "varchar" },
];
const dup = compareResults(
  { columns: dupCols, rows: [["1", "a"], ["1", "b"]] },
  { columns: dupCols, rows: [["1", "b"], ["1", "a"]] },
  0,
);
assert.equal(dup.changed.length, 0, "reordered duplicate keys are not changes");
assert.equal(dup.unchanged, 2);
assert.equal(dup.ambiguous.length, 0, "fully paired duplicate groups need no ambiguous rows");
assert.equal(dup.added.length + dup.removed.length, 0);

// Duplicate keys with a real difference: the rows that cannot be paired are
// reported as ambiguous (with before/after kept), never as invented changes.
const dupDiff = compareResults(
  { columns: dupCols, rows: [["1", "a"], ["1", "b"]] },
  { columns: dupCols, rows: [["1", "a"], ["1", "c"]] },
  0,
);
assert.equal(dupDiff.unchanged, 1, "the identical row still pairs off");
assert.equal(dupDiff.changed.length, 0, "an ambiguous key must not invent a changed row");
assert.equal(dupDiff.ambiguous.length, 2);
assert.deepEqual(
  dupDiff.ambiguous.map((r) => [r.before?.[1] ?? null, r.after?.[1] ?? null]),
  [["b", null], [null, "c"]],
);
assert.equal(dupDiff.summary, "0 added · 0 removed · 0 changed · 2 ambiguous");

// NULL keys never match each other, not even two identical NULL-keyed rows.
const nullDiff = compareResults(
  { columns: dupCols, rows: [["7", "x"], [null, "a"]] },
  { columns: dupCols, rows: [["7", "x"], [null, "a"]] },
  0,
);
assert.equal(nullDiff.unchanged, 1, "only the unique non-NULL key pairs");
assert.equal(nullDiff.ambiguous.length, 2, "both NULL-keyed rows are ambiguous");
assert.ok(nullDiff.ambiguous.every((r) => r.keyValue === null));

// ---- 2. Browser verification ------------------------------------------------

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-result-filter-"));
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import myflames.ui as ui; import os,sys; from pathlib import Path; ui.ASSET_DIR=Path(os.environ.get('WORKSPACE_ASSETS', str(ui.ASSET_DIR))); s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

let runN = 0;
function session(over = {}) {
  return {
    schema_version: "editor-session-1.0",
    tab_id: "1",
    state: "idle",
    connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "development", read_only: false },
    engine: "mysql",
    server_version: "8.4.3",
    connection_id: 42,
    autocommit: true,
    in_transaction: false,
    transaction_source: "performance_schema",
    transaction_note: null,
    sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
    backslash_escapes: true,
    safe_updates: true,
    safe_updates_default: true,
    read_only_transactions: false,
    started_at: "2026-10-15T12:00:00+00:00",
    idle_seconds: 0,
    idle_timeout_seconds: 1800,
    ended: false,
    ended_reason: null,
    message: null,
    notes: [],
    run: null,
    ...over,
  };
}
function statement(index, text, start = 0, over = {}) {
  return { index, text, start, end: start + text.length, verb: "select", kind: "read", category: "read", status: "ok", duration_ms: 4, ...over };
}
function rows(statementIndex, columns, data, over = {}) {
  return {
    type: "rows",
    statement: statementIndex,
    columns: columns.map((c) => (typeof c === "string" ? { name: c, hex: false, type: null } : c)),
    columns_known: columns.length > 0,
    rows: data,
    row_count: data.length,
    truncated: false,
    limit: 1000,
    stopped: null,
    label: null,
    server_rows: data.length,
    warnings: 0,
    duration_ms: 4,
    ...over,
  };
}
function makeRun(statements, results, over = {}) {
  return {
    schema_version: "editor-run-1.0",
    id: `run-${++runN}`,
    kind: "run",
    state: "complete",
    scope: "selection",
    started_at: "2026-10-15T12:00:00+00:00",
    finished_at: "2026-10-15T12:00:00+00:00",
    duration_ms: 4,
    limit: 1000,
    statements,
    results,
    error: null,
    stopped: null,
    message: null,
    counts: {
      statements: statements.length,
      run: statements.length,
      rows: results.filter((r) => r.type === "rows").reduce((a, r) => a + r.row_count, 0),
      affected_rows: 0,
      warnings: 0,
      errors: 0,
    },
    audit_id: null,
    history_id: null,
    ...over,
  };
}

const queuedRuns = [];
const exportsSeen = [];

let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const m = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (m) {
        clearTimeout(timer);
        resolve(m[0]);
      }
    });
    server.on("error", reject);
  });

  const launchArgs = process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [];
  browser = await puppeteer.launch({ headless: "shell", args: launchArgs });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));

  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (p === "/api/editor/status")
      return request.respond({ status: 200, contentType: "application/json", body: '{"status":"none","session":null,"run":null}' });
    if (p === "/api/editor/preview")
      return request.respond({
        status: 200,
        contentType: "application/json",
        body: '{"status":"ready","statements":[{"index":0,"kind":"read","action_keys":[],"state":"allowed","missing":[],"reason":""}],"aggregate":{"state":"allowed","missing":[],"reason":"","warning":""},"policy":{"action":"allow","text":""}}',
      });
    if (p === "/api/live/schemas")
      return request.respond({
        status: 200,
        contentType: "application/json",
        body: '{"schema_version":"schemas-1.0","engine":"mysql","server_version":"8.4.3","taken_at":"x","uptime_s":1,"performance_schema":true,"schemas":[],"user_tables":0,"all_limit":2000,"all_note":"","system":[]}',
      });
    if (p === "/api/live/privileges")
      return request.respond({
        status: 200,
        contentType: "application/json",
        body: '{"schema_version":"privileges-1.0","account":"root@localhost","grants":[],"current_roles":[],"actions":{}}',
      });
    if (p === "/api/editor/run") {
      const next = queuedRuns.shift();
      return request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(next) });
    }
    if (p === "/api/editor/close")
      return request.respond({ status: 200, contentType: "application/json", body: '{"closed":true,"reason":"","message":"","session":null}' });
    if (p === "/api/editor/export") {
      const body = JSON.parse(request.postData() || "{}");
      exportsSeen.push(body);
      return request.respond({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          format: body.format,
          content: body.result.rows.map((r) => r.map((v) => v ?? "").join("\t")).join("\r\n"),
          mime: "text/tab-separated-values",
          filename: `myflames-result.${body.format}`,
        }),
      });
    }
    return request.continue();
  });

  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.evaluate(() =>
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: async (v) => {
          (window.__copied = window.__copied || []).push(v);
        },
      },
    }),
  );
  const copied = () => page.evaluate(() => (window.__copied || []).at(-1));
  const copyCount = () => page.evaluate(() => (window.__copied || []).length);
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  const text = (s) => page.$eval(s, (e) => e.textContent);
  const texts = (s) => page.$$eval(s, (es) => es.map((e) => e.textContent));
  async function chord(k) {
    await page.keyboard.down(MOD);
    await page.keyboard.press(k);
    await page.keyboard.up(MOD);
  }
  async function runSql(sql, replyRun) {
    queuedRuns.push({ status: "complete", session: session(), run: replyRun });
    await page.locator('[aria-label="SQL editor"]').fill(sql);
    await chord("Enter");
    await page.waitForFunction((id) => !document.querySelector(".results-running") && !!document.querySelector(".result-table"), {}, replyRun.id);
  }

  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector('[aria-label="SQL editor"]');

  // 2a. 1,000-row result: filter matches 12 rows -> summary reads "12 of 1,000 loaded rows"
  // and Export menu says "Export 12 filtered rows".
  const thousandRows = Array.from({ length: 1000 }, (_, i) => [
    String(i + 1),
    i < 12 ? `needle-match-${i + 1}` : `other-row-${i + 1}`,
    String((i + 1) * 10),
  ]);
  await runSql(
    "SELECT id, label, amount FROM items",
    makeRun([statement(0, "SELECT id, label, amount FROM items")], [rows(0, ["id", "label", "amount"], thousandRows)]),
  );

  const filterInput = '.result-toolbar input[aria-label="Filter loaded rows"]';
  await page.waitForSelector(filterInput);
  await page.type(filterInput, "NEEDLE-MATCH");
  await page.waitForFunction(() => document.querySelector(".result-filter-summary")?.textContent === "12 of 1,000 loaded rows");
  assert.equal(await text(".result-filter-summary"), "12 of 1,000 loaded rows");
  assert.equal((await page.$$(".result-table tbody tr[data-row-key]")).length, 12);

  // Selection totals on filtered rows: Alt+click on the amount column (c2) sums only the 12 filtered rows.
  // Sum of 10, 20, ..., 120 = 780.
  await page.keyboard.down("Alt");
  await page.click(".result-table th[data-col=c2] .sort-button");
  await page.keyboard.up("Alt");
  assert.equal(await text(".result-grid-footer"), "Count 12 · Sum 780 · Avg 65 · Min 10 · Max 120");

  // Export menu says "Export 12 filtered rows" and sends only the 12 filtered rows.
  assert.match(await text(".result-export button"), /Export 12 filtered rows/);
  await page.click('button[aria-label="Export result 1"]');
  await page.waitForSelector(".popover .popover-title");
  assert.equal(await text(".popover .popover-title"), "Export 12 filtered rows");
  await page.locator(".popover button ::-p-text(Copy as TSV)").click();
  await page.waitForFunction(() => (window.__copied || []).length >= 1);
  assert.equal(exportsSeen.length, 1);
  assert.equal(exportsSeen[0].result.rows.length, 12);

  // Escape in the filter input clears the filter immediately.
  await page.focus(filterInput);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".result-filter-summary"));
  assert.equal(await page.$eval(filterInput, (e) => e.value), "");

  // 2b. Cell context menu (right-click and Shift+F10 / ContextMenu key), chips, and NULL vs empty string.
  const distinctRows = [
    ["1", "paid", null, "100"],
    ["2", "paid", "", "200"],
    ["3", "paid", "NULL", "300"],
    ["4", "pending", "", "400"],
    ["5", "refunded", "note", "500"],
  ];
  await runSql(
    "SELECT region, status, note, amount FROM orders",
    makeRun([statement(0, "SELECT region, status, note, amount FROM orders")], [rows(0, ["region", "status", "note", "amount"], distinctRows)]),
  );

  const cell = (row, col) => `.result-table tbody tr[data-row-key="${row}"] td[data-col="c${col}"]`;
  const visibleRowKeys = () => page.$$eval(".result-table tbody tr[data-row-key]", (trs) => trs.map((tr) => tr.dataset.rowKey));

  // Selecting a range of cells (⌘A = 20 cells) and right-clicking inside the selection
  // keeps the whole selection (Copy selection (20 cells) stays in the menu, footer stays 20 cells),
  // and "Show only this value" acts on the right-clicked cell while keeping the selection unchanged.
  await page.focus('.result-table tbody tr[data-row-key="0"]');
  await chord("a");
  assert.match(await text(".result-grid-footer"), /^20 cells · /);
  await page.click(cell(0, 2), { button: "right" });
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  assert.deepEqual(
    (await texts('.context-menu[aria-label="Result actions"] [role=menuitem]')).map((t) =>
      t.replace(/(⌘C|Ctrl\+C|Return|Enter)$/, ""),
    ),
    [
      "Show row",
      "Select column (note)",
      "Show only this value",
      "Hide this value",
      "Copy selection (20 cells)",
      "Copy value (note)",
      "Copy row",
      "Copy column (note)",
      "Copy all rows with header",
    ],
    "right-click inside a multi-cell selection keeps the selection and includes both filter actions and Copy selection",
  );
  assert.match(await text(".result-grid-footer"), /^20 cells · /, "selection is unchanged on right-click inside it");
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Show only this value)').click();
  await page.waitForSelector(".result-filter-chip");
  assert.deepEqual(await visibleRowKeys(), ["0"], "SQL NULL chip matches only SQL NULL");
  assert.equal(await text(".result-filter-summary"), "1 of 5 loaded rows");
  assert.match(await text(".result-grid-footer"), /^4 cells · /, "multi-column selection remains active across the filtered row");

  // Remove the NULL chip via its remove button.
  await page.click(".result-filter-chip .result-chip-remove");
  await page.waitForFunction(() => !document.querySelector(".result-filter-chip"));
  assert.deepEqual(await visibleRowKeys(), ["0", "1", "2", "3", "4"]);
  assert.match(await text(".result-grid-footer"), /^20 cells · /, "full selection is still intact after removing the chip");

  // Right-click outside a 2-cell selection moves the selection to that single cell.
  await page.click(cell(0, 0));
  await page.keyboard.down("Shift");
  await page.click(cell(1, 0));
  await page.keyboard.up("Shift");
  assert.match(await text(".result-grid-footer"), /^Count 2 · /);
  await page.click(cell(0, 2), { button: "right" });
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  assert.equal(await text(".result-grid-footer"), "1 cell · 1 NULL", "right-click outside the selection moves selection to that cell");
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Show only this value)').click();
  await page.waitForSelector(".result-filter-chip");
  assert.deepEqual(await visibleRowKeys(), ["0"], "SQL NULL chip matches only SQL NULL");
  assert.equal(await text(".result-filter-summary"), "1 of 5 loaded rows");

  // Remove the NULL chip via its remove button.
  await page.click(".result-filter-chip .result-chip-remove");
  await page.waitForFunction(() => !document.querySelector(".result-filter-chip"));
  assert.deepEqual(await visibleRowKeys(), ["0", "1", "2", "3", "4"]);

  // Click row 1, col 2 (empty string "") and press Shift+F10 -> "Show only this value" matches ONLY rows 1 and 3 (""), not NULL.
  await page.click(cell(1, 2));
  await page.keyboard.down("Shift");
  await page.keyboard.press("F10");
  await page.keyboard.up("Shift");
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Show only this value)').click();
  await page.waitForSelector(".result-filter-chip");
  assert.deepEqual(await visibleRowKeys(), ["1", "3"], "empty string chip matches only empty strings");
  assert.equal(await text(".result-filter-summary"), "2 of 5 loaded rows");

  // Combine the chip with the filter input: type "paid" -> only row 1 remains.
  await page.type(filterInput, "paid");
  await page.waitForFunction(() => document.querySelector(".result-filter-summary")?.textContent === "1 of 5 loaded rows");
  assert.deepEqual(await visibleRowKeys(), ["1"]);

  // Clear button clears both the text filter and chips.
  await page.click('button[aria-label="Clear filter"]');
  await page.waitForFunction(() => !document.querySelector(".result-filter-summary") && !document.querySelector(".result-filter-chip"));
  assert.deepEqual(await visibleRowKeys(), ["0", "1", "2", "3", "4"]);

  // ContextMenu key on row 0, col 2 (SQL NULL) -> "Hide this value" hides only SQL NULL (row 0), keeping "" and "NULL".
  await page.click(cell(0, 2));
  await page.keyboard.press("ContextMenu");
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Hide this value)').click();
  await page.waitForSelector(".result-filter-chip.is-hide");
  assert.deepEqual(await visibleRowKeys(), ["1", "2", "3", "4"]);
  assert.equal(await text(".result-filter-summary"), "4 of 5 loaded rows");

  // Copy all rows with header while filtered copies only the 4 filtered rows.
  const beforeCopies = await copyCount();
  await page.click(cell(1, 0), { button: "right" });
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Copy all rows with header)').click();
  await page.waitForFunction((n) => (window.__copied || []).length > n, {}, beforeCopies);
  assert.equal(
    await copied(),
    'region\tstatus\tnote\tamount\r\n2\tpaid\t""\t200\r\n3\tpaid\tNULL\t300\r\n4\tpending\t""\t400\r\n5\trefunded\tnote\t500',
  );

  await page.click('button[aria-label="Clear filter"]');
  await page.waitForFunction(() => !document.querySelector(".result-filter-summary"));

  // 2c. Pin and compare in the same tab:
  // Run baseline result with columns [tenant, id, status, amount], click "Pin result".
  await runSql(
    "SELECT tenant, id, status, amount FROM invoices",
    makeRun(
      [statement(0, "SELECT tenant, id, status, amount FROM invoices")],
      [
        rows(0, ["tenant", "id", "status", "amount"], [
          ["acme", "101", "open", "50"],
          ["acme", "102", "paid", "75"],
          ["beta", "103", "open", "90"],
        ]),
      ],
    ),
  );
  await page.waitForSelector('[data-action="pin-result"]');
  assert.equal(await page.$('[data-action="compare-pinned"]'), null, "no Compare button before pinning a baseline");
  await page.click('[data-action="pin-result"]');
  await page.waitForSelector(".result-pin-badge");
  assert.equal(await text(".result-pin-badge"), "Pinned · 3 rows");
  assert.doesNotMatch(await text(".editor-feedback"), /Pinned .*in this tab/,
    "the toolbar's pin badge carries the confirmation; no footer line repeats it");
  assert.equal(await page.$('[data-action="compare-pinned"]'), null, "not offered against the pinned result itself");

  // Run a later query in the same tab with DIFFERENT columns -> Compare with pinned is not offered.
  await runSql(
    "SELECT count_only FROM summary",
    makeRun([statement(0, "SELECT count_only FROM summary")], [rows(0, ["count_only"], [["3"]])]),
  );
  assert.ok(await page.$(".result-pin-badge"), "pin persists in the tab");
  assert.equal(await page.$('[data-action="compare-pinned"]'), null, "different columns: Compare with pinned is not offered");

  // Run a later query in the same tab with the SAME columns -> Compare with pinned is offered.
  await runSql(
    "SELECT tenant, id, status, amount FROM invoices",
    makeRun(
      [statement(0, "SELECT tenant, id, status, amount FROM invoices")],
      [
        rows(0, ["tenant", "id", "status", "amount"], [
          ["acme", "101", "paid", "50"], // changed: status open -> paid
          ["acme", "102", "paid", "75"], // unchanged
          ["gamma", "104", "open", "120"], // added (103 was removed)
        ]),
      ],
    ),
  );
  await page.waitForSelector('[data-action="compare-pinned"]');
  await page.click('[data-action="compare-pinned"]');
  await page.waitForSelector(".result-compare-table");

  // The compare toggle is never a second primary: pressed takes the kit's
  // soft accent state, released leaves it.
  assert.equal(await page.$eval('[data-action="compare-pinned"]', (e) => e.className), "button small",
    "Compare with pinned is not .primary next to the editor's primary Run");
  assert.equal(await page.$eval('[data-action="compare-pinned"]', (e) => e.getAttribute("aria-pressed")), "true");
  const accentSoft = await page.evaluate(() => {
    const probe = document.createElement("div");
    probe.style.backgroundColor = "var(--accent-soft)";
    document.body.append(probe);
    const value = getComputedStyle(probe).backgroundColor;
    probe.remove();
    return value;
  });
  // The state change transitions the fill; measure the settled, unhovered button.
  await page.mouse.move(5, 5);
  await pause(250);
  assert.equal(await page.$eval('[data-action="compare-pinned"]', (e) => getComputedStyle(e).backgroundColor), accentSoft,
    "pressed takes the soft accent state");

  // Key column preselects 'id' (even though 'tenant' is column 0); option values are column indexes.
  assert.equal(await page.$eval('select[aria-label="Key column"]', (e) => e.value), "1");
  assert.equal(await text(".result-compare-summary"), "1 added · 1 removed · 1 changed · 1 unchanged");

  // Verify added, removed, and changed rows and changed cell marking.
  const diffRows = await page.$$eval(".result-compare-table tbody tr[data-diff-status]", (trs) =>
    trs.map((tr) => ({
      status: tr.dataset.diffStatus,
      changedCols: [...tr.querySelectorAll("[data-changed=true]")].map((el) => el.closest("td").dataset.col),
      text: tr.textContent,
    })),
  );
  assert.equal(diffRows.length, 3);
  const changedRow = diffRows.find((r) => r.status === "changed");
  const addedRow = diffRows.find((r) => r.status === "added");
  const removedRow = diffRows.find((r) => r.status === "removed");
  assert.ok(changedRow && addedRow && removedRow);
  assert.deepEqual(changedRow.changedCols, ["c2"], "only the status column (c2) is marked as changed");
  assert.match(changedRow.text, /open→paid/);
  assert.match(addedRow.text, /104/);
  assert.match(removedRow.text, /103/);

  // 2d. 720 px minimum viewport width check (light and dark, filter and compare view).
  await page.setViewport({ width: 720, height: 600 });
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => (document.documentElement.dataset.theme = t), theme);
    await pause(100);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
      false,
      `no horizontal page overflow in compare view at 720 px (${theme})`,
    );
  }
  // Toggle back to the filtered grid at 720 px and verify no overflow.
  await page.click('[data-action="compare-pinned"]');
  await page.waitForSelector(".result-table");
  assert.equal(await page.$eval('[data-action="compare-pinned"]', (e) => e.getAttribute("aria-pressed")), "false");
  await page.mouse.move(5, 5);
  await pause(250);
  assert.notEqual(await page.$eval('[data-action="compare-pinned"]', (e) => getComputedStyle(e).backgroundColor), accentSoft,
    "released leaves the soft accent state");
  await page.type(filterInput, "acme");
  await page.waitForFunction(() => document.querySelector(".result-filter-summary")?.textContent === "2 of 3 loaded rows");
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
    false,
    "no horizontal page overflow in filtered grid at 720 px",
  );

  // Back to a wide, light window for the regression checks.
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => (document.documentElement.dataset.theme = "light"));

  // 2e. Filtering never extends a selection to rows the user did not select.
  // Sorted screen order is original indexes [0,4,2,1,3]; the selected range
  // covers original rows 4,2,1. Filtering row 1 out must keep rows 4 and 2
  // only: the old range fallback pulled row 3 into totals and copied text.
  const rangeRows = [
    ["1", "row-a", "100"], // original 0
    ["4", "zzz", "10"], // original 1
    ["3", "row-c", "20"], // original 2
    ["5", "row-d", "30"], // original 3
    ["2", "row-e", "40"], // original 4
  ];
  await runSql(
    "SELECT k, label, amount FROM ranged",
    makeRun([statement(0, "SELECT k, label, amount FROM ranged")], [rows(0, ["k", "label", "amount"], rangeRows)]),
  );
  const rowOrder = () =>
    page.$$eval(".result-table tbody tr[data-row-key]", (trs) => trs.map((tr) => tr.dataset.rowKey).join(","));
  await page.click('.result-table th[data-col=c0] .sort-button');
  if ((await rowOrder()) !== "0,4,2,1,3") await page.click('.result-table th[data-col=c0] .sort-button');
  await page.waitForFunction(
    () => [...document.querySelectorAll(".result-table tbody tr[data-row-key]")].map((tr) => tr.dataset.rowKey).join(",") === "0,4,2,1,3",
  );
  const amountCell = (row) => `.result-table tbody tr[data-row-key="${row}"] td[data-col="c2"]`;
  await page.click(amountCell(4));
  await page.keyboard.down("Shift");
  await page.click(amountCell(1));
  await page.keyboard.up("Shift");
  assert.equal(await text(".result-grid-footer"), "Count 3 · Sum 70 · Avg 23.333333 · Min 10 · Max 40", "range over sorted rows 4,2,1");
  await page.type(filterInput, "row-");
  await page.waitForFunction(() => document.querySelector(".result-filter-summary")?.textContent === "4 of 5 loaded rows");
  assert.equal(
    await text(".result-grid-footer"),
    "Count 2 · Sum 60 · Avg 30 · Min 20 · Max 40",
    "filtering out an endpoint row keeps only the selected rows that remain (never row 3)",
  );
  // Right-click inside the selection keeps it, and Copy selection copies only the surviving rows.
  const copiesBeforeRange = await copyCount();
  await page.click(amountCell(4), { button: "right" });
  await page.waitForSelector('.context-menu[aria-label="Result actions"]');
  await page.locator('.context-menu[aria-label="Result actions"] [role=menuitem] ::-p-text(Copy selection (2 cells))').click();
  await page.waitForFunction((n) => (window.__copied || []).length > n, {}, copiesBeforeRange);
  assert.equal(await copied(), "40\r\n20", "copy uses the surviving selected rows only");
  await page.click('button[aria-label="Clear filter"]');
  await page.waitForFunction(() => !document.querySelector(".result-filter-summary"));

  // 2f. A repeated key pairs identical rows and reports the rest as ambiguous
  // with an actionable note; duplicate column names each get their own
  // key-column option ("id (2)" selects the second id).
  const dupRun = (data) => makeRun([statement(0, "SELECT a.id, b.id, v FROM dup")], [rows(0, ["id", "id", "v"], data)]);
  await runSql("SELECT a.id, b.id, v FROM dup", dupRun([["1", "10", "a"], ["1", "20", "b"]]));
  await page.waitForSelector('[data-action="pin-result"]');
  await page.click('[data-action="pin-result"]');
  await page.waitForSelector(".result-pin-badge");
  await runSql("SELECT a.id, b.id, v FROM dup", dupRun([["1", "10", "a"], ["1", "30", "c"]]));
  await page.waitForSelector('[data-action="compare-pinned"]');
  await page.click('[data-action="compare-pinned"]');
  await page.waitForSelector(".result-compare-table");
  assert.equal(
    await text(".result-compare-summary"),
    "0 added · 0 removed · 0 changed · 2 ambiguous · 1 unchanged",
    "the identical duplicate-key row pairs off; the rest are ambiguous, not changed",
  );
  assert.equal(
    await text(".result-compare-note"),
    "Ambiguous key: 2 rows could not be matched because id has duplicate or NULL values. Choose another key column.",
  );
  assert.equal((await page.$$('.result-compare-table tbody tr[data-diff-status="ambiguous"]')).length, 2);
  assert.deepEqual(
    await page.$$eval('select[aria-label="Key column"] option', (os) => os.map((o) => o.textContent)),
    ["id (1)", "id (2)", "v"],
    "duplicate column names are numbered distinctly",
  );
  await page.select('select[aria-label="Key column"]', "1");
  await page.waitForFunction(
    () => document.querySelector(".result-compare-summary")?.textContent === "1 added · 1 removed · 0 changed · 1 unchanged",
  );
  assert.equal(await page.$(".result-compare-note"), null, "a unique key clears the ambiguous note");

  // 2g. A large pin against a large disjoint result renders through the shared
  // windowed table: the whole diff exists (aria-rowcount) but mounted rows stay
  // bounded instead of drawing 105,000 rows.
  const bigPinRows = Array.from({ length: 5200 }, (_, i) => [String(i + 1), `p${i + 1}`]);
  await runSql(
    "SELECT id, v FROM big",
    makeRun([statement(0, "SELECT id, v FROM big")], [rows(0, ["id", "v"], bigPinRows, { limit: 100000 })]),
  );
  await page.waitForSelector('[data-action="pin-result"]');
  await page.click('[data-action="pin-result"]');
  await page.waitForSelector(".result-pin-badge");
  assert.equal(await text(".result-pin-badge"), "Pinned · 5,000 of 5,200 rows", "a pin keeps at most 5,000 rows");
  const bigCurRows = Array.from({ length: 100000 }, (_, i) => [String(1000000 + i), `c${i}`]);
  await runSql(
    "SELECT id, v FROM big",
    makeRun([statement(0, "SELECT id, v FROM big")], [rows(0, ["id", "v"], bigCurRows, { limit: 100000 })]),
  );
  await page.waitForSelector('[data-action="compare-pinned"]');
  await page.click('[data-action="compare-pinned"]');
  await page.waitForSelector(".result-compare-table tbody tr[data-diff-status]");
  assert.equal(await text(".result-compare-summary"), "100,000 added · 5,000 removed · 0 changed");
  const mountedDiffRows = (await page.$$(".result-compare-table tbody tr[data-diff-status]")).length;
  assert.ok(mountedDiffRows > 0 && mountedDiffRows <= 400, `windowed compare mounts ${mountedDiffRows} rows, not 105,000`);
  assert.equal(
    await page.$eval(".result-compare-table table", (e) => e.getAttribute("aria-rowcount")),
    "105001",
    "the full diff stays one windowed table",
  );

  // 2h. A pin dies with its query tab; the surviving tab keeps its own pin.
  const tabsBefore = await page.$$eval(".query-tablist [role=tab]", (els) => els.length);
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction((n) => document.querySelectorAll(".query-tablist [role=tab]").length === n + 1, {}, tabsBefore);
  await runSql(
    "SELECT id, v FROM small",
    makeRun([statement(0, "SELECT id, v FROM small")], [rows(0, ["id", "v"], [["1", "x"]])]),
  );
  await page.waitForSelector('[data-action="pin-result"]');
  await page.click('[data-action="pin-result"]');
  await page.waitForSelector(".result-pin-badge");
  assert.equal(await text(".result-pin-badge"), "Pinned · 1 row");
  await page.click(".query-tab:has([aria-selected=true]) .query-tab-close");
  await page.waitForFunction((n) => document.querySelectorAll(".query-tablist [role=tab]").length === n, {}, tabsBefore);
  await page.waitForSelector(".result-pin-badge");
  assert.equal(await text(".result-pin-badge"), "Pinned · 5,000 of 5,200 rows", "closing another tab keeps this tab's pin");

  assert.deepEqual(errors, []);
  console.log("result-filter.mjs: all unit and browser assertions passed");
} finally {
  await browser?.close();
  server.kill();
  await fs.rm(directory, { recursive: true, force: true });
}
