// Locks view of the Server place: mocked /api/live/locks with real locks-1.0
// and locks-1.1 reports captured from live servers (fixtures/), real Python
// server for plans. One summary line, waiting DDL and the blocking tree as
// outlines, one table at a time for transactions, row locks, metadata locks and
// the latest deadlock; details, Show plan and Kill are in the inspector, and
// Kill opens a confirmation sheet.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { assertPrivilege as assertPrivilegeState, privilegeAnswer, DOCK_KEY } from "./perf-harness.mjs";


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

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));
const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));
const mysql = await load("locks-mysql-8.4.json");
const mysqlOld = await load("locks-mysql-8.0.39.json");
const mariadb = await load("locks-mariadb-13.0-ps-off.json");
const noProcess = await load("locks-mysql-8.4-no-process.json");
// locks-1.1 reports captured live during an ALTER TABLE blocked by an idle transaction:
// MySQL 26.7 (CONNECTION_ADMIN through a default role, exact blockers) and MariaDB 11.4
// with Performance Schema off (likely blockers).
const ddl = await load("locks-mysql-26.7-ddl.json");
const ddlLikely = await load("locks-mariadb-11.4-ddl-likely.json");
// Synthetic identities supplement the reports captured before identity binding.
ddl.server_identity = { server_uuid: "locks-server-uuid" };
ddlLikely.server_identity = { server_id: "1", hostname: "locks-mariadb", version: ddlLikely.server_version };
const reportConnectionKey = (c) => JSON.stringify([
  c.profile_id || "", c.host || "", Number(c.port) || 0, c.user || "", c.ssl_mode || "", c.ssl_ca || "",
]);

// A second table so the row-lock filter has something to choose.
mysql.row_locks.push({
  ...mysql.row_locks.find((l) => l.status === "GRANTED" && l.lock_type === "RECORD"),
  table: "orders", lock_id: "extra", lock_data: "42",
});
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-locks-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);
const raw = JSON.stringify({
  query: "UPDATE t SET v=1 WHERE id=1",
  operation: "Update t", access_type: "table", table_name: "t",
  estimated_rows: 1, estimated_total_cost: 1.1,
  inputs: [{ operation: "Single-row index lookup on t using PRIMARY (id = 1)",
             access_type: "index", table_name: "t", index_name: "PRIMARY",
             estimated_rows: 1, estimated_total_cost: 1.1 }],
});
const warning = "Estimated plan: the optimizer's chosen plan for a running statement.";
const plan = {
  schema_version: "explain-connection-1.0", raw, mode: "explain", engine: "mysql",
  server_version: "8.4.11", connection_id: 24, query: "UPDATE t SET v=1 WHERE id=1",
  warnings: [warning], captured_at: "2026-10-12T21:40:00+00:00",
  capture: {
    schema_version: "capture-1.0", raw, query: "UPDATE t SET v=1 WHERE id=1", mode: "explain",
    engine: "mysql", server_version: "8.4.11", captured_at: "2026-10-12T21:40:00+00:00",
    conditions: "In-flight statement on connection 24 (estimated plan, EXPLAIN FOR CONNECTION).",
    warnings: [warning], runs: [], schema: {}, table_stats: {}, variables: {}, trace: null,
  },
};
const mocks = {
  "/api/live/privileges": { status: 200, body: { schema_version: "privileges-1.0", account: "viewer@%", actions: {} } },
  "/api/live/locks": { status: 200, body: mysql },
  "/api/live/explain-connection": { status: 200, body: plan },
  // A cancelled Kill is recorded in the audit trail (dbops.py) as declined.
  "/api/audit/declined": { status: 200, body: { audit_id: "a1", status: "declined" } },
};
const seen = [];

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
  await browser.defaultBrowserContext().overridePermissions(url, ["clipboard-read", "clipboard-write"]);
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    const mock = mocks[pathname];
    if (!mock) return request.continue();
    const body = JSON.parse(request.postData() || "{}");
    if (pathname === "/api/live/kill") assert.match(body.review_token, /^[A-Za-z0-9_-]{43}$/);
    seen.push({ path: pathname, ...body });
    request.respond({ status: mock.status, contentType: "application/json", body: JSON.stringify(mock.body) });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  // The list of views docked, as a person who docked it: this suite asserts
  // the list's markers and keys (server-nav covers the floating default).
  await page.evaluateOnNewDocument((key) => {
    try { if (localStorage.getItem(key) === null) localStorage.setItem(key, "false"); } catch {}
  }, DOCK_KEY);
  await page.goto(url, { waitUntil: "networkidle0" });
  const view = ".server-view[data-view=locks]";
  const button = (text, scope = "") => page.locator(`${scope}button ::-p-text(${text})`).click();
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  // The Kill sheet's label, without the privilege reason that describes it (aria-describedby).
  const sheetLabel = () => page.$eval(".sheet-actions .danger-solid", (e) => {
    const label = e.cloneNode(true);
    label.querySelectorAll(".account-sr").forEach((x) => x.remove());
    return label.textContent;
  });
  const texts = (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent));
  const inspector = () => text(".view-inspector");
  const inspectorHas = (pattern) =>
    page.waitForFunction((p) => new RegExp(p, "s").test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);
  const lockCalls = () => seen.filter((s) => s.path === "/api/live/locks");
  const idle = () => page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  const reload = async () => {
    const before = lockCalls().length;
    await page.click(`${view} [data-action=refresh]`);
    await idle();
    assert.equal(lockCalls().length, before + 1);
  };
  /** Select a row (tree row, table row, or grouped row) and wait for the inspector. */
  const select = async (selector) => {
    if (await page.$(".detail-back")) await page.click(".detail-back");
    const row = await page.waitForSelector(selector);
    const target = (await row.$("[data-row-key]")) || row;
    await target.click();
    await page.waitForSelector(".view-inspector .inspector-title");
  };
  const treeRow = (id) => `${view} .lock-tree tr[data-connection="${id}"]`;
  const show = async (name) => {
    if (await page.$(".detail-back")) await page.click(".detail-back");
    await page.click(`${view} .segmented button[data-value=${name}]`);
  };

  await button("Server", ".main-nav ");
  await page.waitForSelector(".server-place .source-list");

  // Keyboard: the sub-navigation moves with the arrow keys; End reaches the last view.
  await page.focus("#server-nav-health");
  await page.keyboard.press("End");
  await page.waitForFunction(() => {
    const items = document.querySelectorAll(".source-list .source-item");
    return items.length > 0 && document.activeElement === items[items.length - 1];
  });
  await page.focus("#server-nav-errorlog");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-locks");
  assert.equal(await page.$eval("#server-nav-locks", (e) => e.getAttribute("aria-current")), "page");
  // A view reads once when it first appears on screen (AGENTS: reads on screen, once per connection).
  await page.waitForSelector(`${view} .lock-tree`);
  assert.equal(lockCalls().length, 1, "opening the view reads once on its own");
  assert.equal(await page.$(`${view} .view-empty`), null, "no Load button once the automatic read answered");
  assert.match(await text(`${view} .view-toolbar`), /Waits only/);

  // The automatic read: summary, blocking tree with root blockers first.
  assert.deepEqual(
    { limit: lockCalls().at(-1).limit, waits_only: lockCalls().at(-1).waits_only, host: lockCalls().at(-1).connection.host },
    { limit: 500, waits_only: false, host: "127.0.0.1" },
  );
  assert.equal(await text(`${view} .summary-line .status-pill`), "Problem");
  assert.equal(await text(`${view} .summary-text`), "Blocked · 5 blocked sessions · 2 root blockers (idle in transaction)");
  assert.match(await text(`${view} .fact-strip`), /Blocked sessions\s*5/);
  assert.match(await text(`${view} .fact-strip`), /Root blockers\s*2/);
  const summaryDurations = await page.$$eval(`${view} .fact-strip > div`, (rows) => rows
    .filter((e) => ["Longest wait", "Oldest transaction"].includes(e.querySelector('dt')?.textContent))
    .map((e) => [e.querySelector('dt').textContent, e.querySelector('dd').textContent, e.querySelector('abbr')?.title]));
  assert.deepEqual(summaryDurations, [["Longest wait", "7 min 51 s", "471 seconds"], ["Oldest transaction", "7 min 52 s", "472 seconds"]]);
  assert.equal(await text("#server-nav-locks .count-badge"), "5", "the badge counts blocked sessions");
  assert.match(await text("#server-nav-locks .visually-hidden"), /5 blocked/);
  assert.equal((await page.$$(`${view} .lock-tree tr.root`)).length, 2);
  const first = await text(treeRow(23));
  assert.match(first, /Connection 23/);
  assert.match(first, /Blocker/);
  const roles = await texts(`${treeRow(23)} .pill`);
  assert.equal(roles[0], "Blocker");
  assert.match(roles[1], /^Idle transaction/);
  assert.ok((await texts(`${treeRow(24)} .pill`)).includes("Waiting"));
  assert.match(first, /Idle transaction/);
  assert.match(first, /blocks 3/);
  assert.match(first, /Last: SELECT id FROM t WHERE k BETWEEN 50 AND 70 FOR UPDATE/);
  // A locks-1.0 report has no DDL group and no Kill action (no privilege information).
  assert.equal(await page.$(`${view} .ddl-section`), null);
  // The chain 23 -> 24 -> 27 nests (aria-level), each with its wait and lock.
  const chain = await page.$$eval(`${view} .lock-tree tbody tr`, (els) =>
    els.map((e) => [e.querySelector(".lock-conn")?.textContent, e.getAttribute("aria-level")]));
  assert.deepEqual(chain, [["Connection 23", "1"], ["Connection 24", "2"], ["Connection 27", "3"], ["Connection 25", "2"],
    ["Connection 26", "1"], ["Connection 28", "2"], ["Connection 29", "3"]]);
  const waits = await texts(`${view} .lock-tree tbody td:nth-child(2)`);
  assert.ok(waits.some((w) => /row lock X,REC_NOT_GAP \(record only\) on lk\.t index PRIMARY \[1\]/.test(w)), waits.join("|"));
  assert.ok(waits.some((w) => /insert intention/.test(w)));
  assert.ok(waits.some((w) => /metadata SHARED_READ on table lk\.m, queued behind a waiting request/.test(w)));
  // Waits for is capped (full text in the tooltip and inspector) so Statement
  // keeps real width at 1440 px and ends in an ellipsis instead of the frame edge.
  const tree = await page.$eval(`${view} .lock-tree`, (wrap) => ({
    overflow: wrap.scrollWidth - wrap.clientWidth,
    statement: Math.round(wrap.querySelector("td.tree-statement").getBoundingClientRect().width),
    waits: Math.max(...[...wrap.querySelectorAll("td.cell-flex .cell-truncate")].map((e) => e.getBoundingClientRect().width)),
    titled: [...wrap.querySelectorAll("td.cell-flex .cell-truncate")].every((e) => e.title === e.textContent),
  }));
  assert.equal(tree.overflow, 0, "the blocking tree fits at 1440 px");
  assert.ok(tree.statement >= 200, `Statement keeps at least 200 px (${tree.statement})`);
  assert.ok(tree.waits < 360, `Waits for is capped (${tree.waits})`);
  assert.ok(tree.titled, "each capped wait carries its full text as a tooltip");
  // Disclosure triangles: Left collapses a blocker's chain, Right expands it again.
  await page.focus(treeRow(23));
  await page.keyboard.press("ArrowLeft");
  await page.waitForFunction((s) => document.querySelector(s)?.getAttribute("aria-expanded") === "false", {}, treeRow(23));
  assert.equal(await page.$(treeRow(24)), null, "the chain is hidden");
  await page.keyboard.press("ArrowRight");
  await page.waitForSelector(treeRow(24));

  // The root blocker in the inspector: its last statement, no plan, and KILL to copy (never sent).
  await select(treeRow(23));
  // Lock structs and Isolation start off so the list fits beside the list of
  // views at 1440 px; the Columns menu shows them and the details list both.
  assert.deepEqual(await page.$$eval(`${view} table[aria-label="Transactions"] th[data-col]`, (els) => els.map((e) => e.dataset.col)),
    ["trx", "conn", "stmt", "state", "age", "session", "locked"], "full-width lists retain their columns");
  assert.match(await text(`${view} th[data-col=age]`), /Open for/);
  const idleBadge = await page.$$eval(`${view} table[aria-label="Transactions"] td[data-col=session] .pill`, (els) => els.map((e) => [e.textContent, e.querySelector('abbr')?.title]));
  assert.ok(idleBadge.some(([words, exact]) => words === "Idle 7 min 53 s" && exact === "473 seconds"), JSON.stringify(idleBadge));
  // The age is a duration in words with the exact seconds on hover.
  const ageCells = await page.$$eval(`${view} table[aria-label="Transactions"] td[data-col=age]`, (els) => els.map((e) => [e.textContent.trim(), e.querySelector("abbr")?.title || ""]));
  assert.ok(ageCells.some(([t, title]) => t === "7 min 52 s" && title === "472 seconds"), JSON.stringify(ageCells));
  for (const [t] of ageCells) assert.match(t, /^\d+ s$|^\d+ min \d+ s$|^\d+ h \d+ min$|^\d+ d \d+ h$/, `a duration in words (${t})`);
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  assert.equal(await page.$eval(`${view} .data-table-wrap[aria-label="Transactions"]`, (e) => e.scrollWidth - e.clientWidth), 0,
    "Transactions fit at 1440 px");
  await page.click(`${view} table[aria-label="Transactions"] .table-columns-button`);
  assert.deepEqual(await page.$$eval('.popover input[data-col=structs], .popover input[data-col=iso]', (els) => els.map((e) => e.checked)), [false, false]);
  await page.click('.popover input[data-col=iso]');
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${view} th[data-col=iso]`);
  assert.match(await text(`${view} table[aria-label="Transactions"] tbody`), /REPEATABLE READ/, "Columns shows isolation");
  await page.click(`${view} table[aria-label="Transactions"] .table-columns-button`);
  await page.click('.popover input[data-col=iso]');
  await page.keyboard.press("Escape");
  assert.equal(await page.$(`${view} th[data-col=iso]`), null, "Columns can hide isolation explicitly");
  await page.click(`${view} table[aria-label="Transactions"] .table-columns-button`);
  await page.click('.popover input[data-col=structs]');
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${view} th[data-col=structs]`);
  await page.click(`${view} table[aria-label="Transactions"] .table-columns-button`);
  await page.click('.popover .columns-reset');
  await page.keyboard.press("Escape");
  assert.equal(await page.$(`${view} th[data-col=structs]`), null, "Reset turns Lock structs off again");

  await select(treeRow(23));
  await inspectorHas(/Connection 23/);
  const rootText = await inspector();
  assert.match(rootText, /Last statement\s*SELECT id FROM t WHERE k BETWEEN 50 AND 70 FOR UPDATE/);
  assert.match(rootText, /KILL 23/);
  assert.doesNotMatch(rootText, /KILL QUERY/, "an idle blocker has no statement to stop");
  assert.match(rootText, /To end it, run this yourself in a SQL client/);
  assert.match(rootText, /not running a statement, so there is no plan to show/);
  assert.equal(await page.$('.view-inspector [aria-label^="Show plan"]'), null);
  await assertPrivilege(page, '.view-inspector [aria-label="Kill blocker: connection 23"]', "unknown");
  assert.equal((await page.$$('.view-inspector [aria-label="Kill blocker: connection 23"]')).length, 1);
  assert.equal((await page.$$('.view-inspector [aria-label="Copy KILL 23"]')).length, 1);
  await page.click('.view-inspector .kill-manual > summary');
  await page.locator('.view-inspector [aria-label="Copy KILL 23"]').click();
  await page.waitForFunction(() => document.querySelector(".lock-copied")?.textContent.includes("KILL 23"));
  // Headless shells may not expose a clipboard; then the status asks the user to select the text.
  const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => ""));
  if (clip) assert.equal(clip, "KILL 23");
  else assert.match(await text(".lock-copied"), /Select KILL 23 to copy it/);
  assert.ok(seen.every((s) => !JSON.stringify(s).includes("KILL")), "KILL is never sent to the server");
  // Escape closes the inspector and returns focus to the row.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.dataset.connection === "23");

  // Transactions: one row per transaction, idle marker.
  assert.equal((await page.$$(`${view} [aria-label="Transactions"] tbody tr`)).length, 5);
  assert.match(await text(`${view} [aria-label="Transactions"] tbody`), /Idle/);
  // Row locks: waiting first, plain-language mode, tooltip, filter by table.
  await show("rows");
  const rowsText = await texts(`${view} [aria-label="Row locks"] tbody tr`);
  assert.equal(rowsText.length, 18);
  assert.match(rowsText[0], /Waiting/);
  const mode = await page.$eval(`${view} [aria-label="Row locks"] tbody tr .lock-mode`, (e) => [e.title, e.textContent]);
  assert.match(mode[0], /Locks only this index record/);
  assert.match(mode[1], /record only/);
  assert.ok((await texts(`${view} [aria-label="Row locks"] .lock-mode-label`)).includes("next-key"));
  await page.select(`${view} .lock-filter select`, "lk.orders");
  assert.equal((await page.$$(`${view} [aria-label="Row locks"] tbody tr`)).length, 1);
  await page.select(`${view} .lock-filter select`, "");
  // Metadata locks: computed blockers, not every granted lock.
  await show("mdl");
  const mdl = await text(`${view} .mdl-waits`);
  assert.match(mdl, /Connection 29 waits .* queued behind X of connection 28/);
  assert.match(mdl, /Connection 28 waits .* blocked by SR of connection 26/);
  // The latest deadlock: the victim, and the raw text only behind Show raw output.
  await show("deadlock");
  const deadlock = await text(`${view} .deadlock-list`);
  assert.match(deadlock, /Rolled back/);
  await select(`${view} .deadlock-list li.victim`);
  await inspectorHas(/Transaction/);
  const victimText = await inspector();
  assert.match(victimText + (await text(`${view} .deadlock-list`)), /UPDATE d SET v=2 WHERE id=1/);
  assert.match(victimText, /if it is a signed integer/);
  assert.equal(await page.$(".view-inspector .deadlock-raw"), null, "the raw text waits for a click");
  await page.locator(".view-inspector button ::-p-text(Show raw output)").click();
  assert.match(await text(".view-inspector .deadlock-raw"), /WE ROLL BACK TRANSACTION \(2\)/);
  // Notes are one control.
  await page.click(`${view} .detail-back`);
  await page.click(`${view} .notes-button`);
  assert.match(await text(".popover"), /implicit locks/);
  await page.keyboard.press("Escape");
  await show("transactions");
  await page.screenshot({ path: path.join(directory, "locks.png") });

  // Show plan reuses the explain-connection flow.
  await select(treeRow(24));
  await page.locator('.view-inspector [aria-label="Show plan for connection 24"]').click();
  await page.waitForFunction(
    () => document.querySelector(".plan-heading h1")?.textContent === "Connection 24 · estimated plan",
    { timeout: 20000 },
  );
  assert.equal(seen.at(-1).path, "/api/live/explain-connection");
  assert.equal(seen.at(-1).connection_id, 24);

  // Back to Server: the view and its report are kept; Waits only is sent to the server.
  await button("Server", ".main-nav ");
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  assert.ok(await page.$(`${view} .lock-tree`), "the report stays while the workspace is open");
  await page.click(`${view} .detail-back`);
  mocks["/api/live/locks"] = { status: 200, body: { ...mysql, scope: "waits", deadlock: null,
    capabilities: { ...mysql.capabilities, deadlock: false } } };
  await page.locator(`${view} .lock-check input`).click();
  await reload();
  assert.equal(lockCalls().at(-1).waits_only, true);
  await show("deadlock");
  assert.match(await text(`${view} [aria-labelledby="locks-deadlock-h"]`), /Not read with Waits only/);
  await page.locator(`${view} .lock-check input`).click();

  // MySQL 8.0.39: the data_locks latch caution sits next to automatic refresh.
  mocks["/api/live/locks"] = { status: 200, body: mysqlOld };
  await reload();
  assert.match(await text(".lock-caution"), /MySQL 8\.0\.39.*stalls row locking for the whole server/);
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0", "automatic refresh is off by default");
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.getAttribute("aria-describedby")), "locks-caution");
  assert.deepEqual(await page.$$eval(`${view} .auto-refresh option`, (els) => els.map((e) => e.value)), ["0", "10", "30", "60"]);
  // Opt in: the shortest interval is 10 s and it refreshes on its own.
  await page.select(`${view} .auto-refresh select`, "10");
  const count = lockCalls().length;
  await new Promise((r) => setTimeout(r, 10600));
  assert.ok(lockCalls().length > count, "automatic refresh ran");
  // An error stops automatic refresh and is announced.
  mocks["/api/live/locks"] = { status: 400, body: { error: "Could not complete the request: Access denied" } };
  await page.waitForSelector(`${view} [role="alert"]`, { timeout: 15000 });
  assert.match(await text(`${view} [role="alert"]`), /Access denied/);
  assert.equal(await page.$eval(`${view} .auto-refresh select`, (e) => e.value), "0");

  // MariaDB with Performance Schema off (the default): honest gaps.
  mocks["/api/live/locks"] = { status: 200, body: mariadb };
  await reload();
  await show("mdl");
  assert.match(await text(`${view} .gate`), /Metadata locks are not available on this connection/);
  assert.ok(await page.$(`${view} .gate .instr-link`), "one action: how to turn it on");
  await show("rows");
  assert.match(await text(`${view} [aria-labelledby="locks-rows-h"]`), /MariaDB shows only the locks involved in a wait/);
  assert.ok((await texts(`${view} [aria-label="Row locks"] .lock-mode-label`)).includes("record or next-key"));
  await page.click(`${view} .notes-button`);
  assert.match(await text(".popover"), /performance-schema-instrument='wait\/lock\/metadata\/sql\/mdl=ON'/);
  await page.keyboard.press("Escape");
  assert.equal(await page.$(".lock-caution"), null);

  // MySQL without PROCESS: locks and waits still shown, transactions and deadlock explained.
  mocks["/api/live/locks"] = { status: 200, body: noProcess };
  await reload();
  assert.equal((await page.$$(`${view} .lock-tree tr.root`)).length, 2);
  await show("transactions");
  assert.match(await text(`${view} [aria-labelledby="locks-trx-h"]`), /not available on this connection/);
  await show("deadlock");
  assert.match(await text(`${view} [aria-labelledby="locks-deadlock-h"]`), /Not available/);
  await page.click(`${view} .notes-button`);
  assert.match(await text(".popover"), /GRANT PROCESS ON \*\.\* TO <user>/);
  await page.keyboard.press("Escape");
  assert.ok((await texts(`${view} .lock-tree tbody td:nth-child(3)`)).some((w) => /~/.test(w)),
    "statement time stands in for the unknown wait start");

  // ---- DDL waiting for a metadata lock, and the guarded Kill ----------------------------
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const killCalls = () => seen.filter((s) => s.path === "/api/live/kill");
  const blockerRow = `${view} li[data-blocker="53"]`;
  const killButton = '.view-inspector [aria-label="Kill blocker: connection 53"]';
  const openBlocker = async () => {
    await select(blockerRow);
    await page.waitForSelector(killButton);
  };
  mocks["/api/live/locks"] = { status: 200, body: ddl };
  await reload();
  assert.match(await text(`${view} .ddl-section .grouped-head h3`), /Waiting DDL\s*1/);
  assert.match(await text(`${view} .ddl-section`), /Waiting DDL\s*Connection 54\s*ALTER TABLE on lk\.t waits for an EXCLUSIVE metadata lock/);
  assert.match(await text(blockerRow), /Usual culprit\s*Connection 53\s*Idle in transaction/);
  assert.match(await text(`${view} .ddl-queued-row`), /1 session queued behind it/);
  // The explanation of DDL waits is behind ⓘ.
  await page.click('.server-view button[aria-label="About Waiting DDL"]');
  assert.match(await text('[role=dialog][aria-label="About Waiting DDL"]'),
    /needs exclusive access to its table, at least briefly\. It waits until every transaction or running statement that has used the table ends/);
  await page.keyboard.press("Escape");
  // The DDL row: what it waits for, its blockers, the queue and how to cancel the DDL.
  await select(`${view} li[data-ddl="54"]`);
  await inspectorHas(/Waiting DDL on connection 54/);
  const card = await inspector();
  assert.match(card, /ALTER TABLE on lk\.t waits for an EXCLUSIVE metadata lock/);
  assert.match(card, /Blocked by/);
  assert.match(card, /Connection 53/);
  assert.match(card, /1 session queued behind it/);
  assert.match(card, /line up behind it/);
  assert.match(card, /an ALTER TABLE stopped at its final step loses the work it has done and must run again/);
  assert.match(await text(".view-inspector .ddl-queued"), /Connection 55.*SELECT COUNT\(\*\) FROM t/s);
  assert.ok(await page.$('.view-inspector [aria-label="Copy KILL QUERY 54"]'), "cancelling the DDL is offered as text");
  // The blocker: what it holds, its last statement, and Kill.
  await openBlocker();
  const blocker = await inspector();
  assert.match(blocker, /Usual culprit/);
  assert.match(blocker, /Idle in transaction/);
  assert.match(blocker, /Holds SHARED_READ until its transaction ends, SHARED_WRITE until its transaction ends on lk\.t/);
  assert.match(blocker, /1 row changed/);
  assert.match(blocker, /Last statement\s*UPDATE t SET v = v \+ 1 WHERE id = 1/);
  // The DDL group comes before the blocking tree; the tree's root offers Kill too.
  assert.ok(await page.$eval(`${view} .ddl-section`, (e) => !!(e.compareDocumentPosition(document.querySelector('[aria-labelledby="locks-tree-h"]')) & 4)));
  await select(treeRow(53));
  await page.waitForSelector(killButton);
  assert.match(await inspector(), /Or run it yourself in a SQL client/);
  await page.screenshot({ path: path.join(directory, "locks-ddl.png") });

  // The sheet: details, one choice for an idle session, focus on Cancel;
  // Escape and Cancel send nothing and are recorded as declined.
  const declined = () => seen.filter((s) => s.path === "/api/audit/declined");
  await openBlocker();
  await page.locator(killButton).click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  const dialogText = await text("dialog.kill-dialog");
  assert.match(dialogText, /End connection 53\?/);
  assert.match(dialogText, /Connection\s*53/);
  assert.match(dialogText, /Server\s*Local MySQL.*127\.0\.0\.1:3306/);
  assert.match(dialogText, /Server identity\s*locks-server-uuid/);
  assert.match(dialogText, /User\s*app@172\.17\.0\.1 · lk/);
  assert.match(dialogText, /State\s*Idle in transaction for \d+ s/);
  assert.match(dialogText, /Transaction\s*Open \d+ s/);
  assert.match(dialogText, /Rows changed\s*1/);
  assert.match(dialogText, /Last statement\s*UPDATE t SET v = v \+ 1 WHERE id = 1/);
  assert.match(await text(".kill-blocking"), /^Blocking\s*The ALTER TABLE on connection 54 \(lk\.t\), and 1 session queued behind it$/);
  assert.doesNotMatch(dialogText, /Connection 55/, "the queued session is counted with the DDL, not listed again");
  assert.match(dialogText, /KILL QUERY would not help: there is no statement to stop, and its transaction keeps its locks/);
  assert.doesNotMatch(dialogText, /Kill query only/, "an idle session has no statement to stop");
  assert.equal(await page.$(".kill-warning"), null);
  assert.equal(await page.$eval('input[name="kill-mode"][value="connection"]', (e) => e.checked), true);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel");
  assert.equal(await sheetLabel(), "Kill connection 53");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Kill blocker: connection 53");
  assert.ok(await page.$(".view-inspector"), "Escape in the sheet leaves the inspector open");
  while (declined().length < 1) await new Promise((r) => setTimeout(r, 25));
  // The same identity the KILL would have used: the statement as it would be
  // sent, the confirmed session, and the connection the sheet was opened on.
  const escaped = declined()[0];
  assert.deepEqual(
    { operation: escaped.operation, statements: escaped.statements, target: escaped.target, host: escaped.connection.host },
    {
      operation: "kill",
      statements: ["/*myflames:kill*/ KILL CONNECTION 53"],
      target: { connection_id: 53, mode: "connection", user: "app", host: "172.17.0.1", trx_started: "2026-10-14 16:57:12", ps_thread_id: 102,
        connection_identity: reportConnectionKey(escaped.connection), server_identity: ddl.server_identity },
      host: "127.0.0.1",
    },
  );
  await page.locator(killButton).click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  await page.locator(".sheet-actions ::-p-text(Cancel)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  while (declined().length < 2) await new Promise((r) => setTimeout(r, 25));
  assert.deepEqual(declined()[1], escaped, "Cancel records the same declined attempt");
  assert.equal(killCalls().length, 0, "nothing is sent until the confirm button");

  // Confirm: one request with the session's fingerprint, then the report refreshes and shows the outcome.
  const after = { ...clone(ddl), ddl_waits: [], blocking_tree: [], transactions: [],
    summary: { ...ddl.summary, blocked_sessions: 0, root_blockers: 0, ddl_waits: 0 } };
  mocks["/api/live/kill"] = { status: 200, body: { schema_version: "kill-1.0", status: "killed", connection_id: 53,
    mode: "connection", statement: "KILL CONNECTION 53", verified: ["user", "host", "not this session", "server thread", "transaction"],
    message: "Ended connection 53. Its open transaction was rolled back and its locks were released.", after: { present: false } } };
  mocks["/api/live/locks"] = { status: 200, body: after };
  let before = lockCalls().length;
  await page.locator(killButton).click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  await page.locator(".sheet-actions .danger-solid").click();
  await page.waitForSelector(".kill-result.killed:not([data-refreshing])");
  const call = killCalls().at(-1);
  assert.equal(call.confirmed, true, "confirmation is the literal boolean true");
  assert.deepEqual({ id: call.connection_id, mode: call.mode, expect: call.expect, host: call.connection.host },
    { id: 53, mode: "connection", host: "127.0.0.1",
      expect: { user: "app", host: "172.17.0.1", trx_started: "2026-10-14 16:57:12", ps_thread_id: 102,
        connection_identity: reportConnectionKey(call.connection), server_identity: ddl.server_identity } });
  assert.equal(killCalls().length, 1);
  assert.equal(declined().length, 2, "a confirmed Kill is not recorded as declined");
  assert.equal(lockCalls().length, before + 1, "the report refreshes after the kill");
  const result = await text(".kill-result");
  assert.match(result, /^Done\. Ended connection 53\. Its open transaction was rolled back/);
  assert.match(result, /The ALTER TABLE on connection 54 no longer waits; it is running or done\./);
  assert.match(result, /Connection 55 no longer waits\./);
  assert.equal(await page.$eval(".kill-result", (e) => e.getAttribute("role")), "status");
  await page.waitForFunction(() => document.activeElement?.classList.contains("kill-result"));
  assert.equal(await page.$(`${view} .ddl-section`), null);
  assert.match(await text("#server-nav-locks"), /nothing waiting/);
  await page.locator(".kill-result ::-p-text(Dismiss)").click();
  assert.equal(await page.$(".kill-result"), null);

  // Still blocked by someone else after the kill.
  const stillBlocked = clone(ddl);
  stillBlocked.ddl_waits[0].blockers[0] = { ...stillBlocked.ddl_waits[0].blockers[0], connection_id: 14 };
  stillBlocked.blocking_tree[0].connection_id = 14;
  mocks["/api/live/locks"] = { status: 200, body: ddl };
  await reload();
  mocks["/api/live/locks"] = { status: 200, body: stillBlocked };
  await openBlocker();
  await page.locator(killButton).click();
  await page.locator(".sheet-actions .danger-solid").click();
  await page.waitForSelector(".kill-result.killed:not([data-refreshing])");
  assert.match(await text(".kill-result"), /The ALTER TABLE on connection 54 still waits, now for connection 14\./);

  // A running blocker that changed many rows: the rollback warning and both choices.
  const running = clone(ddl);
  Object.assign(running.ddl_waits[0].blockers[0], { command: "Query", running: true, idle_in_transaction: false,
    culprit: false, statement: "UPDATE t SET v = v + 1", last_statement: null, rows_modified: 250000 });
  mocks["/api/live/locks"] = { status: 200, body: running };
  await reload();
  assert.doesNotMatch(await text(blockerRow), /Usual culprit/);
  await openBlocker();
  await page.locator(killButton).click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.match(await text(".kill-warning"), /It changed about 250,000 rows \(InnoDB's undo record count, so a row changed twice counts twice\)\. Ending the connection rolls them all back, which can take a long time, and it keeps its locks until the rollback finishes/);
  assert.match(await text("dialog.kill-dialog"), /Running\s*UPDATE t SET v = v \+ 1/);
  assert.equal(await page.$eval('input[name="kill-mode"][value="query"]', (e) => e.checked), true, "a running statement defaults to Kill query only");
  assert.match(await text(".kill-choices"), /Its transaction stays open with its locks unless the session runs in autocommit mode/);
  assert.equal(await sheetLabel(), "Kill query on connection 53");
  await (await page.$("dialog.kill-dialog")).screenshot({ path: path.join(directory, "kill-dialog.png") });
  await page.locator('input[name="kill-mode"][value="connection"]').click();
  assert.equal(await sheetLabel(), "Kill connection 53");
  await page.locator('input[name="kill-mode"][value="query"]').click();
  // Escape with Kill query only chosen: declined as kill_query, KILL QUERY 53.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  while (declined().length < 3) await new Promise((r) => setTimeout(r, 25));
  assert.deepEqual(
    [declined()[2].operation, declined()[2].statements, declined()[2].target.mode],
    ["kill_query", ["/*myflames:kill*/ KILL QUERY 53"], "query"],
  );
  await page.locator(killButton).click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  mocks["/api/live/kill"] = { status: 200, body: { schema_version: "kill-1.0", status: "refused", connection_id: 53,
    mode: "query", statement: null, verified: [], after: null,
    message: "Connection 53 is now a different session (user web, not app): the one you saw ended and its id was reused. Nothing was ended. Refresh the lock report." } };
  before = lockCalls().length;
  await page.locator(".sheet-actions .danger-solid").click();
  await page.waitForSelector(".kill-result.refused");
  assert.equal(killCalls().at(-1).mode, "query");
  assert.equal(await page.$eval(".kill-result", (e) => e.getAttribute("role")), "alert");
  assert.match(await text(".kill-result"), /Nothing was sent\. Connection 53 is now a different session/);
  assert.equal(lockCalls().length, before, "a refusal waits for the user's Refresh");
  await page.locator(".kill-result button ::-p-text(Refresh)").click();
  await idle();
  assert.equal(lockCalls().length, before + 1);

  // A server identity refusal keeps the backend's exact explanation and offers Refresh.
  mocks["/api/live/locks"] = { status: 200, body: ddl };
  await reload();
  await openBlocker();
  await page.locator(killButton).click();
  mocks["/api/live/kill"] = { status: 200, body: { schema_version: "kill-1.0", status: "refused",
    outcome: "identity", connection_id: 53, mode: "connection", statement: null, verified: [], after: null,
    message: "The server identity is missing or changed after confirmation; nothing was sent." } };
  before = lockCalls().length;
  await page.locator(".sheet-actions .danger-solid").click();
  await page.waitForSelector(".kill-result.refused");
  assert.match(await text(".kill-result"), /The server identity is missing or changed after confirmation; nothing was sent\./);
  assert.equal(lockCalls().length, before);
  await page.locator(".kill-result button ::-p-text(Refresh)").click();
  await idle();
  assert.equal(lockCalls().length, before + 1);

  // The server's own refusal.
  mocks["/api/live/kill"] = { status: 200, body: { schema_version: "kill-1.0", status: "denied", connection_id: 53,
    mode: "connection", statement: "KILL CONNECTION 53", verified: ["user", "host"], after: null,
    message: "The server refused to end connection 53 (ERROR 1095 (HY000) at line 1: You are not owner of thread 53)." } };
  mocks["/api/live/locks"] = { status: 200, body: ddl };
  await reload();
  await openBlocker();
  await page.locator(killButton).click();
  await page.locator(".sheet-actions .danger-solid").click();
  await page.waitForSelector(".kill-result.denied");
  assert.match(await text(".kill-result"), /The server refused\. The server refused to end connection 53/);

  // Without the privilege: focusable disabled Kill, its reason, and copyable SQL.
  const unprivileged = clone(ddl);
  unprivileged.kill_privilege = { ...unprivileged.kill_privilege, user: "viewer", account: "viewer@%", can_kill_others: false, via: null };
  unprivileged.notes.push("Needs CONNECTION_ADMIN or SUPER to end another user session.");
  unprivileged.ddl_waits[0].blockers[0].kill_allowed = false;
  unprivileged.blocking_tree[0].kill_allowed = false;
  mocks["/api/live/locks"] = { status: 200, body: unprivileged };
  await reload();
  await select(blockerRow);
  await assertPrivilege(page, killButton, "denied", "CONNECTION_ADMIN");
  await page.click(killButton); await page.keyboard.press("Enter"); await page.keyboard.press("Space");
  assert.equal(await page.$("dialog.kill-dialog"), null, "denied Kill stays visible but cannot open");
  assert.ok(await page.$('.view-inspector [aria-label="Copy KILL 53"]'));
  await select(treeRow(53));
  await inspectorHas(/CONNECTION_ADMIN/);
  assert.equal((await page.$$(killButton)).length, 1, 'one Kill control for the selected tree target');
  assert.equal((await page.$$('.view-inspector [aria-label="Copy KILL 53"]')).length, 1, 'one copyable KILL statement');
  assert.equal(await page.$('.view-inspector .kill-manual'), null, 'denied targets do not repeat manual commands');
  await assertPrivilege(page, killButton, 'denied', 'CONNECTION_ADMIN');

  // MySQL: a session that ran XA PREPARE gets XA COMMIT / XA ROLLBACK, never a Kill or KILL text.
  const xa = clone(ddl);
  const xaInfo = { xid: "'mf1'", commit: "XA COMMIT 'mf1'", rollback: "XA ROLLBACK 'mf1'" };
  xa.ddl_waits[0].blockers[0].xa_prepared = xaInfo;
  xa.blocking_tree[0].xa_prepared = xaInfo;
  mocks["/api/live/locks"] = { status: 200, body: xa };
  await reload();
  await select(blockerRow);
  await inspectorHas(/Prepared XA transaction: its locks belong to the transaction, which survives the end of the session, so ending this session would release nothing/);
  assert.equal(await page.$('[aria-label^="Kill blocker"]'), null);
  assert.ok(await page.$(`.view-inspector [aria-label="Copy XA ROLLBACK 'mf1'"]`));
  assert.ok(await page.$(`.view-inspector [aria-label="Copy XA COMMIT 'mf1'"]`));
  await select(treeRow(53));
  await page.waitForSelector(`.view-inspector [aria-label="Copy XA ROLLBACK 'mf1'"]`);
  assert.equal(await page.$('.view-inspector [aria-label="Copy KILL 53"]'), null, "a KILL would release nothing");

  // A lock no client connection holds: what owns it depends on the engine.
  const orphan = clone(ddl);
  Object.assign(orphan.ddl_waits[0].blockers[0], { connection_id: null, user: null, host: null, ps_thread_id: 258,
    idle_in_transaction: false, culprit: false, kill_allowed: null });
  mocks["/api/live/locks"] = { status: 200, body: orphan };
  await reload();
  await select(`${view} li[data-blocker=""]`);
  await inspectorHas(/No client connection.*server thread 258/);
  assert.match(await inspector(), /prepared XA transaction that is no longer attached to a session\. XA RECOVER lists prepared XA transactions \(it needs XA_RECOVER_ADMIN\)/);
  mocks["/api/live/locks"] = { status: 200, body: { ...orphan, engine: "mariadb" } };
  await reload();
  await select(`${view} li[data-blocker=""]`);
  await inspectorHas(/It belongs to a background server thread\.(?! XA)/);
  assert.doesNotMatch(await inspector(), /XA RECOVER/);

  // A foreign key or RENAME also locks another table (MySQL): both names are shown.
  const parent = clone(ddl);
  Object.assign(parent.ddl_waits[0], { statement_table: "child", statement_schema: "lk" });
  mocks["/api/live/locks"] = { status: 200, body: parent };
  await reload();
  assert.match(await text(`${view} li[data-ddl="54"]`), /ALTER TABLE on lk\.child waits for an EXCLUSIVE metadata lock on lk\.t, a table its foreign key or rename also locks\./);

  // MariaDB with Performance Schema off: likely blockers, said plainly, and how to get exact ones.
  mocks["/api/live/locks"] = { status: 200, body: ddlLikely };
  await reload();
  assert.match(await text(`${view} .ddl-section`), /1 session queued behind it \(likely\)/);
  await select(`${view} .ddl-section li.ddl-card-row`);
  await inspectorHas(/Likely blockers/);
  const likely = await inspector();
  assert.match(likely, /This server is not recording metadata locks, so myflames cannot see exactly which session holds lk\.t/);
  assert.match(likely, /Open transaction that started before the DDL/);
  assert.match(likely, /ALTER TABLE on lk\.t waits for a metadata lock/);
  await select(`${view} li[data-blocker="165"]`);
  await page.waitForSelector('.view-inspector [aria-label="Kill blocker: connection 165"]');
  await page.click(`${view} .detail-back`);
  await page.click(`${view} .notes-button`);
  assert.match(await text(".popover"), /INSTALL SONAME 'metadata_lock_info'/);
  await page.keyboard.press("Escape");
  await select(`${view} li[data-blocker="165"]`);

  // The sheet fits a phone screen (the detail view uses the full work area there).
  await page.setViewport({ width: 390, height: 700 });
  await page.waitForSelector(".view-inspector.is-detail");
  await page.locator('.view-inspector [aria-label="Kill blocker: connection 165"]').click();
  await page.waitForSelector("dialog.kill-dialog[open]");
  assert.equal(await page.$eval("dialog.kill-dialog", (e) => e.scrollWidth <= e.clientWidth), true);
  assert.equal(await page.$eval("dialog.kill-dialog", (e) => e.getBoundingClientRect().right <= innerWidth), true);
  await (await page.$("dialog.kill-dialog")).screenshot({ path: path.join(directory, "kill-dialog-mobile.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.kill-dialog"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Kill blocker: connection 165");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.setViewport({ width: 1440, height: 1000 });

  // Nothing waiting.
  mocks["/api/live/locks"] = { status: 200, body: { ...mysql, blocking_tree: [], waits: [],
    summary: { ...mysql.summary, blocked_sessions: 0, root_blockers: 0, longest_wait_s: null } } };
  await reload();
  assert.match(await text(`${view} [aria-labelledby="locks-tree-h"]`), /No session is waiting for a lock right now/);
  assert.equal(await text(`${view} .summary-line .status-pill`), "OK");
  assert.equal(await text(`${view} .summary-text`), "Clear · Nothing is waiting for a lock");

  // Narrow viewport: tables scroll inside their container, never the page.
  mocks["/api/live/locks"] = { status: 200, body: mysql };
  await reload();
  await show("rows");
  await page.setViewport({ width: 390, height: 844 });
  await new Promise((r) => setTimeout(r, 200));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.ok(await page.$eval(`${view} [aria-label="Row locks"]`, (e) => e.scrollWidth > e.clientWidth));
  assert.ok(await page.$(`${view} .server-back`), "the view is pushed with a back button");
  await page.screenshot({ path: path.join(directory, "locks-mobile.png"), fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });

  // The Locks view is part of the restored workspace (the report is not).
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  const callsBeforeReload = lockCalls().length;
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");
  if (!(await page.$("#server-nav-locks"))) await button("Server", ".main-nav ");
  await page.waitForSelector("#server-nav-locks[aria-current=page]");
  // The view reads once on screen, so the report is read again rather than restored.
  await page.waitForSelector(`${view} .lock-tree`);
  assert.equal(lockCalls().length, callsBeforeReload + 1, "reports are never restored: the view reads once more on open");

  // A target allowance beats generic kill denial; an unknown target stays
  // enabled and annotated rather than inheriting that denial.
  mocks["/api/live/privileges"].body.actions = { kill: privilegeAnswer("denied") };
  await page.evaluate(() => { const now = Date.now; Date.now = () => now() + 61_000;
    try { window.dispatchEvent(new Event("focus")); } finally { Date.now = now; } });
  const targetAllowed = clone(ddl);
  targetAllowed.ddl_waits[0].blockers[0].kill_allowed = true;
  mocks["/api/live/locks"] = { status: 200, body: targetAllowed };
  await reload(); await select(blockerRow);
  await assertPrivilege(page, killButton, "allowed");
  await page.click(killButton); await page.waitForSelector("dialog.kill-dialog[open]");
  await assertPrivilege(page, "dialog.kill-dialog .sheet-actions button:last-of-type", "allowed");
  await page.keyboard.press("Escape");
  const targetUnknown = clone(targetAllowed);
  targetUnknown.ddl_waits[0].blockers[0].kill_allowed = null;
  mocks["/api/live/locks"] = { status: 200, body: targetUnknown };
  await reload(); await select(blockerRow);
  await assertPrivilege(page, killButton, "unknown");
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, e => e.disabled), false);

  assert.deepEqual(errors, []);
  console.log("Locks workflow passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
