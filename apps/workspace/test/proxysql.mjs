// The ProxySQL place: mocked /api/live/proxysql answering proxysql-1.0
// reports that the real backend computed (proxysql-fixtures.py: the SQLite
// stand-in for ProxySQL Admin shaped like the live 3.0.11 lab), mocked plan
// and action endpoints answering backend-computed plans and results, and the
// real Python server for the session, saved connections and the Audit log.
// Covers: the place right after Cluster with its own icon, nothing read until
// it is on screen, one read on open, the summary, tiles and tabs, the servers
// table and inspector (runtime, pool, configuration row, monitor), changes in
// the inspector only, the review sheet (exact statements, connection, policy,
// undo), Cancel and Escape recorded as declined with nothing sent, the typed
// name, the value sheet, a planning error shown outside any sheet, a
// read-only refusal that opens no sheet, the result panel and the separate
// Save to disk, a lost reply shown as Outcome unknown and never retried, the
// sidebar muted only for the connection that was read, another connection's
// report greyed with its changes off, and the 720x520 window.
// After Confirm the sheet follows the send: Sending… on a button that cannot
// be clicked twice, each statement Sent (never Done) until the server
// answers, then the outcome, what changed, the Audit log record and, for an
// unknown outcome, what to check. Query digests reads on its own only when the
// tab is on screen: the five orders, Top N, hostgroup, schema, user and text
// filters, a digest or rule filter shown as a chip, the rules' destination
// next to the hostgroup a digest ran on with a mismatch flagged, and jumps
// between a rule and its digests.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";
import { transformWithEsbuild } from "vite";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";
import { reviewedActionsUnit } from "./reviewed-actions-unit.mjs";

await reviewedActionsUnit();

// Q2.1d: servingState delegates the status word to the shared
// proxySqlBackendState (the rule the Readyset topology's ProxySQL tier also
// uses); only the offline-hostgroup override and the config-source rule are
// the tier's own.
{
  const source = await fs.readFile(new URL("../src/ProxySqlTopology.tsx", import.meta.url), "utf8");
  const slice = source.slice(source.indexOf("type StatusNode"), source.indexOf("/** True when the same address is ONLINE"));
  const { code } = await transformWithEsbuild(slice, "ProxySqlTopology.tsx", {
    loader: "tsx", format: "cjs", target: "es2020",
  });
  const context = { module: { exports: {} }, exports: {} };
  vm.runInNewContext(code, context);
  const { proxySqlBackendState, servingState } = context.module.exports;
  const node = (status, status_source = "runtime", hostgroup_roles = []) => ({ status, status_source, hostgroup_roles });
  for (const [word, state] of [
    ["ONLINE", "running"],
    ["OFFLINE_HARD", "error"],
    ["SHUNNED", "stopped"],
    ["SHUNNED_REPLICATION_LAG", "stopped"],
    ["OFFLINE_SOFT", "stopped"],
    ["UNKNOWN", "unread"],
    ["PAUSED", "unread"],
    [null, "unread"],
  ]) {
    assert.equal(proxySqlBackendState(word).state, state, `shared rule: ${word}`);
    assert.equal(servingState(node(word)), proxySqlBackendState(word).state, `servingState follows the shared rule for ${word}`);
  }
  assert.equal(servingState(node("ONLINE", "runtime", ["offline"])), "stopped", "an offline-hostgroup ONLINE row is out of traffic");
  assert.equal(servingState(node("ONLINE", "config")), "unread", "a config-only ONLINE row is not read");
  assert.equal(servingState(node(null)), "unread", "an unreported status is not read");
}
// Useful where loopback listeners and browser suites are unavailable.
if (process.argv.includes("--unit")) process.exit(0);

const R = {};
for (const name of ["lab", "pending", "problem", "not", "plan-offline-soft", "plan-offline-hard", "plan-weight", "plan-rule-disable",
  "plan-read-only", "result-done", "plan-save", "digests-time", "digests-slowest", "digests-rate", "digests-hg10", "digests-digest",
  "digests-rule-30", "digests-mismatch", "progress-checking", "progress-sending", "progress-finished-unknown",
  "rules-pending", "collisions", "digests-collisions"]) R[name] = await load(`proxysql-${name}.json`);

const view = '.server-view[data-view="proxysql"]';
const nav = '.main-nav button[data-place="proxysql"]';
const plans = [];
const sends = [];
const declined = [];
const digestReads = [];
const progressReads = [];
let planAnswer = () => ({ status: 200, body: R["plan-offline-soft"] });
let sendAnswer = () => ({ status: 200, body: R["result-done"] });
let progressAnswer = () => ({ status: 200, body: R["progress-sending"] });
let digestOverride = null;
const digestAnswer = (b) => ({ status: 200, body: digestOverride || (b.rule_id === 30 ? R["digests-rule-30"] : b.digest ? R["digests-digest"]
  : b.hostgroup === 10 ? R["digests-hg10"] : b.order === "slowest" ? R["digests-slowest"] : b.order === "rate" ? R["digests-rate"]
    : b.order === "frequent" ? R["digests-mismatch"] : R["digests-time"]) });
const h = await open("/api/live/proxysql", "proxysql", {
  "/api/live/proxysql/plan": (body) => { plans.push(body); return planAnswer(body); },
  "/api/live/proxysql/action": (body) => { sends.push(body); return sendAnswer(body); },
  "/api/live/proxysql/digests": (body) => { digestReads.push(body); return digestAnswer(body); },
  "/api/live/action/progress": (body) => { progressReads.push(body); return progressAnswer(body); },
  "/api/audit/declined": (body) => { declined.push(body); return { status: 200, body: { audit_id: "x", status: "declined" } }; },
});
const { page, state } = h;
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
const visible = (selector) => page.$$eval(selector, (els) => els.filter((e) => e.offsetParent || e.getClientRects().length).map((e) => e.textContent.replace(/\s+/g, " ").trim()));
const shot = (file) => page.screenshot({ path: path.join(h.shots, file), fullPage: false });
async function refreshTo(report) {
  const before = state.seen.length;
  state.fallback = { status: 200, body: report };
  await h.refresh();
  for (let i = 0; i < 50 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh sends one read");
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line`), {}, view);
  await pause(150);
}
async function tab(label) {
  await page.evaluate((label, sel) => [...document.querySelectorAll(`${sel} .segmented button`)].find((b) => b.textContent.trim().startsWith(label)).click(), label, view);
  await pause(200);
}
async function backToList() {
  const close = await page.$(".view-inspector.is-detail .detail-back, .view-inspector.is-sheet .inspector-close");
  if (close) { await close.click(); await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail, .view-inspector.is-sheet")); }
}
async function selectRow(key) {
  await backToList();
  await page.click(rowSelector(key));
  await page.waitForSelector(".view-inspector .inspector-title");
}
// JSON keys contain quotes and backslashes: escape the CSS attribute too.
const rowSelector = (key) => `${view} tbody tr[data-row-key=${JSON.stringify(key)}]`;
const digestKey = (hg, hash, schema = "information_schema", user = "mf_admin", client = "") =>
  `dg${JSON.stringify([hg, schema, user, client, hash])}`;
const inspectorButtons = () => page.$$eval(".view-inspector .review-actions button", (els) => els.map((e) => e.textContent.trim()));
async function clickAction(label) {
  await page.evaluate((label) => [...document.querySelectorAll(".view-inspector .review-actions button")].find((b) => b.textContent.trim() === label).click(), label);
}
const sheet = "dialog.action-review[open]";
async function waitSheet() { await page.waitForSelector(sheet, { visible: true }); }
async function noSheet() { await page.waitForFunction((s) => !document.querySelector(s), {}, sheet); }
/** The sheet after Confirm: wait for the server's answer, then Close. */
async function answered() {
  await page.waitForSelector(`${sheet} .action-outcome`);
  return page.$eval(sheet, (d) => ({
    outcome: d.querySelector(".action-outcome")?.textContent.replace(/\s+/g, " ").trim(),
    steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state),
    effect: [...d.querySelectorAll(".action-effect tbody tr")].map((tr) => [...tr.children].map((c) => c.textContent)),
    text: d.textContent,
    buttons: [...d.querySelectorAll(".sheet-actions button")].map((b) => b.textContent.trim()),
    focused: document.activeElement?.textContent?.trim(),
  }));
}
async function closeSheet() {
  await page.evaluate((s) => [...document.querySelectorAll(`${s} .sheet-actions button`)].find((b) => b.textContent.trim() === "Close").click(), sheet);
  await noSheet();
}
const resultButton = (label) => page.evaluate((sel, label) => [...document.querySelectorAll(`${sel} .action-result .ops-result-head .text-button`)]
  .find((b) => b.textContent.trim() === label).click(), view, label);

try {
  await api("/api/profiles/save", { value: { name: "ProxySQL lab", host: "127.0.0.1", port: 6032, user: "radmin", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development" } });
  await api("/api/profiles/save", { value: { name: "Plain MySQL", host: "mysql.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await page.reload({ waitUntil: "networkidle0" });

  // The place sits right after Cluster with its own glyph, and reads nothing until it is on screen.
  const places = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.equal(places[places.indexOf("cluster") + 1], "proxysql", `ProxySQL follows Cluster: ${places}`);
  assert.equal(await page.$eval(nav, (b) => (b.childNodes[1]?.textContent || "").trim()), "ProxySQL");
  const glyphs = await page.$$eval('.main-nav button[data-place="proxysql"] svg path, .main-nav button[data-place="replication"] svg path',
    (els) => els.map((e) => e.getAttribute("d")));
  assert.ok(glyphs.length >= 2 && new Set(glyphs).size === glyphs.length, "ProxySQL has a glyph of its own");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "before the first read the item looks normal");
  await pause(400);
  assert.equal(state.seen.length, 0, "nothing is read before the place is opened");

  // One read on open; then the saved ProxySQL connection, read with Refresh.
  state.fallback = { status: 200, body: R.lab };
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await pause(200);
  assert.equal(state.seen.length, 1, "opening the place reads once");
  assert.ok(state.seen[0].connection, "the read carries the connection in use");
  await chooseConnection(page, await connectionOption(page, "ProxySQL lab"));
  await pause(300);
  assert.equal(state.seen.length, 1, "switching while on screen keeps the old report until Refresh");
  await refreshTo(R.lab);
  assert.equal(state.seen.at(-1).connection.port, 6032);
  assert.equal(state.seen.at(-1).connection.type, "development");
  assert.match((await visible(`${view} .summary-line`))[0], /ProxySQL 3\.0\.11-797-g7c91137 · up 2 d 4 h · 10 backends in 5 hostgroups · 8 ONLINE, 2 SHUNNED/);
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Servers10", "Hostgroups8", "Query rules3", "Query digests"]);
  const tiles = await page.$$eval(`${view} .metric-tile`, (els) => els.map((e) => ({
    label: e.querySelector(".tile-label")?.textContent?.trim() || "",
    value: e.querySelector(".tile-value")?.textContent?.trim() || "",
    hint: e.querySelector(".tile-hint")?.textContent?.trim() || "",
  })));
  assert.deepEqual(tiles.map((t) => t.label), ["Backends ONLINE", "Hostgroups", "Query rules active", "Client connections"]);
  assert.deepEqual(tiles[1], { label: "Hostgroups", value: "8", hint: "5 with servers · 8 in monitors" });
  assert.equal(tiles[3].hint, "connected now");
  assert.match((await visible(`${view} .proxysql-config`))[0], /Configuration of servers: memory matches runtime · saved to disk/);
  const configButtons = await page.$$eval(`${view} .proxysql-config button`, (els) => els.map((b) => ({
    text: b.textContent.trim(), ghost: b.classList.contains("ghost"), title: b.title,
  })));
  assert.deepEqual(configButtons, [
    { text: "Load servers to runtime…", ghost: true, title: "Nothing to load: memory matches runtime" },
    { text: "Save servers to disk…", ghost: true, title: "Nothing to save: disk matches memory" },
  ]);
  // d06: summary and server status marks use a dot plus a word.
  assert.notEqual(await page.$eval(`${view} .summary-line .status-pill.is-ok .status-dot`, (d) => getComputedStyle(d).display), "none");
  assert.equal(await page.$eval(`${view} .cluster-mark.is-ok .cluster-shape`, (d) => getComputedStyle(d).borderRadius), "50%");
  const info = await h.popover("About ProxySQL");
  assert.match(info, /memory \(what Admin edits\), runtime \(what it uses\) and disk/);
  assert.match(info, /Save to disk is a separate change/);
  const rows = await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.map((e) => e.dataset.rowKey));
  assert.equal(rows.length, 10);
  assert.ok(rows.includes("rt|32|mf-ic-2|3306"));
  const standby = await page.$eval(`${view} tbody tr[data-row-key="rt|10|mf-galera-2|3306"] td[data-col="status"]`, (e) => e.textContent);
  assert.equal(standby, "SHUNNED (standby writer)");
  const pool = await page.$eval(`${view} tbody tr[data-row-key="rt|32|mf-ic-2|3306"] td[data-col="latency"]`, (e) => e.textContent);
  assert.notEqual(pool, "—", "the connection pool is read");
  await shot("proxysql-servers-1440.png");

  // The inspector: right-hand 320 px pane at 1440 px, list stays mounted with
  // the row selected, and Escape returns focus to the row.
  await selectRow("rt|32|mf-ic-2|3306");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-detail")), false, "right-pane inspector at 1440 px");
  assert.equal(await page.$eval(".view-inspector", (e) => Math.round(e.getBoundingClientRect().width)), 320, "inspector defaults to 320 px");
  assert.equal(await page.$(".view-inspector .detail-back"), null, "no full-width Back button");
  assert.equal(await page.$eval(rowSelector("rt|32|mf-ic-2|3306"), (e) => !!e.offsetParent && e.classList.contains("is-selected")), true, "server list stays mounted with row selected");
  const text = await h.inspector();
  assert.match(text, /Connection pool/);
  assert.match(text, /hostgroup 30, mf-ic-2:3306/);
  assert.match(text, /Group Replication monitor/);
  assert.deepEqual(await inspectorButtons(), ["Set OFFLINE_SOFT…", "Set OFFLINE_HARD…", "Change weight…"], "no move out of a monitor's hostgroups");
  assert.match(await h.inspector(), /Move to hostgroup is not offered: a row moved out of the Group Replication monitor's hostgroups stays/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-row-key") === "rt|32|mf-ic-2|3306");
  await selectRow("rt|11|mf-galera-2|3306");
  assert.deepEqual(await inspectorButtons(), ["Set OFFLINE_HARD…", "Change weight…"], "no OFFLINE_SOFT under the Galera monitor");
  assert.match(await h.inspector(), /OFFLINE_SOFT is not offered: the Galera monitor puts the server back in traffic/);

  // Review, then Escape: recorded as declined, nothing sent.
  await selectRow("rt|32|mf-ic-2|3306");
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  assert.equal(plans.length, 1);
  assert.deepEqual({ action: plans[0].action, target: plans[0].target }, { action: "server_offline_soft", target: { config: [30, "mf-ic-2", 3306] } });
  assert.equal(plans[0].connection.host, "127.0.0.1");
  const review = await page.$eval(sheet, (d) => ({
    eyebrow: d.querySelector(".eyebrow")?.textContent, title: d.querySelector("h2")?.textContent,
    sql: d.querySelector(".ops-pre code")?.textContent, text: d.textContent,
    confirm: d.querySelector(".sheet-actions button:last-child")?.textContent,
    focused: document.activeElement?.textContent }));
  assert.equal(review.eyebrow, "UPDATE");
  assert.equal(review.title, "Set mf-ic-2:3306 to OFFLINE_SOFT?");
  assert.equal(review.sql, R["plan-offline-soft"].statements.join("\n"), "the statements exactly as sent");
  assert.match(review.text, /ConnectionProxySQL lab \(radmin@127\.0\.0\.1:6032\)/);
  assert.match(review.text, /TypeDevelopment/);
  assert.match(review.text, /Applied to runtime only\. Save servers to disk afterwards/);
  assert.match(review.text, /UndoUPDATE mysql_servers SET status = 'ONLINE'/);
  assert.equal(review.confirm, "Set OFFLINE_SOFT");
  assert.equal(review.focused, "Cancel", "Cancel has focus first");
  await pause(400);
  await shot("proxysql-review-1440.png");
  await page.keyboard.press("Escape");
  await noSheet();
  for (let i = 0; i < 50 && !declined.length; i++) await pause(20);
  assert.equal(declined.length, 1, "Escape records a declined attempt");
  assert.deepEqual({ operation: declined[0].operation, statements: declined[0].statements },
    { operation: "proxysql_action", statements: R["plan-offline-soft"].statements });
  assert.equal(sends.length, 0, "nothing sent on Escape");

  // Review, Cancel: declined again.
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  await page.click(`${sheet} .sheet-actions button:first-child`);
  await noSheet();
  for (let i = 0; i < 50 && declined.length < 2; i++) await pause(20);
  assert.equal(declined.length, 2, "Cancel records a declined attempt");

  // Confirm: the sheet follows the send. While the server works on it the
  // button reads Sending… and cannot be clicked twice, Cancel and Escape do
  // nothing, and each statement is Sent, never Done; then the outcome, what
  // changed and the Audit log record. Close leaves the result panel, which
  // offers the separate Save to disk.
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  const readsBefore = state.seen.length;
  progressAnswer = () => ({ status: 200, body: R["progress-checking"] });
  sendAnswer = () => ({ status: 200, body: R["result-done"], delay: 2200 });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction((s) => /Reading the target again/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""), {}, sheet);
  const checking = await page.$eval(sheet, (d) => ({
    confirm: d.querySelector(".sheet-actions button:last-child").textContent, ariaDisabled: d.querySelector(".sheet-actions button:last-child").getAttribute("aria-disabled"),
    cancel: d.querySelector(".sheet-actions button:first-child").disabled, steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state) }));
  assert.deepEqual(checking, { confirm: "Sending…", ariaDisabled: "true", cancel: true, steps: ["waiting", "waiting"] });
  progressAnswer = () => ({ status: 200, body: R["progress-sending"] });
  await page.waitForFunction((s) => /Sent\. Waiting for the server's answer/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""), {}, sheet);
  const sending = await page.$eval(sheet, (d) => ({ steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state),
    words: [...d.querySelectorAll(".action-steps .status-pill")].map((e) => e.textContent), phase: d.querySelector(".action-phase").textContent }));
  assert.deepEqual(sending.steps, ["sent", "sent"], "with the server: sent, never done before it answers");
  assert.deepEqual(sending.words, ["Sent", "Sent"]);
  assert.match(sending.phase, /of at most 60 s/);
  await page.keyboard.press("Escape");
  await page.click(`${sheet} .sheet-actions button:last-child`).catch(() => null);
  await pause(300);
  assert.ok(await page.$(sheet), "Escape does not close a send in flight");
  await pause(200);
  await shot("proxysql-applying-1440.png");
  const done = await answered();
  assert.equal(sends.length, 1, "sent once, however often it was clicked");
  assert.ok(progressReads.length >= 2 && progressReads.every((p) => p.progress_id === sends[0].progress_id), "progress is read by the send's id");
  assert.match(sends[0].progress_id, /^[0-9a-f]{32}$/);
  assert.match(done.outcome, /^Done/);
  assert.deepEqual(done.steps, ["done", "done"]);
  assert.deepEqual(done.effect[0], ["Status in memory", "ONLINE", "OFFLINE_SOFT"]);
  assert.match(done.text, /Recorded in the Audit log|The Audit log has the attempt/);
  assert.deepEqual(done.buttons, ["Save servers to disk…", "Close"], "no Cancel once answered; the next step is its own review");
  assert.equal(done.focused, "Close");
  await shot("proxysql-done-1440.png");
  await closeSheet();
  sendAnswer = () => ({ status: 200, body: R["result-done"] });
  await page.waitForSelector(`${view} .action-result`);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].review_token, R["plan-offline-soft"].review_token);
  assert.equal(sends[0].plan.review_token, undefined, "the token travels apart from the plan");
  assert.deepEqual(sends[0].plan.statements, R["plan-offline-soft"].statements);
  assert.equal(sends[0].confirmed, true);
  const result = await page.$eval(`${view} .action-result`, (e) => e.textContent);
  assert.match(result, /Done/);
  assert.match(result, /Not saved to disk yet/);
  assert.match(result, /Runtime now: hostgroup 32 OFFLINE_SOFT, weight 100/);
  for (let i = 0; i < 50 && state.seen.length === readsBefore; i++) await pause(20);
  assert.equal(state.seen.length, readsBefore + 1, "the place reads again after a change");
  await shot("proxysql-result-1440.png");
  planAnswer = () => ({ status: 200, body: R["plan-save"] });
  await page.click(`${view} .action-result .action-next button`);
  await waitSheet();
  assert.equal(plans.at(-1).action, "servers_save");
  const save = await page.$eval(sheet, (d) => d.textContent);
  assert.match(save, /Save the servers configuration to disk\?/);
  assert.match(save, /1 difference this applies/);
  assert.match(save, /hostgroup 30, mf-ic-2:3306: status ONLINE → OFFLINE_SOFT/);
  await page.keyboard.press("Escape");
  await noSheet();

  // A destructive change needs the typed name before its confirm button works.
  planAnswer = () => ({ status: 200, body: R["plan-offline-hard"] });
  await selectRow("rt|32|mf-ic-2|3306");
  await clickAction("Set OFFLINE_HARD…");
  await waitSheet();
  const confirm = `${sheet} .sheet-actions button:last-child`;
  assert.equal(await page.$eval(confirm, (b) => b.disabled), true, "disabled until the name is typed");
  await page.type(`${sheet} .action-typed input`, "mf-ic-2");
  assert.equal(await page.$eval(confirm, (b) => b.disabled), true);
  await page.type(`${sheet} .action-typed input`, ":3306");
  assert.equal(await page.$eval(confirm, (b) => b.disabled), false);
  await pause(400);
  await shot("proxysql-typed-1440.png");
  await page.keyboard.press("Escape");
  await noSheet();

  // A value first, then the review; an invalid value cannot be reviewed.
  planAnswer = () => ({ status: 200, body: R["plan-weight"] });
  const plansBefore = plans.length;
  await clickAction("Change weight…");
  await page.waitForSelector("dialog.action-value[open]");
  const valueConfirm = "dialog.action-value[open] .sheet-actions button:last-child";
  assert.equal(await page.$eval(valueConfirm, (b) => b.disabled), true, "the current weight is not a change");
  await page.$eval("dialog.action-value[open] input", (i) => { i.select(); });
  await page.keyboard.type("20000000");
  assert.equal(await page.$eval(valueConfirm, (b) => b.disabled), true, "above the maximum");
  await page.$eval("dialog.action-value[open] input", (i) => { i.select(); });
  await page.keyboard.type("50");
  await page.click(valueConfirm);
  await waitSheet();
  assert.equal(plans.length, plansBefore + 1);
  assert.equal(plans.at(-1).value, 50);
  await page.keyboard.press("Escape");
  await noSheet();

  // A planning error shows in the view, outside any sheet.
  planAnswer = () => ({ status: 400, body: { error: "Other server changes wait in memory, and LOAD MYSQL SERVERS TO RUNTIME would apply them too." } });
  await clickAction("Set OFFLINE_SOFT…");
  await page.waitForFunction((sel) => /Other server changes wait in memory/.test(document.querySelector(`${sel}`)?.textContent || ""), {}, view);
  assert.equal(await page.$(sheet), null, "no sheet opens");
  // A read-only connection's plan opens no sheet and says why.
  planAnswer = () => ({ status: 200, body: R["plan-read-only"] });
  await clickAction("Set OFFLINE_SOFT…");
  await page.waitForFunction((sel) => /read-only in myflames/.test(document.querySelector(`${sel}`)?.textContent || ""), {}, view);
  assert.equal(await page.$(sheet), null);

  // The write policy changed after the review: nothing is sent, and the
  // refusal is recorded (reason policy_changed).
  const lab = (await api("/api/profiles/list", {})).items.find((p) => p.name === "ProxySQL lab");
  planAnswer = () => ({ status: 200, body: R["plan-offline-soft"] });
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  const sendsAtPolicy = sends.length;
  const declinedAtPolicy = declined.length;
  await api("/api/profiles/save", { id: lab.id, value: { ...lab, type: "production" } });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  // Either the sheet's own check refused it (its result says Nothing sent),
  // or the app saw the new policy first and closed the review.
  await page.waitForFunction((s) => !document.querySelector(s) || document.querySelector(`${s} .action-outcome`), {}, sheet);
  if (await page.$(`${sheet} .action-outcome`)) {
    assert.match((await answered()).outcome, /Nothing sent/);
    await closeSheet();
  }
  for (let i = 0; i < 50 && declined.length === declinedAtPolicy; i++) await pause(20);
  assert.equal(sends.length, sendsAtPolicy, "nothing sent after a policy change");
  assert.ok(declined.slice(declinedAtPolicy).some((d) => d.reason === "policy_changed" || d.reason === undefined),
    `the stopped review is recorded: ${JSON.stringify(declined.slice(declinedAtPolicy))}`);
  // Either the sheet's own check refused it, or the app saw the new policy
  // first and greyed the report (another connection key).
  assert.match(await page.$eval(view, (e) => e.textContent), /Nothing was sent|refresh to load this server/);
  await api("/api/profiles/save", { id: lab.id, value: { ...lab, type: "development" } });
  await page.reload({ waitUntil: "networkidle0" });
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await refreshTo(R.lab);
  await selectRow("rt|32|mf-ic-2|3306");

  // A lost reply: Outcome unknown, nothing sent again.
  planAnswer = () => ({ status: 200, body: R["plan-offline-soft"] });
  sendAnswer = () => ({ status: 500, body: { error: "connection lost while waiting for a reply" }, delay: 1500 });
  progressAnswer = () => ({ status: 200, body: R["progress-finished-unknown"] });
  const sendsBefore = sends.length;
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  await page.click(`${sheet} .sheet-actions button:last-child`);
  // Finished without an answer never reads as answered.
  await page.waitForFunction((s) => /No answer arrived from the server/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""), {}, sheet);
  const lost = await answered();
  assert.match(lost.outcome, /^Outcome unknown/);
  assert.deepEqual(lost.steps, ["unknown", "unknown"]);
  assert.match(lost.text, /What to check/);
  assert.match(lost.text, /Refresh this place\. Servers shows mf-ic-2:3306/);
  assert.match(lost.text, /Undo, after checking/);
  assert.deepEqual(lost.buttons, ["Close"]);
  await shot("proxysql-unknown-sheet-1440.png");
  await closeSheet();
  await page.waitForFunction((sel) => /Outcome unknown/.test(document.querySelector(`${sel} .action-result`)?.textContent || ""), {}, view);
  await pause(300);
  assert.equal(sends.length, sendsBefore + 1, "never retried");
  assert.match(await page.$eval(`${view} .action-result`, (e) => e.textContent), /whether it ran is unknown/);
  await shot("proxysql-unknown-1440.png");
  // The result's Audit log button opens the Audit log; the place keeps its result.
  await resultButton("Audit log");
  await page.waitForSelector("#audit-title", { visible: true });
  await page.click(nav);
  await page.waitForSelector(`${view} .action-result`, { visible: true });
  await resultButton("Close");
  sendAnswer = () => ({ status: 200, body: R["result-done"] });

  // Query rules and digests.
  await backToList();
  await tab("Query rules");
  planAnswer = () => ({ status: 200, body: R["plan-rule-disable"] });
  await selectRow("rule:30");
  assert.deepEqual(await inspectorButtons(), ["Disable rule…"]);
  await clickAction("Disable rule…");
  await waitSheet();
  assert.deepEqual(plans.at(-1).target, { rule_id: 30 });
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), R["plan-rule-disable"].statements.join("\n"));
  await page.keyboard.press("Escape");
  await noSheet();
  // Rules show their hits and the digests they apply to (evaluated here).
  await backToList();
  assert.match((await visible(`${view} tbody tr[data-row-key="rule:30"] td[data-col="digests"]`))[0], /^5$/);
  assert.match((await visible(`${view} tbody tr[data-row-key="rule:10"] td[data-col="digests"]`))[0], /^0 \+2 maybe$/);
  await selectRow("rule:30");
  assert.match(await h.inspector(), /Digests it applies to/);
  assert.match(await h.inspector(), /SELECT COUNT\(\*\) FROM testdb\.orders WHERE status = \? AND customer_id = \?/);

  // Query digests reads only once its tab is on screen, in the default order.
  await backToList();
  assert.equal(digestReads.length, 0, "no digests read before the tab is open");
  await tab("Query digests");
  await page.waitForSelector(`${view} .proxysql-digest-summary`);
  assert.equal(digestReads.length, 1);
  assert.deepEqual({ order: digestReads[0].order, limit: digestReads[0].limit, hostgroup: digestReads[0].hostgroup },
    { order: "time", limit: 10, hostgroup: null });
  assert.deepEqual(await visible(`${view} .proxysql-digest-tools .segmented button`), ["Slowest", "Most time", "Most frequent", "Highest rate", "Most rows sent"]);
  assert.match((await visible(`${view} .proxysql-digest-summary`))[0], /Top 10 by total time · 10 digests listed/);
  const route = (key) => page.$eval(`${rowSelector(key)} td[data-col="route"]`, (e) => e.textContent);
  assert.equal(await route(digestKey(32, "0xA8027DF9CE26E2FF")), "32 · rule 30");
  assert.equal(await route(digestKey(10, "0xA8027DF9CE26E2FF")), "Maybe rule 10");
  assert.equal(await route(digestKey(30, "0x48BE884F70034786")), "30 · default");
  await shot("proxysql-digests-1440.png");
  // Each order is its own bounded read; the table sorts by that order's column.
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)].find((b) => b.textContent === "Slowest").click(), view);
  for (let i = 0; i < 50 && digestReads.length < 2; i++) await pause(20);
  assert.equal(digestReads.at(-1).order, "slowest");
  await page.waitForFunction((sel) => /Top 10 by average time/.test(document.querySelector(`${sel} .proxysql-digest-summary`)?.textContent || ""), {}, view);
  assert.equal(await page.$eval(`${view} th[aria-sort="descending"]`, (e) => e.textContent.trim()), "Average", "sorted by the order's column");
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)].find((b) => b.textContent === "Highest rate").click(), view);
  for (let i = 0; i < 50 && digestReads.at(-1).order !== "rate"; i++) await pause(20);
  await page.waitForFunction((sel) => /by executions per second/.test(document.querySelector(`${sel} .proxysql-digest-summary`)?.textContent || ""), {}, view);
  assert.match((await visible(`${view} tbody tr[data-row-key] td[data-col="rate"]`))[0], /20\/s/);
  // Top N and the hostgroup filter.
  await page.select(`${view} .proxysql-digest-tools label:not(.proxysql-order-select) select`, "25");
  for (let i = 0; i < 50 && digestReads.at(-1).limit !== 25; i++) await pause(20);
  assert.equal(digestReads.at(-1).limit, 25);
  await page.select(`${view} .proxysql-digest-filters label:nth-of-type(2) select`, "10");
  for (let i = 0; i < 50 && digestReads.at(-1).hostgroup !== 10; i++) await pause(20);
  assert.equal(digestReads.at(-1).hostgroup, 10);
  await page.waitForFunction((sel) => document.querySelectorAll(`${sel} tbody tr[data-row-key]`).length === 2, {}, view);
  await page.select(`${view} .proxysql-digest-filters label:nth-of-type(2) select`, "");
  // Typing waits for a pause, then reads once with the text.
  const typedBefore = digestReads.length;
  await page.type(`${view} .proxysql-digest-filters input[type="search"]`, "orders");
  await pause(900);
  const withText = digestReads.slice(typedBefore).filter((b) => b.text);
  assert.equal(withText.length, 1, `one read after the pause: ${JSON.stringify(digestReads.slice(typedBefore))}`);
  assert.equal(withText[0].text, "orders");
  // An order chosen while typing is kept when the pause ends.
  await page.type(`${view} .proxysql-digest-filters input[type="search"]`, "x");
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)].find((b) => b.textContent === "Slowest").click(), view);
  await pause(900);
  assert.deepEqual({ order: digestReads.at(-1).order, text: digestReads.at(-1).text }, { order: "slowest", text: "ordersx" });
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)].find((b) => b.textContent === "Highest rate").click(), view);
  await page.$eval(`${view} .proxysql-digest-filters input[type="search"]`, (i) => { i.select(); });
  await page.keyboard.press("Backspace");
  await pause(700);
  // A mismatch is flagged next to the hostgroup it ran on.
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)].find((b) => b.textContent === "Most frequent").click(), view);
  await page.waitForFunction((sel) => document.querySelector(`${sel} tbody tr.proxysql-mismatch`), {}, view);
  assert.equal(await route(digestKey(30, "0xC54E85FD66BA43DA")), "Mismatch: 32 (rule 30)");
  assert.match((await visible(`${view} .proxysql-digest-summary`))[0], /1 on another hostgroup than the rules send to/);
  await selectRow(digestKey(30, "0xC54E85FD66BA43DA"));
  const routing = await h.inspector();
  assert.match(routing, /Rule 30 sends it to hostgroup 32, but it ran on hostgroup 30\./);
  assert.match(routing, /transaction_persistent is 1 for mf_admin/);
  assert.deepEqual(await inspectorButtons(), [], "digests offer no change");
  await shot("proxysql-mismatch-1440.png");
  // Digest to rule: the rule opens in Query rules.
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .proxysql-link")].find((b) => b.textContent === "Rule 30").click());
  await page.waitForFunction((sel) => document.querySelector(`${sel} tbody tr[data-row-key="rule:30"].is-selected`), {}, view);
  assert.match(await h.inspector(), /Query rule 30/);
  // Rule to its digests: Query digests filtered by the rule, shown as a chip.
  const ruleRead = digestReads.length;
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .proxysql-jump button")][0].click());
  for (let i = 0; i < 50 && digestReads.length === ruleRead; i++) await pause(20);
  assert.equal(digestReads.at(-1).rule_id, 30);
  await page.waitForSelector(`${view} .proxysql-chip`);
  assert.match((await visible(`${view} .proxysql-chip`))[0], /Rule 30 applies, evaluated here/);
  await page.waitForFunction((sel) => /rule 30 applies to these of the 10 read/.test(document.querySelector(`${sel} .proxysql-digest-summary`)?.textContent || ""), {}, view);
  await page.click(`${view} .proxysql-chip .text-button`);
  for (let i = 0; i < 50 && digestReads.at(-1).rule_id !== null; i++) await pause(20);
  assert.equal(digestReads.at(-1).rule_id, null);
  // Rule to one digest: Query digests filtered by it, with it selected.
  await backToList();
  await tab("Query rules");
  await selectRow("rule:30");
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .proxysql-links .proxysql-link")][0].click());
  for (let i = 0; i < 50 && !digestReads.at(-1).digest; i++) await pause(20);
  assert.match(digestReads.at(-1).digest, /^0x[0-9A-F]+$/);
  await page.waitForSelector(".view-inspector .perf-sql");
  assert.match(await h.inspector(), /Routing/);
  await backToList();
  await page.click(`${view} .proxysql-chip .text-button`);
  await pause(300);
  await tab("Hostgroups");
  assert.equal(await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.length), 8, "Hostgroups table has 8 rows matching the tab count");
  assert.match((await visible(`${view} tbody tr[data-row-key="hg:32"] td[data-col="role"]`))[0], /Group Replication reader/);
  const hgStates = await page.$$eval(`${view} tbody tr[data-row-key]`, (trs) => trs.map((tr) => ({
    key: tr.dataset.rowKey,
    text: tr.querySelector('td[data-col="state"] .cluster-mark')?.textContent?.trim(),
    ok: tr.querySelector('td[data-col="state"] .cluster-mark')?.classList.contains("is-ok"),
    neutral: tr.querySelector('td[data-col="state"] .cluster-mark')?.classList.contains("is-unknown"),
    radius: getComputedStyle(tr.querySelector('td[data-col="state"] .cluster-shape')).borderRadius,
  })));
  assert.ok(hgStates.every((h) => h.radius === "50%"), "every hostgroup state shape is a dot/ring");
  assert.deepEqual(hgStates.filter((h) => h.text === "Empty").map((h) => ({ key: h.key, ok: h.ok, neutral: h.neutral })), [
    { key: "hg:19", ok: false, neutral: true },
    { key: "hg:31", ok: false, neutral: true },
    { key: "hg:39", ok: false, neutral: true },
  ], "Empty hostgroups are neutral, not OK");
  assert.ok(hgStates.filter((h) => h.text === "OK").every((h) => h.ok && !h.neutral));

  // Cached routing must be invalidated after a reviewed rule change. The
  // side inspector stays open and the rules list stays mounted throughout.
  await tab("Query digests");
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .proxysql-digest-tools .segmented button`)]
    .find((b) => b.textContent === "Most time").click(), view);
  await page.waitForFunction((sel) => /Top 25 by total time/.test(document.querySelector(`${sel} .proxysql-digest-summary`)?.textContent || ""), {}, view);
  const changedKey = digestKey(32, "0xA8027DF9CE26E2FF");
  assert.equal(await route(changedKey), "32 · rule 30");
  await tab("Query rules");
  await selectRow("rule:30");
  const readsBeforeChange = digestReads.length;
  const sendsBeforeChange = sends.length;
  const disabledRule = structuredClone(R.lab);
  const rule = disabledRule.query_rules.find((r) => r.rule_id === 30);
  Object.assign(rule, { active: false, runtime_active: false, actions: [], digests: [], digest_count: 0 });
  rule.fields.active = "0";
  const freshDigests = structuredClone(R["digests-time"]);
  const changedDigest = freshDigests.digests.find((d) => d.key === changedKey);
  // Transport fixtures deliberately supply changed routing; the backend's
  // rule evaluator is covered separately by the ProxySQL Python tests.
  Object.assign(changedDigest.routing, { expected: 30, source: "default", rules: [], decisive: null,
    verdict: "mismatch", text: "The user's default sends it to hostgroup 30; it previously ran on 32." });
  planAnswer = () => ({ status: 200, body: R["plan-rule-disable"] });
  sendAnswer = () => {
    state.fallback = { status: 200, body: disabledRule };
    digestOverride = freshDigests;
    return { status: 200, body: { ...R["result-done"], action: "rule_disable",
      statements: R["plan-rule-disable"].statements, sent: R["plan-rule-disable"].statements,
      message: "Rule 30 disabled and loaded to runtime.", after: {}, effect: [["Active", "Yes", "No"]],
      undo: R["plan-rule-disable"].undo, next: null, checks: [] } };
  };
  await clickAction("Disable rule…");
  await waitSheet();
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), R["plan-rule-disable"].statements.join("\n"));
  await page.click(`${sheet} .sheet-actions button:last-child`);
  assert.match((await answered()).outcome, /^Done/);
  await closeSheet();
  await page.waitForFunction(() => /Not active/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.equal(digestReads.length, readsBeforeChange, "reviewed change defers hidden digest reads");
  assert.equal(await page.$eval(rowSelector("rule:30"), (e) => !!e.offsetParent), true);
  await tab("Query digests");
  await page.waitForFunction((sel) => document.querySelector(`${sel} td[data-col="route"]`)?.textContent.includes("Mismatch: 30"), {}, rowSelector(changedKey));
  assert.equal(digestReads.length, readsBeforeChange + 1, "reopening with unchanged filters rereads routing");
  assert.equal(sends.length, sendsBeforeChange + 1, "cache invalidation sends no further write");
  digestOverride = null;
  sendAnswer = () => ({ status: 200, body: R["result-done"] });
  await resultButton("Close");

  // A rule disabled in memory but not loaded still routes in runtime: the
  // inspector keeps the digests it applies to.
  await tab("Query rules");
  await refreshTo(R["rules-pending"]);
  await selectRow("rule:30");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-detail")), false, "query rule inspector opens in right pane");
  assert.equal(await page.$eval(rowSelector("rule:30"), (e) => !!e.offsetParent && e.classList.contains("is-selected")), true, "rules list stays mounted with row selected");
  const pendingRule = await h.inspector();
  assert.match(pendingRule, /Not active \(runtime: active\)/);
  assert.doesNotMatch(pendingRule, /applies to no query/);
  assert.match(pendingRule, /SELECT COUNT\(\*\) FROM testdb\.orders WHERE status = \? AND customer_id = \?/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-row-key") === "rule:30");
  await tab("Servers");

  // Delimiters in schema/user names must not alias row selection or links.
  await refreshTo(R.collisions);
  digestOverride = R["digests-collisions"];
  await tab("Query rules");
  for (const [schema, user, text] of [["a|b", "c", "SELECT collision_one"], ["a", "b|c", "SELECT collision_two"]]) {
    await selectRow("rule:30");
    await page.evaluate((text) => [...document.querySelectorAll(".view-inspector .proxysql-links .proxysql-link")]
      .find((b) => b.textContent === text).click(), text);
    const key = digestKey(32, "0xE1", schema, user);
    await page.waitForFunction((sel, key) => [...document.querySelectorAll(`${sel} tbody tr[data-row-key]`)]
      .some((tr) => tr.dataset.rowKey === key && tr.classList.contains("is-selected")), {}, view, key);
    assert.equal(await page.$eval(".view-inspector .perf-sql code", (e) => e.textContent), text);
    await selectRow(key);
    assert.equal(await page.$eval(".view-inspector .perf-sql code", (e) => e.textContent), text);
    await backToList();
    await tab("Query rules");
  }
  digestOverride = null;
  await backToList();
  await tab("Servers");
  await refreshTo(R.lab);

  // Problems lead: a hostgroup without an ONLINE server.
  await tab("Servers");
  await refreshTo(R.problem);
  assert.match((await visible(`${view} .proxysql-findings`))[0], /No server is ONLINE in hostgroup 32/);
  // Pending changes in memory show on the configuration line.
  await refreshTo(R.pending);
  assert.match((await visible(`${view} .proxysql-config`))[0], /1 change not loaded to runtime · 1 difference from disk/);
  assert.deepEqual(await page.$$eval(`${view} .proxysql-config button`, (els) => els.map((b) => b.classList.contains("ghost"))), [false, false],
    "when changes wait in memory or disk, Load and Save are not ghost buttons");

  // Another connection's report: greyed, its changes off, nothing planned.
  await refreshTo(R.lab);
  await chooseConnection(page, await connectionOption(page, "Plain MySQL"));
  await page.waitForFunction((sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  await selectRow("rt|32|mf-ic-2|3306");
  assert.equal(await page.$eval(".view-inspector .review-actions button", (b) => b.getAttribute("aria-disabled")), "true");
  assert.match(await page.$eval(".view-inspector .review-actions", (e) => e.textContent), /read on ProxySQL lab/);
  const plansNow = plans.length;
  await clickAction("Set OFFLINE_SOFT…");
  await pause(300);
  assert.equal(plans.length, plansNow, "nothing is planned on another connection's report");
  // Not ProxySQL: muted only for the connection that was read.
  await backToList();
  await refreshTo(R.not);
  assert.equal(await h.text(`${view} .view-empty h3`), "Not a ProxySQL Admin interface");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true");
  assert.equal(await page.$eval(nav, (b) => b.title), "This connection is not a ProxySQL Admin interface");
  await chooseConnection(page, await connectionOption(page, "ProxySQL lab"));
  await pause(300);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "another connection is not muted by this read");
  await refreshTo(R.lab);

  // The 720x520 window: no sideways scroll, the table starts above the fold.
  await page.setViewport({ width: 720, height: 520 });
  await pause(400);
  const narrow = await page.evaluate((sel) => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    top: document.querySelector(`${sel} .data-table-wrap`)?.getBoundingClientRect().top,
    fold: document.querySelector(".window-statusbar").getBoundingClientRect().top,
    toolbar: document.querySelector(".window-toolbar").getBoundingClientRect().top,
  }), view);
  assert.ok(narrow.overflow <= 0, `horizontal page scroll at 720 px: ${narrow.overflow}`);
  assert.ok(narrow.top < narrow.fold, `the table starts above the fold: ${JSON.stringify(narrow)}`);
  await shot("proxysql-720.png");
  // Below 1100 px the inspector opens as a bottom sheet, keeping the list mounted; Escape returns focus to the row.
  await selectRow("rt|32|mf-ic-2|3306");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-sheet")), true, "bottom sheet inspector at 720 px");
  assert.equal(await page.$eval(rowSelector("rt|32|mf-ic-2|3306"), (e) => !!e.offsetParent && e.classList.contains("is-selected")), true);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-row-key") === "rt|32|mf-ic-2|3306");
  await tab("Query digests");
  await page.waitForSelector(`${view} .proxysql-digest-summary`);
  const narrowDigests = await page.evaluate((sel) => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    tools: document.querySelector(`${sel} .proxysql-digest-tools`).getBoundingClientRect().right,
    width: document.documentElement.clientWidth,
  }), view);
  assert.ok(narrowDigests.overflow <= 0 && narrowDigests.tools <= narrowDigests.width, `digests fit at 720 px: ${JSON.stringify(narrowDigests)}`);
  // Too narrow for five segments: the order is a menu, the same choice.
  assert.equal(await page.$eval(`${view} .proxysql-order-segments`, (e) => getComputedStyle(e).display), "none");
  await page.select(`${view} .proxysql-order-select select`, "slowest");
  for (let i = 0; i < 50 && digestReads.at(-1).order !== "slowest"; i++) await pause(20);
  assert.equal(digestReads.at(-1).order, "slowest");
  await shot("proxysql-digests-720.png");
  await tab("Servers");
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(200);

  // Multi-tier Topology tab: ProxySQL -> Readyset cache tier -> MySQL Primary & Replica.
  const multiTierProxy = structuredClone(R.lab);
  multiTierProxy.topology = {
    has_readyset: true,
    querypilot_rule_count: 2,
    querypilot_rule_ids: [10, 11],
    readyset_nodes: [
      {
        key: "readyset:rt|40|readyset-cache|3307",
        server_key: "rt|40|readyset-cache|3307",
        kind: "readyset",
        host: "readyset-cache",
        port: 3307,
        address: "readyset-cache:3307",
        hostgroup_id: 40,
        hostgroup_roles: [],
        status: "ONLINE",
        weight: 1000,
        routing_mode: "cached_and_mirrored",
        queries: 4820,
        conn_used: 2,
        conn_free: 14,
        latency_us: 140,
        version: "stable-250605",
        readyset_status: "Online",
        replication_status: "Completed",
        cache_count: 6,
        profile_id: null,
        profile_name: "Readyset Cache",
        verified: true,
        evidence: "endpoint",
        upstreams: [
          {
            host: "mf-ic-1",
            port: 3306,
            address: "mf-ic-1:3306",
            purpose: "replication, queries",
            active_cdc: true,
            health: "Healthy",
            profile_id: null,
            profile_name: "Primary",
          },
        ],
      },
    ],
    database_nodes: [
      {
        key: "db:rt|30|mf-ic-1|3306",
        server_key: "rt|30|mf-ic-1|3306",
        kind: "database",
        host: "mf-ic-1",
        port: 3306,
        address: "mf-ic-1:3306",
        hostgroup_id: 30,
        hostgroup_roles: ["writer"],
        status: "ONLINE",
        weight: 1000,
        comment: null,
        queries: 1940,
        conn_used: 1,
        conn_free: 7,
        latency_us: 410,
        product: "MySQL",
        version: "8.4.11",
        read_only: false,
        cluster_kind: "innodb_cluster",
        cluster_name: "mfcluster",
        profile_id: null,
        profile_name: "Primary",
        verified: true,
        evidence: "endpoint",
      },
      {
        key: "db:rt|32|mf-ic-2|3306",
        server_key: "rt|32|mf-ic-2|3306",
        kind: "database",
        host: "mf-ic-2",
        port: 3306,
        address: "mf-ic-2:3306",
        hostgroup_id: 32,
        hostgroup_roles: ["reader"],
        status: "ONLINE",
        weight: 1000,
        comment: null,
        queries: 3110,
        conn_used: 2,
        conn_free: 6,
        latency_us: 390,
        product: "MySQL",
        version: "8.4.11",
        read_only: true,
        cluster_kind: "innodb_cluster",
        cluster_name: "mfcluster",
        profile_id: null,
        profile_name: "Replica",
        verified: true,
        evidence: "endpoint",
      },
    ],
    notes: [],
  };
  await refreshTo(multiTierProxy);
  await tab("Topology");
  await page.waitForSelector(`${view} .proxysql-topology`, { visible: true });
  const proxyTopoText = await h.text(`${view} .proxysql-topology`);
  assert.match(proxyTopoText, /readyset-cache:3307/);
  assert.match(proxyTopoText, /QueryPilot routing \(2 rules\)/);
  assert.match(proxyTopoText, /6 caches/);
  assert.match(proxyTopoText, /mf-ic-1/);
  assert.match(proxyTopoText, /mf-ic-2/);
  assert.match(proxyTopoText, /InnoDB Cluster \(mfcluster\)/);
  assert.equal(await page.$(`${view} .proxysql-topology .topology-presentation-controls`), null, "no presentation controls");
  assert.equal(await page.$eval(`${view} .proxysql-topology`, (e) => e.dataset.motion), "on");
  // S12: every node above carries a profile_name but profile_id null, so none
  // renders a Use chip; chips come only from a backend profile_id.
  assert.equal(
    await page.$(`${view} .proxysql-topology .cluster-use-node`),
    null,
    "no Use chip without a backend profile_id",
  );
  await shot("proxysql-topology-multitier-1440.png");

  // Lane rs1-B: with the reported statuses stripped, no link runs, no stem is
  // active and the nodes read Not read.
  const unreadTiers = structuredClone(multiTierProxy);
  unreadTiers.topology.readyset_nodes = unreadTiers.topology.readyset_nodes.map((n) => ({ ...n, status: null }));
  unreadTiers.topology.database_nodes = unreadTiers.topology.database_nodes.map((n) => ({ ...n, status: null }));
  await refreshTo(unreadTiers);
  await tab("Topology");
  await page.waitForSelector(`${view} .proxysql-topology`, { visible: true });
  const ptopo = `${view} .proxysql-topology`;
  assert.equal(await page.$(`${ptopo} .tier-link.is-running`), null, "no link runs on unreported statuses");
  assert.equal(await page.$(`${ptopo} .cluster-stem.is-active`), null, "no active stem on unreported statuses");
  // The Admin card keeps its own reported status; the stripped tiers show no OK mark.
  const okOutsideAdmin = await page.evaluate((sel) => {
    const rows = [...document.querySelectorAll(`${sel} .tier-row`)];
    return rows
      .filter((r) => (r.getAttribute("aria-label") || "") !== "ProxySQL routing layer")
      .some((r) => r.querySelector(".cluster-mark.is-ok"));
  }, ptopo);
  assert.equal(okOutsideAdmin, false, "no OK mark on unreported statuses");
  assert.match(await h.text(ptopo), /Not read/);
  await refreshTo(multiTierProxy);

  // Routing states follow the backend (proxysql.py _server_tones): an ONLINE
  // row in a monitor's offline hostgroup is out of traffic, and every SHUNNED
  // variant (SHUNNED_REPLICATION_LAG included) is stopped, never unknown.
  const [dbA, dbB] = multiTierProxy.topology.database_nodes;
  const stateTiers = structuredClone(multiTierProxy);
  stateTiers.monitors = [
    {
      kind: "group_replication",
      source: "runtime",
      active: true,
      writer_hostgroup: 30,
      backup_writer_hostgroup: 31,
      reader_hostgroup: 32,
      offline_hostgroup: 39,
      max_writers: 1,
      writer_is_also_reader: 0,
      max_transactions_behind: 16,
      comment: "",
    },
  ];
  stateTiers.topology.database_nodes = [
    { ...dbA, hostgroup_id: 39, hostgroup_roles: ["offline"], status: "ONLINE", status_source: "runtime" },
    { ...dbB, hostgroup_id: 32, hostgroup_roles: ["reader"], status: "SHUNNED_REPLICATION_LAG", status_source: "runtime" },
  ];
  await refreshTo(stateTiers);
  await tab("Topology");
  const stateLinks = await page.$$eval(`${ptopo} .tier-link`, (els) => els.map((e) => e.className));
  assert.ok(stateLinks.some((c) => c.includes("is-stopped")), "an offline-hostgroup ONLINE row and a SHUNNED variant stop the backend link");
  assert.ok(stateLinks.some((c) => c.includes("is-running")), "the reported ONLINE Readyset tier link still runs");
  const stateTones = await page.$$eval(`${ptopo} .tier-row[aria-label="Database backend hostgroups"] .tier-card`, (els) =>
    els.map((e) => [...e.classList].find((c) => c.startsWith("tone-"))),
  );
  assert.deepEqual(stateTones, ["tone-watch", "tone-watch"], "an offline-hostgroup ONLINE row and a SHUNNED variant read watch, never ok or unknown");
  assert.match(await h.text(ptopo), /SHUNNED_REPLICATION_LAG/, "a SHUNNED variant shows its reported word");

  // CX-M18: a Readyset node ONLINE in a monitor's offline hostgroup is out of
  // traffic: its card reads watch and the Readyset tier link stops.
  const rsOfflineTiers = structuredClone(multiTierProxy);
  rsOfflineTiers.topology.readyset_nodes = rsOfflineTiers.topology.readyset_nodes.map((n) => ({
    ...n,
    hostgroup_roles: ["offline"],
    status: "ONLINE",
    status_source: "runtime",
  }));
  await refreshTo(rsOfflineTiers);
  await tab("Topology");
  const rsOfflineTone = await page.$eval(`${ptopo} .tier-row[aria-label="Readyset cache tier"] .tier-card`, (e) =>
    [...e.classList].find((c) => c.startsWith("tone-")),
  );
  assert.equal(rsOfflineTone, "tone-watch", "an offline-hostgroup ONLINE Readyset node is out of traffic");
  const rsOfflineLink = await page.$eval(`${ptopo} .tier-link`, (e) => e.className);
  assert.ok(rsOfflineLink.includes("is-stopped"), "the Readyset tier link stops for an offline-hostgroup node");

  // A SHUNNED writer row never serves traffic, standby or not: the ONLINE
  // backup-hostgroup row is its own node with its own running state, and the
  // SHUNNED card only describes the standby's health.
  const standbyTiers = structuredClone(stateTiers);
  standbyTiers.topology.database_nodes = [
    { ...dbA, hostgroup_id: 30, hostgroup_roles: ["writer"], status: "SHUNNED", status_source: "runtime" },
    { ...dbB, hostgroup_id: 31, hostgroup_roles: ["backup writer"], host: "mf-ic-1", port: 3306, address: "mf-ic-1:3306", status: "ONLINE", status_source: "runtime" },
  ];
  await refreshTo(standbyTiers);
  await tab("Topology");
  const standbyTones = await page.$$eval(`${ptopo} .tier-row[aria-label="Database backend hostgroups"] .tier-card`, (els) =>
    els.map((e) => [...e.classList].find((c) => c.startsWith("tone-"))),
  );
  assert.deepEqual(standbyTones, ["tone-watch", "tone-ok"], "a SHUNNED writer is stopped; only the ONLINE backup row runs");
  assert.match(await h.text(ptopo), /standby ONLINE/, "the SHUNNED card describes standby health without claiming traffic");
  assert.ok(await page.$(`${ptopo} .tier-row[aria-label="Database backend hostgroups"] .cluster-stem.is-active`), "the ONLINE backup row keeps an active stem");

  // The UNKNOWN placeholder and config-only rows are not runtime health: they
  // read Not read and never run a link or a stem.
  const cfgTiers = structuredClone(multiTierProxy);
  cfgTiers.topology.readyset_nodes = cfgTiers.topology.readyset_nodes.map((n) => ({ ...n, status: "UNKNOWN", status_source: null }));
  cfgTiers.topology.database_nodes = [
    { ...dbA, status: "ONLINE", status_source: "config" },
    { ...dbB, status: null, status_source: "config" },
  ];
  await refreshTo(cfgTiers);
  await tab("Topology");
  const cfgText = await h.text(ptopo);
  assert.doesNotMatch(cfgText, /UNKNOWN/, "the UNKNOWN placeholder reads Not read");
  assert.match(cfgText, /Not read/, "config-only rows read Not read");
  assert.equal(await page.$(`${ptopo} .tier-link.is-running`), null, "a config-only ONLINE row runs no link");
  assert.equal(await page.$(`${ptopo} .cluster-stem.is-active`), null, "a config-only ONLINE row gets no active stem");
  await refreshTo(multiTierProxy);

  // F8: an overflowing tier row scrolls on its own; every other row stays
  // centred and fully visible in the frame. With ten backends the backend
  // row's own scroller overflows: at scrollLeft 0 the first card starts at
  // the scroller's left edge (never clipped on the unreachable side of a
  // centred row), scrolled fully right the last card is reachable, the bus
  // keeps running stem to stem, and the single ProxySQL Admin card and the
  // frame itself never move.
  await refreshTo(R.lab);
  await tab("Topology");
  await page.waitForSelector(`${ptopo} .tier-row[aria-label="Database backend hostgroups"] .tier-card`, { visible: true });
  await pause(200);
  const wideRow = await page.evaluate((sel) => {
    const frame = document.querySelector(sel);
    const admin = frame.querySelector('.tier-row[aria-label="ProxySQL routing layer"] .tier-card');
    const row = frame.querySelector('.tier-row[aria-label="Database backend hostgroups"]');
    const scroller = row.querySelector(".tier-group");
    const cards = [...row.querySelectorAll(".tier-card")];
    const bus = row.querySelector(".cluster-bus-line");
    const f = frame.getBoundingClientRect();
    const a = admin.getBoundingClientRect();
    scroller.scrollLeft = 0;
    const atStart = (() => {
      const s = scroller.getBoundingClientRect();
      const first = cards[0].getBoundingClientRect();
      const b = bus ? bus.getBoundingClientRect() : null;
      return { firstLeft: first.left, firstCenter: first.left + first.width / 2, scrollerLeft: s.left, busLeft: b ? b.left : null };
    })();
    scroller.scrollLeft = scroller.scrollWidth;
    const s = scroller.getBoundingClientRect();
    const last = cards[cards.length - 1].getBoundingClientRect();
    const b = bus ? bus.getBoundingClientRect() : null;
    return {
      cardCount: cards.length,
      scrollWidth: scroller.scrollWidth,
      clientWidth: scroller.clientWidth,
      scrollLeft: scroller.scrollLeft,
      adminInside: a.left >= f.left - 0.5 && a.right <= f.right + 0.5 && a.top >= f.top - 0.5 && a.bottom <= f.bottom + 0.5,
      frameScrolls: frame.scrollWidth > frame.clientWidth + 1,
      ...atStart,
      lastRight: last.right,
      lastCenter: last.left + last.width / 2,
      scrollerRight: s.right,
      busRight: b ? b.right : null,
    };
  }, ptopo);
  assert.equal(wideRow.cardCount, 10, "all ten lab backends render on the tier");
  assert.ok(wideRow.adminInside, `the ProxySQL Admin card lies fully inside the frame at first render: ${JSON.stringify(wideRow)}`);
  assert.equal(wideRow.frameScrolls, false, `the frame itself does not scroll sideways: ${JSON.stringify(wideRow)}`);
  assert.ok(wideRow.scrollWidth > wideRow.clientWidth, `the backend row's own scroller overflows: ${JSON.stringify(wideRow)}`);
  assert.ok(wideRow.scrollLeft > 0, "the row scrolled right");
  assert.ok(wideRow.firstLeft >= wideRow.scrollerLeft - 0.5, `first card starts at the row's left edge: ${JSON.stringify(wideRow)}`);
  assert.ok(wideRow.lastRight <= wideRow.scrollerRight + 0.5, `last card reachable at full row scroll: ${JSON.stringify(wideRow)}`);
  assert.ok(Math.abs(wideRow.busLeft - wideRow.firstCenter) <= 2, `bus starts at the first stem: ${JSON.stringify(wideRow)}`);
  assert.ok(Math.abs(wideRow.busRight - wideRow.lastCenter) <= 2, `bus ends at the last stem: ${JSON.stringify(wideRow)}`);
  await shot("proxysql-topology-wide-row-1440.png");
  await refreshTo(multiTierProxy);
  await tab("Topology");
  // A row that fits stays centred in its scroller, with no scroll.
  const fitRow = await page.evaluate((sel) => {
    const row = document.querySelector(`${sel} .tier-row[aria-label="Database backend hostgroups"]`);
    const scroller = row.querySelector(".tier-group");
    scroller.scrollLeft = 0;
    const members = row.querySelector(".tier-members");
    const s = scroller.getBoundingClientRect();
    const r = members.getBoundingClientRect();
    return { left: r.left - s.left, right: s.right - r.right, scrollWidth: scroller.scrollWidth, clientWidth: scroller.clientWidth };
  }, ptopo);
  assert.ok(fitRow.scrollWidth <= fitRow.clientWidth + 1, `two backends fit without scroll: ${JSON.stringify(fitRow)}`);
  assert.ok(Math.abs(fitRow.left - fitRow.right) <= 2, `a row that fits stays centred: ${JSON.stringify(fitRow)}`);

  assert.deepEqual(h.errors, []);
  console.log("proxysql: ok");
} finally {
  await h.close();
}
