// Edit-menu bridge in the built workspace. Puppeteer answers every request;
// no HTTP server, database, Electron process, or real workspace store is used.
// Run after npm run build: node test/edit-menu.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { EDITOR, assertEditorText, selectEditor, waitForEditorText } from "./editor-helpers.mjs";

const assets = fileURLToPath(new URL("../../../myflames/ui_assets/", import.meta.url));
const origin = "http://myflames.test";
const errors = [], unexpected = [];
const json = body => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
const mocks = {
  "/api/auth/session": { token: "edit-menu-test" },
  "/api/session/load": { revision: 0, restore: false, skip_restore: false, state: null, saved_at: null },
  "/api/profiles/list": { items: [] },
  "/api/news/settings": { enabled: false },
  "/api/settings/editor": { settings: { completion_auto: false, hover_info: false } },
  "/api/investigations/list": { items: [], next_cursor: null },
  "/api/editor/status": { status: "none", session: null, run: null },
  "/api/editor/history": { items: [] },
  "/api/editor/saved/list": { items: [] },
  "/api/live/schemas": { schema_version: "schemas-1.0", engine: "mysql", schemas: [], system: [], user_tables: 0 },
  "/api/live/catalog": { schema_version: "catalog-1.0", level: "tables", tables: [], truncated: false },
};

let browser;
try {
  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [] });
  const page = await browser.newPage();
  page.setDefaultTimeout(8000);
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => {
    if (message.type() === "error" || message.type() === "warn") errors.push(message.text());
  });
  page.on("requestfailed", request => errors.push(`${request.url()}: ${request.failure()?.errorText}`));
  await page.setRequestInterception(true);
  page.on("request", request => void (async () => {
    const url = new URL(request.url());
    if (url.origin !== origin) {
      unexpected.push(request.url());
      return request.respond({ status: 403, body: "Unexpected origin" });
    }
    if (Object.hasOwn(mocks, url.pathname)) return request.respond(json(mocks[url.pathname]));
    // Only generated assets with flat filenames, matching ui.py's asset route.
    const filename = url.pathname === "/" ? "index.html"
      : /^\/assets\/[\w.-]+$/.test(url.pathname) ? url.pathname.slice(1) : null;
    if (!filename) {
      unexpected.push(url.pathname);
      return request.respond({ status: 404, body: "No mock" });
    }
    const contentType = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml" }[path.extname(filename)] || "application/octet-stream";
    return request.respond({ status: 200, contentType, body: await fs.readFile(path.join(assets, filename)) });
  })().catch(error => { errors.push(String(error)); void request.abort().catch(() => {}); }));

  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(origin, { waitUntil: "networkidle0" });
  await page.locator('.main-nav button ::-p-text(Query editor)').click();
  await page.waitForSelector(EDITOR, { visible: true });
  assert.equal(await page.evaluate(() => typeof window.__myflamesEditCommand), "function", "editor installs the bridge");

  // Empty history must be consumed too, rather than reaching native history.
  await page.$eval(EDITOR, element => element.focus());
  const initial = await page.$eval(EDITOR, element => element.value);
  assert.equal(await page.evaluate(() => window.__myflamesEditCommand("undo")), true);
  assert.equal(await page.evaluate(() => window.__myflamesEditCommand("redo")), true);
  await assertEditorText(page, initial, "empty history is handled without changing the document");

  await selectEditor(page, 0, initial.length);
  const sql = "select id,name from users where id=1;";
  await page.keyboard.type(sql);
  await waitForEditorText(page, sql);
  await selectEditor(page, sql.length);
  await page.click('[data-action="format"]');
  await page.waitForFunction((selector, before) => document.querySelector(selector).value !== before, {}, EDITOR, sql);
  const formatted = await page.$eval(EDITOR, element => element.value);
  assert.match(formatted, /\n/, "Format SQL produces an editor-only transaction");
  await page.$eval(EDITOR, element => element.focus());
  assert.equal(await page.evaluate(() => window.__myflamesEditCommand("undo")), true);
  await waitForEditorText(page, sql);
  assert.equal(await page.evaluate(() => window.__myflamesEditCommand("redo")), true);
  await waitForEditorText(page, formatted);
  console.log("PASS typed SQL, Format SQL, menu bridge Undo and Redo");

  // Neither arbitrary property names nor source-like strings are commands.
  assert.deepEqual(await page.evaluate(() => {
    const before = document.querySelector(".sql-editor").value;
    const refused = ["cut", "constructor", "__proto__", "UNDO", "undo(); document.body.remove()", null, {}, ["undo"]]
      .map(command => window.__myflamesEditCommand(command));
    return { refused, unchanged: document.querySelector(".sql-editor").value === before };
  }), { refused: Array(8).fill(false), unchanged: true });
  console.log("PASS unknown command names and non-string inputs refused");

  await page.click(".command-trigger");
  await page.waitForSelector('dialog.command-palette[open] input', { visible: true });
  // One character keeps the palette below its two-character object-search
  // threshold, so this scenario needs no unrelated catalog search.
  await page.type('dialog.command-palette input', "x");
  assert.deepEqual(await page.evaluate(() => {
    const input = document.querySelector("dialog.command-palette input");
    const before = input.value;
    return {
      focused: document.activeElement === input,
      undo: window.__myflamesEditCommand("undo"),
      redo: window.__myflamesEditCommand("redo"),
      unchanged: input.value === before,
    };
  }), { focused: true, undo: false, redo: false, unchanged: true });
  await assertEditorText(page, formatted, "plain input focus leaves the editor untouched");
  console.log("PASS ordinary input declines the bridge and retains its text");
  assert.deepEqual(unexpected, [], "every request is local and explicitly mocked");
  assert.deepEqual(errors, [], "no browser errors, warnings, or failed requests");
} finally {
  await browser?.close();
}
