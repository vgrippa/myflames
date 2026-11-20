// Release screenshots for the release-verification record: each release-notes
// view at 1440 px and 720 px, light and dark, as <view>-<width>-<theme>.png,
// plus manifest.json (view, width, theme, file, sha256 per screenshot).
//
// It builds nothing: it opens the built workspace (myflames/ui_assets, from
// `npm run build`) through the perf harness, with the real Python server on a
// throwaway HOME and store. Every database read is answered from the fixtures
// and mocks the suites already use (group-connections.mjs, cluster.mjs,
// topology-appearance.mjs, settings-page.mjs, client-tools.mjs,
// editor-toolbar-layout.mjs, table-data.mjs); nothing is requested outside
// 127.0.0.1. A view that cannot be reached fails the run (non-zero exit, the
// view named) instead of saving an empty frame.
//
//   npm run build && MYFLAMES_NO_SANDBOX=1 MYFLAMES_BROWSER_ARGS=--no-sandbox \
//     node test/release-shots.mjs <out-dir>
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection, layoutMatrix, openConnectionPicker } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const out = process.argv[2] ? path.resolve(process.argv[2]) : "";
assert.ok(out, "usage: node test/release-shots.mjs <out-dir>");
await fs.access(path.join(root, "myflames/ui_assets/index.html")).catch(() => {
  throw new Error("No built workspace in myflames/ui_assets: run `npm run build` in apps/workspace first.");
});
await fs.mkdir(out, { recursive: true });

// ---- fixtures (the same recordings the suites use) -------------------------
const health = await load("health-mysql-8.4-full.json");
const clusterHealthy = await load("cluster-ic-8.4-healthy.json");
const clusterSetReplica = await load("cluster-ic-8.4-read-replica-clusterset.json");
const replication = await load("replication-mysql-8.4-healthy.json");
const proxysql = await load("proxysql-lab.json");
const readyset = await load("readyset-lab.json");

// Cluster overview actions with a disabled action's reason and the GRANT to
// copy (cluster.mjs's grant report, on an overview action).
const clusterGrant = JSON.parse(JSON.stringify(clusterHealthy));
{
  const action = clusterGrant.actions.find((a) => a.id === "switch_to_single_primary");
  action.enabled = false;
  action.why_not = "This account lacks GROUP_REPLICATION_ADMIN.";
  action.fix = "Grant it on the primary, then Refresh.";
  action.missing_privileges = [["GROUP_REPLICATION_ADMIN", "*.*"]];
  action.grant_sql = "GRANT GROUP_REPLICATION_ADMIN ON *.* TO 'dba'@'%';";
}

// ---- saved connections (group-connections.mjs's group) ---------------------
const base = {
  port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
  read_only: false, remember_password: true, has_saved_password: true,
  secret_backend: "vault", secret_backend_label: "myflames encrypted vault",
};
const profiles = [
  { ...base, id: "p-ic-1", name: "db-1", host: "10.0.0.11", type: "development", group: "mf-ic" },
  { ...base, id: "p-ic-2", name: "db-2", host: "10.0.0.12", type: "production", group: "mf-ic" },
  { ...base, id: "p-ic-3", name: "db-3", host: "10.0.0.13", type: "development", group: "mf-ic" },
  { ...base, id: "p-solo", name: "solo-db", host: "10.0.0.99", type: "development", group: "" },
];
const primaryId = "p-ic-2";

// ---- editor and table-data mocks (readme-screenshots.mjs, table-data.mjs) --
const session = (body = {}) => ({
  schema_version: "editor-session-1.0", tab_id: body.tab_id || "1", state: "idle",
  connection: body.connection || { profile_id: "p-ic-1", name: "db-1", host: "10.0.0.11", port: 3306, user: "dba", type: "development", read_only: false },
  engine: "mysql", server_version: "8.4.11", connection_id: 42, autocommit: true, in_transaction: false,
  transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
  backslash_escapes: true, safe_updates: true, safe_updates_default: true, read_only_transactions: false,
  started_at: "2026-11-17T12:00:00Z", idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null,
  message: null, notes: [], run: null,
});
const text = (value) => ({ kind: "text", value });
const pageData = { snapshot_id: "snapshot", schema: "grid_lab", table: "customers", columns: [
  { name: "id", type: "bigint unsigned", kind: "text", nullable: false },
  { name: "name", type: "varchar(120)", kind: "text", nullable: false },
  { name: "email", type: "varchar(255)", kind: "text", nullable: true },
  { name: "payload", type: "varbinary(32)", kind: "hex", nullable: true } ], rows: [
  { id: "first", values: [text("9007199254740993"), text("Alice"), { kind: "null", value: null }, { kind: "hex", value: "00FF" }] },
  { id: "second", values: [text("2"), text("Bruno"), text(""), { kind: "hex", value: "" }] } ],
  editable: true, reason: "", key: ["id"], offset: 0, limit: 50, has_more: false, expires_in: 300 };

// ---- client tools (client-tools.mjs's owner Mac, shell not installed) ------
const BUNDLED = "/Applications/myflames.app/Contents/Resources/client-tools/8.4/bin";
const toolsAnswer = () => ({ status: 200, body: {
  schema_version: "client-tools-1.0",
  installations: [{
    key: `mysql:${BUNDLED}`, engine: "mysql", version: "8.4.13", series: "8.4",
    folder: BUNDLED, found_in: BUNDLED, origin: "bundled", origin_label: "Bundled with myflames",
    bundled: true, has_auth_plugins: true,
    programs: {
      mysql: { path: `${BUNDLED}/mysql`, realpath: `${BUNDLED}/mysql`, version: "8.4.13" },
      mysqldump: { path: `${BUNDLED}/mysqldump`, realpath: `${BUNDLED}/mysqldump`, version: "8.4.13" },
    },
    chosen_for: [],
  }],
  shells: [],
  skipped: [],
  choice: { mysql: { path: null, installation: null, error: null }, mariadb: { path: null, installation: null, error: null } },
} });
const shellStatus = { status: 200, body: { schema_version: "managed-shell-status-1.0", supported: true,
  unsupported_reason: null, platform: "darwin-arm64",
  pinned: { version: "9.7.0", size: 157547585, license: "GPL-2.0-only WITH Universal-FOSS-exception-1.0",
    license_url: "https://www.mysql.com/about/legal/licensing/oem/", downloads_url: "https://dev.mysql.com/downloads/shell/9.7.html" },
  installed: null, job: null } };

const ok = (body) => ({ status: 200, body });
const extra = {
  "/api/profiles/list": ok({ items: profiles, secret_store: { backend: "vault", label: "myflames encrypted vault", available: true } }),
  // Opening a Group connection (group-connections.mjs's mock): the backend
  // picks the primary as the active member.
  "/api/connect": (body) => {
    const name = typeof body.group_name === "string" ? body.group_name.trim() : "";
    const members = profiles.filter((p) => (p.group || "") === name);
    if (!members.length) return { status: 400, body: { error: "No saved connection belongs to this connection group." } };
    const active = members.find((m) => m.id === primaryId) || members[0];
    const type = members.some((m) => m.type !== "development") ? "production" : "development";
    return ok({
      version: "8.4.3", engine: "mysql", flavor: "mysql", product: "MySQL",
      databases: ["shop"], schemas: ["shop"],
      group_connection: {
        group_name: name,
        effective_safety: { type, read_only: false },
        active_member_profile_id: active.id,
        primary_profile_id: active.id,
        members: members.map((m) => ({
          profile_id: m.id, profile_name: m.name, host: m.host, port: m.port,
          safety: { type: m.type, read_only: false }, server_uuid: null,
          role: m.id === primaryId ? "PRIMARY" : "SECONDARY", state: null, reachable: true,
        })),
      },
    });
  },
  "/api/live/health": ok(health),
  "/api/live/readiness": ok({ engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.11", read_only: false,
    super_read_only: false, performance_schema: true, replica: false, group_member: false, galera_member: false,
    privileges: {}, actions: {}, notes: [], checked_at: "2026-11-17T12:00:00Z" }),
  "/api/live/replication": ok(replication),
  "/api/live/replication/skip-loop/status": ok({ schema_version: "replication-skip-loop-1.0", loops: [] }),
  "/api/live/proxysql": ok(proxysql),
  "/api/live/readyset": ok(readyset),
  "/api/live/schemas": ok({ schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11",
    schemas: [{ name: "grid_lab", tables: 1, system: false }], system: [], user_tables: 1, all_note: "" }),
  "/api/live/catalog": (body) => ok(body.table
    ? { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table,
      columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false }], indexes: [] }
    : { schema_version: "catalog-1.0", level: "tables", schema: body.schema, truncated: false, limit: 200, prefix: null,
      after: null, next_after: null, tables: [{ name: "customers", type: "table" }] }),
  "/api/editor/open": (body) => ok({ status: "idle", session: session(body) }),
  "/api/editor/status": (body) => ok({ status: "idle", session: session(body), run: null }),
  "/api/editor/run": ok({ status: "refused", message: "Runs are mocked in this suite." }),
  "/api/editor/close": ok({ closed: true, session: null }),
  "/api/editor/history": ok({ items: [] }),
  "/api/editor/saved/list": ok({ items: [] }),
  "/api/editor/preview": ok({ status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "", warning: "" },
    policy: { action: "allow", text: "" } }),
  "/api/table-data/open": ok(pageData),
  "/api/table-data/close": ok({ status: "closed" }),
  "/api/table-data/prepare": ok({ snapshot_id: "snapshot", review_token: "one-use", statements: [], privileges: [],
    connection: { host: "10.0.0.11", port: 3306, user: "dba" }, connection_type: "development", warnings: [] }),
  "/api/table-data/decline": ok({ status: "ready" }),
  "/api/client/tools": () => toolsAnswer(),
  "/api/client/shell/status": () => shellStatus,
  // Home news, as when the feeds cannot be read: no headline is invented.
  "/api/news": ok({ schema_version: "news-1.0", enabled: true, items: [], fetched_at: null, stale: true, offline: true, sources: [] }),
  // Never asked without a click; refused if it ever is.
  "/api/app/update/check": { status: 500, body: { error: "Not expected while capturing screenshots." } },
};
const seed = `
import sys
from myflames.workspace_store import WorkspaceStore
store = WorkspaceStore(sys.argv[1])
store.set_ui_preferences({"theme": "light"})
print("null")`;

async function fixPage(page) {
  await page.emulateTimezone("UTC");
  const cdp = await page.createCDPSession();
  await cdp.send("Emulation.setLocaleOverride", { locale: "en-US" });
  await page.emulateMediaFeatures([
    { name: "prefers-color-scheme", value: "light" },
    { name: "prefers-reduced-motion", value: "reduce" },
  ]);
}

const h = await open("/api/live/cluster", "release-shots", extra, {
  fallback: ok(clusterHealthy), seed, beforeGoto: fixPage, viewport: { width: 1440, height: 900 },
  refuse: /^\/api\/editor\/(?!saved\/)/,
  offline: true,
  launchArgs: ["--disable-partial-raster", "--force-color-profile=srgb"],
});
const { page } = h;

// With the whole run in one page, waitForFunction (its rAF and interval
// polling alike) stops resolving partway through headless-shell runs while
// page.evaluate keeps answering, so every content wait goes through until().
const until = async (fn, ...args) => {
  for (let i = 0; i < 300; i++) {
    if (await page.evaluate(fn, ...args).catch(() => false)) return;
    await pause(100);
  }
  throw new Error("until: 30 s without the expected state");
};

const manifest = [];
/** Shoot the current page as *name* at 1440 and 720 px, light and dark. */
async function shoot(name) {
  const written = await layoutMatrix(page, out, name);
  for (const file of written) {
    const bytes = await fs.readFile(file);
    const parsed = path.basename(file, ".png").match(/^(.*)-(\d+)-(light|dark)$/);
    manifest.push({ view: parsed[1], width: Number(parsed[2]), theme: parsed[3],
      file: path.basename(file), sha256: createHash("sha256").update(bytes).digest("hex") });
  }
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  console.log("shot", name);
}

/** Run *reach* and shoot *name*; a failure names the view and stops the run. */
async function view(name, reach) {
  try {
    await reach();
  } catch (error) {
    const now = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").slice(0, 400)).catch(() => "");
    error.message = `release-shots: view "${name}" could not be reached: ${error.message}\non screen: ${now}`;
    throw error;
  }
  await until(() => !document.querySelector(".spinner, [aria-busy=true]")).catch(() => {});
  await shoot(name);
}
/** Set the Cluster read's answer and refresh like cluster.mjs's refreshTo:
 * fail unless exactly one new read arrived and the view settled. */
async function refreshClusterTo(report) {
  h.state.fallback = ok(report);
  const before = h.state.seen.length;
  await h.refresh();
  for (let i = 0; i < 200 && h.state.seen.length === before; i++) await pause(50);
  assert.ok(h.state.seen.length > before, "the Cluster refresh did not read again");
  await until((sel) => !document.querySelector(`${sel} .spinner`), clusterView);
  await pause(200);
}

const clickButton = async (label, scope = "") => {
  const found = (label, scope) => [...document.querySelectorAll(`${scope} button`)]
    .filter((b) => b.textContent.trim().startsWith(label) && b.getClientRects().length && !b.disabled).length;
  await until(found, label, scope);
  await page.evaluate((label, scope) => [...document.querySelectorAll(`${scope} button`)]
    .find((b) => b.textContent.trim().startsWith(label) && b.getClientRects().length && !b.disabled).click(), label, scope);
};
const openSettingsSection = async (label) => {
  await clickButton(label, ".settings-page ");
  await until((t) => document.querySelector(".settings-panel-title")?.textContent === t, label);
};
const clusterView = '.server-view[data-view="cluster"]';
const backToList = async () => {
  const back = await page.$(".view-inspector.is-detail .detail-back");
  if (back) { await back.click(); await until(() => !document.querySelector(".view-inspector.is-detail")); }
};

try {
  // group-picker: the connection picker listing the Group connection.
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".window-toolbar .connection-pill", { visible: true });
  await view("group-picker", async () => {
    await openConnectionPicker(page);
    await page.waitForSelector('.picker-panel .group-connection-row[data-group="mf-ic"]', { visible: true });
  });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector(".window-toolbar .picker-panel"));

  // Open the Group connection for the next two views.
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection mf-ic"]');
  await until(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-ic"));

  // group-editor-sheet: Edit group… from the picker.
  await view("group-editor-sheet", async () => {
    await openConnectionPicker(page);
    await page.click('.picker-panel button[aria-label="Edit group mf-ic"]');
    await page.waitForSelector("dialog.group-editor-sheet[open]", { visible: true });
    await until(() => document.querySelector('dialog.group-editor-sheet input[aria-label="Group name"]')?.value === "mf-ic");
  });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector("dialog.group-editor-sheet"));

  // group-server-picker: the Server view header's Server: control on a Group connection.
  await view("group-server-picker", async () => {
    await h.server();
    await page.waitForSelector('.server-view[data-view="health"] .group-server-picker', { visible: true });
    await until(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("db-2"));
  });

  // The cluster views run on a plain saved connection.
  await chooseConnection(page, "p-ic-1");
  await until(() => !document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection"));

  // cluster-actions: the overview action band, a disabled action's reason and
  // its GRANT to copy.
  h.state.fallback = ok(clusterGrant);
  await view("cluster-actions", async () => {
    await page.click('.main-nav button[data-place="cluster"]');
    await page.waitForSelector(`${clusterView} .cluster-actions .review-actions > button`, { visible: true });
    await page.waitForSelector(`${clusterView} .cluster-actions .cluster-action-why .ops-pre`, { visible: true });
  });

  // cluster-member-actions: a member inspector's actions.
  await view("cluster-member-actions", async () => {
    await h.select(`${clusterView} .cluster-card[data-row-key="${clusterHealthy.members[1].key}"]`);
    await until(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
      .some((b) => b.textContent.trim() === "Stop Group Replication…"));
  });
  await backToList();

  // clusterset-band: the ClusterSet band seen through a read replica.
  await view("clusterset-band", async () => {
    await refreshClusterTo(clusterSetReplica);
    await page.waitForSelector(`${clusterView} .clusterset-band`, { visible: true });
    await until((sel) => document.querySelector(`${sel} .clusterset-band .cluster-row-label`)?.textContent.includes("ClusterSet mfcs"), clusterView);
  });

  // topology-cluster: the healthy InnoDB Cluster stage.
  await view("topology-cluster", async () => {
    await refreshClusterTo(clusterHealthy);
    await until((sel) => document.querySelectorAll(`${sel} .cluster-topology .cluster-card`).length >= 3, clusterView);
  });

  // topology-replication.
  await view("topology-replication", async () => {
    await page.click('.main-nav button[data-place="replication"]');
    await page.waitForSelector('.server-view[data-view="replication"] .replication-topology-container', { visible: true });
  });

  // topology-proxysql.
  await view("topology-proxysql", async () => {
    await page.click('.main-nav button[data-place="proxysql"]');
    const view1 = '.server-view[data-view="proxysql"]';
    await page.waitForSelector(`${view1} .segmented button`, { visible: true });
    await page.evaluate((v) => [...document.querySelectorAll(`${v} .segmented button`)].find((b) => b.textContent.trim() === "Topology").click(), view1);
    await page.waitForSelector(`${view1} .readyset-topology`, { visible: true });
  });

  // topology-readyset (the Stage look).
  await view("topology-readyset", async () => {
    await page.click('.main-nav button[data-place="readyset"]');
    await page.waitForSelector('.server-view[data-view="readyset"] .readyset-topology', { visible: true });
  });

  // version-badge: the version beside the name (Home).
  await view("version-badge", async () => {
    await page.click('.main-nav button[data-place="home"]');
    await page.waitForSelector(".home", { visible: true });
    await until(() => /\d+\.\d+\.\d+|development/.test(document.querySelector(".brand-version")?.textContent || ""));
  });

  // settings-updates: Settings > Updates and news.
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector(".settings-page", { visible: true });
  await view("settings-updates", async () => {
    await openSettingsSection("Updates and news");
    await until(() => /myflames \d+\.\d+\.\d+/.test(document.querySelector(".settings-update-status")?.textContent || ""));
  });

  // client-tools-shell: the Client tools dialog with Get MySQL Shell.
  await view("client-tools-shell", async () => {
    await openSettingsSection("Client tools");
    await clickButton("Client tools…", ".settings-panel ");
    await page.waitForSelector("dialog.client-tools-dialog[open]", { visible: true });
    await until(() => [...document.querySelectorAll("dialog.client-tools-dialog button")]
      .some((b) => b.textContent.includes("Get MySQL Shell")));
  });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector("dialog.client-tools-dialog"));

  // settings-text: Settings > Appearance, the Text group.
  await view("settings-text", async () => {
    await openSettingsSection("Appearance");
    await until(() => document.querySelector("#ui-font")?.disabled === false && !!document.querySelector("#code-font"));
  });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector(".settings-page"));

  // editor-toolbar: Find, Find and replace and the Editing actions menu open.
  await view("editor-toolbar", async () => {
    await page.click('.main-nav button[data-place="query"]');
    await page.waitForSelector(".query-lab .editor-toolbar", { visible: true });
    const SQL = "SELECT id, customer_id, status, total FROM orders ORDER BY id LIMIT 24;";
    await page.$eval(".sql-editor", (el, t) => {
      el.focus();
      el.select();
      const data = new DataTransfer();
      data.setData("text/plain", t);
      el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
    }, SQL);
    await until((t) => document.querySelector(".sql-editor")?.value === t, SQL);
    await page.click('.editor-toolbar button[aria-label="Editing actions"]');
    await page.waitForSelector(".editor-editing-menu", { visible: true });
  });
  await page.keyboard.press("Escape");

  // table-data-review: the review button reading "Review 1 change".
  await view("table-data-review", async () => {
    if (await page.$('button[aria-label="Show the schema browser"]')) await page.click('button[aria-label="Show the schema browser"]');
    await page.waitForSelector(".tree-row.kind-schema", { visible: true });
    await page.click(".tree-row.kind-schema");
    await page.waitForSelector(".tree-row.kind-table", { visible: true });
    await page.click(".tree-row.kind-table", { button: "right" });
    await clickButton("Edit table data");
    await page.waitForSelector(".table-data-panel input", { visible: true });
    await page.$eval('input[aria-label="name for first"]', (el, v) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
      setter.call(el, v);
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }, "Bob");
    await until(() => document.querySelector(".table-data-actions .primary")?.textContent.trim() === "Review 1 change");
  });

  assert.deepEqual(h.errors, [], "page errors");
  assert.deepEqual(h.state.blocked, [], "requests outside the local server (blocked)");
  await fs.writeFile(path.join(out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log("wrote", path.join(out, "manifest.json"), `(${manifest.length} screenshots)`);
} finally {
  await h.close();
}
