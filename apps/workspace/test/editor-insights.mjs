// Build first: npm run build && node test/editor-insights.mjs.
// The real isolated workspace serves assets and saves UI state. All database
// and editor requests are intercepted; no fixture can contact a server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { setEditorText } from "./editor-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-insights-"));
const shots = process.env.EDITOR_SHOTS || directory;
await fs.mkdir(shots, { recursive: true });
const fixtures = await Promise.all(["mysql", "mariadb"].map(async engine => JSON.parse(await fs.readFile(
  path.join(root, "test/fixtures/editor", `insights-${engine}.json`), "utf8"))));
// Show who waits mounts the Server place, which reads Locks (and its first
// view, Health) on open: answer with real reports, never a placeholder.
const report = async name => JSON.parse(await fs.readFile(path.join(root, "apps/workspace/test/fixtures", name), "utf8"));
const [locksReport, healthReport] = await Promise.all([report("locks-mysql-8.4.json"), report("health-mysql-8.4.json")]);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const server = spawn(process.env.PYTHON || "python3", ["-c",
  "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
  path.join(directory, "workspace.sqlite3")], {
  cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" },
});
let browser, page, runN = 0;
let fixture = fixtures[0], nextStats = null, meter = null, tx = false, holdMeter = false, expectMeterAbort = false;
// A message makes the next run's statement fail with it.
let failNext = null;
// Where the person is: the Server place cancels its own reads when it switches views or goes off screen.
let phase = "editor";
const seen = [], held = [], diagnostics = [], responses = [], failedRequests = [];
const calls = endpoint => seen.filter(item => item.path === `/api/${endpoint}`);
const session = tabId => ({
  schema_version: "editor-session-1.0", tab_id: tabId, state: "idle",
  connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
  engine: fixture.engine, server_version: fixture.server_version, connection_id: 42,
  autocommit: true, in_transaction: tx, transaction_source: "performance_schema", transaction_note: null,
  transaction_meter: meter, sql_mode: "", backslash_escapes: true, safe_updates: true,
  safe_updates_default: true, read_only_transactions: false, started_at: "2026-11-08T12:00:00Z",
  idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null, message: null, notes: [], run: null,
});
function result(body) {
  const sql = body.sql || "SELECT 1";
  const statements = [{ index: 0, text: sql, start: 0, end: sql.length, verb: "select", kind: "read", category: "read",
    status: failNext ? "error" : "ok", duration_ms: 5, server_stats: nextStats,
    ...(body.explain_format ? { sent: `EXPLAIN ${sql}` } : {}) }];
  const failure = failNext ? { type: "error", statement: 0, code: 1064, sqlstate: "42000", message: failNext,
    interrupted: false, duration_ms: 5, position: null } : null;
  return { status: "complete", session: session(body.tab_id), run: {
    schema_version: "editor-run-1.0", id: `insight-${++runN}`, kind: "run", state: failure ? "error" : "complete", scope: "statement",
    started_at: "2026-11-08T12:00:00Z", finished_at: "2026-11-08T12:00:01Z", duration_ms: 5, limit: 1000, statements,
    results: failure ? [failure] : [{ type: "rows", statement: 0, columns: [{ name: "one", hex: false, type: null }], columns_known: true,
      rows: [[String(runN)], ["a"], ["b"], ["c"]], row_count: 4, truncated: false, limit: 1000, stopped: null, label: null, server_rows: 4, warnings: 0, duration_ms: 5 }],
    error: failure, stopped: null, message: null,
    counts: { statements: 1, run: 1, rows: failure ? 0 : 4, affected_rows: 0, warnings: 0, errors: failure ? 1 : 0 },
    audit_id: null, history_id: null,
  } };
}
const available = row => { const { sql_text, nesting_event_type, nesting_event_level, ...stats } = row; return { available: true, reason: null, ...stats }; };
async function wait(check, message, timeout = 8000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { if (await check()) return; await pause(25); }
  const state = page ? await page.evaluate(() => ({ text: document.querySelector(".query-lab")?.textContent,
    active: document.activeElement?.outerHTML })) : {};
  if (page) await page.screenshot({ path: path.join(shots, "editor-insights-failure.png") });
  assert.fail(`${message}\n${JSON.stringify({ state, diagnostics, seen, responses })}`);
}
async function run(sql = "SELECT * FROM t") {
  const count = calls("editor/run").length;
  await setEditorText(page, sql);
  await page.click("[data-action=run]");
  await wait(() => calls("editor/run").length === count + 1, "one Run request");
  await page.waitForFunction(value => !document.querySelector(".results-running") &&
    [...document.querySelectorAll(".results-pane .cell-value")].some(element => element.textContent === value), {}, String(runN));
}
async function closePopover() { await page.keyboard.press("Escape"); }
try {
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(Error(output || "Workspace did not start")), 10000);
    server.stderr.on("data", chunk => { output += chunk; const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); } });
    server.once("error", error => { clearTimeout(timer); reject(error); });
    server.once("exit", code => { clearTimeout(timer); reject(Error(`Workspace exited ${code}: ${output}`)); });
  });
  // --no-sandbox: Chrome cannot start without it in some CI containers.
  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [] });
  page = await browser.newPage();
  page.on("pageerror", error => diagnostics.push(String(error)));
  page.on("console", message => { if (["error", "warn"].includes(message.type())) diagnostics.push(message.text()); });
  page.on("requestfailed", request => failedRequests.push({ path: new URL(request.url()).pathname,
    body: JSON.parse(request.postData() || "{}"), error: request.failure()?.errorText, expected: expectMeterAbort, phase }));
  page.on("response", response => { if (response.url().includes("/api/")) responses.push({ path: new URL(response.url()).pathname, status: response.status() }); });
  await page.setRequestInterception(true);
  page.on("request", request => {
    const endpoint = new URL(request.url()).pathname;
    if (!endpoint.startsWith("/api/editor/") && !endpoint.startsWith("/api/live/") && !endpoint.startsWith("/api/connection/") && endpoint !== "/api/analysis/prepare-query") return request.continue();
    const body = JSON.parse(request.postData() || "{}");
    seen.push({ path: endpoint, body });
    const answer = value => request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(value) });
    if (endpoint === "/api/editor/status") {
      if (body.refresh_insights && holdMeter) { held.push({ answer, body }); return; }
      return answer(body.refresh_insights ? { status: "idle", session: session(body.tab_id), run: null } : { status: "none", session: null, run: null });
    }
    if (endpoint === "/api/editor/run") return answer(result(body));
    if (endpoint === "/api/editor/preview") return answer({ status: "ready", statements: [{ index: 0, kind: "read", action_keys: [], state: "unknown", missing: [], reason: "Not verified" }], aggregate: { state: "unknown", missing: [], reason: "Not verified", warning: "" }, policy: { action: "allow", text: "" } });
    if (endpoint === "/api/editor/history" || endpoint === "/api/editor/saved/list") return answer({ items: [] });
    if (endpoint === "/api/editor/close") return answer({ closed: true, session: null });
    if (endpoint === "/api/editor/rollback" || endpoint === "/api/editor/commit") {
      tx = false; meter = null;
      const reply = result({ ...body, sql: endpoint.endsWith("commit") ? "COMMIT" : "ROLLBACK" });
      reply.run.kind = "action"; return answer(reply);
    }
    if (endpoint === "/api/live/privileges") return answer({ schema_version: "privileges-1.0", account: "app@%", grants: [], current_roles: [], actions: {} });
    if (endpoint === "/api/live/schemas") return answer({ schemas: [], user_tables: 0, all_limit: 2000, system: [] });
    if (endpoint === "/api/live/catalog") return answer({ schemas: [], tables: [], columns: [], indexes: [] });
    if (endpoint === "/api/analysis/prepare-query") return answer({ sql: body.query, converted: false, warning: "", problem: "" });
    if (endpoint === "/api/live/start") return answer({ id: "analysis-insight" });
    if (endpoint === "/api/live/status") return answer({ id: "analysis-insight", state: "cancelled", progress: 0, repeat: 1, error: "", result: null });
    if (endpoint === "/api/live/locks") return answer(locksReport);
    if (endpoint === "/api/live/health") return answer(healthReport);
    diagnostics.push(`Unexpected database request: ${endpoint}`);
    return answer({});
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector('[aria-label="SQL editor"]');
  // Both engines, exact rule boundaries, missing history and safe server text.
  for (fixture of fixtures) {
    nextStats = available(fixture.history[0]);
    await run();
    assert.match(await page.$eval(".editor-insight", element => element.textContent), /Examined .*50 returned/);
    assert.equal(await page.$eval(".editor-insight .status-pill", element => element.textContent), "Watch");
    await page.click('[aria-label="Statement stats and verdict"]');
    assert.match(await page.$eval('[role=dialog][aria-label="Statement stats and verdict"]', element => element.textContent), /more than 10,000 examined/);
    await closePopover();
  }
  nextStats = { ...available(fixtures[0].history[0]), rows_examined: "10000", rows_sent: "1", select_scan: "1" };
  await run();
  assert.equal(await page.$eval(".editor-insight .status-pill", element => element.textContent), "OK", "10,000 is not over 10,000; scan alone is not Watch");
  nextStats = { ...nextStats, rows_examined: "10100", rows_sent: "101" };
  await run();
  assert.equal(await page.$eval(".editor-insight .status-pill", element => element.textContent), "OK", "100 per returned row is not over 100");
  nextStats = { ...nextStats, rows_sent: "0" };
  await run();
  assert.doesNotMatch(await page.$eval(".editor-insight", element => element.textContent), /Infinity|NaN|per row/);
  nextStats = { ...nextStats, errors: "1" };
  await run();
  assert.equal(await page.$eval(".editor-insight .status-pill", element => element.textContent), "Problem");
  // Explain analyze is offered only for statements it can read
  // (server_stats.analyzable); the reason is the stats line's tooltip.
  const analyzeButton = () => page.$$eval(".editor-insight button", buttons => buttons.some(b => b.textContent === "Explain analyze"));
  const setReason = "Explain analyze reads SELECT, WITH and simple UPDATE, DELETE or INSERT … SELECT statements; SET is none of these.";
  nextStats = { ...available(fixtures[0].history[0]), analyzable: false, analyze_reason: setReason };
  await run("SET @a = 1");
  assert.equal(await analyzeButton(), false, "no Explain analyze for SET");
  assert.equal(await page.$eval(".editor-insight-text", element => element.title), setReason);
  failNext = "You have an error in your SQL syntax";
  nextStats = { ...available(fixtures[0].history[0]), errors: "1", analyzable: true, analyze_reason: "" };
  {
    // A failed run has no rows: wait for its stats line instead of a cell.
    const count = calls("editor/run").length;
    await setEditorText(page, "SELECT * FROM t WHERE");
    await page.click("[data-action=run]");
    await wait(() => calls("editor/run").length === count + 1, "one Run request");
    await page.waitForFunction(() => !document.querySelector(".results-running") &&
      /^Statement failed/.test(document.querySelector(".editor-insight-text")?.textContent || ""));
  }
  failNext = null;
  assert.match(await page.$eval(".editor-insight-text", element => element.textContent), /^Statement failed · Examined/);
  assert.equal(await analyzeButton(), true, "a failed SELECT can still be analyzed");
  nextStats = available(fixtures[0].history[0]);
  await run();
  assert.equal(await analyzeButton(), true, "an older backend without the field keeps the action");
  nextStats = { available: false, reason: "Performance Schema is off; check Server > Instruments. <img src=x onerror=alert(1)>" };
  await run();
  assert.equal(await page.$eval(".editor-insight", element => element.textContent), "Server stats not collected");
  assert.equal(await page.$(".editor-insight .status-pill"), null);
  await page.click('[aria-label="Why server stats were not collected"]');
  assert.equal(await page.$('[role=dialog][aria-label="Why server stats were not collected"] img'), null);
  assert.match(await page.$eval('[role=dialog][aria-label="Why server stats were not collected"] bdi', element => element.textContent), /<img/);
  await closePopover();
  // Run analysis uses the selected statement and existing review, stays here.
  nextStats = available(fixtures[0].history[0]);
  await run("SELECT * FROM t WHERE id = 42");
  await page.locator(".editor-insight button ::-p-text(Explain analyze)").click();
  await page.waitForSelector(".confirm-sheet", { visible: true });
  assert.equal(await page.$eval(".confirm-sheet .confirm-sql", element => element.textContent), "SELECT * FROM t WHERE id = 42");
  assert.equal(calls("live/start").length, 0, "no executing analysis before its review");
  await page.locator(".confirm-sheet .sheet-buttons button.primary").click();
  await wait(() => calls("live/start").length === 1, "analysis starts exactly once after review");
  assert.equal(calls("live/start")[0].body.query, "SELECT * FROM t WHERE id = 42");
  assert.equal(calls("live/start")[0].body.mode, "analyze");
  await page.waitForFunction(() => !document.querySelector("[data-action=analyze]")?.getAttribute("aria-busy"));
  assert.ok(await page.$('[aria-label="SQL editor"]'), "analysis stays in Query editor");
  // A native Explain result keeps the original SQL for its analysis action,
  // rather than trying to analyze an already-prefixed EXPLAIN statement.
  const beforeExplain = calls("editor/run").length;
  await page.click("[data-action=explain]");
  await wait(() => calls("editor/run").length === beforeExplain + 1, "native Explain request");
  await page.waitForFunction(value => [...document.querySelectorAll(".results-pane .cell-value")]
    .some(element => element.textContent === value), {}, String(runN));
  await page.locator(".editor-insight button ::-p-text(Explain analyze)").click();
  await page.waitForSelector(".confirm-sheet", { visible: true });
  assert.equal(await page.$eval(".confirm-sheet .confirm-sql", element => element.textContent), "SELECT * FROM t WHERE id = 42");
  await closePopover();
  // An open transaction is observed on Run, then automatically sampled every 10s.
  tx = true;
  meter = { age_seconds: 240, age_source: "innodb", rows_modified: "1240", rows_locked: "1243", lock_structs: "5", blocking: 3, blocking_truncated: false, note: null };
  await run();
  assert.match(await page.$eval(".editor-transaction-trigger", element => element.textContent), /Transaction open.*4 min.*1,240 rows changed.*blocking 3/);
  assert.ok(await page.$(".editor-transaction-trigger .status-pill.is-watch"));
  // A long transaction meter never pushes the run actions out of the toolbar:
  // the pill shrinks with an ellipsis (the full text is its tooltip and the
  // popover it opens), and the first group does not scroll away.
  const toolbarFit = () => page.evaluate(() => {
    const bar = document.querySelector(".editor-toolbar");
    const box = bar.getBoundingClientRect();
    const inside = selector => {
      const r = document.querySelector(selector)?.getBoundingClientRect();
      return !!r && r.width > 0 && r.left >= box.left - 1 && r.right <= box.right + 1;
    };
    const trigger = document.querySelector(".editor-transaction-trigger");
    const pill = trigger?.querySelector(".status-pill");
    const style = pill ? getComputedStyle(pill) : {};
    return {
      run: inside("[data-action=run]"), runAll: inside("[data-action=run-all]"), stop: inside("[data-action=stop]"),
      clipped: !!pill && pill.scrollWidth > pill.clientWidth,
      nowrap: style.whiteSpace === "nowrap" && style.textOverflow === "ellipsis",
      barOverflows: bar.scrollWidth > bar.clientWidth,
      title: trigger.title,
    };
  });
  const assertRunActions = (fit, where) => {
    assert.ok(fit.run && fit.runAll && fit.stop, `Run, Run all and Stop fully visible ${where}: ${JSON.stringify(fit)}`);
    assert.ok(fit.nowrap, `the meter pill is one capped line ${where}`);
    assert.equal(fit.title, "Transaction open · 4 min 0 s · 1,240 rows changed · blocking 3", `full meter text in the tooltip ${where}`);
    if (fit.barOverflows) assert.ok(fit.clipped, `a toolbar that still overflows caps the pill with an ellipsis ${where}`);
  };
  await page.setViewport({ width: 1440, height: 1000 });
  // The Schemas pane is open by default at this width.
  await page.waitForSelector(".schema-browser", { visible: true });
  assertRunActions(await toolbarFit(), "at 1440 px with Schemas open");
  await page.click('.editor-toolbar button[aria-label="Hide the schema browser"]');
  await page.waitForFunction(() => !document.querySelector(".schema-browser")?.offsetParent);
  assertRunActions(await toolbarFit(), "at 1440 px with Schemas closed");
  // At the 720x520 minimum the insight line and a wrapped result toolbar come
  // out of the SQL pane's room: the results keep at least two data rows.
  await page.setViewport({ width: 720, height: 520 });
  await page.waitForFunction(() => {
    const wrap = document.querySelector(".result-grid .data-table-wrap");
    if (!wrap) return false;
    const box = wrap.getBoundingClientRect();
    return [...document.querySelectorAll(".result-table tbody tr[data-row-key]")]
      .filter(tr => { const r = tr.getBoundingClientRect(); return r.top >= box.top - 1 && r.bottom <= box.bottom + 1; }).length >= 2;
  });
  assertRunActions(await toolbarFit(), "at 720x520");
  // Schemas opens as a sheet over the narrow window; the toolbar keeps its run actions behind it.
  await page.click('.editor-toolbar button[aria-label="Show the schema browser"]');
  await page.waitForSelector(".editor-workbench.is-narrow .schema-browser", { visible: true });
  assertRunActions(await toolbarFit(), "at 720x520 with the Schemas sheet open");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".schema-browser")?.offsetParent);
  await page.setViewport({ width: 1440, height: 1000 });
  await page.click(".editor-transaction-trigger");
  await page.waitForSelector('[data-action="rollback"]', { visible: true });
  assert.ok(await page.$('[data-action="commit"]'));
  phase = "server";
  await page.locator(".editor-transaction-actions button ::-p-text(Show who waits)").click();
  await page.waitForSelector('.server-view[data-view="locks"]', { visible: true });
  // Locks reads the connection the editor's transaction runs on; nothing is written.
  await wait(() => calls("live/locks").length > 0, "Locks reads on open");
  const editorConnection = calls("editor/run").at(-1).body.connection;
  for (const key of ["host", "port", "user", "profile_id"]) {
    assert.equal(calls("live/locks").at(-1).body.connection?.[key], editorConnection[key], `Locks reads the editor's ${key}`);
  }
  assert.equal(calls("editor/commit").length + calls("editor/rollback").length, 0, "Show who waits sends no transaction write");
  const beforeHidden = calls("editor/status").filter(item => item.body.refresh_insights).length;
  await pause(10500);
  assert.equal(calls("editor/status").filter(item => item.body.refresh_insights).length, beforeHidden, "hidden editor sends no meter polls");
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  const beforePoll = calls("editor/status").filter(item => item.body.refresh_insights).length;
  meter = { ...meter, age_seconds: 251, rows_modified: null, rows_locked: null, lock_structs: null, blocking: null,
    age_source: "observed", note: "PROCESS is required for InnoDB details; age is measured since first observed." };
  await wait(() => calls("editor/status").filter(item => item.body.refresh_insights).length === beforePoll + 1, "10-second meter poll", 12000);
  phase = "editor";
  await page.waitForFunction(() => !document.querySelector(".editor-transaction-trigger")?.textContent.includes("rows changed"));
  await page.click(".editor-transaction-trigger");
  assert.match(await page.$eval('.editor-transaction-popover [role=dialog]', element => element.textContent), /PROCESS/);
  await closePopover();
  // Both themes at desktop/minimum width; readable summary, no page overflow.
  for (const scheme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
    await page.evaluate(theme => { localStorage.setItem("myflames.theme", theme); document.documentElement.dataset.theme = theme; }, scheme);
    await page.setViewport({ width, height: width === 720 ? 720 : 1000 });
    await page.evaluate(() => document.fonts.ready);
    // A theme switch animates colors: shoot the settled frame, not a mid-transition one.
    await page.waitForFunction(() => document.getAnimations().every(animation =>
      !(animation instanceof CSSTransition) || animation.playState !== "running"));
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    await page.screenshot({ path: path.join(shots, `editor-insights-${width}-${scheme}.png`) });
  }
  // A delayed meter reply cannot overwrite a newer Run's closed transaction.
  holdMeter = true;
  await wait(() => held.length > 0, "held meter request", 12000);
  const late = session("1");
  expectMeterAbort = true;
  tx = false; meter = null;
  await run();
  holdMeter = false;
  for (const request of held) await request.answer({ status: "idle", session: late, run: null }).catch(() => {});
  await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Auto-commit");
  assert.equal(await page.$(".editor-transaction-trigger"), null);
  assert.equal(calls("editor/commit").length, 0);
  assert.equal(calls("editor/rollback").length, 0);
  assert.ok(responses.every(response => response.status === 200), JSON.stringify(responses));
  // The Server place opens on its first view (Health) and cancels that read
  // itself when Show who waits switches it to Locks or the editor returns.
  const serverOwn = request => request.phase === "server" && request.path === "/api/live/health" && request.error === "net::ERR_ABORTED";
  const editorFailed = failedRequests.filter(request => !serverOwn(request));
  assert.ok(editorFailed.every(request => request.expected && request.path === "/api/editor/status" &&
    request.body.refresh_insights === true && request.error === "net::ERR_ABORTED"), JSON.stringify(failedRequests));
  assert.ok(editorFailed.length <= 1, "only the deliberately held meter request may be aborted");
  assert.deepEqual(diagnostics, []);
  console.log("editor-insights: both engines, verdict boundaries, missing history, transaction meter, navigation, polling races, 1440/720 light/dark");
} finally {
  if (browser) await browser.close();
  const exited = once(server, "exit").catch(() => {});
  if (server.exitCode === null) { server.kill("SIGTERM"); await exited; }
  await fs.rm(directory, { recursive: true, force: true });
}
