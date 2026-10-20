// Query editor: run only the selected text, or the statement under the
// cursor. The packaged page and the real Python server; /api/live/start and
// /api/live/status are mocked, so no database is needed. A missing parameter
// goes to the real server, which validates before connecting.
// Run after `npm run build`: node test/query-selection.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-selection-"));
const PLAN = await fs.readFile(path.join(root, "test/mysql-explain-json-sample.json"), "utf8");
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

// Every /api/live/start body the page sent; `real` lets one through to the server.
const starts = [];
let real = false;
let finish = "failed";
function capture(body) {
  return {
    schema_version: "capture-1.0",
    raw: PLAN,
    query: body.query,
    mode: body.mode,
    engine: "mysql",
    server_version: "8.4.3",
    captured_at: "2026-10-12T12:00:00+00:00",
    runs: [],
    schema: {},
    table_stats: {},
    variables: {},
    trace: null,
    warnings: [],
    conditions: "",
  };
}

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
    const json = (body) =>
      request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    if (pathname === "/api/live/start") {
      const body = JSON.parse(request.postData() || "{}");
      starts.push(body);
      if (real) return request.continue();
      return json({ id: "job-" + starts.length, state: "queued" });
    }
    if (pathname === "/api/live/status") {
      const body = starts.at(-1);
      return json(
        finish === "complete"
          ? { id: "job", state: "complete", progress: 1, repeat: 1, error: "", result: capture(body) }
          : { id: "job", state: "failed", progress: 0, repeat: 1, error: "Mock capture stopped.", result: null },
      );
    }
    request.continue();
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });

  const EDITOR = '[aria-label="SQL editor"]';
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  // Button labels without the privilege chip ("Unverified") and the reason
  // that describes them; the chip is checked where it matters.
  const buttons = () =>
    page.$$eval(".editor-actions [data-action]", (els) => els.map((e) => {
      const label = e.cloneNode(true);
      label.querySelectorAll(".privilege-unverified, .account-sr").forEach((x) => x.remove());
      return label.textContent.trim();
    }));
  const scope = () => page.$eval(".run-scope", (e) => e.textContent).catch(() => "");
  async function waitScope(expected) {
    await page.waitForFunction(
      (expected) => (document.querySelector(".run-scope")?.textContent || "") === expected,
      { timeout: 3000 },
      expected,
    ).catch(async () => assert.equal(await scope(), expected));
  }
  async function select(start, end = start) {
    await page.$eval(
      EDITOR,
      (el, start, end) => {
        el.focus();
        el.setSelectionRange(start, end);
      },
      start,
      end,
    );
  }
  async function started(count) {
    await page.waitForFunction(
      () => !document.querySelector(".capture-status"),
      { timeout: 5000 },
    );
    assert.equal(starts.length, count, "live/start requests");
    return starts.at(-1);
  }
  async function idle() {
    await page.waitForFunction(() =>
      [...document.querySelectorAll(".editor-actions .button")].every((b) => !b.disabled),
    );
  }

  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab");

  // One statement: today's labels, no scope chip. Explain is ⌘E (Ctrl+E
  // elsewhere) and Run analysis ⇧⌘E; ⌘Return runs SQL in the tab's session.
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  async function chord(key, shift = false) {
    await page.keyboard.down(MOD);
    if (shift) await page.keyboard.down("Shift");
    await page.keyboard.press(key);
    if (shift) await page.keyboard.up("Shift");
    await page.keyboard.up(MOD);
  }
  await page.locator(EDITOR).fill("SELECT 1;");
  assert.deepEqual(await buttons(), ["Explain", "Run analysis"]);
  assert.equal(await page.$(".run-scope"), null);
  assert.equal(
    await page.$eval(".editor-actions [data-action=analyze]", (e) => e.getAttribute("aria-keyshortcuts")),
    `${MOD}+Shift+E`,
  );
  assert.equal(
    await page.$eval(".editor-actions [data-action=explain]", (e) => e.getAttribute("aria-keyshortcuts")),
    `${MOD}+E`,
  );
  assert.equal(
    await page.$eval("[data-action=run]", (e) => e.getAttribute("aria-keyshortcuts")),
    "Control+Enter Meta+Enter",
  );

  const SQL = [
    "SELECT 1;",
    "-- orders for one customer",
    "SELECT id, total",
    "FROM orders",
    "WHERE customer_id = :c;",
    "SELECT 3",
  ].join("\n");
  await page.click('.query-tablist [role=tab][aria-selected=true]', { clickCount: 2 });
  await page.locator('[aria-label="Query name"]').fill("Selected orders");
  await page.keyboard.press("Enter");
  assert.equal(await text('.query-tablist [role=tab][aria-selected=true]'), "Selected orders");
  await page.locator(EDITOR).fill(SQL);
  await page.click('button[aria-label="Explain and Run analysis options"]');
  await page.click(".editor-parameters summary");
  await page.locator('[aria-label="Query parameters"]').fill('{"c": 42}');

  // 1. A selection runs alone; the buttons and chip say so.
  const selection = "SELECT id, total\nFROM orders\nWHERE customer_id = :c";
  const from = SQL.indexOf(selection);
  await select(from, from + selection.length);
  await waitScope("Selection · 3 lines");
  assert.ok(await page.$(".editor-toolbar #run-scope"), "scope stays with the run actions");
  assert.deepEqual(await buttons(), ["Explain selection", "Analyze selection"]);
  assert.match(
    await page.$eval(".editor-actions [data-action=analyze]", (e) => e.title),
    /Run analysis on only the selected text \((⇧⌘E|Ctrl\+Shift\+E)\)/,
  );
  finish = "complete";
  await page.locator(".editor-actions button ::-p-text(Analyze selection)").click();
  // The capture opens in Plans and shows the SQL that ran.
  await page.waitForFunction(
    () => document.querySelector(".plan-heading h1")?.textContent === "Selected orders",
    { timeout: 15000 },
  );
  assert.equal(starts.length, 1);
  assert.equal(starts[0].query, selection);
  assert.equal(starts[0].mode, "analyze");
  assert.deepEqual(starts[0].parameters, { c: 42 });
  assert.equal(await text(".sql-card pre"), selection);
  finish = "failed";

  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab:not([hidden])");
  assert.equal(await page.$eval(EDITOR, (e) => e.value), SQL);
  assert.match(await text(".sent-sql summary"), /SQL sent: the selection \(3 lines\)/);

  // A selection with surrounding whitespace and its ; is trimmed.
  await select(SQL.indexOf("SELECT 3") - 1, SQL.length);
  await page.locator(".editor-actions button ::-p-text(Explain selection)").click();
  assert.equal((await started(2)).query, "SELECT 3");
  assert.equal(starts[1].mode, "explain");
  await idle();

  // 2. No selection, several statements: the statement under the cursor.
  const third = SQL.indexOf("SELECT 3") + 3;
  await select(third);
  await waitScope("Statement 3 of 3 · line 6");
  assert.deepEqual(await buttons(), ["Explain", "Run analysis"]);
  await chord("e");
  // The statement that runs is highlighted briefly, behind the editor text.
  await page.waitForSelector(".sql-backdrop mark");
  assert.equal(await text(".sql-backdrop mark"), "SELECT 3");
  await (await page.$(".editor-panel")).screenshot({ path: path.join(directory, "statement-highlight.png") });
  assert.ok(await page.$(".run-scope.is-lit"));
  assert.equal((await started(3)).query, "SELECT 3");
  assert.equal(starts[2].mode, "explain");
  assert.equal(await page.$eval(EDITOR, (e) => e.value), SQL, "the shortcut inserted text");
  // The caret and selection are untouched by the highlight.
  assert.deepEqual(
    await page.$eval(EDITOR, (e) => [e.selectionStart, e.selectionEnd]),
    [third, third],
  );
  await page.waitForFunction(() => !document.querySelector(".sql-backdrop"), { timeout: 4000 });
  assert.match(await text(".sent-sql summary"), /statement 3 of 3, at the cursor/);
  await idle();

  // ⇧⌘E (Ctrl+Shift+E) runs analysis on the statement at the cursor.
  const second = SQL.indexOf("FROM orders");
  await select(second);
  await waitScope("Statement 2 of 3 · lines 2–5");
  await chord("e", true);
  const statement = await started(4);
  assert.equal(
    statement.query,
    "-- orders for one customer\nSELECT id, total\nFROM orders\nWHERE customer_id = :c",
  );
  assert.equal(statement.mode, "analyze");
  await idle();

  // ⌘E (Ctrl+E) with a selection explains only the selection.
  await select(0, "SELECT 1".length);
  await waitScope("Selection · 1 line");
  await chord("e");
  assert.equal((await started(5)).query, "SELECT 1");
  assert.equal(starts[4].mode, "explain");
  await idle();

  // 3. Typing still works; Enter alone inserts a line break and runs nothing.
  await select(SQL.length);
  await page.keyboard.press("Enter");
  await page.keyboard.type("SELECT 4");
  assert.equal(await page.$eval(EDITOR, (e) => e.value), SQL + "\nSELECT 4");
  assert.equal(starts.length, 5);
  await waitScope("Statement 3 of 3 · lines 6–7");

  // 4. A selection with two statements is refused before any request.
  await select(0, SQL.length);
  await page.locator(".editor-actions button ::-p-text(Analyze selection)").click();
  await page.waitForFunction(() =>
    document.querySelector(".editor-panel .error")?.textContent.includes("holds 3 statements"),
  );
  assert.equal(starts.length, 5);

  // 5. Parameters bind against the text that runs. The selection references
  // :missing, which the JSON does not define: the server's parameter error.
  const withMissing = "SELECT :c;\nSELECT * FROM orders WHERE id = :missing;";
  await page.locator(EDITOR).fill(withMissing);
  await select(withMissing.indexOf("SELECT *"), withMissing.length);
  real = true;
  await page.locator(".editor-actions button ::-p-text(Explain selection)").click();
  await page.waitForFunction(() =>
    document.querySelector(".editor-panel .error")?.textContent.includes("Missing parameter: missing"),
  );
  real = false;
  assert.equal(starts.at(-1).query, "SELECT * FROM orders WHERE id = :missing");
  // The other statement only needs :c, which is defined.
  await select(0, "SELECT :c".length);
  await page.locator(".editor-actions button ::-p-text(Explain selection)").click();
  assert.equal((await started(7)).query, "SELECT :c");
  await idle();

  // 6. Session restore keeps the tab text and its caret or selection (no results).
  await page.waitForFunction(
    async (sql) => {
      const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
      const r = await fetch("/api/session/load", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: "{}",
      });
      const tabs = (await r.json()).state?.query_lab?.tabs || [];
      return tabs.some((t) => t.sql === sql);
    },
    { timeout: 10000, polling: 500 },
    withMissing,
  );
  const saved = await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    return (await r.json()).state.query_lab.tabs;
  });
  for (const tab of saved) {
    for (const key of Object.keys(tab)) assert.ok(["id", "name", "parameters", "sql", "selection", "result_tab", "limit"].includes(key), key);
    if (tab.selection) assert.deepEqual(Object.keys(tab.selection).sort(), ["end", "start"]);
  }

  // 7. Narrow window: toolbar overflow stays inside the editor, not the page.
  await select(0, 4);
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth),
    false,
  );
  await page.screenshot({ path: path.join(directory, "query-selection-mobile.png"), fullPage: true });

  assert.deepEqual(errors, []);
  console.log("Query selection passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
