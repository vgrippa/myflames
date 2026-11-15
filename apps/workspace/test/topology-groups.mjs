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
import assert from "node:assert/strict";
import { chooseConnection, connectionOption, layoutMatrix, openConnectionPicker } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const clusterBase = await load("cluster-ic-8.4-healthy.json");
const replicationBase = await load("replication-mysql-8.4-healthy.json");
const proxysqlBase = await load("proxysql-lab.json");

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
let proxysqlReport = structuredClone(proxysqlBase);

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
  "/api/live/replication": (body) => {
    replicationReads.push(body);
    return { status: 200, body: replicationReport };
  },
  "/api/live/replication/skip-loop/status": () => ({
    status: 200,
    body: { schema_version: "replication-skip-loop-1.0", loops: [] },
  }),
  "/api/live/proxysql": (body) => {
    proxysqlReads.push(body);
    return { status: 200, body: proxysqlReport };
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
  assert.deepEqual(h.errors, [], "Group actions and replication branch changes produce no runtime exceptions");

  console.log(`topology-groups: ok (${writtenShots.length} screenshots: ${writtenShots.join(", ")})`);
} finally {
  await h.close();
}
