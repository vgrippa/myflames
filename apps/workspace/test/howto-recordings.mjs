// How-to guide recordings (docs/screenshots/howto-<id>.gif): short animated
// GIFs of common tasks, driven through the real built workspace the same way
// readme-screenshots.mjs captures the PNGs (perf harness, throwaway HOME and
// store, fixture-backed reads, frozen clock, nothing outside 127.0.0.1).
//
// While a guide records, a caption near the bottom right names the keys being
// pressed and a dot follows the pointer; both are injected into the page by
// this script only and are never part of the app bundle.
//
// Frames are page screenshots at a fixed rate, encoded with gifski. A GIF
// over 1.5 MB is re-encoded smaller (quality, then width, then fps).
//
//   npm run build && npm run docs:howtos      (writes docs/screenshots)
//   node test/howto-recordings.mjs --only multi-cursor
//   node test/howto-recordings.mjs --out DIR  (writes DIR instead)
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection } from "./chrome-helpers.mjs";
import { EDITOR, editorSelection, setEditorText } from "./editor-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const onlyFlag = process.argv.indexOf("--only");
const only = onlyFlag > 0 ? process.argv[onlyFlag + 1] : "";
const outFlag = process.argv.indexOf("--out");
const out = path.resolve(outFlag > 0 ? process.argv[outFlag + 1] : path.join(root, "docs/screenshots"));
const GIFSKI = "/opt/homebrew/bin/gifski";
const SIZE = { width: 1280, height: 800 };
const FPS = 10;
const MAX_BYTES = 1.5 * 1024 * 1024;
// The same frozen moment as the README screenshots.
const CLOCK = "2026-10-14T16:26:00Z";
const RUN_SQL = "SELECT id, customer_id, status, total FROM orders WHERE status = 'new' ORDER BY id LIMIT 24;";

await fs.access(GIFSKI).catch(() => {
  throw new Error(`gifski not found at ${GIFSKI}: install it (brew install gifski) to record the how-to GIFs.`);
});
await fs.access(path.join(root, "myflames/ui_assets/index.html")).catch(() => {
  throw new Error("No built workspace in myflames/ui_assets: run `npm run build` in apps/workspace first.");
});
await fs.mkdir(out, { recursive: true });

// ---- fixtures (as readme-screenshots.mjs, plus capture jobs for Explore) ----
const health = await load("health-mysql-8.4-full.json");
const schemas = await load("schemas-mysql-8.4.json");
schemas.schemas = [{ name: "shop", tables: 4, system: false }, ...schemas.schemas.filter((s) => s.system)];
schemas.user_tables = 4;
const ORDERS_COLUMNS = ["id", "customer_id", "status", "total", "created_at"]
  .map((name) => ({ name, type: name === "id" || name === "customer_id" ? "int" : name === "total" ? "decimal(10,2)" : name === "created_at" ? "datetime" : "varchar(20)", nullable: false, key: name === "id" ? "PRI" : "", default: null, extra: "", generated: false }));
const catalog = (body) => body.table
  ? { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table, columns: ORDERS_COLUMNS, indexes: [] }
  : { schema_version: "catalog-1.0", level: "tables", schema: body.schema, truncated: false, limit: 200, prefix: null, after: null,
    next_after: null, tables: [{ name: "customers", type: "table" }, { name: "order_items", type: "table" }, { name: "orders", type: "table" }, { name: "products", type: "table" }] };
// The mocked session reflects the transaction the recording opened: only the
// transaction guide sets txOpen, and it rolls back before it ends.
let txOpen = false;
const session = (over = {}) => ({
  schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
  connection: { profile_id: null, name: "Dev MySQL", host: "dev.example", port: 3306, user: "dba", type: "development", read_only: false },
  engine: "mysql", server_version: "8.4.11", connection_id: 42, autocommit: true, in_transaction: txOpen,
  transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
  backslash_escapes: true, safe_updates: true, safe_updates_default: true, read_only_transactions: false,
  started_at: CLOCK, idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null, message: null, notes: [], run: null,
  ...over,
});
const READ_VERBS = new Set(["select", "show", "with", "explain", "describe", "rollback"]);
const statement = (sql) => {
  const verb = sql.trim().split(/\s+/)[0].toLowerCase().replace(/;$/, "");
  const kind = READ_VERBS.has(verb) ? "read" : "write";
  return { index: 0, text: sql, start: 0, end: sql.length, verb, kind, category: kind, status: "ok", duration_ms: 12 };
};
const runBase = (sql) => ({
  schema_version: "editor-run-1.0", id: "run-1", kind: "run", state: "complete", scope: "selection",
  started_at: CLOCK, finished_at: CLOCK, duration_ms: 12, limit: 1000, statements: [statement(sql)],
  error: null, stopped: null, message: null, audit_id: null, history_id: null,
});
// A write's reply (no rows): START TRANSACTION opens the session's
// transaction, Roll back in the transaction pill closes it.
const writeRun = (sql, affected = 0) => ({ ...runBase(sql),
  results: [{ type: "ok", statement: 0, affected_rows: affected, info: null, warnings: 0, duration_ms: 3 }],
  counts: { statements: 1, run: 1, rows: 0, affected_rows: affected, warnings: 0, errors: 0 } });
const editorRun = (sql) => {
  // The transaction guide re-reads the row it just updated.
  const one = /WHERE id = 7/i.test(sql);
  const columns = (one ? ["id", "status"] : ["id", "customer_id", "status", "total", "created_at"]).map((name) => ({ name, hex: false, type: null }));
  const rows = one ? [["7", "paid"]] : Array.from({ length: 24 }, (_, i) => [String(i + 1), String(100 + i), i % 3 ? "paid" : "new",
    (19.5 + i * 3.25).toFixed(2), `2026-10-${String(1 + (i % 28)).padStart(2, "0")} 10:00:00`]);
  return { ...runBase(sql),
    results: [{ type: "rows", statement: 0, columns, columns_known: true, rows, row_count: rows.length, truncated: false,
      limit: 1000, stopped: null, label: null, server_rows: rows.length, warnings: 0, duration_ms: 12 }],
    counts: { statements: 1, run: 1, rows: rows.length, affected_rows: 0, warnings: 0, errors: 0 } };
};
const explainRun = (sql) => {
  const columns = ["id", "select_type", "table", "partitions", "type", "possible_keys", "key", "key_len", "ref", "rows", "filtered", "Extra"]
    .map((name) => ({ name, hex: false, type: null }));
  const rows = [["1", "SIMPLE", "orders", null, "ALL", "status", null, null, null, "24", "100.00", "Using where"]];
  return { ...runBase(sql),
    results: [{ type: "rows", statement: 0, columns, columns_known: true, rows, row_count: 1, truncated: false,
      limit: 1000, stopped: null, label: null, server_rows: 1, warnings: 0, duration_ms: 3 }],
    counts: { statements: 1, run: 1, rows: 1, affected_rows: 0, warnings: 0, errors: 0 } };
};
// Explore captures EXPLAIN FORMAT=JSON (MySQL format 2), without actual
// execution counters. Adapt the simple scan fixture to the editor's query.
const scanFixture = JSON.parse(await fs.readFile(path.join(root, "test/fixtures/explain-001-table-scan-users-no-filter.json"), "utf8"));
const PLAN_RAW = JSON.stringify({
  ...Object.fromEntries(Object.entries(scanFixture).filter(([key]) => !key.startsWith("actual_"))),
  query: RUN_SQL, operation: "Table scan on orders", table_name: "orders", schema_name: "shop",
  used_columns: ["id", "customer_id", "status", "total"],
});
const CAPTURE = { schema_version: "capture-1.0", raw: PLAN_RAW, query: RUN_SQL, mode: "explain", engine: "mysql",
  server_version: "8.4.11", captured_at: CLOCK, conditions: "", warnings: [], runs: [], measurements: { count: 0 },
  schema: {}, table_stats: {}, variables: {}, trace: null };
const ok = (body) => ({ status: 200, body });
// Run sends the editor's whole text plus the caret; the session runs the one
// statement at the caret. The mock picks it the same way (the recorded
// scripts have no procedure bodies).
const atCursor = (body) => {
  const full = body.sql || "";
  if (typeof body.cursor !== "number") return full.trim();
  let from = 0;
  for (let i = 0; i < body.cursor && i < full.length; i++) if (full[i] === ";") from = i + 1;
  const end = full.indexOf(";", body.cursor);
  return full.slice(from, end < 0 ? full.length : end + 1).trim();
};

// Edit table data… (guide: edit-table-rows): an editable orders grid; after
// Apply changes the reloaded page shows the saved value.
let tableApplied = false;
const cell = (value) => ({ kind: "text", value });
const tablePage = () => ({
  snapshot_id: "snapshot", schema: "shop", table: "orders",
  columns: [{ name: "id", type: "int", kind: "text", nullable: false },
    { name: "customer_id", type: "int", kind: "text", nullable: false },
    { name: "status", type: "varchar(20)", kind: "text", nullable: false },
    { name: "total", type: "decimal(10,2)", kind: "text", nullable: false }],
  rows: [
    { id: "r1", values: [cell("7"), cell("102"), cell(tableApplied ? "paid" : "new"), cell("42.50")] },
    { id: "r2", values: [cell("8"), cell("104"), cell("paid"), cell("19.75")] },
    { id: "r3", values: [cell("9"), cell("105"), cell("new"), cell("58.00")] },
  ],
  editable: true, reason: "", key: ["id"], offset: 0, limit: 50, has_more: false, expires_in: 300 });

// Import CSV… (guide: import-csv): a small clean file into shop.orders, the
// contracts of docs/schemas (csv-inspect-1.0, csv-import-plan-1.0 and
// csv-import-result-1.0), simplified from import-csv.mjs.
const CSV_FILE = { file_id: "file-1", name: "orders-2026-10.csv", size: 481, modified: "2026-10-14T09:12:00Z" };
const CSV_HEAD = ["id", "customer_id", "status", "total"];
const CSV_ROWS = [["101", "103", "new", "42.50"], ["102", "104", "paid", "19.75"], ["103", "105", "paid", "58.00"],
  ["104", "103", "new", "12.00"], ["105", "106", "new", "77.30"], ["106", "104", "paid", "23.40"]];
const CSV_FORMAT = { encoding: "utf-8", separator: ",", quote: '"', escape: "", header: true, line_ending: "\n", skip_lines: 0, empty: "null", null_markers: ["\\N"] };
let csvApplying = false;
const csvInspect = (body) => ({ schema_version: "csv-inspect-1.0", file: CSV_FILE, detected: CSV_FORMAT, format: body.format || CSV_FORMAT,
  columns: CSV_HEAD.map((header, i) => ({ index: i, header, name: header,
    inferred: { sql_type: ["INT UNSIGNED", "INT UNSIGNED", "VARCHAR(20)", "DECIMAL(10,2)"][i], nullable: false, max_length: 8 },
    samples: CSV_ROWS.slice(0, 3).map((r) => r[i]) })),
  preview: { rows: CSV_ROWS.map((r) => [...r]), lines: CSV_ROWS.map((_r, i) => i + 2), truncated: false },
  rows: CSV_ROWS.length, rows_estimate: null, rows_counted: true, problems: [], problems_total: 0,
  notes: ["Line endings: LF."] });
const csvPlan = (body) => {
  const list = body.target.mapping.filter((m) => m.csv_index !== null).map((m) => `\`${m.column}\``).join(", ");
  return ok({ schema_version: "csv-import-plan-1.0", token: "tok-1", ready: true, blockers: [], expires_in: 600,
    file: { ...CSV_FILE, sha256: "ab".repeat(32) }, format: body.format || CSV_FORMAT,
    connection: { name: "Dev MySQL", host: "dev.example", port: 3306, user: "dba", type: "development", read_only: false, engine: "mysql", server_version: "8.4.11" },
    target: { mode: body.target.mode, schema: "shop", table: "orders", exists: true, engine: "InnoDB", transactional: true,
      columns: [], rows_estimate: 24, triggers: 0, foreign_keys: 0, check_constraints: 0, primary_key: ["id"] },
    mapping: body.target.mapping.map((m) => ({ csv_index: m.csv_index, csv_header: m.csv_index === null ? "" : CSV_HEAD[m.csv_index],
      column: m.column, type: "varchar(40)" })),
    statements: { create: null,
      data_sample: `INSERT INTO \`shop\`.\`orders\` (${list}) VALUES (101, 103, 'new', 42.50), (102, 104, 'paid', 19.75) …`,
      data_summary: `INSERT INTO \`shop\`.\`orders\` (${list}) VALUES ... : 6 rows in 1 statement from orders-2026-10.csv`,
      setup: ["SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',STRICT_ALL_TABLES')", "SET SESSION lock_wait_timeout = 5"] },
    batches: 1, rows: { total: 6, valid: 6, invalid: 0, skipped: 0 }, problems: [], problems_total: 0,
    method: { chosen: body.method, load_data_available: false, local_infile: false, client_limits_files: false,
      why: "The server has local_infile OFF (MySQL's default), so it refuses LOAD DATA LOCAL. myflames never changes it; INSERT batches work either way." },
    transaction: { ...body.transaction, commits: 1 }, on_error: body.on_error,
    privileges: [{ action: "insert", state: "allowed", reason: "Allowed through INSERT on shop.*." }],
    policy: { action: "confirm", tone: "danger", title: "Risky write",
      text: "Connection Dev MySQL is a Development connection. myflames asks only before risky writes, and this statement is one.",
      connection_type: "development", read_only: false },
    warnings: [], notes: [], drop_hint: null });
};
const CSV_RESULT = { schema_version: "csv-import-result-1.0", outcome: "done", rows: { total: 6, sent: 6, committed: 6, skipped: 0 },
  failed_at: null, next_line: null, last_committed_line: null, server_error: null, warnings: { count: 0, items: [] },
  duration_ms: 840, table_created: false, table_rows_after: 30, drop_hint: null, audit: { id: "audit-1" },
  problems: [], problems_total: 0, method: "insert", transaction: { mode: "all", every: 0 } };

// Query digest baselines (guide: digest-baseline): the /api/baselines/*
// contract of baselines.py answered from memory, as query-digest-baselines.mjs
// does, over the recorded lab reading fixtures/top-mysql-8.4-lab.json.
const digestRecorded = await load("top-mysql-8.4-lab.json");
const DIGEST_SERVER_TIME = "2026-10-31 10:18:52.000000";
const DIGEST_SERVER = { engine: "mysql", flavor: "mysql", product: "MySQL", version: digestRecorded.server_version,
  server_uuid: "3e11fa47-71ca-11e1-9e33-c80aa9429562", hostname: "lab", port: 3306 };
const DIGEST_KEY = `mysql/uuid/${DIGEST_SERVER.server_uuid}`;
const digestTop = { ...digestRecorded, coverage: { ...digestRecorded.coverage, oldest_first_seen: "2026-10-31 09:48:00.000000" },
  reading_server: { verified: true, server_key: DIGEST_KEY, server: DIGEST_SERVER, uptime_s: digestRecorded.coverage.uptime_s + 1, server_time: DIGEST_SERVER_TIME } };
// A reading 600 s later on the same run: one digest new, two grown by over 50%.
const digestLater = structuredClone(digestTop);
digestLater.coverage.uptime_s = 2454;
const byDigest = (r, prefix) => r.statements.find((s) => s.digest.startsWith(prefix));
Object.assign(byDigest(digestLater, "2f09595d41"), { count: 753, total_ms: 6.789 + 600 * 0.05 });
Object.assign(byDigest(digestLater, "2dd7bae4c1"), { count: 93, total_ms: 5.961 + 30 * 0.5 });
digestLater.statements.push({ ...byDigest(digestTop, "2dd7bae4c1"), digest: "e".repeat(64),
  digest_text: "SELECT * FROM `audit_log` WHERE `ts` > ?", count: 120, total_ms: 360, avg_ms: 3,
  first_seen: "2026-10-31 10:25:00.000000", last_seen: "2026-10-31 10:28:00.000000", sample_text: null, new: false });
digestLater.statements.push({ ...byDigest(digestTop, "2f09595d41"), digest: "f".repeat(64),
  digest_text: "INSERT INTO `audit_log` (`ts`, `actor`, `action`) VALUES (?, ?, ?)", count: 96, total_ms: 288, avg_ms: 3,
  first_seen: "2026-10-31 10:26:00.000000", last_seen: "2026-10-31 10:28:30.000000", sample_text: null, new: false });
let digestReport = digestTop;
const digestBaselines = [];
let digestClock = Date.UTC(2026, 9, 14, 16, 26);
const keepDigests = (r) => ({
  schema_version: r.schema_version, engine: r.engine, server_version: r.server_version, server_product: r.server_product ?? null,
  order: r.order, limit: r.limit, read_at: r.read_at, server_time: DIGEST_SERVER_TIME,
  coverage: { uptime_s: r.coverage.uptime_s, counting_since: r.coverage.counting_since ?? null, oldest_first_seen: r.coverage.oldest_first_seen ?? null, digest_lost: r.coverage.digest_lost },
  totals: r.totals ?? null,
  statements: r.statements.map(({ digest, schema, digest_text, count, total_ms, avg_ms, max_ms, first_seen, last_seen }) =>
    ({ digest, schema, digest_text, count, total_ms, avg_ms, max_ms, first_seen, last_seen })),
});

// Database diagram (guide: diagram): four shop tables and their foreign keys,
// the model schema-diagram.mjs builds.
const diagramColumn = (name, data_type = "bigint", primary_key = false, nullable = false) => ({ name, data_type, primary_key, nullable });
const DIAGRAM = { schema_version: "schema-diagram-1.0",
  tables: [
    { id: "customers", schema: "shop", name: "customers", columns: [diagramColumn("id", "bigint", true), diagramColumn("name", "varchar(100)"), diagramColumn("email", "varchar(255)"), diagramColumn("created_at", "datetime")] },
    { id: "orders", schema: "shop", name: "orders", columns: [diagramColumn("id", "bigint", true), diagramColumn("customer_id"), diagramColumn("ordered_at", "datetime"), diagramColumn("status", "varchar(20)")] },
    { id: "order_items", schema: "shop", name: "order_items", columns: [diagramColumn("order_id", "bigint", true), diagramColumn("product_id", "bigint", true), diagramColumn("quantity", "int"), diagramColumn("price", "decimal(10,2)")] },
    { id: "products", schema: "shop", name: "products", columns: [diagramColumn("id", "bigint", true), diagramColumn("name", "varchar(100)"), diagramColumn("price", "decimal(10,2)"), diagramColumn("category_id")] },
  ],
  relationships: [
    { id: "orders_customer", name: "orders_customer", from_table: "orders", from_columns: ["customer_id"], to_table: "customers", to_columns: ["id"] },
    { id: "items_order", name: "items_order", from_table: "order_items", from_columns: ["order_id"], to_table: "orders", to_columns: ["id"] },
    { id: "items_product", name: "items_product", from_table: "order_items", from_columns: ["product_id"], to_table: "products", to_columns: ["id"] },
  ],
  warnings: [] };

// Schema versions (guide: schema-compare): the replay fixtures of
// schema-versions.mjs, generated by the Python package, trimmed to one saved
// version and its comparison with the database now.
let schemaMade = null;
const schemaFixtures = () => {
  if (!schemaMade) {
    schemaMade = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json, sys
sys.path.insert(0, 'test')
import _schema_replay as r
from myflames import schema_versions as sv
before, after = r.snapshot('mysql-8.4', 'before'), r.snapshot('mysql-8.4', 'after')
for s in (before, after):
    s['server']['host'], s['server']['port'] = '127.0.0.1', 3306
    note = next(o for o in s['objects'] if o['type'] == 'table' and o['name'] == 'notes')
    note['ddl'] += ' ROW_FORMAT=' + ('COMPACT' if s is before else 'DYNAMIC')
before, after = sv.validate_snapshot(before), sv.validate_snapshot(after)
v1 = {'id': 'a' * 32, 'saved_at': '2026-10-14T08:00:00Z', 'source_key': sv.source_key(before), 'number': 1,
      'name': 'before release 42', 'note': ''}
v1.update(sv.version_summary(before))
report = sv.compare(before, after, {'kind': 'version', 'id': v1['id'], 'number': 1, 'name': v1['name']}, {'kind': 'live'})
report['comparison_id'] = 'howto-comparison'
report['markdown'] = sv.format_markdown(report)
print(json.dumps({'v1': v1, 'snapshot': before, 'report': report}))
`], { cwd: root, maxBuffer: 64 * 1024 * 1024 }).toString());
  }
  return schemaMade;
};
// The catalog the schema-compare guide reads: the saved version's databases.
const SV_CATALOG = { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11", user_tables: 14, all_limit: 2000, all_note: "", system: [],
  schemas: [{ name: "sv_b", tables: 7, system: false }, { name: "sv_m", tables: 7, system: false }, { name: "mysql", tables: 38, system: true }] };
let schemasReply = null;
if (!only || only === "schema-compare") schemaFixtures();

const extra = {
  "/api/live/schemas": () => ok(schemasReply || schemas),
  "/api/live/catalog": (body) => ok(catalog(body)),
  "/api/editor/open": ok({ status: "idle", session: session() }),
  "/api/editor/status": ok({ status: "none", session: null, run: null }),
  "/api/editor/run": (body) => {
    const sql = atCursor(body);
    if (/^(start transaction|begin);?$/i.test(sql)) {
      txOpen = true;
      return ok({ status: "complete", session: session(), run: writeRun(sql) });
    }
    if (/^(update|insert|replace|delete)\b/i.test(sql))
      return ok({ status: "complete", session: session(), run: writeRun(sql, 1) });
    return ok({ status: "complete", session: session(),
      run: body.explain_format ? explainRun(sql) : editorRun(sql) });
  },
  "/api/editor/rollback": () => {
    txOpen = false;
    return ok({ status: "complete", session: session(), run: { ...writeRun("ROLLBACK"), kind: "action" } });
  },
  "/api/editor/commit": () => {
    txOpen = false;
    return ok({ status: "complete", session: session(), run: { ...writeRun("COMMIT"), kind: "action" } });
  },
  "/api/editor/export": (body) => {
    const rows = body.result?.rows || [];
    const names = (body.result?.columns || []).map((c) => c.name);
    const content = body.format === "json"
      ? JSON.stringify(rows.map((r) => Object.fromEntries(names.map((n, i) => [n, r[i]]))), null, 2)
      : [names.join(","), ...rows.map((r) => r.map((v) => v ?? "").join(","))].join("\r\n");
    const mime = { csv: "text/csv", json: "application/json", markdown: "text/markdown" }[body.format] || "text/plain";
    return ok({ format: body.format, content, mime, filename: `myflames-result.${body.format}` });
  },
  "/api/editor/close": ok({ closed: true, session: null }),
  "/api/editor/history": ok({ items: [] }),
  "/api/editor/preview": (body) => ok({ status: "ready",
    statements: [{ index: 0, kind: statement(body.sql || "SELECT 1").kind, action_keys: [], state: "unknown", missing: [], reason: "" }],
    aggregate: { state: "unknown", missing: [], reason: "", warning: "" }, policy: { action: "run", text: "" } }),
  "/api/table-data/open": () => ok(tablePage()),
  "/api/table-data/close": ok({ status: "closed" }),
  "/api/table-data/prepare": ok({ snapshot_id: "snapshot", review_token: "one-use",
    statements: ["UPDATE `shop`.`orders` SET `status` = 'paid' WHERE `id` = 7 LIMIT 1"],
    privileges: [], connection: { host: "dev.example", port: 3306, user: "dba" }, connection_type: "development", warnings: [] }),
  "/api/table-data/decline": ok({ status: "ready" }),
  "/api/table-data/apply": ok({ status: "running", snapshot_id: "snapshot" }),
  "/api/table-data/status": (_body, request) => {
    setTimeout(() => {
      tableApplied = true;
      void request.respond({ status: 200, contentType: "application/json",
        body: JSON.stringify({ snapshot_id: "snapshot", status: "complete", message: "Changes committed. Reload the table." }) }).catch(() => {});
    }, 500);
    return null;
  },
  "/api/import/csv/choose": ok(CSV_FILE),
  "/api/import/csv/inspect": (body) => ok(csvInspect(body)),
  "/api/import/csv/plan": (body, request) => {
    const reply = csvPlan(body);
    setTimeout(() => void request.respond({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) }).catch(() => {}), 250);
    return null;
  },
  "/api/import/csv/progress": () => ok({ schema_version: "csv-import-progress-1.0", kind: csvApplying ? "apply" : "plan",
    phase: csvApplying ? "sending" : "checking", rows_total: 6, rows_checked: 6, rows_sent: csvApplying ? 4 : 0, rows_committed: 0,
    batches_done: 0, batches_total: 1, in_flight: csvApplying, started_at: CLOCK, elapsed_s: 1, stop_requested: false }),
  "/api/import/csv/apply": (_body, request) => {
    csvApplying = true;
    setTimeout(() => {
      csvApplying = false;
      void request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(CSV_RESULT) }).catch(() => {});
    }, 1200);
    return null;
  },
  "/api/import/csv/stop": ok({ stop_requested: true, message: "Stopping after the current batch." }),
  "/api/live/top": () => ok(digestReport),
  "/api/live/digest-tables": ok({ schema_version: "digest-tables-1.0", method: "statement text", tables: [], notes: [] }),
  "/api/baselines/list": (body) => ok({ schema_version: "baseline-list-1.0", kind: body.kind, server: DIGEST_SERVER,
    server_key: DIGEST_KEY, max: 20, items: digestBaselines.filter((b) => b.item.kind === body.kind).map((b) => b.item).reverse() }),
  "/api/baselines/save": (body) => {
    digestClock += 60_000;
    const item = { id: (digestBaselines.length + 1).toString(16).padStart(32, "0"), kind: body.kind, name: body.name.trim(),
      created_at: new Date(digestClock).toISOString(), server: DIGEST_SERVER, server_key: DIGEST_KEY, read_at: body.report.read_at,
      server_time: DIGEST_SERVER_TIME, uptime_s: body.report.coverage.uptime_s, order: body.report.order, limit: body.report.limit,
      statements: body.report.statements.length };
    digestBaselines.push({ item, payload: keepDigests(body.report) });
    return ok({ ...item, max: 20 });
  },
  "/api/baselines/get": (body) => {
    const found = digestBaselines.find((b) => b.item.id === body.id);
    return found ? ok({ ...found.item, payload: found.payload })
      : { status: 400, body: { error: "Could not open the baseline: it is no longer saved on this computer." } };
  },
  "/api/schema-diagram/read": () => ok(structuredClone(DIAGRAM)),
  "/api/schema-versions/list": () => ok({ schema_version: "schema-version-list-1.0", items: [schemaFixtures().v1] }),
  "/api/schema-versions/get": (body) => {
    const made = schemaFixtures();
    return ok({ ...made.v1, id: body.id, snapshot: made.snapshot });
  },
  "/api/schema-versions/compare": () => ok(schemaFixtures().report),
  "/api/schema-versions/sync": () => ok(schemaFixtures().report),
  // Explore from an Explain result captures the plan in the tab's session.
  "/api/live/start": ok({ id: "howto-capture" }),
  "/api/live/status": ok({ id: "howto-capture", state: "complete", progress: 1, repeat: 1, error: "", result: CAPTURE }),
  "/api/live/readiness": ok({ engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.11", read_only: false, super_read_only: false,
    performance_schema: true, replica: false, group_member: false, galera_member: false, privileges: {}, actions: {}, notes: [], checked_at: CLOCK }),
  "/api/news": ok({ schema_version: "news-1.0", enabled: true, items: [], fetched_at: null, stale: true, offline: true, sources: [] }),
  "/api/app/update/check": { status: 500, body: { error: "Not expected while recording." } },
};
const seed = `from myflames.workspace_store import WorkspaceStore
import json, sys
store = WorkspaceStore(sys.argv[1]); ids = {}
store.set_ui_preferences({"theme": "light"})
for name, host, kind in (("Dev MySQL", "dev.example", "development"), ("Prod MySQL", "prod.example", "production")):
    saved = store.save("profile", dict(name=name, host=host, port=3306, user="dba", database="shop", ssl_mode="REQUIRED", type=kind, read_only=False), password=None, remember=False)
    ids[name] = saved["id"]
print(json.dumps(ids))`;

// A clock that starts at CLOCK and ticks normally from there: the editor's
// completion debounce stalls on a fully frozen Date (Tab stops accepting
// suggestions), so only the epoch is fixed. Locale is fixed too; the
// recording overlay (key caption bottom right, pointer dot) is injected only
// while recording.
async function fixPage(page) {
  await page.evaluateOnNewDocument((fixed) => {
    const Real = Date;
    const start = Real.now();
    const now = () => fixed + (Real.now() - start);
    function Frozen(...args) {
      if (!new.target) return new Real(now()).toString();
      return args.length ? new Real(...args) : new Real(now());
    }
    Frozen.prototype = Real.prototype;
    Frozen.now = now;
    Frozen.parse = Real.parse;
    Frozen.UTC = Real.UTC;
    globalThis.Date = Frozen;
  }, Date.parse(CLOCK));
  await page.emulateTimezone("UTC");
  const cdp = await page.createCDPSession();
  await cdp.send("Emulation.setLocaleOverride", { locale: "en-US" });
  await page.evaluateOnNewDocument(() => {
    const install = () => {
      if (document.getElementById("howto-keys")) return;
      const style = document.createElement("style");
      style.textContent = [
        "#howto-keys{position:fixed;right:18px;bottom:18px;z-index:2147483647;pointer-events:none;",
        "font:600 14px/1.4 -apple-system,system-ui,sans-serif;color:#fff;background:rgba(17,17,17,.85);",
        "padding:7px 12px;border-radius:8px;opacity:0;transition:opacity .15s;letter-spacing:.02em;}",
        "#howto-keys.show{opacity:1;}",
        "#howto-dot{position:fixed;left:-100px;top:-100px;width:16px;height:16px;margin:-8px 0 0 -8px;",
        "z-index:2147483647;pointer-events:none;border-radius:50%;background:rgba(234,88,12,.45);",
        "border:2px solid rgba(154,52,18,.95);box-sizing:border-box;}",
      ].join("");
      document.head.appendChild(style);
      const keys = document.createElement("div");
      keys.id = "howto-keys";
      keys.setAttribute("aria-hidden", "true");
      const dot = document.createElement("div");
      dot.id = "howto-dot";
      dot.setAttribute("aria-hidden", "true");
      document.body.append(keys, dot);
      window.addEventListener("mousemove", (e) => { dot.style.left = `${e.clientX}px`; dot.style.top = `${e.clientY}px`; }, true);
      window.addEventListener("mousedown", () => { dot.style.background = "rgba(234,88,12,.85)"; }, true);
      window.addEventListener("mouseup", () => { dot.style.background = "rgba(234,88,12,.45)"; }, true);
      window.__howtoKeys = (text) => { keys.textContent = text || ""; keys.classList.toggle("show", !!text); };
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
    else install();
  });
}

// ---- recording --------------------------------------------------------------
/** Page screenshots at FPS into a temp directory; encode with gifski. */
class Recorder {
  constructor(page, directory) {
    this.page = page;
    this.directory = directory;
    this.frames = 0;
    this.chain = Promise.resolve();
    this.busy = false;
    this.timer = null;
  }
  start() {
    this.startedAt = Date.now();
    this.timer = setInterval(() => {
      if (this.busy) return;
      this.busy = true;
      this.chain = this.chain.then(async () => {
        const png = await this.page.screenshot();
        await fs.writeFile(path.join(this.directory, `frame-${String(this.frames++).padStart(6, "0")}.png`), png);
        this.busy = false;
      });
    }, 1000 / FPS);
  }
  async stop() {
    clearInterval(this.timer);
    await this.chain;
    this.seconds = (Date.now() - this.startedAt) / 1000;
  }
  caption(text) {
    return this.page.evaluate((t) => window.__howtoKeys?.(t), text);
  }
  hold(ms) {
    return pause(ms);
  }
}

// ---- page helpers -----------------------------------------------------------
const clickButton = async (page, text, scope = "") => {
  const handle = await page.waitForFunction((text, scope) => [...document.querySelectorAll(`${scope} button`)]
    .find((b) => b.textContent.trim() === text && b.getClientRects().length && !b.disabled), {}, text, scope);
  await handle.asElement().click();
  await handle.dispose();
};
/** The page point of line (1-based), column (0-based) in the editor. */
async function posPoint(page, line, col) {
  return page.$eval(EDITOR, (el, line, col) => {
    const lineEl = el.querySelectorAll(".cm-line")[line - 1];
    if (!lineEl) throw new Error(`no editor line ${line}`);
    let left = col;
    const walker = document.createTreeWalker(lineEl, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      if (node.length >= left) {
        const range = document.createRange();
        range.setStart(node, left);
        range.setEnd(node, left);
        const r = range.getBoundingClientRect();
        return { x: r.left + 1, y: r.top + r.height / 2 };
      }
      left -= node.length;
    }
    const r = lineEl.getBoundingClientRect();
    return { x: r.left + 4, y: r.top + r.height / 2 };
  }, line, col);
}
const cursorCount = (page) => page.$eval(`${EDITOR}`, (el) => el.closest(".cm-editor").querySelectorAll(".cm-cursor").length);
async function press(page, ...keys) {
  const holds = keys.slice(0, -1);
  for (const k of holds) await page.keyboard.down(k);
  await page.keyboard.press(keys[keys.length - 1]);
  for (const k of holds.reverse()) await page.keyboard.up(k);
}
/** Drag with fixed steps so the motion reads in the recording. */
async function drag(page, from, to, { steps = 8, keys = [] } = {}) {
  await page.mouse.move(from.x, from.y);
  for (const k of keys) await page.keyboard.down(k);
  await page.mouse.down();
  for (let i = 1; i <= steps; i++) {
    await page.mouse.move(from.x + ((to.x - from.x) * i) / steps, from.y + ((to.y - from.y) * i) / steps);
    await pause(70);
  }
  await page.mouse.up();
  for (const k of keys) await page.keyboard.up(k);
}
/** Esc closes suggestions first, then extra cursors: press until one cursor. */
async function escapeToOneCursor(page) {
  for (let i = 0; i < 3; i++) {
    await page.keyboard.press("Escape");
    await pause(120);
    if ((await cursorCount(page)) <= 1) return;
  }
  assert.equal(await cursorCount(page), 1, "Esc kept one cursor");
}
/** Click the visible completion option whose label starts with *text*. */
async function pickCompletion(page, text) {
  const handle = await page.waitForFunction((t) => [...document.querySelectorAll(".cm-tooltip-autocomplete li")]
    .find((li) => li.textContent.trim().startsWith(t) && li.getClientRects().length), { timeout: 8000 }, text);
  await handle.asElement().click();
  await handle.dispose();
}

async function boot() {
  const h = await open("/api/live/health", "howto-recordings", extra, {
    fallback: ok(health), seed, beforeGoto: fixPage, viewport: SIZE,
    refuse: /^\/api\/editor\/(?!saved\/)/,
    offline: true,
    launchArgs: ["--disable-partial-raster", "--force-color-profile=srgb"],
  });
  await h.page.waitForFunction(() => /Dev MySQL/.test(document.body.textContent) && /Prod MySQL/.test(document.body.textContent));
  // Exports (a result CSV, a diagram, a sync script) download into a
  // throwaway directory instead of the person's Downloads folder.
  h.downloads = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-howto-dl-"));
  const cdp = await h.page.createCDPSession();
  await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: h.downloads });
  return h;
}
async function openEditor(h) {
  await clickButton(h.page, "Query editor", ".main-nav");
  await chooseConnection(h.page, h.seeded["Dev MySQL"]);
  await h.page.waitForSelector(EDITOR);
}
/** The editor with the Schemas panel open and the shop schema expanded. */
async function openSchemaTree(h) {
  const { page } = h;
  await openEditor(h);
  if (await page.$('button[aria-label="Show the schema browser"]')) await page.click('button[aria-label="Show the schema browser"]');
  await page.waitForSelector(".schema-browser .tree-row.kind-schema");
  await page.click(".schema-browser .tree-row.kind-schema");
  await page.waitForSelector('.schema-browser .tree-row.kind-table[data-node$="orders"]');
}
/** Plans with the bundled sample plan open and its chart drawn. */
async function openSamplePlan(h) {
  const { page } = h;
  await clickButton(page, "Plans", ".main-nav ");
  await clickButton(page, "Explore sample plan");
  await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
}

// ---- guides -----------------------------------------------------------------
const MULTI_SQL = [
  "SELECT id FROM orders WHERE status = 'new';",
  "SELECT id FROM orders WHERE status = 'paid';",
  "SELECT id FROM orders WHERE status = 'refunded';",
].join("\n");
const TX_SQL = [
  "START TRANSACTION;",
  "UPDATE orders SET status = 'paid' WHERE id = 7;",
  "SELECT id, status FROM orders WHERE id = 7;",
].join("\n");

const GUIDES = [
  ["multi-cursor", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, MULTI_SQL);
    await rec.hold(500);
    // Three cursors: click line 1, then Option-click lines 2 and 3 at the same column.
    let p = await posPoint(page, 1, 9);
    await page.mouse.click(p.x, p.y);
    await rec.caption("⌥ click");
    await page.keyboard.down("Alt");
    for (const line of [2, 3]) {
      p = await posPoint(page, line, 9);
      await page.mouse.click(p.x, p.y);
      await pause(200);
    }
    await page.keyboard.up("Alt");
    assert.equal(await cursorCount(page), 3, "Option-click added two cursors");
    await rec.hold(300);
    await rec.caption("type once");
    await page.keyboard.type(", customer_id", { delay: 45 });
    await rec.hold(500);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(300);
    // ⌘D twice on "status" (line 1, after the edit at 0-based column 41).
    p = await posPoint(page, 1, 43);
    await page.mouse.click(p.x, p.y);
    await pause(150);
    await rec.caption("⌘D ⌘D");
    await press(page, "Meta", "d");
    await pause(300);
    await press(page, "Meta", "d");
    await pause(300);
    assert.equal(await cursorCount(page), 2, "⌘D twice selected two occurrences");
    await rec.hold(400);
    await rec.caption("type: state");
    await page.keyboard.type("state", { delay: 45 });
    await rec.hold(500);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(250);
    // ⇧⌘L on "orders" selects all three occurrences (column 30 after the edit).
    p = await posPoint(page, 2, 30);
    await page.mouse.click(p.x, p.y);
    await rec.caption("⇧⌘L");
    await press(page, "Meta", "Shift", "l");
    await pause(150);
    assert.ok((await cursorCount(page)) >= 3, "⇧⌘L selected every occurrence");
    await rec.hold(600);
    await rec.caption("Esc");
    await escapeToOneCursor(page);
    await rec.hold(250);
    // ⇧Option-drag a box over the "id" column of all three lines.
    const from = await posPoint(page, 1, 7);
    const to = await posPoint(page, 3, 9);
    await rec.caption("⇧⌥ drag");
    await drag(page, from, to, { keys: ["Shift", "Alt"] });
    const selection = await editorSelection(page);
    assert.notEqual(selection.start, selection.end, "the box selection is not empty");
    await rec.caption("");
  }],
  ["suggestions", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, "");
    let p = await posPoint(page, 1, 0);
    await page.mouse.click(p.x, p.y);
    await rec.caption("type: SEL");
    await page.keyboard.type("SEL", { delay: 110 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("Tab");
    await page.keyboard.press("Tab");
    await pause(300);
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value === "SELECT");
    await rec.hold(350);
    await rec.caption("type: FROM o");
    await page.keyboard.type(" FROM o", { delay: 90 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("click orders");
    await pickCompletion(page, "orders");
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value === "SELECT FROM orders");
    await rec.hold(350);
    await rec.caption("type: o");
    await page.keyboard.type(" o", { delay: 90 });
    await rec.hold(250);
    // Back after "SELECT ": complete a column through the alias o.
    p = await posPoint(page, 1, 7);
    await page.mouse.click(p.x, p.y);
    await rec.caption("type: o.");
    await page.keyboard.type("o.", { delay: 110 });
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(350);
    await rec.caption("click status");
    await pickCompletion(page, "status");
    await page.keyboard.type(" ");
    await page.waitForFunction(() => document.querySelector(".sql-editor")?.value === "SELECT o.status FROM orders o");
    await rec.hold(400);
    // Show the on-demand list at the end of the statement.
    p = await posPoint(page, 1, 7);
    await page.mouse.click(p.x, p.y);
    await page.keyboard.press("End");
    await pause(150);
    await rec.caption("Ctrl+Space");
    await press(page, "Control", "Space");
    await page.waitForSelector(".cm-tooltip-autocomplete", { visible: true });
    await rec.hold(600);
    await rec.caption("Esc");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".cm-tooltip-autocomplete"));
    assert.equal(await page.$eval(".sql-editor", (e) => e.value), "SELECT o.status FROM orders o");
    await rec.caption("");
  }],
  ["run-explain", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    await rec.hold(700);
    await rec.caption("⌘Enter");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
    await rec.hold(900);
    await rec.caption("click Explain");
    await page.click("[data-action=explain]");
    await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .some((b) => b.textContent.trim() === "Explore" && b.getClientRects().length), { timeout: 10000 });
    await rec.hold(1000);
    await rec.caption("click Explore");
    const explore = await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "Explore" && b.getClientRects().length));
    await explore.asElement().click();
    await explore.dispose();
    await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
    await page.waitForFunction(() => [...document.querySelectorAll(".plan-screen svg")]
      .map((svg) => svg.textContent).join(" ").match(/\borders\b/));
    assert.match(await page.$$eval(".plan-screen svg", (svgs) => svgs.map((svg) => svg.textContent).join(" ")), /\borders\b/);
    await rec.caption("");
    await rec.hold(900);
  }],
  ["find-replace", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, MULTI_SQL.replace(/FROM orders/g, "FROM orders"));
    await rec.hold(300);
    await rec.caption("⌘F");
    await press(page, "Meta", "f");
    await page.waitForSelector(".sql-find", { visible: true });
    await rec.hold(250);
    await rec.caption("type: status");
    await page.click('.sql-find input[aria-label="Find"]');
    await page.keyboard.type("status", { delay: 70 });
    await rec.hold(500);
    await rec.caption("⌘G");
    await press(page, "Meta", "g");
    await rec.hold(400);
    await rec.caption("⌥⌘F");
    await press(page, "Meta", "Alt", "f");
    await page.waitForSelector('.sql-find input[aria-label="Replace with"]', { visible: true });
    await rec.hold(300);
    await rec.caption("type: state");
    await page.click('.sql-find input[aria-label="Replace with"]');
    await page.keyboard.type("state", { delay: 70 });
    await rec.hold(350);
    await rec.caption("click Replace all");
    await clickButton(page, "Replace all", ".sql-find");
    await page.waitForFunction(() => (document.querySelector(".sql-editor")?.value.match(/state =/g) || []).length === 3);
    await rec.hold(900);
    await rec.caption("⌘Z");
    await page.$eval(EDITOR, (el) => el.focus());
    await press(page, "Meta", "z");
    await page.waitForFunction(() => (document.querySelector(".sql-editor")?.value.match(/status =/g) || []).length === 3);
    await rec.caption("");
    await rec.hold(500);
  }],
  ["settings-theme", async (h, rec) => {
    const { page } = h;
    await rec.hold(300);
    await rec.caption("⌘,");
    await press(page, "Meta", ",");
    await page.waitForSelector('.settings-page [data-theme-id="dark"]', { visible: true });
    await rec.hold(400);
    await rec.caption("click Dark");
    await page.click('[data-theme-id="dark"]');
    await page.waitForFunction(() => document.documentElement.dataset.theme === "dark");
    await rec.hold(800);
    await rec.caption("click System");
    await page.click('[data-theme-id="system"]');
    await page.waitForFunction(() => !document.documentElement.dataset.theme);
    await rec.hold(800);
    await rec.caption("click Query editor");
    await page.click("#settings-section-editor");
    await page.waitForFunction(() => /Multiple cursors/.test(document.querySelector(".settings-panel")?.textContent || ""));
    await rec.hold(350);
    const toggle = async (on) => {
      await page.locator('.settings-page .settings-editor-group .session-toggle ::-p-text(Multiple cursors)').click();
      await page.waitForFunction((want) => {
        const field = [...document.querySelectorAll(".settings-editor-group .check-field")]
          .find((f) => /Multiple cursors/.test(f.textContent));
        return field?.querySelector("input")?.checked === want;
      }, {}, on);
    };
    await rec.caption("Multiple cursors: off");
    await toggle(false);
    await rec.hold(500);
    await rec.caption("Multiple cursors: on");
    await toggle(true);
    await rec.caption("");
    await rec.hold(800);
  }],
  ["layout", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    await rec.caption("⌘Enter");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
    await rec.hold(500);
    // Sidebar: drag the right edge wider.
    let box = await (await page.waitForSelector(".sidebar-resizer")).boundingBox();
    const resizerPoint = () => ({ x: box.x + box.width / 2, y: box.y + box.height / 2 });
    await rec.caption("drag sidebar edge");
    const from = resizerPoint();
    await drag(page, from, { x: from.x + 80, y: from.y });
    const widened = await page.$eval(".sidebar-resizer", (e) => Number(e.getAttribute("aria-valuenow")));
    assert.ok(widened > 208, `sidebar widened (${widened})`);
    await rec.hold(800);
    // Query editor splitter: drag the editor taller.
    const splitter = await (await page.waitForSelector(".editor-splitter")).boundingBox();
    const beforeHeight = await page.$eval(".editor-splitter", (e) => Number(e.getAttribute("aria-valuenow")));
    await rec.caption("drag editor splitter");
    const sfrom = { x: splitter.x + splitter.width / 2, y: splitter.y + splitter.height / 2 };
    await drag(page, sfrom, { x: sfrom.x, y: sfrom.y + 90 });
    const taller = await page.$eval(".editor-splitter", (e) => Number(e.getAttribute("aria-valuenow")));
    assert.ok(taller > beforeHeight, `editor grew (${beforeHeight} to ${taller})`);
    await rec.hold(800);
    // Double-click the sidebar edge to reset it (the editor splitter has only
    // drag and arrow keys: QueryLab.tsx "editor-splitter").
    box = await (await page.waitForSelector(".sidebar-resizer")).boundingBox();
    await rec.caption("double-click to reset");
    const p = resizerPoint();
    await page.mouse.click(p.x, p.y, { clickCount: 2 });
    await page.waitForFunction(() => document.querySelector(".sidebar-resizer")?.getAttribute("aria-valuenow") === "208");
    await rec.caption("");
    await rec.hold(700);
  }],
  ["edit-table-rows", async (h, rec) => {
    const { page } = h;
    tableApplied = false;
    await openSchemaTree(h);
    await rec.hold(400);
    await rec.caption("right-click orders");
    await page.click('.schema-browser .tree-row.kind-table[data-node$="orders"]', { button: "right" });
    await page.waitForSelector(".schema-browser .context-menu[role=menu]");
    await rec.caption("Edit table data…");
    await page.locator(".schema-browser .context-menu [role=menuitem] ::-p-text(Edit table data)").click();
    await page.waitForSelector(".table-data-panel input");
    await rec.hold(700);
    await rec.caption("change new to paid");
    await page.click('input[aria-label="status for r1"]', { clickCount: 3 });
    await page.keyboard.type("paid", { delay: 80 });
    await rec.hold(500);
    await rec.caption("Review 1 changes");
    await clickButton(page, "Review 1 changes", ".table-data-panel ");
    await page.waitForSelector(".confirm-sheet");
    await rec.hold(1100);
    await rec.caption("Apply changes");
    await clickButton(page, "Apply changes", ".confirm-sheet ");
    await page.waitForFunction(() => document.querySelector(".table-data-panel [role=status]")?.textContent.includes("Changes committed"));
    await rec.hold(600);
    await rec.caption("Reload rows");
    await clickButton(page, "Reload rows", ".table-data-panel ");
    await page.waitForFunction(() => document.querySelector('input[aria-label="status for r1"]')?.value === "paid");
    await rec.caption("");
    await rec.hold(700);
  }],
  ["transaction", async (h, rec) => {
    const { page } = h;
    txOpen = false;
    await openEditor(h);
    await setEditorText(page, TX_SQL);
    await rec.hold(1000);
    let p = await posPoint(page, 1, 5);
    await page.mouse.click(p.x, p.y);
    await pause(1000);
    await rec.caption("⌘Enter: START TRANSACTION");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Transaction open");
    await rec.caption("Transaction open");
    await rec.hold(2500);
    p = await posPoint(page, 2, 5);
    await page.mouse.click(p.x, p.y);
    await pause(1000);
    await rec.caption("⌘Enter: UPDATE");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /affected/i.test(document.querySelector(".results-pane")?.textContent || ""));
    await rec.hold(2500);
    p = await posPoint(page, 3, 5);
    await page.mouse.click(p.x, p.y);
    await pause(1000);
    await rec.caption("⌘Enter: SELECT");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => [...document.querySelectorAll(".result-table tbody tr td")]
      .some((td) => td.textContent === "paid"));
    await rec.hold(2500);
    await rec.caption("click Transaction open");
    await page.click(".editor-transaction-trigger");
    await page.waitForSelector("[data-action=rollback]", { visible: true });
    await rec.hold(2500);
    await rec.caption("Roll back");
    await page.click("[data-action=rollback]");
    await page.waitForFunction(() => document.querySelector(".statusbar-note")?.textContent === "Rolled back.");
    await rec.caption("Rolled back.");
    await rec.hold(2500);
    await rec.caption("");
  }],
  ["explain-plans", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    const p = await posPoint(page, 1, 12);
    await page.mouse.click(p.x, p.y);
    await rec.hold(400);
    await rec.caption("Explain format: JSON");
    await page.select('select[aria-label="Explain format"]', "json");
    await rec.hold(500);
    await rec.caption("click Explain");
    await page.click("[data-action=explain]");
    await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .some((b) => b.textContent.trim() === "Explore" && b.getClientRects().length), { timeout: 10000 });
    await rec.hold(900);
    await rec.caption("click Explore");
    const explore = await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .find((b) => b.textContent.trim() === "Explore" && b.getClientRects().length));
    await explore.asElement().click();
    await explore.dispose();
    await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
    await rec.hold(900);
    await rec.caption("Execution tree");
    await page.click('.segmented[aria-label="Chart view"] button[data-value="tree"]');
    await page.waitForFunction(() => document.querySelector(".plan-chart")?.dataset.view === "tree"
      || !!document.querySelector(".plan-screen .mfc-tree"));
    await rec.caption("");
    await rec.hold(900);
  }],
  ["results-export", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, RUN_SQL);
    await rec.caption("⌘Enter");
    await press(page, "Meta", "Enter");
    await page.waitForFunction(() => /24 rows/.test(document.body.textContent));
    await rec.hold(600);
    await rec.caption("filter: new");
    await page.click('input[aria-label="Filter loaded rows"]');
    await page.keyboard.type("new", { delay: 90 });
    await page.waitForFunction(() => /8 of 24 loaded rows/.test(document.body.textContent));
    await rec.hold(700);
    await rec.caption("click the total heading to sort");
    await page.click('.result-table th[data-col="c3"] .sort-button');
    await rec.hold(700);
    await rec.caption("Export 8 filtered rows");
    await page.click('button[aria-label="Export result 1"]');
    await page.waitForSelector(".popover .popover-title");
    await rec.hold(800);
    await rec.caption("choose CSV");
    await clickButton(page, "CSV", ".popover ");
    await page.waitForFunction(() => /exported as myflames-result\.csv/.test(document.body.textContent));
    await rec.caption("");
    await rec.hold(700);
  }],
  ["named-parameters", async (h, rec) => {
    const { page } = h;
    await openEditor(h);
    await setEditorText(page, "SELECT id FROM orders WHERE id = :id;");
    const p = await posPoint(page, 1, 5);
    await page.mouse.click(p.x, p.y);
    await rec.hold(1000);
    await rec.caption("Run analysis options");
    await page.click('button[aria-label="Run analysis options"]');
    await page.waitForSelector(".editor-options-popover.popover");
    await rec.hold(1000);
    await rec.caption("Parameters and execution conditions");
    await page.click(".editor-parameters summary");
    await page.waitForSelector('textarea[aria-label="Query parameters"]', { visible: true });
    await rec.hold(1000);
    await rec.caption('type: {"id": 42}');
    await page.click('textarea[aria-label="Query parameters"]');
    await press(page, "Meta", "a");
    await page.keyboard.type('{"id": 42}', { delay: 80 });
    await rec.hold(1500);
    await page.keyboard.press("Escape");
    await pause(1000);
    await rec.caption("click Explain");
    await page.click("[data-action=explain]");
    if (process.env.HOWTO_DEBUG) { await pause(1500); console.log("NP-DEBUG", JSON.stringify(h.state.other.filter((o) => o.path === "/api/editor/run").map((o) => ({ sql: o.body.sql, ef: o.body.explain_format, params: o.body.parameters }))), JSON.stringify(await page.evaluate(() => ({ alerts: [...document.querySelectorAll("[role=alert], .editor-error, .editor-note")].map((e) => e.textContent), explore: [...document.querySelectorAll("button")].filter((b) => /Explore/.test(b.textContent)).length })))); }
    await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .some((b) => b.textContent.trim() === "Explore" && b.getClientRects().length), { timeout: 10000 });
    await rec.caption("Explore in the Explain result");
    await rec.hold(2500);
    await rec.caption("");
  }],
  ["digest-baseline", async (h, rec) => {
    const { page } = h;
    digestReport = digestTop;
    digestBaselines.length = 0;
    const qd = '[data-view="query-digest"]';
    await rec.caption("Query digest");
    await page.click('.main-nav button[data-place="digest"]');
    await page.waitForSelector(`${qd} tbody tr[data-row-key]`);
    await rec.hold(1200);
    await rec.caption("click Refresh");
    await h.refresh();
    await page.waitForFunction((q) => /^30 statement/.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""), {}, qd);
    await rec.hold(2500);
    await rec.caption("Refresh options → Save baseline…");
    await page.click(`${qd} button[aria-label="Refresh options"]`);
    await page.waitForSelector(`${qd} [data-action=save-baseline]`, { visible: true });
    await rec.hold(1000);
    await page.click(`${qd} [data-action=save-baseline]`);
    await page.waitForSelector("dialog.baseline-sheet");
    await rec.hold(2500);
    await rec.caption("name: Before deploy");
    await page.$eval("dialog.baseline-sheet input", (e) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(e, "Before deploy");
      e.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await rec.hold(1200);
    await rec.caption("click Save baseline");
    await page.click("dialog.baseline-sheet .sheet-buttons .action-button");
    await page.waitForFunction(() => /Saved the baseline/.test(document.body.textContent));
    await rec.hold(2500);
    // The reading after the deployment: two digests new, two grown.
    digestReport = digestLater;
    await rec.caption("click Refresh");
    await h.refresh();
    await page.waitForFunction((q) => /^32 statement/.test(document.querySelector(`${q} .summary-line .summary-text`)?.textContent || ""), {}, qd);
    await rec.hold(1500);
    await rec.caption("Refresh options → Compare with baseline…");
    await page.click(`${qd} button[aria-label="Refresh options"]`);
    await page.waitForSelector(`${qd} [data-action=compare-baseline]`, { visible: true });
    await rec.hold(1000);
    await page.click(`${qd} [data-action=compare-baseline]`);
    await page.waitForSelector("dialog.baseline-sheet .baseline-choices");
    await rec.hold(2500);
    await rec.caption("choose the baseline");
    await page.click("dialog.baseline-sheet .baseline-choice");
    await rec.hold(1200);
    await rec.caption("click Compare");
    await page.click("dialog.baseline-sheet .sheet-buttons .action-button");
    await page.waitForSelector(`${qd} [data-baseline-changes]`);
    await page.waitForFunction((q) => /new, 2 grew/.test(document.querySelector(`${q} [data-baseline-summary]`)?.textContent || ""), {}, qd);
    await rec.caption("Since the baseline");
    await rec.hold(3500);
    await rec.caption("");
  }],
  ["compare-plans", async (h, rec) => {
    const { page } = h;
    await clickButton(page, "Plans", ".main-nav ");
    await rec.caption("Explore sample plan");
    await clickButton(page, "Explore sample plan");
    await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
    await rec.hold(700);
    await rec.caption("Import plan");
    await clickButton(page, "Import plan", ".plans-bar ");
    await page.waitForSelector("dialog[open] input[type=file]");
    await (await page.$("dialog[open] input[type=file]")).uploadFile(path.join(root, "test/mariadb-explain-join.json"));
    await page.waitForFunction(() => document.querySelectorAll(".plan-item").length === 2);
    await rec.hold(500);
    await rec.caption("click Compare");
    await clickButton(page, "Compare", ".plans-bar ");
    await page.waitForSelector(".compare-selectors select");
    const ids = await page.$$eval(".compare-selectors select",
      (sels) => sels.map((s) => [...s.options].filter((o) => o.value).map((o) => o.value)));
    await rec.caption("Before and After");
    await page.select(".compare-selectors label:first-of-type select", ids[0][0]);
    await page.select(".compare-selectors label:last-of-type select", ids[1][1] || ids[1][0]);
    await page.waitForSelector(".comparison-row");
    await rec.hold(900);
    await rec.caption("click a change");
    const rows = await page.$$(".comparison-row");
    await rows[Math.min(1, rows.length - 1)].click();
    await page.waitForFunction(() => !!document.querySelector(".comparison-row.selected"));
    await rec.caption("");
    await rec.hold(1100);
  }],
  ["slowest-operator", async (h, rec) => {
    const { page } = h;
    await openSamplePlan(h);
    await rec.hold(600);
    await rec.caption("Go to");
    await clickButton(page, "Go to");
    await page.waitForFunction(() => [...document.querySelectorAll("button")]
      .some((b) => b.textContent.trim() === "Largest self time" && b.getClientRects().length));
    await rec.hold(300);
    await rec.caption("Largest self time");
    await clickButton(page, "Largest self time");
    await page.waitForFunction(() => /Self time/.test(document.querySelector(".plan-fold")?.textContent || ""));
    await rec.hold(1000);
    await rec.caption("click Stats");
    await page.click('.segmented[aria-label="Chart view"] button[data-value="stats"]');
    await page.waitForFunction(() => /Time per table/.test(document.body.textContent) && /Time per operator type/.test(document.body.textContent));
    await rec.caption("");
    await rec.hold(1000);
  }],
  ["investigation", async (h, rec) => {
    const { page } = h;
    await openSamplePlan(h);
    await rec.hold(500);
    await rec.caption("Save investigation");
    await clickButton(page, "Save investigation", ".topbar ");
    await page.waitForSelector("dialog.notebook-sheet[open]");
    await rec.caption("name: Orders index review");
    await page.click(".notebook-grid .field:nth-child(1) input");
    await page.keyboard.type("Orders index review", { delay: 40 });
    await rec.caption("Baseline: the open plan");
    const baseline = await page.$eval(".notebook-grid .field:nth-child(3) select option", (o) => o.value);
    await page.select(".notebook-grid .field:nth-child(3) select", baseline);
    await rec.hold(300);
    await rec.caption("type the findings");
    await page.click(".investigation-notes textarea");
    await page.keyboard.type("Full scan on orders: add an index on status.", { delay: 25 });
    await rec.hold(400);
    await rec.caption("click Save");
    await clickButton(page, "Save", "dialog.notebook-sheet .sheet-actions ");
    await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
    await rec.hold(600);
    await rec.caption("open Investigations");
    await clickButton(page, "Investigations", ".main-nav ");
    await page.waitForSelector(".library-item");
    await rec.hold(500);
    await page.click(".library-item");
    await page.waitForSelector(".library-detail h2");
    await rec.hold(600);
    await rec.caption("click Open all");
    await clickButton(page, "Open all", ".library-actions ");
    await page.waitForSelector(".plan-screen svg", { visible: true, timeout: 15000 });
    await rec.caption("");
    await rec.hold(800);
  }],
  ["import-csv", async (h, rec) => {
    const { page } = h;
    csvApplying = false;
    const sheet = "dialog.import-csv-sheet";
    const nextEnabled = (label) => page.waitForFunction((s, l) => {
      const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === l);
      return b && !b.disabled && b.getAttribute("aria-disabled") !== "true";
    }, { timeout: 10000 }, sheet, label);
    await openSchemaTree(h);
    await rec.caption("right-click orders");
    await page.click('.schema-browser .tree-row.kind-table[data-node$="orders"]', { button: "right" });
    await page.waitForSelector(".schema-browser .context-menu[role=menu]");
    await rec.caption("Import CSV…");
    await page.locator(".schema-browser .context-menu [role=menuitem] ::-p-text(Import CSV…)").click();
    await page.waitForSelector(`${sheet}[open]`);
    await rec.hold(400);
    await rec.caption("Choose file…");
    await clickButton(page, "Choose file…", `${sheet} `);
    await page.waitForSelector(`${sheet} [aria-label="Preview of the first rows"]`);
    await rec.hold(1000);
    await rec.caption("Next");
    await clickButton(page, "Next", `${sheet} .sheet-actions `);
    await page.waitForFunction((s) => /Target/.test(document.querySelector(`${s} [aria-current=step]`)?.textContent || ""), {}, sheet);
    await nextEnabled("Next");
    await rec.hold(1000);
    await rec.caption("Next");
    await clickButton(page, "Next", `${sheet} .sheet-actions `);
    await page.waitForFunction((s) => /Check/.test(document.querySelector(`${s} [aria-current=step]`)?.textContent || ""), {}, sheet);
    await nextEnabled("Review…");
    await rec.hold(800);
    await rec.caption("Review…");
    await clickButton(page, "Review…", `${sheet} .sheet-actions `);
    await page.waitForFunction((s) => /Review/.test(document.querySelector(`${s} [aria-current=step]`)?.textContent || ""), {}, sheet);
    await rec.hold(1300);
    await rec.caption("click Import");
    await clickButton(page, "Import", `${sheet} .sheet-actions `);
    await page.waitForFunction((s) => /committed/i.test(document.querySelector(s)?.textContent || ""), { timeout: 15000 }, sheet);
    await rec.caption("");
    await rec.hold(900);
  }],
  ["diagram", async (h, rec) => {
    const { page } = h;
    await page.click('.main-nav button[data-place="diagram"]');
    await page.waitForSelector('.server-view[data-view="diagram"]', { visible: true });
    await page.waitForSelector('select[aria-label="Database"] option[value="shop"]');
    await rec.caption("Database: shop");
    await page.select('select[aria-label="Database"]', "shop");
    await rec.hold(1000);
    await rec.caption("click Load");
    await page.click('[data-action="refresh"]');
    await page.waitForSelector(".diagram-table");
    await rec.hold(2500);
    await rec.caption("find: orders");
    await page.click('[aria-label="Find a table or column"]');
    await page.keyboard.type("orders", { delay: 70 });
    await rec.hold(1200);
    await rec.caption("click the orders heading");
    await page.click('[data-table-id="orders"]');
    await page.waitForFunction(() => /customer_id → id/.test(document.querySelector(".view-inspector")?.textContent || ""));
    await rec.hold(2500);
    await rec.caption("Export → Editable JSON");
    if (process.env.HOWTO_DEBUG) console.log("DG-DEBUG", await page.evaluate(() => JSON.stringify([...document.querySelectorAll("button")].map((b) => ({ t: b.textContent.trim().slice(0, 30), v: !!b.getClientRects().length, d: b.disabled })).filter((b) => /Export|Import|Load|Diagram|Mermaid/.test(b.t)))));
    await clickButton(page, "Export");
    await page.waitForSelector(".diagram-export-menu");
    await rec.hold(1200);
    await clickButton(page, "Editable JSON");
    await page.waitForFunction(() => !document.querySelector(".diagram-export-menu"));
    await rec.caption("");
    await rec.hold(1000);
  }],
  ["schema-compare", async (h, rec) => {
    const { page } = h;
    schemasReply = SV_CATALOG;
    await page.click('.main-nav button[data-place="schema"]');
    await page.waitForSelector(".schema-versions-view tbody tr");
    await rec.hold(500);
    await rec.caption("select the saved version");
    await page.click('.schema-versions-view tbody tr td[data-col="database"]');
    await page.waitForSelector(".view-inspector .schema-inspector-actions button.primary");
    await rec.hold(500);
    await rec.caption("Compare with the database now");
    await page.click(".view-inspector .schema-inspector-actions button.primary");
    await page.waitForSelector(".schema-compare-sides");
    await page.waitForSelector('.schema-versions-view tbody tr[data-row-key]');
    await rec.hold(1000);
    await rec.caption("select a changed object");
    await page.locator('.schema-versions-view tbody tr td[data-col="name"] ::-p-text(customers)').click();
    await page.waitForSelector(".schema-changes-table");
    await rec.hold(1300);
    await clickButton(page, "Back to differences", ".schema-versions-view ");
    await page.waitForSelector('.schema-versions-view tbody tr[data-row-key]');
    await rec.caption("Sync script");
    await page.click("#schema-compare-tab-sync");
    await page.waitForSelector(".schema-migration");
    await rec.hold(1500);
    await rec.caption("Open in Query editor");
    await page.click(".schema-migration-actions button.primary");
    await page.waitForFunction(() => /-- myflames schema sync/.test(document.querySelector(".sql-editor")?.value || ""));
    await rec.caption("");
    await rec.hold(900);
  }],
];

// ============================================================================
// BEGIN server / connections / topology how-to recordings (one appended
// block: the guides of docs/handbook/howto-server.md, howto-connections.md
// and howto-topology.md). Own fixtures, mocks, boot variant and GUIDES
// entries below; nothing above this banner is touched, so parallel edits to
// the editor guides merge cleanly.
// ============================================================================
const locksDdl = await load("locks-mysql-26.7-ddl.json");
// The additive identity fields the Kill sheet trusts, as the suites set them.
locksDdl.server_identity = { server_uuid: "locks-server-uuid" };
const sessionsActive = await load("sessions-mysql-8.4-active.json");
// The additive identity fields the view trusts, as sessions.mjs sets them.
sessionsActive.server_identity = { server_uuid: "sessions-server-uuid" };
sessionsActive.trx_checked = true;
const instrumentsLive = await load("instruments-mysql-8.4.json");
const topLab = await load("top-mysql-8.4-lab.json");
const indexesReport = await load("indexes-mysql-8.4.json");
const tablesFragmented = await load("tables-mysql-8.4.json");
const parametersCatalog = await load("parameters-mysql.json");
const variableGlobal = await load("variable-mysql-8.4-global.json");
const compareFixtures = await load("server-compare.json");
const instrumentsSelected = await load("instruments-selection.json");
const replReceiverGuard = {
  receiver_guard: "off",
  receiver_guard_reason: "Receiver stage instrumentation is off.",
  receiver_guard_instrument: "stage/sql/Checking source version",
  receiver_guard_note: "Receiver reconnects are not detected: the loop's other guards remain active.",
};
const replicationError = await load("replication-mysql-8.4-gtid-error.json");
Object.assign(replicationError, replReceiverGuard);
// Like withReceiverGuard in replication.mjs: the additive guard fields and the
// review token, leaving the recorded transaction identities and SQL untouched.
for (const channel of replicationError.channels) if (channel.skip_plan) Object.assign(channel.skip_plan, replReceiverGuard, { review_token: "review-skip" });
const replicationHealthy = await load("replication-mysql-8.4-healthy.json");
const replicationSkipped = await load("replication-mysql-8.4-skip.json");
const clusterHealthy = await load("cluster-ic-8.4-healthy.json");
const clusterPlan = await load("cluster-plan-switchover.json");
const clusterResult = await load("cluster-result-switchover.json");
const clusterProgress = await load("cluster-progress-sending.json");
const proxysqlLab = await load("proxysql-lab.json");
const proxysqlPlanOffline = await load("proxysql-plan-offline-soft.json");
const proxysqlPlanSave = await load("proxysql-plan-save.json");
const proxysqlDone = await load("proxysql-result-done.json");
const proxysqlProgress = await load("proxysql-progress-sending.json");
const proxysqlDigests = await load("proxysql-digests-time.json");
const readysetLab = await load("readyset-lab.json");
const readysetExplain = await load("readyset-explain-both.json");
const readysetPlan = await load("readyset-plan-create-cache.json");
const readysetDone = await load("readyset-result-create-done.json");
const readysetProgress = await load("readyset-progress-sending.json");

// Accounts (Users and roles), shaped as accounts.mjs builds them.
const accountDetail = {
  schema_version: "account-1.0", name: "app", host: "%", engine: "mysql", version: "8.4.0", exists: true,
  fingerprint: "fp-1", grants: ["GRANT SELECT ON *.* TO `app`@`%`"],
  attributes: { locked: false, expired: false, plugin: "caching_sha2_password" },
  model: { global: ["SELECT"], dynamic: [], global_grantable: ["SELECT"], static_grant_option: false, schemas: [], tables: [], columns: [], routines: [], proxies: [], restrictions: [], roles: [], unparsed: [] },
  roles: [], default_roles: [], defaults_checked: true, warnings: [],
  privilege_catalog: {
    global: [{ name: "SELECT" }, { name: "INSERT", grantable_here: true }, { name: "UPDATE", grantable_here: true }],
    database: [{ name: "SELECT", grantable_here: true }, { name: "CREATE ROUTINE", grantable_here: true, description: "Create stored routines." }],
    table: [{ name: "SELECT", grantable_here: true }], column: [{ name: "SELECT", grantable_here: true }],
    routine: [{ name: "EXECUTE", grantable_here: true }], proxy: [{ name: "PROXY", grantable_here: true }],
  },
  privilege_catalog_meta: { engine: "mysql", version: "8.4.0", source: "server" },
  grant_scopes: [
    { id: "server:global", label: "*.*", level: "global", cells: [{ privilege: "SELECT", direct: true, grantable: false }] },
    ...["shop", "inventory", "mysql"].map((database) => ({ id: `server:${database}`, label: `${database}.*`, level: "database", database, pattern: false, system: database === "mysql", cells: [] })),
  ],
};
const accountsReport = {
  schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, warnings: [],
  accounts: [
    { name: "app", host: "%", plugin: "caching_sha2_password", locked: false, expired: false, role: false },
    { name: "reader", host: "%", role: true, locked: true },
    { name: "mysql.sys", host: "localhost", locked: true }],
  role_grants: [], default_roles: [],
};
let accountPlanCount = 0;
let latestAccountPlan = null;
const accountPlanMock = (body) => {
  accountPlanCount += 1;
  latestAccountPlan = {
    schema_version: "account-plan-1.0", plan_id: `plan-${accountPlanCount}`, change_set_id: `set-${accountPlanCount}`,
    name: body.user, host: body.host, role: body.role, operation: body.operation, account: body.user, engine: "mysql", version: "8.4.0",
    request: Object.fromEntries(Object.entries(body).filter(([key]) => !["connection", "password"].includes(key))),
    before: structuredClone(accountDetail), expect: "fp-1", status: "ready",
    statements: [`GRANT ${(body.privileges || ["SELECT"]).join(", ")} ON \`${body.database}\`.* TO \`${body.user}\`@\`${body.host}\``],
    warnings: [], guards: { blocked: [], typed_name: `${body.user}@${body.host}` },
    privileges: { state: "allowed", allowed: true, missing: [], reason: "Required privileges are present." },
    policy: { action: "confirm", text: "Development: confirm this account change." },
  };
  return ok(latestAccountPlan);
};
const accountApplyMock = () => ok({
  schema_version: "account-change-1.0", change_set_id: latestAccountPlan?.change_set_id,
  status: "applied", message: "Statements accepted.", before: latestAccountPlan?.before,
  after: { ...accountDetail, grants: ["GRANT SELECT ON *.* TO `app`@`%`", "GRANT CREATE ROUTINE ON `shop`.* TO `app`@`%`"] },
  diff: { added: ["GRANT CREATE ROUTINE ON `shop`.* TO `app`@`%`"], removed: [] },
  statements: (latestAccountPlan?.statements || []).map((sql, index) => ({ index, statement: sql, status: "ok" })),
  warnings: [], audit_ids: ["audit-1"],
});

// Kill reviews and sends (Locks and Processlist), as locks.mjs/sessions.mjs mock them.
const killPrepare = (body) => ok({ review_token: "review-kill",
  statements: [`/*myflames:kill*/ KILL ${body.mode === "query" ? "QUERY" : "CONNECTION"} ${body.connection_id}`],
  expect: body.expect, connection_id: body.connection_id, mode: body.mode });
const killReply = { schema_version: "kill-1.0", status: "killed", connection_id: 53, mode: "connection",
  statement: "KILL CONNECTION 53", verified: ["user", "host", "not this session", "server thread", "transaction"],
  message: "Ended connection 53. Its open transaction was rolled back (1 row changed) and its locks were released.",
  target: { connection_id: 53, user: "app", host: "172.17.0.1", command: "Sleep", state: null, info: null, time_s: 5,
    trx: { trx_id: "2123", state: "RUNNING", started: "2026-10-14 16:57:12", age_s: 5, rows_locked: 1, rows_modified: 1 }, trx_checked: true },
  after: { present: false, trx_checked: true, trx_started: null, rows_locked: null, same_trx: null } };
const bulkPrepare = (body) => ok({ review_token: "review-bulk", targets: body.targets, mode: body.targets[0]?.mode || "query",
  statements: body.targets.map((t) => `/*myflames:kill*/ KILL ${t.mode === "query" ? "QUERY" : "CONNECTION"} ${t.connection_id}`) });
const bulkReply = (body) => ok({ schema_version: "kill-bulk-1.0", total: body.targets.length, killed: body.targets.length,
  gone: 0, refused: 0, denied: 0, errors: 0, message: `Ended ${body.targets.length} ${body.targets.length === 1 ? "query" : "queries"}.`,
  results: body.targets.map((t) => ({ connection_id: t.connection_id, status: "killed", message: "Ended." })) });

// Explain top N captures (as digest-actions.mjs mocks the capture engine: a
// queued job per start, complete with the plan on the first status poll).
const topJobs = new Map();
const topPlan = (query) => JSON.stringify({ query, operation: "Sort: orders.total DESC", access_type: "sort",
  estimated_rows: 3000, estimated_total_cost: 302.75,
  inputs: [{ operation: "Table scan on orders", access_type: "table", table_name: "orders", estimated_rows: 3000, estimated_total_cost: 302.75 }] });
const topCapture = (request) => ({ schema_version: "capture-1.0", raw: topPlan(request.query), query: request.query, mode: request.mode || "explain",
  engine: "mysql", server_version: "8.4.3", captured_at: CLOCK, conditions: "", warnings: [],
  runs: [{ total_time_ms: null, statistics: null, session_status_delta: {}, plan: "" }],
  measurements: { count: 0, median_ms: null, min_ms: null, max_ms: null, stdev_ms: null },
  schema: {}, table_stats: {}, variables: {}, trace: null, ...(request.source ? { source: request.source } : {}) });
const topMocks = {
  "/api/live/top": ok(topLab),
  "/api/live/start": (body) => { const id = `howto-top-${topJobs.size + 1}`; topJobs.set(id, body); return ok({ id, state: "queued" }); },
  "/api/live/status": (body) => ok({ id: body.id, state: "complete", progress: 1, repeat: 1, error: "",
    result: topJobs.has(body.id) ? topCapture(topJobs.get(body.id)) : null }),
};

// Parameters: one generic variable detail and plan per name (as config.mjs
// builds them over the variable fixture), plus the server comparison.
const parameterVariable = (name) => {
  const p = parametersCatalog.parameters.items.find((item) => item.name === name) || {};
  const v = structuredClone(variableGlobal.variable);
  Object.assign(v, { name, type: p.type || "integer", values: p.values || null, min: p.min ?? 1, max: p.max ?? 100000,
    global_value: p.value ?? "151", default: p.default ?? "151", scope: "both", session_value: p.value ?? "151", nullable: false,
    dynamic: p.apply_type === "dynamic", description: p.description, read_at: "2026-10-14T16:26:00Z" });
  for (const m of Object.values(v.modes)) { m.available = true; m.recommended = false; m.reason = null; }
  if (v.modes.global) v.modes.global.recommended = true;
  return v;
};
const parameterPlan = (body) => {
  const v = parameterVariable(body.name);
  const verb = { global: "SET GLOBAL", persist: "SET PERSIST", persist_only: "SET PERSIST_ONLY", session: "SET SESSION" }[body.mode] || "SET GLOBAL";
  const sql = body.mode === "reset_persist" ? `RESET PERSIST ${body.name}` : `${verb} ${body.name} = ${body.value}`;
  const plan = structuredClone(variableGlobal.plan);
  Object.assign(plan, { engine: "mysql", name: body.name, variable: v, mode: body.mode, requested: body.value, value: body.value, display: body.value,
    statement: sql, status: "ready", problems: [],
    before: { global: v.global_value, session: v.session_value, persisted: null, persisted_state: { state: "absent", present: null, value: null } },
    policy: { action: "confirm", tone: "primary", title: "Development", text: "" },
    privileges: { need: "SYSTEM_VARIABLES_ADMIN", note: "", checked: true, allowed: true, via: null },
    config_line: `${body.name} = ${body.value}`,
    effect: { now: true, after_restart: body.mode !== "global", needs_restart: false, text: "Changes the running value." } });
  plan.expect = { name: body.name, mode: body.mode, statement: sql, global_value: v.global_value, session_value: v.session_value, persisted_value: null };
  return plan;
};
const parameterMocks = {
  "/api/live/config": ok(parametersCatalog),
  "/api/live/variable": (body) => ok(parameterVariable(body.name)),
  "/api/live/variable/plan": (body) => ok(parameterPlan(body)),
  "/api/live/variable/apply": (body) => body.declined
    ? ok({ status: "declined" })
    : ok({ ...structuredClone(variableGlobal.change), name: body.name, mode: body.mode, status: "changed", message: "The new value was read back." }),
  "/api/config/compare": ok(compareFixtures.parameters),
};

// Table structure (Server > Tables), a small table-structure-1.0 body.
const tableStructure = (body) => ok({ schema_version: "table-structure-1.0", schema: body.schema, table: body.table, type: "table",
  engine: "InnoDB", collation: "utf8mb4_0900_ai_ci", comment: "",
  columns: [
    { name: "id", position: 1, type: "bigint unsigned", nullable: false, default: null, extra: "auto_increment", comment: "", character_set: null, collation: null, key: "PRI", generated: null },
    { name: "note", position: 2, type: "varchar(190)", nullable: true, default: null, extra: "", comment: "", character_set: "utf8mb4", collation: "utf8mb4_0900_ai_ci", key: "", generated: null },
    { name: "created_at", position: 3, type: "datetime", nullable: false, default: "CURRENT_TIMESTAMP", extra: "", comment: "", character_set: null, collation: null, key: "", generated: null }],
  indexes: [{ name: "PRIMARY", primary: true, unique: true, type: "BTREE", comment: "", visible: true, columns: [{ name: "id", expression: null, sub_part: null, order: "asc" }] }],
  foreign_keys: [],
  create_table: `CREATE TABLE \`${body.table}\` (\n  \`id\` bigint unsigned NOT NULL AUTO_INCREMENT,\n  \`note\` varchar(190) DEFAULT NULL,\n  \`created_at\` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,\n  PRIMARY KEY (\`id\`\n) ENGINE=InnoDB`,
  create_error: null, filename: `${body.schema}.${body.table}.sql` });

// The replication read flips to healthy once the reviewed skip is sent.
let replicationSkipSent = false;
const replicationMocks = {
  "/api/live/replication": () => ok(replicationSkipSent ? replicationHealthy : replicationError),
  "/api/live/replication/skip": () => { replicationSkipSent = true; return ok(replicationSkipped); },
  "/api/live/replication/skip-loop/status": ok({ schema_version: "replication-skip-loop-1.0", loops: [] }),
  "/api/live/instruments": ok(instrumentsLive),
};

// The driver boots every guide through boot() above, which passes the shared
// `extra` map: this block's endpoints are added to it here (new keys only),
// and the two endpoints the editor guides already use delegate to the
// original mock unless the request is clearly this block's (a write review, or
// a capture started for a digest batch, which carries a source).
// The write reviews of Indexes, Tables and Instruments run through the editor
// API (ReviewedSqlAction): preview, then a needs_confirmation run, then the
// confirmed run (as indexes.mjs and instruments.mjs mock them).
const isWriteSql = (sql) => /^(ALTER|OPTIMIZE|ANALYZE|DROP|UPDATE\s+performance_schema)/i.test((sql || "").trim());
const baseEditorRun = extra["/api/editor/run"];
const baseLiveStart = extra["/api/live/start"];
const baseLiveStatus = extra["/api/live/status"];
Object.assign(extra, {
  "/api/editor/run": (body) => {
    if (body.decline) return ok({ status: "declined" });
    if (body.confirmed) return ok({ status: "complete", session: session(), run: { ...runBase(body.sql),
      statements: [{ ...statement(body.sql), verb: "write", kind: "write", category: "write" }],
      results: [], counts: { statements: 1, run: 1, rows: 0, affected_rows: 0, warnings: 0, errors: 0 } } });
    if (isWriteSql(body.sql)) return ok({ status: "needs_confirmation", confirmation: { token: "howto-review",
      text: "Review the statement before it runs.", tone: "primary",
      connection: { user: "dba", host: "dev.example", port: 3306 }, connection_type: "development",
      warnings: [], statements: [body.sql], typed_target: null } });
    return baseEditorRun(body);
  },
  "/api/live/start": (body) => body.source ? topMocks["/api/live/start"](body) : baseLiveStart,
  "/api/live/status": (body) => topJobs.has(body.id) ? topMocks["/api/live/status"](body) : baseLiveStatus,
  "/api/live/locks": () => ok({ ...locksDdl, captured_at: new Date(Date.now() + 1000).toISOString() }),
  "/api/live/kill/prepare": killPrepare,
  "/api/live/kill": ok(killReply),
  "/api/live/kill/bulk/prepare": bulkPrepare,
  "/api/live/kill/bulk": (body) => bulkReply(body),
  "/api/live/sessions": () => ok({ ...sessionsActive, taken_at: new Date(Date.now() + 1000).toISOString() }),
  // Query digest reads ask for the server's identity (with_identity), which
  // baselines require; Top statements reads do not and keep the lab fixture.
  "/api/live/top": (body) => (body && body.with_identity ? ok(digestReport) : topMocks["/api/live/top"]),
  "/api/live/indexes": ok(indexesReport),
  "/api/live/tables": ok(tablesFragmented),
  "/api/live/table-structure": tableStructure,
  "/api/live/instruments": ok(instrumentsSelected),
  "/api/accounts/list": ok(accountsReport),
  "/api/accounts/read": (body) => ok({ ...accountDetail, name: body.user, host: body.host }),
  "/api/accounts/scope-objects": (body) => ok({ database: body.database, tables: [{ name: "orders" }, { name: "customers" }], procedures: [], functions: [], truncated: false, warnings: [] }),
  "/api/accounts/plan": accountPlanMock,
  "/api/accounts/apply": accountApplyMock,
  "/api/live/replication": replicationMocks["/api/live/replication"],
  "/api/live/replication/skip": replicationMocks["/api/live/replication/skip"],
  "/api/live/replication/skip-loop/status": replicationMocks["/api/live/replication/skip-loop/status"],
  "/api/live/cluster": ok(clusterHealthy),
  "/api/live/cluster/plan": ok(clusterPlan),
  "/api/live/cluster/action": { ...ok(clusterResult), delay: 600 },
  "/api/live/action/progress": ok(clusterProgress),
  "/api/live/proxysql": ok(proxysqlLab),
  "/api/live/proxysql/plan": (body) => ok(body.action === "servers_save" ? proxysqlPlanSave : proxysqlPlanOffline),
  "/api/live/proxysql/action": { ...ok(proxysqlDone), delay: 500 },
  "/api/live/proxysql/digests": ok(proxysqlDigests),
  "/api/live/readyset": ok(readysetLab),
  "/api/live/readyset/explain-cache": ok(readysetExplain),
  "/api/live/readyset/plan": ok(readysetPlan),
  "/api/live/readyset/action": { ...ok(readysetDone), delay: 500 },
  "/api/connections/import": ok({ found: true, cancelled: false, items: [
    { name: "Bench MySQL", host: "bench.example", port: 3306, user: "reader", database: "" },
    { name: "Reports MariaDB", host: "reports.example", port: 3307, user: "etl", database: "reports",
      ssh_host: "bastion.example", ssh_user: "deploy", ssh_port: 2222 }] }),
  ...parameterMocks,
});

/** Choose the seeded Development connection from the toolbar picker. */
async function useDev(h, rec) {
  await rec.caption("choose Dev MySQL");
  await chooseConnection(h.page, h.seeded["Dev MySQL"]);
  await rec.caption("");
  await rec.hold(300);
}
/** Click the first visible element whose text starts with *text*. */
async function clickText(page, selector, text) {
  const handle = await page.waitForFunction((sel, t) => [...document.querySelectorAll(sel)]
    .find((b) => b.textContent.trim().startsWith(t) && b.getClientRects().length), {}, selector, text);
  await handle.asElement().click();
  await handle.dispose();
}
/** A place of its own in the sidebar (Replication, Cluster, ProxySQL, Readyset). */
async function openPlace(h, place) {
  await h.page.waitForSelector(`.main-nav button[data-place="${place}"]`, { visible: true });
  await h.page.click(`.main-nav button[data-place="${place}"]`);
  await h.page.waitForSelector(`.server-view[data-view="${place}"]`, { visible: true });
}
/** A topology place read under the previous connection keeps it ("From …,
 * refresh to load this server"); Refresh reads it for the connection in use. */
async function refreshForConnection(h, place) {
  const before = h.state.seen.length;
  await h.refresh();
  for (let i = 0; i < 150 && h.state.seen.length === before; i++) await pause(20);
  await h.page.waitForFunction((sel) => [...document.querySelectorAll(sel)]
    .some((v) => v.offsetParent && !v.classList.contains("is-other-connection") && !v.querySelector(".spinner")),
    { timeout: 15000 }, `.server-view[data-view="${place}"]`);
}

GUIDES.push(
  // ---- docs/handbook/howto-server.md ----
  ["kill-blocking", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Locks");
    await h.server();
    await useDev(h, rec);
    await h.view("locks");
    const view = '.server-view[data-view="locks"]';
    await page.waitForSelector(`${view} .lock-tree`, { visible: true });
    await rec.caption("");
    await rec.hold(1300);
    await rec.caption("select the blocker");
    await h.select(`${view} .lock-tree tr[data-connection="53"]`);
    await rec.hold(1100);
    await rec.caption("click Kill…");
    await page.click('.view-inspector [aria-label="Kill blocker: connection 53"]');
    await page.waitForSelector("dialog.kill-dialog[open]");
    // The reviewed statement (kill/prepare) enables the confirm button.
    await page.waitForFunction(() => {
      const b = document.querySelector("dialog.kill-dialog .sheet-actions .danger-solid");
      return b && !b.disabled && b.getAttribute("aria-disabled") !== "true";
    });
    await rec.caption("");
    await rec.hold(1600);
    await rec.caption("Kill connection 53");
    await page.click("dialog.kill-dialog .sheet-actions .danger-solid");
    await page.waitForFunction(() => [...document.querySelectorAll(".kill-result")]
      .some((e) => e.classList.contains("killed") && e.offsetParent !== null));
    await rec.caption("");
    await rec.hold(1400);
  }],
  ["top-statements", async (h, rec) => {
    const { page } = h;
    await rec.caption("Workload > Top statements");
    await h.workload();
    await useDev(h, rec);
    await h.view("top");
    const view = '.server-view[data-view="top"]';
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.caption("");
    await rec.hold(2500);
    await rec.caption("select a statement");
    await h.select(`${view} tbody tr[data-row-key]`);
    await rec.hold(2500);
    // The list's toolbar (with Explain top N) shows again after Back.
    await rec.caption("Back to statements");
    await clickText(page, `${view} button`, "Back to statements");
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(1000);
    await rec.caption("click Explain top 5");
    await clickText(page, `${view} button`, "Explain top 5");
    await page.waitForFunction(() => /plans added to the open plans/.test(
      [...document.querySelectorAll(".server-view[data-view=\"top\"]")].map((v) => v.textContent).join(" ")), { timeout: 20000 });
    assert.ok(h.state.other.filter((o) => o.path === "/api/live/start").length > 0, "captures started");
    await rec.caption("");
    await rec.hold(2500);
  }],
  ["kill-sessions", async (h, rec) => {
    const { page } = h;
    await rec.caption("Workload > Processlist");
    await h.workload();
    await useDev(h, rec);
    await h.view("sessions");
    const view = '.server-view[data-view="sessions"]';
    await page.waitForSelector(`${view} tbody tr[data-session]`, { visible: true });
    await rec.caption("");
    await rec.hold(1200);
    await rec.caption("click Kill sessions…");
    await page.click(`${view} .view-toolbar button[data-action=kill-sessions]`);
    await page.waitForSelector("dialog.mass-kill-dialog[open]");
    await rec.hold(600);
    await rec.caption("Kill query");
    await clickText(page, 'dialog.mass-kill-dialog .segmented[aria-label="Kill mode"] button', "Kill query");
    await rec.hold(400);
    await rec.caption("preset: Runaway queries");
    await clickText(page, "dialog.mass-kill-dialog .mass-kill-presets button", "Runaway queries");
    await page.waitForFunction(() => /[1-9]\d* matching sessions/.test(document.querySelector(".preview-title")?.textContent || ""));
    await rec.caption("");
    await rec.hold(1100);
    const footer = await page.waitForFunction(() => {
      const b = document.querySelector(".mass-kill-footer-actions button.danger");
      return b && !b.disabled && /Kill \d+ quer/.test(b.textContent) ? b : false;
    });
    await rec.caption("review the sessions");
    await rec.hold(700);
    await footer.asElement().click();
    await footer.dispose();
    await page.waitForSelector("dialog.confirm-sheet[open]");
    await page.waitForFunction(() => /KILL QUERY \d+/.test(document.querySelector("dialog.confirm-sheet")?.textContent || ""));
    await rec.caption("check the statements");
    await rec.hold(1500);
    await clickText(page, "dialog.confirm-sheet .sheet-actions button", "Send ");
    await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"), { timeout: 10000 });
    assert.ok(h.state.other.some((o) => o.path === "/api/live/kill/bulk"), "the reviewed KILLs were sent");
    await rec.caption("");
    await rec.hold(1300);
  }],
  ["hide-index", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Indexes");
    await h.server();
    await useDev(h, rec);
    await h.view("indexes");
    const view = '.server-view[data-view="indexes"]';
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(1000);
    await rec.caption("filter: Unused");
    await clickText(page, `${view} .segmented button`, "Unused");
    const rows = `${view} [aria-label="Unused indexes"] tbody tr[data-row-key]`;
    await page.waitForSelector(rows, { visible: true });
    await rec.caption("");
    await rec.hold(1500);
    await rec.caption("select the index");
    await h.select(rows);
    await rec.hold(2500);
    await rec.caption("Set invisible…");
    await page.$eval(`${rows} .row-more`, (b) => { b.focus(); b.click(); });
    await clickText(page, ".row-action-menu [role=menuitem]", "Set invisible…");
    await page.waitForSelector("dialog.confirm-sheet[open]");
    await page.waitForFunction(() => /ALTER TABLE/.test(document.querySelector("dialog.confirm-sheet .sql-preview")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3500);
    await rec.caption("confirm");
    await page.click("dialog.confirm-sheet .sheet-actions button:last-child");
    await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"), { timeout: 10000 });
    await page.waitForFunction(() => /completed/.test(document.querySelector("[data-reviewed-status]")?.textContent || ""));
    await rec.caption("");
    await rec.hold(2500);
  }],
  ["optimize-table", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Tables");
    await h.server();
    await useDev(h, rec);
    await h.view("tables");
    const view = '.server-view[data-view="tables"]';
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(700);
    await rec.caption("filter: Fragmentation");
    await clickText(page, `${view} .segmented[aria-label="List"] button`, "Fragmentation");
    const row = `${view} [aria-label="Fragmentation by table"] tbody tr[data-row-key]`;
    await page.waitForSelector(row, { visible: true });
    await rec.caption("");
    await rec.hold(900);
    await rec.caption("select the table");
    await h.select(row);
    await rec.hold(1500);
    await rec.caption("Table structure");
    await clickText(page, ".view-inspector button", "Table structure");
    await page.waitForSelector("dialog.text-sheet[open] .table-structure-table", { visible: true });
    await rec.caption("");
    await rec.hold(3500);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.text-sheet"));
    await rec.hold(1000);
    await rec.caption("Optimize table…");
    await page.$eval(`${row} .row-more`, (b) => { b.focus(); b.click(); });
    await clickText(page, ".row-action-menu [role=menuitem]", "Optimize table…");
    await page.waitForSelector("dialog.confirm-sheet[open]");
    await page.waitForFunction(() => /OPTIMIZE TABLE/.test(document.querySelector("dialog.confirm-sheet .sql-preview")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3500);
    await rec.caption("confirm");
    await page.click("dialog.confirm-sheet .sheet-actions button:last-child");
    await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"), { timeout: 10000 });
    await page.waitForFunction(() => /completed/.test(document.querySelector("[data-reviewed-status]")?.textContent || ""));
    await rec.caption("");
    await rec.hold(2500);
  }],
  ["parameters-compare", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Parameters");
    await h.server();
    await useDev(h, rec);
    await h.view("config");
    const view = '.server-view[data-view="config"]';
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(700);
    await rec.caption("Show: Recommendations");
    await page.select(`${view} .toolbar-field select`, "recommendations");
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.caption("");
    await rec.hold(900);
    await rec.caption("select a parameter");
    const name = await page.$eval(`${view} tbody tr[data-row-key]`, (tr) => tr.dataset.rowKey);
    await h.select(`${view} tbody tr[data-row-key="${name}"]`);
    await rec.hold(1600);
    // The parameter's detail covers the list; Back returns to it before Edit.
    await rec.caption("Back");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".view-inspector"));
    await rec.hold(1000);
    await rec.caption("click Edit");
    await page.click(`${view} [data-action="edit-parameters"]`);
    const input = `${view} #parameter-value-${name}`;
    await page.waitForSelector(input, { visible: true });
    await rec.hold(400);
    await rec.caption("enter the new value");
    if (await page.$eval(input, (e) => e.getAttribute("role") === "switch" || e.tagName === "SELECT")) {
      await page.click(input);
    } else {
      await page.click(input, { clickCount: 3 });
      await page.keyboard.type("200", { delay: 90 });
    }
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="config"] .toolbar-controls button')]
      .some((b) => b.textContent === "Review and apply (1)"));
    await rec.hold(1200);
    await rec.caption("Review and apply (1)");
    await clickText(page, `${view} .toolbar-controls button`, "Review and apply (1)");
    await page.waitForSelector("dialog.parameter-batch-sheet[open]");
    await page.waitForFunction(() => /SET (PERSIST|GLOBAL)/.test(document.querySelector("dialog.parameter-batch-sheet")?.textContent || ""));
    await rec.caption("");
    await rec.hold(2800);
    await rec.caption("confirm");
    await page.click("dialog.parameter-batch-sheet .sheet-actions button:last-of-type");
    await page.waitForFunction(() => !document.querySelector("dialog.parameter-batch-sheet"), { timeout: 10000 });
    await rec.hold(1300);
    await rec.caption("Compare tab");
    await page.click("#parameters-views-tab-compare");
    await page.waitForSelector('[data-view="parameters-compare"] select[aria-label="Target"]', { visible: true });
    await rec.hold(1000);
    await rec.caption("Target: Prod MySQL");
    const target = await page.$eval('[data-view="parameters-compare"] select[aria-label="Target"]', (sel) =>
      [...sel.options].find((o) => /Prod MySQL/.test(o.textContent))?.value);
    assert.ok(target, "Prod MySQL is a compare target");
    await page.select('[data-view="parameters-compare"] select[aria-label="Target"]', target);
    await rec.hold(1000);
    await rec.caption("click Compare");
    await page.click('[data-view="parameters-compare"] .server-compare-run button');
    await page.waitForSelector('[data-view="parameters-compare"] .comparison-header', { visible: true });
    await rec.caption("");
    await rec.hold(2800);
  }],
  ["instruments-preset", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Instruments");
    await h.server();
    await useDev(h, rec);
    await h.view("instruments");
    const view = '.server-view[data-view="instruments"]';
    await page.waitForSelector("#instr-ps", { visible: true });
    await rec.caption("");
    await rec.hold(1100);
    await rec.caption("Preset: Wait analysis");
    await page.select(`${view} select[aria-label=Preset]`, "waits");
    await rec.hold(1400);
    await rec.caption("Show statements");
    await page.click(`${view} .instr-show-statements`);
    await page.waitForSelector(".view-inspector .perf-sql pre", { visible: true });
    await rec.caption("");
    await rec.hold(2600);
    await rec.caption("Apply Wait analysis…");
    await page.click('.view-inspector [data-reviewed-sql="Apply Wait analysis"]');
    await page.waitForSelector("dialog.confirm-sheet[open]");
    await page.waitForFunction(() => /UPDATE performance_schema/.test(document.querySelector("dialog.confirm-sheet .sql-preview")?.textContent || ""));
    await rec.hold(2800);
    await rec.caption("confirm");
    await page.click("dialog.confirm-sheet .sheet-actions button:last-child");
    await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"), { timeout: 10000 });
    await page.waitForFunction(() => /completed/.test(document.querySelector("[data-reviewed-status]")?.textContent || ""));
    await rec.caption("");
    await rec.hold(2000);
  }],
  ["users-grants", async (h, rec) => {
    const { page } = h;
    await rec.caption("Server > Users and roles");
    await h.server();
    await useDev(h, rec);
    await h.view("accounts");
    const view = '.server-view[data-view="accounts"]';
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.caption("");
    await rec.hold(1100);
    await rec.caption("select app@%");
    await h.select(`${view} tbody tr[data-row-key]`);
    await page.waitForSelector(".account-matrix", { visible: true });
    await rec.hold(1400);
    await rec.caption("Grant level: Databases");
    await page.select('select[aria-label="Grant level"]', "database");
    await rec.hold(1000);
    await rec.caption("Add scope");
    await clickText(page, ".view-inspector button", "Add scope");
    await page.waitForSelector('.account-scope-form select[aria-label="Database"]', { visible: true });
    await page.select('.account-scope-form select[aria-label="Database"]', "shop");
    await rec.hold(1000);
    await clickText(page, ".account-scope-form button", "Use scope");
    await page.waitForSelector(".account-matrix", { visible: true });
    await rec.hold(1000);
    await rec.caption("tick CREATE ROUTINE");
    await page.$$eval(".account-matrix tbody tr", (rows) => {
      const row = rows.find((r) => (r.querySelector(".account-privilege-name")?.textContent || r.querySelector("th")?.textContent) === "CREATE ROUTINE");
      row?.querySelector("input")?.click();
    });
    await rec.hold(1200);
    await rec.caption("Review changes");
    await clickText(page, ".view-inspector button", "Review changes");
    await page.waitForSelector(".account-review[open]", { visible: true });
    await page.waitForFunction(() => /GRANT CREATE ROUTINE ON `shop`\.\*/.test(document.querySelector(".account-review")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3200);
    await rec.caption("type app@% to confirm");
    await page.click(".account-review .account-typed input");
    await page.keyboard.type("app@%", { delay: 90 });
    await rec.hold(1000);
    await rec.caption("apply");
    await page.click(".account-review .sheet-actions button:last-of-type");
    await page.waitForFunction(() => !document.querySelector(".account-review[open]"), { timeout: 10000 });
    await page.waitForSelector('[aria-label="Account change result"]', { visible: true });
    await rec.caption("");
    await rec.hold(2600);
  }],
  // ---- docs/handbook/howto-connections.md ----
  // The group bar is the same in the four topology places; this one uses Cluster.
  ["connection-group", async (h, rec) => {
    const { page } = h;
    await rec.caption("Cluster");
    await openPlace(h, "cluster");
    await useDev(h, rec);
    await rec.caption("Refresh");
    await refreshForConnection(h, "cluster");
    await rec.caption("");
    await page.waitForSelector('.server-view[data-view="cluster"] .cluster-topology', { visible: true });
    await page.waitForSelector('.server-view[data-view="cluster"] .cluster-topology', { visible: true });
    await page.waitForFunction(() => /No connection group/.test(
      [...document.querySelectorAll(".connection-group-line")].map((e) => e.textContent).join(" ")));
    await rec.caption("");
    await rec.hold(1300);
    await rec.caption("Create a group…");
    await clickText(page, ".connection-group-actions button", "Create a group…");
    await page.waitForSelector("dialog.connection-group-sheet[open]", { visible: true });
    await rec.hold(1300);
    await rec.caption("name the group");
    await page.click(".connection-group-field input");
    await page.keyboard.type("shop-pipeline", { delay: 70 });
    // The Create sheet groups the connection in use; ticking other saved
    // connections is the Review sheet's flow (the bar's Review… state).
    await page.waitForFunction(() => [...document.querySelectorAll("dialog.connection-group-sheet .sheet-actions button")]
      .some((b) => /Add 1 to shop-pipeline/.test(b.textContent) && !b.disabled));
    await rec.hold(1400);
    await rec.caption("Add to shop-pipeline");
    await clickText(page, "dialog.connection-group-sheet .sheet-actions button", "Add ");
    await page.waitForFunction(() => !document.querySelector("dialog.connection-group-sheet[open]"), { timeout: 10000 });
    await page.waitForFunction(() => /shop-pipeline/.test(
      [...document.querySelectorAll(".connection-group-line, .connection-group-summary, [data-state]")].map((e) => e.textContent).join(" ")));
    await rec.caption("");
    await rec.hold(2600);
  }],
  ["import-connections", async (h, rec) => {
    const { page } = h;
    await rec.caption("Home: Connections menu");
    await page.click('.connections-header-actions button[aria-label="Connections menu"]');
    await rec.hold(300);
    await rec.caption("Import from MySQL Workbench or DBeaver…");
    await clickText(page, '[role="menu"] button', "Import from MySQL Workbench or DBeaver…");
    await page.waitForSelector("dialog.connection-import-sheet[open]");
    await page.waitForFunction(() => document.querySelector(".connection-import-preview")?.textContent.includes("Reports MariaDB"));
    await rec.caption("");
    await rec.hold(1500);
    await rec.caption("Import 2 connections");
    await clickText(page, "dialog.connection-import-sheet button", "Import 2 connections");
    await page.waitForFunction(() => document.querySelector("dialog.connection-import-sheet")?.textContent.includes("Imported 2 connections."));
    await rec.caption("");
    await rec.hold(1000);
    await clickText(page, "dialog.connection-import-sheet button", "Done");
    await page.waitForFunction(() => /Reports MariaDB/.test(document.body.textContent) && !document.querySelector("dialog.connection-import-sheet"));
    await rec.hold(1300);
  }],
  // ---- docs/handbook/howto-topology.md ----
  ["replication-skip", async (h, rec) => {
    const { page } = h;
    await rec.caption("Replication");
    await openPlace(h, "replication");
    await useDev(h, rec);
    await rec.caption("Refresh");
    await refreshForConnection(h, "replication");
    await rec.caption("");
    const view = '.server-view[data-view="replication"]';
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="replication"]')]
      .some((v) => v.offsetParent && v.querySelector(".summary-line")), { timeout: 15000 });
    await rec.hold(1300);
    await rec.caption("Recovery");
    await clickText(page, '[aria-label="Replication sub-tabs"] button', "Recovery");
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="replication"]')]
      .some((v) => v.offsetParent && v.querySelector('[data-row-key="channel:"]')));
    await rec.hold(700);
    await rec.caption("select the stopped channel");
    await h.select(`${view} [data-row-key="channel:"]`);
    await page.waitForFunction(() => /1062/.test(document.querySelector(".view-inspector")?.textContent || ""));
    await rec.hold(1800);
    await rec.caption("Skip this transaction…");
    await clickText(page, ".view-inspector button", "Skip this transaction");
    await page.waitForSelector("dialog.ops-confirm[open]");
    await page.waitForFunction(() => /GTID|sql_slave_skip/i.test(document.querySelector("dialog.ops-confirm")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3000);
    await rec.caption("Skip transaction");
    await page.click("dialog.ops-confirm .sheet-actions button:last-of-type");
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="replication"] .ops-result')]
      .some((e) => e.dataset.status === "skipped" && e.offsetParent !== null), { timeout: 10000 });
    await rec.caption("");
    await rec.hold(2400);
  }],
  ["cluster-primary", async (h, rec) => {
    const { page } = h;
    await rec.caption("Cluster");
    await openPlace(h, "cluster");
    await useDev(h, rec);
    await rec.caption("Refresh");
    await refreshForConnection(h, "cluster");
    await rec.caption("");
    const view = '.server-view[data-view="cluster"]';
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="cluster"]')]
      .some((v) => v.offsetParent && v.querySelector(".cluster-topology")), { timeout: 15000 });
    await rec.hold(1400);
    await rec.caption("Members");
    await clickText(page, `${view} .segmented button`, "Members");
    await page.waitForSelector(`${view} tbody tr[data-row-key^="member:"]`, { visible: true });
    await rec.hold(700);
    await rec.caption("select a secondary");
    await h.select(`${view} tr[data-row-key="${clusterPlan.member_key}"]`);
    await rec.hold(1000);
    await rec.caption("Make primary…");
    await clickText(page, ".view-inspector .review-actions button", "Make primary…");
    await page.waitForSelector("dialog.action-review[open]", { visible: true });
    await page.waitForFunction(() => /group_replication_set_as_primary/.test(document.querySelector("dialog.action-review")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3000);
    await rec.caption("confirm");
    await page.click("dialog.action-review[open] .sheet-actions button:last-child");
    await page.waitForSelector("dialog.action-review[open] .action-outcome", { visible: true, timeout: 10000 });
    await rec.hold(2200);
    await clickText(page, "dialog.action-review[open] .sheet-actions button", "Close");
    await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
    await rec.caption("");
    await rec.hold(1600);
  }],
  ["proxysql-offline", async (h, rec) => {
    const { page } = h;
    await rec.caption("ProxySQL");
    await openPlace(h, "proxysql");
    await useDev(h, rec);
    await rec.caption("Refresh");
    await refreshForConnection(h, "proxysql");
    await rec.caption("");
    const view = '.server-view[data-view="proxysql"]';
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="proxysql"]')]
      .some((v) => v.offsetParent && v.querySelector(".summary-line")), { timeout: 15000 });
    await rec.hold(1200);
    await rec.caption("Servers");
    await clickText(page, `${view} .segmented button`, "Servers");
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(800);
    await rec.caption("select the backend");
    await h.select(`${view} tbody tr[data-row-key="rt|32|mf-ic-2|3306"]`);
    await rec.hold(1000);
    await rec.caption("Set OFFLINE_SOFT…");
    await clickText(page, ".view-inspector .review-actions button", "Set OFFLINE_SOFT…");
    await page.waitForSelector("dialog.action-review[open]", { visible: true });
    await page.waitForFunction(() => /OFFLINE_SOFT/.test(document.querySelector("dialog.action-review .ops-pre")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3000);
    await rec.caption("confirm");
    await page.click("dialog.action-review[open] .sheet-actions button:last-child");
    await page.waitForSelector("dialog.action-review[open] .action-outcome", { visible: true, timeout: 10000 });
    await rec.hold(2300);
    await clickText(page, "dialog.action-review[open] .sheet-actions button", "Close");
    await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
    await rec.caption("");
    await rec.hold(1600);
  }],
  ["readyset-cache", async (h, rec) => {
    const { page } = h;
    await rec.caption("Readyset");
    await openPlace(h, "readyset");
    await useDev(h, rec);
    await rec.caption("Refresh");
    await refreshForConnection(h, "readyset");
    await rec.caption("");
    const view = '.server-view[data-view="readyset"]';
    await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="readyset"]')]
      .some((v) => v.offsetParent && v.querySelector(".summary-line")), { timeout: 15000 });
    await rec.hold(1200);
    await rec.caption("Proxied");
    await clickText(page, `${view} .segmented button`, "Proxied");
    await page.waitForSelector(`${view} tbody tr[data-row-key]`, { visible: true });
    await rec.hold(800);
    await rec.caption("select the query");
    await h.select(`${view} tbody tr[data-row-key="pq:q_3333cccc"]`);
    await rec.hold(900);
    await rec.caption("Create cache…");
    await clickText(page, ".view-inspector button", "Create cache…");
    await page.waitForSelector("dialog.readyset-create-dialog[open]", { visible: true });
    // The pre-filled query id runs the support check (EXPLAIN) on its own.
    await page.waitForFunction(() => /Shallow: Supported/.test(document.querySelector("dialog.readyset-create-dialog")?.textContent || ""), { timeout: 10000 });
    await rec.caption("Check support (EXPLAIN)");
    await rec.hold(3000);
    await rec.caption("Review CREATE CACHE…");
    await page.click('dialog.readyset-create-dialog .sheet-actions button:last-child');
    await page.waitForSelector("dialog.action-review[open]", { visible: true });
    await page.waitForFunction(() => /CREATE SHALLOW CACHE/.test(document.querySelector("dialog.action-review .ops-pre")?.textContent || ""));
    await rec.caption("");
    await rec.hold(3000);
    await rec.caption("Create shallow cache");
    await page.click("dialog.action-review[open] .sheet-actions button:last-child");
    await page.waitForSelector("dialog.action-review[open] .action-outcome", { visible: true, timeout: 10000 });
    await rec.hold(2200);
    await clickText(page, "dialog.action-review[open] .sheet-actions button", "Close");
    await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
    await rec.caption("");
    await rec.hold(1600);
  }],
);
// ============================================================================
// END server / connections / topology how-to recordings
// ============================================================================

// ---- driver -------------------------------------------------------------------
const chosen = GUIDES.filter(([id]) => !only || id === only);
if (!chosen.length) throw new Error(`Unknown guide "${only}". Known: ${GUIDES.map(([id]) => id).join(", ")}`);
const results = [];
for (const [id, steps] of chosen) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), `myflames-howto-${id}-`));
  const h = await boot();
  const rec = new Recorder(h.page, directory);
  try {
    rec.start();
    await steps(h, rec);
    await rec.hold(1000); // a one-second hold on the final state
  } finally {
    await rec.stop();
  }
  assert.deepEqual(h.errors, [], `${id}: page errors`);
  assert.deepEqual(h.state.blocked, [], `${id}: requests outside the local server (blocked)`);
  await h.close();
  await fs.rm(h.downloads, { recursive: true, force: true });
  const file = path.join(out, `howto-${id}.gif`);
  const frames = (await fs.readdir(directory)).filter((f) => f.endsWith(".png")).sort()
    .map((f) => path.join(directory, f));
  assert.ok(frames.length >= FPS * 3, `${id}: too few frames (${frames.length})`);
  // The capture loop skips ticks while a screenshot is in flight, so the real
  // rate is below FPS; play back at the measured rate (kept inside 8-12 fps)
  // for real-time motion.
  const playbackFps = Math.min(12, Math.max(8, Math.round(rec.frames / rec.seconds)));
  // Re-encode smaller until the GIF fits; report what it took.
  const attempts = [
    { fps: playbackFps, quality: 90, width: SIZE.width },
    { fps: playbackFps, quality: 70, width: SIZE.width },
    { fps: 8, quality: 70, width: 1024 },
    { fps: 8, quality: 60, width: 960 },
    { fps: 6, quality: 50, width: 854 },
  ];
  let encoded = null;
  for (const a of attempts) {
    execFileSync(GIFSKI, ["-o", file, "--fps", String(a.fps), "--quality", String(a.quality), "--width", String(a.width), ...frames],
      { stdio: ["ignore", "pipe", "pipe"] });
    const { size } = await fs.stat(file);
    encoded = { ...a, size };
    if (size <= MAX_BYTES) break;
  }
  const probe = JSON.parse(execFileSync("ffprobe", ["-v", "error", "-count_frames", "-select_streams", "v:0",
    "-show_entries", "stream=nb_read_frames,duration", "-of", "json", file], { encoding: "utf8" })).streams[0];
  const probedFrames = Number(probe.nb_read_frames);
  const seconds = Number(probe.duration);
  await fs.rm(directory, { recursive: true, force: true });
  results.push({ id, file, ...encoded, gifFrames: probedFrames, seconds });
  console.log(`howto-${id}.gif  ${(encoded.size / 1024).toFixed(0)} KB  ${probedFrames} frames  ${seconds.toFixed(1)} s  (${encoded.fps} fps q${encoded.quality} ${encoded.width}px)`);
  assert.ok(encoded.size <= MAX_BYTES, `${id}: still over 1.5 MB after re-encoding`);
}
console.log(`recorded ${results.length} guide(s) into ${path.relative(root, out)}`);
