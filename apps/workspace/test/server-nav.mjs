// The Server place's shell: the grouped sub-navigation (arrow keys, badges from
// data already loaded), the inspector (toggle, ⌥⌘I, Escape, resizing, width
// remembered, bottom sheet on narrow layouts), ⓘ popovers, prerequisite
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
  "/api/accounts/list": { status: 200, body: accounts } });
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: config };
const live = () => [...state.seen, ...state.other.filter((o) => o.path.startsWith("/api/live/"))];
const current = () => page.$eval(".source-list [aria-current=page]", (e) => e.dataset.view);
const inspectorWidth = () => page.$eval(".view-inspector", (e) => Math.round(e.getBoundingClientRect().width));
/** Wait for the inspector to reach a width (renders follow key presses). */
const widthIs = async (want, message) => {
  for (let i = 0; i < 50 && (await inspectorWidth()) !== want; i++) await pause(100);
  assert.equal(await inspectorWidth(), want, message);
};
try {
  await h.server();
  // Groups by job, each item with its own icon, one item current.
  assert.deepEqual(await texts(".source-list .source-group h3"), ["Overview", "Workload", "Contention", "Schema", "Setup", "Security"]);
  const items = await page.$$eval(".source-list .source-item", (els) => els.map((e) => [e.dataset.view, e.querySelector("svg path, svg rect, svg circle")?.outerHTML]));
  assert.deepEqual(items.map((i) => i[0]),
    ["health", "innodb", "report", "top", "sessions", "replication", "waits", "io", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"]);
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
  // Opening views reads nothing, and no badge appears without data.
  for (const view of ["innodb", "report", "top", "sessions", "replication", "waits", "io", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"]) await h.view(view);
  assert.deepEqual(live().filter((c) => c.path !== "/api/live/schemas" && c.path !== "/api/live/privileges"), [], "no view is read just by opening it");
  assert.equal(await page.$$eval(".source-list .count-badge, .source-list .status-dot", (els) => els.length), 0);

  // The shared database chooser participates in the kit's one-popover and
  // focus-return contract. Dismissing it does not navigate or load a report.
  await h.view("indexes");
  await page.click("#indexes-schemas");
  await page.waitForSelector('[role=dialog][aria-label="Choose databases"]');
  // Focus moves to the search once the popover is placed (the next frame).
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "Search databases");
  await page.$eval('button[aria-label="About Indexes"]', (e) => e.click());
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Choose databases"]'));
  assert.ok(await page.$('[role=dialog][aria-label="About Indexes"]'));
  await page.keyboard.press("Escape");
  assert.equal(await current(), "indexes");
  assert.deepEqual(live().filter((c) => c.path !== "/api/live/schemas" && c.path !== "/api/live/privileges"), []);

  // A badge from data the user loaded.
  await h.view("config");
  await h.refresh();
  await page.waitForSelector("#server-nav-config .status-dot");
  assert.equal(await page.$eval("#server-nav-config .status-dot", (e) => e.className), "status-dot is-watch");
  assert.match(await text("#server-nav-config .visually-hidden"), /Watch/);
  assert.equal(await page.$$eval(".source-list .status-dot", (els) => els.length), 1, "only the loaded view has a badge");

  // The inspector: closed at first; the toolbar toggle shows it with a placeholder.
  assert.equal(await page.$(".view-inspector"), null);
  const toggle = ".server-view .inspector-toggle";
  assert.equal(await page.$eval(toggle, (e) => e.getAttribute("aria-pressed")), "false");
  await page.click(toggle);
  await page.waitForSelector(".view-inspector .inspector-placeholder");
  assert.equal(await page.$eval(toggle, (e) => e.getAttribute("aria-pressed")), "true");
  assert.match(await text(".view-inspector"), /Select a setting/);
  // ⌥⌘I hides and shows it.
  await page.keyboard.down("Alt");
  await page.keyboard.down("Meta");
  await page.keyboard.press("KeyI");
  await page.keyboard.up("Meta");
  await page.keyboard.up("Alt");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  // Selecting a row opens it; Enter on a row too; Escape closes it and returns focus to the row.
  await h.select('.server-view li[data-check="buffer_pool"]');
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Buffer pool size");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.closest("li")?.dataset.check === "buffer_pool");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Sort buffer");

  // Resizing: the separator takes the arrow keys within limits, and the width is remembered.
  const start = await inspectorWidth();
  assert.equal(start, 340, "the default width");
  const separator = ".view-inspector [role=separator]";
  assert.equal(await page.$eval(separator, (e) => [e.getAttribute("aria-valuemin"), e.getAttribute("aria-valuemax")].join("-")), "280-560");
  await page.focus(separator);
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("ArrowLeft");
  await widthIs(start + 32);
  await page.keyboard.press("Home");
  await widthIs(280, "never narrower than 280 px");
  await page.keyboard.press("ArrowLeft");
  await widthIs(296);
  assert.equal(await page.evaluate(() => localStorage.getItem("myflames.inspector.width")), "296");
  // Dragging works too.
  const box = await page.$eval(separator, (e) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + 40 }; });
  await page.mouse.move(box.x, box.y);
  await page.mouse.down();
  await page.mouse.move(box.x - 60, box.y, { steps: 4 });
  await page.mouse.up();
  await widthIs(356);

  // ⓘ popovers: one at a time, Escape returns focus, a click outside closes.
  await page.click('button[aria-label="About Server"]');
  assert.equal(await page.$eval('button[aria-label="About Server"]', (e) => e.getAttribute("aria-expanded")), "true");
  const pop = await page.$eval('[role=dialog][aria-label="About Server"]', (e) => { const r = e.getBoundingClientRect(); return [r.width, r.right]; });
  assert.ok(pop[0] <= 320 && pop[1] <= 1440, `popover fits (${pop})`);
  // (The open popover may cover the other button, so it is activated directly.)
  await page.$eval('button[aria-label="About Configuration"]', (e) => e.click());
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Server"]'));
  assert.ok(await page.$('[role=dialog][aria-label="About Configuration"]'));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "About Configuration");
  assert.ok(await page.$(".view-inspector"), "Escape in a popover leaves the inspector open");
  await page.click('button[aria-label="About Configuration"]');
  await page.mouse.click(700, 20);
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Configuration"]'));
  // Notes: one button with a count.
  assert.match(await text(".server-view .notes-button"), /^\d+ notes?$/);

  // A view that cannot run: one sentence and one action instead of a readiness grid.
  await h.view("waits");
  await h.refresh();
  await page.waitForSelector(".server-view .gate");
  const gate = await page.$eval(".server-view .gate", (e) => [e.querySelectorAll("p").length, e.querySelectorAll("button, a").length]);
  assert.deepEqual(gate, [1, 1]);

  // Session restore: the view and the open inspector; the width is a browser preference.
  await h.view("config");
  const saved = await h.savedSlow((s) => s.kind === "config" && s.inspector === true);
  assert.equal(saved.state.query_lab.slow.inspector, true);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector("#server-nav-config[aria-current=page]");
  await page.waitForSelector(".view-inspector .inspector-placeholder");
  await widthIs(356, "the width came back from this browser's storage");
  // Storage that throws does not break the page.
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
  });
  await page.focus(".view-inspector [role=separator]");
  await page.keyboard.press("ArrowRight");
  await widthIs(340);

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
  await h.server();
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
  // One loaded view: "1 of 1 view refreshed" (singular).
  await page.waitForFunction(() => /\b1 of 1 view refreshed/.test(document.querySelector(".refresh-all-status")?.textContent || ""));
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
  await phone.focus("#server-nav-sessions");
  await phone.keyboard.press("ArrowDown");
  await phone.waitForFunction(() => document.activeElement?.id === "server-nav-replication");
  assert.ok(await phone.$(".server-layout.show-list"), "arrows only move focus in the pushed list");
  // Sessions to Configuration: Replication, Waits, I/O, Locks, Indexes, Tables, Database diagram, Configuration.
  for (const id of ["waits", "io", "locks", "indexes", "tables", "diagram", "config"]) {
    await phone.keyboard.press("ArrowDown");
    await phone.waitForFunction((target) => document.activeElement?.id === `server-nav-${target}`, {}, id);
  }
  await phone.keyboard.press("Enter");
  await phone.waitForSelector(".server-layout:not(.show-list) .server-view[data-view=config]");
  await phone.waitForFunction(() => document.activeElement?.id === "config-title");
  await phone.locator(".server-view button ::-p-text(Load configuration)").click();
  await phone.waitForSelector(".server-view li[data-check]");
  // The inspector is a bottom sheet: focus moves in, Tab stays in, Escape closes it.
  // The last rows can sit under the fixed status bar until the page scrolls.
  await phone.$eval('.server-view li[data-check="slow_log"] button', (e) => e.scrollIntoView({ block: "center" }));
  await phone.click('.server-view li[data-check="slow_log"] button');
  await phone.waitForSelector(".view-inspector.is-sheet");
  const sheet = await phone.$eval(".view-inspector.is-sheet", (el) => {
    const animation = el.getAnimations()[0];
    animation?.pause();
    if (animation) animation.currentTime = 90;
    const css = getComputedStyle(el);
    const result = { opacity: css.opacity, background: css.backgroundColor, fades: animation?.effect.getKeyframes().some((frame) => frame.opacity !== undefined) || false };
    animation?.finish();
    return result;
  });
  assert.equal(sheet.opacity, "1", "the sheet stays opaque halfway through its slide");
  assert.equal(sheet.background, "rgb(255, 255, 255)");
  assert.equal(sheet.fades, false);
  await phone.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  assert.equal(await phone.$eval(".view-inspector.is-sheet", (el) => getComputedStyle(el).animationName), "none");
  await phone.emulateMediaFeatures([]);
  await phone.waitForFunction(() => document.activeElement?.classList.contains("inspector-close"));
  for (let i = 0; i < 12; i++) await phone.keyboard.press("Tab");
  assert.ok(await phone.evaluate(() => !!document.activeElement?.closest(".view-inspector")), "focus stays in the sheet");
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await phone.screenshot({ path: path.join(h.shots, "server-sheet-390.png") });
  await phone.keyboard.press("Escape");
  await phone.waitForFunction(() => !document.querySelector(".view-inspector"));
  await phone.waitForFunction(() => document.activeElement?.closest("li")?.dataset.check === "slow_log");
  // Back returns to the list with focus on the view it came from.
  await phone.click(".server-back");
  await phone.waitForSelector(".server-layout.show-list");
  await phone.waitForFunction(() => document.activeElement?.id === "server-nav-config");
  await phone.close();

  assert.deepEqual(h.errors, []);
  console.log("server navigation: ok");
} finally {
  await h.close();
}
