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
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chooseConnection, connectionOption, openConnectionPicker, openRefreshMenu } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";

const csShots = fileURLToPath(new URL("../../../local/cs-shots/", import.meta.url));
mkdirSync(csShots, { recursive: true });

const R = {};
for (const name of ["galera-11.8-healthy", "galera-11.8-donor", "galera-11.8-two-of-three", "galera-11.8-non-primary", "pxc-8.4-healthy",
  "pxc-8.4-donor", "pxc-8.4-member-donor", "ic-8.4-healthy", "ic-8.4-degraded", "ic-8.4-unreachable", "ic-8.4-no-quorum",
  "ic-8.4-member-offline", "ic-8.4-local-offline", "gr-8.4-multi-primary", "gr-8.4-denied", "mysql-8.4-standalone",
  "mariadb-11.8-standalone", "pxc-8.4-two-of-three", "galera-11.8-two-nodes", "galera-11.8-partial-seeds", "galera-mysql-8.4-healthy",
  "galera-11.8-wsrep-off", "ic-8.4-read-replica", "ic-8.4-read-replica-local", "plan-switchover", "result-switchover",
  "progress-sending", "ic-8.4-clusterset-primary", "ic-8.4-clusterset-replica", "ic-8.4-clusterset-replica-channel-off",
  "ic-8.4-clusterset-invalidated", "ic-8.4-read-replica-clusterset",
  "ic-8.4-clusterset-primary-fenced", "ic-8.4-clusterset-primary-no-quorum"]) R[name] = await load(`cluster-${name}.json`);

const view = '.server-view[data-view="cluster"]';
const nav = '.main-nav button[data-place="cluster"]';
const plans = [];
const sends = [];
const declined = [];
const connectCalls = [];
let planDelay = 0;
const h = await open("/api/live/cluster", "cluster", {
  "/api/live/cluster/plan": (body) => {
    plans.push(body);
    const plan = body.action === "check_clusterset" ? {
      ...R["plan-switchover"], action: "check_clusterset", title: "Check ClusterSet with MySQL Shell?", confirm_label: "Check ClusterSet",
      typed_guard: null, review_token: "check-token", statements: ["print(JSON.stringify(myflamesClusterSet.status({extended: 1}), null, 2));"],
      policy: { action: "allow", tone: "danger", title: "Read", text: "This check only reads.", connection_type: "production", read_only: true },
    } : R["plan-switchover"];
    return { status: 200, body: plan, delay: planDelay };
  },
  "/api/live/cluster/action": (body) => { sends.push(body); return { status: 200, body: R["result-switchover"], delay: 1500 }; },
  "/api/live/action/progress": () => ({ status: 200, body: R["progress-sending"] }),
  "/api/audit/declined": (body) => { declined.push(body); return { status: 200, body: { audit_id: "x", status: "declined" } }; },
  // The ClusterSet fixture's routes name the members of group "production":
  // "local" (Local member, mf-ic-1) and "remote" (DR member, mf-dr-1).
  "/api/connect": (body) => {
    connectCalls.push(body);
    const name = typeof body.group_name === "string" ? body.group_name.trim() : "";
    if (name !== "production") return { status: 400, body: { error: "No saved connection belongs to this connection group." } };
    const member = (id, profileName, host, role) => ({
      profile_id: id, profile_name: profileName, host, port: 3306,
      safety: { type: "production", read_only: false },
      server_uuid: null, role, state: null, reachable: true,
    });
    return { status: 200, body: {
      version: "8.4.3", engine: "mysql", flavor: "mysql", product: "MySQL",
      databases: [], schemas: [],
      group_connection: {
        group_name: "production",
        effective_safety: { type: "production", read_only: false },
        active_member_profile_id: "local",
        primary_profile_id: "local",
        members: [member("local", "Local member", "mf-ic-1", "PRIMARY"), member("remote", "DR member", "mf-dr-1", "SECONDARY")],
      },
    } };
  },
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
const MOD = process.platform === "darwin" ? "Meta" : "Control";
const rgbChannels = (cssColor) => {
  const m = String(cssColor || "").match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
};
const luminance = (rgb) => (rgb ? 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] : NaN);
const findAx = (node, pred) => {
  if (!node) return null;
  if (pred(node)) return node;
  for (const child of node.children || []) {
    const hit = findAx(child, pred);
    if (hit) return hit;
  }
  return null;
};
async function answer(report) {
  state.fallback = { status: 200, body: report };
}
/** Select appearance through Settings > Appearance (the app's own preference
 * flow, without opening the command palette or sending catalog reads) and
 * wait for persistence so no stale preference reply repaints before a shot (W9). */
async function setTheme(theme) {
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("myflames-popover", { detail: "" })));
  await page.keyboard.down(MOD);
  await page.keyboard.press(",");
  await page.keyboard.up(MOD);
  await page.waitForSelector(`.settings-page .theme-card[data-theme-id="${theme}"]`, { visible: true });
  await page.click(`.settings-page .theme-card[data-theme-id="${theme}"]`);
  await page.evaluate(async () => { await window.__myflamesFlushUiPreferences?.(); });
  await page.waitForFunction(async (expected) => {
    const current = document.documentElement.dataset.theme || "system";
    if (current !== expected) return false;
    const reply = await fetch("/api/settings/ui", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" },
      body: "{}",
    }).then((r) => r.json());
    return reply.preferences?.theme === expected;
  }, {}, theme);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));
  await page.waitForSelector(view, { visible: true });
}
async function assertThemeColors(theme, msgSelector, contextLabel) {
  const colors = await page.evaluate((sel) => {
    const msg = document.querySelector(sel);
    return {
      attr: document.documentElement.dataset.theme || "system",
      scheme: getComputedStyle(document.documentElement).colorScheme,
      bodyBg: getComputedStyle(document.body).backgroundColor,
      msgBg: msg ? getComputedStyle(msg).backgroundColor : "",
      msgInk: msg ? getComputedStyle(msg).color : "",
    };
  }, msgSelector);
  assert.equal(colors.attr, theme, `${contextLabel}: data-theme is ${theme}`);
  assert.equal(colors.scheme, theme, `${contextLabel}: color-scheme is ${theme}`);
  const bodyL = luminance(rgbChannels(colors.bodyBg));
  const msgBgL = luminance(rgbChannels(colors.msgBg));
  const msgInkL = luminance(rgbChannels(colors.msgInk));
  if (theme === "dark") {
    assert.ok(bodyL < 80 && msgBgL < 80 && msgInkL > 150, `${contextLabel}: expected dark colors, got ${JSON.stringify(colors)}`);
  } else {
    assert.ok(bodyL > 200 && msgBgL > 200 && msgInkL < 120, `${contextLabel}: expected light colors, got ${JSON.stringify(colors)}`);
  }
  return colors;
}
async function assertNoChromeOverlap(msgSelector, contextLabel) {
  const check = await page.evaluate((sel) => {
    const msg = document.querySelector(sel);
    if (!msg) return { found: false };
    const r = msg.getBoundingClientRect();
    const overlaps = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
    const statusBar = document.querySelector(".window-statusbar");
    const statusRect = statusBar && statusBar.getClientRects().length ? statusBar.getBoundingClientRect() : null;
    const stickyBars = [...document.querySelectorAll(".topbar-stack, .view-toolbar, .place-toolbar, .editor-toolbar, .teach-toolbar, .connection-group-bar")]
      .filter((el) => {
        if (!el.isConnected || el.closest('[hidden], [inert], [aria-hidden="true"]') || !el.getClientRects().length) return false;
        const pos = getComputedStyle(el).position;
        return pos === "sticky" || pos === "fixed";
      })
      .map((el) => {
        const b = el.getBoundingClientRect();
        return { cls: el.className, top: b.top, bottom: b.bottom, left: b.left, right: b.right, overlap: overlaps(r, b) };
      });
    return {
      found: true,
      rect: { left: r.left, right: r.right, top: r.top, bottom: r.bottom, vw: window.innerWidth, vh: window.innerHeight },
      statusBar: statusRect ? { top: statusRect.top, bottom: statusRect.bottom, overlap: overlaps(r, statusRect) } : null,
      stickyBars,
    };
  }, msgSelector);
  assert.equal(check.found, true, `${contextLabel}: message is open`);
  assert.ok(
    check.rect.left >= 0 && check.rect.top >= 0 && check.rect.right <= check.rect.vw && check.rect.bottom <= check.rect.vh,
    `${contextLabel}: inside viewport: ${JSON.stringify(check.rect)}`,
  );
  if (check.statusBar) {
    assert.equal(check.statusBar.overlap, false, `${contextLabel}: never covers fixed status bar: ${JSON.stringify(check)}`);
    assert.ok(check.rect.bottom <= check.statusBar.top + 0.5, `${contextLabel}: ends above status bar: ${JSON.stringify(check)}`);
  }
  for (const bar of check.stickyBars) {
    assert.equal(bar.overlap, false, `${contextLabel}: never covers sticky toolbar ${bar.cls}: ${JSON.stringify(check)}`);
  }
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
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line, ${sel} .clusterset-map`), {}, view);
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
  await fs.mkdir(h.shots, { recursive: true });
  await api("/api/profiles/save", { value: { name: "Node A", host: "node-a.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await api("/api/profiles/save", { value: { name: "Solo", host: "solo.test", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await api("/api/profiles/save", { value: { name: "cluster-galera-2", host: "192.168.80.3", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  // A saved connection at a drcluster member's address, in the group the
  // ClusterSet fixture's routes name: the metadata-only zoom's fix names it
  // ("Use DR member"), never a new connection, and the group opens as one
  // Group connection for the routed zoom.
  await api("/api/profiles/save", { value: { name: "DR member", host: "mf-dr-1", port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "", group: "production" } });
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

  // W11: double click plus an Enter/Space burst on a disabled action sends
  // 0 plans and 0 requests.
  await page.click(thirdWhyButton, { clickCount: 2 });
  await page.focus(thirdWhyButton);
  for (const key of ["Enter", "Space", "Enter", "Space", "Enter"]) await page.keyboard.press(key);
  await pause(200);
  assert.equal(plans.length, plansBeforeDisabled, "double click + Enter/Space burst plans nothing");
  assert.equal(sends.length, 0, "double click + Enter/Space burst sends nothing");
  assert.equal(await page.$("dialog.action-review[open]"), null, "double click + Enter/Space burst opens no review sheet");
  if (await page.$(openWhy)) {
    await page.keyboard.press("Escape");
    await page.waitForFunction((sel) => !document.querySelector(sel), {}, openWhy);
  }

  // W2: shared popover coordination with About Cluster. Opening About Cluster
  // while a disabled message is open closes the message, and Escape closes
  // About Cluster; opening a disabled message while About Cluster is open
  // closes About Cluster.
  await page.focus(thirdWhyButton);
  await page.keyboard.press("Enter");
  await page.waitForSelector(openWhy, { visible: true });
  await page.click(`${view} button[aria-label="About Cluster"]`);
  await page.waitForSelector('[role=dialog][aria-label="About Cluster"]', { visible: true });
  assert.equal(await page.$(openWhy), null, "opening About Cluster closes the older disabled-action message");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Cluster"]'));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "About Cluster",
    "Escape closes About Cluster and returns focus to its button");
  await page.click(`${view} button[aria-label="About Cluster"]`);
  await page.waitForSelector('[role=dialog][aria-label="About Cluster"]', { visible: true });
  await page.focus(thirdWhyButton);
  await page.keyboard.press("Enter");
  await page.waitForSelector(openWhy, { visible: true });
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="About Cluster"]'));
  assert.equal(await page.$('[role=dialog][aria-label="About Cluster"]'), null, "opening a disabled-action message closes About Cluster");
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, openWhy);

  // W1 (H1): opening with Enter then navigating back with Cmd+[ closes the
  // hidden place's message immediately, so the very first Escape in the next
  // place reaches a document listener un-consumed.
  await page.focus(`${view} .cluster-actions .review-actions > button`);
  await page.keyboard.press("Enter");
  await page.waitForSelector(openWhy, { visible: true });
  await page.keyboard.down(MOD);
  await page.keyboard.press("[");
  await page.keyboard.up(MOD);
  await page.waitForFunction((sel) => !document.querySelector(`${sel}.active`), {}, nav);
  await page.waitForFunction(() => !document.querySelector(".cluster-action-why:popover-open"));
  await page.evaluate(() => {
    window.__escRecorded = [];
    window.__escHandler = (e) => {
      if (e.key === "Escape") window.__escRecorded.push({ defaultPrevented: e.defaultPrevented });
    };
    document.addEventListener("keydown", window.__escHandler);
  });
  await page.keyboard.press("Escape");
  const escRecorded = await page.evaluate(() => {
    document.removeEventListener("keydown", window.__escHandler);
    return window.__escRecorded;
  });
  assert.equal(escRecorded.length, 1, "first Escape after Cmd+[ reaches the document un-eaten");
  assert.equal(escRecorded[0].defaultPrevented, false, "hidden WhyDisabled does not preventDefault on Escape");
  await page.click(nav);
  await page.waitForSelector(`${nav}.active`);
  await page.waitForSelector(`${view} .cluster-topology`, { visible: true });

  // W3 (missing button fallback): focus inside the message when a refresh
  // removes the action altogether moves focus to a visible fallback, never body.
  await page.click(thirdWhyButton);
  await page.waitForSelector(`${openWhy} .why-disabled-close`, { visible: true });
  await page.focus(`${openWhy} .why-disabled-close`);
  const withoutThird = structuredClone(R["ic-8.4-healthy"]);
  withoutThird.actions = withoutThird.actions.slice(0, 2);
  await refreshTo(withoutThird, { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "removing the action closes its message");
  const fallbackFocus = await page.evaluate(() => ({
    isBody: document.activeElement === document.body || !document.activeElement,
    visible: !!document.activeElement && document.activeElement.getClientRects().length > 0,
  }));
  assert.equal(fallbackFocus.isBody, false, "focus never drops to body when the action unmounts");
  assert.equal(fallbackFocus.visible, true, "fallback focus lands on a visible element");
  await refreshTo(R["ic-8.4-healthy"]);

  // Refresh invalidates an open explanation; server text remains literal in
  // both the hidden description and the message, even when it resembles HTML
  // or contains U+200B and U+202E (W11).
  await page.click(thirdWhyButton);
  await page.waitForSelector(openWhy, { visible: true });
  const changedWhy = structuredClone(R["ic-8.4-healthy"]);
  const literalReason = '<img src=x onerror="window.__whyInjection = true"> needs <script>privileges</script>.';
  const literalFix = '<svg onload="window.__whyInjection = true">Grant\u200b\u202e it first.';
  changedWhy.actions[2].why_not = literalReason;
  changedWhy.actions[2].fix = literalFix;
  await refreshTo(changedWhy, { preservePopover: true });
  assert.equal(await page.$(openWhy), null, "Refresh closes a message when its explanation changes");
  await page.click(thirdWhyButton);
  await page.waitForFunction((sel, reason) => document.querySelector(sel)?.textContent.includes(reason), {}, openWhy, literalReason);
  assert.ok((await page.$eval(openWhy, (e) => e.textContent)).includes(literalFix), "fix with markup, U+200B and U+202E stays literal text");
  assert.equal(await page.$eval(openWhy, (e) => e.querySelectorAll("img, script, svg:not(.why-disabled-close svg)").length), 0, "server text never becomes message markup");
  assert.equal(await page.$eval(thirdWhyButton, (b) => document.getElementById(b.getAttribute("aria-describedby")).querySelectorAll("img, svg, script").length),
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
  // W5: verify the Chromium accessibility tree exposes the named non-modal
  // dialog and its controls (Copy, Close).
  const grantAx = await page.accessibility.snapshot();
  const grantDialog = findAx(grantAx, (n) => n.role === "dialog" && n.name === "Why Stop Group Replication is unavailable");
  assert.ok(grantDialog, `accessibility tree exposes named dialog for Stop Group Replication`);
  assert.ok(findAx(grantDialog, (n) => n.role === "button" && n.name === "Copy: Grant for Stop Group Replication"), "Copy control is inside the named dialog");
  assert.ok(findAx(grantDialog, (n) => n.role === "button" && n.name === "Close"), "Close control is inside the named dialog");
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, memberWhy);
  assert.ok(await page.$(".view-inspector"), "Escape dismisses only the message, keeping its inspector open");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim()), "Stop Group Replication…",
    "Escape from inside the message returns focus to the action");

  // W1 (H1): with the message open, moving focus to an inspector control
  // outside the message lets a single Escape close the inspector.
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(memberWhy, { visible: true });
  await page.focus(".view-inspector .detail-back");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  assert.equal(await page.$(memberWhy), null, "inspector closes on one Escape when focus is outside the message");

  // W3 + S-F1: focus inside the message (Copy) never drops to body when a
  // refresh enables the action and unmounts the message, and never lands on
  // the newly enabled action button (lands on a safe visible fallback instead).
  await h.select(`${view} tr[data-row-key="${grantMember.key}"]`);
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(`${memberWhy} .ops-commands button`, { visible: true });
  await page.focus(`${memberWhy} .ops-commands button`);
  const enabledReport = structuredClone(grantReport);
  const enabledStop = enabledReport.members.find((m) => m.local).actions.find((a) => a.id === "stop_member");
  enabledStop.enabled = true;
  enabledStop.why_not = null;
  enabledStop.fix = null;
  enabledStop.grant_sql = null;
  await refreshTo(enabledReport, { preservePopover: true });
  assert.equal(await page.$(memberWhy), null, "enabling the action closes and unmounts the message");
  const enabledFocus = await page.evaluate(() => ({
    text: document.activeElement?.textContent?.trim() || "",
    isBody: !document.activeElement || document.activeElement === document.body,
    visible: !!document.activeElement && document.activeElement.getClientRects().length > 0,
  }));
  assert.equal(enabledFocus.isBody, false, "W3: focus never drops to body when enabling the action unmounts the message");
  assert.equal(enabledFocus.visible, true, "W3: focus lands on a visible fallback element");
  assert.notEqual(enabledFocus.text, "Stop Group Replication…",
    "S-F1: focus never moves onto the newly enabled action button");

  // W11: fix and grant_sql containing markup plus U+200B and U+202E add no
  // elements, run no script, and Copy replaces U+202E with a space while keeping literal markup and U+200B.
  const abuseGrantReport = structuredClone(grantReport);
  const abuseStop = abuseGrantReport.members.find((m) => m.local).actions.find((a) => a.id === "stop_member");
  abuseStop.fix = '<img src=x onerror="window.__w11Fix=1"><svg onload="window.__w11Fix=2">Grant\u200b\u202e it.';
  abuseStop.grant_sql = "GRANT GROUP_REPLICATION_ADMIN ON *.* TO '<img src=x onerror=window.__w11Grant=1><svg onload=window.__w11Grant=2>\u200b\u202e'@'%';";
  await refreshTo(abuseGrantReport);
  await page.evaluate(() => { window.__grants = []; navigator.clipboard.writeText = (t) => (window.__grants.push(t), Promise.resolve()); });
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(`${memberWhy} .ops-commands button`, { visible: true });
  assert.equal(await page.$eval(memberWhy, (e) => e.querySelectorAll("img, script, svg:not(.why-disabled-close svg)").length), 0, "W11: no element created from fix or grant_sql");
  assert.equal(await page.evaluate(() => window.__w11Fix || window.__w11Grant || 0), 0, "W11: no script executes");
  await page.click(`${memberWhy} .ops-commands button`);
  await page.waitForFunction(() => /Copied\./.test(document.querySelector(".view-inspector .cluster-action-why:popover-open .ops-commands [role=status]")?.textContent || ""));
  assert.equal(
    await page.evaluate(() => window.__grants.at(-1)),
    abuseStop.grant_sql.replace(/\u202e/g, " "),
    "W11 + lead 27: Copy replaces bidi override U+202E with a space while keeping literal markup and U+200B",
  );
  assert.equal(
    await page.$eval(".view-inspector .cluster-action-why:popover-open .ops-commands [role=status]", (e) => e.textContent),
    "Copied. Removed 1 control character.",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, memberWhy);
  await refreshTo(grantReport);

  // A long GRANT scrolls inside its box instead of stretching the page, in both
  // real appearances selected through the app's preference flow (W8, W9).
  const grantBg = {};
  for (const theme of ["light", "dark"]) {
    await setTheme(theme);
    await clickMemberAction("Stop Group Replication…");
    await page.waitForSelector(`${memberWhy} .ops-pre`, { visible: true });
    await pause(150);
    const box = await page.evaluate(() => {
      const msg = document.querySelector(".view-inspector .cluster-action-why:popover-open");
      const e = msg?.querySelector(".ops-pre");
      return {
        found: !!e,
        maxH: e ? getComputedStyle(e).maxHeight : "",
        scroll: e ? e.scrollHeight : 0, client: e ? e.clientHeight : 0,
        pre: e ? e.scrollHeight - e.clientHeight : null,
        page: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    assert.ok(box.pre > 0, `a long GRANT scrolls inside its box (${theme}): ${JSON.stringify(box)}`);
    assert.ok(box.page <= 0, `no horizontal page scroll with the GRANT open (${theme}): ${box.page}`);
    const colors = await assertThemeColors(theme, memberWhy, `GRANT 1440 ${theme}`);
    grantBg[theme] = colors.msgBg;
    await shot(`cluster-why-grant-1440-${theme}.png`);
    await page.keyboard.press("Escape");
    await page.waitForFunction((sel) => !document.querySelector(sel), {}, memberWhy);
  }
  assert.notEqual(grantBg.light, grantBg.dark, `GRANT message background differs between light and dark: ${JSON.stringify(grantBg)}`);
  await setTheme("system");
  await clickMemberAction("Stop Group Replication…");
  await page.waitForSelector(`${memberWhy} .ops-pre`, { visible: true });
  await pause(150);
  await page.evaluate(() => { window.__grants = []; navigator.clipboard.writeText = (t) => (window.__grants.push(t), Promise.resolve()); });
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

  // InnoDB ClusterSet: the Topology tab opens on the zoomable set map. One
  // card per cluster: role, the status word with the Shell term as secondary
  // text, member count and primary when read, and how it was read; an arrow
  // from the primary cluster to each replica carries the channel state.
  const csShot = (file) => page.screenshot({ path: path.join(csShots, file), fullPage: false });
  const csCards = () => page.$$eval(`${view} .clusterset-map .clusterset-cluster`, (els) => els.map((e) => ({
    name: e.dataset.cluster, local: e.classList.contains("is-local"), text: (e.innerText || e.textContent).replace(/\s+/g, " ") })));
  const openCluster = async (name, how) => {
    const sel = `${view} .clusterset-cluster[data-cluster="${name}"]`;
    if (how === "button") await page.click(`${sel} .clusterset-open`);
    else if (how === "double") await page.evaluate((s) => document.querySelector(s)
      .dispatchEvent(new MouseEvent("dblclick", { bubbles: true })), sel);
    else { await page.focus(sel); await page.keyboard.press("Enter"); }
    await page.waitForSelector(`${view} .clusterset-breadcrumb`, { visible: true });
    await pause(250);
  };
  const zoomOut = async (how) => {
    if (how === "button") await page.click(`${view} .clusterset-zoom-out`);
    else { await page.keyboard.down(MOD); await page.keyboard.press("ArrowUp"); await page.keyboard.up(MOD); await pause(100); }
    await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
    await pause(250);
  };
  const pressZoomOut = async () => {
    await page.keyboard.down(MOD); await page.keyboard.press("ArrowUp"); await page.keyboard.up(MOD);
    await pause(100);
  };
  const waitForZoomRead = async (name) => {
    await page.waitForFunction((sel, cluster) => !document.querySelector(`${sel} .clusterset-pending`)
      && document.querySelector(`${sel} .summary-line`)?.textContent.includes(`InnoDB Cluster ${cluster}`), {}, view, name);
  };
  const holdZoomRead = (body, status = 200) => {
    let release;
    const wait = new Promise((resolve) => { release = resolve; });
    state.queue.push({ status, body, wait });
    return release;
  };
  // Every regenerated scenario carries the complete backend catalog. Removal
  // has two catalog variants but one visible choice button: nine controls.
  const setFamilies = ["set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "repair_cluster",
    "remove_cluster", "remove_cluster_keep", "set_routing_option", "create_replica_cluster",
    "dissolve_clusterset", "check_clusterset"];
  for (const [name, report] of Object.entries(R).filter(([, r]) => r.clusterset)) {
    const entries = report.actions.filter((a) => a.scope === "clusterset");
    assert.deepEqual(entries.map((a) => a.id.split(":")[0]), setFamilies, `${name}: all catalog families`);
    assert.equal(new Set(report.actions.map((a) => a.id)).size, report.actions.length, `${name}: unique action IDs`);
    assert.ok(entries.every((a) => a.enabled || (a.why_not && a.fix)), `${name}: every disabled entry explains why and how to proceed`);
    await refreshTo(report);
    const controls = await page.$$eval(`${view} .cluster-actions[aria-label="ClusterSet changes"] .review-actions > button`,
      (buttons) => buttons.map((b) => ({ label: b.textContent.trim(), disabled: b.getAttribute("aria-disabled") === "true",
        reason: document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "" })));
    // This connection is individual (no group): an action routed through
    // another member's saved connection is disabled with the reason instead of
    // being planned without its route (ClusterPlace.gateUngroupedRoutes).
    const peerReadFamilies = new Set(["set_primary_cluster", "force_primary_cluster", "rejoin_cluster", "repair_cluster",
      "remove_cluster", "remove_cluster_keep", "create_replica_cluster"]);
    const gated = (a) => a.enabled && (peerReadFamilies.has(a.id.split(":")[0]) || (!a.target?.self && a.target?.profile_id))
      ? { ...a, enabled: false, why_not: "only an open Group connection may use" } : a;
    const gatedEntries = entries.map(gated);
    const expected = gatedEntries.filter((a) => !a.id.startsWith("remove_cluster_keep:")).map((a) => {
      if (!a.id.startsWith("remove_cluster:")) return a;
      const pair = gatedEntries.filter((e) => e.id === a.id || e.id === a.id.replace("remove_cluster:", "remove_cluster_keep:"));
      return { ...a, label: `Remove ${a.cluster_name} from the ClusterSet…`, enabled: pair.some((e) => e.enabled) };
    });
    assert.equal(controls.length, 9, `${name}: all nine set controls render`);
    assert.deepEqual(controls.map((b) => b.label), expected.map((a) => a.label), `${name}: real catalog labels`);
    for (const [i, action] of expected.entries()) {
      assert.equal(controls[i].disabled, !action.enabled, `${name}: ${action.id} eligibility`);
      if (!action.enabled) assert.ok(controls[i].reason.includes(action.why_not), `${name}: ${action.id} retains its disabled reason`);
    }
  }

  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  const setLine = await h.text(`${view} .clusterset-setline`);
  assert.match(setLine, /ClusterSet mfcs/);
  assert.match(setLine, /Healthy/);
  assert.match(setLine, /Read on Local member \(this connection\)/);
  let csCardList = await csCards();
  assert.deepEqual(csCardList.map((c) => [c.name, c.local]), [["mfcluster", true], ["drcluster", false]]);
  assert.match(csCardList[0].text, /primary cluster/i);
  assert.match(csCardList[0].text, /OK OK/, `the status word with the Shell term: ${csCardList[0].text}`);
  assert.match(csCardList[0].text, /3 members · primary mf-ic-1:3306/);
  assert.match(csCardList[0].text, /This connection/);
  assert.match(csCardList[1].text, /replica cluster/i);
  assert.match(csCardList[1].text, /3 members · primary mf-dr-1:3306/);
  assert.match(csCardList[1].text, /Read through DR member/);
  assert.match(csCardList[1].text, /Not checked/,
    "running replica threads do not establish Shell's GTID consistency verdict");
  assert.equal(await page.$(`${view} .clusterset-cluster[data-cluster="drcluster"] .clusterset-shell`), null,
    "an unchecked replica has no Shell OK term");
  assert.match(await page.$eval(`${view} .clusterset-cluster[data-cluster="drcluster"] .cluster-mark`,
    (e) => e.className), /is-unknown/, "an unchecked replica never gets a green status mark");
  assert.match(await h.text(`${view} .clusterset-link-label`), /clusterset_replication · receiver ON · applier ON/);
  // The set status reason is on demand, the Findings Details pattern.
  const setReason = `${view} .clusterset-setline .clusterset-reason-text`;
  assert.equal(await page.$eval(setReason, (e) => e.hidden), true, "the set status reason is on demand");
  await page.click(`${view} .clusterset-setline .cluster-finding-toggle`);
  assert.equal(await page.$eval(setReason, (e) => e.hidden), false);
  assert.match(await page.$eval(setReason, (e) => e.textContent), /Every cluster was read with quorum/);
  await page.click(`${view} .clusterset-setline .cluster-finding-toggle`);
  // The set level leads with the ClusterSet (set status line, set changes,
  // map): the local cluster's own summary (status line, Quorum/Members/This
  // member/Mode cards, group line) renders only when zoomed into it.
  assert.equal(await page.$(`${view} .summary-line`), null, "no cluster status line at the ClusterSet level");
  assert.equal(await page.$(`${view} .cluster-tiles`), null, "no cluster summary cards at the ClusterSet level");
  assert.equal(await page.$(`${view} .cluster-identity`), null, "no group line at the ClusterSet level");
  assert.ok(await page.$(`${view} .clusterset-setline`), "the ClusterSet header leads the set level");
  const setFirst = await page.evaluate((sel) => {
    const main = document.querySelector(`${sel} .view-main`);
    const first = main.querySelector(".clusterset-setline, .summary-line, .cluster-tiles, .cluster-identity");
    return first ? first.className : "";
  }, view);
  assert.match(setFirst, /clusterset-setline/, "the ClusterSet header comes before any cluster summary");
  // The ClusterSet changes sit above the map: the named replica cluster gets
  // its own button (disabled without group peer reads), and the bare
  // placeholder keeps its original reason.
  const csAction = R["ic-8.4-clusterset-primary"].actions.find((a) => a.id === "set_primary_cluster:drcluster");
  assert.ok(csAction && csAction.enabled, "the named ClusterSet action is enabled");
  const csButtons = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true",
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim() })));
  assert.equal(csButtons.find((b) => b.label === "Make drcluster the primary cluster…").disabled, true);
  assert.match(csButtons.find((b) => b.label === "Make drcluster the primary cluster…").reason, /reads from other ClusterSet members/);
  const rejoinPlaceholder = csButtons.find((b) => b.label === "Rejoin cluster to the ClusterSet…");
  assert.equal(rejoinPlaceholder.disabled, true);
  assert.match(rejoinPlaceholder.reason, /No eligible target cluster is recorded in this ClusterSet\./);
  // The zoom transition is a short scale and fade; reduced motion skips it.
  const zoomAnim = () => page.$eval(`${view} .clusterset-zoom`, (e) => {
    const style = getComputedStyle(e);
    return { name: style.animationName, ms: parseFloat(style.animationDuration) * 1000 };
  });
  assert.equal((await zoomAnim()).name, "clusterset-zoom");
  assert.ok((await zoomAnim()).ms < 250, `the transition stays under 250 ms: ${(await zoomAnim()).ms}`);
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  assert.equal((await zoomAnim()).name, "none", "reduced motion skips the zoom transition");
  await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);

  // Zoom into the local cluster with the Open button: today's cluster view,
  // only this cluster's changes, and the breadcrumb with its shortcut.
  await openCluster("mfcluster", "button");
  assert.match(await h.text(`${view} .clusterset-crumb`), /ClusterSet mfcs › mfcluster/);
  assert.match(await h.text(`${view} .clusterset-zoom-out`), /Zoom out/);
  assert.equal(await h.text(`${view} .clusterset-zoom-out kbd`), process.platform === "darwin" ? "⌘↑" : "Ctrl+↑",
    "the Zoom out button shows the shortcut");
  // The zoomed cluster's own summary (status line and cards) returns.
  assert.match((await visible(`${view} .summary-line`))[0], /InnoDB Cluster mfcluster/, "the zoomed cluster's status line returns");
  assert.deepEqual(await visible(`${view} .tile-label`), ["Quorum", "Members", "This member", "Mode"], "the zoomed cluster's cards return");
  assert.equal(await page.$$eval(`${view} .cluster-group .cluster-card`, (els) => els.length), 3, "the local cluster zooms into today's cluster view");
  const zoomedActions = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual(zoomedActions, ["Switch to single-primary…", "Switch to multi-primary…"], "the set changes stay on the map");
  // Members and Routers tabs follow the zoomed cluster.
  await tab("Members");
  assert.equal((await visible(`${view} tbody tr`)).length, 3, "the Members tab shows the zoomed cluster");
  await zoomOut("button");
  assert.equal(await page.$eval(`${view} .segmented [aria-pressed="true"]`, (b) => b.textContent.trim()), "Topology",
    "zoom out from Members returns to the Topology map");
  await openCluster("mfcluster", "button");
  await tab("Routers");
  assert.match((await visible(`${view} tbody tr`))[0], /mf-ic-router/);
  await zoomOut("keyboard");
  assert.equal(await page.$eval(`${view} .segmented [aria-pressed="true"]`, (b) => b.textContent.trim()), "Topology",
    "the shortcut from Routers returns to the Topology map");
  await openCluster("mfcluster", "button");
  // The selection survives a zoom out and back in (the inspector is the
  // place's full-width detail, so it closes first, as elsewhere).
  const secondMember = R["ic-8.4-clusterset-primary"].members[1];
  await page.click(`${view} .cluster-card[data-row-key="${secondMember.key}"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.match(await h.inspector(), new RegExp(secondMember.name.replace(/[.:]/g, "\\$&")));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await zoomOut("button");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("data-cluster")), "mfcluster",
    "zoom out focuses the cluster's card on the map");
  assert.equal(await page.$$eval(`${view} .cluster-group .cluster-card`, (els) => els.length), 0, "the map replaces the member cards");
  await openCluster("mfcluster", "double");
  assert.equal(await page.$eval(`${view} .cluster-card[data-row-key="${secondMember.key}"]`, (e) => e.getAttribute("aria-pressed")), "true",
    "the selection is kept across zooming");
  // Enter on a focused card zooms in; the shortcut on the Zoom out button returns.
  await zoomOut("button");
  await openCluster("mfcluster", "enter");
  await zoomOut("keyboard");
  assert.equal(await page.$(`${view} .summary-line`), null, "back at the set level the cluster summary is gone again");
  // Cmd/Ctrl+0 is the View menu's Actual Size: it never zooms out. Nor does
  // the zoom-out shortcut fire while a text field has focus.
  await openCluster("mfcluster", "button");
  await page.keyboard.down(MOD); await page.keyboard.press("0"); await page.keyboard.up(MOD);
  await pause(150);
  assert.ok(await page.$(`${view} .clusterset-breadcrumb`), "Cmd/Ctrl+0 stays zoomed (Actual Size belongs to the View menu)");
  await page.evaluate((sel) => {
    const input = document.createElement("input");
    input.type = "text";
    document.querySelector(`${sel} .clusterset-breadcrumb`).append(input);
    input.focus();
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", metaKey: true, ctrlKey: true, bubbles: true }));
    input.remove();
  }, view);
  await pause(100);
  assert.ok(await page.$(`${view} .clusterset-breadcrumb`), "the zoom-out shortcut is ignored in a text field");
  // Native dialogs and modal sheets own their keys, including when focus is
  // on a button rather than a text field. Hidden dialogs do not block zoom.
  for (const native of [true, false]) {
    await page.evaluate((native) => {
      const modal = document.createElement(native ? "dialog" : "aside");
      modal.id = "zoom-key-modal";
      if (!native) { modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true"); }
      const button = document.createElement("button");
      button.textContent = "Modal control";
      modal.append(button);
      document.body.append(modal);
      if (native) modal.showModal();
      button.focus();
    }, native);
    await pressZoomOut();
    assert.ok(await page.$(`${view} .clusterset-breadcrumb`), `the shortcut stays inside ${native ? "a native dialog" : "a modal sheet"}`);
    await page.evaluate(() => document.getElementById("zoom-key-modal").remove());
  }
  await page.click('.main-nav button[data-place="home"]');
  await page.waitForSelector(view, { hidden: true });
  await page.evaluate(() => document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", metaKey: true, ctrlKey: true, bubbles: true })));
  await page.click(nav);
  await page.waitForSelector(view, { visible: true });
  assert.ok(await page.$(`${view} .clusterset-breadcrumb`), "the shortcut does not change a hidden Cluster place");
  await page.evaluate(() => {
    const dialog = document.createElement("dialog");
    dialog.id = "zoom-key-hidden";
    document.body.append(dialog);
  });
  await zoomOut("keyboard");
  await page.evaluate(() => document.getElementById("zoom-key-hidden").remove());
  await openCluster("mfcluster", "button");
  await zoomOut("button");

  // Dissolving the ClusterSet (or removing this cluster from it) ends the
  // navigation: the fresh report replaces the view, zoomed in or zoomed out,
  // never the stale pre-dissolve snapshot.
  const freshClusterView = async (when) => {
    await page.waitForFunction((sel) => !document.querySelector(`${sel} .clusterset-breadcrumb`)
      && !document.querySelector(`${sel} .clusterset-map`)
      && !document.querySelector(`${sel} .clusterset-setline`)
      && !!document.querySelector(`${sel} .cluster-topology`), {}, view);
    assert.match((await visible(`${view} .summary-line`))[0], /MySQL InnoDB Cluster mfcluster/,
      `the fresh report shows after the ClusterSet is gone (${when})`);
    assert.deepEqual(await page.$$eval(`${view} .cluster-local`, (els) => els.map((e) => e.textContent)),
      ["This connection"], `the fresh report is not greyed as the stale snapshot (${when})`);
  };
  await openCluster("mfcluster", "button");
  await refreshTo(R["ic-8.4-healthy"]);
  await freshClusterView("zoomed in");
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  await openCluster("mfcluster", "button");
  await zoomOut("button");
  await refreshTo(R["ic-8.4-healthy"]);
  await freshClusterView("zoomed out");
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });

  // A cluster with a saved route zooms through the per-place member routing
  // (chooseMember, the mechanism the Topology map's cards use): the place
  // moves to the route's group member, reads its report, and the top bar says
  // which connection it uses. No myflames:cluster-zoom event fires.
  await page.evaluate(() => { window.__zooms = []; window.addEventListener("myflames:cluster-zoom", (e) => window.__zooms.push(e.detail.profileId)); });
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection production"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection production"));
  // The group opens on its active member ("local"); Refresh makes its report
  // the current one, and the set map renders again.
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  assert.equal(state.seen.at(-1).connection.profile_id, "local", "the Group connection reads its active member");
  assert.equal(state.seen.at(-1).connection.group_name, "production");
  // Synthetic routed response: unlike the overview, its local topology and
  // catalog belong to drcluster. A report from the old connection is never
  // evidence that the requested cluster was read.
  const routedDr = JSON.parse(JSON.stringify(R["ic-8.4-clusterset-primary"]).replaceAll("mf-ic", "mf-dr"));
  routedDr.cluster.name = "drcluster";
  routedDr.metadata.cluster_name = "drcluster";
  routedDr.clusterset = structuredClone(R["ic-8.4-clusterset-primary"].clusterset);
  routedDr.clusterset.local_cluster = "drcluster";
  routedDr.clusterset.local_role = "REPLICA";
  for (const cluster of routedDr.clusterset.clusters) {
    cluster.local = cluster.name === "drcluster";
    cluster.route.self = cluster.local;
    cluster.read = cluster.local ? "this_connection" : "route";
  }
  const zoomReads = state.seen.length;
  const finishFirstZoom = holdZoomRead(routedDr);
  await openCluster("drcluster", "button");
  assert.deepEqual(await page.evaluate(() => window.__zooms), [], "no myflames:cluster-zoom event: the zoom uses the member routing");
  assert.match(await h.text(`${view} .clusterset-pending`), /Reading drcluster through DR member/);
  assert.equal(await page.$(`${view} .summary-line`), null, "a pending route shows no old cluster summary");
  assert.equal(await page.$(`${view} .cluster-actions`), null, "a pending route shows no old cluster actions");
  // The place reads the route's member once, through the Group connection.
  for (let i = 0; i < 100 && state.seen.length === zoomReads; i++) await pause(20);
  assert.ok(state.seen.length > zoomReads, "the routed zoom reads the route's member");
  const routedRead = state.seen.at(-1).connection;
  assert.equal(routedRead.profile_id, "remote", "the zoomed cluster is read through its route's saved connection");
  assert.equal(routedRead.group_name, "production");
  assert.equal(routedRead.host, "mf-dr-1");
  await page.waitForFunction(() => (document.querySelector(".nav-member-note")?.textContent || "").includes("Using DR member from group production."));
  // The mock answers that member's report: the pending status clears and the
  // zoomed view renders it.
  finishFirstZoom();
  await waitForZoomRead("drcluster");
  assert.equal(await page.$$eval(`${view} .cluster-group .cluster-card`, (els) => els.length), 3);
  assert.match(await h.text(`${view} .clusterset-crumb`), /› drcluster/);
  assert.match(await h.text(`${view} .cluster-card.is-local`), /mf-dr-1/);
  await zoomOut("button");
  state.queue.push({ status: 200, body: R["ic-8.4-clusterset-primary"] });
  await openCluster("mfcluster", "button");
  await waitForZoomRead("mfcluster");
  await zoomOut("button");

  // A -> B while A is pending: hold both replies and release A first.
  // Neither the breadcrumb, member inspector nor actions may adopt A's data.
  const finishOldZoom = holdZoomRead(routedDr);
  await openCluster("drcluster", "button");
  await zoomOut("button");
  const readsBeforeSecondZoom = state.seen.length;
  const finishNewZoom = holdZoomRead(R["ic-8.4-clusterset-primary"]);
  await openCluster("mfcluster", "button");
  await page.waitForFunction((sel) => document.querySelector(`${sel} .clusterset-pending`)?.textContent.includes("mfcluster"), {}, view);
  assert.equal(state.seen.length, readsBeforeSecondZoom + 1, "another zoom starts its own routed read");
  assert.equal(state.seen.at(-1).connection.profile_id, "local", "the old map's local route belongs to the source connection");
  finishOldZoom();
  await pause(150);
  assert.match(await h.text(`${view} .clusterset-crumb`), /› mfcluster/);
  assert.equal(await page.$(`${view} .cluster-topology`), null, "an obsolete reply cannot populate another cluster's topology");
  assert.equal(await page.$(`${view} .cluster-actions`), null, "an obsolete reply cannot populate another cluster's actions");
  assert.equal(await page.$(`${view} .view-inspector .review-actions`), null, "an obsolete reply cannot expose member actions");
  finishNewZoom();
  await waitForZoomRead("mfcluster");
  assert.match(await h.text(`${view} .cluster-card.is-local`), /mf-ic-1/);
  await zoomOut("button");

  // A failed route keeps the map and a usable way back. Reopening the same
  // route starts a new read even though its connection key has not changed.
  state.queue.push({ status: 400, body: { error: "Routed cluster read failed." } });
  await openCluster("drcluster", "button");
  await page.waitForFunction((sel) => document.querySelector(`${sel} .clusterset-pending`)?.textContent.includes("was not read"), {}, view);
  assert.equal(await page.$(`${view} .cluster-topology`), null);
  await zoomOut("button");
  assert.equal((await csCards()).length, 2, "a failed route retains the ClusterSet map");
  const readsBeforeRetry = state.seen.length;
  state.queue.push({ status: 200, body: routedDr });
  await openCluster("drcluster", "button");
  await waitForZoomRead("drcluster");
  assert.equal(state.seen.length, readsBeforeRetry + 1, "reopening a failed route retries it");
  await zoomOut("button");

  // An answered but unreadable report also keeps the source map. Refresh
  // retries within the same zoom, without changing the breadcrumb.
  state.queue.push({ status: 200, body: R["ic-8.4-clusterset-primary"] });
  await openCluster("mfcluster", "button");
  await waitForZoomRead("mfcluster");
  await zoomOut("button");
  state.queue.push({ status: 200, body: R["gr-8.4-denied"] });
  await openCluster("drcluster", "button");
  await page.waitForFunction((sel) => document.querySelector(`${sel} .clusterset-pending`)?.textContent.includes("was not read"), {}, view);
  assert.match(await h.text(`${view} .clusterset-crumb`), /› drcluster/);
  assert.equal(await page.$(`${view} .cluster-topology`), null, "an unreadable route never wipes the map with an unrelated report");
  await zoomOut("button");
  assert.equal((await csCards()).length, 2);
  const finishWrongCluster = holdZoomRead(R["ic-8.4-clusterset-primary"]);
  await openCluster("drcluster", "button");
  finishWrongCluster();
  await page.waitForFunction((sel) => document.querySelector(`${sel} .clusterset-pending`)?.textContent.includes("was not read"), {}, view);
  assert.equal(await page.$(`${view} .summary-line`), null, "a current-key report of the wrong cluster is still rejected");
  const inconsistentCluster = structuredClone(routedDr);
  inconsistentCluster.cluster.name = "mfcluster";
  const readsBeforeMismatch = state.seen.length;
  await answer(inconsistentCluster);
  await h.refresh();
  for (let i = 0; i < 100 && state.seen.length === readsBeforeMismatch; i++) await pause(20);
  assert.equal(state.seen.length, readsBeforeMismatch + 1, "the inconsistent topology was read on the requested connection");
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`), {}, view);
  assert.equal(await page.$(`${view} .summary-line`), null, "the ClusterSet label cannot substitute for the topology's cluster identity");
  await answer(routedDr);
  await h.refresh();
  await waitForZoomRead("drcluster");
  assert.match(await h.text(`${view} .clusterset-crumb`), /› drcluster/);
  await zoomOut("button");

  // A connection switch ends the zoom before its delayed answer can arrive.
  state.queue.push({ status: 200, body: R["ic-8.4-clusterset-primary"] });
  await openCluster("mfcluster", "button");
  await waitForZoomRead("mfcluster");
  await zoomOut("button");
  const finishAbandonedZoom = holdZoomRead(routedDr);
  await openCluster("drcluster", "button");
  await chooseConnection(page, await connectionOption(page, "Solo"));
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .clusterset-breadcrumb`), {}, view);
  finishAbandonedZoom();
  await pause(150);
  assert.equal(await page.$(`${view} .clusterset-breadcrumb`), null, "an abandoned route never revives its breadcrumb");
  assert.equal(await page.$(`${view} .cluster-topology`), null, "an abandoned route never replaces the retained map");
  // Back to a plain connection for the rest of the suite.
  await pause(200);

  // A cluster without a route: the metadata members, every action disabled,
  // and the fix (the saved connection for one of its members, never a new one).
  const metaOnly = JSON.parse(JSON.stringify(R["ic-8.4-clusterset-primary"]));
  const metaDr = metaOnly.clusterset.clusters.find((c) => c.name === "drcluster");
  metaDr.route = null;
  metaDr.read = "metadata_only";
  metaDr.status = "NOT_READ";
  metaDr.status_reason = "No saved connection in this group was verified as a route into this cluster.";
  metaOnly.clusterset.status = "AVAILABLE";
  metaOnly.clusterset.status_reason = "The primary cluster is OK; at least one other cluster is not OK or was not read.";
  await refreshTo(metaOnly);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  assert.match((await csCards())[1].text, /Metadata only/);
  assert.match((await csCards())[1].text, /Not read/);
  assert.equal(await page.$(`${view} .clusterset-cluster[data-cluster="drcluster"] .clusterset-shell`), null,
    "no saved route does not establish Shell UNKNOWN");
  await openCluster("drcluster", "button");
  assert.equal(await page.evaluate(() => window.__zooms.length), 0, "no route, no zoom request");
  const metaText = await h.text(`${view} .clusterset-metadata-only`);
  assert.match(metaText, /Metadata only/);
  assert.match(metaText, /mf-dr-1:3306/);
  assert.match(metaText, /This cluster was not read, so no change is offered/);
  const metaButtons = await page.$$eval(`${view} .clusterset-metadata-only .review-actions > button`, (els) => els.map((e) => e.getAttribute("aria-disabled")));
  assert.ok(metaButtons.length > 0 && metaButtons.every((d) => d === "true"), "every action is disabled on a metadata-only cluster");
  assert.deepEqual(await page.$$eval(`${view} .clusterset-metadata-only .review-actions > button`, (els) => els.map((b) => b.textContent.trim())),
    ["Make drcluster the primary cluster…", "Fail over to drcluster…", "Repair replication on drcluster…", "Remove drcluster from the ClusterSet…"],
    "metadata-only zoom includes only its target's actions, with removal choices merged");
  assert.equal(await h.text(`${view} .clusterset-metadata-only .cluster-use-node`), "Use DR member");
  await tab("Members");
  const metaRows = await visible(`${view} tbody tr`);
  assert.equal(metaRows.length, 3, "the Members tab lists the metadata members");
  assert.match(metaRows[0], /mf-dr-1:3306/);
  await tab("Topology");
  await zoomOut("button");

  // An individual (non-group) connection cannot follow a saved route: the
  // shell's member routing only moves an active Group connection, so the zoom
  // never starts a wait that cannot end. It shows the metadata with the
  // reason and the way to read the cluster (Use DR member) instead.
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  assert.match((await csCards())[1].text, /Read through DR member/, "the route is advertised on the card");
  const readsBeforeSoloZoom = state.seen.length;
  await openCluster("drcluster", "button");
  assert.equal(await page.$(`${view} .clusterset-pending`), null, "an individual connection never waits on a routed read");
  assert.equal(state.seen.length, readsBeforeSoloZoom, "no routed read starts without a Group connection");
  assert.equal(await page.evaluate(() => window.__zooms.length), 0, "no zoom request the shell cannot honour");
  const soloMeta = await h.text(`${view} .clusterset-metadata-only`);
  assert.match(soloMeta, /Metadata only/);
  assert.match(soloMeta, /This cluster was not read, so no change is offered/);
  assert.equal(await h.text(`${view} .clusterset-metadata-only .cluster-use-node`), "Use DR member");
  await zoomOut("button");

  // The same individual connection never advertises an action whose route the
  // plan would omit: it is disabled with the reason, and clicking it plans
  // nothing.
  await refreshTo(R["ic-8.4-clusterset-replica"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  const gateLabel = "Make mfcluster the primary cluster…";
  const gatedButton = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true",
    reason: (document.getElementById(e.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim() })))
    .then((all) => all.find((b) => b.label === gateLabel));
  assert.ok(gatedButton, `${gateLabel} is listed`);
  assert.equal(gatedButton.disabled, true, "a route-requiring action is disabled without a Group connection");
  assert.match(gatedButton.reason, /through their saved connections, which only an open Group connection may use\./);
  assert.match(gatedButton.reason, /Open the server's connection group, then try again\./);
  const plansBeforeGate = plans.length;
  await page.evaluate((sel, text) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.textContent.trim() === text).click(), view, gateLabel);
  await page.waitForSelector(`${view} .cluster-action-why:popover-open`, { visible: true });
  assert.match(await h.text(`${view} .cluster-action-why:popover-open`), /only an open Group connection may use/);
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, `${view} .cluster-action-why:popover-open`);
  assert.equal(plans.length, plansBeforeGate, "a route-gated action plans nothing");
  assert.equal(await page.$("dialog.clusterset-form[open]"), null, "a route-gated action opens no form");

  // Open a saved member of production individually. The catalog can have
  // discovered peers, but target.self only proves Shell's execution route.
  await chooseConnection(page, await connectionOption(page, "DR member"));
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  assert.equal(state.seen.at(-1).connection.group_name, undefined);
  const selfActions = R["ic-8.4-clusterset-primary"].actions.filter((a) =>
    ["set_primary_cluster:drcluster", "remove_cluster:drcluster", "remove_cluster_keep:drcluster"].includes(a.id));
  assert.equal(selfActions.length, 3);
  assert.ok(selfActions.every((a) => a.target.self), "all three variants use the local Shell route");
  assert.ok(selfActions.filter((a) => a.id !== "remove_cluster_keep:drcluster").every((a) => a.enabled),
    "the backend fixture reproduces the enabled self-route bypass");
  assert.equal(selfActions.find((a) => a.id === "remove_cluster_keep:drcluster").enabled, false,
    "the older Shell version's disabled removal variant keeps its original gate");
  const selfButtons = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true" })));
  for (const label of ["Make drcluster the primary cluster…", "Remove drcluster from the ClusterSet…"]) {
    assert.equal(selfButtons.find((b) => b.label === label)?.disabled, true, `${label} needs peers despite target.self`);
  }
  const localCheckLabel = R["ic-8.4-clusterset-primary"].actions.find((a) => a.id === "check_clusterset").label;
  assert.equal(selfButtons.find((b) => b.label === localCheckLabel)?.disabled, false,
    "the local read-only Check remains available on an individual connection");
  const plansBeforeSelfGate = plans.length;
  await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.textContent.trim() === "Make drcluster the primary cluster…").click(), view);
  await page.waitForSelector(`${view} .cluster-action-why:popover-open`, { visible: true });
  assert.match(await h.text(`${view} .cluster-action-why:popover-open`), /reads from other ClusterSet members/);
  await page.keyboard.press("Escape");
  assert.equal(plans.length, plansBeforeSelfGate, "a self-route peer action cannot start review individually");
  assert.equal(await page.$("dialog.clusterset-form[open]"), null);

  // Failover executes on the connected replica but still needs group routes
  // to prove that every old-primary member is unreachable before review.
  const localFailover = structuredClone(R["ic-8.4-clusterset-replica"]);
  const failover = localFailover.actions.find((a) => a.id.startsWith("force_primary_cluster:"));
  assert.equal(failover.target.self, true, "failover's Shell route is the local replica");
  failover.enabled = true;
  failover.why_not = null;
  failover.fix = null;
  await refreshTo(localFailover);
  const failoverButton = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els, text) =>
    els.find((e) => e.textContent.trim() === text)?.getAttribute("aria-disabled"), failover.label);
  assert.equal(failoverButton, "true", "local failover still requires the Group connection's reachability checks");
  // The same action is enabled again once the Group connection is active.
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection production"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection production"));
  await refreshTo(R["ic-8.4-clusterset-replica"]);
  assert.equal(state.seen.at(-1).connection.group_name, "production");
  const ungated = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((e) => ({
    label: e.textContent.trim(), disabled: e.getAttribute("aria-disabled") === "true" })))
    .then((all) => all.find((b) => b.label === gateLabel));
  assert.equal(ungated.disabled, false, "the Group connection carries the route the action needs");
  // Back to the plain connection for the rest of the suite.
  await chooseConnection(page, await connectionOption(page, "Solo"));
  await pause(200);

  // The status words, in plain words with the Shell term, across the states:
  // a stopped channel, an invalidated cluster, an unread one.
  await refreshTo(R["ic-8.4-clusterset-replica-channel-off"]);
  csCardList = await csCards();
  const offCard = csCardList.find((c) => c.name === "mfcluster");
  assert.match(offCard.text, /Not replicating OK_NOT_REPLICATING/, offCard.text);
  assert.match(offCard.text, /This connection/);
  const misconfPrimary = csCardList.find((c) => c.name === "maincluster");
  assert.match(misconfPrimary.text, /Read through DR member/);
  assert.match(misconfPrimary.text, /Misconfigured OK_MISCONFIGURED/, misconfPrimary.text);
  assert.match(await page.$eval(`${view} .clusterset-cluster[data-cluster="maincluster"] .cluster-mark`,
    (e) => e.className), /is-watch/, "OK_MISCONFIGURED never falls back to Not read or green OK");
  assert.match(await h.text(`${view} .clusterset-setline`), /Available/);
  assert.match(await h.text(`${view} .clusterset-setline`), /Read on DR member/);
  const cardReason = `${view} .clusterset-cluster[data-cluster="mfcluster"] .clusterset-reason-text`;
  assert.equal(await page.$eval(cardReason, (e) => e.hidden), true, "a card's status reason is on demand");
  await page.click(`${view} .clusterset-cluster[data-cluster="mfcluster"] .cluster-finding-toggle`);
  assert.equal(await page.$eval(cardReason, (e) => e.hidden), false);
  assert.match(await page.$eval(cardReason, (e) => e.textContent), /clusterset_replication channel reports receiver OFF/);
  const offLink = await page.$eval(`${view} .clusterset-link`, (e) => ({ cls: e.className, text: e.textContent,
    style: getComputedStyle(e.querySelector(".clusterset-arrow")).borderTopStyle }));
  assert.match(offLink.cls, /is-problem/);
  assert.match(offLink.text, /receiver OFF.*error 2003/);
  assert.equal(offLink.style, "dashed", "a stopped channel is a broken arrow, not color alone");
  // An invalidated replica cluster: the word, a dashed edge and a broken arrow.
  await refreshTo(R["ic-8.4-clusterset-invalidated"]);
  const dr = await page.$eval(`${view} .clusterset-cluster[data-cluster="drcluster"]`, (e) => ({
    cls: e.className, text: (e.innerText || e.textContent).replace(/\s+/g, " "), border: getComputedStyle(e).borderTopStyle }));
  assert.match(dr.cls, /is-invalidated/);
  assert.match(dr.text, /Invalidated INVALIDATED/);
  assert.equal(dr.border, "dashed");
  assert.match(await h.text(`${view} .clusterset-link-label`), /Invalidated · not replicating/);
  await shot("cluster-clusterset-invalidated-1440.png");

  // A fenced primary and a primary without quorum come from SQL answer
  // variants through the real read model, including Shell's fence action.
  await refreshTo(R["ic-8.4-clusterset-primary-fenced"]);
  csCardList = await csCards();
  const fencedCard = csCardList.find((c) => c.name === "mfcluster");
  assert.match(fencedCard.text, /Writes fenced OK_FENCED_WRITES/, fencedCard.text);
  assert.match(await page.$eval(`${view} .clusterset-cluster[data-cluster="mfcluster"] .cluster-mark`,
    (e) => e.className), /is-watch/);
  assert.match(csCardList.find((c) => c.name === "drcluster").text, /Not checked/,
    "a replica's expected super_read_only does not make it Writes fenced");
  await refreshTo(R["ic-8.4-clusterset-primary-no-quorum"]);
  const notOkCard = (await csCards()).find((c) => c.name === "mfcluster");
  assert.match(notOkCard.text, /Not OK NOT_OK/, notOkCard.text);
  assert.match(await page.$eval(`${view} .clusterset-cluster[data-cluster="mfcluster"] .cluster-mark`,
    (e) => e.className), /is-problem/);
  assert.match(await h.text(`${view} .clusterset-setline`), /Unavailable/);

  // Map status words and Shell terms (C8/G5): OK_MISCONFIGURED on a replica
  // never falls back to Not read or draws a green channel arrow; MISSING maps
  // to Shell's OK_MISCONFIGURED; CONNECTING never claims OK_NOT_REPLICATING;
  // every Shell Cluster_global_status word is mapped; raw-SRO NOT_OK omits the
  // Shell term; pinned target_cluster UUIDs resolve via clusters[].uuid.
  const mapWords = structuredClone(R["ic-8.4-clusterset-primary"]);
  mapWords.clusterset.clusters[0].status = "NOT_OK";
  mapWords.clusterset.clusters[0].super_read_only = true;
  mapWords.clusterset.clusters[1].uuid = "dddddddd-2222-11f0-8a00-0242ac130002";
  mapWords.clusterset.clusters[1].status = "OK_MISCONFIGURED";
  mapWords.clusterset.clusters[1].channel = {
    present: true, receiver_state: "ON", applier_state: "ON", last_error_number: 0,
    source: "other-primary:3306", source_uuid: "99999999-9999-11f0-8a00-0242ac130002",
    status: "MISCONFIGURED", read_error: null, workers: [],
  };
  mapWords.clusterset.clusters.push(
    {
      ...structuredClone(mapWords.clusterset.clusters[1]),
      name: "missingcluster", uuid: null, status: "NOT_REPLICATING",
      channel: { present: false, status: "MISSING", read_error: null, workers: [] },
    },
    {
      ...structuredClone(mapWords.clusterset.clusters[1]),
      name: "conncluster", uuid: null, status: "NOT_REPLICATING",
      channel: { present: true, receiver_state: "CONNECTING", applier_state: "ON", last_error_number: 0, status: "CONNECTING", read_error: null, workers: [] },
    },
    {
      ...structuredClone(mapWords.clusterset.clusters[1]),
      name: "errantcluster", uuid: null, status: "OK_NOT_CONSISTENT",
      channel: { present: true, receiver_state: "ON", applier_state: "ON", last_error_number: 0, status: "OK", read_error: null, workers: [] },
    },
  );
  mapWords.clusterset.routing_options = { stored_configuration: true, options: { target_cluster: "dddddddd-2222-11f0-8a00-0242ac130002" } };
  await refreshTo(mapWords);
  const wordChecks = await page.$$eval(`${view} .clusterset-map .clusterset-cluster`, (els) => Object.fromEntries(els.map((e) => [
    e.dataset.cluster,
    {
      word: e.querySelector(".cluster-mark")?.textContent || "",
      tone: e.querySelector(".cluster-mark")?.className || "",
      shell: e.querySelector(".clusterset-shell")?.textContent || null,
    },
  ])));
  assert.deepEqual(wordChecks.mfcluster, { word: "Not OK", tone: "cluster-mark is-problem", shell: null },
    "raw-SRO primary NOT_OK omits Shell term");
  assert.deepEqual(wordChecks.drcluster, { word: "Misconfigured", tone: "cluster-mark is-watch", shell: "OK_MISCONFIGURED" },
    "OK_MISCONFIGURED maps to Misconfigured / OK_MISCONFIGURED");
  assert.deepEqual(wordChecks.missingcluster, { word: "Not replicating", tone: "cluster-mark is-problem", shell: "OK_MISCONFIGURED" },
    "missing channel uses Shell's OK_MISCONFIGURED, never OK_NOT_REPLICATING");
  assert.deepEqual(wordChecks.conncluster, { word: "Connecting", tone: "cluster-mark is-watch", shell: null },
    "CONNECTING channel never claims OK_NOT_REPLICATING");
  assert.deepEqual(wordChecks.errantcluster, { word: "Not consistent", tone: "cluster-mark is-problem", shell: "OK_NOT_CONSISTENT" },
    "Shell OK_NOT_CONSISTENT is mapped");
  const replicaLinks = await page.$$eval(`${view} .clusterset-replica`, (els) => Object.fromEntries(els.map((li) => [
    li.querySelector(".clusterset-cluster")?.dataset.cluster,
    { cls: li.querySelector(".clusterset-link")?.className || "", text: li.querySelector(".clusterset-link-label")?.textContent || "" },
  ])));
  assert.match(replicaLinks.drcluster.cls, /is-problem/, "misconfigured replica channel never draws green");
  assert.match(replicaLinks.drcluster.cls, /is-broken/, "misconfigured replica channel draws broken/dashed");
  assert.doesNotMatch(replicaLinks.drcluster.cls, /is-ok/);
  assert.match(replicaLinks.drcluster.text, /misconfigured/);
  assert.match(replicaLinks.missingcluster.cls, /is-problem.*is-broken|is-broken.*is-problem/);
  assert.match(replicaLinks.missingcluster.text, /missing/);
  assert.match(replicaLinks.conncluster.cls, /is-watch/);
  assert.match(await h.text(`${view} .clusterset-plain`), /Routing options → drcluster/,
    "ClusterSet map resolves pinned target_cluster UUID via clusters[].uuid");

  // Connected to a read replica of the ClusterSet's primary cluster: the map
  // still renders, from the replica's replicated copy of the metadata.
  await refreshTo(R["ic-8.4-read-replica-clusterset"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  const rrCards = await csCards();
  assert.deepEqual(rrCards.map((c) => c.name), ["mfcluster", "drcluster"]);
  assert.match(rrCards[0].text, /Not read/, rrCards[0].text);
  assert.equal(await page.$(`${view} .clusterset-cluster[data-cluster="mfcluster"] .clusterset-shell`), null,
    "an unread cluster has no Shell UNKNOWN term");
  const rrLine = await h.text(`${view} .clusterset-setline`);
  assert.match(rrLine, /Unknown/);
  assert.match(rrLine, /this cluster's metadata copy, may be behind/);
  // The read replica's own status line and notes stay for the zoom into its
  // cluster: at the set level only the ClusterSet shows.
  assert.equal(await page.$(`${view} .summary-line`), null, "the read replica's status line stays for the zoom");
  await openCluster("mfcluster", "button");
  assert.match((await visible(`${view} .summary-line`))[0], /this server is a read replica/);
  assert.match(await h.notes(), /the ClusterSet read here is its replicated copy of the metadata and may be behind/);
  await zoomOut("button");

  // The set map and a zoomed cluster at the 720 px minimum window and at
  // 1440 px, in both themes: no horizontal page scroll.
  await refreshTo(R["ic-8.4-clusterset-primary"]);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await page.setViewport({ width: 1440, height: 1000 });
    await pause(200);
    await csShot(`set-map-1440-${theme}.png`);
    await page.setViewport({ width: 720, height: 520 });
    await pause(300);
    assert.ok((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0,
      `horizontal page scroll on the set map at 720 px (${theme})`);
    await csShot(`set-map-720-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await openCluster("mfcluster", "button");
  const scrollTop = () => page.evaluate((sel) => {
    const main = document.querySelector(`${sel} .view-main`);
    if (main) main.scrollTop = 0;
    window.scrollTo(0, 0);
  }, view);
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await page.setViewport({ width: 720, height: 520 });
    await scrollTop();
    await pause(300);
    assert.ok((await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0,
      `horizontal page scroll on a zoomed cluster at 720 px (${theme})`);
    await csShot(`zoomed-cluster-720-${theme}.png`);
    await page.setViewport({ width: 1440, height: 1000 });
    await scrollTop();
    await pause(200);
    await csShot(`zoomed-cluster-1440-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await zoomOut("button");

  // ClusterSet action parameters: set-level entries that need a choice open a
  // small form before the review, and the plan request carries `parameters`
  // only when the form set them.
  const openFormGroup = async () => {
    if (await page.$eval(".window-toolbar .connection-pill", (e) => e.textContent.includes("Group connection production"))) return;
    await openConnectionPicker(page);
    await page.click('button[aria-label="Open group connection production"]');
    await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection production"));
  };
  await openFormGroup();
  const csActionEntry = (id, label, extra = {}) => ({
    schema_version: "cluster-action-1.1", id, label, scope: "clusterset", kind: "risky", via: "mysqlsh",
    danger: true, description: label, member_key: null, member_name: null, member_uuid: null,
    cluster_name: null, enabled: true, why_not: null, fix: null, missing_privileges: [], grant_sql: null,
    privileges_unreadable: false, target: { self: true, member_name: "mf-ic-1:3306" }, ...extra,
  });
  const gate = { enabled: true, why_not: null, fix: null };
  const paramReport = JSON.parse(JSON.stringify(R["ic-8.4-clusterset-primary"]));
  paramReport.clusterset.routing_options = { stored_configuration: true, options: { target_cluster: "primary", invalidated_cluster_policy: "drop_all" } };
  // These form-specific capabilities deliberately override the real entries
  // (newer Shell and a spare), rather than leaving duplicate catalog IDs.
  paramReport.actions = paramReport.actions.filter((a) => !/^(remove_cluster|remove_cluster_keep|create_replica_cluster|set_routing_option|dissolve_clusterset)(:|$)/.test(a.id));
  paramReport.actions.push(
    csActionEntry("remove_cluster:drcluster", "Remove and dissolve the cluster drcluster…"),
    csActionEntry("remove_cluster_keep:drcluster", "Remove drcluster, keep the cluster running…"),
    csActionEntry("create_replica_cluster:spare", "Create replica cluster on Spare server…"),
    csActionEntry("set_routing_option", "Change ClusterSet routing defaults…", { danger: false, options: {
      target_cluster: { ...gate }, invalidated_cluster_policy: { ...gate }, stats_updates_frequency: { ...gate },
      use_replica_primary_as_rw: { ...gate }, read_only_targets: { ...gate },
      tags: { enabled: false, why_not: "tags needs MySQL Shell 8.1.0 or later.", fix: null },
      guideline: { enabled: false, why_not: "guideline needs MySQL Shell 9.2.0 or later.", fix: null } } }),
    csActionEntry("dissolve_clusterset", "Dissolve ClusterSet…"),
  );
  const clickSetAction = (label) => page.evaluate((sel, text) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
    .find((b) => b.textContent.trim() === text)?.click(), view, label);
  const formSelector = "dialog.clusterset-form[open]";
  const formConfirm = () => page.$eval(formSelector, (d) => {
    const b = [...d.querySelectorAll(".sheet-buttons button")].pop();
    return { text: b.textContent.replace(/\s+/g, " ").trim(), disabled: b.disabled };
  });
  const clickFormConfirm = () => page.$eval(formSelector, (d) => [...d.querySelectorAll(".sheet-buttons button")].pop().click());
  const closeReview = async () => {
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  };
  const setFormInput = (sel, value) => page.evaluate((s, v) => {
    const input = document.querySelector(s);
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set.call(input, v);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }, sel, value);
  await refreshTo(paramReport);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  const removeEntries = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) =>
    els.map((e) => e.textContent.trim()).filter((t) => /Remove.*drcluster|Remove drcluster/.test(t)));
  assert.deepEqual(removeEntries, ["Remove drcluster from the ClusterSet…"], "one remove entry per replica cluster");

  // Remove: the entry opens the chooser with the two variants, each with its
  // consequence; choosing one starts the normal review with that action id.
  let plansBeforeForm = plans.length;
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  assert.equal(plans.length, plansBeforeForm, "the remove chooser plans nothing");
  const choices = await page.$$eval(`${formSelector} .clusterset-choice`, (els) => els.map((e) => ({
    id: e.dataset.choice, text: (e.innerText || e.textContent).replace(/\s+/g, " "),
    disabled: e.querySelector(".button").getAttribute("aria-disabled") === "true" })));
  assert.deepEqual(choices.map((c) => c.id), ["dissolve", "keep"]);
  assert.match(choices[0].text, /dissolves it into standalone instances/);
  assert.match(choices[1].text, /standalone InnoDB Cluster/);
  assert.ok(choices.every((c) => !c.disabled));
  assert.equal((await formConfirm()).disabled, true, "the review waits for a choice");
  await page.$eval(`${formSelector} .clusterset-choice[data-choice="dissolve"] .button`, (b) => b.click());
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeForm + 1, "choosing remove plans once");
  assert.equal(plans.at(-1).action, "remove_cluster:drcluster");
  assert.equal(plans.at(-1).connection.group_name, "production", "the removal review carries group consent");
  assert.ok(!("parameters" in plans.at(-1)), "removal without options sends no parameters");
  await closeReview();
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  await page.$eval(`${formSelector} .clusterset-choice[data-choice="keep"] .button`, (b) => b.click());
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.at(-1).action, "remove_cluster_keep:drcluster", "the keep choice plans remove_cluster_keep");
  await closeReview();

  // A catalog-disabled choice stays visible and shows its reason on click.
  const keepOff = JSON.parse(JSON.stringify(paramReport));
  keepOff.actions = keepOff.actions.map((a) => a.id === "remove_cluster_keep:drcluster"
    ? { ...a, enabled: false, why_not: "removeCluster's dissolve option needs MySQL Shell 9.0.0 or later.", fix: "Install MySQL Shell 9.0.0 or later, then Refresh." }
    : a);
  await refreshTo(keepOff);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  plansBeforeForm = plans.length;
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(formSelector, { visible: true });
  const keepChoice = await page.$eval(`${formSelector} .clusterset-choice[data-choice="keep"] .button`, (b) => b.getAttribute("aria-disabled"));
  assert.equal(keepChoice, "true", "a disabled choice stays visible, disabled");
  await page.click(`${formSelector} .clusterset-choice[data-choice="keep"] .button`);
  await page.waitForFunction(() => /9\.0\.0/.test(document.querySelector("dialog.clusterset-form .why-disabled")?.textContent || ""));
  assert.equal(plans.length, plansBeforeForm, "a disabled choice plans nothing");
  await page.$eval(formSelector, (d) => [...d.querySelectorAll(".sheet-buttons button")][0].click());
  await page.waitForFunction(() => !document.querySelector("dialog.clusterset-form[open]"));

  // Create replica cluster: the target comes from the offered catalog entries
  // (never typed), the name is pre-filled and validated, and recoveryMethod is
  // a required explicit choice.
  await refreshTo(paramReport);
  await page.waitForSelector(`${view} .clusterset-map`, { visible: true });
  await clickSetAction("Create replica cluster on Spare server…");
  await page.waitForSelector(formSelector, { visible: true });
  const targetOptions = await page.$eval(`${formSelector} select`, (s) => [...s.options].map((o) => o.textContent));
  assert.deepEqual(targetOptions, ["Spare server"], "the target is chosen from the offered saved connections");
  const suggested = await page.$eval(`${formSelector} input`, (i) => i.value);
  assert.match(suggested, /^[A-Za-z_][A-Za-z0-9_.-]{0,62}$/, "the pre-filled name follows MySQL Shell's rule");
  assert.ok(suggested.length > 0);
  assert.equal((await formConfirm()).disabled, true, "create needs an explicit recoveryMethod");
  await setFormInput(`${formSelector} input`, "1bad name");
  await page.$eval(`${formSelector} .clusterset-choice[data-choice="incremental"] .button`, (b) => b.click());
  assert.equal((await formConfirm()).disabled, true, "an invalid name holds the review back");
  await setFormInput(`${formSelector} input`, "replica-dr-2");
  assert.equal((await formConfirm()).disabled, false);
  plansBeforeForm = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeForm + 1);
  assert.equal(plans.at(-1).action, "create_replica_cluster:spare", "the target comes from the catalog entry");
  assert.deepEqual(plans.at(-1).parameters, { name: "replica-dr-2", recoveryMethod: "incremental" });
  await closeReview();

  // Router routing: only the options the backend accepts, pre-filled with the
  // stored values, and the plan carries option and value.
  await clickSetAction("Change ClusterSet routing defaults…");
  await page.waitForSelector(formSelector, { visible: true });
  const routing = await page.$eval(formSelector, (d) => {
    const selects = [...d.querySelectorAll("select")];
    return { options: [...selects[0].options].map((o) => o.value), option: selects[0].value, value: selects[1]?.value ?? null };
  });
  assert.deepEqual(routing.options, ["target_cluster", "invalidated_cluster_policy", "stats_updates_frequency", "use_replica_primary_as_rw", "read_only_targets"],
    "only the options the backend accepts");
  assert.equal(routing.option, "target_cluster");
  assert.equal(routing.value, "primary", "the routing form is pre-filled with the stored value");
  await page.select(`${formSelector} select`, "invalidated_cluster_policy");
  const policy = await page.$eval(formSelector, (d) => [...d.querySelectorAll("select")][1]?.value);
  assert.equal(policy, "drop_all", "the stored policy pre-fills the value");
  plansBeforeForm = plans.length;
  await clickFormConfirm();
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeForm + 1);
  assert.equal(plans.at(-1).action, "set_routing_option");
  assert.deepEqual(plans.at(-1).parameters, { option: "invalidated_cluster_policy", value: "drop_all" });
  await closeReview();

  // A set action whose form has nothing to offer (no unreachable replica to
  // invalidate) goes straight to review; the request carries no `parameters`
  // key.
  plansBeforeForm = plans.length;
  await clickSetAction("Make drcluster the primary cluster…");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeForm + 1);
  assert.equal(plans.at(-1).action, "set_primary_cluster:drcluster");
  assert.ok(!("parameters" in plans.at(-1)), "no invalidation choice applies, so no parameters");
  await closeReview();

  // Pre-review forms belong to one connection and one catalog generation.
  // Refresh and connection changes can happen outside the sheet (Refresh all,
  // saved profile edits or another window), so dispatch the real controls.
  const refreshBehindForm = async (reply) => {
    const before = state.seen.length;
    state.queue.push(reply);
    await page.$eval(`${view} [data-action="refresh"]`, (b) => b.click());
    await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`), {}, view);
    for (let i = 0; i < 100 && state.seen.length === before; i++) await pause(20);
    assert.equal(state.seen.length, before + 1, "the background refresh ran");
    await page.waitForSelector(formSelector, { hidden: true });
  };
  const soloId = await connectionOption(page, "Solo");
  const readOnlyId = await connectionOption(page, "Read-only");
  const nodeId = await connectionOption(page, "Node A");
  await page.keyboard.press("Escape");
  const selectBehindForm = async (id) => {
    await page.$eval(".window-toolbar .connection-pill", (b) => b.click());
    await page.waitForSelector('.window-toolbar select[aria-label="Connection"]');
    await page.select('.window-toolbar select[aria-label="Connection"]', id);
    await page.waitForSelector(formSelector, { hidden: true });
    await page.keyboard.press("Escape");
  };
  for (const kind of ["connection", "read-only", "failed read", "catalog"]) {
    await openFormGroup();
    await refreshTo(paramReport);
    plansBeforeForm = plans.length;
    await clickSetAction("Remove drcluster from the ClusterSet…");
    await page.waitForSelector(formSelector, { visible: true });
    await page.$eval(`${formSelector} .clusterset-choice[data-choice="keep"] .button`, (b) => b.click());
    assert.equal((await formConfirm()).disabled, false, "the old form could review before invalidation");
    if (kind === "connection") {
      // A different endpoint, including a stale report, closes the form.
      await selectBehindForm(nodeId);
    } else if (kind === "read-only") await selectBehindForm(readOnlyId);
    else if (kind === "failed read") await refreshBehindForm({ status: 400, body: { error: "Catalog refresh failed." } });
    else {
      const changedCatalog = structuredClone(paramReport);
      for (const action of changedCatalog.actions.filter((a) => a.id.startsWith("remove_cluster"))) action.enabled = false;
      await refreshBehindForm({ status: 200, body: changedCatalog });
    }
    assert.equal(await page.$(formSelector), null, `${kind} invalidates the parameter sheet`);
    assert.equal(await page.$("dialog.action-review[open]"), null, `${kind} cannot open a review from an invalid form`);
    assert.equal(plans.length, plansBeforeForm, `${kind} sends no stale plan`);
  }

  // A busy review cannot be bypassed by activating another pre-review action.
  await refreshTo(paramReport);
  planDelay = 600;
  plansBeforeForm = plans.length;
  await clickSetAction("Make drcluster the primary cluster…");
  await page.waitForFunction((sel) => [...document.querySelectorAll(`${sel} .review-actions > button`)].some((b) => b.textContent === "Reading…"), {}, view);
  await clickSetAction("Remove drcluster from the ClusterSet…");
  assert.equal(await page.$(formSelector), null, "a busy action cannot open a parameter sheet");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.length, plansBeforeForm + 1, "busy action activation never starts a second plan");
  planDelay = 0;
  await closeReview();

  // Only the fixed Check call gets the read-only exception. Every set change,
  // whole-cluster change and member change retains its refusal and reason.
  await chooseConnection(page, readOnlyId);
  const checkReport = structuredClone(paramReport);
  const checkLabel = checkReport.actions.find((a) => a.id === "check_clusterset").label;
  await refreshTo(checkReport);
  const checkButtons = await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((b) => ({
    label: b.textContent.trim(), blocked: b.getAttribute("aria-disabled") === "true",
  })));
  assert.equal(checkButtons.find((b) => b.label === checkLabel).blocked, false, "Check is available on a Read-only connection");
  assert.ok(checkButtons.filter((b) => b.label !== checkLabel).every((b) => b.blocked), "every set change stays blocked on Read-only");
  plansBeforeForm = plans.length;
  await clickSetAction("Remove drcluster from the ClusterSet…");
  await page.waitForSelector(`${view} .cluster-action-why:popover-open`, { visible: true });
  assert.match(await h.text(`${view} .cluster-action-why:popover-open`), /read-only in myflames/);
  await page.keyboard.press("Escape");
  assert.equal(plans.length, plansBeforeForm, "a read-only write activation plans nothing");
  await clickSetAction(checkLabel);
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(plans.at(-1).action, "check_clusterset");
  assert.equal(plans.at(-1).connection.read_only, true, "the Check plan retains the connection's read-only policy");
  const checkConfirm = "dialog.action-review[open] .sheet-buttons button:last-child";
  assert.deepEqual(await page.$eval(checkConfirm, (b) => ({ disabled: b.disabled, danger: b.classList.contains("danger-solid"), primary: b.classList.contains("primary") })),
    { disabled: false, danger: false, primary: true }, "Check confirms as a read even when the production policy returned danger tone");
  const sendsBeforeCheck = sends.length;
  await page.click(checkConfirm);
  await page.waitForFunction(() => [...document.querySelectorAll("dialog.action-review[open] .sheet-buttons button")].some((b) => b.textContent.trim() === "Close"));
  assert.equal(sends.length, sendsBeforeCheck + 1);
  assert.equal(sends.at(-1).plan.action, "check_clusterset");
  assert.equal(sends.at(-1).review_token, "check-token", "the check keeps the server-issued review token");
  assert.deepEqual(sends.at(-1).plan.statements, ["print(JSON.stringify(myflamesClusterSet.status({extended: 1}), null, 2));"],
    "the read exception preserves the exact reviewed call");
  assert.equal(sends.at(-1).connection.read_only, true, "confirm preserves Read-only rather than changing connection policy");
  await closeReview();
  await openCluster("mfcluster", "button");
  assert.ok((await page.$$eval(`${view} .cluster-actions .review-actions > button`, (els) => els.map((b) => b.getAttribute("aria-disabled")))).every((value) => value === "true"),
    "whole-cluster changes remain blocked on Read-only");
  await page.click(`${view} .cluster-card[data-row-key="${secondMember.key}"]`);
  await page.waitForSelector(".view-inspector .review-actions");
  assert.ok((await page.$$eval(".view-inspector .review-actions > button", (els) => els.map((b) => b.getAttribute("aria-disabled")))).every((value) => value === "true"),
    "member changes remain blocked on Read-only");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await zoomOut("button");
  await chooseConnection(page, soloId);

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

  // An account that cannot read: one sentence, backend grant_sql in Commands, no muted sidebar.
  await refreshTo(R["gr-8.4-denied"]);
  const deniedGate = await h.text(`${view} .gate`);
  assert.match(deniedGate, /cannot read performance_schema\.replication_group_members/);
  assert.doesNotMatch(deniedGate, /\bGRANT\b/i, "deniedText never builds a GRANT inside .gate");
  assert.equal(await page.$(`${view} .gate code`), null, "no browser-built GRANT code element in .gate");
  assert.equal(await page.$eval(`${view} .cluster-denied-grant .ops-pre code`, (e) => e.textContent),
    R["gr-8.4-denied"].grant_sql, "backend grant_sql renders in Commands block");
  assert.ok(await page.$(`${view} .cluster-denied-grant button[aria-label="Copy: Grant to read cluster status"]`),
    "Commands block carries its Copy button");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "");

  // S-F4 abuse cases: hostile user/host text in report.message never produces a GRANT
  // when backend grant_sql is null or absent (nor does a plain message without grant_sql).
  const deniedLead = "This account cannot read performance_schema.replication_group_members (it needs SELECT on performance_schema): ";
  for (const [label, user, host, grantSql] of [
    ["ctrl-a", "\u0001\\! touch /tmp/x #", "%", null],
    ["egrave", "\u00c8".repeat(7) + "\\! touch /tmp/x #", "%", null],
    ["esc-host", "app", "h\u001b[201~", null],
    ["benign-msg-omitted-grant", "app", "%", undefined],
  ]) {
    const abused = structuredClone(R["gr-8.4-denied"]);
    abused.message = `${deniedLead}ERROR 1142 (42000) at line 1: SELECT command denied to user '${user}'@'${host}' for table 'replication_group_members'`;
    if (grantSql === undefined) delete abused.grant_sql;
    else abused.grant_sql = grantSql;
    await refreshTo(abused);
    const body = await page.$eval(view, (e) => ({
      gate: e.querySelector(".gate")?.textContent || "",
      gateCode: !!e.querySelector(".gate code"),
      grantBlock: !!e.querySelector(".cluster-denied-grant, .ops-commands"),
      all: e.textContent || "",
    }));
    assert.match(body.gate, /cannot read performance_schema\.replication_group_members/, `${label}: keeps reason`);
    assert.equal(body.gateCode, false, `${label}: no <code> inside .gate`);
    assert.equal(body.grantBlock, false, `${label}: no Commands block without backend grant_sql`);
    assert.doesNotMatch(body.all, /\bGRANT\b/i, `${label}: no GRANT text shown`);
  }

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
  // This case exercises a uniformly blocked group of writes. The complete
  // catalog's read-only Check exception is covered on the set map above.
  blockedReport.actions = blockedReport.actions.filter((a) => a.id !== "check_clusterset");
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
  // The action band wraps at 720x520, and a disabled action's open message
  // never covers the fixed status bar or a sticky toolbar (W6), in both real
  // appearances selected through the app's preference flow (W8, W9).
  const narrowBg = {};
  const narrowWhySel = `${view} .cluster-actions .cluster-action-why:popover-open`;
  for (const theme of ["light", "dark"]) {
    await setTheme(theme);
    await page.evaluate((sel) => document.querySelector(`${sel} .cluster-actions`)?.scrollIntoView({ block: "end" }), view);
    await pause(150);
    await page.evaluate((sel) => [...document.querySelectorAll(`${sel} .cluster-actions .review-actions > button`)]
      .find((b) => b.getAttribute("aria-disabled") === "true").click(), view);
    await page.waitForFunction((sel) => {
      const open = document.querySelector(sel);
      return !!open && open.getClientRects().length > 0 && (open.textContent || "").length > 0;
    }, {}, narrowWhySel);
    await pause(150);
    const wrap = await page.evaluate((sel) => ({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      band: document.querySelector(`${sel} .cluster-actions`) ? 1 : 0,
    }), view);
    assert.equal(wrap.band, 1, "the overview action band is present at 720 px");
    assert.ok(wrap.overflow <= 0, `horizontal page scroll at 720 px (${theme}): ${wrap.overflow}`);
    await assertNoChromeOverlap(narrowWhySel, `Cluster 720x520 bottom (${theme})`);
    const colors = await assertThemeColors(theme, narrowWhySel, `Cluster 720x520 (${theme})`);
    narrowBg[theme] = colors.msgBg;
    await shot(`cluster-720-${theme}.png`);
    await shot(`cluster-why-720-${theme}.png`);
    // Also verify no overlap when the anchor sits near the top sticky toolbar.
    await page.evaluate((sel) => document.querySelector(`${sel} .cluster-actions`)?.scrollIntoView({ block: "start" }), view);
    await pause(150);
    await assertNoChromeOverlap(narrowWhySel, `Cluster 720x520 top (${theme})`);
    await page.keyboard.press("Escape");
    await page.waitForFunction((sel) => !document.querySelector(sel), {}, narrowWhySel);
  }
  assert.notEqual(narrowBg.light, narrowBg.dark, `720 px message background differs between light and dark: ${JSON.stringify(narrowBg)}`);
  await setTheme("system");
  await pause(150);
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
