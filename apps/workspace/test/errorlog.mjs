// Error log view. Hand-authored error-log-1.0 fixtures cover the UI contract
// (long and hostile messages, unavailable states); errorlog-live-*.json are
// computed by myflames/error_log.py from live MySQL 8.4 and MariaDB 11.4
// recordings (server-fixtures.py).
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const mysql = await load("errorlog-mysql-8.4.json");
const maria = await load("errorlog-mariadb-11.4-file.json");
const remote = await load("errorlog-mariadb-11.4-remote.json");
const denied = await load("errorlog-mysql-8.4-denied.json");
// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/error-log", "errorlog", {}, { list: "docked" });
const { page, state, text } = h;
state.fallback = { status: 200, body: mysql };
const view = '.server-view[data-view="errorlog"]';
const table = `${view} table[aria-label="Error log entries"]`;
const rows = `${table} tbody tr[data-row-key]`;
const codes = `${view} table[aria-label="Error log by code"]`;
const level = value => page.click(`${view} [aria-label="Log level"] button[data-value="${value}"]`);
const group = value => page.click(`${view} [aria-label="Error log grouping"] button[data-value="${value}"]`);
const rowCount = () => page.$$eval(rows, els => els.length);
const filter = async value => {
  const selector = `${view} input[aria-label="Filter error log"]`;
  await page.click(selector, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  if (value) await page.type(selector, value);
};
const refresh = async report => {
  state.queue.push({ status: 200, body: report });
  const before = state.seen.length;
  await h.refresh();
  await page.waitForFunction((selector, summary) => document.querySelector(selector)?.textContent.includes(summary), {}, `${view} .summary-line`, report.summary.text);
  await h.idle(view);
  assert.equal(state.seen.length, before + 1);
};
try {
  await h.server();
  await h.view("errorlog");
  assert.equal(await page.$eval('#server-nav-errorlog', e => e.closest("li")?.previousElementSibling?.querySelector("button")?.dataset.view), "innodb");
  // Opening the view on screen reads it at once (once per connection), like
  // every other view: no Load button.
  await page.waitForSelector(rows); await h.idle(view);
  assert.equal(state.seen.length, 1, "opening Error log reads once");
  assert.equal(await page.$(`${view} .view-empty button`), null, "no Load button");
  // Once per connection: another view and back reads nothing more.
  await h.view("innodb");
  await h.view("errorlog");
  await pause(300);
  assert.equal(state.seen.length, 1, "coming back reads nothing again");
  assert.deepEqual(Object.keys(state.seen[0]).sort(), ["connection", "limit"]);
  assert.equal(state.seen[0].connection.host, "127.0.0.1");
  assert.equal(state.seen[0].limit, 200);
  assert.match(await text(`${view} .summary-line`), /2 errors and 4 warnings/);
  assert.match(await text(`${view} .fact-strip`), /MySQL 8\.4\.11.*performance_schema.error_log.*2026-10-19 20:26:06.*12 events/s);
  assert.equal(await rowCount(), 12);
  assert.match(await text("#server-nav-errorlog"), /2 errors this run, Watch/);
  assert.ok(await page.$("#server-nav-errorlog .count-badge.is-neutral") && await page.$("#server-nav-errorlog .status-dot.is-watch"), "a watch dot beside a neutral count");
  assert.match(await h.notes(), /expired 120 older events/);
  // One toolbar row at 1440 px: the filters and Export are in the row above the list.
  assert.ok(await page.$eval(`${view} .view-toolbar`, (e) => e.getBoundingClientRect().height) <= 48, "the toolbar keeps one row");
  for (const sel of ['[aria-label="Error log grouping"]', '[aria-label="Log level"]', 'input[aria-label="Filter error log"]', 'button[aria-label="Export error log"]'])
    assert.ok(await page.$(`${view} .errorlog-tools ${sel}`) && !(await page.$(`${view} .view-toolbar ${sel}`)), `${sel} is in the filter row`);
  const toolsRow = await page.$eval(`${view} .errorlog-tools`, (e) => [e.getBoundingClientRect().height, e.getBoundingClientRect().width, [...e.children].map((c) => Math.round(c.getBoundingClientRect().top)).join(",")]);
  assert.ok(toolsRow[0] <= 48, `the filter row is one row (${toolsRow.join(" | ")})`);

  await level("Error"); assert.equal(await rowCount(), 3);
  assert.equal(await page.$$eval(`${rows} .status-pill.is-problem`, els => els.length), 3);
  await level("Warning"); assert.equal(await rowCount(), 4);
  await level("Note"); assert.equal(await rowCount(), 1);
  await level("System"); assert.equal(await rowCount(), 4);
  await level("all");
  await filter("MY-013183"); assert.equal(await rowCount(), 2);
  await filter("innodb"); assert.equal(await rowCount(), 4);
  await filter("end-of-message"); assert.equal(await rowCount(), 1);
  await h.select(rows);
  assert.equal(await text(".view-inspector pre"), mysql.entries[0].message, "the long message is not cut in the inspector");
  assert.match(await h.inspector(), /Thread id47.*MY-010914.*Server/s);
  await page.keyboard.press("Escape");
  await filter("window.errorLogInjected");
  await h.select(rows);
  assert.equal(await text(".view-inspector pre"), mysql.entries[1].message);
  assert.equal(await page.$(".view-inspector pre script"), null, "messages are text, never HTML");
  assert.equal(await page.evaluate(() => window.errorLogInjected), undefined);
  await page.keyboard.press("Escape");
  await filter("");
  await h.select(`${rows}[data-previous-run="true"]`);
  assert.match(await h.inspector(), /From a previous run/);
  await page.keyboard.press("Escape");
  await page.click(`${view} .checkbox input`);
  assert.equal(await rowCount(), 10);
  assert.equal(await page.$(`${rows}[data-previous-run="true"]`), null);
  await group("codes");
  assert.equal(await page.$$eval(`${codes} tbody tr[data-row-key]`, els => els.length), mysql.codes.length);
  assert.equal(await page.$eval(`${view} .checkbox input`, e => e.disabled), true);
  assert.match(await text(`${view} .grouped-footer`), /including previous runs/);
  await h.select(`${codes} tbody tr[data-row-key]`);
  assert.match(await h.inspector(), /Sample message.*First seen.*Last seen/s);
  await page.keyboard.press("Escape");
  assert.equal(state.seen.length, 1, "all filters, grouping and inspection are local");
  await group("entries");
  await page.click(`${view} .checkbox input`);

  // Time filtering
  const timeSelect = `${view} .errorlog-time-select select`;
  await page.select(timeSelect, "custom");
  await page.waitForSelector(`${view} input[aria-label="Start time"]`);
  const setInput = async (selector, val) => {
    await page.$eval(selector, (el, v) => {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
      setter.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
      el.dispatchEvent(new Event("change", { bubbles: true }));
    }, val);
  };
  await setInput(`${view} input[aria-label="Start time"]`, "2026-10-19T20:26");
  await setInput(`${view} input[aria-label="End time"]`, "2026-10-19T20:26");
  assert.equal(await rowCount(), 5, "5 entries in minute 20:26");
  await page.select(timeSelect, "all");
  assert.equal(await rowCount(), 12, "all entries shown when All time selected");

  // Export options: CSV, Log, JSON
  await page.evaluate(() => {
    window.errorLogExports = [];
    const origCreateObjectURL = URL.createObjectURL;
    URL.createObjectURL = function(blob) {
      const url = origCreateObjectURL.apply(this, arguments);
      blob.text().then(text => window.errorLogExports.push({ text, type: blob.type }));
      return url;
    };
  });
  await page.click(`${view} .errorlog-export button`);
  await page.waitForSelector(".errorlog-export-formats button");
  // CSV
  await page.click('.errorlog-export-formats button:nth-child(1)');
  await page.waitForFunction(() => window.errorLogExports.length >= 1);
  const csv = await page.evaluate(() => window.errorLogExports[0].text);
  assert.match(csv, /^Logged,Level,Code,Subsystem,Message/);
  assert.match(csv, /MY-010914/);
  // Log
  await page.click('.errorlog-export-formats button:nth-child(2)');
  await page.waitForFunction(() => window.errorLogExports.length >= 2);
  const log = await page.evaluate(() => window.errorLogExports[1].text);
  assert.match(log, /\[Warning\]/);
  // JSON
  await page.click('.errorlog-export-formats button:nth-child(3)');
  await page.waitForFunction(() => window.errorLogExports.length >= 3);
  const json = await page.evaluate(() => JSON.parse(window.errorLogExports[2].text));
  assert.equal(json.schema_version, "error-log-1.0");
  assert.equal(json.total_entries, 12);
  await page.keyboard.press("Escape");

  // Real table geometry and long messages at both themes and widths.
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => document.documentElement.dataset.theme = value, theme);
    await page.screenshot({ path: path.join(h.shots, `errorlog-${theme}-1440.png`), fullPage: true });
    await h.narrow(`errorlog-${theme}-390.png`);
    await h.select(rows);
    await h.narrow(`errorlog-message-${theme}-390.png`);
    await page.keyboard.press("Escape");
  }

  // A System filter from MySQL cannot hide every MariaDB entry.
  await level("System");
  await refresh(maria);
  assert.equal(await page.$(`${view} [aria-label="Log level"] [data-value="System"]`), null);
  assert.ok(await page.$(`${view} [aria-label="Log level"] [data-value="all"][aria-pressed="true"]`));
  assert.equal(await rowCount(), maria.entries.length);
  assert.match(await text(`${view} .fact-strip`), /MariaDB 11\.4.*file.*1\.0 MiB of 2\.0 MiB/s);
  await h.select(`${rows}[data-level="Note"]`);
  assert.match(await h.inspector(), /Version: '11\.4\.8-MariaDB'/);
  await page.keyboard.press("Escape");
  await group("codes");
  assert.equal(await page.$$eval(`${codes} tbody tr[data-row-key]`, els => els.length), maria.codes.length);
  await group("entries");

  await refresh(remote);
  assert.match(await text(`${view} .gate`), /another computer/);
  assert.match(await text(`${view} [aria-label="Error log settings"]`), /log_error.*\.\/host.err.*datadir.*\/var\/lib\/mysql/s);
  assert.equal(await text(`${view} pre`), remote.how_to_view);
  assert.equal(await page.$(table), null);
  assert.match(await text("#server-nav-errorlog"), /not available/);
  // Keep clipboard assertions inside the page; never alter the user's clipboard.
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true,
    value: { writeText: async value => { window.errorLogCopied = value; } } }));
  await h.button("Copy instructions", `${view} `);
  await page.waitForFunction(() => !!window.errorLogCopied);
  assert.equal(await page.evaluate(() => window.errorLogCopied), remote.how_to_view);
  await h.narrow("errorlog-remote-390.png");
  await refresh(denied);
  assert.match(await text(`${view} .gate`), /SELECT privilege/);
  assert.equal(await page.$(`${view} pre`), null);
  assert.ok(await page.$("#server-nav-errorlog .status-dot.is-unknown"));

  const quiet = structuredClone(mysql);
  quiet.counts_current_run.error = 0;
  quiet.entries.forEach(e => { if (e.prio === "Error") e.previous_run = true; });
  quiet.summary = { status: "ok", text: "No errors recorded since server start." };
  await refresh(quiet);
  assert.ok(await page.$("#server-nav-errorlog .count-badge.is-neutral"), "OK draws no colored count");
  assert.match(await text("#server-nav-errorlog"), /0 errors this run, OK/);
  // A failed Refresh keeps the accepted report and displays the failure.
  state.queue.push({ status: 400, body: { error: "Could not read the error log: lost connection" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role="alert"]`);
  assert.match(await text(`${view} .summary-line`), /No errors recorded/);
  assert.equal(await rowCount(), quiet.entries.length);

  // Reports computed from live recordings render with their own numbers.
  const liveMysql = await load("errorlog-live-mysql-8.4.json");
  const liveMaria = await load("errorlog-live-mariadb-11.4.json");
  for (const live of [liveMysql, liveMaria]) {
    await refresh(live);
    const errors = live.entries.filter(e => e.prio === "Error");
    await level("Error");
    assert.equal(await rowCount(), errors.length, `${live.engine}: Error rows`);
    assert.ok(errors.length > 0);
    await level("all");
    const n = live.counts_current_run.error;
    assert.match(await text("#server-nav-errorlog"), new RegExp(`${n} ${n === 1 ? "error" : "errors"} this run`));
    await h.select(`${rows}[data-level="Error"]`);
    assert.equal(await text(".view-inspector pre"), live.entries.find(e => e.prio === "Error").message);
    await page.keyboard.press("Escape");
  }
  assert.match(await text(`${view} .fact-strip`), /MariaDB 11\.4\.13.*file/s);

  assert.deepEqual(h.errors, []);
  console.log("errorlog view: ok");
} finally {
  await h.close();
}
