// Group connections: a connection group opens as one Group connection from
// the connection picker (an explicit open, probe_members: true), the top bar
// and the status bar name it, and one group editor sheet (reachable from the
// picker, the status bar and the Connections place) renames a group, adds and
// removes members and deletes the group while its saved connections stay. A
// save that removes the active member moves the Group connection to the
// member the backend picks.
// The real workspace server on a temporary store (vault backend, throwaway
// HOME); /api/connect and /api/profiles/* are mocked from a mutable profiles
// list, server views are refused by the harness. Run after `npm run build`:
// node test/group-connections.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { load, open, pause } from "./perf-harness.mjs";
import { layoutMatrix, openConnectionPicker } from "./chrome-helpers.mjs";

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
const healthCalls = [];
const editorRuns = [];
const healthFixture = await load("health-mysql-8.4.json");

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
  "/api/profiles/list": () => ({
    status: 200,
    body: {
      items: profiles,
      secret_store: { backend: "vault", label: "myflames encrypted vault", available: true },
    },
  }),
  "/api/connect": (body) => {
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
    const type = members.some((m) => m.type !== "development") ? "production" : "development";
    return {
      status: 200,
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
    healthCalls.push({ profile: body.connection?.profile_id || "", group: body.connection?.group_name || "" });
    return { status: 200, body: healthFixture };
  },
  // Editor runs never reach a server here: refused, but recorded so a tab's
  // member binding shows in the request (its profile id and the group name).
  "/api/editor/run": (body) => {
    editorRuns.push(body);
    return { status: 200, body: { status: "refused", message: "Runs are mocked in this suite." } };
  },
});
const { page } = h;
await fs.mkdir(h.shots, { recursive: true });
const pillText = () => page.$eval(".window-toolbar .connection-pill", (e) => e.textContent.replace(/\s+/g, " ").trim());
const statusText = () => page.$eval(".window-statusbar", (e) => e.textContent.replace(/\s+/g, " ").trim());
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
  assert.equal(chosen, "p-ic-2", "the primary member is the connection in use");
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
  for (let i = 0; i < 100 && !healthCalls.length; i++) await pause(100);
  assert.ok(healthCalls.length >= 1, "Health read on open");
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
  await page.setViewport({ width: 1440, height: 900 });
  await page.waitForSelector('.server-view[data-view="health"] .group-server-segmented', { visible: true });

  // --- Switching the picker: an explicit login to that member; automatic
  // refresh (While visible, the default) only ever logs in to the shown member.
  await page.click('.server-view[data-view="health"] .group-server-segmented button[data-value="p-ic-1"]');
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="health"] .view-server-name')?.textContent.includes("cluster-ic-1"));
  assert.equal(await headerName(), "cluster-ic-1 · 10.0.0.11:3306 · SECONDARY", "the header follows the chosen member");
  const sinceSwitch = healthCalls.length;
  // The previous member's report stays, greyed, until an explicit Refresh:
  // that read is the login to the chosen member.
  await h.refresh();
  for (let i = 0; i < 100 && !healthCalls.slice(sinceSwitch).some((c) => c.profile === "p-ic-1"); i++) await pause(100);
  assert.ok(healthCalls.slice(sinceSwitch).some((c) => c.profile === "p-ic-1"), "refreshing after the switch reads the chosen member");
  assert.equal(connectCalls.length, 1, "switching the picker is no group re-open; the view read is the login");
  // One more automatic read lands while the view stays visible (5 s cadence).
  const beforeAuto = healthCalls.length;
  for (let i = 0; i < 100 && healthCalls.length === beforeAuto; i++) await pause(100);
  assert.ok(healthCalls.length > beforeAuto, "automatic refresh read again while visible");
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
  for (let i = 0; i < 100 && !editorRuns.length; i++) await pause(100);
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
  assert.equal(await page.$eval('.picker-panel select[aria-label="Connection"]', (e) => e.value), "p-ic-1");
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
  const runsBeforeRefusal = editorRuns.length;
  await page.click('.editor-toolbar [data-action="run"]');
  await page.waitForFunction(() => document.body.textContent.includes("Pick a server for this tab"));
  assert.equal(editorRuns.length, runsBeforeRefusal, "a removed member's tab sends nothing");
  // Picking a member rebinds the tab; it runs on that member.
  await page.select(".group-member-rebind", "p-ic-3");
  await page.waitForFunction(() => !document.querySelector(".group-member-note"));
  assert.ok((await tabLabels()).includes("Query 2 · cluster-ic-3"), "the tab names the member it was moved to");
  await page.click('.editor-toolbar [data-action="run"]');
  for (let i = 0; i < 100 && editorRuns.length === runsBeforeRefusal; i++) await pause(100);
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

  // --- Delete the group: its saved connections stay.
  await page.click(`${sheet} .group-editor-delete`);
  await page.waitForSelector("dialog.group-editor-sheet[open] .sheet-buttons button.danger-solid", { visible: true });
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

  console.log("group-connections: all checks passed");
} finally {
  await h.close();
}
