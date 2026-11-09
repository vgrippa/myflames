// Statement inspector and Measure again contracts, executed without a browser.
// These assertions exercise the production module, never a second implementation.
// Run: node test/statement-thread-unit.mjs [details|origin|applied|measurement|identity|render]
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// SSR has no browser storage. Newer Node versions expose a Web Storage
// getter; prevent it from opening a store while imported UI modules initialize.
Object.defineProperty(globalThis, "localStorage", { value: undefined, configurable: true });

const result = await build({
  stdin: { contents: `export * from './src/statementThread';
    export { StatementInspector } from './src/StatementInspector';
    export { MeasuredStatement } from './src/MeasureAgain';
    export { createElement } from 'react';
    export { renderToStaticMarkup } from 'react-dom/server';`,
    loader: "ts", resolveDir: fileURLToPath(new URL("..", import.meta.url)) },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent", jsx: "automatic", loader: { ".css": "empty" },
  define: { "process.env.NODE_ENV": '"production"' },
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const { statementDetails, suggestedIndexOrigin, indexWasApplied, captureMeasurement, captureThreadKey, measurementMatches } = out.exports;
const { StatementInspector, MeasuredStatement, createElement, renderToStaticMarkup } = out.exports;

const SQL = "SELECT * FROM orders WHERE status = 'new'";
const DDL = "CREATE INDEX `idx_orders_status` ON `shop`.`orders` (`status`);";
const connection = {
  name: "Lab MySQL", profile_id: "mysql-lab", host: "127.0.0.1", port: 3306,
  user: "tester", database: "shop", ssl_mode: "PREFERRED", password: "never-store-this",
};
const row = (over = {}) => ({
  digest: "a".repeat(64), digest_text: "SELECT * FROM `orders` WHERE `status` = ?", schema: "shop",
  count: 20, total_ms: 900, avg_ms: 45, max_ms: 80, rows_examined: 6000, rows_sent: 200,
  examined_per_sent: 30, no_index_used: 20, no_good_index_used: 0, tmp_disk_tables: 0,
  sort_merge_passes: 0, sample_text: SQL, sample_truncated: false,
  first_seen: "2026-11-09 10:00:00", last_seen: "2026-11-09 10:10:00",
  flags: ["no_index_used", "low_selectivity"], ...over,
});
const run = (over = {}) => ({
  schema_version: "editor-run-1.0", id: "run-1", kind: "run", state: "complete", scope: "statement",
  started_at: "2026-11-09T13:00:00Z", finished_at: "2026-11-09T13:00:01Z", duration_ms: 1000, limit: 1000,
  statements: [{ index: 0, text: DDL, sent: DDL, start: 0, end: DDL.length,
    verb: "CREATE", kind: "write", category: "ddl", status: "ok", duration_ms: 1000 }],
  results: [{ type: "ok", statement: 0, affected_rows: 0, info: null, warnings: 0, duration_ms: 1000 }],
  error: null, stopped: null, message: null,
  counts: { statements: 1, run: 1, rows: 0, affected_rows: 0, warnings: 0, errors: 0 },
  audit_id: null, history_id: null, ...over,
});
const capture = (over = {}) => ({
  raw: JSON.stringify({ operation: "Table scan on orders", actual_rows: 777, actual_loops: 5, actual_last_row_ms: 91 }),
  query: SQL, mode: "analyze", engine: "mysql", server_version: "8.4.3", captured_at: "2026-11-09T13:00:00Z",
  runs: [
    { total_time_ms: 90, statistics: { timer_wait_ps: 90000000000, rows_examined: 5000 }, session_status_delta: {}, plan: "" },
    { total_time_ms: 17, statistics: { timer_wait_ps: 12500000000, rows_examined: 8 }, session_status_delta: {}, plan: "" },
  ],
  measurements: { count: 2, median_ms: 53.5, min_ms: 17, max_ms: 90, stdev_ms: 36.5 },
  schema: {}, table_stats: {}, variables: {}, trace: null, warnings: [], conditions: "", ...over,
});

const cases = {
  details() {
    const mysql = statementDetails(row(), "pfs");
    assert.deepEqual(mysql, { text: SQL, avgMs: 45, examinedPerExecution: 300, examinedPerReturned: 30,
      flags: ["no_index_used", "low_selectivity"], suggestIndex: true }, "6000 rows over 20 calls is 300 per execution, not 30 per returned row");
    const maria = row({ digest: "b".repeat(32), sample_text: null, flags: ["full_scan"], avg_ms: 0, rows_examined: 0 });
    assert.deepEqual(statementDetails(maria, "pfs"), { text: maria.digest_text, avgMs: 0,
      examinedPerExecution: 0, examinedPerReturned: 0, flags: ["full_scan"], suggestIndex: true }, "MariaDB has no query sample; zero is a measured value");
    for (const source of ["table", "file"]) {
      assert.equal(statementDetails(row({ source: "mysql.slow_log" }), source).suggestIndex, false,
        "slow-log fingerprints cannot be sent to the Performance Schema suggestion endpoint");
    }
    for (const flags of [[], ["low_selectivity"], ["no_good_index_used"], ["tmp_disk_tables"]]) {
      assert.equal(statementDetails(row({ flags }), "pfs").suggestIndex, false, "only full scans request index advice");
    }
    assert.equal(statementDetails(row({ digest: null }), "pfs").suggestIndex, false);
    assert.equal(statementDetails(row({ digest: "" }), "pfs").suggestIndex, false);
    assert.equal(statementDetails(row({ sample_text: null, digest_text: null }), "pfs").text, "");
    assert.deepEqual(statementDetails(row({ flags: undefined }), "pfs").flags, []);
    for (const bad of [null, undefined, NaN, Infinity, -Infinity, -1, "12"]) {
      assert.equal(statementDetails(row({ avg_ms: bad }), "pfs").avgMs, null, `invalid average ${String(bad)}`);
      assert.equal(statementDetails(row({ rows_examined: bad }), "pfs").examinedPerExecution, null);
      assert.equal(statementDetails(row({ count: bad }), "pfs").examinedPerExecution, null);
      assert.equal(statementDetails(row({ rows_sent: bad }), "pfs").examinedPerReturned, null);
    }
    const noCalls = statementDetails(row({ count: 0, rows_sent: 0, examined_per_sent: 999 }), "pfs");
    assert.equal(noCalls.examinedPerExecution, null, "zero calls never divide into Infinity");
    assert.equal(noCalls.examinedPerReturned, null, "zero returned rows is unknown, never a cached ratio");
  },
  origin() {
    const original = row();
    const origin = suggestedIndexOrigin(original, connection);
    assert.deepEqual(origin.statement, original, "digest, sample SQL and schema travel to the index tab");
    assert.deepEqual(origin.before, { avgMs: 45, examinedPerExecution: 300 });
    assert.equal(origin.target.label, "Lab MySQL (tester@127.0.0.1:3306)");
    assert.equal(origin.target.key, '["mysql-lab","127.0.0.1",3306,"tester","PREFERRED",""]');
    assert.equal(JSON.stringify(origin).includes(connection.password), false, "origin retains no credentials");
    original.avg_ms = 900;
    original.sample_text = "SELECT 9";
    original.flags.push("new_flag");
    assert.equal(origin.before.avgMs, 45, "before values cannot follow a refreshed row");
    assert.equal(origin.statement.sample_text, SQL);
    assert.deepEqual(origin.statement.flags, ["no_index_used", "low_selectivity"]);
    const missing = suggestedIndexOrigin(row({ avg_ms: null, rows_examined: null }), connection);
    assert.deepEqual(missing.before, { avgMs: null, examinedPerExecution: null });
    assert.notEqual(suggestedIndexOrigin(row(), { ...connection, host: "db2" }).target.key, origin.target.key);
  },
  applied() {
    assert.equal(indexWasApplied(run(), DDL), true);
    assert.equal(indexWasApplied(run({ statements: [{ ...run().statements[0], sent: ` \n${DDL.slice(0, -1)}  ` }] }), DDL), true,
      "one trailing delimiter and edge whitespace do not change the executed DDL");
    assert.equal(indexWasApplied(run({ statements: [{ ...run().statements[0], sent: undefined }] }), DDL), true, "older runs retain text only");
    assert.equal(indexWasApplied(run({ statements: [{ ...run().statements[0], text: "bound text", sent: DDL }] }), DDL), true,
      "the backend's sent SQL is authoritative");
    assert.equal(indexWasApplied(null, DDL), false);
    for (const state of ["running", "error", "stopped", "session_ended"]) {
      assert.equal(indexWasApplied(run({ state }), DDL), false, `${state} is not a completed application`);
    }
    for (const status of ["pending", "running", "error", "stopped", "not_run", "session_ended"]) {
      assert.equal(indexWasApplied(run({ statements: [{ ...run().statements[0], status }] }), DDL), false, `statement ${status}`);
    }
    for (const over of [{ kind: "action" }, { error: { message: "failed" } }, { stopped: { by: "user" } },
      { statements: [] }, { statements: [...run().statements, { ...run().statements[0], index: 1 }] }]) {
      assert.equal(indexWasApplied(run(over), DDL), false, JSON.stringify(over));
    }
    for (const sent of [DDL.replace("`status`", "`customer_id`"), DDL.replace("`shop`", "`other`"),
      DDL.toLowerCase(), `${DDL} SELECT 1`, `EXPLAIN ${DDL}`]) {
      assert.equal(indexWasApplied(run({ statements: [{ ...run().statements[0], sent }] }), DDL), false,
        "successful different or multiple SQL cannot mark the suggested index applied");
    }
  },
  measurement() {
    for (const engine of ["mysql", "mariadb"]) {
      assert.deepEqual(captureMeasurement(capture({ engine })), { ms: 12.5, rows: 8 },
        `${engine}: the final statement event, not median, first run, iterator time or output rows`);
      assert.deepEqual(captureMeasurement(capture({ engine, runs: [{ total_time_ms: 123, statistics: null }] })),
        { ms: null, rows: null }, "missing event statistics must stay unknown even with iterator time");
    }
    assert.deepEqual(captureMeasurement(capture({ mode: "explain" })), { ms: null, rows: null }, "EXPLAIN estimates are not measurements");
    assert.deepEqual(captureMeasurement(capture({ runs: [] })), { ms: null, rows: null });
    assert.deepEqual(captureMeasurement(capture({ runs: [...capture().runs, { statistics: null, total_time_ms: 999 }] })),
      { ms: null, rows: null }, "never fall back to a previous run with statistics");
    assert.deepEqual(captureMeasurement(capture({ runs: [{ statistics: { timer_wait_ps: 0, rows_examined: 0 } }] })),
      { ms: 0, rows: 0 }, "zero is collected, not missing");
    assert.deepEqual(captureMeasurement(capture({ runs: [{ statistics: { rows_examined: 7 } }] })), { ms: null, rows: 7 });
    assert.deepEqual(captureMeasurement(capture({ runs: [{ statistics: { timer_wait_ps: 500000000 } }] })), { ms: 0.5, rows: null });
    for (const bad of [null, undefined, NaN, Infinity, -Infinity, -1, "0"]) {
      assert.deepEqual(captureMeasurement(capture({ runs: [{ statistics: { timer_wait_ps: bad, rows_examined: bad } }] })),
        { ms: null, rows: null }, `invalid statistic ${String(bad)}`);
    }
  },
  identity() {
    const key = captureThreadKey(connection, capture());
    assert.equal(captureThreadKey(connection, capture({ query: ` \n${SQL};\n ` })), key);
    assert.equal(captureThreadKey({ ...connection, name: "New label", password: "rotated" }, capture()), key,
      "labels and passwords do not rename the same captured statement");
    for (const changed of ["SELECT * FROM orders WHERE status = 'paid'", "SELECT  * FROM orders WHERE status = 'new'", SQL.toLowerCase()]) {
      assert.notEqual(captureThreadKey(connection, capture({ query: changed })), key, "similar digest text is not exact statement identity");
    }
    for (const changed of [{ database: "billing" }, { database: "" }, { host: "db2" }, { port: 3307 },
      { user: "other" }, { profile_id: "second" }, { ssh_host: "bastion" }]) {
      assert.notEqual(captureThreadKey({ ...connection, ...changed }, capture()), key, `different capture scope ${JSON.stringify(changed)}`);
    }
    assert.equal(key.includes(connection.password), false);
    assert.notEqual(captureThreadKey({ ...connection, database: "a:b" }, capture({ query: "c" })),
      captureThreadKey({ ...connection, database: "a" }, capture({ query: "b:c" })), "identity fields cannot collide through delimiters");
    const parameterized = "SELECT * FROM orders WHERE status = :p1";
    assert.equal(measurementMatches(`${parameterized};`, parameterized, false), true,
      "parameter filling and removal of the final delimiter preserve source attribution");
    assert.equal(measurementMatches(parameterized, "SELECT * FROM orders WHERE customer_id = :p1", false), false,
      "editing the WHERE clause cannot inherit the digest's before values");
    assert.equal(measurementMatches(undefined, parameterized, false), false, "an unrelated editor tab has no origin");
    assert.equal(measurementMatches(parameterized, parameterized, true), false,
      "session-local temporary tables and USE state cannot be safely attributed to a digest measurement");
    assert.equal(measurementMatches(parameterized, SQL, false), false,
      "match unbound source to prepared SQL; bound capture SQL belongs only in capture-history identity");
  },
  render() {
    // These are the real shared inspector and measurement components. SSR
    // exercises their props and text without browser effects or server reads.
    const render = (component, props) => renderToStaticMarkup(createElement(component, props));
    const headings = html => [...html.matchAll(/<h4\b[^>]*>([\s\S]*?)<\/h4>/g)]
      .map(match => match[1].replace(/<[^>]+>/g, ""));
    const common = ["Statement", "Latency", "Rows", "Flags", "Actions", "Source notes"];
    const props = source => ({ source, connection, disabled: false,
      actions: createElement("button", { "data-test-action": source }, "Use source statement"),
      notes: createElement("p", null, `Source note for ${source}`),
      statementExtra: createElement("p", null, `Statement extra for ${source}`),
      children: createElement("section", null, `Additional details for ${source}`),
    });
    const mysqlRow = row({ p95_ms: 100, p99_ms: 120, p999_ms: 150,
      index_hint: { status: "suggestion", ddl: DDL, reason: "Filter on status", caveats: "Indexes add write cost." } });
    const mysql = render(StatementInspector, { ...props("pfs"), row: mysqlRow });
    assert.deepEqual(headings(mysql), ["Statement", "Latency", "Rows", "Flags", "Suggested index", "Actions", "Source notes"]);
    for (const label of ["P95", "P99", "P99.9", "Examined per execution", "Examined per row returned", "Latency histogram"]) {
      assert.ok(mysql.includes(label), `MySQL inspector retains ${label}`);
    }
    assert.match(mysql, /100 ms/);
    assert.match(mysql, /120 ms/);
    assert.match(mysql, /150 ms/);
    assert.match(mysql, /300/);
    assert.match(mysql, /30/);
    assert.match(mysql, /data-statement-source="pfs"/);
    assert.ok(mysql.indexOf("Statement extra for pfs") < mysql.indexOf(">Latency"), "sample-specific guidance stays with Statement");
    assert.ok(mysql.indexOf("Additional details for pfs") > mysql.indexOf("Source note for pfs"), "source-specific sections survive after shared fields");
    const maria = render(StatementInspector, { ...props("pfs"), row: row({ digest: "b".repeat(32), sample_text: null,
      p95_ms: null, p99_ms: null, p999_ms: null, flags: [] }) });
    assert.deepEqual(headings(maria), common);
    assert.match(maria, /Normalized text/);
    assert.doesNotMatch(maria, /P95|P99|Latency histogram/, "MariaDB without quantiles must not fabricate percentile fields");
    for (const source of ["table", "file"]) {
      const html = render(StatementInspector, { ...props(source), row: mysqlRow,
        latencyDetails: createElement("p", null, `${source} histogram supplied by source`) });
      assert.deepEqual(headings(html), common, `${source} shares heading order but never offers PFS index advice`);
      for (const marker of [`data-statement-source="${source}"`, `Statement extra for ${source}`,
        `Source note for ${source}`, `Additional details for ${source}`, `${source} histogram supplied by source`]) {
        assert.ok(html.includes(marker), `${source} preserves ${marker}`);
      }
      assert.match(html, /P95/);
      assert.doesNotMatch(html, /Suggested index|CREATE INDEX/);
    }
    const after = render(MeasuredStatement, { before: { avgMs: 45, examinedPerExecution: 300 }, capture: capture() });
    assert.match(after, /data-measure-result/);
    assert.match(after, /Digest average before 45 ms, rows examined 300/);
    assert.match(after, /after 12\.5 ms, 8 \(this run; caches may be warm\)/);
    const unknown = render(MeasuredStatement, { before: { avgMs: null, examinedPerExecution: null },
      capture: capture({ runs: [{ total_time_ms: 1.5, statistics: null }] }) });
    assert.match(unknown, /Digest average before Not collected, rows examined Not collected/);
    assert.match(unknown, /after Not collected, Not collected/);
    assert.doesNotMatch(unknown, /1\.5 ms/);
    const hostile = render(StatementInspector, { ...props("file"),
      row: row({ sample_text: "SELECT '<script>alert(1)</script>'", flags: ["<img src=x onerror=alert(2)>"] }),
      notes: "<iframe src=javascript:alert(3)>", statementExtra: "<svg onload=alert(4)>" });
    assert.doesNotMatch(hostile, /<script>|<img src=x|<iframe src=javascript|<svg onload=/,
      "server-provided statement, flags and source notes must stay text");
    assert.match(hostile, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
    assert.match(hostile, /<bdi>&lt;img src=x onerror=alert\(2\)&gt;<\/bdi>/,
      "unknown server flags remain escaped and isolated from adjacent text direction");
    assert.match(hostile, /&lt;iframe src=javascript:alert\(3\)&gt;/);
    assert.match(hostile, /&lt;svg onload=alert\(4\)&gt;/);
  },
};

const selected = process.argv[2];
assert.ok(!selected || Object.hasOwn(cases, selected), `unknown test group: ${selected}`);
for (const [name, check] of Object.entries(cases)) {
  if (!selected || name === selected) {
    check();
    console.log(`statement thread ${name}: ok`);
  }
}
