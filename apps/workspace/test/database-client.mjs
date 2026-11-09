// A missing database client (mysql/mariadb) is a typed, recoverable error:
// the client-missing notice (with Choose client… and Try again, never the
// --mysql-binary text) in Health, in the connection form's Test connection,
// in Maintenance > Tools (which still lists the tools), in the Query editor
// after a refused Run, and in the Users and roles inspector after a refused
// account read; and the Database client dialog from Settings: the client in
// use, Detect, Use this one, a refused path, and Use automatic detection.
// Mocked /api/client/*, /api/connection/test, /api/lifecycle/tools,
// /api/editor/* and /api/accounts/*; the real Python server for the rest.
// Screenshots of the notice and the dialog at 1440 and 720 px, light and dark.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, pause } from "./perf-harness.mjs";

const CLIENT_ERROR = "myflames could not find the MySQL or MariaDB command-line client (mysql or mariadb).";
const missing = (prefix = "") => ({ status: 400, body: { error: `${prefix}${CLIENT_ERROR}`, code: "client-missing" } });

// The /api/client/* store of this suite: a stored choice, or automatic.
let storedPath = null;
const REFUSED = "/not/a/client/mysql";
const DETECTED = [
  { path: "/usr/local/bin/mysql", name: "mysql", version: "mysql  Ver 8.4.3 for macos15 on arm64", source: "path" },
  { path: "/opt/homebrew/opt/mysql-client/bin/mysql", name: "mysql", version: "mysql  Ver 9.1.0 for macos15 on arm64", source: "common" },
];
const settingsAnswer = () => ({
  status: 200,
  body: {
    schema_version: "client-settings-1.0",
    path: storedPath,
    active: storedPath ? { path: storedPath, version: "mysql  Ver 8.4.3 for macos15 on arm64", source: "chosen" } : null,
    detected: DETECTED,
  },
});
const toolsAnswer = {
  status: 200,
  body: {
    schema_version: "lifecycle-tools-1.0",
    tools: {
      mysqlsh: { available: true, deprecated: false, path: "/usr/local/bin/mysqlsh", version: "8.4.3", version_tuple: [8, 4, 3], engine: "mysql", download_url: "https://dev.mysql.com/downloads/shell/" },
      mysqldump: { available: false, deprecated: false, download_url: "https://dev.mysql.com/downloads/", error: "Not found on PATH" },
    },
    server: {},
    offered: {},
    server_error: { code: "client-missing", error: CLIENT_ERROR },
  },
};

const h = await open("/api/live/health", "database-client", {
  "/api/client/settings": (body) => {
    if (!("path" in body)) return settingsAnswer();
    if (body.path === null) { storedPath = null; return settingsAnswer(); }
    if (body.path === REFUSED) return { status: 400, body: { error: "That path is not a MySQL or MariaDB client." } };
    storedPath = body.path;
    return settingsAnswer();
  },
  "/api/client/detect": () => settingsAnswer(),
  "/api/connection/test": () => missing("Could not test the connection: "),
  "/api/lifecycle/tools": () => toolsAnswer,
  "/api/editor/status": { status: 200, body: { status: "none", session: null, run: null } },
  "/api/editor/preview": { status: 200, body: { status: "ready", statements: [],
    aggregate: { state: "unknown", missing: [], reason: "This session's privileges are not verified.", warning: "" },
    policy: { action: "confirm", text: "Review before running." } } },
  "/api/editor/run": () => missing("Could not run the statement: "),
  "/api/live/schemas": { status: 200, body: { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.3", taken_at: "x", uptime_s: 1,
    performance_schema: true, schemas: [{ name: "testdb", tables: 1, system: false }], user_tables: 1, all_limit: 2000, all_note: "", system: ["mysql"] } },
  "/api/accounts/list": { status: 200, body: { schema_version: "accounts-1.0", engine: "mysql", version: "8.4.3", complete: true, warnings: [],
    accounts: [{ name: "app", host: "%", role: false, locked: false }], role_grants: [], default_roles: [] } },
  "/api/accounts/read": () => missing("Could not read the account: "),
});
const { page, state } = h;
const shots = path.resolve(process.env.PERF_SHOTS || h.shots);
await fs.mkdir(shots, { recursive: true });
state.fallback = missing("Could not read the server status: ");
const settingsCalls = () => state.other.filter((r) => r.path === "/api/client/settings").map((r) => r.body);
const noBinaryFlag = async (where) =>
  assert.equal(await page.evaluate(() => document.body.textContent.includes("--mysql-binary")), false, `no --mysql-binary text ${where}`);
async function shoot(name, selector = "") {
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await pause(100);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
    if (selector) {
      const box = await page.$eval(selector, (e) => {
        const r = e.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      });
      assert.ok(box.left >= 0 && box.top >= 0 && box.right <= width && box.bottom <= (width === 720 ? 520 : 900),
        `${name} inside the window at ${width}: ${JSON.stringify(box)}`);
    }
    await page.screenshot({ path: path.join(shots, `client-${name}-${theme}-${width}.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
}

try {
  // 1. Health refused for a missing client: the notice, not the bare text.
  await h.server();
  await page.waitForSelector('.server-view[data-view=health] [data-client-missing]', { visible: true });
  const notice = '.server-view[data-view=health] [data-client-missing]';
  assert.match(await h.text(notice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(notice), /Choose client…/);
  assert.match(await h.text(notice), /Try again/);
  await noBinaryFlag("on Health");
  await shoot("notice", notice);

  // 2. Choose client… opens the Database client dialog; the read lists the
  // detected clients, Use this one stores the choice.
  await h.button("Choose client…", notice + " ");
  await page.waitForSelector("dialog.client-dialog[open]");
  await page.waitForSelector("dialog.client-dialog .client-detected-row");
  assert.deepEqual(settingsCalls(), [{}], "opening the dialog reads the settings once");
  const rows = await page.$$eval("dialog.client-dialog .client-detected-row", (els) => els.map((e) => e.textContent));
  assert.match(rows[0], /mysql/);
  assert.match(rows[0], /8\.4\.3/);
  assert.match(rows[0], /\/usr\/local\/bin\/mysql/);
  assert.match(await h.text("dialog.client-dialog"), /No client found\./);
  await shoot("dialog", "dialog.client-dialog");
  await h.button("Detect", "dialog.client-dialog ");
  for (let i = 0; i < 100 && !state.other.some((r) => r.path === "/api/client/detect"); i++) await pause(50);
  assert.ok(state.other.some((r) => r.path === "/api/client/detect"), "Detect reads /api/client/detect");
  await h.button("Use this one", "dialog.client-dialog .client-detected-row ");
  await page.waitForSelector("dialog.client-dialog .client-in-use");
  assert.deepEqual(settingsCalls().at(-1), { path: "/usr/local/bin/mysql" });
  assert.match(await h.text("dialog.client-dialog .client-in-use"), /\/usr\/local\/bin\/mysql/);
  assert.match(await h.text("dialog.client-dialog .client-in-use"), /Chosen here/);

  // 3. A refused path shows the server's words and changes nothing.
  await page.type("dialog.client-dialog .client-path-field input", REFUSED);
  await h.button("Use this path", "dialog.client-dialog ");
  await page.waitForSelector("dialog.client-dialog .form-error");
  assert.match(await h.text("dialog.client-dialog .form-error"), /That path is not a MySQL or MariaDB client\./);
  assert.deepEqual(settingsCalls().at(-1), { path: REFUSED });
  assert.match(await h.text("dialog.client-dialog .client-in-use"), /\/usr\/local\/bin\/mysql/, "the choice in use is kept");

  // 4. Use automatic detection clears the stored choice.
  await h.button("Use automatic detection", "dialog.client-dialog ");
  await page.waitForFunction(() => /No client found\./.test(document.querySelector("dialog.client-dialog")?.textContent || ""));
  assert.deepEqual(settingsCalls().at(-1), { path: null });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-dialog"));

  // 5. Try again reads the server again.
  const before = state.seen.length;
  await h.button("Try again", notice + " ");
  for (let i = 0; i < 100 && state.seen.length === before; i++) await pause(50);
  assert.ok(state.seen.length > before, "Try again reads again");
  await page.waitForSelector(notice, { visible: true });
  await noBinaryFlag("after Try again");

  // 6. The same dialog opens from Settings > Database client…
  await page.click(".settings-button");
  await page.waitForSelector("#settings-popover");
  await h.button("Database client…", "#settings-popover ");
  await page.waitForSelector("dialog.client-dialog[open]");
  await page.waitForSelector("dialog.client-dialog .client-detected-row");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-dialog"));

  // 7. Test connection refused for a missing client shows the notice.
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector("#connections-table");
  await h.button("Add connection");
  await page.waitForFunction(() => document.querySelector("#connection-form-title")?.textContent === "Add connection");
  const field = async (label) => page.evaluateHandle((text) => [...document.querySelectorAll(".connection-panel label.field")]
    .find((l) => l.firstChild?.textContent?.trim() === text)?.querySelector("input"), label);
  const host = await field("Host");
  await host.click({ clickCount: 3 });
  await host.type("db1.example");
  const user = await field("User");
  await user.click({ clickCount: 3 });
  await user.type("app");
  await h.button("Test connection", ".connection-panel ");
  await page.waitForSelector(".connection-panel [data-client-missing]");
  assert.match(await h.text(".connection-panel [data-client-missing]"), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  await noBinaryFlag("in the connection form");

  // 8. Maintenance > Tools with server_error: the tools table stays, the
  // notice stands where the server details would be.
  await h.server();
  await h.view("maintenance");
  await page.waitForSelector('.server-view[data-view=maintenance] table');
  assert.match(await h.text('.server-view[data-view=maintenance]'), /mysqlsh/);
  await page.waitForSelector('.server-view[data-view=maintenance] [data-client-missing]', { visible: true });
  assert.match(await h.text('.server-view[data-view=maintenance]'), /Server unreadable · Tools/);
  await noBinaryFlag("in Maintenance > Tools");

  // 9. Query editor: a Run refused for a missing client shows the notice in
  // the editor (not the bare text), and Try again runs the same statement.
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");
  await page.locator('[aria-label="SQL editor"]').fill("SELECT 1");
  await page.click("[data-action=run]");
  const editorNotice = ".query-lab [data-client-missing]";
  await page.waitForSelector(editorNotice, { visible: true });
  assert.match(await h.text(editorNotice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(editorNotice), /Choose client…/);
  assert.match(await h.text(editorNotice), /Try again/);
  await noBinaryFlag("in the Query editor");
  const editorRuns = () => state.other.filter((r) => r.path === "/api/editor/run");
  const runsBefore = editorRuns().length;
  await h.button("Try again", editorNotice + " ");
  for (let i = 0; i < 100 && editorRuns().length === runsBefore; i++) await pause(50);
  assert.equal(editorRuns().length, runsBefore + 1, "Try again runs the statement again");
  assert.equal(editorRuns().at(-1).body.sql, "SELECT 1", "the same statement runs again");
  await page.waitForSelector(editorNotice, { visible: true });

  // 10. Users and roles: the inspector's account read refused for a missing
  // client shows the notice there, and Try again reads the account again.
  await h.server();
  await h.view("accounts");
  await page.waitForSelector(".accounts-view tbody tr[data-row-key]", { visible: true });
  await page.click(".accounts-view tbody tr[data-row-key]");
  const inspectorNotice = ".view-inspector [data-client-missing]";
  await page.waitForSelector(inspectorNotice, { visible: true });
  assert.match(await h.text(inspectorNotice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(inspectorNotice), /Choose client…/);
  await noBinaryFlag("in the Users and roles inspector");
  const accountReads = () => state.other.filter((r) => r.path === "/api/accounts/read").length;
  const readsBefore = accountReads();
  await h.button("Try again", inspectorNotice + " ");
  for (let i = 0; i < 100 && accountReads() === readsBefore; i++) await pause(50);
  assert.equal(accountReads(), readsBefore + 1, "Try again reads the account again");

  assert.deepEqual(h.errors, []);
  console.log(`Database client suite passed. Screenshots: ${shots}`);
} finally { await h.close(); }
