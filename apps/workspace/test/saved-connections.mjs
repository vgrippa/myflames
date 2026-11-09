// The window's one list of saved connections (savedConnections.ts). A
// connection created, renamed, deleted or whose password is forgotten on Home
// shows at once, without a reload, in every list of saved connections that is
// already open: the connection picker, Compare's Source and Target in
// Parameters and in Users and roles, and Schema versions' Compare sides. A
// chosen connection that was deleted stays chosen and says so; one deleted in
// another window or with the CLI shows when the window comes back (focus),
// the connection in use included. Refreshing the list reads no server.
// The real workspace server on a temporary store (vault backend, throwaway
// HOME); server views are refused by the harness, the Schema versions
// database list is mocked. Run after `npm run build`: node test/saved-connections.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, pause } from "./perf-harness.mjs";
import { chooseConnection, connectionOption, openConnectionPicker, openViewList } from "./chrome-helpers.mjs";
import { connectionAction } from "./connection-helpers.mjs";

const h = await open("/api/unused-saved-connections", "saved-connections", {
  "/api/live/schemas": { status: 200, body: { schema_version: "schemas-1.0", schemas: [{ name: "shop", tables: 1, system: false }], user_tables: 1, all_limit: 2000 } },
});
const { page, state } = h;
await fs.mkdir(h.shots, { recursive: true });
// Every API request after a point, by path (the harness answers them).
const sent = [];
page.on("request", (request) => { const p = new URL(request.url()).pathname; if (p.startsWith("/api/")) sent.push(p); });
const token = () => page.evaluate(async () => (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token);
/** Another window or the CLI: the profiles API called outside this page's lists. */
const outside = async (endpoint, body) => page.evaluate(async (t, endpoint, body) => (await fetch(`/api/profiles/${endpoint}`, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(body) })).json(), await token(), endpoint, body);
const options = (selector) => page.$$eval(`${selector} option`, (els) => els.map((e) => e.textContent));
const selected = (selector) => page.$eval(selector, (e) => e.selectedOptions[0]?.textContent || "");
const PARAMS = '[data-compare-host="parameters"] select[aria-label="Target"]';
const ACCOUNTS = '[data-compare-host="accounts"] select[aria-label="Target"]';
const SCHEMA = "#schema-side-target-connection";
const go = async (place) => { await page.click(`.main-nav button[data-place="${place}"]`); await pause(150); };
const setField = (label, value) => page.$eval(".connection-panel", (panel, label, value) => {
  const input = [...panel.querySelectorAll("label")].find((e) => e.firstChild.textContent.trim() === label).querySelector("input");
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}, label, value);
/** Wait until the select lists (or stops listing) an option starting with *text*. */
const listed = (selector, text, present = true) => page.waitForFunction((s, t, p) => [...document.querySelectorAll(`${s} option`)].some((o) => o.textContent.startsWith(t)) === p, { timeout: 5000 }, selector, text, present);
const shown = (selector) => page.$eval(selector, (e) => !e.closest("[hidden]") && e.offsetParent !== null).catch(() => false);
/** A Server view whose Compare tab is open (its list tab is hidden). */
async function compareOf(view, select) {
  if (!(await page.$('.main-nav button[data-place="server"].active'))) await go("server");
  if (await shown(select)) return;
  await page.waitForFunction(() => [...document.querySelectorAll(".server-place:not(.is-tuning-place) :is(.source-list, .view-switcher)")].some((e) => !e.closest("[hidden]") && e.offsetParent !== null));
  if (!(await shown(`#server-nav-${view}`))) await openViewList(page);
  await page.click(`#server-nav-${view}`);
  await page.waitForSelector(select, { visible: true });
}
const backInWindow = async () => {
  // A return to the window reads the list again at most once a second.
  await pause(1100);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
};
try {
  await outside("save", { value: { name: "Alpha", host: "db.alpha", port: 3306, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "" }, password: "pw", remember_password: true });
  await page.reload({ waitUntil: "networkidle0" });
  // Alpha in use; every list of saved connections opened once.
  await h.view("config");
  await chooseConnection(page, await connectionOption(page, "Alpha"));
  await page.click("#parameters-views-tab-compare");
  await page.waitForSelector(PARAMS, { visible: true });
  assert.deepEqual(await options(PARAMS), ["This connection · Alpha (dev@db.alpha:3306)"]);
  await h.view("accounts");
  await page.click("#accounts-views-tab-compare");
  await page.waitForSelector(ACCOUNTS, { visible: true });
  await go("schema");
  await page.click("#schema-versions-tab-compare");
  await page.waitForSelector(SCHEMA, { visible: true });
  assert.deepEqual(await options(SCHEMA), ["In use: Alpha (dev@db.alpha:3306)"]);

  // Create Bravo on Home, its password saved: every open list shows it.
  sent.length = 0;
  await go("home");
  await page.locator("button ::-p-text(Add connection)").click();
  await page.waitForSelector(".connection-panel");
  await setField("Connection name", "Bravo");
  await setField("Host", "db.bravo");
  await setField("User", "dev");
  await setField("Password", "secret");
  await page.locator(".connection-panel button ::-p-text(Save connection)").click();
  await page.locator(".save-prompt button ::-p-text(Save with password)").click();
  await page.waitForFunction(() => /Connection "Bravo" saved/.test(document.querySelector(".connections .notice")?.textContent || ""));
  await go("schema");
  await listed(SCHEMA, "Bravo (dev@db.bravo:3306)");
  await page.select(SCHEMA, (await page.$$eval(`${SCHEMA} option`, (els) => els.find((o) => o.textContent.startsWith("Bravo")).value)));
  await compareOf("config", PARAMS);
  await listed(PARAMS, "Bravo (dev@db.bravo:3306)");
  assert.deepEqual(await options(PARAMS), ["This connection · Alpha (dev@db.alpha:3306)", "Bravo (dev@db.bravo:3306)"]);
  const bravo = await page.$$eval(`${PARAMS} option`, (els) => els.find((o) => o.textContent.startsWith("Bravo")).value);
  await page.select(PARAMS, bravo);
  await compareOf("accounts", ACCOUNTS);
  await listed(ACCOUNTS, "Bravo (dev@db.bravo:3306)");
  await openConnectionPicker(page);
  assert.ok((await options('.window-toolbar select[aria-label="Connection"]')).includes("Bravo"), "the picker lists Bravo");
  await page.keyboard.press("Escape");
  assert.equal(sent.filter((p) => p === "/api/profiles/list").length >= 1, true);

  // Rename it: the choice stays, its name follows.
  await go("home");
  await connectionAction(page, "Bravo", "Edit");
  await page.waitForSelector(".connection-panel");
  await setField("Connection name", "Bravo two");
  await page.locator(".connection-panel button ::-p-text(Save connection)").click();
  await page.waitForFunction(() => /Connection "Bravo two" saved/.test(document.querySelector(".connections .notice")?.textContent || ""));
  await compareOf("config", PARAMS);
  await page.waitForFunction((s) => document.querySelector(s)?.selectedOptions[0]?.textContent === "Bravo two (dev@db.bravo:3306)", {}, PARAMS);
  assert.equal(await page.$eval(PARAMS, (e) => e.value), bravo, "the chosen target is kept");

  // Forget its password: Compare says so; Schema versions keeps the choice and says why it cannot read it.
  await go("home");
  await connectionAction(page, "Bravo two", "Forget password");
  await page.waitForFunction(() => /Saved password forgotten/.test(document.querySelector(".connections .notice")?.textContent || ""));
  await go("schema");
  await page.waitForFunction((s) => document.querySelector(s)?.selectedOptions[0]?.textContent === "Bravo two (dev@db.bravo:3306) · no saved password", {}, SCHEMA);
  await compareOf("config", PARAMS);
  await page.waitForFunction((s) => document.querySelector(s)?.selectedOptions[0]?.textContent === "Bravo two (dev@db.bravo:3306) · no saved password", {}, PARAMS);

  // Delete it: the chosen side stays chosen and says it was deleted.
  await go("home");
  await connectionAction(page, "Bravo two", "Delete");
  await page.locator("dialog.confirm-sheet button ::-p-text(Delete connection)").click();
  await page.waitForFunction(() => !document.querySelector('button[aria-label="Actions for Bravo two"]'));
  await compareOf("config", PARAMS);
  await page.waitForFunction((s) => document.querySelector(s)?.selectedOptions[0]?.textContent === "Bravo two (deleted)", {}, PARAMS);
  assert.match(await h.text('[data-compare-host="parameters"] .server-compare-view .view-empty'), /Target: the saved connection “Bravo two” was deleted\. Choose another target\./);
  assert.equal(await page.$eval('[data-compare-host="parameters"] .server-compare-run .action-button', (e) => e.disabled), true, "Compare waits for another target");
  for (const theme of ["light", "dark"]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width: 720, height: 900 });
    await pause(200);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), 0, `no overflow at 720 px (${theme})`);
    await page.screenshot({ path: path.join(h.shots, `saved-connections-deleted-${theme}-720.png`) });
    await page.setViewport({ width: 1440, height: 1000 });
    await pause(150);
    await page.screenshot({ path: path.join(h.shots, `saved-connections-deleted-${theme}-1440.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await go("schema");
  await page.waitForFunction((s) => document.querySelector(s)?.selectedOptions[0]?.textContent === "Bravo two (deleted)", {}, SCHEMA);

  // Changed in another window or with the CLI: seen when the window comes back.
  await outside("save", { value: { name: "Charlie", host: "db.charlie", port: 3306, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await compareOf("config", PARAMS);
  assert.ok(!(await options(PARAMS)).some((t) => t.startsWith("Charlie")), "not before the window comes back");
  await backInWindow();
  await listed(PARAMS, "Charlie (dev@db.charlie:3306) · no saved password");
  // No server is read for any of it: only the saved connections (and the
  // Schema versions database list of the connection chosen there).
  const serverReads = sent.filter((p) => (p.startsWith("/api/live/") && !["/api/live/schemas", "/api/live/privileges"].includes(p)) ||
    p.startsWith("/api/connection/") || p.startsWith("/api/accounts/") || p.startsWith("/api/config/"));
  assert.deepEqual(serverReads, [], "refreshing the lists reads no server");

  // The connection in use deleted elsewhere: the picker says so and keeps its settings.
  await chooseConnection(page, await connectionOption(page, "Charlie"));
  const charlie = (await outside("list", {})).items.find((p) => p.name === "Charlie");
  await outside("delete", { id: charlie.id });
  await backInWindow();
  await openConnectionPicker(page);
  await page.waitForFunction(() => document.querySelector('.window-toolbar select[aria-label="Connection"]')?.selectedOptions[0]?.textContent === "Charlie (not saved)");
  assert.match(await h.text(".window-toolbar .picker-note"), /The saved connection “Charlie” was deleted\. Its settings are kept as an unsaved connection\./);
  assert.match(await h.text(".window-toolbar .picker-endpoint"), /dev@db\.charlie:3306/);
  await page.keyboard.press("Escape");
  assert.deepEqual(h.errors, []);
  console.log("saved connections: created, renamed, password forgotten and deleted on Home show in the picker, Parameters and Users and roles Compare, and Schema versions at once; deleted choices say so; another window's changes show on focus; no server read.");
} finally {
  await h.close();
}
void state;
