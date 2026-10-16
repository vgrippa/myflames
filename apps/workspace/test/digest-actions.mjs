import { chooseConnection } from "./chrome-helpers.mjs";
// Top statements row actions: Explain, Run analysis (with its confirmation),
// the parameter path for normalized text, Explain top N with progress and
// cancel, disabled non-SELECT rows, and session restore of the digest source.
// The live/* endpoints are mocked; plan analysis and session storage use the
// real Python server. Run: npm run test:digest-actions
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-digest-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

const A = "a".repeat(64);
const B = "b".repeat(32);
const C = "c".repeat(64);
const D = "d".repeat(64);
const E = "e".repeat(64);
const statement = (overrides) => ({
  digest: A,
  digest_text: "SELECT ?",
  schema: "shop",
  count: 120,
  total_ms: 5400.5,
  avg_ms: 45,
  max_ms: 90,
  rows_examined: 360000,
  rows_sent: 1200,
  examined_per_sent: 300,
  no_index_used: 120,
  no_good_index_used: 0,
  tmp_disk_tables: 0,
  sort_merge_passes: 0,
  sample_text: null,
  sample_truncated: false,
  first_seen: "2026-10-12 10:00:00",
  last_seen: "2026-10-12 11:00:00",
  flags: ["no_index_used"],
  capture_problem: null,
  ...overrides,
});
const SAMPLE_1 = "SELECT * FROM orders WHERE status = 'new' ORDER BY total DESC LIMIT 10";
const SAMPLE_4 = "SELECT name FROM customers WHERE country = 'US'";
const top = {
  schema_version: "top-1.0",
  engine: "mysql",
  server_version: "8.4.3",
  order: "total",
  notes: [],
  statements: [
    statement({
      digest: A,
      digest_text: "SELECT * FROM `orders` WHERE STATUS = ? ORDER BY `total` DESC LIMIT ?",
      sample_text: SAMPLE_1,
    }),
    statement({
      digest: "f".repeat(64),
      digest_text: "UPDATE `orders` SET STATUS = ? WHERE `id` = ?",
      sample_text: "UPDATE orders SET status = 'paid' WHERE id = 1",
      capture_problem: "Capture accepts one SELECT or WITH … SELECT statement.",
    }),
    statement({
      digest: B,
      digest_text: "SELECT * FROM `orders` WHERE `customer_id` IN (...) AND `created_at` > ? ",
      sample_text: null,
    }),
    statement({
      digest: C,
      digest_text: "SELECT NAME FROM `customers` WHERE `country` = ?",
      sample_text: SAMPLE_4,
    }),
    statement({
      digest: D,
      digest_text: "SELECT * FROM `missing` WHERE `a` = ?",
      sample_text: "SELECT * FROM missing WHERE a = 1",
    }),
    statement({
      digest: E,
      digest_text: "SELECT COUNT ( * ) FROM `orders`",
      sample_text: "SELECT COUNT(*) FROM orders",
    }),
  ],
};
// A sort over a table scan: the analysis reports a filesort finding.
const planFor = (query) =>
  JSON.stringify({
    query,
    operation: "Sort: orders.total DESC",
    access_type: "sort",
    estimated_rows: 3000,
    estimated_total_cost: 302.75,
    inputs: [
      {
        operation: "Table scan on orders",
        access_type: "table",
        table_name: "orders",
        estimated_rows: 3000,
        estimated_total_cost: 302.75,
      },
    ],
  });
const captureFor = (request) => ({
  schema_version: "capture-1.0",
  raw: planFor(request.query),
  query: request.query,
  mode: request.mode,
  engine: "mysql",
  server_version: "8.4.3",
  captured_at: "2026-10-12T11:00:00+00:00",
  conditions: "",
  warnings: [],
  runs: [{ total_time_ms: null, statistics: null, session_status_delta: {}, plan: "" }],
  schema: {},
  table_stats: {},
  variables: {},
  trace: null,
  ...(request.source ? { source: request.source } : {}),
});

// Mocked capture engine. `busyOnce` makes the next start report the
// two-capture limit; `hang` keeps jobs running until they are cancelled.
const starts = [];
const cancels = [];
const jobs = new Map();
const privilegeReads = [];
let privilegeActions = {};
const mock = { busyOnce: false, hang: false, busyReplies: 0 };
function respond(pathname, body) {
  if (pathname === "/api/live/privileges") {
    privilegeReads.push(body);
    return [200, { schema_version: "privileges-1.0", account: "test@%", actions: privilegeActions }];
  }
  if (pathname === "/api/live/top") return [200, top];
  if (pathname === "/api/live/start") {
    if (mock.busyOnce) {
      mock.busyOnce = false;
      mock.busyReplies++;
      return [400, { error: "Could not complete the request: Two captures are running. Wait or cancel one first." }];
    }
    starts.push(body);
    const id = `job-${starts.length}`;
    jobs.set(id, { request: body, cancelled: false, hang: mock.hang, polls: 0 });
    return [200, { id, state: "queued" }];
  }
  if (pathname === "/api/live/status") {
    const job = jobs.get(body.id);
    job.polls++;
    const base = { id: body.id, progress: 0, repeat: 1, error: "", result: null };
    if (job.cancelled) return [200, { ...base, state: "cancelled", error: "Capture cancelled." }];
    if (job.hang || job.polls < 2) return [200, { ...base, state: "running" }];
    if (job.request.query.includes("missing"))
      return [200, { ...base, state: "failed", error: "ERROR 1146 (42S02): Table 'shop.missing' doesn't exist" }];
    return [200, { ...base, state: "complete", progress: 1, result: captureFor(job.request) }];
  }
  if (pathname === "/api/live/cancel") {
    cancels.push(body.id);
    const job = jobs.get(body.id);
    if (job) job.cancelled = true;
    return [200, { id: body.id, state: "cancelled" }];
  }
  return null;
}

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
    const reply = respond(pathname, JSON.parse(request.postData() || "{}"));
    if (!reply && (pathname.startsWith("/api/live/") || pathname.startsWith("/api/connection/")))
      return request.respond({ status: 400, contentType: "application/json", body: JSON.stringify({ error: "Not mocked in this suite." }) });
    if (!reply) return request.continue();
    request.respond({
      status: reply[0],
      contentType: "application/json",
      body: JSON.stringify(reply[1]),
    });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const nav = (label) => page.locator(`.main-nav button ::-p-text(${label})`).click();
  async function clickExact(selector, label) {
    const handles = await page.$$(selector);
    for (const handle of handles)
      if ((await handle.evaluate((e) => { const copy = e.cloneNode(true); copy.querySelectorAll(".privilege-unverified").forEach(n => n.remove()); return copy.textContent.trim(); })) === label) {
        await handle.click();
        return;
      }
    throw new Error(`No ${selector} reading "${label}"`);
  }
  async function setField(label, value) {
    await page.evaluate(
      (label, value) => {
        const field = [...document.querySelectorAll("label")].find((l) =>
          l.textContent.trim().startsWith(label),
        );
        const input = field.querySelector("input");
        const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
        setter.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      },
      label,
      value,
    );
  }
  const heading = (name) =>
    page.waitForFunction(
      (name) => document.querySelector(".plan-heading h1")?.textContent === name,
      { timeout: 20000 },
      name,
    );
  const activeTab = () => text('.query-tabs [role="tab"][aria-selected="true"]');
  /** Select a Top statements row: its actions are in the inspector. */
  async function row(rank) {
    await page.click(`.server-view[data-view=top] tbody tr[data-rank="${rank}"]`);
    await page.waitForFunction((r) => document.querySelector(".view-inspector h3")?.textContent === `Statement ${r}`, {}, rank);
  }

  // Give the provider a real connection identity while keeping every
  // database endpoint mocked.
  const privilegeProfile = await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: JSON.stringify({ value: { name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "tester", database: "", ssl_mode: "PREFERRED" } }) });
    return (await response.json()).id;
  });
  await page.reload({ waitUntil: "networkidle0" });
  await nav("Query editor");
  await chooseConnection(page, privilegeProfile);
  // Query editor settings that the row actions must reuse.
  await page.click('button[aria-label="Explain and Run analysis options"]');
  await setField("Time limit per run", "45");
  await setField("Analysis runs", "3");
  await page.locator("label.check-field ::-p-text(Capture optimizer trace)").click();
  await page.keyboard.press("Escape");
  await nav("Server");
  await page.click("#server-nav-top");
  await page.locator("button ::-p-text(Load top statements)").click();
  await page.waitForSelector(".server-view tbody tr");
  assert.equal((await page.$$(".server-view tbody tr")).length, 6);

  // 1. A non-SELECT row: Explain and Run analysis are disabled with a reason.
  const disabled = (label) => page.$eval(`[aria-label="${label}"]`, (e) => e.disabled);
  await row(2);
  assert.equal(await page.$('.server-view[data-view=top] th[data-col=avg]'), null,
    "detail columns collapse without changing digest actions");
  assert.ok(await page.$('.server-view[data-view=top] th[data-col=statement]'));
  assert.equal(await disabled("Explain statement 2"), true);
  assert.equal(await disabled("Run analysis on statement 2"), true);
  assert.equal(await disabled("Use statement 2 in the editor"), false);
  assert.match(
    await text("#top-reason-2"),
    /Explain and Run analysis are off for this statement: Capture accepts one SELECT/,
  );
  assert.ok(await page.$eval('[aria-label="Explain statement 2"]', (e) => e.getAttribute("aria-describedby").split(" ").includes("top-reason-2")));
  await row(1);
  assert.equal(await disabled("Explain statement 1"), false);
  assert.match(await text('[aria-label="Explain statement 1"]'), /Unverified/);
  const refreshPrivileges = async () => {
    const reads = privilegeReads.length;
    await page.click('button[aria-label="Privileges"]');
    await page.locator('[role=dialog][aria-label="Privileges"] button ::-p-text(Refresh privileges)').click();
    await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Privileges"] button')?.disabled);
    assert.equal(privilegeReads.length, reads + 1);
    await page.keyboard.press("Escape");
  };
  // Synthetic definitive answers exercise UI projection, not SQL grants.
  const denied = { state: "denied", grey_out: true, missing: ["TEST_GLOBAL_PRIVILEGE"], reason: "Definitive global denial." };
  privilegeActions = { explain: denied, run_analysis: denied };
  await refreshPrivileges();
  const beforeGated = starts.length;
  for (const selector of ['[aria-label="Explain statement 1"]', '[aria-label="Run analysis on statement 1"]', '.batch-controls > button']) {
    assert.equal(await page.$eval(selector, e => e.disabled), false);
    assert.equal(await page.$eval(selector, e => e.getAttribute("aria-disabled")), "true");
    await page.focus(selector);
    assert.equal(await page.$eval(selector, e => e === document.activeElement), true);
    assert.match(await page.$eval(selector, e => e.getAttribute("aria-describedby").split(" ").map(id => document.getElementById(id)?.textContent).join(" ")), /TEST_GLOBAL_PRIVILEGE/);
    await page.click(selector);
  }
  assert.equal(starts.length, beforeGated);
  assert.equal(await page.$(".run-confirm"), null);
  privilegeActions = {};
  await refreshPrivileges();
  await page.locator('[aria-label="Run analysis on statement 1"]').click();
  assert.match(await text(".run-confirm"), /Unverified/);
  // A denial arriving while the analysis review is open disables confirm.
  privilegeActions = { run_analysis: denied };
  await refreshPrivileges();
  assert.equal(await page.$eval(".run-confirm .button.primary", e => e.getAttribute("aria-disabled")), "true");
  await page.locator(".run-confirm button ::-p-text(Execute and analyze)").click();
  assert.equal(starts.length, beforeGated);
  await page.locator(".run-confirm button ::-p-text(Cancel)").click();
  privilegeActions = {};
  await refreshPrivileges();
  await row(2);
  // Use this query still opens it, as recorded, with no parameters to fill,
  // in a new tab of the Query editor.
  await page.locator('[aria-label="Use statement 2 in the editor"]').click();
  await page.waitForSelector(".main-nav button.active ::-p-text(Query editor)");
  await page.waitForFunction(() =>
    document.querySelector(".editor-panel .notice")?.textContent.includes("cannot run it: Capture accepts one SELECT"),
  );
  assert.equal(
    await page.$eval('[aria-label="SQL editor"]', (e) => e.value),
    "UPDATE orders SET status = 'paid' WHERE id = 1",
  );
  assert.equal(await page.$(".fill-notice"), null);
  assert.equal(starts.length, 0);

  // 2. Explain from a row: new tab, digest schema as default database, editor
  // settings, source recorded, and the plan opens in Plans.
  await nav("Server");
  await row(1);
  await page.locator('[aria-label="Explain statement 1"]').click();
  await heading("Top #1 · orders");
  assert.equal(starts.length, 1);
  let sent = starts[0];
  assert.equal(sent.query, SAMPLE_1);
  assert.equal(sent.mode, "explain");
  assert.equal(sent.timeout, 45);
  assert.equal(sent.trace, true);
  assert.equal(sent.connection.database, "shop");
  assert.deepEqual(sent.parameters, {});
  assert.deepEqual(sent.source, { kind: "digest", digest: A, schema: "shop" });
  // The source is in the plan summary strip: the full digest and its schema.
  assert.equal(
    await page.$$eval(".primary-finding .strip-body p", (els) => els.map((e) => e.textContent).find((t) => t.startsWith("From Top statements"))),
    `From Top statements: ${A} in schema shop`,
  );
  await nav("Query editor");
  assert.equal(await activeTab(), "Top #1 · orders");
  assert.match(await text(".tab-source"), /From Top statements, digest aaaaaaaaaaaa · runs in shop, the schema it was recorded in/);

  // 3. Run analysis states what it executes before anything runs.
  await nav("Server");
  await row(4);
  await page.locator('[aria-label="Run analysis on statement 4"]').click();
  await page.waitForSelector(".run-confirm");
  assert.match(
    await text(".run-confirm p"),
    /executes this statement on 127\.0\.0\.1:3306: 3 runs inside a read-only transaction that is rolled back, each stopped after 45 s/,
  );
  await page.waitForFunction(() => document.activeElement?.textContent === "Execute and analyze");
  assert.equal(starts.length, 1, "the confirmation must not start a capture");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".run-confirm"));
  await page.waitForFunction(
    () => document.activeElement?.getAttribute("aria-label") === "Run analysis on statement 4",
  );
  assert.equal(starts.length, 1);
  await page.locator('[aria-label="Run analysis on statement 4"]').click();
  await page.locator(".run-confirm button ::-p-text(Execute and analyze)").click();
  await heading("Top #4 · customers");
  sent = starts[1];
  assert.equal(sent.mode, "analyze");
  assert.equal(sent.repeat, 3);
  assert.equal(sent.query, SAMPLE_4);
  assert.deepEqual(sent.source, { kind: "digest", digest: C, schema: "shop" });

  // 4. Normalized text: :p1, :p2 and a parameters skeleton; nothing runs
  // until the values are filled in.
  await nav("Server");
  await row(3);
  await page.locator('[aria-label="Explain statement 3"]').click();
  await page.waitForFunction(
    () => document.activeElement?.getAttribute("aria-label") === "Query parameters",
  );
  assert.equal(await activeTab(), "Top #3 · orders");
  assert.equal(
    await page.$eval('[aria-label="SQL editor"]', (e) => e.value),
    "SELECT * FROM `orders` WHERE `customer_id` IN (:p1) AND `created_at` > :p2",
  );
  assert.deepEqual(
    JSON.parse(await page.$eval('[aria-label="Query parameters"]', (e) => e.value)),
    { p1: null, p2: null },
  );
  assert.equal(await page.$eval(".editor-parameters", (e) => e.open), true);
  assert.ok(await page.$('[role=dialog][aria-label="Explain and Run analysis options"]:not([hidden]) [aria-label="Query parameters"]'), "a digest needing values opens its parameters popover automatically");
  const notice = await text(".fill-notice");
  assert.match(notice, /only the normalized text/);
  assert.match(notice, /Fill in p1 and p2 under Parameters/);
  assert.match(notice, /p1 replaces a collapsed list of values/);
  assert.equal(starts.length, 2, "the parameter path must not run");
  await clickExact(".editor-actions button", "Explain");
  await page.waitForFunction(() =>
    document.querySelector(".editor-panel .error")?.textContent.includes("Fill in p1 and p2 under Parameters before running"),
  );
  assert.equal(starts.length, 2);
  await page.locator('[aria-label="Query parameters"]').fill('{"p1": 7, "p2": "2026-01-01"}');
  await page.waitForFunction(() => !document.querySelector(".fill-notice"));
  await clickExact(".editor-actions button", "Explain");
  await heading("Top #3 · orders");
  sent = starts[2];
  assert.equal(sent.query, "SELECT * FROM `orders` WHERE `customer_id` IN (:p1) AND `created_at` > :p2");
  assert.deepEqual(sent.parameters, { p1: 7, p2: "2026-01-01" });
  assert.deepEqual(sent.source, { kind: "digest", digest: B, schema: "shop" });
  assert.equal(sent.connection.database, "shop");
  assert.match(await text(".primary-finding .strip-body"), /From Top statements: bbbbbbbbbbbb/);
  await page.screenshot({ path: path.join(directory, "digest-plan.png") });

  // 5. Explain top N: the two-capture limit, a failure, skipped rows, and
  // plans added without leaving Server.
  await nav("Server");
  await setField("Top N", "3");
  mock.busyOnce = true;
  const before = (await page.$$(".plan-item")).length;
  await page.locator("button ::-p-text(Explain top 3)").click();
  await page.waitForSelector(".batch-summary");
  // Editor runs wait while the batch runs (the first start waits a second
  // for a free capture slot, so the batch is still running here).
  await page.waitForFunction(
    () => document.querySelector(".editor-actions [data-action=analyze]")?.disabled === true,
  );
  await page.waitForFunction(() =>
    document.querySelector(".batch-summary [role=status]")?.textContent.includes("Waiting for a free capture slot"),
  );
  await page.waitForFunction(
    () => /Explain top N: 2 plans added to the open plans, 1 failed/.test(
      document.querySelector("#batch-title")?.textContent || "",
    ),
    { timeout: 30000 },
  );
  assert.equal(mock.busyReplies, 1);
  const batchStarts = starts.slice(3);
  assert.deepEqual(batchStarts.map((s) => s.query), [SAMPLE_1, SAMPLE_4, "SELECT * FROM missing WHERE a = 1"]);
  assert.ok(batchStarts.every((s) => s.mode === "explain" && s.trace === false && s.timeout === 45));
  assert.deepEqual(batchStarts.map((s) => s.source.digest), [A, C, D]);
  const rows = await page.$$eval(".batch-table tbody tr", (trs) =>
    trs.map((tr) => [...tr.querySelectorAll("td")].map((td) => td.textContent.trim())),
  );
  assert.equal(rows.length, 3);
  assert.deepEqual(rows.map((r) => r[0]), ["1", "4", "5"]);
  assert.equal(rows[0][2], "3,000");
  assert.match(rows[0][3], /Filesort/);
  assert.match(rows[0][4], /Open plan/);
  assert.match(rows[2][3], /Failed: ERROR 1146/);
  const skipped = await text(".batch-skipped");
  assert.match(skipped, /Skipped #3: only normalized text was recorded, so it needs parameter values/);
  assert.match(skipped, /Skipped #2: not a read-only SELECT/);
  assert.equal((await page.$$(".plan-item")).length, before + 2);
  assert.equal(await page.$eval(".server-place", (e) => e.closest("[hidden]") === null), true);
  assert.equal(await text(".main-nav button.active"), "Server");
  assert.equal(
    await page.$eval(".editor-actions [data-action=analyze]", (e) => e.disabled),
    false,
  );
  await page.locator('[aria-label="Open plan Top #4 · customers"]').click();
  await heading("Top #4 · customers");
  assert.match(await text(".primary-finding .strip-body"), /From Top statements: cccccccccccc/);

  // 6. Cancel: a job that keeps running is cancelled on the server.
  await nav("Server");
  await setField("Top N", "5");
  mock.hang = true;
  const startsBefore = starts.length;
  await page.locator("button ::-p-text(Explain top 5)").click();
  await page.waitForFunction(() =>
    document.querySelector(".batch-summary [role=status]")?.textContent.includes("Explaining Top #1 · orders"),
  );
  while (starts.length === startsBefore) await new Promise((r) => setTimeout(r, 50));
  await page.locator("button ::-p-text(Cancel Explain top 5)").click();
  await page.waitForFunction(
    () => /\(cancelled\)/.test(document.querySelector("#batch-title")?.textContent || ""),
    { timeout: 10000 },
  );
  assert.deepEqual(cancels, [`job-${startsBefore + 1}`]);
  assert.equal(starts.length, startsBefore + 1, "nothing starts after a cancel");
  assert.match(await text("#batch-title"), /0 plans added/);
  mock.hang = false;
  await (await page.$(".server-place")).screenshot({ path: path.join(directory, "digest-batch.png") });

  // 7. Narrow viewport: the summary and row actions fit without page scroll.
  await page.setViewport({ width: 390, height: 844 });
  await new Promise((r) => setTimeout(r, 200));
  const wide = await page.evaluate(() => [...document.querySelectorAll("body *")]
    .filter((e) => e.getBoundingClientRect().right > innerWidth + 1 && !e.closest(".data-table-wrap"))
    .map((e) => `${e.tagName}.${e.className} ${Math.round(e.getBoundingClientRect().right)}`).slice(0, 8));
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
    false,
    wide.join(" | "),
  );
  await page.screenshot({ path: path.join(directory, "digest-mobile.png") });
  await page.setViewport({ width: 1440, height: 1000 });

  // 8. Session restore keeps the source on plans and on the query tab.
  await page.waitForFunction(
    () => document.querySelector(".session-status")?.dataset.state === "saved",
    { timeout: 15000 },
  );
  const stored = await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    return r.json();
  });
  const storedPlan = stored.state.plans.find((p) => p.name === "Top #1 · orders");
  assert.deepEqual(storedPlan.capture.source, { kind: "digest", digest: A, schema: "shop" });
  const storedTab = stored.state.query_lab.tabs.find((t) => t.name === "Top #3 · orders");
  assert.deepEqual(storedTab.source, { kind: "digest", digest: B, schema: "shop" });
  assert.deepEqual(storedTab.fill, ["p1", "p2"]);
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".session-notice");
  await page.locator(".plan-select ::-p-text(Top #1 · orders)").click();
  await heading("Top #1 · orders");
  assert.match(await text(".primary-finding .strip-body"), /From Top statements: aaaaaaaaaaaa/);
  await nav("Query editor");
  await page.locator('.query-tabs [role="tab"] ::-p-text(Top #3 · orders)').click();
  assert.match(await text(".tab-source"), /digest bbbbbbbbbbbb/);
  assert.equal(await page.$(".fill-notice"), null, "filled parameters stay filled");

  assert.deepEqual(errors, []);
  console.log("Digest actions passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
