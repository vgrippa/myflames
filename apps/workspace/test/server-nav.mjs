// The Server place's shell: the floating list of views (the default: the
// view's title opens it, with keyboard, focus return and a marker for the
// other views), docking it (persisted), the grouped sub-navigation (arrow
// keys, badges from data already loaded), full-width details (Back, Escape, selection and focus
// restored on desktop and narrow layouts), ⓘ popovers, prerequisite
// gates, the pushed list at phone width, and session restore of the view and
// the inspector, including view names saved by earlier versions.
import assert from "node:assert/strict";
import path from "node:path";
import { DOCK_KEY, load, open, pause } from "./perf-harness.mjs";

const config = await load("config-mysql-8.4.json");
const tableReport = await load("tables-mysql-8.4.json");
const tableReads = [];
const largeCatalog = { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11", taken_at: "x", uptime_s: 1, performance_schema: true,
  schemas: [{ name: "bigdb", tables: 2501, system: false }, { name: "small", tables: 1, system: false }], user_tables: 2502, all_limit: 2000, all_note: "", system: [] };
const waitsOff = await load("waits-mariadb-13.0-default.json");
const blocked = await load("locks-mysql-8.4.json");
// Locks answers only once the floating marker test asks for it.
let locksReply = { status: 400, body: { error: "Not mocked in this suite." } };

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
  "/api/live/locks": () => locksReply,
  "/api/lifecycle/tools": { status: 400, body: { error: "Not mocked in this suite." } },
  "/api/schema-diagram/read": { status: 400, body: { error: "Not mocked in this suite." } } });
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: config };
// state.seen holds this suite's own endpoint (/api/live/config) without a path.
const live = () => [...state.seen.map((body) => ({ path: "/api/live/config", body })), ...state.other.filter((o) => o.path.startsWith("/api/live/"))];
const current = () => page.$eval(".source-list [aria-current=page]", (e) => e.dataset.view);
const active = () => page.evaluate(() => document.activeElement?.id || "");
const switcher = (view) => page.$eval(`#${view}-switcher`, (e) => ({
  expanded: e.getAttribute("aria-expanded"), popup: e.getAttribute("aria-haspopup"), name: e.querySelector(".view-switcher-name")?.textContent,
  text: e.textContent, marker: e.querySelector(".view-switcher-marker")?.className || "", heading: e.parentElement?.id,
  size: getComputedStyle(e).fontSize, weight: getComputedStyle(e).fontWeight }));
const popover = () => page.$('.view-switcher-popover[role=dialog][aria-label="Server views"]');
try {
  // ---- The floating list of views (the default when nothing is stored) ----
  assert.equal(await page.evaluate((key) => localStorage.getItem(key), DOCK_KEY), null, "nothing stored: the default applies");
  // Users and roles loads late (its chunk is held back) for the focus check below.
  state.slowAssets = /\/Accounts-[^/]*\.js$/;
  await h.server();
  await page.waitForSelector("#health-switcher");
  assert.ok(await page.$(".server-place.is-nav-collapsed"), "floating by default");
  assert.equal(await page.$(".server-source-wrap"), null, "no docked list");
  assert.equal(await page.$(".source-list"), null, "the list stays closed until the title opens it");
  // The view's title is the switcher: one name, a pop-up button at the title's size and weight.
  assert.deepEqual(await switcher("health"), { expanded: "false", popup: "dialog", name: "Health", text: "Health", marker: "",
    heading: "health-title", size: "15px", weight: "600" });
  assert.equal(await page.$eval('.server-view[data-view=health]', (e) => document.getElementById(e.getAttribute("aria-labelledby"))?.textContent), "Health",
    "the view is named by its title text");
  assert.ok(await page.$('.server-view[data-view=health] .view-title button[aria-label="About Health"]'), "ⓘ stays beside the title");
  // No second row naming the view: the view's toolbar is the first thing in the content.
  assert.equal(await page.$eval(".server-content", (c) => {
    const toolbar = c.querySelector(".server-view .view-toolbar");
    return Math.round(toolbar.getBoundingClientRect().top - c.getBoundingClientRect().top);
  }), 0, "no row above the view's toolbar");
  assert.equal(await page.$$eval(".server-content *", (els) => els.filter((e) => e.children.length === 0 && e.textContent.trim() === "Health" && e.getClientRects().length).length), 1,
    "the view's name appears once");

  // A view chosen from the list that loads late never takes focus from a
  // surface opened since (here the connection picker).
  await page.click("#health-switcher");
  await page.waitForSelector(".view-switcher-popover");
  await page.click("#server-nav-accounts");
  await page.waitForFunction(() => !document.querySelector(".view-switcher-popover"));
  assert.equal(await page.$('.server-view[data-view=accounts]'), null, "the chosen view is still loading");
  await page.click(".window-toolbar .connection-pill");
  await page.waitForSelector(".window-toolbar .picker-panel", { visible: true });
  await page.waitForFunction(() => !!document.activeElement?.closest(".window-toolbar .connection-picker"));
  await page.waitForSelector("#accounts-switcher", { visible: true });
  await pause(200);
  assert.equal(await page.evaluate(() => !!document.activeElement?.closest(".window-toolbar .connection-picker")), true,
    "the late view leaves focus in the picker");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  state.slowAssets = null;
  await h.view("health");

  // Keyboard: Enter opens on the current view; arrows, Home and End move without choosing; Escape closes and returns focus.
  await page.focus("#health-switcher");
  await page.keyboard.press("Enter");
  await page.waitForSelector('.view-switcher-popover[role=dialog][aria-label="Server views"]');
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-health");
  assert.equal((await switcher("health")).expanded, "true");
  assert.equal(await page.$eval("#health-switcher", (e) => e.getAttribute("aria-controls")), await page.$eval(".view-switcher-popover", (e) => e.id));
  assert.deepEqual(await texts(".view-switcher-popover .source-group h3"), ["Overview", "Contention", "Schema", "Setup", "Security"]);
  // Ten views: Database diagram is a place of its own (SQL workspace group).
  assert.equal(await page.$$eval(".view-switcher-popover .source-item", (els) => els.length), 10);
  assert.equal(await page.$("#server-nav-diagram"), null, "no Database diagram in the Server list");
  assert.ok(await page.$('.view-switcher-popover .server-floating-header button[aria-label="Dock the list of views"]'), "the dock control in its header");
  // Anchored under the title, aligned to its left edge, never over it, within the window.
  const anchor = await page.evaluate(() => {
    const b = document.getElementById("health-switcher").getBoundingClientRect();
    const p = document.querySelector(".view-switcher-popover").getBoundingClientRect();
    const footer = document.querySelector(".window-statusbar")?.getBoundingClientRect().top ?? innerHeight;
    return { gap: p.top - b.bottom, left: Math.round(p.left - b.left), bottom: footer - p.bottom };
  });
  assert.ok(anchor.gap >= 0 && anchor.gap <= 12, `the list opens under the title (${JSON.stringify(anchor)})`);
  assert.equal(anchor.left, 0, "aligned to the title's left edge");
  assert.ok(anchor.bottom >= 0, `the list stays above the window footer (${JSON.stringify(anchor)})`);
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-innodb");
  assert.equal(await page.$eval(".view-switcher-popover [aria-current=page]", (e) => e.dataset.view), "health", "moving does not choose");
  await page.keyboard.press("End");
  assert.equal(await active(), "server-nav-accounts");
  await page.keyboard.press("ArrowUp");
  assert.equal(await active(), "server-nav-maintenance");
  await page.keyboard.press("Home");
  assert.equal(await active(), "server-nav-health");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-switcher-popover"));
  assert.equal(await active(), "health-switcher", "Escape returns focus to the title");
  assert.equal((await switcher("health")).expanded, "false");
  // Space opens too; a click outside closes it.
  await page.keyboard.press("Space");
  await page.waitForSelector(".view-switcher-popover");
  await page.mouse.click(900, 600);
  await page.waitForFunction(() => !document.querySelector(".view-switcher-popover"));
  // Empty space takes no focus: it returns to the title.
  await page.waitForFunction(() => document.activeElement?.id === "health-switcher");
  // A click on a control keeps focus on that control.
  await page.click("#health-switcher");
  await page.waitForSelector(".view-switcher-popover");
  await page.click('.server-view[data-view=health] .view-toolbar select');
  await page.waitForFunction(() => !document.querySelector(".view-switcher-popover"));
  await pause(100);
  assert.equal(await page.evaluate(() => document.activeElement?.tagName), "SELECT", "a clicked control keeps focus");
  await page.keyboard.press("Escape");
  // Typing ranks commands whose name holds every word first, then keyword
  // matches, each in the declared order: health lists the Health view before Tuning.
  await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "health");
  const ranked = await page.$$eval("dialog.command-palette .command-option > span", (els) => els.map((e) => e.textContent));
  assert.equal(ranked[0], "Health", `the Health view comes first: ${ranked}`);
  assert.ok(ranked.indexOf("Tuning") > 0, `Tuning still matches by keyword: ${ranked}`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.focus("#health-switcher");
  // Navigation from elsewhere (the command palette) never carries an open list onto the next view.
  await page.click("#health-switcher");
  await page.waitForSelector(".view-switcher-popover");
  await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "innodb");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.waitForSelector(".server-view[data-view=innodb]", { visible: true });
  await pause(200);
  assert.equal(await popover(), null, "the list closed with the view it was opened on");
  assert.equal(await page.$eval("#innodb-switcher", (e) => e.getAttribute("aria-expanded")), "false");
  await h.view("health");
  assert.equal(await popover(), null, "returning to the view does not reopen its list");
  // Down opens; Enter chooses: the list closes and the chosen view's title has focus.
  await page.focus("#health-switcher");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-health");
  for (const id of ["innodb", "errorlog", "locks", "indexes", "tables", "config"]) {
    await page.keyboard.press("ArrowDown");
    await page.waitForFunction((target) => document.activeElement?.id === `server-nav-${target}`, {}, id);
  }
  await page.keyboard.press("Enter");
  await page.waitForSelector('.server-view[data-view=config]', { visible: true });
  assert.equal(await popover(), null, "choosing a view closes the list");
  await page.waitForFunction(() => document.activeElement?.id === "config-switcher");
  // Parameters reads on open; its Watch dot is in the list, and the other views' titles show it.
  await page.waitForSelector(".server-view tr[data-row-key]");
  assert.equal((await switcher("config")).marker, "", "a view's own status is not its title's marker");
  await page.click("#config-switcher");
  await page.waitForSelector(".view-switcher-popover #server-nav-config .status-dot.is-watch");
  await page.click("#server-nav-health");
  await page.waitForSelector('.server-view[data-view=health]', { visible: true });
  const marked = await switcher("health");
  assert.equal(marked.marker, "status-dot is-watch view-switcher-marker");
  assert.equal(marked.text, "Health, 1 other view to watch", "the marker's words reach screen readers");
  assert.equal(await page.$eval("#health-switcher .view-switcher-marker", (e) => e.getAttribute("aria-hidden")), "true");
  assert.equal(await page.$eval("#health-switcher .view-switcher-marker", (e) => e.textContent), "", "no counts on the title");

  // Docking from the list's header persists across a reload. From the
  // keyboard, focus moves to the toolbar's Dock/Float toggle, never the page.
  await page.click("#health-switcher");
  await page.waitForSelector('.view-switcher-popover button[aria-label="Dock the list of views"]');
  await page.focus('.view-switcher-popover button[aria-label="Dock the list of views"]');
  await page.keyboard.press("Enter");
  await page.waitForSelector(".server-place:not(.is-nav-collapsed) .server-source-wrap .source-list");
  assert.equal(await page.$(".view-switcher"), null, "docked: the title is a heading again");
  await page.waitForFunction(() => document.activeElement?.matches(".place-toolbar .server-sidebar-toggle"));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Float the list of views",
    "docking from the list keeps focus on the Dock/Float toggle");
  assert.equal(await page.evaluate((key) => localStorage.getItem(key), DOCK_KEY), "false");
  await page.reload({ waitUntil: "networkidle0" });
  // The reloaded workspace starts over: count its reads only.
  state.seen.length = 0;
  state.other.length = 0;
  await h.server();
  await page.waitForSelector(".server-place:not(.is-nav-collapsed) .server-source-wrap .source-list");
  assert.equal(await page.$(".view-switcher"), null, "still docked after a reload");
  assert.equal(await page.$eval("#health-title", (e) => e.textContent), "Health");

  // ---- The docked list ----
  // Groups by job, each item with its own icon, one item current.
  // Replication and Tuning are places of their own, not Server views.
  assert.deepEqual(await texts(".source-list .source-group h3"), ["Overview", "Contention", "Schema", "Setup", "Security"]);
  const items = await page.$$eval(".source-list .source-item", (els) => els.map((e) => [e.dataset.view, e.querySelector("svg path, svg rect, svg circle")?.outerHTML]));
  assert.deepEqual(items.map((i) => i[0]),
    ["health", "innodb", "errorlog", "locks", "indexes", "tables", "config", "instruments", "maintenance", "accounts"]);
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
  await page.waitForSelector("#server-nav-accounts .count-badge");
  await page.keyboard.press("ArrowUp");
  assert.equal(await current(), "maintenance");
  await page.keyboard.press("Home");
  assert.equal(await current(), "health");
  // A view reads when it is opened on screen (Parameters, Instruments and
  // Users and roles read their catalogs once); views never opened read nothing, and nothing
  // was read before Server was opened (Server and Replication mount on their
  // first visit). No badge appears without data.
  const onOpen = ["/api/live/config", "/api/live/instruments", "/api/live/error-log"];
  for (const view of ["innodb", "errorlog", "locks", "indexes", "tables", "config", "instruments", "maintenance", "accounts"]) await h.view(view);
  const opened = ["/api/live/health", "/api/live/innodb", "/api/live/error-log", "/api/live/locks", "/api/live/indexes", "/api/live/tables", ...onOpen];
  assert.deepEqual(live().filter((c) => c.path !== "/api/live/schemas" && c.path !== "/api/live/privileges" && !opened.includes(c.path)), [],
    "only views opened on screen read (never Top statements, Processlist, Waits, I/O, Replication or the Full analysis)");
  for (const path of onOpen) assert.equal(live().filter((c) => c.path === path).length, 1, `${path} is read once on open`);
  assert.equal(state.other.filter((c) => c.path === "/api/accounts/list").length, 1, "/api/accounts/list is read once on open");
  // Only the views that read on open can show a badge: here Parameters and Users and roles (Instruments is not mocked).
  assert.deepEqual(await page.$$eval(".source-list .count-badge, .source-list .status-dot", (els) => els.map((e) => e.closest("[id^=server-nav-]")?.id)), ["server-nav-config", "server-nav-accounts"]);

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
  // A workspace saved on the old Server > Database diagram view opens the
  // Database diagram place (an old route opens the place); Server then opens Health.
  await page.evaluate(async (t) => {
    const call = (route, body) => fetch(route, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: JSON.stringify(body) }).then((r) => r.json());
    const stored = await call("/api/session/load", {});
    const state = stored.state;
    state.mode = "server";
    state.query_lab.slow.kind = "diagram";
    await call("/api/session/save", { revision: stored.revision, client_id: "an-earlier-version-diagram", seq: 1, state });
  }, token);
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector('.server-place.is-diagram-place .server-view[data-view="diagram"]', { visible: true });
  assert.equal(await page.$eval(".main-nav button[aria-current=page]", (e) => e.dataset.place), "diagram");
  await h.server();
  await page.waitForSelector("#server-nav-health[aria-current=page]");
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

  // The packaged view restores its id only (never the list), and reads once
  // when opened on screen to show its count badge.
  await h.view("accounts");
  await page.waitForSelector("#server-nav-accounts .count-badge");
  assert.match(await text("#server-nav-accounts"), /1/);
  const savedAccounts = await h.savedSlow(s => s.kind === "accounts");
  assert.ok(!JSON.stringify(savedAccounts.state).includes("nav-account"));
  const readsBeforeReload = state.other.filter(r => r.path === "/api/accounts/list").length;
  await page.reload({ waitUntil: "networkidle0" }); await h.server();
  await page.waitForSelector("#server-nav-accounts[aria-current=page]");
  await page.waitForSelector("#server-nav-accounts .count-badge");
  assert.equal(state.other.filter(r => r.path === "/api/accounts/list").length, readsBeforeReload + 1);

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
  // Locks to Parameters: Indexes, Tables, Parameters.
  for (const id of ["tables", "config"]) {
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

  // Floating again from the top bar's toggle, kept across a reload; the other
  // views' worst state shows on the title: a problem outranks a watch.
  await page.click('.place-toolbar button[aria-label="Float the list of views"]');
  await page.waitForSelector(".server-place.is-nav-collapsed .view-switcher");
  assert.equal(await page.$(".server-source-wrap"), null);
  assert.equal(await page.evaluate((key) => localStorage.getItem(key), DOCK_KEY), "true");
  assert.ok(await page.$('.place-toolbar button[aria-label="Dock the list of views"]'), "the toggle now docks it");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector(".server-place.is-nav-collapsed .view-switcher");
  await h.view("config");
  await page.waitForSelector(".server-view tr[data-row-key]");
  locksReply = { status: 200, body: blocked };
  await h.view("locks");
  await page.waitForFunction(() => !document.querySelector('.server-view[data-view="locks"] .spinner'));
  if (!(await page.$('.server-view[data-view="locks"] [data-row-key]'))) await h.refresh();
  await page.waitForSelector('.server-view[data-view="locks"] [data-row-key]');
  await h.view("health");
  await page.waitForFunction(() => document.querySelector("#health-switcher .view-switcher-marker")?.classList.contains("is-problem"));
  assert.equal((await switcher("health")).text, "Health, 1 other view has a problem, 1 to watch");
  await h.view("locks");
  assert.equal((await switcher("locks")).text, "Locks, 1 other view to watch", "the view's own problem is in its content");
  // The list keeps its counts and dots.
  await h.openViews();
  assert.equal(await text(".view-switcher-popover #server-nav-locks .count-badge"), "5");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.id === "locks-switcher");
  // Workload works the same way, with its own list.
  await h.workload();
  await page.waitForSelector("#top-switcher");
  assert.deepEqual(await switcher("top").then(({ popup, expanded, name }) => ({ popup, expanded, name })), { popup: "dialog", expanded: "false", name: "Top statements" });
  await page.click("#top-switcher");
  await page.waitForSelector('.view-switcher-popover[role=dialog][aria-label="Workload views"]');
  assert.deepEqual(await texts(".view-switcher-popover .source-label"), ["Top statements", "Processlist", "Waits", "I/O"]);
  await page.waitForFunction(() => document.activeElement?.id === "server-nav-top");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.id === "top-switcher");

  assert.deepEqual(h.errors, []);
  console.log("server navigation: ok");
} finally {
  await h.close();
}
