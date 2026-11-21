// The Topology place: a sidebar place of its own after Workload. On a Group
// connection its tabs (Map, Members, Parameters, Accounts, Plugins) read one
// /api/live/topology section each, only when a tab first comes on screen and
// on Refresh, never on automatic refresh. The four other connection states
// (no connection, unsaved, saved without a group, saved in a group) explain
// and route instead of reading.
// The real workspace server on a temporary store (vault backend, throwaway
// HOME); /api/connect, /api/profiles/* and /api/live/topology are mocked from
// fixtures that follow docs/schemas/topology-group-1.0.json. Other server
// views are refused by the harness. Run after `npm run build`:
// node test/topology-place.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { open, pause } from "./perf-harness.mjs";
import { chooseConnection, layoutMatrix } from "./chrome-helpers.mjs";
import { connectionAction } from "./connection-helpers.mjs";

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
  { ...base, id: "p-topo-1", name: "db-1", host: "10.0.0.11", type: "production", group: "mf-topo" },
  { ...base, id: "p-topo-2", name: "db-2", host: "10.0.0.12", type: "development", group: "mf-topo" },
  { ...base, id: "p-topo-3", name: "db-3", host: "10.0.0.13", type: "production", group: "mf-topo" },
  { ...base, id: "p-topo-4", name: "db-4", host: "10.0.0.14", type: "development", group: "mf-topo" },
  { ...base, id: "p-solo", name: "solo-db", host: "10.0.0.99", type: "development", group: "" },
];
const memberIds = ["p-topo-1", "p-topo-2", "p-topo-3", "p-topo-4"];
const connectCalls = [];
const topologyCalls = [];
let topologyDelay = 0;

function member(over) {
  return {
    profile_id: "", profile_name: "", host: "", port: 3306,
    safety: { type: "production", read_only: false },
    outcome: "read", message: "Read from this member.", elapsed_s: 0.1,
    engine: "mysql", product: "MySQL", version: "8.4.6", version_comment: "MySQL Community Server - GPL",
    server_uuid: null, server_id: null, hostname: null,
    read_only: false, read_only_value: "OFF", super_read_only: false,
    role: "standalone", state: "reachable", lag: null, behind: null, behind_reason: null, channels: [],
    ...over,
  };
}
const members = [
  member({
    profile_id: "p-topo-1", profile_name: "db-1", host: "10.0.0.11", version: "8.4.6",
    role: "primary", state: "ONLINE", super_read_only: false,
    lag: { kind: "gr_applier_queue", value: 0, message: "Transactions waiting in this member's applier queue." },
    behind: { kind: "mysql_gtid_transactions", value: 0, message: "Transactions in the primary or source sample absent from this member sample." },
  }),
  member({
    profile_id: "p-topo-2", profile_name: "db-2", host: "10.0.0.12", version: "8.4.4",
    role: "secondary", state: "ONLINE", super_read_only: true, read_only: true, read_only_value: "ON",
    lag: { kind: "gr_applier_queue", value: 3, message: "Transactions waiting in this member's applier queue." },
    behind: { kind: "mysql_gtid_transactions", value: 1204, message: "Transactions in the primary or source sample absent from this member sample." },
  }),
  member({
    profile_id: "p-topo-3", profile_name: "db-3", host: "10.0.0.13",
    outcome: "failed", message: "Access denied for user 'dba'@'10.0.0.13'.",
    engine: null, product: null, version: null, version_comment: null,
    read_only: null, read_only_value: null, super_read_only: null, role: null, state: null,
  }),
  member({
    profile_id: "p-topo-4", profile_name: "db-4", host: "10.0.0.14",
    outcome: "not_reached", message: "Not read: the time budget did not allow this member to start.",
    engine: null, product: null, version: null, version_comment: null,
    read_only: null, read_only_value: null, super_read_only: null, role: null, state: null,
  }),
];
const summary = { read: 2, failed: 1, not_reached: 1, versions: ["8.4.4", "8.4.6"], text: "2 read, 1 failed, 1 not reached." };

function report(section, extra = {}) {
  return {
    schema_version: "topology-group-1.0", section, group_name: "mf-topo",
    taken_at: "2026-11-21T12:00:00Z", elapsed_s: 0.4,
    effective_safety: { type: "production", read_only: false },
    active_member_profile_id: "p-topo-1", primary_profile_id: "p-topo-1",
    members, summary, ...extra,
  };
}
const notCompared = [
  { profile_id: "p-topo-3", reason: "Access denied for user 'dba'@'10.0.0.13'." },
  { profile_id: "p-topo-4", reason: "Not read: the time budget did not allow this member to start." },
];
const reports = {
  members: report("members"),
  parameters: report("parameters", {
    parameters: {
      rows: [
        { name: "group_replication_group_name", status: "missing", values: { "p-topo-1": "cccccccc-cccc-cccc-cccc-cccccccccccc" }, apply_type: null, dynamic: false },
        { name: "max_connections", status: "different", values: { "p-topo-1": 500, "p-topo-2": 200 }, apply_type: null, dynamic: true },
        { name: "innodb_buffer_pool_size", status: "same", values: { "p-topo-1": 134217728, "p-topo-2": 134217728 }, apply_type: null, dynamic: false },
        { name: "server_id", status: "per_server", values: { "p-topo-1": 11, "p-topo-2": 12 }, apply_type: null, dynamic: false, reason: "Server identity: each member keeps its own value." },
        { name: "audit_log_encryption", status: "missing", values: { "p-topo-1": "[redacted]", "p-topo-2": "[redacted]" }, apply_type: null, dynamic: true, reason: "A value was redacted, truncated or unavailable; it cannot be compared." },
      ],
      compared: ["p-topo-1", "p-topo-2"],
      not_compared: notCompared,
    },
  }),
  accounts: report("accounts", {
    accounts: {
      rows: [
        { account: "'app'@'%'", status: "different", present_on: ["p-topo-1", "p-topo-2"], differences: [
          { profile_id: "p-topo-2", kind: "privileges", detail: "Grants differ: UPDATE on shop.* is missing." },
        ] },
        { account: "'root'@'localhost'", status: "same", present_on: ["p-topo-1", "p-topo-2"], differences: [] },
      ],
      compared: ["p-topo-1", "p-topo-2"],
      not_compared: notCompared,
    },
  }),
  plugins: report("plugins", {
    plugins: {
      rows: [
        { name: "audit_log", kind: "plugin", status: "missing", values: { "p-topo-1": { status: "ACTIVE", version: "1.1" }, "p-topo-2": null } },
        { name: "group_replication", kind: "plugin", status: "different", values: { "p-topo-1": { status: "ACTIVE", version: "1.1.1" }, "p-topo-2": { status: "ACTIVE", version: "1.1.0" } } },
        { name: "clone", kind: "plugin", status: "same", values: { "p-topo-1": { status: "ACTIVE", version: "1.0" }, "p-topo-2": { status: "ACTIVE", version: "1.0" } } },
      ],
      compared: ["p-topo-1", "p-topo-2"],
      not_compared: notCompared,
      notes: [{ profile_id: "p-topo-1", message: "mysql.component lists components installed with INSTALL COMPONENT; components loaded from a manifest file are not listed." }],
    },
  }),
  map: report("map", {
    map: {
      nodes: [
        { id: "p-topo-1", kind: "member", profile_id: "p-topo-1", label: "db-1", host: "10.0.0.11", port: 3306, engine: "mysql", product: "MySQL", version: "8.4.6", role: "primary", state: "ONLINE", outcome: "read", server_uuid: "11111111-1111-1111-1111-111111111111", cluster_id: "gr:cccccccc-cccc-cccc-cccc-cccccccccccc" },
        { id: "p-topo-2", kind: "member", profile_id: "p-topo-2", label: "db-2", host: "10.0.0.12", port: 3306, engine: "mysql", product: "MySQL", version: "8.4.4", role: "secondary", state: "ONLINE", outcome: "read", server_uuid: "22222222-2222-2222-2222-222222222222", cluster_id: "gr:cccccccc-cccc-cccc-cccc-cccccccccccc" },
        { id: "p-topo-3", kind: "member", profile_id: "p-topo-3", label: "db-3", host: "10.0.0.13", port: 3306, engine: null, product: null, version: null, role: null, state: null, outcome: "failed", server_uuid: null, cluster_id: null },
        { id: "p-topo-4", kind: "member", profile_id: "p-topo-4", label: "db-4", host: "10.0.0.14", port: 3306, engine: null, product: null, version: null, role: null, state: null, outcome: "not_reached", server_uuid: null, cluster_id: null },
        { id: "peer:10.0.0.77:3306", kind: "peer", profile_id: null, label: "10.0.0.77:3306", host: "10.0.0.77", port: 3306, server_uuid: null, seen_by: ["p-topo-2"], reason: "Not in this group" },
      ],
      edges: [
        { from: "p-topo-1", to: "p-topo-2", kind: "replication", evidence: "server_uuid", verified: true, state: "running", label: "Replication" },
        { from: "p-topo-1", to: "peer:10.0.0.77:3306", kind: "replication", evidence: "endpoint", verified: false, state: null, label: "Replication" },
      ],
      clusters: [
        { id: "gr:cccccccc-cccc-cccc-cccc-cccccccccccc", kind: "group_replication", name: "cccccccc-cccc-cccc-cccc-cccccccccccc", members: ["p-topo-1", "p-topo-2"], seen_from: ["p-topo-1", "p-topo-2"] },
      ],
      notes: [{ profile_id: "p-topo-3", message: "Access denied for user 'dba'@'10.0.0.13'." }],
    },
  }),
};

// The rolling change endpoints (docs/schemas/topology-change-plan-1.0.json and
// topology-change-1.0.json), mocked: plan reads, apply writes, stop prevents
// the next member, progress names the member being changed.
const rollingCalls = { plan: [], apply: [], stop: [], progress: [] };
let applyReply = null;
let applyDelay = 0;
function rollingMember(over) {
  return {
    profile_id: "", profile_name: "", host: "", port: 3306, identity: null,
    status: "planned", statement: null, before: null, after: null, effect: null,
    caveats: [], privileges: null, config_line: null, reason: null, ...over,
  };
}
const ROLLING_ORDER = "Group Replication and InnoDB Cluster secondaries first, then the primary; ties use saved name order.";
function rollingPlan(membersList) {
  return {
    schema_version: "topology-change-plan-1.0", plan_id: `plan-${rollingCalls.plan.length}`, change_set_id: "cs-1",
    group_name: "mf-topo", name: "max_connections", value: "400", mode: "persist",
    order_rule: ROLLING_ORDER,
    effective_safety: { type: "production", read_only: false },
    confirmation: { required: true, typed_name: "mf-topo" },
    members: membersList,
    expect: {
      group_name: "mf-topo", group_generation: "g1", active_member_profile_id: "p-topo-1",
      active_endpoint: { host: "10.0.0.11", port: 3306, user: "dba", ssl_mode: "PREFERRED", ssl_ca: "", ssh_host: "", ssh_user: "", ssh_port: 22 },
      effective_safety: { type: "production", read_only: false }, members: [],
    },
  };
}
const plannedDb2 = () => rollingMember({
  profile_id: "p-topo-2", profile_name: "db-2", host: "10.0.0.12",
  status: "planned", statement: "SET PERSIST max_connections = 400;",
  before: { global: "200", persisted: null }, after: { global: "400", persisted: "400" },
  effect: { global: "200 to 400" },
});
const plannedDb1 = () => rollingMember({
  profile_id: "p-topo-1", profile_name: "db-1", host: "10.0.0.11",
  status: "planned", statement: "SET PERSIST max_connections = 400;",
  before: { global: "500", persisted: null }, after: { global: "400", persisted: "400" },
  effect: { global: "500 to 400" },
});
const refusedDb1 = () => rollingMember({
  profile_id: "p-topo-1", profile_name: "db-1", host: "10.0.0.11",
  status: "refused", reason: "The live member role is unavailable. Review after it can be read.",
});
// Plan replies in order: first review has a refused member, the rest are clean.
const rollingPlanQueue = [];
const partialResult = () => ({
  schema_version: "topology-change-1.0", plan_id: "plan-2", change_set_id: "cs-1",
  group_name: "mf-topo", name: "max_connections", value: "400", mode: "persist",
  status: "partial", message: "The rolling change stopped. Check each member result before reviewing another change.",
  members: [
    { ...plannedDb2(), status: "applied", message: "The reviewed value was applied.", audit_id: "a-2" },
    { ...plannedDb1(), status: "not_sent", message: "Not sent because this rolling change stopped.", audit_id: "a-1" },
  ],
  audit_ids: ["a-2", "a-1"],
});
const staleResult = () => ({
  schema_version: "topology-change-1.0", plan_id: "plan-3", change_set_id: null,
  group_name: null, name: null, value: null, mode: null,
  status: "stale", message: "The review changed or expired. Review the change again.",
  members: [], audit_ids: [],
});

const h = await open("/api/unused-topology-place", "topology-place", {
  "/api/profiles/list": () => ({ status: 200, body: {
    items: profiles,
    secret_store: { backend: "vault", label: "myflames encrypted vault", available: true },
  } }),
  "/api/connect": (body) => {
    connectCalls.push(body);
    const name = typeof body.group_name === "string" ? body.group_name.trim() : "";
    const grouped = profiles.filter((p) => (p.group || "") === name);
    if (!grouped.length) return { status: 400, body: { error: "No saved connection belongs to this connection group." } };
    const active = grouped.find((m) => m.id === body.active_member_profile_id) || grouped[0];
    return { status: 200, body: {
      version: "8.4.6",
      engine: "mysql",
      flavor: "mysql",
      product: "MySQL",
      databases: ["shop"],
      schemas: ["shop"],
      group_connection: {
        group_name: name,
        effective_safety: { type: "production", read_only: false },
        active_member_profile_id: active.id,
        primary_profile_id: "p-topo-1",
        members: grouped.map((m) => ({
          profile_id: m.id,
          profile_name: m.name,
          host: m.host,
          port: m.port,
          safety: { type: m.type, read_only: !!m.read_only },
          server_uuid: null,
          role: m.id === "p-topo-1" ? "PRIMARY" : "SECONDARY",
          state: null,
          reachable: body.probe_members === true ? true : null,
        })),
      },
    } };
  },
  // The Topology place's one endpoint: one bounded read of the whole group
  // per section, on the Group connection (its group name and active member).
  "/api/live/topology": (body) => {
    topologyCalls.push(body);
    assert.equal(body.connection.group_name, "mf-topo", "topology reads name the group");
    assert.ok(memberIds.includes(body.connection.profile_id), "topology reads name the active member");
    assert.equal(body.connection.password, "", "topology reads never carry a typed password");
    assert.equal(body.connection.group, undefined, "the request never carries the saved group metadata");
    const reply = reports[body.section];
    assert.ok(reply, `unknown topology section ${body.section}`);
    return { status: 200, body: reply, ...(topologyDelay ? { delay: topologyDelay } : {}) };
  },
  "/api/profiles/groups/save": (body) => {
    const name = (body.name || "").trim();
    const ids = new Set(body.profile_ids || []);
    for (const p of profiles) p.group = ids.has(p.id) ? name : p.group;
    return { status: 200, body: { group: name, name, profile_ids: [...ids], updated: [...ids], groups: [], profiles } };
  },
  "/api/live/topology/variable/plan": (body) => {
    rollingCalls.plan.push(body);
    assert.equal(body.connection.group_name, "mf-topo", "a rolling plan names the group");
    assert.equal(body.connection.password, undefined, "a rolling plan never carries a password");
    assert.equal(body.name, "max_connections");
    const reply = rollingPlanQueue.length
      ? rollingPlanQueue.shift()
      : rollingPlan([plannedDb2(), plannedDb1()].filter((m) => body.member_ids.includes(m.profile_id)));
    return { status: 200, body: reply };
  },
  "/api/live/topology/variable/apply": (body) => {
    rollingCalls.apply.push(body);
    assert.ok(body.plan_id && body.expect, "apply sends the reviewed plan_id and expect");
    assert.equal(body.confirmed, true);
    assert.equal(body.typed_name, "mf-topo", "a Production group applies with the typed name");
    assert.ok(body.progress_id, "apply names its progress id");
    const reply = applyReply || partialResult();
    return { status: 200, body: reply, ...(applyDelay ? { delay: applyDelay } : {}) };
  },
  "/api/live/topology/variable/stop": (body) => {
    rollingCalls.stop.push(body);
    return { status: 200, body: { progress_id: body.progress_id, status: "stopping",
      message: "Stop prevents the next member from starting. A statement already sent is not interrupted." } };
  },
  "/api/live/action/progress": (body) => {
    rollingCalls.progress.push(body);
    return { status: 200, body: { phase: "sending", states: ["sent", "waiting"], status: null,
      profile_id: "p-topo-2", profile_name: "db-2", message: "db-2: sending." } };
  },
  "/api/profiles/delete": (body) => {
    const at = profiles.findIndex((p) => p.id === body.id);
    if (at >= 0) profiles.splice(at, 1);
    return { status: 200, body: { deleted: true, id: body.id } };
  },
});
const { page } = h;
const mapShots = fileURLToPath(new URL("../../../local/topo-map/", import.meta.url));
await fs.mkdir(mapShots, { recursive: true });
await fs.mkdir(h.shots, { recursive: true });
const waitFor = async (check, message) => {
  for (let i = 0; i < 300; i++) { if (check()) return; await pause(100); }
  assert.fail(message);
};
const placeText = () => page.$eval(".topology-place", (e) => e.textContent.replace(/\s+/g, " ").trim());
const clickText = async (selector, text) => {
  const found = await page.evaluate((sel, t) => {
    const button = [...document.querySelectorAll(`${sel} button`)].find((b) => b.textContent.trim() === t);
    if (!button) return false;
    button.click();
    return true;
  }, selector, text);
  assert.ok(found, `button "${text}" under ${selector}`);
};
const topoTab = (label) => clickText(".topology-place .segmented", label);
const sections = () => topologyCalls.map((c) => c.section);

try {
  await page.reload({ waitUntil: "networkidle0" });

  // --- The sidebar item sits right after Workload.
  await page.waitForSelector('.main-nav button[data-place="topology"]', { visible: true });
  const order = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.deepEqual(order.slice(0, 5), ["home", "server", "workload", "topology", "replication"],
    "Topology sits after Workload, before Replication");
  assert.equal(await page.$eval('.main-nav button[data-place="topology"]', (e) => e.textContent.trim()), "Topology");
  const placeGlyphs = await page.$$eval('.main-nav button[data-place]', (buttons) => Object.fromEntries(buttons
    .filter((b) => ["topology", "cluster", "plans", "diagram"].includes(b.dataset.place))
    .map((b) => [b.dataset.place, b.querySelector("svg").innerHTML])));
  for (const place of ["cluster", "plans", "diagram"]) assert.notEqual(placeGlyphs.topology, placeGlyphs[place], `Topology has a glyph distinct from ${place}`);

  // --- An unsaved connection (the fresh workspace's default): save it first.
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector(".topology-place .view-empty", { visible: true });
  assert.match(await placeText(), /Save this connection to add it to a group\./);
  assert.equal(topologyCalls.length, 0, "an unsaved connection reads nothing");

  // --- A saved connection with no group: the sentence, New group... and the
  // ConnectionGroupBar states.
  await chooseConnection(page, "p-solo");
  await page.waitForSelector(".topology-place .topology-member-note", { visible: true });
  assert.match(await placeText(), /Topology reads a whole group of servers\. This connection is not in a group\./);
  assert.equal(topologyCalls.length, 0, "a saved connection without a group reads nothing");
  await clickText(".topology-place", "New group…");
  const sheet = 'dialog.group-editor-sheet[open]';
  await page.waitForSelector(sheet, { visible: true });
  assert.equal(await page.$eval(`${sheet} h2`, (e) => e.textContent), "New group");
  assert.deepEqual(
    await page.$$eval(`${sheet} .group-editor-item`, (els) => els.filter((li) => li.querySelector("input[type=checkbox]").checked).map((li) => li.dataset.profileId)),
    ["p-solo"], "New group... ticks this connection");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.group-editor-sheet"));
  // ConnectionGroupBar's own no-group line and Create a group... stay.
  assert.match(await placeText(), /No connection group: myflames reads only this connection\./);

  // --- A saved connection in a group: one sentence and the open button.
  await chooseConnection(page, "p-topo-2");
  await page.waitForSelector(".topology-place .view-empty", { visible: true });
  assert.match(await placeText(), /This connection is in group mf-topo\./);
  assert.equal(topologyCalls.length, 0, "a grouped saved connection reads nothing until opened as a Group connection");
  await clickText(".topology-place", "Open group connection mf-topo");
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection mf-topo · 4 servers"));
  assert.deepEqual(connectCalls.at(-1), { group_name: "mf-topo", probe_members: true });

  // --- A Group connection: the tabs in order, and only the tab on screen reads.
  await page.waitForFunction(() => [...document.querySelectorAll(".topology-place .segmented button")].length === 5);
  assert.deepEqual(
    await page.$$eval(".topology-place .segmented button", (els) => els.map((e) => e.textContent.trim())),
    ["Map", "Members", "Parameters", "Accounts", "Plugins"], "the tabs in their order");
  await waitFor(() => topologyCalls.length === 1, "the Map tab read on first show");
  assert.deepEqual(sections(), ["map"], "only the tab on screen reads");
  // Map: members, the "Not in this group" peer, links and the unverified one.
  await page.waitForSelector('.topology-map-list [data-node="peer:10.0.0.77:3306"]', { visible: true });
  const mapText = await placeText();
  assert.match(mapText, /Not in this group/, "a peer is named as not in this group");
  assert.match(mapText, /reported by db-2/, "the peer names its observer");
  const verifiedEdge = await page.$eval('.topology-map-list [data-edge-kind="replication"][data-verified="true"]', (e) => e.textContent);
  assert.match(verifiedEdge, /db-1 → db-2/);
  assert.match(verifiedEdge, /verified/);
  const unverifiedEdge = await page.$eval('.topology-map-list [data-verified="false"]', (e) => e.textContent);
  assert.match(unverifiedEdge, /db-1 → 10\.0\.0\.77:3306/);
  assert.match(unverifiedEdge, /address match, not verified/, "an address-only link says it is not verified");
  assert.match(mapText, /group replication · 2 members/, "the cluster box is listed");
  // The diagram keeps cluster membership, evidence and read outcomes visible.
  const diagram = ".topology-map-diagram";
  const nodeButton = (id) => `${diagram} button[data-node="${id}"]`;
  assert.deepEqual(await page.$$eval(`${diagram} .topology-map-cluster button`, (nodes) => nodes.map((n) => n.dataset.node)),
    ["p-topo-1", "p-topo-2"], "the cluster box contains its member buttons");
  assert.match(await page.$eval(`${diagram} .topology-map-cluster`, (e) => e.getAttribute("aria-label")), /Group Replication: cccccccc/);
  const peerId = "peer:10.0.0.77:3306";
  assert.equal(await page.$eval(nodeButton(peerId), (e) => getComputedStyle(e).borderTopStyle), "dashed", "a peer is drawn dashed");
  assert.match(await page.$eval(nodeButton(peerId), (e) => e.textContent), /10\.0\.0\.77:3306.*Not in this group/s);
  assert.match(await page.$eval(nodeButton("p-topo-3"), (e) => e.textContent), /Failed/);
  assert.match(await page.$eval(nodeButton("p-topo-4"), (e) => e.textContent), /Not reached/);
  assert.match(await page.$eval(`${diagram} .topology-map-edge[data-verified="false"] title`, (e) => e.textContent), /address match, not verified/);
  assert.equal(await page.$eval(`${diagram} .topology-map-edge[data-verified="false"] > path`, (e) => e.getAttribute("stroke-dasharray")), "6 5");
  assert.equal(await page.$eval(`${diagram} .topology-map-edge[data-verified="true"] > path`, (e) => e.getAttribute("stroke-dasharray")), null);
  assert.match(await page.$eval(`${diagram} .topology-map-edge[data-verified="true"] text`, (e) => e.textContent), /running/, "link state is text as well as color");
  const positions = () => page.$$eval(`${diagram} button[data-node]`, (nodes) => nodes.map((n) => ({
    id: n.dataset.node, x: n.offsetLeft + n.parentElement.offsetLeft, y: n.offsetTop + n.parentElement.offsetTop,
  })));
  const firstPositions = await positions();
  const nodeY = (id) => firstPositions.find((n) => n.id === id).y;
  assert.ok(nodeY("p-topo-1") < nodeY("p-topo-2"), "a replication source is above its replica inside a cluster");
  assert.ok(nodeY("p-topo-1") < nodeY(peerId), "a replication peer appears downstream");
  // Tab reaches every native button in visual group order. Enter uses the
  // same member inspector as Members, without sending a read.
  await page.focus(`${diagram} .topology-map-scroll`);
  const focusIds = [];
  for (let i = 0; i < firstPositions.length; i++) {
    await page.keyboard.press("Tab");
    focusIds.push(await page.evaluate(() => document.activeElement?.dataset.node));
  }
  assert.deepEqual(focusIds, await page.$$eval(`${diagram} button[data-node]`, (nodes) => nodes.map((n) => n.dataset.node)));
  await page.focus(nodeButton("p-topo-2"));
  await page.keyboard.press("Enter");
  assert.match(await h.inspector(), /db-2.*Secondary.*super_read_only/s, "Enter opens the shared member inspector");
  await page.click('.topology-place .inspector-close');
  await page.click(nodeButton("p-topo-1"));
  assert.match(await h.inspector(), /db-1.*Primary/s, "click opens the shared member inspector");
  assert.match(await h.inspector(), /address match, not verified/, "incident links explain address-only evidence in the inspector");
  await page.click('.topology-place .inspector-close');
  await page.click(nodeButton(peerId));
  const peerInspector = await h.inspector();
  assert.match(peerInspector, /Reported bydb-2/, "the peer inspector names saved-member observers");
  assert.match(peerInspector, /Not in this group: add a saved connection for 10\.0\.0\.77:3306 to this group to read it\./);
  assert.equal(await page.$('.topology-place .view-inspector .topology-member-actions'), null, "a peer has no saved-member actions");
  await page.click('.topology-place .inspector-close');
  assert.deepEqual(await positions(), firstPositions, "selection never moves nodes");
  assert.deepEqual(sections(), ["map"], "node selection never reads a member or peer");
  // No read on automatic refresh: nothing polls while the place stays open.
  await pause(1500);
  assert.deepEqual(sections(), ["map"], "no read on automatic refresh");

  // --- Members: busy with the elapsed read text, then the table with roles,
  // failed and not-reached members, and version drift in the summary.
  topologyDelay = 1200;
  await topoTab("Members");
  await page.waitForFunction(() => document.querySelector(".topology-place")?.textContent.includes("Reading 4 servers…"));
  topologyDelay = 0;
  await waitFor(() => sections().includes("members"), "the Members tab read when shown");
  await page.waitForSelector('.topology-place tr[data-row-key="p-topo-2"]', { visible: true });
  const summaryLine = await page.$eval(".topology-place .summary-line", (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(summaryLine, /2 versions: 8\.4\.4, 8\.4\.6/, "the summary names version drift");
  assert.match(summaryLine, /2 read, 1 failed, 1 not reached\./, "the summary keeps the counts");
  const rowText = (id) => page.$eval(`.topology-place tr[data-row-key="${id}"]`, (e) => e.textContent.replace(/\s+/g, " ").trim());
  assert.match(await rowText("p-topo-1"), /Primary/);
  assert.match(await rowText("p-topo-1"), /db-1/);
  assert.match(await rowText("p-topo-2"), /Secondary/);
  assert.match(await rowText("p-topo-2"), /MySQL 8\.4\.4/);
  assert.match(await rowText("p-topo-3"), /Failed · Access denied for user 'dba'@'10\.0\.0\.13'\./);
  assert.match(await rowText("p-topo-4"), /Not reached · Not read: the time budget did not allow this member to start\./);
  assert.deepEqual(sections(), ["map", "members"], "still one read per shown tab");

  // A row's inspector: facts, and Use in Server views / Open in Cluster by role.
  await h.select('.topology-place tr[data-row-key="p-topo-2"]');
  const inspector = await h.inspector();
  assert.match(inspector, /db-2/);
  assert.match(inspector, /Secondary/);
  assert.match(inspector, /super_read_only/);
  assert.match(inspector, /gr_applier_queue/);
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector button")]
    .some((b) => b.textContent.trim() === "Use in Server views"));
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector button")]
    .some((b) => b.textContent.trim() === "Open in Cluster"));

  // Refresh re-reads the tab on screen.
  const beforeRefresh = topologyCalls.length;
  await h.refresh();
  await waitFor(() => topologyCalls.length === beforeRefresh + 1, "Refresh re-read the tab on screen");
  assert.equal(topologyCalls.at(-1).section, "members", "Refresh re-reads the tab on screen");
  await pause(1200);
  assert.equal(topologyCalls.length, beforeRefresh + 1, "no automatic read follows a Refresh");

  // --- Parameters: Differences hides same and per-server rows; All shows
  // them with the Per server pill; not-compared members keep their reason.
  await topoTab("Parameters");
  await waitFor(() => sections().includes("parameters"), "the Parameters tab read when shown");
  const paramRow = (name) => page.$(`.topology-place tr[data-row-key="${name}"]`);
  await page.waitForSelector('.topology-place tr[data-row-key="max_connections"]', { visible: true });
  assert.ok(await paramRow("group_replication_group_name"), "a missing row shows under Differences");
  assert.equal(await paramRow("innodb_buffer_pool_size"), null, "Differences hides a same row");
  assert.equal(await paramRow("server_id"), null, "Differences hides a per-server row");
  const headers = await page.$$eval(".topology-place .drift-table thead th", (els) => els.map((e) => e.textContent.trim()));
  assert.ok(headers.includes("db-1") && headers.includes("db-2"), "one column per compared member");
  assert.match(await placeText(), /Not compared ?db-3: Access denied/, "not-compared members keep their reason");
  await clickText(".topology-place .drift-controls", "All");
  await page.waitForSelector('.topology-place tr[data-row-key="innodb_buffer_pool_size"]', { visible: true });
  assert.ok(await paramRow("server_id"), "All shows the per-server row");
  await page.waitForFunction(() => [...document.querySelectorAll('.topology-place tr[data-row-key="server_id"] .pill')]
    .some((e) => e.textContent.trim() === "Per server"), "the per-server row carries the pill");
  // The search box narrows the matrix.
  await page.type(".topology-place .drift-search", "max_conn");
  await page.waitForFunction(() => !document.querySelector('.topology-place tr[data-row-key="server_id"]'));
  assert.ok(await paramRow("max_connections"), "the search keeps matching rows");
  await page.click(".topology-place .drift-search", { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.waitForSelector('.topology-place tr[data-row-key="server_id"]', { visible: true });

  // --- Accounts: the row's inspector shows differences and the member link.
  await topoTab("Accounts");
  await waitFor(() => sections().includes("accounts"), "the Accounts tab read when shown");
  await h.select(".topology-place .drift-table tbody tr");
  const accountInspector = await h.inspector();
  assert.match(accountInspector, /Grants differ: UPDATE on shop\.\* is missing\./, "the inspector shows the differences");
  const openUsers = await page.$$eval(".view-inspector button", (els) => els.map((e) => e.textContent.trim()).filter((t) => t.startsWith("Open in Users and roles on ")));
  assert.deepEqual(openUsers, ["Open in Users and roles on db-1", "Open in Users and roles on db-2"],
    "each compared member offers its Users and roles");
  // The link sets the Server place's member choice and opens Users and roles.
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector button")]
    .find((b) => b.textContent.trim() === "Open in Users and roles on db-2").click());
  await page.waitForSelector('.main-nav button[data-place="server"].active', { visible: true });
  await page.waitForFunction(() => !!document.querySelector('.server-place .group-server-segmented button[data-value="p-topo-2"][aria-pressed="true"]')
    || document.querySelector(".server-place .group-server-select")?.value === "p-topo-2",
    "Users and roles opened with db-2 chosen");

  // --- Back to Map: its report is kept, no second read.
  await page.click('.main-nav button[data-place="topology"]');
  await topoTab("Map");
  await page.waitForSelector('.topology-map-list [data-node="peer:10.0.0.77:3306"]', { visible: true });
  const mapCalls = sections().filter((s) => s === "map").length;
  await pause(500);
  assert.equal(sections().filter((s) => s === "map").length, mapCalls, "a read tab never reads again on its own");

  // Sticky toolbar, no horizontal page scroll, light and dark.
  assert.equal(await page.$eval(".topbar-stack", (e) => getComputedStyle(e).position), "sticky", "the toolbar stays sticky");
  await layoutMatrix(page, mapShots, "topology-map");
  await page.setViewport({ width: 1440, height: 900 });
  await page.click(nodeButton("p-topo-2"));
  await page.waitForSelector('.topology-place .view-inspector', { visible: true });
  await layoutMatrix(page, mapShots, "topology-map-inspector");
  await page.click('.topology-place .inspector-close');
  await page.setViewport({ width: 1440, height: 900 });

  // Multiple upstream tiers, deep replicas, circular replication and the
  // 16-member limit exercise layout rather than just the small screenshot.
  const originalMap = reports.map.map;
  const originalMembers = reports.map.members;
  const makeNode = (id, extra = {}) => ({ ...originalMap.nodes[0], id, profile_id: id,
    label: id, host: `${id}.example.test`, role: "standalone", state: "reachable", cluster_id: null, ...extra });
  const extraNodes = [
    makeNode("proxy", { role: "proxy", product: "ProxySQL", version: "3.0.2" }),
    makeNode("cache", { role: "cache", product: "Readyset", version: "2026.11" }),
    makeNode("replica-1", { role: "replica" }), makeNode("replica-2", { role: "replica" }),
    makeNode("cycle-1", { role: "source", state: "connecting" }), makeNode("cycle-2", { role: "replica", state: "stopped" }),
    ...Array.from({ length: 6 }, (_, i) => makeNode(`extra-${i}`, i === 0 ? { label: '<img src=x onerror="window.topologyInjected=true">' + "Long name ".repeat(30) }
      : i === 1 ? { host: "[2001:db8::1]" } : {})),
  ];
  const link = (from, to, extra = {}) => ({ from, to, kind: "replication", evidence: "server_uuid", verified: true,
    state: "running", label: "Replication", ...extra });
  reports.map.map = {
    ...originalMap,
    nodes: [...originalMap.nodes, ...extraNodes],
    edges: [...originalMap.edges,
      link("proxy", "cache", { kind: "proxy_route", evidence: "endpoint", verified: false, state: "SHUNNED", label: "hostgroup 10" }),
      link("proxy", "cache", { kind: "proxy_route", evidence: "endpoint", verified: false, state: "ONLINE", label: "hostgroup 20" }),
      link("cache", "p-topo-1", { kind: "cache_upstream", state: "up", label: "Upstream" }),
      link("p-topo-2", "replica-1"), link("replica-1", "replica-2"),
      link("cycle-1", "cycle-2", { state: "connecting" }), link("cycle-2", "cycle-1", { state: "stopped" }),
    ],
    clusters: [originalMap.clusters[0], { id: "unconfirmed", kind: "pxc", name: "No identity confirmed", members: [], seen_from: ["p-topo-3"] }],
  };
  reports.map.members = [...members, ...extraNodes.map((node) => member({
    profile_id: node.profile_id, profile_name: node.label, host: node.host, role: node.role, state: node.state,
  }))];
  assert.equal(reports.map.members.length, 16);
  await h.refresh();
  await page.waitForSelector(nodeButton("replica-2"), { visible: true });
  const complexPositions = await positions();
  const complexY = (id) => complexPositions.find((node) => node.id === id).y;
  assert.ok(complexY("proxy") < complexY("cache"), "ProxySQL is above Readyset");
  assert.ok(complexY("cache") < complexY("p-topo-1"), "caches are above databases");
  assert.ok(complexY("p-topo-2") < complexY("replica-1") && complexY("replica-1") < complexY("replica-2"), "deepest replicas are lowest");
  assert.equal(complexY("cycle-1"), complexY("cycle-2"), "a circular replication component shares a level");
  assert.match(await page.$eval(`${diagram} .topology-map-edge[data-edge-kind="proxy_route"] text`, (e) => e.textContent), /SHUNNED/);
  const routePaths = await page.$$eval(`${diagram} .topology-map-edge[data-edge-kind="proxy_route"] > path:first-of-type`, (paths) => paths.map((p) => p.getAttribute("d")));
  assert.equal(new Set(routePaths).size, 2, "parallel hostgroup routes have separate paths");
  const routeLabels = await page.$$eval(`${diagram} .topology-map-edge[data-edge-kind="proxy_route"] text`, (labels) => labels.map((label) => label.getAttribute("y")));
  assert.equal(new Set(routeLabels).size, 2, "parallel route state labels do not overlap");
  assert.match(await page.$eval(nodeButton("extra-1"), (e) => e.textContent), /\[2001:db8::1\]:3306/, "IPv6 addresses have one pair of brackets");
  assert.match(await page.$eval(nodeButton("cycle-1"), (e) => e.textContent), /connecting/);
  assert.match(await page.$eval(`${diagram} [data-cluster="unconfirmed"]`, (e) => e.textContent), /Percona XtraDB Cluster.*No confirmed members/s);
  assert.equal(await page.$(`${diagram} img`), null, "reported labels stay text");
  assert.equal(await page.evaluate(() => window.topologyInjected), undefined, "reported labels never execute HTML");
  await h.idle(".topology-place");
  const beforeRepeatMap = topologyCalls.length;
  await h.refresh();
  await waitFor(() => topologyCalls.length === beforeRepeatMap + 1, "Map Refresh sent one read");
  await h.idle(".topology-place");
  assert.deepEqual(await positions(), complexPositions, "the same report produces the same positions on Refresh");
  await layoutMatrix(page, mapShots, "topology-map-complex");
  const upstreamVisible = await page.$eval(`${diagram} .topology-map-scroll`, (region) => {
    const bounds = region.getBoundingClientRect();
    return ["proxy", "cache"].every((id) => {
      const node = region.querySelector(`button[data-node="${id}"]`).getBoundingClientRect();
      return node.left >= bounds.left && node.right <= bounds.right;
    });
  });
  assert.ok(upstreamVisible, "small upstream tiers are visible on opening even when the database row is wide");
  const diagramScroll = await page.$eval(`${diagram} .topology-map-scroll`, (e) => {
    e.scrollLeft = e.scrollWidth;
    return { left: e.scrollLeft, overflow: getComputedStyle(e).overflowX, pageLeft: window.scrollX };
  });
  assert.ok(diagramScroll.left > 0, "wide groups scroll inside the diagram at 720 px");
  assert.equal(diagramScroll.overflow, "auto");
  assert.equal(diagramScroll.pageLeft, 0, "scrolling the diagram never moves the page sideways");

  // A peer listed in cluster evidence remains an unconsented peer.
  reports.map.map = { ...originalMap, clusters: [{ ...originalMap.clusters[0], members: ["p-topo-1", "p-topo-2", peerId] }] };
  reports.map.members = originalMembers;
  await h.refresh();
  await page.waitForFunction(() => document.querySelectorAll('.topology-map-cluster .is-peer').length === 1);
  assert.equal(await page.$eval(`${diagram} .topology-map-cluster .is-peer`, (e) => getComputedStyle(e).borderTopStyle), "dashed");

  // Empty reports do not produce invalid geometry or hide the text alternative.
  reports.map.map = { nodes: [], edges: [], clusters: [], notes: [] };
  await h.refresh();
  await page.waitForFunction(() => document.querySelector('.topology-place')?.textContent.includes("No servers were reported."));
  assert.ok(await page.$('.topology-map-section[aria-label="Servers"]'));
  reports.map.map = originalMap;
  await page.setViewport({ width: 1440, height: 900 });

  // --- Parameters: a row's inspector offers the rolling change, disabled with
  // a visible reason where it cannot run.
  await topoTab("Parameters");
  await page.waitForSelector('.topology-place tr[data-row-key="max_connections"]', { visible: true });
  await clickText(".topology-place .drift-controls", "All");
  const changeAction = () => page.$eval(".view-inspector .topology-member-actions button", (e) => ({ disabled: e.disabled, title: e.title }));
  await h.select('.topology-place tr[data-row-key="server_id"]');
  let action = await changeAction();
  assert.equal(action.disabled, true, "a per-server row cannot be changed as one");
  assert.match(await h.inspector(), /Server identity: each member keeps its own value\./);
  await h.select('.topology-place tr[data-row-key="audit_log_encryption"]');
  action = await changeAction();
  assert.equal(action.disabled, true, "a redacted row cannot be changed as one");
  assert.match(await h.inspector(), /A value was redacted, truncated or unavailable/);
  reports.parameters.effective_safety = { type: "production", read_only: true };
  await h.refresh();
  await h.select('.topology-place tr[data-row-key="max_connections"]');
  action = await changeAction();
  assert.equal(action.disabled, true, "a Read-only group connection cannot change");
  assert.match(await h.inspector(), /This Group connection is Read-only\./);
  reports.parameters.effective_safety = { type: "production", read_only: false };
  await h.refresh();

  // --- The rolling change sheet: value, mode and members, one review, apply
  // with progress, Stop, and the per-member result.
  const sheetSel = "dialog.rolling-change-sheet[open]";
  const sheetButton = async (text) => page.evaluate((t) => {
    const button = [...document.querySelectorAll("dialog.rolling-change-sheet .sheet-buttons button")]
      .find((b) => b.textContent.trim() === t);
    if (!button) return null;
    return { disabled: button.disabled };
  }, text);
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
  const sheetText = async () => page.$eval("dialog.rolling-change-sheet", (e) => e.textContent.replace(/\s+/g, " ").trim());
  const tickOf = (name) => page.evaluate((n) => {
    const label = [...document.querySelectorAll("dialog.rolling-change-sheet .rolling-members label")]
      .find((l) => l.textContent.includes(n));
    return label ? label.querySelector("input").checked : null;
  }, name);
  const setTheme = (theme) => page.evaluate((t) => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("myflames.theme", t); } catch { /* best effort */ }
  }, theme);
  const rollingShots = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "local", "topo-rolling");
  await fs.mkdir(rollingShots, { recursive: true });

  await h.select('.topology-place tr[data-row-key="max_connections"]');
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector button")]
    .find((b) => b.textContent.trim() === "Change on every member…").click());
  await page.waitForSelector(sheetSel, { visible: true });
  assert.equal(await page.$eval(`${sheetSel} .rolling-field input`, (e) => e.value), "500", "prefilled with the most common current value");
  assert.equal(await tickOf("db-1"), false, "a member already at the target starts unticked");
  assert.equal(await tickOf("db-2"), true, "a differing member starts ticked");
  assert.match(await sheetText(), /This server now \(SET GLOBAL\)/);
  assert.match(await sheetText(), /Now and after restart \(SET PERSIST\)/);
  assert.match(await sheetText(), /After restart only \(SET PERSIST_ONLY\)/);
  await page.click(`${sheetSel} .rolling-field input`, { clickCount: 3 });
  await page.type(`${sheetSel} .rolling-field input`, "400");
  assert.equal(await tickOf("db-1"), true, "editing the value reticks a member that differs from it");

  // Review: a refused member is listed with its reason and keeps Apply off.
  rollingPlanQueue.push(rollingPlan([plannedDb2(), refusedDb1()]));
  await sheetClick("Review");
  await page.waitForSelector(`${sheetSel} .rolling-review-list li`, { visible: true });
  assert.deepEqual(rollingCalls.plan[0].member_ids, ["p-topo-1", "p-topo-2"], "the review plans the ticked members");
  assert.deepEqual(await page.$$eval(`${sheetSel} .rolling-review-list li`, (els) => els.map((e) => e.dataset.member)),
    ["p-topo-2", "p-topo-1"], "the review lists the members in plan order");
  let text = await sheetText();
  assert.match(text, /SET PERSIST max_connections = 400;/, "the exact statement is shown");
  assert.match(text, /200 → 400/, "before and after are shown");
  assert.match(text, /Group Replication and InnoDB Cluster secondaries first, then the primary/, "the order rule is one sentence");
  assert.match(text, /The live member role is unavailable\./, "the refused member keeps its reason");
  assert.match(text, /Type mf-topo to confirm/, "a Production group asks for the typed name");
  assert.match(text, /Untick the refused servers and review again\./);
  assert.equal((await sheetButton("Apply")).disabled, true, "a refused member keeps Apply disabled");

  // Back, review again: the clean plan still needs the typed name.
  await sheetClick("Back");
  await page.waitForSelector(`${sheetSel} .rolling-members`, { visible: true });
  await sheetClick("Review");
  await page.waitForSelector(`${sheetSel} .rolling-review-list li[data-member="p-topo-2"]`, { visible: true });
  assert.deepEqual(await page.$$eval(`${sheetSel} .rolling-review-list li`, (els) => els.map((e) => e.dataset.member)),
    ["p-topo-2", "p-topo-1"], "the clean review keeps the plan order");
  assert.equal((await sheetButton("Apply")).disabled, true, "Apply waits for the typed name");
  await page.type(`${sheetSel} .rolling-typed input`, "mf-topo");
  assert.equal((await sheetButton("Apply")).disabled, false, "the typed group name unlocks Apply");
  await setTheme("dark");
  await page.screenshot({ path: path.join(rollingShots, "review-1440-dark.png") });

  // Apply: progress names the member, Stop answers with its sentence, and the
  // partial result lists every member.
  applyDelay = 2500;
  await sheetClick("Apply");
  await page.waitForFunction(() => document.querySelector("dialog.rolling-change-sheet")?.textContent.includes("db-2: sending."),
    { timeout: 10000 });
  assert.ok(rollingCalls.progress.length > 0, "the sheet polls the progress of the apply");
  await sheetClick("Stop");
  await waitFor(() => rollingCalls.stop.length === 1, "Stop reaches the stop endpoint");
  assert.equal(rollingCalls.stop[0].progress_id, rollingCalls.apply[0].progress_id, "Stop names the apply's progress id");
  await page.waitForFunction(() => document.querySelector("dialog.rolling-change-sheet")?.textContent
    .includes("Stop requested. No later server will start; a statement already sent is not interrupted."));
  await page.waitForSelector(`${sheetSel} .rolling-result`, { visible: true, timeout: 10000 });
  text = await sheetText();
  assert.match(text, /The rolling change stopped\. Check each member result before reviewing another change\./, "the overall status is one sentence");
  assert.match(text, /Open the Audit log/, "the result links to the Audit log");
  assert.ok(await page.$(`${sheetSel} .rolling-result-list li[data-member="p-topo-2"][data-status="applied"]`), "db-2 applied");
  assert.ok(await page.$(`${sheetSel} .rolling-result-list li[data-member="p-topo-1"][data-status="not_sent"]`), "db-1 not sent");
  assert.match(text, /Not sent because this rolling change stopped\./);
  assert.match(text, /The reviewed value was applied\./);

  // The result at 720 px: no horizontal page scroll, light and dark.
  await page.setViewport({ width: 720, height: 520 });
  await setTheme("light");
  await pause(200);
  await page.screenshot({ path: path.join(rollingShots, "result-720-light.png") });
  for (const theme of ["light", "dark"]) {
    await setTheme(theme);
    await pause(150);
    const metrics = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
    assert.ok(metrics.scroll <= metrics.client, `the rolling result sheet at 720px (${theme}) has no horizontal page scroll`);
  }
  await setTheme("dark");
  await page.setViewport({ width: 1440, height: 900 });
  await sheetClick("Close");
  await page.waitForFunction(() => !document.querySelector("dialog.rolling-change-sheet"));
  assert.match(await placeText(), /Refresh reads the group again/, "the Parameters tab offers Refresh after a change");

  // A stale review offers to review again.
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector button")]
    .find((b) => b.textContent.trim() === "Change on every member…").click());
  await page.waitForSelector(sheetSel, { visible: true });
  await sheetClick("Review");
  await page.waitForSelector(`${sheetSel} .rolling-review-list li`, { visible: true });
  await page.type(`${sheetSel} .rolling-typed input`, "mf-topo");
  applyReply = staleResult();
  applyDelay = 0;
  await sheetClick("Apply");
  await page.waitForFunction(() => document.querySelector("dialog.rolling-change-sheet")?.textContent
    .includes("The review changed or expired. Review the change again."));
  await sheetClick("Review again");
  await page.waitForSelector(`${sheetSel} .rolling-field input`, { visible: true });
  await sheetClick("Cancel");
  await page.waitForFunction(() => !document.querySelector("dialog.rolling-change-sheet"));

  // --- No connection: deleting the saved connection in use leaves an empty
  // connection, and the place shows the usual no-connection state.
  await chooseConnection(page, "p-topo-4");
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector(".connection-row", { visible: true });
  await connectionAction(page, "db-4", "Delete");
  await page.waitForSelector("dialog[open]", { visible: true });
  await page.evaluate(() => [...document.querySelectorAll("dialog[open] .sheet-buttons button")]
    .find((b) => b.textContent.trim() === "Delete connection").click());
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector(".topology-place .view-empty", { visible: true });
  await page.waitForFunction(() => document.querySelector(".topology-place")?.textContent.includes("Connect to a server to read the topology of its connection group."));

  assert.deepEqual(h.errors, [], "no page errors");
  console.log("topology-place: all checks passed");
} finally {
  await h.close();
}
