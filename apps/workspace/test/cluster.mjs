// The Cluster place: mocked /api/live/cluster answering cluster-1.0 reports
// that the real backend computed (cluster-fixtures.py: live recordings of a
// MariaDB 11.8 Galera, a Percona XtraDB Cluster 8.4 and a MySQL 8.4 InnoDB
// Cluster, states derived from them, and synthetic servers), and the real
// Python server for the session and saved connections. Covers: the place
// right after Replication, nothing read until it is on screen, one read on
// open, each kind (Galera, PXC, InnoDB Cluster with routers, multi-primary
// Group Replication), the topology (member cards, roles, "This connection",
// a degraded member drawn apart by a dashed edge, a broken link and a mark,
// not color alone; no animation), Members and Routers tables, the inspector
// per member and per router, findings, the not-a-cluster state and the
// sidebar item that looks disabled but stays focusable, an account that
// cannot read, connection binding (another connection's report greyed, the
// sidebar back to normal), Refresh, Refresh all feeding the place, keyboard,
// and the 720 px window.
import assert from "node:assert/strict";
import path from "node:path";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const R = {};
for (const name of ["galera-11.8-healthy", "galera-11.8-donor", "galera-11.8-two-of-three", "galera-11.8-non-primary", "pxc-8.4-healthy",
  "pxc-8.4-donor", "pxc-8.4-member-donor", "ic-8.4-healthy", "ic-8.4-degraded", "ic-8.4-unreachable", "ic-8.4-no-quorum",
  "ic-8.4-member-offline", "ic-8.4-local-offline", "gr-8.4-multi-primary", "gr-8.4-denied", "mysql-8.4-standalone",
  "mariadb-11.8-standalone"]) R[name] = await load(`cluster-${name}.json`);

const view = '.server-view[data-view="cluster"]';
const nav = '.main-nav button[data-place="cluster"]';
const h = await open("/api/live/cluster", "cluster");
const { page, state } = h;
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
const visible = (selector) => page.$$eval(selector, (els) => els.filter((e) => e.offsetParent || e.getClientRects().length).map((e) => e.textContent.replace(/\s+/g, " ").trim()));
const shot = (file) => page.screenshot({ path: path.join(h.shots, file), fullPage: false });
async function answer(report) {
  state.fallback = { status: 200, body: report };
}
async function refreshTo(report) {
  const before = state.seen.length;
  await answer(report);
  await h.refresh();
  await page.waitForFunction((n, sel) => !document.querySelector(`${sel} .spinner`), {}, before, view);
  for (let i = 0; i < 50 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh sends one read");
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line`), {}, view);
  await pause(150);
}
async function tab(label) {
  await page.evaluate((label, sel) => [...document.querySelectorAll(`${sel} .segmented button`)].find((b) => b.textContent.trim().startsWith(label)).click(), label, view);
  await pause(200);
}
async function backToList() {
  const back = await page.$(".view-inspector.is-detail .detail-back");
  if (back) { await back.click(); await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail")); }
}

try {
  await api("/api/profiles/save", { value: { name: "Node A", host: "node-a.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await api("/api/profiles/save", { value: { name: "Solo", host: "solo.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await page.reload({ waitUntil: "networkidle0" });

  // The place sits right after Replication and reads nothing until it is on screen.
  const places = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.equal(places[places.indexOf("replication") + 1], "cluster", `Cluster follows Replication: ${places}`);
  assert.equal(await page.$eval(nav, (b) => (b.childNodes[1]?.textContent || "").trim()), "Cluster");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "before the first read the item looks normal");
  await pause(400);
  assert.equal(state.seen.length, 0, "nothing is read before the place is opened");

  // InnoDB Cluster: one read on open, the summary, tiles, the topology.
  await answer(R["ic-8.4-healthy"]);
  await page.click(nav);
  await page.waitForSelector(`${view} .cluster-topology`);
  assert.equal(state.seen.length, 1, "opening the place reads once");
  assert.ok(state.seen[0].connection, "the read carries the connection in use");
  assert.match((await visible(`${view} .summary-line`))[0], /MySQL InnoDB Cluster mfcluster · 3 of 3 members ONLINE · single-primary/);
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Members", "Routers1"]);
  const tiles = await visible(`${view} .tile-label`);
  assert.deepEqual(tiles, ["Quorum", "Members", "This member", "Mode"]);
  const cards = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => ({
    label: e.getAttribute("aria-label"), local: e.classList.contains("is-local"), role: e.querySelector(".cluster-role")?.textContent,
    state: e.querySelector(".cluster-state")?.textContent, flow: e.querySelector(".cluster-flow")?.textContent })));
  assert.equal(cards.length, 3);
  assert.equal(cards.filter((c) => c.local).length, 1, "one card is this connection");
  assert.deepEqual(cards.map((c) => c.role).sort(), ["Primary", "Secondary", "Secondary"]);
  assert.ok(cards.every((c) => c.state === "ONLINE" && /^Queue \d+ · applier \d+$/.test(c.flow)), JSON.stringify(cards));
  assert.equal(await page.$$eval(`${view} .cluster-card.is-local .cluster-local`, (els) => els.map((e) => e.textContent).join()), "This connection");
  assert.match(await h.text(`${view} .cluster-bus-label`), /Single-primary group · quorum · tolerates 1 failure/);
  assert.equal(await page.$$eval(`${view} .cluster-routers .cluster-card`, (els) => els.length), 1, "the router hangs under the group");
  // Static: nothing in the picture animates.
  const animated = await page.$$eval(`${view} .cluster-topology *`, (els) => els.filter((e) => {
    const s = getComputedStyle(e);
    return s.animationName !== "none" && s.animationName !== "";
  }).length);
  assert.equal(animated, 0, "no animation in the topology");
  const info = await h.popover("About Cluster");
  assert.match(info, /Read-only in this release/);
  assert.match(info, /MariaDB Galera Cluster, Percona XtraDB Cluster, MySQL InnoDB Cluster/);
  await shot("cluster-ic-topology.png");
  assert.equal(await page.$eval(nav, (b) => [...b.querySelectorAll(".status-dot")].filter((d) => d.getClientRects().length).length), 0, "OK draws no marker");
  assert.match(await page.$eval(nav, (b) => b.textContent), /cluster OK/, "the state still reaches screen readers");

  // Keyboard: arrows move between member cards and select; Enter opens the inspector; Escape returns.
  await page.focus(`${view} .cluster-members .cluster-card`);
  await page.keyboard.press("ArrowRight");
  const second = await page.evaluate(() => document.activeElement?.getAttribute("data-row-key"));
  assert.ok(second && second.startsWith("member:"), "ArrowRight moves to the next card");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".view-inspector .inspector-title");
  const inspector = await h.inspector();
  assert.match(inspector, /performance_schema\.replication_group_members/);
  assert.match(inspector, /Group Replication counters/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction((key) => document.activeElement?.getAttribute("data-row-key") === key, {}, second);

  // Routers table and its inspector.
  await tab("Routers");
  await page.waitForSelector(`${view} [data-row-key^="router:"]`);
  const routerRow = await visible(`${view} tbody tr`);
  assert.match(routerRow[0], /mf-ic-router.*MySQL Router 8\.4\.10.*6446.*6447.*6450/);
  await h.select(`${view} tr[data-row-key^="router:"]`);
  assert.match(await h.inspector(), /MySQL Router writes its check-in time periodically.*It is shown, not judged/);
  await backToList();
  await tab("Topology");

  // A degraded group: the unreachable member is drawn apart (dashed edge,
  // broken link, warning mark and the word), and findings name it.
  await refreshTo(R["ic-8.4-degraded"]);
  const degraded = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => ({
    state: e.querySelector(".cluster-state")?.textContent, detached: e.classList.contains("is-detached"),
    border: getComputedStyle(e).borderTopStyle, mark: !!e.querySelector(".cluster-state svg"),
    stem: e.parentElement.querySelector(".cluster-stem")?.classList.contains("is-broken") })));
  const unreachable = degraded.find((c) => c.state === "UNREACHABLE");
  assert.deepEqual(unreachable, { state: "UNREACHABLE", detached: true, border: "dashed", mark: true, stem: true });
  const recovering = degraded.find((c) => c.state === "RECOVERING");
  assert.equal(recovering.detached, false);
  assert.equal(recovering.border, "solid");
  assert.ok(degraded.filter((c) => c.state === "ONLINE").every((c) => !c.detached && c.border === "solid"));
  const findings = await visible(`${view} .cluster-findings li`);
  assert.ok(findings.some((f) => /^Problem.*is UNREACHABLE/.test(f)), findings.join(" | "));
  assert.ok(findings.some((f) => /Cannot tolerate a failure/.test(f)));
  assert.match((await visible(`${view} .summary-line`))[0], /^Problem/);
  assert.ok(await page.$(`${nav} .status-dot.is-problem`), "the sidebar names the problem");
  assert.match(await page.$eval(nav, (b) => b.textContent), /problem/);
  await shot("cluster-ic-degraded.png");
  // Members: state pills with words; the inspector says why.
  await tab("Members");
  const memberRows = await visible(`${view} tbody tr`);
  assert.equal(memberRows.length, 3);
  assert.ok(memberRows.some((r) => /UNREACHABLE/.test(r)) && memberRows.some((r) => /RECOVERING/.test(r)));
  const recoveringKey = R["ic-8.4-degraded"].members.find((m) => m.state === "RECOVERING").key;
  await h.select(`${view} tr[data-row-key="${recoveringKey}"]`);
  assert.match(await h.inspector(), /Queues: 4 in certification, 12 in the applier\./);
  await backToList();
  await shot("cluster-ic-members.png");

  // No quorum, a MISSING instance, this member OFFLINE.
  await tab("Topology");
  await refreshTo(R["ic-8.4-no-quorum"]);
  assert.ok(await page.$(`${view} .cluster-bus.is-broken`), "the group line is broken without quorum");
  assert.match(await h.text(`${view} .cluster-bus-label`), /no quorum/);
  await refreshTo(R["ic-8.4-member-offline"]);
  assert.ok((await visible(`${view} .cluster-card.is-detached .cluster-state`)).includes("MISSING"));
  // Recorded live: a paused member is UNREACHABLE and keeps its role badge.
  await refreshTo(R["ic-8.4-unreachable"]);
  const paused = await page.$$eval(`${view} .cluster-card.is-detached`, (els) => els.map((e) => [e.querySelector(".cluster-role")?.textContent, e.querySelector(".cluster-state")?.textContent]));
  assert.deepEqual(paused, [["Secondary", "UNREACHABLE"]]);
  await shot("cluster-ic-unreachable.png");
  await refreshTo(R["ic-8.4-local-offline"]);
  assert.match(await h.text(`${view} .cluster-bus-label`), /quorum not visible from here/);
  assert.ok((await visible(`${view} .cluster-card.is-local.is-detached .cluster-state`)).includes("OFFLINE"));
  assert.equal((await visible(`${view} .cluster-card:not(.is-local) .cluster-state`)).filter((s) => s === "Not visible here").length, 2);

  // Multi-primary Group Replication without metadata: every member PRIMARY, no Routers tab.
  await refreshTo(R["gr-8.4-multi-primary"]);
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Members"]);
  assert.deepEqual(await visible(`${view} .cluster-members .cluster-role`), ["Primary", "Primary", "Primary"]);
  assert.match((await visible(`${view} .summary-line`))[0], /MySQL Group Replication · 3 of 3 members ONLINE · multi-primary/);

  // MariaDB Galera: only this node is judged; the others are listed without a verdict.
  await refreshTo(R["galera-11.8-healthy"]);
  assert.match((await visible(`${view} .summary-line`))[0], /MariaDB Galera Cluster mf_galera · Primary component · 3 nodes · this node Synced/);
  const galera = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => ({
    local: e.classList.contains("is-local"), state: e.querySelector(".cluster-state")?.textContent, meta: [...e.querySelectorAll(".cluster-meta")].map((m) => m.textContent) })));
  assert.deepEqual(galera.map((c) => c.state).sort(), ["Not judged here", "Not judged here", "Synced"]);
  assert.ok(galera.find((c) => c.local).meta.some((m) => /^MariaDB 11\.8\.9/.test(m)), JSON.stringify(galera));
  assert.ok(galera.every((c) => c.meta.some((m) => /port not reported/.test(m))), "port 0 is not shown as a port");
  assert.deepEqual(await visible(`${view} .tile-label`), ["Component", "Nodes", "This node", "Flow control paused"]);
  assert.match(await h.text(`${view} .cluster-bus-label`), /Primary component · 3 of 3 nodes · flow control paused 0% on this node/);
  await shot("cluster-galera-topology.png");
  await tab("Members");
  const galeraCols = await visible(`${view} thead th`);
  for (const col of ["Recv queue", "Send queue", "Flow control paused", "Cert failures"]) assert.ok(galeraCols.some((c) => c.startsWith(col)), `${col} in ${galeraCols}`);
  await tab("Topology");
  await refreshTo(R["galera-11.8-donor"]);
  const donor = await page.$$eval(`${view} .cluster-card.is-local`, (els) => els.map((e) => ({ state: e.querySelector(".cluster-state").textContent,
    watch: e.classList.contains("tone-watch"), flow: e.querySelector(".cluster-flow").textContent })));
  assert.deepEqual(donor, [{ state: "Donor/Desynced", watch: true, flow: "Recv queue 0 · send 0 · flow control paused 0%" }]);
  await refreshTo(R["galera-11.8-two-of-three"]);
  assert.match(await h.text(`${view} .cluster-bus-label`), /Primary component · 2 of 3 nodes/);
  assert.ok((await visible(`${view} .cluster-findings li`)).some((f) => /Fewer nodes than configured/.test(f)));
  await refreshTo(R["galera-11.8-non-primary"]);
  assert.ok(await page.$(`${view} .cluster-bus.is-broken`));
  assert.match(await h.text(`${view} .cluster-bus-label`), /non-Primary component · 1 of 3 nodes/);

  // Percona XtraDB Cluster: every node's state comes from pxc_cluster_view.
  await refreshTo(R["pxc-8.4-healthy"]);
  assert.deepEqual(await visible(`${view} .cluster-members .cluster-state`), ["Synced", "Synced", "Synced"]);
  assert.ok((await visible(`${view} .cluster-members .cluster-meta`)).some((m) => /^Percona XtraDB Cluster 8\.4\.11/.test(m)));
  await refreshTo(R["pxc-8.4-member-donor"]);
  assert.ok((await visible(`${view} .cluster-card.tone-watch .cluster-state`)).includes("Donor"));
  await refreshTo(R["pxc-8.4-donor"]);
  assert.deepEqual(await visible(`${view} .cluster-card.is-local .cluster-state`), ["Donor/Desynced"]);
  await shot("cluster-pxc-topology.png");

  // An account that cannot read: one sentence, no muted sidebar.
  await refreshTo(R["gr-8.4-denied"]);
  assert.match(await h.text(`${view} .gate`), /cannot read performance_schema\.replication_group_members/);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");

  // Not part of a cluster: a calm state naming the three kinds, and a muted
  // sidebar item that stays focusable and opens the place.
  await refreshTo(R["mysql-8.4-standalone"]);
  assert.equal(await h.text(`${view} .view-empty h3`), "Not part of a cluster");
  const calm = await h.text(`${view} .view-empty p`);
  assert.match(calm, /MariaDB Galera Cluster, Percona XtraDB Cluster and MySQL InnoDB Cluster/);
  assert.match(calm, /^This server has no Galera provider, and Group Replication is not installed or not running\./);
  assert.equal(await page.$(`${view} .gate`), null, "not a warning");
  assert.equal(await page.$$eval(`${view} .segmented`, (els) => els.length), 0, "no sections without a cluster");
  const muted = await page.$eval(nav, (b) => ({ muted: b.dataset.muted, title: b.title, cls: b.className, disabled: b.disabled,
    tab: b.tabIndex, text: b.textContent }));
  assert.equal(muted.muted, "true");
  assert.equal(muted.title, "This connection is not part of a cluster");
  assert.equal(muted.disabled, false);
  assert.equal(muted.tab, 0);
  assert.match(muted.text, /This connection is not part of a cluster/, "the reason reaches screen readers");
  await shot("cluster-not-a-cluster.png");
  // Away from the place the item looks disabled (faint text and icon), yet Tab reaches it and Enter opens the place.
  await page.click('.main-nav button[data-place="home"]');
  await pause(300);
  const look = await page.$eval(nav, (b) => {
    const probe = document.createElement("span");
    probe.style.color = "var(--ink-faint)";
    document.body.append(probe);
    const faint = getComputedStyle(probe).color;
    probe.remove();
    return { color: getComputedStyle(b).color, faint, icon: getComputedStyle(b.querySelector("svg")).color };
  });
  assert.equal(look.color, look.faint, "muted text");
  assert.equal(look.icon, look.faint, "muted icon");
  await shot("cluster-sidebar-muted-away.png");
  await page.focus('.main-nav button[data-place="replication"]');
  await page.keyboard.press("ArrowDown");
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.place), "cluster");
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${nav}.active`);
  await page.waitForFunction((sel) => !!document.querySelector(`${sel} .view-empty`)?.offsetParent, {}, view);
  await page.mouse.move(900, 600);
  await pause(200);
  // On screen it reads as the current place, not as disabled.
  const here = await page.$eval(nav, (b) => {
    const probe = document.createElement("span");
    probe.style.background = "var(--selection-fill)";
    probe.style.color = "var(--selection-ink)";
    document.body.append(probe);
    const want = { bg: getComputedStyle(probe).backgroundColor, ink: getComputedStyle(probe).color };
    probe.remove();
    return { bg: getComputedStyle(b).backgroundColor, ink: getComputedStyle(b).color, want };
  });
  assert.deepEqual({ bg: here.bg, ink: here.ink }, here.want, "the open Cluster place looks current");
  await shot("cluster-sidebar-muted.png");
  await refreshTo(R["mariadb-11.8-standalone"]);
  assert.match(await h.text(`${view} .view-empty p`), /wsrep_on is OFF/);

  // Connection binding: on another connection the report is greyed until
  // Refresh, and the sidebar no longer claims anything about it.
  await refreshTo(R["galera-11.8-healthy"]);
  const before = state.seen.length;
  const nodeA = await connectionOption(page, "Node A");
  await chooseConnection(page, nodeA);
  await page.waitForFunction((sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  assert.match(await h.text(`${view} .other-connection`), /From .*refresh to load this server/);
  assert.equal(await page.$(`${nav} .status-dot`), null, "no marker from another connection");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");
  await pause(300);
  assert.equal(state.seen.length, before, "switching while on screen keeps the old report until Refresh");
  await refreshTo(R["mysql-8.4-standalone"]);
  assert.equal(state.seen.at(-1).connection.host, "node-a.test");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true");
  // Another connection: the Cluster item is normal again (not read yet).
  const solo = await connectionOption(page, "Solo");
  await chooseConnection(page, solo);
  await pause(300);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "unknown for a connection not read yet");
  // Leaving and coming back on screen with another connection's report reads once.
  await page.click('.main-nav button[data-place="home"]');
  await pause(300);
  const reads = state.seen.length;
  await answer(R["pxc-8.4-healthy"]);
  await page.click(nav);
  await page.waitForFunction((sel) => /Percona XtraDB Cluster/.test(document.querySelector(`${sel} .summary-line`)?.textContent || ""), {}, view);
  assert.equal(state.seen.length, reads + 1, "opening with a report from another connection reads once");
  assert.equal(state.seen.at(-1).connection.host, "solo.test");

  // Until now the Cluster place read nothing but /api/live/cluster.
  assert.deepEqual(state.other.filter((o) => o.path.startsWith("/api/live/")).map((o) => o.path), [], "no other view read");
  // Server's Refresh all reads the cluster once, and the place shows that answer.
  await answer(R["mysql-8.4-standalone"]);
  const beforeAll = state.seen.length;
  await h.server();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.click(".server-place [data-action=refresh-all]");
  await page.waitForFunction(() => !document.querySelector(".server-place .refresh-all-stop") && /refreshed/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""), { timeout: 20000 });
  assert.equal(state.seen.length, beforeAll + 1, "Refresh all sends one cluster read");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true", "Refresh all tells the sidebar");
  await page.click(nav);
  await page.waitForFunction((sel) => !!document.querySelector(`${sel} .view-empty`)?.offsetParent, {}, view);
  await pause(300);
  assert.equal(state.seen.length, beforeAll + 1, "opening Cluster after Refresh all reads nothing more");

  // The app's 720 px minimum window: no page scroll sideways; the picture
  // scrolls inside its frame; the sections stay reachable.
  await refreshTo(R["ic-8.4-degraded"]);
  await page.setViewport({ width: 720, height: 520 });
  await pause(400);
  const narrow = await page.evaluate((sel) => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    tabs: [...document.querySelectorAll(`${sel} .segmented button`)].filter((b) => b.offsetParent).length,
    frame: (() => { const f = document.querySelector(`${sel} .cluster-topology`); return f ? getComputedStyle(f).overflowX : null; })(),
  }), view);
  assert.ok(narrow.overflow <= 0, `horizontal page scroll at 720 px: ${narrow.overflow}`);
  assert.equal(narrow.tabs, 3);
  assert.equal(narrow.frame, "auto");
  await shot("cluster-720.png");
  await tab("Members");
  await pause(200);
  assert.ok((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0);
  await page.setViewport({ width: 1440, height: 1000 });
  await h.narrow("cluster-390.png", { place: ".cluster-place" });

  assert.deepEqual(h.errors, []);
  console.log("cluster: ok");
} finally {
  await h.close();
}
