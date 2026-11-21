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
  "/api/profiles/delete": (body) => {
    const at = profiles.findIndex((p) => p.id === body.id);
    if (at >= 0) profiles.splice(at, 1);
    return { status: 200, body: { deleted: true, id: body.id } };
  },
});
const { page } = h;
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
  await layoutMatrix(page, h.shots, "topology-place");
  await page.setViewport({ width: 1440, height: 900 });

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
