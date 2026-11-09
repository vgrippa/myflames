// Feedback audit, part C: the reviewed SQL runner (Optimize), local exports,
// Library delete with its sheet, Plans Compare, and the Processlist scope.
import { chooseView } from "./chrome-helpers.mjs";
import { editorRun, editorSession, pause, waitVisible } from "./feedback-audit-lib.mjs";
import { goPlace } from "./feedback-audit-scenarios.mjs";
import { load } from "./feedback-audit-lib.mjs";

const SESSIONS = await load("sessions-mysql-8.4.json");
SESSIONS.server_identity = { server_uuid: "audit-server-uuid" };

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
const confirmation = (sql) => ({ token: "c1", expires_in: 300, title: "Run?", text: "OPTIMIZE rebuilds the table and can block writes.", tone: "danger",
  kind: "risky", category: "maintenance", reasons: [], warnings: [], in_transaction: false, statements: [sql], count: 1,
  connection_type: "production", connection: { name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "" } });
const optimizeRun = (b) => {
  if (!b.confirmed) return { status: "needs_confirmation", session: editorSession(), confirmation: confirmation(b.sql) };
  const run = editorRun(b.sql);
  run.results = [{ type: "rows", statement: 0, columns: ["Table", "Op", "Msg_type", "Msg_text"].map((name) => ({ name, hex: false, type: null })),
    columns_known: true, rows: [["mfshop.orders", "optimize", "status", "OK"]], row_count: 1, truncated: false, limit: 1000, stopped: null,
    label: null, server_rows: 1, warnings: 0, duration_ms: 3 }];
  return { status: "complete", session: editorSession(), run };
};
async function openOptimize(page) {
  await goPlace(page, "server");
  await chooseView(page, "indexes");
  await pause(900);
  await page.click('button[aria-label="Actions for mfshop.orders.k_status"]');
  await pause(300);
}

export const SCENARIOS_C = [
  {
    name: "indexes-optimize-open", label: "Set invisible… / Optimize… row actions (prepares the review)", owner: "Indexes.tsx / ReviewedSqlAction.tsx", place: "server", sheet: true,
    paths: ["/api/editor/run"], mocks: { "/api/editor/run": optimizeRun },
    setup: async ({ page }) => openOptimize(page), target: named("Set invisible…"),
  },
  {
    name: "indexes-optimize-confirm", label: "Reviewed SQL confirm (Set invisible / Optimize / Analyze / Instruments apply)", owner: "Indexes.tsx / ReviewedSqlAction.tsx", place: "server", sheet: true,
    paths: ["/api/editor/run"], mocks: { "/api/editor/run": optimizeRun },
    setup: async ({ page }) => {
      await openOptimize(page);
      await (await named("Set invisible…")(page)).click();
      await page.waitForSelector("dialog.confirm-sheet[open]");
    },
    target: confirmButton("dialog.confirm-sheet[open]"),
  },
  {
    name: "investigation-delete-sheet", label: "Delete permanently (investigation sheet)", owner: "Library.tsx", place: "investigations", connection: false, sheet: true,
    paths: ["/api/investigations/delete"], modes: ["hang"],
    setup: async ({ page }) => {
      await goPlace(page, "investigations"); await pause(600);
      await (await named("Delete")(page)).click(); await pause(400);
    },
    target: confirmButton("dialog[open]"),
  },
  {
    name: "indexes-export-csv", label: "Export (CSV, local file)", owner: "PaginatedReportTable.tsx / reportTableExport.ts", place: "server", modes: ["ok"],
    paths: [], setup: async ({ page }) => {
      await goPlace(page, "server"); await chooseView(page, "indexes"); await pause(900);
      await (await named("Export Unused indexes")(page)).click(); await pause(300);
    },
    target: named("CSV"),
  },
  {
    name: "audit-export-json", label: "Export (Audit log JSON)", owner: "Audit.tsx", place: "home", connection: false,
    paths: ["/api/audit/export"],
    setup: async ({ page }) => {
      await goPlace(page, "home");
      await (await named("Settings")(page)).click(); await pause(300);
      await (await named("Audit log")(page)).click(); await pause(1200);
      await (await named("Export the audit log")(page)).click(); await pause(300);
    },
    target: named("JSON"),
  },
  {
    name: "sessions-scope-all", label: "All (Processlist scope)", owner: "Sessions.tsx", place: "workload",
    paths: ["/api/live/sessions"],
    setup: async ({ page }) => { await goPlace(page, "workload"); await chooseView(page, "sessions"); await pause(900); },
    target: ".server-view[data-view=sessions] .segmented button[data-value=all]",
  },
  {
    name: "plans-compare", label: "Compare (two open plans)", owner: "main.tsx", place: "plans", connection: false,
    paths: ["/api/compare"],
    setup: async ({ page }) => {
      await goPlace(page, "investigations"); await pause(600);
      await (await named("Open all")(page)).click();
      await pause(3000);
      await goPlace(page, "plans"); await pause(600);
    },
    target: async (page) => {
      for (const h of await page.$$("button")) {
        if (await h.evaluate((e) => e.textContent.trim() === "Compare" && e.getClientRects().length > 0)) return h;
      }
      throw Error("no Compare");
    },
  },
  {
    name: "diagram-import-sql", label: "Import diagram (SQL schema)", owner: "SchemaDiagram.tsx", place: "server", sheet: true,
    paths: ["/api/schema-diagram/import"],
    mocks: { "/api/schema-diagram/import": () => ({ schema_version: "schema-diagram-1.0", warnings: [], relationships: [], tables: [
      { id: "shop.orders", schema: "shop", name: "orders", columns: [{ name: "id", data_type: "bigint", nullable: false, primary_key: true }] }] }) },
    setup: async ({ page }) => {
      await goPlace(page, "server"); await chooseView(page, "diagram"); await pause(900);
      await (await named("Import")(page)).click(); await pause(400);
      const area = await waitVisible(page, "dialog.diagram-import[open] textarea");
      await area.click(); await area.type("CREATE TABLE orders (id bigint primary key);");
      await pause(300);
    },
    target: confirmButton("dialog.diagram-import[open]"),
  },
  {
    name: "editor-run-second", label: "Run (a tab that already has a result)", owner: "QueryLab.tsx / ResultsPane.tsx", place: "query",
    paths: ["/api/editor/run"],
    setup: async ({ page }) => {
      await goPlace(page, "query"); await page.locator('[aria-label="SQL editor"]').fill("SELECT 1"); await pause(500);
      await page.click("[data-action=run]"); await pause(1200);
    },
    target: "[data-action=run]",
  },
  {
    name: "sessions-kill-reread-hangs", label: "Kill confirm while the follow-up re-read hangs", owner: "Sessions.tsx confirmKill", place: "workload", sheet: true, modes: ["ok"],
    paths: ["/api/live/kill"],
    mocks: { "/api/live/kill": () => ({ schema_version: "kill-1.0", status: "killed", connection_id: 94, mode: "query", statement: "KILL QUERY 94",
      verified: ["user"], after: { present: true }, message: "Stopped the running statement on connection 94." }) },
    setup: async (ctx) => {
      const { page } = ctx;
      ctx.mocks["/api/live/sessions"] = () => SESSIONS;
      await goPlace(page, "workload"); await chooseView(page, "sessions"); await pause(900);
      await page.locator('tr[data-session="94"] td:nth-child(3)').click(); await pause(500);
      await page.click('button[aria-label="Kill: connection 94"]');
      await page.waitForSelector("dialog.kill-dialog[open]");
      await confirmButton("dialog.kill-dialog[open]")(page);
      ctx.modes["/api/live/sessions"] = "hang";
    },
    target: confirmButton("dialog.kill-dialog[open]"),
  },
];

