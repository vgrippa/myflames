// Import CSV: the wizard (ImportCsv.tsx) and its entry points, against mocked
// replies that follow csv-inspect-1.0, csv-import-plan-1.0 and
// csv-import-result-1.0 (docs/schemas/). Covers every step, each separator
// and the preview read again after each format change, column matching, a
// new table, LOAD DATA LOCAL off with its reason, the review sheet (Cancel
// focused, Escape and Cancel recorded as declined), progress and Stop, every
// outcome, Close on a slow import with its line in the view, the entry points
// with their pre-filled targets and their reasons when off, a keyboard-only
// run, no page overflow at 720 px, and light and dark screenshots.
// IMPORT_CSV_SHOTS names the screenshot folder (keep it outside the repository).
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { load, open, pause, privilegeAnswer } from "./perf-harness.mjs";
import { chooseConnection } from "./chrome-helpers.mjs";

const shotsDir = process.env.IMPORT_CSV_SHOTS || "";
const SIZES = [[1440, 900], [720, 600]];
const tablesReport = await load("tables-mysql-8.4.json");
const ok = (body) => ({ status: 200, body });

// ---- The file and its mocked parser ----------------------------------------------
const CSV = [
  "Order ID;Customer;Amount;Note",
  "1001;Ana Souza;10.50;first order",
  "1002;Bruno Lima;7.00;",
  "1003;Carla Dias;abc;bad amount",
  '1004;Davi Rocha;12.00;"quoted; note"',
  "1005;Eva Costa;99.90;gift",
];
const HEADERS = ["Order ID", "Customer", "Amount", "Note"];
const INFERRED = ["INT UNSIGNED", "VARCHAR(40)", "VARCHAR(10)", "VARCHAR(40)"];
const DETECTED = { encoding: "utf-8", separator: ";", quote: '"', escape: "", header: true, line_ending: "\n", skip_lines: 0, empty: "null", null_markers: ["\\N"] };
const FILE = { file_id: "file-1", name: "Orders 2026.csv", size: 524288, modified: "2026-10-29T09:12:00Z" };
function split(line, sep, quote) {
  const out = [];
  let cell = "", quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quote && ch === quote) { quoted = !quoted; continue; }
    if (ch === sep && !quoted) { out.push(cell); cell = ""; continue; }
    cell += ch;
  }
  out.push(cell);
  return out;
}
function inspectReply(body) {
  const format = body.format || DETECTED;
  const lines = CSV.slice(format.skip_lines || 0);
  const rows = lines.map((l) => split(l, format.separator, format.quote));
  // Columns come from the header row (else the widest row); other widths are problems.
  const width = format.header ? rows[0].length : Math.max(...rows.map((r) => r.length));
  const head = format.header ? rows[0] : null;
  const data = format.header ? rows.slice(1) : rows;
  const first = (format.skip_lines || 0) + (format.header ? 2 : 1);
  const columns = Array.from({ length: width }, (_v, i) => ({
    index: i, header: head?.[i] ?? "", name: (head?.[i] ?? `column_${i + 1}`).toLowerCase().replace(/[^a-z0-9]+/g, "_"),
    inferred: { sql_type: width === 4 ? INFERRED[i] : "VARCHAR(255)", nullable: i === 3, max_length: 40 },
    samples: data.map((r) => r[i] ?? "").filter(Boolean).slice(0, 3),
  }));
  return {
    schema_version: "csv-inspect-1.0", file: FILE, detected: DETECTED, format, columns,
    preview: { rows: data.map((r) => columns.map((_c, i) => (r[i] === "" && format.empty === "null" ? null : r[i] ?? null))), lines: data.map((_r, i) => first + i), truncated: true },
    rows: 12345, rows_estimate: null, rows_counted: true,
    problems: data.map((r, i) => ({ r, line: first + i })).filter(({ r }) => r.length !== width)
      .map(({ r, line }) => ({ line, message: `${r.length} fields; the header has ${width}.` })),
    problems_total: data.filter((r) => r.length !== width).length, notes: ["Line endings: LF.", "Read 2,048 bytes to detect the format."],
  };
}

// ---- Plans and results ---------------------------------------------------------------
let tokenSeq = 0, localInfile = false;
const TOTAL = 12345;
function planReply(body) {
  const t = body.target;
  const fresh = t.mode === "new";
  const q = `\`${t.schema}\`.\`${t.table}\``;
  const skip = body.on_error === "skip";
  const blockers = skip ? [] : ["Line 4 has a value amount cannot take. Choose Skip and report, or fix the file."];
  const valid = TOTAL - 1;
  const columns = fresh ? t.new_columns.map((c) => c.name) : t.mapping.filter((m) => m.csv_index !== null).map((m) => m.column);
  const list = columns.map((c) => `\`${c}\``).join(", ");
  return ok({
    schema_version: "csv-import-plan-1.0", token: blockers.length ? null : `tok-${++tokenSeq}`, ready: !blockers.length, blockers, expires_in: 600,
    file: { ...FILE, sha256: "ab".repeat(32) },
    connection: { name: "Lab production", host: "127.0.0.1", port: 3306, user: "root", type: "production", read_only: false, engine: "mysql", server_version: "8.4.7" },
    target: { schema: t.schema, table: t.table, exists: !fresh, engine: fresh ? null : "InnoDB", columns: [], rows_estimate: fresh ? null : 42,
      triggers: 0, foreign_keys: 0, primary_key: fresh ? ["id"] : ["order_id"], transactional: true, check_constraints: 0 },
    mapping: t.mapping.map((m) => ({ ...m, csv_header: m.csv_index === null ? null : HEADERS[m.csv_index], type: "varchar(40)" })),
    statements: {
      setup: ["SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',STRICT_ALL_TABLES')", "SET SESSION lock_wait_timeout = 5", "SET SESSION innodb_lock_wait_timeout = 5"],
      create: fresh ? `CREATE TABLE ${q} (\n  \`id\` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,\n${t.new_columns.map((c) => `  \`${c.name}\` ${c.sql_type}${c.nullable ? "" : " NOT NULL"}`).join(",\n")},\n  PRIMARY KEY (\`id\`)\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4` : null,
      data_sample: `INSERT INTO ${q} (${list}) VALUES (1001, 'Ana Souza', 10.50, 'first order'), (1002, 'Bruno Lima', 7.00, NULL) …`,
      data_summary: `INSERT INTO ${q} (${list}) VALUES ... : ${valid.toLocaleString("en-US")} rows in 13 statements from Orders 2026.csv`,
    },
    batches: body.method === "load_data" ? 1 : 13, rows: { total: TOTAL, valid, invalid: 1, skipped: skip ? 1 : 0 },
    problems: [{ line: 4, column: "amount", value: "abc", message: "Not a number: DECIMAL(10,2) needs digits." }], problems_total: 1,
    method: { chosen: body.method, load_data_available: localInfile, local_infile: localInfile, why: localInfile ? "The server has local_infile ON, so LOAD DATA LOCAL can be used."
      : "The server has local_infile OFF (MySQL's default), so it refuses LOAD DATA LOCAL. myflames never changes it; a DBA can turn it on with SET PERSIST local_infile = ON. INSERT batches work either way." },
    transaction: { ...body.transaction, commits: body.transaction.mode === "every" ? Math.ceil(valid / body.transaction.every) : 1 }, on_error: body.on_error,
    privileges: [{ action: "insert", state: "allowed", reason: `Allowed through INSERT on ${t.schema}.*.` },
      ...(fresh ? [{ action: "create_table", state: "unknown", reason: "Not verified: SHOW GRANTS cannot tell; the server decides." }] : [])],
    policy: { action: "confirm", tone: "danger", title: "Production", text: "Production: every change on Lab production asks first.", connection_type: "production", read_only: false },
    warnings: fresh ? ["CREATE TABLE commits at once: if the rows fail or you stop, the new table stays, empty."] : [],
    notes: ["Session: sql_mode adds STRICT_ALL_TABLES; lock_wait_timeout and innodb_lock_wait_timeout are 5 s."],
    drop_hint: fresh ? `DROP TABLE ${q};` : null,
  });
}
const result = (outcome, extra = {}) => ({
  schema_version: "csv-import-result-1.0", outcome, rows: { total: TOTAL, sent: TOTAL - 1, committed: outcome === "done" ? TOTAL - 1 : 0, skipped: 1 },
  failed_at: null, next_line: outcome === "done" ? null : 2, last_committed_line: null, server_error: null, warnings: { count: 0, items: [] },
  duration_ms: 1840, table_created: false, table_rows_after: outcome === "done" ? TOTAL + 41 : 42, drop_hint: null, audit: { id: "audit-7" },
  problems: outcome === "done" ? [{ line: 4, column: "amount", value: "abc", message: "Not a number: DECIMAL(10,2) needs digits." }] : [], problems_total: outcome === "done" ? 1 : 0,
  method: "insert", transaction: { mode: "all", every: 0 }, ...extra,
});

// ---- Mocked endpoints ---------------------------------------------------------------------
const calls = { choose: 0, openPath: [], inspect: [], plan: [], apply: [], progress: 0, stop: [], declined: [], catalog: [] };
let planDelay = 0;
let chooseMode = "file", inspectDelay = 0, inspectFail = false, applyMode = "done", pendingApply = null, progressRows = 0;
function respondApply(status, body) {
  const p = pendingApply;
  pendingApply = null;
  if (p) void p.respond({ status, contentType: "application/json", body: JSON.stringify(body) });
}
const schemas = { schema_version: "schemas-1.0", user_tables: 23, all_limit: 2000, all_note: "", system: ["mysql", "sys"],
  schemas: [{ name: "shop", tables: 2, system: false }, { name: "mftab", tables: 18, system: false }, { name: "mysql", tables: 38, system: true }] };
const ORDERS = [
  { name: "order_id", type: "int unsigned", nullable: false, key: "PRI", default: null, extra: "", generated: false },
  { name: "customer", type: "varchar(80)", nullable: false, key: "", default: null, extra: "", generated: false },
  { name: "amount", type: "decimal(10,2)", nullable: true, key: "", default: null, extra: "", generated: false },
  { name: "note", type: "text", nullable: true, key: "", default: null, extra: "", generated: false },
  { name: "created_at", type: "timestamp", nullable: false, key: "", default: "CURRENT_TIMESTAMP", extra: "DEFAULT_GENERATED", generated: false },
  { name: "total_cents", type: "bigint", nullable: true, key: "", default: null, extra: "STORED GENERATED", generated: true },
];
const extra = {
  "/api/live/schemas": ok(schemas),
  "/api/live/catalog": (body) => {
    calls.catalog.push(body);
    if (body.table) return ok({ schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table, indexes: [],
      columns: body.table === "orders" ? ORDERS : [{ name: "a", type: "int", nullable: true, key: "", default: null, extra: "", generated: false },
        { name: "b", type: "varchar(20)", nullable: true, key: "", default: null, extra: "", generated: false }] });
    const names = body.schema === "shop" ? ["customers", "orders"] : ["func_uk", "mixed_uk", "no_key"];
    return ok({ schema_version: "catalog-1.0", level: "tables", schema: body.schema, tables: names.map((name) => ({ name, type: "table" })),
      truncated: false, limit: 200, prefix: null, after: null, next_after: null });
  },
  "/api/import/csv/choose": () => {
    calls.choose++;
    if (chooseMode === "cancel") return ok({ cancelled: true });
    if (chooseMode === "error") return { status: 400, body: { error: "The system file chooser is not available here." } };
    return ok(FILE);
  },
  "/api/import/csv/open-path": (body) => { calls.openPath.push(body); return body.path.endsWith(".csv") ? ok(FILE) : { status: 400, body: { error: "Not a regular file: /tmp." } }; },
  "/api/import/csv/inspect": (body, request) => {
    calls.inspect.push(body);
    const reply = inspectFail ? { status: 400, body: { error: "Could not decode line 3 as UTF-16 LE." } } : ok(inspectReply(body));
    if (!inspectDelay) return reply;
    setTimeout(() => void request.respond({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) }), inspectDelay);
    return null;
  },
  "/api/import/csv/plan": (body, request) => {
    calls.plan.push(body);
    const reply = planReply(body);
    // Answered later; a check the wizard stopped may already be aborted.
    setTimeout(() => void request.respond({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) }).catch(() => {}), planDelay || 200);
    return null;
  },
  "/api/import/csv/progress": (body) => {
    calls.progress++;
    const applying = !!pendingApply && pendingApply.id === body.progress_id;
    if (applying) progressRows = Math.min(TOTAL - 1, progressRows + 3000);
    return ok({ schema_version: "csv-import-progress-1.0", kind: applying ? "apply" : "plan", phase: applying ? "sending" : "checking",
      rows_total: TOTAL - 1, rows_checked: 6000, rows_sent: applying ? progressRows : 0, rows_committed: 0,
      batches_done: Math.floor(progressRows / 1000), batches_total: 13, in_flight: applying, started_at: "2026-10-29T10:00:00Z", elapsed_s: 2, stop_requested: false });
  },
  "/api/import/csv/stop": (body) => {
    calls.stop.push(body);
    if (pendingApply && applyMode === "stop") setTimeout(() => respondApply(200, result("stopped", { rows: { total: TOTAL, sent: 3000, committed: 0, skipped: 0 },
      message: "Stopped. Nothing was kept: the transaction rolled back." })), 600);
    return ok({ stop_requested: true, message: "Stopping after the current batch." });
  },
  "/api/import/csv/apply": (body, request) => {
    calls.apply.push(body);
    progressRows = 0;
    pendingApply = { id: body.progress_id, respond: (r) => request.respond(r), abort: () => request.abort("failed") };
    if (applyMode === "done") setTimeout(() => respondApply(200, result("done")), 1600);
    if (applyMode === "failed") setTimeout(() => respondApply(200, result("failed", { failed_at: { line: 4, message: "Incorrect decimal value: 'abc' for column 'amount'" },
      server_error: { code: 1366, message: "Incorrect decimal value: 'abc' for column 'amount' at row 1" }, rows: { total: TOTAL, sent: 1000, committed: 0, skipped: 0 } })), 900);
    if (applyMode === "unknown") setTimeout(() => { const p = pendingApply; pendingApply = null; void p?.abort(); }, 900);
    if (applyMode === "refused") { pendingApply = null; return { status: 400, body: { error: "This review expired. Nothing was sent. Check the import again." } }; }
    if (applyMode === "refused-audited") { pendingApply = null; return { status: 400, body: { error: "The file changed since the check (size or modification time). Nothing was sent. Check the import again.", audit_id: "audit-refused" } }; }
    if (applyMode === "slow") setTimeout(() => respondApply(200, result("done")), 12500);
    if (applyMode === "new-failed") setTimeout(() => respondApply(200, result("failed", { failed_at: { line: 4, message: "Bad value." }, table_created: true,
      table_rows_after: 0, drop_hint: "DROP TABLE `shop`.`orders_2026`;", warnings: { count: 2, items: [{ level: "Warning", code: 1265, message: "Data truncated for column 'note' at row 2" }, { level: "Note", code: 1003, message: "Second note" }] } })), 800);
    return null;
  },
  // The Audit log as the backend keeps a stopped import: status ok, outcome stopped.
  "/api/audit/list": ok({ schema_version: "audit-1.0", retention_days: 90, has_more: false, records: [{
    id: "audit-7", attempted_at: "2026-10-29T10:00:00.000000+00:00", finished_at: "2026-10-29T10:00:02.000000+00:00",
    status: "ok", outcome: "stopped", outcome_unknown: false, surface: "ui", os_user: "me", operation: "import_csv",
    connection: { profile_id: null, profile_name: "Lab production", type: "production", read_only: false, host: "127.0.0.1", port: 3306,
      user: "root", engine: "mysql", server_version: "8.4.7" },
    statements: ["SET SESSION lock_wait_timeout = 5", "INSERT INTO `shop`.`orders` (`order_id`) VALUES ... : 12,344 rows in 13 statements from Orders 2026.csv"],
    target: { schema: "shop", table: "orders" }, verified: [], policy: null, error_code: null,
    message: "Stopped. Nothing was kept: the transaction rolled back.", rows_affected: 0, after: null, duration_ms: 1840 }] }),
  "/api/audit/declined": (body) => { calls.declined.push(body); return ok({ audit_id: `d${calls.declined.length}`, status: "declined" }); },
  "/api/editor/status": ok({ status: "none", session: null, run: null }),
  "/api/editor/history": ok({ items: [] }),
};

const h = await open("/api/live/tables", "import-csv", extra);
const { page, state } = h;
state.fallback = { status: 200, body: tablesReport };
const sheet = "dialog.import-csv-sheet";
const inSheet = (text) => h.button(text, `${sheet} `);
const sheetText = () => page.$eval(sheet, (e) => e.textContent);
const waitText = (selector, pattern) => page.waitForFunction((s, p) => new RegExp(p).test(document.querySelector(s)?.textContent || ""), { timeout: 15000 }, selector, pattern.source);
const currentStep = () => page.$eval(`${sheet} .import-csv-steps [aria-current=step] .import-csv-step-label`, (e) => e.textContent);
const nextState = (label) => page.$$eval(`${sheet} .sheet-actions button`, (els, l) => {
  const b = els.find((e) => e.textContent.trim() === l);
  return b ? { disabled: b.disabled, aria: b.getAttribute("aria-disabled") } : null;
}, label);
const pressed = (group) => page.$eval(`${sheet} .segmented[aria-label="${group}"] [aria-pressed=true]`, (e) => e.textContent);
const lastInspect = () => calls.inspect[calls.inspect.length - 1];
const lastPlan = () => calls.plan[calls.plan.length - 1];
const idleReading = () => page.waitForFunction((s) => !document.querySelector(`${s} .import-csv-line[data-phase=pending]`), { timeout: 10000 }, sheet);
const previewHeaders = () => page.$$eval(`${sheet} [aria-label="Preview of the first rows"] thead th`, (els) => els.map((e) => e.querySelector(".import-csv-cell")?.textContent ?? e.textContent));
async function waitInspectCount(n) {
  for (let i = 0; i < 200 && calls.inspect.length < n; i++) await pause(20);
  assert.ok(calls.inspect.length >= n, `inspect called ${n} times (got ${calls.inspect.length})`);
}

async function noOverflow(label) {
  const result = await page.evaluate((s) => {
    const d = document.querySelector(s);
    const body = d?.querySelector(".sheet-body");
    return {
      page: document.documentElement.scrollWidth - window.innerWidth,
      dialog: d ? d.scrollWidth - d.clientWidth : 0,
      body: body ? body.scrollWidth - body.clientWidth : 0,
      right: d ? d.getBoundingClientRect().right - window.innerWidth : 0,
      bottom: d ? d.getBoundingClientRect().bottom - window.innerHeight : 0,
    };
  }, sheet);
  assert.ok(result.page <= 1, `${label}: page overflows by ${result.page} px`);
  assert.ok(result.dialog <= 1 && result.body <= 1, `${label}: the sheet overflows sideways (${result.dialog}, ${result.body})`);
  assert.ok(result.right <= 1 && result.bottom <= 1, `${label}: the sheet leaves the window`);
  return result;
}
const measured = [];
/** Each size and appearance: overflow checks, and a screenshot when asked. */
async function shoot(name) {
  for (const [width, height] of SIZES) {
    for (const scheme of ["light", "dark"]) {
      await page.setViewport({ width, height });
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await pause(120);
      const r = await noOverflow(`${name} ${width}x${height} ${scheme}`);
      if (scheme === "light") measured.push({ name, width, height, ...r });
      if (shotsDir) await page.screenshot({ path: `${shotsDir}/${name}-${width}x${height}-${scheme}.png` });
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await pause(80);
}

async function saveProfile(value) {
  return page.evaluate(async (v) => {
    const headers = { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" };
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers, body: "{}" })).json()).token;
    return (await (await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify({ value: v }) })).json()).id;
  }, value);
}
async function expandShop() {
  const shop = '.schema-browser .tree-row.kind-schema[data-node="s:shop"]';
  await page.waitForSelector(shop);
  if (await page.$eval(shop, (e) => e.getAttribute("aria-expanded")) !== "true") await page.click(shop);
  await page.waitForSelector('.schema-browser .tree-row.kind-table[data-node$="orders"]');
}
async function treeMenu(kind, name) {
  const row = kind === "schema" ? `.schema-browser .tree-row.kind-schema[data-node="s:${name}"]` : `.schema-browser .tree-row.kind-${kind}[data-node$="${name}"]`;
  await page.waitForSelector(row);
  await page.click(row, { button: "right" });
  await page.waitForSelector('.schema-browser .context-menu[role=menu]');
}
const menuItem = (text) => page.$$eval('.schema-browser .context-menu [role=menuitem]', (els, t) => {
  const e = els.find((x) => x.textContent.startsWith(t));
  return e ? { aria: e.getAttribute("aria-disabled"), title: e.title, text: e.textContent } : null;
}, text);
async function openFromTable(name = "orders") {
  await treeMenu("table", name);
  await page.locator('.schema-browser .context-menu [role=menuitem] ::-p-text(Import CSV…)').click();
  await page.waitForSelector(`${sheet}[open]`);
}
async function chooseFile() {
  chooseMode = "file";
  const before = calls.inspect.length;
  await inSheet("Choose file…");
  await page.waitForSelector(`${sheet} .import-csv-file-name`);
  await waitInspectCount(before + 1);
  await page.waitForSelector(`${sheet} [aria-label="Preview of the first rows"]`);
  await idleReading();
}
/** File and format, then Target and columns as opened, to the Check step. */
async function toCheck() {
  await chooseFile();
  await inSheet("Next");
  await page.waitForFunction((s) => document.querySelector(`${s} [aria-current=step]`)?.textContent.includes("Target"), {}, sheet);
  await page.waitForFunction((s) => {
    const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === "Next");
    return b && b.getAttribute("aria-disabled") !== "true";
  }, { timeout: 10000 }, sheet);
  await inSheet("Next");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Check/);
  await waitText(`${sheet} .import-csv-summary`, /12,345 rows/);
}
async function toReview() {
  await toCheck();
  const before = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="Bad rows"] [data-value=skip]`);
  for (let i = 0; i < 100 && calls.plan.length === before; i++) await pause(20);
  await page.waitForFunction((s) => {
    const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  }, { timeout: 10000 }, sheet);
  await inSheet("Review…");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Review/);
}
async function closeSheet() {
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), {}, sheet);
}

try {
  if (shotsDir) await fs.mkdir(shotsDir, { recursive: true });
  const prod = await saveProfile({ name: "Lab production", host: "127.0.0.1", port: 3306, user: "root" });
  const readOnly = await saveProfile({ name: "Lab read-only", host: "127.0.0.1", port: 3307, user: "root", read_only: true });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await h.button("Query editor", ".main-nav ");
  await chooseConnection(page, prod);
  if (await page.$('button[aria-label="Show the schema browser"]')) await page.click('button[aria-label="Show the schema browser"]');
  await h.button("Load schemas", ".schema-browser ");
  await expandShop();

  // ---- Entry points in the Schemas panel: names, pre-fill, system databases.
  await treeMenu("table", "orders");
  const tableItem = await menuItem("Import CSV…");
  assert.ok(tableItem && tableItem.aria === null, "the table menu offers Import CSV…, enabled");
  await page.keyboard.press("Escape");
  // The menu at each size and appearance: inside the window, the page never scrolls sideways.
  for (const [width, height] of SIZES) for (const scheme of ["light", "dark"]) {
    await page.setViewport({ width, height });
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
    await pause(150);
    if (!(await page.$eval(".schema-browser", (e) => e.getClientRects().length > 0 && !e.closest("[hidden]")).catch(() => false))) {
      await page.click('button[aria-label="Show the schema browser"]');
    }
    await treeMenu("table", "orders");
    const box = await page.$eval(".schema-browser .context-menu", (e) => { const r = e.getBoundingClientRect(); return { bottom: r.bottom, right: r.right }; });
    assert.ok(box.bottom <= height && box.right <= width, `the table menu stays in the window at ${width}x${height}`);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "no page overflow with the menu open");
    if (shotsDir) await page.screenshot({ path: `${shotsDir}/entry-schemas-table-menu-${width}x${height}-${scheme}.png` });
    await page.keyboard.press("Escape");
  }
  await page.setViewport({ width: 1440, height: 900 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await pause(150);
  if (!(await page.$eval(".schema-browser", (e) => e.getClientRects().length > 0 && !e.closest("[hidden]")).catch(() => false))) {
    await page.click('button[aria-label="Show the schema browser"]');
  }
  await treeMenu("schema", "shop");
  assert.ok(await menuItem("Import CSV into a new table…"), "the database menu offers Import CSV into a new table…");
  await page.keyboard.press("Escape");
  await page.waitForSelector('.tree-row.kind-schema[data-node="s:mysql"]');
  await treeMenu("schema", "mysql");
  assert.equal(await menuItem("Import CSV into a new table…"), null, "system databases do not offer a new table");
  await page.keyboard.press("Escape");

  // ---- Step 1: File and format.
  await openFromTable();
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "Import CSV into shop.orders");
  assert.equal(await currentStep(), "File and format");
  assert.match(await page.$eval(`${sheet} .connection-type-band`, (e) => e.textContent), /Production/);
  assert.equal((await nextState("Next")).aria, "true", "Next waits for a file");
  await shoot("step1-empty");
  // Cancel closes without recording anything (nothing was reviewed).
  await inSheet("Cancel");
  await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), {}, sheet);
  assert.equal(calls.declined.length, 0);

  await openFromTable();
  // The chooser fails: the error and the typed path, focused.
  chooseMode = "error";
  await inSheet("Choose file…");
  await waitText(`${sheet} .import-csv-line`, /not available here\. You can type the path instead/);
  await page.waitForFunction(() => document.activeElement?.id && document.activeElement.closest(".import-csv-path"));
  await page.keyboard.type("/tmp");
  await inSheet("Open");
  await waitText(`${sheet} .import-csv-path`, /Not a regular file/);
  await page.$eval(`${sheet} .import-csv-path input`, (e) => e.select());
  await page.keyboard.type("/Users/me/Orders 2026.csv");
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${sheet} .import-csv-file-name`);
  assert.equal(calls.openPath[calls.openPath.length - 1].path, "/Users/me/Orders 2026.csv");
  await page.waitForSelector(`${sheet} [aria-label="Preview of the first rows"]`);
  await idleReading();
  assert.equal(lastInspect().format, undefined, "the first read detects the format");
  assert.match(await sheetText(), /Detected: semicolon/);
  assert.equal(await pressed("Separator"), "Semicolon", "the detected separator is chosen");
  assert.deepEqual(await previewHeaders(), ["Line", "Order ID", "Customer", "Amount", "Note"]);
  assert.match(await page.$eval(`${sheet} .import-csv-counts`, (e) => e.textContent), /12,345 rows · 4 columns/);
  assert.match(await page.$eval(`${sheet} [aria-label="Preview of the first rows"] tbody`, (e) => e.textContent), /quoted; note/);
  assert.equal(await page.$eval(`${sheet} [aria-label="Preview of the first rows"] tbody tr:nth-child(2) .import-csv-null`, (e) => e.textContent), "NULL", "an empty field reads NULL");
  // A choice cancelled in the system dialog changes nothing.
  chooseMode = "cancel";
  await inSheet("Choose another file…");
  await waitText(`${sheet} .import-csv-line`, /No other file chosen; Orders 2026\.csv stays\./);
  assert.match(await page.$eval(`${sheet} .import-csv-file-name`, (e) => e.textContent), /Orders 2026\.csv/);

  // Every separator re-reads the preview, with a busy line while it reads.
  inspectDelay = 350;
  for (const [label, value, width] of [["Comma", ",", 1], ["Tab", "\t", 1], ["Pipe", "|", 1]]) {
    const before = calls.inspect.length;
    await page.locator(`${sheet} .segmented[aria-label="Separator"] button ::-p-text(${label})`).click();
    await page.waitForSelector(`${sheet} .import-csv-line[data-phase=pending]`);
    await waitInspectCount(before + 1);
    await idleReading();
    assert.equal(lastInspect().format.separator, value, `${label} is sent`);
    assert.equal((await previewHeaders()).length, width + 1, `${label} re-reads the preview`);
    assert.equal(await pressed("Separator"), label);
  }
  {
    const before = calls.inspect.length;
    await page.locator(`${sheet} .segmented[aria-label="Separator"] button ::-p-text(Other)`).click();
    await page.waitForSelector(`${sheet} input[aria-label="Other separator (one character)"]`);
    await page.type(`${sheet} input[aria-label="Other separator (one character)"]`, "~");
    await waitInspectCount(before + 1);
    await idleReading();
    assert.equal(lastInspect().format.separator, "~", "another single character is sent");
  }
  inspectDelay = 0;
  await page.locator(`${sheet} .segmented[aria-label="Separator"] button ::-p-text(Semicolon)`).click();
  await page.waitForFunction(() => document.querySelectorAll('dialog.import-csv-sheet [aria-label="Preview of the first rows"] thead th').length === 5);
  assert.equal(lastInspect().format.separator, ";");
  // Header row, encoding, quote, empty fields, skipped lines: each read again.
  {
    let before = calls.inspect.length;
    await page.locator(`${sheet} .checkbox ::-p-text(First row has column names)`).click();
    await waitInspectCount(before + 1);
    await idleReading();
    assert.equal(lastInspect().format.header, false);
    assert.deepEqual((await previewHeaders()).slice(1), ["Column 1", "Column 2", "Column 3", "Column 4"]);
    await page.locator(`${sheet} .checkbox ::-p-text(First row has column names)`).click();
    await waitInspectCount(before + 2);
    before = calls.inspect.length;
    await page.select(`${sheet} .import-csv-field select`, "cp1252");
    await waitInspectCount(before + 1);
    assert.equal(lastInspect().format.encoding, "cp1252");
    before = calls.inspect.length;
    await page.select(`${sheet} .import-csv-field select`, "utf-8");
    await waitInspectCount(before + 1);
    // Without a quote character the quoted separator splits line 5: a problem with its line.
    before = calls.inspect.length;
    await page.select(`${sheet} .import-csv-grid-fields label:nth-of-type(2) select`, "");
    await waitInspectCount(before + 1);
    assert.equal(lastInspect().format.quote, "");
    await waitText(`${sheet} [aria-label="Problems by line"]`, /55 fields; the header has 4\./);
    assert.match(await page.$eval(`${sheet} .import-csv-counts`, (e) => e.textContent), /1 problem/);
    await shoot("step1-problems");
    before = calls.inspect.length;
    await page.select(`${sheet} .import-csv-grid-fields label:nth-of-type(2) select`, '"');
    await waitInspectCount(before + 1);
    await page.waitForFunction(() => !document.querySelector('dialog.import-csv-sheet [aria-label="Problems by line"]'));
    before = calls.inspect.length;
    await page.locator(`${sheet} .segmented[aria-label="Empty fields are"] button ::-p-text(Empty string)`).click();
    await waitInspectCount(before + 1);
    assert.equal(lastInspect().format.empty, "empty");
    await page.locator(`${sheet} .segmented[aria-label="Empty fields are"] button ::-p-text(NULL)`).click();
    await waitInspectCount(before + 2);
    assert.deepEqual(lastInspect().format.null_markers, ["\\N"], "NULL markers pass through unchanged");
    // NULL markers: \N on (the backend's default), the word NULL off; each change reads again.
    const marker = (text) => page.$eval(`${sheet} [role=group][aria-label="NULL markers"]`, (g, t) =>
      [...g.querySelectorAll("label")].find((l) => l.textContent === t).querySelector("input").checked, text);
    // Clicked through its label (a backslash cannot go into a ::-p-text selector).
    const toggleMarker = async (text) => {
      const handle = await page.evaluateHandle((sel, t) => [...document.querySelectorAll(`${sel} [role=group][aria-label="NULL markers"] label`)]
        .find((l) => l.textContent === t), sheet, text);
      await handle.asElement().click();
      await handle.dispose();
    };
    assert.equal(await marker("Treat \\N as NULL"), true);
    assert.equal(await marker("Treat the word NULL as NULL"), false);
    before = calls.inspect.length;
    await toggleMarker("Treat the word NULL as NULL");
    await waitInspectCount(before + 1);
    assert.deepEqual(lastInspect().format.null_markers, ["\\N", "NULL"]);
    await toggleMarker("Treat \\N as NULL");
    await waitInspectCount(before + 2);
    assert.deepEqual(lastInspect().format.null_markers, ["NULL"]);
    await toggleMarker("Treat the word NULL as NULL");
    await waitInspectCount(before + 3);
    assert.deepEqual(lastInspect().format.null_markers, [], "no marker: every field is text");
    await toggleMarker("Treat \\N as NULL");
    await waitInspectCount(before + 4);
    assert.deepEqual(lastInspect().format.null_markers, ["\\N"]);
    await idleReading();
    // A failed read: the error once, with Try again; Next stays off.
    inspectFail = true;
    before = calls.inspect.length;
    await page.$eval(`${sheet} .number-field input`, (e) => e.select());
    await page.keyboard.type("1");
    await waitInspectCount(before + 1);
    await waitText(`${sheet} .import-csv-line[data-phase=failed]`, /Could not read the file: Could not decode line 3/);
    assert.equal(lastInspect().format.skip_lines, 1);
    assert.equal((await nextState("Next")).aria, "true");
    inspectFail = false;
    await page.locator(`${sheet} .import-csv-line button ::-p-text(Try again)`).click();
    await idleReading();
    await page.$eval(`${sheet} .number-field input`, (e) => e.select());
    await page.keyboard.type("0");
    await page.waitForFunction(() => document.querySelectorAll('dialog.import-csv-sheet [aria-label="Preview of the first rows"] thead th').length === 5);
    await idleReading();
    await page.waitForFunction(() => document.querySelector('dialog.import-csv-sheet .sheet-actions .primary')?.getAttribute("aria-disabled") !== "true");
  }
  await shoot("step1-file-and-format");

  // ---- Step 2: Target and columns (existing table, matched by name).
  await inSheet("Next");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Target and columns/);
  assert.equal(await pressed("Into"), "Existing table");
  await page.waitForSelector(`${sheet} [aria-label="CSV columns and the table columns they fill"] select`);
  const mapping = () => page.$$eval(`${sheet} [aria-label="CSV columns and the table columns they fill"] tbody select`, (els) => els.map((e) => e.value));
  assert.deepEqual(await mapping(), ["order_id", "customer", "amount", "note"], "matched by header name ignoring case and punctuation");
  assert.match(await sheetText(), /Not filled from the file: created_at; generated by the server: total_cents\./);
  assert.ok(!(await page.$$eval(`${sheet} [aria-label="CSV columns and the table columns they fill"] tbody select option`, (els) => els.some((o) => o.value === "total_cents"))), "generated columns are not offered");
  // Two CSV columns to one table column: named, and Next stays off.
  await page.select(`${sheet} select[aria-label="Table column for Note"]`, "amount");
  await waitText(`${sheet} .import-csv-warnings`, /2 CSV columns go to amount/);
  assert.equal((await nextState("Next")).aria, "true");
  await page.select(`${sheet} select[aria-label="Table column for Note"]`, "");
  await page.select(`${sheet} select[aria-label="Table column for Customer"]`, "");
  await waitText(`${sheet} .import-csv-watch`, /customer has no default and cannot be NULL/);
  await page.locator(`${sheet} button ::-p-text(Match by name again)`).click();
  await page.waitForFunction(() => document.querySelector('dialog.import-csv-sheet select[aria-label="Table column for Note"]')?.value === "note");
  await shoot("step2-existing-table");

  // ---- Step 3: Check (the plan), LOAD DATA LOCAL off with its reason.
  await inSheet("Next");
  await waitText(`${sheet} .import-csv-summary`, /12,345 rows · 12,344 ready · 1 with problems · 13 statements/);
  const first = lastPlan();
  assert.equal(first.target.mode, "existing");
  assert.deepEqual(first.target.mapping, [
    { csv_index: 0, column: "order_id" }, { csv_index: 1, column: "customer" }, { csv_index: 2, column: "amount" },
    { csv_index: 3, column: "note" }, { csv_index: null, column: "created_at" }]);
  assert.equal(first.method, "insert");
  assert.deepEqual(first.transaction, { mode: "all", every: 0 });
  assert.equal(first.on_error, "stop");
  assert.equal(first.format.separator, ";");
  assert.equal(first.connection.host, "127.0.0.1");
  assert.match(first.progress_id, /^[0-9a-f]{32}$/);
  assert.match(await page.$eval(`${sheet} .import-csv-blockers`, (e) => e.textContent), /Line 4 has a value amount cannot take/);
  assert.equal((await nextState("Review…")).aria, "true", "a blocker keeps Review off");
  assert.match(await page.$eval(`${sheet} [aria-label="Problems by line"]`, (e) => e.textContent), /4amountabcNot a number/);
  const loadData = await page.$eval(`${sheet} .segmented[aria-label="Method"] [data-value=load_data]`, (e) => ({ disabled: e.disabled, title: e.title }));
  assert.equal(loadData.disabled, true, "LOAD DATA LOCAL is off while the server has local_infile OFF");
  assert.match(await sheetText(), /LOAD DATA LOCAL is off: The server has local_infile OFF \(MySQL's default\), so it refuses LOAD DATA LOCAL\. myflames never changes it;/);
  assert.equal((await sheetText()).match(/never changes it|does not change/g).length, 1, "the backend's reason is shown once");
  await shoot("step3-check-blocked");
  // Every choice checks again.
  let planned = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="Bad rows"] [data-value=skip]`);
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  assert.equal(lastPlan().on_error, "skip");
  await page.waitForFunction(() => !document.querySelector("dialog.import-csv-sheet .import-csv-blockers"));
  planned = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="If something fails"] [data-value=every]`);
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  assert.deepEqual(lastPlan().transaction, { mode: "every", every: 1000 });
  await page.$eval(`${sheet} input[aria-label="Rows per commit"]`, (e) => e.select());
  await page.keyboard.type("0");
  await waitText(`${sheet} .import-csv-field .form-error`, /A whole number of rows from 1 to 1,000,000/);
  await page.$eval(`${sheet} input[aria-label="Rows per commit"]`, (e) => e.select());
  planned = calls.plan.length;
  await page.keyboard.type("500");
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  assert.deepEqual(lastPlan().transaction, { mode: "every", every: 500 });
  await page.click(`${sheet} .segmented[aria-label="If something fails"] [data-value=all]`);
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll("dialog.import-csv-sheet .sheet-actions button")].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  });
  assert.deepEqual(lastPlan().transaction, { mode: "all", every: 0 });
  // A check that is superseded is stopped on the server too (it reads the whole file), not only its fetch.
  const stopped = (id) => calls.stop.some((x) => x.progress_id === id);
  const waitStopped = async (id, why) => {
    for (let i = 0; i < 150 && !stopped(id); i++) await pause(20);
    assert.ok(stopped(id), why);
  };
  planDelay = 1500;
  planned = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="If something fails"] [data-value=every]`);
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  const slowEvery = lastPlan();
  assert.deepEqual(slowEvery.transaction, { mode: "every", every: 500 });
  await page.waitForSelector(`${sheet} .import-csv-line[data-phase=pending]`);
  // A new choice while it runs: the old check is stopped and a new one starts.
  await page.$eval(`${sheet} input[aria-label="Rows per commit"]`, (e) => e.select());
  await page.keyboard.type("200");
  await waitStopped(slowEvery.progress_id, "a superseded check is stopped on the server");
  for (let i = 0; i < 100 && lastPlan().transaction.every !== 200; i++) await pause(20);
  const slow200 = lastPlan();
  assert.deepEqual(slow200.transaction, { mode: "every", every: 200 });
  // Back to the choices the checked plan already answers: the running check is stopped, none starts.
  planned = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="If something fails"] [data-value=all]`);
  await waitStopped(slow200.progress_id, "a check made needless by going back is stopped");
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll("dialog.import-csv-sheet .sheet-actions button")].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  });
  await pause(1800);
  assert.equal(calls.plan.length, planned, "the plan on screen still answers; no other check starts");
  assert.equal(await page.$(`${sheet} .import-csv-line[data-phase=pending]`), null, "no stale check line");
  planDelay = 0;
  await shoot("step3-check-ready");

  // ---- Step 4: Review. Cancel focused; Back records nothing; Escape declines once.
  await inSheet("Review…");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Review/);
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel");
  const review = await sheetText();
  assert.match(review, /Import 12,344 rows into shop\.orders\?/);
  assert.match(review, /INSERT INTO `shop`\.`orders` \(`order_id`, `customer`, `amount`, `note`\) VALUES \.\.\. : 12,344 rows in 13 statements from Orders 2026\.csv/);
  assert.match(review, /First statement|The first statement, shortened/);
  assert.match(review, /Allowed INSERT: Allowed through INSERT on shop\.\*\./);
  assert.match(review, /Production: every change on Lab production asks first\./);
  assert.doesNotMatch(review, /CREATE TABLE, sent first/);
  assert.match(review, /First, in the import's own session:SET SESSION sql_mode = CONCAT\(@@SESSION\.sql_mode, ',STRICT_ALL_TABLES'\);\nSET SESSION lock_wait_timeout = 5;/);
  assert.equal(await page.$eval(`${sheet} .sheet-actions .action-button`, (e) => e.textContent), "Import");
  assert.equal(await page.$eval(sheet, (e) => e.getAttribute("role")), "alertdialog");
  await shoot("step4-review-existing");
  await inSheet("Back");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Check/);
  assert.equal(calls.declined.length, 0, "Back records nothing");
  await inSheet("Review…");
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel");
  await closeSheet();
  assert.equal(calls.declined.length, 1, "Escape on the review records one declined attempt");
  assert.equal(calls.declined[0].operation, "import_csv");
  assert.ok(calls.declined[0].statements.some((s) => s.startsWith("INSERT INTO `shop`.`orders`")), "the data summary, never row data");
  assert.equal(calls.declined[0].connection.host, "127.0.0.1");
  assert.equal(calls.apply.length, 0);

  // Cancel on the review also declines.
  await openFromTable();
  await toReview();
  await inSheet("Cancel");
  await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), {}, sheet);
  assert.equal(calls.declined.length, 2, "Cancel on the review records a declined attempt");

  // ---- Step 5: Import, progress, Done; Close leaves the result in the view.
  await openFromTable();
  await toReview();
  applyMode = "done";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Import/);
  await waitText(`${sheet} .import-csv-meter`, /of 12,344 rows · batch \d+ of 13/);
  assert.equal(calls.apply.length, 1);
  assert.match(calls.apply[0].token, /^tok-/);
  assert.equal(calls.apply[0].progress_id.length, 32);
  assert.equal(calls.apply[0].connection.host, "127.0.0.1");
  assert.equal(await page.$eval(`${sheet} .sheet-actions button`, (e) => e.disabled), true, "Close waits until the import is slow");
  await shoot("step5-progress");
  await waitText(`${sheet} .import-csv-outcome`, /Done/);
  await page.waitForFunction(() => document.activeElement?.textContent === "Close");
  const done = await page.$eval(`${sheet} .import-csv-outcome`, (e) => e.textContent);
  assert.match(done, /Imported 12,344 rows into shop\.orders\. 1 bad row skipped\./);
  assert.match(done, /Rows committed12,344/);
  assert.match(done, /The table now has12,386 rows/);
  assert.match(done, /Recorded in the Audit log\./);
  assert.match(done, /Rows not imported \(1\)/);
  assert.match(done, /4amountabcNot a number/);
  await shoot("step5-result-done");
  await inSheet("Close");
  await waitText(".query-lab .import-csv-status", /Import CSV: Done\. 12,344 rows committed into shop\.orders\./);
  await page.locator(".query-lab .import-csv-status button ::-p-text(Show result)").click();
  await page.waitForSelector(`${sheet}[open]`);
  await waitText(`${sheet} .import-csv-outcome`, /Done/);
  await inSheet("Close");
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();
  await page.waitForFunction(() => !document.querySelector(".query-lab .import-csv-status"));
  assert.equal(calls.apply.length, 1, "one import sends once");

  // Stop: Stopping…, one stop request, then Stopped.
  await openFromTable();
  await toReview();
  applyMode = "stop";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-meter`, /of 12,344 rows/);
  await page.locator(`${sheet} .sheet-actions button ::-p-text(Stop)`).click();
  await waitText(`${sheet} .sheet-actions`, /Stopping…/);
  await waitText(`${sheet} .import-csv-step`, /Stopping after the statement with the server…/);
  await waitText(`${sheet} .import-csv-step`, /Stopping: nothing is kept; the transaction rolls back\./);
  assert.equal(calls.stop.filter((x) => x.progress_id === calls.apply[1].progress_id).length, 1, "one stop for the import");
  await waitText(`${sheet} .import-csv-outcome`, /Stopped/);
  assert.match(await sheetText(), /Stopped\. Nothing was kept: the transaction rolled back\./);
  await shoot("step5-result-stopped");
  // Its Audit log record reads Stopped and Import CSV, never Done or "answered without an error".
  await page.locator(`${sheet} .import-csv-outcome button ::-p-text(Open the Audit log)`).click();
  const auditRow = '.server-view[data-view=audit] [aria-label="Audit records"] tr[data-row-key="audit-7"]';
  await page.waitForSelector(auditRow);
  assert.match(await page.$eval(auditRow, (e) => e.textContent), /Stopped\s*Import CSV/);
  await page.waitForFunction(() => /Stopped on request/.test(document.querySelector(".view-inspector")?.textContent || ""));
  const auditInspector = await page.$eval(".view-inspector", (e) => e.textContent);
  assert.match(auditInspector, /Import CSV/);
  assert.doesNotMatch(auditInspector, /answered without an error|Done/);
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/audit-stopped-import-1440x900-light.png` });
  await h.button("Query editor", ".main-nav ");
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // Failed: the failing line and the server's error.
  await openFromTable();
  await toReview();
  applyMode = "failed";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-outcome`, /Failed/);
  const failed = await page.$eval(`${sheet} .import-csv-outcome`, (e) => e.textContent);
  assert.match(failed, /Nothing was kept: the import rolled back at line 4\./);
  assert.match(failed, /Failing lineLine 4: Incorrect decimal value/);
  assert.match(failed, /Server errorERROR 1366: Incorrect decimal value/);
  await shoot("step5-result-failed");
  await closeSheet();
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // No reply: Outcome unknown, never sent again.
  await openFromTable();
  await toReview();
  applyMode = "unknown";
  const applied = calls.apply.length;
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-outcome`, /Outcome unknown/);
  assert.match(await sheetText(), /It may have run, so it is not sent again\..*check shop\.orders before importing again/);
  assert.match(await sheetText(), /If the import reached the server, the Audit log has its record\./);
  await pause(1500);
  assert.equal(calls.apply.length, applied + 1, "an unknown outcome is never retried");
  await shoot("step5-result-unknown");
  await closeSheet();
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // Refused before sending (4xx): Nothing sent, then Check again plans afresh.
  await openFromTable();
  await toReview();
  applyMode = "refused";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-outcome`, /Nothing sent/);
  assert.match(await sheetText(), /This review expired\. Nothing was sent\./);
  assert.doesNotMatch(await page.$eval(`${sheet} .import-csv-outcome`, (e) => e.textContent), /Audit log/,
    "a refusal without a record does not claim one");
  planned = calls.plan.length;
  await inSheet("Check again");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Check/);
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  assert.equal(calls.plan.length, planned + 1, "Check again plans afresh");
  await closeSheet();
  // A refusal the backend recorded names its record.
  await openFromTable();
  await toReview();
  applyMode = "refused-audited";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-outcome`, /Nothing sent/);
  assert.match(await page.$eval(`${sheet} .import-csv-outcome`, (e) => e.textContent), /The file changed since the check.*Recorded in the Audit log\. ?Open the Audit log/);
  await closeSheet();
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // Closing the sheet while a check runs stops the check on the server.
  await openFromTable();
  await chooseFile();
  await inSheet("Next");
  await page.waitForFunction(() => document.querySelector('dialog.import-csv-sheet select[aria-label="Table column for Note"]')?.value === "note");
  planDelay = 1500;
  planned = calls.plan.length;
  await inSheet("Next");
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  const leftCheck = lastPlan();
  await page.waitForSelector(`${sheet} .import-csv-line[data-phase=pending]`);
  await closeSheet();
  await waitStopped(leftCheck.progress_id, "closing the sheet stops its check");
  planDelay = 0;

  // A server with local_infile ON: LOAD DATA LOCAL can be chosen, and the review names it.
  localInfile = true;
  await openFromTable();
  await toReview();
  await inSheet("Back");
  await page.waitForFunction(() => !document.querySelector('dialog.import-csv-sheet .segmented[aria-label="Method"] [data-value=load_data]')?.disabled);
  planned = calls.plan.length;
  await page.click(`${sheet} .segmented[aria-label="Method"] [data-value=load_data]`);
  for (let i = 0; i < 100 && calls.plan.length === planned; i++) await pause(20);
  assert.equal(lastPlan().method, "load_data");
  await waitText(`${sheet} .import-csv-field`, /Faster: the server reads a checked copy of the rows, in 1 statement\. myflames checks every row first/);
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll("dialog.import-csv-sheet .sheet-actions button")].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  });
  await inSheet("Review…");
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel");
  assert.equal(await page.$eval(`${sheet} .eyebrow`, (e) => e.textContent), "LOAD DATA LOCAL INFILE");
  assert.match(await sheetText(), /MethodLOAD DATA LOCAL INFILE \(1 statement; the server reads a checked copy of the rows\)/);
  await closeSheet();
  localInfile = false;

  // A slow import: Close after 10 s keeps the request; its line shows progress, then the outcome.
  await openFromTable();
  await toReview();
  applyMode = "slow";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-meter`, /of 12,344 rows/);
  await page.waitForFunction(() => {
    const b = document.querySelector("dialog.import-csv-sheet .sheet-actions button");
    return b && b.textContent === "Close" && !b.disabled;
  }, { timeout: 15000 });
  assert.match(await sheetText(), /Close keeps the import running/);
  await inSheet("Close");
  await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), {}, sheet);
  await waitText(".query-lab .import-csv-status", /Importing Orders 2026\.csv into shop\.orders: [\d,]+ of 12,344 rows\./);
  assert.ok(await page.$(".query-lab .import-csv-status button"), "Show import and Stop stay in the view");
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/view-status-running-1440x900-light.png` });
  // Opening Import CSV again shows the running import, never a second one.
  await openFromTable();
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Import/);
  await page.waitForFunction(() => !document.querySelector("dialog.import-csv-sheet .sheet-actions button")?.disabled, { timeout: 5000 });
  await inSheet("Close");
  await waitText(".query-lab .import-csv-status", /Import CSV: Done\./);
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/view-status-done-1440x900-light.png` });
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // ---- New table from the database menu: name from the file, types, id key, CREATE TABLE.
  await treeMenu("schema", "shop");
  await page.locator('.schema-browser .context-menu [role=menuitem] ::-p-text(Import CSV into a new table…)').click();
  await page.waitForSelector(`${sheet}[open]`);
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "Import CSV into a new table in shop");
  await chooseFile();
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "Import CSV into a new table shop.orders_2026");
  await inSheet("Next");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Target and columns/);
  assert.equal(await pressed("Into"), "New table");
  assert.equal(await page.$eval(`${sheet} .import-csv-field input`, (e) => e.value), "orders_2026", "the name comes from the file");
  await page.waitForSelector(`${sheet} [aria-label="Columns of the new table"]`);
  const names = await page.$$eval(`${sheet} [aria-label="Columns of the new table"] tbody input[aria-label^="Name of"]`, (els) => els.map((e) => e.value));
  assert.deepEqual(names, ["order_id", "customer", "amount", "note"]);
  const types = await page.$$eval(`${sheet} [aria-label="Columns of the new table"] tbody select`, (els) => els.map((e) => e.value));
  assert.deepEqual(types, INFERRED, "suggested types first");
  assert.equal(await page.$eval(`${sheet} .import-csv-columns .checkbox input`, (e) => e.checked), true, "Add an id primary key is on");
  // An existing name is refused with what to do.
  await page.$eval(`${sheet} .import-csv-field input`, (e) => e.select());
  await page.keyboard.type("orders");
  await waitText(`${sheet} .import-csv-warnings`, /shop\.orders exists\. Choose Existing table to add rows to it, or another name\./);
  await page.$eval(`${sheet} .import-csv-field input`, (e) => e.select());
  await page.keyboard.type("orders_2026");
  // Another type: Other… with a typed type, checked before planning.
  await page.select(`${sheet} select[aria-label="Type of amount"]`, "__other");
  await page.waitForSelector(`${sheet} input[aria-label="Other type of amount"]`);
  await page.$eval(`${sheet} input[aria-label="Other type of amount"]`, (e) => e.select());
  await page.keyboard.type("DECIMAL(12;2)");
  await waitText(`${sheet} .import-csv-warnings`, /Column amount: DECIMAL takes whole numbers in parentheses\./);
  assert.equal((await nextState("Next")).aria, "true", "a type the backend refuses keeps Next off");
  // The backend's grammar exactly: what it refuses never reaches /plan (test/import-csv-unit.mjs has the full table).
  for (const [typed, why] of [["DECIMAL(10,2) UNSIGNED", /UNSIGNED is deprecated for DECIMAL; use a CHECK in the Query editor\./],
    ["INT ZEROFILL", /INT ZEROFILL is not a type Import CSV can create\./], ["DOUBLE PRECISION", /DOUBLE PRECISION is not a type Import CSV can create\./],
    ["ENUM('a\\\\b')", /ENUM members must be quoted text without backslashes or control characters\./]]) {
    await page.$eval(`${sheet} input[aria-label="Other type of amount"]`, (e) => e.select());
    await page.keyboard.type(typed);
    await waitText(`${sheet} .import-csv-warnings`, why);
  }
  await page.$eval(`${sheet} input[aria-label="Other type of amount"]`, (e) => e.select());
  await page.keyboard.type("DECIMAL(12,2)");
  await page.waitForFunction(() => !document.querySelector("dialog.import-csv-sheet .import-csv-warnings"));
  await shoot("step2-new-table");
  await inSheet("Next");
  await waitText(`${sheet} .import-csv-summary`, /12,345 rows/);
  const newPlan = lastPlan();
  assert.equal(newPlan.target.mode, "new");
  assert.equal(newPlan.target.table, "orders_2026");
  assert.equal(newPlan.target.add_id_key, true);
  assert.deepEqual(newPlan.target.new_columns.map((c) => [c.name, c.sql_type]), [["order_id", "INT UNSIGNED"], ["customer", "VARCHAR(40)"], ["amount", "DECIMAL(12,2)"], ["note", "VARCHAR(40)"]]);
  assert.deepEqual(newPlan.target.mapping, [0, 1, 2, 3].map((i) => ({ csv_index: i, column: names[i] })));
  await page.click(`${sheet} .segmented[aria-label="Bad rows"] [data-value=skip]`);
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll("dialog.import-csv-sheet .sheet-actions button")].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  });
  await inSheet("Review…");
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel");
  const newReview = await sheetText();
  assert.match(newReview, /CREATE TABLE, sent first on its own/);
  assert.match(newReview, /CREATE TABLE `shop`\.`orders_2026` \(/);
  assert.match(newReview, /DROP TABLE `shop`\.`orders_2026`;/);
  assert.match(newReview, /The new table stays, empty/);
  assert.match(newReview, /Unverified CREATE TABLE: Not verified/);
  assert.match(await page.$eval(`${sheet} .eyebrow`, (e) => e.textContent), /CREATE TABLE · INSERT/);
  await shoot("step4-review-new-table");
  applyMode = "new-failed";
  await inSheet("Import");
  await waitText(`${sheet} .import-csv-outcome`, /Failed/);
  const newFailed = await page.$eval(`${sheet} .import-csv-outcome`, (e) => e.textContent);
  assert.match(newFailed, /The new table is still there/);
  assert.match(newFailed, /DROP TABLE `shop`\.`orders_2026`;/);
  assert.match(newFailed, /Copy/);
  assert.match(newFailed, /Server warnings2/);
  await shoot("step5-result-new-table-failed");
  await closeSheet();
  await page.locator(".query-lab .import-csv-status button ::-p-text(Dismiss)").click();

  // ---- Keyboard only: the menu from Shift+F10, every step by Tab, Enter, Space and arrows.
  const tabTo = async (match) => {
    for (let i = 0; i < 80; i++) {
      if (await page.evaluate(match)) return;
      await page.keyboard.press("Tab");
    }
    assert.fail(`Tab never reached ${match}`);
  };
  await page.focus('.schema-browser .tree-row.kind-table[data-node$="orders"]');
  await page.keyboard.down("Shift"); await page.keyboard.press("F10"); await page.keyboard.up("Shift");
  await page.waitForSelector(".schema-browser .context-menu[role=menu]");
  for (let i = 0; i < 12 && !(await page.evaluate(() => document.activeElement?.textContent === "Import CSV…")); i++) await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${sheet}[open]`);
  chooseMode = "file";
  await tabTo(() => document.activeElement?.textContent === "Choose file…");
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${sheet} [aria-label="Preview of the first rows"]`);
  await idleReading();
  await tabTo(() => document.activeElement?.textContent === "Next" && document.activeElement.getAttribute("aria-disabled") !== "true");
  await page.keyboard.press("Enter");
  await waitText(`${sheet} .import-csv-steps [aria-current=step]`, /Target and columns/);
  await page.waitForFunction(() => document.querySelector('dialog.import-csv-sheet select[aria-label="Table column for Note"]')?.value === "note");
  await tabTo(() => document.activeElement?.textContent === "Next" && document.activeElement.getAttribute("aria-disabled") !== "true");
  await page.keyboard.press("Enter");
  await waitText(`${sheet} .import-csv-summary`, /12,345 rows/);
  await tabTo(() => document.activeElement?.closest('.segmented[aria-label="Bad rows"]') !== null);
  await page.keyboard.press("ArrowRight");
  assert.equal(await pressed("Bad rows"), "Skip and report");
  await page.waitForFunction(() => {
    const b = [...document.querySelectorAll("dialog.import-csv-sheet .sheet-actions button")].find((e) => e.textContent.trim() === "Review…");
    return b && b.getAttribute("aria-disabled") !== "true";
  });
  await tabTo(() => document.activeElement?.textContent === "Review…");
  await page.keyboard.press("Space");
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel");
  const declinedBefore = calls.declined.length;
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), {}, sheet);
  assert.equal(calls.declined.length, declinedBefore + 1);
  await page.waitForFunction(() => document.activeElement?.closest(".schema-browser"), { timeout: 3000 });

  // ---- Off with the reason: read-only, a proven missing privilege; unknown stays on.
  await h.setPrivileges({ insert: privilegeAnswer("denied", "INSERT"), create_table: privilegeAnswer("denied", "CREATE") });
  await treeMenu("table", "orders");
  let item = await menuItem("Import CSV…");
  assert.equal(item.aria, "true");
  assert.match(item.text, /Needs INSERT/);
  await page.locator('.schema-browser .context-menu [role=menuitem] ::-p-text(Import CSV…)').click();
  await pause(300);
  assert.equal(await page.$(`${sheet}[open]`), null, "an item that is off opens nothing");
  await page.keyboard.press("Escape");
  await treeMenu("schema", "shop");
  assert.match((await menuItem("Import CSV into a new table…")).text, /Needs CREATE/);
  await page.keyboard.press("Escape");
  await h.setPrivileges({ insert: privilegeAnswer("unknown", "INSERT"), create_table: privilegeAnswer("unknown", "CREATE") });
  await treeMenu("table", "orders");
  assert.equal((await menuItem("Import CSV…")).aria, null, "unknown leaves it enabled");
  await page.keyboard.press("Escape");
  await chooseConnection(page, readOnly);
  await h.button("Load schemas", ".schema-browser ");
  await expandShop();
  await treeMenu("table", "orders");
  item = await menuItem("Import CSV…");
  assert.equal(item.aria, "true");
  assert.match(item.text, /This connection is read-only\./);
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/entry-schemas-read-only-1440x900-light.png` });
  await page.keyboard.press("Escape");
  await chooseConnection(page, prod);

  // ---- Server > Tables: row menu and inspector action, pre-filled.
  await h.view("tables");
  const row = '[aria-label="Tables without a primary key"] tbody tr[data-table="mftab.no_key"]';
  await page.waitForSelector(row);
  await page.click(`${row} td:nth-child(2)`, { button: "right" });
  await page.waitForSelector(".context-menu[role=menu][aria-label='Row actions']");
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/entry-tables-row-menu-1440x900-light.png` });
  await page.locator(".context-menu[aria-label='Row actions'] [role=menuitem] ::-p-text(Import CSV…)").click();
  await page.waitForSelector(`${sheet}[open]`);
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "Import CSV into mftab.no_key");
  await closeSheet();
  await h.select(row);
  await page.waitForSelector(".view-inspector [data-action=import-csv]");
  if (shotsDir) await page.screenshot({ path: `${shotsDir}/entry-tables-inspector-1440x900-light.png` });
  await page.click(".view-inspector [data-action=import-csv]");
  await page.waitForSelector(`${sheet}[open]`);
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "Import CSV into mftab.no_key");
  await chooseFile();
  await inSheet("Next");
  await page.waitForSelector(`${sheet} [aria-label="CSV columns and the table columns they fill"] select`);
  assert.equal(await page.$eval(`${sheet} .import-csv-field select`, (e) => e.value), "mftab");
  assert.ok(calls.catalog.some((c) => c.schema === "mftab" && c.table === "no_key"), "the target's columns are read");
  await closeSheet();
  await chooseConnection(page, readOnly);
  await page.waitForSelector(".view-inspector [data-action=import-csv]");
  // The list now belongs to another connection: off with the reason.
  await page.waitForFunction(() => document.querySelector(".view-inspector [data-action=import-csv]")?.getAttribute("aria-disabled") === "true");

  assert.deepEqual(h.errors, []);
  if (shotsDir) await fs.writeFile(`${shotsDir}/measurements.json`, JSON.stringify(measured, null, 2));
  console.log(`import-csv: ${calls.inspect.length} inspects, ${calls.plan.length} plans, ${calls.apply.length} imports, ${calls.declined.length} declines; every step, separator, outcome, entry point, keyboard and 720 px passed`);
} finally {
  await h.close();
}
