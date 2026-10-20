import { connectionAction } from "./connection-helpers.mjs";
import { chooseView, currentView, openConnectionPicker } from "./chrome-helpers.mjs";
// Session restore: reload, server restart, Start fresh, a two-tab conflict,
// the startup toggle, and --no-restore, against the packaged UI and server.
// Run after `npm run build`: node test/session.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-session-"));
const storeDir = path.join(directory, "store");
await fs.mkdir(storeDir);
const store = path.join(storeDir, "workspace.sqlite3");
const PASSWORD = "s3cret-session-password";

let server = null;
async function start(noRestore = false) {
  const proc = spawn(
    process.env.PYTHON || "python3",
    [
      "-c",
      "from myflames.ui import WorkspaceServer; import sys; " +
        "s=WorkspaceServer(store_path=sys.argv[1], no_restore=sys.argv[2]=='1'); " +
        "print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
      store,
      noRestore ? "1" : "0",
    ],
    {
      cwd: root,
      env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" },
    },
  );
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error("UI did not start: " + text)), 10000);
    proc.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    proc.on("error", reject);
  });
  server = proc;
  return url;
}
async function stop() {
  if (server && server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
  server = null;
}

let browser;
try {
  let url = await start();
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  const dialogs = [];
  const watch = (p) => {
    p.on("pageerror", (e) => errors.push(String(e)));
    p.on("dialog", (d) => {
      dialogs.push(d.type());
      d.accept();
    });
  };
  watch(page);
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: directory,
  });
  await page.setViewport({ width: 1440, height: 1000 });

  const button = (p, label, scope = "") =>
    p.locator(`${scope}button ::-p-text(${label})`).click();
  const status = (p) =>
    p.$eval(".session-status", (e) => [e.dataset.state, e.textContent]);
  // The stored session, read through the page's own token.
  const stored = (p) =>
    p.evaluate(async () => {
      const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
      const r = await fetch("/api/session/load", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: "{}",
      });
      return r.json();
    });
  async function until(p, predicate, message) {
    const deadline = Date.now() + 10000;
    let last;
    while (Date.now() < deadline) {
      last = await stored(p);
      if (predicate(last)) return last;
      await new Promise((r) => setTimeout(r, 100));
    }
    throw new Error(message + ": " + JSON.stringify({...last, state: {...last.state, plans: last.state?.plans?.length}}).slice(0, 2000) + " status=" + JSON.stringify(await status(p)));
  }
  async function saved(p) {
    await p.waitForFunction(
      () => document.querySelector(".session-status")?.dataset.state === "saved",
    );
  }
  async function open(p, address) {
    await p.goto(address, { waitUntil: "networkidle0" });
    await p.waitForFunction(
      () => document.querySelector(".session-status")?.dataset.state !== "loading",
    );
  }
  async function chart(p) {
    await p.waitForFunction(
      () => !!document.querySelector('.plan-canvas .plan-chart[data-mfc-mounted="true"] svg'),
    );
  }
  async function importPlan(p) {
    // Home and Plans place this action beside their respective content.
    // Repeated imports stay in Plans rather than navigating away to Home.
    await p.locator('.topbar [aria-label="Import plan"], .plans-bar .plans-import').click();
    await p.waitForSelector('dialog[open] input[type="file"]');
    const input = await p.$('dialog[open] input[type="file"]');
    await input.uploadFile(path.join(root, "test/mysql-explain-json-sample.json"));
    await p.waitForFunction(() => !document.querySelector('dialog[open]'));
    await chart(p);
    assert.equal(await p.$(".save-state"), null, "importing a plan does not edit an investigation");
  }
  const leaveBlocked = (p) => p.evaluate(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  });
  async function holdSaves(p) {
    await p.evaluate(() => {
      const original = window.fetch;
      let release;
      const gate = new Promise(resolve => { release = resolve; });
      window.fetch = (...args) => String(args[0]).endsWith("/api/session/save")
        ? gate.then(() => original(...args)) : original(...args);
      window.__releaseSessionSaves = () => { window.fetch = original; release(); };
    });
  }
  const releaseSaves = (p) => p.evaluate(() => window.__releaseSessionSaves());
  const leaveSafe = (p) => p.waitForFunction(() => {
    const event = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(event);
    return !event.defaultPrevented;
  });
  async function setField(p, label, value) {
    await p.$eval(
      ".connection-panel",
      (panel, label, value) => {
        const input = [...panel.querySelectorAll("label")]
          .find((e) => e.firstChild.textContent.trim() === label)
          ?.querySelector("input");
        if (!input) throw Error(label);
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      },
      label,
      value,
    );
  }
  const place = (p, label) => button(p, label, ".main-nav ");
  async function openSettings(p) {
    if (!(await p.$("#settings-popover"))) await p.click('button[aria-label="Settings"]');
    await p.waitForSelector("#settings-popover .session-toggle input");
  }
  async function diskHasPassword() {
    for (const name of await fs.readdir(storeDir)) {
      const bytes = await fs.readFile(path.join(storeDir, name));
      if (bytes.includes(Buffer.from(PASSWORD))) return name;
    }
    return "";
  }

  // 1. Build up a workspace: a plan, view, metric, selection, and two query tabs.
  await open(page, url);
  assert.equal((await status(page))[0], "saved");
  assert.equal(await page.$(".session-notice"), null, "nothing to restore yet");
  await button(page, "Explore sample plan");
  await chart(page);
  assert.equal(await page.$(".save-state"), null, "opening the sample does not edit an investigation");
  assert.match(await page.$eval(".window-statusbar", e => e.textContent), /Sample plan/);
  const sample = await until(page, r => r.state?.plans?.[0]?.origin === "sample", "sample provenance autosaved");
  assert.equal(sample.state.investigation.dirty, false);
  await page.reload({ waitUntil: "networkidle0" });
  await chart(page);
  assert.equal(await page.$(".save-state"), null);
  assert.match(await page.$eval(".window-statusbar", e => e.textContent), /Sample plan/, "provenance survives reload");
  // Read-only navigation persists without changing the footer to Saving….
  await saved(page);
  await page.evaluate(() => {
    window.__saveStates = [];
    window.__saveObserver = new MutationObserver(() => window.__saveStates.push(document.querySelector(".session-status").textContent));
    window.__saveObserver.observe(document.querySelector(".session-status"), { subtree: true, childList: true, characterData: true, attributes: true });
  });
  await page.click('button[aria-label="Settings"]');
  await button(page, "Audit log", "#settings-popover ");
  await until(page, r => r.state?.mode === "audit", "Audit navigation restored");
  assert.equal(await page.evaluate(() => window.__saveStates.some(s => /Saving…/.test(s))), false);
  await page.evaluate(() => window.__saveObserver.disconnect());
  await place(page, "Plans");
  await chart(page);
  await button(page, "Execution tree", ".chart-views ");
  await page.select('[aria-label="Operator metric"]', "rows");
  await page.$$eval(".plan-fold .inspector-metrics .metric-row", (els) =>
    els.find((e) => e.textContent.includes("Table scan [categories]")).click(),
  );
  await page.waitForSelector(".operator-detail");
  // Notes are edited in the Save investigation sheet; Close keeps them unsaved.
  await page.locator('.topbar [aria-label="Save investigation"]').click();
  await page.waitForSelector("dialog.notebook-sheet[open]");
  await page.locator(".investigation-notes textarea").fill("Draft notes, not saved as an investigation.");
  await page.locator("dialog.notebook-sheet .sheet-actions button ::-p-text(Close)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));

  // An unsaved connection, set up on Home and put in use with its password.
  await place(page, "Home");
  await button(page, "Add connection");
  await setField(page, "Host", "db.internal");
  await setField(page, "User", "analyst");
  await setField(page, "Password", PASSWORD);
  await button(page, "Use without saving", ".connection-panel ");
  await place(page, "Query editor");
  await page.waitForSelector(".query-lab");
  assert.equal(await page.$('.topbar [aria-label="Save investigation"]'), null, "even a dirty investigation has no action in Query editor");
  assert.equal(await page.$('.topbar .save-state'), null);
  assert.match(
    await page.$eval(".window-toolbar .connection-picker", (e) => e.textContent),
    /analyst@db\.internal:3306/,
  );
  await page.locator('[aria-label="SQL editor"]').fill("SELECT * FROM users WHERE id = :id");
  // The sticky top bar can cover the tab strip once the page has scrolled.
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('button[aria-label="New query tab"]').click();
  await page.waitForFunction(() => document.querySelectorAll(".query-tabs [role=tab][id^=query-tab-]").length === 2);
  // Tabs rename inline: F2 on the selected tab, Enter keeps the name.
  await page.focus(".query-tabs [role=tab][id^=query-tab-][aria-selected=true]");
  await page.keyboard.press("F2");
  await page.locator('[aria-label="Query name"]').fill("Orders by customer");
  await page.keyboard.press("Enter");
  await page.locator('[aria-label="SQL editor"]').fill("SELECT * FROM orders WHERE customer_id = :c");
  // Parameters and the trace option live in the analysis options popover.
  await page.click('button[aria-label="Explain and Run analysis options"]');
  await page.click(".editor-parameters summary");
  await page.locator('[aria-label="Query parameters"]').fill('{"c": 42}');
  await page.$eval(".execution-options input[type=checkbox]", (e) => e.click());
  await page.keyboard.press("Escape");
  await place(page, "Workload");
  await chooseView(page, "sessions");
  await place(page, "Plans");
  await chart(page);
  const before = await until(
    page,
    (s) =>
      s.state?.plans.length === 1 &&
      s.state.view === "tree" &&
      s.state.query_lab.tabs.length === 2 &&
      s.state.query_lab.slow.kind === "sessions" &&
      s.state.mode === "inspect",
    "autosave did not store the workspace",
  );
  await saved(page);
  assert.equal(before.state.metric, "rows");
  assert.ok(before.state.selected_id);
  assert.equal(before.state.query_lab.connection.host, "db.internal");
  assert.equal(before.state.query_lab.trace, true);
  assert.equal(JSON.stringify(before).includes(PASSWORD), false);
  assert.equal(await diskHasPassword(), "", "password reached the database file");

  // 2. Reload: everything comes back, without a leave-page prompt.
  async function assertRestored(p) {
    await p.waitForSelector(".session-notice");
    assert.match(
      await p.$eval(".session-notice", (e) => e.textContent),
      /Restored your workspace from/,
    );
    await chart(p);
    assert.equal((await p.$$(".plan-item")).length, 1);
    assert.equal(
      await p.$eval(".chart-views button[aria-pressed=true]", (e) => e.textContent),
      "Execution tree",
    );
    assert.equal(await p.$eval('[aria-label="Operator metric"]', (e) => e.value), "rows");
    assert.match(await p.$eval(".operator-detail h3", (e) => e.textContent), /categories/);
    await p.locator('.topbar [aria-label="Save investigation"]').click();
    await p.waitForSelector("dialog.notebook-sheet[open]");
    assert.equal(
      await p.$eval(".investigation-notes textarea", (e) => e.value),
      "Draft notes, not saved as an investigation.",
    );
    await p.keyboard.press("Escape");
    await p.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
    await place(p, "Query editor");
    await p.waitForSelector(".query-lab");
    assert.deepEqual(
      await p.$$eval(".query-tabs [role=tab][id^=query-tab-]", (els) => els.map((e) => e.textContent)),
      ["Query 1", "Orders by customer"],
    );
    assert.equal(
      await p.$eval(".query-tabs [id^=query-tab-][aria-selected=true]", (e) => e.textContent),
      "Orders by customer",
    );
    assert.equal(
      await p.$eval('[aria-label="SQL editor"]', (e) => e.value),
      "SELECT * FROM orders WHERE customer_id = :c",
    );
    assert.equal(await p.$eval('[aria-label="Query parameters"]', (e) => e.value), '{"c": 42}');
    assert.equal(await p.$eval(".execution-options input[type=checkbox]", (e) => e.checked), true);
    // The connection in use comes back, without its password.
    await openConnectionPicker(p);
    const picker = await p.$eval(".window-toolbar .connection-picker", (e) => e.textContent);
    assert.match(picker, /Local MySQL \(not saved\)/);
    assert.match(picker, /analyst@db\.internal:3306/);
    assert.equal(
      await p.$eval(".window-toolbar .picker-password", (e) => e.value),
      "",
      "a typed password is never restored",
    );
    await place(p, "Workload");
    await p.waitForSelector(".server-view[data-view=sessions]", { visible: true });
    assert.equal(await currentView(p), "sessions");
    await place(p, "Home");
    const unsavedName = await p.$eval(".connection-row.is-active [data-col=name] strong", el => el.textContent);
    await connectionAction(p, unsavedName, "Save");
    await p.waitForSelector(".connection-panel");
    const fields = await p.$eval(".connection-panel", (panel) =>
      Object.fromEntries(
        [...panel.querySelectorAll("label")].map((l) => [
          l.firstChild.textContent.trim(),
          l.querySelector("input")?.value,
        ]),
      ),
    );
    assert.equal(fields.Host, "db.internal");
    assert.equal(fields.User, "analyst");
    assert.equal(fields.Password, "", "a typed password is never restored");
    await place(p, "Plans");
    await chart(p);
  }
  await page.reload({ waitUntil: "networkidle0" });
  await assertRestored(page);
  assert.deepEqual(dialogs, [], "autosave on: no leave-page prompt");
  await page.screenshot({ path: path.join(directory, "restored.png") });

  // 3. Restart the Python server with the same store: still restored.
  await stop();
  url = await start();
  await open(page, url);
  await assertRestored(page);

  // 4. Two tabs: the later save over an older revision shows the notice and
  // reloads the newer state.
  await saved(page);
  const other = await browser.newPage();
  watch(other);
  await other.setViewport({ width: 1440, height: 1000 });
  await open(other, url);
  await chart(other);
  await page.bringToFront();
  await button(page, "Bar chart", ".chart-views ");
  await until(page, (s) => s.state?.view === "bargraph", "first tab did not save");
  await other.bringToFront();
  await other.select('[aria-label="Operator metric"]', "total_time");
  await other.waitForFunction(() =>
    document.querySelector(".session-notice")?.textContent.includes("changed in another browser tab"),
  );
  await other.waitForFunction(
    () => document.querySelector(".chart-views button[aria-pressed=true]")?.textContent === "Bar chart",
  );
  assert.equal(await other.$eval('[aria-label="Operator metric"]', (e) => e.value), "rows");
  await saved(other);
  await other.close();
  await page.bringToFront();

  // 5. Start fresh offers a backup of unsaved plans, then clears the session.
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".session-notice");
  await (await page.$(".session-notice")).screenshot({ path: path.join(directory, "notice.png") });
  await button(page, "Start fresh", ".session-notice ");
  await page.waitForSelector(".session-notice .text-button ::-p-text(Cancel)");
  await (await page.$(".session-notice")).screenshot({ path: path.join(directory, "notice-backup.png") });
  await page.waitForFunction(() =>
    document.querySelector(".session-notice")?.textContent.includes("Download a backup"),
  );
  await button(page, "Download backup and start fresh", ".session-notice ");
  await page.waitForFunction(() => !document.querySelector(".plan-item"));
  const backupPath = path.join(directory, "unsaved-investigation.json");
  for (let i = 0; i < 100; i++) {
    try {
      await fs.access(backupPath);
      break;
    } catch {
      await new Promise((r) => setTimeout(r, 50));
    }
  }
  const backup = JSON.parse(await fs.readFile(backupPath, "utf8"));
  assert.equal(backup.schema_version, "investigation-1.0");
  assert.equal(backup.plans.length, 1);
  assert.equal((await stored(page)).state, null);
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".welcome");
  assert.equal(await page.$(".session-notice"), null);
  assert.equal((await page.$$(".plan-item")).length, 0);
  await page.waitForSelector('.home [aria-label="Get started"]');
  assert.equal((await page.$$('.topbar [aria-label="Import plan"]')).length, 1);
  // One Import plan on Home (the top bar); the empty state offers the sample.
  assert.equal((await page.$$('.home-start button ::-p-text(Import plan)')).length, 0);
  assert.equal((await page.$$('.home-start button ::-p-text(Explore sample plan)')).length, 1);
  assert.equal(await page.$('.topbar [aria-label="New query tab"]'), null);
  await place(page, "Query editor");
  assert.deepEqual(
    await page.$$eval(".query-tabs [role=tab][id^=query-tab-]", (els) => els.map((e) => e.textContent)),
    ["Query 1"],
  );

  // 6. Turning the toggle off stops saving and clears the session; unsaved
  // work then gets the leave-page prompt.
  await place(page, "Home");
  await openSettings(page);
  await page.click(".session-toggle input");
  await page.waitForFunction(
    () => document.querySelector(".session-status")?.dataset.state === "off",
  );
  assert.equal((await status(page))[1], "Autosave is off");
  await button(page, "Explore sample plan");
  await chart(page);
  await new Promise((r) => setTimeout(r, 1500));
  const off = await stored(page);
  assert.equal(off.restore, false);
  assert.equal(off.state, null);
  dialogs.length = 0;
  await page.reload({ waitUntil: "networkidle0" });
  assert.deepEqual(dialogs, [], "the reopenable sample alone never prompts");
  await page.waitForSelector(".welcome");
  await importPlan(page);
  await page.reload({ waitUntil: "networkidle0" });
  assert.deepEqual(dialogs, ["beforeunload"], "an imported plan with autosave off prompts");
  await page.waitForSelector(".welcome");
  await openSettings(page);
  assert.equal(await page.$eval(".session-toggle input", (e) => e.checked), false);
  // Back on: the current workspace is saved right away.
  await page.click(".session-toggle input");
  await button(page, "Explore sample plan");
  await chart(page);
  await until(page, (s) => s.restore && s.state?.plans.length === 1, "toggle on did not save");
  await saved(page);

  // Small pending saves still protect real content, even before the status
  // reports Saving. Only an acknowledged snapshot clears that protection.
  await holdSaves(page);
  await importPlan(page);
  assert.equal(await leaveBlocked(page), true, "an imported plan pending autosave prompts");
  await releaseSaves(page);
  await until(page, s => s.state?.plans.length === 2, "import did not autosave");
  await leaveSafe(page);
  await holdSaves(page);
  await page.click('button[aria-label="Remove mysql-explain-json-sample.json"]');
  assert.equal(await leaveBlocked(page), true, "closing a plan pending autosave prompts");
  assert.equal(await page.$(".save-state"), null, "closing workspace content does not edit an investigation");
  await releaseSaves(page);
  await until(page, s => s.state?.plans.length === 1, "plan closure did not autosave");
  await leaveSafe(page);

  // 7. --no-restore: clean for this launch, no autosave, session kept.
  await stop();
  url = await start(true);
  await open(page, url);
  await page.waitForSelector(".welcome");
  assert.equal((await page.$$(".plan-item")).length, 0);
  assert.equal(await page.$(".session-notice"), null);
  assert.deepEqual(await status(page), ["off", "Autosave is off for this launch"]);
  await openSettings(page);
  assert.match(await page.$eval("#session-toggle-hint", (e) => e.textContent), /--no-restore/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
    false,
  );
  await page.click("#window-sidebar-toggle");
  await page.waitForSelector(".sidebar-drawer.open");
  assert.ok(await page.$eval(".session-status", (e) => e.getBoundingClientRect().height > 0));
  await page.screenshot({ path: path.join(directory, "no-restore-mobile.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  // With autosave off, edited query tabs alone are unsaved work: prompt.
  // React's scheduler tasks are held back 400 ms in this page from here on,
  // so the check must see the edit without waiting for one (the reload once
  // ran before the editor's state reached the workspace).
  dialogs.length = 0;
  await place(page, "Query editor");
  await page.evaluate(() => {
    const original = MessagePort.prototype.postMessage;
    MessagePort.prototype.postMessage = function (...args) {
      setTimeout(() => original.apply(this, args), 400);
    };
  });
  await page.focus(".sql-editor");
  await page.keyboard.press("End");
  await page.keyboard.type(" -- edited");
  await page.reload({ waitUntil: "networkidle0" });
  assert.deepEqual(dialogs, ["beforeunload"], "edited query tab, autosave off: prompt");
  await page.waitForSelector(".welcome");
  await importPlan(page);
  await new Promise((r) => setTimeout(r, 1500));
  dialogs.length = 0;
  await stop();
  url = await start();
  await open(page, url);
  assert.deepEqual(dialogs, ["beforeunload"], "no autosave this launch: prompt");
  await page.waitForSelector(".session-notice");
  await chart(page);
  assert.equal((await page.$$(".plan-item")).length, 1, "stored session kept");

  assert.deepEqual(errors, []);
  console.log("Session restore checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  await stop();
}
