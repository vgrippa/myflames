// Sessions view of the Server place (replaces Running queries): mocked
// /api/live/sessions with sessions-1.0 reports computed by myflames/sessions.py
// from recordings of live MySQL 8.4 and MariaDB 11.4 servers (fixtures/, see
// server-fixtures.py): an idle transaction holding a row lock, sessions waiting
// for it and for a metadata lock, long statements, and a load. Show plan and
// the guarded Kill are in the inspector.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege, privilegeAnswer, load, open, pause, SERVER_GROUPS } from "./perf-harness.mjs";

const active = await load("sessions-mysql-8.4-active.json");
const all = await load("sessions-mysql-8.4.json");
const mariadb = await load("sessions-mariadb-11.4.json");
const noProcess = await load("sessions-mysql-8.4-noproc.json");
// Synthetic identities supplement the older live recordings without rewriting them.
for (const report of [active, all, noProcess]) report.server_identity = { server_uuid: "sessions-server-uuid" };
mariadb.server_identity = { server_id: "1", hostname: "sessions-mariadb", version: mariadb.server_version };
const reportConnectionKey = (c) => JSON.stringify([
  c.profile_id || "", c.host || "", Number(c.port) || 0, c.user || "", c.ssl_mode || "", c.ssl_ca || "",
]);

const raw = JSON.stringify({
  query: "SELECT SUM(v) FROM t WHERE id BETWEEN 10 AND 20",
  operation: "Aggregate: sum(t.v)", access_type: "aggregate", estimated_rows: 1, estimated_total_cost: 2.1,
  inputs: [{ operation: "Index range scan on t using PRIMARY", access_type: "index", table_name: "t", index_name: "PRIMARY",
    estimated_rows: 11, estimated_total_cost: 2.1 }],
});
const plan = {
  schema_version: "explain-connection-1.0", raw, mode: "explain", engine: "mysql", server_version: "8.4.11",
  connection_id: 100, query: "SELECT SUM(v) FROM t WHERE id BETWEEN 10 AND 20", warnings: [],
  captured_at: "2026-10-14T18:21:45+00:00",
  capture: {
    schema_version: "capture-1.0", raw, query: "SELECT SUM(v) FROM t WHERE id BETWEEN 10 AND 20", mode: "explain",
    engine: "mysql", server_version: "8.4.11", captured_at: "2026-10-14T18:21:45+00:00",
    conditions: "In-flight statement on connection 100 (estimated plan, EXPLAIN FOR CONNECTION).",
    warnings: [], runs: [], schema: {}, table_stats: {}, variables: {}, trace: null,
  },
};
const kill = { status: 200, body: { schema_version: "kill-1.0", status: "killed", connection_id: 93, mode: "connection",
  statement: "KILL CONNECTION 93", verified: ["user", "host", "not this session", "transaction"], after: { present: false },
  message: "Ended connection 93. Its open transaction was rolled back and its locks were released." } };
const h = await open("/api/live/sessions", "sessions", {
  "/api/live/explain-connection": { status: 200, body: plan },
  "/api/live/kill": kill,
  // A cancelled Kill is recorded in the audit trail as declined.
  "/api/audit/declined": { status: 200, body: { audit_id: "a1", status: "declined" } },
});
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: active };
const view = ".server-view[data-view=sessions]";
const row = (id) => `${view} tbody tr[data-session="${id}"]`;
const inspectorHas = (pattern) =>
  page.waitForFunction((p) => new RegExp(p, "s").test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);
try {
  await h.server();
  // Sessions replaces Running queries in the Workload group.
  assert.deepEqual(await texts(".source-list .source-group:nth-child(2) .source-label"), SERVER_GROUPS.Workload);
  assert.equal(await page.$("#server-nav-running"), null);
  await h.view("sessions");
  assert.match(await text(`${view} .view-empty`), /Every connection, what it runs/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");
  assert.equal(await page.$eval(`${view} .segmented button[data-value=active]`, (e) => e.getAttribute("aria-pressed")), "true",
    "Active is the default");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0");
  assert.match(await h.popover("About Sessions"), /Active hides sleeping sessions without a transaction/);

  // Load: the counter strip, one summary line, the table sorted problems first.
  await button("Load sessions", view + " ");
  await page.waitForSelector(`${view} tbody tr`);
  await h.idle(view);
  assert.deepEqual([state.seen[0].active_only, state.seen[0].min_seconds], [true, 0]);
  assert.equal(state.seen[0].connection.host, "127.0.0.1");
  const summary = await text(`${view} .summary-line`);
  assert.match(summary, /Problem\s*9 active · 2 idle in transaction · 2 waiting for a lock · 5 running 10 s or more · oldest transaction \d+ m \d+ s/);
  assert.ok(summary.replace(/\d+ notes?$/, "").length <= 130, "the summary is one line");
  const facts = await text(`${view} .fact-strip`);
  assert.match(facts, /Connected\s*12/);
  assert.match(facts, /Limit\s*151/);
  assert.match(facts, /Max seen\s*12/);
  assert.match(facts, /History list\s*[\d,]+/);
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.session)),
    active.sessions.map((s) => String(s.id)));
  assert.equal(await page.$eval(row(93), (e) => e.dataset.status), "problem");
  assert.deepEqual(await page.$$eval(`${row(94)} .pill`, (els) => els.map((e) => e.textContent)), ["Long running", "Lock wait"]);
  assert.deepEqual(await page.$$eval(`${row(93)} .pill`, (els) => els.map((e) => e.textContent)), ["Idle in transaction"]);
  assert.match(await text(row(93)), /Last: SELECT \* FROM u WHERE id = 1/);
  // The badge: active sessions, in red while something is a problem.
  assert.equal(await text("#server-nav-sessions .count-badge"), "9");
  assert.equal(await page.$eval("#server-nav-sessions .count-badge", (e) => e.className), "count-badge is-danger");
  assert.match(await h.notes(), /holding back purge|history list/);
  // The breakdown waits behind a disclosure.
  assert.equal(await page.$eval(`${view} .sessions-breakdown`, (e) => e.open), false);
  await page.click(`${view} .sessions-breakdown summary`);
  assert.deepEqual(await texts(`${view} .breakdown h4`), ["State", "User", "Host", "Database", "Command"]);
  assert.match(await text(`${view} .breakdown[aria-label="Sessions by user"]`), /root\s*10/);
  await page.click(`${view} .sessions-breakdown summary`);

  // An idle transaction: why, its last statement, the transaction, the rule; Kill is offered.
  await h.select(row(93));
  await inspectorHas(/Connection 93/);
  const idle = await h.inspector();
  assert.match(idle, /Idle for .* inside a transaction open for/);
  assert.match(idle, /Last statement\s*SELECT \* FROM u WHERE id = 1/);
  assert.match(idle, /Rows locked\s*1/);
  assert.match(idle, /REPEATABLE READ/);
  assert.match(idle, /Idle in transaction: a session idle \(Sleep\) inside an open InnoDB transaction keeps its row locks/);
  assert.equal(await page.$('.view-inspector [aria-label^="Show plan"]'), null, "an idle session has no plan");
  assert.equal(await page.$eval('.view-inspector [aria-label="Kill: connection 93"]', (e) => e.disabled), false);
  // Compact columns retain identity, time and SQL. The Columns menu still
  // contains detail columns; explicit choices survive inspector toggles.
  assert.deepEqual(await page.$$eval(`${view} thead th[data-col]`, (els) => els.map((e) => e.dataset.col)),
    ["status", "id", "time", "statement"]);
  assert.match(await text(`${view} th[data-col=time]`), /Time \(s\)/);
  assert.equal(await page.$eval(`${view} [aria-label="Width of ID"]`, (e) => e.getAttribute("role")), "separator");
  assert.equal(await page.$eval(`${view} [aria-label="Width of Time (s)"]`, (e) => e.getAttribute("role")), "separator");
  assert.equal(await text(`${row(93)} td[data-col=id]`), "93", "identity remains alongside the compact numeric columns");
  const readsBeforeColumns = state.seen.length;
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover input[data-col=db]');
  await page.keyboard.press("Escape");
  assert.ok(await page.$(`${view} th[data-col=db]`));
  await page.click(`${view} [aria-label="Width of Database"]`, { clickCount: 2 });
  assert.ok(await page.$(`${view} th[data-col=db]`), "fitting an explicitly shown column keeps it visible");
  await page.click(`${view} .inspector-toggle`);
  assert.ok(await page.$(`${view} th[data-col=user]`), "closing the inspector restores secondary columns");
  await page.click(`${view} .inspector-toggle`);
  assert.ok(await page.$(`${view} th[data-col=db]`), "explicitly shown columns stay shown");
  assert.equal(await page.$(`${view} th[data-col=user]`), null);
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover .columns-reset');
  await page.keyboard.press("Escape");
  assert.equal(await page.$(`${view} th[data-col=db]`), null, "Reset restores the compact preset");
  assert.equal(state.seen.length, readsBeforeColumns, "column changes never read the server");
  await page.focus(row(93));
  // A lock wait says what it waits for and opens Locks.
  await page.keyboard.press("ArrowDown");
  await inspectorHas(/Connection 94/);
  assert.match(await h.inspector(), /Waiting \d+ m \d+ s for an InnoDB lock \(a row lock, or a table lock such as AUTO-INC\)/);
  assert.match(await h.inspector(), /UPDATE t SET v = 0 WHERE id = 10/);
  await page.keyboard.press("ArrowDown");
  await inspectorHas(/for a metadata lock \(Waiting for table metadata lock\)/);
  await page.locator(".view-inspector button ::-p-text(Open Locks to see who blocks it)").click();
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  await h.view("sessions");
  assert.ok(await page.$(`${row(95)}[aria-current=true]`), "the report and the selection stay");

  // Kill: the confirmation sheet starts on Cancel; Escape and Cancel send
  // nothing and are recorded as declined; confirm sends the fingerprint.
  const declined = () => state.other.filter((o) => o.path === "/api/audit/declined").map((o) => o.body);
  await h.select(row(93));
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  const sheet = await text("dialog.kill-dialog");
  assert.match(sheet, /End connection 93\?/);
  assert.match(sheet, /Server\s*Local MySQL.*127\.0\.0\.1:3306/);
  assert.match(sheet, /Server identity\s*sessions-server-uuid/);
  assert.match(sheet, /User\s*root@172\.17\.0\.1:60204 · mf/);
  assert.match(sheet, /State\s*Idle in transaction for \d+ m \d+ s/);
  assert.match(sheet, /KILL QUERY would not help/);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Kill: connection 93");
  assert.equal(state.other.filter((o) => o.path === "/api/live/kill").length, 0, "nothing is sent until the confirm button");
  while (declined().length < 1) await pause(25);
  assert.deepEqual(
    { operation: declined()[0].operation, statements: declined()[0].statements, target: declined()[0].target, host: declined()[0].connection.host },
    {
      operation: "kill",
      statements: ["/*myflames:kill*/ KILL CONNECTION 93"],
      target: { connection_id: 93, mode: "connection", user: "root", host: "172.17.0.1:60204", trx_started: "2026-10-14 18:00:53",
        connection_identity: reportConnectionKey(declined()[0].connection), server_identity: all.server_identity },
      host: "127.0.0.1",
    },
  );
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  await page.locator(".sheet-actions ::-p-text(Cancel)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  while (declined().length < 2) await pause(25);
  assert.deepEqual(declined()[1], declined()[0], "Cancel records the same declined attempt");
  const reads = state.seen.length;
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(`${view} .kill-result.killed`);
  const sent = state.other.find((o) => o.path === "/api/live/kill").body;
  assert.equal(sent.confirmed, true, "confirmation is the literal boolean true");
  assert.deepEqual({ id: sent.connection_id, mode: sent.mode, expect: sent.expect },
    { id: 93, mode: "connection", expect: { user: "root", host: "172.17.0.1:60204", trx_started: "2026-10-14 18:00:53", ps_thread_id: null,
      connection_identity: reportConnectionKey(sent.connection), server_identity: all.server_identity } });
  assert.equal(state.seen.length, reads + 1, "the list refreshes after the kill");
  assert.equal(declined().length, 2, "a confirmed Kill is not recorded as declined");
  assert.match(await text(`${view} .kill-result`), /Ended\. Ended connection 93/);
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();

  // Show plan for a running statement opens it in Plans.
  await h.select(row(100));
  await page.click('.view-inspector [aria-label="Show plan for connection 100"]');
  await page.waitForFunction(
    () => document.querySelector(".plan-heading h1")?.textContent === "Connection 100 · estimated plan",
    { timeout: 20000 },
  );
  assert.equal(state.other.findLast((o) => o.path === "/api/live/explain-connection").body.connection_id, 100);
  await h.server();

  // All: idle and system threads too (a new read); a server thread is never killed.
  state.queue.push({ status: 200, body: all });
  await page.click(`${view} .segmented button[data-value=all]`);
  await page.waitForSelector(row(5));
  await h.idle(view);
  assert.equal(state.seen.at(-1).active_only, false);
  // Flags are a detail column, hidden while the inspector is open beside the table.
  await page.click(`${view} .inspector-toggle`);
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  assert.deepEqual(await page.$$eval(`${row(5)} .pill`, (els) => els.map((e) => e.textContent)), ["Server thread"]);
  await h.select(row(5));
  await inspectorHas(/Connection 5/);
  assert.equal(await page.$eval('.view-inspector [aria-label="Kill: connection 5"]', (e) => e.disabled), true);
  assert.match(await page.$eval('.view-inspector [aria-label="Kill: connection 5"]', (e) => e.title), /server thread/);
  // The filter narrows the rows on screen; the minimum time is a new read.
  await page.type(`${view} input[aria-label="Filter sessions"]`, "SLEEP(");
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.session)), ["97", "103"]);
  await page.click(`${view} input[aria-label="Filter sessions"]`, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.locator(`${view} input[aria-label="Running at least (seconds)"]`).fill("30");
  await h.refresh();
  await h.idle(view);
  assert.equal(state.seen.at(-1).min_seconds, 30);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "sessions.png") });

  // MariaDB: the same scenario from MariaDB 11.4.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForSelector(row(79));
  await h.idle(view);
  assert.match(await text(`${view} .fact-strip`), /MariaDB 11\.4/);
  assert.match(await h.notes(), /last statement of a session idle in a transaction/);

  // Without PROCESS: one sentence, only the account's own sessions.
  state.queue.push({ status: 200, body: noProcess });
  await h.refresh();
  await page.waitForSelector(`${view} .gate`);
  await h.idle(view);
  assert.match(await text(`${view} .gate`), /Without the PROCESS privilege only this account's own sessions are listed/);
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.session)), ["103"]);

  // An error keeps the list.
  state.queue.push({ status: 400, body: { error: "Could not read the sessions: ERROR 2013 (HY000): Lost connection" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.ok(await page.$(`${view} tbody tr`));

  // Automatic refresh is opt-in.
  await page.evaluate(() => {
    const original = window.setInterval;
    window.setInterval = (fn, ms, ...rest) => original(fn, ms >= 10000 ? 150 : ms, ...rest);
  });
  state.queue.push({ status: 200, body: all });
  const before = state.seen.length;
  await page.select(`${view} .auto-refresh select`, "10");
  await pause(600);
  assert.ok(state.seen.length > before, "auto-refresh read again");
  await page.select(`${view} .auto-refresh select`, "0");
  await h.idle(view);

  // Narrow: the view is pushed with a back button; the inspector is a sheet that Escape closes.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.click(row(97));
  await page.waitForSelector(".view-inspector.is-sheet");
  await page.waitForFunction(() => document.activeElement?.classList.contains("inspector-close"));
  await page.screenshot({ path: path.join(h.shots, "sessions-390.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);

  // Session restore keeps the view, Active / All and the minimum time, never the list.
  const saved = await h.savedSlow((s) => s.kind === "sessions" && s.sessions_scope === "all" && s.min_seconds === 30);
  assert.equal(JSON.stringify(saved.state).includes("sessions-1.0"), false, "reports are not saved");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-sessions[aria-current=page]");
  assert.equal(await page.$eval(`${view} .segmented button[data-value=all]`, (e) => e.getAttribute("aria-pressed")), "true");
  assert.equal(await page.$eval(`${view} input[aria-label="Running at least (seconds)"]`, (e) => e.value), "30");
  assert.ok(await page.$(`${view} .view-empty`));

  // Provider denials do not block Load/Refresh or hide actions. Own-user
  // KILL uses kill_own, independently of another user's generic denial.
  const foreign = structuredClone(active);
  foreign.sessions.forEach(s => { s.user = "another_user"; });
  state.fallback = { status: 200, body: foreign };
  await h.setPrivileges({ kill: privilegeAnswer("denied"), kill_own: privilegeAnswer("allowed"),
    sessions_all: privilegeAnswer("denied", "PROCESS", { grey_out: false }),
    explain_connection: privilegeAnswer("unknown") });
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, e => e.disabled), false);
  await h.refresh(); await h.idle(view); await h.select(row(100));
  const gatedKill = '.view-inspector [aria-label="Kill: connection 100"]';
  await assertPrivilege(page, gatedKill, "denied", "CONNECTION_ADMIN");
  await page.click(gatedKill); await page.keyboard.press("Enter"); await page.keyboard.press("Space");
  assert.equal(await page.$("dialog.kill-dialog"), null, "a denied Kill cannot open its sheet");
  await assertPrivilege(page, '.view-inspector [aria-label="Show plan for connection 100"]', "unknown");
  await h.setPrivileges({ kill: privilegeAnswer("allowed") });
  await assertPrivilege(page, gatedKill, "allowed");
  await page.click(gatedKill); await page.waitForSelector("dialog.kill-dialog[open]");
  const gatedConfirm = "dialog.kill-dialog .sheet-actions button:last-of-type";
  await h.setPrivileges({ kill: privilegeAnswer("denied", "SYSTEM_USER") });
  await assertPrivilege(page, gatedConfirm, "denied", "SYSTEM_USER");
  const sentBefore = state.other.filter(o => o.path === "/api/live/kill").length;
  await page.click(gatedConfirm); await page.keyboard.press("Enter");
  assert.equal(state.other.filter(o => o.path === "/api/live/kill").length, sentBefore);
  await page.keyboard.press("Escape");
  await h.setPrivileges({ kill: privilegeAnswer("unknown") });
  await assertPrivilege(page, gatedKill, "unknown");
  const owned = structuredClone(active);
  // The default connection has no user name (the OS login): its own sessions
  // are the account the privileges read names (root@localhost in the harness).
  owned.sessions.forEach(s => { s.user = state.seen.at(-1).connection.user || "root"; });
  state.fallback = { status: 200, body: owned };
  await h.setPrivileges({ kill: privilegeAnswer("denied"), kill_own: privilegeAnswer("allowed"),
    explain_connection: privilegeAnswer("denied", "PROCESS", { grey_out: false }) });
  await h.refresh(); await h.idle(view); await h.select(row(100));
  await assertPrivilege(page, gatedKill, "allowed");
  await assertPrivilege(page, '.view-inspector [aria-label="Show plan for connection 100"]', "allowed");

  assert.deepEqual(h.errors, []);
  console.log("sessions view: ok");
} finally {
  await h.close();
}
