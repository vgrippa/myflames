// Real CodeMirror catalog, engine vocabulary, hover, warnings and settings.
// Opt in with MYFLAMES_EDITOR_LAB and MYFLAMES_EDITOR_LAB_PORT. The named
// container must already be running. This suite never manages containers.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import {
  EDITOR, setEditorText, insertEditorText, selectEditor, editorSelection, waitForEditorText,
} from "./editor-helpers.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
const container = process.env.MYFLAMES_EDITOR_LAB;
const port = Number(process.env.MYFLAMES_EDITOR_LAB_PORT);
if (!container || !port) {
  console.log("editor-live: skipped (set MYFLAMES_EDITOR_LAB to a running MySQL or MariaDB container and MYFLAMES_EDITOR_LAB_PORT to its published port)");
  process.exit(0);
}
assert.ok(Number.isInteger(port) && port > 0 && port <= 65535, "a valid published lab port is required");
const client = process.env.MYFLAMES_LAB_CLIENT || "mysql";
assert.ok(["mysql", "mariadb"].includes(client), "MYFLAMES_LAB_CLIENT must be mysql or mariadb");
const containerEnv = execFileSync("docker", ["inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"], { timeout: 15000 }).toString();
const passwordKeys = client === "mariadb" ? ["MARIADB_ROOT_PASSWORD", "MYSQL_ROOT_PASSWORD"] : ["MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD"];
const password = passwordKeys.map(key => containerEnv.split("\n").find(line => line.startsWith(`${key}=`))?.slice(key.length + 1)).find(Boolean);
assert.ok(password, "the container has MYSQL_ROOT_PASSWORD or MARIADB_ROOT_PASSWORD");
// Passwords stay in memory and the inherited MYSQL_PWD environment, never argv.
const sql = text => execFileSync("docker", ["exec", "-i", "-e", "MYSQL_PWD", container, client, "-uroot", "-N", "-B"],
  { input: text, env: { ...process.env, MYSQL_PWD: password }, timeout: 20000 }).toString().trim();
sql("CREATE DATABASE IF NOT EXISTS mf_editor;\n"
  + "CREATE TABLE IF NOT EXISTS mf_editor.users (id INT PRIMARY KEY, email VARCHAR(100) NOT NULL, created_at DATETIME);\n"
  + "CREATE TABLE IF NOT EXISTS mf_editor.orders (id INT PRIMARY KEY, user_id INT, total DECIMAL(10,2), KEY k_user (user_id));\n"
  + "CREATE TABLE IF NOT EXISTS mf_editor.`select` (id INT);\n");
const serverVersion = sql("SELECT VERSION();");
const engine = /mariadb/i.test(serverVersion) ? "mariadb" : "mysql";
assert.ok(Number(sql("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = 'mf_editor';")) <= 200,
  "mf_editor must have at most 200 tables for full-list fuzzy completion");

const home = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-live-")));
const serverEnv = { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault" };
delete serverEnv.MYFLAMES_VAULT_PASSPHRASE;
let proc, serverExited, browser, page, heartbeat;
let stage = "startup";
let labConnectionChosen = false;
const startupRequests = new WeakSet();
const errors = [], consoleErrors = [], httpErrors = [], failedRequests = [], requests = [];
const pathOf = request => new URL(request.url()).pathname;
const pass = (number, text) => console.log(`PASS ${number}: ${text}`);
const TIMEOUT = 15000;
const COMPLETION_DEBOUNCE = 220, HOVER_DWELL = 350, LINT_DELAY = 750;
try {
  proc = spawn(process.env.PYTHON || "python3",
    ["-c", "import sys, myflames.ui as u; s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()", path.join(home, "workspace.sqlite3")],
    { cwd: root, env: serverEnv });
  serverExited = new Promise(resolve => proc.once("exit", resolve));
  // Report progress and process state every 30 seconds during a slow lab run.
  heartbeat = setInterval(() => console.log(`editor-live: ${stage}; WorkspaceServer ${proc.exitCode === null && proc.signalCode === null ? "alive" : "exited"}`), 30000);
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(Error("WorkspaceServer did not publish its launch URL within 15 seconds")), TIMEOUT);
    const fail = error => { clearTimeout(timer); reject(error); };
    proc.once("error", fail);
    proc.once("exit", code => fail(Error(`WorkspaceServer exited during startup (${code})`)));
    proc.stderr.on("data", chunk => {
      output = (output + chunk).slice(-16384);
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
  });
  browser = await puppeteer.launch({ headless: "shell", pipe: true });
  page = await browser.newPage();
  page.setDefaultTimeout(TIMEOUT);
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => { if (["error", "warn"].includes(message.type())) consoleErrors.push(message.text()); });
  page.on("request", request => {
    if (!labConnectionChosen) startupRequests.add(request);
    if (pathOf(request).startsWith("/api/")) requests.push(pathOf(request));
  });
  page.on("requestfailed", request => failedRequests.push({ path: pathOf(request), error: request.failure()?.errorText }));
  page.on("response", response => {
    if (response.status() < 400) return;
    const request = response.request();
    let connectionPort, connectionUser;
    try {
      const connection = JSON.parse(request.postData() || "{}")?.connection;
      connectionPort = connection?.port;
      connectionUser = connection?.user;
    } catch { /* Malformed request bodies remain unexpected HTTP errors. */ }
    // Retain only port/user, never the request body or its password. Classify
    // by request time because a startup response may arrive after selection.
    httpErrors.push({ path: pathOf(request), status: response.status(), connectionPort, connectionUser,
      beforeLabConnection: startupRequests.has(request) });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url);
  await page.waitForSelector('.main-nav button[data-place="query"]');
  // Same cookie/proof bootstrap as editor-explain-live, also assert HTTP status.
  const api = async (pathname, body) => {
    const reply = await page.evaluate(async (p, b) => {
      const headers = { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" };
      const auth = await fetch("/api/auth/session", { method: "POST", headers, body: "{}" });
      if (auth.status !== 200) throw Error(`Session bootstrap returned ${auth.status}`);
      const token = (await auth.json()).token;
      const response = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
      return { status: response.status, data: await response.json() };
    }, pathname, body);
    assert.equal(reply.status, 200, `${pathname} answers HTTP 200`);
    return reply.data;
  };
  const profile = await api("/api/profiles/save", { value: { name: "Editor live lab", host: "127.0.0.1", port, user: "root", database: "mf_editor",
    ssl_mode: "PREFERRED", ssl_ca: "", type: "development" }, password, remember_password: true });
  assert.ok(profile.id, "the real workspace saved the lab profile");
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".query-lab .editor-toolbar", { visible: true });
  // Register before selecting: QueryLab obtains engine words from this report.
  const connectionId = await connectionOption(page, "Editor live lab");
  // End the startup exception before selection starts, including its requests.
  labConnectionChosen = true;
  const [privilegeResponse] = await Promise.all([
    page.waitForResponse(response => pathOf(response.request()) === "/api/live/privileges"
      && JSON.parse(response.request().postData() || "{}").connection?.database === "mf_editor"),
    chooseConnection(page, connectionId),
  ]);
  assert.equal(privilegeResponse.status(), 200, "the connected server's privileges load");
  const identity = await privilegeResponse.json();
  assert.equal(identity.engine, engine, "workspace engine agrees with the real container");
  assert.equal(identity.version, serverVersion, "workspace version agrees with SELECT VERSION()");
  await page.waitForSelector(EDITOR, { visible: true });

  const dismiss = async () => {
    await page.keyboard.press("Escape");
    await page.waitForSelector(".sql-completion", { hidden: true });
  };
  const manualCompletion = async () => {
    await page.keyboard.down("Control");
    try { await page.keyboard.press("Space"); } finally { await page.keyboard.up("Control"); }
  };
  const typeSuffix = async (base, suffix) => {
    await dismiss();
    await setEditorText(page, base);
    await insertEditorText(page, suffix);
    await waitForEditorText(page, base + suffix);
  };
  const option = async label => {
    await page.waitForFunction(name => [...document.querySelectorAll(".sql-completion .sql-completion-option")]
      .some(row => row.querySelector(".sql-completion-label")?.textContent === name), {}, label);
    return page.$$eval(".sql-completion .sql-completion-option", (rows, name) => {
      const row = rows.find(row => row.querySelector(".sql-completion-label")?.textContent === name);
      return { kind: row.querySelector(".sql-completion-kind")?.textContent, detail: row.querySelector(".cm-completionDetail")?.textContent || "" };
    }, label);
  };
  // Observe absence throughout a full debounce/dwell interval, catching flashes.
  const staysAbsent = (selector, duration) => page.evaluate((selector, duration) => new Promise(resolve => {
    let found = !!document.querySelector(selector);
    const observer = new MutationObserver(() => { found ||= !!document.querySelector(selector); });
    observer.observe(document.body, { childList: true, subtree: true, attributes: true });
    setTimeout(() => { observer.disconnect(); resolve(!found); }, duration);
  }), selector, duration);
  const warningText = async () => {
    await dismiss();
    await page.mouse.move(0, 0);
    await page.waitForSelector(".query-lab .cm-lint-marker-warning", { visible: true });
    await page.waitForSelector(".query-lab .cm-lintRange-warning", { visible: true });
    // Hover an actual character inside the underlined diagnostic range,
    // using CodeMirror's range tooltip rather than its separate gutter hover.
    const point = await page.$eval(".query-lab .cm-lintRange-warning", mark => {
      let offset = Math.floor(mark.textContent.length / 2);
      const walker = document.createTreeWalker(mark, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (offset >= node.textContent.length) { offset -= node.textContent.length; continue; }
        const range = document.createRange();
        range.setStart(node, offset); range.setEnd(node, offset + 1);
        const rect = range.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      }
      throw Error("Warning range has no visible text node");
    });
    await page.mouse.move(point.x, point.y);
    const tooltipText = ".cm-tooltip-lint .cm-diagnostic-warning .cm-diagnosticText";
    try {
      await page.waitForSelector(tooltipText, { visible: true, timeout: 2000 });
    } catch (error) {
      if (error.name !== "TimeoutError") throw error;
      // If range hover is unreliable, read the same diagnostic through the
      // user-facing Problems list (Shift-Mod-m), without exhausting cache TTL.
      await page.mouse.move(0, 0);
      await page.$eval(EDITOR, editor => editor.focus());
      const mod = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? "Meta" : "Control");
      await page.keyboard.down(mod);
      await page.keyboard.down("Shift");
      try { await page.keyboard.press("m"); } finally {
        await page.keyboard.up("Shift");
        await page.keyboard.up(mod);
      }
      try {
        const panelText = ".query-lab .cm-panel-lint .cm-diagnostic-warning .cm-diagnosticText";
        await page.waitForSelector(panelText, { visible: true });
        return await page.$$eval(panelText, nodes => nodes.map(node => node.textContent).join("\n"));
      } finally {
        await page.keyboard.press("Escape");
        await page.waitForSelector(".query-lab .cm-panel-lint", { hidden: true });
      }
    }
    return page.$$eval(tooltipText, nodes => nodes.map(node => node.textContent).join("\n"));
  };

  stage = "1 endpoints";
  const settings = await api("/api/settings/editor", {});
  assert.equal(settings.schema_version, "editor-settings-1.0");
  const keys = ["multi_cursor", "completion_auto", "close_brackets", "soft_wrap", "problem_warnings", "hover_info"];
  assert.deepEqual(Object.keys(settings.settings).sort(), [...keys].sort(), "all six persisted editor settings are returned");
  for (const key of keys) assert.equal(typeof settings.settings[key], "boolean", `${key} is a parsed boolean`);
  assert.equal(settings.settings.completion_auto, true);
  assert.equal(settings.settings.problem_warnings, true);
  assert.equal(settings.settings.hover_info, true);
  const names = await api("/api/config/versions/names", { engine: identity.engine, version: identity.version });
  assert.equal(names.schema_version, "param-names-1.0");
  assert.equal(names.engine, engine);
  assert.ok(Array.isArray(names.names) && names.names.length > 0, "the connected release has variable names");
  assert.equal(names.names.find(item => item.name === "innodb_buffer_pool_size")?.scope, "global");
  pass(1, `${engine} ${serverVersion}: settings and variable names return 200 with parsed data`);

  // Cold half of check 6 must precede the first completion catalog read.
  stage = "6 cold-cache warning guard";
  const coldStart = requests.length;
  await setEditorText(page, "SELECT * FROM no_such_table");
  await dismiss();
  assert.ok(await staysAbsent(".query-lab .cm-lint-marker-warning, .cm-diagnostic-warning", LINT_DELAY + HOVER_DWELL),
    "an uncached schema must not produce an unknown-table warning after linting");
  assert.ok(!requests.slice(coldStart).includes("/api/live/catalog"), "linting alone never fetches the catalog");

  stage = "2 table suggestions";
  const [tableResponse] = await Promise.all([
    page.waitForResponse(response => {
      if (pathOf(response.request()) !== "/api/live/catalog") return false;
      const body = JSON.parse(response.request().postData() || "{}");
      return body.schema === "mf_editor" && !body.table && !body.prefix && !body.after;
    }),
    typeSuffix("SELECT * FROM ", "us"),
  ]);
  assert.equal(tableResponse.status(), 200, "the full table inventory answers HTTP 200");
  const fullTables = await tableResponse.json();
  const warmedAt = Date.now();
  assert.equal(fullTables.truncated, false, "unknown-table warnings require a complete inventory");
  for (const name of ["users", "orders", "select"])
    assert.ok(fullTables.tables.some(table => table.name === name), `the real catalog contains ${name}`);
  assert.equal((await option("users")).kind, "table");
  assert.match((await option("users")).detail, /mf_editor/);
  await typeSuffix("SELECT * FROM ", "usr");
  assert.equal((await option("users")).kind, "table", "a full small-schema list supports fuzzy usr -> users");
  await typeSuffix("SELECT * FROM ", "sel");
  await option("select");
  const reserved = await page.$$(".sql-completion .sql-completion-option");
  for (const row of reserved) {
    if (await row.$eval(".sql-completion-label", node => node.textContent === "select")) { await row.click(); break; }
  }
  await waitForEditorText(page, "SELECT * FROM `select`");
  pass(2, "real users prefix/fuzzy suggestions and reserved `select` insertion");

  // Inspect the warm half immediately so the 30-second cache cannot expire
  // during the independent column, vocabulary and hover checks.
  stage = "6 warm-cache warning";
  await dismiss();
  await setEditorText(page, "SELECT * FROM no_such_table");
  const unknownWarning = await warningText();
  assert.match(unknownWarning, /No table `no_such_table` in `mf_editor`/);
  assert.ok(Date.now() - warmedAt < 30000, "unknown-table warning used the still-fresh full table list");

  stage = "3 column suggestions";
  await page.mouse.move(0, 0);
  await setEditorText(page, "SELECT u. FROM users u");
  await selectEditor(page, "SELECT u.".length);
  assert.deepEqual(await editorSelection(page), { start: 9, end: 9 }, "cursor is immediately after u.");
  await manualCompletion();
  const email = await option("email");
  assert.equal(email.kind, "column");
  assert.match(email.detail, /mf_editor\.users/);
  assert.match(email.detail, /varchar\(100\)/i);
  pass(3, "alias-qualified email suggestion includes mf_editor.users and varchar(100)");

  stage = "4 engine vocabulary";
  await typeSuffix("SELECT ", "CONCA");
  const concat = await option("CONCAT");
  assert.equal(concat.kind, "function");
  assert.match(concat.detail, /argument/i);
  await typeSuffix("SELECT ", "@@innodb_buffer_p");
  const bufferPool = await option("innodb_buffer_pool_size");
  assert.match(bufferPool.detail, /\bglobal\b/);
  pass(4, "CONCAT is a function and @@innodb_buffer_pool_size shows global scope");

  stage = "5 hover";
  const hoverToken = async (text, token) => {
    await dismiss();
    await page.mouse.move(0, 0);
    await setEditorText(page, text);
    await dismiss();
    // Locate the visible character through DOM text nodes, without accessing
    // CodeMirror internals or writing textarea-shaped editor properties.
    const point = await page.$eval(`${EDITOR} .cm-line`, (line, token) => {
      const index = line.textContent.indexOf(token);
      if (index < 0) throw Error(`Hover token ${token} is not rendered`);
      let offset = index + Math.floor(token.length / 2);
      const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        if (offset >= node.textContent.length) { offset -= node.textContent.length; continue; }
        const range = document.createRange();
        range.setStart(node, offset); range.setEnd(node, offset + 1);
        const rect = range.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      }
      throw Error("Hover token has no visible text node");
    }, token);
    await page.mouse.move(point.x, point.y);
    await page.waitForFunction(() => {
      const hover = document.querySelector(".sql-hover");
      return hover && hover.textContent !== "Reading…" && !!hover.querySelector(".sql-hover-heading");
    });
  };
  await hoverToken("SELECT * FROM users", "users");
  assert.equal(await page.$eval(".sql-hover-heading", node => node.textContent), "mf_editor.users");
  const columns = await page.$$eval(".sql-hover .sql-hover-row", rows => rows.map(row => ({
    name: row.querySelector(".sql-hover-name")?.textContent, note: row.querySelector(".sql-hover-note")?.textContent,
  })));
  assert.ok(columns.some(row => row.name === "email"), "table hover names email");
  assert.ok(columns.some(row => row.name === "id" && /\bPRI\b/.test(row.note)), "id carries the primary-key marker PRI");
  await hoverToken("SELECT email FROM users", "email");
  assert.equal(await page.$eval(".sql-hover-heading", node => node.textContent), "mf_editor.users.email");
  assert.match(await page.$eval(".sql-hover-type", node => node.textContent), /varchar\(100\)/i);
  pass(5, "table hover lists email/id with PRI; column hover shows varchar(100)");

  stage = "6 missing WHERE warning";
  await page.mouse.move(0, 0);
  await dismiss();
  await setEditorText(page, "UPDATE users SET email = 'x'");
  assert.match(await warningText(), /UPDATE without WHERE: it changes every row/);
  const executions = () => requests.filter(p => ["/api/editor/run", "/api/editor/open", "/api/live/start"].includes(p));
  assert.deepEqual(executions(), [], "catalog, hover and warnings never run editor SQL or open a session");
  pass(6, "unknown table warned only after a full cached list; UPDATE without WHERE warned without execution");

  stage = "7 settings";
  const autoCheckbox = '#settings-popover input[aria-describedby="editor-setting-completion_auto-hint"]';
  const toggleAuto = async on => {
    await page.mouse.move(0, 0);
    await page.click('button[aria-label="Settings"]');
    await page.waitForSelector(autoCheckbox, { visible: true });
    assert.equal(await page.$eval(autoCheckbox, input => input.checked), !on, "the switch begins in the expected state");
    const saved = page.waitForResponse(response => pathOf(response.request()) === "/api/settings/editor"
      && JSON.parse(response.request().postData() || "{}").set?.completion_auto === on);
    await page.click(`#settings-popover label:has(input[aria-describedby="editor-setting-completion_auto-hint"])`);
    const response = await saved;
    assert.equal(response.status(), 200, "the settings popover saved through the real endpoint");
    assert.equal((await response.json()).settings.completion_auto, on);
    await page.waitForFunction((selector, on) => {
      const input = document.querySelector(selector);
      return input?.checked === on && input.getAttribute("aria-disabled") !== "true";
    }, {}, autoCheckbox, on);
    await page.keyboard.press("Escape");
    await page.waitForSelector("#settings-popover", { hidden: true });
  };
  await toggleAuto(false);
  await typeSuffix("SELECT * FROM ", "us");
  assert.ok(await staysAbsent(".sql-completion", COMPLETION_DEBOUNCE + LINT_DELAY),
    "Suggestions while typing off prevents even a transient automatic popup");
  await manualCompletion();
  assert.equal((await option("users")).kind, "table", "Ctrl+Space still reads suggestions when automatic completion is off");
  await dismiss();
  await toggleAuto(true);
  await typeSuffix("SELECT * FROM ", "us");
  assert.equal((await option("users")).kind, "table", "turning the switch back on restores automatic suggestions");
  assert.equal((await api("/api/settings/editor", {})).settings.completion_auto, true);
  await dismiss();
  assert.deepEqual(executions(), [], "the settings check also never executes editor SQL");
  pass(7, "typing suggestions off suppresses popup; Ctrl+Space works; restored on and persisted");

  stage = "8 page errors";
  assert.deepEqual(errors, [], "no page errors");
  // The default Local MySQL connection is read before the lab is selected.
  // Ignore only its known startup 400s, never lab or later failures.
  const isIgnoredStartup400 = item => item.status === 400 && item.beforeLabConnection
    && ["/api/live/privileges", "/api/live/schemas"].includes(item.path)
    && item.connectionPort === 3306 && item.connectionUser === "";
  const ignoredStartup400Count = httpErrors.filter(isIgnoredStartup400).length;
  const chromiumBadRequest = "Failed to load resource: the server responded with a status of 400 (Bad Request)";
  let ignoredConsole400Count = 0;
  const unexpectedConsoleErrors = consoleErrors.filter(message => {
    if (message === chromiumBadRequest && ignoredConsole400Count < ignoredStartup400Count) {
      ignoredConsole400Count += 1;
      return false;
    }
    return true;
  });
  assert.deepEqual(unexpectedConsoleErrors, [], "no browser console warnings or errors beyond ignored startup 400s");
  const unexpectedHttpErrors = httpErrors.filter(item => !isIgnoredStartup400(item));
  assert.deepEqual(unexpectedHttpErrors, [], "no unexpected HTTP errors");
  // Superseding a completion or switching connection intentionally aborts
  // these read-only requests. All other failed requests are unexpected.
  const allowedAborts = new Set(["/api/live/schemas", "/api/live/catalog", "/api/live/privileges", "/api/editor/preview", "/api/config/versions/names"]);
  assert.deepEqual(failedRequests.filter(item => !(item.error === "net::ERR_ABORTED" && allowedAborts.has(item.path))), [], "no unexpected failed requests");
  pass(8, "no page errors or unexpected browser/network diagnostics");

  // Capture only after every existing assertion passes. With EDITOR_SHOTS
  // unset, the live checks and their output take exactly the original path.
  if (process.env.EDITOR_SHOTS) {
    const out = path.resolve(process.env.EDITOR_SHOTS);
    await fs.mkdir(out, { recursive: true });
    const written = [];
    const mod = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent) ? "Meta" : "Control");
    const chord = async (modifiers, key) => {
      for (const modifier of modifiers) await page.keyboard.down(modifier);
      try { await page.keyboard.press(key); } finally {
        for (const modifier of [...modifiers].reverse()) await page.keyboard.up(modifier);
      }
    };
    const closeShotUI = async () => {
      await page.mouse.move(0, 0);
      if (await page.$("#settings-popover")) {
        await page.keyboard.press("Escape");
        await page.waitForSelector("#settings-popover", { hidden: true });
      }
      if (await page.$(".query-lab .sql-find-close")) {
        await page.click(".query-lab .sql-find-close");
        await page.waitForSelector(".query-lab .sql-find", { hidden: true });
      }
      // Collapse extra selections before Escape, which otherwise only removes
      // those selections and leaves Problems open on its first press.
      await selectEditor(page, 0);
      await dismiss();
      await page.keyboard.press("Escape");
      await page.waitForSelector(".query-lab .cm-panel-lint, .sql-hover", { hidden: true });
    };
    const scheme = async value => {
      await closeShotUI();
      // Match ui-review's myflames.theme/data-theme and media emulation.
      // Use Appearance too, so the Settings buttons reflect the same scheme.
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value }]);
      await page.click('button[aria-label="Settings"]');
      const themeButton = `#settings-popover .theme-segmented button:nth-child(${value === "light" ? 2 : 3})`;
      await page.waitForSelector(themeButton, { visible: true });
      await page.click(themeButton);
      await page.waitForFunction((value, selector) => document.documentElement.dataset.theme === value
        && localStorage.getItem("myflames.theme") === value
        && document.querySelector(selector)?.getAttribute("aria-pressed") === "true", {}, value, themeButton);
      await page.keyboard.press("Escape");
      await page.waitForSelector("#settings-popover", { hidden: true });
    };
    const completionDetail = async (label, detail) => {
      await option(label);
      await page.waitForFunction((label, detail) => {
        const row = [...document.querySelectorAll(".sql-completion-option")]
          .find(row => row.querySelector(".sql-completion-label")?.textContent === label);
        const note = row?.querySelector(".cm-completionDetail");
        const rect = note?.getBoundingClientRect();
        return note?.textContent.includes(detail) && rect.width > 0 && rect.height > 0
          && rect.top >= 0 && rect.bottom <= innerHeight;
      }, {}, label, detail);
    };
    const unknownTable = async () => {
      // Re-warm through real completion, since the full inventory's 30 s TTL
      // may expire while capturing the other states, widths and schemes.
      await typeSuffix("SELECT * FROM ", "us");
      await completionDetail("users", "mf_editor");
      await dismiss();
      await setEditorText(page, "SELECT * FROM no_such_table");
      await dismiss();
      await page.waitForSelector(".query-lab .cm-lintRange-warning", { visible: true });
    };
    const repeated = "SELECT email FROM users;\nSELECT email FROM users;\nSELECT email FROM users;";
    const states = [
      ["suggestions", async () => {
        await typeSuffix("SELECT * FROM ", "us");
        await completionDetail("users", "mf_editor");
      }],
      ["columns", async () => {
        await setEditorText(page, "SELECT u. FROM users u");
        await selectEditor(page, "SELECT u.".length);
        await manualCompletion();
        await completionDetail("email", "mf_editor.users");
      }],
      ["function", async () => {
        await typeSuffix("SELECT ", "CONCA");
        await completionDetail("CONCAT", "at least 1 argument");
      }],
      ["argument-hint", async () => {
        await setEditorText(page, "SELECT DATE_FORMAT(created_at, ) FROM users;");
        await selectEditor(page, "SELECT DATE_FORMAT(created_at, ".length);
        await page.waitForSelector(".sql-arg-hint", { visible: true });
        await page.waitForFunction(() => {
          const text = document.querySelector(".sql-arg-hint")?.textContent || "";
          return text.includes("DATE_FORMAT") && text.includes("argument 2");
        });
      }],
      ["hover-table", async () => {
        await hoverToken("SELECT * FROM users", "users");
        await page.waitForSelector(".sql-hover", { visible: true });
        await page.waitForFunction(() => document.querySelector(".sql-hover-heading")?.textContent === "mf_editor.users"
          && [...document.querySelectorAll(".sql-hover-name")].some(node => node.textContent === "email"));
      }],
      ["hover-column", async () => {
        await hoverToken("SELECT email FROM users", "email");
        await page.waitForSelector(".sql-hover", { visible: true });
        await page.waitForFunction(() => document.querySelector(".sql-hover-heading")?.textContent === "mf_editor.users.email"
          && /varchar\(100\)/i.test(document.querySelector(".sql-hover-type")?.textContent || ""));
      }],
      ["warning", async () => {
        await unknownTable();
        // Hover a character inside the underline, as in warningText, but do
        // not fall back to Problems: this image must show the actual tooltip.
        const point = await page.$eval(".query-lab .cm-lintRange-warning", mark => {
          let offset = Math.floor(mark.textContent.length / 2);
          const walker = document.createTreeWalker(mark, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            if (offset >= node.textContent.length) { offset -= node.textContent.length; continue; }
            const range = document.createRange();
            range.setStart(node, offset); range.setEnd(node, offset + 1);
            const rect = range.getBoundingClientRect();
            return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
          }
          throw Error("Warning range has no visible text node");
        });
        await page.mouse.move(point.x, point.y);
        const selector = ".cm-tooltip-lint .cm-diagnostic-warning .cm-diagnosticText";
        await page.waitForSelector(selector, { visible: true });
        await page.waitForFunction(selector => document.querySelector(selector)?.textContent
          .includes("No table `no_such_table` in `mf_editor`"), {}, selector);
      }],
      ["problems", async () => {
        await unknownTable();
        await page.$eval(EDITOR, editor => editor.focus());
        await chord([mod, "Shift"], "KeyM");
        const selector = ".query-lab .cm-panel-lint .cm-diagnostic-warning .cm-diagnosticText";
        await page.waitForSelector(selector, { visible: true });
        await page.waitForFunction(selector => document.querySelector(selector)?.textContent
          .includes("No table `no_such_table` in `mf_editor`"), {}, selector);
      }],
      ["find", async () => {
        await setEditorText(page, repeated);
        await selectEditor(page, 0);
        await chord([mod], "KeyF");
        const field = '.sql-find input[aria-label="Find"]';
        await page.waitForSelector(field, { visible: true });
        await page.$eval(field, input => { input.focus(); input.select(); });
        await page.keyboard.type("email");
        await page.waitForFunction(() => document.querySelector(".sql-find-count")?.textContent === "1 of 3"
          && document.querySelectorAll(".sql-find-mark").length === 3
          && document.querySelector(".sql-find-current")?.textContent === "email");
      }],
      ["goto", async () => {
        await setEditorText(page, repeated);
        await selectEditor(page, 0);
        await chord(["Control"], "KeyG");
        const field = '.sql-find[aria-label="Go to line"] input[type="number"]';
        await page.waitForSelector(field, { visible: true });
        await page.waitForFunction(selector => document.activeElement === document.querySelector(selector)
          && document.querySelector(".sql-find-line-hint")?.textContent === "1 to 3", {}, field);
      }],
      ["settings", async () => {
        await setEditorText(page, "SELECT email FROM users;");
        await page.click('button[aria-label="Settings"]');
        await page.waitForSelector("#settings-popover .settings-editor-group", { visible: true });
        await page.$eval("#settings-popover", popover => {
          const group = popover.querySelector(".settings-editor-group");
          popover.scrollTop += group.getBoundingClientRect().top - popover.getBoundingClientRect().top - 8;
        });
        await page.waitForFunction(() => {
          const popover = document.querySelector("#settings-popover").getBoundingClientRect();
          const group = document.querySelector(".settings-editor-group").getBoundingClientRect();
          return group.top >= popover.top && group.bottom <= popover.bottom
            && group.top >= 0 && group.bottom <= innerHeight;
        });
      }],
      ["multi-cursor", async () => {
        await setEditorText(page, repeated);
        await selectEditor(page, "SELECT ".length, "SELECT email".length);
        await chord([mod], "KeyD");
        await chord([mod], "KeyD");
        await page.waitForFunction(() => document.querySelectorAll(".query-lab .cm-cursor").length === 3
          && document.querySelectorAll(".query-lab .cm-cursor-secondary").length === 2);
      }],
    ];
    // Keep the three real cursors visible instead of sampling the off phase
    // of CodeMirror's blink animation. Only the optional screenshots use this.
    const cursorStyle = await page.addStyleTag({ content: ".query-lab .cm-cursorLayer { animation: none !important; }" });
    try {
      for (const appearance of ["light", "dark"]) {
        await scheme(appearance);
        for (const [width, height] of [[1440, 900], [720, 520]]) {
          await page.setViewport({ width, height });
          for (const [state, prepare] of states) {
            stage = `screenshots ${state} ${width} ${appearance}`;
            const consoleStart = consoleErrors.length, errorStart = errors.length;
            const diagnostics = () => [...consoleErrors.slice(consoleStart), ...errors.slice(errorStart)];
            try {
              // Re-open every state after resizing; tooltips cannot be reused.
              await closeShotUI();
              await prepare();
              const file = `${state}-${width}${appearance === "dark" ? "-dark" : ""}.png`;
              await page.waitForFunction(() => document.getAnimations()
                .every(animation => animation.playState !== "running"), { timeout: 2000 });
              await page.screenshot({ path: path.join(out, file) });
              written.push({ file, state, width, scheme: appearance, console_errors: diagnostics() });
            } catch (error) {
              written.push({ state, width, scheme: appearance, error: String(error), console_errors: diagnostics() });
              throw error;
            }
          }
        }
      }
    } finally {
      try { await scheme("light"); } finally {
        await fs.writeFile(path.join(out, "index.json"), JSON.stringify(written, null, 2) + "\n");
        await cursorStyle.dispose();
      }
    }
    console.log(`editor-live: wrote ${written.length} screenshots and index.json to ${out}`);
  }

} catch (error) {
  // Do not dump request bodies, profile JSON, launch URLs or container env.
  const dom = page && !page.isClosed() ? await page.evaluate(() => ({
    options: [...document.querySelectorAll(".sql-completion-option")].map(node => node.textContent),
    hints: [...document.querySelectorAll(".sql-completion-hint")].map(node => node.textContent),
    warnings: [...document.querySelectorAll(".cm-diagnosticText")].map(node => node.textContent),
  })).catch(() => null) : null;
  console.error("editor-live failed", { stage, dom, errors, consoleErrors, httpErrors, failedRequests, recentPaths: requests.slice(-15) });
  throw error;
} finally {
  clearInterval(heartbeat);
  try { await browser?.close(); } finally {
    try {
      if (proc?.pid && proc.exitCode === null && proc.signalCode === null) {
        const stopped = serverExited;
        proc.kill("SIGTERM");
        const killTimer = setTimeout(() => proc.kill("SIGKILL"), 5000);
        try { await stopped; } finally { clearTimeout(killTimer); }
      }
    } finally { await fs.rm(home, { recursive: true, force: true }); }
  }
}
