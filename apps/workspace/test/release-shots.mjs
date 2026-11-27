// Release screenshots for the release-verification record: each release-notes
// view at 1440 px and 720 px, light and dark, as <view>-<width>-<theme>.png,
// plus manifest.json (view, width, theme, file, sha256 per screenshot).
//
// It builds nothing: it opens the built workspace (myflames/ui_assets, from
// `npm run build`) through the perf harness, with the real Python server on a
// throwaway HOME and store. Every database read is answered from the fixtures
// and mocks the suites already use (group-connections.mjs, cluster.mjs,
// topology-place.mjs, topology-appearance.mjs, settings-page.mjs,
// client-tools.mjs, editor-toolbar-layout.mjs, table-data.mjs, plan-editor-layout.mjs,
// update-check.mjs, health.mjs, config.mjs, schema-diagram.mjs and mermaid-export.mjs);
// nothing is requested outside
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
  { ...base, id: "p-ic-4", name: "db-4", host: "10.0.0.14", type: "development", group: "mf-ic" },
  { ...base, id: "p-solo", name: "solo-db", host: "10.0.0.99", type: "development", group: "" },
  // A second group (topology-place.mjs's mf-two) so Home lists two group cards.
  { ...base, id: "p-two-1", name: "two-1", host: "10.0.0.21", type: "development", group: "mf-two" },
  { ...base, id: "p-two-2", name: "two-2", host: "10.0.0.22", type: "development", group: "mf-two" },
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

// ---- Topology place (topology-place.mjs's group report and rolling plan) ---
// The group is mf-ic here: three readable members in one Group Replication
// cluster (a verified link between them), a failed member, a router alias of
// db-1 and an outside peer reached through an address-only (unverified) link.
const topoMember = (over) => ({
  profile_id: "", profile_name: "", host: "", port: 3306,
  safety: { type: "production", read_only: false },
  outcome: "read", message: "Read from this member.", elapsed_s: 0.1,
  engine: "mysql", product: "MySQL", version: "8.4.6", version_comment: "MySQL Community Server - GPL",
  server_uuid: null, server_id: null, hostname: null,
  read_only: false, read_only_value: "OFF", super_read_only: false,
  role: "standalone", state: "reachable", lag: null, behind: null, behind_reason: null, channels: [],
  ...over,
});
const lagMessage = "Transactions waiting in this member's applier queue.";
const behindMessage = "Transactions in the primary or source sample absent from this member sample.";
const topoMembers = [
  topoMember({ profile_id: "p-ic-1", profile_name: "db-1", host: "10.0.0.11", version: "8.4.6",
    role: "primary", state: "ONLINE",
    lag: { kind: "gr_applier_queue", value: 0, message: lagMessage },
    behind: { kind: "mysql_gtid_transactions", value: 0, message: behindMessage } }),
  topoMember({ profile_id: "p-ic-2", profile_name: "db-2", host: "10.0.0.12", version: "8.4.4",
    role: "secondary", state: "ONLINE", super_read_only: true, read_only: true, read_only_value: "ON",
    lag: { kind: "gr_applier_queue", value: 3, message: lagMessage },
    behind: { kind: "mysql_gtid_transactions", value: 1204, message: behindMessage } }),
  topoMember({ profile_id: "p-ic-3", profile_name: "db-3", host: "10.0.0.13", version: "8.4.6",
    role: "secondary", state: "ONLINE", super_read_only: true, read_only: true, read_only_value: "ON",
    lag: { kind: "gr_applier_queue", value: 0, message: lagMessage },
    behind: { kind: "mysql_gtid_transactions", value: 0, message: behindMessage } }),
  topoMember({ profile_id: "p-ic-4", profile_name: "db-4", host: "10.0.0.14",
    outcome: "failed", message: "Access denied for user 'dba'@'10.0.0.14'.",
    engine: null, product: null, version: null, version_comment: null,
    read_only: null, read_only_value: null, super_read_only: null, role: null, state: null }),
];
const topoAliasMember = topoMember({
  profile_id: "p-ic-5", profile_name: "db-1-router", host: "10.0.0.15", port: 6446,
  role: "primary", state: "ONLINE", duplicate_of: "p-ic-1",
  server_uuid: "11111111-1111-1111-1111-111111111111",
});
const topoSummary = { read: 3, failed: 1, not_reached: 0, versions: ["8.4.4", "8.4.6"], text: "3 read, 1 failed." };
function topoReport(section, extraReport = {}) {
  return {
    schema_version: "topology-group-1.0", section, group_name: "mf-ic",
    taken_at: "2026-11-21T12:00:00Z", elapsed_s: 0.4,
    effective_safety: { type: "production", read_only: false },
    active_member_profile_id: "p-ic-1", primary_profile_id: "p-ic-1",
    members: topoMembers, summary: topoSummary, ...extraReport,
  };
}
const topoNode = (id, over) => ({
  id, kind: "member", profile_id: id, label: over.profile_name || "", host: "", port: 3306,
  engine: "mysql", product: "MySQL", version: "8.4.6", role: "standalone", state: "ONLINE", outcome: "read",
  server_uuid: null, cluster_id: null, ...over,
});
const GR_CLUSTER = "gr:cccccccc-cccc-cccc-cccc-cccccccccccc";
const topoPeerId = "peer:10.0.0.77:3306";
const topologyReports = {
  map: topoReport("map", {
    members: [...topoMembers, topoAliasMember],
    map: {
      nodes: [
        topoNode("p-ic-1", { label: "db-1", host: "10.0.0.11", role: "primary", version: "8.4.6",
          server_uuid: "11111111-1111-1111-1111-111111111111", cluster_id: GR_CLUSTER }),
        topoNode("p-ic-2", { label: "db-2", host: "10.0.0.12", role: "secondary", version: "8.4.4",
          server_uuid: "22222222-2222-2222-2222-222222222222", cluster_id: GR_CLUSTER }),
        topoNode("p-ic-3", { label: "db-3", host: "10.0.0.13", role: "secondary", version: "8.4.6",
          server_uuid: "33333333-3333-3333-3333-333333333333", cluster_id: GR_CLUSTER }),
        topoNode("p-ic-4", { label: "db-4", host: "10.0.0.14", role: null, state: null, outcome: "failed",
          engine: null, product: null, version: null }),
        topoNode("p-ic-5", { label: "db-1-router", host: "10.0.0.15", port: 6446, role: "primary",
          server_uuid: "11111111-1111-1111-1111-111111111111", duplicate_of: "p-ic-1" }),
        // topology-place.mjs's offline route fixture, retaining its short
        // hostgroup label and the longer reason only in the tooltip/list.
        topoNode("proxy", { label: "ProxySQL", host: "10.0.0.16", port: 6032,
          role: "proxy", product: "ProxySQL", version: "3.0.2" }),
        { id: topoPeerId, kind: "peer", profile_id: null, label: "10.0.0.77:3306", host: "10.0.0.77", port: 3306,
          server_uuid: null, seen_by: ["p-ic-2"], reason: "Not in this group" },
      ],
      edges: [
        { from: "p-ic-1", to: "p-ic-2", kind: "replication", evidence: "server_uuid", verified: true, state: "running", label: "Replication" },
        { from: "p-ic-1", to: "p-ic-3", kind: "replication", evidence: "server_uuid", verified: true, state: "running", label: "Replication" },
        { from: "p-ic-1", to: topoPeerId, kind: "replication", evidence: "endpoint", verified: false, state: null, label: "Replication" },
        { from: "p-ic-5", to: "p-ic-1", kind: "alias", evidence: "server_uuid", verified: false, state: null, label: "same server (alias)" },
        { from: "proxy", to: "p-ic-1", kind: "proxy_route", evidence: "endpoint", verified: false,
          state: "OFFLINE_HARD", label: "hostgroup 40",
          reason: "ProxySQL has taken this backend out of traffic. No ONLINE member in this hostgroup." },
      ],
      clusters: [
        { id: GR_CLUSTER, kind: "group_replication", name: "cccccccc-cccc-cccc-cccc-cccccccccccc",
          members: ["p-ic-1", "p-ic-2", "p-ic-3"], seen_from: ["p-ic-1", "p-ic-2", "p-ic-3"] },
      ],
      notes: [{ profile_id: "p-ic-4", message: "Access denied for user 'dba'@'10.0.0.14'." }],
    },
  }),
  members: topoReport("members"),
  parameters: topoReport("parameters", {
    parameters: {
      rows: [
        { name: "group_replication_group_name", status: "missing", values: { "p-ic-1": "cccccccc-cccc-cccc-cccc-cccccccccccc" }, apply_type: null, dynamic: false },
        { name: "max_connections", status: "different", values: { "p-ic-1": 500, "p-ic-2": 200, "p-ic-3": 500 }, apply_type: null, dynamic: true },
        { name: "innodb_buffer_pool_size", status: "same", values: { "p-ic-1": 134217728, "p-ic-2": 134217728, "p-ic-3": 134217728 }, apply_type: null, dynamic: false },
        { name: "server_id", status: "per_server", values: { "p-ic-1": 11, "p-ic-2": 12, "p-ic-3": 13 }, apply_type: null, dynamic: false, reason: "Server identity: each member keeps its own value." },
        { name: "audit_log_encryption", status: "missing", values: { "p-ic-1": "[redacted]", "p-ic-2": "[redacted]", "p-ic-3": "[redacted]" }, apply_type: null, dynamic: true, reason: "A value was redacted, truncated or unavailable; it cannot be compared." },
      ],
      compared: ["p-ic-1", "p-ic-2", "p-ic-3"],
      not_compared: [{ profile_id: "p-ic-4", reason: "Access denied for user 'dba'@'10.0.0.14'." }],
    },
  }),
};
// The rolling change plan (topology-place.mjs's planned members): db-2 and
// db-3 as secondaries first, then the primary db-1, each with its statement
// and before and after values.
const rollingMember = (over) => ({
  profile_id: "", profile_name: "", host: "", port: 3306, identity: null,
  status: "planned", statement: null, before: null, after: null, effect: null,
  caveats: [], privileges: null, config_line: null, reason: null, ...over,
});
const ROLLING_ORDER = "Group Replication and InnoDB Cluster secondaries first, then the primary; ties use saved name order.";
const rollingPlanReply = {
  schema_version: "topology-change-plan-1.0", plan_id: "plan-1", change_set_id: "cs-1",
  group_name: "mf-ic", name: "max_connections", value: "400", mode: "persist",
  order_rule: ROLLING_ORDER,
  effective_safety: { type: "production", read_only: false },
  confirmation: { required: true, typed_name: "mf-ic" },
  members: [
    rollingMember({ profile_id: "p-ic-2", profile_name: "db-2", host: "10.0.0.12",
      statement: "SET PERSIST max_connections = 400;",
      before: { global: "200", persisted: null }, after: { global: "400", persisted: "400" },
      effect: { global: "200 to 400" } }),
    rollingMember({ profile_id: "p-ic-3", profile_name: "db-3", host: "10.0.0.13",
      statement: "SET PERSIST max_connections = 400;",
      before: { global: "500", persisted: null }, after: { global: "400", persisted: "400" },
      effect: { global: "500 to 400" } }),
    rollingMember({ profile_id: "p-ic-1", profile_name: "db-1", host: "10.0.0.11",
      statement: "SET PERSIST max_connections = 400;",
      before: { global: "500", persisted: null }, after: { global: "400", persisted: "400" },
      effect: { global: "500 to 400" } }),
  ],
  expect: {
    group_name: "mf-ic", group_generation: "g1", active_member_profile_id: "p-ic-1",
    active_endpoint: { host: "10.0.0.11", port: 3306, user: "dba", ssl_mode: "PREFERRED", ssl_ca: "", ssh_host: "", ssh_user: "", ssh_port: 22 },
    effective_safety: { type: "production", read_only: false }, members: [],
  },
};

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
  // The Topology place's one endpoint: one read of the whole group per tab
  // (topology-place.mjs's mock, without its request assertions).
  "/api/live/topology": (body) => {
    const reply = topologyReports[body.section];
    return reply ? ok(reply) : { status: 400, body: { error: `unknown topology section ${body.section}` } };
  },
  "/api/live/topology/variable/plan": () => ok(rollingPlanReply),
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
async function shoot(name, options) {
  const written = await layoutMatrix(page, out, name, options);
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
async function view(name, reach, options) {
  try {
    await reach();
  } catch (error) {
    const now = await page.evaluate(() => document.body.innerText.replace(/\s+/g, " ").slice(0, 400)).catch(() => "");
    error.message = `release-shots: view "${name}" could not be reached: ${error.message}\non screen: ${now}`;
    throw error;
  }
  await until(() => !document.querySelector(".spinner, [aria-busy=true]")).catch(() => {});
  await shoot(name, options);
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
  // home-groups: Home with two group cards (members listed, Open on each),
  // the Saved connections heading and New group… beside Add connection.
  await view("home-groups", async () => {
    await page.click('.main-nav button[data-place="home"]');
    await page.waitForSelector('[data-testid="home-group-connections"]', { visible: true });
    await until(() => {
      for (const group of ["mf-ic", "mf-two"]) {
        const row = document.querySelector(`[data-testid="home-group-connections"] .group-connection-row[data-group="${group}"]`);
        if (!row || !row.offsetParent) return false;
        if (!row.querySelector(".group-connection-members")?.textContent.trim()) return false;
        if (row.querySelector('button[data-action="open-group"]')?.textContent.trim() !== "Open") return false;
      }
      return true;
    });
    await until(() => [...document.querySelectorAll(".connections h3")]
      .some((e) => e.textContent.trim() === "Saved connections"));
    await until(() => [...document.querySelectorAll(".connections-header-actions button")]
      .some((b) => b.textContent.includes("New group…"))
      && [...document.querySelectorAll(".connections-header-actions button")]
        .some((b) => b.textContent.includes("Add connection")));
  });

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

  // topology-map: the Topology place on the Group connection, Map tab: a
  // cluster box with three members, a verified link, an unverified link, a
  // router alias and an outside peer, with the diagram drawn.
  await view("topology-map", async () => {
    await page.click('.main-nav button[data-place="topology"]');
    await page.waitForSelector(".topology-place .segmented button", { visible: true });
    await page.waitForSelector(".topology-map-diagram .topology-map-cluster", { visible: true });
    await until(() => document.querySelectorAll(".topology-map-diagram .topology-map-cluster button[data-node]").length === 3);
    await until(() => !!document.querySelector('.topology-map-diagram button[data-node="p-ic-5"]')
      && document.querySelector('.topology-map-diagram button[data-node="p-ic-5"]').textContent.includes("Alias of db-1"));
    await until(() => !!document.querySelector('.topology-map-diagram button[data-node="peer:10.0.0.77:3306"]'));
    await until(() => !!document.querySelector('.topology-map-diagram .topology-map-edge[data-verified="true"]')
      && !!document.querySelector('.topology-map-diagram .topology-map-edge[data-verified="false"]'));
    await until(() => {
      const route = document.querySelector('.topology-map-edge[data-edge-kind="proxy_route"]');
      return route?.classList.contains("tone-problem") && route.querySelector("title")?.textContent.includes("OFFLINE_HARD")
        && /^hostgroup 40(?: · OFFLINE_HARD)?$/.test(route.querySelector("text")?.textContent.trim() || "");
    });
  });

  // topology-members: the Members tab with roles, state, version, read-only
  // and lag filled.
  await view("topology-members", async () => {
    await clickButton("Members", ".topology-place .segmented ");
    await page.waitForSelector('.topology-place tr[data-row-key="p-ic-2"]', { visible: true });
    await until(() => document.querySelector('.topology-place tr[data-row-key="p-ic-1"]')?.textContent.includes("Primary")
      && document.querySelector('.topology-place tr[data-row-key="p-ic-2"]')?.textContent.includes("Secondary")
      && document.querySelector('.topology-place tr[data-row-key="p-ic-2"]')?.textContent.includes("MySQL 8.4.4")
      && document.querySelector('.topology-place tr[data-row-key="p-ic-2"]')?.textContent.includes("On (both)")
      && [...document.querySelectorAll('.topology-place tr[data-row-key="p-ic-2"] [title]')]
        .some((e) => e.title.includes("gr_applier_queue")));
  });

  // topology-parameters: the Parameters tab with the differing rows, the Per
  // server row and the Not compared member, a row selected so its inspector
  // shows Change on every member….
  await view("topology-parameters", async () => {
    await clickButton("Parameters", ".topology-place .segmented ");
    await page.waitForSelector('.topology-place tr[data-row-key="max_connections"]', { visible: true });
    await clickButton("All", ".topology-place .drift-controls ");
    await page.waitForSelector('.topology-place tr[data-row-key="server_id"]', { visible: true });
    await until(() => [...document.querySelectorAll('.topology-place tr[data-row-key="server_id"] .pill')]
      .some((e) => e.textContent.trim() === "Per server"));
    await until(() => document.querySelector(".topology-place")?.textContent.includes("Not compared"));
    await h.select('.topology-place tr[data-row-key="max_connections"]');
    await until(() => [...document.querySelectorAll(".view-inspector button")]
      .some((b) => b.textContent.trim() === "Change on every member…"));
    // Back to the matrix for the frame: the row keeps its selection.
    await page.click(".view-inspector.is-detail .detail-back");
    await until(() => !document.querySelector(".view-inspector.is-detail"));
    await until(() => !!document.querySelector('.topology-place tr[data-row-key="max_connections"].is-selected'));
  });

  // topology-rolling-review: the Change on every member… sheet at its review
  // step (each member's statement and before and after values, the order
  // sentence).
  const sheetSel = "dialog.rolling-change-sheet[open]";
  const sheetClick = async (text) => {
    const found = await page.evaluate((t) => {
      const button = [...document.querySelectorAll("dialog.rolling-change-sheet .sheet-buttons button, dialog.rolling-change-sheet .rolling-applying button")]
        .find((b) => b.textContent.trim() === t && !b.disabled);
      if (!button) return false;
      button.click();
      return true;
    }, text);
    assert.ok(found, `sheet button "${text}"`);
  };
  await view("topology-rolling-review", async () => {
    await h.select('.topology-place tr[data-row-key="max_connections"]');
    await until(() => [...document.querySelectorAll(".view-inspector button")]
      .some((b) => b.textContent.trim() === "Change on every member…"));
    await page.evaluate(() => [...document.querySelectorAll(".view-inspector button")]
      .find((b) => b.textContent.trim() === "Change on every member…").click());
    await page.waitForSelector(sheetSel, { visible: true });
    await until((sel) => document.querySelector(`${sel} .rolling-field input`)?.value === "500", sheetSel);
    await page.click(`${sheetSel} .rolling-field input`, { count: 3 });
    await page.type(`${sheetSel} .rolling-field input`, "400");
    await sheetClick("Review");
    await page.waitForSelector(`${sheetSel} .rolling-review-list li[data-member="p-ic-2"]`, { visible: true });
    await until(() => {
      const text = document.querySelector("dialog.rolling-change-sheet")?.textContent || "";
      return text.includes("SET PERSIST max_connections = 400;") && text.includes("200 → 400")
        && text.includes("secondaries first, then the primary");
    });
  });
  await sheetClick("Back");
  await page.waitForSelector(`${sheetSel} .rolling-members`, { visible: true });
  await sheetClick("Cancel");
  await until(() => !document.querySelector("dialog.rolling-change-sheet"));
  await backToList();

  // The cluster views run on a plain saved connection.
  await chooseConnection(page, "p-ic-1");
  await until(() => !document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection"));

  // cluster-actions: the overview action band, a disabled action's message
  // with its reason and its GRANT to copy.
  h.state.fallback = ok(clusterGrant);
  await view("cluster-actions", async () => {
    await page.click('.main-nav button[data-place="cluster"]');
    await page.waitForSelector(`${clusterView} .cluster-actions .review-actions > button`, { visible: true });
    await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
      .find((b) => b.textContent.trim() === "Switch to single-primary…")?.click(), clusterView);
    await page.waitForSelector(`${clusterView} .cluster-actions .cluster-action-why:popover-open .ops-pre`, { visible: true });
  });

  // cluster-member-actions: a member inspector's actions.
  await view("cluster-member-actions", async () => {
    await h.select(`${clusterView} .cluster-card[data-row-key="${clusterHealthy.members[1].key}"]`);
    await until(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
      .some((b) => b.textContent.trim() === "Stop Group Replication…"));
  });
  await backToList();

  // clusterset-map: the zoomable ClusterSet map seen through a read replica.
  await view("clusterset-map", async () => {
    await refreshClusterTo(clusterSetReplica);
    await page.waitForSelector(`${clusterView} .clusterset-map`, { visible: true });
    await until((sel) => document.querySelector(`${sel} .clusterset-map .clusterset-setline`)?.textContent.includes("ClusterSet mfcs"), clusterView);
    const controls = await page.$$eval(`${clusterView} .cluster-actions[aria-label="ClusterSet changes"] .review-actions > button`,
      (buttons) => buttons.map((b) => ({ label: b.textContent.trim(), disabled: b.getAttribute("aria-disabled") === "true" })));
    assert.equal(controls.length, 9, "the screenshot exercises all nine backend set controls");
    const check = clusterSetReplica.actions.find((a) => a.id === "check_clusterset");
    assert.deepEqual(controls.find((b) => b.label === check.label), { label: check.label, disabled: false },
      "the real Check action stays available in the read-replica scenario");
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

  await view("visual-explain", async () => {
    // plan-editor-layout.mjs imports this recorded JSON through the file picker.
    await page.click('.main-nav button[data-place="home"]');
    await page.click('button[aria-label="Import plan"]');
    const file = await page.waitForSelector('input[aria-label="Choose JSON plan file"]');
    await file.uploadFile(path.join(root, "test/mysql-explain-complex-join.json"));
    await until(() => !document.querySelector(".import-dialog"));
    await page.waitForSelector('.plan-canvas .plan-chart svg.wb-plan[data-wired="true"]', { visible: true });
    await clickButton("Visual Explain", ".chart-views ");
    const viewport = ".plan-canvas .plan-chart .wb-viewport";
    const before = await page.$eval(viewport, (el) => el.getAttribute("viewBox"));
    await page.click('.plan-canvas .chart-toolbar [data-action="in"]');
    await until((selector, previous) => document.querySelector(selector)?.getAttribute("viewBox") !== previous, viewport, before);
    await until(() => document.querySelector('.plan-canvas [data-action="reset"]')?.textContent.trim() === "125%");
  });

  await view("update-dialog", async () => {
    // update-check.mjs's available-release reply and Download flow. Install
    // controls are never used; checking only reads this in-memory reply.
    const current = await page.$eval('meta[name="myflames-version"]', (el) => el.content);
    // Stay newer than the release being captured, including a release build
    // that has already been bumped to 3.5.2. No real release is advertised.
    const [major, minor, patch] = current.split(".").map(Number);
    const latest = `${major}.${minor}.${patch + 1}`;
    const checked = new Date().toISOString();
    extra["/api/app/update/check"] = ok({
      schema_version: "app-update-1.0", current_version: current,
      latest_version: latest, update_available: true,
      release_title: `myflames ${latest}`, published_at: "", checked_at: checked,
      release_notes: "## Release highlights\n\n- **Group connections** open a group as one connection, with `sql_mode` inline code.\n- A second bullet with <img src=x onerror=alert(1)> inside.\n\nA closing paragraph.",
      html_url: "https://github.com/vgrippa/myflames/releases",
      asset_name: null, asset_url: null, asset_size: null,
      last_check: { checked_at: checked, outcome: "available", current_version: current, latest_version: latest, reason: null },
    });
    await page.click('button[aria-label="Settings"]');
    await openSettingsSection("Updates and news");
    await page.click(".settings-update-status .update-check-button");
    await page.waitForSelector(".settings-update-status .update-check-download", { visible: true });
    await page.click(".settings-update-status .update-check-download");
    await page.waitForSelector("dialog.update-dialog[open] .update-notes-content", { visible: true });
    await until(() => document.querySelectorAll(".update-notes-content li").length === 2
      && !!document.querySelector(".update-notes-content strong")
      && !!document.querySelector(".update-notes-content code"));
    assert.equal(await page.$(".update-notes-content img"), null, "release-note HTML stays text");
  });
  await clickButton("Later", "dialog.update-dialog ");
  await until(() => !document.querySelector("dialog.update-dialog"));
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector(".settings-page"));

  await view("health-watch", async () => {
    // health.mjs's watch stays below the default capture threshold. No
    // capture (and therefore no secondary database read) can be triggered.
    extra["/api/live/watch"] = ok({
      schema_version: "watch-1.0", engine: "mysql", server_version: "8.4.11",
      read_at: "2026-10-27T14:02:31Z", threads_running: 4,
      longest_statement: { id: 42, seconds: 2, user: "app", db: "shop" },
      longest_lock_wait: null, errors: {}, notes: [], reason: null,
    });
    await h.server();
    await h.view("health");
    const healthView = '.server-view[data-view="health"]';
    // The earlier Health frame belonged to the Group connection. Read the
    // fixture again for the plain connection before starting the watch.
    const before = h.state.other.filter((r) => r.path === "/api/live/health").length;
    await h.refresh();
    for (let i = 0; i < 100 && h.state.other.filter((r) => r.path === "/api/live/health").length === before; i++) await pause(50);
    assert.ok(h.state.other.filter((r) => r.path === "/api/live/health").length > before, "Health refreshed for the connection in use");
    await until((sel) => !document.querySelector(`${sel} .spinner, ${sel} .other-connection`), healthView);
    await clickButton("Capture when…", `${healthView} `);
    await page.waitForSelector(`${healthView} .capture-when-form`, { visible: true });
    await page.select(`${healthView} select[aria-label="Check interval"]`, "30");
    await clickButton("Start", `${healthView} .capture-when-form `);
    await until(() => /Watching: checked [1-9]/.test(document.querySelector(".health-watch-line")?.textContent || ""));
    await page.waitForSelector(`${healthView} .health-watch-line button`, { visible: true });
  });
  await clickButton("Stop", '.server-view[data-view="health"] .health-watch-line ');
  await until(() => !document.querySelector(".health-watch-line"));

  await view("server-popover", async () => {
    // health.mjs's compact Locks Columns menu, portaled near the toolbar.
    extra["/api/live/locks"] = ok(await load("locks-mysql-8.4.json"));
    await page.setViewport({ width: 720, height: 600 });
    await h.view("locks");
    const button = '.server-view[data-view="locks"] button[aria-label="Columns of Transactions"]';
    await page.waitForSelector(button, { visible: true });
    await page.evaluate((selector) => {
      const opener = document.querySelector(selector);
      opener.scrollIntoView({ block: "center", behavior: "instant" });
      window.scrollBy(0, Math.round(opener.getBoundingClientRect().top - innerHeight * 0.72));
    }, button);
    await page.click(button);
    await page.waitForSelector('.popover[aria-label="Columns of Transactions"]', { visible: true });
    assert.equal(await page.$eval('.popover[aria-label="Columns of Transactions"]', (el) => el.parentElement === document.body), true,
      "Columns menu escapes the content stacking context");
  }, { frame: '.popover[aria-label="Columns of Transactions"]' });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector('.popover[aria-label="Columns of Transactions"]'));
  await page.setViewport({ width: 1440, height: 900 });

  await view("parameters-set-value", async () => {
    // config.mjs's recorded set catalog and unknown-member hint. The draft
    // remains local: no planning or apply request is needed for this frame.
    const config = await load("parameters-mysql.json");
    extra["/api/live/config"] = ok(config);
    await h.view("config");
    const configView = '.server-view[data-view="config"]';
    await page.waitForSelector(`${configView} [data-action="edit-parameters"]`, { visible: true });
    await page.click(`${configView} [data-action="edit-parameters"]`);
    await page.type(`${configView} input[aria-label="Filter parameters"]`, "sql_mode");
    const input = `${configView} #parameter-value-sql_mode`;
    await page.waitForSelector(`${input} .set-value-as-text`, { visible: true });
    await page.click(`${input} .set-value-as-text`);
    await page.waitForSelector(`${input}.variable-input`, { visible: true });
    await page.click(`${input}.variable-input`, { count: 3 });
    await page.keyboard.press("Backspace");
    const members = config.parameters.items.find((p) => p.name === "sql_mode").values;
    const value = [...members, "PORTAL_TEST_MEMBER"].join(",");
    await page.type(`${input}.variable-input`, value);
    await clickButton("Choose from list", `${configView} .set-value `);
    await until((selector, expected) => document.querySelector(`${selector} .set-value-summary`)?.textContent === expected, input, value);
    await page.click(`${input} .set-value-button`);
    await page.waitForSelector(".set-value-panel .hint", { visible: true });
    assert.match(await page.$eval(".set-value-panel .hint", (el) => el.textContent), /PORTAL_TEST_MEMBER/);
    assert.equal(await page.$eval(".set-value-panel", (el) => el.parentElement === document.body), true, "members picker uses the body portal");
  }, { frame: ".set-value-panel" });
  await page.keyboard.press("Escape");
  await until(() => !document.querySelector(".set-value-panel"));

  await view("schema-diagram-label", async () => {
    // schema-diagram.mjs's read envelope with mermaid-export.mjs's dollar
    // identifiers. The local export endpoint only converts this model to
    // Mermaid; it never connects to a database or requests a remote asset.
    extra["/api/schema-diagram/read"] = ok({
      schema_version: "schema-diagram-1.0", warnings: [],
      tables: [
        { id: "dollars", schema: "", name: "$$x$$", columns: [
          { name: "$$column$$", data_type: "int", nullable: false, primary_key: true },
        ], indexes: [{ name: "PRIMARY", columns: ["$$column$$"], unique: true, primary: true }] },
        { id: "child", schema: "", name: "child", columns: [
          { name: "parent_id", data_type: "int", nullable: true, primary_key: false },
        ], indexes: [] },
      ],
      relationships: [{ id: "dollar-fk", name: "$$relationship$$", from_table: "child",
        from_columns: ["parent_id"], to_table: "dollars", to_columns: ["$$column$$"] }],
    });
    await h.view("diagram");
    await page.waitForSelector('select[aria-label="Database"] option[value="grid_lab"]');
    await page.select('select[aria-label="Database"]', "grid_lab");
    await clickButton("Load grid_lab", '.server-view[data-view="diagram"] ');
    await page.waitForSelector(".diagram-table", { visible: true });
    await clickButton("Mermaid", '.server-view[data-view="diagram"] ');
    await page.waitForSelector(".diagram-mermaid-preview svg", { visible: true });
    await until(() => ["$$x$$", "$$column$$", "$$relationship$$"]
      .every((label) => document.querySelector(".diagram-mermaid-preview svg")?.textContent.includes(label)));
    assert.equal(await page.$(".diagram-mermaid-preview .katex, .diagram-mermaid-preview math"), null, "dollar labels render as text");
  }, { frame: ".diagram-mermaid-preview" });

  assert.deepEqual(h.errors, [], "page errors");
  assert.deepEqual(h.state.blocked, [], "requests outside the local server (blocked)");
  await fs.writeFile(path.join(out, "manifest.json"), JSON.stringify(manifest, null, 2) + "\n");
  console.log("wrote", path.join(out, "manifest.json"), `(${manifest.length} screenshots)`);
} finally {
  await h.close();
}
