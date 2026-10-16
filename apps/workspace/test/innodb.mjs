// InnoDB view of the Server place: mocked /api/live/innodb with innodb-1.0
// reports computed by myflames/innodb_status.py from recordings of live MySQL
// 8.4 and MariaDB 11.4 servers (fixtures/, see server-fixtures.py). SHOW
// ENGINE INNODB STATUS parsed into sections of one-line rows; numbers, rules
// and links in the inspector; the raw text only behind "Show raw output".
import assert from "node:assert/strict";
import path from "node:path";
import { load, open } from "./perf-harness.mjs";

const mysql = await load("innodb-mysql-8.4.json");
const rawReport = await load("innodb-mysql-8.4-raw.json");
const mariadb = await load("innodb-mariadb-11.4.json");
const noProcess = await load("innodb-mysql-8.4-noproc.json");

const h = await open("/api/live/innodb", "innodb");
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=innodb]";
const section = (key) => `${view} .innodb-section#innodb-${key}`;
const item = (key, name) => `${view} li[data-section="${key}"][data-item="${name}"]`;
const inspectorHas = (pattern) =>
  page.waitForFunction((p) => new RegExp(p, "s").test(document.querySelector(".view-inspector")?.textContent || ""), {}, pattern.source);
try {
  await h.server();
  // InnoDB is in the Overview group, after Health.
  assert.deepEqual(await texts(".source-list .source-group:first-child .source-label"), ["Health", "InnoDB", "Full analysis"]);
  await h.view("innodb");
  assert.match(await text(`${view} .view-empty`), /SHOW ENGINE INNODB STATUS/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");
  assert.match(await h.popover("About InnoDB"), /needs the PROCESS privilege/);

  await button("Load InnoDB status", view + " ");
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  assert.equal(state.seen.length, 1);
  assert.equal(state.seen[0].raw, false, "the raw text is not asked for");
  assert.match(await text(`${view} .summary-line`),
    /Problem\s*3 of \d+ sections need attention · \d+ s window/);
  assert.match(await text(`${view} .fact-strip`), /MySQL 8\.4\.11/);
  assert.match(await text(`${view} .fact-strip`), /Printed\s*2026-10-14/);
  assert.equal(await page.$eval("#server-nav-innodb .status-dot", (e) => e.className), "status-dot is-problem");
  assert.match(await h.notes(), /restarts InnoDB's per-second averages/);
  // Every section, in order, as a group with its status and a one-line summary.
  assert.deepEqual(await page.$$eval(`${view} .innodb-section`, (els) => els.map((e) => e.id.replace("innodb-", ""))),
    mysql.sections.map((s) => s.key));
  assert.match(await text(`${section("transactions")} > summary`), /Transactions and purge\s*Problem/);
  assert.match(await text(`${section("transactions")} .innodb-section-summary`), /history list length/);
  assert.deepEqual(await page.$$eval(`${view} .innodb-section`, (els) => els.map((e) => e.open)),
    mysql.sections.map((s) => s.status === "watch" || s.status === "problem"), "only attention sections start expanded");
  const quiet = mysql.sections.find((s) => s.present && s.status !== "watch" && s.status !== "problem");
  await page.click(`${section(quiet.key)} > summary`);
  assert.equal(await page.$eval(section(quiet.key), (e) => e.open), true, "a quiet section can be expanded");
  await page.click(`${section(quiet.key)} > summary`);
  // A few key rows per section (graded first), the rest one click away.
  assert.equal((await page.$$(`${section("buffer_pool")} li[data-item]`)).length, 5);
  assert.match(await text(item("buffer_pool", "more")), /7 more/);
  assert.match(await text(item("transactions", "oldest_trx")), /Problem\s*Oldest active transaction.*connection 93/);
  // No paragraph on screen: rows are one line.
  const heights = await page.$$eval(`${view} li[data-item] .grouped-row`, (els) => els.map((e) => e.getBoundingClientRect().height));
  assert.ok(heights.every((x) => x <= 40), `one-line rows (${Math.max(...heights)})`);

  // The inspector: summary, every value with its status, the rules, the oldest
  // read view, the transactions, and links to Sessions and Locks.
  await h.select(item("transactions", "oldest_trx"));
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Transactions and purge");
  const trx = await h.inspector();
  assert.match(trx, /History list length/);
  assert.match(trx, /Oldest active transaction: the oldest active transaction is watched at/);
  assert.match(trx, /Holding purge back.*Transaction 21536 \(connection 93\) has the oldest read view/);
  assert.match(trx, /Transactions \(oldest first, \d+\)/);
  assert.match(await text(".view-inspector .inspector-actions-bar"), /Open Sessions\s*Open Locks/);
  await page.click(".view-inspector .innodb-data summary");
  assert.match(await text(".view-inspector .innodb-data"), /trx_id_counter/);
  // Arrow keys walk the rows across sections.
  await page.focus(`${item("transactions", "oldest_trx")} button`);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.closest("li")?.dataset.item === "purge_state");
  await page.locator(".view-inspector button ::-p-text(Open Sessions)").click();
  await page.waitForSelector("#server-nav-sessions[aria-current=page]");
  await h.view("innodb");
  assert.equal(state.seen.length, 1, "switching views keeps the report");
  // The latest deadlock links to Locks; the foreign key error shows its statement.
  await page.$eval(section("deadlock"), (e) => { e.open = true; });
  await h.select(item("deadlock", "victim"));
  await inspectorHas(/Transactions in the cycle/);
  assert.match(await text(".view-inspector .inspector-actions-bar"), /Open Locks/);
  await page.$eval(section("foreign_key_error"), (e) => { e.open = true; });
  await h.select(item("foreign_key_error", "cause"));
  await inspectorHas(/INSERT INTO child VALUES \(1, 999\)/);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "innodb.png") });

  // The raw text: only on an explicit action, for support.
  assert.equal(await page.$(".innodb-raw"), null);
  state.queue.push({ status: 200, body: rawReport });
  await page.locator(`${view} button ::-p-text(Show raw output)`).click();
  await page.waitForSelector(".view-inspector .innodb-raw");
  assert.equal(state.seen.at(-1).raw, true);
  assert.match(await text(".view-inspector .innodb-raw"), /INNODB MONITOR OUTPUT/);
  await page.keyboard.press("Escape");

  // MariaDB: a section the server does not print says why, in its one row.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 11\.4/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  assert.match(await text(section("insert_buffer")), /MariaDB 11\.0 and later have no change buffer/);
  assert.equal((await page.$$(`${section("insert_buffer")} li`)).length, 1);
  assert.match(await text(section("adaptive_hash_index")), /The adaptive hash index is off/);
  assert.ok((await page.$$(`${view} .innodb-section`)).length === mariadb.sections.length);

  // Without PROCESS: one sentence with the GRANT, no sections.
  state.queue.push({ status: 200, body: noProcess });
  await h.refresh();
  await page.waitForSelector(`${view} .gate`);
  await h.idle(view);
  assert.match(await text(`${view} .gate`), /needs the PROCESS privilege.*GRANT PROCESS ON \*\.\* TO <user>;/);
  assert.equal(await page.$(`${view} .innodb-section`), null);
  assert.equal(await page.$eval("#server-nav-innodb .status-dot", (e) => e.className), "status-dot is-unknown");

  // An error keeps the last report.
  state.queue.push({ status: 400, body: { error: "Could not read the InnoDB status: ERROR 2013 (HY000): Lost connection" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.ok(await page.$(`${view} .gate`));

  await h.narrow("innodb-390.png");

  const saved = await h.savedSlow((s) => s.kind === "innodb");
  assert.equal(JSON.stringify(saved.state).includes("innodb-1.0"), false, "reports are not saved");
  assert.deepEqual(h.errors, []);
  console.log("innodb view: ok");
} finally {
  await h.close();
}
