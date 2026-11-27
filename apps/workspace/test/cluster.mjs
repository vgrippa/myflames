// The Cluster place: mocked /api/live/cluster answering cluster-1.0 reports
// that the real backend computed (cluster-fixtures.py: live recordings of a
// MariaDB 11.8 Galera, a Percona XtraDB Cluster 8.4 and a MySQL 8.4 InnoDB
// Cluster, states derived from them, and synthetic servers), and the real
// Python server for the session and saved connections. Covers: the place
// right after Replication, nothing read until it is on screen, one read on
// open, each kind (Galera, PXC, InnoDB Cluster with routers, multi-primary
// Group Replication), the topology (member cards, roles, "This connection",
// a degraded member drawn apart by a dashed edge, a broken link and a mark,
// not color alone; controlled motion), Members and Routers tables, the inspector
// per member and per router, findings, the not-a-cluster state and the
// sidebar item that looks disabled but stays focusable, an account that
// cannot read, connection binding (another connection's report greyed, the
// sidebar back to normal), Refresh, Refresh all feeding the place, keyboard,
// and the 720 px window.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection, connectionOption, openRefreshMenu } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const R = {};
for (const name of ["galera-11.8-healthy", "galera-11.8-donor", "galera-11.8-two-of-three", "galera-11.8-non-primary", "pxc-8.4-healthy",
  "pxc-8.4-donor", "pxc-8.4-member-donor", "ic-8.4-healthy", "ic-8.4-degraded", "ic-8.4-unreachable", "ic-8.4-no-quorum",
  "ic-8.4-member-offline", "ic-8.4-local-offline", "gr-8.4-multi-primary", "gr-8.4-denied", "mysql-8.4-standalone",
  "mariadb-11.8-standalone", "pxc-8.4-two-of-three", "galera-11.8-two-nodes", "galera-11.8-partial-seeds", "galera-mysql-8.4-healthy",
  "galera-11.8-wsrep-off", "ic-8.4-read-replica", "ic-8.4-read-replica-local", "plan-switchover", "result-switchover",
  "progress-sending", "ic-8.4-clusterset-primary", "ic-8.4-clusterset-replica", "ic-8.4-clusterset-replica-channel-off",
  "ic-8.4-clusterset-invalidated", "ic-8.4-read-replica-clusterset"]) R[name] = await load(`cluster-${name}.json`);

const view = '.server-view[data-view="cluster"]';
const nav = '.main-nav button[data-place="cluster"]';
const plans = [];
const sends = [];
const declined = [];
let planDelay = 0;
const h = await open("/api/live/cluster", "cluster", {
  "/api/live/cluster/plan": (body) => { plans.push(body); return { status: 200, body: R["plan-switchover"], delay: planDelay }; },
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
// The open disabled-action message, kept under local/why-shots for review.
const whyShots = fileURLToPath(new URL("../../../local/why-shots/", import.meta.url));
await fs.mkdir(whyShots, { recursive: true });
const whyShot = (file) => page.screenshot({ path: path.join(whyShots, file), fullPage: false });
async function answer(report) {
  state.fallback = { status: 200, body: report };
}
async function refreshTo(report, { preservePopover = false } = {}) {
  const before = state.seen.length;
  await answer(report);
  if (preservePopover) {
    // A DOM activation refreshes without an outside pointer gesture light-
    // dismissing the message first, so these cases exercise report updates.
    await page.evaluate(() => [...document.querySelectorAll("[data-action=refresh]")]
      .find((b) => !b.closest("[hidden]") && b.getClientRects().length)?.click());
  } else await h.refresh();
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
  await api("/api/profiles/save", { value: { name: "Read-only", host: "readonly.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", read_only: true } });
  await page.reload({ waitUntil: "networkidle0" });

  // Session hydration can finish after networkidle under parallel browser load.
  await page.waitForSelector(nav, { visible: true });
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
  // Motion conveys known healthy membership, with an OS override.
  const stemMotion = () => page.$eval(`${view} .cluster-stem.is-active`, (e) => getComputedStyle(e, "::after").animationName);
  assert.equal(await stemMotion(), "topology-membership");
  // Lane F9: Cluster's own stems keep the 18 px default dot travel (the 24 px
  // stem minus the 6 px dot); only the shorter tier stems override it.
  const clusterDotTravel = await page.$eval(`${view} .cluster-stem.is-active`, (stem) => {
    const probe = document.body.appendChild(document.createElement("div"));
    const travel = getComputedStyle(stem).getPropertyValue("--stem-dot-travel").trim() || "18px";
    probe.style.cssText = `position:absolute;visibility:hidden;transform:translateY(${travel})`;
    const t = getComputedStyle(probe).transform;
    probe.remove();
    return t === "none" ? 0 : parseFloat(t.slice(7, -1).split(",").pop());
  });
  assert.equal(clusterDotTravel, 18, "a Cluster stem keeps the 18 px dot travel");
  assert.equal(await page.$(`${view} .topology-presentation-controls`), null, "no presentation controls");
  const controlLabels = await page.$$eval(`${view} button`, (els) => els.map((e) => (e.textContent || "").trim()));
  for (const label of ["Depth", "Pause motion", "Resume motion"]) assert.ok(!controlLabels.includes(label), `no ${label} button`);
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  assert.equal(await stemMotion(), "none", "system reduced motion overrides playback");
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
  assert.equal(await stemMotion(), "topology-membership");
  const info = await h.popover("About Cluster");
  assert.match(info, /does not measure traffic or throughput/);
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

  // The identity line under the summary names this server, and the overview
  // band lists every cluster-wide action the backend offers, enabled or not.
  assert.equal(await h.text(`${view} .cluster-identity`),
    "You are connected to mf-ic-1:3306, a SECONDARY, ONLINE. Stop and Start act on this server only.");
  const overview = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled"), tab: e.tabIndex, why: e.getAttribute("aria-describedby") })));
  assert.deepEqual(overview.map((a) => a.label), R["ic-8.4-healthy"].actions.map((a) => a.label));
  assert.ok(overview.every((a) => a.disabled === "true"), "every mode switch and bare ClusterSet placeholder is disabled here");
  assert.ok(overview.every((a) => a.tab === 0), "a disabled action stays in the tab order");
  // Each reason is the button's accessible description, in the DOM but not
  // visible: never a caption under the group before a click.
  const reasons = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(),
    title: e.title,
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    reasonHidden: document.getElementById(e.getAttribute("aria-describedby") || "")?.classList.contains("visually-hidden") || false,
    controls: document.getElementById(e.getAttribute("aria-describedby") || "")?.querySelectorAll("button, .ops-commands").length })));
  for (const [i, a] of R["ic-8.4-healthy"].actions.entries()) {
    assert.equal(reasons[i].label, a.label);
    assert.ok(reasons[i].reason.includes(a.why_not), `${a.id}: its reason is described: ${reasons[i].reason}`);
    assert.ok(reasons[i].reason.includes(a.fix), `${a.id}: its fix is described: ${reasons[i].reason}`);
    assert.equal(reasons[i].reasonHidden, true, `${a.id}: the description is visually hidden, not a caption`);
    assert.equal(reasons[i].title, [...new Set([a.why_not, a.fix].filter(Boolean))].join(" "), `${a.id}: hover explains the disabled action`);
    assert.equal(reasons[i].controls, 0, `${a.id}: its hidden description contains no controls or Commands`);
  }
  assert.equal(await page.$$eval(`${view} .cluster-actions .cluster-action-why`, (els) =>
    els.filter((e) => e.getClientRects().length).length), 0, "no reason text is visible under the buttons before a click");
  // Click, Enter or Space on a disabled action opens the message anchored to
  // the button with the reason; it plans and sends nothing.
  const plansBeforeDisabled = plans.length;
  const firstWhy = R["ic-8.4-healthy"].actions[0];
  const openWhy = `${view} .cluster-actions .cluster-action-why:popover-open`;
  await page.focus(`${view} .cluster-actions .review-actions > button`);
  await page.keyboard.press("Enter");
  await page.waitForFunction((sel, text) => {
    const open = document.querySelector(sel);
    return !!open && open.textContent.includes(text);
  }, {}, openWhy, firstWhy.why_not);
  let whyText = await page.$eval(openWhy, (e) => e.textContent);
  assert.ok(whyText.includes(firstWhy.why_not) && whyText.includes(firstWhy.fix), `Enter opens the reason message: ${whyText}`);
  // Escape closes it and focus is back on the button.
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, openWhy);
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-describedby")), overview[0].why,
    "after Escape the focus is back on the disabled action");
  // Space opens it too; clicking a second action's button closes the first
  // message and opens the second; clicking the same button again closes it.
  const thirdWhy = R["ic-8.4-healthy"].actions[2];
  const thirdWhyButton = `${view} .cluster-actions .review-actions > button:nth-of-type(3)`;
  await page.keyboard.press("Space");
  await page.waitForFunction((sel, text) => {
    const open = document.querySelector(sel);
    return !!open && open.textContent.includes(text);
  }, {}, openWhy, firstWhy.why_not);
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel, text) => {
    const open = document.querySelector(sel);
    return !!open && open.textContent.includes(text);
  }, {}, openWhy, thirdWhy.why_not);
  whyText = await page.$eval(openWhy, (e) => e.textContent);
  assert.ok(whyText.includes(thirdWhy.why_not), `the other action's message replaces the first: ${whyText}`);
  assert.equal(await page.$$eval(".cluster-action-why:popover-open", (els) => els.length), 1, "one message at a time");
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, openWhy);
  await page.click(thirdWhyButton);
  await page.waitForSelector(openWhy, { visible: true });
  await page.click(`${view} .summary-line`);
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, openWhy);
  await pause(300);
  assert.equal(plans.length, plansBeforeDisabled, "a disabled action plans nothing");
  assert.equal(sends.length, 0, "disabled-action messages send no action requests");
  assert.equal(await page.$("dialog.action-review[open]"), null, "a disabled action opens no review sheet");

  // Refresh invalidates an open explanation; server text remains literal in
  // both the hidden description and the message, even when it resembles HTML.
  await page.click(thirdWhyButton);
  await page.waitForSelector(openWhy, { visible: true });
  const changedWhy = structuredClone(R["ic-8.4-healthy"]);
  const literalReason = '<img src=x onerror="window.__whyInjection = true"> needs <script>privileges</script>.';
  changedWhy.actions[2].why_not = literalReason;
  await refreshTo(changedWhy, { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "Refresh closes a message when its explanation changes");
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel, reason) => document.querySelector(sel)?.textContent.includes(reason), {}, openWhy, literalReason);
  assert.equal(await page.$eval(openWhy, (e) => e.querySelectorAll("img, script").length), 0, "server text never becomes message markup");
  assert.equal(await page.$eval(thirdWhyButton, (b) => document.getElementById(b.getAttribute("aria-describedby")).querySelectorAll("img, script").length),
    0, "server text never becomes hidden-description markup");
  assert.equal(await page.evaluate(() => window.__whyInjection), undefined, "server text cannot execute");
  await refreshTo(R["ic-8.4-healthy"], { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "the next explanation change also closes the message");

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
  await page.click(`${view} button[aria-label="Columns of Cluster members"]`);
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
  await page.click(`${view} button[aria-label="Columns of Cluster members"]`);
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
  const memberButtons = await page.$$eval(".view-inspector .review-actions > button", (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(memberButtons, secondary.actions.map((a) => a.label));
  assert.ok(memberButtons.includes("Make primary…"), memberButtons.join());
  // The member page lists every member action the backend offers, always: on
  // this secondary connection Make primary is enabled, Start Group Replication
  // stays visible, focusable and disabled with its reason as its description.
  const onSecondary = await page.$$eval(".view-inspector .review-actions > button", (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true", tab: e.tabIndex,
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim() })));
  const secondaryEnabled = Object.fromEntries(onSecondary.map((a) => [a.label, !a.disabled]));
  assert.equal(onSecondary.find((a) => a.label === "Make primary…").disabled, false, "Make primary is enabled on this secondary");
  const startDisabled = onSecondary.find((a) => a.label === "Start Group Replication…");
  assert.equal(startDisabled.disabled, true);
  assert.equal(startDisabled.tab, 0, "a disabled member action stays in the tab order");
  assert.match(startDisabled.reason, /This member is not OFFLINE\. Stop Group Replication on this member before starting it again\./);
  const plansBeforeMember = plans.length;
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Start Group Replication…").click());
  await page.waitForSelector(".view-inspector .cluster-action-why:popover-open", { visible: true });
  assert.equal(plans.length, plansBeforeMember, "a disabled member action plans nothing");
  assert.equal(await page.$("dialog.action-review[open]"), null, "a disabled member action opens no review sheet");
  // Clicking the same action again closes its message (the inspector stays).
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Start Group Replication…").click());
  await page.waitForFunction(() => !document.querySelector(".view-inspector .cluster-action-why:popover-open"));
  planDelay = 1200;
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions button")].find((b) => b.textContent.trim() === "Make primary…").click());
  await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector .review-actions > button")].some((b) => b.textContent === "Reading…"));
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Start Group Replication…").click());
  assert.equal(await page.$(".view-inspector .cluster-action-why:popover-open"), null, "a busy group opens no disabled-action message");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  planDelay = 0;
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
  assert.doesNotMatch(await page.$eval("dialog.action-review[open] .action-effect", (e) => e.textContent),
    /Not readable/i, "the effect table says why a value is missing, never 'Not readable'");
  assert.equal(sends.length, 1);
  assert.match(sends[0].progress_id, /^[0-9a-f]{32}$/);
  await page.evaluate(() => [...document.querySelectorAll("dialog.action-review[open] .sheet-actions button")].find((b) => b.textContent.trim() === "Close").click());
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  await page.waitForSelector(`${view} .action-result`);
  assert.match(await page.$eval(`${view} .action-result`, (e) => e.textContent), /Done/);
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .action-result .text-button`)].find((b) => b.textContent.trim() === "Close").click(), view);
  await backToList();

  // A disabled action that needs a privilege opens its message on click: the
  // reason, the copyable GRANT (the existing copy pattern) with "Run it as an
  // administrator, then Refresh.", and the unreadable-privileges line on the
  // action whose privileges the server cannot be asked.
  const grantReport = JSON.parse(JSON.stringify(R["ic-8.4-healthy"]));
  const grantMember = grantReport.members.find((m) => m.local);
  const stopAction = grantMember.actions.find((a) => a.id === "stop_member");
  stopAction.enabled = false;
  stopAction.why_not = "This account lacks GROUP_REPLICATION_ADMIN.";
  stopAction.fix = "Grant it on this server.";
  stopAction.missing_privileges = [["GROUP_REPLICATION_ADMIN", "*.*"]];
  stopAction.grant_sql = ["GRANT GROUP_REPLICATION_ADMIN ON *.* TO 'dba'@'%';",
    ...Array.from({ length: 12 }, (x, i) => `-- grant note ${i + 1}: run as an administrator, then Refresh`)].join("\n");
  const startAction = grantMember.actions.find((a) => a.id === "start_member");
  startAction.privileges_unreadable = true;
  await page.evaluate(() => { window.__grants = []; navigator.clipboard.writeText = (t) => (window.__grants.push(t), Promise.resolve()); });
  await refreshTo(grantReport);
  await h.select(`${view} tr[data-row-key="${grantMember.key}"]`);
  const clickMemberAction = async (label) => {
    const button = await page.evaluateHandle((l) => [...document.querySelectorAll(".view-inspector .review-actions > button")]
      .find((b) => b.textContent.trim() === l), label);
    await button.asElement().click();
    await button.dispose();
  };
  const memberWhy = ".view-inspector .cluster-action-why:popover-open";
  // Before any click the inspector shows no caption: the reasons are hidden descriptions.
  assert.equal(await page.$$eval(".view-inspector .cluster-action-why", (els) => els.filter((e) => e.getClientRects().length).length),
    0, "no reason text is visible under the member actions before a click");
  await clickMemberAction("Stop Group Replication…");
  await page.waitForFunction((sel) => !!document.querySelector(sel)?.textContent.includes("GROUP_REPLICATION_ADMIN"), {}, memberWhy);
  let grantInspector = await h.inspector();
  assert.match(grantInspector, /This account lacks GROUP_REPLICATION_ADMIN\. Grant it on this server\./);
  assert.match(grantInspector, /Run it as an administrator, then Refresh\./);
  await clickMemberAction("Stop Group Replication…");
  await page.waitForFunction(() => !document.querySelector(".view-inspector .cluster-action-why:popover-open"));
  await clickMemberAction("Start Group Replication…");
  await page.waitForFunction((sel) => !!document.querySelector(sel)?.textContent.includes("Privileges could not be read"), {}, memberWhy);
  assert.match(await h.inspector(), /Privileges could not be read; the server refuses the change if one is missing\./);
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(`${memberWhy} .ops-pre`, { visible: true });
  // The top-layer message stays beside its anchor, and follows the button
  // in DOM order so Tab reaches the scrollable GRANT and then Copy.
  const anchored = await page.$eval(memberWhy, (message) => {
    const button = message.previousElementSibling;
    const a = button.getBoundingClientRect();
    const b = message.getBoundingClientRect();
    return { supports: CSS.supports("anchor-name", "--probe") && CSS.supports("position-area", "block-end span-inline-end"),
      anchor: button.style.getPropertyValue("anchor-name"), target: message.style.getPropertyValue("position-anchor"),
      distance: Math.min(Math.abs(a.bottom - b.top), Math.abs(a.top - b.bottom)) };
  });
  assert.equal(anchored.supports, true, "Puppeteer's Chromium supports CSS anchor positioning");
  assert.ok(anchored.anchor && anchored.anchor === anchored.target, "the message uses its own button's CSS anchor");
  assert.ok(anchored.distance <= 1, `the message touches its anchor's block edge: ${JSON.stringify(anchored)}`);
  await page.$eval(memberWhy, (message) => message.previousElementSibling.focus());
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Grant for Stop Group Replication",
    "Tab from the action enters its message at the scrollable GRANT");
  await page.keyboard.press("Tab");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Copy: Grant for Stop Group Replication",
    "the next Tab reaches Copy inside the message");
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, memberWhy);
  assert.ok(await page.$(".view-inspector"), "Escape dismisses only the message, keeping its inspector open");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim()), "Stop Group Replication…",
    "Escape from inside the message returns focus to the action");
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(`${memberWhy} .ops-pre`, { visible: true });
  // A long GRANT scrolls inside its box instead of stretching the page, in both themes.
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(150);
    const box = await page.evaluate(() => {
      const e = document.querySelector(".view-inspector .cluster-action-why:popover-open .ops-pre");
      return {
        found: !!e,
        maxH: e ? getComputedStyle(e).maxHeight : "",
        scroll: e ? e.scrollHeight : 0, client: e ? e.clientHeight : 0,
        pre: e ? e.scrollHeight - e.clientHeight : null,
        page: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    assert.ok(box.pre > 0, `a long GRANT scrolls inside its box (${theme}): ${JSON.stringify(box)}`);
    assert.ok(box.page <= 0, `no horizontal page scroll with the GRANT open (${theme}): ${box.page}`);
    await whyShot(`cluster-why-grant-1440-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await shot("cluster-grant.png");
  await page.click(".view-inspector .cluster-action-why:popover-open .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector(".view-inspector .cluster-action-why:popover-open .ops-commands [role=status]")?.textContent || ""));
  assert.equal(await page.evaluate(() => window.__grants[0]), stopAction.grant_sql, "Copy puts the GRANT on the clipboard");
  // The Close button in the message closes it and returns focus to the action.
  await page.click(`${memberWhy} .why-disabled-close`);
  await page.waitForFunction(() => !document.querySelector(".view-inspector .cluster-action-why:popover-open"));
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim()), "Stop Group Replication…",
    "the message's Close button returns focus to its action");
  const plansBeforeGrant = plans.length;
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Stop Group Replication…").click());
  await page.waitForSelector(memberWhy, { visible: true });
  await pause(300);
  assert.equal(plans.length, plansBeforeGrant, "the action with the missing privilege still plans nothing");
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

  // The same member page, connected to the primary: the same buttons as on
  // the secondary connection, with different enabled states and visible reasons.
  const primaryMember = R["ic-8.4-degraded"].members.find((m) => m.local);
  await h.select(`${view} tr[data-row-key="${primaryMember.key}"]`);
  const onPrimary = await page.$$eval(".view-inspector .review-actions > button", (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true", tab: e.tabIndex,
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim() })));
  assert.deepEqual(onPrimary.map((a) => a.label), Object.keys(secondaryEnabled), "primary and secondary connections list the same member actions");
  assert.notDeepEqual(Object.fromEntries(onPrimary.map((a) => [a.label, !a.disabled])), secondaryEnabled,
    "the enabled states differ between the primary and the secondary connection");
  const makeOnPrimary = onPrimary.find((a) => a.label === "Make primary…");
  assert.equal(makeOnPrimary.disabled, true, "Make primary is disabled on the primary");
  assert.equal(makeOnPrimary.tab, 0);
  assert.match(makeOnPrimary.reason, /mf-ic-1:3306 is already the primary\. Choose an ONLINE secondary\./);
  assert.equal(onPrimary.find((a) => a.label === "Stop Group Replication…").disabled, false, "Stop runs on the connected member");
  // The overview band lists the same cluster-wide actions on both connections.
  const degradedOverview = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(degradedOverview, R["ic-8.4-degraded"].actions.map((a) => a.label));
  assert.deepEqual(degradedOverview, overview.map((a) => a.label), "the same overview buttons on both connections");
  await backToList();

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

  // InnoDB ClusterSet: a band above the group, the primary cluster first,
  // an arrow to each replica cluster, the other clusters' instances from the
  // shared metadata (never drawn as members), and the state as words.
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-band`, { visible: true });
  assert.match(await h.text(`${view} .clusterset-band .cluster-row-label`), /ClusterSet mfcs/);
  const boxes = await page.$$eval(`${view} .clusterset-cluster`, (els) => els.map((e) => ({
    name: e.dataset.cluster, local: e.classList.contains("is-local"), text: e.textContent.replace(/\s+/g, " ") })));
  assert.deepEqual(boxes.map((b) => [b.name, b.local]), [["mfcluster", true], ["drcluster", false]]);
  assert.match(boxes[0].text, /Primary cluster.*This cluster/);
  assert.match(boxes[1].text, /Replica cluster.*mf-dr-1:3306.*mf-dr-2:3306.*mf-dr-3:3306/);
  assert.equal(await page.$$eval(`${view} .cluster-group .cluster-card`, (els) => els.length), 3, "other clusters are not members");
  assert.match(await h.text(`${view} .clusterset-link-label`), /^clusterset_replication$/);
  // The ClusterSet actions are cluster-wide, on the overview band (MySQL Shell
  // installed): the named replica cluster gets its own enabled button, the bare
  // placeholder keeps its reason.
  const csAction = R["ic-8.4-clusterset-primary"].actions.find((a) => a.id === "set_primary_cluster:drcluster");
  assert.ok(csAction && csAction.enabled, "the named ClusterSet action is an overview action, enabled");
  const csButtons = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true",
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim() })));
  assert.equal(csButtons.find((b) => b.label === "Make drcluster the primary cluster…").disabled, false);
  const rejoinPlaceholder = csButtons.find((b) => b.label === "Rejoin cluster to the ClusterSet…");
  assert.equal(rejoinPlaceholder.disabled, true);
  assert.match(rejoinPlaceholder.reason, /No invalidated cluster is recorded in this ClusterSet\./);
  await shot("cluster-clusterset-primary-1440.png");
  // From a replica cluster: its copy of the metadata, the channel on the arrow, the source marked.
  await refreshTo(R["ic-8.4-clusterset-replica"]);
  assert.match(await h.text(`${view} .clusterset-band .cluster-row-label`), /metadata copy, may be behind/);
  const link = await page.$eval(`${view} .clusterset-link`, (e) => ({ cls: e.className, text: e.textContent }));
  assert.match(link.cls, /is-ok/);
  assert.match(link.text, /receiver ON · applier ON/);
  assert.match(await h.text(`${view} .clusterset-tag.is-source`), /Channel source/);
  await refreshTo(R["ic-8.4-clusterset-replica-channel-off"]);
  const off = await page.$eval(`${view} .clusterset-link`, (e) => ({ cls: e.className, text: e.textContent,
    style: getComputedStyle(e.querySelector(".clusterset-arrow")).borderTopStyle }));
  assert.match(off.cls, /is-problem/);
  assert.match(off.text, /receiver OFF.*error 2003/);
  assert.equal(off.style, "dashed", "a stopped channel is a broken arrow, not color alone");
  // An invalidated replica cluster: the word, a dashed edge and a broken arrow.
  await refreshTo(R["ic-8.4-clusterset-invalidated"]);
  const dr = await page.$eval(`${view} .clusterset-cluster[data-cluster="drcluster"]`, (e) => ({
    cls: e.className, text: e.textContent, border: getComputedStyle(e).borderTopStyle }));
  assert.match(dr.cls, /is-invalidated/);
  assert.match(dr.text, /Invalidated/);
  assert.equal(dr.border, "dashed");
  assert.match(await h.text(`${view} .clusterset-link-label`), /Invalidated · not replicating/);
  await shot("cluster-clusterset-invalidated-1440.png");

  // Connected to a read replica of the ClusterSet's primary cluster: the band
  // still renders, from the replica's replicated copy of the metadata.
  await refreshTo(R["ic-8.4-read-replica-clusterset"]);
  await page.waitForSelector(`${view} .clusterset-band`, { visible: true });
  assert.match(await h.text(`${view} .clusterset-band .cluster-row-label`), /ClusterSet mfcs/);
  assert.match(await h.text(`${view} .clusterset-band .clusterset-copy`), /this cluster's metadata copy, may be behind/);
  assert.deepEqual(await page.$$eval(`${view} .clusterset-cluster`, (els) => els.map((e) => e.dataset.cluster)), ["mfcluster", "drcluster"]);
  assert.match((await visible(`${view} .summary-line`))[0], /this server is a read replica/);
  assert.match(await h.notes(), /the ClusterSet read here is its replicated copy of the metadata and may be behind/);

  // Multi-primary Group Replication without metadata: every member PRIMARY, no Routers tab.
  await refreshTo(R["gr-8.4-multi-primary"]);
  assert.deepEqual(await visible(`${view} .segmented button`), ["Topology", "Members"]);
  assert.deepEqual(await visible(`${view} .cluster-members .cluster-role`), ["Primary", "Primary", "Primary"]);
  assert.match((await visible(`${view} .summary-line`))[0], /MySQL Group Replication · 3 of 3 members ONLINE · multi-primary/);
  // An enabled cluster-wide action plans from the overview band, for the group.
  const plansBeforeMode = plans.length;
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.textContent.trim() === "Switch to single-primary…").click(), view);
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeMode + 1, "the enabled overview action plans");
  assert.deepEqual({ action: plans.at(-1).action, member: plans.at(-1).member_key },
    { action: "switch_to_single_primary", member: null }, "an overview action plans for the group, not a member");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));

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
  // A node action that must run on that node opens its message on click: the
  // reason and fix, with the saved connection for that node's address (Use
  // cluster-galera-2), which switches the connection.
  const galera2 = R["galera-11.8-healthy"].members.find((m) => m.name === "galera-2");
  await h.select(`${view} tr[data-row-key="${galera2.key}"]`);
  const nodeInspector = await h.inspector();
  assert.match(nodeInspector, /Desync runs on the node it changes, galera-2; this connection is to galera-1\./);
  assert.match(nodeInspector, /Connect to galera-2 to desync it\./);
  await page.evaluate(() => [...document.querySelectorAll(".view-inspector .review-actions > button")]
    .find((b) => b.textContent.trim() === "Desync this node…").click());
  await page.waitForSelector(".view-inspector .cluster-action-why:popover-open .cluster-use-node", { visible: true });
  const useFix = await page.$$eval(".view-inspector .cluster-action-why:popover-open .cluster-use-node", (els) => els.map((e) => e.textContent));
  assert.deepEqual(useFix, ["Use cluster-galera-2"], `the message carries the saved connection: ${useFix}`);
  await page.click(".view-inspector .cluster-action-why:popover-open .cluster-use-node");
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("cluster-galera-2"));
  await page.waitForFunction(() => !document.querySelector(".view-inspector .cluster-action-why:popover-open"));
  assert.equal(await page.$(".view-inspector .cluster-action-why:popover-open"), null, "the message does not carry over to another connection");
  await backToList();
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
  // Another connection's report offers no change: one blocked line under the
  // group, and a click opens the message leading with that line.
  const galeraLocal = R["galera-11.8-healthy"].members.find((m) => m.local);
  await page.click(`${view} .cluster-card[data-row-key="${galeraLocal.key}"]`);
  await page.waitForSelector(".view-inspector .review-actions button");
  assert.equal(await page.$eval(".view-inspector .review-actions button", (b) => b.getAttribute("aria-disabled")), "true");
  const plansBefore = plans.length;
  await page.click(".view-inspector .review-actions button");
  await page.waitForFunction((sel) => !!document.querySelector(sel)?.textContent.includes("This report was read on"), {},
    ".view-inspector .cluster-action-why:popover-open");
  const blockedMsg = await page.$eval(".view-inspector .cluster-action-why:popover-open", (e) => e.textContent);
  assert.match(blockedMsg, /^This report was read on/, `the blocked line leads the message: ${blockedMsg}`);
  assert.equal(plans.length, plansBefore, "nothing is planned on another connection's report");
  await page.click(".view-inspector .review-actions button");
  await page.waitForFunction(() => !document.querySelector(".view-inspector .cluster-action-why:popover-open"));
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
  // A connection chosen in the picker checks its readiness (connection
  // readiness contract): that check is not a view read.
  assert.deepEqual(state.other.filter((o) => o.path.startsWith("/api/live/") && !o.path.startsWith("/api/live/cluster/")
    && o.path !== "/api/live/action/progress" && o.path !== "/api/live/readiness").map((o) => o.path), [], "no other view read");
  // Server's Refresh all reads the cluster once, and the place shows that answer.
  await answer(R["mysql-8.4-standalone"]);
  const beforeAll = state.seen.length;
  await h.server();
  await page.evaluate(() => window.scrollTo(0, 0));
  await openRefreshMenu(page);
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
    await openRefreshMenu(page);
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

  // A read-only connection keeps a client block even when eligibility changes.
  // A slot-only disabled reason has the same sentence as an enabled-but-blocked
  // action, so the enabled transition must close it independently of text.
  await chooseConnection(page, await connectionOption(page, "Read-only"));
  const blockedReport = structuredClone(R["ic-8.4-healthy"]);
  Object.assign(blockedReport.actions[2], {
    enabled: false, why_not: null, fix: null, privileges_unreadable: true,
  });
  await refreshTo(blockedReport);
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel) => document.querySelector(sel)?.textContent.includes("Privileges could not be read"), {}, openWhy);
  const blockedReason = "This connection is read-only in myflames, so changes are refused.";
  assert.equal(await page.$eval(openWhy, (e) => e.querySelector("p")?.textContent), blockedReason,
    "the client block is the message's first sentence");
  assert.equal(await page.$eval(thirdWhyButton, (b) => b.title), blockedReason, "a blocked action's hover text is the client block");
  assert.equal(await page.$$eval(`${view} .cluster-actions .review-actions > .hint`, (els) => els.length), 1,
    "the group keeps one visible blocked line");
  const newlyEnabled = structuredClone(blockedReport);
  Object.assign(newlyEnabled.actions[2], { enabled: true, privileges_unreadable: false });
  await refreshTo(newlyEnabled, { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "an enabled transition closes the message even when the blocked sentence is unchanged");
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel, text) => document.querySelector(sel)?.textContent === text, {}, openWhy, blockedReason);
  await pause(100);
  assert.ok(await page.$(openWhy), "an already enabled but blocked action can still explain the client block");
  await refreshTo(blockedReport, { preservePopover: true });
  assert.ok(await page.$(openWhy), "the unchanged blocked message may stay open when the catalog disables the action");
  await refreshTo(newlyEnabled, { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "a later enabled transition also closes a message first opened while enabled and blocked");
  await chooseConnection(page, await connectionOption(page, "Node A"));

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
  // The action band wraps at 720 px, and a disabled action's open message
  // stays inside the viewport without horizontal page scroll, in both themes.
  await page.evaluate((sel) => document.querySelector(`${sel} .cluster-actions`)?.scrollIntoView({ block: "end" }), view);
  await pause(150);
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.getAttribute("aria-disabled") === "true").click(), view);
  await page.waitForFunction((sel) => {
    const open = document.querySelector(sel);
    return !!open && open.getClientRects().length > 0 && (open.textContent || "").length > 0;
  }, {}, `${view} .cluster-actions .cluster-action-why:popover-open`);
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(200);
    const wrap = await page.evaluate((sel) => {
      const msg = document.querySelector(`${sel} .cluster-actions .cluster-action-why:popover-open`);
      const r = msg ? msg.getBoundingClientRect() : null;
      return {
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        band: document.querySelector(`${sel} .cluster-actions`) ? 1 : 0,
        message: r ? { left: r.left, right: r.right, top: r.top, bottom: r.bottom, vw: window.innerWidth, vh: window.innerHeight } : null };
    }, view);
    assert.equal(wrap.band, 1, "the overview action band is present at 720 px");
    assert.ok(wrap.overflow <= 0, `horizontal page scroll at 720 px (${theme}): ${wrap.overflow}`);
    assert.ok(wrap.message, `the disabled action's message is open at 720 px (${theme})`);
    assert.ok(wrap.message.left >= 0 && wrap.message.top >= 0 && wrap.message.right <= wrap.message.vw && wrap.message.bottom <= wrap.message.vh,
      `the open message is inside the viewport at 720 px (${theme}): ${JSON.stringify(wrap.message)}`);
    await shot(`cluster-720-${theme}.png`);
    await whyShot(`cluster-why-720-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.getAttribute("aria-disabled") === "true").click(), view);
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .cluster-action-why:popover-open`), {}, view);
  await shot("cluster-720.png");
  await tab("Members");
  await pause(200);
  assert.ok((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0);
  const tableTop = await page.evaluate((sel) => ({ top: document.querySelector(`${sel} .data-table-wrap`).getBoundingClientRect().top,
    fold: document.querySelector(".window-statusbar").getBoundingClientRect().top }), view);
  assert.ok(tableTop.top < tableTop.fold - 24, `the members table starts above the fold: ${JSON.stringify(tableTop)}`);
  await page.setViewport({ width: 1440, height: 1000 });
  await h.narrow("cluster-390.png", { place: ".cluster-place" });

  // Multi-tier upstream tiers in ClusterTopology: ProxySQL -> Readyset -> InnoDB Cluster.
  await page.setViewport({ width: 1440, height: 1000 });
  const multiTierCluster = {
    ...R["ic-8.4-healthy"],
    topology_tiers: {
      proxysql: [
        {
          key: "proxysql:127.0.0.1:6032",
          label: "ProxySQL Admin",
          address: "127.0.0.1:6032",
          version: "3.0.11",
          routes_to_readyset: true,
          querypilot_rule_count: 2,
          profile_id: null,
          profile_name: "ProxySQL Admin",
          verified: true,
          evidence: "mysql_servers",
        },
      ],
      readyset: [
        {
          key: "readyset:127.0.0.1:3307",
          label: "Readyset Cache",
          address: "127.0.0.1:3307",
          version: "stable-250605",
          status: "Online",
          replication_status: "Running",
          database_connection: "Connected",
          replication_error: false,
          purpose: "cdc, query",
          cache_count: 7,
          profile_id: null,
          profile_name: "Readyset Cache",
          verified: true,
          evidence: "upstream_servers",
        },
      ],
      live_readyset_connections: 3,
      readyset_heartbeat_schema: true,
      notes: [],
    },
  };
  await refreshTo(multiTierCluster);
  await tab("Topology");
  await page.waitForSelector(`${view} .upstream-tiers`, { visible: true });
  const clusterTierText = await h.text(`${view} .upstream-tiers`);
  assert.match(clusterTierText, /ProxySQL routing tier/);
  assert.match(clusterTierText, /2 QueryPilot rules/);
  assert.match(clusterTierText, /Readyset cache tier/);
  assert.match(clusterTierText, /7 caches/);
  // Lane rs1-B: ProxySQL reports no own health here, so its card reads Not
  // read and its link (the first) stays unread. The reported Replication
  // "Running" runs the Readyset -> database link with an active stem.
  assert.match(clusterTierText, /Not read/);
  const clusterTierLinks = await page.$$eval(`${view} .upstream-tiers .tier-link`, (els) => els.map((e) => e.className));
  assert.match(clusterTierLinks[0] || "", /is-unread/, "the ProxySQL link stays unread without a reported running state");
  assert.match(
    clusterTierLinks[clusterTierLinks.length - 1] || "",
    /is-running/,
    "a reported Running replication runs the Readyset tier link",
  );
  assert.ok(await page.$(`${view} .upstream-tiers .cluster-stem.is-active`), "a reported Running replication activates the Readyset stem");
  // Lane F9: on the shared tier topology the dot travel follows the shorter
  // stems, so the dot never travels into a tier card.
  const tierStemDots = await page.$$eval(`${view} .upstream-tiers .cluster-stem.is-active`, (stems) => {
    const probe = document.body.appendChild(document.createElement("div"));
    const rows = stems.map((stem) => {
      const travel = getComputedStyle(stem).getPropertyValue("--stem-dot-travel").trim() || "18px";
      probe.style.cssText = `position:absolute;visibility:hidden;transform:translateY(${travel})`;
      const t = getComputedStyle(probe).transform;
      const ty = t === "none" ? 0 : parseFloat(t.slice(7, -1).split(",").pop());
      return {
        cls: String(stem.className),
        height: stem.getBoundingClientRect().height,
        dot: parseFloat(getComputedStyle(stem, "::after").height),
        travel: ty,
      };
    });
    probe.remove();
    return rows;
  });
  for (const dot of tierStemDots) {
    assert.ok(Number.isFinite(dot.travel) && dot.travel >= 0, `tier stem dot travel resolves: ${JSON.stringify(dot)}`);
    assert.ok(dot.dot <= 6 && dot.travel + dot.dot <= dot.height + 0.5, `motion dot stays on the tier stem: ${JSON.stringify(dot)}`);
  }
  assert.match(clusterTierText, /CDC binlog stream & query fallback/, "a running link carries the CDC label");
  // S8: server-reported versions inside the compound role labels sit in <bdi>.
  const roleBdis = await page.$$eval(`${view} .upstream-tiers .cluster-role bdi`, (els) => els.map((e) => e.textContent));
  assert.deepEqual(roleBdis, ["3.0.11", "stable-250605"], "server versions in role labels are bidi-isolated");
  assert.equal(await page.$(`${view} .upstream-tiers .cluster-use-node`), null, "no Use chip without a backend profile_id");
  await shot("cluster-multitier-1440.png");

  // With the Readyset tier's state stripped the way the backend reports it
  // (Status null, Replication Status null), no card has an OK mark and the
  // card reads Not read (never Online, never OK, never Unknown).
  const strippedTiers = {
    ...multiTierCluster,
    topology_tiers: {
      ...multiTierCluster.topology_tiers,
      readyset: multiTierCluster.topology_tiers.readyset.map((n) => ({ ...n, status: null, replication_status: null })),
    },
  };
  await refreshTo(strippedTiers);
  await tab("Topology");
  await page.waitForSelector(`${view} .upstream-tiers`, { visible: true });
  assert.equal(await page.$(`${view} .upstream-tiers .cluster-mark.is-ok`), null, "no upstream tier card has an OK mark without a reported state");
  assert.equal(await page.$(`${view} .upstream-tiers .tier-link.is-running`), null, "no upstream tier link runs without a reported state");
  const strippedText = await h.text(`${view} .upstream-tiers`);
  assert.match(strippedText, /Not read/);
  assert.match(strippedText, /Replication not read · query fallback/, "an unread replication link says so");
  assert.doesNotMatch(strippedText, /Online · Repl/, "an unreported Readyset state never renders as Online");
  assert.doesNotMatch(strippedText, /Unknown/, "the backend's unread placeholder never renders");
  await refreshTo(multiTierCluster);

  // A reported replicator error (the new tier field) draws the Readyset card
  // detached with the problem tone and breaks the link.
  const errorTiers = {
    ...multiTierCluster,
    topology_tiers: {
      ...multiTierCluster.topology_tiers,
      readyset: multiTierCluster.topology_tiers.readyset.map((n) => ({ ...n, replication_error: true })),
    },
  };
  await refreshTo(errorTiers);
  await tab("Topology");
  await page.waitForSelector(`${view} .upstream-tiers`, { visible: true });
  assert.ok(await page.$(`${view} .upstream-tiers .tier-link.is-error`), "a reported replicator error draws the error link");
  assert.ok(
    await page.$(`${view} .upstream-tiers .tier-node.is-detached .cluster-card.is-detached`),
    "a reported replicator error detaches the Readyset card",
  );
  assert.match(await h.text(`${view} .upstream-tiers`), /Replication error or upstream unreachable/, "an errored link says so");
  await refreshTo(multiTierCluster);

  // S12: a ProxySQL tier linked only by address evidence (mysql_servers) is
  // labelled Matched by address, never Verified.
  const addrOnly = {
    ...multiTierCluster,
    topology_tiers: {
      ...multiTierCluster.topology_tiers,
      proxysql: multiTierCluster.topology_tiers.proxysql.map((n) => ({ ...n, querypilot_rule_count: 0 })),
    },
  };
  await refreshTo(addrOnly);
  await tab("Topology");
  await page.waitForSelector(`${view} .upstream-tiers`, { visible: true });
  const addrText = await h.text(`${view} .upstream-tiers`);
  assert.match(addrText, /Matched by address/, "an address-only ProxySQL link is labelled as such");
  assert.doesNotMatch(addrText, /Verified/, "an address-only match never reads Verified");
  await refreshTo(multiTierCluster);

  assert.deepEqual(h.errors, []);
  console.log("cluster: ok");
} finally {
  await h.close();
}
