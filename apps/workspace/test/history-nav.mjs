// Window navigation history and the command palette's object search: Back and
// Forward (⌘[ / ⌘]) through places and Server/Workload views, the cross-link
// chip in the top bar, ⌘K with recent views first then matching objects
// (cached catalog, then the bounded server search), a table revealed in the
// Query editor's schema browser, a routine's SHOW CREATE in a new tab, the
// schema browser's Routines/Triggers/Events folders with Show CREATE, and the
// other-connection notice when history crosses connections.
// Mocked profile/catalog/search APIs, real Python server for the page.
// Run after `npm run build`: node test/history-nav.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { chooseView, currentView, openConnectionPicker } from "./chrome-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-history-nav-"));
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
const items = [
  {
    id: "p2", name: "Staging", host: "db.staging", port: 3307, user: "dev",
    database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development", read_only: false,
    has_saved_password: false, secret_backend: "", secret_backend_label: "",
  },
];
items.push({ ...items[0], id: "p3", name: "Other", host: "db.other" });
const objectsReply = (body, objects) => ({
  schema_version: "catalog-1.0", level: "objects", schema: body.schema, group: body.group,
  objects, truncated: false, limit: 200,
});
const tableNames = ["orders", "v_orders", ...Array.from({ length: 205 }, (_, i) => `t${String(i).padStart(3, "0")}`), "z_target"].sort();
const seen = {};
const handlers = {
  "/api/profiles/list": () => ({ items, secret_store: store }),
  "/api/live/schemas": () => ({
    schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.3",
    schemas: [
      { name: "orders_archive", tables: 1, system: false },
      { name: "shop", tables: 2, system: false },
    ],
    user_tables: 3, all_limit: 2000, all_note: "", system: ["mysql", "sys", "performance_schema", "information_schema"],
  }),
  "/api/live/catalog": (body) => {
    if (body.group === "routines")
      return objectsReply(body, [{ name: body.connection.host === "db.staging" ? "p_top_staging" : "p_top", kind: "procedure" }, { name: "f_total", kind: "function" }]);
    if (body.group === "triggers")
      return objectsReply(body, [{ name: "orders_bi", table: "orders", event: "insert", timing: "before" }]);
    if (body.group === "events")
      return objectsReply(body, [{ name: "e_nightly", status: "enabled" }]);
    if (body.table)
      return {
        schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table,
        columns: [{ name: "id", type: "bigint", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false }],
        indexes: [],
      };
    const names = tableNames.filter((name) => (!body.prefix || name.startsWith(body.prefix)) && (!body.after || name > body.after));
    const page = names.slice(0, 200);
    return {
      schema_version: "catalog-1.0", level: "tables", schema: body.schema,
      tables: page.map((name) => ({ name, type: name.startsWith("v_") ? "view" : "table" })),
      truncated: names.length > 200, limit: 200, prefix: body.prefix ?? null, after: body.after ?? null,
      next_after: names.length > 200 ? page.at(-1) : null,
    };
  },
  "/api/live/object-search": (body) => {
    const text = (body.text || "").toLowerCase();
    const all = [
      { kind: "table", schema: "shop", name: "orders" },
      { kind: "table", schema: "shop", name: "z_target" },
      { kind: "table", schema: "shop", name: "gone_table" },
      { kind: "procedure", schema: "shop", name: "p_orders" },
      { kind: "trigger", schema: "shop", name: "orders_bi" },
      { kind: "event", schema: "shop", name: "e_orders" },
    ];
    return {
      schema_version: "object-search-1.0", text: body.text,
      objects: all.filter((o) => o.name.includes(text)), truncated: false, limit: 50,
    };
  },
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
    const body = JSON.parse(request.postData() || "{}");
    (seen[pathname] = seen[pathname] || []).push(body);
    if (!handler) {
      // This fixture never connects to a real database, including readiness
      // checks and the schema browser's existing Show CREATE action.
      if (pathname.startsWith("/api/live/") || pathname.startsWith("/api/connection/") ||
          pathname === "/api/editor/run" || pathname === "/api/editor/open")
        return request.respond({ status: 400, contentType: "application/json",
          body: JSON.stringify({ error: "No database connection in the navigation fixture." }) });
      return request.continue();
    }
    request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(handler(body)) });
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const go = (label) => page.locator(`.main-nav button ::-p-text(${label})`).click();
  const active = () => page.$$eval(".main-nav button.active", (els) => els.map((e) => e.textContent));
  const back = async () => { await page.keyboard.down("Meta"); await page.keyboard.press("["); await page.keyboard.up("Meta"); };
  const forward = async () => { await page.keyboard.down("Meta"); await page.keyboard.press("]"); await page.keyboard.up("Meta"); };
  const palette = async () => {
    await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
    await page.waitForSelector("dialog.command-palette[open]");
  };
  const options = () => page.$$eval(".command-option", (els) => els.map((e) => [e.querySelector("span")?.textContent, e.querySelector("small")?.textContent]));

  // 1. Back and Forward through places and views.
  await go("Server");
  await page.waitForSelector('.server-view[data-view="health"]', { visible: true });
  await chooseView(page, "indexes");
  await go("Workload");
  await chooseView(page, "top");
  await page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  assert.equal(await page.$(".nav-back-chip"), null, "plain navigation shows no chip");
  await back();
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='indexes']")?.offsetParent));
  assert.deepEqual(await active(), ["Server"], "back returns to Server");
  assert.equal(await currentView(page), "indexes", "back returns to the Indexes view");
  await back();
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='health']")?.offsetParent));
  assert.equal(await currentView(page), "health");
  await back();
  await page.waitForSelector(".home .welcome, .home [aria-label='Get started']", { visible: true });
  assert.deepEqual(await active(), ["Home"], "back reaches Home");
  await forward();
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='health']")?.offsetParent));
  assert.deepEqual(await active(), ["Server"], "forward returns to Server");
  assert.equal(await currentView(page), "health");
  await forward();
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='indexes']")?.offsetParent));
  assert.equal(await currentView(page), "indexes", "forward returns to Indexes");
  assert.equal(await page.$(".nav-back-chip"), null, "history moves show no chip");

  // 2. ⌘K with an empty search lists recent views first (last 5 distinct).
  await palette();
  const recents = (await options()).filter(([, g]) => g === "Recent").map(([l]) => l);
  assert.deepEqual(recents, ["Workload · Top statements", "Server · Health", "Home"], `recent views first: ${recents}`);
  assert.equal((await options())[0][1], "Recent", "the first option is a recent view");
  // Enter on the first recent jumps back to it.
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='top']")?.offsetParent));
  assert.deepEqual(await active(), ["Workload"]);
  assert.equal(await currentView(page), "top");

  // 3. From two characters: cached catalog (the database) first, then the
  // bounded server search's tables, routines, triggers and events.
  await palette();
  await page.type('input[aria-label="Search commands"]', "orders");
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.orders")));
  assert.deepEqual(await options(), [
    ["orders_archive", "Database"],
    ["shop.orders", "Table"],
    ["shop.p_orders", "Procedure"],
    ["shop.orders_bi", "Trigger"],
    ["shop.e_orders", "Event"],
  ]);
  const searchCalls = seen["/api/live/object-search"] || [];
  assert.equal(searchCalls.at(-1).text, "orders", "the server search got the typed text");
  assert.equal(searchCalls.at(-1).connection.host, "127.0.0.1", "the search uses the connection in use");
  // Enter on a table reveals it in the Query editor's schema browser.
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.waitForFunction(() =>
    Boolean(document.querySelector(".query-lab")?.offsetParent) &&
    document.activeElement?.closest(".schema-browser") &&
    document.activeElement?.querySelector(".tree-label")?.textContent === "orders");
  assert.deepEqual(await active(), ["Query editor"]);
  // The cross-link chip names the previous view and goes back on click.
  await page.waitForSelector(".nav-back-chip");
  assert.match(await text(".nav-back-chip"), /Workload · Top statements/);
  await page.click(".nav-back-chip");
  await page.waitForFunction(() => Boolean(document.querySelector(".server-view[data-view='top']")?.offsetParent));
  assert.equal(await page.$(".nav-back-chip"), null, "the chip leaves with its jump");
  assert.equal(await currentView(page), "top");

  // 4. Enter on a routine opens its SHOW CREATE in a new tab, selected, not run.
  const tabsBefore = (await page.$$(".query-tab")).length;
  await palette();
  await page.type('input[aria-label="Search commands"]', "p_orders");
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.p_orders")));
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.waitForFunction(() =>
    document.querySelector('[aria-label="SQL editor"]')?.value.includes("SHOW CREATE PROCEDURE shop.p_orders"));
  assert.deepEqual(await active(), ["Query editor"]);
  assert.equal((await page.$$(".query-tab")).length, tabsBefore + 1, "the routine opened in a new tab");
  assert.equal((seen["/api/editor/run"] || []).length, 0, "SHOW CREATE waits in the tab; nothing ran");

  // A same-place cross-link gets its own stop and restores the captured
  // tab, even when the preceding place in history was Server.
  await go("Server");
  await go("Query editor");
  const originalTab = await page.$eval('.query-tabs [role="tab"][aria-selected="true"]', (e) => e.dataset.tabId);
  await palette();
  await page.type('input[aria-label="Search commands"]', "p_orders");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.p_orders")));
  await page.keyboard.press("Enter");
  await page.waitForFunction((id) => document.querySelector('.query-tabs [role="tab"][aria-selected="true"]')?.dataset.tabId !== id, {}, originalTab);
  const linkedTab = await page.$eval('.query-tabs [role="tab"][aria-selected="true"]', (e) => e.dataset.tabId);
  assert.match(await text(".nav-back-chip"), /Query editor/);
  await page.click(".nav-back-chip");
  await page.waitForFunction((id) => document.querySelector('.query-tabs [role="tab"][aria-selected="true"]')?.dataset.tabId === id, {}, originalTab);
  assert.deepEqual(await active(), ["Query editor"], "the chip restores its captured Query editor origin");
  await forward();
  await page.waitForFunction((id) => document.querySelector('.query-tabs [role="tab"][aria-selected="true"]')?.dataset.tabId === id, {}, linkedTab);
  // Subsequent local tab navigation cannot retarget an already visible chip.
  await palette();
  await page.type('input[aria-label="Search commands"]', "p_orders");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.p_orders")));
  await page.keyboard.press("Enter");
  await page.waitForSelector(".nav-back-chip");
  await page.click(`[data-tab-id="${originalTab}"]`);
  await page.click(".nav-back-chip");
  await page.waitForFunction((id) => document.querySelector('.query-tabs [role="tab"][aria-selected="true"]')?.dataset.tabId === id, {}, linkedTab);

  // An object after the first 200 table names uses a bounded targeted read.
  assert.ok(tableNames.indexOf("z_target") >= 200);
  await palette();
  await page.type('input[aria-label="Search commands"]', "z_target");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.z_target")));
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.activeElement?.closest(".schema-browser") &&
    document.activeElement?.querySelector(".tree-label")?.textContent === "z_target");
  assert.ok(seen["/api/live/catalog"].some((b) => b.schema === "shop" && b.prefix === "z_target" && !b.after),
    "reveal asks for the full literal name as its prefix");
  await palette();
  await page.type('input[aria-label="Search commands"]', "gone_table");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => e.textContent.includes("shop.gone_table")));
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".schema-browser")?.textContent.includes("The object is no longer visible"));

  // 5. The schema browser's groups: Routines, Triggers and Events under each
  // database, read when opened, with Show CREATE in the row's menu.
  const folder = (label) =>
    page.$$eval(".browser-tree .tree-row", (rows, wanted) => {
      const row = rows.find((r) => r.querySelector(".tree-label")?.textContent === wanted);
      if (!row) return false;
      row.click();
      return true;
    }, label);
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".browser-tree .tree-label")].some((e) => e.textContent === "Routines"));
  assert.equal(await folder("Routines"), true);
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".browser-tree .tree-label")].some((e) => e.textContent === "p_top"));
  assert.ok((seen["/api/live/catalog"] || []).some((b) => b.group === "routines" && b.schema === "shop"),
    "the Routines folder read its group once opened");
  const routineMeta = await page.$$eval(".browser-tree .tree-row", (rows) =>
    rows.find((r) => r.querySelector(".tree-label")?.textContent === "p_top")?.querySelector(".tree-meta")?.textContent);
  assert.equal(routineMeta, "procedure");
  assert.equal(await folder("Triggers"), true);
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".browser-tree .tree-label")].some((e) => e.textContent === "orders_bi"));
  const triggerMeta = await page.$$eval(".browser-tree .tree-row", (rows) =>
    rows.find((r) => r.querySelector(".tree-label")?.textContent === "orders_bi")?.querySelector(".tree-meta")?.textContent);
  assert.equal(triggerMeta, "before insert on orders");
  assert.equal(await folder("Events"), true);
  await page.waitForFunction(() =>
    [...document.querySelectorAll(".browser-tree .tree-label")].some((e) => e.textContent === "e_nightly"));
  // Show CREATE from a routine's menu goes through the editor's Show CREATE path.
  await page.click('.browser-tree button[aria-label="More actions for p_top"]');
  await page.waitForSelector('.context-menu [role="menuitem"]');
  const menuItems = await page.$$eval('.context-menu [role="menuitem"]', (els) => els.map((e) => e.textContent));
  assert.deepEqual(menuItems, ["Insert name", "Show CREATE PROCEDURE"]);
  await page.locator('.context-menu [role="menuitem"] ::-p-text(Show CREATE PROCEDURE)').click();
  await page.waitForFunction(() =>
    document.querySelector('[aria-label="SQL editor"]')?.value.includes("SHOW CREATE PROCEDURE shop.p_top"));

  // 6. History that crosses connections says so instead of switching.
  await openConnectionPicker(page);
  await page.select('.window-toolbar select[aria-label="Connection"]', "p2");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  await page.waitForFunction(() => [...document.querySelectorAll(".browser-tree .tree-label")].some((e) => e.textContent === "p_top_staging"));
  assert.equal(await page.$$eval(".browser-tree .tree-label", (els) => els.some((e) => e.textContent === "p_top")), false,
    "expanded groups never show the previous connection's objects");
  assert.equal(seen["/api/live/catalog"].filter((b) => b.group === "routines" && b.connection.host === "db.staging").length, 1,
    "the already expanded group reloads once without a toggle");
  await go("Server");
  await page.waitForFunction(() => Boolean(document.querySelector(".server-place .server-view")?.offsetParent));
  await back(); // the Query editor on this connection: no notice
  assert.equal(await page.$(".notice"), null, "same-connection history is silent");
  await back(); // the Query editor as it was on the other connection
  await page.waitForSelector(".notice");
  assert.match(await text(".notice"), /was open on Local MySQL/, "the notice names the other connection");
  assert.match(await text(".notice"), /Nothing was switched/);
  await openConnectionPicker(page);
  assert.equal(await page.$eval('.window-toolbar select[aria-label="Connection"]', (e) => e.value), "p2",
    "the connection in use did not change");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  // And the picker still works afterwards.
  await page.locator(".notice .text-button ::-p-text(Dismiss)").click();
  await page.waitForFunction(() => !document.querySelector(".notice"));

  // Traversing A's entry while B stays active must retain both Forward stops.
  await forward();
  await page.waitForFunction(() => Boolean(document.querySelector(".query-lab")?.offsetParent));
  await forward();
  await page.waitForFunction(() => Boolean(document.querySelector(".server-place .server-view")?.offsetParent));
  assert.deepEqual(await active(), ["Server"], "Forward survives history from another connection");
  await go("Query editor");

  // Layout and text isolation at the two supported widths, both themes.
  for (const width of [1440, 720]) for (const theme of ["light", "dark"]) {
    await page.setViewport({ width, height: 1000 });
    await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth);
    assert.ok(await page.$(".workspace .topbar-stack"), "workspace and sticky toolbar wrappers survive");
    await page.screenshot({ path: path.join(directory, `history-${width}-${theme}.png`) });
  }
  await page.setViewport({ width: 1440, height: 1000 });

  // Deliberately ignore AbortSignal in this transport. Old success/error
  // callbacks must still be rejected, including A -> B -> A. Hold the search
  // debounce explicitly so the first-render suppression cannot pass by timing.
  await page.evaluate(() => {
    const fetch = window.fetch.bind(window), later = window.setTimeout.bind(window), cancel = window.clearTimeout.bind(window);
    const race = window.__historyRace = { requests: [], timers: new Map(), serial: 1000000 };
    window.setTimeout = (callback, ms, ...args) => {
      if (ms !== 250) return later(callback, ms, ...args);
      const id = ++race.serial;
      race.timers.set(id, () => callback(...args));
      return id;
    };
    window.clearTimeout = (id) => { if (!race.timers.delete(id)) cancel(id); };
    window.fetch = (url, init) => {
      if (!String(url).endsWith("/api/live/object-search")) return fetch(url, init);
      return new Promise((resolve, reject) => {
        const body = JSON.parse(init.body);
        race.requests.push({ body, reject, resolve(name) {
          resolve(new Response(JSON.stringify({ schema_version: "object-search-1.0", text: body.text,
            objects: [{ kind: "procedure", schema: "shop", name }], truncated: false, limit: 50 }),
          { status: 200, headers: { "Content-Type": "application/json" } }));
        } });
      });
    };
    race.restore = () => { window.fetch = fetch; window.setTimeout = later; window.clearTimeout = cancel; race.timers.clear(); };
  });
  const flushSearch = async (count) => {
    await page.waitForFunction(() => window.__historyRace.timers.size > 0);
    await page.evaluate(() => {
      const timers = [...window.__historyRace.timers.values()]; window.__historyRace.timers.clear();
      for (const callback of timers) callback();
    });
    await page.waitForFunction((n) => window.__historyRace.requests.length === n, {}, count);
  };
  const changeWhileOpen = (id) => page.select('.window-toolbar select[aria-label="Connection"]', id);
  await openConnectionPicker(page);
  await palette();
  await page.type('input[aria-label="Search commands"]', "race");
  await flushSearch(1);
  await page.evaluate(() => window.__historyRace.requests[0].resolve("race_staging"));
  await page.waitForFunction(() => document.querySelector(".command-results")?.textContent.includes("race_staging"));
  await changeWhileOpen("p3");
  await page.waitForFunction(() => window.__historyRace.timers.size > 0);
  assert.ok(!(await text(".command-results")).includes("race_staging"), "A's results disappear before B's debounce runs");
  await flushSearch(2);
  // Keep B pending, return to A and accept only its new reply.
  await changeWhileOpen("p2");
  await flushSearch(3);
  await page.evaluate(() => window.__historyRace.requests[2].resolve("race_current"));
  await page.waitForFunction(() => document.querySelector(".command-results")?.textContent.includes("race_current"));
  await page.evaluate(() => window.__historyRace.requests[1].reject(Error("late_other_error")));
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.ok(!(await text(".command-results")).includes("late_other_error"), "a cancelled error cannot replace current results");

  // Another A read remains pending through A -> B -> A and answers last.
  await page.focus('input[aria-label="Search commands"]');
  await page.keyboard.press("End"); await page.type('input[aria-label="Search commands"]', "_");
  await flushSearch(4);
  await changeWhileOpen("p3"); await flushSearch(5);
  await changeWhileOpen("p2"); await flushSearch(6);
  await page.evaluate(() => window.__historyRace.requests[5].resolve("race_current_2<img src=x onerror=window.__historyInjected=1>"));
  await page.waitForFunction(() => document.querySelector(".command-results")?.textContent.includes("race_current_2"));
  await page.evaluate(() => {
    window.__historyRace.requests[3].resolve("race_stale_A");
    window.__historyRace.requests[4].resolve("race_stale_B");
  });
  await page.evaluate(() => new Promise(requestAnimationFrame));
  assert.ok((await text(".command-results")).includes("race_current_2"));
  assert.ok(!(await text(".command-results")).includes("race_stale"), "aborted replies cannot win after returning to A");
  assert.equal(await page.$(".command-option img"), null, "server object names remain text");
  assert.equal(await page.evaluate(() => window.__historyInjected), undefined);
  assert.ok(await page.$(".command-option bdi"), "server names are isolated from surrounding UI text");
  // CommandPalette schedules run() after closing. Change identity before
  // that task gets a turn, so even a captured option must refuse selection.
  const beforeStalePick = (await page.$$(".query-tab")).length;
  await page.evaluate(() => {
    const select = document.querySelector('.window-toolbar select[aria-label="Connection"]');
    [...document.querySelectorAll(".command-option")].find((e) => e.textContent.includes("race_current_2")).click();
    select.value = "p3";
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal((await page.$$(".query-tab")).length, beforeStalePick, "a deferred stale selection opens no query tab");
  assert.equal(await page.$eval('.window-toolbar select[aria-label="Connection"]', (e) => e.value), "p3");
  await page.evaluate(() => window.__historyRace.restore());

  assert.deepEqual(errors, []);
  console.log("History navigation checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
