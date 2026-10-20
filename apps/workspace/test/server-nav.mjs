// The Server place's shell: the grouped sub-navigation (arrow keys, badges from
// data already loaded), full-width details (Back, Escape, selection and focus
// restored on desktop and narrow layouts), ⓘ popovers, prerequisite
// gates, the pushed list at phone width, and session restore of the view and
// the inspector, including view names saved by earlier versions.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const config = await load("config-mysql-8.4.json");
const tableReport = await load("tables-mysql-8.4.json");
const tableReads = [];
const largeCatalog = { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11", taken_at: "x", uptime_s: 1, performance_schema: true,
  schemas: [{ name: "bigdb", tables: 2501, system: false }, { name: "small", tables: 1, system: false }], user_tables: 2502, all_limit: 2000, all_note: "", system: [] };
const waitsOff = await load("waits-mariadb-13.0-default.json");

const accounts = { schema_version: "accounts-1.0", engine: "mysql", version: "8.4.11", complete: true, warnings: [],
  accounts: [{ name: "nav-account", host: "%", role: false }], role_grants: [], default_roles: [] };
const h = await open("/api/live/config", "server-nav", { "/api/live/waits": { status: 200, body: waitsOff },
  "/api/live/privileges": { status: 200, body: { schema_version: "privileges-1.0", account: "test@%", actions: {} } },
  "/api/live/schemas": { status: 200, body: largeCatalog },
  "/api/live/tables": body => {
    tableReads.push(body);
    if (!body.schemas?.length && !body.all_schemas) return { status: 400, body: { error: "explicit all_schemas required" } };
    return { status: 200, body: tableReport };
  },
  "/api/accounts/list": { status: 200, body: accounts },
  "/api/lifecycle/tools": { status: 400, body: { error: "Not mocked in this suite." } },
  "/api/schema-diagram/read": { status: 400, body: { error: "Not mocked in this suite." } } });
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: config };
// state.seen holds this suite's own endpoint (/api/live/config) without a path.
const live = () => [...state.seen.map((body) => ({ path: "/api/live/config", body })), ...state.other.filter((o) => o.path.startsWith("/api/live/"))];
const current = () => page.$eval(".source-list [aria-current=page]", (e) => e.dataset.view);
try {
  await h.server();
  // Groups by job, each item with its own icon, one item current.
  // Replication and Tuning are places of their own, not Server views.
  assert.deepEqual(await texts(".source-list .source-group h3"), ["Overview", "Contention", "Schema", "Setup", "Security"]);
  const items = await page.$$eval(".source-list .source-item", (els) => els.map((e) => [e.dataset.view, e.querySelector("svg path, svg rect, svg circle")?.outerHTML]));
  assert.deepEqual(items.map((i) => i[0]),
    ["health", "innodb", "errorlog", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"]);
  assert.equal(new Set(items.map((i) => i[1])).size, items.length, "one glyph per view");
  assert.equal((await page.$$(".source-list [aria-current=page]")).length, 1);
  assert.equal(await current(), "health");
  // The intro paragraph is behind ⓘ; the place toolbar holds the title and the connection.
  assert.equal(await page.$(".server-place .place-toolbar p"), null);
  assert.ok(await page.$(".window-toolbar .connection-picker"));

  // Keyboard: Down/Up move and select, Home/End reach the ends; one tab stop.
  assert.deepEqual(await page.$$eval(".source-list .source-item", (els) => els.map((e) => e.tabIndex)).then((t) => t.filter((x) => x === 0).length), 1);
  await page.focus("#server-nav-health");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-innodb");
  assert.equal(await current(), "innodb");
  await page.keyboard.press("End");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-accounts");
  assert.equal(await current(), "accounts");
  await page.keyboard.press("ArrowUp");
  assert.equal(await current(), "maintenance");
  await page.keyboard.press("Home");
  assert.equal(await current(), "health");
  // A view reads when it is opened on screen (Parameters and Instruments
  // read their catalogs once); views never opened read nothing, and nothing
  // was read before Server was opened (Server and Replication mount on their
  // first visit). No badge appears without data.
  const onOpen = ["/api/live/config", "/api/live/instruments"];
  for (const view of ["innodb", "errorlog", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"]) await h.view(view);
  const opened = ["/api/live/health", "/api/live/innodb", "/api/live/error-log", "/api/live/locks", "/api/live/indexes", "/api/live/tables", ...onOpen];
  assert.deepEqual(live().filter((c) => c.path !== "/api/live/schemas" && c.path !== "/api/live/privileges" && !opened.includes(c.path)), [],
    "only views opened on screen read (never Top statements, Processlist, Waits, I/O, Replication or the Full analysis)");
  for (const path of onOpen) assert.equal(live().filter((c) => c.path === path).length, 1, `${path} is read once on open`);
  // Only the views that read on open can show a badge: here Parameters (Instruments is not mocked).
  assert.deepEqual(await page.$$eval(".source-list .count-badge, .source-list .status-dot", (els) => els.map((e) => e.closest("[id^=server-nav-]")?.id)), ["server-nav-config"]);

  // The shared database chooser participates in the kit's one-popover and
  // focus-return contract. Dismissing it does not navigate or load a report.
  await h.view("indexes");
  await page.waitForFunction(() => !document.querySelector('.server-view[data-view="indexes"] .spinner'));
  const readsBefore = live().length;
  await page.click("#indexes-schemas");
  await page.waitForSelector('[role=dialog][aria-label="Choose databases"]');
  // Focus moves to the search once the popover is placed (the next frame).
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Search databases");
  await page.$eval('button[aria-label="About Indexes"]', (e) => e.click());
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Choose databases"]'));
  assert.ok(await page.$('[role=dialog][aria-label="About Indexes"]'));
  await page.keyboard.press("Escape");
  assert.equal(await current(), "indexes");
  assert.deepEqual(live().slice(readsBefore).filter((c) => c.path !== "/api/live/schemas" && c.path !== "/api/live/privileges"), [], "the chooser reads nothing");

  // A badge from data the user loaded.
  await h.view("config");
  await h.refresh();
  await page.waitForSelector("#server-nav-config .status-dot");
  assert.equal(await page.$eval("#server-nav-config .status-dot", (e) => e.className), "status-dot is-watch");
  assert.match(await text("#server-nav-config .visually-hidden"), /Watch/);
  assert.equal(await page.$$eval(".source-list .status-dot", (els) => els.length), 1, "only the loaded view has a badge");

  // Details only open for a selected object. There is no empty side panel.
  assert.equal(await page.$(".view-inspector"), null);
  assert.equal(await page.$(".server-view .inspector-toggle"), null);
  // Selecting a row opens it; Enter on a row too; Escape closes it and returns focus to the row.
  // Parameters rows are table rows keyed by parameter name.
  await h.select('.server-view tr[data-row-key="innodb_buffer_pool_size"]');
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "innodb_buffer_pool_size");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.closest("tr")?.dataset.rowKey === "innodb_buffer_pool_size");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "innodb_dedicated_server");

  // The selected object owns the full work area, with an explicit way back.
  assert.equal(await page.$eval(".view-main", element => element.hidden), true);
  assert.ok(await page.$(".view-inspector.is-detail .detail-back"));
  assert.equal(await page.$(".view-inspector [role=separator]"), null);
  const geometry = await page.$eval(".view-inspector", element => {
    const parent = element.parentElement.getBoundingClientRect();
    const detail = element.getBoundingClientRect();
    return { detail: detail.width, parent: parent.width };
  });
  assert.equal(geometry.detail, geometry.parent, "details use the available width");
  // ⓘ popovers: one at a time, Escape returns focus, a click outside closes.
  await page.click('button[aria-label="About Server"]');
  assert.equal(await page.$eval('button[aria-label="About Server"]', (e) => e.getAttribute("aria-expanded")), "true");
  const pop = await page.$eval('[role=dialog][aria-label="About Server"]', (e) => { const r = e.getBoundingClientRect(); return [r.width, r.right]; });
  assert.ok(pop[0] <= 320 && pop[1] <= 1440, `popover fits (${pop})`);
  // (The open popover may cover the other button, so it is activated directly.)
  await page.$eval('button[aria-label="About Parameters"]', (e) => e.click());
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Server"]'));
  assert.ok(await page.$('[role=dialog][aria-label="About Parameters"]'));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "About Parameters");
  assert.ok(await page.$(".view-inspector"), "Escape in a popover leaves the inspector open");
  await page.click('button[aria-label="About Parameters"]');
  await page.mouse.click(700, 20);
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Parameters"]'));
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  // Notes: one button with a count.
  assert.match(await text(".server-view .notes-button"), /^\d+ notes?$/);

  // A view that cannot run: one sentence and one action instead of a readiness grid.
  await h.view("waits");
  await h.refresh();
  await page.waitForSelector(".server-view .gate");
  const gate = await page.$eval(".server-view .gate", (e) => [e.querySelectorAll("p").length, e.querySelectorAll("button, a").length]);
  assert.deepEqual(gate, [1, 1]);

  // Session restore keeps the view, but does not restore private detail data.
  await h.view("config");
  await h.select('.server-view tr[data-row-key="innodb_buffer_pool_size"]');
  const saved = await h.savedSlow((s) => s.kind === "config" && s.inspector === true);
  assert.equal(saved.state.query_lab.slow.inspector, true);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-config[aria-current=page]");
  assert.equal(await page.$(".view-inspector"), null, "no empty detail screen after reload");
  await h.refresh();
  await h.select('.server-view tr[data-row-key="innodb_buffer_pool_size"]');
  // Storage failures cannot prevent the local Back action.
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error("blocked"); }; });
  await page.click(".detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));

  // A view saved by an earlier version under its old name opens as its new view.
  const token = await page.evaluate(async () => (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token);
  await page.evaluate(async (t) => {
    const call = (route, body) => fetch(route, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(body) }).then((r) => r.json());
    const stored = await call("/api/session/load", {});
    const state = stored.state;
    state.query_lab.slow.kind = "running";
    delete state.query_lab.slow.inspector;
    delete state.query_lab.slow.sessions_scope;
    await call("/api/session/save", { revision: stored.revision, client_id: "an-earlier-version", seq: 1, state });
  }, token);
  await page.reload({ waitUntil: "networkidle0" });
  await h.workload();
  await page.waitForSelector("#server-nav-sessions[aria-current=page]");
  assert.equal(await page.$(".view-inspector"), null, "no inspector state saved: closed");
  // The next save writes the new names and the defaults for the new fields.
  await h.view("io");
  const migrated = await h.savedSlow((s) => s.kind === "io");
  assert.equal(migrated.state.query_lab.slow.inspector, false);
  assert.equal(migrated.state.query_lab.slow.sessions_scope, "active");
  // A stored view id that names an Object property ("constructor", "__proto__") is no view: Health opens.
  for (const kind of ["constructor", "__proto__", "toString"]) {
    await page.evaluate(async (t, k) => {
      const call = (route, body) => fetch(route, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(body) }).then((r) => r.json());
      const stored = await call("/api/session/load", {});
      const state = stored.state;
      state.query_lab.slow.kind = k;
      await call("/api/session/save", { revision: stored.revision, client_id: `an-earlier-version-${k}`, seq: 1, state });
    }, token, kind);
    await page.reload({ waitUntil: "networkidle0" });
    await h.server();
    await page.waitForSelector("#server-nav-health[aria-current=page]");
    await page.waitForSelector(".server-view[data-view=health]");
  }

  // The packaged view restores its id only. Its badge counts an explicit
  // list read, and a reload does not restore the list or issue an account read.
  await h.view("accounts");
  assert.equal(await page.$("#server-nav-accounts .count-badge"), null);
  await h.button("Load users and roles"); await page.waitForSelector("#server-nav-accounts .count-badge");
  assert.match(await text("#server-nav-accounts"), /1/);
  const savedAccounts = await h.savedSlow(s => s.kind === "accounts");
  assert.ok(!JSON.stringify(savedAccounts.state).includes("nav-account"));
  const readsBeforeReload = state.other.filter(r => r.path === "/api/accounts/list").length;
  await page.reload({ waitUntil: "networkidle0" }); await h.server();
  await page.waitForSelector("#server-nav-accounts[aria-current=page]");
  assert.equal(await page.$("#server-nav-accounts .count-badge"), null);
  assert.equal(state.other.filter(r => r.path === "/api/accounts/list").length, readsBeforeReload);

  // Refresh all repeats the explicitly approved table scope, including
  // Fresh numbers before the :all suffix. It never grants a new scope consent.
  await h.view("tables");
  await page.waitForSelector("#tables-load-hint");
  const tablesView = '.server-view[data-view="tables"]';
  const freshInput = `${tablesView} .tables-fresh input`;
  if (!(await page.$eval(freshInput, e => e.checked))) await page.click(freshInput);
  await page.locator(`${tablesView} button ::-p-text(Read all anyway)`).click();
  await page.waitForSelector("#server-nav-tables .status-dot");
  assert.equal(tableReads.at(-1).all_schemas, true);
  assert.equal(tableReads.at(-1).fresh, true);
  const beforeRefresh = tableReads.length;
  await page.click('.server-place [data-action="refresh-all"]');
  // Unopened sections refresh too; unsupported mocks fail independently.
  await page.waitForFunction(() => !document.querySelector(".refresh-all-stop"));
  assert.equal(tableReads.length, beforeRefresh + 1);
  assert.equal(tableReads.at(-1).all_schemas, true, "Refresh all keeps explicit consent");
  assert.equal(tableReads.at(-1).fresh, true, "the :all suffix does not turn fresh off");
  await page.click("#tables-schemas");
  await page.locator(".perf-picker-option ::-p-text(small)").click();
  await page.keyboard.press("Escape");
  await page.click('.server-place [data-action="refresh-all"]');
  await page.waitForFunction(() => !document.querySelector(".refresh-all-stop"));
  assert.deepEqual(tableReads.at(-1).schemas, ["small"]);
  assert.equal("all_schemas" in tableReads.at(-1), false, "a different scope does not inherit consent");

  // Phone width on a fresh page: the list of views is the first screen; choosing pushes the view.
  const phone = await page.browser().newPage();
  await phone.setViewport({ width: 390, height: 844 });
  await phone.setRequestInterception(true);
  phone.on("request", (r) => {
    const p = new URL(r.url()).pathname;
    if (p === "/api/live/config") return r.respond({ status: 200, contentType: "application/json", body: JSON.stringify(config) });
    if (p.startsWith("/api/live/")) return r.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
    return r.continue();
  });
  await phone.goto(page.url(), { waitUntil: "networkidle0" });
  await phone.click('#window-sidebar-toggle');
  await phone.waitForSelector(".sidebar-drawer.open");
  await phone.locator(".main-nav button ::-p-text(Server)").click();
  await phone.waitForSelector(".server-layout.show-list");
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.ok(await phone.$eval(".source-list", (e) => e.classList.contains("is-pushed")));
  assert.equal(await phone.$eval(".server-content", (e) => getComputedStyle(e).display), "none");
  await phone.screenshot({ path: path.join(h.shots, "server-list-390.png") });
  await phone.focus("#server-nav-locks");
  await phone.keyboard.press("ArrowDown");
  await phone.waitForFunction(() => document.activeElement?.id === "server-nav-indexes");
  assert.ok(await phone.$(".server-layout.show-list"), "arrows only move focus in the pushed list");
  // Locks to Parameters: Indexes, Tables, Database diagram, Parameters.
  for (const id of ["tables", "diagram", "config"]) {
    await phone.keyboard.press("ArrowDown");
    await phone.waitForFunction((target) => document.activeElement?.id === `server-nav-${target}`, {}, id);
  }
  await phone.keyboard.press("Enter");
  await phone.waitForSelector(".server-layout:not(.show-list) .server-view[data-view=config]");
  await phone.waitForFunction(() => document.activeElement?.id === "config-title");
  // Parameters reads on open.
  await phone.waitForSelector(".server-view tr[data-row-key]");
  // Narrow details occupy the full work area; Back receives focus, Escape returns it.
  // The last rows can sit under the fixed status bar until the page scrolls.
  await phone.$eval('.server-view tr[data-row-key="slow_query_log"]', (e) => e.scrollIntoView({ block: "center" }));
  await phone.click('.server-view tr[data-row-key="slow_query_log"] td');
  await phone.waitForSelector(".view-inspector.is-detail");
  assert.equal(await phone.$(".view-inspector.is-sheet"), null);
  assert.equal(await phone.$eval(".view-main", element => element.hidden), true);
  await phone.waitForFunction(() => document.activeElement?.classList.contains("detail-back"));
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await phone.screenshot({ path: path.join(h.shots, "server-detail-390.png") });
  await phone.keyboard.press("Escape");
  await phone.waitForFunction(() => !document.querySelector(".view-inspector"));
  await phone.waitForFunction(() => document.activeElement?.closest("tr")?.dataset.rowKey === "slow_query_log");
  // Back returns to the list with focus on the view it came from.
  await phone.$eval(".server-view[data-view=config] .server-back", e => e.scrollIntoView({block: "center"}));
  await phone.click(".server-view[data-view=config] .server-back");
  await phone.waitForSelector(".server-layout.show-list");
  await phone.waitForFunction(() => document.activeElement?.id === "server-nav-config");
  await phone.close();

  // The top bar names the place once; the place toolbar has one control for
  // the list of views: dock/float when wide, a Views menu at the 720 px window.
  assert.equal(await page.$(".topbar-title button"), null, "no breadcrumb menus in the top bar");
  assert.ok(await page.$(".place-toolbar .server-sidebar-toggle"), "wide: dock or float the list of views");
  assert.equal(await page.$(".place-toolbar .server-views-menu"), null, "wide: no Views menu beside the docked list");
  const narrow = await page.browser().newPage();
  await narrow.setViewport({ width: 720, height: 520 });
  await narrow.setRequestInterception(true);
  narrow.on("request", (r) => {
    const p = new URL(r.url()).pathname;
    if (p === "/api/live/config") return r.respond({ status: 200, contentType: "application/json", body: JSON.stringify(config) });
    if (p.startsWith("/api/live/")) return r.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
    return r.continue();
  });
  await narrow.goto(page.url(), { waitUntil: "networkidle0" });
  await narrow.locator(".main-nav button ::-p-text(Server)").click();
  await narrow.waitForSelector(".server-layout.is-compact");
  assert.equal(await narrow.$(".place-toolbar .server-sidebar-toggle"), null, "narrow: the list is a screen, nothing to dock");
  await narrow.click(".place-toolbar .server-views-menu");
  await narrow.waitForSelector('.popover[role=menu][aria-label="Server views"]');
  assert.ok((await narrow.$$eval('.popover[role=menu] [role=menuitem]', (els) => els.map((e) => e.textContent))).includes("Parameters"));
  await narrow.locator('.popover[role=menu] [role=menuitem] ::-p-text(Parameters)').click();
  await narrow.waitForSelector(".server-layout:not(.show-list) .server-view[data-view=config]");
  assert.equal(await narrow.$('.popover[role=menu]'), null, "choosing a view closes the menu");
  await narrow.click(".place-toolbar .server-views-menu");
  await narrow.waitForSelector('.popover[role=menu] [role=menuitem][aria-current=page]');
  assert.equal(await narrow.$eval('.popover[role=menu] [aria-current=page]', (e) => e.textContent), "Parameters");
  await narrow.keyboard.press("Escape");
  await narrow.waitForFunction(() => !document.querySelector(".popover[role=menu]"));
  assert.equal(await narrow.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await narrow.close();

  assert.deepEqual(h.errors, []);
  console.log("server navigation: ok");
} finally {
  await h.close();
}
