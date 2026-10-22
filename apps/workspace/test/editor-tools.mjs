// Query editor tools: the tab menu (right-click, Shift+F10, the context-menu
// key: Rename…, Duplicate, Close, Close others), renaming with validation
// that survives a reload, Format SQL (the toolbar button, Shift+Option+F and
// the palette) on the whole tab or the selection as one undo step, the
// refusal when formatting would change meaning, DELIMITER blocks left as
// written, a caret moved or a selection changed while the formatter's chunk
// loads (the request is held), tab names cut without splitting an emoji, and
// the palette's Rename query tab. The packaged page and the real
// Python server; the editor API is mocked and nothing may run. Run after
// `npm run build`: node test/editor-tools.mjs (EDITOR_TOOLS_SHOTS=<dir> also
// writes light and dark screenshots at 1440x900 and 720x520).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-tools-"));
const shots = process.env.EDITOR_TOOLS_SHOTS || "";
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

const seen = [];
const calls = (p) => seen.filter((s) => s.path === p);
function reply(p) {
  switch (p) {
    case "/api/editor/preview":
      return { status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "This session's privileges are not verified.", warning: "" }, policy: { action: "allow", text: "" } };
    case "/api/live/privileges":
      return { schema_version: "privileges-1.0", account: "app@%", grants: [], current_roles: [], actions: {} };
    case "/api/editor/status":
      return { status: "none", session: null, run: null };
    case "/api/editor/history":
      return { items: [] };
    case "/api/editor/close":
      return { closed: true, session: null };
  }
  return undefined;
}

const EDITOR = '[aria-label="SQL editor"]';
const MENU = '[role=menu].query-tab-menu';
const ACTIVE_TAB = ".query-tablist [role=tab][aria-selected=true]";
const FORMAT = 'button[data-action="format"]';
const FEEDBACK = ".editor-feedback";

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
  // While set, the Format SQL chunk's request waits for release(): what the
  // user does during the formatter's first load.
  let heldChunk = null;
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (heldChunk && /\/assets\/sqlFormat-[^/]+\.js$/.test(p)) {
      heldChunk.requested();
      heldChunk.released.then(() => request.continue());
      return;
    }
    if (!p.startsWith("/api/editor/") && p !== "/api/live/privileges") {
      if (p.startsWith("/api/live/") || p.startsWith("/api/connection/")) {
        seen.push({ path: p });
        return request.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
      }
      return request.continue();
    }
    seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
    const answer = reply(p);
    if (answer === undefined) return request.respond({ status: 500, contentType: "application/json", body: '{"error":"no mock"}' });
    request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");

  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const tabNames = () => page.$$eval(".query-tablist [role=tab] .query-tab-label", (els) => els.map((e) => e.textContent));
  const sql = () => page.$eval(EDITOR, (e) => e.value);
  const feedback = () => page.$eval(FEEDBACK, (e) => (e.hidden ? "" : e.textContent));
  const active = (selector) => page.$eval(selector, (e) => e === document.activeElement).catch(() => false);
  /** Replace the editor's text the way typing does (an input event React
   * sees), so the tab's state holds it too. */
  async function setText(value) {
    await page.$eval(EDITOR, (el, v) => {
      el.focus();
      el.select();
      document.execCommand("insertText", false, v);
    }, value);
    await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, value);
    await pause(30);
    assert.equal(await sql(), value, "the tab keeps the new text");
  }
  /** Hold the Format SQL chunk until release(); `seen` resolves once the
   * page asked for it (the chunk is not loaded yet). */
  function holdFormatter() {
    let requested;
    let release;
    let timer;
    const hold = {
      requested: () => requested(),
      released: new Promise((r) => (release = r)),
      seen: new Promise((resolve, reject) => {
        requested = resolve;
        timer = setTimeout(() => reject(Error("the Format SQL chunk was not requested (already loaded?)")), 5000);
      }).finally(() => clearTimeout(timer)),
      release: () => {
        heldChunk = null;
        release();
      },
    };
    heldChunk = hold;
    return hold;
  }
  /** True when *value* has a lone surrogate (half of an emoji). */
  const broken = (value) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
  async function select(start, end) {
    await page.$eval(EDITOR, (el, start, end) => {
      el.focus();
      el.setSelectionRange(start, end);
    }, start, end);
  }
  async function until(check, message, timeout = 8000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = await check();
      if (value) return value;
      await pause(25);
    }
    assert.fail(message);
  }
  /** Run *action* and return how long, measured in the page, the first
   * change under *selector* took to appear. */
  async function timed(selector, action) {
    await page.evaluate((s) => {
      window.__shown = 0;
      window.__start = performance.now();
      const seenNow = () => !!document.querySelector(s);
      const watch = new MutationObserver(() => {
        if (seenNow()) {
          window.__shown = performance.now();
          watch.disconnect();
        }
      });
      watch.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true });
    }, selector);
    await action();
    await page.waitForSelector(selector);
    return page.evaluate(() => (window.__shown || performance.now()) - window.__start);
  }
  async function openMenuByClick(name) {
    const tab = await page.$(`xpath/.//div[contains(@class,"query-tablist")]//*[@role="tab"][.//span[text()="${name}"]]`);
    assert.ok(tab, `tab ${name}`);
    const box = await tab.boundingBox();
    const took = await timed(MENU, () => page.mouse.click(box.x + 12, box.y + box.height / 2, { button: "right" }));
    return took;
  }
  const items = () => page.$$eval(`${MENU} [role=menuitem]`, (els) => els.map((e) => [e.textContent.replace(/F2$/, "").trim(), e.disabled]));

  // 1. Right-click opens the tab menu at once, focus on its first item.
  await setText("select a, b from t where x = 1");
  const tookMenu = await openMenuByClick("Query 1");
  assert.ok(tookMenu < 100, `the menu appeared ${tookMenu.toFixed(1)} ms after the click`);
  assert.deepEqual(await items(), [["Rename…", false], ["Duplicate", false], ["Close", false], ["Close others", true]]);
  assert.equal(await page.$eval(MENU, (e) => e.getAttribute("aria-label")), "Query tab Query 1");
  assert.equal(await page.$eval(`${MENU} [role=menuitem]`, (e) => e === document.activeElement), true, "focus starts on Rename…");
  assert.equal(await page.$eval(`${MENU} .context-menu-shortcut`, (e) => e.textContent), "F2");
  const menuBox = await page.$eval(MENU, (e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, bottom: r.bottom, w: innerWidth, h: innerHeight }; });
  assert.ok(menuBox.left >= 0 && menuBox.right <= menuBox.w && menuBox.bottom <= menuBox.h, `the menu fits the window: ${JSON.stringify(menuBox)}`);
  // Escape closes it and returns focus to the tab.
  await page.keyboard.press("Escape");
  await page.waitForFunction((m) => !document.querySelector(m), {}, MENU);
  assert.equal(await active(ACTIVE_TAB), true, "Escape returns focus to the tab");

  // 2. Shift+F10 opens it under the tab; Down then Enter duplicates.
  await page.keyboard.down("Shift");
  await page.keyboard.press("F10");
  await page.keyboard.up("Shift");
  await page.waitForSelector(MENU);
  const under = await page.evaluate((m, t) => {
    const menu = document.querySelector(m).getBoundingClientRect();
    const tab = document.querySelector(t).getBoundingClientRect();
    return { gap: menu.top - tab.bottom, left: Math.abs(menu.left - tab.left) };
  }, MENU, ACTIVE_TAB);
  assert.ok(under.gap >= 0 && under.gap <= 6 && under.left <= 1, `Shift+F10 opens the menu under the tab: ${JSON.stringify(under)}`);
  await page.keyboard.press("ArrowDown");
  assert.equal(await page.$eval(`${MENU} [role=menuitem]:nth-of-type(2)`, (e) => e === document.activeElement), true, "Down moves to Duplicate");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  assert.deepEqual(await tabNames(), ["Query 1", "Query 1 copy"]);
  assert.equal(await text(`${ACTIVE_TAB} .query-tab-label`), "Query 1 copy", "the copy is the current tab");
  assert.equal(await sql(), "select a, b from t where x = 1", "the copy has the same SQL");
  await until(() => active(ACTIVE_TAB), "focus moves to the copy's tab");
  assert.match(await feedback(), /^Duplicated “Query 1” as “Query 1 copy”\. The copy runs in its own session\.$/);

  // 3. The context-menu key and Rename…: the field opens focused and selected;
  // the name is trimmed.
  await page.keyboard.press("ContextMenu");
  await page.waitForSelector(MENU);
  await page.keyboard.press("Enter");
  await page.waitForSelector('[aria-label="Query name"]');
  assert.equal(await active('[aria-label="Query name"]'), true, "Rename… focuses the name field");
  assert.equal(await page.$eval('[aria-label="Query name"]', (e) => e.selectionStart === 0 && e.selectionEnd === e.value.length), true, "the old name is selected");
  await page.keyboard.type("  Orders by day  ");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector('[aria-label="Query name"]'));
  assert.deepEqual(await tabNames(), ["Query 1", "Orders by day"]);
  await until(() => active(ACTIVE_TAB), "Enter returns focus to the tab");

  // 4. Validation: an empty name keeps the field open and says why; Escape
  // keeps the old name; the field holds at most 64 characters.
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  await page.keyboard.press("Backspace");
  await page.keyboard.type("   ");
  await page.keyboard.press("Enter");
  await page.waitForSelector('[aria-label="Query name"][aria-invalid="true"]');
  assert.equal(await active('[aria-label="Query name"]'), true, "the field stays focused");
  assert.equal(await feedback(), "Type a name for the tab, or press Escape to keep “Orders by day”.");
  assert.equal(await page.$eval(FEEDBACK, (e) => e.classList.contains("is-error")), true);
  const border = await page.$eval('[aria-label="Query name"]', (e) => getComputedStyle(e).borderTopColor);
  assert.notEqual(border, "rgba(0, 0, 0, 0)", "the invalid field is outlined");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[aria-label="Query name"]'));
  assert.deepEqual(await tabNames(), ["Query 1", "Orders by day"], "Escape keeps the name");
  assert.equal(await feedback(), "", "the hint goes away");
  await until(() => active(ACTIVE_TAB), "Escape returns focus to the tab");
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  assert.equal(await page.$eval('[aria-label="Query name"]', (e) => e.maxLength), 64);
  await page.keyboard.type("x".repeat(80));
  assert.equal(await page.$eval('[aria-label="Query name"]', (e) => e.value.length), 64, "at most 64 characters");
  await page.keyboard.press("Escape");
  await until(() => active(ACTIVE_TAB), "Escape returns focus to the tab");
  // Leaving an empty field keeps the old name and says so.
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  await page.keyboard.press("Backspace");
  await page.click(EDITOR);
  await page.waitForFunction(() => !document.querySelector('[aria-label="Query name"]'));
  assert.deepEqual(await tabNames(), ["Query 1", "Orders by day"]);
  assert.equal(await feedback(), "Kept the name “Orders by day”: a tab name cannot be empty.");

  // 5. A long name ends in an ellipsis; the tooltip has all of it.
  await page.focus(ACTIVE_TAB);
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  const long = "Monthly revenue by region, product line and sales channel 2026";
  await page.keyboard.type(long);
  await page.keyboard.press("Enter");
  await page.waitForFunction((n) => document.querySelector(".query-tablist [role=tab][aria-selected=true] .query-tab-label")?.textContent === n, {}, long);
  const label = await page.$eval(`${ACTIVE_TAB} .query-tab-label`, (e) => ({ clipped: e.scrollWidth > e.clientWidth, width: e.getBoundingClientRect().width, title: e.closest("[role=tab]").title }));
  assert.ok(label.clipped && label.width <= 241, `a long name is cut: ${JSON.stringify(label)}`);
  assert.ok(label.title.startsWith(`${long}\n`), "the tooltip has the whole name");

  // 6. Names survive a reload (the session keeps them).
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");
  assert.deepEqual(await tabNames(), ["Query 1", long], "renamed tabs come back after a reload");

  // 7. Close others from the menu of a tab; Close from the menu.
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 3);
  await openMenuByClick(long);
  assert.deepEqual((await items()).map(([n, d]) => [n, d]), [["Rename…", false], ["Duplicate", false], ["Close", false], ["Close others", false]]);
  const closesBefore = calls("/api/editor/close").length;
  await page.click(`${MENU} [role=menuitem]:nth-of-type(4)`);
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);
  assert.deepEqual(await tabNames(), [long]);
  assert.equal(calls("/api/editor/close").length - closesBefore, 2, "each other tab ends its session");
  await until(async () => (await feedback()) === "Closed 2 other tabs.", "Close others reports what it closed");
  await until(() => active(ACTIVE_TAB), "focus stays on the kept tab");
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  await openMenuByClick("Query 2");
  await page.click(`${MENU} [role=menuitem]:nth-of-type(3)`);
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);
  assert.deepEqual(await tabNames(), [long], "Close closes the tab it was opened on");
  await until(() => active(ACTIVE_TAB), "focus moves to the remaining tab");

  // 8. Format SQL from the toolbar: the whole tab, keywords in upper case, two-space indent.
  const formatButton = await page.$eval(FORMAT, (e) => ({ label: e.getAttribute("aria-label"), title: e.title, keys: e.getAttribute("aria-keyshortcuts"), icon: !!e.querySelector("svg") }));
  assert.equal(formatButton.label, "Format SQL");
  assert.equal(formatButton.keys, "Shift+Alt+F");
  assert.ok(formatButton.icon, "an icon");
  assert.match(formatButton.title, /^Format SQL: the whole tab \((⇧⌥F|Shift\+Alt\+F)\)\. Changes only spaces, line breaks and keyword case; nothing runs\.$/);
  const runsBefore = calls("/api/editor/run").length;
  const ORIGINAL = "select a, b from orders o where o.status = 'paid' and o.total > 10 order by a";
  await setText(ORIGINAL);
  await select(0, 0);
  // The first use loads the formatter (held here). The click is answered at
  // once, and a caret moved meanwhile is where the caret stays (review
  // finding: it went back to where it was at the click).
  const firstLoad = holdFormatter();
  const tookFormat = await timed(`${FEEDBACK}:not([hidden])`, () => page.click(FORMAT));
  assert.ok(tookFormat < 150, `Format SQL reacted ${tookFormat.toFixed(1)} ms after the click`);
  await firstLoad.seen;
  assert.equal(await feedback(), "Formatting the tab…", "the click is answered while the formatter loads");
  await select(ORIGINAL.indexOf("where"), ORIGINAL.indexOf("where"));
  firstLoad.release();
  const FORMATTED = "SELECT\n  a,\n  b\nFROM\n  orders o\nWHERE\n  o.status = 'paid'\n  AND o.total > 10\nORDER BY\n  a";
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, FORMATTED);
  await until(async () => /^Formatted 1 statement\. (⌘Z|Ctrl\+Z) undoes it\.$/.test(await feedback()), "Format SQL says what it did");
  // The caret keeps its latest place: still before WHERE.
  assert.equal(await page.$eval(EDITOR, (e) => e.value.slice(e.selectionStart, e.selectionStart + 5)), "WHERE");
  // One undo step brings the original text back.
  await page.$eval(EDITOR, (e) => { e.focus(); document.execCommand("undo"); });
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, ORIGINAL);
  await page.$eval(EDITOR, (e) => document.execCommand("redo"));
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, FORMATTED);

  // 9. Shift+Option+F formats only the selection and keeps it selected.
  const SCRIPT = "select 1;\nselect a,b from t where c=1;\nselect 3";
  await setText(SCRIPT);
  const from = SCRIPT.indexOf("select a");
  const to = SCRIPT.indexOf(";", from);
  await select(from, to);
  await page.keyboard.down("Shift");
  await page.keyboard.down("Alt");
  await page.keyboard.press("KeyF");
  await page.keyboard.up("Alt");
  await page.keyboard.up("Shift");
  const PART = "SELECT\n  a,\n  b\nFROM\n  t\nWHERE\n  c = 1";
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, `select 1;\n${PART};\nselect 3`);
  assert.deepEqual(await page.$eval(EDITOR, (e) => [e.selectionStart, e.selectionEnd]), [from, from + PART.length], "the selection covers the formatted text");
  await until(async () => /^Formatted the selection\./.test(await feedback()), "the selection is formatted");
  await page.$eval(EDITOR, (e) => { e.focus(); document.execCommand("undo"); });
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, SCRIPT);

  // 10. A change of meaning is refused: nothing changes, the message says where.
  const RISKY = "select 1;\nselect count (*) from t";
  await setText(RISKY);
  await select(0, 0);
  await page.click(FORMAT);
  await until(async () => (await feedback()).startsWith("Format SQL changed nothing"), "the refusal is shown");
  assert.equal(await feedback(), "Format SQL changed nothing: formatting would have changed “count (*)” to “count(*)” on line 2, which the server can read differently.");
  assert.equal(await page.$eval(FEEDBACK, (e) => e.classList.contains("is-error")), true, "the refusal reads as an error");
  assert.equal(await sql(), RISKY, "the text is unchanged");
  // A selection inside a string is refused: alone it would look like SQL.
  const QUOTED = "select 'a,b from t'";
  await setText(QUOTED);
  await select(QUOTED.indexOf("a,b"), QUOTED.length - 1);
  await page.click(FORMAT);
  await until(async () => (await feedback()) === "Format SQL changed nothing: the selection starts inside a quoted string on line 1. Select whole statements or clauses, or nothing to format the whole tab.", "a selection inside a string is refused");
  assert.equal(await sql(), QUOTED, "the string is unchanged");

  // 11. A DELIMITER block stays as written; the rest is formatted.
  const PROGRAM = "DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n  select 1;\nEND//\nDELIMITER ;\nselect x from t;";
  await setText(PROGRAM);
  await select(0, 0);
  await page.click(FORMAT);
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, "DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n  select 1;\nEND//\nDELIMITER ;\nSELECT\n  x\nFROM\n  t;");
  await until(async () => (await feedback()).startsWith("Formatted 1 statement; left line 2 as written (inside a DELIMITER block)."), "kept statements are named");
  // Already formatted: said, and no edit.
  await page.click(FORMAT);
  await until(async () => (await feedback()) === "Already formatted: nothing changed.", "a second Format changes nothing");
  assert.equal(calls("/api/editor/run").length, runsBefore, "formatting never runs SQL");

  // 11b. A selection changed while the formatter loads (its first use after a
  // reload) changes what to format: nothing changes, and the message says so.
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");
  const MOVED = "select a,b from t;\nselect c,d from u";
  await setText(MOVED);
  await select(0, MOVED.indexOf(";"));
  const reload = holdFormatter();
  await page.click(FORMAT);
  await reload.seen;
  assert.equal(await feedback(), "Formatting the selection…");
  await select(MOVED.indexOf("select c"), MOVED.length);
  reload.release();
  await until(async () => (await feedback()) === "The selection changed while Format SQL was starting, so nothing was changed. Try again.", "a selection changed meanwhile is refused");
  assert.equal(await page.$eval(FEEDBACK, (e) => e.classList.contains("is-error")), true);
  assert.equal(await sql(), MOVED, "the text is unchanged");
  assert.deepEqual(await page.$eval(EDITOR, (e) => [e.selectionStart, e.selectionEnd]), [MOVED.indexOf("select c"), MOVED.length], "the new selection stays");
  // Format SQL again acts on the selection now made.
  await page.click(FORMAT);
  await page.waitForFunction((v) => document.querySelector('[aria-label="SQL editor"]').value === v, {}, "select a,b from t;\nSELECT\n  c,\n  d\nFROM\n  u");
  assert.equal(calls("/api/editor/run").length, runsBefore, "formatting never runs SQL");

  // 12. The palette: Format SQL and Rename query tab, from another place
  // (and from the Query digest view, which switches back to the editor).
  await setText("select 1 from dual");
  const digest = await page.$('.query-mode-switch [role=tab] ::-p-text(Query digest)');
  if (digest) {
    await digest.click();
    await page.waitForFunction(() => document.getElementById("query-tab-panel")?.hidden);
  }
  await page.locator(".main-nav button ::-p-text(Home)").click();
  await page.keyboard.down(MOD); await page.keyboard.press("k"); await page.keyboard.up(MOD);
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "format sql");
  const firstCommand = await page.$eval("dialog.command-palette [role=option]", (e) => e.textContent);
  assert.match(firstCommand, /Format SQL/);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector('[aria-label="SQL editor"]')?.value === "SELECT\n  1\nFROM\n  DUAL");
  assert.equal(await page.$eval(".query-lab", (e) => !e.closest("[hidden]")), true, "the palette opens the Query editor");
  await page.$eval(EDITOR, (e) => { e.focus(); document.execCommand("undo"); });
  await page.waitForFunction(() => document.querySelector('[aria-label="SQL editor"]').value === "select 1 from dual");
  await page.keyboard.down(MOD); await page.keyboard.press("k"); await page.keyboard.up(MOD);
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "rename query tab");
  await page.keyboard.press("Enter");
  await page.waitForSelector('[aria-label="Query name"]');
  await until(() => active('[aria-label="Query name"]'), "the palette's Rename query tab focuses the name field");
  await page.keyboard.type("Renamed from the palette");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".query-tablist [role=tab][aria-selected=true] .query-tab-label")?.textContent === "Renamed from the palette");

  // 12b. Tab names are cut without splitting an emoji (review finding: a
  // lone half of it is invalid text). Duplicate makes room for " copy".
  await page.click('button[aria-label="New query tab"]');
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 2);
  await page.focus(ACTIVE_TAB);
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  const EMOJI = `${"a".repeat(58)}\u{1F600}`;
  await page.keyboard.type(EMOJI);
  await page.keyboard.press("Enter");
  await page.waitForFunction((n) => document.querySelector(".query-tablist [role=tab][aria-selected=true] .query-tab-label")?.textContent === n, {}, EMOJI);
  await until(() => active(ACTIVE_TAB), "Enter returns focus to the tab");
  await page.keyboard.down("Shift");
  await page.keyboard.press("F10");
  await page.keyboard.up("Shift");
  await page.waitForSelector(MENU);
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 3);
  const copied = await text(`${ACTIVE_TAB} .query-tab-label`);
  assert.equal(copied, `${"a".repeat(58)} copy`, "the copy's name drops the whole emoji");
  assert.ok(!broken(copied), "the copy's name is valid text");
  // A name longer than the field allows (a converted tab's "NAME SELECT", an
  // older session) is cut on save without splitting the emoji at the edge.
  await until(() => active(ACTIVE_TAB), "focus moves to the copy's tab");
  await page.keyboard.press("F2");
  await page.waitForSelector('[aria-label="Query name"]');
  await page.$eval('[aria-label="Query name"]', (el, v) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, `${"b".repeat(63)}\u{1F600}tail`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector('[aria-label="Query name"]'));
  const cut = await text(`${ACTIVE_TAB} .query-tab-label`);
  assert.equal(cut, "b".repeat(63), "the name is cut before the emoji");
  assert.ok(!broken(cut), "the saved name is valid text");
  await openMenuByClick("Renamed from the palette");
  await page.click(`${MENU} [role=menuitem]:nth-of-type(4)`);
  await page.waitForFunction(() => document.querySelectorAll(".query-tablist [role=tab]").length === 1);
  assert.deepEqual(await tabNames(), ["Renamed from the palette"]);

  // 13. Screenshots and layout at 1440x900 and the 720x520 minimum, light and dark.
  await setText("select o.id, o.total, c.name from orders o join customers c on c.id = o.customer_id where o.status = 'paid' order by o.total desc limit 20");
  await select(0, 0);
  for (const [width, height] of [[1440, 900], [720, 520]]) {
    await page.setViewport({ width, height });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await pause(50);
      const toolbar = await page.$eval(FORMAT, (e) => { const r = e.getBoundingClientRect(); return { visible: r.width > 0 && r.right <= innerWidth && r.bottom <= innerHeight, w: r.width }; });
      assert.ok(toolbar.visible, `Format SQL is on screen at ${width} px: ${JSON.stringify(toolbar)}`);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert.ok(overflow <= 0, `no horizontal page scroll at ${width} px (${overflow})`);
      await openMenuByClick("Renamed from the palette");
      const box = await page.$eval(MENU, (e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, w: innerWidth, h: innerHeight }; });
      assert.ok(box.left >= 0 && box.right <= box.w && box.bottom <= box.h, `the menu fits at ${width} px: ${JSON.stringify(box)}`);
      if (shots) {
        await fs.mkdir(shots, { recursive: true });
        await pause(150);
        await page.screenshot({ path: path.join(shots, `editor-tab-menu-${width}x${height}-${scheme}.png`) });
      }
      await page.keyboard.press("Escape");
      await page.waitForFunction((m) => !document.querySelector(m), {}, MENU);
      if (shots) {
        await page.click(FORMAT);
        await until(async () => /^Formatted|^Already formatted/.test(await feedback()), "formatted for the screenshot");
        await pause(150);
        await page.screenshot({ path: path.join(shots, `editor-format-${width}x${height}-${scheme}.png`) });
      }
    }
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

  assert.deepEqual(errors, [], "no page errors");
  console.log("Query editor tools: tab menu (right-click, Shift+F10, context-menu key), Duplicate, Rename… with validation and reload, Close and Close others, Format SQL (button, Shift+Option+F, palette, selection, undo, refusal, DELIMITER block, already formatted, caret moved and selection changed while the formatter loads), names cut without splitting an emoji, palette Rename query tab, and 1440/720 px layouts in light and dark passed.");
} finally {
  await browser?.close();
  server.kill();
  await once(server, "exit").catch(() => {});
  await fs.rm(directory, { recursive: true, force: true });
}
