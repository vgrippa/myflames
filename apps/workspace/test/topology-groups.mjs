// Topology connection groups (myflames 3.4.3 section 4):
// 1. Group field in Connections editor, datalist of existing groups, help text,
//    save through /api/profiles/save, and group shown in the connection list
//    and connection picker.
// 2. ConnectionGroupBar in Cluster, Replication, ProxySQL and Readyset places:
//    - group set with read outcomes (read, not_reached, failed)
//    - join callout with Add to G and Not now (persists until app restart)
//    - suggestions with Review… sheet, ungrouped items pre-ticked, type pills, address,
//      current group, evidence text, editable group name pre-filled from
//      suggested_name, moves from another group unticked and disclosed
//    - no group and no suggestions with Create a group…
//    - unsaved connections never acquire a saved identity by name or endpoint
//    - busy, result and error states on /api/profiles/group, then re-read
// 3. layoutMatrix on the bar and on the open sheet (1440/720 x light/dark).
// 4. The Topology map on a Group connection: a member card opens that
//    member's place (Readyset, ProxySQL, Cluster, Replication or Server),
//    with a Details button that keeps the inspector, a per-place member note,
//    focus that follows the navigation, and a Back to Topology chip that
//    keeps the map's selection and returns focus to the card.
import assert from "node:assert/strict";
import path from "node:path";
import { chooseConnection, connectionOption, layoutMatrix, openConnectionPicker } from "./chrome-helpers.mjs";
import { load, open, pause, setUiPreferences } from "./perf-harness.mjs";

const clusterBase = await load("cluster-ic-8.4-healthy.json");
const replicationBase = await load("replication-mysql-8.4-healthy.json");
const replicationError = await load("replication-mysql-8.4-gtid-error.json");
const proxysqlBase = await load("proxysql-lab.json");
const readysetBase = await load("readyset-lab.json");
const healthBase = await load("health-mysql-8.4.json");
const lifecycleTools = await load("lifecycle-tools-mysql.json");
const lifecycleJobRunning = await load("lifecycle-job-dump-running.json");
const lifecycleJobCancelled = await load("lifecycle-job-dump-cancelled.json");

const profiles = [
  {
    id: "p-ic-1",
    name: "cluster-ic-1",
    host: "10.0.0.11",
    port: 3306,
    user: "dba",
    database: "",
    ssl_mode: "PREFERRED",
    ssl_ca: "",
    type: "production",
    read_only: false,
    group: "mf-ic",
    remember_password: true,
    has_saved_password: true,
    secret_backend: "vault",
    secret_backend_label: "myflames encrypted vault",
  },
  {
    id: "p-ic-2",
    name: "cluster-ic-2",
    host: "10.0.0.12",
    port: 3306,
    user: "dba",
    database: "",
    ssl_mode: "PREFERRED",
    ssl_ca: "",
    type: "development",
    read_only: false,
    group: "",
    remember_password: true,
    has_saved_password: true,
    secret_backend: "vault",
    secret_backend_label: "myflames encrypted vault",
  },
  {
    id: "p-ic-3",
    name: "cluster-ic-3",
    host: "10.0.0.13",
    port: 3306,
    user: "dba",
    database: "",
    ssl_mode: "PREFERRED",
    ssl_ca: "",
    type: "production",
    read_only: false,
    group: "legacy-ic",
    remember_password: true,
    has_saved_password: true,
    secret_backend: "vault",
    secret_backend_label: "myflames encrypted vault",
  },
  {
    id: "p-solo",
    name: "solo-db",
    host: "10.0.0.99",
    port: 3306,
    user: "dba",
    database: "",
    ssl_mode: "PREFERRED",
    ssl_ca: "",
    type: "development",
    read_only: false,
    group: "",
    remember_password: true,
    has_saved_password: true,
    secret_backend: "vault",
    secret_backend_label: "myflames encrypted vault",
  },
];

const saveCalls = [];
const groupCalls = [];
const replicationReads = [];
const proxysqlReads = [];
let groupError = "";
let onGroupSuccess = null;
let replicationReport = structuredClone(replicationBase);
let replicationReportFor = null;
let proxysqlReport = structuredClone(proxysqlBase);

// Section 8 (Topology map navigation): the Group connection mf-nav, its Map
// report and the reads of the member places it opens.
const connectCalls = [];
const topologyCalls = [];
const readysetReads = [];
const healthReads = [];
const loopRegistry = new Map();
const loopStarts = [];
const loopStatusReads = [];
const loopStops = [];
const lifecycleJobs = new Map();
const lifecycleCancels = [];
const loopEndpoint = (c) => `${c?.host}:${c?.port}:${c?.user}`;
const loopRunning = await load("replication-loop-running.json");
const loopStopped = await load("replication-loop-stopped.json");
function navMember(over) {
  return {
    profile_id: "", profile_name: "", host: "", port: 3306,
    safety: { type: "production", read_only: false },
    outcome: "read", message: "Read from this member.", elapsed_s: 0.1,
    engine: "mysql", product: "MySQL", version: "8.4.6", version_comment: "MySQL Community Server - GPL",
    server_uuid: null, server_id: null, hostname: null,
    read_only: false, read_only_value: "OFF", super_read_only: false,
    role: "standalone", state: "reachable", lag: null, behind: null, behind_reason: null, channels: [],
    duplicate_of: null, ...over,
  };
}
const navMembers = [
  navMember({ profile_id: "p-nav-1", profile_name: "nav-db-1", host: "10.0.1.11", role: "primary", state: "ONLINE", server_uuid: "11111111-1111-1111-1111-1111111111aa" }),
  navMember({ profile_id: "p-nav-2", profile_name: "nav-db-2", host: "10.0.1.12", role: "source", state: "running", server_uuid: "22222222-2222-2222-2222-2222222222aa" }),
  navMember({ profile_id: "p-nav-3", profile_name: "nav-db-3", host: "10.0.1.13", server_uuid: "33333333-3333-3333-3333-3333333333aa" }),
  navMember({ profile_id: "p-nav-proxy", profile_name: "nav-proxy", host: "10.0.1.14", port: 6032, engine: null, product: "ProxySQL Admin", version: "2.5.1", version_comment: null, role: "proxy" }),
  navMember({ profile_id: "p-nav-rs", profile_name: "nav-rs", host: "10.0.1.15", port: 3307, engine: null, product: "Readyset", version: "2410.30", version_comment: null, role: "cache" }),
];
function navMapNode(over) {
  return {
    id: "", kind: "member", profile_id: "", label: "", host: "", port: 3306,
    engine: "mysql", product: "MySQL", version: "8.4.6", role: "standalone",
    state: "reachable", outcome: "read", server_uuid: null, cluster_id: null, duplicate_of: null,
    ...over,
  };
}
const navMapReport = {
  schema_version: "topology-group-1.0", section: "map", group_name: "mf-nav",
  taken_at: "2026-11-26T12:00:00Z", elapsed_s: 0.3,
  effective_safety: { type: "production", read_only: false },
  active_member_profile_id: "p-nav-1", primary_profile_id: "p-nav-1",
  members: navMembers,
  summary: { read: 5, failed: 0, not_reached: 0, versions: ["8.4.6"], text: "5 read." },
  map: {
    nodes: [
      navMapNode({ id: "p-nav-proxy", profile_id: "p-nav-proxy", label: "nav-proxy", host: "10.0.1.14", port: 6032, engine: null, product: "ProxySQL Admin", version: "2.5.1", role: "proxy" }),
      navMapNode({ id: "p-nav-rs", profile_id: "p-nav-rs", label: "nav-rs", host: "10.0.1.15", port: 3307, engine: null, product: "Readyset", version: "2410.30", role: "cache" }),
      navMapNode({ id: "p-nav-1", profile_id: "p-nav-1", label: "nav-db-1", host: "10.0.1.11", role: "primary", state: "ONLINE", server_uuid: "11111111-1111-1111-1111-1111111111aa" }),
      navMapNode({ id: "p-nav-2", profile_id: "p-nav-2", label: "nav-db-2", host: "10.0.1.12", role: "source", state: "running", server_uuid: "22222222-2222-2222-2222-2222222222aa" }),
      navMapNode({ id: "p-nav-3", profile_id: "p-nav-3", label: "nav-db-3", host: "10.0.1.13", server_uuid: "33333333-3333-3333-3333-3333333333aa" }),
      { id: "peer:10.0.0.88:3306", kind: "peer", profile_id: null, label: "10.0.0.88:3306", host: "10.0.0.88", port: 3306, server_uuid: null, seen_by: ["p-nav-2"], reason: "Not in this group" },
    ],
    edges: [
      { from: "p-nav-proxy", to: "p-nav-rs", kind: "proxy_route", evidence: "endpoint", verified: true, state: "ONLINE", label: "ProxySQL route" },
      { from: "p-nav-rs", to: "p-nav-1", kind: "cache_upstream", evidence: "server_uuid", verified: true, state: "running", label: "Cache upstream" },
      { from: "p-nav-2", to: "p-nav-3", kind: "replication", evidence: "server_uuid", verified: true, state: "running", label: "Replication" },
      { from: "p-nav-2", to: "peer:10.0.0.88:3306", kind: "replication", evidence: "endpoint", verified: false, state: null, label: "Replication" },
    ],
    clusters: [],
    notes: [],
  },
};

const h = await open("/api/live/cluster", "topology-groups", {
  "/api/profiles/list": () => ({
    status: 200,
    body: {
      items: profiles,
      secret_store: {
        backend: "vault",
        label: "myflames encrypted vault",
        available: true,
      },
    },
  }),
  "/api/profiles/save": (body) => {
    saveCalls.push(body);
    const val = body.value || {};
    const id = body.id || `p-saved-${saveCalls.length}`;
    const existing = profiles.find((p) => p.id === id);
    const record = {
      ...(existing || {}),
      id,
      name: (val.name || val.host || "unnamed").trim(),
      host: val.host || "127.0.0.1",
      port: Number(val.port) || 3306,
      user: val.user || "",
      database: val.database || "",
      ssl_mode: val.ssl_mode || "PREFERRED",
      ssl_ca: val.ssl_ca || "",
      type: val.type === "development" ? "development" : "production",
      read_only: val.read_only === true,
      group:
        typeof val.group === "string"
          ? val.group.trim()
          : existing?.group || "",
      remember_password: Boolean(body.remember_password),
      has_saved_password: Boolean(body.remember_password),
      secret_backend: "vault",
      secret_backend_label: "myflames encrypted vault",
    };
    if (existing) Object.assign(existing, record);
    else profiles.push(record);
    return { status: 200, body: record };
  },
  "/api/profiles/group": (body) => {
    groupCalls.push(body);
    if (groupError) {
      return { status: 400, body: { error: groupError } };
    }
    const ids = Array.isArray(body.profile_ids) ? body.profile_ids : [];
    const targetGroup = typeof body.group === "string" ? body.group.trim() : "";
    for (const id of ids) {
      const found = profiles.find((p) => p.id === id);
      if (found) found.group = targetGroup;
    }
    if (onGroupSuccess) onGroupSuccess(body);
    return { status: 200, body: { updated: ids, group: targetGroup } };
  },
  // The group editor sheet (Edit group…): an unticked member leaves the group.
  "/api/profiles/groups/save": (body) => {
    const name = (body.name || "").trim();
    const previous = (body.previous_name || "").trim() || name;
    const ids = new Set(Array.isArray(body.profile_ids) ? body.profile_ids : []);
    for (const p of profiles) {
      if (ids.has(p.id)) p.group = name;
      else if ((p.group || "") === previous) p.group = "";
    }
    return { status: 200, body: { group: name, name, profile_ids: [...ids], updated: [...ids] } };
  },
  "/api/live/replication": (body) => {
    replicationReads.push(body);
    return { status: 200, body: replicationReportFor ? replicationReportFor(body) : replicationReport };
  },
  "/api/live/replication/skip-loop/start": (body) => {
    loopStarts.push(body);
    const running = structuredClone(loopRunning);
    loopRegistry.set(loopEndpoint(body.connection), [running]);
    return { status: 200, body: running };
  },
  "/api/live/replication/skip-loop/status": (body) => {
    loopStatusReads.push(body);
    return { status: 200, body: { schema_version: "replication-skip-loop-1.0", loops: loopRegistry.get(loopEndpoint(body.connection)) || [] } };
  },
  "/api/live/replication/skip-loop/stop": (body) => {
    assert.equal(body.connection.password, "", "Skip loop stop never carries another member's typed password");
    loopStops.push(body);
    loopRegistry.set(loopEndpoint(body.connection), [structuredClone(loopStopped)]);
    return { status: 200, body: structuredClone(loopStopped) };
  },
  "/api/lifecycle/tools": () => ({ status: 200, body: lifecycleTools }),
  "/api/lifecycle/jobs": (body) => ({
    status: 200,
    body: { schema_version: "lifecycle-jobs-1.0", jobs: lifecycleJobs.get(loopEndpoint(body.connection)) || [] },
  }),
  "/api/lifecycle/status": (body) => {
    const list = lifecycleJobs.get(loopEndpoint(body.connection)) || [];
    return { status: 200, body: list.find((j) => j.id === body.id) || structuredClone(lifecycleJobCancelled) };
  },
  "/api/lifecycle/cancel": (body) => {
    assert.equal(body.connection.password, "", "Maintenance cancel never carries another member's typed password");
    lifecycleCancels.push(body);
    const cancelled = { ...structuredClone(lifecycleJobCancelled), id: body.id };
    const ep = loopEndpoint(body.connection);
    lifecycleJobs.set(ep, (lifecycleJobs.get(ep) || []).map((j) => (j.id === body.id ? cancelled : j)));
    return { status: 200, body: cancelled };
  },
  "/api/live/proxysql": (body) => {
    proxysqlReads.push(body);
    return { status: 200, body: proxysqlReport };
  },
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
        primary_profile_id: active.id,
        members: grouped.map((m) => ({
          profile_id: m.id,
          profile_name: m.name,
          host: m.host,
          port: m.port,
          safety: { type: m.type, read_only: !!m.read_only },
          server_uuid: null,
          role: m.id === active.id ? "PRIMARY" : "SECONDARY",
          state: null,
          reachable: body.probe_members === true ? true : null,
        })),
      },
    } };
  },
  "/api/live/topology": (body) => {
    topologyCalls.push(body);
    assert.equal(body.connection.group_name, "mf-nav", "the map read names its group");
    assert.equal(body.section, "map", "only the Map tab is read here");
    return { status: 200, body: navMapReport };
  },
  "/api/live/readyset": (body) => {
    readysetReads.push(body);
    return { status: 200, body: readysetBase };
  },
  "/api/live/health": (body) => {
    healthReads.push(body);
    return { status: 200, body: healthBase };
  },
});

const { page, state } = h;
const clusterView = '.server-view[data-view="cluster"]';
const writtenShots = [];

async function refreshClusterTo(report) {
  const before = state.seen.length;
  state.fallback = { status: 200, body: report };
  await h.refresh();
  for (let i = 0; i < 60 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh reads the cluster once");
  await page.waitForFunction(
    (sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line`),
    {},
    clusterView,
  );
  await pause(120);
}

try {
  await page.reload({ waitUntil: "networkidle0" });

  // ---------------------------------------------------------------------------
  // 1. Connections editor, list and picker
  // ---------------------------------------------------------------------------
  // The Connections editor lives on the Home place (no sub-section buttons):
  // open Home through the places navigation, as connections.mjs does.
  await page.waitForSelector('.main-nav button[data-place="home"]', { visible: true });
  if (!(await page.$('.main-nav button[data-place="home"].active'))) {
    await page.click('.main-nav button[data-place="home"]');
  }
  await page.waitForSelector('.server-view[data-view="connections"] tbody tr[data-row-key="p-ic-1"]');

  // List shows the group badge next to the connection name.
  const listGroupTags = await page.$$eval(
    '.server-view[data-view="connections"] tbody tr[data-row-key="p-ic-1"] .connection-group-tag',
    (els) => els.map((e) => e.textContent.trim()),
  );
  assert.deepEqual(listGroupTags, ["mf-ic"], "Connection list shows the group next to the name");

  // Edit solo-db: verify Group field, datalist of existing groups, and help text.
  await page.click('.server-view[data-view="connections"] tbody tr[data-row-key="p-solo"] .connection-actions');
  await page.waitForSelector('.connection-menu button[aria-label="Edit solo-db"]', { visible: true });
  await page.click('.connection-menu button[aria-label="Edit solo-db"]');
  await page.waitForSelector('#connection-group-hint', { visible: true });

  const hintText = await page.$eval('#connection-group-hint', (el) => el.textContent.trim());
  assert.equal(
    hintText,
    "Connections in the same group may be read together to draw a cluster or replication topology. myflames never reads a saved connection outside the group of the connection in use.",
  );

  const datalistOptions = await page.$$eval(
    '#connection-group-list option',
    (els) => els.map((e) => e.value),
  );
  assert.deepEqual(datalistOptions, ["legacy-ic", "mf-ic"], "Existing group names are offered in the datalist");

  // Type a new group name and save through /api/profiles/save.
  await page.evaluate(() => {
    const input = document.querySelector('input[list="connection-group-list"]');
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, "stage-topo");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('.connection-panel .button-row button')].find(
      (b) => b.textContent.trim() === "Save connection",
    );
    btn.click();
  });
  for (let i = 0; i < 40 && saveCalls.length === 0; i++) await pause(25);
  assert.equal(saveCalls.length, 1, "Save connection calls /api/profiles/save");
  assert.equal(saveCalls[0].id, "p-solo");
  assert.equal(saveCalls[0].value.group, "stage-topo", "Group field saves through profile save");

  // Verify the updated group shows in the connection list and picker.
  await page.waitForFunction(
    () =>
      document
        .querySelector('.server-view[data-view="connections"] tbody tr[data-row-key="p-solo"] .connection-group-tag')
        ?.textContent?.trim() === "stage-topo",
  );
  // Reset solo-db's group to "" for the topology discovery scenarios below.
  profiles.find((p) => p.id === "p-solo").group = "";

  // The connection picker lives in the toolbar of a connection place, never on
  // Home (main.tsx connectionPlace): open the Query editor, as connections.mjs
  // does for its picker checks.
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector('.window-toolbar .connection-pill', { visible: true });

  // Select cluster-ic-1 in the connection picker and verify the picker pill and option show its group.
  await chooseConnection(page, await connectionOption(page, "cluster-ic-1"));
  const pillGroup = await page.$eval(
    '.window-toolbar .connection-pill .connection-group-tag',
    (el) => el.textContent.trim(),
  );
  assert.equal(pillGroup, "mf-ic", "Connection picker pill shows the active connection group");
  await openConnectionPicker(page);
  const pickerOptionTexts = await page.$$eval(
    '.window-toolbar select[aria-label="Connection"] option',
    (els) => els.map((e) => e.textContent.trim()),
  );
  assert.ok(
    pickerOptionTexts.includes("cluster-ic-1 (mf-ic)"),
    `Picker options include group name: ${pickerOptionTexts.join(", ")}`,
  );
  await page.keyboard.press("Escape");

  // ---------------------------------------------------------------------------
  // 2. Cluster place — State 1: group set with read outcomes (read, not_reached, failed)
  // ---------------------------------------------------------------------------
  const reportGroupRead = {
    ...structuredClone(clusterBase),
    connection_group: {
      name: "mf-ic",
      read: [
        { id: "p-ic-2", name: "cluster-ic-2", type: "development", outcome: "read" },
        { id: "p-ic-3", name: "cluster-ic-3", type: "production", outcome: "read" },
        {
          id: "p-ic-router",
          name: "cluster-ic-router",
          type: "production",
          outcome: "not_reached",
          message: "connection timed out",
        },
        {
          id: "p-ic-4",
          name: "cluster-ic-4",
          type: "production",
          outcome: "failed",
          message: "Access denied",
        },
      ],
      suggestions: [],
      join: [],
      suggested_name: "mf-ic",
    },
  };
  state.fallback = { status: 200, body: reportGroupRead };
  await page.click('.main-nav button[data-place="cluster"]');
  await page.waitForSelector(`${clusterView} .connection-group-bar [data-state="group-read"]`, { visible: true });

  const barText1 = await page.$eval(
    `${clusterView} .connection-group-bar`,
    (el) => el.textContent.replace(/\s+/g, " ").trim(),
  );
  assert.match(barText1, /Group mf-ic · read cluster-ic-2, cluster-ic-3/);
  assert.match(barText1, /cluster-ic-router not reached: connection timed out/);
  assert.match(barText1, /cluster-ic-4 failed: Access denied/);

  // Verify the bar is positioned above .cluster-topology.
  const barAboveTopology = await page.evaluate((sel) => {
    const bar = document.querySelector(`${sel} .connection-group-bar`).getBoundingClientRect();
    const topo = document.querySelector(`${sel} .cluster-topology`).getBoundingClientRect();
    return bar.bottom <= topo.top + 2;
  }, clusterView);
  assert.equal(barAboveTopology, true, "ConnectionGroupBar sits above ClusterTopology");

  // Run layoutMatrix on the bar (1440 & 720, light & dark).
  writtenShots.push(
    ...(await layoutMatrix(page, h.shots, "topology-group-bar", {
      frame: `${clusterView} .connection-group-bar`,
    })),
  );
  await page.setViewport({ width: 1440, height: 900 });

  // ---------------------------------------------------------------------------
  // 3. Cluster place — State 2: join callout with Not now and Add to G (+ error state)
  // ---------------------------------------------------------------------------
  await chooseConnection(page, await connectionOption(page, "cluster-ic-2"));
  const reportJoin = {
    ...structuredClone(clusterBase),
    connection_group: {
      name: "",
      read: [],
      suggestions: [],
      join: [
        {
          group: "mf-ic",
          evidence: "gr_group_name",
          evidence_text: "same Group Replication group_name as cluster-ic-1",
        },
        {
          group: "other-ic",
          evidence: "server_uuid",
          evidence_text: "listed in replication_group_members on other-ic-1",
        },
      ],
      suggested_name: "mfcluster",
    },
  };
  await refreshClusterTo(reportJoin);

  const joinLines = await page.$$eval(
    `${clusterView} .connection-group-bar [data-state="group-join"]`,
    (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()),
  );
  assert.equal(joinLines.length, 2);
  assert.match(
    joinLines[0],
    /This connection is part of the topology of group mf-ic \(same Group Replication group_name as cluster-ic-1\)\. Add it to mf-ic\?/,
  );

  // Clicking "Not now" on the second join callout hides it for this connection across refreshes.
  await page.evaluate((sel) => {
    const rows = [...document.querySelectorAll(`${sel} .connection-group-bar [data-state="group-join"]`)];
    const second = rows.find((r) => r.textContent.includes("other-ic"));
    second.querySelector('button[data-action="join-dismiss"]').click();
  }, clusterView);
  await refreshClusterTo(reportJoin);
  const joinLinesAfterDismiss = await page.$$eval(
    `${clusterView} .connection-group-bar [data-state="group-join"]`,
    (els) => els.map((e) => e.textContent.replace(/\s+/g, " ").trim()),
  );
  assert.equal(joinLinesAfterDismiss.length, 1, "Not now hides the dismissed join callout across refreshes");
  assert.ok(!joinLinesAfterDismiss[0].includes("other-ic"));

  // Test error state when Add to mf-ic fails.
  groupError = "Unknown profile ID in group update.";
  await page.click(`${clusterView} .connection-group-bar button[data-action="join-add"]`);
  await page.waitForSelector(`${clusterView} .connection-group-bar .action-status[data-phase="failed"]`, {
    visible: true,
  });
  const joinErrText = await page.$eval(
    `${clusterView} .connection-group-bar .action-status[data-phase="failed"]`,
    (el) => el.textContent.trim(),
  );
  assert.match(joinErrText, /Unknown profile ID in group update\./);
  assert.deepEqual(groupCalls.at(-1), {
    profile_ids: ["p-ic-2"],
    group: "mf-ic",
  });

  // Now succeed on Add to mf-ic and verify the place re-reads.
  groupError = "";
  const readsBeforeJoin = state.seen.length;
  onGroupSuccess = () => {
    state.fallback = {
      status: 200,
      body: {
        ...structuredClone(clusterBase),
        connection_group: {
          name: "mf-ic",
          read: [{ id: "p-ic-1", name: "cluster-ic-1", type: "production", outcome: "read" }],
          suggestions: [],
          join: [],
          suggested_name: "mf-ic",
        },
      },
    };
  };
  await page.click(`${clusterView} .connection-group-bar button[data-action="join-add"]`);
  await page.waitForFunction(
    (sel) =>
      document
        .querySelector(`${sel} .connection-group-bar [data-state="group-read"]`)
        ?.textContent?.includes("Group mf-ic · read cluster-ic-1"),
    {},
    clusterView,
  );
  assert.equal(state.seen.length, readsBeforeJoin + 1, "Add to G re-reads the Cluster place");
  assert.deepEqual(groupCalls.at(-1), {
    profile_ids: ["p-ic-2"],
    group: "mf-ic",
  });
  onGroupSuccess = null;

  // ---------------------------------------------------------------------------
  // 4. Cluster place — State 3: suggestions with Review… sheet, pre-ticked items,
  //    type pills, evidence text, editable group name pre-filled from suggested_name,
  //    and unticked production connection safety
  // ---------------------------------------------------------------------------
  await chooseConnection(page, await connectionOption(page, "solo-db"));
  const reportSuggestions = {
    ...structuredClone(clusterBase),
    connection_group: {
      name: "",
      read: [],
      suggestions: [
        {
          id: "p-ic-2",
          name: "cluster-ic-2",
          type: "development",
          address: "10.0.0.12:3306",
          group: "",
          evidence: "gr_member",
          evidence_text: "online member in performance_schema.replication_group_members",
        },
        {
          id: "p-ic-3",
          name: "cluster-ic-3",
          type: "production",
          address: "10.0.0.13:3306",
          group: "legacy-ic",
          evidence: "gr_member",
          evidence_text: "secondary member in performance_schema.replication_group_members",
        },
      ],
      join: [],
      suggested_name: "mfcluster",
    },
  };
  await refreshClusterTo(reportSuggestions);

  const suggestionsBarText = await page.$eval(
    `${clusterView} .connection-group-bar [data-state="group-suggestions"]`,
    (el) => el.textContent.replace(/\s+/g, " ").trim(),
  );
  assert.match(suggestionsBarText, /2 saved connections are part of this topology/);

  // Open the Review… sheet.
  await page.click(`${clusterView} .connection-group-bar button[data-action="review-group-suggestions"]`);
  const sheetSel = "dialog.connection-group-sheet[open]";
  await page.waitForSelector(sheetSel, { visible: true });

  // Verify group name input is pre-filled with suggested_name ("mfcluster").
  const prefilledGroup = await page.$eval(
    `${sheetSel} .connection-group-field input`,
    (el) => el.value,
  );
  assert.equal(prefilledGroup, "mfcluster", "Group name input is pre-filled from suggested_name");

  // Ungrouped suggestions start ticked; moving a connection from another group
  // needs explicit consent and names the group it would leave.
  const sheetItems = await page.$$eval(
    `${sheetSel} li[data-suggestion-id]`,
    (els) =>
      els.map((li) => ({
        id: li.dataset.suggestionId,
        checked: li.querySelector('input[type="checkbox"]').checked,
        name: li.querySelector(".connection-group-item-name")?.textContent?.trim(),
        typePill: li.querySelector(".type-tag")?.textContent?.trim(),
        typeClass: li.querySelector(".type-tag")?.className,
        address: li.querySelector(".connection-group-addr")?.textContent?.trim(),
        currentGroup: li.querySelector(".connection-group-current")?.textContent?.trim() || "",
        evidence: li.querySelector(".connection-group-evidence")?.textContent?.trim() || "",
      })),
  );
  assert.deepEqual(sheetItems, [
    {
      id: "p-ic-2",
      checked: true,
      name: "cluster-ic-2",
      typePill: "Development",
      typeClass: "type-tag is-development",
      address: "10.0.0.12:3306",
      currentGroup: "",
      evidence: "online member in performance_schema.replication_group_members",
    },
    {
      id: "p-ic-3",
      checked: false,
      name: "cluster-ic-3",
      typePill: "Production",
      typeClass: "type-tag is-production",
      address: "10.0.0.13:3306",
      currentGroup: "Moves it from group legacy-ic",
      evidence: "secondary member in performance_schema.replication_group_members",
    },
  ]);

  // Run layoutMatrix on the open sheet (1440 & 720, light & dark).
  writtenShots.push(
    ...(await layoutMatrix(page, h.shots, "topology-group-sheet", {
      frame: sheetSel,
    })),
  );
  await page.setViewport({ width: 1440, height: 900 });

  // A move can be explicitly selected, then unticked before confirmation.
  await page.click(`${sheetSel} li[data-suggestion-id="p-ic-3"] input[type="checkbox"]`);
  assert.equal(await page.$eval(`${sheetSel} li[data-suggestion-id="p-ic-3"] input[type="checkbox"]`, (el) => el.checked), true);
  await page.click(`${sheetSel} li[data-suggestion-id="p-ic-3"] input[type="checkbox"]`);
  await page.evaluate((sel) => {
    const input = document.querySelector(`${sel} .connection-group-field input`);
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, "mfcluster-custom");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }, sheetSel);

  // Confirm button text reflects the 2 connections being added (current p-solo + ticked p-ic-2).
  const confirmButtonText = await page.$eval(
    `${sheetSel} .sheet-buttons button.primary`,
    (el) => el.textContent.trim(),
  );
  assert.equal(confirmButtonText, "Add 2 to mfcluster-custom");

  const readsBeforeReviewConfirm = state.seen.length;
  onGroupSuccess = (reqBody) => {
    state.fallback = {
      status: 200,
      body: {
        ...structuredClone(clusterBase),
        connection_group: {
          name: reqBody.group,
          read: [{ id: "p-ic-2", name: "cluster-ic-2", type: "development", outcome: "read" }],
          suggestions: [],
          join: [],
          suggested_name: reqBody.group,
        },
      },
    };
  };
  await page.click(`${sheetSel} .sheet-buttons button.primary`);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, sheetSel);

  const reviewCall = groupCalls.at(-1);
  assert.deepEqual(
    reviewCall,
    {
      profile_ids: ["p-solo", "p-ic-2"],
      group: "mfcluster-custom",
    },
    "Calls /api/profiles/group with current profile id + ticked ids and chosen name",
  );
  assert.ok(
    !reviewCall.profile_ids.includes("p-ic-3"),
    "Unticked production connection is never sent to /api/profiles/group",
  );
  assert.equal(state.seen.length, readsBeforeReviewConfirm + 1, "Confirming Review sheet re-reads the Cluster place");
  onGroupSuccess = null;

  // ---------------------------------------------------------------------------
  // 5. Cluster place — State 4: no group and no suggestions with Create a group…
  // ---------------------------------------------------------------------------
  profiles.find((p) => p.id === "p-solo").group = "";
  const reportNoGroup = {
    ...structuredClone(clusterBase),
    connection_group: {
      name: "",
      read: [],
      suggestions: [],
      join: [],
      suggested_name: "solo-cluster",
    },
  };
  await refreshClusterTo(reportNoGroup);

  const noGroupText = await page.$eval(
    `${clusterView} .connection-group-bar [data-state="no-group"]`,
    (el) => el.textContent.replace(/\s+/g, " ").trim(),
  );
  assert.match(noGroupText, /No connection group: myflames reads only this connection\./);
  assert.match(noGroupText, /Create a group…/);

  await page.click(`${clusterView} .connection-group-bar button[data-action="create-group"]`);
  await page.waitForSelector(sheetSel, { visible: true });

  const createPrefilled = await page.$eval(
    `${sheetSel} .connection-group-field input`,
    (el) => el.value,
  );
  assert.equal(createPrefilled, "solo-cluster");

  const createConfirmLabel = await page.$eval(
    `${sheetSel} .sheet-buttons button.primary`,
    (el) => el.textContent.trim(),
  );
  assert.equal(createConfirmLabel, "Add 1 to solo-cluster");

  const readsBeforeCreate = state.seen.length;
  onGroupSuccess = (reqBody) => {
    state.fallback = {
      status: 200,
      body: {
        ...structuredClone(clusterBase),
        connection_group: {
          name: reqBody.group,
          read: [],
          suggestions: [],
          join: [],
          suggested_name: reqBody.group,
        },
      },
    };
  };
  await page.click(`${sheetSel} .sheet-buttons button.primary`);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, sheetSel);
  assert.deepEqual(groupCalls.at(-1), {
    profile_ids: ["p-solo"],
    group: "solo-cluster",
  });
  assert.equal(state.seen.length, readsBeforeCreate + 1, "Create a group re-reads the Cluster place");
  onGroupSuccess = null;

  // ---------------------------------------------------------------------------
  // 6. Replication and ProxySQL places render ConnectionGroupBar and re-read
  // ---------------------------------------------------------------------------
  profiles.find((p) => p.id === "p-solo").group = "";
  replicationReport = {
    ...structuredClone(replicationBase),
    connection_group: {
      name: "",
      read: [],
      suggestions: [
        {
          id: "p-ic-2",
          name: "cluster-ic-2",
          type: "development",
          address: "10.0.0.12:3306",
          group: "",
          evidence: "source_uuid",
          evidence_text: "source_uuid matches SHOW REPLICA STATUS on this replica",
        },
      ],
      join: [],
      suggested_name: "repl-group",
    },
  };
  await page.click('.main-nav button[data-place="replication"]');
  await page.waitForSelector('.replication-place .connection-group-bar [data-state="group-suggestions"]', {
    visible: true,
  });
  const replBarText = await page.$eval(
    '.replication-place .connection-group-bar [data-state="group-suggestions"]',
    (el) => el.textContent.replace(/\s+/g, " ").trim(),
  );
  assert.match(replBarText, /1 saved connection is part of this topology/);

  const replicationView = '.replication-place .server-view[data-view="replication"]';
  async function assertReplicationBarAbove(selector) {
    assert.equal(await page.evaluate((view, target) => {
      const bar = document.querySelector(`${view} .connection-group-bar`);
      const content = document.querySelector(`${view} ${target}`);
      return bar.parentElement === content.parentElement &&
        bar.nextElementSibling === content &&
        bar.getBoundingClientRect().bottom <= content.getBoundingClientRect().top + 2;
    }, replicationView, selector), true, `Replication bar is a sibling directly above ${selector}`);
  }
  await assertReplicationBarAbove('.replication-topology-container');
  writtenShots.push(...await layoutMatrix(page, h.shots, "replication-group-bar", {
    frame: `${replicationView} .connection-group-bar`,
  }));
  await page.setViewport({ width: 1440, height: 900 });

  // Changing tabs must let React remove and restore the bar with the topology.
  // The tab lives in the sub-tabs segmented control (a "Channels" metric tile
  // is a button too, so scope the text selector), as replication.mjs does.
  await page.locator(`${replicationView} [aria-label="Replication sub-tabs"] button ::-p-text(Channels)`).click();
  await page.waitForFunction((view) => !document.querySelector(`${view} .connection-group-bar`), {}, replicationView);
  await page.locator(`${replicationView} [aria-label="Replication sub-tabs"] button ::-p-text(Topology)`).click();
  await page.waitForSelector(`${replicationView} .replication-topology-container`, { visible: true });
  await assertReplicationBarAbove('.replication-topology-container');

  // Confirm the Review… sheet in Replication and verify /api/live/replication re-reads.
  await page.click('.replication-place .connection-group-bar button[data-action="review-group-suggestions"]');
  await page.waitForSelector(sheetSel, { visible: true });
  const replReadsBefore = replicationReads.length;
  onGroupSuccess = (reqBody) => {
    replicationReport = {
      ...structuredClone(replicationBase),
      connection_group: {
        name: reqBody.group,
        read: [{ id: "p-ic-2", name: "cluster-ic-2", type: "development", outcome: "read" }],
        suggestions: [],
        join: [],
        suggested_name: reqBody.group,
      },
    };
  };
  await page.click(`${sheetSel} .sheet-buttons button.primary`);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, sheetSel);
  assert.deepEqual(groupCalls.at(-1), {
    profile_ids: ["p-solo", "p-ic-2"],
    group: "repl-group",
  });
  await page.waitForFunction(
    () =>
      document
        .querySelector('.replication-place .connection-group-bar [data-state="group-read"]')
        ?.textContent?.includes("Group repl-group · read cluster-ic-2"),
  );
  assert.ok(replicationReads.length > replReadsBefore, "Replication place re-reads after group update");
  onGroupSuccess = null;

  // A source with no replica channels still offers its group's controls.
  // Switching from a non-topology tab exercises the empty-state visibility rule.
  await page.locator(`${replicationView} [aria-label="Replication sub-tabs"] button ::-p-text(Channels)`).click();
  replicationReport = {
    ...structuredClone(replicationReport),
    replica: false,
    channels: [],
    message: "This server is not a replica: it has no replication channel.",
  };
  await page.click(`${replicationView} button[data-action="refresh"]`);
  await page.waitForFunction((view) => document.querySelector(`${view} .view-empty`)?.textContent.includes("Not a replica"), {}, replicationView);
  await assertReplicationBarAbove('.view-main > .view-empty');
  writtenShots.push(...await layoutMatrix(page, h.shots, "replication-source-group-bar", {
    frame: `${replicationView} .connection-group-bar`,
  }));
  await page.setViewport({ width: 1440, height: 900 });

  // ProxySQL place renders ConnectionGroupBar above ProxySqlTopologyDiagram.
  proxysqlReport = {
    ...structuredClone(proxysqlBase),
    connection_group: {
      name: "repl-group",
      read: [{ id: "p-ic-2", name: "cluster-ic-2", type: "development", outcome: "read" }],
      suggestions: [],
      join: [],
      suggested_name: "repl-group",
    },
  };
  await page.click('.main-nav button[data-place="proxysql"]');
  // The bar sits on the Topology section; the place opens on Servers
  // (ProxySqlPlace default tab).
  await page.waitForSelector('.proxysql-view [aria-label="ProxySQL sections"] button[data-value="topology"]', { visible: true });
  await page.click('.proxysql-view [aria-label="ProxySQL sections"] button[data-value="topology"]');
  await page.waitForSelector('.proxysql-view .connection-group-bar [data-state="group-read"]', {
    visible: true,
  });
  const proxyBarText = await page.$eval(
    '.proxysql-view .connection-group-bar [data-state="group-read"]',
    (el) => el.textContent.replace(/\s+/g, " ").trim(),
  );
  assert.match(proxyBarText, /Group repl-group · read cluster-ic-2/);

  // ---------------------------------------------------------------------------
  // 7. Unsaved connections with colliding display fields never become profiles.
  // ---------------------------------------------------------------------------
  const unsavedJoin = {
    ...structuredClone(reportNoGroup),
    connection_group: {
      ...structuredClone(reportNoGroup.connection_group),
      join: [{ group: "mf-ic", evidence: "gr_member", evidence_text: "same member address" }],
    },
  };
  const callsBeforeUnsaved = groupCalls.length;
  for (const name of ["solo-db", "unsaved endpoint twin"]) {
    await page.click('.main-nav button[data-place="home"]');
    // Home's Connections editor: no sub-section buttons exist.
    await page.waitForSelector('.server-view[data-view="connections"] .connections-header-actions .pane-action', { visible: true });
    await page.click('.connections-header-actions .pane-action');
    await page.waitForSelector('.connection-panel', { visible: true });
    // Both cases share the saved endpoint; one also shares its exact name.
    for (const [label, value] of [["Host", "10.0.0.99"], ["Port", "3306"], ["User", "dba"], ["Connection name", name]]) {
      await page.evaluate((text, value) => {
        const field = [...document.querySelectorAll('.connection-panel label.field')]
          .find((el) => el.firstChild.textContent.trim() === text);
        const input = field.querySelector('input');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }, label, value);
    }
    await page.locator('.connection-panel button ::-p-text(Use without saving)').click();
    state.fallback = { status: 200, body: unsavedJoin };
    await page.click('.main-nav button[data-place="cluster"]');
    // The picker lives in a connection place's toolbar, never on Home: check
    // the cleared identity here, on the Cluster place.
    await openConnectionPicker(page);
    assert.equal(await page.$eval('.window-toolbar select[aria-label="Connection"]', (el) => el.value), "",
      "Use without saving clears the saved profile identity");
    await page.keyboard.press("Escape");
    // Opening the place on a connection it has not read reads once on its own
    // (the first twin); the second twin shares the first's connection key, so
    // the kept report answers at once. Either way the bar is up before Refresh,
    // keeping the read count in refreshClusterTo exact.
    await page.waitForSelector(`${clusterView} [data-state="unsaved-connection"]`, { visible: true });
    await refreshClusterTo(unsavedJoin);
    await page.waitForSelector(`${clusterView} [data-state="unsaved-connection"]`, { visible: true });
    assert.equal(await page.$eval(`${clusterView} [data-state="unsaved-connection"]`, (el) => el.textContent.trim()),
      "Save this connection to add it to a group.");
    assert.equal(await page.$$eval(`${clusterView} [data-action="join-add"], ${clusterView} [data-action="create-group"]`, (els) => els.length),
      0, "Unsaved connections have no Add or Create action, even with a matching saved name or endpoint");
    assert.equal(state.seen.at(-1).connection.profile_id, undefined, "The active connection is unsaved");
  }
  assert.equal(groupCalls.length, callsBeforeUnsaved, "Unsaved display-field collisions never update a saved group");

  // Reviewing suggested saved connections remains available, without ever
  // listing or adding the current unsaved connection.
  await refreshClusterTo(reportSuggestions);
  await page.click(`${clusterView} [data-action="review-group-suggestions"]`);
  await page.waitForSelector(sheetSel, { visible: true });
  assert.equal(await page.$$eval(`${sheetSel} [data-current-connection]`, (els) => els.length), 0);
  assert.equal(await page.$eval(`${sheetSel} .sheet-buttons button.primary`, (el) => el.textContent.trim()), "Add 1 to mfcluster");
  await page.click(`${sheetSel} .sheet-buttons button.primary`);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, sheetSel);
  assert.deepEqual(groupCalls.at(-1), { profile_ids: ["p-ic-2"], group: "mfcluster" },
    "Review adds only the selected saved suggestion when the current connection is unsaved");

  // ---------------------------------------------------------------------------
  // 8. Topology map on a Group connection: a member card opens that member's
  //    place; Details keeps the inspector; the note is the place's own; focus
  //    follows the navigation and Back returns it to the card.
  // ---------------------------------------------------------------------------
  profiles.push(
    { id: "p-nav-1", name: "nav-db-1", host: "10.0.1.11", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production", read_only: false, group: "mf-nav", remember_password: true, has_saved_password: true, secret_backend: "vault", secret_backend_label: "myflames encrypted vault" },
    { id: "p-nav-2", name: "nav-db-2", host: "10.0.1.12", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development", read_only: false, group: "mf-nav", remember_password: true, has_saved_password: true, secret_backend: "vault", secret_backend_label: "myflames encrypted vault" },
    { id: "p-nav-3", name: "nav-db-3", host: "10.0.1.13", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development", read_only: false, group: "mf-nav", remember_password: true, has_saved_password: true, secret_backend: "vault", secret_backend_label: "myflames encrypted vault" },
    { id: "p-nav-proxy", name: "nav-proxy", host: "10.0.1.14", port: 6032, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production", read_only: false, group: "mf-nav", remember_password: true, has_saved_password: true, secret_backend: "vault", secret_backend_label: "myflames encrypted vault" },
    // nav-rs carries its own database and read-only flag: the sidebar badge and
    // the top-edge strip on its place are the member's, not the connection in
    // use's.
    { id: "p-nav-rs", name: "nav-rs", host: "10.0.1.15", port: 3307, user: "dba", database: "shop", ssl_mode: "PREFERRED", ssl_ca: "", type: "development", read_only: true, group: "mf-nav", remember_password: true, has_saved_password: true, secret_backend: "vault", secret_backend_label: "myflames encrypted vault" },
  );
  // The saved-connections list re-reads on window focus.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await pause(1100);
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector('button[aria-label="Open group connection mf-nav"]', { visible: true });
  await page.click('button[aria-label="Open group connection mf-nav"]');
  await page.waitForSelector('.topology-map-node[data-node="p-nav-rs"]', { visible: true });
  assert.equal(topologyCalls.length, 1, "the Map tab read once when shown");
  assert.equal(connectCalls.length, 1, "opening the group connection is the one /api/connect");
  const connectsBefore = connectCalls.length;
  const rsCard = '.topology-map-node[data-node="p-nav-rs"]';
  // Screenshots go through the harness's shots folder; layoutMatrix adds the
  // overflow checks (1440/720 px, light/dark).
  async function takeShots(name, hover) {
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: 900 });
      for (const theme of ["light", "dark"]) {
        await setUiPreferences(page, { theme });
        if (hover) await page.hover(hover);
        await pause(150);
        await page.screenshot({ path: path.join(h.shots, `${name}-${width}-${theme}.png`) });
        writtenShots.push(`${name}-${width}-${theme}`);
      }
    }
    await page.setViewport({ width: 1440, height: 900 });
    await setUiPreferences(page, { theme: "light" });
    await pause(150);
  }
  // The map with the Details button visible on hover.
  await takeShots("topo-map-details", rsCard);
  writtenShots.push(...await layoutMatrix(page, h.shots, "topo-map", { frame: ".topology-place" }));
  await page.setViewport({ width: 1440, height: 900 });
  await setUiPreferences(page, { theme: "light" });

  const rsDetails = '.topology-map-node-details[aria-label="Details for nav-rs"]';
  await page.hover(rsCard);
  await pause(180);
  const hoverOpacity = await page.$eval(rsDetails, (e) => getComputedStyle(e).opacity);
  assert.equal(hoverOpacity, "1", "Details shows while its card is hovered");
  await page.hover(".topology-map-legend");
  await pause(180);
  const restOpacity = await page.$eval(rsDetails, (e) => getComputedStyle(e).opacity);
  assert.equal(restOpacity, "0", "Details hides again when the pointer leaves");
  // On touch (no hover) the Details button is always visible: the stylesheet
  // keeps a (hover: none) rule for it. (Headless Chrome does not emulate the
  // hover media feature, so the rule itself is what the test can prove.)
  const touchRule = await page.evaluate(() => {
    for (const sheet of document.styleSheets) {
      let rules;
      try { rules = sheet.cssRules; } catch { continue; }
      for (const rule of rules) {
        if (!(rule instanceof CSSMediaRule) || rule.conditionText !== "(hover: none)") continue;
        for (const inner of rule.cssRules) {
          if (inner.selectorText === ".topology-map-node-details" && inner.style.opacity === "1") return true;
        }
      }
    }
    return false;
  });
  assert.equal(touchRule, true, "Details is always visible without hover (touch)");

  // A member card's name and title state the navigation; a peer card has no
  // Details button (its click already opens the inspector).
  const cardA11y = await page.$eval(rsCard, (e) => ({ label: e.getAttribute("aria-label") || "", title: e.getAttribute("title") || "" }));
  assert.ok(cardA11y.label.startsWith("Open nav-rs in Readyset: "), `the card's name states the navigation: ${cardA11y.label}`);
  assert.match(cardA11y.title, /Opens the Readyset place on this member/, "the card's title states the navigation");
  const clusterCardA11y = await page.$eval('.topology-map-node[data-node="p-nav-1"]', (e) => e.getAttribute("aria-label") || "");
  assert.ok(clusterCardA11y.startsWith("Open nav-db-1 in Cluster: "), `the cluster card's name states the navigation: ${clusterCardA11y}`);
  assert.equal(await page.$('.topology-map-node-details[data-details="peer:10.0.0.88:3306"]'), null, "a peer card has no Details button");

  // The Details button opens the inspector without navigating.
  await page.click('.topology-map-node-details[aria-label="Details for nav-rs"]');
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector .inspector-title")]
    .some((e) => e.getClientRects().length > 0));
  assert.match(await h.inspector(), /nav-rs/, "Details opens the member inspector");
  assert.match(await h.inspector(), /Readyset/, "the inspector names the engine");
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector button")]
    .some((b) => b.textContent.trim() === "Open in Readyset"));
  assert.ok(await page.$('.main-nav button[data-place="topology"].active'), "Details never leaves the map");
  assert.equal(readysetReads.length, 0, "Details never reads the member's place");
  await page.click('.topology-place .inspector-close');

  // A peer card opens the inspector and never navigates.
  await page.click('.topology-map-node[data-node="peer:10.0.0.88:3306"]');
  assert.match(await h.inspector(), /Not in this group/, "a peer keeps its inspector");
  assert.ok(await page.$('.main-nav button[data-place="topology"].active'), "a peer never navigates");
  await page.click('.topology-place .inspector-close');

  // A Readyset member opens the Readyset place on that member.
  await page.click(rsCard);
  await page.waitForSelector('.main-nav button[data-place="readyset"].active');
  for (let i = 0; i < 100 && !readysetReads.length; i++) await pause(25);
  assert.equal(readysetReads.length, 1, "the Readyset place reads when opened");
  assert.equal(readysetReads[0].connection.profile_id, "p-nav-rs", "the read carries the clicked member");
  assert.equal(readysetReads[0].connection.group_name, "mf-nav", "the read re-checks group membership");
  assert.equal(readysetReads[0].connection.database, "shop", "the read carries the member's database");
  assert.equal(connectCalls.length, connectsBefore, "navigation never opens a new connection");
  const rsNote = "Using nav-rs from group mf-nav.";
  assert.equal(await page.$eval('.nav-member-note', (e) => e.textContent.trim()), rsNote);
  assert.equal(await page.$eval('.nav-member-note', (e) => e.getAttribute("title")), rsNote, "the truncated note keeps its full text in the title");
  // The sidebar badge and the top-edge strip are the member's own: nav-rs is
  // read-only with database shop; the connection in use (nav-db-1) is neither.
  let readysetBadgeHtml = "";
  for (let i = 0; i < 100 && !readysetBadgeHtml.includes("nav-marker"); i++) {
    readysetBadgeHtml = await page.$eval('.main-nav button[data-place="readyset"]', (e) => e.outerHTML);
    await pause(150);
  }
  assert.ok(readysetBadgeHtml.includes("nav-marker"), `the Readyset badge is the member's own: ${readysetBadgeHtml}`);
  assert.equal(await page.$eval('.connection-type-strip', (e) => e.dataset.connectionStrip), "read-only", "the strip shows the member's read-only flag");
  // The Readyset report of its own member mutes nothing else: the ProxySQL
  // and Cluster places read other members and stay plain sidebar items.
  assert.equal(await page.$eval('.main-nav button[data-place="proxysql"]', (e) => e.dataset.muted || ""), "", "a Readyset member's report never mutes ProxySQL");
  assert.equal(await page.$eval('.main-nav button[data-place="cluster"]', (e) => e.dataset.muted || ""), "", "a Readyset member's report never mutes Cluster");
  // The arrival note in the Readyset place, with the picker intact at 720 px.
  writtenShots.push(...await layoutMatrix(page, h.shots, "topo-arrive-readyset", { frame: ".window-toolbar" }));
  await page.setViewport({ width: 1440, height: 900 });
  await setUiPreferences(page, { theme: "light" });
  // Back to Topology returns with the same selection kept and focus on the card.
  await page.click('button.nav-back-chip[aria-label="Back to Topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  assert.ok(await page.$(`${rsCard}.is-selected`), "the clicked card stays selected on return");
  // Focus returns to the card: the Topology place drives it from its own
  // layout, so no settle pause is needed.
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-node") === "p-nav-rs");

  // The note is the place's own: from the sidebar, Server and ProxySQL read
  // the active member (no member was chosen for them) and show no note.
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  const healthSidebar = healthReads.length;
  for (let i = 0; i < 100 && healthReads.length === healthSidebar; i++) await pause(25);
  assert.equal(healthReads.at(-1).connection.profile_id, "p-nav-1", "Server from the sidebar reads the active member");
  assert.equal(await page.$('.nav-member-note'), null, "no nav-rs note follows into the Server place");
  assert.equal(await page.$('.server-place .group-server-note'), null, "the Server header has no member note for the active member");
  const proxySidebar = proxysqlReads.length;
  await page.click('.main-nav button[data-place="proxysql"]');
  await page.waitForSelector('.main-nav button[data-place="proxysql"].active');
  for (let i = 0; i < 100 && proxysqlReads.length === proxySidebar; i++) await pause(25);
  assert.equal(proxysqlReads.at(-1).connection.profile_id, "p-nav-1", "ProxySQL from the sidebar reads the active member");
  assert.equal(await page.$('.nav-member-note'), null, "no nav-rs note follows into the ProxySQL place");
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  // A ProxySQL member (product "ProxySQL Admin", as the backend reports it)
  // opens the ProxySQL place on that member.
  const proxyBeforeCard = proxysqlReads.length;
  await page.click('.topology-map-node[data-node="p-nav-proxy"]');
  await page.waitForSelector('.main-nav button[data-place="proxysql"].active');
  for (let i = 0; i < 100 && proxysqlReads.length === proxyBeforeCard; i++) await pause(25);
  assert.equal(proxysqlReads.at(-1).connection.profile_id, "p-nav-proxy", "the read carries the clicked member");
  assert.equal(proxysqlReads.at(-1).connection.group_name, "mf-nav", "the read re-checks group membership");
  assert.equal(await page.$eval('.nav-member-note', (e) => e.textContent.trim()), "Using nav-proxy from group mf-nav.");
  // The badge is keyed to the member the place reads: the read above answered
  // for nav-proxy, so the sidebar names nav-proxy's state.
  let proxyBadgeHtml = "";
  for (let i = 0; i < 100 && !proxyBadgeHtml.includes("nav-marker"); i++) {
    proxyBadgeHtml = await page.$eval('.main-nav button[data-place="proxysql"]', (e) => e.outerHTML);
    await pause(150);
  }
  assert.ok(proxyBadgeHtml.includes("nav-marker"), `the ProxySQL badge is the member's own: ${proxyBadgeHtml}`);
  // The ProxySQL report of its own member mutes nothing else either: Readyset
  // still reads nav-rs, and Cluster still reads nav-db-1.
  assert.equal(await page.$eval('.main-nav button[data-place="readyset"]', (e) => e.dataset.muted || ""), "", "a ProxySQL member's report never mutes Readyset");
  assert.equal(await page.$eval('.main-nav button[data-place="cluster"]', (e) => e.dataset.muted || ""), "", "a ProxySQL member's report never mutes Cluster");

  // Switching places through the sidebar keeps each place's own note: Readyset
  // still reads nav-rs, never nav-proxy.
  await page.click('.main-nav button[data-place="readyset"]');
  await page.waitForSelector('.main-nav button[data-place="readyset"].active');
  assert.equal(await page.$eval('.nav-member-note', (e) => e.textContent.trim()), rsNote, "the Readyset place keeps its own member note");
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  // A cluster member opens the Cluster place on that member.
  state.fallback = { status: 200, body: clusterBase };
  const clusterReadsNav = state.seen.length;
  await page.click('.topology-map-node[data-node="p-nav-1"]');
  await page.waitForSelector('.main-nav button[data-place="cluster"].active');
  for (let i = 0; i < 100 && state.seen.length === clusterReadsNav; i++) await pause(25);
  assert.equal(state.seen.at(-1).connection.profile_id, "p-nav-1", "the Cluster place reads the clicked member");
  assert.equal(state.seen.at(-1).connection.group_name, "mf-nav", "the read re-checks group membership");
  // The clicked member is the active member: the place reads the connection
  // in use and no note is needed.
  assert.equal(await page.$('.nav-member-note'), null, "no member note when the place reads the active member");
  await page.click('button.nav-back-chip[aria-label="Back to Topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  // Keyboard Enter on a focused card navigates the same way, on a member not
  // read yet: the wait is for that member's own read. nav-db-2 answers as a
  // plain server (no channels), so the shared cluster report read on nav-db-1
  // is the only source a leaked cluster kind could come from.
  replicationReport = {
    ...structuredClone(replicationBase),
    replica: false,
    channels: [],
    message: "This server is not a replica: it has no replication channel.",
  };
  const replReadsNav = replicationReads.length;
  await page.focus('.topology-map-node[data-node="p-nav-2"]');
  await page.keyboard.press("Enter");
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  for (let i = 0; i < 100 && replicationReads.length === replReadsNav; i++) await pause(25);
  assert.equal(replicationReads.at(-1).connection.profile_id, "p-nav-2", "Enter opens the clicked member's Replication place");
  assert.equal(replicationReads.at(-1).connection.group_name, "mf-nav", "the read re-checks group membership");
  assert.equal(await page.$eval('.nav-member-note', (e) => e.textContent.trim()), "Using nav-db-2 from group mf-nav.");
  // Keyboard activation lands focus on the place's heading.
  await page.waitForFunction(() => document.activeElement?.id === "replication-title");
  // The cluster report read on nav-db-1 never lends its kind to this member:
  // nav-db-2's Not a replica state offers no Open Cluster action.
  await page.waitForFunction(() => document.querySelector('.replication-place')?.textContent.includes("Not a replica"));
  assert.equal(await page.evaluate(() =>
    [...document.querySelectorAll('.replication-place button')].some((b) => b.textContent.trim() === "Open Cluster")),
    false, "another member's cluster report never lends Open Cluster to this member");
  // A skip loop running on this non-active member. The replica answer turns
  // the loop status poll on; Server's banner must show it and stop it there.
  loopRegistry.set("10.0.1.12:3306:dba", [structuredClone(loopRunning)]);
  replicationReport = structuredClone(replicationBase);
  const loopStatusBefore = loopStatusReads.length;
  await page.click(`${replicationView} button[data-action="refresh"]`);
  for (let i = 0; i < 100 && loopStatusReads.length === loopStatusBefore; i++) await pause(25);
  assert.equal(loopStatusReads.at(-1).connection.profile_id, "p-nav-2", "loop status reads on the replication member");
  await page.click('button.nav-back-chip[aria-label="Back to Topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  // Focus returns to the card that opened the place, driven by the Topology
  // place itself once its map shows the card (no settle pause).
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-node") === "p-nav-2");

  // Server's persistent banner shows the loop started on the non-active
  // replication member, names the server it runs on, and stops it there.
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  await page.waitForSelector('.server-content > .ops-loop[data-loop="running"]', { visible: true });
  assert.ok(await page.$$eval('.server-content > .ops-loop .ops-last',
    (els) => els.some((e) => e.textContent.trim() === "on nav-db-2 · 10.0.1.12:3306 · SECONDARY")),
    "the banner names the member the loop runs on");
  await page.click('.server-content > .ops-loop button');
  for (let i = 0; i < 100 && !loopStops.length; i++) await pause(25);
  assert.equal(loopStops.length, 1, "Stop from the banner was sent");
  assert.equal(loopStops.at(-1).connection.profile_id, "p-nav-2", "Stop goes to the member the loop runs on, not the member on screen");
  assert.equal(loopStops.at(-1).connection.group_name, "mf-nav");
  assert.equal(loopStops.at(-1).connection.host, "10.0.1.12");
  await page.waitForFunction(() => !document.querySelector('.server-content > .ops-loop[data-loop="running"]'));
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  // Any other database member opens the Server place on that member, with the
  // member note in the view header. The place was visited above (it read the
  // active member): the previous member's report stays, greyed, until an
  // explicit Refresh, whose read is the login to the clicked member.
  await page.click('.topology-map-node[data-node="p-nav-3"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  // Five members: the Server: control is a select naming the chosen member.
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .group-server-select')?.value === "p-nav-3");
  assert.equal(await page.$eval('.server-place .group-server-note', (e) => e.textContent.trim()), "Using nav-db-3 from group mf-nav.");
  const healthBefore = healthReads.length;
  await h.refresh();
  for (let i = 0; i < 100 && healthReads.length === healthBefore; i++) await pause(25);
  assert.equal(healthReads.at(-1).connection.profile_id, "p-nav-3", "the explicit read is the login to the clicked member");
  assert.equal(healthReads.at(-1).connection.group_name, "mf-nav", "the read re-checks group membership");
  await page.click('button.nav-back-chip[aria-label="Back to Topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  // Refresh all reads the reports of the members the Replication and Cluster
  // places read, never overwriting them with the connection in use.
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  const clusterBadgeBefore = await page.$eval('.main-nav button[data-place="cluster"]', (e) => e.textContent);
  const replReadsBeforeAll = replicationReads.length, clusterReadsBeforeAll = state.seen.length;
  await page.click('.server-view[data-view="health"] button[aria-label="Refresh options"]');
  await page.click('[data-action="refresh-all"]');
  for (let i = 0; i < 200 && !(replicationReads.length > replReadsBeforeAll && state.seen.length > clusterReadsBeforeAll); i++) await pause(25);
  assert.ok(replicationReads.length > replReadsBeforeAll && state.seen.length > clusterReadsBeforeAll, "Refresh all read both topology reports");
  await page.waitForFunction(() => !document.querySelector(".refresh-all-stop"));
  assert.equal(replicationReads.at(-1).connection.profile_id, "p-nav-2", "Refresh all keeps the member the Replication place reads");
  assert.equal(replicationReads.at(-1).connection.group_name, "mf-nav", "the read re-checks group membership");
  assert.equal(state.seen.at(-1).connection.profile_id, "p-nav-1", "Refresh all keeps the member the Cluster place reads");
  assert.equal(await page.$eval('.main-nav button[data-place="cluster"]', (e) => e.textContent), clusterBadgeBefore, "the Cluster badge is the same report after Refresh all");
  // The replication place still shows its own member's report, never a report
  // the connection in use overwrote it with.
  await page.click('.main-nav button[data-place="replication"]');
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  assert.equal(await page.$('.replication-place .gate.other-connection'), null, "Refresh all never overwrote the replication member's report");
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');

  assert.equal(topologyCalls.length, 1, "the map never re-read on navigation");
  assert.equal(connectCalls.length, connectsBefore, "no card ever opened a new connection");

  // A group edit that removes a member a place was on: the notice names the
  // removal and never hides another place's "Using <member>" note, and it
  // clears on the next navigation.
  await page.click('.main-nav button[data-place="readyset"]');
  await page.waitForSelector('.main-nav button[data-place="readyset"].active');
  await page.click('.window-statusbar .statusbar-group-edit');
  await page.waitForSelector('dialog.group-editor-sheet[open]', { visible: true });
  await page.click('dialog.group-editor-sheet[open] .group-editor-item[data-profile-id="p-nav-proxy"] input[type="checkbox"]');
  await page.click('dialog.group-editor-sheet[open] .sheet-buttons button.primary');
  await page.waitForFunction(() => !document.querySelector('dialog.group-editor-sheet[open]'));
  await page.waitForFunction(() => {
    const text = document.querySelector('.nav-member-note')?.textContent || "";
    return text.includes("nav-proxy is not in this group any more; its views moved to nav-db-1.") &&
      text.includes("Using nav-rs from group mf-nav.");
  });
  assert.equal(await page.$eval('.nav-member-note', (e) => e.getAttribute("title")), await page.$eval('.nav-member-note', (e) => e.textContent.trim()), "the joined note keeps its full text in the title");
  await page.click('.main-nav button[data-place="cluster"]');
  await page.waitForSelector('.main-nav button[data-place="cluster"].active');
  assert.equal(await page.$('.nav-member-note'), null, "the removal notice clears on navigation");
  await page.click('.main-nav button[data-place="readyset"]');
  await page.waitForSelector('.main-nav button[data-place="readyset"].active');
  assert.equal(await page.$eval('.nav-member-note', (e) => e.textContent.trim()), rsNote, "only the place's own note remains");

  // ---------------------------------------------------------------------------
  // 9. Write-safety S4: a skip loop started on p-nav-2 stays visible with Stop
  //    bound to p-nav-2 when p-nav-3's card moves Replication (non-replica or
  //    replica), and survives View > Reload without a false restart note.
  //    Variant: a running maintenance job on another member stays visible with
  //    Cancel bound to that member.
  // ---------------------------------------------------------------------------
  // Mark p-nav-3 as a replica card on the map so its card opens Replication.
  navMembers.find((m) => m.profile_id === "p-nav-3").role = "replica";
  navMapReport.map.nodes.find((n) => n.id === "p-nav-3").role = "replica";
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  const topoBeforeRefresh = topologyCalls.length;
  await page.click('.topology-place button[data-action="refresh"]');
  for (let i = 0; i < 100 && topologyCalls.length === topoBeforeRefresh; i++) await pause(25);
  await page.waitForFunction(() => (document.querySelector('.topology-map-node[data-node="p-nav-3"]')?.getAttribute("aria-label") || "").startsWith("Open nav-db-3 in Replication:"));

  // Start a skip loop on p-nav-2 from its card.
  loopRegistry.clear();
  let nav3IsReplica = false;
  replicationReportFor = (body) => {
    if (body.connection.profile_id === "p-nav-2") return structuredClone(replicationError);
    if (body.connection.profile_id === "p-nav-3" && !nav3IsReplica) {
      return {
        ...structuredClone(replicationBase),
        replica: false,
        channels: [],
        message: "This server is not a replica: it has no replication channel.",
      };
    }
    return structuredClone(replicationBase);
  };
  await page.click('.topology-map-node[data-node="p-nav-2"]');
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  const replBeforeStart = replicationReads.length;
  await page.click(`${replicationView} button[data-action="refresh"]`);
  for (let i = 0; i < 100 && replicationReads.length === replBeforeStart; i++) await pause(25);
  await page.evaluate(() => {
    [...document.querySelectorAll('[aria-label="Replication sub-tabs"] button')]
      .find((b) => b.offsetParent && b.textContent.startsWith("Channels"))?.click();
  });
  await page.waitForSelector(`${replicationView} [data-row-key="channel:"]`, { visible: true });
  await page.click(`${replicationView} [data-row-key="channel:"]`);
  await page.waitForFunction(() => [...document.querySelectorAll('.replication-place .view-inspector button')]
    .some((b) => b.textContent.includes("Skip until stopped") && !b.disabled));
  await page.evaluate(() => {
    [...document.querySelectorAll('.replication-place .view-inspector button')]
      .find((b) => b.textContent.includes("Skip until stopped"))?.click();
  });
  await page.waitForSelector('dialog.ops-confirm[open]', { visible: true });
  await page.click('dialog.ops-confirm[open] .ops-loop-options label:first-of-type input');
  const startsBeforeS4 = loopStarts.length;
  await page.click('dialog.ops-confirm[open] .sheet-actions button:last-of-type');
  for (let i = 0; i < 100 && loopStarts.length === startsBeforeS4; i++) await pause(25);
  assert.equal(loopStarts.at(-1).connection.profile_id, "p-nav-2", "loop start runs on p-nav-2");
  await page.waitForFunction(() => !document.querySelector('dialog.ops-confirm[open]'));
  await page.waitForSelector('.replication-place .ops-loop[data-loop="running"]', { visible: true });
  await h.savedSlow((s) => s.replication_loop_active === true && s.replication_loop_member === "p-nav-2");

  // Open p-nav-3's card as a non-replica: Server's banner (and Replication)
  // must still show the nav-db-2 loop.
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  const replBeforeNav3NonReplica = replicationReads.length;
  await page.click('.topology-map-node[data-node="p-nav-3"]');
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  for (let i = 0; i < 100 && replicationReads.length === replBeforeNav3NonReplica; i++) await pause(25);
  await page.waitForFunction(() => document.querySelector('.replication-place')?.textContent.includes("Not a replica"));
  await page.waitForSelector('.replication-place .ops-loop[data-loop="running"]', { visible: true });
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  await page.waitForSelector('.server-content > .ops-loop[data-loop="running"]', { visible: true });
  assert.ok(await page.$$eval('.server-content > .ops-loop .ops-last',
    (els) => els.some((e) => e.textContent.trim() === "on nav-db-2 · 10.0.1.12:3306 · SECONDARY")),
    "Server's banner still names nav-db-2 after opening non-replica p-nav-3");

  // Open p-nav-3's card as a replica (whose own loop poll returns []): it must
  // publish only p-nav-3's key and never overwrite nav-db-2's loop or Stop.
  nav3IsReplica = true;
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  const statusBeforeNav3Replica = loopStatusReads.length;
  await page.click('.topology-map-node[data-node="p-nav-3"]');
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  await page.click(`${replicationView} button[data-action="refresh"]`);
  for (let i = 0; i < 120 && !loopStatusReads.slice(statusBeforeNav3Replica).some((b) => b.connection.profile_id === "p-nav-3"); i++) await pause(25);
  assert.ok(loopStatusReads.slice(statusBeforeNav3Replica).some((b) => b.connection.profile_id === "p-nav-3"), "p-nav-3 polled its own loop status");
  await page.waitForSelector('.replication-place .ops-loop[data-loop="running"]', { visible: true });
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  await page.waitForSelector('.server-content > .ops-loop[data-loop="running"]', { visible: true });
  assert.ok(await page.$$eval('.server-content > .ops-loop .ops-last',
    (els) => els.some((e) => e.textContent.trim() === "on nav-db-2 · 10.0.1.12:3306 · SECONDARY")),
    "Server's banner still names nav-db-2 after replica p-nav-3 polled []");
  await h.savedSlow((s) => s.replication_loop_active === true && s.replication_loop_member === "p-nav-2");
  const stopsBeforeS4 = loopStops.length;
  await page.click('.server-content > .ops-loop[data-loop="running"] button');
  for (let i = 0; i < 100 && loopStops.length === stopsBeforeS4; i++) await pause(25);
  assert.equal(loopStops.at(-1).connection.profile_id, "p-nav-2", "Stop posts skip-loop/stop with profile_id p-nav-2 even while Replication is on p-nav-3");
  assert.equal(loopStops.at(-1).connection.group_name, "mf-nav");
  assert.equal(loopStops.at(-1).connection.host, "10.0.1.12");
  await page.waitForFunction(() => !document.querySelector('.server-content > .ops-loop[data-loop="running"]'));
  await h.savedSlow((s) => s.replication_loop_active !== true && !s.replication_loop_member);

  // View > Reload with the registry still running on nav-db-2: restore polls
  // p-nav-2, shows no "Stopped when myflames restarted" note, and keeps the
  // nav-db-2 loop visible with a working Stop.
  loopRegistry.set("10.0.1.12:3306:dba", [structuredClone(loopRunning)]);
  await page.click('.main-nav button[data-place="topology"]');
  await page.waitForSelector('.main-nav button[data-place="topology"].active');
  await page.click('.topology-map-node[data-node="p-nav-2"]');
  await page.waitForSelector('.main-nav button[data-place="replication"].active');
  await page.waitForSelector('.replication-place .ops-loop[data-loop="running"]', { visible: true });
  await h.savedSlow((s) => s.replication_loop_active === true && s.replication_loop_member === "p-nav-2");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  const statusBeforeReload = loopStatusReads.length;
  await page.reload({ waitUntil: "networkidle0" });
  for (let i = 0; i < 160 && !loopStatusReads.slice(statusBeforeReload).some((b) => b.connection.profile_id === "p-nav-2"); i++) await pause(25);
  assert.ok(loopStatusReads.slice(statusBeforeReload).some((b) => b.connection.profile_id === "p-nav-2"), "reload polls the persisted loop member p-nav-2");
  await page.waitForSelector('.replication-place .ops-loop[data-loop="running"]', { visible: true });
  assert.equal(await page.$('.replication-place .ops-note'), null, "no false Stopped when myflames restarted note on reload");
  assert.ok(await page.$$eval('.replication-place .ops-loop[data-loop="running"] .ops-last',
    (els) => els.some((e) => e.textContent.includes("on nav-db-2"))),
    "the reloaded Replication place names the running nav-db-2 loop");
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector('.main-nav button[data-place="server"].active');
  await page.waitForSelector('.server-content > .ops-loop[data-loop="running"]', { visible: true });
  assert.ok(await page.$$eval('.server-content > .ops-loop[data-loop="running"] .ops-last',
    (els) => els.some((e) => e.textContent.includes("on nav-db-2"))),
    "the reloaded Server banner names the running nav-db-2 loop");
  const stopsBeforeReloadStop = loopStops.length;
  await page.click('.server-content > .ops-loop[data-loop="running"] button');
  for (let i = 0; i < 100 && loopStops.length === stopsBeforeReloadStop; i++) await pause(25);
  assert.equal(loopStops.at(-1).connection.profile_id, "p-nav-2", "Stop after reload targets p-nav-2");
  assert.equal(loopStops.at(-1).connection.host, "10.0.1.12");
  await page.waitForFunction(() => !document.querySelector('.server-content > .ops-loop[data-loop="running"]'));

  // Variant: a running maintenance job on another member of the active group
  // stays visible in Server's banner with a working Cancel bound to that member.
  lifecycleJobs.set("10.0.1.12:3306:dba", [{ ...structuredClone(lifecycleJobRunning), id: "job-nav-2" }]);
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector('button[aria-label="Open group connection mf-nav"]', { visible: true });
  await page.click('button[aria-label="Open group connection mf-nav"]');
  await page.waitForSelector('.topology-map-node[data-node="p-nav-2"]', { visible: true });
  await h.view("maintenance");
  await page.waitForSelector('.server-view[data-view="maintenance"] .group-server-select, .server-view[data-view="maintenance"] .group-server-segmented', { visible: true });
  const chooseMaintServer = async (id) => {
    const sel = await page.$('.server-view[data-view="maintenance"] .group-server-select');
    if (sel) await page.select('.server-view[data-view="maintenance"] .group-server-select', id);
    else await page.evaluate((target) => {
      const m = [...document.querySelectorAll('.server-view[data-view="maintenance"] .group-server-segmented button')]
        .find((b) => b.title.includes(target));
      m?.click();
    }, id === "p-nav-2" ? "nav-db-2" : "nav-db-3");
  };
  await chooseMaintServer("p-nav-2");
  await page.evaluate(() => {
    [...document.querySelectorAll('.server-view[data-view="maintenance"] [aria-label="Maintenance task"] button')]
      .find((b) => b.textContent.trim() === "Dump")?.click();
  });
  await page.waitForSelector('.server-view[data-view="maintenance"] .ops-job[data-job="job-nav-2"][data-state="running"]');
  await chooseMaintServer("p-nav-3");
  await page.waitForSelector('.server-content > .ops-loop[data-maintenance-job="job-nav-2"]', { visible: true });
  await h.view("health");
  await page.waitForSelector('.server-content > .ops-loop[data-maintenance-job="job-nav-2"]', { visible: true });
  assert.match(await page.$eval('.server-content > .ops-loop[data-maintenance-job="job-nav-2"]', (e) => e.textContent),
    /Dump on nav-db-2 · 10\.0\.1\.12:3306 · SECONDARY is running\./);
  const cancelsBefore = lifecycleCancels.length;
  await page.click('.server-content > .ops-loop[data-maintenance-job="job-nav-2"] button.action-button');
  for (let i = 0; i < 100 && lifecycleCancels.length === cancelsBefore; i++) await pause(25);
  assert.equal(lifecycleCancels.at(-1).id, "job-nav-2");
  assert.equal(lifecycleCancels.at(-1).connection.profile_id, "p-nav-2", "Cancel targets the job's member p-nav-2");
  assert.equal(lifecycleCancels.at(-1).connection.group_name, "mf-nav");
  assert.equal(lifecycleCancels.at(-1).connection.host, "10.0.1.12");
  await page.waitForFunction(() => !document.querySelector('.server-content > .ops-loop[data-maintenance-job="job-nav-2"]'));

  assert.deepEqual(h.errors, [], "Group actions and replication branch changes produce no runtime exceptions");

  console.log(`topology-groups: ok (${writtenShots.length} screenshots: ${writtenShots.join(", ")})`);
} finally {
  await h.close();
}
