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
// server for the rest. The MySQL Shell section mocks /api/client/shell/*:
// idle, downloading with progress, Stop, failed, done, too old, unsupported,
// stalled retries, out-of-order replies, cleanup, focus and closing mid-job.
// Screenshots at 1440 and 720 px, light and dark.
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

// The managed MySQL Shell endpoints (managed-shell-status-1.0): a mutable
// state the scenarios below steer. Install rejoins active/cleaning jobs, and
// cancel requests cancellation without pretending the worker already ended.
// Each response is a snapshot, including when a scenario holds its delivery.
const SHELL_PINNED = { version: "9.7.0", size: 157547585, license: "GPL-2.0-only WITH Universal-FOSS-exception-1.0",
  license_url: "https://www.mysql.com/about/legal/licensing/oem/", downloads_url: "https://dev.mysql.com/downloads/shell/9.7.html" };
let shellState = { supported: true, unsupported_reason: null, platform: "darwin-arm64", pinned: SHELL_PINNED, installed: null, job: null };
let shellNext = null;
let shellSerial = 0;
const shellJob = (state, extra = {}) => ({ id: (++shellSerial).toString(16).padStart(32, "0"), state, version: "9.7.0",
  received: 0, total: SHELL_PINNED.size, elapsed_s: 0.2, error: null, cleaning: false, ...extra });
const shellActive = (job) => job && ["downloading", "verifying", "installing"].includes(job.state);
const shellStatusAnswer = () => ({ status: 200, body: JSON.parse(JSON.stringify({ schema_version: "managed-shell-status-1.0", ...shellState })) });
const shellReplies = { status: [], install: [], cancel: [] };
const shellReply = (kind, request, answer) => {
  const override = shellReplies[kind].shift();
  return override ? override(answer, request) : answer;
};
const SHELL_33 = 51990703; // 33% of 157547585: "52 MB of 158 MB"

const h = await open("/api/live/health", "client-tools", {
  "/api/client/tools": (body) => toolsAnswer(!!body.connection),
  "/api/client/shell/status": (_body, request) => shellReply("status", request, shellStatusAnswer()),
  "/api/client/shell/install": (_body, request) => {
    if (!shellActive(shellState.job) && !shellState.job?.cleaning) {
      shellState.job = shellNext || shellJob("downloading");
      shellNext = null;
    }
    return shellReply("install", request, shellStatusAnswer());
  },
  "/api/client/shell/cancel": (body, request) => {
    if (!shellState.job) return { status: 400, body: { error: "No MySQL Shell download is running." } };
    if (body.id !== shellState.job.id) return { status: 400, body: { error: "That MySQL Shell download is no longer active." } };
    return shellReply("cancel", request, shellStatusAnswer());
  },
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
  await page.waitForSelector(".settings-page .source-item");
  await page.locator(".settings-page .source-item ::-p-text(Client tools)").click();
  await h.button("Client tools…", ".settings-panel ");
  await page.waitForSelector(`${dialog}[open]`);
  await page.waitForSelector(`${dialog} .client-tools-table tbody tr`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-tools-dialog"));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));

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

  // 17. MySQL Shell, idle: nothing usable found; the offer, the provenance
  // line with sizes, and both Oracle links.
  const shellSection = `${dialog} [data-shell-section]`;
  const openDialog = async () => {
    await page.click(".settings-button");
    await page.waitForSelector(".settings-page .source-item");
    await page.locator(".settings-page .source-item ::-p-text(Client tools)").click();
    await h.button("Client tools…", ".settings-panel ");
    await page.waitForSelector(`${dialog}[open]`);
    await page.waitForSelector(`${shellSection} :is(.managed-shell-actions, .managed-shell-status) > *, ${shellSection} > .hint`);
  };
  const closeDialog = async () => {
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.client-tools-dialog"));
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector(".settings-page"));
  };
  const shellCalls = (path) => state.other.filter((r) => r.path === path).map((r) => r.body);
  const waitForShellCalls = async (path, count) => {
    for (let i = 0; i < 100 && shellCalls(path).length < count; i++) await pause(25);
    assert.ok(shellCalls(path).length >= count, `${path} reached ${count} requests`);
  };
  const holdShellReply = (kind) => {
    let held = null;
    shellReplies[kind].push((answer, request) => {
      held = { answer, request };
      return null;
    });
    return {
      async wait() {
        for (let i = 0; i < 100 && !held; i++) await pause(25);
        assert.ok(held, `a ${kind} reply was held`);
      },
      async release() {
        assert.ok(held, `a ${kind} reply is available`);
        await held.request.respond({ status: held.answer.status, contentType: "application/json", body: JSON.stringify(held.answer.body) });
      },
    };
  };
  // The dialog scrolls its own body: bring the section into view after each
  // viewport change, then assert it sits inside the window like shoot() does.
  const shootShell = async (name) => {
    for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
      await page.setViewport({ width, height: width === 720 ? 520 : 900 });
      await page.$eval(shellSection, (e) => {
        const d = e.closest("dialog");
        if (d) d.scrollTop = Math.max(0, e.offsetTop - (d.clientHeight - e.offsetHeight) / 2);
      });
      await pause(100);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
      const overflow = await page.$$eval(`${dialog}, ${shellSection}, ${shellSection} .managed-shell-progress`, (els) =>
        els.filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => ({ class: e.className, scroll: e.scrollWidth, width: e.clientWidth })));
      assert.deepEqual(overflow, [], `${name} has no internal horizontal overflow in ${theme} at ${width}`);
      const box = await page.$eval(shellSection, (e) => {
        const r = e.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
      });
      assert.ok(box.left >= 0 && box.top >= 0 && box.right <= width && box.bottom <= (width === 720 ? 520 : 900),
        `${name} inside the window at ${width}: ${JSON.stringify(box)}`);
      await page.screenshot({ path: path.join(shots, `client-tools-${name}-${theme}-${width}.png`) });
    }
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
    await page.setViewport({ width: 1440, height: 900 });
  };

  variant = "empty";
  shellState = { supported: true, unsupported_reason: null, platform: "darwin-arm64", pinned: SHELL_PINNED, installed: null, job: null };
  await openDialog();
  assert.match(await h.text(shellSection), /MySQL Shell/);
  assert.match(await h.text(shellSection), /Downloads MySQL Shell 9\.7\.0 \(158 MB, about 650 MB installed\) from Oracle into myflames's data folder; not redistributed by myflames\./);
  const licenseLink = await page.$eval(`${shellSection} a`, (a) => a.href);
  assert.equal(licenseLink, "https://www.mysql.com/about/legal/licensing/oem/");
  const links = await page.$$eval(`${shellSection} a`, (as) => as.map((a) => [a.textContent, a.href]));
  assert.deepEqual(links, [["GPLv2 license (Oracle)", "https://www.mysql.com/about/legal/licensing/oem/"],
    ["Oracle downloads", "https://dev.mysql.com/downloads/shell/9.7.html"]]);
  assert.equal(shellCalls("/api/client/shell/install").length, 0, "idle reads nothing but status");
  await shootShell("shell-idle");

  // 18. Downloading: the determinate bar with the progress line, Stop armed
  // after 600 ms, and every poll only while the job runs.
  await h.button("Get MySQL Shell", `${shellSection} `);
  // Stop is disarmed until 600 ms into the run: read it as soon as it exists.
  const stopAt = `${shellSection} .managed-shell-actions button.small`;
  const stopHandle = await page.waitForSelector(stopAt);
  assert.equal(await stopHandle.evaluate((b) => b.disabled), true, "Stop is not armed yet");
  await page.waitForSelector(`${shellSection} progress`);
  assert.match(await h.text(shellSection), /Downloading MySQL Shell 9\.7\.0 from Oracle: 0% \(0 MB of 158 MB\)/);
  shellState.job = { ...shellState.job, received: SHELL_33 };
  await page.waitForFunction(() => /from Oracle: 33% \(52 MB of 158 MB\)/.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  assert.equal(await page.$eval(`${shellSection} progress`, (p) => p.value), 33);
  const polls = shellCalls("/api/client/shell/status").length;
  await pause(700);
  assert.ok(shellCalls("/api/client/shell/status").length > polls, "status is polled while the job runs");
  assert.equal(await page.$eval(stopAt, (b) => b.disabled), false, "Stop is armed after 600 ms");
  await shootShell("shell-downloading");

  // 19. A successful cancel can still report an active worker. Keep Stopping…
  // disabled across polls until its final state, then restore focus and stop.
  const stoppedId = shellState.job.id;
  await h.button("Stop", `${shellSection} `);
  await page.waitForFunction(() => document.querySelector('[data-shell-section] .managed-shell-actions button.small')?.textContent === "Stopping…");
  const stoppingPolls = shellCalls("/api/client/shell/status").length;
  await waitForShellCalls("/api/client/shell/status", stoppingPolls + 2);
  assert.equal(await page.$eval(stopAt, (b) => b.disabled), true, "Stop stays unavailable after the cancel reply and later active polls");
  assert.equal(await h.text(stopAt), "Stopping…");
  assert.equal(shellCalls("/api/client/shell/cancel").length, 1, "polling does not send another cancel");
  await shootShell("shell-stopping");
  shellState.job = { ...shellState.job, state: "cancelled" };
  await page.waitForFunction(() => /Download stopped; nothing was installed\./.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  assert.equal(shellCalls("/api/client/shell/cancel").length, 1);
  assert.deepEqual(shellCalls("/api/client/shell/cancel")[0], { id: stoppedId });
  await page.waitForFunction(() => document.activeElement?.textContent?.includes("Get MySQL Shell"), {}, );
  const pollsAfterStop = shellCalls("/api/client/shell/status").length;
  await pause(1200);
  assert.equal(shellCalls("/api/client/shell/status").length, pollsAfterStop, "no polling without a running job");
  await closeDialog();

  // 20. Failed: the backend's plain reason in an alert, with Try again.
  shellNext = shellJob("failed", { error: "The download ended early." });
  await openDialog();
  await h.button("Get MySQL Shell", `${shellSection} `);
  await page.waitForSelector(`${shellSection} [role="alert"]`);
  assert.match(await h.text(`${shellSection} [role="alert"]`), /Could not install MySQL Shell: The download ended early\. Nothing was installed\./);
  assert.ok(await page.$eval(`${shellSection} [role="alert"] button`, (b) => b.textContent === "Try again"), "the alert offers Try again");
  await shootShell("shell-failed");

  // 21. Done: the ready line, and the client tools list is read again so the
  // new copy shows under Found on this computer.
  shellNext = shellJob("downloading");
  await h.button("Try again", `${shellSection} `);
  await page.waitForSelector(`${shellSection} progress`);
  const toolsBefore = toolsCalls().length;
  shellState.job = { ...shellState.job, state: "done" };
  await page.waitForFunction(() => /MySQL Shell 9\.7\.0 is ready\./.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  for (let i = 0; i < 100 && toolsCalls().length === toolsBefore; i++) await pause(50);
  assert.equal(toolsCalls().length, toolsBefore + 1, "a finished install re-reads the client tools");
  assert.equal(toolsCalls().at(-1).refresh, true, "the re-read re-probes");
  await shootShell("shell-done");
  await closeDialog();

  // 22. Too old: a found 9.6.0 Shell with 9.7.0 pinned says so and names the
  // pinned version on the button.
  variant = "mysql843";
  shellState = { supported: true, unsupported_reason: null, platform: "darwin-arm64", pinned: SHELL_PINNED, installed: null, job: null };
  await openDialog();
  await page.waitForSelector(`${shellSection} .managed-shell-actions .action-button`);
  assert.match(await h.text(shellSection), /MySQL Shell 9\.6\.0 on this computer is older than 9\.7\.0, the version myflames works with\./);
  assert.match(await h.text(`${shellSection} .managed-shell-actions`), /Get MySQL Shell 9\.7\.0/);
  await shootShell("shell-too-old");
  await closeDialog();

  // 23. Unsupported platform: no Get button, only Open Oracle downloads.
  shellState = { supported: false, unsupported_reason: "MySQL Shell downloads are available for macOS arm64, macOS x64 and Windows x64.", platform: null, pinned: null, installed: null, job: null };
  await openDialog();
  await page.waitForSelector(`${shellSection} a`);
  assert.match(await h.text(shellSection), /MySQL Shell downloads are available for macOS arm64, macOS x64 and Windows x64\./);
  assert.equal(await page.$(`${shellSection} .action-button`), null, "no Get MySQL Shell button on an unsupported platform");
  assert.match(await h.text(`${shellSection} .managed-shell-actions`), /Open Oracle downloads/);
  assert.equal(await page.$eval(`${shellSection} a`, (a) => a.href), "https://dev.mysql.com/downloads/shell/");
  await shootShell("shell-unsupported");
  await closeDialog();

  // 24. A zero-byte failure followed by a zero-byte retry gets a fresh stall
  // clock. Move Date.now only, leaving the real 500 ms polling timers intact.
  variant = "empty";
  shellState = { supported: true, unsupported_reason: null, platform: "darwin-arm64", pinned: SHELL_PINNED, installed: null, job: null };
  await openDialog();
  await h.button("Get MySQL Shell", `${shellSection} `);
  await page.waitForSelector(`${shellSection} progress`);
  const zeroByteId = shellState.job.id;
  await page.evaluate(() => {
    window.__shellRealNow = Date.now;
    Date.now = () => window.__shellRealNow() + 11_000;
  });
  try {
    await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Still waiting for cdn.mysql.com."));
    await shootShell("shell-stalled");
    shellState.job = { ...shellState.job, state: "failed", error: "The download ended early." };
    await page.waitForSelector(`${shellSection} [role=alert]`);
    await h.button("Try again", `${shellSection} `);
    await page.waitForSelector(`${shellSection} progress`);
    assert.notEqual(shellState.job.id, zeroByteId, "retry starts a distinct job");
    assert.equal(shellState.job.received, 0, "both attempts received zero bytes");
    assert.doesNotMatch(await h.text(shellSection), /Still waiting for cdn.mysql.com\./, "retry does not inherit the old stall");
    const retryPolls = shellCalls("/api/client/shell/status").length;
    await waitForShellCalls("/api/client/shell/status", retryPolls + 2);
    assert.doesNotMatch(await h.text(shellSection), /Still waiting for cdn.mysql.com\./, "later zero-byte polls keep the retry's fresh clock");
  } finally {
    await page.evaluate(() => { Date.now = window.__shellRealNow; delete window.__shellRealNow; });
  }
  shellState.job = { ...shellState.job, state: "cancelled" };
  await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Download stopped; nothing was installed."));
  await closeDialog();

  // 25. Hold a failed poll from A. A newer cancel read sees A's failure, then
  // Try again starts B. Deliver the poll last, and also return A from a newer
  // status request: neither may replace B or stop its polling.
  await openDialog();
  await h.button("Get MySQL Shell", `${shellSection} `);
  await page.waitForFunction(() => document.querySelector('[data-shell-section] .managed-shell-actions button.small')?.disabled === false);
  shellState.job = { ...shellState.job, state: "failed", error: "Failure from the previous attempt." };
  const oldFailure = { ...shellState.job };
  const latePoll = holdShellReply("status");
  await latePoll.wait();
  await h.button("Stop", `${shellSection} `);
  await page.waitForSelector(`${shellSection} [role=alert]`);
  await h.button("Try again", `${shellSection} `);
  await page.waitForSelector(`${shellSection} progress`);
  const currentId = shellState.job.id;
  assert.notEqual(currentId, oldFailure.id);
  await latePoll.release();
  await pause(100);
  assert.equal(await page.$(`${shellSection} [role=alert]`), null, "late A cannot restore its failure");
  assert.ok(await page.$(`${shellSection} progress`), "B keeps its progress bar");
  shellReplies.status.push((answer) => ({ ...answer, body: { ...answer.body, job: oldFailure } }));
  const afterLatePoll = shellCalls("/api/client/shell/status").length;
  await waitForShellCalls("/api/client/shell/status", afterLatePoll + 2);
  assert.equal(await page.$(`${shellSection} [role=alert]`), null, "a retired job id is ignored even on a newer request");
  shellState.job = { ...shellState.job, received: SHELL_33 };
  await page.waitForFunction(() => /from Oracle: 33%/.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  assert.equal(shellState.job.id, currentId, "B remains the current job");

  // A late same-job poll cannot move progress backwards after a newer action
  // reply either. The action is deliberately held to exercise reply latency.
  const oldProgress = holdShellReply("status");
  await oldProgress.wait();
  shellState.job = { ...shellState.job, received: SHELL_33 * 2 };
  const lateCancel = holdShellReply("cancel");
  await h.button("Stop", `${shellSection} `);
  await lateCancel.wait();
  const duringCancel = shellCalls("/api/client/shell/status").length;
  await pause(1100);
  assert.equal(shellCalls("/api/client/shell/status").length, duringCancel, "polls do not supersede a pending action reply");
  await lateCancel.release();
  await page.waitForFunction(() => /from Oracle: 66%/.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  await oldProgress.release();
  await pause(100);
  assert.match(await h.text(shellSection), /from Oracle: 66%/, "an older reply cannot roll back the newer action's progress");
  assert.equal(await h.text(stopAt), "Stopping…");
  shellState.job = { ...shellState.job, state: "cancelled" };
  await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Download stopped; nothing was installed."));
  await closeDialog();

  // 26. A cancel refusal survives active polls. Clear it only when a retry
  // succeeds or when the job ends, and allow Stop to be retried after failure.
  const cancelMessage = "Could not stop the MySQL Shell download. Try Stop again.";
  for (const resolution of ["retry", "end"]) {
    await openDialog();
    await h.button("Get MySQL Shell", `${shellSection} `);
    await page.waitForFunction(() => document.querySelector('[data-shell-section] .managed-shell-actions button.small')?.disabled === false);
    shellReplies.cancel.push(() => ({ status: 400, body: { error: cancelMessage } }));
    await h.button("Stop", `${shellSection} `);
    await page.waitForSelector(`${shellSection} [role=alert]`);
    const failedCancelPolls = shellCalls("/api/client/shell/status").length;
    await waitForShellCalls("/api/client/shell/status", failedCancelPolls + 3);
    assert.equal(await h.text(`${shellSection} [role=alert]`), cancelMessage, "polls preserve the cancel failure");
    assert.equal(await page.$eval(stopAt, (b) => b.disabled), false, "a failed cancel permits another Stop");
    if (resolution === "retry") {
      await shootShell("shell-cancel-error");
      const cancelCount = shellCalls("/api/client/shell/cancel").length;
      await h.button("Stop", `${shellSection} `);
      await page.waitForFunction(() => document.querySelector('[data-shell-section] .managed-shell-actions button.small')?.textContent === "Stopping…");
      await page.waitForFunction(() => !document.querySelector("[data-shell-section] [role=alert]"));
      assert.equal(shellCalls("/api/client/shell/cancel").length, cancelCount + 1);
      assert.equal(await page.$eval(stopAt, (b) => b.disabled), true, "successful retry waits for the worker's final state");
    }
    shellState.job = { ...shellState.job, state: "cancelled" };
    await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Download stopped; nothing was installed."));
    assert.equal(await page.$(`${shellSection} [role=alert]`), null, "job completion clears a cancel failure");
    await closeDialog();
  }

  // 27. Cleanup can start between the last status and Get/Try again. Install
  // returns the SAME terminal job, then a single click waits and starts B as
  // soon as cleaning becomes false. Do not show stale failure/progress text.
  for (const trigger of ["Get MySQL Shell", "Try again"]) {
    const previous = shellJob(trigger === "Try again" ? "failed" : "cancelled", { error: trigger === "Try again" ? "Previous attempt failed." : null });
    shellState.job = trigger === "Try again" ? previous : null;
    await openDialog();
    shellState.job = { ...previous, cleaning: true };
    shellNext = shellJob("downloading");
    const nextId = shellNext.id;
    const installBeforeCleanup = shellCalls("/api/client/shell/install").length;
    await h.button(trigger, `${shellSection} `);
    await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Finishing the last attempt…"));
    assert.equal(shellState.job.id, previous.id, "install rejoins the cleaning job without replacing it");
    assert.equal(shellNext.id, nextId, "cleanup does not consume the next job");
    assert.equal(await page.$(`${shellSection} progress`), null, "cleanup has no progress bar");
    assert.equal(await page.$(`${shellSection} [role=alert]`), null, "cleanup hides the previous failure");
    assert.doesNotMatch(await h.text(shellSection), /Download stopped|Could not install MySQL Shell/);
    const cleanupPolls = shellCalls("/api/client/shell/status").length;
    await waitForShellCalls("/api/client/shell/status", cleanupPolls + 2);
    assert.equal(shellCalls("/api/client/shell/install").length, installBeforeCleanup + 1, "cleanup polls status without repeatedly starting installs");
    await shootShell(trigger === "Try again" ? "shell-retry-cleanup" : "shell-get-cleanup");
    shellState.job = { ...shellState.job, cleaning: false };
    await page.waitForSelector(`${shellSection} progress`);
    assert.equal(shellState.job.id, nextId, "cleanup completion automatically starts the requested install");
    assert.equal(shellCalls("/api/client/shell/install").length, installBeforeCleanup + 2, "exactly one automatic start follows cleanup");
    assert.doesNotMatch(await h.text(shellSection), /Finishing the last attempt|Previous attempt failed/);
    shellState.job = { ...shellState.job, state: "cancelled" };
    await page.waitForFunction(() => document.querySelector("[data-shell-section]")?.textContent.includes("Download stopped; nothing was installed."));
    await closeDialog();
  }

  // 28. An idle dialog reads once when it regains focus or becomes visible,
  // adopts another window's job, and then polls it. Closing mid-download
  // removes both the interval and the idle refresh listeners.
  const refreshShell = async (event) => page.evaluate((type) => {
    if (type === "focus") window.dispatchEvent(new Event(type));
    else document.dispatchEvent(new Event(type));
  }, event);
  for (const event of ["focus", "visibilitychange"]) {
    shellState.job = null;
    await openDialog();
    const idleStatus = shellCalls("/api/client/shell/status").length;
    await pause(1100);
    assert.equal(shellCalls("/api/client/shell/status").length, idleStatus, "an idle dialog does not poll");
    await refreshShell(event);
    await waitForShellCalls("/api/client/shell/status", idleStatus + 1);
    await pause(1100);
    assert.equal(shellCalls("/api/client/shell/status").length, idleStatus + 1, `${event} reads idle status once`);
    if (event === "visibilitychange") {
      await page.evaluate(() => {
        Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await pause(600);
      assert.equal(shellCalls("/api/client/shell/status").length, idleStatus + 1, "becoming hidden does not refresh");
      await page.evaluate(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); });
    }
    shellState.job = shellJob("downloading", { received: SHELL_33 });
    await refreshShell(event);
    await page.waitForFunction(() => /from Oracle: 33%/.test(document.querySelector("[data-shell-section]")?.textContent || ""));
    await page.waitForFunction(() => document.querySelector('[data-shell-section] .managed-shell-actions button.small')?.disabled === false);
    const adoptedPolls = shellCalls("/api/client/shell/status").length;
    await waitForShellCalls("/api/client/shell/status", adoptedPolls + 2);
    await closeDialog();
    const afterClose = shellCalls("/api/client/shell/status").length;
    await refreshShell("focus");
    await refreshShell("visibilitychange");
    await pause(1500);
    assert.equal(shellCalls("/api/client/shell/status").length, afterClose, "closing a downloading dialog stops polling and focus/visibility reads");
    if (event === "visibilitychange") await page.evaluate(() => { delete document.visibilityState; });
  }

  // 29. A delayed action reply from a closed dialog has no authority over a
  // reopened one. The backend may have finished A and started B in between.
  shellState.job = null;
  await openDialog();
  const closedInstall = holdShellReply("install");
  await h.button("Get MySQL Shell", `${shellSection} `);
  await closedInstall.wait();
  await closeDialog();
  shellState.job = shellJob("downloading", { received: SHELL_33 });
  await openDialog();
  await page.waitForFunction(() => /from Oracle: 33%/.test(document.querySelector("[data-shell-section]")?.textContent || ""));
  await closedInstall.release();
  await pause(100);
  assert.match(await h.text(shellSection), /from Oracle: 33%/, "a closed dialog's action reply cannot replace the reopened job");
  await closeDialog();
  const finalStatus = shellCalls("/api/client/shell/status").length;
  await pause(1100);
  assert.equal(shellCalls("/api/client/shell/status").length, finalStatus, "the final dialog leaves no polling running");

  assert.deepEqual(h.errors, []);
  console.log(`Client tools suite passed. Screenshots: ${shots}`);
} finally { await h.close(); }
