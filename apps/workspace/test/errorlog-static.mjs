// Socket-free fallback for the Error log envelope and text-only render contract.
// The interaction, stale-read and layout checks belong to errorlog.mjs.
import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const here = path.dirname(fileURLToPath(import.meta.url));
const directory = await mkdtemp(path.join(here, ".errorlog-static-"));
const require = createRequire(import.meta.url);
const originalError = console.error;
const noop = () => {};
try {
  const bundle = await build({ stdin: { contents: `export { ErrorLogView, ERROR_LOG_READ, errorLogBody, parseLogTimestamp, escapeCsv, formatErrorLogLine, TIME_PRESETS } from './ErrorLog';
    export { InspectorContext } from './kit'; export { connectionKey } from './binding';
    export { serverKind, TABLE_IDS } from './session';`, resolveDir: path.join(here, "../src"), loader: "tsx" },
    bundle: true, platform: "node", format: "cjs", write: false, loader: { ".css": "empty" },
    external: ["react", "react-dom"], jsx: "automatic" });
  const output = path.join(directory, "view.cjs");
  await writeFile(output, bundle.outputFiles[0].text);
  const { ErrorLogView, InspectorContext, connectionKey, ERROR_LOG_READ, errorLogBody, serverKind, TABLE_IDS, parseLogTimestamp, escapeCsv, formatErrorLogLine, TIME_PRESETS } = require(output);
  console.error = (...args) => {
    if (String(args[0]).includes("useLayoutEffect does nothing on the server")) return;
    originalError(...args);
  };
  const connection = { host: "127.0.0.1", port: 3306, user: "root", password: "", database: "", ssl_mode: "PREFERRED", ssl_ca: "" };
  const render = (report = null, key = "", overrides = {}) => renderToStaticMarkup(React.createElement(InspectorContext.Provider, {
    value: { open: !!key, setOpen: noop, width: 340, setWidth: noop, sheet: false },
  }, React.createElement(ErrorLogView, {
    connection, disabled: false, selection: { key, select: noop }, onReport: noop,
    report: report && { key: connectionKey(connection), label: "Test server", report }, ...overrides,
  })));
  const reports = {};
  for (const name of ["mysql-8.4", "mariadb-11.4-file", "mariadb-11.4-remote", "mysql-8.4-denied"]) {
    const report = JSON.parse(await readFile(path.join(here, "fixtures", `errorlog-${name}.json`), "utf8"));
    reports[name] = report;
    assert.equal(report.schema_version, "error-log-1.0");
    for (const key of ["reason", "reason_text", "how_to_view", "source", "settings", "buffer", "file", "server_started",
      "counts", "counts_current_run", "codes", "entries", "entries_total", "limit", "levels", "summary", "notes"])
      assert.ok(key in report, `${name}: ${key}`);
    assert.equal(report.entries_total, report.entries.length);
    assert.ok(report.entries.length <= report.limit);
    for (const level of ["System", "Error", "Warning", "Note"]) {
      assert.equal(report.counts[level.toLowerCase()], report.entries.filter(e => e.prio === level).length);
      assert.equal(report.counts_current_run[level.toLowerCase()], report.entries.filter(e => e.prio === level && !e.previous_run).length);
    }
    for (let i = 1; i < report.entries.length; i++) assert.ok(report.entries[i - 1].logged >= report.entries[i].logged);
    for (let i = 1; i < report.codes.length; i++) assert.ok(report.codes[i - 1].count >= report.codes[i].count);
    assert.equal(report.codes.reduce((sum, code) => sum + code.count, 0), report.entries.length);
    if (report.available) assert.equal(report.summary.status, report.counts_current_run.error ? "watch" : "ok");
    else assert.equal(report.summary.status, "unknown");
  }
  assert.equal(serverKind("errorlog"), "errorlog");
  assert.ok(TABLE_IDS.includes("errorlog.entries") && TABLE_IDS.includes("errorlog.codes"));
  assert.equal(ERROR_LOG_READ, "errorlog:200");
  assert.deepEqual(errorLogBody(connection), { connection, limit: 200 });
  const empty = render();
  // The view reads when it opens on screen: no Load button.
  assert.doesNotMatch(empty, /Load error log/);
  assert.match(empty, /performance_schema.error_log.*MariaDB.*only when the server runs on this computer/s);
  assert.doesNotMatch(empty, /<table/);
  const mysql = reports["mysql-8.4"];
  const available = render(mysql);
  assert.match(available, /2 errors and 4 warnings/);
  assert.match(available, /MySQL 8\.4\.11/);
  assert.match(available, /aria-label="Error log entries"/);
  assert.match(available, /class="status-pill is-problem"/);
  assert.match(available, /class="status-pill is-watch"/);
  assert.equal((available.match(/data-previous-run="true"/g) || []).length, 2);
  assert.doesNotMatch(available, /<script/);
  const entryKey = entry => `entry:${JSON.stringify([entry.logged, entry.thread_id, entry.prio, entry.error_code, entry.subsystem, entry.message])}:0`;
  const malicious = render(mysql, entryKey(mysql.entries[1]));
  assert.match(malicious, /&lt;script&gt;window.errorLogInjected = true&lt;\/script&gt; &amp; record contents\./);
  assert.match(malicious, /class="inspector-sql"/);
  assert.doesNotMatch(malicious, /<script/);
  assert.match(render(mysql, entryKey(mysql.entries[0])), /end-of-message<\/pre>/);
  assert.match(render(mysql, entryKey(mysql.entries.at(-1))), /From a previous run/);
  const code = mysql.codes[0];
  const codeKey = `code:${JSON.stringify([code.error_code, code.prio, code.subsystem, null])}`;
  assert.match(render(mysql, codeKey), /Sample message.*First seen.*Last seen/s);
  const maria = render(reports["mariadb-11.4-file"]);
  assert.doesNotMatch(maria, /data-value="System"/);
  assert.match(maria, /1\.0 MiB of 2\.0 MiB/);
  const remote = render(reports["mariadb-11.4-remote"]);
  assert.match(remote, /another computer/);
  assert.doesNotMatch(remote, /class="gate\b/);
  assert.match(remote, /<code>tail -n 200 \/var\/lib\/mysql\/host\.err<\/code>/);
  assert.match(remote, />Copy<\/button>/);
  assert.match(remote, /title="MariaDB 11\.4\.8-MariaDB"[^>]*>MariaDB 11\.4\.8<\/span>/);
  assert.doesNotMatch(remote, /<table/);
  assert.match(render(reports["mysql-8.4-denied"]), /SELECT privilege/);
  assert.match(render(mysql, "", { connection: { ...connection, host: "another-server" } }), /From Test server, refresh to load this server/);

  // Time preset options and Export button in available state
  assert.match(available, /aria-label="Export error log"/);
  assert.match(available, /<span>Time<\/span>/);
  assert.match(available, /All time/);
  assert.match(available, /Last 24 hours/);
  assert.match(available, /Custom range/);
  assert.doesNotMatch(available, /All Time|Last 24 Hours|Custom Range/, "sentence case");
  // The filters and Export sit in the row above the list, not in the view toolbar.
  assert.match(available, /class="report-table-tools errorlog-tools"[^>]*aria-label="Error log filters"/);
  assert.doesNotMatch(available.slice(0, available.indexOf("errorlog-tools")), /aria-label="Export error log"/);

  // Helper unit tests
  assert.ok(!isNaN(parseLogTimestamp("2026-10-19 20:25:59.999999")));
  assert.ok(!isNaN(parseLogTimestamp("2026-10-19 20:25:59")));
  assert.ok(!isNaN(parseLogTimestamp("2026-10-19T20:25:59Z")));
  assert.ok(isNaN(parseLogTimestamp("not-a-date")));
  assert.equal(escapeCsv('simple'), 'simple');
  assert.equal(escapeCsv('has,comma'), '"has,comma"');
  assert.equal(escapeCsv('has"quote'), '"has""quote"');
  assert.equal(formatErrorLogLine(mysql.entries[0]), `${mysql.entries[0].logged} ${mysql.entries[0].thread_id} [${mysql.entries[0].prio}] [${mysql.entries[0].error_code}] [${mysql.entries[0].subsystem}] ${mysql.entries[0].message}`);
  assert.equal(TIME_PRESETS.length, 5);

  console.log("Error log static contracts passed (4 fixtures, empty/available/unavailable, inspector, escaping, binding, session ids, time filter, export).");
} finally {
  console.error = originalError;
  await rm(directory, { recursive: true, force: true });
}
