import { connectionAction } from "./connection-helpers.mjs";
import { chooseConnection } from "./chrome-helpers.mjs";
// Reports, selections, requests and actions bound to the connection they
// belong to (binding.ts). Two saved connections, Prod (p1) and Staging (p2):
// a report read on one is greyed with its actions off on the other; a late
// answer for a connection no longer in use is dropped; Kill goes to the
// connection it was confirmed for; a newer Sessions scope replaces a read
// still running; Explain top N keeps its connection when the picker changes
// mid-batch; a Run analysis confirmation closes when its statement or
// connection changes; Locks stops polling while Server is hidden; the
// full-width detail keeps focus across refreshes; Compare never shows the
// previous pair's result; Cancel capture works while the start is pending;
// Save connection is single flight and a test result belongs to its draft.
// The live/*, connection/* and profiles/* endpoints are mocked (requests can
// be held and answered late); plan analysis and session storage use the real
// Python server. Run: npm run test:connection-binding
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));
const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (v) => JSON.parse(JSON.stringify(v));

const active = await load("sessions-mysql-8.4-active.json");
const all = await load("sessions-mysql-8.4.json");
const mariadb = await load("sessions-mariadb-11.4.json");
// Kill is bound to the server a report was read from; these fixtures predate that.
for (const report of [active, all]) report.server_identity = { server_uuid: "binding-server-uuid" };
mariadb.server_identity = { server_id: "1", hostname: "binding-mariadb", version: mariadb.server_version };
const locks = await load("locks-mysql-8.4.json");
const io = await load("io-mysql-8.4.json");
const schemas = await load("schemas-mysql-8.4.json");

const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-binding-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

// ---- Mocked endpoints ----------------------------------------------------------
const store = { backend: "vault", label: "encrypted vault", available: true };
let profiles = [
  { id: "p1", name: "Prod", host: "db.prod", port: 3306, user: "analyst", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
    has_saved_password: false, secret_backend: "", secret_backend_label: "" },
  { id: "p2", name: "Staging", host: "db.staging", port: 3307, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
    has_saved_password: false, secret_backend: "", secret_backend_label: "" },
];
const DIGESTS = ["a".repeat(64), "c".repeat(64), "d".repeat(64)];
const statement = (digest, sample, overrides = {}) => ({
  digest, digest_text: "SELECT ?", schema: "shop", count: 10, total_ms: 500, avg_ms: 50, max_ms: 90, rows_examined: 1000,
  rows_sent: 10, examined_per_sent: 100, no_index_used: 0, no_good_index_used: 0, tmp_disk_tables: 0, sort_merge_passes: 0,
  sample_text: sample, sample_truncated: false, first_seen: null, last_seen: null, flags: [], capture_problem: null, ...overrides,
});
const topReport = (statements) => ({ schema_version: "top-1.0", engine: "mysql", server_version: "8.4.3", order: "total", notes: [], statements });
let top = topReport([
  statement(DIGESTS[0], "SELECT * FROM orders WHERE status = 'new'"),
  statement(DIGESTS[1], "SELECT name FROM customers WHERE country = 'US'"),
  statement(DIGESTS[2], "SELECT COUNT(*) FROM orders"),
]);
const planFor = (query) => JSON.stringify({
  query, operation: "Table scan on orders", access_type: "table", table_name: "orders", estimated_rows: 3000, estimated_total_cost: 302.75,
});
const captureFor = (request) => ({
  schema_version: "capture-1.0", raw: planFor(request.query), query: request.query, mode: request.mode, engine: "mysql",
  server_version: "8.4.3", captured_at: "2026-10-14T11:00:00+00:00", conditions: "", warnings: [],
  runs: [{ total_time_ms: null, statistics: null, session_status_delta: {}, plan: "" }],
  schema: {}, table_stats: {}, variables: {}, trace: null, ...(request.source ? { source: request.source } : {}),
});
const jobs = new Map();
const cancels = [];
const killed = { schema_version: "kill-1.0", status: "killed", connection_id: 79, mode: "connection", statement: "KILL CONNECTION 79",
  verified: ["user", "host"], after: { present: false }, message: "Ended connection 79." };
const responders = {
  "/api/profiles/list": () => [200, { items: profiles, secret_store: store }],
  "/api/profiles/save": (body) => {
    const id = body.id || `p${profiles.length + 1}`;
    const saved = { ...body.value, id, has_saved_password: false, secret_backend: "", secret_backend_label: "" };
    profiles = profiles.some((p) => p.id === id) ? profiles.map((p) => (p.id === id ? saved : p)) : [...profiles, saved];
    return [200, saved];
  },
  "/api/live/privileges": (body) => [200, { schema_version: "privileges-1.0", account: `${body.connection.user}@%`, actions: {} }],
  "/api/connection/test": () => [200, { version: "8.4.3", schemas: ["shop", "billing"] }],
  "/api/live/sessions": (body) => [200, body.connection.profile_id === "p2" ? mariadb : active],
  "/api/live/locks": () => [200, locks],
  "/api/live/io": () => [200, io],
  "/api/live/schemas": () => [200, schemas],
  "/api/live/kill": () => [200, killed],
  "/api/live/top": () => [200, top],
  "/api/live/start": (body) => {
    const id = `job-${jobs.size + 1}`;
    jobs.set(id, body);
    return [200, { id, state: "queued" }];
  },
  "/api/live/status": (body) => [200, {
    id: body.id, state: "complete", progress: 1, repeat: 1, error: "", result: captureFor(jobs.get(body.id)),
  }],
  "/api/live/cancel": (body) => {
    cancels.push(body.id);
    return [200, { id: body.id, state: "cancelled" }];
  },
};
const seen = [];
const pending = [];
const hold = new Set();
const flags = { failCompare: false, recordAnalyze: false };
const analyzed = [];
const calls = (p) => seen.filter((s) => s.path === p);

async function until(check, message, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = check();
    if (value) return value;
    await pause(25);
  }
  assert.fail(message);
}
/** The next held request for *p* (removed from the queue). */
async function held(p) {
  const entry = await until(() => pending.find((x) => x.path === p), `no held request for ${p}`);
  pending.splice(pending.indexOf(entry), 1);
  return entry;
}
function answer(entry, reply = responders[entry.path](entry.body)) {
  entry.request.respond({ status: reply[0], contentType: "application/json", body: JSON.stringify(reply[1]) });
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
  page.on("dialog", (d) => d.accept());
  // Automatic refresh ticks (10 s and longer) every 300 ms, so polling can be observed quickly.
  await page.evaluateOnNewDocument(() => {
    const every = window.setInterval.bind(window);
    window.setInterval = (fn, ms, ...rest) => every(fn, ms >= 10000 ? 300 : ms, ...rest);
  });
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (p === "/api/analyze" && flags.recordAnalyze) analyzed.push(JSON.parse(request.postData() || "{}"));
    // Held analysis requests reach the real server when the test lets them go (entry.request.continue()).
    if (p === "/api/analyze" && hold.has(p)) return void pending.push({ path: p, body: {}, request });
    if (p === "/api/compare" && flags.failCompare)
      return request.respond({ status: 400, contentType: "application/json",
        body: JSON.stringify({ error: "Could not complete the request: comparison failed." }) });
    if (!(p in responders)) {
      if ((p.startsWith("/api/live/") && p !== "/api/live/kill/prepare") || p.startsWith("/api/connection/")) {
        // Recorded too, so a test can prove that nothing was read.
        seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
        return request.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
      }
      // The Query editor's session requests and audit records reach the real
      // server (no database behind it here); they are recorded so a test can
      // prove what was sent.
      if (p.startsWith("/api/editor/") || p.startsWith("/api/audit/")) seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
      return request.continue();
    }
    const body = JSON.parse(request.postData() || "{}");
    seen.push({ path: p, body });
    if (hold.has(p)) pending.push({ path: p, body, request });
    else answer({ path: p, body, request });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const nav = (label) => page.locator(`.main-nav button ::-p-text(${label})`).click();
  const use = async (id) => {
    if (await page.$(".window-toolbar .connection-pill")) return chooseConnection(page, id);
    // Home has the connection list; exercise its Use action in place.
    const name = id === "p1" ? "Prod" : "Staging";
    await backToList("connections");
    await connectionAction(page, name, "Use");
  };
  const view = (kind) => `.server-view[data-view=${kind}]`;
  const WORKLOAD_VIEWS = ["top", "sessions", "waits", "io"];
  const openView = async (kind) => {
    if (WORKLOAD_VIEWS.includes(kind)) {
      await nav("Workload");
    } else {
      await nav("Server");
    }
    await page.locator(`#server-nav-${kind}`).click();
    await page.waitForSelector(view(kind));
  };
  const disabled = (selector) => page.$eval(selector, (e) => e.disabled);
  const other = (kind) => page.$eval(view(kind), (e) => e.dataset.otherConnection === "true");
  const rowIds = (kind) => page.$$eval(`${view(kind)} tbody tr[data-session]`, (els) => els.map((e) => +e.dataset.session));
  async function backToList(kind) {
    const back = await page.$(`${view(kind)} .detail-back`);
    if (back) {
      await back.click();
      await page.waitForFunction((v) => !document.querySelector(`${v} .view-inspector.is-detail`), {}, view(kind));
    }
  }
  /** A view reads when it is opened on screen; the empty state's Load is
   * only there when that read has not started (or failed). */
  async function load(kind, label) {
    // The automatic read may replace the empty state between the two steps.
    await page.evaluate((v, l) => {
      const button = document.querySelector(`${v} .view-empty button`);
      if (button && button.textContent.trim() === l) button.click();
    }, view(kind), label);
  }
  async function selectSession(id) {
    await backToList("sessions");
    await page.click(`${view("sessions")} tbody tr[data-session="${id}"]`);
    await page.waitForFunction((i) => document.querySelector(".view-inspector h3")?.textContent === `Connection ${i}`, {}, id);
  }
  async function clickExact(selector, label) {
    for (const handle of await page.$$(selector))
      if ((await handle.evaluate((e) => { const copy = e.cloneNode(true); copy.querySelectorAll(".privilege-unverified").forEach(n => n.remove()); return copy.textContent.trim(); })) === label) return handle.click();
    throw new Error(`No ${selector} reading "${label}"`);
  }
  async function setField(label, value) {
    await page.evaluate((label, value) => {
      const field = [...document.querySelectorAll("label")].find((l) => l.textContent.trim().startsWith(label));
      const input = field.querySelector("input");
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, label, value);
  }

  // 1. Sessions read on Prod: Kill and Show plan are offered.
  await nav("Workload");
  await page.waitForSelector(".server-place .source-list");
  await use("p1");
  await openView("sessions");
  await load("sessions", "Load sessions");
  await page.waitForSelector(`${view("sessions")} tbody tr[data-session="93"]`);
  assert.equal(calls("/api/live/sessions").at(-1).body.connection.profile_id, "p1");
  assert.equal(await other("sessions"), false);
  await selectSession(93);
  assert.equal(await disabled('.view-inspector [aria-label="Kill: connection 93"]'), false);
  assert.equal(await text("#server-nav-sessions .count-badge"), "9");

  // 2. Switch to Staging: the report stays, greyed, and nothing acts on it.
  await use("p2");
  await page.waitForSelector(`${view("sessions")}[data-other-connection=true]`);
  assert.match(await text(`${view("sessions")} .other-connection`),
    /From Prod \(analyst@db\.prod:3306\), refresh to load this server\./);
  assert.equal(await disabled('.view-inspector [aria-label="Kill: connection 93"]'), true, "Kill is off on another connection");
  await selectSession(100);
  assert.equal(await disabled('.view-inspector [aria-label="Show plan for connection 100"]'), true, "Show plan is off");
  assert.equal(await page.$("#server-nav-sessions .count-badge"), null, "no badge from another connection");
  assert.equal(calls("/api/live/sessions").length, 1, "nothing is read by switching");

  // 3. A late answer for Prod after switching to Staging is dropped, and a
  // read still running on Prod does not block Refresh on Staging.
  await use("p1");
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection !== "true", {}, view("sessions"));
  hold.add("/api/live/sessions");
  await page.click(`${view("sessions")} [data-action=refresh]`);
  const lateProd = await held("/api/live/sessions");
  assert.equal(lateProd.body.connection.profile_id, "p1");
  await use("p2");
  await page.waitForFunction(
    (v) => document.querySelector(`${v} [data-action=refresh]`)?.disabled === false, {}, view("sessions"));
  await page.click(`${view("sessions")} [data-action=refresh]`);
  const staging = await held("/api/live/sessions");
  assert.equal(staging.body.connection.profile_id, "p2");
  assert.equal(staging.body.connection.host, "db.staging");
  answer(staging);
  await page.waitForSelector(`${view("sessions")} tbody tr[data-session="79"]`);
  assert.equal(await other("sessions"), false);
  answer(lateProd, [200, all]);
  await pause(400);
  assert.deepEqual(await rowIds("sessions"), mariadb.sessions.map((s) => s.id), "the late Prod answer is dropped");
  assert.equal(await page.$(`${view("sessions")} [role=alert]`), null, "and shows no error");
  hold.delete("/api/live/sessions");

  // 4. Kill goes to the connection it was confirmed for, with the session's fingerprint.
  await selectSession(79);
  await page.click('.view-inspector [aria-label="Kill: connection 79"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.match(await text("dialog.kill-dialog"), /Server\s*Staging \(dev@db\.staging:3307\)/);
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(`${view("sessions")} .kill-result.killed`);
  const kill = calls("/api/live/kill").at(-1).body;
  const target = mariadb.sessions.find((s) => s.id === 79);
  assert.deepEqual(
    { profile: kill.connection.profile_id, host: kill.connection.host, id: kill.connection_id, expect: kill.expect },
    { profile: "p2", host: "db.staging", id: 79,
      expect: { user: target.user, host: target.host, trx_started: target.trx?.started ?? null, ps_thread_id: null,
        // The Kill is bound to the connection (saved id, host, port, user, TLS) and the server the report came from.
        connection_identity: '["p2","db.staging",3307,"dev","PREFERRED",""]', server_identity: mariadb.server_identity } },
  );
  await page.locator(`${view("sessions")} .kill-result ::-p-text(Dismiss)`).click();
  // The Kill sheet is modal (a macOS-style sheet), so the connection cannot
  // change while it is open. Cancel records one declined audit entry and sends
  // nothing; after a switch the old target cannot be killed from the new
  // connection: the Staging report is greyed, Kill on it is off, and a
  // refresh is required (which reads the new server, without that row).
  await selectSession(80);
  await page.click('.view-inspector [aria-label="Kill: connection 80"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  const killsBefore = calls("/api/live/kill").length;
  const declinedBefore = calls("/api/audit/declined").length;
  await page.locator("dialog.kill-dialog button ::-p-text(Cancel)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog[open]"));
  await until(() => calls("/api/audit/declined").length > declinedBefore, "Cancel recorded no declined audit");
  await pause(300);
  assert.equal(calls("/api/audit/declined").length, declinedBefore + 1, "one declined record");
  assert.equal(calls("/api/audit/declined").at(-1).body.connection.profile_id, "p2", "declined on the connection it was for");
  assert.equal(calls("/api/live/kill").length, killsBefore, "nothing was sent");
  const readsBeforeSwitch = calls("/api/live/sessions").length;
  await use("p1");
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection === "true", {}, view("sessions"));
  assert.match(await text(`${view("sessions")} .other-connection`), /refresh to load this server/);
  const staleKill = await page.$('.view-inspector [aria-label="Kill: connection 80"]');
  assert.ok(!staleKill || (await staleKill.evaluate((e) => e.disabled || e.getAttribute("aria-disabled") === "true")),
    "Kill is off on a report from the other connection");
  assert.equal(calls("/api/live/sessions").length, readsBeforeSwitch, "switching reads nothing");
  assert.ok(!active.sessions.some((x) => x.id === 80), "the Production server has no connection 80");
  await use("p2");
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection !== "true", {}, view("sessions"));

  // 5. Latest wins: All while an Active read runs replaces it.
  await backToList("sessions");
  hold.add("/api/live/sessions");
  await page.click(`${view("sessions")} [data-action=refresh]`);
  const activeRead = await held("/api/live/sessions");
  await page.click(`${view("sessions")} .segmented button[data-value=all]`);
  const allRead = await held("/api/live/sessions");
  assert.deepEqual([activeRead.body.active_only, allRead.body.active_only], [true, false]);
  answer(allRead, [200, all]);
  answer(activeRead, [200, mariadb]);
  await page.waitForFunction((v, n) => document.querySelectorAll(`${v} tbody tr[data-session]`).length === n, {},
    view("sessions"), all.sessions.length);
  await pause(300);
  assert.deepEqual(await rowIds("sessions"), all.sessions.map((s) => s.id), "the All report stays");
  assert.equal(await page.$eval(`${view("sessions")} .segmented button[data-value=all]`, (e) => e.getAttribute("aria-pressed")), "true");
  hold.delete("/api/live/sessions");
  await page.click(`${view("sessions")} .segmented button[data-value=active]`);
  await page.waitForFunction((v) => !document.querySelector(`${v} .spinner`), {}, view("sessions"));

  // 6. Full-width details keep focus on their action across automatic refreshes.
  await backToList("sessions");
  await page.setViewport({ width: 1150, height: 900 });
  await page.select(`${view("sessions")} .auto-refresh select`, "10");
  await page.click(`${view("sessions")} tbody tr[data-session="80"]`);
  await page.waitForSelector(".view-inspector.is-detail");
  await page.waitForFunction(() => document.activeElement?.classList.contains("inspector-close"), {}, "focus moves in once");
  await page.locator(".view-inspector button ::-p-text(Open Locks to see who blocks it)").setTimeout(5000).wait();
  await page.evaluate(() =>
    [...document.querySelectorAll(".view-inspector button")].find((b) => b.textContent === "Open Locks to see who blocks it").focus());
  const before = calls("/api/live/sessions").length;
  await until(() => calls("/api/live/sessions").length >= before + 3, "automatic refresh did not run");
  await pause(100);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Open Locks to see who blocks it",
    "a refresh does not move focus to Back");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  await page.select(`${view("sessions")} .auto-refresh select`, "0");
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(400);

  // 7. Locks polls only while it is on screen.
  await openView("locks");
  await load("locks", "Load locks");
  await page.waitForSelector(`${view("locks")} .summary-line`);
  await page.select(`${view("locks")} .auto-refresh select`, "10");
  let count = calls("/api/live/locks").length;
  await until(() => calls("/api/live/locks").length >= count + 2, "Locks automatic refresh did not run");
  await nav("Home");
  await pause(400);
  count = calls("/api/live/locks").length;
  await pause(1500);
  assert.equal(calls("/api/live/locks").length, count, "no reads while Server is hidden");
  await nav("Server");
  await until(() => calls("/api/live/locks").length > count, "Locks automatic refresh did not resume");
  await page.select(`${view("locks")} .auto-refresh select`, "0");

  // 7b. Links from a report of another connection read nothing: the buttons
  // are off, and the handler refuses a stale click anyway.
  await use("p1");
  await openView("io");
  await load("io", "Load I/O");
  await page.waitForSelector(`${view("io")} tr[data-table="orders"]`);
  await page.click(`${view("io")} tr[data-table="orders"]`);
  await page.locator(".view-inspector button ::-p-text(Index health)").setTimeout(5000).wait();
  const linkButton = (label) => page.evaluateHandle((l) =>
    [...document.querySelectorAll(".view-inspector button")].find((b) => b.textContent.trim() === l), label);
  assert.equal(await (await linkButton("Index health")).evaluate((b) => b.disabled), false);
  await use("p2");
  await page.waitForSelector(`${view("io")}[data-other-connection=true]`);
  assert.equal(await (await linkButton("Index health")).evaluate((b) => b.disabled), true, "Index health is off");
  assert.equal(await (await linkButton("Top statements")).evaluate((b) => b.disabled), true, "Top statements is off");
  // React ignores clicks on a disabled button, so call the button's own
  // handler (from this render, bound to the Prod report) to reach Server's check.
  const linkReads = () => calls("/api/live/indexes").length + calls("/api/live/top").length;
  const readsBeforeLinks = linkReads();
  for (const label of ["Index health", "Top statements"]) {
    await (await linkButton(label)).evaluate((b) => {
      const props = Object.entries(b).find(([k]) => k.startsWith("__reactProps$"))?.[1];
      props.onClick();
    });
    await pause(500);
    assert.equal(linkReads(), readsBeforeLinks, `a stale ${label} link reads nothing on the connection in use`);
    assert.ok(await page.$("#server-nav-io[aria-current=page]"), "and does not navigate");
  }

  // 8. Run analysis: the confirmation belongs to one statement, its SQL and
  // its connection; rows are keyed by schema and digest, not rank.
  await use("p1");
  await openView("top");
  await load("top", "Load top statements");
  await page.waitForSelector(`${view("top")} tbody tr[data-rank="1"]`);
  await page.click(`${view("top")} tbody tr[data-rank="1"]`);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Statement 1");
  await page.locator('[aria-label="Run analysis on statement 1"]').click();
  await page.waitForSelector(".run-confirm");
  // The statement's text changes on the server: the confirmation closes.
  top = topReport([statement(DIGESTS[0], "SELECT * FROM orders WHERE status = 'paid'"), ...top.statements.slice(1)]);
  await page.click(`${view("top")} [data-action=refresh]`);
  await page.waitForSelector(".top-voided");
  assert.equal(await page.$(".run-confirm"), null);
  assert.equal(calls("/api/live/start").length, 0, "nothing ran");
  // Another statement takes rank 1: the selection follows the digest.
  top = topReport([top.statements[1], top.statements[0], top.statements[2]]);
  await page.click(`${view("top")} [data-action=refresh]`);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Statement 2");
  assert.match(await text(".view-inspector"), /status = 'paid'/);
  // Switching the connection closes an open confirmation and turns the actions off.
  await page.locator('[aria-label="Run analysis on statement 2"]').click();
  await page.waitForSelector(".run-confirm");
  await use("p2");
  await page.waitForSelector(".top-voided");
  assert.equal(await page.$(".run-confirm"), null);
  assert.equal(await disabled('[aria-label="Run analysis on statement 2"]'), true);
  assert.equal(await disabled('[aria-label="Explain statement 2"]'), true);
  assert.equal(await disabled('[aria-label="Use statement 2 in the editor"]'), true);
  assert.equal(await other("top"), true);
  assert.equal(calls("/api/live/start").length, 0, "nothing ran");

  // Return from the full-width statement before choosing a list action.
  await page.locator('button ::-p-text(Back to statements)').click();
  // 9. Explain top N keeps the connection and settings it started with.
  await use("p1");
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection !== "true", {}, view("top"));
  await setField("Top N", "3");
  hold.add("/api/live/start");
  await page.locator("button ::-p-text(Explain top 3)").click();
  const first = await held("/api/live/start");
  await use("p2");
  await page.waitForSelector(`${view("top")}[data-other-connection=true]`);
  answer(first);
  const second = await held("/api/live/start");
  answer(second);
  const third = await held("/api/live/start");
  answer(third);
  await page.waitForFunction(
    () => /Explain top N: 3 plans added/.test(document.querySelector("#batch-title")?.textContent || ""), { timeout: 30000 });
  assert.deepEqual(
    [first, second, third].map((s) => [s.body.connection.profile_id, s.body.connection.host, s.body.mode]),
    [["p1", "db.prod", "explain"], ["p1", "db.prod", "explain"], ["p1", "db.prod", "explain"]],
    "every statement of the batch ran on the connection it started on",
  );
  assert.equal(await disabled(`${view("top")} .batch-controls .button.small`), true, "Explain top N is off on another connection");
  hold.delete("/api/live/start");

  // Cancel Explain top N while a status read is on its way: the finished plan is not imported.
  await use("p1");
  await page.waitForFunction((v) => document.querySelector(v)?.dataset.otherConnection !== "true", {}, view("top"));
  const plans = (await page.$$(".plan-item")).length;
  await setField("Top N", "1");
  hold.add("/api/live/status");
  await page.locator("button ::-p-text(Explain top 1)").click();
  const poll = await held("/api/live/status");
  await page.locator("button ::-p-text(Cancel Explain top 1)").click();
  await until(() => cancels.includes(poll.body.id), "the running job was not cancelled");
  answer(poll);
  await page.waitForFunction(() => /\(cancelled\)/.test(document.querySelector("#batch-title")?.textContent || ""));
  await pause(300);
  assert.equal((await page.$$(".plan-item")).length, plans, "a result that arrives after Cancel is not imported");
  hold.delete("/api/live/status");
  // Cancel while the start is on its way: the job it started is cancelled at once.
  hold.add("/api/live/start");
  await page.locator("button ::-p-text(Explain top 1)").click();
  const lateStart = await held("/api/live/start");
  await page.locator("button ::-p-text(Cancel Explain top 1)").click();
  answer(lateStart);
  const lateJob = `job-${jobs.size}`;
  await until(() => cancels.includes(lateJob), "a job started after Cancel was not cancelled");
  await page.waitForFunction(() => /\(cancelled\)/.test(document.querySelector("#batch-title")?.textContent || ""));
  await pause(600);
  assert.equal(calls("/api/live/status").filter((c) => c.body.id === lateJob).length, 0);
  assert.equal((await page.$$(".plan-item")).length, plans);
  hold.delete("/api/live/start");
  // Cancel while the finished plan is being analyzed: the importer adds nothing.
  hold.add("/api/analyze");
  await page.locator("button ::-p-text(Explain top 1)").click();
  const analysis = await held("/api/analyze");
  await page.locator("button ::-p-text(Cancel Explain top 1)").click();
  analysis.request.continue();
  await page.waitForFunction(() => /\(cancelled\)/.test(document.querySelector("#batch-title")?.textContent || ""));
  await pause(500);
  assert.equal((await page.$$(".plan-item")).length, plans, "a plan analyzed after Cancel is not added");
  hold.delete("/api/analyze");

  // 10. Compare: a failed comparison never shows the previous pair's result.
  await nav("Plans");
  await page.locator(".plans-bar button ::-p-text(Compare)").click();
  const ids = await page.$$eval(".compare-selectors label:first-of-type select option", (o) => o.map((e) => e.value).filter(Boolean));
  assert.equal(ids.length, 3);
  const [before2, after2] = await page.$$(".compare-selectors select");
  await before2.select(ids[0]);
  await after2.select(ids[2]);
  await page.waitForSelector(".comparison-canvases", { timeout: 20000 });
  flags.failCompare = true;
  await after2.select(ids[1]);
  await page.waitForFunction(() => /comparison failed/.test(document.querySelector("main > [role=alert]")?.textContent || ""));
  assert.equal(await page.$(".comparison-canvases"), null, "no charts from the previous pair");
  assert.equal(await page.$(".comparison-list"), null);
  assert.equal(await page.$("main section .heading-actions"), null, "no export of the previous pair");
  flags.failCompare = false;

  // 11. Query tabs follow the tab pattern: one tab stop, arrows, Home and End,
  // a linked panel, and New query tab outside the tab list.
  await nav("Query editor");
  await page.click('button[aria-label="New query tab"]');
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction(() => document.querySelectorAll(".query-tabs [role=tab][id^=query-tab-]").length === 3);
  assert.equal(await page.$('[role=tablist] [aria-label="New query tab"]'), null);
  const tabState = () => page.$$eval("[role=tablist]:not(.query-mode-switch) [role=tab]", (els) => ({
    selected: els.findIndex((e) => e.getAttribute("aria-selected") === "true"),
    focused: els.indexOf(document.activeElement),
    stops: els.filter((e) => e.tabIndex === 0).length,
    controls: els.every((e) => e.getAttribute("aria-controls") === "query-tab-panel"),
    labelled: document.getElementById("query-tab-panel")?.getAttribute("aria-labelledby") ===
      els.find((e) => e.getAttribute("aria-selected") === "true")?.id,
  }));
  assert.deepEqual(await tabState(), { selected: 2, focused: -1, stops: 1, controls: true, labelled: true });
  await page.focus("[role=tablist]:not(.query-mode-switch) [role=tab][aria-selected=true]");
  await page.keyboard.press("Home");
  assert.deepEqual(await tabState(), { selected: 0, focused: 0, stops: 1, controls: true, labelled: true });
  await page.keyboard.press("ArrowLeft");
  assert.deepEqual(await tabState(), { selected: 2, focused: 2, stops: 1, controls: true, labelled: true }, "Left wraps to the last tab");
  await page.keyboard.press("ArrowRight");
  assert.deepEqual(await tabState(), { selected: 0, focused: 0, stops: 1, controls: true, labelled: true }, "Right wraps to the first tab");
  await page.keyboard.press("End");
  assert.deepEqual(await tabState(), { selected: 2, focused: 2, stops: 1, controls: true, labelled: true });
  await page.keyboard.press("Home");

  // 12. Cancel capture while the start is still on its way.
  hold.add("/api/live/start");
  await clickExact(".editor-actions button", "Explain");
  const start = await held("/api/live/start");
  await page.locator(".editor-actions button ::-p-text(Cancel capture)").click();
  answer(start);
  const job = `job-${jobs.size}`;
  await until(() => cancels.includes(job), "the started job was not cancelled");
  await pause(1200);
  assert.equal(calls("/api/live/status").filter((c) => c.body.id === job).length, 0, "it never became the running capture");
  assert.match(await text(".query-lab .notice[role=status]"), /Capture cancelled before it started/);
  assert.equal(await page.$(".editor-actions button.danger"), null);
  hold.delete("/api/live/start");

  // 12b. A Top statements tab stays bound to the server it came from, also
  // for a run deferred until its parameters are filled in, and after restore.
  await use("p1");
  await nav("Server");
  await openView("top");
  const PARAMETERIZED = "e".repeat(64);
  top = topReport([...top.statements, statement(PARAMETERIZED, null, { digest_text: "SELECT * FROM `orders` WHERE `id` = ?" })]);
  await page.click(`${view("top")} [data-action=refresh]`);
  await page.waitForSelector(`${view("top")} tbody tr[data-digest="${PARAMETERIZED}"]`);
  await page.click(`${view("top")} tbody tr[data-digest="${PARAMETERIZED}"]`);
  const rank = await page.$eval(`${view("top")} tbody tr[data-digest="${PARAMETERIZED}"]`, (e) => e.dataset.rank);
  await page.locator(`[aria-label="Run analysis on statement ${rank}"]`).click();
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Query parameters");
  await use("p2");
  const startsBefore = calls("/api/live/start").length;
  await page.evaluate(() => {
    const field = document.querySelector('[aria-label="Query parameters"]');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(field, '{"p1": 7}');
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.focus(".sql-editor");
  // Run analysis (⇧⌘E, Ctrl+Shift+E elsewhere) and Run all (Ctrl+Shift+Enter)
  // from the keyboard both refuse: the tab is bound to Prod.
  const mod = (await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent))) ? "Meta" : "Control";
  await page.keyboard.down(mod);
  await page.keyboard.down("Shift");
  await page.keyboard.press("e");
  await page.keyboard.up("Shift");
  await page.keyboard.up(mod);
  await page.waitForFunction(() => /came from Top statements on Prod \(analyst@db\.prod:3306\)/.test(
    document.querySelector(".query-lab [role=alert]")?.textContent || ""));
  assert.equal(calls("/api/live/start").length, startsBefore, "Run analysis with the keyboard sends nothing to Staging");
  await page.keyboard.down("Control");
  await page.keyboard.down("Shift");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Shift");
  await page.keyboard.up("Control");
  await pause(200);
  assert.match(await text(".query-lab [role=alert]"), /came from Top statements on Prod/);
  assert.equal(calls("/api/editor/run").length, 0, "Run all with the keyboard sends nothing to Staging");
  await clickExact(".editor-actions button", "Run analysis");
  await pause(300);
  assert.equal(calls("/api/live/start").length, startsBefore, "nor does the button");
  assert.match(await text(".query-lab .tab-target"), /Use this connection/);
  // Restored: the tab still knows where it came from.
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved", { timeout: 20000 });
  await page.reload({ waitUntil: "networkidle0" });
  await nav("Query editor");
  await page.waitForSelector(".query-lab .tab-target");
  await clickExact(".editor-actions button", "Run analysis");
  await pause(300);
  assert.equal(calls("/api/live/start").length, startsBefore, "a restored tab is still bound");
  // An explicit choice moves it to the connection in use.
  await page.locator(".query-lab .tab-target button ::-p-text(Use this connection)").click();
  await page.waitForFunction(() => !document.querySelector(".query-lab .tab-target"));
  hold.add("/api/live/start");
  await clickExact(".editor-actions button", "Run analysis");
  const rebound = await held("/api/live/start");
  assert.deepEqual([rebound.body.connection.profile_id, rebound.body.source.digest, rebound.body.mode],
    ["p2", PARAMETERIZED, "analyze"]);
  answer(rebound, [400, { error: "Could not complete the request: not run in this test." }]);
  hold.delete("/api/live/start");

  // 13. Save connection runs once per click burst; a test answer belongs to the settings it tested.
  await nav("Home");
  await page.locator("button ::-p-text(Add connection)").click();
  await page.waitForSelector(".connection-panel");
  await setField("Connection name", "Gamma");
  await setField("Host", "db.gamma");
  hold.add("/api/profiles/save");
  const saves = calls("/api/profiles/save").length;
  await page.evaluate(() => {
    const save = [...document.querySelectorAll(".connection-panel button")].find((b) => b.textContent === "Save connection");
    save.click();
    save.click();
  });
  const save = await held("/api/profiles/save");
  await pause(300);
  assert.equal(calls("/api/profiles/save").length, saves + 1, "one save for two clicks");
  assert.equal(await page.$eval(".connection-panel .button.primary", (e) => e.disabled), true, "Save waits while saving");
  answer(save);
  await page.waitForFunction(() => /Connection "Gamma" saved/.test(document.querySelector(".connections-status")?.textContent || ""));
  hold.delete("/api/profiles/save");
  hold.add("/api/connection/test");
  await page.locator(".connection-panel button ::-p-text(Test connection)").click();
  const test = await held("/api/connection/test");
  assert.equal(test.body.connection.host, "db.gamma");
  await setField("Host", "db.delta");
  answer(test);
  await pause(400);
  assert.doesNotMatch(await page.$eval(".connections", (e) => e.textContent), /Connected · 8\.4\.3/,
    "the answer for db.gamma is not shown next to db.delta");
  assert.equal((await page.$$("#schema-list option")).length, 0);
  await page.locator(".connection-panel button ::-p-text(Test connection)").click();
  const retest = await held("/api/connection/test");
  assert.equal(retest.body.connection.host, "db.delta");
  answer(retest);
  await page.waitForFunction(() => /Connected · 8\.4\.3/.test(document.querySelector(".connections-status")?.textContent || ""));
  assert.equal((await page.$$("#schema-list option")).length, 2);
  await setField("Host", "db.epsilon");
  await page.waitForFunction(() => !/Connected/.test(document.querySelector(".connections")?.textContent || ""));
  hold.delete("/api/connection/test");

  // 13b. A save that answers after a newer Use keeps that newer choice (here
  // from an unsaved connection in use, which a first save would otherwise replace).
  await backToList("connections");
  await page.locator("button ::-p-text(Add connection)").click();
  await setField("Connection name", "Zeta");
  await setField("Host", "db.zeta");
  await page.locator(".connection-panel button ::-p-text(Use without saving)").click();
  // The list is hidden while the form is open: read the connection in use
  // from the autosaved workspace until returning to the list.
  const inUse = async (pattern) => {
    for (let i = 0; i < 100; i++) {
      const host = await page.evaluate(async () => {
        const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
        const saved = await (await fetch("/api/session/load", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}" })).json();
        return saved.state?.query_lab?.connection?.host || "";
      });
      if (pattern.test(host)) return;
      await pause(100);
    }
    assert.fail(`the connection in use never matched ${pattern}`);
  };
  await inUse(/db\.zeta/);
  hold.add("/api/profiles/save");
  await page.locator(".connection-panel button ::-p-text(Save connection)").click();
  const lateSave = await held("/api/profiles/save");
  await setField("Host", "db.eta");
  await page.locator(".connection-panel button ::-p-text(Use without saving)").click();
  await inUse(/db\.eta/);
  answer(lateSave);
  await page.waitForFunction(() => /Connection "Zeta" saved/.test(document.querySelector(".connections-status")?.textContent || ""));
  await pause(300);
  await page.click(".connections .detail-back");
  await page.waitForSelector(".connection-row.is-active [data-col=endpoint]");
  assert.match(await text(".connection-row.is-active [data-col=endpoint]"), /db\.eta/, "the connection chosen later stays in use");
  hold.delete("/api/profiles/save");
  await use("p2");

  // 13c. A row's Test answer belongs to the settings it tested.
  if (await page.$(".connections .detail-back")) await page.click(".connections .detail-back");
  hold.add("/api/connection/test");
  await connectionAction(page, "Prod", "Test");
  const rowTest = await held("/api/connection/test");
  assert.equal(rowTest.body.connection.host, "db.prod");
  await backToList("connections");
  await connectionAction(page, "Prod", "Edit");
  await setField("Host", "db.prod2");
  await page.locator(".connection-panel button ::-p-text(Save connection)").click();
  await page.waitForFunction(() => /Connection "Prod" saved/.test(document.querySelector(".connections-status")?.textContent || ""));
  answer(rowTest);
  await pause(400);
  assert.doesNotMatch(await text(".connection-panel"), /Connected/, "db.prod's answer is not shown on db.prod2");
  // The row shows the saved endpoint once we return from the form.
  await page.click(".connections .detail-back");
  await page.waitForSelector(".connection-row [data-col=endpoint]");
  const prodRow = () => page.$$eval(".connection-row", (rows) =>
    rows.find((r) => r.querySelector("strong")?.textContent === "Prod")?.textContent || "");
  assert.match(await prodRow(), /db\.prod2/);
  hold.delete("/api/connection/test");
  await connectionAction(page, "Prod", "Test");
  await page.waitForFunction(() => /Connected · 8\.4\.3/.test(document.querySelector(".connection-panel .row-test")?.textContent || ""));

  // 13d. The database list is cached per connection identity, CA file included.
  await use("p1");
  await nav("Server");
  await openView("indexes");
  await until(() => calls("/api/live/schemas").some((c) => c.body.connection.profile_id === "p1" && !c.body.connection.ssl_ca),
    "the database list was not read");
  await nav("Home");
  await backToList("connections");
  await connectionAction(page, "Prod", "Edit");
  await setField("CA file path", "/etc/ca-b.pem");
  await page.locator(".connection-panel button ::-p-text(Use)").click();
  await nav("Server");
  await until(() => calls("/api/live/schemas").some((c) => c.body.connection.ssl_ca === "/etc/ca-b.pem"),
    "another CA file is another connection: its database list is read again");
  await use("p2");

  // 14. Restore sends each plan's text once (capture.raw, run plans and the
  // trace are put back on the result), and a plan that cannot be reopened
  // stays in the saved workspace until it is removed.
  const token = await page.evaluate(async () => (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token);
  const call = (route, body) => page.evaluate(async (t, r, b) => (await fetch(r, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(b),
  })).json(), token, route, body);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved", { timeout: 20000 });
  const stored = await call("/api/session/load", {});
  const captured = stored.state.plans.filter((p) => p.capture);
  assert.equal(captured.length, 3);
  stored.state.plans[0].capture.trace = { data: { steps: [{ marker: "kept" }] }, missing_bytes: 0, insufficient_privileges: 0 };
  stored.state.plans[0].capture.runs[0].plan = stored.state.plans[0].raw;
  stored.state.plans.push({ id: "broken-plan", name: "Broken", raw: '{"not": "a plan"}', capture: null, notes: "" });
  await call("/api/session/save", { revision: stored.revision, client_id: "binding-suite", seq: 1, state: stored.state });
  flags.recordAnalyze = true;
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".held-notice");
  assert.match(await text(".held-notice"), /One plan from your saved workspace could not be reopened\. It stays in the saved workspace\./);
  const restoredCaptures = analyzed.filter((b) => b.capture);
  assert.equal(restoredCaptures.length, 3);
  for (const body of restoredCaptures) {
    assert.equal(body.capture.raw, "", "the plan text is sent once");
    assert.ok(body.capture.runs.every((r) => r.plan === ""), "run plans are not sent");
    assert.equal(body.capture.trace, null, "the trace is not sent");
  }
  flags.recordAnalyze = false;
  // The next autosave keeps the plan it could not reopen, and every capture whole.
  await nav("Server");
  let saved;
  for (let i = 0; i < 100; i++) {
    saved = await call("/api/session/load", {});
    if (saved.state?.mode === "server") break;
    await pause(100);
  }
  assert.equal(saved.state.mode, "server");
  assert.ok(saved.state.plans.some((p) => p.id === "broken-plan"), "autosave keeps the plan it could not reopen");
  const reopened = saved.state.plans.find((p) => p.id === stored.state.plans[0].id);
  assert.equal(reopened.capture.raw, reopened.raw, "capture.raw is put back");
  assert.equal(reopened.capture.runs[0].plan, reopened.raw, "run plans are put back");
  assert.deepEqual(reopened.capture.trace?.data, { steps: [{ marker: "kept" }] }, "the trace is put back");
  await page.locator(".held-notice button ::-p-text(Remove it)").click();
  await page.waitForFunction(() => !document.querySelector(".held-notice"));
  for (let i = 0; i < 100; i++) {
    saved = await call("/api/session/load", {});
    if (!saved.state.plans.some((p) => p.id === "broken-plan")) break;
    await pause(100);
  }
  assert.equal(saved.state.plans.some((p) => p.id === "broken-plan"), false, "Remove it drops it from the saved workspace");

  assert.deepEqual(errors, []);
  console.log("Connection binding checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  server.kill();
}
