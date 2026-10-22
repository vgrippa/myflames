// Scenarios for the action feedback audit: how to reach each action, the
// control to click, and the endpoints whose answer is delayed, failed or held.
import { chooseView } from "./chrome-helpers.mjs";
import { clickText, pause, visibleHandle, waitVisible } from "./feedback-audit-lib.mjs";

export async function goPlace(page, place) {
  await page.click(`.main-nav button[data-place="${place}"]`);
  await pause(500);
}
async function goView(page, place, kind) {
  await goPlace(page, place);
  await chooseView(page, kind);
  await pause(800);
}
const byText = (text, scope = "") => async (page) => {
  const handles = await page.$$(`${scope} :is(button,[role=menuitem],[role=tab],a,summary)`);
  for (const h of handles) {
    const ok = await h.evaluate((e, t) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0 &&
      (e.textContent.replace(/\s+/g, " ").trim() === t || e.getAttribute("aria-label") === t), text);
    if (ok) return h;
  }
  throw Error(`no visible control named ${text}`);
};
const REFRESH = "[data-action=refresh]";
const LIVE_VIEWS = ["/api/live/health", "/api/live/innodb", "/api/live/error-log", "/api/live/locks", "/api/live/indexes",
  "/api/live/tables", "/api/live/config", "/api/live/instruments", "/api/live/sessions", "/api/live/waits", "/api/live/io",
  "/api/live/top", "/api/live/replication", "/api/live/cluster", "/api/live/proxysql", "/api/accounts/list",
  "/api/lifecycle/tools", "/api/schema-diagram/read", "/api/live/report"];

const view = (name, place, kind, path, owner, extra = {}) => ({
  name, label: extra.label || "Refresh", owner, place, paths: [path],
  setup: async ({ page }) => { await goView(page, place, kind); if (extra.prepare) await extra.prepare(page); },
  target: extra.target || REFRESH, ...extra,
});

export const SCENARIOS = [
  // ---- Server and Workload view reads ------------------------------------------------
  view("health-refresh", "server", "health", "/api/live/health", "Health.tsx"),
  view("innodb-refresh", "server", "innodb", "/api/live/innodb", "InnoDB.tsx"),
  view("errorlog-load", "server", "errorlog", "/api/live/error-log", "ErrorLog.tsx", { label: "Load error log", target: byText("Load error log") }),
  view("locks-refresh", "server", "locks", "/api/live/locks", "Locks.tsx"),
  view("indexes-refresh", "server", "indexes", "/api/live/indexes", "Indexes.tsx"),
  view("tables-refresh", "server", "tables", "/api/live/tables", "Tables.tsx"),
  view("config-refresh", "server", "config", "/api/live/config", "Config.tsx"),
  view("instruments-refresh", "server", "instruments", "/api/live/instruments", "Instruments.tsx"),
  view("maintenance-detect-tools", "server", "maintenance", "/api/lifecycle/tools", "Maintenance.tsx", { label: "Detect tools", target: byText("Detect tools") }),
  view("accounts-load", "server", "accounts", "/api/accounts/list", "Accounts.tsx", { label: "Load users and roles", target: byText("Load users and roles") }),
  view("top-load", "workload", "top", "/api/live/top", "SlowQueries.tsx", { label: "Load top statements", target: byText("Load top statements") }),
  view("sessions-refresh", "workload", "sessions", "/api/live/sessions", "Sessions.tsx"),
  view("waits-refresh", "workload", "waits", "/api/live/waits", "Waits.tsx"),
  view("io-refresh", "workload", "io", "/api/live/io", "IoStats.tsx"),
  {
    name: "replication-refresh", label: "Refresh", owner: "Replication.tsx / ReplicationPlace.tsx", place: "replication",
    paths: ["/api/live/replication"], setup: async ({ page }) => { await goPlace(page, "replication"); await pause(1000); }, target: REFRESH,
  },
  {
    name: "cluster-refresh", label: "Refresh", owner: "ClusterPlace.tsx", place: "cluster",
    paths: ["/api/live/cluster"], setup: async ({ page }) => { await goPlace(page, "cluster"); await pause(1000); }, target: REFRESH,
  },
  {
    name: "proxysql-refresh", label: "Refresh", owner: "ProxySqlPlace.tsx", place: "proxysql",
    paths: ["/api/live/proxysql"], setup: async ({ page }) => { await goPlace(page, "proxysql"); await pause(1000); }, target: REFRESH,
  },
  {
    name: "server-refresh-all", label: "Refresh all", owner: "Server.tsx", place: "server", paths: LIVE_VIEWS,
    setup: async ({ page }) => { await goView(page, "server", "health"); }, target: "[data-action=refresh-all]",
  },
  {
    name: "server-refresh-all-single", label: "Refresh all (one click)", owner: "Server.tsx", place: "server", paths: LIVE_VIEWS, noSecondClick: true,
    setup: async ({ page }) => { await goView(page, "server", "health"); }, target: "[data-action=refresh-all]",
  },
  {
    name: "tuning-run-full-analysis", label: "Run full analysis", owner: "Tuning.tsx / Report.tsx (Server.tsx owns the read)", place: "tuning",
    paths: ["/api/live/report"], setup: async ({ page }) => { await goPlace(page, "tuning"); await pause(800); },
    target: "[data-action=run-analysis]",
  },
  // ---- Home ------------------------------------------------------------------------
  {
    name: "home-refresh-news", label: "Refresh news", owner: "HomeNews.tsx", place: "home", away: "plans", connection: false,
    paths: ["/api/news"], setup: async ({ page }) => { await goPlace(page, "home"); await pause(800); }, target: 'button[aria-label="Refresh news"]',
  },
  {
    name: "home-check-updates", label: "Check for updates", owner: "UpdateCheck.tsx", place: "home", away: "plans", connection: false,
    paths: ["/api/app/update/check"], setup: async ({ page }) => { await goPlace(page, "home"); },
    target: async (page) => waitVisible(page, ".home-version .update-check-button"),
  },
  {
    name: "home-test-connection", label: "Test connection (form)", owner: "Connections.tsx", place: "home", away: "plans", connection: false,
    paths: ["/api/connection/test"],
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await (await byText("Add connection")(page)).click();
      await pause(400);
    },
    target: byText("Test connection"),
  },
  {
    name: "home-save-connection", label: "Save connection", owner: "Connections.tsx", place: "home", away: "plans", connection: false,
    paths: ["/api/profiles/save"],
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await (await byText("Add connection")(page)).click();
      await pause(400);
      const name = await waitVisible(page, '.connection-panel .connection-fields input');
      await name.click({ clickCount: 3 });
      await name.type("Audit server");
    },
    target: byText("Save connection"),
  },
  // ---- Plans and investigations ----------------------------------------------------------
  {
    name: "plans-sample", label: "Explore sample plan", owner: "main.tsx", place: "plans", away: "home", connection: false,
    paths: ["/api/analyze"], setup: async ({ page }) => { await goPlace(page, "plans"); }, target: byText("Explore sample plan"),
  },
  {
    name: "investigation-open-all", label: "Open all", owner: "Library.tsx", place: "investigations", connection: false,
    paths: ["/api/investigations/get", "/api/analyze"], setup: async ({ page }) => { await goPlace(page, "investigations"); await pause(600); },
    target: byText("Open all"),
  },
  {
    name: "investigation-compare", label: "Compare to baseline", owner: "Library.tsx", place: "investigations", connection: false,
    paths: ["/api/investigations/get", "/api/compare"], setup: async ({ page }) => { await goPlace(page, "investigations"); await pause(600); },
    target: byText("Compare to baseline"),
  },
  {
    name: "investigation-export", label: "Export (investigation)", owner: "Library.tsx", place: "investigations", connection: false,
    paths: ["/api/investigations/get"], setup: async ({ page }) => { await goPlace(page, "investigations"); await pause(600); },
    target: byText("Export", ".library"),
  },
  {
    name: "investigation-delete", label: "Delete (investigation, confirmed)", owner: "Library.tsx", place: "investigations", connection: false,
    paths: ["/api/investigations/delete"],
    setup: async ({ page }) => {
      await goPlace(page, "investigations"); await pause(600);
      await (await byText("Delete")(page)).click(); await pause(400);
    },
    target: async (page) => {
      const buttons = await page.$$("dialog[open] .dialog-actions button");
      return buttons[buttons.length - 1];
    },
  },
  {
    name: "learn-open-lesson", label: "Open a lesson", owner: "Teach.tsx", place: "learn", connection: false,
    paths: ["/api/teach/lesson"], setup: async ({ page }) => { await goPlace(page, "learn"); await pause(600); },
    target: async (page) => {
      const h = await page.$$(".teach-workspace button");
      for (const b of h) if (await b.evaluate((e) => /Full table scan/.test(e.textContent))) return b;
      throw Error("no lesson");
    },
  },
  // ---- Query editor --------------------------------------------------------------------
  {
    name: "editor-run", label: "Run", owner: "QueryLab.tsx", place: "query",
    paths: ["/api/editor/run"], setup: async ({ page }) => { await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(600); },
    target: "[data-action=run]",
  },
  {
    name: "editor-run-all", label: "Run all", owner: "QueryLab.tsx", place: "query",
    paths: ["/api/editor/run"], setup: async ({ page }) => { await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1;\nSELECT 2"); await pause(600); },
    target: "[data-action=run-all]",
  },
  {
    name: "editor-explain", label: "Explain", owner: "QueryLab.tsx", place: "query",
    paths: ["/api/live/start"], setup: async ({ page }) => { await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT * FROM orders"); await pause(600); },
    target: "[data-action=explain]",
  },
  {
    name: "editor-load-schemas", label: "Load schemas", owner: "SchemaBrowser.tsx", place: "query",
    paths: ["/api/live/schemas"], setup: async ({ page }) => { await goPlace(page, "query"); await pause(400); },
    target: byText("Load schemas"),
  },
  // ---- Database diagram ----------------------------------------------------------------
  {
    name: "diagram-reload-databases", label: "Reload database list", owner: "SchemaDiagram.tsx", place: "server",
    paths: ["/api/live/schemas"], setup: async ({ page }) => { await goView(page, "server", "diagram"); },
    target: byText("Reload database list"),
  },
];
