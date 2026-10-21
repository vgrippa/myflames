// Nothing reads a server until its view is opened on screen. Startup (a fresh
// load, a reload with a restored connection, choosing a connection in a place)
// sends only the connected account's privileges read, which is not a view
// read: it enables or greys actions (accounts skill, "When to read it again").
// Opening Replication or Server reads once; Replication opened for a new
// connection reads once for that connection instead of keeping the report
// of the previous one. Run after `npm run build`: node test/startup-reads.mjs
import assert from "node:assert/strict";
import { load, open, pause } from "./perf-harness.mjs";
import { chooseConnection, connectionOption, openConnectionPicker } from "./chrome-helpers.mjs";

const healthy = await load("replication-mysql-8.4-healthy.json");
const extra = {};
const h = await open("/api/live/replication", "startup-reads", extra, { fallback: { status: 200, body: healthy } });
const { page, state } = h;
// Skip loop status lists the loops this myflames holds and reads no database.
const viewReads = () => [...state.seen.map(() => "/api/live/replication"), ...state.other.map((o) => o.path)].filter((p) => p !== "/api/live/replication/skip-loop/status");
const reset = () => { state.seen.length = 0; state.other.length = 0; state.privilegeReads.length = 0; };
const place = async (label) => {
  await page.locator(`.main-nav button ::-p-text(${label})`).click();
  await pause(900);
};
try {
  // A fresh load on the default (unsaved, nothing listening) connection.
  await pause(1000);
  assert.deepEqual(viewReads(), [], "a fresh load reads no view");
  assert.ok(state.privilegeReads.length <= 1, "at most one privileges read on a fresh load");
  await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    for (const [name, host, port] of [["Staging", "db.staging", 3307], ["Archive", "db.archive", 3308]])
      await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify({ value: { name, host, port, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } }) });
  });
  reset();
  await page.reload({ waitUntil: "networkidle0" });
  await pause(1000);
  assert.deepEqual(viewReads(), [], "a reload on Home reads no view");

  // Choosing a connection while Replication is on screen reads nothing by
  // itself: the report stays greyed ("refresh to load this server") until
  // Refresh, like the Server views. Refresh then reads once for the new connection.
  await place("Replication");
  assert.deepEqual(viewReads(), ["/api/live/replication"], "opening Replication reads once");
  reset();
  await chooseConnection(page, await connectionOption(page, "Staging"));
  await pause(1200);
  assert.deepEqual(viewReads(), [], "choosing a connection reads no view");
  assert.equal(state.privilegeReads.length, 1, "one privileges read for the new connection");
  await page.waitForSelector('.server-view[data-view="replication"][data-other-connection="true"]');
  await h.refresh();
  await page.waitForFunction(() => !document.querySelector('.server-view[data-view="replication"][data-other-connection]'));
  assert.deepEqual(viewReads(), ["/api/live/replication"], "Refresh reads once");
  assert.equal(state.seen.at(-1).connection.host, "db.staging");

  // Back on Home after a reload: the restored connection reads no view either.
  await place("Home");
  reset();
  await page.reload({ waitUntil: "networkidle0" });
  await pause(1000);
  assert.deepEqual(viewReads(), [], "a reload on Home with a restored connection reads no view");
  assert.equal(state.privilegeReads.length, 1, "one privileges read for the restored connection");

  // Opening Server reads its first view once; Home reads nothing.
  reset();
  await place("Server");
  assert.deepEqual(viewReads(), ["/api/live/health"], "opening Server reads its first view once");
  reset();
  await place("Home");
  await pause(300);
  assert.deepEqual(viewReads(), [], "leaving a place reads nothing");

  // Another connection chosen in Server: Replication, opened afterwards, reads
  // once for that connection and does not keep showing the Staging report
  // (the reload above emptied it, so first read it for Staging).
  await place("Replication");
  await place("Server");
  await place("Server");
  await chooseConnection(page, await connectionOption(page, "Archive"));
  await pause(1200);
  reset();
  await place("Replication");
  assert.deepEqual(viewReads(), ["/api/live/replication"], "opening Replication for a new connection reads once");
  assert.equal(state.seen.at(-1).connection.host, "db.archive");
  assert.equal(await page.$('.server-view[data-view="replication"][data-other-connection]'), null, "the report shown is this connection's");

  // Revisiting Replication for the same connection reads nothing, and
  // choosing another connection while it stays on screen greys the report.
  await place("Home");
  reset();
  await place("Replication");
  assert.deepEqual(viewReads(), [], "reopening Replication for the same connection reads nothing");
  await chooseConnection(page, await connectionOption(page, "Staging"));
  await pause(1000);
  assert.deepEqual(viewReads(), [], "choosing a connection while on screen reads nothing");
  await page.waitForSelector('.server-view[data-view="replication"][data-other-connection="true"]');

  // Refresh all (Server) reads Staging; choosing Archive and opening
  // Replication then reads once for Archive, although Archive was read here
  // before: the report on hand is Staging's.
  const replicationReads = () => state.seen.length;
  await place("Server");
  const beforeAll = replicationReads();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(".server-place [data-action=refresh-all]");
  await page.waitForFunction(() => !document.querySelector(".server-place .refresh-all-stop") && /refreshed/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""));
  assert.equal(replicationReads(), beforeAll + 1, "Refresh all reads replication once");
  await chooseConnection(page, await connectionOption(page, "Archive"));
  await pause(500);
  const beforeOpen = replicationReads();
  await place("Replication");
  assert.equal(replicationReads(), beforeOpen + 1, "opening Replication reads once for Archive");
  assert.equal(state.seen.at(-1).connection.host, "db.archive");
  assert.equal(await page.$('.server-view[data-view="replication"][data-other-connection]'), null, "the report shown is Archive's");


  // Password drafts must never authenticate. Focus refresh is suppressed too;
  // committing with Enter and then blurring sends the full password only once.
  await openConnectionPicker(page);
  const password = '.window-toolbar .picker-password';
  reset();
  await page.type(password, "secret", { delay: 20 });
  await page.evaluate(() => {
    const now = Date.now;
    Date.now = () => now() + 61_000;
    try { window.dispatchEvent(new Event("focus")); } finally { Date.now = now; }
  });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(state.privilegeReads.length, 0, "typing never sends a partial password");
  await Promise.all([page.waitForResponse(r => new URL(r.url()).pathname === "/api/live/privileges"), page.keyboard.press("Enter")]);
  await page.keyboard.press("Tab");
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.deepEqual(state.privilegeReads.map(r => r.connection.password), ["secret"], "Enter plus blur commits once with all six characters");
  await page.keyboard.press("Escape");

  // Parameters and Instruments share the same on-open rule as other views:
  // no automatic request on an on-screen connection switch, explicit Refresh.
  for (const [kind, fixture] of [["config", "parameters-mysql.json"], ["instruments", "instruments-mysql-8.4.json"]]) {
    const report = await load(fixture);
    let reads = 0;
    extra[`/api/live/${kind}`] = () => { reads++; return { status: 200, body: report }; };
    await h.view(kind);
    const selector = `.server-view[data-view=${kind}]`;
    await page.waitForFunction(s => !!document.querySelector(`${s} .summary-line`), {}, selector);
    await h.idle(selector);
    assert.equal(reads, 1, `${kind} reads on open`);
    const next = kind === "config" ? "Staging" : "Archive";
    await chooseConnection(page, await connectionOption(page, next));
    await page.waitForSelector(`${selector}[data-other-connection=true]`);
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(reads, 1, `${kind} keeps the old report until Refresh`);
    await page.click(`${selector} [data-action=refresh]`);
    await page.waitForFunction(s => !document.querySelector(`${s}[data-other-connection]`), {}, selector);
    assert.equal(reads, 2, `${kind} Refresh reads the chosen connection`);
  }

  // Hold fetch before it reaches the network. A connection switch must abort
  // each view's actual API signal, not only ignore an eventual stale response.
  for (const [kind, fixture] of [["waits", "waits-mysql-8.4.json"], ["innodb", "innodb-mysql-8.4.json"],
    ["locks", "locks-mysql-8.4.json"], ["sessions", "sessions-mysql-8.4-active.json"]]) {
    extra[`/api/live/${kind}`] = { status: 200, body: await load(fixture) };
    await h.view(kind);
    const selector = `.server-view[data-view=${kind}]`;
    await page.waitForFunction(s => !!document.querySelector(`${s} .summary-line`), {}, selector);
    await h.idle(selector);
    await page.evaluate(route => {
      const original = window.fetch;
      window.__readAbort = { started: false, aborted: false };
      window.__restoreReadFetch = () => { window.fetch = original; };
      window.fetch = (url, options) => {
        if (new URL(String(url), location.href).pathname !== route) return original(url, options);
        window.__readAbort.started = true;
        return new Promise((resolve, reject) => {
          options?.signal?.addEventListener("abort", () => {
            window.__readAbort.aborted = true;
            reject(new DOMException("Aborted", "AbortError"));
          }, { once: true });
        });
      };
    }, `/api/live/${kind}`);
    await page.click(`${selector} [data-action=refresh]`);
    await page.waitForFunction(() => window.__readAbort.started);
    const next = ["waits", "locks"].includes(kind) ? "Staging" : "Archive";
    await chooseConnection(page, await connectionOption(page, next));
    await page.waitForFunction(() => window.__readAbort.aborted);
    await page.evaluate(() => { window.__restoreReadFetch(); delete window.__restoreReadFetch; delete window.__readAbort; });
  }

  assert.deepEqual(h.errors, []);
  console.log("startup-reads: ok");
} finally {
  await h.close();
}
