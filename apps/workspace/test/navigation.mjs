import { connectionAction, connectionRow } from "./connection-helpers.mjs";
import { chooseView, openConnectionPicker, openViewList } from "./chrome-helpers.mjs";
// Workspace navigation: places and their active state, the open-plans list,
// the single status indicator and the Settings popover with the Audit log
// (a screen with no active place) and About myflames
// (version, license, credits), the Production / Read-only strip across the
// Query editor and Server with the picker's type dot, Home's Connections
// (add, edit, test, use, forget, delete), the connection picker in the Query
// editor and Server, keyboard navigation, and the narrow layout's drawer.
// Mocked profile/connection API, real Python server for the page and plans.
// Run after `npm run build`: node test/navigation.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-navigation-"));
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
let items = [
  {
    id: "p1", name: "Prod", host: "db.prod", port: 3306, user: "analyst",
    database: "shop", ssl_mode: "REQUIRED", ssl_ca: "",
    has_saved_password: true, secret_backend: "macos-keychain", secret_backend_label: "macOS Keychain",
  },
  {
    id: "p2", name: "Staging", host: "db.staging", port: 3307, user: "dev",
    database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development", read_only: false,
    has_saved_password: false, secret_backend: "", secret_backend_label: "",
  },
];
let investigations = [];
/** pathname -> { when(body), promise }: matching replies wait for the promise (slow-reply cases). */
const gates = {};
const hold = (pathname, when = () => true) => {
  let release;
  gates[pathname] = { when, promise: new Promise((resolve) => { release = resolve; }) };
  return () => { delete gates[pathname]; release(); };
};
const rawPlan = await fs.readFile(path.join(root, "test/mysql-explain-json-sample.json"), "utf8");
const seen = {};
const handlers = {
  // Like ui.py: summaries newest first (updated_at, then id), filtered, in
  // keyset pages; "get" returns the bundle.
  "/api/investigations/list": (body) => {
    const needle = (body.search || "").trim().toLowerCase();
    const key = (b) => [b.updated_at, b.id];
    const before = (a, b) => a[0] < b[0] || (a[0] === b[0] && a[1] < b[1]);
    const rows = investigations
      .filter((b) => `${b.name} ${b.tags.join(" ")} ${b.notes}`.toLowerCase().includes(needle))
      .filter((b) => !body.cursor || before(key(b), [body.cursor.updated_at, body.cursor.id]))
      .sort((a, b) => (before(key(a), key(b)) ? 1 : -1))
      .map(({ plans, ...rest }) => ({ ...rest, schema_version: "investigation-summary-1.0",
        plans: plans.map((p) => ({ id: p.id, name: p.name })) }));
    const items = rows.slice(0, body.limit);
    const last = items[items.length - 1];
    return { schema_version: "investigation-list-1.0", items,
      next_cursor: rows.length > body.limit ? { updated_at: last.updated_at, id: last.id } : null };
  },
  "/api/investigations/get": (body) => investigations.find((b) => b.id === body.id),
  "/api/investigations/delete": (body) => {
    investigations = investigations.filter((b) => b.id !== body.id);
    return { deleted: true };
  },
  "/api/profiles/list": () => ({ items, secret_store: store }),
  "/api/profiles/save": (body) => {
    const id = body.id || "p" + (items.length + 1);
    const saved = {
      ...body.value,
      id,
      has_saved_password: !!body.remember_password,
      secret_backend: body.remember_password ? store.backend : "",
      secret_backend_label: body.remember_password ? store.label : "",
    };
    items = items.some((p) => p.id === id)
      ? items.map((p) => (p.id === id ? saved : p))
      : [...items, saved];
    return saved;
  },
  "/api/profiles/forget-password": (body) => {
    items = items.map((p) =>
      p.id === body.id ? { ...p, has_saved_password: false, secret_backend: "", secret_backend_label: "" } : p,
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
  const downloads = await page.createCDPSession();
  await downloads.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: directory });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", async (request) => {
    const pathname = new URL(request.url()).pathname;
    const handler = handlers[pathname];
    if (!handler) return request.continue();
    const body = JSON.parse(request.postData() || "{}");
    (seen[pathname] = seen[pathname] || []).push(body);
    const reply = JSON.stringify(handler(body));
    const gate = gates[pathname];
    if (gate && gate.when(body)) await gate.promise;
    try { await request.respond({ status: 200, contentType: "application/json", body: reply }); }
    catch { /* the page aborted the request while it was held */ }
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  const button = (text, scope = "") => page.locator(`${scope}button ::-p-text(${text})`).click();
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const texts = (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));
  const go = (label) => button(label, ".main-nav ");
  const active = () => page.$$eval(".main-nav button.active", (els) => els.map((e) => e.textContent));
  const current = () =>
    page.$$eval(".main-nav [aria-current=page]", (els) => els.map((e) => e.textContent));
  const visible = (selector) =>
    page.$eval(selector, (e) => !!(e.offsetWidth || e.offsetHeight)).catch(() => false);
  const last = (pathname) => seen[pathname].at(-1);
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth > innerWidth);

  // 1. Places: nine, in order, each with its own icon.
  const places = ["Home", "Server", "Workload", "Replication", "Tuning", "Query editor", "Plans", "Investigations", "Learn"];
  assert.deepEqual(await texts(".main-nav button"), places);
  const icons = await page.$$eval(".main-nav button svg", (els) => els.map((e) => e.innerHTML));
  assert.equal(icons.length, 9);
  assert.equal(new Set(icons).size, 9, "every place has a distinct icon");
  assert.equal(await page.$eval(".brand", (e) => e.getAttribute("aria-label")), "myflames Home");
  assert.equal(await page.title(), "myflames · Performance workspace");

  // 2. Active state means "you are here": the empty welcome is Home, not Plans.
  assert.deepEqual(await active(), ["Home"]);
  assert.deepEqual(await current(), ["Home"]);
  assert.ok(await visible(".welcome"));
  assert.match(await text(".topbar-title"), /^Home/);
  await page.waitForSelector('.home [aria-label="Get started"]');
  assert.equal((await page.$$('.topbar button[aria-label="Import plan"]')).length, 1);
  // Home has one Import plan (the top bar, also with recent work) and one
  // primary action (Add connection); the empty state offers the sample.
  assert.equal(await page.$$eval('button', (els) => els.filter((el) => el.textContent.trim() === 'Import plan' && el.getClientRects().length).length), 1, "one Import plan on Home");
  assert.equal(await page.$('.topbar [aria-label="Import plan"].primary'), null, "Import plan is secondary on Home");
  assert.deepEqual(await page.$$eval('.home button.primary, .topbar button.primary', (els) => els.filter((el) => el.getClientRects().length).map((el) => el.textContent.trim())), ["Add connection"], "one primary action on Home");
  assert.equal((await page.$$('.home-start button ::-p-text(Explore sample plan)')).length, 1, "empty state offers the sample");
  assert.equal(await page.$('.topbar [aria-label="New query tab"]'), null, "New query tab belongs to the editor");
  assert.equal((await page.$$('.home [aria-label="Recent work"]')).length, 1, "one recent list");
  assert.equal(await page.$('.home-capabilities'), null);
  assert.equal((await page.$$('.home .sample-card')).length, 1);
  assert.equal(await page.$$eval('.home .connection-row', rows => rows.every(row => row.getBoundingClientRect().height <= 40)), true);
  assert.equal(await page.$eval('.recent-work', el => el.getBoundingClientRect().top < innerHeight), true);
  const screens = {
    Plans: ".plans-empty",
    "Query editor": ".query-lab",
    Server: ".server-place",
    Workload: ".server-place",
    Replication: ".replication-place",
    Tuning: ".server-place.is-tuning-place .server-view[data-view=tuning]",
    Investigations: '[aria-label="Search investigations"]',
    Learn: ".teach-workspace",
    Home: ".home",
  };
  const sidebarWidths = new Set();
  for (const [label, selector] of Object.entries(screens)) {
    await go(label);
    await page.waitForFunction(
      (s) => { const e = document.querySelector(s); return !!(e && (e.offsetWidth || e.offsetHeight)); },
      {},
      selector,
    );
    assert.deepEqual(await active(), [label], `${label} is the only active place`);
    assert.deepEqual(await current(), [label]);
    assert.match(await text(".topbar-title"), new RegExp("^" + label));
    // The place is named once: the top bar title is not a menu of places or
    // views (the sidebar and the list of views are), and there is one sidebar toggle.
    assert.equal(await page.$$eval(".topbar-title button", (els) => els.length), 0, `${label}: the top bar title is plain text`);
    assert.equal(await page.$$eval(".topbar .icon-button[aria-controls=workspace-sidebar]", (els) => els.length), 1, `${label}: one sidebar toggle`);
    // Exactly one place looks selected: the current one has the selection
    // fill, every other place (a badge dot included) keeps a clear background.
    // (Hover and selection fade in 120 ms; the mouse moves off the list first.)
    await page.mouse.move(900, 600);
    const filled = () => page.$$eval(".main-nav button", (els) => els
      .filter((e) => !["rgba(0, 0, 0, 0)", "transparent"].includes(getComputedStyle(e).backgroundColor)).map((e) => e.textContent));
    await page.waitForFunction((l) => {
      const on = [...document.querySelectorAll(".main-nav button")].filter((e) => !["rgba(0, 0, 0, 0)", "transparent"].includes(getComputedStyle(e).backgroundColor));
      return on.length === 1 && on[0].textContent === l;
    }, { timeout: 3000 }, label).catch(() => {});
    assert.deepEqual(await filled(), [label], `${label}: one place has the selection fill`);
    // A mouse click leaves no focus ring on the place.
    assert.equal(await page.$eval(".main-nav button.active", (e) => e.matches(":focus-visible") || getComputedStyle(e).outlineStyle !== "none"), false,
      `${label}: no focus ring after a click`);
    // One sidebar width for every place: the chrome never shifts.
    sidebarWidths.add(await page.$eval(".sidebar", (e) => Math.round(e.getBoundingClientRect().width)));
    // The top bar stays light: no Local badge (privacy is in About myflames)
    // and no Privileges button (Account and privileges is in the picker).
    assert.equal(await page.$(".topbar .local-badge"), null, `${label}: no Local badge`);
    assert.equal(await page.$('.window-toolbar button[aria-label="Privileges"], .window-toolbar .privileges-button'), null, `${label}: no Privileges button`);
    // Server and Workload: Refresh all follows the connection picker in the
    // top bar, above the bar's hairline, and never sits over a view's Refresh.
    if (label === "Server" || label === "Workload") {
      const geometry = await page.evaluate(() => {
        const box = (s) => { const e = [...document.querySelectorAll(s)].find((x) => x.getClientRects().length); return e ? e.getBoundingClientRect() : null; };
        const all = box(".server-place [data-action=refresh-all]");
        const own = box(".server-view [data-action=refresh]");
        const bar = box(".window-toolbar");
        const picker = box(".window-toolbar .connection-picker");
        return { all: all && { left: all.left, right: all.right, bottom: all.bottom, icon: !!document.querySelector(".server-place [data-action=refresh-all] svg") },
          own: own && { left: own.left, right: own.right }, barBottom: bar.bottom, pickerRight: picker && picker.right,
          toolbarBottom: box(".server-place > .place-toolbar").bottom };
      });
      assert.ok(geometry.all, `${label}: Refresh all is shown`);
      assert.equal(geometry.all.icon, false, `${label}: Refresh all is a text button, unlike a view's Refresh`);
      if (geometry.pickerRight !== null) assert.ok(geometry.all.left >= geometry.pickerRight, `${label}: Refresh all follows the picker ${JSON.stringify(geometry)}`);
      if (geometry.own) assert.ok(geometry.all.right <= geometry.own.left || geometry.all.left >= geometry.own.right, `${label}: Refresh all is not stacked over Refresh ${JSON.stringify(geometry)}`);
      assert.ok(geometry.toolbarBottom <= geometry.barBottom - 1, `${label}: the top bar keeps its hairline ${JSON.stringify(geometry)}`);
    }
    // Tuning is a place of its own: no Server list of views, no Server toolbar.
    if (label === "Tuning") {
      assert.equal(await page.$eval(".server-place.is-tuning-place", (e) => !!e.querySelector(".source-list, .place-toolbar")), false,
        "Tuning shows no Server navigation");
      assert.ok(await page.$('.window-statusbar[aria-label="Workspace status"] [data-status="safety"]'), "Tuning has the window footer");
    }
  }
  assert.equal(sidebarWidths.size, 1, `one sidebar width: ${[...sidebarWidths]}`);
  // The command palette lists the places in the sidebar's order, Compare
  // under Plans (a mode, not a place) and the Audit log with its own icon.
  await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
  await page.waitForSelector("dialog.command-palette[open]");
  const commands = await page.$$eval(".command-option", (els) => els.map((e) => [e.querySelector("span")?.textContent, e.querySelector("small")?.textContent]));
  assert.deepEqual(commands.filter(([, g]) => g === "Places").map(([l]) => l), places, "palette places follow the sidebar");
  assert.deepEqual(commands.find(([l]) => l === "Compare"), ["Compare", "Plans"]);
  assert.equal(commands.some(([l]) => l === "Compare plans"), false);
  // Account and privileges is a palette action as well as a picker item.
  await page.type('input[aria-label="Search commands"]', "privileges");
  assert.deepEqual(await page.$$eval(".command-option", (els) => els.map((e) => [e.querySelector("span")?.textContent, e.querySelector("small")?.textContent])),
    [["Account and privileges", "Actions"]]);
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.account-sheet[open]");
  assert.equal(await text("dialog.account-sheet h2"), "Account and privileges");
  await page.waitForFunction(() => document.activeElement?.textContent === "Close" && document.activeElement.closest("dialog.account-sheet"));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.account-sheet"));
  await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "audit");
  assert.deepEqual(await texts(".command-option span"), ["Audit log"]);
  const auditIcon = await page.$eval(".command-option svg", (e) => e.innerHTML);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector("#settings-popover");
  assert.equal(await page.$$eval("#settings-popover .settings-item", (els) => els.find((e) => e.textContent.trim() === "Audit log")?.querySelector("svg")?.innerHTML),
    auditIcon, "the palette uses the Audit log's own icon");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  // The keyboard focus ring is visible and is not the selection fill.
  await page.focus('.main-nav button[data-place="server"]');
  await page.keyboard.press("ArrowDown");
  const ring = await page.$eval('.main-nav button[data-place="workload"]', (e) => ({
    focused: document.activeElement === e && e.matches(":focus-visible"),
    outline: getComputedStyle(e).outlineStyle,
    active: e.classList.contains("active"),
  }));
  assert.deepEqual(ring, { focused: true, outline: "solid", active: false }, "keyboard focus draws a ring on a place that is not selected");
  // Server never shows the Tuning view: Server opens its own last view.
  await go("Tuning");
  await page.waitForSelector(".server-place.is-tuning-place .server-view[data-view=tuning]");
  await go("Server");
  // The list of views floats by default: the view's title opens it.
  await page.waitForFunction(() => [...document.querySelectorAll(".server-place:not(.is-tuning-place) .server-view .view-switcher")]
    .some((e) => !e.closest("[hidden]") && e.offsetParent !== null));
  assert.equal(await page.$(".server-place:not(.is-tuning-place) .server-view[data-view=tuning]"), null, "Server does not embed Tuning");
  await openViewList(page);
  assert.equal(await page.$$eval(".source-list .source-item", (els) => els.some((e) => e.dataset.view === "tuning")), false, "Tuning is not a Server view");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-switcher-popover"));
  await go("Home");
  assert.equal(await page.$(".welcome") !== null, true);
  // Plans has its own empty state, with Import plan once (in its bar).
  await go("Plans");
  assert.deepEqual(await texts(".plans-bar .segmented button"), ["Explore", "Compare"]);
  assert.equal((await page.$$("button ::-p-text(Import plan)")).length, 1);
  assert.equal(await page.$(".welcome"), null, "the welcome is not part of Plans");

  // 3. One status indicator, no Local badge, no "Local workspace" line.
  assert.equal((await page.$$(".session-status")).length, 1);
  assert.equal((await page.$$(".local-badge")).length, 0);
  assert.equal(await page.evaluate(() => document.body.textContent.includes("Local workspace")), false);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  assert.equal(await text(".session-status"), "All changes saved");

  // 4. Settings: a popover with the startup option; Escape returns focus.
  const settings = 'button[aria-label="Settings"]';
  assert.equal(await page.$eval(settings, (e) => e.getAttribute("aria-expanded")), "false");
  await page.click(settings);
  await page.waitForSelector("#settings-popover[role=dialog]");
  assert.equal(await page.$eval(settings, (e) => e.getAttribute("aria-expanded")), "true");
  assert.match(await text("#settings-popover"), /Restore workspace on startup/);
  // Saved passwords exist (Keychain); they are never part of the workspace.
  assert.match(await text("#session-toggle-hint"), /Passwords are never part of the workspace\./);
  assert.equal(await page.$eval("#settings-popover .session-toggle input", (e) => e.checked), true);
  assert.equal(
    await page.evaluate(() => document.activeElement?.closest("#settings-popover") !== null),
    true,
    "focus moves into Settings",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Settings");
  await page.click(settings);
  await page.waitForSelector("#settings-popover");
  await page.mouse.click(900, 500);
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));

  // 4b. About myflames: the credit lives in an About window opened from the
  // Settings menu, not in a footer on every screen.
  assert.equal(await page.$(".app-footer"), null);
  assert.equal(
    await page.evaluate(() => document.body.textContent.includes("Inspired by")),
    false,
    "no credit footer on every screen",
  );
  const about = "dialog.about-dialog";
  const focused = () => page.evaluate(() => document.activeElement?.textContent.trim());
  const focusLabel = () => page.evaluate(() => document.activeElement?.getAttribute("aria-label"));
  await page.click(settings);
  await page.waitForSelector("#settings-popover");
  // Keyboard reachable: Tab from the startup option reaches the menu items.
  await page.keyboard.press("Tab");
  assert.equal(await focused(), "Audit log");
  await page.keyboard.press("Tab");
  assert.equal(await focused(), "About myflames");
  await page.keyboard.press("Enter");
  await page.waitForSelector(about + "[open]");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  assert.deepEqual(
    await page.$eval(about, (e) => ({
      role: e.getAttribute("role"),
      modal: e.getAttribute("aria-modal"),
      title: document.getElementById(e.getAttribute("aria-labelledby"))?.textContent,
    })),
    { role: "dialog", modal: "true", title: "myflames" },
  );
  assert.equal(
    await page.evaluate((s) => document.activeElement?.closest(s) !== null, about),
    true,
    "focus moves into About",
  );
  assert.equal(
    await page.$eval(about + " .about-mark", (e) => e.getAttribute("src")),
    await page.$eval(".brand img", (e) => e.getAttribute("src")),
    "the same app mark as the sidebar",
  );
  const aboutText = await text(about);
  // Each fact once: the subtitle names the product, the facts the databases.
  assert.equal(aboutText.split("Performance workspace").length - 1, 1, "Performance workspace appears once");
  assert.equal(aboutText.split("MySQL and MariaDB").length - 1, 1, "MySQL and MariaDB appears once");
  assert.equal(await page.$eval(about + " .about-close", (e) => e.classList.contains("primary")), true, "Close is the default button");
  const version = await page.$eval('meta[name="myflames-version"]', (e) => e.content);
  assert.match(version, /^\d+\.\d+/, "the server fills in the package version");
  // About lists the version as a labelled field ("Version" row), the span holds the number.
  assert.equal(await text(about + " .about-version"), version);
  assert.equal(
    await text(about + " .about-credits p"),
    "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.",
  );
  assert.deepEqual(
    await page.$$eval(about + " .about-credits a", (els) =>
      els.map((a) => [a.textContent, a.href, a.target, a.rel]),
    ),
    [
      ["Brendan Gregg's FlameGraph", "https://github.com/brendangregg/FlameGraph", "_blank", "noopener noreferrer"],
      [
        "Tanel Poder's SQL Plan FlameGraphs",
        "https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/",
        "_blank",
        "noopener noreferrer",
      ],
    ],
  );
  assert.match(aboutText, /LicenseApache-2\.0/); // a labelled field row since the About redesign
  assert.match(aboutText, /CDDL 1\.0 \(see NOTICE\)/);
  // One privacy line names what the app contacts, and when.
  assert.equal(await text(about + " .about-privacy p"),
    "myflames contacts only your database servers, official release pages when you check for updates, and news feeds when shown and turned on.");
  // Focus starts on Close and stays inside: Tab wraps to the first control
  // (Check for updates in the Updates row), Shift+Tab wraps back.
  assert.equal(await focused(), "Close");
  await page.keyboard.press("Tab");
  assert.equal(await focused(), "Check for updates");
  await page.keyboard.down("Shift");
  await page.keyboard.press("Tab");
  await page.keyboard.up("Shift");
  assert.equal(await focused(), "Close");
  // Escape closes it and focus returns to Settings.
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, about);
  assert.equal(await focusLabel(), "Settings");
  // The Close button and a click on the backdrop close it too.
  const openAbout = async () => {
    await page.click(settings);
    await page.waitForSelector("#settings-popover");
    await page.locator("#settings-popover button ::-p-text(About myflames)").click();
    await page.waitForSelector(about + "[open]");
  };
  await openAbout();
  await page.locator(about + " button ::-p-text(Close)").click();
  await page.waitForFunction((s) => !document.querySelector(s), {}, about);
  assert.equal(await focusLabel(), "Settings");
  await openAbout();
  await page.mouse.click(10, 10);
  await page.waitForFunction((s) => !document.querySelector(s), {}, about);
  assert.equal(await focusLabel(), "Settings");

  // 4c. The Audit log opens from Settings as a screen of its own: no place is
  // "you are here", the top bar names it, and any place leads back.
  await page.click(settings);
  await page.waitForSelector("#settings-popover");
  await page.locator("#settings-popover button ::-p-text(Audit log)").click();
  await page.waitForSelector(".server-view[data-view=audit]");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
  assert.deepEqual(await active(), []);
  assert.deepEqual(await current(), []);
  assert.match(await text(".topbar-title"), /^Audit log/);
  assert.equal(await page.$('.window-statusbar [data-status="safety"]'), null, "the Audit log reads no server: no safety state");
  await go("Home");
  assert.deepEqual(await active(), ["Home"]);

  // 4d. The connection in use (the unsaved default) is Production: the window
  // footer of every place that sends to it starts with that state (a dot and
  // words, named for screen readers); Home, Plans and Learn have none, and
  // nothing is drawn over the top bar.
  assert.equal(await page.$('[data-status="safety"]'), null, "no safety state on Home");
  assert.equal(await page.$(".safety-strip"), null, "no strip under the top bar");
  const safety = '.window-statusbar[aria-label="Workspace status"] > [data-status="safety"]:first-child';
  for (const label of ["Query editor", "Server", "Workload", "Replication", "Tuning"]) {
    await go(label);
    await page.waitForSelector(`${safety}.is-production`);
    assert.equal(await text(`${safety} strong`), "Production");
    assert.match(await text(safety), /^Production\s*· changes ask first$/);
    // The sentence names the note; Retry sits outside it as its own button.
    assert.equal(await page.$eval(`${safety} [role=note]`, (e) => e.getAttribute("aria-label")), "Production: every change on Local MySQL asks first");
    assert.equal(await page.$eval(safety, (e) => e.getAttribute("data-safety")), "production");
    assert.equal(await page.$$eval(".window-toolbar", (els) => els.length), 1);
    assert.equal(await page.$eval(".workspace", (e) => e.classList.contains("has-safety-strip")), false);
    // The footer sits below the views: it never covers the top bar.
    const box = await page.$eval(".window-statusbar", (e) => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; });
    const topbar = await page.$eval(".window-toolbar", (e) => e.getBoundingClientRect().bottom);
    assert.ok(box.top > topbar && Math.round(box.bottom) <= 1000, `${label}: the footer is at the bottom (${JSON.stringify(box)})`);
    // Real values only: the connection in use; nothing read yet, so no
    // server version and no refresh time. Unknown fields are hidden.
    await page.waitForFunction(() => document.querySelector('.window-statusbar [data-status="connection"]')?.textContent === "Local MySQL (127.0.0.1:3306)");
    if (label === "Query editor")
      await page.waitForFunction(() => document.querySelector('.window-statusbar [data-status="session"]')?.textContent === "No session");
    const fields = await page.$$eval(".window-statusbar [data-status]", (els) => els.map((e) => e.dataset.status));
    assert.deepEqual(fields, label === "Query editor" ? ["safety", "connection", "session"] : ["safety", "connection"], `${label} status fields`);
    assert.doesNotMatch(await text(".window-statusbar"), /unknown|not checked|not refreshed/i, "unknown fields are hidden");
    assert.equal(await page.$eval(".connection-picker .type-dot", (e) => e.getAttribute("aria-label")), "Production");
  }
  for (const label of ["Plans", "Learn", "Investigations", "Home"]) {
    await go(label);
    assert.equal(await page.$('[data-status="safety"]'), null, `no safety state on ${label}`);
  }

  // 5. Open plans: a quiet list with a count; Baseline only with 2+ plans.
  assert.match(await text(".open-plans .section-label"), /Open plans\s*0/i);
  assert.match(await text(".sidebar-empty"), /appear here/);
  await go("Home");
  await button("Explore sample plan");
  await page.waitForFunction(() => document.querySelector(".plan-heading h1")?.textContent === "sample-plan.json");
  assert.deepEqual(await active(), ["Plans"]);
  // The top bar names the place once; the plan header names the plan, and
  // the Explore | Compare switch and Import plan sit in the top bar.
  assert.equal(await text(".topbar-title"), "Plans");
  assert.ok(await page.$(".topbar .plans-bar .segmented"), "Explore | Compare is in the top bar");
  assert.ok(await page.$(".topbar .plans-bar .plans-import"), "Import plan is in the top bar");
  // The plan summary and SQL are disclosures in the plan header, not bands.
  assert.ok(await page.$(".plan-heading .primary-finding > summary"));
  assert.equal(await page.$(".plan-screen > .plan-strip"), null, "no strip below the plan header");
  // Each opens a panel in one click and closes like a popover: the other
  // panel, a click elsewhere, or Escape (focus returns to its summary).
  const openPanel = (sel) => page.$eval(sel, (e) => e.open);
  await page.click(".plan-heading .primary-finding > summary");
  assert.equal(await openPanel(".plan-heading .primary-finding"), true);
  assert.ok(await page.$eval(".plan-heading .primary-finding .plan-header-panel", (e) => e.getBoundingClientRect().right <= innerWidth), "the panel stays on screen");
  if (await page.$(".plan-heading .sql-card")) {
    await page.click(".plan-heading .sql-card > summary");
    assert.equal(await openPanel(".plan-heading .sql-card"), true);
    assert.equal(await openPanel(".plan-heading .primary-finding"), false, "opening SQL closes the summary");
    await page.keyboard.press("Escape");
    assert.equal(await openPanel(".plan-heading .sql-card"), false);
    assert.equal(await page.evaluate(() => document.activeElement?.closest(".sql-card") !== null), true, "focus returns to the SQL summary");
    // The keyboard gets the same one-panel rule.
    await page.focus(".plan-heading .sql-card > summary");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".plan-heading .sql-card").open);
    await page.focus(".plan-heading .primary-finding > summary");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".plan-heading .primary-finding").open && !document.querySelector(".plan-heading .sql-card").open);
  }
  await page.click(".plan-heading h1");
  assert.equal(await openPanel(".plan-heading .primary-finding"), false, "a click elsewhere closes the panel");
  assert.equal(await page.$(".save-state"), null, "sample plan is not an edited investigation");
  assert.match(await text(".open-plans .section-label"), /Open plans\s*1/i);
  assert.match(await text(".plan-item .plan-select small"), /ms/);
  assert.equal(await page.$(".plan-item .visually-hidden"), null, "no Baseline badge with one plan");
  await button("Import plan", ".plans-bar ");
  await page.waitForSelector("dialog[open]");
  await page.focus("dialog textarea");
  await page.keyboard.sendCharacter(await fs.readFile(path.join(root, "test/mariadb-explain-join.json"), "utf8"));
  await page.locator("dialog .field input").fill("after.json");
  await button("Analyze plan");
  await page.waitForFunction(() => document.querySelector(".plan-heading h1")?.textContent === "after.json");
  assert.match(await text(".open-plans .section-label"), /Open plans\s*2/i);
  assert.equal(await page.$(".save-state"), null, "an imported plan is read-only until an investigation is edited");
  assert.match(await text(".window-statusbar"), /Imported plan/);
  assert.deepEqual(await texts(".plan-item .visually-hidden"), ["Baseline"]);
  assert.match(await text(".plan-item:first-child"), /sample-plan\.json.*Baseline/);
  // The open plan is highlighted only while Plans explores it.
  assert.deepEqual(await texts(".plan-item.selected strong"), ["after.json"]);
  await button("Compare", ".plans-bar ");
  await page.waitForSelector(".comparison-row");
  assert.deepEqual(await active(), ["Plans"]);
  assert.equal((await page.$$(".plan-item.selected")).length, 0);
  await go("Home");
  assert.equal((await page.$$(".plan-item.selected")).length, 0);
  // Plans remembers Compare; Explore returns to the plan.
  await go("Plans");
  await page.waitForSelector(".comparison-row");
  await button("Explore", ".plans-bar ");
  // Row actions appear on hover or keyboard focus.
  const removeFirst = '.plan-item:first-child button[aria-label="Remove sample-plan.json"]';
  assert.equal(await page.$eval(removeFirst, (e) => getComputedStyle(e).opacity), "0");
  await page.focus(removeFirst);
  await page.waitForFunction((s) => getComputedStyle(document.querySelector(s)).opacity === "1", {}, removeFirst);
  await page.locator(".plan-select ::-p-text(sample-plan.json)").click();
  await page.waitForFunction(() => document.querySelector(".plan-heading h1")?.textContent === "sample-plan.json");
  assert.deepEqual(await texts(".plan-item.selected strong"), ["sample-plan.json"]);
  // Save investigation lives in the top bar with its own status words.
  assert.equal(await page.$(".topbar .save-state"), null, "viewing and importing plans does not edit an investigation");
  assert.match(await text(".window-statusbar"), /Sample plan/);
  for (const place of ["Query editor", "Server", "Workload", "Investigations", "Learn"]) {
    await go(place);
    assert.equal(await page.$('.topbar [aria-label="Save investigation"]'), null, place + " has no investigation action");
    assert.equal(await page.$('.topbar .save-state'), null, place + " has no investigation status");
  }
  await go("Plans");
  assert.equal(await page.$(".investigation-notes button ::-p-text(Save investigation)"), null);

  // Investigations: selection reveals one detail pane, with search and actions.
  investigations = [
    { schema_version: "investigation-1.0", id: "older", name: "Inventory review", notes: "Stock levels",
      tags: ["inventory"], baseline_id: "only", updated_at: "2026-10-10T12:00:00Z",
      plans: [{ id: "only", name: "inventory.json", raw: rawPlan }] },
    { schema_version: "investigation-1.0", id: "newer", name: "Orders review", notes: "Check the customer lookup",
      tags: ["orders", "release"], baseline_id: "before", updated_at: "2026-10-14T12:00:00Z",
      plans: [{ id: "before", name: "before.json", raw: rawPlan }, { id: "after", name: "after.json", raw: rawPlan }] },
  ];
  await go("Investigations");
  await page.waitForSelector('.library-item[aria-current="true"]');
  assert.deepEqual(await texts(".library-item strong"), ["Orders review", "Inventory review"]);
  assert.equal(await text(".library-detail h2"), "Orders review");
  assert.match(await text(".library-notes"), /Check the customer lookup/);
  assert.deepEqual(await texts(".library-tags .pill"), ["orders", "release"]);
  assert.deepEqual(await texts(".library-plans .pill"), ["Baseline"]);
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await overflow(), false, "populated investigations fit at 390 px");
  await page.screenshot({ path: path.join(directory, "investigations-populated-mobile.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.focus('.library-item[aria-current="true"]');
  await page.keyboard.press("ArrowDown");
  assert.equal(await text(".library-detail h2"), "Inventory review");
  assert.equal(await page.$eval('.library-actions button:nth-child(2)', (e) => e.disabled), true);
  assert.equal((await page.$$(".library-plans .pill")).length, 0, "one plan has no baseline badge");
  await page.locator('[aria-label="Search investigations"]').fill("customer");
  assert.deepEqual(await texts(".library-item strong"), ["Orders review"], "notes are searchable");
  assert.equal(await text(".library-detail h2"), "Orders review");
  await button("Export", ".library-actions ");
  let exported;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { exported = JSON.parse(await fs.readFile(path.join(directory, "Orders-review.json"), "utf8")); break; }
    catch { await new Promise((resolve) => setTimeout(resolve, 50)); }
  }
  assert.deepEqual(exported, investigations.find((b) => b.id === "newer"), "export preserves notes, tags, plans, and baseline");
  // The list carries summaries in bounded pages; Export reads the complete bundle.
  assert.ok(seen["/api/investigations/list"].every((b) => Number.isInteger(b.limit) && b.limit >= 1 && b.limit <= 50));
  assert.equal(last("/api/investigations/get").id, "newer");
  // After a short typing pause the server searches every saved investigation.
  let listReads;
  for (let attempt = 0; attempt < 100 && last("/api/investigations/list").search !== "customer"; attempt++)
    await new Promise((resolve) => setTimeout(resolve, 20));
  listReads = seen["/api/investigations/list"].length;
  assert.equal(last("/api/investigations/list").search, "customer", "the server searches every saved investigation");
  // The server gets the search trimmed as the list filters it (JavaScript trim, U+FEFF included).
  await page.locator('[aria-label="Search investigations"]').fill("\uFEFF customer ");
  const trimmedRead = () => seen["/api/investigations/list"].slice(listReads).some((b) => b.search === "customer");
  for (let attempt = 0; attempt < 100 && !trimmedRead(); attempt++)
    await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(trimmedRead(), "the search is sent trimmed");
  assert.ok(seen["/api/investigations/list"].slice(listReads).every((b) => b.search === b.search.trim()));
  assert.deepEqual(await texts(".library-item strong"), ["Orders review"]);
  await button("Compare to baseline", ".library-actions ");
  await page.waitForSelector('iframe[title="Baseline comparison"]');
  assert.equal(await page.$eval('iframe[title="Baseline comparison"]', (e) => e.getAttribute("sandbox")), "allow-scripts");
  await button("Close comparison", ".library-comparison ");
  assert.equal(await page.$('iframe[title="Baseline comparison"]'), null);
  await page.locator('[aria-label="Search investigations"]').fill("no results");
  assert.equal((await page.$$(".library-item")).length, 0);
  assert.equal(await page.$(".library-detail h2"), null, "a filtered out collection has no stale detail");
  await button("Clear search", ".library ");
  await button("Delete", ".library-actions ");
  await page.waitForSelector("dialog.confirm-sheet[open]");
  assert.equal(await page.evaluate(() => document.activeElement.textContent.trim()), "Cancel");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"));
  await page.waitForFunction(() => document.activeElement?.textContent.trim() === "Delete");
  assert.equal((seen["/api/investigations/delete"] || []).length, 0);
  // Selecting an investigation never opens its plans; Open all does so explicitly.
  assert.deepEqual(await active(), ["Investigations"]);
  const reads = (seen["/api/investigations/get"] || []).length;
  await button("Open all", ".library-actions ");
  await page.waitForSelector(".plan-heading");
  assert.equal(seen["/api/investigations/get"].length, reads + 1, "Open all reads the complete bundle");
  assert.deepEqual(await active(), ["Plans"]);
  assert.deepEqual(await texts(".plan-select strong"), ["before.json", "after.json"]);
  await go("Home");
  await page.waitForSelector('[aria-label="Recent work"] [data-kind="investigation"]');
  assert.equal(await page.$('.home [aria-label="Get started"]'), null);
  const homeLayout = await page.evaluate(() => {
    const connections = document.querySelector('.home-launcher > .connections').getBoundingClientRect();
    const recent = document.querySelector('.recent-work').getBoundingClientRect();
    return { sideBySide: recent.left >= connections.right - 1,
      aligned: Math.abs(connections.top - recent.top) < 1,
      visible: recent.top < innerHeight };
  });
  assert.deepEqual(homeLayout, { sideBySide: true, aligned: true, visible: true });
  assert.equal(await page.$(".topbar .window-save"), null, "Home keeps its own Import action");
  assert.equal(await page.$('.topbar [aria-label="Import plan"].primary'), null, "Import is secondary while plans are open");
  assert.equal(await page.$$eval('button', (els) => els.filter((el) => el.textContent.trim() === 'Import plan').length), 1);
  assert.deepEqual(await texts('[aria-label="Recent work"] [data-kind="investigation"] strong'), ["Orders review", "Inventory review"]);
  assert.equal(last("/api/investigations/list").limit, 6, "Home reads only the six recent investigations");
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await overflow(), false, "Home with recent work fits at 390 px");
  await page.screenshot({ path: path.join(directory, "home-populated-mobile.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  await button("All investigations", ".home ");
  await page.waitForSelector(".library-item");
  for (const id of ["newer", "older"]) {
    await button("Delete", ".library-actions ");
    await button("Delete permanently", "dialog.confirm-sheet ");
    await page.waitForFunction((id) => !document.querySelector(`.library-item[data-row-key="${id}"]`), {}, id);
  }
  assert.match(await text(".library-detail"), /Open plans, then choose Save investigation/);
  await button("Import plan", ".library-detail ");
  await page.waitForSelector("dialog.import-dialog[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.import-dialog"));
  await button("Import plan", ".library-detail ");
  await page.waitForSelector("dialog.import-dialog[open]");
  await page.locator("dialog textarea").fill("not JSON");
  await button("Analyze plan", "dialog ");
  await page.waitForSelector('dialog [role="alert"]');
  // Puppeteer fills 100+ characters by setting the value directly, which React
  // never sees: paste it as one character run instead (as browser.mjs does).
  await page.locator("dialog textarea").fill("");
  await page.focus("dialog textarea");
  await page.keyboard.sendCharacter(rawPlan);
  await page.locator("dialog .field input").fill("new-from-investigations.json");
  await button("Analyze plan", "dialog ");
  await page.waitForFunction(() => document.querySelector(".plan-heading h1")?.textContent === "new-from-investigations.json");
  assert.deepEqual(await active(), ["Plans"]);
  assert.ok(await page.$(".topbar button ::-p-text(Save investigation)"), "the imported plan can now be saved as an investigation");

  // Investigations arrive in pages of 25, newest first, with Load more.
  investigations = Array.from({ length: 30 }, (_, i) => ({
    schema_version: "investigation-1.0", id: `bulk-${String(i).padStart(2, "0")}`,
    name: `Bulk ${String(i).padStart(2, "0")}`, notes: "", tags: [], baseline_id: "p",
    updated_at: new Date(Date.UTC(2026, 8, 1) + i * 60000).toISOString(),
    plans: [{ id: "p", name: "p.json", raw: rawPlan }] }));
  const libraryRows = () => page.$$eval(".library-item", (els) => els.map((e) => e.dataset.rowKey));
  const waitFor = async (check, message) => {
    for (let attempt = 0; attempt < 250 && !(await check()); attempt++)
      await new Promise((resolve) => setTimeout(resolve, 20));
    assert.ok(await check(), message);
  };
  await go("Investigations");
  await waitFor(async () => (await libraryRows()).length === 25, "the first page holds 25 investigations");
  assert.equal((await libraryRows())[0], "bulk-29", "newest first");
  // A page that arrives while a delete is pending keeps both: no row is lost.
  const releasePage = hold("/api/investigations/list", (body) => !!body.cursor);
  await button("Load more investigations", ".library ");
  await waitFor(() => !!last("/api/investigations/list").cursor, "Load more asks for the next page");
  const releaseDelete = hold("/api/investigations/delete");
  await button("Delete", ".library-actions ");
  await button("Delete permanently", "dialog.confirm-sheet ");
  await waitFor(() => (seen["/api/investigations/delete"] || []).some((b) => b.id === "bulk-29"), "the delete is sent");
  releasePage();
  await waitFor(async () => (await libraryRows()).length === 30, "the next page arrives during the delete");
  releaseDelete();
  await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet"));
  const remaining = await libraryRows();
  assert.equal(remaining.length, 29, "the delete removes one row and keeps the page that arrived meanwhile");
  assert.ok(!remaining.includes("bulk-29") && remaining.includes("bulk-00"));
  assert.equal(await page.$(".library button ::-p-text(Load more investigations)"), null, "no more pages");
  // A search typed before the first reply is a search with no match, not an empty library.
  await go("Home");
  const releaseFirst = hold("/api/investigations/list", (body) => !body.search);
  await go("Investigations");
  await page.locator('[aria-label="Search investigations"]').fill("zzz");
  await waitFor(async () => /No matching investigations/.test(await text(".library")), "no match while the first page is pending");
  assert.doesNotMatch(await text(".library-detail"), /No investigations yet/);
  releaseFirst();
  await button("Clear search", ".library ");
  await waitFor(async () => (await libraryRows()).length === 25, "clearing the search lists the first page");
  investigations = [];

  // 6. Home Connections: rows with settings and where the password is kept.
  await go("Home");
  await page.waitForSelector(".connection-row");
  const rows = () => texts(".connection-row [data-col=name] strong");
  assert.deepEqual(await rows(), ["Local MySQL", "Prod", "Staging"]);
  const prodRow = connectionRow("Prod");
  const stagingRow = connectionRow("Staging");
  assert.match(await text(prodRow), /analyst@db\.prod:3306shop/);
  assert.equal(await page.$eval(prodRow + " [data-col=password] span", el => el.title), "Password saved in macOS Keychain");
  // No default schema reads as a dash (named for screen readers).
  assert.match(await text(stagingRow), /dev@db\.staging:3307—No default schemaNot saved/);
  // The cell reads "Not saved"; its tooltip keeps the full status.
  assert.equal(await text(stagingRow + " [data-col=password]"), "Not saved");
  assert.equal(await page.$eval(stagingRow + " [data-col=password] span", el => el.title), "No saved password");
  // The unsaved default connection is in use until another is chosen.
  assert.match(await text(".connection-row.is-active"), /Local MySQL.*In use/);
  // Test a saved connection from its row: by id, with no password sent.
  await connectionAction(page, "Prod", "Test");
  await page.waitForFunction((s) => document.querySelector(".connection-panel .row-test")?.textContent === "Connected · 8.4.3", {}, prodRow);
  assert.equal(last("/api/connection/test").connection.profile_id, "p1");
  assert.equal(last("/api/connection/test").connection.password, "");
  // Testing opens the focused connection details. Return to the retained
  // list before choosing its row action again.
  await page.click(".connections .detail-back");
  // Use: Prod becomes the connection in use.
  await connectionAction(page, "Prod", "Use");
  await page.waitForFunction(() => /Prod/.test(document.querySelector(".connection-row.is-active")?.textContent || ""));
  assert.match(await text(".connections .notice"), /Using "Prod" in the Query editor and Server/);
  assert.deepEqual(await rows(), ["Prod", "Staging"], "the unsaved row leaves once a saved one is in use");
  // Add: a new connection, saved with its settings only.
  await button("Add connection");
  await page.waitForSelector(".connection-panel");
  assert.equal(await text("#connection-form-title"), "Add connection");
  // The form opens with focus on its first field.
  await page.waitForFunction(
    () =>
      document.activeElement?.closest(".connection-panel label")?.firstChild.textContent.trim() ===
      "Connection name",
  );
  const setField = (label, value) =>
    page.$eval(
      ".connection-panel",
      (panel, label, value) => {
        const input = [...panel.querySelectorAll("label")]
          .find((e) => e.firstChild.textContent.trim() === label)
          .querySelector("input");
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      },
      label,
      value,
    );
  await setField("Connection name", "Reporting");
  await setField("Host", "db.reports");
  await setField("User", "viewer");
  await button("Save connection");
  await page.waitForFunction(() => document.querySelector(".connections .notice")?.textContent.includes("saved without a password"));
  assert.equal(last("/api/profiles/save").id, undefined);
  assert.equal(last("/api/profiles/save").value.host, "db.reports");
  assert.deepEqual(await rows(), ["Prod", "Reporting", "Staging"]);
  assert.match(await text(".connection-row.is-active"), /Prod/, "adding does not change the connection in use");
  await page.click(".connections .detail-back");
  // Edit: change a saved connection's schema.
  await connectionAction(page, "Staging", "Edit");
  await page.waitForFunction(() => document.querySelector("#connection-form-title")?.textContent === "Edit “Staging”");
  await setField("Schema", "reports");
  await button("Save connection");
  await page.waitForFunction(() => /Connection "Staging" saved/.test(document.querySelector(".connections .notice")?.textContent || ""));
  assert.equal(last("/api/profiles/save").id, "p2");
  // Columns are compact while the form is open; the row shows the new schema once it closes.
  await page.click(".connections .inspector-close");
  assert.equal(await page.$(".connection-panel"), null);
  await page.waitForFunction((s) => /reports/.test(document.querySelector(s)?.textContent || ""), {}, stagingRow);
  // A row menu and the inspector share the same confirmation sheet.
  await connectionAction(page, "Reporting", "Delete");
  await page.waitForSelector("dialog.confirm-sheet[open]");
  await button("Cancel", "dialog.confirm-sheet ");
  assert.equal(await page.$("dialog.confirm-sheet"), null);
  assert.ok(await page.$(connectionRow("Reporting")));
  await connectionAction(page, "Reporting", "Delete");
  await button("Delete connection", "dialog.confirm-sheet ");
  await page.waitForFunction(() => !document.querySelector('button[aria-label="Actions for Reporting"]'));
  assert.equal(last("/api/profiles/delete").id, "p3");

  // 7. The picker in the Query editor and Server: name, target, state, switch.
  await go("Query editor");
  const picker = ".window-toolbar .connection-picker";
  assert.equal(await page.$(picker + " select"), null, "connection options start closed");
  await openConnectionPicker(page);
  assert.equal(await page.$eval(picker + " select", (e) => e.selectedOptions[0].textContent), "Prod");
  // Account and privileges lives in the picker, beside Manage connections.
  assert.deepEqual(await texts(picker + " .picker-actions button"), ["Account and privileges…", "Manage connections…"]);
  await button("Account and privileges…", picker + " ");
  await page.waitForSelector("dialog.account-sheet[open]");
  assert.match(await text("dialog.account-sheet .sheet-lead"), /^Prod$/);
  assert.ok(await page.$("dialog.account-sheet button ::-p-text(Refresh privileges)"), "Refresh privileges in the sheet");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.account-sheet"));
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("connection-pill")), true, "focus returns to the picker");
  await openConnectionPicker(page);
  assert.equal(await page.$eval('.window-statusbar [data-status="safety"]', (e) => [e.dataset.safety, e.querySelector('.safety-status-text').getAttribute("aria-label")].join(" ")),
    "production Production: every change on Prod asks first");
  assert.match(await text(picker), /analyst@db\.prod:3306 · shop/);
  assert.match(await text(picker + " .picker-state"), /Password saved in macOS Keychain/);
  assert.equal(await page.$(picker + " .picker-password"), null, "no password field when one is saved");
  await button("Use a different password", picker + " ");
  await page.waitForSelector(picker + " .picker-password");
  // Switching to a connection without a saved password asks for it inline.
  await page.select(picker + " select", "p2");
  await page.waitForFunction((s) => /dev@db\.staging:3307/.test(document.querySelector(s).textContent), {}, picker);
  assert.match(await text(picker + " .picker-state"), /No password entered/);
  // Staging is Development: the footer says so (risky changes ask), a green dot in the picker.
  await page.waitForFunction(() => document.querySelector('.window-statusbar [data-status="safety"]')?.dataset.safety === "development");
  assert.match(await text('.window-statusbar [data-status="safety"]'), /^Development\s*· risky changes ask first$/);
  assert.equal(await page.$eval(picker + " .type-dot", (e) => [e.className, e.getAttribute("aria-label")].join(" ")), "type-dot is-development Development");
  await page.locator(picker + " .picker-password").fill("typed-secret");
  await go("Workload");
  const serverPicker = ".window-toolbar .connection-picker";
  await openConnectionPicker(page);
  assert.equal(await page.$eval(serverPicker + " select", (e) => e.value), "p2");
  assert.equal(await page.$eval(serverPicker + " .picker-password", (e) => e.value), "typed-secret");
  await chooseView(page, "top");
  await button("Load top statements");
  await page.waitForFunction(() => !document.querySelector(".server-view .spinner"));
  assert.equal(last("/api/live/top").connection.profile_id, "p2");
  assert.equal(last("/api/live/top").connection.password, "typed-secret");
  // Requests carry the type they are judged by (part of the connection key).
  assert.equal(last("/api/live/top").connection.type, "development");
  assert.equal(last("/api/live/top").connection.read_only, false);
  assert.equal(await page.$eval('.window-statusbar [data-status="safety"]', (e) => e.dataset.safety), "development", "Development on Workload too");
  assert.equal(last("/api/live/top").connection.database, "reports");
  // The connection in use is part of the restorable session; its password is not.
  // A connection switch is navigation: it is saved silently after the
  // debounce, so wait for the stored session itself rather than the status.
  const loadStored = () => page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    return r.json();
  });
  let stored = await loadStored();
  for (let i = 0; i < 100 && stored.state?.query_lab?.profile_id !== "p2"; i++) {
    await new Promise((r) => setTimeout(r, 100));
    stored = await loadStored();
  }
  assert.equal(stored.state.query_lab.profile_id, "p2");
  assert.equal(stored.state.mode, "workload");
  assert.equal(JSON.stringify(stored).includes("typed-secret"), false);
  // Manage connections goes to Home, where Staging is in use.
  await openConnectionPicker(page);
  await button("Manage connections…", serverPicker + " ");
  await page.waitForSelector(".connections");
  assert.deepEqual(await active(), ["Home"]);
  assert.match(await text(".connection-row.is-active"), /Staging/);
  // Forget a saved password from its row.
  await page.waitForFunction(() => document.activeElement?.id === "connections-table");
  await connectionAction(page, "Prod", "Forget password");
  await page.waitForFunction(() => document.querySelector(".connections .notice")?.textContent.includes("Saved password forgotten"));
  assert.equal(last("/api/profiles/forget-password").id, "p1");
  // Forget password opens the form (compact columns); the row shows Not saved once it closes.
  await page.click(".connections .inspector-close");
  await page.waitForSelector(prodRow + " [data-col=password] span");
  assert.equal(await text(prodRow + " [data-col=password]"), "Not saved");
  assert.equal(await page.$eval(prodRow + " [data-col=password] span", el => el.title), "No saved password");

  // 8. Keyboard: arrows move between places, Enter opens one.
  await page.focus(".main-nav button.active");
  await page.keyboard.press("ArrowDown");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "Server");
  await page.keyboard.press("End");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "Learn");
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("ArrowUp");
  assert.equal(await page.evaluate(() => document.activeElement.textContent), "Plans");
  await page.keyboard.press("Home");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".main-nav button.active")?.textContent === "Query editor");
  assert.ok(await visible(".query-lab"));
  // The skip link reaches the content.
  assert.equal(await page.$eval(".skip", (e) => e.getAttribute("href")), "#main");

  // 9. Narrow layout: a compact bar with status and a drawer behind Menu.
  await page.setViewport({ width: 390, height: 844 });
  // Resizing resolves before the media-query listener's React update. Wait
  // for that state transition, then verify that the drawer really is hidden.
  await page.waitForFunction(() => document.querySelector("#window-sidebar-toggle")?.getAttribute("aria-expanded") === "false");
  const overflown = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('*'))
      .filter(el => el.scrollWidth > window.innerWidth)
      .map(el => ({
        tag: el.tagName,
        className: el.className,
        id: el.id,
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
        innerWidth: window.innerWidth,
      }));
  });
  console.log("OVERFLOWN ELEMENTS:", JSON.stringify(overflown, null, 2));
  assert.equal(await overflow(), false);
  const menu = "#window-sidebar-toggle";
  assert.ok(await visible(menu));
  assert.equal(await visible(".main-nav"), false, "places wait in the drawer");
  assert.equal(await visible(".session-status"), false, "save status stays in the sidebar");
  assert.equal(await page.$eval(menu, (e) => e.getAttribute("aria-expanded")), "false");
  await page.click(menu);
  await page.waitForFunction(() => !!document.querySelector(".sidebar-drawer.open"));
  assert.equal(await page.$eval(menu, (e) => e.getAttribute("aria-expanded")), "true");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Home", "focus moves into the drawer");
  assert.ok(await visible(".open-plans"));
  assert.equal(await overflow(), false);
  await page.screenshot({ path: path.join(directory, "drawer-mobile.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".sidebar-drawer.open"));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Show sidebar");
  await page.click(menu);
  await page.waitForSelector(".sidebar-drawer.open");
  await go("Server");
  await page.waitForFunction(() => !document.querySelector(".sidebar-drawer.open"));
  assert.ok(await visible(".server-place"));
  assert.equal(await overflow(), false);
  // Settings stays inside the drawer and viewport.
  await page.click(menu);
  await page.waitForSelector(".sidebar-drawer.open");
  await page.click(settings);
  await page.waitForSelector("#settings-popover");
  const box = await page.$eval("#settings-popover", (e) => {
    const r = e.getBoundingClientRect();
    return { left: r.left, right: r.right };
  });
  assert.ok(box.left >= 0 && box.right <= 390, JSON.stringify(box));
  // About myflames fits too.
  await page.locator("#settings-popover button ::-p-text(About myflames)").click();
  await page.waitForSelector("dialog.about-dialog[open]");
  const aboutBox = await page.$eval("dialog.about-dialog", (e) => {
    const r = e.getBoundingClientRect();
    return { left: r.left, right: r.right, scroll: e.scrollWidth <= e.clientWidth };
  });
  assert.ok(aboutBox.left >= 0 && aboutBox.right <= 390 && aboutBox.scroll, JSON.stringify(aboutBox));
  await page.screenshot({ path: path.join(directory, "about-mobile.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.about-dialog"));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Settings");
  await page.click(menu);
  for (const label of places) {
    await page.click(menu);
    await page.waitForSelector(".sidebar-drawer.open");
    await go(label);
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(await overflow(), false, `${label} fits at 390 px`);
    if (label === "Query editor") {
      assert.equal(await page.$(".editor-statusbar"), null, "the editor has no status bar of its own");
      assert.deepEqual(await page.$$eval(".window-statusbar", els => els
        .filter(e => e.getClientRects().length > 0).map(e => e.getAttribute("aria-label"))), ["Workspace status"],
        "the phone editor has one visible status bar");
      assert.equal(await page.$(".editor-toolbar .privilege-unverified"), null, "run buttons have no privilege chips");
      assert.ok(await page.$$eval(".editor-privilege-note", els => els.length <= 1));
    }
    await page.screenshot({ path: path.join(directory, `${label.replace(/ /g, "-").toLowerCase()}-mobile.png`) });
  }

  assert.deepEqual(errors, []);
  console.log("Navigation checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
