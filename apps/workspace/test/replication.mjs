import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
// Replication view of the Server place (Workload, after Sessions): mocked
// /api/live/replication with replication-1.0 reports recorded live (MySQL
// 8.4.11 and MariaDB 11.4.13 replicas; fixtures written by ops-fixtures.py),
// the skip and skip loop endpoints answered from the same recordings, and the
// real Python server for the workspace session, saved connections and the
// audit trail. Covers: nothing read until Load, the channel table and the
// inspector, gates (no privilege, not a replica), the skip confirmation with
// the exact statements, Cancel and Escape recorded as declined, a changed or
// expired plan planned again before anything is sent, the skip result, the
// skip loop (none preselected, start, status, Stop from the view and from
// another view), "stopped when myflames restarted" after a reload, a change
// of the connection's type keeping the loop's Stop, greyed reports after a
// connection switch, MariaDB connections, and 390 px.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege as assertPrivilegeState, privilegeAnswer, load, open, pause } from "./perf-harness.mjs";

// Older live recordings predate receiver coverage. Add explicitly synthetic
// coverage, leaving their recorded transaction identities and SQL untouched.
function receiverGuard(engine, enabled) {
  const instrument = engine === "mariadb" ? "stage/sql/Checking master version" : "stage/sql/Checking source version";
  return {
    receiver_guard: enabled ? "on" : "off",
    receiver_guard_reason: enabled ? null : "Receiver stage instrumentation is off.",
    receiver_guard_instrument: instrument,
    receiver_guard_note: enabled ? "Receiver reconnects are detected; a changed receiver epoch stops the loop." :
      `Receiver reconnects are not detected: enable ${instrument}, global_instrumentation and thread_instrumentation, and allow reads of the receiver stage counters. Performance Schema must be on. All other loop guards remain active.`,
  };
}
function withReceiverGuard(report, enabled) {
  const next = structuredClone(report);
  const guard = receiverGuard(report.engine, enabled);
  Object.assign(next, guard);
  for (const channel of next.channels) if (channel.skip_plan) Object.assign(channel.skip_plan, guard, { review_token: "review-skip" });
  return next;
}

// Retain the shared semantic/focus checks, then check the touch-visible feedback.
async function assertPrivilege(page, selector, state, need) {
  await assertPrivilegeState(page, selector, state, need);
  const feedback = await page.$eval(selector, (button, state) => {
    const wrapper = button.closest('.visible-privilege');
    const note = wrapper
      ? wrapper.querySelector(state === 'unknown' ? '.privilege-status-note' : '.account-sr')
      : button.closest('.sheet-actions')?.querySelector('.privilege-confirm-note');
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 1 && rect.height > 1 && style.clipPath === 'none' && style.visibility !== 'hidden';
    };
    return { visible: visible(note), outside: !!note && !button.contains(note), text: note?.textContent || '',
      inlineUnknown: [...button.querySelectorAll('.privilege-unverified')].some(visible) };
  }, state);
  assert.equal(feedback.inlineUnknown, false, 'status never appears inside the action button');
  if (state === 'unknown' || state === 'denied') {
    assert.equal(feedback.visible, true, 'the gate has visible secondary text');
    assert.equal(feedback.outside, true, 'feedback sits beside or below the button');
    if (state === 'unknown') assert.equal(feedback.text, 'Unverified');
    else if (need) assert.ok(feedback.text.includes(need), feedback.text);
  }
}

const error = withReceiverGuard(await load("replication-mysql-8.4-gtid-error.json"), false);
const healthy = await load("replication-mysql-8.4-healthy.json");
const multi = await load("replication-mysql-8.4-multi-error.json");
const maria = withReceiverGuard(await load("replication-mariadb-11.4-error.json"), false);
// Recorded live: worker 1 failed with 1062, worker 2 then stopped with 3030 and the status reports the 3030.
const behind = await load("replication-mysql-8.4-mta-3030-error.json");
const source = await load("replication-mysql-8.4-source.json");
const denied = await load("replication-mysql-8.4-denied.json");
const skipped = await load("replication-mysql-8.4-skip.json");
const loopRunning = { ...await load("replication-loop-running.json"), ...receiverGuard("mysql", false) };
const loopStopped = await load("replication-loop-stopped.json");
const instruments = await load("instruments-mysql-8.4.json");
instruments.features.push({ key: "replication_receiver", label: "Replication: receiver reconnect guard", view: "replication",
  status: "off", explain: "Detect receiver reconnects during Skip until stopped.",
  missing: ["Receiver stage instrumentation is off"], optional_missing: [], rows: ["group:stage"] });

const STATUS = "/api/live/replication/skip-loop/status";
const START = "/api/live/replication/skip-loop/start";
const STOP = "/api/live/replication/skip-loop/stop";
const SKIP = "/api/live/replication/skip";
const DECLINED = "/api/audit/declined";
/** The loops this myflames holds, by endpoint (host:port:user), like SkipLoopRegistry. */
const registry = new Map();
const endpoint = (c) => `${c?.host}:${c?.port}:${c?.user}`;
/** While true, loop status fails as if myflames were restarting. */
let restarting = false;
const h = await open("/api/live/replication", "replication", {
  [STATUS]: (body) =>
    restarting
      ? { status: 502, body: { error: "myflames is restarting." } }
      : { status: 200, body: { schema_version: "replication-skip-loop-1.0", loops: registry.get(endpoint(body.connection)) || [] } },
  [START]: (body) => {
    assert.ok(["on", "off"].includes(body.receiver_guard), "Start echoes the confirmed coverage mode");
    const running = { ...loopRunning, ...receiverGuard("mysql", body.receiver_guard === "on") };
    registry.set(endpoint(body.connection), [running]);
    return { status: 200, body: running, delay: 50 };
  },
  "/api/live/instruments": () => ({ status: 200, body: instruments }),
  [STOP]: (body) => {
    registry.set(endpoint(body.connection), [loopStopped]);
    return { status: 200, body: loopStopped };
  },
  [SKIP]: () => ({ status: 200, body: skipped, delay: 50 }),
  // Declined sheets reach the real audit endpoint of the isolated store.
  [DECLINED]: (body, request) => {
    void request.continue();
    return null;
  },
}, { fallback: { status: 200, body: denied } });
// The view reads once on its own at start-up (auto-load); later reads are queued per step.
const { page, state, texts } = h;
/** A channel row in the visible copy of the view (a hidden copy is retained). */
const waitRow = (name) => page.waitForFunction((v, key) => [...document.querySelectorAll(v)].some((e) => e.offsetParent && e.querySelector(`[data-row-key="channel:${key}"]`)), {}, view, name);
// A new connection mounts the view again on its default Topology Map tab; the
// table-based checks below use Channels & Status.
const channelsTab = () => page.evaluate(() => {
  const tab = [...document.querySelectorAll('[aria-label="Replication sub-tabs"] button')].find((b) => b.offsetParent && b.textContent.startsWith("Channels"));
  if (tab) tab.click();
});
// The workspace retains a hidden copy of the view: read the visible match.
const text = (selector) => page.evaluate((q, v) => {
  if (q.startsWith(v)) {
    const all = [...document.querySelectorAll(v)];
    const live = all.find((e) => e.offsetParent) || all[0];
    const rest = q.slice(v.length).trim();
    return (rest ? live.querySelector(rest) : live).textContent;
  }
  return document.querySelector(q).textContent;
}, selector, view);
state.fallback = null;
state.seen.length = 0;
const view = '.server-view[data-view="replication"]';
const sheet = "dialog.ops-confirm[open]";
const confirmButton = `${sheet} .sheet-actions button:last-of-type`;
const cancelButton = `${sheet} .sheet-actions button:first-child`;
const row = (name) => `${view} [data-row-key="channel:${name}"]`;
const calls = (p) => state.other.filter((o) => o.path === p);
async function settle(p, n, message) {
  for (let i = 0; i < 100 && calls(p).length < n; i++) await pause(50);
  assert.equal(calls(p).length, n, message || `${p} called ${n} times`);
}
const audit = () =>
  page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch("/api/audit/list", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}",
    });
    return (await response.json()).records;
  });
/** The audit records once at least *n* exist (a declined sheet is recorded in the background). */
async function recorded(n) {
  let records = [];
  for (let i = 0; i < 100 && records.length < n; i++) {
    records = await audit();
    if (records.length < n) await pause(50);
  }
  assert.ok(records.length >= n, `${n} audit records`);
  return records;
}
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
const action = (label) => page.locator(`.view-inspector button ::-p-text(${label})`);
const actionDisabled = (label) =>
  page.$$eval(".view-inspector .inspector-actions button, .view-inspector button", (els, l) => els.find((e) => e.textContent.startsWith(l))?.matches(":disabled, [aria-disabled=true]"), label);
const script = (statements) => statements.map((s) => `/*myflames:replication-skip*/ ${s}`).join(";\n") + ";";
async function openSheet(label) {
  const reads = state.seen.length;
  await action(label).click();
  await page.waitForSelector(sheet);
  assert.equal(state.seen.length, reads + 1, "the failure is read again before the sheet opens");
}
async function closedSheet() {
  await page.waitForFunction(() => !document.querySelector("dialog.ops-confirm"));
}
/** The report with the failing transaction moved on to another GTID (someone skipped it). */
function movedOn(report, n) {
  const next = structuredClone(report);
  const swap = (text) => (typeof text === "string" ? text.replaceAll("306a:13", `306a:${n}`) : text);
  const channel = next.channels[0];
  channel.failing_gtid = swap(channel.failing_gtid);
  channel.skip_plan.identity.gtid = swap(channel.skip_plan.identity.gtid);
  channel.skip_plan.statements = channel.skip_plan.statements.map(swap);
  channel.skip_plan.skip.gtid = swap(channel.skip_plan.skip.gtid);
  return next;
}

try {
  // A saved Development connection to switch to later (the page starts on the unsaved default).
  const replica = await api("/api/profiles/save", {
    value: { name: "Replica", host: "replica.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development" },
  });
  await api("/api/profiles/save", {
    value: { name: "Archive", host: "archive.test", port: 3307, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" },
  });
  state.queue.push({ status: 200, body: denied });
  await page.reload({ waitUntil: "networkidle0" });
  // Replication sits in its own top-level place in the main navigation.
  await page.waitForSelector('.main-nav button[data-place="replication"]');
  // A view that is not on screen reads nothing at startup (ViewOnScreenContext, viewOnScreen.ts).
  await pause(500);
  assert.equal(state.seen.length, 0, "Replication reads nothing while its place is not on screen");
  // Opening the view reads once (auto-load); the first answer is the denied one.
  await h.view("replication");
  // The Topology Map tab is the default; the table-based checks below use Channels & Status.
  await page.locator('[aria-label="Replication sub-tabs"] button ::-p-text(Channels & Status)').click();
  // No privilege: one sentence from the server, no table, no loop status.
  await page.waitForSelector(`${view} .gate`);
  assert.equal(state.seen.length, 1, "one read when the view opens");
  assert.match(await text(`${view} .gate`), /REPLICATION CLIENT or SUPER/);
  assert.match(await text(`${view} .summary-line`), /Replication status not available/);
  assert.match(await h.popover("About Replication"), /Audit log/);
  state.queue.push({ status: 200, body: source });
  await h.refresh();
  await page.waitForFunction((v) => /not a replica/.test(document.querySelector(`${v} .gate`)?.textContent || ""), {}, view);
  assert.match(await text(`${view} .summary-line`), /Not a replica/);
  assert.equal(calls(STATUS).length, 0);

  // The recorded duplicate key failure on the default channel.
  state.fallback = { status: 200, body: error };
  await h.refresh();
  await waitRow("");
  await h.idle(view);
  assert.equal(await text(`${view} .summary-text`), "MySQL 8.4.11 · 1 channel · 1 failed · lag unknown");
  const cells = await page.$eval(row(""), (e) => [...e.querySelectorAll("td")].map((td) => td.textContent));
  assert.deepEqual(cells.slice(0, 7), ["(default)", "Problem", "mf-repl-g-src:3306", "Yes", "No", "Unknown", "1062"]);
  await page.waitForSelector('.main-nav button[data-place="replication"] .count-badge');
  assert.equal(await text('.main-nav button[data-place="replication"] .count-badge'), "1");
  await settle(STATUS, 1, "loop status is read once the replica is shown");
  await pause(2500);
  assert.equal(calls(STATUS).length, 1, "no polling while no loop runs");

  // Topology Map: a static diagram. State is a line style plus a text label; nothing loops.
  const topology = ".replication-topology-container";
  const tab = (label) => page.evaluate((l) => {
    [...document.querySelectorAll('[aria-label="Replication sub-tabs"] button')].find((b) => b.offsetParent && b.textContent.startsWith(l)).click();
  }, label);
  const infinite = () => page.evaluate((q) => {
    const root = [...document.querySelectorAll(q)].find((e) => e.offsetParent);
    return document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations === Infinity && root.contains(a.effect.target)).length;
  }, topology);
  await tab("Topology Map");
  await page.waitForFunction((q) => [...document.querySelectorAll(q)].some((e) => e.offsetParent && e.querySelector(".topology-link")), {}, topology);
  const label = () => page.evaluate((q) => {
    const root = [...document.querySelectorAll(q)].find((e) => e.offsetParent);
    const link = root.querySelector(".topology-link");
    const box = link.querySelector(".topology-link-state").getBoundingClientRect();
    return { text: link.querySelector(".topology-link-state").textContent, cls: link.className, visible: box.width > 0 && box.height > 0 };
  }, topology);
  const failing = await label();
  assert.equal(failing.text, "Error 1062", "the error state shows a visible text label");
  assert.ok(failing.visible && failing.cls.includes("is-error"));
  assert.equal(await infinite(), 0, "no endless animation while a channel has an error");
  assert.equal(await page.evaluate((q) => [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelectorAll(".topology-map svg, [class*=packet], [class*=radar]").length, topology), 0, "no packets or radar pulses");
  assert.equal(await page.evaluate((q) => [...document.querySelectorAll(`${q} button`)].filter((b) => /pause|play|speed|0\.5|2×|1×/i.test(b.textContent + (b.getAttribute("aria-label") || ""))).length, topology), 0, "no play, pause or speed control");
  // A stopped thread without an error: dashed line, the stopped thread named.
  const stopped = structuredClone(error);
  stopped.channels[0].sql.error = { ...stopped.channels[0].sql.error, errno: 0, message: null };
  stopped.channels[0].io.running = "No";
  stopped.channels[0].sql.running = "Yes";
  state.queue.push({ status: 200, body: stopped });
  await h.refresh();
  await page.waitForFunction((q) => [...document.querySelectorAll(q)].some((e) => e.offsetParent && /IO thread stopped/.test(e.textContent)), {}, topology);
  const idle = await label();
  assert.ok(idle.cls.includes("is-stopped") && idle.visible, "stopped thread drawn as a dashed connector with its label");
  assert.equal(await page.evaluate((q) => getComputedStyle([...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-link.is-stopped .topology-line")).borderTopStyle, topology), "dashed");
  assert.equal(await infinite(), 0, "no endless animation while a thread is stopped");
  await tab("Channels & Status");
  state.queue.push({ status: 200, body: error });
  await h.refresh();
  await waitRow("");

  // The inspector: threads, the error once, lag signals and positions in words.
  await h.select(row(""));
  const inspector = await h.inspector();
  assert.match(inspector, /\(default\)Problem/);
  assert.match(inspector, /SQL threadNo/);
  assert.match(inspector, /Error 1062 at 261015 16:11:00/);
  assert.match(inspector, /Tableshop\.t/);
  assert.match(inspector, /Failing GTID7256d98a-c8b1-11f1-be59-aa9276ea306a:13/);
  assert.match(inspector, /Heartbeat interval \(s\)30/);
  assert.match(inspector, /Executed GTID set/);
  const shown = await page.$eval(".view-inspector", (e) => e.innerText);
  assert.equal((shown.match(/Worker 1 failed executing transaction/g) || []).length, 1, "the error text is shown once");
  assert.equal(await actionDisabled("Skip this transaction"), false);
  assert.equal(await actionDisabled("Skip until stopped"), false);

  // Skip this transaction: the exact statements, Cancel focused first.
  const plan = error.channels[0].skip_plan;
  await openSheet("Skip this transaction");
  assert.equal(await text(`${sheet} .eyebrow`), "SKIP TRANSACTION");
  assert.equal(await text(`${sheet} h2`), "Skip the failed transaction on the default channel?");
  assert.equal(await text(`${sheet} .ops-commands code`), script(plan.statements));
  assert.match(await text(`${sheet} .ops-drift`), /missing on the replica/);
  assert.equal(await page.$(`${sheet} .ops-receiver-guard`), null, "single skip has no loop coverage note");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel");
  // Escape sends nothing and is recorded as declined, with the statements shown.
  await page.keyboard.press("Escape");
  await closedSheet();
  await settle(DECLINED, 1);
  const declined = calls(DECLINED)[0].body;
  assert.equal(declined.operation, "replication_skip");
  assert.deepEqual(declined.statements, plan.statements.map((s) => `/*myflames:replication-skip*/ ${s}`));
  assert.equal(declined.target.gtid, plan.identity.gtid);
  const records = await recorded(1);
  assert.deepEqual([records[0].status, records[0].operation], ["declined", "replication_skip"]);
  // Cancel too.
  await openSheet("Skip this transaction");
  await page.click(cancelButton);
  await closedSheet();
  await settle(DECLINED, 2);
  assert.deepEqual(calls(DECLINED)[1].body.statements, declined.statements);
  assert.equal(calls(SKIP).length, 0, "nothing was sent");

  // A plan that changed between the sheet and the click is shown again; nothing is sent.
  const moved = movedOn(error, 14);
  await openSheet("Skip this transaction");
  state.queue.push({ status: 200, body: moved });
  await page.click(confirmButton);
  await page.waitForSelector(`${sheet} .ops-notice`);
  assert.match(await text(`${sheet} .ops-notice`), /plan was refreshed.*Nothing was sent\./);
  assert.match(await text(`${sheet} .ops-commands code`), /306a:14'/);
  assert.equal(calls(SKIP).length, 0);
  // Confirming the refreshed plan sends it, then reads the replica again.
  state.fallback = { status: 200, body: moved };
  state.queue.push({ status: 200, body: moved }, { status: 200, body: healthy });
  await page.click(confirmButton);
  await settle(SKIP, 1);
  const sent = calls(SKIP)[0].body;
  assert.equal(sent.channel, "");
  assert.equal(sent.confirmed, true);
  assert.equal(sent.review_token, "review-skip");
  assert.deepEqual(sent.expect, moved.channels[0].skip_plan.identity);
  assert.deepEqual(sent.statements, moved.channels[0].skip_plan.statements);
  assert.equal(sent.connection.host, "127.0.0.1");
  assert.equal("password" in sent.connection ? typeof sent.connection.password : "string", "string");
  await closedSheet();
  await page.waitForSelector(`${view} .ops-result[data-status=skipped]`);
  const result = await text(`${view} .ops-result`);
  assert.match(result, /^Skipped/);
  assert.match(result, /Skipped transaction 7256d98a-c8b1-11f1-be59-aa9276ea306a:13/);
  assert.match(result, /moved past the skipped transaction/);
  await page.waitForFunction((r) => /OK/.test(document.querySelector(r)?.textContent || ""), {}, row(""));
  assert.equal(await page.$('.main-nav button[data-place="replication"] .count-badge'), null, "the badge follows the new report");

  // An expired plan (older than a minute) is read and shown again before anything is sent.
  state.fallback = { status: 200, body: error };
  await h.refresh();
  await page.waitForFunction((r) => /Problem/.test(document.querySelector(r)?.textContent || ""), {}, row(""));
  await h.select(row(""));
  await openSheet("Skip this transaction");
  await page.evaluate(() => {
    const real = Date.now.bind(Date);
    window.__realNow = real;
    Date.now = () => real() + 61_000;
  });
  await page.click(confirmButton);
  await page.waitForSelector(`${sheet} .ops-notice`);
  assert.equal(calls(SKIP).length, 1, "an expired plan sends nothing");
  await page.evaluate(() => { Date.now = window.__realNow; });
  await page.keyboard.press("Escape");
  await closedSheet();
  await settle(DECLINED, 3);

  // Skip until stopped: no error code is chosen for you; a cap is required.
  const statusBefore = calls(STATUS).length;
  await openSheet("Skip until stopped");
  assert.equal(await text(`${sheet} .eyebrow`), "SKIP UNTIL STOPPED");
  assert.equal((await text(`${sheet} .ops-receiver-guard`)).replace(/\s+/g, " ").trim(), `${plan.receiver_guard_note} How to turn it on`);
  assert.equal(await page.$eval(`${sheet} .ops-drift + .ops-receiver-guard`, e => !!e), true, "coverage immediately follows the drift warning");
  assert.ok(await page.$(`${sheet} [data-instruments-row="feature:replication_receiver"]`));
  const boxes = await page.$$eval(`${sheet} .ops-loop-options input[type=checkbox]`, (els) => els.map((e) => [e.closest("label").textContent, e.checked]));
  assert.deepEqual(boxes, [["1062 Duplicate key", false], ["1032 Row not found", false], ["1452 Foreign key child row", false]]);
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true, "Start waits for a chosen error");
  await page.click(`${sheet} .ops-loop-options label:first-of-type input`);
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), false);
  const skipsField = `${sheet} .ops-loop-options label:nth-of-type(4) input`;
  await page.click(skipsField, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true, "the cap is required");
  await page.type(skipsField, "2");
  await page.click(confirmButton);
  await settle(START, 1);
  const startBody = calls(START)[0].body;
  assert.deepEqual([startBody.channel, startBody.codes, startBody.max_skips, startBody.max_minutes, startBody.confirmed], ["", [1062], 2, 10, true]);
  assert.equal(startBody.receiver_guard, "off");
  await closedSheet();
  await page.waitForSelector(`${view} .ops-loop[data-loop=running]`);
  assert.match(loopRunning.message, /^Skipping 1062 on channel '': 1 skipped since \d\d:\d\d UTC\.$/);
  assert.ok((await text(`${view} .ops-loop`)).startsWith(loopRunning.message), "the loop's own message");
  assert.match(await text(`${view} .ops-loop`), /Last skip: 7256d98a-c8b1-11f1-be59-aa9276ea306a:16/);
  assert.match(await text(`${view} .ops-loop`), /Reconnect detection off/);
  // Status is read while it runs; a running loop holds the channel's skip actions.
  await settle(STATUS, statusBefore + 2, "loop status is read while the loop runs");
  assert.equal(await actionDisabled("Skip this transaction"), true);
  assert.equal(await actionDisabled("Skip until stopped"), true);
  // The workspace remembers only that a loop was running.
  const saved = await h.savedSlow((s) => s.replication_loop_active === true);
  assert.equal(JSON.stringify(saved.state).includes(loopRunning.job_id), false, "no loop id in the session");

  // Losing skip privileges never takes away Stop for an authorized loop.
  await h.setPrivileges({ skip_gtid: privilegeAnswer("denied", "REPLICATION_SLAVE_ADMIN") });
  assert.equal(await page.$eval(`${view} .ops-loop button`, e => e.disabled || e.getAttribute("aria-disabled") === "true"), false);
  // From another view the loop stays visible with its Stop.
  await h.view("health");
  await page.waitForSelector(".server-content > .ops-loop[data-loop=running]");
  assert.match(await text(".server-content > .ops-loop"), /Reconnect detection off/);
  await page.locator(".server-content > .ops-loop button ::-p-text(Stop)").click();
  await settle(STOP, 1);
  assert.equal(calls(STOP)[0].body.job_id, loopRunning.job_id);
  await h.setPrivileges({});
  await page.waitForFunction(() => !document.querySelector(".server-content > .ops-loop"));
  await h.view("replication");
  await page.waitForSelector(`${view} .ops-loop[data-loop=stopped]`);
  assert.match(await text(`${view} .ops-loop`), /Reached the maximum number of skips\. Skipped 2/);
  assert.equal(await page.$(`${view} .ops-loop button`), null, "a stopped loop has no Stop");
  await h.savedSlow((s) => s.replication_loop_active !== true);

  // A loop running when the workspace was saved, and myflames restarted: the
  // registry is empty, nothing starts, and the view says so before any read.
  registry.clear();
  state.fallback = { status: 200, body: withReceiverGuard(error, true) };
  await h.select(row(""));
  await openSheet("Skip until stopped");
  assert.equal(await text(`${sheet} .ops-receiver-guard`), "Stops if a receiver reconnects.");
  assert.equal(await page.$(`${sheet} [data-instruments-row]`), null);
  await page.click(`${sheet} .ops-loop-options label:first-of-type input`);
  // A fresh read losing coverage must preserve the reviewed "on" requirement.
  state.queue.push({ status: 200, body: error });
  await page.click(confirmButton);
  await settle(START, 2);
  assert.equal(calls(START)[1].body.receiver_guard, "on");
  await closedSheet();
  assert.doesNotMatch(await text(`${view} .ops-loop`), /Reconnect detection off/);
  state.fallback = { status: 200, body: error };
  await h.savedSlow((s) => s.replication_loop_active === true);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  // myflames restarts: status fails meanwhile, then answers with an empty registry.
  restarting = true;
  registry.clear();
  await pause(2500);
  await h.savedSlow((s) => s.replication_loop_active === true);
  restarting = false;
  const reads = state.seen.length;
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector(`${view} .ops-note`);
  assert.equal(await text(`${view} .ops-note`), "Stopped when myflames restarted. Skip loops never resume from a restored workspace.");
  // The view reads once on its own when it opens; no skip is sent.
  await h.view("replication");
  await page.locator('[aria-label="Replication sub-tabs"] button ::-p-text(Channels & Status)').click();
  await waitRow("");
  assert.equal(state.seen.length, reads + 1, "one automatic read, nothing more");
  assert.equal(calls(START).length, 2, "a restored workspace never starts a loop");
  await h.savedSlow((s) => s.replication_loop_active !== true);

  // A loop keeps its Stop after the connection's type changes (same endpoint).
  const replicaOption = await connectionOption(page, "Replica");
  await chooseConnection(page, replicaOption);
  await page.waitForFunction(() => /replica\.test/.test(document.querySelector(".window-toolbar .picker-target")?.textContent || ""));
  await h.refresh();
  await waitRow("");
  await h.select(row(""));
  await openSheet("Skip until stopped");
  await page.click(`${sheet} .ops-loop-options label:first-of-type input`);
  await page.click(confirmButton);
  await settle(START, 3);
  assert.equal(calls(START)[2].body.connection.type, "development");
  await page.waitForSelector(`${view} .ops-loop[data-loop=running]`);
  await api("/api/profiles/save", {
    id: replica.id,
    value: { name: "Replica", host: "replica.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production" },
  });
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForSelector('.window-statusbar [data-status="safety"].is-production');
  // The report is from the earlier key: greyed until refreshed; the loop is read again and stays stoppable.
  await page.waitForFunction((v) => [...document.querySelectorAll(v)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  await page.waitForSelector(`${view} .ops-loop[data-loop=running] button`);
  const stops = calls(STOP).length;
  await page.$$eval(`${view} .ops-loop button`, (bs) => bs.find((b) => b.offsetParent && b.textContent === "Stop").click());
  await settle(STOP, stops + 1);
  assert.equal(calls(STOP).at(-1).body.connection.type, "production");
  await page.waitForSelector(`${view} .ops-loop[data-loop=stopped]`);

  // Another connection: the report stays greyed with every skip action off.
  await h.refresh();
  await channelsTab();
  await page.waitForFunction((v) => ![...document.querySelectorAll(v)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  const archiveOption = await connectionOption(page, "Archive");
  await chooseConnection(page, archiveOption);
  await page.waitForFunction((v) => [...document.querySelectorAll(v)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  assert.equal(await page.$(`${view} .ops-loop`), null, "the Replica loops are not shown for Archive");
  assert.match(await text(`${view} .other-connection`), /From .*refresh to load this server/);
  await h.select(row(""));
  assert.equal(await actionDisabled("Skip this transaction"), true);
  assert.equal(await actionDisabled("Skip until stopped"), true);
  assert.equal(await page.$('.main-nav button[data-place="replication"] .count-badge, .main-nav button[data-place="replication"] .status-dot'), null, "no badge from another connection");

  // Server's Refresh all reads replication once and the Replication place
  // shows that answer (it is one report, not one per place): back on
  // Replication nothing is greyed and nothing is read again.
  const beforeAll = state.seen.length;
  await h.server();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(".server-place [data-action=refresh-all]");
  await page.waitForFunction(() => !document.querySelector(".server-place .refresh-all-stop") && /views? refreshed|refreshed/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(state.seen.length, beforeAll + 1, "Refresh all sends one replication read");
  await h.view("replication");
  await channelsTab();
  await page.waitForFunction((v) => ![...document.querySelectorAll(v)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  await pause(300);
  assert.equal(state.seen.length, beforeAll + 1, "opening Replication after Refresh all reads nothing more");

  // Two channels; MariaDB names connections.
  state.fallback = { status: 200, body: multi };
  await h.refresh();
  await channelsTab();
  await waitRow("ch80");
  assert.equal(await text(`${view} .summary-text`), "MySQL 8.4.11 · 2 channels · 1 failed · max lag 0 s");
  state.fallback = { status: 200, body: maria };
  await h.refresh();
  await channelsTab();
  await waitRow("src1");
  assert.equal(await text(`${view} .summary-text`), "MariaDB 11.4.13 · 1 connection · 1 failed · lag unknown");
  assert.equal(await text(`${view} thead th:first-child`).then((t) => t.trim()), "Connection");
  await h.select(row("src1"));
  await openSheet("Skip this transaction");
  assert.equal(await text(`${sheet} h2`), "Skip the failed transaction on connection 'src1'?");
  assert.equal(await text(`${sheet} .ops-commands code`), script(maria.channels[0].skip_plan.statements));
  await page.keyboard.press("Escape");
  await closedSheet();

  // A worker stopped behind a failure (3030): the earlier failure is the one shown and skipped.
  state.fallback = { status: 200, body: behind };
  await h.refresh();
  await channelsTab();
  await waitRow("");
  await h.select(row(""));
  const behindInspector = await h.inspector();
  assert.match(behindInspector, /SQL threadError 3030/);
  assert.match(behindInspector, /Failed firstError 1062 \(worker 1\)/);
  assert.match(behindInspector, /Failing GTIDe5572236-c8c2-11f1-b861-9a23772a56c6:15/);
  await openSheet("Skip this transaction");
  assert.match(await text(`${sheet} .ops-commands code`), /SET GTID_NEXT = 'e5572236-c8c2-11f1-b861-9a23772a56c6:15'/);
  assert.doesNotMatch(await text(`${sheet} .ops-commands code`), /:16'/);
  assert.match(await text(`${sheet} .inspector-fields`), /Error1062 on shop\.t/);
  assert.match(await text(`${sheet} .ops-plan-note`), /3030/);
  await page.keyboard.press("Escape");
  await closedSheet();

  // Canonical method keys gate both recovery entries and confirmations.
  const skipButton = '.view-inspector [data-privilege-action="skip_gtid"]';
  await h.setPrivileges({ skip_gtid: privilegeAnswer("denied", "REPLICATION_SLAVE_ADMIN"),
    replication_status: privilegeAnswer("denied", "REPLICATION CLIENT", { grey_out: false }) });
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, e => e.disabled), false);
  await assertPrivilege(page, skipButton, "denied", "REPLICATION_SLAVE_ADMIN");
  assert.equal(await actionDisabled("Skip until stopped"), true);
  const readsBeforeGate = state.seen.length;
  await action("Skip this transaction").click(); await page.keyboard.press("Enter");
  assert.equal(state.seen.length, readsBeforeGate, "denied entry does not start a preflight");
  await h.setPrivileges({ skip_gtid: privilegeAnswer("allowed") });
  await assertPrivilege(page, skipButton, "allowed");
  await openSheet("Skip this transaction");
  await assertPrivilege(page, confirmButton, "allowed");
  await h.setPrivileges({ skip_gtid: privilegeAnswer("denied", "REPLICATION_SLAVE_ADMIN") });
  await assertPrivilege(page, confirmButton, "denied", "REPLICATION_SLAVE_ADMIN");
  const writesBeforeGate = calls(SKIP).length;
  await page.click(confirmButton); await page.keyboard.press("Space");
  assert.equal(calls(SKIP).length, writesBeforeGate);
  await page.keyboard.press("Escape"); await closedSheet();
  await h.setPrivileges({});
  await assertPrivilege(page, skipButton, "unknown");
  await openSheet("Skip until stopped");
  await page.click(`${sheet} input[type=checkbox]`);
  await assertPrivilege(page, confirmButton, "unknown");
  await h.setPrivileges({ skip_gtid: privilegeAnswer("denied", "SYSTEM_VARIABLES_ADMIN") });
  await assertPrivilege(page, confirmButton, "denied", "SYSTEM_VARIABLES_ADMIN");
  const loopsBeforeGate = calls(START).length;
  await page.click(confirmButton);
  assert.equal(calls(START).length, loopsBeforeGate);
  await page.keyboard.press("Escape"); await closedSheet();
  // MariaDB's counter method must not accidentally use the MySQL GTID key.
  await h.setPrivileges({ skip_gtid: privilegeAnswer("allowed"), skip_counter: privilegeAnswer("denied", "REPLICATION SLAVE ADMIN") });
  state.fallback = { status: 200, body: maria };
  await h.refresh();
  await channelsTab(); await h.idle(view); await h.select(row("src1"));
  await assertPrivilege(page, '.view-inspector [data-privilege-action="skip_counter"]', "denied", "REPLICATION SLAVE ADMIN");
  await h.setPrivileges({});
  await assertPrivilege(page, '.view-inspector [data-privilege-action="skip_counter"]', "unknown");

  // Missing MariaDB coverage is advisory; its link closes and declines the
  // sheet, then selects the dedicated Instruments feature without starting.
  await openSheet("Skip until stopped");
  assert.match(await text(`${sheet} .ops-receiver-guard`), /stage\/sql\/Checking master version/);
  await page.click(`${sheet} input[type=checkbox]`);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), false);
  const startsBeforeLink = calls(START).length;
  const declinesBeforeLink = calls(DECLINED).length;
  await page.click(`${sheet} [data-instruments-row="feature:replication_receiver"]`);
  await closedSheet();
  await settle(DECLINED, declinesBeforeLink + 1);
  assert.equal(calls(START).length, startsBeforeLink);
  // The link opens the Server place on its Instruments row (main.tsx passes
  // onOpenInstruments); it never starts anything.
  await page.waitForSelector("#server-nav-instruments[aria-current=page]");
  await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="instruments"] .view-inspector .inspector-title')]
    .some((e) => !!e.offsetParent && /Replication: receiver reconnect guard/.test(e.textContent || "")));
  assert.equal(calls(START).length, startsBeforeLink);
  await h.view("replication");

  // Phone width: no page scroll.
  // Replication is a place of its own: no list of views to go back to, so the
  // open view fills the window with no horizontal scroll.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  const phone = await page.evaluate(() => {
    // Server can keep its own hidden copy of the view mounted; judge the one on screen.
    const views = [...document.querySelectorAll('.server-view[data-view="replication"]')].filter((e) => !e.closest("[hidden]") && e.offsetParent);
    return {
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      count: views.length,
      back: !!views[0]?.querySelector(".server-back"),
      list: !!document.querySelector(".server-layout.show-list"),
      placeNav: document.querySelector(".main-nav [aria-current=page]")?.textContent?.startsWith("Replication") === true,
    };
  });
  assert.ok(phone.overflow <= 0, `horizontal scroll of ${phone.overflow}px at 390 px`);
  assert.deepEqual({ count: phone.count, back: phone.back, list: phone.list, placeNav: phone.placeNav }, { count: 1, back: false, list: false, placeNav: true });
  await page.screenshot({ path: path.join(h.shots, "replication-390.png"), fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
  await page.screenshot({ path: path.join(h.shots, "replication-1440.png") });
  assert.deepEqual(h.errors, []);
  console.log("replication: ok");
} finally {
  await h.close();
}
