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
  "mariadb-11.8-standalone", "pxc-8.4-two-of-three", "galera-11.8-two-nodes", "galera-11.8-partial-seeds", "galera-mysql-8.4-healthy",
  "galera-11.8-wsrep-off", "ic-8.4-read-replica", "ic-8.4-read-replica-local", "plan-switchover", "result-switchover",
  "progress-sending"]) R[name] = await load(`cluster-${name}.json`);

const view = '.server-view[data-view="cluster"]';
const nav = '.main-nav button[data-place="cluster"]';
const plans = [];
const sends = [];
const declined = [];
const h = await open("/api/live/cluster", "cluster", {
  "/api/live/cluster/plan": (body) => { plans.push(body); return { status: 200, body: R["plan-switchover"] }; },
  "/api/live/cluster/action": (body) => { sends.push(body); return { status: 200, body: R["result-switchover"], delay: 1500 }; },
  "/api/live/action/progress": () => ({ status: 200, body: R["progress-sending"] }),
  "/api/audit/declined": (body) => { declined.push(body); return { status: 200, body: { audit_id: "x", status: "declined" } }; },
});
const { page, state } = h;
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
/** The findings with their Details state, in order. */
const findingStates = () => page.$$eval(`${view} .cluster-findings li`, (lis) => lis.map((li) => ({
  title: li.querySelector(".cluster-finding-title").textContent,
  expanded: li.querySelector(".cluster-finding-toggle").getAttribute("aria-expanded") })));
async function toggleFinding(pattern) {
  const at = (await findingStates()).findIndex((f) => pattern.test(f.title));
  assert.ok(at >= 0, `a finding matches ${pattern}`);
  await page.click(`${view} .cluster-findings li:nth-child(${at + 1}) .cluster-finding-toggle`);
}
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
  await api("/api/profiles/save", { value: { name: "cluster-galera-2", host: "192.168.80.3", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
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
    state: e.querySelector(".cluster-mark")?.textContent, flow: e.querySelector(".cluster-flow")?.textContent })));
  assert.equal(cards.length, 3);
  assert.equal(cards.filter((c) => c.local).length, 1, "one card is this connection");
  assert.deepEqual(cards.map((c) => c.role).sort(), ["Primary", "Secondary", "Secondary"]);
  assert.ok(cards.every((c) => c.state === "ONLINE" && /^Queue \d+ · applier \d+$/.test(c.flow)), JSON.stringify(cards));
  assert.equal(await page.$$eval(`${view} .cluster-card.is-local .cluster-local`, (els) => els.map((e) => e.textContent).join()), "This connection");
  // Every card's lines start at the same height, the local one included.
  const nameTops = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => e.querySelector(".cluster-name").getBoundingClientRect().top - e.getBoundingClientRect().top));
  assert.equal(new Set(nameTops).size, 1, `name rows line up: ${nameTops}`);
  assert.match(await h.text(`${view} .cluster-bus-label`), /Single-primary group · quorum · tolerates 1 failure/);
  assert.equal(await page.$$eval(`${view} .cluster-routers .cluster-card`, (els) => els.length), 1, "the router hangs under the group");
  // Static: nothing in the picture animates.
  const animated = await page.$$eval(`${view} .cluster-topology *`, (els) => els.filter((e) => {
    const s = getComputedStyle(e);
    return s.animationName !== "none" && s.animationName !== "";
  }).length);
  assert.equal(animated, 0, "no animation in the topology");
  const info = await h.popover("About Cluster");
  assert.match(info, /Opening it reads; nothing changes/);
  assert.match(info, /asks first, and is recorded in the Audit log/);
  assert.match(info, /MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster, MySQL InnoDB Cluster/);
  assert.match(info, /OK: quorum, and every expected member present and healthy/);
  // The healthy summary reads like Watch and Problem: a shape and the word, inline.
  assert.notEqual(await page.$eval(`${view} .summary-line .status-pill.is-ok .status-dot`, (d) => getComputedStyle(d).display), "none");
  // d06: OK member cards draw a green dot instead of a checkmark.
  assert.equal(await page.$eval(`${view} .cluster-mark.is-ok .cluster-shape`, (d) => getComputedStyle(d).borderRadius), "50%");
  // d19.2: window footer shows server version in Cluster.
  assert.match(await page.$eval(".window-statusbar", (el) => el.textContent || ""), /MySQL 8\.4/);
  // A name equal to its address is announced once.
  assert.ok(cards.every((c) => c.label.split("mf-ic-").length === 2), JSON.stringify(cards.map((c) => c.label)));
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

  // Each table keeps its own column layout, saved with the workspace:
  // hiding Version among Members leaves the Routers' Version, and the
  // layout survives Topology and a reload.
  const membersTable = `${view} .data-table-wrap[aria-label="Cluster members"]`;
  const routersTable = `${view} .data-table-wrap[aria-label="Routers"]`;
  const columnKeys = (t) => page.$$eval(`${t} thead th[data-col]`, (els) => els.map((e) => e.dataset.col));
  const membersMenu = '[role=dialog][aria-label="Columns of Cluster members"]';
  await tab("Members");
  await page.waitForSelector(`${membersTable} thead th[data-col="version"]`);
  await page.click(`${membersTable} .table-columns-button`);
  await page.waitForSelector(membersMenu);
  await page.click(`${membersMenu} input[data-col="version"]`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label^="Columns of"]'));
  assert.ok(!(await columnKeys(membersTable)).includes("version"), "Version hidden among Members");
  await tab("Routers");
  await page.waitForSelector(`${routersTable} thead th[data-col]`);
  assert.ok((await columnKeys(routersTable)).includes("version"), "Routers keep their own Version column");
  await tab("Topology");
  await tab("Members");
  assert.ok(!(await columnKeys(membersTable)).includes("version"), "the Members layout survives a visit to Topology");
  const savedColumns = await h.savedSlow((s) => (s.columns_v1?.["cluster.members"]?.hidden || []).includes("version"));
  assert.equal(savedColumns.state.query_lab.slow.columns_v1["cluster.routers"], undefined, "Routers have no layout of their own yet");
  await page.reload({ waitUntil: "networkidle0" });
  await answer(R["ic-8.4-healthy"]);
  await page.click(nav);
  await page.waitForSelector(`${view} .cluster-topology`);
  await tab("Members");
  await page.waitForSelector(`${membersTable} thead th[data-col]`);
  assert.ok(!(await columnKeys(membersTable)).includes("version"), "the Members layout is restored after a reload");
  await page.click(`${membersTable} .table-columns-button`);
  await page.waitForSelector(membersMenu);
  await page.click(`${membersMenu} .columns-reset`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label^="Columns of"]'));
  assert.ok((await columnKeys(membersTable)).includes("version"), "Reset columns shows Version again");
  await h.savedSlow((s) => !s.columns_v1?.["cluster.members"]);
  await tab("Topology");

  // Routers table and its inspector.
  await tab("Routers");
  await page.waitForSelector(`${view} [data-row-key^="router:"]`);
  const routerRow = await visible(`${view} tbody tr`);
  assert.match(routerRow[0], /mf-ic-router.*MySQL Router 8\.4\.10.*6446.*6447.*6450/);
  await h.select(`${view} tr[data-row-key^="router:"]`);
  assert.match(await h.inspector(), /MySQL Router writes its check-in time periodically.*It is shown, not judged/);
  await backToList();

  // Changes live in the member's inspector, one home: Make primary on an
  // ONLINE secondary. The review shows the exact statement with the
  // member's UUID; Escape records a declined attempt and sends nothing.
  await tab("Members");
  const secondary = R["ic-8.4-healthy"].members.find((m) => m.key === R["plan-switchover"].member_key);
  await h.select(`${view} tr[data-row-key="${secondary.key}"]`);
  const memberButtons = await page.$$eval(".view-inspector .review-actions button", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(memberButtons, secondary.actions.map((a) => a.label));
  assert.ok(memberButtons.includes("Make primary…"), memberButtons.join());
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions button")].find((b) => b.textContent.trim() === "Make primary…").click());
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.deepEqual({ action: plans[0].action, member: plans[0].member_key }, { action: "switchover", member: secondary.key });
  const sheet = await page.$eval("dialog.action-review[open]", (d) => ({ eyebrow: d.querySelector(".eyebrow").textContent,
    sql: d.querySelector(".ops-pre code").textContent, confirm: d.querySelector(".sheet-actions button:last-child").textContent }));
  assert.equal(sheet.eyebrow, "SELECT group_replication_set_as_primary");
  assert.equal(sheet.sql, `/*myflames:cluster_op*/ SELECT group_replication_set_as_primary('${secondary.uuid}');`);
  assert.equal(sheet.confirm, `Make ${secondary.name} primary`);
  await pause(400);
  await shot("cluster-review.png");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  for (let i = 0; i < 50 && !declined.length; i++) await pause(20);
  assert.deepEqual({ op: declined[0]?.operation, statements: declined[0]?.statements }, { op: "cluster_action", statements: R["plan-switchover"].statements });
  assert.equal(sends.length, 0, "nothing sent on Escape");
  // Confirm: the same sheet follows the send (the shared review, ActionReview.tsx):
  // Sending… on a button that cannot be clicked twice, the statement Sent
  // until the server answers, then Done, what changed and Close.
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions button")].find((b) => b.textContent.trim() === "Make primary…").click());
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  await page.click("dialog.action-review[open] .sheet-actions button:last-child");
  await page.waitForFunction(() => /Sent\. Waiting for the server's answer/.test(document.querySelector("dialog.action-review[open] .action-phase")?.textContent || ""));
  const applying = await page.$eval("dialog.action-review[open]", (d) => ({ confirm: d.querySelector(".sheet-actions button:last-child").textContent,
    ariaDisabled: d.querySelector(".sheet-actions button:last-child").getAttribute("aria-disabled"), steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state) }));
  assert.deepEqual(applying, { confirm: "Sending…", ariaDisabled: "true", steps: ["sent"] });
  await page.waitForSelector("dialog.action-review[open] .action-outcome");
  const answered = await page.$eval("dialog.action-review[open]", (d) => ({ outcome: d.querySelector(".action-outcome").textContent,
    steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state),
    effect: [...d.querySelectorAll(".action-effect tbody th")].map((e) => e.textContent) }));
  assert.match(answered.outcome, /^Done/);
  assert.deepEqual(answered.steps, ["done"]);
  assert.deepEqual(answered.effect, ["Primary"]);
  assert.equal(sends.length, 1);
  assert.match(sends[0].progress_id, /^[0-9a-f]{32}$/);
  await page.evaluate(() => [...document.querySelectorAll("dialog.action-review[open] .sheet-actions button")].find((b) => b.textContent.trim() === "Close").click());
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  await page.waitForSelector(`${view} .action-result`);
  assert.match(await page.$eval(`${view} .action-result`, (e) => e.textContent), /Done/);
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .action-result .text-button`)].find((b) => b.textContent.trim() === "Close").click(), view);
  await backToList();
  // No Management tab: the sections stay the cluster's.
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Members", "Routers1"]);
  await tab("Topology");

  // A degraded group: the unreachable member is drawn apart (dashed edge,
  // broken link, warning mark and the word), and findings name it.
  await refreshTo(R["ic-8.4-degraded"]);
  const degraded = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => ({
    state: e.querySelector(".cluster-mark")?.textContent, detached: e.classList.contains("is-detached"),
    border: getComputedStyle(e).borderTopStyle, mark: !!e.querySelector(".cluster-mark.is-watch .cluster-shape"),
    stem: e.parentElement.querySelector(".cluster-stem")?.classList.contains("is-broken") })));
  const unreachable = degraded.find((c) => c.state === "UNREACHABLE");
  assert.deepEqual(unreachable, { state: "UNREACHABLE", detached: true, border: "dashed", mark: true, stem: true });
  const recovering = degraded.find((c) => c.state === "RECOVERING");
  assert.equal(recovering.detached, false);
  assert.equal(recovering.border, "solid");
  assert.ok(degraded.filter((c) => c.state === "ONLINE").every((c) => !c.detached && c.border === "solid"));
  const findings = await visible(`${view} .cluster-findings li`);
  // One scale: UNREACHABLE and RECOVERING with quorum are Watch.
  assert.ok(findings.some((f) => /^Watch.*is UNREACHABLE/.test(f)), findings.join(" | "));
  assert.ok(findings.some((f) => /Cannot survive another failure/.test(f)));
  assert.match((await visible(`${view} .summary-line`))[0], /^Watch/);
  assert.ok(await page.$(`${nav} .status-dot.is-watch`), "the sidebar names the state");
  assert.match(await page.$eval(nav, (b) => b.textContent), /to watch/);
  // Not OK: the findings come first, above the tiles; one line each, the
  // title in foreground text with only the member name linked, the reason on demand.
  const order = await page.$eval(`${view} .view-main`, (m) => [...m.querySelectorAll(".cluster-findings, .cluster-tiles")].map((e) => e.className));
  assert.deepEqual(order.map((c) => c.split(" ")[0]), ["cluster-findings", "cluster-tiles"]);
  const row = await page.$$eval(`${view} .cluster-findings li`, (lis) => lis.map((li) => ({
    link: li.querySelector(".cluster-finding-title .text-button")?.textContent || null,
    title: li.querySelector(".cluster-finding-title").textContent, hidden: li.querySelector(".cluster-finding-text").hidden })));
  assert.ok(row.every((r) => r.hidden), "reasons are on demand");
  const unreachableRow = row.find((r) => /UNREACHABLE/.test(r.title));
  assert.equal(unreachableRow.link, "mf-ic-3:3306");
  assert.equal(unreachableRow.title, "mf-ic-3:3306 is UNREACHABLE");
  await page.click(`${view} .cluster-findings li:last-child .cluster-finding-toggle`);
  assert.equal(await page.$eval(`${view} .cluster-findings li:last-child .cluster-finding-text`, (e) => e.hidden), false);
  // Details stay with their finding, never its position: with RECOVERING's
  // open, a report where UNREACHABLE takes that place opens nothing.
  await page.click(`${view} .cluster-findings li:last-child .cluster-finding-toggle`);
  await toggleFinding(/is RECOVERING$/);
  assert.deepEqual((await findingStates()).filter((f) => f.expanded === "true").map((f) => f.title), ["mf-ic-2:3306 is RECOVERING"]);
  await refreshTo(R["ic-8.4-unreachable"]);
  assert.deepEqual((await findingStates()).filter((f) => f.expanded === "true"), [], "no other finding opens in its place");
  await refreshTo(R["ic-8.4-degraded"]);
  await toggleFinding(/is RECOVERING$/);
  // Degraded tiles carry their tone: the quorum tile when one more failure loses it.
  assert.ok(await page.$(`${view} li.tile-item.is-watch`), "a degraded tile has its tone");
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
  assert.ok((await visible(`${view} .cluster-card.is-detached .cluster-mark`)).includes("MISSING"));
  // Recorded live: a paused member is UNREACHABLE and keeps its role badge.
  await refreshTo(R["ic-8.4-unreachable"]);
  const paused = await page.$$eval(`${view} .cluster-card.is-detached`, (els) => els.map((e) => [e.querySelector(".cluster-role")?.textContent, e.querySelector(".cluster-mark")?.textContent]));
  assert.deepEqual(paused, [["Secondary", "UNREACHABLE"]]);
  await shot("cluster-ic-unreachable.png");
  await refreshTo(R["ic-8.4-local-offline"]);
  // This member left the group: no group count, nothing called out of the group.
  assert.match(await h.text(`${view} .cluster-bus-label`), /not visible from this member/);
  const blindSummary = (await visible(`${view} .summary-line`))[0];
  assert.doesNotMatch(blindSummary, /\d+ of \d+ members ONLINE/);
  assert.match(blindSummary, /this member OFFLINE · group not visible from here/);
  // d15: sidebar place badge shows a dot and a neutral count, with the word in its tooltip and accessible name.
  assert.equal(await page.$eval(`${nav} .nav-marker .count-badge`, (m) => m.textContent), "1");
  assert.ok(await page.$(`${nav} .nav-marker .status-dot.is-problem`));
  assert.match(await page.$eval(`${nav} .nav-marker`, (m) => m.getAttribute("title") || ""), /cluster problem, 1 member with a problem/);
  assert.match(await page.$eval(nav, (b) => b.textContent), /cluster problem, 1 member with a problem/);
  assert.match(await h.text(`${view} .tile-item:nth-child(2) .tile-value`), /^Not visible/);
  assert.ok((await visible(`${view} .cluster-card.is-local.is-detached .cluster-mark`)).includes("OFFLINE"));
  assert.equal((await visible(`${view} .cluster-card:not(.is-local) .cluster-mark`)).filter((s) => s === "Not visible here").length, 2);
  assert.ok((await visible(`${view} .cluster-card:not(.is-local) .cluster-flow`)).every((t) => !/Not in the group/.test(t)));
  // The router check-in is this member's frozen copy.
  await tab("Routers");
  assert.match((await visible(`${view} tbody tr`))[0], /this member's copy/);
  await tab("Topology");
  // From a group member, the read replica hangs apart from the group line.
  await refreshTo(R["ic-8.4-read-replica"]);
  const replica = await page.$eval(`${view} .cluster-card.is-replica`, (e) => ({
    inGroup: !!e.closest(".cluster-group"), stem: getComputedStyle(e.parentElement.querySelector(".cluster-stem")).borderLeftStyle }));
  assert.deepEqual(replica, { inGroup: false, stem: "dashed" });
  assert.equal(await page.$$eval(`${view} .cluster-group .cluster-card`, (els) => els.length), 3);
  // Connected to the read replica itself: an InnoDB Cluster, not "not part of a cluster".
  await refreshTo(R["ic-8.4-read-replica-local"]);
  assert.match((await visible(`${view} .summary-line`))[0], /this server is a read replica · Replicating/);
  // It never claims quorum it cannot see: unknown, with the reason.
  const quorumTile = await page.$eval(`${view} .tile-item:first-child`, (t) => ({ text: t.textContent, title: t.getAttribute("title") || t.querySelector("[title]")?.getAttribute("title") || "" }));
  assert.match(quorumTile.text, /Quorum.*Not visible.*Failures tolerated unknown/);
  assert.match(quorumTile.title, /^A read replica is not a group member/);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");

  // Multi-primary Group Replication without metadata: every member PRIMARY, no Routers tab.
  await refreshTo(R["gr-8.4-multi-primary"]);
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Members"]);
  assert.deepEqual(await visible(`${view} .cluster-members .cluster-role`), ["Primary", "Primary", "Primary"]);
  assert.match((await visible(`${view} .summary-line`))[0], /MySQL Group Replication · 3 of 3 members ONLINE · multi-primary/);

  // MariaDB Galera: only this node is judged; the others are listed without a verdict.
  await refreshTo(R["galera-11.8-healthy"]);
  assert.match((await visible(`${view} .summary-line`))[0], /MariaDB Galera Cluster mf_galera · Primary component · 3 nodes · this node Synced/);
  const galera = await page.$$eval(`${view} .cluster-members .cluster-card`, (els) => els.map((e) => ({
    local: e.classList.contains("is-local"), state: e.querySelector(".cluster-mark")?.textContent,
    flow: e.querySelector(".cluster-flow")?.textContent || null,
    useBtn: e.parentElement.querySelector(".cluster-use-node")?.textContent || null,
    meta: [...e.querySelectorAll(".cluster-meta")].map((m) => m.textContent) })));
  assert.deepEqual(galera.map((c) => c.state).sort(), ["In the component · not read here", "In the component · not read here", "Synced"]);
  assert.ok(galera.filter((c) => !c.local).every((c) => c.flow === null), "non-local Galera cards do not repeat the 3-line explanation");
  assert.deepEqual(galera.filter((c) => c.useBtn).map((c) => c.useBtn), ["Use cluster-galera-2"]);
  const galeraNotes = await h.notes();
  assert.match(galeraNotes, /Each other node's state and queues are read on that node; connect to it to inspect\./);
  assert.ok(galera.find((c) => c.local).meta.some((m) => /^MariaDB 11\.8\.9/.test(m)), JSON.stringify(galera));
  // Port 0: the host alone (a note explains it); another node's version is not visible, so none is shown.
  assert.ok(galera.every((c) => c.meta.some((m) => /^192\.168\.80\.\d+$/.test(m))), JSON.stringify(galera));
  assert.ok(galera.filter((c) => !c.local).every((c) => !c.meta.includes("MariaDB")), "no bare product name");
  assert.deepEqual(await visible(`${view} .tile-label`), ["Component", "Nodes", "This node", "Flow control paused"]);
  assert.match(await h.text(`${view} .tile-item:nth-child(2)`), /3 in component.*1 inspected here/);
  assert.match(await h.text(`${view} .cluster-bus-label`), /^Primary component · 3 nodes · tolerates 1 failure$/);
  await shot("cluster-galera-topology.png");
  // Galera says Node, not Member.
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Nodes"]);
  await tab("Nodes");
  const galeraCols = await visible(`${view} thead th`);
  assert.ok(galeraCols[0].startsWith("Node"), galeraCols.join());
  for (const col of ["Receive queue", "Send queue", "Flow control paused", "Cert failures"]) assert.ok(galeraCols.some((c) => c.startsWith(col)), `${col} in ${galeraCols}`);
  // The table fits at 1440 px: nothing under the Columns button, no cut address.
  const fit = await page.$eval(`${view} .data-table-wrap`, (w) => ({ client: w.clientWidth, scroll: w.scrollWidth,
    cut: [...w.querySelectorAll("td")].filter((td) => td.scrollWidth > td.clientWidth + 1).map((td) => td.textContent) }));
  assert.ok(fit.scroll <= fit.client, JSON.stringify(fit));
  assert.deepEqual(fit.cut, []);
  await tab("Topology");
  await refreshTo(R["galera-11.8-donor"]);
  const donor = await page.$$eval(`${view} .cluster-card.is-local`, (els) => els.map((e) => ({ state: e.querySelector(".cluster-mark").textContent,
    watch: e.classList.contains("tone-watch"), flow: e.querySelector(".cluster-flow").textContent })));
  assert.deepEqual(donor, [{ state: "Donor/Desynced", watch: true, flow: "Receive queue 0 · send 0 · flow control paused 0%" }]);
  // The finding names the measured cause.
  await page.click(`${view} .cluster-findings li .cluster-finding-toggle`);
  assert.match(await h.text(`${view} .cluster-findings li .cluster-finding-text`), /^wsrep_desync is ON/);
  await refreshTo(R["galera-11.8-two-of-three"]);
  assert.match(await h.text(`${view} .cluster-bus-label`), /Primary component · 2 of 3 nodes/);
  assert.ok((await visible(`${view} .cluster-findings li`)).some((f) => /Fewer nodes than configured/.test(f)));
  await refreshTo(R["galera-11.8-non-primary"]);
  assert.ok(await page.$(`${view} .cluster-bus.is-broken`));
  assert.match(await h.text(`${view} .cluster-bus-label`), /non-Primary component · 1 of 3 nodes/);
  // A partial seed list: the size alone, never "3 of 1".
  await refreshTo(R["galera-11.8-partial-seeds"]);
  assert.match(await h.text(`${view} .cluster-bus-label`), /^Primary component · 3 nodes ·/);
  assert.doesNotMatch((await visible(`${view} .summary-line`))[0], / of 1/);
  // Two nodes: one more failure stops the cluster.
  await refreshTo(R["galera-11.8-two-nodes"]);
  assert.match((await visible(`${view} .cluster-findings li`)).join(" | "), /Cannot survive another failure/);
  // wsrep_on OFF on a joined node: still a cluster, a problem.
  await refreshTo(R["galera-11.8-wsrep-off"]);
  assert.match((await visible(`${view} .summary-line`))[0], /^Problem/);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");
  // Galera on MySQL that is not Percona XtraDB Cluster.
  await refreshTo(R["galera-mysql-8.4-healthy"]);
  assert.match((await visible(`${view} .summary-line`))[0], /Galera Cluster for MySQL mf_pxc/);

  // Percona XtraDB Cluster: every node's state comes from pxc_cluster_view.
  await refreshTo(R["pxc-8.4-healthy"]);
  assert.deepEqual(await visible(`${view} .cluster-members .cluster-mark`), ["Synced", "Synced", "Synced"]);
  // PXC in the place (the summary names the product); the full name on hover.
  assert.ok((await visible(`${view} .cluster-members .cluster-meta`)).some((m) => /^PXC 8\.4\.11/.test(m)));
  await refreshTo(R["pxc-8.4-member-donor"]);
  // One word for one state on every surface: the card, the finding title.
  assert.ok((await visible(`${view} .cluster-card.tone-watch .cluster-mark`)).includes("Donor/Desynced"));
  assert.match((await visible(`${view} .cluster-findings li .cluster-finding-title`))[0], /is Donor\/Desynced$/);
  await tab("Nodes");
  const pxcFit = await page.$eval(`${view} .data-table-wrap`, (w) => ({ client: w.clientWidth, scroll: w.scrollWidth }));
  assert.ok(pxcFit.scroll <= pxcFit.client, `PXC table fits at 1440: ${JSON.stringify(pxcFit)}`);
  await tab("Topology");
  // A node down: the configured entry that is not in the component is named and drawn apart.
  await refreshTo(R["pxc-8.4-two-of-three"]);
  assert.deepEqual(await visible(`${view} .cluster-card.is-placeholder .cluster-name`), ["mf-pxc-2"]);
  await refreshTo(R["pxc-8.4-donor"]);
  assert.deepEqual(await visible(`${view} .cluster-card.is-local .cluster-mark`), ["Donor/Desynced"]);
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
  assert.match(calm, /MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster and MySQL InnoDB Cluster/);
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
  // On screen it still looks disabled: a neutral gray fill, never the accent, and the faint text.
  const here = await page.$eval(nav, (b) => {
    const probe = document.createElement("span");
    probe.style.background = "var(--surface-hover)";
    probe.style.color = "var(--ink-faint)";
    document.body.append(probe);
    const want = { bg: getComputedStyle(probe).backgroundColor, ink: getComputedStyle(probe).color };
    probe.remove();
    return { bg: getComputedStyle(b).backgroundColor, ink: getComputedStyle(b).color, want };
  });
  assert.deepEqual({ bg: here.bg, ink: here.ink }, here.want, "the open Cluster place keeps its disabled look");
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
  // Another connection's report never marks a member "This connection".
  const stale = await page.evaluate((sel) => ({
    text: document.querySelector(sel).textContent,
    labels: [...document.querySelectorAll(`${sel} [aria-label]`)].map((e) => e.getAttribute("aria-label")).join(" | "),
    chips: [...document.querySelectorAll(`${sel} .cluster-local`)].map((e) => e.textContent) }), view);
  assert.doesNotMatch(stale.text, /This connection/);
  assert.doesNotMatch(stale.labels, /this connection/i);
  assert.deepEqual(stale.chips, ["Read here"]);
  assert.equal(await page.$(`${nav} .status-dot`), null, "no marker from another connection");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");
  // Another connection's report offers no change.
  const galeraLocal = R["galera-11.8-healthy"].members.find((m) => m.local);
  await page.click(`${view} .cluster-card[data-row-key="${galeraLocal.key}"]`);
  await page.waitForSelector(".view-inspector .review-actions button");
  assert.equal(await page.$eval(".view-inspector .review-actions button", (b) => b.getAttribute("aria-disabled")), "true");
  const plansBefore = plans.length;
  await page.click(".view-inspector .review-actions button");
  await pause(300);
  assert.equal(plans.length, plansBefore, "nothing is planned on another connection's report");
  await backToList();
  await page.keyboard.press("Escape");
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

  // Until now the Cluster place read nothing but /api/live/cluster (the review
  // above planned and sent one change, whose progress it followed).
  assert.deepEqual(state.other.filter((o) => o.path.startsWith("/api/live/") && !o.path.startsWith("/api/live/cluster/")
    && o.path !== "/api/live/action/progress").map((o) => o.path), [], "no other view read");
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

  // ProxySQL Admin connection: calm Not part of a cluster state with Open ProxySQL action and muted sidebar item.
  const proxyAdminReport = {
    ...R["mysql-8.4-standalone"],
    server_product: "ProxySQL Admin",
    server_version: "3.0.11",
    message: "Not part of a cluster: this connection is a ProxySQL Admin interface.",
  };
  await refreshTo(proxyAdminReport);
  assert.equal(await h.text(`${view} .view-empty h3`), "Not part of a cluster");
  assert.equal(await h.text(`${view} .view-empty p`), "This connection is a ProxySQL Admin interface.");
  assert.equal(await h.text(`${view} .view-empty button.primary`), "Open ProxySQL");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true");
  assert.equal(await page.$eval(nav, (b) => b.title), "This connection is not part of a cluster");
  await page.click(`${view} .view-empty button.primary`);
  await page.waitForSelector('.main-nav button[data-place="proxysql"].active');
  await page.click(nav);
  await page.waitForSelector(`${nav}.active`);

  // A failed read keeps the last report greyed with its age, a short reason
  // (no client dump), and no "This connection".
  await refreshTo(R["galera-11.8-healthy"]);
  state.fallback = { status: 400, body: { error: "mysql client exited 1: ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:34102' (61)" } };
  await h.refresh();
  await page.waitForFunction((sel) => document.querySelector(`${sel} .view-status .error`), {}, view);
  const failedRead = await page.evaluate((sel) => ({
    error: document.querySelector(`${sel} .view-status .error`).textContent,
    stale: document.querySelector(sel).classList.contains("is-stale"),
    note: document.querySelector(`${sel} .cluster-stale`)?.textContent || "",
    chips: [...document.querySelectorAll(`${sel} .cluster-local`)].map((e) => e.textContent) }), view);
  assert.match(failedRead.error, /^Could not connect to 127\.0\.0\.1:34102/);
  assert.doesNotMatch(failedRead.error, /mysql client exited|ERROR 2003/);
  assert.equal(failedRead.stale, true);
  assert.match(failedRead.note, /^Last read .*the read since failed/);
  assert.deepEqual(failedRead.chips, ["Read here"]);
  // Refresh all shares that state: a successful read through it ends the
  // stale state, and a failed one starts it.
  async function refreshAll(body, status = 200) {
    state.fallback = { status, body };
    await h.server();
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.click(".server-place [data-action=refresh-all]");
    await page.waitForFunction(() => !document.querySelector(".server-place .refresh-all-stop") && /refreshed/.test(document.querySelector(".server-place .refresh-all-status")?.textContent || ""), { timeout: 20000 });
    await page.click(nav);
    await page.waitForFunction((sel) => !!document.querySelector(`${sel} .summary-line`)?.offsetParent, {}, view);
    await pause(200);
    return page.evaluate((sel) => ({ stale: document.querySelector(sel).classList.contains("is-stale"),
      chips: [...document.querySelectorAll(`${sel} .cluster-local`)].map((e) => e.textContent),
      error: document.querySelector(`${sel} .view-status .error`)?.textContent || "" }), view);
  }
  assert.deepEqual(await refreshAll(R["galera-11.8-healthy"]), { stale: false, chips: ["This connection"], error: "" });
  const afterAllFailed = await refreshAll({ error: "mysql client exited 1: ERROR 2013 (HY000): Lost connection to MySQL server at 'reading initial communication packet'" }, 400);
  assert.equal(afterAllFailed.stale, true);
  assert.deepEqual(afterAllFailed.chips, ["Read here"]);
  assert.match(afterAllFailed.error, /^Lost the connection to the server while reading/);

  // A failed read on a different connection drops the stale other-connection
  // report, and ERROR 1045 without Access denied shows the server's own line.
  await chooseConnection(page, await connectionOption(page, "Node A"));
  state.fallback = { status: 400, body: { error: "mysql client exited 1: ERROR 1045 (28000): near \"@@hostname\": syntax error" } };
  await h.refresh();
  await page.waitForFunction((sel) => document.querySelector(`${sel} .view-status .error`), {}, view);
  assert.equal(await page.$(`${view} .other-connection`), null, "stale report from another connection dropped on failed read");
  assert.equal(await page.$(`${view} .cluster-topology`), null);
  assert.match(await h.text(`${view} .view-status .error`), /near "@@hostname": syntax error/);
  assert.doesNotMatch(await h.text(`${view} .view-status .error`), /user name or password/);
  await chooseConnection(page, await connectionOption(page, "Solo"));

  // Details never carry over to another connection's report, even for the
  // same finding.
  await refreshTo(R["ic-8.4-degraded"]);
  await toggleFinding(/^Cannot survive another failure/);
  assert.equal((await findingStates()).find((f) => /^Cannot survive/.test(f.title)).expanded, "true");
  await chooseConnection(page, await connectionOption(page, "Node A"));
  await page.waitForFunction((sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")), {}, view);
  await refreshTo(R["ic-8.4-degraded"]);
  assert.equal(state.seen.at(-1).connection.host, "node-a.test");
  assert.deepEqual((await findingStates()).filter((f) => f.expanded === "true"), [], "a new connection starts with every finding closed");

  // The app's 720 px minimum window: no page scroll sideways; the cards wrap
  // inside the frame; the sections stay reachable; the first tab's content
  // starts above the fold.
  await refreshTo(R["ic-8.4-degraded"]);
  await page.setViewport({ width: 720, height: 520 });
  await pause(400);
  const narrow = await page.evaluate((sel) => ({
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    tabs: [...document.querySelectorAll(`${sel} .segmented button`)].filter((b) => b.offsetParent).length,
    frame: (() => { const f = document.querySelector(`${sel} .cluster-topology`); return f ? f.scrollWidth - f.clientWidth : null; })(),
    top: document.querySelector(`${sel} .cluster-topology`).getBoundingClientRect().top,
    fold: document.querySelector(".window-statusbar").getBoundingClientRect().top,
  }), view);
  assert.ok(narrow.overflow <= 0, `horizontal page scroll at 720 px: ${narrow.overflow}`);
  assert.equal(narrow.tabs, 3);
  assert.equal(narrow.frame, 0, "the cards wrap instead of running off the right edge");
  assert.ok(narrow.top < narrow.fold - 24, `the topology starts above the fold: ${JSON.stringify(narrow)}`);
  await shot("cluster-720.png");
  await tab("Members");
  await pause(200);
  assert.ok((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0);
  const tableTop = await page.evaluate((sel) => ({ top: document.querySelector(`${sel} .data-table-wrap`).getBoundingClientRect().top,
    fold: document.querySelector(".window-statusbar").getBoundingClientRect().top }), view);
  assert.ok(tableTop.top < tableTop.fold - 24, `the members table starts above the fold: ${JSON.stringify(tableTop)}`);
  await page.setViewport({ width: 1440, height: 1000 });
  await h.narrow("cluster-390.png", { place: ".cluster-place" });

  assert.deepEqual(h.errors, []);
  console.log("cluster: ok");
} finally {
  await h.close();
}
