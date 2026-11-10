// Find and replace in the Query editor: ⌘F / ⌥⌘F (Ctrl+F / Ctrl+H) opens the
// bar, the count ("3 of 12"), Enter and Shift+Enter, ⌘G and ⇧⌘G, wrap, the
// Match case, Whole word and Regular expression toggles, an invalid pattern
// shown inline (never thrown), a one-line selection prefilling the field,
// match marks in the CodeMirror content with the current one stronger,
// scrolling to the current match (marks stay capped at FIND_MARK_LIMIT on a
// 1 MB script), Replace and Replace all as single undo steps, Escape returning
// focus to the editor at the current match, hit targets, and the bar fitting
// at 720 px in light and dark. The packaged page and the real Python server;
// the editor API is mocked and nothing may run. The find helpers themselves
// (Unicode offsets, the regex worker timeout, planReplace anchor semantics)
// are unit-tested against the real modules in test/editor-find-unit.mjs, so
// this suite keeps only browser behavior. Run after `npm run build`:
// node test/editor-find.mjs (EDITOR_FIND_SHOTS=<dir> also writes screenshots).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import {
  EDITOR,
  editorSelection,
  editorUndo,
  selectEditor,
  setEditorText,
  waitForEditorText,
} from "./editor-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-editor-find-"));
const shots = process.env.EDITOR_FIND_SHOTS || "";
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

const BAR = '.sql-find[role="search"]';
const FIND = '[aria-label="Find"]';
const REPLACE = '[aria-label="Replace with"]';
const COUNT = ".sql-find-count";
const MARK = ".cm-content .sql-find-mark";
const ERROR = ".sql-find-error";
const SCROLLER = ".cm-scroller";

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
  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [] });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/editor/") && p !== "/api/live/privileges") {
      if (p.startsWith("/api/live/") || p.startsWith("/api/connection/")) {
        return request.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
      }
      return request.continue();
    }
    const answer = reply(p);
    if (answer === undefined) return request.respond({ status: 500, contentType: "application/json", body: '{"error":"no mock"}' });
    request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const mac = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent));
  const MOD = mac ? "Meta" : "Control";
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(`.query-lab ${EDITOR}`, { visible: true });

  const sql = () => page.$eval(EDITOR, (e) => e.value);
  const active = (selector) => page.$eval(selector, (e) => e === document.activeElement).catch(() => false);
  const count = () => page.$eval(COUNT, (e) => e.textContent);
  const selection = async () => {
    const s = await editorSelection(page);
    return [s.start, s.end];
  };
  async function until(check, message, timeout = 8000) {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const value = await check();
      if (value) return value;
      await pause(25);
    }
    assert.fail(message);
  }
  /** Replace the editor's text the way a paste does (one undo step), so the
   * tab's state holds it too. Fast even for a 1 MB script. */
  const setText = (value) => setEditorText(page, value);
  const select = (start, end) => selectEditor(page, start, end);
  /** Replace the find field's content the way typing does. */
  async function setFind(query) {
    await page.$eval(FIND, (el) => {
      el.focus();
      el.select();
    });
    if (query) await page.keyboard.type(query);
    else await page.keyboard.press("Backspace");
  }
  async function openFind(replace = false) {
    if (replace && !mac) {
      await page.keyboard.down("Control");
      await page.keyboard.press("KeyH");
      await page.keyboard.up("Control");
    } else if (replace) {
      await page.keyboard.down("Meta");
      await page.keyboard.down("Alt");
      await page.keyboard.press("KeyF");
      await page.keyboard.up("Alt");
      await page.keyboard.up("Meta");
    } else {
      await page.keyboard.down(MOD);
      await page.keyboard.press("KeyF");
      await page.keyboard.up(MOD);
    }
    await page.waitForSelector(BAR);
    await until(() => active(FIND), "the find field is focused");
  }
  const undo = () => editorUndo(page);

  // 1. ⌘F opens the find bar docked in the editor; the field is focused and
  // the count is a live region.
  await setText("select 1;\nSELECT 2;\nselect 3");
  await openFind();
  assert.equal(await page.$eval(BAR, (e) => e.getAttribute("role")), "search");
  const live = await page.$eval(COUNT, (e) => ({ role: e.getAttribute("role"), live: e.getAttribute("aria-live") }));
  assert.deepEqual(live, { role: "status", live: "polite" }, "the count is a polite live region");
  assert.equal(await count(), "", "no query yet");

  // 2. Typing finds case-insensitively; the first match is selected; marks
  // land in the CodeMirror content with the current one stronger.
  await page.keyboard.type("select");
  await until(async () => (await count()) === "1 of 3", "count 1 of 3");
  assert.deepEqual(await selection(), [0, 6], "the first match is selected");
  await until(async () => (await page.$$eval(MARK, (els) => els.length)) === 3, "three marks");
  const current = await page.$eval(".sql-find-current", (e) => e.textContent);
  assert.equal(current, "select", "the current mark covers the match");

  // 3. Enter and Shift+Enter move with wrap; ⌘G and ⇧⌘G too.
  await page.keyboard.press("Enter");
  await until(async () => (await count()) === "2 of 3", "Enter moves to 2 of 3");
  assert.deepEqual(await selection(), [10, 16], "the second match is selected");
  await page.keyboard.press("Enter");
  await until(async () => (await count()) === "3 of 3", "3 of 3");
  await page.keyboard.press("Enter");
  await until(async () => (await count()) === "1 of 3", "Enter wraps to 1 of 3");
  await page.keyboard.down("Shift");
  await page.keyboard.press("Enter");
  await page.keyboard.up("Shift");
  await until(async () => (await count()) === "3 of 3", "Shift+Enter wraps back");
  await page.keyboard.down(MOD);
  await page.keyboard.press("KeyG");
  await page.keyboard.up(MOD);
  await until(async () => (await count()) === "1 of 3", "⌘G moves to the next match");
  await page.keyboard.down(MOD);
  await page.keyboard.down("Shift");
  await page.keyboard.press("KeyG");
  await page.keyboard.up("Shift");
  await page.keyboard.up(MOD);
  await until(async () => (await count()) === "3 of 3", "⇧⌘G moves back");

  // 4. Match case: "select" now misses the uppercase SELECT (the current
  // match stays on the one at the caret).
  await page.click('[aria-label="Match case"]');
  assert.equal(await page.$eval('[aria-label="Match case"]', (e) => e.getAttribute("aria-pressed")), "true");
  await until(async () => /of 2$/.test(await count()), "Match case finds the two lowercase selects");
  await page.click('[aria-label="Match case"]');

  // 5. Whole word.
  await setText("select in inside tint");
  await setFind("in");
  await until(async () => (await count()) === "1 of 3", 'three runs of "in"');
  await page.click('[aria-label="Whole word"]');
  await until(async () => (await count()) === "1 of 1", 'only the whole word "in"');
  assert.deepEqual(await selection(), [7, 9], "the whole word is selected");
  await page.click('[aria-label="Whole word"]');

  // 6. Regular expression: \d+ finds the numbers; an invalid pattern shows an
  // inline error, marks the field, and throws nothing.
  await setText("a1 b22 c333");
  await page.click('[aria-label="Regular expression"]');
  await setFind("\\d+");
  await until(async () => (await count()) === "1 of 3", "\\d+ finds three numbers");
  await setFind("[");
  await page.waitForSelector(`${ERROR}:not([hidden])`);
  const invalid = await page.$eval(FIND, (e) => ({ invalid: e.getAttribute("aria-invalid"), by: e.getAttribute("aria-describedby") }));
  assert.equal(invalid.invalid, "true", "the field is marked invalid");
  assert.ok(invalid.by, "the field points at the error");
  const message = await page.$eval(ERROR, (e) => e.textContent);
  assert.ok(message.length > 0, "the error says what is wrong");
  assert.equal(await count(), "", "no count while the pattern is invalid");
  assert.equal(await page.$$eval(MARK, (els) => els.length), 0, "no marks while the pattern is invalid");
  // Pathological regex runs in a terminable Web Worker and times out with an
  // inline error while keeping the editor responsive.
  await setText(`${"a".repeat(35)}!`);
  const slowStart = Date.now();
  await setFind("(a+)+$");
  await until(
    async () => (await page.$eval(ERROR, (e) => (!e.hidden ? e.textContent : ""))) === "Pattern too slow to search",
    "a pathological regex times out in the worker with an inline error",
    1500,
  );
  const slowElapsed = Date.now() - slowStart;
  assert.ok(slowElapsed < 1200, `pathological regex timed out without freezing the UI (${slowElapsed} ms)`);
  assert.equal(await page.$eval(FIND, (e) => e.getAttribute("aria-invalid")), "true", "the field is marked invalid on timeout");
  assert.equal(await count(), "", "no count after a regex timeout");
  assert.equal(await page.$$eval(MARK, (els) => els.length), 0, "no marks after a regex timeout");
  await setFind("a+");
  await until(async () => (await count()) === "1 of 1", "a fast regex works after terminating a slow one");
  await page.click('[aria-label="Regular expression"]');

  // 7. A one-line selection prefills the field; a multi-line one does not.
  await setFind("nomatchhere");
  await until(async () => (await count()) === "No matches", "no matches is said");
  await page.keyboard.press("Escape");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);
  await setText("select a;\nselect b;\nselect c");
  await select(10, 16);
  await openFind();
  assert.equal(await page.$eval(FIND, (e) => e.value), "select", "the selection prefills the field");
  await until(async () => (await count()) === "2 of 3", "the prefilled match under the selection is current");
  await page.keyboard.press("Escape");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);
  await select(0, 12);
  await openFind();
  assert.equal(await page.$eval(FIND, (e) => e.value), "select", "a multi-line selection does not prefill");
  await page.keyboard.press("Escape");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);

  // 8. ⌥⌘F (Ctrl+H) opens the bar with the Replace row. With Match case on,
  // Replace changes one match, moves to the next, and is one undo step.
  await openFind(true);
  await page.waitForSelector(REPLACE);
  assert.equal(await page.$eval(FIND, (e) => e.value), "select", "the query survives reopening");
  await page.click('[aria-label="Match case"]');
  await page.click(REPLACE);
  await page.keyboard.type("SELECT");
  // Toggling the option clears the matches until the debounced search lands;
  // the Replace button is disabled while the count is empty.
  await until(async () => (await count()) === "1 of 3", "the Match case search settled");
  await page.click(`${BAR} .sql-find-action`);
  await waitForEditorText(page, "SELECT a;\nselect b;\nselect c");
  await until(async () => (await count()) === "1 of 2", "Replace moves to the next match");
  assert.deepEqual(await selection(), [10, 16], "the next match is selected");
  await undo();
  await waitForEditorText(page, "select a;\nselect b;\nselect c");

  // 9. Replace all changes every match as one undo step.
  await until(async () => (await count()) === "1 of 3", "the undo is reflected in the count");
  await page.click(`${BAR} .sql-find-action ~ .sql-find-action`);
  await waitForEditorText(page, "SELECT a;\nSELECT b;\nSELECT c");
  await until(async () => (await count()) === "No matches", "nothing left to find");
  await undo();
  await waitForEditorText(page, "select a;\nselect b;\nselect c");

  // 10. Escape returns focus to the editor at the current match; the close
  // button does the same.
  await until(async () => (await count()) === "1 of 3", "matches are back");
  await page.click(FIND);
  await page.keyboard.press("Enter");
  await until(async () => (await count()) === "2 of 3", "on the second match");
  await page.keyboard.press("Escape");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);
  await until(() => active(EDITOR), "Escape returns focus to the editor");
  assert.deepEqual(await selection(), [10, 16], "the caret is at the current match");
  await openFind();
  await page.click(".sql-find-close");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);
  await until(() => active(EDITOR), "the close button returns focus to the editor");

  // 10b. Unicode case-insensitive search and Replace preserve original offsets
  // (lowercasing "İ" expands to two code units), and Replace advances past
  // inserted text when the replacement contains the search term.
  await setText("İfoo and İFOO");
  await select(0, 0);
  await openFind(true);
  await page.click('[aria-label="Match case"]');
  assert.equal(await page.$eval('[aria-label="Match case"]', (e) => e.getAttribute("aria-pressed")), "false");
  await setFind("foo");
  await until(async () => (await count()) === "1 of 2", "case-insensitive search in İfoo finds both matches");
  assert.deepEqual(await selection(), [1, 4], "the first match in İfoo is [1, 4], not shifted to [2, 5]");
  assert.equal(await page.$eval(".sql-find-current", (e) => e.textContent), "foo", "the current mark covers foo");
  await page.$eval(REPLACE, (el) => {
    el.focus();
    el.select();
  });
  await page.keyboard.type("bar");
  await page.click(`${BAR} .sql-find-action`);
  await waitForEditorText(page, "İbar and İFOO");
  await until(async () => (await count()) === "1 of 1", "Replace in İfoo moves to the remaining match");
  assert.deepEqual(await selection(), [10, 13], "the second match in İbar and İFOO is selected");
  await page.click(`${BAR} .sql-find-action`);
  await waitForEditorText(page, "İbar and İbar");
  await until(async () => (await count()) === "No matches", "both Unicode matches were replaced");

  await setText("foo foo");
  await select(0, 0);
  await setFind("foo");
  await until(async () => (await count()) === "1 of 2", "two occurrences of foo");
  await page.$eval(REPLACE, (el) => {
    el.focus();
    el.select();
  });
  await page.keyboard.type("foobar");
  await page.click(`${BAR} .sql-find-action`);
  await waitForEditorText(page, "foobar foo");
  await until(async () => (await count()) === "2 of 2", "Replace advances past the inserted replacement text");
  assert.deepEqual(await selection(), [7, 10], "the second foo is selected");
  await page.click(`${BAR} .sql-find-action`);
  await waitForEditorText(page, "foobar foobar");
  await page.keyboard.press("Escape");
  await page.waitForFunction((b) => !document.querySelector(b), {}, BAR);

  // 11. The current match scrolls into view. Marks are capped at
  // FIND_MARK_LIMIT (1000) whatever the document size, and the current match
  // is always drawn, even past the syntax highlight limit on a 1 MB script.
  const tall = Array.from({ length: 400 }, (_, i) => `select ${i};`).join("\n") + "\nselect needle_from_nowhere;";
  await setText(tall);
  await select(0, 0);
  await openFind();
  await setFind("needle_from_nowhere");
  await until(async () => (await count()) === "1 of 1", "the needle is found");
  await until(async () => page.$eval(SCROLLER, (e) => e.scrollTop > 0), "the editor scrolled to the match");
  const huge = "select x from t;\n".repeat(65000) + "select zztargetzz;";
  assert.ok(huge.length > 1_000_000, "about 1 MB");
  await setText(huge);
  await select(0, 0);
  const searchStart = Date.now();
  await setFind("zztargetzz");
  await until(async () => (await count()) === "1 of 1", "find stays responsive on a 1 MB script", 2000);
  const searchElapsed = Date.now() - searchStart;
  assert.ok(searchElapsed < 1500, `searching a 1 MB script stayed responsive (${searchElapsed} ms)`);
  await until(async () => page.$eval(SCROLLER, (e) => e.scrollTop > 0), "the 1 MB script scrolled to the match");
  // CodeMirror renders marks only for the visible viewport; after the scroll
  // the current match's mark is there even past the syntax highlight limit.
  await until(async () => (await page.$$eval(MARK, (els) => els.length)) === 1, "the current match is marked even on a 1 MB script");
  await setFind("select");
  await until(async () => (await count()) === "1 of 65001", "the 1 MB script counts every match");
  const markCount = await page.$$eval(MARK, (els) => els.length);
  assert.ok(markCount > 0 && markCount <= 1000, `marks are capped at FIND_MARK_LIMIT on a 1 MB script (${markCount} drawn)`);

  // 12. Hit targets and the layout at the 720 px minimum, light and dark.
  await setText("select 1;\nselect 2");
  await select(0, 0);
  await openFind(true);
  await page.keyboard.type("x");
  await until(async () => (await count()) === "No matches", "the query applies");
  for (const [width, height] of [[1440, 900], [720, 520]]) {
    await page.setViewport({ width, height });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await pause(80);
      const box = await page.$eval(BAR, (e) => { const r = e.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, w: innerWidth, h: innerHeight }; });
      assert.ok(box.left >= 0 && box.right <= box.w && box.bottom <= box.h && box.top >= 0, `the find bar fits at ${width} px ${scheme}: ${JSON.stringify(box)}`);
      const targets = await page.$$eval(`${BAR} button`, (els) => els.map((e) => { const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
      assert.ok(targets.every(([w, h]) => w >= 24 && h >= 24), `every button is at least 24x24 at ${width} px: ${JSON.stringify(targets)}`);
      if (shots) {
        await fs.mkdir(shots, { recursive: true });
        await page.screenshot({ path: path.join(shots, `editor-find-${width}x${height}-${scheme}.png`) });
      }
    }
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

  assert.deepEqual(errors, [], "no page errors (the invalid pattern never throws)");
  console.log("Editor find and replace: ⌘F / ⌥⌘F (Ctrl+F / Ctrl+H), prefill from a one-line selection, count, Enter / Shift+Enter and ⌘G / ⇧⌘G with wrap, Match case, Whole word, Regular expression with an inline error, marks with a stronger current match, scroll into view (marks capped at FIND_MARK_LIMIT on a 1 MB script), Replace and Replace all as single undo steps, Escape and the close button returning focus to the editor, hit targets, and 1440/720 px layouts in light and dark passed.");
} finally {
  await browser?.close();
  server.kill();
  await once(server, "exit").catch(() => {});
  await fs.rm(directory, { recursive: true, force: true });
}
