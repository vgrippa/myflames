// The Readyset place: mocked /api/live/readyset answering readyset-1.0 reports
// computed by the real backend (readyset-fixtures.py over FakeReadyset and
// FakeProbeConn), mocked dry-run explain, DDL export, plan and action endpoints,
// and the real Python WorkspaceServer for session, saved connections and the
// Audit log.
// Covers: sidebar placement right after ProxySQL with its own glyph, nothing
// read before open, one read on open, summary, 4 metric tiles and 5 tabs,
// multi-tier Topology (ProxySQL -> Readyset -> MySQL Primary & Replica, motion
// & depth toggles, upstream inspector, cross-place navigation), Caches table
// and 320 px right-pane inspector, Export SQL modal, Create cache sheet with
// live EXPLAIN CREATE CACHE dry-run and collision warning, ActionReview lifecycle
// (Escape/Cancel declined, Sending… -> Sent -> Done, policy change refusal,
// read-only refusal, Outcome unknown never retried, Audit log jump), Proxied
// query filtering and Create cache pre-fill, Tables mode-aware replication lag
// (transactions vs bytes) and typed-guard Re-snapshot, Parameters dynamic
// ALTER READYSET controls, shallow allowlists and Admin 6036 shutdown gating,
// other-connection greyed state, sidebar muting per connection, and 720x520.
import assert from "node:assert/strict";
import path from "node:path";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";
import { reviewedActionsUnit } from "./reviewed-actions-unit.mjs";

await reviewedActionsUnit();
if (process.argv.includes("--unit")) process.exit(0);

const R = {};
for (const name of [
  "lab",
  "file-lag",
  "admin",
  "maintenance",
  "problem",
  "not",
  "explain-both",
  "explain-collision",
  "explain-unsupported",
  "export-caches",
  "plan-create-cache",
  "plan-flush-cache",
  "plan-drop-cache",
  "plan-resnapshot",
  "plan-log-level",
  "plan-allow-fn",
  "plan-drop-fn",
  "plan-enter-maint",
  "plan-stop-repl",
  "plan-shutdown",
  "plan-read-only",
  "result-flush-done",
  "result-create-done",
  "progress-checking",
  "progress-sending",
  "progress-finished-unknown",
]) {
  R[name] = await load(`readyset-${name}.json`);
}

const view = '.server-view[data-view="readyset"]';
const nav = '.main-nav button[data-place="readyset"]';
const plans = [];
const sends = [];
const explains = [];
const exportsSeen = [];
const declined = [];
const progressReads = [];

let planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
let sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });
let explainAnswer = (body) => {
  const q = body.query_or_id || body.query || "";
  return {
    status: 200,
    body:
      q === "q_1111aaaa"
        ? R["explain-collision"]
        : /RAND\(\)/i.test(q)
          ? R["explain-unsupported"]
          : R["explain-both"],
  };
};
let progressAnswer = () => ({ status: 200, body: R["progress-sending"] });

const h = await open("/api/live/readyset", "readyset", {
  "/api/live/readyset/plan": (body) => {
    plans.push(body);
    return planAnswer(body);
  },
  "/api/live/readyset/action": (body) => {
    sends.push(body);
    return sendAnswer(body);
  },
  "/api/live/readyset/explain-cache": (body) => {
    explains.push(body);
    return explainAnswer(body);
  },
  "/api/live/readyset/export-caches": (body) => {
    exportsSeen.push(body);
    return { status: 200, body: R["export-caches"] };
  },
  "/api/live/action/progress": (body) => {
    progressReads.push(body);
    return progressAnswer(body);
  },
  "/api/audit/declined": (body) => {
    declined.push(body);
    return { status: 200, body: { audit_id: "x", status: "declined" } };
  },
});

const { page, state } = h;
const api = (route, body) =>
  page.evaluate(
    async (r, b) => {
      const token = (
        await (
          await fetch("/api/auth/session", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Myflames-Bootstrap": "1",
              "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "",
            },
            body: "{}",
          })
        ).json()
      ).token;
      const response = await fetch(r, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: JSON.stringify(b),
      });
      return response.json();
    },
    route,
    body,
  );

const visible = (selector) =>
  page.$$eval(selector, (els) =>
    els
      .filter((e) => e.offsetParent || e.getClientRects().length)
      .map((e) => e.textContent.replace(/\s+/g, " ").trim()),
  );
const shot = (file) => page.screenshot({ path: path.join(h.shots, file), fullPage: false });

async function refreshTo(report) {
  const before = state.seen.length;
  state.fallback = { status: 200, body: report };
  await h.refresh();
  for (let i = 0; i < 50 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh sends one read");
  await page.waitForFunction(
    (sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line`),
    {},
    view,
  );
  await pause(150);
}

async function tab(label) {
  await page.evaluate(
    (label, sel) =>
      [...document.querySelectorAll(`${sel} .segmented button`)]
        .find((b) => b.textContent.trim().startsWith(label))
        .click(),
    label,
    view,
  );
  await pause(200);
}

async function backToList() {
  const close = await page.$(".view-inspector.is-detail .detail-back, .view-inspector.is-sheet .inspector-close");
  if (close) {
    await close.click();
    await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail, .view-inspector.is-sheet"));
  }
}

const rowSelector = (key) => `${view} tbody tr[data-row-key=${JSON.stringify(key)}]`;
async function selectRow(key) {
  await backToList();
  await page.click(rowSelector(key));
  await page.waitForSelector(".view-inspector .inspector-title");
}

const inspectorButtons = () =>
  page.$$eval(".view-inspector .review-actions button", (els) => els.map((e) => e.textContent.trim()));
async function clickAction(label) {
  await page.evaluate(
    (label) =>
      [...document.querySelectorAll(".view-inspector .review-actions button")]
        .find((b) => b.textContent.trim() === label)
        .click(),
    label,
  );
}

const sheet = "dialog.action-review[open]";
async function waitSheet() {
  await page.waitForSelector(sheet, { visible: true });
}
async function noSheet() {
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
}
async function answered() {
  await page.waitForSelector(`${sheet} .action-outcome`);
  return page.$eval(sheet, (d) => ({
    outcome: d.querySelector(".action-outcome")?.textContent.replace(/\s+/g, " ").trim(),
    steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state),
    text: d.textContent,
    buttons: [...d.querySelectorAll(".sheet-actions button")].map((b) => b.textContent.trim()),
    focused: document.activeElement?.textContent?.trim(),
  }));
}
async function closeSheet() {
  await page.evaluate(
    (s) => [...document.querySelectorAll(`${s} .sheet-actions button`)].find((b) => b.textContent.trim() === "Close").click(),
    sheet,
  );
  await noSheet();
}
const resultButton = (label) =>
  page.evaluate(
    (sel, label) =>
      [...document.querySelectorAll(`${sel} .action-result .ops-result-head .text-button`)]
        .find((b) => b.textContent.trim() === label)
        .click(),
    view,
    label,
  );

try {
  await api("/api/profiles/save", {
    value: {
      name: "Readyset lab",
      host: "127.0.0.1",
      port: 3307,
      user: "app",
      database: "shop",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
      type: "development",
    },
  });
  await api("/api/profiles/save", {
    value: {
      name: "Readyset Admin",
      host: "127.0.0.1",
      port: 6036,
      user: "root",
      database: "",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
      type: "development",
    },
  });
  await api("/api/profiles/save", {
    value: {
      name: "Plain MySQL",
      host: "mysql.test",
      port: 3306,
      user: "dba",
      database: "shop",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
    },
  });
  await page.reload({ waitUntil: "networkidle0" });

  // 1. Sidebar placement right after ProxySQL with its own glyph; nothing read before open.
  const places = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.equal(places[places.indexOf("proxysql") + 1], "readyset", `Readyset follows ProxySQL: ${places}`);
  assert.equal(await page.$eval(nav, (b) => (b.childNodes[1]?.textContent || "").trim()), "Readyset");
  const rsSvg = await page.$eval('.main-nav button[data-place="readyset"] svg', (e) => e.innerHTML);
  const pxSvg = await page.$eval('.main-nav button[data-place="proxysql"] svg', (e) => e.innerHTML);
  assert.notEqual(rsSvg, pxSvg, "Readyset has a distinct icon from ProxySQL");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "unmuted before first read");
  await pause(350);
  assert.equal(state.seen.length, 0, "nothing read before Readyset is opened");

  // 2. Open Readyset: reads once, switch to Readyset lab and Refresh.
  state.fallback = { status: 200, body: R.lab };
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await pause(200);
  assert.equal(state.seen.length, 1, "opening Readyset reads once");
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await pause(250);
  assert.equal(state.seen.length, 1, "switching connection keeps prior report until Refresh");
  await refreshTo(R.lab);
  assert.equal(state.seen.at(-1).connection.port, 3307);
  assert.equal(state.seen.at(-1).connection.type, "development");

  assert.match(
    (await visible(`${view} .summary-line`))[0],
    /Readyset stable-250605 · SQL port · 2 caches · 80% cached · Repl Running/,
  );
  assert.deepEqual(await visible(`${view} .segmented button`), [
    "Topology",
    "Caches2",
    "Proxied2",
    "Tables2",
    "Parameters",
  ]);

  const tiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      value: e.querySelector(".tile-value")?.textContent?.trim() || "",
      hint: e.querySelector(".tile-hint")?.textContent?.trim() || "",
    })),
  );
  assert.deepEqual(
    tiles.map((t) => t.label),
    ["Cached share", "Active caches", "Shallow hit rate", "Base replication"],
  );
  assert.match(tiles[0].value, /^80/);
  assert.equal(tiles[0].hint, "1,000 cached · 250 upstream");
  assert.equal(tiles[1].value, "2");
  assert.equal(tiles[1].hint, "1 shallow · 1 deep");
  assert.match(tiles[2].value, /^90/);
  assert.equal(tiles[3].value, "Running");
  assert.equal(tiles[3].hint, "lag 0 transactions");

  const info = await h.popover("About Readyset");
  assert.match(info, /SQL port 3307, admin SQL port 6036/);
  assert.match(info, /Audit log/);

  // 3. Multi-tier Topology tab: ProxySQL -> Readyset -> MySQL Primary & Replica.
  const topoText = await h.text(`${view} .readyset-topology`);
  assert.match(topoText, /ProxySQL lab/);
  assert.match(topoText, /127\.0\.0\.1:6032 · HG 30/);
  assert.match(topoText, /Cached reads \+ QueryPilot mirror/);
  assert.match(topoText, /2 QueryPilot rules/);
  assert.match(topoText, /MySQL Primary/);
  assert.match(topoText, /mysql-primary:3306 · MySQL 8\.4\.11/);
  assert.match(topoText, /MySQL Replica/);
  assert.match(topoText, /mysql-replica:3306/);
  // Redacted SHOW CONNECTIONS upstream URL never leaks credentials.
  const connText = await h.text(`${view}`);
  assert.doesNotMatch(connText, /secret@/);
  assert.match(connText, /\[redacted\]@/);

  const depthToggle = `${view} .readyset-topology .topology-presentation-controls button:first-child`;
  await page.click(`${view} .readyset-topology .topology-motion-toggle`);
  assert.equal(await page.$eval(`${view} .readyset-topology`, (e) => e.dataset.motion), "off");
  await page.click(depthToggle);
  assert.equal(await page.$eval(`${view} .readyset-topology`, (e) => e.dataset.depth), "off");
  await page.click(`${view} .readyset-topology .topology-motion-toggle`);
  await page.click(depthToggle);

  // Clicking an upstream card opens the 320 px right-pane inspector.
  await page.click(`${view} .readyset-topology button[data-row-key="upstream:mysql-primary:3306"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.equal(await page.$eval(".view-inspector", (e) => Math.round(e.getBoundingClientRect().width)), 340);
  assert.match(await h.inspector(), /MySQL 8\.4\.11/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await shot("readyset-topology-1440.png");

  // 4. Caches tab: inspector, Export SQL, Create cache with live EXPLAIN & collision warning, ActionReview.
  await tab("Caches");
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-detail")), false);
  const cacheInsp = await h.inspector();
  assert.match(cacheInsp, /Shallow cache efficiency/);
  assert.match(cacheInsp, /90%/);
  assert.match(cacheInsp, /540 \/ 60/);
  assert.match(cacheInsp, /Cached execution latency/);
  assert.deepEqual(await inspectorButtons(), ["Flush entries…", "Drop cache…"]);

  // Review Flush entries…, then Escape -> declined, nothing sent.
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  await clickAction("Flush entries…");
  await waitSheet();
  assert.equal(plans.length, 1);
  assert.equal(plans[0].action, "flush_cache");
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), R["plan-flush-cache"].statements.join("\n"));
  await page.keyboard.press("Escape");
  await noSheet();
  for (let i = 0; i < 50 && !declined.length; i++) await pause(20);
  assert.equal(declined.length, 1);
  assert.equal(declined[0].operation, "readyset_action");
  assert.equal(sends.length, 0);

  // Confirm Flush entries… -> Sending… -> Sent -> Done -> refreshes report.
  await clickAction("Flush entries…");
  await waitSheet();
  const readsBeforeFlush = state.seen.length;
  progressAnswer = () => ({ status: 200, body: R["progress-checking"] });
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"], delay: 1800 });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction(
    (s) => /Reading the target again/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  assert.equal(await page.$eval(`${sheet} .sheet-actions button:last-child`, (b) => b.textContent), "Sending…");
  assert.equal(await page.$eval(`${sheet} .sheet-actions button:last-child`, (b) => b.getAttribute("aria-disabled")), "true");
  progressAnswer = () => ({ status: 200, body: R["progress-sending"] });
  await page.waitForFunction(
    (s) => /Sent\. Waiting for the server's answer/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  assert.deepEqual(
    await page.$$eval(`${sheet} .action-steps li`, (els) => els.map((e) => e.dataset.state)),
    ["sent"],
  );
  const flushDone = await answered();
  assert.match(flushDone.outcome, /^Done/);
  assert.deepEqual(flushDone.steps, ["done"]);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].review_token, R["plan-flush-cache"].review_token);
  assert.equal(sends[0].plan.review_token, undefined);
  await closeSheet();
  await page.waitForSelector(`${view} .action-result`);
  for (let i = 0; i < 50 && state.seen.length === readsBeforeFlush; i++) await pause(20);
  assert.equal(state.seen.length, readsBeforeFlush + 1, "Readyset refreshes after a confirmed write");
  await resultButton("Close");
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });

  // Export SQL modal (EXPLAIN ALL CACHES).
  await backToList();
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} .readyset-strip-buttons button`)].find((b) => b.textContent.trim() === "Export SQL").click(),
    view,
  );
  await page.waitForSelector("dialog.readyset-export-dialog[open]", { visible: true });
  assert.equal(exportsSeen.length, 1);
  assert.match(await page.$eval("dialog.readyset-export-dialog[open]", (d) => d.textContent), /CREATE DEEP CACHE `c_orders_deep`/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.readyset-export-dialog[open]"));

  // Create cache sheet: validation, EXPLAIN dry-run, collision warning, review.
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} .readyset-strip-buttons button`)].find((b) => b.textContent.trim() === "Create cache…").click(),
    view,
  );
  const createDialog = "dialog.readyset-create-dialog[open]";
  await page.waitForSelector(createDialog, { visible: true });
  const createConfirm = `${createDialog} .sheet-actions button:last-child`;
  assert.equal(await page.$eval(createConfirm, (b) => b.disabled), true, "disabled before query entered");
  await page.type(`${createDialog} textarea`, "q_1111aaaa");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Collides with existing cache c_orders_deep/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);
  assert.match(await page.$eval(createDialog, (d) => d.textContent), /Shallow: Supported/);
  assert.match(await page.$eval(createDialog, (d) => d.textContent), /Deep: Supported/);

  // Unsupported query disables Review CREATE CACHE…
  await page.$eval(`${createDialog} textarea`, (t) => {
    t.select();
  });
  await page.keyboard.type("SELECT RAND(), id FROM orders");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Shallow: Unsupported/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);
  assert.equal(await page.$eval(createConfirm, (b) => b.disabled), true, "unsupported query cannot be reviewed");

  // Valid query -> Review CREATE CACHE… -> opens ActionReview with exact CREATE SHALLOW CACHE SQL.
  await page.$eval(`${createDialog} textarea`, (t) => {
    t.select();
  });
  await page.keyboard.type("q_3333cccc");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Shallow: Supported/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);
  await shot("readyset-create-cache-1440.png");
  planAnswer = () => ({ status: 200, body: R["plan-create-cache"] });
  await page.click(createConfirm);
  await waitSheet();
  assert.equal(plans.at(-1).action, "create_cache");
  assert.match(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), /CREATE SHALLOW CACHE `c_orders_status`/);
  await page.keyboard.press("Escape");
  await noSheet();

  // Policy change after review -> Nothing sent.
  const rsLab = (await api("/api/profiles/list", {})).items.find((p) => p.name === "Readyset lab");
  await selectRow("cache:c_recent_shallow");
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  await clickAction("Flush entries…");
  await waitSheet();
  const sendsAtPolicy = sends.length;
  await api("/api/profiles/save", { id: rsLab.id, value: { ...rsLab, type: "production" } });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction((s) => !document.querySelector(s) || document.querySelector(`${s} .action-outcome`), {}, sheet);
  if (await page.$(`${sheet} .action-outcome`)) {
    assert.match((await answered()).outcome, /Nothing sent/);
    await closeSheet();
  }
  assert.equal(sends.length, sendsAtPolicy, "nothing sent after policy changed");
  await api("/api/profiles/save", { id: rsLab.id, value: { ...rsLab, type: "development" } });
  await page.reload({ waitUntil: "networkidle0" });
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await refreshTo(R.lab);
  await tab("Caches");
  await selectRow("cache:c_recent_shallow");

  // Read-only refusal opens no sheet.
  planAnswer = () => ({ status: 200, body: R["plan-read-only"] });
  await clickAction("Flush entries…");
  await page.waitForFunction((sel) => /read-only in myflames/.test(document.querySelector(sel)?.textContent || ""), {}, view);
  assert.equal(await page.$(sheet), null);

  // Lost reply -> Outcome unknown, never retried, Audit log jump.
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  sendAnswer = () => ({ status: 500, body: { error: "connection lost while waiting for a reply" }, delay: 1400 });
  progressAnswer = () => ({ status: 200, body: R["progress-finished-unknown"] });
  const sendsBeforeLost = sends.length;
  await clickAction("Flush entries…");
  await waitSheet();
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction(
    (s) => /No answer arrived from the server/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  const lost = await answered();
  assert.match(lost.outcome, /^Outcome unknown/);
  assert.deepEqual(lost.steps, ["unknown"]);
  assert.match(lost.text, /What to check/);
  await closeSheet();
  await page.waitForFunction((sel) => /Outcome unknown/.test(document.querySelector(`${sel} .action-result`)?.textContent || ""), {}, view);
  await pause(250);
  assert.equal(sends.length, sendsBeforeLost + 1, "Outcome unknown is never retried");
  await resultButton("Audit log");
  await page.waitForSelector("#audit-title", { visible: true });
  await page.click(nav);
  await page.waitForSelector(`${view} .action-result`, { visible: true });
  await resultButton("Close");
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });

  // 5. Proxied tab: filtering + Create cache… pre-fills query_id and runs EXPLAIN.
  await backToList();
  await tab("Proxied");
  assert.equal(await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.length), 2);
  await page.type(`${view} .readyset-strip-filters input[type="search"]`, "RAND");
  await pause(150);
  assert.equal(await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.length), 1);
  await page.$eval(`${view} .readyset-strip-filters input[type="search"]`, (i) => {
    i.select();
  });
  await page.keyboard.press("Backspace");
  await pause(150);
  await selectRow("pq:q_3333cccc");
  const explainsBeforePre = explains.length;
  await clickAction("Create cache…");
  await page.waitForSelector(createDialog, { visible: true });
  assert.equal(await page.$eval(`${createDialog} textarea`, (t) => t.value), "q_3333cccc");
  for (let i = 0; i < 50 && explains.length === explainsBeforePre; i++) await pause(20);
  assert.equal(explains.at(-1).query_or_id, "q_3333cccc", "pre-filled query_id triggers EXPLAIN automatically");
  await page.keyboard.press("Escape");
  await page.waitForFunction((d) => !document.querySelector(d), {}, createDialog);

  // 6. Tables tab: mode-aware replication lag (transactions vs bytes) & typed-guard Re-snapshot.
  await backToList();
  await tab("Tables");
  assert.match(await h.text(`${view} .readyset-repl-bar`), /0 transactions/);
  await refreshTo(R["file-lag"]);
  assert.match(await h.text(`${view} .readyset-repl-bar`), /1,024 bytes/);
  await refreshTo(R.lab);
  planAnswer = () => ({ status: 200, body: R["plan-resnapshot"] });
  await selectRow("tbl:`shop`.`orders`");
  await clickAction("Re-snapshot table…");
  await waitSheet();
  const resnapConfirm = `${sheet} .sheet-actions button:last-child`;
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), true, "typed guard disables Confirm initially");
  await page.type(`${sheet} .action-typed input`, "`shop`.");
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), true);
  await page.type(`${sheet} .action-typed input`, "`orders`");
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), false, "exact table name unlocks Confirm");
  await page.keyboard.press("Escape");
  await noSheet();

  // 7. Parameters tab: dynamic ALTER READYSET, allowlists, Maintenance mode, Admin 6036 gating.
  await backToList();
  await tab("Parameters");
  // On SQL port 3307, Shut down Readyset… is aria-disabled with explanation.
  const shutdownBtn3307 = await page.$eval(
    `${view} .readyset-admin-lifecycle button`,
    (b) => ({ ariaDisabled: b.getAttribute("aria-disabled"), title: b.title }),
  );
  assert.equal(shutdownBtn3307.ariaDisabled, "true");
  assert.match(shutdownBtn3307.title, /6036/);

  // Set log level… value prompt -> Review SQL… -> ActionReview with exact ALTER READYSET SQL.
  planAnswer = () => ({ status: 200, body: R["plan-log-level"] });
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} button`)].find((b) => b.textContent.trim() === "Set log level…").click(),
    view,
  );
  await page.waitForSelector("dialog.action-value[open]", { visible: true });
  await page.$eval("dialog.action-value[open] input", (i) => {
    i.select();
  });
  await page.keyboard.type("info,readyset_adapter=debug");
  await page.click("dialog.action-value[open] .sheet-actions button:last-child");
  await waitSheet();
  assert.equal(plans.at(-1).action, "set_log_level");
  assert.equal(
    await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent),
    "ALTER READYSET SET LOG LEVEL 'info,readyset_adapter=debug';",
  );
  await page.keyboard.press("Escape");
  await noSheet();

  // Shallow allowlist Remove chip -> ActionReview.
  planAnswer = () => ({ status: 200, body: R["plan-drop-fn"] });
  await page.evaluate(
    (sel) =>
      [...document.querySelectorAll(`${sel} .readyset-allow-chip`)]
        .find((c) => c.textContent.includes("NOW"))
        .querySelector("button")
        .click(),
    view,
  );
  await waitSheet();
  assert.equal(plans.at(-1).action, "drop_shallow_allowed");
  assert.equal(
    await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent),
    "ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `NOW`;",
  );
  await page.keyboard.press("Escape");
  await noSheet();
  await shot("readyset-parameters-1440.png");

  // Maintenance mode & Admin 6036 port behavior.
  await refreshTo(R.maintenance);
  assert.match(await h.text(`${view}`), /Exit maintenance mode…/);
  await chooseConnection(page, await connectionOption(page, "Readyset Admin"));
  await refreshTo(R.admin);
  assert.match((await visible(`${view} .summary-line`))[0], /admin SQL port/);
  assert.equal(
    await page.$eval(`${view} .readyset-admin-lifecycle button`, (b) => b.getAttribute("aria-disabled")),
    null,
    "Admin SQL port 6036 enables Shut down Readyset…",
  );
  planAnswer = () => ({ status: 200, body: R["plan-shutdown"] });
  await page.click(`${view} .readyset-admin-lifecycle button`);
  await waitSheet();
  assert.equal(plans.at(-1).action, "shutdown");
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), "ALTER READYSET SHUTDOWN;");
  await page.keyboard.press("Escape");
  await noSheet();
  // On Admin port 6036, Create cache… in Caches tab is aria-disabled with SQL port explanation.
  await tab("Caches");
  const createOnAdmin = await page.$eval(`${view} .readyset-strip-buttons button`, (b) => ({
    ariaDisabled: b.getAttribute("aria-disabled"),
    title: b.title,
  }));
  assert.equal(createOnAdmin.ariaDisabled, "true");
  assert.match(createOnAdmin.title, /3307/);

  // 8. Problem findings, other-connection greyed state, sidebar muting per connection, and 720x520.
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await refreshTo(R.problem);
  assert.match((await visible(`${view} .readyset-findings`))[0], /Binlog purged|Error/i);
  await refreshTo(R.lab);

  await chooseConnection(page, await connectionOption(page, "Plain MySQL"));
  await page.waitForFunction(
    (sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")),
    {},
    view,
  );
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector .review-actions button", (b) => b.getAttribute("aria-disabled")), "true");
  assert.match(await page.$eval(".view-inspector .review-actions", (e) => e.textContent), /read on Readyset lab/);
  const plansBeforeOther = plans.length;
  await clickAction("Flush entries…");
  await pause(200);
  assert.equal(plans.length, plansBeforeOther, "nothing planned on another connection's report");

  await backToList();
  await refreshTo(R.not);
  assert.equal(await h.text(`${view} .view-empty h3`), "Not a Readyset instance");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true");
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await pause(250);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "Readyset lab stays unmuted");
  await refreshTo(R.lab);

  // 720x520 responsive check: zero horizontal overflow and bottom-sheet inspector.
  await page.setViewport({ width: 720, height: 520 });
  await pause(350);
  const narrow = await page.evaluate(
    (sel) => ({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      top: document.querySelector(`${sel} .data-table-wrap`)?.getBoundingClientRect().top,
      fold: document.querySelector(".window-statusbar").getBoundingClientRect().top,
    }),
    view,
  );
  assert.ok(narrow.overflow <= 0, `horizontal overflow at 720 px: ${narrow.overflow}`);
  assert.ok(narrow.top < narrow.fold, `table starts above fold at 720x520: ${JSON.stringify(narrow)}`);
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-sheet")), true, "bottom-sheet inspector at 720 px");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-row-key") === "cache:c_recent_shallow");
  await shot("readyset-720.png");

  assert.deepEqual(h.errors, []);
  console.log("readyset: ok");
} finally {
  await h.close();
}
