// Client tools: one resolver decides which MySQL/MariaDB program runs each
// job for the server in use, and every place shows that one answer. A read
// refused with the client-missing code (or client-auth-plugin, showing the
// server's own text) gets the notice (Choose client tools…, Try again, never
// the --mysql-binary text) in Health, in the connection form's Test
// connection, in Maintenance > Tools (which still lists the tools), in the
// Query editor after a refused Run, and in the Users and roles inspector.
// The Client tools dialog (Settings > Client tools…): the Used for section,
// the per-engine selects (an installation sends its client program path,
// Automatic sends null, a refused path and a stale stored choice show their
// errors), Found on this computer, Detect again, the older-than-server
// warning, a MariaDB server with only MySQL tools, and the install hint only
// when nothing was found.
// Mocked /api/client/tools, /api/client/tools/choose, /api/connection/test,
// /api/lifecycle/tools, /api/editor/* and /api/accounts/*; the real Python
// server for the rest. Screenshots at 1440 and 720 px, light and dark.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, pause } from "./perf-harness.mjs";

const CLIENT_ERROR = "myflames could not find the MySQL or MariaDB command-line client (mysql or mariadb).";
const PLUGIN_ERROR = "mysql 8.4.13 (Bundled with myflames) cannot sign in to this server: the account uses the authentication_ldap_sasl_client plugin, which these client tools do not include. Choose client tools that include it in Settings > Client tools.";
const missing = (prefix = "") => ({ status: 400, body: { error: `${prefix}${CLIENT_ERROR}`, code: "client-missing" } });

// The owner's Mac: two Homebrew MySQL installations and the bundled one, a
// Homebrew MySQL Shell, and a MySQL 8.4.3 server resolved to the bundled tools.
const BUNDLED = "/Applications/myflames.app/Contents/Resources/client-tools/8.4/bin";
const INSTALLATIONS = [
  {
    key: `mysql:${BUNDLED}`, engine: "mysql", version: "8.4.13", series: "8.4",
    folder: BUNDLED, found_in: BUNDLED, origin: "bundled", origin_label: "Bundled with myflames",
    bundled: true, has_auth_plugins: true,
    programs: {
      mysql: { path: `${BUNDLED}/mysql`, realpath: `${BUNDLED}/mysql`, version: "8.4.13" },
      mysqldump: { path: `${BUNDLED}/mysqldump`, realpath: `${BUNDLED}/mysqldump`, version: "8.4.13" },
    },
    chosen_for: [],
  },
  {
    key: "mysql:/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin", engine: "mysql", version: "8.4.8", series: "8.4",
    folder: "/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin", found_in: "/opt/homebrew/bin",
    origin: "homebrew", origin_label: "Homebrew mysql@8.4", bundled: false, has_auth_plugins: true,
    programs: {
      mysql: { path: "/opt/homebrew/bin/mysql", realpath: "/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin/mysql", version: "8.4.8" },
      mysqldump: { path: "/opt/homebrew/bin/mysqldump", realpath: "/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin/mysqldump", version: "8.4.8" },
    },
    chosen_for: [],
  },
  {
    key: "mysql:/opt/homebrew/Cellar/mysql-client@8.0/8.0.45/bin", engine: "mysql", version: "8.0.45", series: "8.0",
    folder: "/opt/homebrew/Cellar/mysql-client@8.0/8.0.45/bin", found_in: "/opt/homebrew/opt/mysql-client/bin",
    origin: "homebrew", origin_label: "Homebrew mysql-client@8.0", bundled: false, has_auth_plugins: false,
    programs: {
      mysql: { path: "/opt/homebrew/opt/mysql-client/bin/mysql", realpath: "/opt/homebrew/Cellar/mysql-client@8.0/8.0.45/bin/mysql", version: "8.0.45" },
      mysqldump: { path: "/opt/homebrew/opt/mysql-client/bin/mysqldump", realpath: "/opt/homebrew/Cellar/mysql-client@8.0/8.0.45/bin/mysqldump", version: "8.0.45" },
    },
    chosen_for: [],
  },
];
const SHELLS = [
  { path: "/opt/homebrew/bin/mysqlsh", realpath: "/opt/homebrew/Cellar/mysql-shell/9.6.0/bin/mysqlsh", version: "9.6.0", origin: "homebrew", origin_label: "Homebrew mysql-shell" },
];
const bundledUse = (role, program, reason, warning) => ({
  role, program, path: `${BUNDLED}/${program}`, version: "8.4.13", engine: "mysql",
  installation: `mysql:${BUNDLED}`, origin: "bundled", origin_label: "Bundled with myflames",
  bundled: true, reason, warning,
});
const shellUse = {
  role: "shell", program: "mysqlsh", path: "/opt/homebrew/bin/mysqlsh", version: "9.6.0", engine: "mysql",
  installation: null, origin: "homebrew", origin_label: "Homebrew mysql-shell", bundled: false,
  reason: "newest", warning: null,
};
const USED_843 = {
  client: bundledUse("client", "mysql", "series", null),
  dump: bundledUse("dump", "mysqldump", "series", null),
  load: bundledUse("load", "mysql", "series", null),
  backup: null, shell: shellUse, missing: [], notes: [],
};
const USED_97 = {
  client: bundledUse("client", "mysql", "older", "mysql 8.4.13 is older than this server (MySQL 9.7). Install MySQL 9.7 client tools for this server."),
  dump: bundledUse("dump", "mysqldump", "older", "mysqldump 8.4.13 is older than this server (MySQL 9.7). Install MySQL 9.7 client tools for a complete dump."),
  load: bundledUse("load", "mysql", "older", "mysql 8.4.13 is older than this server (MySQL 9.7). Install MySQL 9.7 client tools for this server."),
  backup: null, shell: shellUse, missing: [], notes: [],
};
const USED_MARIADB = {
  client: null, dump: null, load: null, backup: null, shell: shellUse,
  missing: [
    { role: "client", program: "mariadb", text: "No MariaDB client tools found on this computer; queries cannot run." },
    { role: "dump", program: "mariadb-dump", text: "No MariaDB dump program found on this computer; dumps cannot run." },
    { role: "load", program: "mariadb", text: "No MariaDB client tools found on this computer; restores cannot run." },
  ],
  notes: ["The MySQL tools on this computer cannot read this MariaDB server."],
};
const USED_EMPTY = {
  client: null, dump: null, load: null, backup: null, shell: null,
  missing: [
    { role: "client", program: "mysql", text: "No MySQL client tools found on this computer; queries cannot run." },
    { role: "dump", program: "mysqldump", text: "No MySQL dump program found on this computer; dumps cannot run." },
  ],
  notes: [],
};

// The suite's state: what the resolver answers with.
let variant = "mysql843"; // mysql843 | older97 | mariadb | empty
const REFUSED = "/not/a/client/mysql";
const stored = { mysql: null, mariadb: null }; // null: Automatic; else {path, installation, error}
const resolveChoice = (engine, path) => {
  if (path === null) return { path: null, installation: null, error: null };
  if (path === "bundled") {
    const inst = INSTALLATIONS.find((i) => i.engine === engine && i.bundled);
    return { path, installation: inst?.key || null, error: inst ? null : "The bundled client tools are not in this installation." };
  }
  const inst = INSTALLATIONS.find((i) => i.engine === engine && Object.values(i.programs).some((p) => p.path === path));
  return inst
    ? { path, installation: inst.key, error: null }
    : { path, installation: null, error: `The client tools chosen in Settings > Client tools are no longer at ${path}.` };
};
const toolsAnswer = (withConnection) => {
  const answer = {
    schema_version: "client-tools-1.0",
    installations: variant === "empty" ? [] : INSTALLATIONS,
    shells: variant === "empty" ? [] : SHELLS,
    skipped: [],
    choice: {
      mysql: stored.mysql || { path: null, installation: null, error: null },
      mariadb: stored.mariadb || { path: null, installation: null, error: null },
    },
  };
  if (withConnection) {
    if (variant === "mariadb") {
      answer.server = { engine: "mariadb", version: "11.4.5" };
      answer.server_error = null;
      answer.used = USED_MARIADB;
    } else if (variant === "older97") {
      answer.server = { engine: "mysql", version: "9.7.0" };
      answer.server_error = null;
      answer.used = USED_97;
    } else {
      answer.server = { engine: "mysql", version: "8.4.3" };
      answer.server_error = null;
      answer.used = variant === "empty" ? USED_EMPTY : USED_843;
    }
  }
  return { status: 200, body: answer };
};

// Maintenance > Tools when the server cannot be read for lack of a client.
const lifecycleTools = {
  status: 200,
  body: {
    schema_version: "lifecycle-tools-2.0",
    tools: {
      mysqlsh: { available: true, deprecated: false, path: "/usr/local/bin/mysqlsh", version: "8.4.3", version_tuple: [8, 4, 3], engine: "mysql", download_url: "https://dev.mysql.com/downloads/shell/" },
      mysqldump: { available: false, deprecated: false, download_url: "https://dev.mysql.com/downloads/", error: "Not found on PATH" },
    },
    server: {},
    offered: {},
    server_error: { code: "client-missing", error: CLIENT_ERROR },
  },
};

const h = await open("/api/live/health", "client-tools", {
  "/api/client/tools": (body) => toolsAnswer(!!body.connection),
  "/api/client/tools/choose": (body) => {
    if (body.path === REFUSED) return { status: 400, body: { error: "That path is not a MySQL or MariaDB client." } };
    stored[body.engine] = resolveChoice(body.engine, body.path);
    return toolsAnswer(!!body.connection);
  },
  "/api/connection/test": () => missing("Could not test the connection: "),
  "/api/lifecycle/tools": () => lifecycleTools,
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
const toolsCalls = () => state.other.filter((r) => r.path === "/api/client/tools").map((r) => r.body);
const chooseCalls = () => state.other.filter((r) => r.path === "/api/client/tools/choose").map((r) => r.body);
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
    await page.screenshot({ path: path.join(shots, `client-tools-${name}-${theme}-${width}.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
}
const dialog = "dialog.client-tools-dialog";
const engineSection = (engine) => `${dialog} [data-engine="${engine}"]`;
const detectAgain = async () => {
  const before = toolsCalls().length;
  await h.button("Detect again", `${dialog} `);
  for (let i = 0; i < 100 && toolsCalls().length === before; i++) await pause(50);
  assert.equal(toolsCalls().length, before + 1, "Detect again reads again");
  assert.equal(toolsCalls().at(-1).refresh, true, "Detect again re-probes");
};

try {
  // 1. Health refused for a missing client: the notice, not the bare text.
  await h.server();
  const notice = '.server-view[data-view=health] [data-client-missing]';
  await page.waitForSelector(notice, { visible: true });
  assert.match(await h.text(notice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(notice), /Choose client tools…/);
  assert.match(await h.text(notice), /Try again/);
  await noBinaryFlag("on Health");
  await shoot("notice", notice);

  // 2. A client-auth-plugin refusal shows the server's own text with the same button.
  state.fallback = { status: 400, body: { error: PLUGIN_ERROR, code: "client-auth-plugin" } };
  await h.button("Try again", notice + " ");
  await page.waitForFunction(
    (t) => document.querySelector('[data-client-missing] .client-missing-text')?.textContent === t,
    {}, PLUGIN_ERROR);
  assert.match(await h.text(notice), /Choose client tools…/);
  await noBinaryFlag("on Health (client-auth-plugin)");
  state.fallback = missing("Could not read the server status: ");
  await h.button("Try again", notice + " ");
  await page.waitForFunction(() => /myflames needs the MySQL/.test(document.querySelector('[data-client-missing]')?.textContent || ""));

  // 3. Choose client tools… opens the Client tools dialog: the read names the
  // connection in use and the Used for section shows the resolver's lines.
  await h.button("Choose client tools…", notice + " ");
  await page.waitForSelector(`${dialog}[open]`);
  await page.waitForSelector(`${dialog} .client-tools-table tbody tr`);
  assert.equal(await h.text(`${dialog} h2`), "Client tools");
  assert.deepEqual(toolsCalls().length, 1, "opening the dialog reads the client tools once");
  assert.ok(toolsCalls()[0].connection, "the read names the connection in use");
  const used = `${dialog} [data-client-tools-used]`;
  assert.match(await h.text(dialog), /Used for /);
  assert.match(await h.text(used), /Queriesmysql 8\.4\.13 · Bundled with myflames · matches this server \(MySQL 8\.4\)/);
  assert.match(await h.text(used), /Dumpsmysqldump 8\.4\.13 · Bundled with myflames · matches this server \(MySQL 8\.4\)/);
  assert.match(await h.text(used), /Restoresmysql 8\.4\.13 · Bundled with myflames · matches this server \(MySQL 8\.4\)/);
  assert.match(await h.text(used), /MySQL Shellmysqlsh 9\.6\.0 · Homebrew mysql-shell · newest MySQL Shell found/);
  assert.equal(await page.$(`${used} [data-role=backup]`), null, "no physical backup line without one");
  // The install hint stays away while installations exist.
  assert.equal(await page.evaluate(() => document.querySelector("dialog.client-tools-dialog")?.textContent.includes("brew install mysql-client")), false,
    "no install hint while installations exist");
  // Found on this computer: the three installations and the shell.
  const foundRows = await page.$$eval(`${dialog} .client-tools-table tbody tr`, (rows) => rows.map((r) => r.textContent));
  assert.equal(foundRows.length, 3);
  assert.match(foundRows[0], /MySQL 8\.4\.13.*Bundled with myflames.*mysql, mysqldump/);
  assert.match(foundRows[1], /MySQL 8\.4\.8.*Homebrew mysql@8\.4.*\/opt\/homebrew\/Cellar\/mysql@8\.4\/8\.4\.8\/bin/);
  assert.match(foundRows[2], /MySQL 8\.0\.45.*Homebrew mysql-client@8\.0/);
  assert.match(await h.text(`${dialog} .client-tools-shells`), /MySQL Shell 9\.6\.0 · Homebrew mysql-shell/);
  await shoot("dialog", dialog);

  // 4. Choosing an installation sends its client program path with the engine
  // and the connection; the folder shows under the select.
  await page.select(`${engineSection("mysql")} select`, "mysql:/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin");
  await page.waitForFunction(() => document.querySelector('dialog.client-tools-dialog [data-engine="mysql"] select')?.value === "mysql:/opt/homebrew/Cellar/mysql@8.4/8.4.8/bin");
  assert.deepEqual([chooseCalls().at(-1).engine, chooseCalls().at(-1).path], ["mysql", "/opt/homebrew/bin/mysql"]);
  assert.ok(chooseCalls().at(-1).connection, "the choice names the connection in use");
  assert.match(await h.text(`${engineSection("mysql")} .client-tools-folder`), /\/opt\/homebrew\/Cellar\/mysql@8\.4\/8\.4\.8\/bin/);

  // 5. Automatic sends path null.
  await page.select(`${engineSection("mysql")} select`, "auto");
  await page.waitForFunction(() => document.querySelector('dialog.client-tools-dialog [data-engine="mysql"] select')?.value === "auto");
  assert.deepEqual([chooseCalls().at(-1).engine, chooseCalls().at(-1).path], ["mysql", null]);

  // 6. A refused path shows the server's words and changes nothing.
  await page.type(`${engineSection("mysql")} .client-path-field input`, REFUSED);
  await h.button("Use this path", `${engineSection("mysql")} `);
  await page.waitForSelector(`${dialog} .form-error`);
  assert.match(await h.text(`${dialog} .form-error`), /That path is not a MySQL or MariaDB client\./);
  assert.equal(chooseCalls().at(-1).path, REFUSED);
  assert.equal(await page.$eval(`${engineSection("mysql")} select`, (e) => e.value), "auto", "the choice in use is kept");

  // 7. A stored choice that no longer works shows its error and stays selected.
  stored.mysql = { path: "/old/mysql/bin/mysql", installation: null, error: "The client tools chosen in Settings > Client tools are no longer at /old/mysql/bin/mysql." };
  await detectAgain();
  await page.waitForSelector(`${engineSection("mysql")} .form-error`);
  assert.match(await h.text(`${engineSection("mysql")} .form-error`), /no longer at \/old\/mysql\/bin\/mysql\./);
  assert.equal(await page.$eval(`${engineSection("mysql")} select`, (e) => e.value), "broken", "the stale choice stays selected");
  assert.match(await h.text(`${engineSection("mysql")} select option:checked`), /\/old\/mysql\/bin\/mysql/);
  stored.mysql = null;

  // 8. A server newer than the tools: the older warning, in the warning style.
  variant = "older97";
  await detectAgain();
  await page.waitForFunction(() => /MySQL 9\.7/.test(document.querySelector('dialog.client-tools-dialog [data-client-tools-used]')?.textContent || ""));
  assert.match(await h.text(used), /Dumpsmysqldump 8\.4\.13 · Bundled with myflames · mysqldump 8\.4\.13 is older than this server \(MySQL 9\.7\)\. Install MySQL 9\.7 client tools for a complete dump\./);
  assert.ok(await page.$(`${used} [data-role=dump] .client-tools-warning`), "the older-than-server line is a warning");

  // 9. A MariaDB server with only MySQL tools: the missing dump line and the note.
  variant = "mariadb";
  await detectAgain();
  await page.waitForFunction(() => /No MariaDB dump program/.test(document.querySelector('dialog.client-tools-dialog [data-client-tools-used]')?.textContent || ""));
  assert.match(await h.text(used), /DumpsNo MariaDB dump program found on this computer; dumps cannot run\./);
  assert.match(await h.text(used), /QueriesNo MariaDB client tools found on this computer; queries cannot run\./);
  assert.match(await h.text(used), /The MySQL tools on this computer cannot read this MariaDB server\./);
  assert.ok(await page.$(`${used} [data-role=dump] .client-tools-warning`), "a missing role is a warning line");

  // 10. Nothing found: the install hint shows.
  variant = "empty";
  await detectAgain();
  await page.waitForFunction(() => /No client tools found in the usual places\./.test(document.querySelector("dialog.client-tools-dialog")?.textContent || ""));
  assert.match(await h.text(dialog), /Install .* client/);
  variant = "mysql843";
  await detectAgain();
  await page.waitForSelector(`${dialog} .client-tools-table tbody tr`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-tools-dialog"));

  // 11. Try again reads the server again.
  const before = state.seen.length;
  await h.button("Try again", notice + " ");
  for (let i = 0; i < 100 && state.seen.length === before; i++) await pause(50);
  assert.ok(state.seen.length > before, "Try again reads again");
  await page.waitForSelector(notice, { visible: true });
  await noBinaryFlag("after Try again");

  // 12. The same dialog opens from Settings > Client tools…
  await page.click(".settings-button");
  await page.waitForSelector("#settings-popover");
  await h.button("Client tools…", "#settings-popover ");
  await page.waitForSelector(`${dialog}[open]`);
  await page.waitForSelector(`${dialog} .client-tools-table tbody tr`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-tools-dialog"));

  // 13. Test connection refused for a missing client shows the notice.
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
  assert.match(await h.text(".connection-panel [data-client-missing]"), /Choose client tools…/);
  await noBinaryFlag("in the connection form");

  // 14. Maintenance > Tools with server_error: the tools table stays, the
  // notice stands where the server details would be.
  await h.server();
  await h.view("maintenance");
  await page.waitForSelector('.server-view[data-view=maintenance] table');
  assert.match(await h.text('.server-view[data-view=maintenance]'), /mysqlsh/);
  await page.waitForSelector('.server-view[data-view=maintenance] [data-client-missing]', { visible: true });
  assert.match(await h.text('.server-view[data-view=maintenance]'), /Server unreadable · Tools/);
  await noBinaryFlag("in Maintenance > Tools");

  // 15. Query editor: a Run refused for a missing client shows the notice in
  // the editor (not the bare text), and Try again runs the same statement.
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");
  await page.locator('[aria-label="SQL editor"]').fill("SELECT 1");
  await page.click("[data-action=run]");
  const editorNotice = ".query-lab [data-client-missing]";
  await page.waitForSelector(editorNotice, { visible: true });
  assert.match(await h.text(editorNotice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(editorNotice), /Choose client tools…/);
  assert.match(await h.text(editorNotice), /Try again/);
  await noBinaryFlag("in the Query editor");
  const editorRuns = () => state.other.filter((r) => r.path === "/api/editor/run");
  const runsBefore = editorRuns().length;
  await h.button("Try again", editorNotice + " ");
  for (let i = 0; i < 100 && editorRuns().length === runsBefore; i++) await pause(50);
  assert.equal(editorRuns().length, runsBefore + 1, "Try again runs the statement again");
  assert.equal(editorRuns().at(-1).body.sql, "SELECT 1", "the same statement runs again");
  await page.waitForSelector(editorNotice, { visible: true });

  // 16. Users and roles: the inspector's account read refused for a missing
  // client shows the notice there, and Try again reads the account again.
  await h.server();
  await h.view("accounts");
  await page.waitForSelector(".accounts-view tbody tr[data-row-key]", { visible: true });
  await page.click(".accounts-view tbody tr[data-row-key]");
  const inspectorNotice = ".view-inspector [data-client-missing]";
  await page.waitForSelector(inspectorNotice, { visible: true });
  assert.match(await h.text(inspectorNotice), /myflames needs the MySQL or MariaDB command-line client to read servers\./);
  assert.match(await h.text(inspectorNotice), /Choose client tools…/);
  await noBinaryFlag("in the Users and roles inspector");
  const accountReads = () => state.other.filter((r) => r.path === "/api/accounts/read").length;
  const readsBefore = accountReads();
  await h.button("Try again", inspectorNotice + " ");
  for (let i = 0; i < 100 && accountReads() === readsBefore; i++) await pause(50);
  assert.equal(accountReads(), readsBefore + 1, "Try again reads the account again");

  assert.deepEqual(h.errors, []);
  console.log(`Client tools suite passed. Screenshots: ${shots}`);
} finally { await h.close(); }
