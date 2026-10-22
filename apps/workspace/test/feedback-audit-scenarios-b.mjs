// Feedback audit, part B: writes behind confirmation sheets, multi-step
// flows, exports and the Query editor's capture paths.
import { chooseView } from "./chrome-helpers.mjs";
import { FIX, load, pause, waitVisible } from "./feedback-audit-lib.mjs";
import { goPlace } from "./feedback-audit-scenarios.mjs";

const sessions = await load("sessions-mysql-8.4.json");
sessions.server_identity = { server_uuid: "audit-server-uuid" };
const replication = await load("replication-mysql-8.4-gtid-error.json");
for (const c of replication.channels) if (c.skip_plan) Object.assign(c.skip_plan, { review_token: "review-skip" });
const ACTIVE = [/* nothing */];

async function goView(page, place, kind) {
  await goPlace(page, place);
  await chooseView(page, kind);
  await pause(800);
}
const named = (text, scope = "") => async (page) => {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    for (const h of await page.$$(`${scope} :is(button,[role=menuitem],[role=tab],a,summary,[role=option])`)) {
      const ok = await h.evaluate((e, t) => e.isConnected && !e.closest("[hidden]") && e.getClientRects().length > 0 &&
        (e.textContent.replace(/\s+/g, " ").trim() === t || e.getAttribute("aria-label") === t), text);
      if (ok) return h;
    }
    await pause(100);
  }
  throw Error(`no visible control named ${text}`);
};
const click = async (page, text, scope = "") => (await named(text, scope)(page)).click();
/** The confirm button of the open sheet, once it is enabled. */
const confirmButton = (scope = "dialog[open]") => async (page) => {
  const end = Date.now() + 8000;
  while (Date.now() < end) {
    const buttons = await page.$$(`${scope} .dialog-actions button`);
    const last = buttons[buttons.length - 1];
    if (last && !(await last.evaluate((e) => e.disabled))) return last;
    await pause(100);
  }
  throw Error(`confirm never enabled in ${scope}`);
};
const KILL_OK = { schema_version: "kill-1.0", status: "killed", connection_id: 94, mode: "query", statement: "KILL QUERY 94",
  verified: ["user", "host", "not this session"], after: { present: true },
  message: "Stopped the running statement on connection 94. The connection stays open." };
const PLAN_RAW = JSON.stringify({ query: "SELECT SUM(v) FROM t WHERE id BETWEEN 10 AND 20", operation: "Aggregate: sum(t.v)",
  access_type: "aggregate", estimated_rows: 1, estimated_total_cost: 2.1,
  inputs: [{ operation: "Index range scan on t using PRIMARY", access_type: "index", table_name: "t", index_name: "PRIMARY", estimated_rows: 11, estimated_total_cost: 2.1 }] });
const CONNECTION_PLAN = { schema_version: "explain-connection-1.0", raw: PLAN_RAW, mode: "explain", engine: "mysql", server_version: "8.4.11",
  connection_id: 94, query: "SELECT SUM(v) FROM t", warnings: [], captured_at: "2026-10-14T18:21:45+00:00",
  capture: { schema_version: "capture-1.0", raw: PLAN_RAW, query: "SELECT SUM(v) FROM t", mode: "explain", engine: "mysql",
    server_version: "8.4.11", captured_at: "2026-10-14T18:21:45+00:00", conditions: "In-flight statement on connection 94.",
    warnings: [], runs: [], schema: {}, table_stats: {}, variables: {}, trace: null } };
const sessionsMock = { "/api/live/sessions": () => sessions };
async function selectSession(page, id) {
  await goView(page, "workload", "sessions");
  await page.locator(`tr[data-session="${id}"] td:nth-child(3)`).click();
  await pause(500);
}

export const SCENARIOS_B = [
  // ---- Processlist ---------------------------------------------------------------
  {
    name: "sessions-show-plan", label: "Show plan", owner: "Sessions.tsx (Server.tsx explainConnection)", place: "workload",
    paths: ["/api/live/explain-connection"], mocks: { ...sessionsMock, "/api/live/explain-connection": () => CONNECTION_PLAN },
    setup: async ({ page }) => selectSession(page, 94), target: 'button[aria-label="Show plan for connection 94"]',
  },
  {
    name: "sessions-kill-open", label: "Kill (opens the sheet: review)", owner: "Sessions.tsx / Locks.tsx KillDialog", place: "workload", sheet: true,
    paths: ["/api/live/kill/prepare"], mocks: sessionsMock,
    setup: async ({ page }) => selectSession(page, 94), target: 'button[aria-label="Kill: connection 94"]',
  },
  {
    name: "sessions-kill-confirm", label: "Kill query on connection N (confirm)", owner: "Sessions.tsx / Locks.tsx KillDialog, sendKill", place: "workload", sheet: true,
    paths: ["/api/live/kill"], mocks: { ...sessionsMock, "/api/live/kill": () => KILL_OK },
    setup: async ({ page }) => {
      await selectSession(page, 94);
      await page.click('button[aria-label="Kill: connection 94"]');
      await page.waitForSelector("dialog.kill-dialog[open]");
    },
    target: confirmButton("dialog.kill-dialog[open]"),
  },
  {
    name: "sessions-kill-sessions-review", label: "Kill N connections (Kill sessions… review)", owner: "MassKillDialog.tsx", place: "workload", sheet: true,
    paths: ["/api/live/kill/bulk/prepare"], mocks: sessionsMock,
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await click(page, "Runaway queries (> 30s)");
      await pause(300);
    },
    target: async (page) => {
      const h = await page.$$(".mass-kill-footer-actions button.danger");
      if (!h.length || (await h[0].evaluate((e) => e.disabled))) throw Error("Kill N disabled");
      return h[0];
    },
  },
  {
    name: "sessions-kill-sessions-send", label: "Send N statements (Kill sessions… confirm)", owner: "MassKillDialog.tsx", place: "workload", sheet: true,
    paths: ["/api/live/kill/bulk"], mocks: { ...sessionsMock, "/api/live/kill/bulk": (b) => ({ schema_version: "kill-bulk-1.0", total: b.targets.length,
      killed: b.targets.length, refused: 0, failed: 0, results: [], message: `Ended ${b.targets.length} connections.` }) },
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await page.click("[data-action=kill-sessions]");
      await page.waitForSelector("dialog.mass-kill-dialog[open]");
      await click(page, "Runaway queries (> 30s)");
      await pause(300);
      await page.click(".mass-kill-footer-actions button.danger");
      await page.waitForSelector("dialog.mass-kill-confirm[open]");
    },
    target: confirmButton("dialog.mass-kill-confirm[open]"),
  },
  // ---- Parameters ----------------------------------------------------------------
  {
    name: "config-change-open", label: "Change… (opens the sheet: reads the variable)", owner: "Config.tsx", place: "server", sheet: true, noSecondClick: true,
    paths: ["/api/live/variable"], mocks: { "/api/live/variable": () => FIX.variable.variable, "/api/live/variable/plan": () => FIX.variable.plan },
    setup: async ({ page }) => {
      await goView(page, "server", "config");
      await page.locator('.server-view[data-view=config] tr[data-row-key="max_connections"] td:nth-child(1)').click();
      await pause(500);
    },
    target: '.view-inspector [data-change="max_connections"]',
  },
  {
    name: "config-change-confirm", label: "Set globally / Persist (Change… confirm)", owner: "Config.tsx", place: "server", sheet: true,
    paths: ["/api/live/variable/apply"],
    mocks: { "/api/live/variable": () => FIX.variable.variable, "/api/live/variable/plan": () => FIX.variable.plan,
      "/api/live/variable/apply": () => FIX.variable.change },
    setup: async ({ page }) => {
      await goView(page, "server", "config");
      await page.locator('.server-view[data-view=config] tr[data-row-key="max_connections"] td:nth-child(1)').click();
      await pause(500);
      await page.click('.view-inspector [data-change="max_connections"]');
      await page.waitForSelector("dialog.variable-sheet[open]");
      await pause(600);
      const field = await waitVisible(page, "#variable-value-max_connections");
      await field.click({ clickCount: 3 }); await field.type("300");
      await pause(1200);
    },
    target: confirmButton("dialog.variable-sheet[open]"),
  },
  // ---- Replication ---------------------------------------------------------------
  {
    name: "replication-skip-open", label: "Skip this transaction… (re-reads first)", owner: "Replication.tsx", place: "replication",
    paths: ["/api/live/replication"], mocks: { "/api/live/replication": () => replication },
    setup: async ({ page }) => {
      await goPlace(page, "replication"); await pause(1200);
      await click(page, "Channels"); await pause(400);
      await page.locator('.server-view[data-view=replication] tbody tr td:nth-child(1)').click();
      await pause(500);
    },
    target: named("Skip this transaction…"),
  },
  {
    name: "replication-skip-confirm", label: "Skip transaction (confirm)", owner: "Replication.tsx", place: "replication", sheet: true,
    paths: ["/api/live/replication/skip"], mocks: { "/api/live/replication": () => replication, "/api/live/replication/skip": () => FIX.replicationSkip },
    setup: async ({ page }) => {
      await goPlace(page, "replication"); await pause(1200);
      await click(page, "Channels"); await pause(400);
      await page.locator('.server-view[data-view=replication] tbody tr td:nth-child(1)').click();
      await pause(500);
      await click(page, "Skip this transaction…");
      await page.waitForSelector("dialog.ops-confirm[open]");
    },
    target: confirmButton("dialog.ops-confirm[open]"),
  },
  // ---- Maintenance ---------------------------------------------------------------
  {
    name: "maintenance-check-upgrade", label: "Check upgrade", owner: "Maintenance.tsx", place: "server",
    paths: ["/api/lifecycle/upgrade-check"], mocks: { "/api/lifecycle/upgrade-check": () => FIX.upgradeNative },
    setup: async ({ page }) => {
      await goView(page, "server", "maintenance");
      await click(page, "Detect tools"); await pause(800);
      await click(page, "Upgrade check"); await pause(600);
      const value = await page.$$eval('select[aria-label="Target version"] option', (os) => os.map((o) => o.value).find((v) => /^\d/.test(v)));
      await page.select('select[aria-label="Target version"]', value);
      await pause(300);
    },
    target: named("Check upgrade", ".ops-actions"),
  },
  {
    name: "maintenance-dump-preflight", label: "Run preflight… (Dump)", owner: "Maintenance.tsx", place: "server",
    paths: ["/api/lifecycle/prepare"], mocks: { "/api/lifecycle/prepare": () => FIX.dumpPlan },
    setup: async ({ page }) => {
      await goView(page, "server", "maintenance");
      await click(page, "Detect tools"); await pause(800);
      await click(page, "Dump"); await pause(600);
      const input = await waitVisible(page, ".server-view[data-view=maintenance] .ops-folder-input input");
      await input.click({ clickCount: 3 }); await input.type("/tmp/myflames-audit-dump");
      await pause(300);
    },
    target: named("Run preflight…"),
  },
  // ---- Cluster and ProxySQL (ActionReview) ---------------------------------------
  {
    name: "cluster-make-primary-review", label: "Make primary… (plans from a fresh read)", owner: "ClusterPlace.tsx / ActionReview.tsx", place: "cluster",
    paths: ["/api/live/cluster/plan"], mocks: { "/api/live/cluster/plan": () => FIX.clusterPlan },
    setup: async ({ page }) => {
      await goPlace(page, "cluster"); await pause(1200);
      await click(page, "Members"); await pause(400);
      await page.locator('.server-view tr[data-row-key="member:10db71e2-cc92-11f1-aaa1-eec8cb922a3b"] td:nth-child(1)').click();
      await pause(500);
    },
    target: named("Make primary…"),
  },
  {
    name: "cluster-make-primary-confirm", label: "Make <member> primary (confirm)", owner: "ClusterPlace.tsx / ActionReview.tsx", place: "cluster", sheet: true,
    paths: ["/api/live/cluster/action"], mocks: { "/api/live/cluster/plan": () => ({ ...FIX.clusterPlan, typed_guard: null, review_token: "t" }),
      "/api/live/cluster/action": () => ({ schema_version: "cluster-action-1.0", action: "switchover", status: "done", message: "mf-ic-3:3306 is now the primary.",
        statements: FIX.clusterPlan.statements, sent: FIX.clusterPlan.statements, not_sent: [], error: null, after: null, undo: null, next: null, audit_id: "a1" }) },
    setup: async ({ page }) => {
      await goPlace(page, "cluster"); await pause(1200);
      await click(page, "Members"); await pause(400);
      await page.locator('.server-view tr[data-row-key="member:10db71e2-cc92-11f1-aaa1-eec8cb922a3b"] td:nth-child(1)').click();
      await pause(500);
      await click(page, "Make primary…");
      await page.waitForSelector("dialog.action-review[open]");
    },
    target: confirmButton("dialog.action-review[open]"),
  },
  // ---- Top statements ------------------------------------------------------------
  {
    name: "top-explain-top-n", label: "Explain top N", owner: "SlowQueries.tsx", place: "workload",
    paths: ["/api/live/start"],
    mocks: { "/api/live/status": (b) => ({ id: b.id, state: "done", progress: 1, repeat: 1, error: "", result: { raw: PLAN_RAW, query: "SELECT 1", mode: "explain", engine: "mysql", server_version: "8.4.11", captured_at: "x", runs: [], measurements: { count: 0 }, schema: {}, table_stats: {}, variables: {}, trace: null, warnings: [], conditions: "" } }) },
    setup: async ({ page }) => {
      await goView(page, "workload", "top");
      await click(page, "Load top statements"); await pause(1200);
    },
    target: async (page) => {
      for (const h of await page.$$(".server-view[data-view=top] button")) {
        if (await h.evaluate((e) => /^Explain top/.test(e.textContent.trim()) && e.getClientRects().length > 0)) return h;
      }
      throw Error("no Explain top N");
    },
  },
  // ---- Query editor --------------------------------------------------------------
  {
    name: "editor-run-analysis", label: "Run analysis", owner: "QueryLab.tsx", place: "query",
    paths: ["/api/live/start"], setup: async ({ page }) => { await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT * FROM orders"); await pause(600); },
    target: "[data-action=analyze]",
  },
  {
    name: "editor-stop", label: "Stop (a running statement)", owner: "QueryLab.tsx", place: "query",
    paths: ["/api/editor/stop"],
    setup: async (ctx) => {
      const { page } = ctx;
      ctx.modes["/api/editor/run"] = "hang";
      await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT SLEEP(100)"); await pause(600);
      await page.click("[data-action=run]");
      await pause(800);
    },
    target: "[data-action=stop]",
  },
  {
    name: "editor-export-csv", label: "Export result (CSV)", owner: "ResultsPane.tsx", place: "query",
    paths: ["/api/editor/export"],
    setup: async ({ page }) => {
      await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(500);
      await page.click("[data-action=run]"); await pause(1200);
      await (await named("Export")(page)).click(); await pause(300);
    },
    target: named("CSV"),
  },
  {
    name: "editor-history", label: "Show the run history", owner: "HistoryPanel.tsx", place: "query", noSecondClick: true,
    paths: ["/api/editor/history"], setup: async ({ page }) => { await goPlace(page, "query"); await pause(500); },
    target: 'button[aria-label="Show the run history"]',
  },
  // ---- Plans ---------------------------------------------------------------------
  {
    name: "plans-import-paste", label: "Import plan (paste, Import)", owner: "components.tsx ImportDialog / main.tsx", place: "plans", connection: false,
    paths: ["/api/analyze"],
    setup: async ({ page }) => {
      await goPlace(page, "plans");
      await page.click(".plans-import"); await pause(400);
      const area = await waitVisible(page, "dialog[open] textarea, [role=dialog] textarea");
      await area.evaluate((e, raw) => { const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set; set.call(e, raw); e.dispatchEvent(new Event("input", { bubbles: true })); }, PLAN_RAW);
      await pause(300);
    },
    target: async (page) => {
      for (const h of await page.$$("dialog[open] button, [role=dialog] button")) {
        if (await h.evaluate((e) => /^(Import|Open|Analyze|Use)/.test(e.textContent.trim()) && !e.disabled && e.getClientRects().length > 0)) return h;
      }
      throw Error("no import button");
    },
  },
  {
    name: "plans-export-html", label: "Export HTML (plan)", owner: "main.tsx", place: "plans", connection: false,
    paths: ["/api/report"],
    setup: async ({ page }) => {
      await goPlace(page, "plans"); await click(page, "Explore sample plan"); await pause(2500);
      await (await named("Export plan")(page)).click();
      await pause(400);
    },
    target: named("HTML report"),
  },
  {
    name: "plans-save-investigation", label: "Save investigation (Save)", owner: "main.tsx", place: "plans", connection: false,
    paths: ["/api/investigations/save"],
    setup: async ({ page }) => {
      await goPlace(page, "plans"); await click(page, "Explore sample plan"); await pause(2500);
      await page.click(".window-save"); await pause(500);
    },
    target: async (page) => {
      for (const h of await page.$$("button.button.primary")) {
        if (await h.evaluate((e) => e.textContent.trim() === "Save" && e.getClientRects().length > 0)) return h;
      }
      throw Error("no Save");
    },
  },
  // ---- Tuning, audit log, about --------------------------------------------------
  {
    name: "audit-open", label: "Audit log (Settings menu)", owner: "Audit.tsx / Sidebar.tsx", place: "home", connection: false,
    paths: ["/api/audit/list"],
    setup: async ({ page }) => { await goPlace(page, "home"); await click(page, "Settings"); await pause(400); },
    target: named("Audit log"),
  },
  {
    name: "about-check-updates", label: "Check for updates (About myflames)", owner: "About.tsx / UpdateCheck.tsx", place: "home", connection: false, sheet: true,
    paths: ["/api/app/update/check"],
    setup: async ({ page }) => { await goPlace(page, "home"); await click(page, "Settings"); await pause(300); await click(page, "About myflames"); await pause(500); },
    target: async (page) => waitVisible(page, "dialog[open] .update-check-button, [role=dialog] .update-check-button"),
  },
  // ---- Home connections ------------------------------------------------------------
  {
    name: "home-delete-connection", label: "Delete connection (confirmed)", owner: "Connections.tsx", place: "home", connection: false, sheet: true,
    paths: ["/api/profiles/delete"],
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await page.click('button[aria-label="Actions for Billing replica"]'); await pause(300);
      await page.click('[role="menu"] button[aria-label="Delete Billing replica…"]');
      await page.waitForSelector("dialog[open]");
    },
    target: confirmButton("dialog[open]"),
  },
  {
    name: "home-test-row", label: "Test (connection row menu)", owner: "Connections.tsx", place: "home", connection: false,
    paths: ["/api/connection/test"],
    setup: async ({ page }) => { await goPlace(page, "home"); await page.click('button[aria-label="Actions for Lab primary"]'); await pause(300); },
    target: '[role="menu"] button[aria-label="Test Lab primary"]',
  },
  {
    name: "home-use-connection", label: "Use (connection row menu)", owner: "Connections.tsx", place: "home", connection: false,
    paths: ["/api/live/privileges"],
    setup: async ({ page }) => { await goPlace(page, "home"); await page.click('button[aria-label="Actions for Lab primary"]'); await pause(300); },
    target: '[role="menu"] button[aria-label="Use Lab primary"]',
  },
  // ---- Diagram ---------------------------------------------------------------------
  {
    name: "diagram-rebuild", label: "Rebuild from database", owner: "SchemaDiagram.tsx", place: "server",
    paths: ["/api/schema-diagram/read"],
    setup: async ({ page }) => {
      await goPlace(page, "server"); await chooseView(page, "diagram"); await pause(1000);
      const input = await waitVisible(page, '.server-view[data-view=diagram] input[aria-label="Database"]');
      await input.click(); await input.type("mftab");
      await pause(500);
    },
    target: named("Rebuild from database"),
  },
];
void ACTIVE;
