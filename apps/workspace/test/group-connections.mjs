// Group connections: a connection group opens as one Group connection from
// the connection picker (an explicit open, probe_members: true), the top bar
// and the status bar name it, and one group editor sheet (reachable from the
// picker, the status bar and the Connections place) renames a group, adds and
// removes members and deletes the group while its saved connections stay. A
// save that removes the active member moves the Group connection to the
// member the backend picks.
// The real workspace server on a temporary store (vault backend, throwaway
// HOME); /api/connect and /api/profiles/* are mocked from a mutable profiles
// list. Cluster reads and reviewed actions are mocked too: a member's catalog
// route must survive review and confirmation, without matching its address.
// Other server views are refused by the harness. Run after `npm run build`:
// node test/group-connections.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { load, open, pause } from "./perf-harness.mjs";
import { chooseConnection, closeAccountSheet, layoutMatrix, openAccountSheet, openConnectionPicker } from "./chrome-helpers.mjs";
import { connectionAction } from "./connection-helpers.mjs";

import { setEditorText } from "./editor-helpers.mjs";

const base = {
  port: 3306,
  user: "dba",
  database: "",
  ssl_mode: "PREFERRED",
  ssl_ca: "",
  read_only: false,
  remember_password: true,
  has_saved_password: true,
  secret_backend: "vault",
  secret_backend_label: "myflames encrypted vault",
};
const profiles = [
  { ...base, id: "p-ic-1", name: "cluster-ic-1", host: "10.0.0.11", type: "production", group: "mf-ic" },
  { ...base, id: "p-ic-2", name: "cluster-ic-2", host: "10.0.0.12", type: "development", group: "mf-ic" },
  { ...base, id: "p-ic-3", name: "cluster-ic-3", host: "10.0.0.13", type: "production", group: "mf-ic" },
  { ...base, id: "p-solo", name: "solo-db", host: "10.0.0.99", type: "development", group: "" },
];
// The member the stored topology identity marks primary (connection_groups.group_primary).
let primaryId = "p-ic-2";
const connectCalls = [];
const saveCalls = [];
const deleteCalls = [];
// Server view reads ({profile, group} per /api/live/health request) and editor
// runs (the full request body per /api/editor/run), for the member picker checks.
const healthCalls = [], healthRequests = [];
let expectedActivePassword = "";
const editorRuns = [];
const editorOpens = [];
let endedStatusMember = "";
let profileListReads = 0;
let holdGroupOpen = false;
let releaseGroupOpen = null;
const variableApplies = [], captureStarts = [], replicationCalls = [], catalogCalls = [];
const readinessCalls = [];
const loopStatusReads = [], loopStops = [], testCalls = [];
/** The loops this myflames holds, by endpoint (host:port:user), like SkipLoopRegistry. */
const loopRegistry = new Map();
const loopEndpoint = (c) => `${c?.host}:${c?.port}:${c?.user}`;
let exerciseEditor = false;
let connectDelay = 0;
// When set, the /api/connect mock makes this member active even when the
// re-open asks for another (a backend that reports a different active member).
let connectActiveMember = "";
let ambiguousRoutes = false;
let runSequence = 0;
// Every request that carries a connection sends its connection fields only:
// the saved profile's group metadata (group, group_generation, and anything
// else stored) never leaves the profile list.
function noGroupMeta(body, label) {
  assert.ok(body && body.connection, `${label}: the request carries a connection`);
  assert.equal(body.connection.group, undefined, `${label}: the request never carries the saved group`);
  assert.equal(body.connection.group_generation, undefined, `${label}: the request never carries the stored group generation`);
}
const editorSessions = new Map();
const configFixture = await load("parameters-mysql.json");
const variableFixture = await load("variable-mysql-8.4-global.json");
const replicationFixture = await load("replication-mysql-8.4-healthy.json");
const loopRunning = await load("replication-loop-running.json");
const loopStopped = await load("replication-loop-stopped.json");

function editorSession(body) {
  if (!editorSessions.has(body.tab_id)) editorSessions.set(body.tab_id, 100 + editorSessions.size);
  return {
    schema_version: "editor-session-1.0", tab_id: body.tab_id, state: "idle", connection: body.connection,
    engine: "mysql", server_version: "8.4.3", connection_id: editorSessions.get(body.tab_id),
    autocommit: true, in_transaction: false, transaction_source: "performance_schema", transaction_note: null,
    sql_mode: "STRICT_TRANS_TABLES", backslash_escapes: true, safe_updates: true, safe_updates_default: true,
    read_only_transactions: false, started_at: "2026-11-17T12:00:00Z", idle_seconds: 0, idle_timeout_seconds: 1800,
    ended: false, ended_reason: null, message: null, notes: [], run: null, database_name: body.connection.database || "",
  };
}
function editorReply(body) {
  const session = editorSession(body);
  const write = /^UPDATE\b/i.test(body.sql);
  if (write && !body.confirmed && !body.decline) return { status: "needs_confirmation", session, confirmation: {
    token: `member-write-${body.tab_id}`, expires_in: 300, title: "Production connection", text: "Review this change.",
    tone: "primary", kind: "write", category: "dml", reasons: [], warnings: [], in_transaction: false,
    statements: [body.sql], count: 1, connection_type: "production", connection: body.connection,
  } };
  if (body.decline) return { status: "declined", session };
  const limit = body.limit || 1000;
  const statement = { index: 0, text: body.sql, start: 0, end: body.sql.length, verb: write ? "UPDATE" : "SELECT",
    kind: write ? "write" : "read", category: write ? "dml" : "read", status: "ok", duration_ms: 1,
    ...(body.explain_format ? { sent: `EXPLAIN ${body.sql}` } : {}) };
  const result = write ? { type: "ok", statement: 0, affected_rows: 1, info: null, warnings: 0, duration_ms: 1 }
    : { type: "rows", statement: 0, columns: [{ name: body.explain_format ? "select_type" : "n", hex: false, type: null }],
      columns_known: true, rows: [[body.explain_format ? "SIMPLE" : "1"]], row_count: 1,
      truncated: !body.explain_format && limit === 1000, limit, stopped: null, label: null, server_rows: null, warnings: 0, duration_ms: 1 };
  return { status: "complete", session, run: {
    schema_version: "editor-run-1.0", id: `member-run-${++runSequence}`, kind: "run", state: "complete", scope: "all",
    started_at: "2026-11-17T12:00:00Z", finished_at: "2026-11-17T12:00:01Z", duration_ms: 1, limit,
    statements: [statement], results: [result], error: null, stopped: null, message: null,
    counts: { statements: 1, run: 1, rows: write ? 0 : 1, affected_rows: write ? 1 : 0, warnings: 0, errors: 0 },
    audit_id: null, history_id: null,
  } };
}
const healthFixture = await load("health-mysql-8.4.json");
const clusterFixture = await load("cluster-ic-8.4-healthy.json");
const clusterCalls = [];
const clusterPlans = [];
const clusterSends = [];
const planReplies = [];
const clusterProfileIds = ["p-ic-1", "p-ic-2", "p-ic-3"];
const clone = (value) => JSON.parse(JSON.stringify(value));
const stopTemplate = clusterFixture.members[0].actions.find((a) => a.id === "stop_member");
const startTemplate = clusterFixture.members[0].actions.find((a) => a.id === "start_member");

/** Synthetic Group catalog: db-2's topology address deliberately matches an
 * unrelated saved connection, while its confirmed route is p-ic-2. Once db-3
 * joins the group, its address suggestion needs identity confirmation in Review. */
function clusterReport(connection) {
  const report = clone(clusterFixture);
  report.kind = "group_replication";
  report.metadata = null;
  report.routers = [];
  report.actions = [];
  report.findings = [];
  report.connection_group = null;
  report.captured_at = new Date().toISOString();
  report.members.forEach((m, i) => {
    const profile = profiles.find((p) => p.id === clusterProfileIds[i]);
    m.name = `db-${i + 1}`;
    m.label = m.name;
    m.host = i === 1 ? "10.0.0.99" : profile.host;
    m.address = `${m.host}:${m.port}`;
    m.endpoint = m.address;
    m.local = profile.id === connection.profile_id;
    m.role = i === 0 ? "PRIMARY" : "SECONDARY";
    const grouped = !!connection.group_name;
    const routed = grouped && profile.group === connection.group_name;
    const target = { self: m.local, member_name: m.name,
      ...(grouped ? { profile_id: routed ? profile.id : null } : {}),
      ...(routed && i === 2 ? { confirm: true,
        note: `Review checks that ${profile.name} (${profile.host}:${profile.port}) is this member.` } : {}) };
    const missing = `Add a saved connection for ${m.name} to this group.`;
    const reason = grouped ? (routed ? null : missing)
      : m.local ? null : "Stop Group Replication runs on the server it stops.";
    m.actions = [
      { ...clone(stopTemplate), member_key: m.key, member_name: m.name, member_uuid: m.uuid,
        enabled: !reason, why_not: reason, fix: reason, target },
      { ...clone(startTemplate), member_key: m.key, member_name: m.name, member_uuid: m.uuid,
        enabled: false, why_not: grouped && !routed ? missing : "This member is not OFFLINE.",
        fix: grouped && !routed ? missing : "Stop Group Replication on this member before starting it again.", target },
    ];
    if (ambiguousRoutes && i === 1) for (const action of m.actions) {
      action.enabled = false;
      action.target = { ...target, profile_id: null };
      action.why_not = "db-2 and another saved connection both reach this member.";
      action.fix = "Connect to db-2 to act on this member, or keep only one of them in the group.";
    }
    // A reason that names a saved connection outside the group: no Use link.
    if (ambiguousRoutes && i === 2) for (const action of m.actions) {
      action.enabled = false;
      action.target = { ...target, profile_id: null };
      action.why_not = "db-3 and another saved connection both reach this member.";
      action.fix = "Connect to solo-db to act on this member, or keep only one of them in the group.";
    }
  });
  const local = report.members.find((m) => m.local);
  report.local = local.key;
  report.server = { hostname: local.host, port: local.port, report_host: local.host, server_uuid: local.uuid };
  return report;
}

function stopPlan(body) {
  const report = clusterReport(body.connection);
  const member = report.members.find((m) => m.key === body.member_key);
  const profile = profiles.find((p) => p.id === (body.target_profile_id || body.connection.profile_id));
  const target = `${profile.name} (${profile.host}:${profile.port})`;
  return {
    schema_version: "cluster-action-1.1", action: "stop_member", member_key: member.key,
    eyebrow: "STOP GROUP_REPLICATION", title: `Stop Group Replication on ${target}?`,
    confirm_label: "Stop Group Replication", description: "This member leaves the group and becomes read-only (super_read_only ON).",
    target_text: target, server_name: target, statements: ["/*myflames:cluster_op*/ STOP GROUP_REPLICATION;"],
    facts: [["Runs on", target], ["Listed in the cluster as", member.name]], warnings: [],
    typed_guard: member.role === "PRIMARY" ? profile.name : null,
    undo: "START GROUP_REPLICATION;", next: null, expect: { kind: report.kind, member: { key: member.key, uuid: member.uuid } },
    policy: { action: "confirm", tone: "danger", title: "Review cluster change", text: "Confirm the reviewed change." },
    review_token: `fixture-stop-${planReplies.length + 1}`,
  };
}

function groupList() {
  const byName = new Map();
  for (const p of profiles) {
    const g = (p.group || "").trim();
    if (!g) continue;
    if (!byName.has(g)) byName.set(g, []);
    byName.get(g).push(p);
  }
  return [...byName.entries()]
    .sort(([a], [b]) => a.toLowerCase().localeCompare(b.toLowerCase()) || a.localeCompare(b))
    .map(([name, members]) => ({
      name,
      profile_ids: members.map((m) => m.id),
      count: members.length,
      type: members.some((m) => m.type !== "development") ? "production" : "development",
      read_only: members.every((m) => m.read_only === true),
    }));
}

const h = await open("/api/unused-group-connections", "group-connections", {
  "/api/profiles/list": () => {
    profileListReads++;
    return { status: 200, body: {
      items: profiles,
      secret_store: { backend: "vault", label: "myflames encrypted vault", available: true },
    } };
  },
  "/api/connect": (body, request) => {
    connectCalls.push(body);
    const name = typeof body.group_name === "string" ? body.group_name.trim() : "";
    const members = profiles.filter((p) => (p.group || "") === name);
    if (!members.length) return { status: 400, body: { error: "No saved connection belongs to this connection group." } };
    let active = null;
    if (body.active_member_profile_id) {
      active = members.find((m) => m.id === body.active_member_profile_id) || null;
      if (!active) return { status: 400, body: { error: "That saved connection is not a member of this connection group." } };
    }
    const primary = members.find((m) => m.id === primaryId) || null;
    if (!active) active = primary || members[0];
    if (connectActiveMember) active = members.find((m) => m.id === connectActiveMember) || active;
    const type = members.some((m) => m.type !== "development") ? "production" : "development";
    const response = {
      status: 200,
      delay: connectDelay,
      body: {
        version: "8.4.3",
        engine: "mysql",
        flavor: "mysql",
        product: "MySQL",
        databases: ["shop"],
        schemas: ["shop"],
        group_connection: {
          group_name: name,
          effective_safety: { type, read_only: !!active.read_only },
          active_member_profile_id: active.id,
          primary_profile_id: primary ? primary.id : null,
          members: members.map((m) => ({
            profile_id: m.id,
            profile_name: m.name,
            host: m.host,
            port: m.port,
            safety: { type: m.type, read_only: !!m.read_only },
            server_uuid: null,
            role: m.id === primaryId ? "PRIMARY" : "SECONDARY",
            state: null,
            reachable: m.id === active.id ? true : body.probe_members === true ? true : null,
          })),
        },
      },
    };
    // Hold the already-built reply while a profile refresh changes its active
    // member's settings. This exercises openGroup's post-await profile lookup
    // without depending on the speed of either browser or network.
    if (holdGroupOpen) {
      releaseGroupOpen = () => request.respond({ status: response.status,
        contentType: "application/json", body: JSON.stringify(response.body) });
      return null;
    }
    return response;
  },
  "/api/profiles/groups/save": (body) => {
    saveCalls.push(body);
    const name = (body.name || "").trim();
    const previous = (body.previous_name || "").trim() || name;
    const ids = new Set(body.profile_ids || []);
    if (!name || !ids.size) return { status: 400, body: { error: "Enter a connection group name." } };
    for (const p of profiles) {
      if (ids.has(p.id)) p.group = name;
      else if ((p.group || "") === previous) p.group = "";
    }
    return { status: 200, body: { group: name, name, profile_ids: [...ids], updated: [...ids], groups: groupList(), profiles } };
  },
  "/api/profiles/groups/delete": (body) => {
    deleteCalls.push(body);
    for (const p of profiles) if ((p.group || "") === body.name) p.group = "";
    return { status: 200, body: { deleted: true, group: body.name, name: body.name, updated: [], groups: groupList(), profiles } };
  },
  // The Health view's read, recorded by the member its connection names: a
  // member switch or an automatic refresh must only ever log in to the shown
  // member (the backend re-checks the group name and membership at login).
  "/api/live/health": (body) => {
    assert.equal(body.connection.password, body.connection.profile_id === primaryId ? expectedActivePassword : "",
      "only the active member keeps its own typed password");
    noGroupMeta(body, "Health");
    healthRequests.push(body);
    healthCalls.push({ profile: body.connection?.profile_id || "", group: body.connection?.group_name || "" });
    return { status: 200, body: healthFixture };
  },
  // The picker's readiness probe: the connection fields only, never the saved
  // profile (group, group_generation, has_saved_password stay on the list).
  "/api/live/readiness": (body) => {
    noGroupMeta(body, "Readiness");
    assert.equal(body.connection.has_saved_password, undefined, "the readiness probe never sends password metadata");
    readinessCalls.push(body);
    return { status: 200, body: {
      engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.3",
      read_only: false, super_read_only: false, performance_schema: true, replica: false,
      group_member: null, galera_member: null, privileges: {}, actions: {}, notes: [],
      checked_at: "2026-11-17T12:00:00Z",
    } };
  },
  // Editor runs never reach a server here: refused, but recorded so a tab's
  // member binding shows in the request (its profile id and the group name).
  "/api/editor/run": (body) => {
    assert.equal(body.connection.password, "", "editor member runs never carry another member's typed password");
    noGroupMeta(body, "Editor run");
    editorRuns.push(body);
    return { status: 200, body: exerciseEditor ? editorReply(body) : { status: "refused", message: "Runs are mocked in this suite." } };
  },
  "/api/editor/status": (body) => {
    noGroupMeta(body, "Editor status");
    if (body.connection.profile_id === endedStatusMember) return { status: 200, body: {
      status: "session_ended", session: { ...editorSession(body), ended: true, state: "ended", ended_reason: "idle_timeout" }, run: null,
    } };
    return { status: 200, body: { status: "none", session: null, run: null } };
  },
  "/api/editor/open": (body) => {
    noGroupMeta(body, "Editor open");
    editorOpens.push(body);
    return { status: 200, body: { status: "complete", session: editorSession(body), run: null } };
  },
  "/api/editor/close": (body) => {
    noGroupMeta(body, "Editor close");
    return { status: 200, body: { closed: true, session: null } };
  },
  "/api/editor/preview": (body) => {
    noGroupMeta(body, "Editor preview");
    return { status: 200, body: { status: "ok", statements: [],
      aggregate: { state: "allowed", missing: [], warning: "", reason: "" } } };
  },
  "/api/live/schemas": (body) => {
    noGroupMeta(body, "Schema catalog");
    catalogCalls.push(body);
    return { status: 200, body: { schemas: [{ name: "shop", tables: 1, system: false }] } };
  },
  "/api/live/config": (body) => {
    noGroupMeta(body, "Config");
    return { status: 200, body: configFixture };
  },
  "/api/live/variable": (body) => {
    noGroupMeta(body, "Variable");
    return { status: 200, body: variableFixture.variable };
  },
  "/api/live/variable/plan": (body) => {
    noGroupMeta(body, "Variable plan");
    const plan = clone(variableFixture.plan);
    plan.requested = plan.value = plan.display = body.value;
    plan.mode = body.mode;
    plan.statement = `SET GLOBAL max_connections = ${body.value}`;
    plan.expect.statement = plan.statement;
    return { status: 200, body: plan };
  },
  "/api/live/variable/apply": (body) => {
    noGroupMeta(body, "Variable apply");
    variableApplies.push(body);
    return { status: 200, body: variableFixture.change };
  },
  "/api/live/replication": (body) => {
    noGroupMeta(body, "Replication");
    replicationCalls.push(body);
    return { status: 200, body: replicationFixture };
  },
  // Loop status reads no database: it lists the loops this myflames holds for
  // the connection's endpoint. Stop cancels a loop already authorized, so it
  // stays enabled; it must go to the connection the loop runs on.
  "/api/live/replication/skip-loop/status": (body) => {
    noGroupMeta(body, "Skip loop status");
    loopStatusReads.push(body);
    return { status: 200, body: { schema_version: "replication-skip-loop-1.0", loops: loopRegistry.get(loopEndpoint(body.connection)) || [] } };
  },
  "/api/live/replication/skip-loop/stop": (body) => {
    assert.equal(body.connection.password, "", "Skip loop stop never carries another member's typed password");
    noGroupMeta(body, "Skip loop stop");
    loopStops.push(body);
    loopRegistry.set(loopEndpoint(body.connection), [clone(loopStopped)]);
    return { status: 200, body: clone(loopStopped) };
  },
  // Test connection from the form: the connection fields only, never the
  // saved profile's group metadata.
  "/api/connection/test": (body) => {
    noGroupMeta(body, "Test connection");
    testCalls.push(body);
    return { status: 200, body: { version: "8.4.3", schemas: ["shop"], databases: ["shop"], databases_truncated: false } };
  },
  "/api/live/start": (body) => {
    noGroupMeta(body, "Capture start");
    captureStarts.push(body);
    return { status: 400, body: { error: "Mock capture stopped after verifying its destination." } };
  },
  "/api/live/cluster": (body) => {
    assert.equal(body.connection.password, "", "Cluster member reads keep the request password empty");
    noGroupMeta(body, "Cluster read");
    clusterCalls.push(body);
    return { status: 200, body: clusterReport(body.connection) };
  },
  "/api/live/cluster/plan": (body) => {
    assert.equal(body.connection.password, "", "Cluster plans never carry another member's typed password");
    noGroupMeta(body, "Cluster plan");
    clusterPlans.push(body);
    const plan = stopPlan(body);
    planReplies.push(plan);
    return { status: 200, body: plan };
  },
  "/api/live/cluster/action": (body) => {
    assert.equal(body.connection.password, "", "Cluster actions never carry another member's typed password");
    noGroupMeta(body, "Cluster action");
    clusterSends.push(body);
    return { status: 200, body: {
      schema_version: "cluster-action-1.1", action: "stop_member", status: "done", message: "Group Replication stopped.",
      statements: body.plan.statements, sent: body.plan.statements, not_sent: [], error: null,
      after: null, undo: "START GROUP_REPLICATION;", next: null, audit_id: "mock-cluster-stop", states: ["done"],
    } };
  },
  "/api/live/action/progress": () => ({ status: 200, body: {
    schema_version: "action-progress-1.0", phase: "checking", states: ["waiting"], status: null,
    elapsed_s: 0, waiting_s: 0, timeout_s: 120,
  } }),
});
const { page } = h;
await fs.mkdir(h.shots, { recursive: true });
// Node-side request logs (healthCalls, editorRuns, ...) are polled on exactly
// the state each step asserts, with a budget well over the Health view's 5 s
// auto-refresh cadence, so a loaded machine cannot outrun a fixed pause loop.
const waitFor = async (check, message) => {
  for (let i = 0; i < 300; i++) { if (check()) return; await pause(100); }
  assert.fail(message);
};
const pillText = () => page.$eval(".window-toolbar .connection-pill", (e) => e.textContent.replace(/\s+/g, " ").trim());
const statusText = () => page.$eval(".window-statusbar", (e) => e.textContent.replace(/\s+/g, " ").trim());
// Use the public return-to-window path to see edits from another window.
// Advance only the saved-list throttle, not the privilege refresh TTL. The app
// throttles reloads to once a second measured from the last reload's own
// (possibly shifted) Date.now(), so each call must advance past the previous
// shift: a fixed offset would leave the throttle's clock behind.
let focusReturnOffset = 0;
const refreshProfileList = async () => {
  const before = profileListReads;
  focusReturnOffset += 2_000;
  const offset = focusReturnOffset;
  await page.evaluate((shift) => {
    const now = Date.now;
    Date.now = () => now() + shift;
    try { window.dispatchEvent(new Event("focus")); } finally { Date.now = now; }
  }, offset);
  await waitFor(() => profileListReads > before, "returning to the window did not refresh saved connections");
};
const assertNoTabConnection = async () => {
  await page.waitForFunction(() => document.querySelector(".window-statusbar")?.textContent.includes("No connection"));
  const text = await statusText();
  assert.doesNotMatch(text, /Group connection|10\.0\.0\./, "an orphaned tab's status never borrows the window's server");
  const before = h.state.privilegeReads.length;
  await openConnectionPicker(page);
  assert.equal(await page.$(".picker-panel .picker-account"), null, "no account from the window's connection is shown for an orphaned tab");
  assert.match(await page.$eval(".picker-panel .picker-note", (e) => e.textContent), /This tab has no open connection\. Account and privileges are unavailable\./);
  await openAccountSheet(page);
  assert.match(await page.$eval("dialog.account-sheet .sheet-lead", (e) => e.textContent), /No connection in use/);
  assert.doesNotMatch(await page.$eval("dialog.account-sheet", (e) => e.textContent), /Connected as/);
  assert.equal(await page.$("dialog.account-sheet .sheet-actions button:not(.primary)"), null, "there is no privilege refresh for an unavailable member");
  assert.equal(h.state.privilegeReads.length, before, "opening account details never reads privileges through a fallback connection");
  await closeAccountSheet(page);
};
const sheet = "dialog.group-editor-sheet[open]";
const setName = (value) => page.evaluate((sel, v) => {
  const input = document.querySelector(`${sel} input[aria-label="Group name"]`);
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  setter.call(input, v);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}, sheet, value);
const ticked = () => page.$$eval(`${sheet} .group-editor-item`, (els) =>
  els.map((li) => ({ id: li.dataset.profileId, checked: li.querySelector('input[type="checkbox"]').checked })));
const tick = (id) => page.click(`${sheet} .group-editor-item[data-profile-id="${id}"] input[type="checkbox"]`);

try {
  await page.reload({ waitUntil: "networkidle0" });
  // The picker lives on a connection place (the Query editor), never on Home.
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".window-toolbar .connection-pill", { visible: true });

  // --- The picker lists groups beside saved connections, with the strictest pill.
  await openConnectionPicker(page);
  await page.waitForSelector(".picker-panel .group-connections-section", { visible: true });
  const rowText = await page.$eval('.picker-panel .group-connection-row[data-group="mf-ic"]', (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(rowText, /mf-ic\s*· 3 servers/, "the group row names the group and its server count");
  const pill = await page.$eval('.picker-panel .group-connection-row[data-group="mf-ic"] .type-tag', (e) => e.textContent.trim());
  assert.equal(pill, "Production", "any Production member makes the group's pill Production");
  await page.keyboard.press("Escape");

  // --- Open the group: one /api/connect with probe_members (an explicit open).
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection mf-ic"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-ic"));
  assert.equal(connectCalls.length, 1, "one connect call for the open");
  assert.deepEqual(connectCalls[0], { group_name: "mf-ic", probe_members: true });
  assert.match(await pillText(), /Group connection mf-ic · 3 servers/);
  const status1 = await statusText();
  assert.match(status1, /Group connection mf-ic · 3 servers/, "the status bar names the Group connection");
  assert.match(status1, /Production/, "the status bar shows the effective (strictest) safety");
  await page.waitForSelector(".window-statusbar .statusbar-group-edit", { visible: true });

  // The backend picked the primary (p-ic-2) as the active member.
  await openConnectionPicker(page);
  const activeNote = await page.$eval(".picker-panel .picker-note", (e) => e.textContent.trim());
  assert.match(activeNote, /cluster-ic-2 is the active member/);
  const chosen = await page.$eval('.picker-panel select[aria-label="Connection"]', (e) => e.value);
  assert.equal(chosen, "group-connection", "the group has its own picker value, distinct from the active member");
  await page.keyboard.press("Escape");

  // --- Single-server places on a Group connection: the Server: control ------
  // The Server place's Health view names the member it reads and starts on
  // the primary (the backend's active member).
  await h.server();
  await page.waitForSelector('.server-view[data-view="health"] .group-server-picker', { visible: true });
  const headerName = () => page.$eval('.server-view[data-view="health"] .view-server-name', (e) => e.textContent.trim());
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("cluster-ic-2"));
  assert.equal(await headerName(), "cluster-ic-2 · 10.0.0.12:3306 · PRIMARY", "the view header names its server");
  // Three members at 1440 px: segmented buttons, the primary pressed.
  await page.waitForSelector('.server-view[data-view="health"] .group-server-segmented', { visible: true });
  assert.deepEqual(
    await page.$$eval('.server-view[data-view="health"] .group-server-segmented button', (els) => els.map((e) => e.textContent)),
    ["cluster-ic-1", "cluster-ic-2", "cluster-ic-3"],
    "the picker lists the group's members by profile name",
  );
  assert.ok(await page.$('.server-view[data-view="health"] .group-server-segmented button[data-value="p-ic-2"][aria-pressed="true"]'),
    "the picker starts on the primary");
  // Health read the primary, with the group name for the membership re-check.
  await waitFor(() => healthCalls.length >= 1, "Health read on open");
  assert.deepEqual(healthCalls[0], { profile: "p-ic-2", group: "mf-ic" });
  // At 720 px the control is a select of the same members.
  await page.setViewport({ width: 720, height: 900 });
  await h.view("health");
  await page.waitForSelector('.server-view[data-view="health"] .group-server-select', { visible: true });
  assert.deepEqual(
    await page.$$eval('.server-view[data-view="health"] .group-server-select option', (els) => els.map((e) => e.textContent)),
    ["cluster-ic-1 · 10.0.0.11:3306 · SECONDARY", "cluster-ic-2 · 10.0.0.12:3306 · PRIMARY", "cluster-ic-3 · 10.0.0.13:3306 · SECONDARY"],
    "the narrow picker names role and address",
  );
  assert.equal(await page.$eval('.server-view[data-view="health"] .group-server-select', (e) => e.value), "p-ic-2");
  assert.equal(await page.$('.server-view[data-view="health"] .view-server-name'), null,
    "the compact header renders the member label once, inside the select");
  await page.setViewport({ width: 1440, height: 900 });
  await page.waitForSelector('.server-view[data-view="health"] .group-server-segmented', { visible: true });

  // --- Switching the picker: an explicit login to that member; automatic
  // refresh (While visible, the default) only ever logs in to the shown member.
  await page.click('.server-view[data-view="health"] .group-server-segmented button[data-value="p-ic-1"]');
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("cluster-ic-1"));
  assert.equal(await headerName(), "cluster-ic-1 · 10.0.0.11:3306 · SECONDARY", "the header follows the chosen member");
  await openConnectionPicker(page);
  assert.match(await page.$eval(".picker-panel .picker-note", (e) => e.textContent), /Account and privileges shown for cluster-ic-1\./,
    "the picker names the member whose grants it shows, rather than the window's active member");
  await openAccountSheet(page);
  assert.equal(await page.$eval("dialog.account-sheet .sheet-lead", (e) => e.textContent), "cluster-ic-1",
    "the account sheet names Server's selected member");
  await closeAccountSheet(page);
  const sinceSwitch = healthCalls.length;
  // The previous member's report stays, greyed, until an explicit Refresh:
  // that read is the login to the chosen member.
  await h.refresh();
  await waitFor(() => healthCalls.slice(sinceSwitch).some((c) => c.profile === "p-ic-1"),
    "refreshing after the switch reads the chosen member");
  assert.equal(connectCalls.length, 1, "switching the picker is no group re-open; the view read is the login");
  // One more automatic read lands while the view stays visible (5 s cadence).
  const beforeAuto = healthCalls.length;
  await waitFor(() => healthCalls.length > beforeAuto, "automatic refresh read again while visible");
  assert.ok(healthCalls.slice(sinceSwitch).every((c) => c.profile === "p-ic-1" && c.group === "mf-ic"),
    "every read since the switch names the shown member and the group");
  assert.ok(!healthCalls.some((c) => c.profile === "p-ic-3"), "no read ever logged in to another member");
  await layoutMatrix(page, h.shots, "group-connections-server");
  await page.setViewport({ width: 1440, height: 900 });
  // Back to the primary for the removed-member checks below.
  await h.view("health");
  await page.click('.server-view[data-view="health"] .group-server-segmented button[data-value="p-ic-2"]');
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("cluster-ic-2"));

  // --- Query editor tabs bind the Server: control's member. ----------------
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".editor-connection .group-server-picker", { visible: true });
  assert.ok(await page.$('.editor-connection .group-server-segmented button[data-value="p-ic-2"][aria-pressed="true"]'),
    "the editor's Server: control starts on the primary");
  await page.click(".query-tab-add");
  await page.waitForFunction(() => [...document.querySelectorAll(".query-tab .query-tab-label")].some((e) => e.textContent.includes("Query 2 · cluster-ic-2")));
  // The picker moves; the tab keeps its member.
  await page.click('.editor-connection .group-server-segmented button[data-value="p-ic-3"]');
  await page.waitForSelector('.editor-connection .group-server-segmented button[data-value="p-ic-3"][aria-pressed="true"]');
  await page.click(".query-tab-add");
  await page.waitForFunction(() => [...document.querySelectorAll(".query-tab .query-tab-label")].some((e) => e.textContent.includes("Query 3 · cluster-ic-3")));
  const tabLabels = () => page.$$eval(".query-tab .query-tab-label", (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()));
  assert.deepEqual(await tabLabels(), ["Query 1", "Query 2 · cluster-ic-2", "Query 3 · cluster-ic-3"],
    "each new tab binds the member the control named when it was created");
  // Running in a tab uses its member's connection: the session request carries
  // that member's profile id and the group name.
  await page.click("#query-tab-1");
  await page.click('.editor-toolbar [data-action="run"]');
  await waitFor(() => editorRuns.length === 1, "the run request was sent");
  assert.equal(editorRuns.length, 1, "the run request was sent");
  assert.equal(editorRuns[0].connection.profile_id, "p-ic-2", "the tab runs on its own member");
  assert.equal(editorRuns[0].connection.group_name, "mf-ic", "the request names the group for the membership re-check");
  assert.equal(editorRuns[0].connection.host, "10.0.0.12");

  // --- Edit group… from the picker: rename and add a member.
  await openConnectionPicker(page);
  await page.click('.picker-panel button[aria-label="Edit group mf-ic"]');
  await page.waitForSelector(sheet, { visible: true });
  assert.equal(await page.$eval(`${sheet} input[aria-label="Group name"]`, (e) => e.value), "mf-ic");
  assert.deepEqual(await ticked(), [
    { id: "p-ic-1", checked: true },
    { id: "p-ic-2", checked: true },
    { id: "p-ic-3", checked: true },
    { id: "p-solo", checked: false },
  ]);
  // Each member row shows its type and address.
  const memberRow = await page.$eval(`${sheet} .group-editor-item[data-profile-id="p-ic-2"]`, (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(memberRow, /cluster-ic-2/);
  assert.match(memberRow, /Development/);
  assert.match(memberRow, /dba@10\.0\.0\.12:3306/);
  await setName("mf-icx");
  await tick("p-solo");
  await page.click(`${sheet} .sheet-buttons button.primary`);
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  assert.equal(saveCalls.length, 1);
  assert.equal(saveCalls[0].name, "mf-icx");
  assert.equal(saveCalls[0].previous_name, "mf-ic");
  assert.deepEqual([...saveCalls[0].profile_ids].sort(), ["p-ic-1", "p-ic-2", "p-ic-3", "p-solo"]);
  // The Group connection followed the rename at once (a re-open, no probe).
  assert.equal(connectCalls.length, 2);
  assert.deepEqual(connectCalls[1], { group_name: "mf-icx", active_member_profile_id: "p-ic-2" });
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-icx · 4 servers"));

  // --- Edit group… from the status bar: remove the active member, it moves to the primary.
  await page.click(".window-statusbar .statusbar-group-edit");
  await page.waitForSelector(sheet, { visible: true });
  assert.match(await page.$eval(`${sheet} h2`, (e) => e.textContent), /Edit group mf-icx/);
  await tick("p-ic-2");
  await page.click(`${sheet} .sheet-buttons button.primary`);
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  assert.equal(saveCalls.length, 2);
  assert.deepEqual([...saveCalls[1].profile_ids].sort(), ["p-ic-1", "p-ic-3", "p-solo"]);
  // The active member left the group: the Group connection re-opened on the
  // member the backend picked (the mock's new primary) and says so.
  assert.equal(connectCalls.length, 3);
  assert.deepEqual(connectCalls[2], { group_name: "mf-icx" });
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-icx · 3 servers"));
  await openConnectionPicker(page);
  assert.match(await page.$eval(".picker-panel .picker-note", (e) => e.textContent.trim()), /moved to cluster-ic-1/);
  assert.equal(await page.$eval('.picker-panel select[aria-label="Connection"]', (e) => e.value), "group-connection");
  // Layout of the open picker with its Group connections section.
  await layoutMatrix(page, h.shots, "group-connections-picker", { frame: ".picker-panel" });
  await page.setViewport({ width: 1440, height: 900 });
  await page.keyboard.press("Escape");

  // --- A removed member: views that were on it moved to the primary and say
  // so; its tabs say "not in this group any more" and refuse to run until the
  // user picks a member.
  await h.server();
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("cluster-ic-1"));
  assert.equal(await headerName(), "cluster-ic-1 · 10.0.0.11:3306 · SECONDARY", "views on the removed member moved to the primary");
  assert.match(
    await page.$eval('.server-view[data-view="health"] .group-server-note', (e) => e.textContent),
    /cluster-ic-2 is not in this group any more; its views moved to cluster-ic-1\./,
  );
  // The tab bound to the removed member says so and refuses to run.
  await page.click('.main-nav button[data-place="query"]');
  await page.click("#query-tab-1");
  await page.waitForSelector(".group-member-note", { visible: true });
  assert.match(await page.$eval(".group-member-note", (e) => e.textContent.replace(/\s+/g, " ")),
    /cluster-ic-2 is not in this group any more\. Pick a server for this tab to run it\./);
  assert.ok(await page.$(".query-tab-member.is-gone"), "the tab's member is marked gone");
  assert.match(await page.$eval(".query-tab-member.is-gone", (e) => e.textContent), /cluster-ic-2/);
  await assertNoTabConnection();
  const runsBeforeRefusal = editorRuns.length;
  await page.click('.editor-toolbar [data-action="run"]');
  await page.waitForFunction(() => document.body.textContent.includes("Pick a server for this tab"));
  assert.equal(editorRuns.length, runsBeforeRefusal, "a removed member's tab sends nothing");
  // Picking a member rebinds the tab; it runs on that member.
  await page.select(".group-member-rebind", "p-ic-3");
  await page.waitForFunction(() => !document.querySelector(".group-member-note"));
  assert.ok((await tabLabels()).includes("Query 2 · cluster-ic-3"), "the tab names the member it was moved to");
  await page.click('.editor-toolbar [data-action="run"]');
  await waitFor(() => editorRuns.length > runsBeforeRefusal, "the rebound tab runs again");
  assert.equal(editorRuns.length, runsBeforeRefusal + 1, "the rebound tab runs again");
  assert.equal(editorRuns[editorRuns.length - 1].connection.profile_id, "p-ic-3");
  assert.equal(editorRuns[editorRuns.length - 1].connection.group_name, "mf-icx");

  // --- Edit group… from the Connections place (Home).
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector('.group-connections-section[data-testid="home-group-connections"]', { visible: true });
  const homeRow = await page.$eval('.group-connections-section[data-testid="home-group-connections"] .group-connection-row[data-group="mf-icx"]', (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(homeRow, /mf-icx\s*· 3 servers/);
  await page.click('.group-connections-section[data-testid="home-group-connections"] button[aria-label="Edit group mf-icx"]');
  await page.waitForSelector(sheet, { visible: true });
  await layoutMatrix(page, h.shots, "group-connections-editor", { frame: sheet });
  await page.setViewport({ width: 1440, height: 900 });

  // --- Test connection from the form sends the connection fields only: the
  // saved profile's group metadata never leaves the list (noGroupMeta in the
  // mock asserts group and group_generation are absent).
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  await connectionAction(page, "cluster-ic-2", "Edit");
  await page.waitForSelector(".connection-panel", { visible: true });
  const beforeTest = testCalls.length;
  await page.evaluate(() => [...document.querySelectorAll(".connection-panel button")]
    .find((b) => b.textContent.trim() === "Test connection").click());
  await waitFor(() => testCalls.length > beforeTest, "Test connection was not sent");
  assert.equal(testCalls.at(-1).connection.profile_id, "p-ic-2");
  assert.equal(testCalls.at(-1).connection.host, "10.0.0.12");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#connection-form-title"));
  // Back to the group editor for the delete below.
  await page.click('.group-connections-section[data-testid="home-group-connections"] button[aria-label="Edit group mf-icx"]');
  await page.waitForSelector(sheet, { visible: true });

  // --- Delete the group: its saved connections stay.
  await page.click(`${sheet} .group-editor-delete`);
  await page.waitForSelector("dialog.group-editor-sheet[open] .sheet-buttons button.danger-solid", { visible: true });
  await page.waitForFunction(() => document.activeElement?.closest("dialog.group-editor-sheet") !== null);
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel", "delete confirmation puts focus on Cancel");
  const confirmText = await page.$eval("dialog.group-editor-sheet[open]", (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(confirmText, /saved connections stay/, "the delete confirmation says the saved connections stay");
  await page.click("dialog.group-editor-sheet[open] .sheet-buttons button.danger-solid");
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  assert.deepEqual(deleteCalls, [{ name: "mf-icx" }]);
  // The group is gone; every saved connection is still listed.
  await page.waitForFunction(() => !document.querySelector('.group-connections-section[data-testid="home-group-connections"]'));
  for (const id of ["p-ic-1", "p-ic-2", "p-ic-3", "p-solo"]) {
    await page.waitForSelector(`.connection-row[data-row-key="${id}"]`, { visible: true });
  }
  // The Group connection is over: the active member stays as a plain connection.
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".window-toolbar .connection-pill", { visible: true });
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("cluster-ic-1"));
  assert.ok(!(await page.$(".window-statusbar .statusbar-group-edit")), "no Edit group… without a Group connection");
  await h.server();
  await h.view("health");
  assert.equal(await page.$('.server-view[data-view="health"] .group-server-picker'), null, "deleting a group clears its member picker");
  assert.equal(await page.$('.server-view[data-view="health"] .group-server-note'), null, "deleting a group clears its removed-member notice");

  // --- Cluster member actions use the catalog's saved route. ---------------
  // Start a fresh Group connection with db-1 active and db-3 not yet in the
  // group. The topology calls db-2 by an address belonging to p-solo, so any
  // attempt to infer its action route from the address fails these assertions.
  for (let i = 0; i < clusterProfileIds.length; i++) {
    const profile = profiles.find((p) => p.id === clusterProfileIds[i]);
    profile.name = `db-${i + 1}`;
    profile.group = i < 2 ? "mf-ic" : "";
    // The active db-1 is Development, but db-2 makes the Group connection
    // Production. Confirmation must retain that freshly checked group type.
    profile.type = i === 1 ? "production" : "development";
  }
  primaryId = "p-ic-1";
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection mf-ic"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-ic · 2 servers"));
  await page.click('.main-nav button[data-place="cluster"]');
  const clusterView = '.server-view[data-view="cluster"]';
  const review = "dialog.action-review[open]";
  const db1Key = clusterFixture.members[0].key;
  const db2Key = clusterFixture.members[1].key;
  const db3Key = clusterFixture.members[2].key;
  const groupRequest = {
    name: "Group connection mf-ic · 2 servers", host: "10.0.0.11", port: 3306, user: "dba", database: "",
    ssl_mode: "PREFERRED", ssl_ca: "", read_only: false, password: "", type: "production",
    profile_id: "p-ic-1", group_name: "mf-ic",
  };
  const clickStop = () => page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Stop Group Replication…").click());
  const closeReview = async () => {
    await page.evaluate(() => [...document.querySelectorAll("dialog.action-review[open] .sheet-actions button")]
      .find((b) => b.textContent.trim() === "Close").click());
    await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  };
  const assertSend = (body, connection, plan, typed, route = {}) => {
    const { review_token, ...reviewed } = plan;
    assert.match(body.progress_id, /^[0-9a-f]{32}$/);
    assert.deepEqual(body, {
      connection, plan: reviewed, review_token, typed, confirmed: true, progress_id: body.progress_id, ...route,
    }, "the exact action body preserves the review and its saved route");
  };
  await page.waitForSelector(`${clusterView} .cluster-card`, { visible: true });
  const identity = await page.$eval(`${clusterView} .cluster-identity`, (e) => e.textContent);
  assert.match(identity, /Each member's actions run through its own saved connection in mf-ic\./);
  assert.doesNotMatch(identity, /act on this server only/);
  // A card's Use suggestion names only the group's own members: db-2's
  // topology address belongs to solo-db, outside the group, and db-3 is not
  // in the group yet, so neither card suggests a switch.
  const cardUse = (key) => page.$$eval(`${clusterView} .cluster-node`, (nodes, k) => {
    const node = nodes.find((n) => n.querySelector(`.cluster-card[data-row-key="${k}"]`));
    const button = node ? node.querySelector(".cluster-use-node") : null;
    return button ? button.textContent.trim() : null;
  }, key);
  assert.equal(await cardUse(db2Key), null, "db-2's address matches solo-db, which is outside the group");
  assert.equal(await cardUse(db3Key), null, "db-3 is not in the group yet");
  await layoutMatrix(page, h.shots, "group-cluster-actions", { frame: clusterView });
  await page.setViewport({ width: 1440, height: 900 });

  await h.select(`${clusterView} .cluster-card[data-row-key="${db2Key}"]`);
  const plansBeforeStop = clusterPlans.length;
  await clickStop();
  await page.waitForSelector(review, { visible: true });
  assert.equal(clusterPlans.length, plansBeforeStop + 1);
  assert.deepEqual(clusterPlans.at(-1), {
    connection: groupRequest, action: "stop_member", member_key: db2Key, target_profile_id: "p-ic-2",
  }, "planning uses db-2's catalog profile, never the matching address or the active db-1");
  assert.equal(await page.$eval(`${review} .ops-target strong`, (e) => e.textContent), "db-2 (10.0.0.12:3306)");
  assert.match(await page.$eval(review, (e) => e.textContent), /Listed in the cluster as\s*db-2/);
  await layoutMatrix(page, h.shots, "group-cluster-review", { frame: review });
  await page.setViewport({ width: 1440, height: 900 });
  const sendsBeforeStop = clusterSends.length;
  await page.click(`${review} .sheet-actions button:last-child`);
  await page.waitForSelector(`${review} .action-outcome[data-status="done"]`);
  assert.equal(clusterSends.length, sendsBeforeStop + 1);
  assertSend(clusterSends.at(-1), groupRequest, planReplies.at(-1), "", { target_profile_id: "p-ic-2" });
  await closeReview();

  // A member with no saved route keeps the exact reason, stays focusable,
  // sends nothing, and opens the same group editor used by the picker.
  await h.select(`${clusterView} .cluster-card[data-row-key="${db3Key}"]`);
  const missingStop = await page.$$eval(".view-inspector .review-actions > button", (buttons) => {
    const button = buttons.find((b) => b.textContent.trim() === "Stop Group Replication…");
    return { disabled: button.getAttribute("aria-disabled"), tab: button.tabIndex,
      reason: document.getElementById(button.getAttribute("aria-describedby"))?.querySelector("p")?.textContent };
  });
  assert.deepEqual(missingStop, { disabled: "true", tab: 0, reason: "Add a saved connection for db-3 to this group." });
  const beforeDisabled = clusterPlans.length;
  await clickStop();
  assert.equal(clusterPlans.length, beforeDisabled, "a disabled missing-route action does not plan");
  assert.equal(await page.$(review), null);
  await layoutMatrix(page, h.shots, "group-cluster-missing-route", { frame: ".view-inspector" });
  await page.setViewport({ width: 1440, height: 900 });
  await page.click(".view-inspector .cluster-edit-group");
  await page.waitForSelector(sheet, { visible: true });
  assert.equal((await page.$$(sheet)).length, 1, "there is one shared editor sheet");
  assert.equal(await page.$eval(`${sheet} input[aria-label="Group name"]`, (e) => e.value), "mf-ic");
  await layoutMatrix(page, h.shots, "group-cluster-edit-group", { frame: sheet });
  await page.setViewport({ width: 1440, height: 900 });
  await tick("p-ic-3");
  const readsBeforeSave = clusterCalls.length;
  await page.click(`${sheet} .sheet-buttons button.primary`);
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .some((b) => b.textContent.trim() === "Stop Group Replication…" && b.getAttribute("aria-disabled") === "false"));
  assert.ok(clusterCalls.length > readsBeforeSave, "saving the group reads the Cluster place again");
  assert.equal(clusterCalls.at(-1).connection.group_name, "mf-ic");
  assert.equal(clusterCalls.at(-1).connection.name, "Group connection mf-ic · 3 servers");
  assert.deepEqual(connectCalls.at(-1), { group_name: "mf-ic", active_member_profile_id: "p-ic-1" });
  assert.equal(await page.$(".view-inspector .cluster-edit-group"), null, "the fresh catalog has a saved route for db-3");
  assert.equal(await cardUse(db3Key), "Use db-3", "in the group, db-3's card suggests its own saved connection");
  assert.equal(await cardUse(db2Key), null, "db-2's card still does not suggest solo-db, outside the group");
  const suggestedStop = await page.$$eval(".view-inspector .review-actions > button", (buttons) => {
    const button = buttons.find((b) => b.textContent.trim() === "Stop Group Replication…");
    const note = document.getElementById(button.getAttribute("aria-describedby"));
    return { disabled: button.getAttribute("aria-disabled"), note: note?.querySelector("p")?.textContent,
      muted: note?.classList.contains("cluster-action-why") };
  });
  assert.deepEqual(suggestedStop, { disabled: "false",
    note: "Review checks that db-3 (10.0.0.13:3306) is this member.", muted: true },
    "an enabled address suggestion explains Review's identity check under its action");
  assert.equal(await page.$$eval(".view-inspector .cluster-action-why p", (notes) =>
    notes.filter((n) => n.textContent.startsWith("Review checks that")).length), 1,
  "the disabled Start action does not show an enabled-action note");
  await layoutMatrix(page, h.shots, "group-cluster-confirm-member", { frame: ".view-inspector" });
  await page.setViewport({ width: 1440, height: 900 });

  // A single saved connection, even one in the same saved group, keeps the
  // original request shapes and connected-member explanation.
  await chooseConnection(page, "p-ic-1");
  await h.refresh();
  await page.waitForFunction((sel) => document.querySelector(`${sel} .cluster-identity`)?.textContent.includes("Stop and Start act on this server only."), {}, clusterView);
  assert.doesNotMatch(await page.$eval(`${clusterView} .cluster-identity`, (e) => e.textContent), /Each member's actions/);
  // Without a Group connection the address match across saved connections
  // stays: db-2's card suggests solo-db, whose address the topology reports.
  assert.equal(await cardUse(db2Key), "Use solo-db", "a single connection keeps the address match across saved connections");
  await h.select(`${clusterView} .cluster-card[data-row-key="${db1Key}"]`);
  await clickStop();
  await page.waitForSelector(review, { visible: true });
  const { group_name: _group, ...singleRequest } = { ...groupRequest, name: "db-1", type: "development" };
  assert.deepEqual(clusterPlans.at(-1), { connection: singleRequest, action: "stop_member", member_key: db1Key },
    "a single saved connection sends no target_profile_id in its plan body");
  assert.equal(Object.hasOwn(clusterPlans.at(-1), "target_profile_id"), false);
  await page.type(`${review} .action-typed input`, "db-1");
  await layoutMatrix(page, h.shots, "single-cluster-review", { frame: review });
  await page.setViewport({ width: 1440, height: 900 });
  await page.click(`${review} .sheet-actions button:last-child`);
  await page.waitForSelector(`${review} .action-outcome[data-status="done"]`);
  assertSend(clusterSends.at(-1), singleRequest, planReplies.at(-1), "db-1");
  assert.equal(Object.hasOwn(clusterSends.at(-1), "target_profile_id"), false);
  await closeReview();
  // Back to the cluster list: the Cluster place stays mounted (hidden) after
  // this, and a leftover full-width detail would shadow other places'
  // inspectors (h.select's Back looks for any .view-inspector.is-detail).
  await page.click(`${clusterView} .view-inspector.is-detail .detail-back`);
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));

  // --- Regressions: every place and tab owns its destination. ------------
  exerciseEditor = true;
  const openGroup = async (name) => {
    await openConnectionPicker(page);
    await page.click(`button[aria-label="Open group connection ${name}"], button[aria-label="Refresh group connection ${name}"]`);
    await page.waitForFunction((n) => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes(`Group connection ${n} ·`), {}, name);
  };
  const chooseServer = async (id, scope = ".server-place") => {
    const select = `${scope} .group-server-select`;
    if (await page.$(select)) await page.select(select, id);
    else await page.click(`${scope} .group-server-segmented button[data-value="${id}"]`);
  };

  // Current saved profiles decide group safety without reopening the group.
  // Read-only remains the shown member's flag, not the active member's flag.
  const beforeSafetyEdits = profiles.map((p) => ({ ...p }));
  await openGroup("mf-ic");
  await h.server(); await h.view("health");
  await chooseServer("p-ic-3");
  const opensBeforeSafetyRefresh = connectCalls.length;
  for (const profile of profiles.filter((p) => p.group === "mf-ic")) profile.type = "development";
  profiles.find((p) => p.id === "p-ic-1").read_only = true;
  await refreshProfileList();
  await page.waitForFunction(() => {
    const pill = document.querySelector(".window-toolbar .connection-pill")?.textContent || "";
    return pill.includes("Development") && pill.includes("Read-only");
  });
  await page.waitForFunction(() => {
    const status = document.querySelector(".window-statusbar")?.textContent || "";
    return status.includes("Development") && !status.includes("Read-only");
  });
  const beforeFreshMemberRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforeFreshMemberRead, "the member did not read with the current group safety");
  assert.equal(healthRequests.at(-1).connection.profile_id, "p-ic-3");
  assert.equal(healthRequests.at(-1).connection.type, "development", "memberRequest uses the current strictest type");
  assert.equal(healthRequests.at(-1).connection.read_only, false, "a read-only active member does not relabel the writable member");
  await chooseServer("p-ic-1");
  await page.waitForFunction(() => document.querySelector(".window-statusbar")?.textContent.includes("Read-only"));
  const beforeFreshActiveRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforeFreshActiveRead, "the active member did not read with its current policy");
  assert.equal(healthRequests.at(-1).connection.read_only, true, "the active member's read-only flag follows profile edits");
  profiles.find((p) => p.id === "p-ic-3").read_only = true;
  await refreshProfileList();
  await chooseServer("p-ic-3");
  await page.waitForFunction(() => document.querySelector(".window-statusbar")?.textContent.includes("Read-only"));

  // Include a newly grouped Production profile even though it was not in the
  // snapshot returned by the earlier group open.
  Object.assign(profiles.find((p) => p.id === "p-solo"), { type: "production", group: "mf-ic" });
  await refreshProfileList();
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Production"));
  const beforeStricterRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforeStricterRead, "the member did not adopt the stricter current group type");
  assert.equal(healthRequests.at(-1).connection.type, "production", "a newly grouped Production member updates the strictest type");
  assert.equal(healthRequests.at(-1).connection.read_only, true, "a non-active member's read-only flag follows profile edits");
  assert.equal(connectCalls.length, opensBeforeSafetyRefresh, "profile edits do not reopen or probe the group");
  for (const original of beforeSafetyEdits) Object.assign(profiles.find((p) => p.id === original.id), original);
  await refreshProfileList();
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Read-only"));

  profiles.find((p) => p.id === "p-ic-1").read_only = true;
  await openGroup("mf-ic");
  await h.server();
  await h.view("config");
  await chooseServer("p-ic-3");
  await h.refresh();
  const parameterRow = '.server-view[data-view="config"] tr[data-row-key="max_connections"]';
  await page.waitForSelector(parameterRow, { visible: true });
  await h.select(parameterRow);
  await page.click('.view-inspector button[data-change="max_connections"]');
  const variableSheet = 'dialog.variable-sheet[open]';
  await page.waitForSelector(`${variableSheet} #variable-value-max_connections`, { visible: true });
  await page.click(`${variableSheet} #variable-value-max_connections`, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.type(`${variableSheet} #variable-value-max_connections`, "300");
  await page.waitForFunction((sel) => document.querySelector(`${sel} .variable-statement`)?.textContent === "SET GLOBAL max_connections = 300;" &&
    !document.querySelector(`${sel} .sheet-actions button:last-of-type`)?.disabled, {}, variableSheet);
  const beforeVariable = variableApplies.length;
  await page.click(`${variableSheet} .sheet-actions button:last-of-type`);
  await waitFor(() => variableApplies.length > beforeVariable, "confirmed parameter write on the chosen member was not sent");
  assert.equal(variableApplies.at(-1).connection.profile_id, "p-ic-3");
  assert.equal(variableApplies.at(-1).connection.group_name, "mf-ic");
  assert.equal(variableApplies.at(-1).confirmed, true);
  assert.equal(variableApplies.at(-1).connection.read_only, false, "the active member's read-only flag does not block a writable member");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  // Back to the parameter list: Config stays mounted (hidden) once visited,
  // and a leftover full-width detail would shadow other places' inspectors.
  const configBack = await page.$('.server-view[data-view="config"] .view-inspector.is-detail .detail-back');
  if (configBack) {
    await configBack.click();
    await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  }

  profiles.find((p) => p.id === "p-ic-1").read_only = false;
  await openGroup("mf-ic");

  // Refresh all keeps shared topology reports on the window's active db-1.
  await page.click('.main-nav button[data-place="cluster"]');
  await page.waitForSelector(`${clusterView} .cluster-card`, { visible: true });
  await h.refresh();
  await page.waitForSelector('.main-nav button[data-place="cluster"] .nav-marker');
  const clusterBadge = () => page.$eval('.main-nav button[data-place="cluster"]', (e) => e.textContent);
  const beforeBadge = await clusterBadge();
  assert.ok(await page.$('.main-nav button[data-place="cluster"] .nav-marker'), "Cluster has a badge before Refresh all");
  await h.server();
  await h.view("health");
  const beforeClusterRefresh = clusterCalls.length, beforeReplicationRefresh = replicationCalls.length;
  await page.click('.server-view[data-view="health"] button[aria-label="Refresh options"]');
  await page.click('[data-action="refresh-all"]');
  await waitFor(() => clusterCalls.length > beforeClusterRefresh && replicationCalls.length > beforeReplicationRefresh,
    "Refresh all did not read both topology reports");
  await page.waitForFunction(() => !document.querySelector(".refresh-all-stop"));
  assert.equal(clusterCalls.at(-1).connection.profile_id, "p-ic-1", "Cluster stays on the window connection");
  assert.equal(replicationCalls.at(-1).connection.profile_id, "p-ic-1", "Replication stays on the window connection");
  assert.equal(healthCalls.at(-1).profile, "p-ic-3", "per-view reads stay on Server's chosen member");
  assert.equal(await clusterBadge(), beforeBadge, "Refresh all preserves the shared Cluster badge");
  assert.ok(await page.$('.main-nav button[data-place="cluster"] .nav-marker'));
  await page.click('.main-nav button[data-place="cluster"]');
  assert.equal(await page.$('.server-view[data-view="cluster"] .other-connection'), null, "the shared Cluster report is current");

  // An ambiguous action reason names its saved route, regardless of address.
  ambiguousRoutes = true;
  await h.refresh();
  await h.select(`${clusterView} .cluster-card[data-row-key="${db2Key}"]`);
  const useText = await page.$$eval('.view-inspector .cluster-action-why .cluster-use-node', (buttons) => buttons.map((b) => b.textContent.trim()));
  assert.ok(useText.length > 0);
  assert.ok(useText.every((text) => text === "Use db-2"), "action links name the route from the reason, never solo-db's matching address");
  // A reason naming a saved connection outside the group offers no Use link;
  // Edit group… stays, the route being the group's to fix.
  await h.select(`${clusterView} .cluster-card[data-row-key="${db3Key}"]`);
  assert.match(await page.$eval(".view-inspector", (e) => e.textContent), /Connect to solo-db to act on this member/);
  assert.equal(await page.$$eval(".view-inspector .cluster-action-why .cluster-use-node", (b) => b.length), 0,
    "solo-db is not a member of mf-ic: no Use link names it");
  assert.ok(await page.$(".view-inspector .cluster-edit-group"), "Edit group… stays offered for the unrouted member");
  ambiguousRoutes = false;

  // --- A skip loop running on the active member keeps its banner, named by
  // the server it runs on, whichever member the Server: control shows; Stop
  // from the banner goes to the loop's own connection.
  loopRegistry.set("10.0.0.11:3306:dba", [clone(loopRunning)]);
  assert.ok(await page.$('.main-nav button[data-place="replication"] .nav-marker'),
    "the sidebar badge names the window connection's report before any loop");
  await page.click('.main-nav button[data-place="replication"]');
  await page.waitForFunction(() => [...document.querySelectorAll('.server-view[data-view="replication"] .ops-loop')]
    .some((e) => e.offsetParent && e.dataset.loop === "running"));
  await h.server();
  await h.view("health");
  await chooseServer("p-ic-1");
  await page.waitForSelector(".server-content > .ops-loop[data-loop=running]", { visible: true });
  assert.equal(await page.$eval(".server-content > .ops-loop", (e) =>
    [...e.querySelectorAll(".ops-last")].some((s) => s.textContent.startsWith("on "))), false,
    "on the loop's own member the banner names no other server");
  await chooseServer("p-ic-2");
  await page.waitForFunction(() => [...document.querySelectorAll(".server-content > .ops-loop .ops-last")]
    .some((e) => e.textContent === "on db-1 · 10.0.0.11:3306 · PRIMARY"));
  assert.ok(await page.$('.main-nav button[data-place="replication"] .nav-marker'),
    "the sidebar badge stays while Server shows another member");
  const beforeStop = loopStops.length;
  await page.click(".server-content > .ops-loop button");
  await waitFor(() => loopStops.length > beforeStop, "Stop from the banner was not sent");
  assert.equal(loopStops.at(-1).connection.profile_id, "p-ic-1", "Stop goes to the member the loop runs on, not the member on screen");
  assert.equal(loopStops.at(-1).connection.group_name, "mf-ic");
  assert.equal(loopStops.at(-1).connection.host, "10.0.0.11");
  await page.waitForFunction(() => !document.querySelector(".server-content > .ops-loop[data-loop=running]"));

  // Member-bound SQL writes confirm on db-3 even after the picker moves.
  await page.click('.main-nav button[data-place="query"]');
  await chooseServer("p-ic-3", ".editor-connection");
  endedStatusMember = "p-ic-3";
  await page.click(".query-tab-add");
  await page.waitForFunction(() => document.querySelector('.query-tab [aria-selected="true"] .query-tab-label')?.textContent.includes("db-3"));
  const boundTabId = await page.$eval('.query-tab [aria-selected="true"]', (e) => e.id);
  await page.waitForSelector('.editor-banner[data-banner="ended"]', { visible: true });
  endedStatusMember = "";
  const beforeReconnectReads = h.state.privilegeReads.length;
  const beforeReconnectOpens = editorOpens.length;
  await page.click('.editor-banner[data-banner="ended"] button');
  await waitFor(() => editorOpens.length > beforeReconnectOpens, "the member tab did not reconnect");
  await waitFor(() => h.state.privilegeReads.length > beforeReconnectReads, "reconnecting a member tab did not refresh its privileges");
  assert.equal(editorOpens.at(-1).connection.profile_id, "p-ic-3");
  assert.ok(h.state.privilegeReads.slice(beforeReconnectReads).every((call) => call.connection.profile_id === "p-ic-3"),
    "a successful member reconnect refreshes that tab's grants, even though the window is on db-1");
  await page.waitForFunction(() => !document.querySelector('.editor-banner[data-banner="ended"]'));
  await chooseServer("p-ic-2", ".editor-connection");
  await openConnectionPicker(page);
  assert.match(await page.$eval(".picker-panel .picker-note", (e) => e.textContent), /Account and privileges shown for db-3\./,
    "the picker identifies the tab's bound member, independent of both Server: and the window connection");
  await openAccountSheet(page);
  assert.equal(await page.$eval("dialog.account-sheet .sheet-lead", (e) => e.textContent), "db-3");
  await closeAccountSheet(page);
  await setEditorText(page, "UPDATE shop.t SET n = 2 WHERE id = 1");
  const beforeWrite = editorRuns.length;
  await page.click('.editor-toolbar [data-action="run"]');
  await page.waitForSelector('dialog.editor-confirm[open]', { visible: true });
  await page.click('dialog.editor-confirm[open] .sheet-actions button:last-of-type');
  await waitFor(() => editorRuns.slice(beforeWrite).some((body) => body.confirmed), "confirmed member-tab write was not sent");
  const confirmed = editorRuns.slice(beforeWrite).find((body) => body.confirmed);
  assert.equal(confirmed.connection.profile_id, "p-ic-3");
  assert.equal(confirmed.connection.group_name, "mf-ic");
  assert.equal(confirmed.confirm_token, `member-write-${confirmed.tab_id}`);
  await page.waitForFunction(() => !document.querySelector('dialog.editor-confirm[open]'));

  await setEditorText(page, "SELECT n FROM shop.t");
  await page.click('.editor-toolbar [data-action="run"]');
  await page.waitForSelector('.result-toolbar button ::-p-text(Load more)', { visible: true });
  assert.equal(await page.$(".query-lab .result-from"), null, "fresh member results are not labelled as another connection");
  const beforeMore = editorRuns.length;
  await page.locator('.result-toolbar button ::-p-text(Load more)').click();
  await waitFor(() => editorRuns.length > beforeMore, "Load more did not send on the member tab");
  assert.equal(editorRuns.at(-1).connection.profile_id, "p-ic-3");
  assert.ok(editorRuns.at(-1).limit > 1000);
  await page.waitForFunction(() => !document.querySelector('.editor-toolbar [data-action="run"]')?.disabled);
  await page.click('.editor-toolbar [data-action="explain"]');
  await page.waitForSelector('.results-tabs-actions button ::-p-text(Explore)', { visible: true });
  const beforeExplore = captureStarts.length;
  await page.locator('.results-tabs-actions button ::-p-text(Explore)').click();
  await waitFor(() => captureStarts.length > beforeExplore, "Explore did not start for the member's Explain result");
  assert.equal(captureStarts.at(-1).connection.profile_id, "p-ic-3");
  assert.equal(captureStarts.at(-1).connection.group_name, "mf-ic");
  assert.equal(captureStarts.at(-1).mode, "explain");
  assert.ok(catalogCalls.some((call) => call.connection.profile_id === "p-ic-3"), "the member tab reads its own schema catalog");
  assert.ok(h.state.privilegeReads.some((call) => call.connection.profile_id === "p-ic-3"), "privileges follow the tab/member");

  // Persist a member-bound tab, then restore with only the plain connection.
  const persisted = page.waitForResponse((response) => {
    if (!response.url().endsWith("/api/session/save") || response.status() !== 200) return false;
    const body = JSON.parse(response.request().postData() || "{}");
    return body.state?.query_lab?.tabs?.some((tab) => tab.member === "p-ic-3" && tab.sql === "SELECT 42");
  });
  await setEditorText(page, "SELECT 42");
  await persisted;
  const beforeRestore = editorRuns.length;
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await page.click(`#${boundTabId}`);
  await page.waitForSelector(".group-member-note", { visible: true });
  assert.match(await page.$eval(".group-member-note", (e) => e.textContent), /This tab ran on db-3, which is not open now\./);
  assert.match(await page.$eval('.query-tab [aria-selected="true"] .query-tab-member', (e) => e.textContent), /db-3/);
  await assertNoTabConnection();
  await page.click('.editor-toolbar [data-action="run"]');
  assert.equal(editorRuns.length, beforeRestore, "a restored member tab without its group never falls back to the active connection");
  await openGroup("mf-ic");
  await page.waitForFunction(() => !document.querySelector(".group-member-note"));
  await page.click('.editor-toolbar [data-action="run"]');
  await waitFor(() => editorRuns.length > beforeRestore, "opening its group did not restore the member tab");
  assert.equal(editorRuns.at(-1).connection.profile_id, "p-ic-3");

  // A typed password stays with the active member, including after a group edit.
  await h.server(); await h.view("health");
  await chooseServer("p-ic-1");
  await openConnectionPicker(page);
  await page.locator('.picker-panel button ::-p-text(Use a different password)').click();
  expectedActivePassword = "group-session-only";
  await page.type(".picker-password", expectedActivePassword);
  await page.click(".picker-password-set");
  await page.keyboard.press("Escape");
  const beforePasswordRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforePasswordRead, "the active member did not read with its typed password");
  assert.equal(healthRequests.at(-1).connection.password, expectedActivePassword);
  await page.click(".window-statusbar .statusbar-group-edit");
  await page.waitForSelector(sheet, { visible: true });
  await page.click(`${sheet} .sheet-buttons button.primary`);
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  await openConnectionPicker(page);
  assert.equal(await page.$eval(".picker-password", (e) => e.value), expectedActivePassword, "saving a group keeps the active member's typed password");
  await page.keyboard.press("Escape");
  await chooseServer("p-ic-3");
  const beforePeerRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforePeerRead, "the peer did not read");
  assert.equal(healthRequests.at(-1).connection.password, "", "another member never inherits the active member's password");
  await openConnectionPicker(page);
  expectedActivePassword = "";
  await page.locator('.picker-panel button ::-p-text(Use saved password)').click();
  await page.keyboard.press("Escape");

  // A group save whose re-open reports another active member clears the typed
  // password: the password belongs to the member it was typed for, and the
  // next request on the new active member carries none.
  await openConnectionPicker(page);
  await page.locator('.picker-panel button ::-p-text(Use a different password)').click();
  expectedActivePassword = "group-moved-away";
  await page.type(".picker-password", expectedActivePassword);
  await page.click(".picker-password-set");
  await page.keyboard.press("Escape");
  connectActiveMember = "p-ic-3";
  await page.click(".window-statusbar .statusbar-group-edit");
  await page.waitForSelector(sheet, { visible: true });
  await page.click(`${sheet} .sheet-buttons button.primary`);
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  connectActiveMember = "";
  await page.waitForFunction(() => document.querySelector(".window-toolbar .picker-target")?.textContent.includes("10.0.0.13"),
    "the Group connection did not move to the active member the save reported");
  const beforeMovedRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforeMovedRead, "the reported active member did not read");
  assert.equal(healthRequests.at(-1).connection.profile_id, "p-ic-3", "the read went to the reported active member");
  assert.equal(healthRequests.at(-1).connection.password, "",
    "a group save that reports another active member clears the typed password");
  expectedActivePassword = "";

  // Five members always use a select, even at 1440 px, with one header label.
  profiles.push(...[4, 5].map((n) => ({ ...base, id: `p-extra-${n}`, name: `db-${n}`, host: `10.0.0.${10 + n}`, type: "development", group: "mf-ic" })));
  profiles.find((p) => p.id === "p-solo").group = "stage-group";
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await openGroup("mf-ic");
  await h.server(); await h.view("health");
  await page.waitForSelector('.server-view[data-view="health"] .group-server-select', { visible: true });
  assert.equal(await page.$$eval('.server-view[data-view="health"] .group-server-select option', (options) => options.length), 5);
  assert.equal(await page.$('.server-view[data-view="health"] .group-server-segmented'), null);
  assert.equal(await page.$('.server-view[data-view="health"] .view-server-name'), null, "large groups show the member label once");
  await chooseServer("p-ic-3");
  await openGroup("stage-group");
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .group-server [data-value="p-solo"][aria-pressed="true"]'));
  assert.equal(await page.$('.server-view[data-view="health"] .group-server-note'), null, "switching groups clears the previous group's member choice and warning");
  await openGroup("mf-ic");
  assert.equal(await page.$eval('.server-view[data-view="health"] .group-server-select', (e) => e.value), "p-ic-1", "switching back starts on the group's active member");

  // Selecting the active member in the plain-connection select fires change.
  await chooseConnection(page, "p-ic-1");
  assert.doesNotMatch(await pillText(), /Group connection/);
  assert.equal(await page.$(".window-statusbar .statusbar-group-edit"), null);
  // The picker's readiness probe went out with the connection fields only
  // (noGroupMeta above refuses group metadata on every mocked endpoint).
  await waitFor(() => readinessCalls.some((call) => call.connection.profile_id === "p-ic-1"),
    "switching the connection did not probe its readiness");
  const probe = readinessCalls.at(-1).connection;
  assert.equal(probe.group, undefined, "the readiness probe never sends the saved group");
  assert.equal(probe.group_generation, undefined, "the readiness probe never sends the stored group generation");
  assert.equal(probe.has_saved_password, undefined, "the readiness probe never sends password metadata");
  // A slow group probe cannot replace the single connection chosen after it.
  connectDelay = 700;
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection mf-ic"]');
  await chooseConnection(page, "p-ic-2");
  await pause(900);
  assert.doesNotMatch(await pillText(), /Group connection/);
  assert.match(await pillText(), /db-2/);
  connectDelay = 0;

  // A group probe may finish after another window refreshes the selected
  // member's endpoint. The open must use the current profile, not the render
  // closure or the older endpoint in the held connect reply.
  holdGroupOpen = true;
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection mf-ic"]');
  await waitFor(() => !!releaseGroupOpen, "the group open was not held");
  const editedDuringOpen = profiles.find((p) => p.id === "p-ic-1");
  const beforeOpenEdit = { ...editedDuringOpen };
  Object.assign(editedDuringOpen, { name: "db-1-current", host: "10.0.0.51", user: "current-dba", database: "current_shop" });
  await refreshProfileList();
  // The Open click closed the picker; re-open it to see the refreshed list.
  await openConnectionPicker(page);
  await page.waitForFunction(() => [...document.querySelectorAll('.picker-panel select[aria-label="Connection"] option')]
    .some((option) => option.value === "p-ic-1" && option.textContent.includes("db-1-current")));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  holdGroupOpen = false;
  await releaseGroupOpen();
  releaseGroupOpen = null;
  await page.waitForFunction(() => document.querySelector(".window-toolbar .picker-target")?.textContent.includes("current-dba@10.0.0.51:3306"));
  await h.server(); await h.view("health");
  await chooseServer("p-ic-1");
  const beforeCurrentEndpointRead = healthRequests.length;
  await h.refresh();
  await waitFor(() => healthRequests.length > beforeCurrentEndpointRead, "the group did not read through the profile refreshed during its probe");
  assert.equal(healthRequests.at(-1).connection.host, "10.0.0.51");
  assert.equal(healthRequests.at(-1).connection.user, "current-dba");
  assert.equal(healthRequests.at(-1).connection.database, "current_shop");
  Object.assign(editedDuringOpen, beforeOpenEdit);
  await chooseConnection(page, "p-ic-2");

  // The group editor explains its limit, and Delete confirmation retains focus.
  profiles.push(...Array.from({ length: 12 }, (_, i) => ({ ...base, id: `p-limit-${i}`, name: `limit-${i}`, host: `10.1.0.${i + 1}`, type: "development", group: "" })));
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await openGroup("mf-ic");
  await page.click(".window-statusbar .statusbar-group-edit");
  await page.waitForSelector(sheet, { visible: true });
  for (let i = 0; i < 12; i++) await tick(`p-limit-${i}`);
  assert.equal(await page.$eval(`${sheet} .sheet-actions button:last-of-type`, (e) => e.disabled), true);
  assert.match(await page.$eval(`${sheet} [role="alert"]`, (e) => e.textContent), /at most 16 saved connections\. Uncheck 1 to save\./);
  await page.click(`${sheet} .group-editor-delete`);
  await page.waitForFunction(() => document.activeElement?.closest("dialog.group-editor-sheet") && document.activeElement.textContent === "Cancel");
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${sheet} input[aria-label="Group name"]`, { visible: true });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));

  console.log("group-connections: all checks passed");
} finally {
  await h.close();
}
