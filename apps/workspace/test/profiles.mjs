import { connectionAction, connectionRow } from "./connection-helpers.mjs";
// Remembered connection passwords, managed in Home's Connections section,
// and each connection's write safety: Type (Production by default,
// Development) and Read-only, the strip and the picker's type dot; an
// unsaved connection stays Production; a saved Development connection moved
// to another endpoint is Production until saved as Development again.
// Mocked profile/connection API, real server for the page.
// Run after `npm run build`: node test/profiles.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-profiles-"));
// The page never reaches the real profile endpoints (they are mocked below),
// but isolate HOME and force the vault anyway so no OS keychain is touched.
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

const store = { backend: "macos-keychain", label: "macOS Keychain", available: true };
let items = [];
let created = 0;
// The saved connections cannot be read (a locked database, a stopped server).
let listFails = false;
const sessionsReport = JSON.parse(await fs.readFile(new URL("./fixtures/sessions-mysql-8.4-active.json", import.meta.url), "utf8"));
// Kill is bound to the server a report was read from; the fixture predates that.
sessionsReport.server_identity = { server_uuid: "profiles-server-uuid" };
const seen = {};
const handlers = {
  "/api/profiles/list": () => (listFails ? { __status: 500, error: "database is locked" } : { items, secret_store: store }),
  "/api/live/sessions": () => sessionsReport,
  // Profiles are synthetic in this suite; the real review-token protocol is
  // covered by review-auth.mjs against an isolated workspace.
  "/api/live/kill/prepare": body => ({ review_token: "profile-review-token",
    connection_id: body.connection_id, mode: body.mode, expect: body.expect,
    statements: [`KILL ${body.mode === "query" ? "QUERY" : "CONNECTION"} ${body.connection_id}`] }),
  "/api/live/kill": () => ({ schema_version: "kill-1.0", status: "killed", connection_id: 93, mode: "connection",
    statement: "KILL CONNECTION 93", verified: [], after: { present: false }, message: "Ended connection 93." }),
  "/api/profiles/save": (body) => {
    const saved = {
      ...body.value,
      id: body.id || "p" + ++created,
      updated_at: "2026-10-12T12:00:00+00:00",
      remember_password: body.remember_password,
      has_saved_password: body.remember_password,
      secret_backend: body.remember_password ? store.backend : "",
      secret_backend_label: body.remember_password ? store.label : "",
    };
    items = items.some((p) => p.id === saved.id)
      ? items.map((p) => (p.id === saved.id ? saved : p))
      : [...items, saved];
    return saved;
  },
  "/api/profiles/forget-password": (body) => {
    items = items.map((p) =>
      p.id === body.id
        ? { ...p, remember_password: false, has_saved_password: false, secret_backend: "", secret_backend_label: "" }
        : p,
    );
    return items.find((p) => p.id === body.id);
  },
  "/api/profiles/delete": (body) => {
    items = items.filter((p) => p.id !== body.id);
    return { deleted: true };
  },
  "/api/connection/test": () => ({ version: "8.4.3", schemas: ["shop"] }),
  "/api/live/top": () => ({
    schema_version: "top-1.0", engine: "mysql", server_version: "8.4.3",
    order: "total", notes: [], statements: [],
  }),
};

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
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    const handler = handlers[pathname];
    if (!handler) return request.continue();
    const body = JSON.parse(request.postData() || "{}");
    (seen[pathname] = seen[pathname] || []).push(body);
    const answer = handler(body);
    const status = answer.__status || 200;
    delete answer.__status;
    request.respond({ status, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const button = (text, scope = "") => page.locator(`${scope}button ::-p-text(${text})`).click();
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const last = (pathname) => seen[pathname].at(-1);
  const password = ".connection-panel input[type=password]";
  const notice = (needle) =>
    page.waitForFunction(
      (n) => document.querySelector(".connections .notice")?.textContent.includes(n),
      {},
      needle,
    );
  const place = (label) => button(label, ".main-nav ");
  const editConnection = (name) => connectionAction(page, name, "Edit");
  // A row's type from its dot, which stays beside the name while the form
  // is open and the other columns are hidden.
  const rowType = (row) => page.$eval(`${row} .type-dot`, (e) => e.getAttribute("aria-label"));
  const setField = async (label, value) => {
    const input = await page.evaluateHandle(
      (l) =>
        [...document.querySelectorAll(".connection-panel label.field")]
          .find((e) => e.textContent.trim().startsWith(l))
          .querySelector("input"),
      label,
    );
    await input.click({ clickCount: 3 });
    await page.keyboard.press("Backspace");
    await input.type(value);
  };
  const setName = (value) => setField("Connection name", value);

  await place("Home");
  // Home embeds the existing connection manager beside recent work.
  assert.ok(await page.$('.home-launcher > .connections'));
  assert.ok(await page.$('.home [aria-labelledby="recent-title"]'));
  assert.ok(await page.$('.topbar button[aria-label="Import plan"]'));
  assert.equal(await page.$('.topbar button[aria-label="New query tab"]'), null);
  await button("Add connection");
  await page.waitForFunction(() =>
    document.querySelector(".connection-panel > .hint:last-child")?.textContent.includes("Save connection asks"),
  );
  assert.equal(await page.$(".remember-field"), null);
  assert.match(await text(".connection-panel > .hint:last-child"), /never in the workspace database/);

  // Save connection with a typed password asks; "Save with password" stores it separately.
  await setName("Prod");
  await page.locator(password).fill("pw-1");
  await button("Save connection");
  await page.waitForSelector(".save-prompt");
  assert.match(await text("#save-prompt-title"), /Also save the password\?/);
  assert.match(await text("#save-prompt-title"), /saved in macOS Keychain/);
  await button("Save with password", ".save-prompt ");
  await notice("Password saved in macOS Keychain");
  assert.equal(await page.$(".save-prompt"), null);
  const saved = last("/api/profiles/save");
  assert.equal(saved.password, "pw-1");
  assert.equal(saved.remember_password, true);
  assert.equal("password" in saved.value, false);
  // The list shows where the password is kept, and the first saved
  // connection becomes the one in use.
  // The form stays open after saving, with compact columns; close it to see the row.
  assert.match(await text(".connection-row.is-active"), /Prod/);
  await page.click(".connections .inspector-close");
  await page.waitForSelector(".connection-row.is-active [data-col=password] span");
  assert.equal(await text(".connection-row.is-active [data-col=password]"), "Saved");
  assert.equal(await page.$eval(".connection-row.is-active [data-col=password] span", el => el.title), "Password saved in macOS Keychain");

  // Editing the connection again shows the saved-password state without the secret.
  await editConnection("Prod");
  await page.waitForSelector("#saved-password-hint");
  assert.equal(await page.$eval(password, (e) => e.value), "");
  assert.equal(await page.$eval(password, (e) => e.placeholder), "Saved password");
  assert.equal(await page.$eval(password, (e) => e.getAttribute("aria-describedby")), "saved-password-hint");
  assert.match(await text("#saved-password-hint"), /saved in macOS Keychain/);

  // Saving settings without typing keeps the saved password (no prompt).
  const saves = seen["/api/profiles/save"].length;
  await button("Save connection");
  await notice("Password saved in macOS Keychain");
  assert.equal(seen["/api/profiles/save"].length, saves + 1);
  assert.equal(last("/api/profiles/save").remember_password, true);
  assert.equal(last("/api/profiles/save").password, undefined);

  // Connect and discover without typing: the request names the profile, not a password.
  await button("Test connection");
  await notice("Connected");
  assert.equal(last("/api/connection/test").connection.profile_id, "p1");
  assert.equal(last("/api/connection/test").connection.password, "");
  assert.equal(await page.$(".save-prompt"), null);
  // Server reads with the connection in use, by id, without a password.
  await place("Server");
  await page.click("#server-nav-top");
  await button("Load top statements");
  await page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  assert.equal(last("/api/live/top").connection.profile_id, "p1");
  assert.equal(last("/api/live/top").connection.password, "");
  await place("Home");
  await editConnection("Prod");
  await page.waitForSelector("#saved-password-hint");

  // A typed password overrides the saved one for this session.
  await page.locator(password).fill("typed");
  await page.waitForFunction(() =>
    document.querySelector("#saved-password-hint")?.textContent.includes("typed password"),
  );
  assert.equal(await page.$eval(password, (e) => e.placeholder), "");
  const tests = seen["/api/connection/test"].length;
  await button("Test connection");
  while (seen["/api/connection/test"].length === tests) await new Promise((r) => setTimeout(r, 25));
  assert.equal(last("/api/connection/test").connection.password, "typed");
  await page.click(password, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.waitForFunction(
    (selector) => document.querySelector(selector).placeholder === "Saved password",
    {},
    password,
  );

  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  const fields = await page.$$eval(".connection-panel > .connection-fields input", (els) => els.map((e) => {
    const r = e.getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, width: r.width };
  }));
  assert.ok(fields.length >= 5);
  assert.ok(fields.every((r) => r.width >= 240), "phone inputs use the available width");
  assert.ok(fields.every((r, i) => i === 0 || r.top >= fields[i - 1].bottom), "Port, User and Password stack");
  await (await page.$(".connection-panel")).screenshot({ path: path.join(directory, "profiles-mobile.png") });
  await page.setViewport({ width: 1440, height: 1000 });

  // Forget the saved password; the profile stays and says no password is saved.
  await button("Forget saved password");
  await notice("Saved password forgotten");
  assert.equal(last("/api/profiles/forget-password").id, "p1");
  assert.equal(await page.$("#saved-password-hint"), null);
  assert.match(await text("#no-saved-password-hint"), /No password is saved/);
  assert.equal(await page.$eval(password, (e) => e.placeholder), "");

  // A successful test with a typed password offers to save it.
  await page.locator(password).fill("pw-2");
  await button("Test connection");
  await page.waitForFunction(() => document.querySelector("#offer-save-title"));
  assert.match(await text("#offer-save-title"), /Save this password for “Prod”\?/);
  await button("Not now", ".save-prompt ");
  assert.equal(await page.$("#offer-save-title"), null);
  await button("Test connection");
  await page.waitForFunction(() => document.querySelector("#offer-save-title"));
  await button("Save password", ".save-prompt ");
  await notice("Password saved in macOS Keychain");
  assert.equal(last("/api/profiles/save").password, "pw-2");
  assert.equal(last("/api/profiles/save").id, "p1");
  await (await page.$(".connection-panel")).screenshot({ path: path.join(directory, "profiles.png") });

  // "Settings only" saves a new connection without its password.
  await button("Add connection");
  await setName("Staging");
  await page.locator(password).fill("pw-3");
  await button("Save connection");
  await page.waitForSelector(".save-prompt");
  await button("Settings only", ".save-prompt ");
  await notice("saved without a password");
  assert.equal(last("/api/profiles/save").remember_password, false);
  assert.equal(last("/api/profiles/save").password, undefined);
  assert.equal(last("/api/profiles/save").id, undefined);

  // A new connection with an existing name asks to replace it; Escape cancels.
  await button("Add connection");
  await setName("prod");
  await button("Save connection");
  await page.waitForSelector(".save-prompt");
  assert.match(await text("#save-prompt-title"), /“Prod” already exists/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".save-prompt"));
  await button("Save connection");
  await page.waitForSelector(".save-prompt");
  await button("Replace", ".save-prompt ");
  await notice("saved");
  assert.equal(last("/api/profiles/save").id, "p1");
  assert.equal(last("/api/profiles/save").remember_password, true); // keeps p1's saved password

  // Delete removes the connection (and, server side, its saved password).
  await button("Delete connection");
  await button("Delete connection", "dialog.confirm-sheet ");
  await notice("removed");
  assert.equal(last("/api/profiles/delete").id, "p1");

  // ---- Connection type and read-only -----------------------------------------
  const typeControl = ".connection-panel .type-control";
  const pressed = () => page.$eval(`${typeControl} button[aria-pressed=true]`, (e) => e.textContent);
  const readOnly = ".connection-panel .read-only-field input";
  const strip = async () => {
    await place("Query editor");
    await page.waitForSelector(".window-toolbar .connection-picker");
    const result = await page.evaluate(() => ({
      strip: document.querySelector(".safety-strip")?.textContent || "",
      kind: document.querySelector(".safety-strip")?.dataset.safety || "",
      dot: document.querySelector(".window-toolbar .connection-picker .type-dot")?.getAttribute("aria-label") || "",
    }));
    await place("Home");
    return result;
  };
  // A new connection is Production and read-write; the ⓘ says what each type confirms.
  await button("Add connection");
  await setName("Dev box");
  await setField("Host", "dev.local");
  assert.equal(await pressed(), "Production");
  assert.equal(await page.$eval(readOnly, (e) => e.checked), false);
  await page.click('.connection-panel button[aria-label="About connection types"]');
  const info = await text('[role=dialog][aria-label="About connection types"]');
  assert.match(info, /Production asks before every change myflames sends\./);
  assert.match(info, /Development asks only before risky ones: DDL, KILL, SET GLOBAL, and UPDATE or DELETE without a key\./);
  assert.match(info, /Read-only refuses every change, whatever the type\. A connection used without saving is Production\./);
  await page.keyboard.press("Escape");
  await page.click(`${typeControl} button[data-value=development]`);
  assert.equal(await pressed(), "Development");
  assert.match(await text(".connection-panel .safety-hint"), /^Development applies once saved\. Used without saving, a connection is Production\.$/);
  // The choice holds for the host, port, user and TLS settings it was made
  // with: another host shows Production until Development is chosen again.
  await setField("Host", "dev1.local");
  assert.equal(await pressed(), "Production");
  assert.match(
    await text(".connection-panel .safety-hint"),
    /^Development covers dev\.local:3306 \(TLS PREFERRED\) only\. Other host, port, user or TLS settings are Production until you choose Development again\.$/,
  );
  await setField("Host", "dev.local");
  assert.equal(await pressed(), "Development", "back on the endpoint it was chosen for");
  await button("Save connection");
  await notice("Dev box");
  assert.equal(last("/api/profiles/save").value.type, "development");
  assert.equal(last("/api/profiles/save").value.read_only, false);
  const devRow = connectionRow("Dev box");
  // Columns are compact while the form is open: close it to read the Type cells.
  await page.click(".connections .inspector-close");
  await page.waitForSelector(`${devRow} [data-col=type]`);
  assert.equal(await text(`${devRow} [data-col=type]`), "Development");
  assert.equal(await text(connectionRow("Staging") + " [data-col=type]"), "Production");
  // The first saved connection is in use: Development, so no strip, a green dot.
  assert.match(await text(".connection-row.is-active"), /Dev box/);
  assert.deepEqual(await strip(), { strip: "", kind: "", dot: "Development" });

  // Another host for a saved Development connection: warned, shown and
  // saved as Production unless Development is chosen again; used without
  // saving, it is Production.
  await editConnection("Dev box");
  await page.waitForSelector(typeControl);
  assert.equal(await pressed(), "Development");
  assert.equal(await page.$(".connection-panel .safety-hint"), null);
  // Choosing Development again before changing the host does not carry over
  // to the new host (review gate: the choice is bound to its endpoint).
  await page.click(`${typeControl} button[data-value=development]`);
  await setField("Port", "3310");
  assert.equal(await pressed(), "Production");
  await setField("Port", "3306");
  assert.equal(await pressed(), "Development");
  await setField("Host", "dev2.local");
  assert.match(
    await text(".connection-panel .safety-hint"),
    /^Development covers dev\.local:3306 \(TLS PREFERRED\) only\. Other host, port, user or TLS settings are Production until you choose Development again\.$/,
  );
  assert.equal(await pressed(), "Production");
  await button("Use", ".connection-panel .button-row ");
  assert.deepEqual(await strip(), { strip: "Productionevery change on Dev box asks first", kind: "production", dot: "Production" });
  // The strip shows only in the Query editor and Server (strip() returns to Home).
  await place("Query editor");
  assert.deepEqual(await page.$eval(".safety-strip", (e) => {
    const css = getComputedStyle(e);
    return { background: css.backgroundColor, color: css.color, border: css.borderBottomStyle, bold: Number(getComputedStyle(e.querySelector("strong")).fontWeight) >= 600 };
  }), {
    // The soft danger treatment, whatever the token values are.
    ...(await page.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.background = "var(--danger-soft)";
      probe.style.color = "var(--danger)";
      document.body.append(probe);
      const css = getComputedStyle(probe);
      const out = { background: css.backgroundColor, color: css.color };
      probe.remove();
      return out;
    })),
    border: "solid", bold: true,
  });
  await place("Home");
  await editConnection("Dev box");
  await setField("Host", "dev2.local");
  await button("Save connection");
  await notice("saved");
  assert.equal(last("/api/profiles/save").value.type, "production");
  assert.equal(last("/api/profiles/save").value.host, "dev2.local");
  assert.equal(await rowType(devRow), "Production");
  assert.equal(await pressed(), "Production", "the form shows what was saved");
  assert.equal(await page.$(".connection-panel .safety-hint"), null);
  await page.click(`${typeControl} button[data-value=development]`);
  await button("Save connection");
  await page.waitForFunction((r) => document.querySelector(r + " .type-dot")?.getAttribute("aria-label") === "Development", {}, devRow);
  assert.equal(last("/api/profiles/save").value.type, "development");
  assert.deepEqual(await strip(), { strip: "", kind: "", dot: "Development" });

  // Read-only: saved with the connection, shown in its row, a neutral strip.
  await editConnection("Dev box");
  await page.click(readOnly);
  await button("Save connection");
  await page.waitForFunction((r) => /read-only/.test(document.querySelector(r + " .type-dot")?.getAttribute("aria-label") || ""), {}, devRow);
  assert.equal(last("/api/profiles/save").value.read_only, true);
  assert.equal(last("/api/profiles/save").value.type, "development");
  assert.equal(await rowType(devRow), "Development, read-only");
  assert.deepEqual(await strip(), {
    strip: "Read-onlymyflames refuses every change on Dev box",
    kind: "read-only",
    dot: "Development, read-only",
  });

  // The policy can change elsewhere (another tab, the CLI). While the saved
  // connections cannot be read, the strip assumes Production and Kill stays
  // off; coming back to the window reads them again.
  listFails = true;
  await place("Query editor");
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForSelector('.safety-strip[data-safety="unknown"]');
  assert.match(await text(".safety-strip"), /^Production assumedcould not read the settings of Dev box; changes stay off until they loadRetry$/);
  await place("Server");
  await page.click("#server-nav-sessions");
  await page.click(".server-view[data-view=sessions] [data-action=refresh]");
  await page.waitForSelector('.server-view[data-view=sessions] tbody tr[data-session="93"]');
  await page.click('.server-view[data-view=sessions] tbody tr[data-session="93"]');
  const kill = '.view-inspector [aria-label="Kill: connection 93"]';
  await page.waitForSelector(kill);
  // Off means aria-disabled: the button stays focusable with its reason.
  const killOff = () => page.$eval(kill, (e) => e.disabled || e.getAttribute("aria-disabled") === "true");
  assert.equal(await killOff(), true, "Kill is off while the policy is unknown");
  assert.match(await page.$eval(kill, (e) => e.title), /could not be read \(database is locked\)/);
  // Meanwhile the CLI made Dev box Production and read-write.
  items = items.map((p) => (p.name === "Dev box" ? { ...p, type: "production", read_only: false } : p));
  listFails = false;
  await page.locator(".safety-strip button ::-p-text(Retry)").click();
  await page.waitForSelector('.safety-strip[data-safety="production"]');
  assert.match(await text(".safety-strip"), /every change on Dev box asks first/);
  // The report was read under the old policy: it is greyed with its actions
  // off until it is read again.
  await page.waitForSelector('.server-view[data-view=sessions][data-other-connection="true"]');
  assert.equal(await killOff(), true);
  await page.click(".server-view[data-view=sessions] [data-action=refresh]");
  await page.waitForFunction(() => !document.querySelector(".server-view[data-view=sessions][data-other-connection]"));
  await page.click('.server-view[data-view=sessions] tbody tr[data-session="93"]');
  await page.waitForFunction((k) => { const e = document.querySelector(k); return !!e && !e.disabled && e.getAttribute("aria-disabled") !== "true"; }, {}, kill);
  // A sheet confirmed under one policy sends nothing once it changed: here
  // another tab makes Dev box Development again before Kill is confirmed.
  await page.click(kill);
  await page.waitForSelector("dialog.kill-dialog[open]");
  items = items.map((p) => (p.name === "Dev box" ? { ...p, type: "development" } : p));
  const lists = (seen["/api/profiles/list"] || []).length;
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(".server-view[data-view=sessions] .kill-result");
  assert.ok(seen["/api/profiles/list"].length > lists, "the policy is read again right before sending");
  assert.equal(seen["/api/live/kill"], undefined, "nothing was sent");
  assert.match(
    await text(".server-view[data-view=sessions] .kill-result"),
    /Nothing was sent\. The connection's type or read-only setting changed after this dialog opened, so nothing was sent\./,
  );
  await page.waitForFunction(() => !document.querySelector(".safety-strip"));
  // Unchanged policy: the same Kill goes through.
  await page.locator(".server-view[data-view=sessions] .kill-result ::-p-text(Dismiss)").click();
  await page.click(".server-view[data-view=sessions] [data-action=refresh]");
  await page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  await page.click('.server-view[data-view=sessions] tbody tr[data-session="93"]');
  await page.click(kill);
  await page.waitForSelector("dialog.kill-dialog[open]");
  await page.click(".sheet-actions .danger-solid");
  await page.waitForSelector(".server-view[data-view=sessions] .kill-result.killed");
  assert.equal(seen["/api/live/kill"].length, 1);
  assert.equal(seen["/api/live/kill"][0].connection.type, "development");
  await place("Home");

  // An unsaved connection is Production whatever the form says, and may
  // only choose Read-only; requests carry what it is judged by.
  await button("Add connection");
  await setName("Scratch");
  await setField("Host", "scratch.local");
  await page.click(`${typeControl} button[data-value=development]`);
  await button("Use without saving", ".connection-panel ");
  assert.equal(await rowType(".connection-row.is-active"), "Production");
  assert.deepEqual(await strip(), { strip: "Productionevery change on Scratch asks first", kind: "production", dot: "Production" });
  await connectionAction(page, "Scratch", "Save");
  await page.waitForSelector(readOnly);
  assert.equal(await pressed(), "Production", "the unsaved connection opens as what it is");
  await page.click(readOnly);
  await button("Use without saving", ".connection-panel ");
  assert.equal(await rowType(".connection-row.is-active"), "Production, read-only");
  assert.deepEqual(await strip(), { strip: "Read-onlymyflames refuses every change on Scratch", kind: "read-only", dot: "Production, read-only" });
  await place("Server");
  await page.click("#server-nav-top");
  await page.click(".server-view [data-action=refresh]");
  await page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  const sent = last("/api/live/top").connection;
  assert.deepEqual([sent.profile_id, sent.host, sent.type, sent.read_only], [undefined, "scratch.local", "production", true]);
  await place("Home");

  // Disabled secret store: Save connection never asks and says passwords stay in memory.
  store.available = false;
  store.backend = "none";
  store.label = "not saved";
  await page.reload({ waitUntil: "networkidle0" });
  await place("Home");
  await button("Add connection");
  await page.waitForFunction(() =>
    document.querySelector(".connection-panel > .hint:last-child")?.textContent.includes("stay in memory"),
  );
  await setName("NoStore");
  await page.locator(password).fill("pw-4");
  await button("Save connection");
  await notice("saved without a password");
  assert.equal(await page.$(".save-prompt"), null);
  assert.equal(last("/api/profiles/save").password, undefined);

  assert.deepEqual(errors, []);
  console.log("Profile password workflow passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
