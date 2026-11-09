// Server place: Top statements (a sortable table, the statement in the
// inspector) and Sessions' Show plan (the flow Running queries had), with a
// mocked live API and the real Python server for plan analysis.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { SERVER_VIEW_LABELS, WORKLOAD_VIEW_LABELS, DOCK_KEY } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-discover-"));
const shots = process.env.PERF_SHOTS || directory;
await fs.mkdir(shots, {recursive:true});
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

const longSql =
  "SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id " +
  "WHERE o.status = 'new' AND o.created_at > NOW() - INTERVAL 7 DAY ORDER BY o.total DESC LIMIT 50";
const statement = (overrides) => ({
  digest: "d".repeat(64),
  digest_text: "SELECT ?",
  schema: "shop",
  count: 1786,
  total_ms: 246814.1,
  avg_ms: 138.19,
  max_ms: 262.4,
  rows_examined: 113992796,
  rows_sent: 28576,
  examined_per_sent: 3989.11,
  no_index_used: 1786,
  no_good_index_used: 0,
  tmp_disk_tables: 0,
  sort_merge_passes: 0,
  sample_text: null,
  sample_truncated: false,
  first_seen: "2026-10-12 10:00:00",
  last_seen: "2026-10-12 11:00:00",
  flags: [],
  ...overrides,
});
const top = {
  schema_version: "top-1.0",
  engine: "mysql",
  server_version: "8.4.3",
  order: "total",
  notes: ["Counters accumulate since the server started or the digest table was last truncated."],
  statements: [
    statement({
      digest: "a".repeat(64),
      digest_text: "SELECT `o` . `id` FROM `orders` `o` WHERE `o` . `status` = ?",
      sample_text: longSql,
      flags: ["no_index_used", "low_selectivity"],
    }),
    statement({
      digest: "b".repeat(64),
      digest_text: "SELECT * FROM `customers` WHERE `id` = ?",
      rows_examined: 10,
      rows_sent: 10,
    }),
  ],
};
// A sessions-1.0 report computed from a live MySQL 8.4 recording, with two
// sessions shaped like the old Running queries rows: one that can show its
// plan and a prepared statement that cannot.
const sessionsFixture = JSON.parse(await fs.readFile(fileURLToPath(new URL("./fixtures/sessions-mysql-8.4-active.json", import.meta.url)), "utf8"));
const session = (over) => ({ ...sessionsFixture.sessions.find((x) => x.explainable), ...over });
const sessions = {
  ...sessionsFixture,
  sessions: [
    session({ id: 42, info: "SELECT * FROM t WHERE a > 1", info_short: "SELECT * FROM t WHERE a > 1", explainable: true, trx: null, lock_wait: null, flags: [] , status: "ok", why: null }),
    session({ id: 43, command: "Execute", info: "SELECT ?", info_short: "SELECT ?", explainable: false, trx: null, lock_wait: null, flags: [], status: "ok", why: null }),
  ],
};
const raw = JSON.stringify(
  {
    query: "SELECT * FROM t WHERE a > 1",
    operation: "Filter: (t.a > 1)",
    access_type: "filter",
    estimated_rows: 33.3,
    estimated_total_cost: 10.25,
    inputs: [
      { operation: "Table scan on t", access_type: "table", table_name: "t",
        estimated_rows: 100, estimated_total_cost: 10.25 },
    ],
  },
  null,
  2,
);
const warning = "Estimated plan: the optimizer's chosen plan for a running statement.";
const plan = {
  schema_version: "explain-connection-1.0",
  raw,
  mode: "explain",
  engine: "mysql",
  server_version: "8.4.3",
  connection_id: 42,
  query: "SELECT * FROM t WHERE a > 1",
  warnings: [warning],
  captured_at: "2026-10-12T11:00:00+00:00",
  capture: {
    schema_version: "capture-1.0", raw, query: "SELECT * FROM t WHERE a > 1", mode: "explain",
    engine: "mysql", server_version: "8.4.3", captured_at: "2026-10-12T11:00:00+00:00",
    conditions: "In-flight statement on connection 42 (estimated plan, EXPLAIN FOR CONNECTION).",
    warnings: [warning], runs: [], schema: {}, table_stats: {}, variables: {}, trace: null,
  },
};
const hint = { status: "suggestion", ddl: "CREATE INDEX `idx_orders_status` ON `shop`.`orders` (`status`);",
  reason: "Full scan on orders with filter on (status).",
  caveats: "Heuristic: check with Explain first; indexes add write cost. InnoDB builds can need time and disk." };
const mocks = {
  "/api/live/index-suggestion": { status: 200, body: hint },
  "/api/live/privileges": { status: 200, body: { schema_version: "privileges-1.0", account: "test@%", actions: {} } },
  "/api/live/top": { status: 200, body: top },
  "/api/live/sessions": { status: 200, body: sessions },
  "/api/live/explain-connection": { status: 200, body: plan },
};
const seen = [];
const editorRuns = [];

let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    server.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/editor/run") editorRuns.push(JSON.parse(request.postData() || "{}"));
    const mock = mocks[pathname];
    if (!mock && (pathname.startsWith("/api/live/") || pathname.startsWith("/api/connection/")))
      return request.respond({ status: 400, contentType: "application/json", body: JSON.stringify({ error: "Not mocked in this suite." }) });
    if (!mock) return request.continue();
    seen.push({ path: pathname, ...JSON.parse(request.postData() || "{}") });
    request.respond({ status: mock.status, contentType: "application/json", body: JSON.stringify(mock.body) });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  // The list of views docked, as a person who docked it: this suite asserts
  // the list's markers and keys (server-nav covers the floating default).
  await page.evaluateOnNewDocument((key) => {
    try { if (localStorage.getItem(key) === null) localStorage.setItem(key, "false"); } catch {}
  }, DOCK_KEY);
  await page.goto(url, { waitUntil: "networkidle0" });
  const button = (text, scope = "") => page.locator(`${scope}button ::-p-text(${text})`).click();
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const view = ".server-view[data-view=top]";
  const idle = () => page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  const inspectorHas = (pattern) =>
    page.waitForFunction((p) => new RegExp(p).test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);

  await button("Server", ".main-nav ");
  await page.waitForSelector(".server-place .source-list");
  assert.deepEqual(
    await page.$$eval(".source-list .source-label", (els) => els.map((e) => e.textContent)),
    SERVER_VIEW_LABELS,
  );
  // Health opens first (it needs no Performance Schema)
  assert.equal(await page.$eval("#server-nav-health", (e) => e.getAttribute("aria-current")), "page");
  await button("Workload", ".main-nav ");
  await page.waitForSelector(".server-place .source-list");
  assert.deepEqual(
    await page.$$eval(".source-list .source-label", (els) => els.map((e) => e.textContent)),
    WORKLOAD_VIEW_LABELS,
  );
  assert.equal(await page.$eval("#server-nav-top", (e) => e.getAttribute("aria-current")), "page");

  // Top statements reads when it opens on screen, once (no Load button): one
  // row per statement, flag pills with plain-English titles, notes behind a button.
  await page.waitForSelector(`${view} tbody tr`);
  assert.equal(seen.filter((s) => s.path === "/api/live/top").length, 1, "opening Top statements reads once");
  assert.equal((await page.$$(`${view} tbody tr`)).length, 2);
  assert.equal(seen.at(-1).order, "total");
  assert.equal(seen.at(-1).connection.host, "127.0.0.1");
  assert.match(await text(`${view} .summary-line`), /2 statements · 1 flagged · ranked by total time/);
  assert.equal(await text("#server-nav-top .count-badge"), "1", "the badge counts flagged statements");
  const chips = await page.$$eval(`${view} .flag-chip`, (els) => els.map((e) => [e.firstChild.textContent, e.title]));
  assert.deepEqual(chips.map((c) => c[0]), ["No index used", "Reads ≫ returns"]);
  assert.ok(chips.every((c) => c[1].length > 20));
  assert.match(await text(`${view} tbody tr[data-rank="1"]`), /1,786/);
  await page.click(`${view} .notes-button`);
  assert.match(await text(".popover"), /Counters accumulate/);
  await page.keyboard.press("Escape");
  // One line per row: long SQL is cut in the table, whole (and copyable) in the inspector.
  const cell = await page.$eval(`${view} tbody tr[data-rank="1"] .cell-sql`, (e) => [e.title, e.getBoundingClientRect().height]);
  assert.equal(cell[0], longSql);
  assert.ok(cell[1] <= 20, "the statement stays on one line");
  await page.focus(`${view} tbody tr[data-rank="1"]`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Statement 1");
  assert.equal(await text(".view-inspector .perf-sql pre"), longSql);
  assert.match(await text(".view-inspector"), /No index used/);
  assert.equal(seen.filter(request => request.path === "/api/live/index-suggestion").length, 0,
    "opening the inspector never explains a workload statement");
  await page.click('[data-action="read-index-suggestion"]');
  await inspectorHas(/CREATE INDEX `idx_orders_status`/);
  assert.match(await text(".view-inspector"), /Suggested index/);
  assert.match(await text(".view-inspector"), /Heuristic: check with Explain first/);
  const hintReads = () => seen.filter(request => request.path === "/api/live/index-suggestion");
  assert.equal(hintReads().length, 1);
  assert.equal(hintReads()[0].digest, "a".repeat(64));
  assert.equal(hintReads()[0].schema, "shop");
  assert.equal(hintReads()[0].sql, undefined);

  assert.match(await text(".view-inspector"), /Rows examined\s*113,992,796/);
  assert.equal(await page.$eval(`${view} .view-main`,e=>e.hidden),true);
  assert.equal(await page.$(".sheet-scrim"),null,"Statement details are a full view, not an overlay");
  for(const theme of ["light","dark"]){
    await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:theme}]);
    for(const width of [1440,720,390]){
      await page.setViewport({width,height:1000});
      await page.waitForFunction(()=>document.querySelector(".view-inspector.is-detail"));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>document.documentElement.clientWidth),false);
      await page.evaluate(async()=>{await Promise.all(document.getAnimations().map(a=>a.finished.catch(()=>{})));});
      await page.screenshot({path:path.join(shots,`statement-details-${theme}-${width}.png`),fullPage:true});
    }
  }
  await page.setViewport({width:1440,height:1000});
  await page.emulateMediaFeatures([{name:"prefers-color-scheme",value:"light"}]);
  // Reopening the same digest uses the completed cached result.
  await button("Back to statements", ".view-inspector ");
  await page.click(`${view} tbody tr[data-rank="1"]`);
  await inspectorHas(/CREATE INDEX `idx_orders_status`/);
  assert.equal(hintReads().length, 1);
  await button("Open in Query editor", ".view-inspector ");
  await page.waitForFunction(sql => document.querySelector('[aria-label="SQL editor"]')?.value === sql, {}, hint.ddl);
  assert.equal(editorRuns.length, 0);
  await button("Workload", ".main-nav ");
  // Back restores the selected row and keyboard movement remains in the list.
  await button("Back to statements", ".view-inspector ");
  await page.waitForFunction(()=>document.activeElement?.getAttribute("data-rank")==="1");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Statement 2");
  await page.screenshot({ path: path.join(directory, "discover-top.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(()=>!document.querySelector(".view-inspector.is-detail"));
  assert.ok(await page.$(`${view} th[data-col=examined]`), "full-width list retains its detail columns");
  assert.match(await text(`${view} th[data-col=total]`), /Total \(ms\)/);
  // The list fits beside the list of views at 1440 px, two flags included:
  // Avg is short (its full name is in the Columns menu and the tooltip), and
  // Rows sent and Schema start off (both are in the details).
  assert.equal(await text(`${view} th[data-col=avg]`), "Avg (ms)");
  assert.equal(await page.$eval(`${view} th[data-col=avg]`, (e) => e.title), "Average latency per call, in milliseconds");
  assert.equal(await page.$(`${view} th[data-col=sent]`), null, "Rows sent starts off");
  assert.equal(await page.$(`${view} th[data-col=schema]`), null, "Schema starts off");
  assert.equal(await page.$eval(`${view} .data-table-wrap`, (e) => e.scrollWidth - e.clientWidth), 0, "no sideways scroll at 1440 px");
  assert.ok(await page.$eval(`${view} th[data-col=statement]`, (e) => e.getBoundingClientRect().width) >= 260, "the statement keeps 260 px");
  await page.click(`${view} .table-columns-button`);
  assert.deepEqual(await page.$$eval('.popover .columns-list label', (els) => els.map((e) => e.textContent.trim())),
    ["#", "Statement", "Calls", "Total (ms)", "Average (ms)", "Rows examined", "Rows sent", "Schema", "Flags"]);
  await page.click('.popover input[data-col=sent]');
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${view} th[data-col=sent]`);
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover .columns-reset');
  await page.keyboard.press("Escape");
  assert.equal(await page.$(`${view} th[data-col=sent]`), null, "Reset turns Rows sent off again");
  assert.equal(await page.$(`${view} th[data-col=schema]`), null);
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover input[data-col=examined]');
  assert.equal(await page.$(`${view} th[data-col=examined]`), null, "Columns can hide a detail column");
  await page.click('.popover input[data-col=examined]');
  await page.keyboard.press("Escape");
  // A column shown through Columns remains sortable on its original raw value.
  await page.click(`${view} th[data-col=examined] button`);
  assert.equal(await page.$eval(`${view} th[data-col=examined]`, (e) => e.getAttribute("aria-sort")), "descending");
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.rank)), ["1", "2"]);
  await page.click(`${view} th[data-col=examined] button`);
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.rank)), ["2", "1"]);
  // The filter narrows the rows on screen.
  await page.type(`${view} input[aria-label="Filter statements"]`, "WHERE `id`");
  await page.waitForFunction(selector => {
    const rows = [...document.querySelectorAll(selector)];
    return rows.length === 1 && rows[0].dataset.rank === "2";
  }, { timeout: 5000 }, `${view} tbody tr`).catch(error => {
    if (error.name !== "TimeoutError") throw error;
    throw new Error('Filter statements "WHERE `id`": expected only statement 2 within 5000 ms');
  });
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.rank)), ["2"]);
  await page.click(`${view} input[aria-label="Filter statements"]`, { clickCount: 3 });
  await page.keyboard.press("Backspace");

  await page.select(`${view} .view-toolbar select`, "avg");
  await page.click(`${view} [data-action=refresh]`);
  await idle();
  assert.equal(seen.at(-1).order, "avg");

  // Both ranked views expose the same statement sections before their extras.
  await page.click(`${view} tbody tr[data-rank="1"]`);
  await page.waitForSelector('[data-statement-inspector]');
  assert.deepEqual(await page.$$eval('[data-statement-inspector] > .inspector-block > h4', nodes => nodes.slice(0, 7).map(n => n.textContent.trim())),
    ["Statement", "Latency", "Rows", "Flags", "Suggested index", "Actions", "Source notes"]);
  await inspectorHas(/Examined per execution/);
  await inspectorHas(/Examined per row returned/);
  for (const theme of ["light", "dark"]) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: 900 });
      await page.evaluate(async () => { await document.fonts.ready; await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); });
      await page.waitForFunction(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);
      assert.ok(await page.$eval('[data-statement-inspector]', element => element.getBoundingClientRect().width > 0),
        `the shared inspector is visible at ${width} px (${theme})`);
      await page.screenshot({ path: path.join(shots, `statement-inspector-${width}-${theme}.png`) });
    }
  }
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await page.setViewport({ width: 1440, height: 1000 });
  // Open in Query editor: sample text goes to a new tab in the Query editor.
  // The detail for statement 1 is still on screen from the checks above (the
  // list is hidden while it is), so the click goes to its action.
  await page.locator('[aria-label="Open in Query editor: statement 1"]').click();
  await page.waitForSelector(".main-nav button.active ::-p-text(Query editor)");
  await page.waitForFunction((sql) => document.querySelector('[aria-label="SQL editor"]').value === sql, {}, longSql);
  assert.equal(await text('.query-tablist [role="tab"][aria-selected="true"]'), "Top #1 · orders");
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "SQL editor");
  // Digest-only rows explain the ? placeholders in the inspector, and open
  // with :p1 parameters to fill in (see digest-actions.mjs for the details).
  await button("Workload", ".main-nav ");
  await button("Back to statements", ".view-inspector ");
  await page.click(`${view} tbody tr[data-rank="2"]`);
  await inspectorHas(/\? marks each literal value/);
  await page.locator('[aria-label="Open in Query editor: statement 2"]').click();
  await page.waitForFunction(() =>
    document.querySelector(".editor-panel .fill-notice")?.textContent.includes("Fill in p1 under Parameters"),
  );
  assert.equal(await page.$eval('[aria-label="SQL editor"]', (e) => e.value), "SELECT * FROM `customers` WHERE `id` = :p1");
  // The fill-in form opens first; Later keeps the Parameters JSON path.
  await page.waitForSelector("dialog.fill-sheet[open]");
  await page.locator("dialog.fill-sheet button ::-p-text(Later)").click();
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Query parameters");

  // A cut sample (top-1.0 text_limits): the inspector names the parameter,
  // the suggested value, the memory it costs, and shows copyable fix text.
  const cutSample = `${longSql.slice(0, 1021)}...`;
  const topCutSample = {
    ...top,
    text_limits: {
      sql_text: { name: "performance_schema_max_sql_text_length", value: 1024, cut_rows: 1, suggested: 4096,
                  estimate_bytes: 64740000, estimate_basis: "measured" },
      digest: null,
      persist_only: true,
    },
    statements: [
      statement({ digest: "a".repeat(64), digest_text: "SELECT `o` . `id` FROM `orders` `o` WHERE `o` . `status` = ?",
                  sample_text: cutSample, sample_truncated: true, flags: ["no_index_used", "low_selectivity"] }),
      top.statements[1],
    ],
  };
  mocks["/api/live/top"] = { status: 200, body: topCutSample };
  await button("Workload", ".main-nav ");
  if (await page.$(".view-inspector.is-detail")) await button("Back to statements", ".view-inspector ");
  await page.click(`${view} [data-action=refresh]`);
  await idle();
  await page.click(`${view} tbody tr[data-rank="1"]`);
  await inspectorHas(/Cut at 1,024 bytes: performance_schema_max_sql_text_length/);
  await inspectorHas(/Raise it to 4,096 and restart; about \+61\.7 MiB of Performance Schema memory \(estimate\)/);
  await inspectorHas(/The sample was cut off, so Explain and Run analysis use the normalized text/);
  const sampleFix = await page.$eval(".view-inspector .top-text-limit .perf-sql pre", (e) => e.textContent);
  assert.match(sampleFix, /\[mysqld\]/);
  assert.match(sampleFix, /performance_schema_max_sql_text_length = 4096/);
  assert.match(sampleFix, /SET PERSIST_ONLY performance_schema_max_sql_text_length = 4096;/);
  await button("Back to statements", ".view-inspector ");

  // A cut digest text names both digest length parameters.
  mocks["/api/live/top"] = {
    status: 200,
    body: {
      ...top,
      text_limits: {
        sql_text: null,
        digest: { names: ["max_digest_length", "performance_schema_max_digest_length"],
                  values: { max_digest_length: 1024, performance_schema_max_digest_length: 2048 },
                  cut_rows: 1, suggested: 4096, estimate_bytes: 34020000,
                  estimate_basis: "formula, plus per-connection buffers (Max_used_connections)" },
        persist_only: true,
      },
      statements: [
        statement({ digest: "a".repeat(64), digest_text: "INSERT INTO `t` SELECT * FROM `u` ON DUPLICATE ...",
                    sample_text: null, flags: ["no_index_used"] }),
        top.statements[1],
      ],
    },
  };
  await page.click(`${view} [data-action=refresh]`);
  await idle();
  await page.click(`${view} tbody tr[data-rank="1"]`);
  await inspectorHas(/Cut at 1,024 bytes: the smaller of max_digest_length and performance_schema_max_digest_length/);
  await inspectorHas(/Raise them to 4,096 and restart; about \+32\.4 MiB of Performance Schema memory \(estimate, from the sizing settings\)/);
  const digestFix = await page.$eval(".view-inspector .top-text-limit .perf-sql pre", (e) => e.textContent);
  assert.match(digestFix, /max_digest_length = 4096/);
  assert.match(digestFix, /performance_schema_max_digest_length = 4096/);
  assert.match(digestFix, /SET PERSIST_ONLY max_digest_length = 4096;/);
  await page.screenshot({ path: path.join(shots, "statement-text-limit.png"), fullPage: true });
  mocks["/api/live/top"] = { status: 200, body: top };
  await button("Back to statements", ".view-inspector ");

  // Sessions (the old Running queries): Show plan only for statements that can be explained.
  await button("Workload", ".main-nav ");
  await page.focus("#server-nav-top");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-sessions");
  assert.equal(await page.$eval("#server-nav-sessions", (e) => e.getAttribute("aria-current")), "page");
  const sv = ".server-view[data-view=sessions]";
  // The view reads once when it opens on screen, with the default filter.
  await page.waitForSelector(`${sv} tbody tr[data-session="42"]`);
  assert.equal(seen.at(-1).path, "/api/live/sessions");
  assert.equal(seen.at(-1).min_seconds, 0);
  // Changing the filter reads again only on an explicit Refresh.
  const readsBefore = seen.filter((x) => x.path === "/api/live/sessions").length;
  await page.locator(`${sv} input[aria-label="Running at least (seconds)"]`).fill("2");
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(seen.filter((x) => x.path === "/api/live/sessions").length, readsBefore);
  await page.click(`${sv} [data-action=refresh]`);
  for (let i = 0; i < 100 && seen.filter((x) => x.path === "/api/live/sessions").length === readsBefore; i++) await new Promise((r) => setTimeout(r, 50));
  await idle();
  assert.equal(seen.at(-1).path, "/api/live/sessions");
  assert.equal(seen.at(-1).min_seconds, 2);
  assert.equal(seen.at(-1).active_only, true);
  await page.click(`${sv} tbody tr[data-session="43"]`);
  await inspectorHas(/Only text-protocol statements can be explained while running/);
  assert.equal(await page.$('.view-inspector [aria-label="Show plan for connection 43"]'), null);
  await page.screenshot({ path: path.join(directory, "discover.png"), fullPage: true });

  // Narrow viewport: no horizontal page scroll.
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({ path: path.join(directory, "discover-mobile.png") });
  await page.keyboard.press("Escape");
  await page.setViewport({ width: 1440, height: 1000 });

  // Show plan opens the estimated plan in the explorer like a captured plan.
  await page.click(`${sv} tbody tr[data-session="42"]`);
  await page.locator('.view-inspector [aria-label="Show plan for connection 42"]').click();
  await page.waitForFunction(
    () => document.querySelector(".plan-heading h1")?.textContent === "Connection 42 · estimated plan",
    { timeout: 20000 },
  );
  assert.equal(seen.at(-1).connection_id, 42);
  assert.match(await text(".plan-item.selected"), /Estimated plan/);
  assert.match(await text(".sql-card"), /SELECT \* FROM t WHERE a > 1/);

  // A Performance Schema problem: one sentence and one action, not an error wall.
  mocks["/api/live/top"] = {
    status: 400,
    body: { error: "Could not complete the request: Performance Schema is off." },
  };
  await button("Workload", ".main-nav ");
  await page.locator("#server-nav-top").click();
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForSelector(`${view} .gate`);
  assert.match(await text(`${view} .gate`), /Performance Schema is off, so there are no statement digests/);
  assert.ok(await page.$(`${view} .gate [data-instruments-row=ps]`));
  // Any other error is an alert.
  mocks["/api/live/top"] = { status: 400, body: { error: "Could not complete the request: Access denied" } };
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForSelector(`${view} [role="alert"]`);
  assert.match(await text(`${view} [role="alert"]`), /Access denied/);

  // A simple unfiltered read arrives with text-only advice. Opening it must
  // not ask for an estimated plan or another server read.
  const noPredicate = "No index can help if sbc_stage.calls_disconnect_temp is a base table: this statement reads every row (no WHERE, JOIN condition, GROUP BY or ORDER BY), so no index can reduce the rows read. No rows were examined in the recorded calls; the table may have been empty. Its cost is how often it runs: 9.3 M calls.";
  mocks["/api/live/top"] = { status: 200, body: { ...top, statements: [statement({
    digest: "e".repeat(64), digest_text: "SELECT * FROM sbc_stage.calls_disconnect_temp",
    sample_text: "SELECT * FROM sbc_stage.calls_disconnect_temp", schema: "sbc_stage",
    rows_examined: 0, rows_sent: 0, count: 9326892, flags: ["no_index_used"],
    index_hint: { status: "no_predicate", reason: noPredicate },
  })] } };
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="top"] .cell-sql')?.textContent.includes("calls_disconnect_temp"));
  const beforeNoPredicate = hintReads().length;
  await page.click(`${view} tbody tr[data-rank="1"]`);
  await inspectorHas(/No index can help/);
  assert.match(await text(".view-inspector"), /9\.3 M calls/);
  assert.match(await text(".view-inspector"), /table may have been empty/);
  assert.equal(hintReads().length, beforeNoPredicate);
  await button("Back to statements", ".view-inspector ");

  // Empty state.
  mocks["/api/live/top"] = { status: 200, body: { ...top, statements: [] } };
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForFunction(() =>
    document.querySelector(".server-view .table-empty")?.textContent.includes("No statements recorded yet"),
  );

  assert.deepEqual(errors, []);
  console.log("Discover workflow passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
