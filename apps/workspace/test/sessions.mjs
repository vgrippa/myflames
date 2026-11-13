// Sessions view of the Server place (replaces Running queries): mocked
// /api/live/sessions with sessions-1.0 reports computed by myflames/sessions.py
// from recordings of live MySQL 8.4 and MariaDB 11.4 servers (fixtures/, see
// server-fixtures.py): an idle transaction holding a row lock, sessions waiting
// for it and for a metadata lock, long statements, and a load. Show plan and
// the guarded Kill are in the inspector and in each row's ⋯ menu.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege, privilegeAnswer, load, open, pause, SERVER_GROUPS, WORKLOAD_GROUPS } from "./perf-harness.mjs";

const active = await load("sessions-mysql-8.4-active.json");
const all = await load("sessions-mysql-8.4.json");
const mariadb = await load("sessions-mariadb-11.4.json");
const ddlProgress = await load("ddl-progress.json");
const instruments = await load("instruments-mysql-8.4.json");
const noProcess = await load("sessions-mysql-8.4-noproc.json");
// Synthetic identities supplement the older live recordings without rewriting them.
for (const report of [active, all, noProcess]) report.server_identity = { server_uuid: "sessions-server-uuid" };
// These recordings predate the additive flag; the scenarios read INNODB_TRX.
active.trx_checked = all.trx_checked = true;
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
  statement: "KILL CONNECTION 93", verified: ["user", "host", "not this session", "transaction"],
  message: "Ended connection 93. Its open transaction was rolled back (1 row changed) and its locks were released.",
  target: { connection_id: 93, user: "root", host: "172.17.0.1:60204", command: "Sleep", state: null, info: null, time_s: 1250,
    trx: { trx_id: "21536", state: "RUNNING", started: "2026-10-14 18:00:53", age_s: 1250, rows_locked: 1, rows_modified: 1 },
    trx_checked: true },
  after: { present: false, trx_checked: true, trx_started: null, rows_locked: null, same_trx: null } } };
// For the kill outcome: 93 blocks 94; the re-read after the kill finds 94 free.
const killable = structuredClone(active);
killable.sessions.find((s) => s.id === 93).blocks_ids = [94];
killable.sessions.find((s) => s.id === 94).blocked_by = [93];
const killFreed = structuredClone(killable);
killFreed.sessions = killFreed.sessions.filter(s => s.id !== 93);
Object.assign(killFreed.sessions.find((s) => s.id === 94), { blocked_by: [], lock_wait: null, is_locked: false, lock_status: null });
// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/sessions", "sessions", {
  "/api/live/sessions/history": body => ({ status: 200, body: { id: body.id, available: true, reason: "No completed statements are retained for this session.", statements: [], limit: 10 } }),
  "/api/live/explain-connection": { status: 200, body: plan },
  "/api/live/kill": kill,
  "/api/live/instruments": { status: 200, body: instruments },
  // A cancelled Kill is recorded in the audit trail as declined.
  "/api/audit/declined": { status: 200, body: { audit_id: "a1", status: "declined" } },
}, { list: "docked", stampReport: "taken_at" });
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: active };
const view = ".server-view[data-view=sessions]";
const row = (id) => `${view} tbody tr[data-session="${id}"]`;
const inspectorHas = (pattern) =>
  page.waitForFunction((p) => new RegExp(p, "s").test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);
try {
  await h.workload();
  assert.deepEqual(await texts(".source-list .source-group:nth-child(1) .source-label"), WORKLOAD_GROUPS.Queries);
  assert.equal(await page.$("#server-nav-running"), null);
  await h.view("sessions");
  // Opening the view reads once, because it is on screen.
  await page.waitForSelector(`${view} tbody tr`);
  await h.idle(view);
  assert.equal(state.seen.length, 1, "opening reads once");
  assert.equal(await page.$eval(`${view} .segmented button[data-value=active]`, (e) => e.getAttribute("aria-pressed")), "true",
    "Active is the default");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0");
  assert.match(await h.popover("About Processlist"), /Active hides sleeping sessions without a transaction/);
  // One toolbar row at 1440 px: Kill sessions…, Auto-refresh and Refresh stay; the
  // list's filters are in the row above it.
  assert.ok(await page.$eval(`${view} .view-toolbar`, (e) => e.getBoundingClientRect().height) <= 48, "the toolbar keeps one row");
  assert.match(await text(`${view} .view-toolbar`), /Kill sessions…/);
  assert.doesNotMatch(await text(`${view} .view-toolbar`), /Mass kill/i);
  for (const sel of ['[aria-label="Sessions to list"]', 'select[aria-label="Processlist source"]', 'input[aria-label="Running at least (seconds)"]', 'input[aria-label="Filter sessions"]'])
    assert.ok(await page.$(`${view} .sessions-tools ${sel}`) && !(await page.$(`${view} .view-toolbar ${sel}`)), `${sel} is in the filter row`);

  // Load: the counter strip, one summary line, the table sorted problems first.
  assert.deepEqual([state.seen[0].active_only, state.seen[0].min_seconds], [true, 0]);
  assert.equal(state.seen[0].connection.host, "127.0.0.1");
  const summary = await text(`${view} .summary-line`);
  assert.match(summary, /Problem\s*9 active · 2 idle in transaction · 2 waiting for a lock · 5 running 10 s or more · oldest transaction \d+ min \d+ s/);
  assert.ok(summary.replace(/\d+ notes?$/, "").length <= 130, "the summary is one line");
  const facts = await text(`${view} .fact-strip`);
  assert.match(facts, /Connected\s*12/);
  assert.match(facts, /Limit\s*151/);
  assert.match(facts, /Max seen\s*12/);
  assert.match(facts, /History list\s*[\d,]+/);
  // The counters are server-wide status counters, not sessions of the list:
  // the group is labeled so, with the reason in its tooltip.
  assert.equal(await page.$eval(`${view} dl.fact-strip`, (e) => e.getAttribute("aria-label")), "Server-wide");
  assert.equal(await page.$eval(`${view} dl.fact-strip`, (e) => e.title),
    "Threads_connected and Threads_running from SHOW GLOBAL STATUS; they include monitoring and background threads.");
  // Kill sessions… opens the review dialog, titled Kill sessions; Escape closes
  // it without sending anything.
  const killSessions = `${view} .view-toolbar button[data-action=kill-sessions]`;
  assert.equal(await text(killSessions), "Kill sessions…");
  assert.equal(await page.$eval(killSessions, (b) => b.getAttribute("aria-haspopup")), "dialog");
  await page.click(killSessions);
  await page.waitForSelector("dialog.mass-kill-dialog[open]");
  assert.equal(await page.$eval("dialog.mass-kill-dialog", (d) => document.getElementById(d.getAttribute("aria-labelledby"))?.textContent?.trim()),
    "Kill sessions");
  assert.equal(await page.$eval("dialog.mass-kill-dialog .segmented", (e) => e.getAttribute("aria-label")), "Kill mode");
  assert.doesNotMatch(await text("dialog.mass-kill-dialog"), /mass kill/i);

  // Ignore users offers the users of the loaded sessions as a pick list, and
  // Match host offers their client hosts; both keep a free-text path.
  assert.deepEqual(
    await page.$$eval(".mass-kill-user-picks .mass-kill-checkbox-label", (es) => es.map((e) => e.textContent.trim())),
    ["noproc", "root"],
    "Ignore users lists the sessions' users",
  );
  assert.deepEqual(await page.$$eval("#mk-hosts option", (es) => es.map((e) => e.value)), ["172.17.0.1"], "Match host offers the client hosts");

  await button("All clients", "dialog.mass-kill-dialog ");
  const eligible = active.sessions.filter(s => s.thread_kind === "client" && !s.flags.includes("own_connection") && !s.flags.includes("system_thread"));
  // Ticking a user in the pick list filters exactly like typing the name did,
  // and the chosen names stay visible.
  const ignoreBox = async (name) => {
    const handle = await page.evaluateHandle((n) => [...document.querySelectorAll(".mass-kill-user-picks .mass-kill-checkbox-label")]
      .find((e) => e.textContent.trim() === n)?.querySelector("input"), name);
    const el = handle.asElement();
    assert.ok(el, `Ignore users checkbox ${name}`);
    return el;
  };
  const clearInput = (selector) => page.$eval(selector, (input) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, "");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 9 matching sessions");
  await (await ignoreBox("root")).click();
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 1 matching sessions");
  assert.deepEqual(await page.$$eval(".mass-kill-table tbody tr td:nth-child(2) strong", (es) => es.map((e) => Number(e.textContent))), [103],
    "ticking root ignores exactly the root sessions");
  assert.equal(await text(".mass-kill-ignore-summary"), "Ignoring: root");
  await (await ignoreBox("root")).click();
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 9 matching sessions");
  // A name outside the list still goes in the text field.
  await page.type("#mk-ignore-extra", "noproc");
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 8 matching sessions");
  assert.equal(await text(".mass-kill-ignore-summary"), "Ignoring: noproc");
  await clearInput("#mk-ignore-extra");
  // Match host still takes free text (patterns); the datalist only suggests.
  await page.type("#mk-host", "10.9.");
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 0 matching sessions");
  await clearInput("#mk-host");
  await page.waitForFunction(() => document.querySelector(".preview-title strong")?.textContent === "Preview: 9 matching sessions");
  for (const pattern of [String.raw`\S`, String.raw`\D`, String.raw`\W`, String.raw`\B`, " SELECT", "["]) {
    await page.$eval("#mk-query", (input, value) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }, pattern);
    const expected = pattern === "[" ? [] : eligible.filter(s => new RegExp(pattern, "i").test(s.info || s.info_short || s.last_statement || "")).map(s => s.id);
    await page.waitForFunction(n => document.querySelector(".preview-title strong")?.textContent === `Preview: ${n} matching sessions`, {}, expected.length);
    assert.deepEqual(await page.$$eval(".mass-kill-table tbody tr td:nth-child(2) strong", es => es.map(e => Number(e.textContent))), expected, `pattern ${pattern} is preserved`);
    // The preview's Time is a duration in words, with the exact seconds on hover.
    for (const cell of await page.$$eval(".mass-kill-table tbody tr td:nth-child(6)", (es) => es.map((e) => [e.textContent.trim(), e.querySelector("abbr")?.title || ""]))) {
      assert.match(cell[0], /^\d+ s$|^\d+ min \d+ s$|^\d+ h \d+ min$|^\d+ d \d+ h$/, `a duration in words (${cell[0]})`);
      assert.match(cell[1], /^[\d,]+ seconds$/, `the exact seconds on hover (${cell[1]})`);
    }
    assert.equal(await page.$eval("#mk-query", e => e.getAttribute("aria-invalid")), String(pattern === "["));
    if (pattern === "[") {
      assert.equal(await text("#mk-query-error"), "Not a valid pattern");
      assert.equal(await page.$eval(".mass-kill-footer-actions button.danger", b => b.disabled), true, "an invalid pattern cannot select a kill target");
    }
  }

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.mass-kill-dialog"));
  assert.ok(!state.other.some((r) => r.path.startsWith("/api/live/kill")), "nothing sent");
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.session)),
    active.sessions.map((s) => String(s.id)));
  assert.equal(await page.$eval(row(93), (e) => e.dataset.status), "problem");
  assert.ok(await page.$(`${row(94)} td[data-col=id] [role=img][aria-label="Waiting for a lock"]`));
  assert.ok(await page.$(`${row(93)} td[data-col=id] [role=img][aria-label="Holding locks"]`));
  // The list fits beside the list of views at 1440 px: Kill and Show plan
  // live in the row menu, and Flags, Database and Command start off (their
  // words are on the status, in State, in the details and in the Breakdown).
  assert.deepEqual(await page.$$eval(`${view} thead th[data-col]`, els => els.map(e => e.dataset.col)),
    ["status", "id", "user", "state", "time", "statement", "lock"]);
  assert.equal(await page.$eval(`${view} .data-table-wrap`, (e) => e.scrollWidth - e.clientWidth), 0, "no sideways scroll at 1440 px");
  assert.equal(await text(`${view} th[data-col=time]`), "Time");
  assert.equal(await page.$eval(`${view} th[data-col=time]`, (e) => e.title), "How long the session has been in its current state");
  assert.equal(await page.$eval(`${view} th[data-col=state]`, (e) => e.title), "What the session is doing now: the server's State, or its command (Sleep, Daemon) when it reports none");
  // State is its own column (MySQL's Command, State, Time order), and Time says
  // the duration in words with the exact seconds on hover.
  assert.equal(await text(`${row(95)} td[data-col=state]`), "Waiting for table metadata lock");
  assert.equal(await text(`${row(100)} td[data-col=state]`), "executing");
  assert.equal(await text(`${row(93)} td[data-col=state]`), "Sleep", "no state shows the command");
  assert.equal(await page.$eval(`${row(93)} td[data-col=state] span`, (e) => [e.className, e.title].join("|")), "cell-muted|No state; command Sleep");
  assert.equal(await text(`${row(93)} td[data-col=time]`), "20 min 50 s");
  assert.equal(await page.$eval(`${row(93)} td[data-col=time] abbr`, (e) => e.title), "1,250 seconds");
  assert.equal(await text(`${row(100)} td[data-col=time]`), "36 s");
  for (const t of await texts(`${view} td[data-col=time]`)) assert.doesNotMatch(t, /\d\.\d+m|^\d+m$/, `never a compact number (${t})`);
  assert.equal(await page.$eval(`${row(94)} td[data-col=status] [title]`, (e) => e.title), "Long running, Lock wait");
  assert.equal(await page.$eval(`${row(93)} td[data-col=status] [title]`, (e) => e.title), "Idle in transaction");
  assert.equal(await page.$(`${row(93)} td[data-col=actions]`), null, "no per-row Kill button");
  assert.match(await text(row(93)), /Last: SELECT \* FROM u WHERE id = 1/);
  // The badge: active sessions, in red while something is a problem.
  assert.equal(await text("#server-nav-sessions .count-badge"), "9");
  // State is the dot and its word beside a neutral count (one marker system).
  assert.equal(await page.$eval("#server-nav-sessions .count-badge", (e) => e.className), "count-badge is-neutral");
  assert.ok(await page.$("#server-nav-sessions .status-dot.is-problem"), "problem dot beside the count");
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
  const durationFact = (label) => page.$$eval('.view-inspector .inspector-fields > div', (rows, name) => {
    const value = rows.find((e) => e.querySelector('dt')?.textContent === name)?.querySelector('dd abbr');
    return [value?.textContent, value?.title, value?.getAttribute('aria-label')];
  }, label);
  assert.deepEqual(await durationFact("Time in state"), ["20 min 50 s", "1,250 seconds", "1,250 seconds"]);
  assert.deepEqual(await durationFact("Open for"), ["20 min 50 s", "1,250 seconds", "1,250 seconds"]);
  assert.match(idle, /Idle in transaction: a session idle \(Sleep\) inside an open InnoDB transaction keeps its row locks/);
  assert.equal(await page.$('.view-inspector [aria-label^="Show plan"]'), null, "an idle session has no plan");
  assert.equal(await page.$eval('.view-inspector [aria-label="Kill: connection 93"]', (e) => e.disabled), false);
  // Full-width details preserve the list's complete column choices.
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  assert.deepEqual(await page.$$eval(`${view} thead th[data-col]`, els => els.map(e => e.dataset.col)),
    ["status", "id", "user", "state", "time", "statement", "lock"]);
  const readsBeforeColumns = state.seen.length;
  // Flags is in the Columns menu, off by default; showing it brings the pills back.
  await page.click(`${view} .table-columns-button`);
  assert.equal(await page.$eval('.popover input[data-col=flags]', (e) => e.checked), false);
  await page.click('.popover input[data-col=flags]');
  await page.keyboard.press("Escape");
  assert.deepEqual(await page.$$eval(`${row(94)} td[data-col=flags] .pill`, (els) => els.map((e) => e.textContent)), ["Long running", "Lock wait"]);
  assert.deepEqual(await page.$$eval(`${row(93)} td[data-col=flags] .pill`, (els) => els.map((e) => e.textContent)), ["Idle in transaction"]);
  // Database starts off too; showing it survives the full-width details.
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover input[data-col=db]');
  await page.keyboard.press("Escape");
  assert.ok(await page.$(`${view} th[data-col=db]`), "Columns shows Database");
  await h.select(row(93));
  await page.click(".detail-back");
  assert.ok(await page.$(`${view} th[data-col=db]`), "explicit column choices survive details");
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover .columns-reset');
  await page.keyboard.press("Escape");
  assert.equal(await page.$(`${view} th[data-col=db]`), null, "Reset turns Database off again");
  assert.equal(await page.$(`${view} th[data-col=flags]`), null, "Reset turns Flags off again");
  await page.click(`${view} .table-columns-button`);
  await page.click('.popover input[data-col=flags]');
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${view} th[data-col=flags]`);
  assert.equal(state.seen.length, readsBeforeColumns, "column choices never read the server");
  await page.focus(row(93));
  // A lock wait says what it waits for and opens Locks.
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await inspectorHas(/Connection 94/);
  assert.match(await h.inspector(), /Waiting \d+ min \d+ s for an InnoDB lock \(a row lock, or a table lock such as AUTO-INC\)/);
  assert.match(await h.inspector(), /UPDATE t SET v = 0 WHERE id = 10/);
  assert.deepEqual(await durationFact("Lock wait"), ["20 min 49 s", "1,249 seconds", "1,249 seconds"]);
  await page.click(".detail-back");
  await page.waitForFunction(selector => document.activeElement?.matches(selector), {}, row(94));
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await inspectorHas(/for a metadata lock \(Waiting for table metadata lock\)/);
  await page.locator(".view-inspector button ::-p-text(Open Locks to see who blocks it)").click();
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  await h.view("sessions");
  assert.ok(await page.$(`${row(95)}[aria-current=true]`), "the report and the selection stay");

  // Kill: the confirmation sheet starts on Cancel; Escape and Cancel send
  // nothing and are recorded as declined; confirm sends the fingerprint.
  const declined = () => state.other.filter((o) => o.path === "/api/audit/declined").map((o) => o.body);
  // 93 blocks 94 in this read, so the Kill sheet and the outcome can name it.
  state.fallback = { status: 200, body: killable };
  await h.refresh();
  await h.idle(view);
  await h.select(row(93));
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  const sheet = await text("dialog.kill-dialog");
  assert.match(sheet, /End connection 93\?/);
  assert.match(sheet, /Server\s*Local MySQL.*127\.0\.0\.1:3306/);
  assert.match(sheet, /Server identity\s*sessions-server-uuid/);
  assert.match(sheet, /User\s*root@172\.17\.0\.1:60204 · mf/);
  assert.match(sheet, /State\s*Idle in transaction for \d+ min \d+ s/);
  assert.match(sheet, /KILL QUERY would not help/);
  // The Kill connection consequence box, with the session that keeps waiting.
  assert.match(sheet, /Kill connection 93 rolls back its open transaction\./);
  assert.match(sheet, /Connection 93 keeps its locks until it ends, so connection 94 keeps waiting until then\./);
  assert.match(sheet, /The application loses its connection and gets an error\./);
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
  state.queue.push({ status: 200, body: killFreed });
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(`${view} .kill-result.killed:not([data-refreshing])`);
  const sent = state.other.find((o) => o.path === "/api/live/kill").body;
  assert.equal(sent.confirmed, true, "confirmation is the literal boolean true");
  assert.deepEqual({ id: sent.connection_id, mode: sent.mode, expect: sent.expect },
    { id: 93, mode: "connection", expect: { user: "root", host: "172.17.0.1:60204", trx_started: "2026-10-14 18:00:53", ps_thread_id: null,
      connection_identity: reportConnectionKey(sent.connection), server_identity: all.server_identity } });
  assert.equal(state.seen.length, reads + 1, "the list refreshes after the kill");
  assert.equal(declined().length, 2, "a confirmed Kill is not recorded as declined");
  const killNoteText = await text(`${view} .kill-result`);
  assert.match(killNoteText, /^Done\. Ended connection 93\. Its open transaction was rolled back \(1 row changed\) and its locks were released\./);
  // The outcome comes from the fresh rows' blocked_by; the note carries the
  // waiter is known, but Active cannot prove the missing target disconnected.
  assert.match(killNoteText, /Connection 94 no longer waits\./);
  assert.equal(await page.$(`${view} .kill-result .kill-state.ok`), null);
  assert.ok(await page.$(`${view} .kill-result.killed.held`));
  assert.equal(await text(`${view} .kill-result .kill-update`), "Checked again: this list cannot show whether its transaction is still open.");
  assert.match(await text(`${view} .kill-result .kill-evidence`), /Transactionopen since 2026-10-14 18:00:53 → unknown/);
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();

  // The row menu (⋯): Show plan when the statement can be explained, and
  // Kill, which opens the same sheet. With the keyboard: the sheet starts on
  // Cancel, Escape sends nothing (recorded as declined) and focus returns to ⋯.
  // Connection 93 ended with that Kill; read the list that still has it (its
  // selection reopens the details, so go back to the list afterwards).
  state.fallback = { status: 200, body: killable };
  await h.refresh();
  await h.idle(view);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  const more = (id) => `${row(id)} button[aria-label="Actions for connection ${id}"]`;
  const menuItems = () => page.$$eval('.popover[role=menu] [role=menuitem]', (els) => els.map((e) => e.textContent.trim()));
  // The re-read list may restore its scroll: bring the row clear of the top bar.
  await page.waitForSelector(more(100), { visible: true });
  await page.$eval(more(100), (e) => e.scrollIntoView({ block: "center", inline: "nearest", behavior: "instant" }));
  await page.click(more(100));
  await page.waitForSelector('.popover[role=menu]');
  assert.deepEqual(await menuItems(), ["Show plan", "Kill…"]);
  assert.equal(await page.$(".view-inspector"), null, "the menu does not open the details");
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => document.activeElement?.matches(s), {}, more(100));
  await page.focus(more(93));
  await page.keyboard.press("Enter");
  await page.waitForSelector('.popover[role=menu]');
  assert.deepEqual(await menuItems(), ["Kill…"], "an idle session has no plan");
  await page.waitForFunction(() => document.activeElement?.getAttribute("role") === "menuitem");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Kill…");
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.match(await text("dialog.kill-dialog"), /End connection 93\?/);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  await page.waitForFunction((s) => document.activeElement?.matches(s), {}, more(93));
  while (declined().length < 3) await pause(25);
  assert.equal(declined()[2].target.connection_id, 93);
  assert.equal(state.other.filter((o) => o.path === "/api/live/kill").length, 1, "the row menu sends nothing until the confirm button");

  // The row's context menu (right click, Shift+F10 or the context menu key):
  // Copy query, and Kill query… and Kill connection… for that row, each
  // opening the same single Kill sheet with that KILL chosen. An item that
  // cannot run stays in the menu, focusable, with its reason under it.
  const ctx = ".context-menu[role=menu]";
  const ctxItems = () => page.$$eval(`${ctx} [role=menuitem]`, (els) => els.map((e) => ({
    label: e.querySelector(".context-menu-text > span, :scope > span:not(.context-menu-icon):not(.context-menu-text)")?.textContent.trim(),
    off: e.getAttribute("aria-disabled") === "true" || e.disabled, hint: e.querySelector(".context-menu-hint")?.textContent || "" })));
  const ctxItem = (label) => page.evaluateHandle((m, l) => [...document.querySelectorAll(`${m} [role=menuitem]`)]
    .find((e) => (e.querySelector(".context-menu-text > span, :scope > span:not(.context-menu-icon):not(.context-menu-text)")?.textContent || "").trim() === l), ctx, label);
  await page.click(`${row(100)} td[data-col=statement]`, { button: "right" });
  await page.waitForSelector(ctx);
  const running = await ctxItems();
  assert.deepEqual(running.map((i) => i.label).filter((l) => !/^Filter by/.test(l)),
    ["Show plan", "Open in Query editor", "Open in Locks", "Copy query", "Copy connection ID", "Kill query…", "Kill connection…", "Copy cell"]);
  assert.deepEqual(running.filter((i) => /^Kill/.test(i.label)).map((i) => [i.off, i.hint]), [[false, ""], [false, ""]], "both kills are offered for a running statement");
  assert.equal(await page.$$eval(`${ctx} [role=menuitem].is-danger`, (els) => els.length), 2, "Kill is drawn as destructive");
  await (await ctxItem("Kill query…")).asElement().click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.match(await text("dialog.kill-dialog"), /End connection 100\?/);
  assert.equal(await page.$eval('dialog.kill-dialog input[value="query"]', (e) => e.checked), true, "Kill query… opens the sheet on KILL QUERY");
  await page.waitForFunction(() => /Kill query on connection 100/.test(document.querySelector("dialog.kill-dialog .danger-solid")?.textContent || ""));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  // Focus returns to the row the menu was opened on.
  await page.waitForFunction((s) => document.activeElement?.matches(s), {}, row(100));
  while (declined().length < 4) await pause(25);
  assert.deepEqual([declined()[3].target.connection_id, declined()[3].target.mode], [100, "query"]);
  // From the keyboard on an idle session: Shift+F10 opens the menu; Kill
  // query… is off with its reason and stays focusable; Kill connection… runs.
  await page.focus(row(93));
  await page.keyboard.down("Shift"); await page.keyboard.press("F10"); await page.keyboard.up("Shift");
  await page.waitForSelector(ctx);
  const idleItems = await ctxItems();
  assert.deepEqual(idleItems.find((i) => i.label === "Kill query…"),
    { label: "Kill query…", off: true, hint: "Connection 93 runs no statement now; Kill connection… ends it." });
  assert.equal(idleItems.some((i) => i.label === "Show plan"), false, "an idle session has no plan");
  assert.equal(idleItems.find((i) => i.label === "Kill connection…").off, false);
  assert.equal(await page.$eval(`${ctx} [aria-disabled=true]`, (e) => e.disabled), false, "the reason can be reached");
  await page.keyboard.press("End");
  await page.waitForFunction(() => document.activeElement?.textContent.trim() === "Copy cell");
  await page.keyboard.press("ArrowUp");
  await page.waitForFunction(() => document.activeElement?.textContent.trim() === "Kill connection…");
  await page.keyboard.press("ArrowUp");
  await page.waitForFunction(() => /^Kill query…/.test(document.activeElement?.textContent.trim() || ""));
  await page.keyboard.press("Enter");
  await pause(200);
  assert.ok(await page.$(ctx), "a Kill that is off does nothing when chosen");
  assert.equal(await page.$("dialog.kill-dialog"), null);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.equal(await page.$eval('dialog.kill-dialog input[value="connection"]', (e) => e.checked), true);
  const killsBefore = state.other.filter((o) => o.path === "/api/live/kill").length;
  const readsBefore = state.seen.length;
  // Progress, then the answer at once, then the list read again.
  state.delay = 400;
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog .danger-solid")?.disabled);
  await page.click("dialog.kill-dialog .danger-solid");
  await page.waitForSelector(`${view} .kill-result[data-refreshing=true]`);
  assert.match(await text(`${view} .kill-result`), /^Done\. Ended connection 93/);
  assert.match(await text(`${view} .view-status`), /Reading the sessions…/, "the view's loading line says the list is read again");
  await page.waitForSelector(`${view} .kill-result.killed:not([data-refreshing])`);
  state.delay = 0;
  assert.equal(state.seen.length, readsBefore + 1, "the list is read again after the kill");
  const sentKill = state.other.filter((o) => o.path === "/api/live/kill");
  assert.equal(sentKill.length, killsBefore + 1);
  assert.deepEqual([sentKill.at(-1).body.connection_id, sentKill.at(-1).body.mode, sentKill.at(-1).body.confirmed], [93, "connection", true]);
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();

  // A minimum-duration filter hides the waiter without proving release.
  state.fallback = { status: 200, body: killable };
  await h.refresh();
  await h.idle(view);
  // The filter lives in the list toolbar, which the full-width details hide:
  // set it first, then open connection 93.
  await page.locator(`${view} input[aria-label="Running at least (seconds)"]`).fill("60");
  await h.idle(view);
  await h.select(row(93));
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  const hiddenWaiter = structuredClone(killFreed);
  hiddenWaiter.sessions = hiddenWaiter.sessions.filter(s => s.id !== 94);
  hiddenWaiter.filters.min_seconds = 60;
  delete hiddenWaiter.blocking_edges;
  state.fallback = { status: 200, body: hiddenWaiter };
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(`${view} .kill-result.killed:not([data-refreshing])`);
  assert.match(await text(`${view} .kill-result`), /Connection 94: this read cannot show whether it still waits\./);
  assert.equal(await page.$(`${view} .kill-result .kill-state.ok`), null, "a hidden waiter never turns the note green");
  assert.equal(await page.$(`${view} .kill-result li.free`), null, "an unknown waiter is not styled as released");
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();
  await page.locator(`${view} input[aria-label="Running at least (seconds)"]`).fill("0");

  // KILL CONNECTION still rolling back: amber until a timed follow-up read
  // finds the connection gone and its waiter free, then the green state line.
  state.fallback = { status: 200, body: killable };
  await h.refresh();
  await h.idle(view);
  await h.select(row(93));
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  const completedKill = kill.body;
  kill.body = { ...structuredClone(completedKill),
    message: "Connection 93 is ending: it is rolling back its transaction (about 120 changes left to undo) and keeps its locks until the rollback finishes.",
    after: { present: true, command: "Killed", state: "Killed", statement: null,
      trx_state: "ROLLING BACK", rows_modified: 120, trx_checked: true,
      trx_started: "2026-10-14 18:00:53", rows_locked: 1, same_trx: true, trx_survived: null } };
  const rollingBack = structuredClone(killable);
  const rollingSession = rollingBack.sessions.find((s) => s.id === 93);
  Object.assign(rollingSession, { command: "Killed", state: "Rolling back" });
  Object.assign(rollingSession.trx, { state: "ROLLING BACK", rows_modified: 120 });
  state.fallback = { status: 200, body: rollingBack };
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(`${view} .kill-result.killed:not([data-refreshing])`);
  assert.equal(await text(`${view} .kill-result .kill-state.warn`), "Rolling back: about 120 changes left to undo.");
  assert.match(await text(`${view} .kill-result`), /Connection 94 still waits/);
  rollingBack.filters.active_only = false;
  // The scope switch is in the list toolbar: leave the details first.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  await page.click(`${view} .segmented button[data-value=all]`);
  await h.idle(view);
  const readsAfterScopeChange = state.seen.length;
  const goneAndFreed = structuredClone(killFreed);
  goneAndFreed.sessions = goneAndFreed.sessions.filter((s) => s.id !== 93);
  goneAndFreed.filters.active_only = false;
  state.fallback = { status: 200, body: goneAndFreed };
  // The kill was confirmed in the Active scope, which cannot settle it: no timed
  // follow-ups ran, and the note keeps Refresh. Refresh in the All scope settles it.
  const readsBeforeFollow = state.seen.length;
  await page.locator(`${view} .kill-result button ::-p-text(Refresh)`).click();
  await page.waitForFunction(
    () => document.querySelector('.server-view[data-view=sessions] .kill-result .kill-state.ok')?.textContent === "Lock released: connection 94 no longer waits.",
    { timeout: 9000 },
  );
  assert.ok(state.seen.length > readsBeforeFollow, "the note's Refresh read the list again");
  assert.ok(state.seen.slice(readsAfterScopeChange).every(read => read.active_only === false),
    "later reads retain the newly selected All scope");
  assert.equal(await text(`${view} .kill-result .kill-update`), "Checked again: the connection has ended.");
  assert.match(await text(`${view} .kill-result .kill-evidence`), /Transactionopen since 2026-10-14 18:00:53 → rolled back/);
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();
  state.fallback = { status: 200, body: killable };
  await page.click(`${view} .segmented button[data-value=active]`);
  await h.idle(view);
  // Dismiss while the FIRST post-kill report is held, before followKill exists.
  state.fallback = { status: 200, body: killable };
  await h.refresh();
  await h.idle(view);
  await h.select(row(93));
  await page.click('.view-inspector [aria-label="Kill: connection 93"]');
  await page.waitForSelector("dialog.kill-dialog[open]");
  let releaseFirstReport;
  const heldFirstReport = new Promise((resolve) => { releaseFirstReport = resolve; });
  state.queue.push({ status: 200, body: rollingBack, wait: heldFirstReport });
  state.fallback = { status: 200, body: rollingBack };
  const firstReportStarted = page.waitForRequest((r) => new URL(r.url()).pathname === "/api/live/sessions");
  await page.click(".sheet-actions .danger-solid");
  await firstReportStarted;
  await page.waitForSelector(`${view} .kill-result[data-refreshing=true]`);
  await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();
  const readsAtDismiss = state.seen.length;
  const firstReportAnswered = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/live/sessions");
  releaseFirstReport();
  await firstReportAnswered;
  await pause(3000);
  assert.equal(state.seen.length, readsAtDismiss, "Processlist dismiss during the first re-read prevents follow-up requests");
  assert.equal(await page.$(`${view} .kill-result`), null, "the late answer cannot restore the dismissed note");
  kill.body = completedKill;
  state.fallback = { status: 200, body: killable };

  // A vanished session may leave prepared XA locks; a Killed session with a
  // checked, absent transaction must not get the rollback state line.
  for (const prepared of [true, false]) {
    state.fallback = { status: 200, body: killable };
    await h.refresh();
    await h.idle(view);
    await h.select(row(93));
    await page.click('.view-inspector [aria-label="Kill: connection 93"]');
    await page.waitForSelector("dialog.kill-dialog[open]");
    kill.body = { ...structuredClone(completedKill),
      message: prepared
        ? "Ended connection 93, but its transaction is still open as a prepared XA transaction (7 rows locked) and keeps its locks. Resolve it with XA RECOVER and then XA COMMIT or XA ROLLBACK."
        : "Connection 93 is closing; it has no open transaction, so nothing is rolled back.",
      after: { present: !prepared, command: prepared ? null : "Killed", statement: null,
        trx_state: null, trx_checked: !prepared, trx_started: null, rows_modified: null,
        rows_locked: prepared ? 7 : null, same_trx: null, trx_survived: prepared ? true : null } };
    const next = structuredClone(prepared ? killable : goneAndFreed);
    next.sessions = next.sessions.filter((s) => s.id !== 93);
    state.fallback = { status: 200, body: next };
    await page.click(".sheet-actions .danger-solid");
    await page.waitForSelector(`${view} .kill-result.killed:not([data-refreshing])`);
    if (prepared) {
      assert.equal(await text(`${view} .kill-result .kill-state.warn`), "Prepared XA transaction and locks still open.");
      assert.match(await text(`${view} .kill-result .kill-evidence`), /Transactionopen since 2026-10-14 18:00:53 → prepared XA still open/);
      assert.match(await text(`${view} .kill-result .kill-evidence`), /Rows locked1 → 7/);
    } else {
      assert.match(await text(`${view} .kill-result`), /Connection 93 is closing; it has no open transaction, so nothing is rolled back\./);
      assert.equal(await page.$(`${view} .kill-result .kill-state.warn`), null);
      assert.equal(await text(`${view} .kill-result .kill-state.ok`), "Lock released: connection 94 no longer waits.");
    }
    await page.locator(`${view} .kill-result ::-p-text(Dismiss)`).click();
  }
  kill.body = completedKill;
  state.fallback = { status: 200, body: killable };

  // Show plan for a running statement opens it in Plans.
  await h.select(row(100));
  await page.click('.view-inspector [aria-label="Show plan for connection 100"]');
  await page.waitForFunction(
    () => document.querySelector(".plan-heading h1")?.textContent === "Connection 100 · estimated plan",
    { timeout: 20000 },
  );
  assert.equal(state.other.findLast((o) => o.path === "/api/live/explain-connection").body.connection_id, 100);
  await h.workload();

  // All: idle and system threads too (a new read); a server thread is never killed.
  state.queue.push({ status: 200, body: all });
  await page.click(`${view} .detail-back`);
  await page.click(`${view} .segmented button[data-value=all]`);
  await page.waitForSelector(row(5));
  await h.idle(view);
  assert.equal(state.seen.at(-1).active_only, false);
  // Flags remain in the full-width list.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  assert.deepEqual(await page.$$eval(`${row(5)} .pill`, (els) => els.map((e) => e.textContent)), ["Server thread"]);
  // With Flags off, an OK row still names its informational flags in Status.
  assert.equal(await page.$eval(`${row(5)} td[data-col=status] [title]`, (e) => [e.textContent, e.title].join("|")), "Server thread|Server thread");
  // A daemon's state is in the State column; the statement cell no longer repeats it.
  assert.equal(await text(`${row(5)} td[data-col=state]`), "Waiting on empty queue");
  assert.equal(await text(`${row(5)} td[data-col=statement]`), "—", "the statement cell no longer repeats the state");
  await page.click(more(5));
  await page.waitForSelector('.popover[role=menu]');
  assert.deepEqual(await page.$eval('.popover[role=menu] .is-danger', (e) => [e.disabled, e.title]),
    [true, "A server thread, not a client session: myflames does not end it."], "the row menu never kills a server thread");
  await page.keyboard.press("Escape");
  await page.click(`${row(5)} td[data-col=id]`, { button: "right" });
  await page.waitForSelector(ctx);
  assert.equal((await ctxItems()).some((i) => /^Kill/.test(i.label)), false, "the context menu never offers to kill a server thread");
  await page.keyboard.press("Escape");
  await page.waitForFunction((m) => !document.querySelector(m), {}, ctx);
  await h.select(row(5));
  await inspectorHas(/Connection 5/);
  assert.equal(await page.$eval('.view-inspector [aria-label="Kill: connection 5"]', (e) => e.disabled), true);
  assert.match(await page.$eval('.view-inspector [aria-label="Kill: connection 5"]', (e) => e.title), /server thread/);
  // The filter narrows the rows on screen; the minimum time is a new read.
  await page.click(`${view} .detail-back`);
  await page.focus(`${view} input[aria-label="Filter sessions"]`);
  await page.keyboard.type("SLEEP(");
  await page.waitForFunction(() => document.querySelectorAll(".server-view[data-view=sessions] tbody tr").length === 2);
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.map((e) => e.dataset.session)), ["97", "103"]);
  await page.evaluate(() => {
    const input = document.querySelector(".server-view[data-view=sessions] input[aria-label=\"Filter sessions\"]");
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, "");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForFunction(() => document.querySelectorAll(".server-view[data-view=sessions] tbody tr").length > 2);
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

  // An empty table under Active with active sessions in the summary means the
  // filters hid them; with none active the table says so plainly.
  const minField = `${view} input[aria-label="Running at least (seconds)"]`;
  // A minimum duration has its own, more specific sentence: clear it here,
  // and look at the Active scope (the All scope says "No sessions.").
  await page.locator(minField).fill("0");
  await page.click(`${view} .segmented button[data-value=active]`);
  await h.idle(view);
  const emptied = structuredClone(active);
  emptied.sessions = [];
  emptied.matched = 0;
  state.queue.push({ status: 200, body: emptied });
  await h.refresh();
  await h.idle(view);
  assert.equal(await text(`${view} .table-empty`), "No sessions match the filters.", "the summary still counts 9 active sessions");
  const quiet = structuredClone(emptied);
  quiet.summary = { ...emptied.summary, active: 0 };
  state.queue.push({ status: 200, body: quiet });
  await h.refresh();
  await h.idle(view);
  assert.equal(await text(`${view} .table-empty`), "No active sessions right now.");
  await page.locator(minField).fill("30");
  await page.click(`${view} .segmented button[data-value=all]`);
  await h.idle(view);
  state.queue.push({ status: 200, body: active });
  await h.refresh();
  await page.waitForSelector(`${view} tbody tr`);
  await h.idle(view);

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

  // Narrow: the view is pushed with a back button; details use the full work area that Escape closes.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.click(row(97));
  await page.waitForSelector(".view-inspector.is-detail");
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
  await h.workload();
  await page.waitForSelector("#server-nav-sessions[aria-current=page]");
  assert.equal(await page.$eval(`${view} .segmented button[data-value=all]`, (e) => e.getAttribute("aria-pressed")), "true");
  assert.equal(await page.$eval(`${view} input[aria-label="Running at least (seconds)"]`, (e) => e.value), "30");
  await page.waitForSelector(`${view} tbody tr`);

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

  // A session up for days (a daemon or a replication thread) reads as a
  // duration in words, never "1.6m", and sorting by Time still orders by seconds.
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  const longRun = structuredClone(active);
  longRun.sessions = [{
    id: 900, user: null, host: null, client_host: null, db: null, command: "Daemon",
    time_s: 1598400, state: "Waiting on empty queue", info: null, info_short: null, info_truncated: false,
    thread_kind: "daemon", trx: null, lock_wait: null, last_statement: null, progress: null,
    memory_used: null, rows_examined: null, explainable: false, flags: ["system_thread"], status: "ok", why: null, active: false,
  }, ...longRun.sessions];
  state.fallback = { status: 200, body: longRun };
  await h.refresh();
  await page.waitForSelector(row(900));
  await h.idle(view);
  assert.equal(await text(`${row(900)} td[data-col=time]`), "18 d 12 h");
  assert.equal(await page.$eval(`${row(900)} td[data-col=time] abbr`, (e) => e.title), "1,598,400 seconds");
  assert.equal(await page.$eval(`${row(900)} td[data-col=time] abbr`, (e) => e.getAttribute("aria-label")), "1,598,400 seconds");
  assert.equal(await text(`${row(900)} td[data-col=state]`), "Waiting on empty queue");
  await page.click(`${view} th[data-col=time] button`);
  assert.equal(await page.$eval(`${view} th[data-col=time]`, (e) => e.getAttribute("aria-sort")), "descending");
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.slice(0, 2).map((e) => e.dataset.session)), ["900", "93"], "largest first, by seconds");
  await page.click(`${view} th[data-col=time] button`);
  assert.deepEqual(await page.$$eval(`${view} tbody tr`, (els) => els.slice(0, 2).map((e) => e.dataset.session)), ["98", "102"], "smallest first, by seconds");
  await page.click(`${view} th[data-col=time] button`);

  // DDL progress is additive: same session, exact counters in its inspector,
  // compact text plus a bar on the existing State cell, and opt-in following.
  const ddlReport = structuredClone(active);
  const ddlItem = { ...ddlProgress.items[0], id: 95 };
  ddlReport.ddl = { ...ddlProgress, items: [ddlItem] };
  ddlReport.sessions.find(x => x.id === 95).ddl = ddlItem;
  state.fallback = { status: 200, body: ddlReport };
  await h.refresh();
  await page.waitForSelector(`${row(95)} .ddl-meter progress`);
  assert.match(await text(`${row(95)} td[data-col=state]`), /42%/);
  assert.match(await page.$eval(`${row(95)} .ddl-meter`, el => el.title), /Copying rows.*2 min 54 s left \(estimate\)/);
  await button("Follow progress", `${view} `);
  assert.equal(await page.$eval(`${view} .auto-refresh select`, el => el.value), "10");
  await page.select(`${view} .auto-refresh select`, "0");
  await page.click(`${row(95)} td[data-col=state]`, { clickCount: 2 });
  await inspectorHas(/DDL progress.*Copying rows.*stage\/sql\/copy to tmp table.*Elapsed.*2 min 6 s.*Time left \(estimate\).*2 min 54 s.*42[,.]?000 of 100[,.]?000/);
  for (const width of [1440, 720]) {
    await page.setViewport({ width, height: 900 });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await pause(200);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(h.shots, `ddl-progress-${width}-${scheme}.png`), fullPage: true });
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.click(`${view} .detail-back`);
  const missingDdl = structuredClone(ddlReport);
  missingDdl.ddl = { available: false, source: null, items: [], missing: ["events_stages_current"],
    reason: "Turn on events_stages_current for DDL stage tracking." };
  missingDdl.sessions.find(x => x.id === 95).ddl = null;
  state.fallback = { status: 200, body: missingDdl };
  await h.refresh();
  await page.waitForSelector(`${view} [data-instruments-row="consumer:events_stages_current"]`);
  assert.match(await text(`${view} .ddl-unavailable`), /events_stages_current.*Turn on stage tracking…/);
  const writesBefore = state.other.filter(x => /reviewed.*apply|instruments.*apply/.test(x.path)).length;
  await page.click(`${view} [data-instruments-row="consumer:events_stages_current"]`);
  await h.onView("instruments");
  await page.waitForFunction(() => /events_stages_current/.test(document.querySelector(".instruments-panel .detail-title, .instruments-panel .inspector-title, .instruments-panel .view-inspector")?.textContent || ""));
  assert.equal(state.other.filter(x => /reviewed.*apply|instruments.*apply/.test(x.path)).length, writesBefore,
    "navigation never turns tracking on by itself");
  assert.deepEqual(h.errors, []);
  console.log("sessions view: ok");
} finally {
  await h.close();
}
