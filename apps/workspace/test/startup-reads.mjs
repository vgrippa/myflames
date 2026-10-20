// Nothing reads a server until its view is opened on screen. Startup (a fresh
// load, a reload with a restored connection, choosing a connection in a place)
// sends only the connected account's privileges read, which is not a view
// read: it enables or greys actions (accounts skill, "When to read it again").
// Opening Replication or Server reads once; Replication opened for a new
// connection reads once for that connection instead of keeping the report
// of the previous one. Run after `npm run build`: node test/startup-reads.mjs
import assert from "node:assert/strict";
import { load, open, pause } from "./perf-harness.mjs";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";

const healthy = await load("replication-mysql-8.4-healthy.json");
const h = await open("/api/live/replication", "startup-reads", {}, { fallback: { status: 200, body: healthy } });
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

  assert.deepEqual(h.errors, []);
  console.log("startup-reads: ok");
} finally {
  await h.close();
}
