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
import assert from "node:assert/strict";
import path from "node:path";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const R = {};
for (const name of ["lab", "pending", "problem", "not", "plan-offline-soft", "plan-offline-hard", "plan-weight", "plan-rule-disable",
  "plan-read-only", "result-done", "plan-save"]) R[name] = await load(`proxysql-${name}.json`);

const view = '.server-view[data-view="proxysql"]';
const nav = '.main-nav button[data-place="proxysql"]';
const plans = [];
const sends = [];
const declined = [];
let planAnswer = () => ({ status: 200, body: R["plan-offline-soft"] });
let sendAnswer = () => ({ status: 200, body: R["result-done"] });
const h = await open("/api/live/proxysql", "proxysql", {
  "/api/live/proxysql/plan": (body) => { plans.push(body); return planAnswer(body); },
  "/api/live/proxysql/action": (body) => { sends.push(body); return sendAnswer(body); },
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
  const back = await page.$(".view-inspector.is-detail .detail-back");
  if (back) { await back.click(); await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail")); }
}
async function selectRow(key) {
  await backToList();
  await page.click(`${view} tbody tr[data-row-key="${key}"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
}
const inspectorButtons = () => page.$$eval(".view-inspector .review-actions button", (els) => els.map((e) => e.textContent.trim()));
async function clickAction(label) {
  await page.evaluate((label) => [...document.querySelectorAll(".view-inspector .review-actions button")].find((b) => b.textContent.trim() === label).click(), label);
}
const sheet = "dialog.action-review[open]";
async function waitSheet() { await page.waitForSelector(sheet, { visible: true }); }
async function noSheet() { await page.waitForFunction((s) => !document.querySelector(s), {}, sheet); }

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
  assert.deepEqual(await visible(`${view} .segmented button`), ["Servers10", "Hostgroups5", "Query rules3", "Query digests1"]);
  assert.deepEqual(await visible(`${view} .tile-label`), ["Backends ONLINE", "Hostgroups", "Query rules active", "Client connections"]);
  assert.match((await visible(`${view} .proxysql-config`))[0], /Configuration of servers: memory matches runtime · saved to disk/);
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

  // The inspector: runtime, pool, configuration row, monitor, and the changes.
  await selectRow("rt|32|mf-ic-2|3306");
  const text = await h.inspector();
  assert.match(text, /Connection pool/);
  assert.match(text, /hostgroup 30, mf-ic-2:3306/);
  assert.match(text, /Group Replication monitor/);
  assert.deepEqual(await inspectorButtons(), ["Set OFFLINE_SOFT…", "Set OFFLINE_HARD…", "Change weight…"], "no move out of a monitor's hostgroups");
  assert.match(await h.inspector(), /Move to hostgroup is not offered: a row moved out of the Group Replication monitor's hostgroups stays/);
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

  // Confirm: the reviewed plan and its token are sent once; the result panel
  // offers the separate Save to disk.
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  const readsBefore = state.seen.length;
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await noSheet();
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
  await noSheet();
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
  sendAnswer = () => ({ status: 500, body: { error: "connection lost while waiting for a reply" } });
  const sendsBefore = sends.length;
  await clickAction("Set OFFLINE_SOFT…");
  await waitSheet();
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await noSheet();
  await page.waitForFunction((sel) => /Outcome unknown/.test(document.querySelector(`${sel} .action-result`)?.textContent || ""), {}, view);
  await pause(300);
  assert.equal(sends.length, sendsBefore + 1, "never retried");
  assert.match(await page.$eval(`${view} .action-result`, (e) => e.textContent), /whether it ran is unknown/);
  await shot("proxysql-unknown-1440.png");
  await page.click(`${view} .action-result .ops-result-head .text-button`);
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
  await backToList();
  await tab("Query digests");
  await page.click(`${view} tbody tr[data-row-key]`);
  await page.waitForSelector(".view-inspector .perf-sql");
  assert.match(await h.inspector(), /SELECT COUNT\(\*\) FROM orders WHERE status = \?/);
  assert.deepEqual(await inspectorButtons(), [], "digests offer no change");
  await backToList();
  await tab("Hostgroups");
  assert.match((await visible(`${view} tbody tr[data-row-key="hg:32"] td[data-col="role"]`))[0], /Group Replication reader/);

  // Problems lead: a hostgroup without an ONLINE server.
  await tab("Servers");
  await refreshTo(R.problem);
  assert.match((await visible(`${view} .proxysql-findings`))[0], /No server is ONLINE in hostgroup 32/);
  // Pending changes in memory show on the configuration line.
  await refreshTo(R.pending);
  assert.match((await visible(`${view} .proxysql-config`))[0], /1 change not loaded to runtime · 1 difference from disk/);

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
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(200);

  assert.deepEqual(h.errors, []);
  console.log("proxysql: ok");
} finally {
  await h.close();
}
