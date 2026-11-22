// The Readyset place: mocked /api/live/readyset answering readyset-1.0 reports
// computed by the real backend (readyset-fixtures.py over FakeReadyset and
// FakeProbeConn), mocked dry-run explain, DDL export, plan and action endpoints,
// and the real Python WorkspaceServer for session, saved connections and the
// Audit log.
// Covers: sidebar placement right after ProxySQL with its own glyph, nothing
// read before open, one read on open, summary, 4 metric tiles and 5 tabs,
// multi-tier Topology (ProxySQL -> Readyset -> MySQL Primary & Replica, motion
// & depth toggles, upstream inspector, cross-place navigation), Caches table
// and 320 px right-pane inspector, Export SQL modal, Create cache sheet with
// live EXPLAIN CREATE CACHE dry-run and collision warning, ActionReview lifecycle
// (Escape/Cancel declined, Sending… -> Sent -> Done, policy change refusal,
// read-only refusal, Outcome unknown never retried, Audit log jump), Proxied
// query filtering and Create cache pre-fill, Tables mode-aware replication lag
// (transactions vs bytes) and typed-guard Re-snapshot, Parameters dynamic
// ALTER READYSET controls, shallow allowlists and Admin 6036 shutdown gating,
// other-connection greyed state, sidebar muting per connection, and 720x520.
import assert from "node:assert/strict";
import path from "node:path";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
import { load, open, pause } from "./perf-harness.mjs";
import { reviewedActionsUnit } from "./reviewed-actions-unit.mjs";

await reviewedActionsUnit();
if (process.argv.includes("--unit")) process.exit(0);

const R = {};
for (const name of [
  "lab",
  "file-lag",
  "admin",
  "maintenance",
  "problem",
  "not",
  "read-fail",
  "explain-both",
  "explain-collision",
  "explain-unsupported",
  "export-caches",
  "plan-create-cache",
  "plan-flush-cache",
  "plan-drop-cache",
  "plan-resnapshot",
  "plan-log-level",
  "plan-allow-fn",
  "plan-drop-fn",
  "plan-enter-maint",
  "plan-stop-repl",
  "plan-shutdown",
  "plan-read-only",
  "result-flush-done",
  "result-create-done",
  "progress-checking",
  "progress-sending",
  "progress-finished-unknown",
]) {
  R[name] = await load(`readyset-${name}.json`);
}

const view = '.server-view[data-view="readyset"]';
const nav = '.main-nav button[data-place="readyset"]';
const plans = [];
const sends = [];
const explains = [];
const exportsSeen = [];
const declined = [];
const progressReads = [];

let planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
let sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });
let explainAnswer = (body) => {
  const q = body.query_or_id || body.query || "";
  return {
    status: 200,
    body:
      q === "q_1111aaaa"
        ? R["explain-collision"]
        : /RAND\(\)/i.test(q)
          ? R["explain-unsupported"]
          : R["explain-both"],
  };
};
let progressAnswer = () => ({ status: 200, body: R["progress-sending"] });

const h = await open("/api/live/readyset", "readyset", {
  "/api/live/readyset/plan": (body) => {
    plans.push(body);
    return planAnswer(body);
  },
  "/api/live/readyset/action": (body) => {
    sends.push(body);
    return sendAnswer(body);
  },
  "/api/live/readyset/explain-cache": (body) => {
    explains.push(body);
    return explainAnswer(body);
  },
  "/api/live/readyset/export-caches": (body) => {
    exportsSeen.push(body);
    return { status: 200, body: R["export-caches"] };
  },
  "/api/live/action/progress": (body) => {
    progressReads.push(body);
    return progressAnswer(body);
  },
  "/api/audit/declined": (body) => {
    declined.push(body);
    return { status: 200, body: { audit_id: "x", status: "declined" } };
  },
});

const { page, state } = h;
const api = (route, body) =>
  page.evaluate(
    async (r, b) => {
      const token = (
        await (
          await fetch("/api/auth/session", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Myflames-Bootstrap": "1",
              "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "",
            },
            body: "{}",
          })
        ).json()
      ).token;
      const response = await fetch(r, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
        body: JSON.stringify(b),
      });
      return response.json();
    },
    route,
    body,
  );

const visible = (selector) =>
  page.$$eval(selector, (els) =>
    els
      .filter((e) => e.offsetParent || e.getClientRects().length)
      .map((e) => e.textContent.replace(/\s+/g, " ").trim()),
  );
const shot = (file) => page.screenshot({ path: path.join(h.shots, file), fullPage: false });

async function refreshTo(report) {
  const before = state.seen.length;
  state.fallback = { status: 200, body: report };
  await h.refresh();
  for (let i = 0; i < 50 && state.seen.length === before; i++) await pause(20);
  assert.equal(state.seen.length, before + 1, "Refresh sends one read");
  await page.waitForFunction(
    (sel) => !document.querySelector(`${sel} .spinner`) && document.querySelector(`${sel} .summary-line`),
    {},
    view,
  );
  await pause(150);
}

async function tab(label) {
  await page.evaluate(
    (label, sel) =>
      [...document.querySelectorAll(`${sel} .segmented button`)]
        .find((b) => b.textContent.trim().startsWith(label))
        .click(),
    label,
    view,
  );
  await pause(200);
}

async function backToList() {
  const close = await page.$(".view-inspector.is-detail .detail-back, .view-inspector.is-sheet .inspector-close");
  if (close) {
    await close.click();
    await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail, .view-inspector.is-sheet"));
  }
}

const rowSelector = (key) => `${view} tbody tr[data-row-key=${JSON.stringify(key)}]`;
async function selectRow(key) {
  await backToList();
  await page.click(rowSelector(key));
  await page.waitForSelector(".view-inspector .inspector-title");
}

const inspectorButtons = () =>
  page.$$eval(".view-inspector .review-actions button", (els) => els.map((e) => e.textContent.trim()));
async function clickAction(label) {
  await page.evaluate(
    (label) =>
      [...document.querySelectorAll(".view-inspector .review-actions button")]
        .find((b) => b.textContent.trim() === label)
        .click(),
    label,
  );
}

const sheet = "dialog.action-review[open]";
async function waitSheet() {
  await page.waitForSelector(sheet, { visible: true });
}
async function noSheet() {
  await page.waitForFunction((s) => !document.querySelector(s), {}, sheet);
}
async function answered() {
  await page.waitForSelector(`${sheet} .action-outcome`);
  return page.$eval(sheet, (d) => ({
    outcome: d.querySelector(".action-outcome")?.textContent.replace(/\s+/g, " ").trim(),
    steps: [...d.querySelectorAll(".action-steps li")].map((li) => li.dataset.state),
    text: d.textContent,
    buttons: [...d.querySelectorAll(".sheet-actions button")].map((b) => b.textContent.trim()),
    focused: document.activeElement?.textContent?.trim(),
  }));
}
async function closeSheet() {
  await page.evaluate(
    (s) => [...document.querySelectorAll(`${s} .sheet-actions button`)].find((b) => b.textContent.trim() === "Close").click(),
    sheet,
  );
  await noSheet();
}
const resultButton = (label) =>
  page.evaluate(
    (sel, label) =>
      [...document.querySelectorAll(`${sel} .action-result .ops-result-head .text-button`)]
        .find((b) => b.textContent.trim() === label)
        .click(),
    view,
    label,
  );

try {
  await api("/api/profiles/save", {
    value: {
      name: "Readyset lab",
      host: "127.0.0.1",
      port: 3307,
      user: "app",
      database: "shop",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
      type: "development",
    },
  });
  await api("/api/profiles/save", {
    value: {
      name: "Readyset Admin",
      host: "127.0.0.1",
      port: 6036,
      user: "root",
      database: "",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
      type: "development",
    },
  });
  await api("/api/profiles/save", {
    value: {
      name: "Plain MySQL",
      host: "mysql.test",
      port: 3306,
      user: "dba",
      database: "shop",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
    },
  });
  await api("/api/profiles/save", {
    value: {
      name: "Upstream copy",
      host: "127.0.0.1",
      port: 3306,
      user: "dba",
      database: "",
      ssl_mode: "PREFERRED",
      ssl_ca: "",
    },
  });
  await page.reload({ waitUntil: "networkidle0" });

  // 1. Sidebar placement right after ProxySQL with its own glyph; nothing read before open.
  const places = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.equal(places[places.indexOf("proxysql") + 1], "readyset", `Readyset follows ProxySQL: ${places}`);
  assert.equal(await page.$eval(nav, (b) => (b.childNodes[1]?.textContent || "").trim()), "Readyset");
  const rsSvg = await page.$eval('.main-nav button[data-place="readyset"] svg', (e) => e.innerHTML);
  const pxSvg = await page.$eval('.main-nav button[data-place="proxysql"] svg', (e) => e.innerHTML);
  assert.notEqual(rsSvg, pxSvg, "Readyset has a distinct icon from ProxySQL");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "unmuted before first read");
  await pause(350);
  assert.equal(state.seen.length, 0, "nothing read before Readyset is opened");

  // 2. Open Readyset: reads once, switch to Readyset lab and Refresh.
  state.fallback = { status: 200, body: R.lab };
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await pause(200);
  assert.equal(state.seen.length, 1, "opening Readyset reads once");
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await pause(250);
  assert.equal(state.seen.length, 1, "switching connection keeps prior report until Refresh");
  await refreshTo(R.lab);
  assert.equal(state.seen.at(-1).connection.port, 3307);
  assert.equal(state.seen.at(-1).connection.type, "development");

  assert.match(
    (await visible(`${view} .summary-line`))[0],
    /Readyset stable-250605 · SQL port · 2 caches · 80% cached · Repl Running/,
  );
  assert.deepEqual(await visible(`${view} .segmented button`), [
    "Topology",
    "Caches2",
    "Proxied2",
    "Tables2",
    "Parameters",
  ]);

  const tiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      value: e.querySelector(".tile-value")?.textContent?.trim() || "",
      hint: e.querySelector(".tile-hint")?.textContent?.trim() || "",
    })),
  );
  assert.deepEqual(
    tiles.map((t) => t.label),
    ["Cached share", "Active caches", "Shallow hit rate", "Base replication"],
  );
  assert.match(tiles[0].value, /^80/);
  assert.equal(tiles[0].hint, "1,000 cached · 250 upstream");
  assert.equal(tiles[1].value, "2");
  assert.equal(tiles[1].hint, "1 shallow · 1 deep");
  assert.match(tiles[2].value, /^90/);
  assert.equal(tiles[3].value, "Running");
  assert.equal(tiles[3].hint, "lag 0 transactions");

  const info = await h.popover("About Readyset");
  assert.match(info, /SQL port 3307, admin SQL port 6036/);
  assert.match(info, /Audit log/);

  // 3. Multi-tier Topology tab: ProxySQL -> Readyset -> MySQL Primary & Replica.
  const topoText = await h.text(`${view} .readyset-topology`);
  assert.match(topoText, /ProxySQL lab/);
  assert.match(topoText, /127\.0\.0\.1:6032 · HG 30/);
  assert.match(topoText, /Cached reads \+ QueryPilot mirror/);
  assert.match(topoText, /2 QueryPilot rules/);
  assert.match(topoText, /MySQL Primary/);
  // Address and product/version sit on separate meta lines (no truncation).
  assert.match(topoText, /mysql-primary:3306/);
  assert.match(topoText, /MySQL 8\.4\.11/);
  assert.doesNotMatch(topoText, /mysql-primary:3306 · MySQL/, "upstream address and version are separate meta lines");
  assert.match(topoText, /MySQL Replica/);
  assert.match(topoText, /mysql-replica:3306/);
  // The CDC link label reads without stray spaces around the mode value.
  const plates = await page.$$eval(`${view} .readyset-topology .tier-link-plate`, (els) =>
    els.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()),
  );
  assert.ok(
    plates.includes("CDC replication (mysql_gtid) & query fallback"),
    `CDC link label has no stray spaces: ${JSON.stringify(plates)}`,
  );
  // Redacted SHOW CONNECTIONS upstream URL never leaks credentials.
  const connText = await h.text(`${view}`);
  assert.doesNotMatch(connText, /secret@/);
  assert.match(connText, /\[redacted\]@/);

  assert.equal(await page.$(`${view} .readyset-topology .topology-presentation-controls`), null, "no presentation controls");
  assert.equal(await page.$eval(`${view} .readyset-topology`, (e) => e.dataset.motion), "on");

  // Clicking an upstream card opens the 320 px right-pane inspector, which
  // carries the fields the dropped upstream table had.
  await page.click(`${view} .readyset-topology button[data-row-key="upstream:mysql-primary:3306"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.equal(await page.$eval(".view-inspector", (e) => Math.round(e.getBoundingClientRect().width)), 340);
  const upInspector = await h.inspector();
  assert.match(upInspector, /MySQL 8\.4\.11/);
  assert.match(upInspector, /Purpose/);
  assert.match(upInspector, /cdc, query/);
  assert.match(upInspector, /Health/);
  assert.match(upInspector, /UP/);
  assert.match(upInspector, /Pool connections/);
  assert.match(upInspector, /Database/);
  // S12: the lab upstreams matched by endpoint evidence, an address match.
  assert.match(upInspector, /Matched by address/, "an address-only upstream match is labelled as such");
  assert.doesNotMatch(upInspector, /Verified/, "an address-only match never reads Verified");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  // S12: an identity match (the server's own uuid) reads Verified.
  const idMatch = structuredClone(R.lab);
  idMatch.topology.upstreams = idMatch.topology.upstreams.map((u, i) =>
    i === 0 ? { ...u, evidence: "server_uuid" } : u,
  );
  await refreshTo(idMatch);
  // Escape left the upstream selected; switching tabs clears the selection so
  // the click opens the inspector instead of toggling it closed.
  await tab("Caches");
  await tab("Topology");
  await page.click(`${view} .readyset-topology button[data-row-key="upstream:mysql-primary:3306"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  const idInspector = await h.inspector();
  assert.match(idInspector, /Verified engine/, "an identity match reads Verified engine");
  assert.match(idInspector, /Verification\s*Verified/, "an identity match reads Verified");
  assert.doesNotMatch(idInspector, /Matched by address/, "an identity match never reads Matched by address");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await refreshTo(R.lab);
  assert.doesNotMatch(await h.text(view), /Upstream database servers/, "no duplicated upstream table on Topology");
  assert.match(await h.text(view), /Active client connections/, "the client connections table stays");
  await shot("readyset-topology-1440.png");
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await pause(200);
  await shot("readyset-topology-1440-dark.png");
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await pause(150);

  // Lane rs1-B: the lab topology stays compact, and its states are honest.
  const topoSel = `${view} .readyset-topology`;
  const topoHeight = await page.$eval(topoSel, (e) => Math.round(e.getBoundingClientRect().height));
  assert.ok(topoHeight <= 480, `lab topology stays compact at 1440 px: ${topoHeight} <= 480`);

  // A link label is plain text on a plate beside the stem, in the accessibility tree.
  const plate = await page.$eval(`${topoSel} .tier-link-plate`, (e) => ({
    visible: !!e.offsetParent,
    hidden: !!e.closest('[aria-hidden="true"]'),
    text: (e.textContent || "").trim(),
  }));
  assert.ok(plate.visible && !plate.hidden && plate.text.length > 0, `label plate is readable: ${JSON.stringify(plate)}`);

  // S11: a report with no reported states runs nothing and marks nothing OK.
  // Backend-shaped (G2): Status null, Replication Status "Unknown", ProxySQL
  // backend status and upstream health null; replication is always an object.
  const unread = structuredClone(R.lab);
  unread.status = "unknown";
  unread.replication = {
    ...unread.replication,
    status: "Unknown",
    mode: null,
    lag_unit: null,
    consume_lag: null,
    persist_lag: null,
    upstream_offset: null,
    replicator_offset: null,
    persisted_offset: null,
    heartbeat_staleness_s: null,
  };
  unread.status_fields.status = null;
  unread.status_fields.replication_status = null;
  unread.topology.this_server.status = null;
  unread.topology.this_server.replication_status = null;
  unread.topology.proxysql_nodes = unread.topology.proxysql_nodes.map((p) => ({ ...p, backend_status: null }));
  unread.topology.upstreams = unread.topology.upstreams.map((u) => ({ ...u, health: null }));
  unread.upstreams = unread.upstreams.map((u) => ({ ...u, health: null }));
  await refreshTo(unread);
  assert.equal(await page.$(`${topoSel} .tier-link.is-running`), null, "no link runs on an unreported state");
  assert.equal(await page.$(`${topoSel} .cluster-stem.is-active`), null, "no active stem on an unreported state");
  assert.equal(await page.$(`${topoSel} .cluster-mark.is-ok`), null, "no OK mark on an unreported state");
  const unreadText = await h.text(topoSel);
  assert.match(unreadText, /Not read/);
  assert.doesNotMatch(unreadText, /ONLINE|Running|\bUP\b/, "no state word survives an unreported state");
  // "Unknown" is the backend's unread placeholder: the CDC plate reads
  // "Replication not read", never the CDC label with a binlog fallback.
  const unreadPlates = await page.$$eval(`${topoSel} .tier-link-plate`, (els) =>
    els.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()),
  );
  assert.ok(
    unreadPlates.includes("Replication not read"),
    `unreported replication reads Replication not read: ${JSON.stringify(unreadPlates)}`,
  );
  assert.ok(
    !unreadPlates.some((p) => /CDC replication|binlog/.test(p)),
    `an unreported replication never draws the CDC plate: ${JSON.stringify(unreadPlates)}`,
  );
  // The Base replication tile and the maintenance row read "Not read" too.
  const unreadTiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      value: e.querySelector(".tile-value")?.textContent?.trim() || "",
    })),
  );
  assert.equal(unreadTiles.find((t) => t.label === "Base replication")?.value, "Not read", "unreported replication reads Not read");
  await tab("Parameters");
  const maintUnread = await page.$$eval(`${view} .grouped-item`, (els) =>
    els.filter((e) => /Maintenance mode/.test(e.textContent)).map((e) => ({
      text: e.textContent.replace(/\s+/g, " ").trim(),
      pill: e.querySelector(".status-pill")?.className || "",
    })),
  );
  assert.match(maintUnread[0]?.text || "", /Not read/, "unreported Status reads Not read");
  assert.doesNotMatch(maintUnread[0]?.text || "", /Normal/, "unreported Status is never Normal");
  assert.match(maintUnread[0]?.pill || "", /is-unknown/, "unreported Status has the unknown tone");
  assert.doesNotMatch(maintUnread[0]?.pill || "", /is-ok/, "unreported Status never has the OK tone");
  await tab("Topology");

  // An unreported upstream health (null) reads "Not read" in the inspector,
  // never an UNKNOWN placeholder.
  await page.click(`${topoSel} button[data-row-key="upstream:mysql-primary:3306"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  const unreadUpInspector = await h.inspector();
  assert.match(unreadUpInspector, /Health\s*Not read/, "an unreported upstream health reads Not read");
  assert.doesNotMatch(unreadUpInspector, /UNKNOWN/, "an unreported upstream health never reads UNKNOWN");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));

  // Disabled replication (shallow-cache mode) is a reported state with its own
  // label, never a CDC connection and never unread.
  const disabledRepl = structuredClone(R.lab);
  disabledRepl.replication = { ...disabledRepl.replication, status: "Disabled" };
  disabledRepl.status_fields.replication_status = "Disabled";
  disabledRepl.topology.this_server.replication_status = "Disabled";
  await refreshTo(disabledRepl);
  const disPlates = await page.$$eval(`${topoSel} .tier-link-plate`, (els) =>
    els.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()),
  );
  assert.ok(
    disPlates.includes("Replication disabled · query fallback"),
    `Disabled replication reads Replication disabled: ${JSON.stringify(disPlates)}`,
  );
  assert.ok(
    !disPlates.some((p) => /CDC replication/.test(p)),
    `Disabled replication is never labelled as a CDC connection: ${JSON.stringify(disPlates)}`,
  );
  const disLinks = await page.$$eval(`${topoSel} .tier-link`, (els) => els.map((e) => e.className));
  assert.doesNotMatch(disLinks[disLinks.length - 1] || "", /is-running/, "a disabled replication never runs the CDC link");
  assert.match(disLinks[disLinks.length - 1] || "", /is-stopped/, "a disabled replication dashes the CDC link");
  assert.doesNotMatch(disLinks[disLinks.length - 1] || "", /is-unread/, "a reported Disabled is not unread");
  await refreshTo(R.lab);

  // The shared ProxySQL backend rule: a SHUNNED_REPLICATION_LAG backend is a
  // reported stop (watch card, dashed link), never UNKNOWN and never running.
  const shunnedPx = structuredClone(R.lab);
  shunnedPx.topology.proxysql_nodes = shunnedPx.topology.proxysql_nodes.map((p) => ({
    ...p,
    backend_status: "SHUNNED_REPLICATION_LAG",
  }));
  await refreshTo(shunnedPx);
  const shunnedCard = await page.$eval(`${topoSel} .tier-card`, (e) => ({
    text: (e.textContent || "").replace(/\s+/g, " ").trim(),
    cls: e.className,
  }));
  assert.match(shunnedCard.cls, /tone-watch/, "a shunned ProxySQL backend keeps the watch tone");
  assert.match(shunnedCard.text, /SHUNNED_REPLICATION_LAG/, "the shunned variant word is shown");
  assert.doesNotMatch(shunnedCard.text, /Not read/, "a reported shunned state is never Not read");
  const shunnedLink = await page.$eval(`${topoSel} .tier-link`, (e) => e.className);
  assert.match(shunnedLink, /is-stopped/, "a shunned ProxySQL backend stops the routing link");
  assert.doesNotMatch(shunnedLink, /is-running/, "a shunned ProxySQL backend never runs the routing link");

  // The UNKNOWN placeholder is not a reported state: Not read, unknown tone.
  const unknownPx = structuredClone(R.lab);
  unknownPx.topology.proxysql_nodes = unknownPx.topology.proxysql_nodes.map((p) => ({ ...p, backend_status: "UNKNOWN" }));
  await refreshTo(unknownPx);
  const unknownPxCard = await page.$eval(`${topoSel} .tier-card`, (e) => ({
    text: (e.textContent || "").replace(/\s+/g, " ").trim(),
    cls: e.className,
  }));
  assert.match(unknownPxCard.text, /Not read/, "UNKNOWN reads Not read");
  assert.doesNotMatch(unknownPxCard.text, /UNKNOWN/, "the UNKNOWN placeholder is never shown");
  assert.match(unknownPxCard.cls, /tone-unknown/, "UNKNOWN keeps the unknown tone");
  const unknownPxLink = await page.$eval(`${topoSel} .tier-link`, (e) => e.className);
  assert.match(unknownPxLink, /is-unread/, "UNKNOWN leaves the routing link unread");
  await refreshTo(R.lab);

  // A down active CDC upstream stands detached (dashed edge, broken stem); a
  // down non-CDC upstream keeps the watch edge.
  const down = structuredClone(R.lab);
  down.topology.upstreams = down.topology.upstreams.map((u) => ({ ...u, health: "down" }));
  await refreshTo(down);
  assert.ok(await page.$(`${topoSel} .tier-node.is-detached .cluster-card.is-detached`), "down active CDC upstream is detached");
  assert.ok(await page.$(`${topoSel} .tier-node.is-detached > .cluster-stem.is-broken`), "a detached upstream's stem is broken");
  assert.ok(await page.$(`${topoSel} .cluster-card.tone-watch:not(.is-detached)`), "down non-CDC upstream keeps the watch edge");

  // A replica reported as IO Connecting with SQL Yes reads Connecting with the
  // watch tone; Not read is reserved for absent values.
  const connecting = structuredClone(R.lab);
  connecting.topology.database_peers = [
    {
      key: "replica:mysql-rep2:3306",
      relation: "replica",
      name: "MySQL Replica 2",
      host: "mysql-rep2",
      port: 3306,
      address: "mysql-rep2:3306",
      io_running: "Connecting",
      sql_running: "Yes",
      lag_s: null,
      product: "MySQL",
      version: "8.4.11",
      profile_id: null,
      profile_name: null,
      verified: false,
      evidence: null,
    },
  ];
  await refreshTo(connecting);
  const connCard = await page.$$eval(`${topoSel} .tier-card`, (els) =>
    els
      .map((e) => ({ text: (e.textContent || "").replace(/\s+/g, " ").trim(), cls: e.className }))
      .find((c) => c.text.includes("MySQL Replica 2")),
  );
  assert.match(connCard?.text || "", /Connecting/, "a connecting replica reads Connecting");
  assert.doesNotMatch(connCard?.text || "", /Not read/, "a reported Connecting is never Not read");
  assert.match(connCard?.cls || "", /tone-watch/, "a connecting replica keeps the watch tone");
  await refreshTo(R.lab);

  // A stale report claims no "This connection", never borrows the connection
  // in use's address (mysql.test:3306), renders no Use chips, and disables
  // the inspector's Use button like the diagram's.
  const staleNoAddr = structuredClone(R.lab);
  staleNoAddr.topology.this_server.address = null;
  await refreshTo(staleNoAddr);
  assert.match(await h.text(topoSel), /This connection/, "a current report labels its own card This connection");
  await chooseConnection(page, await connectionOption(page, "Plain MySQL"));
  await page.waitForFunction(
    (sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")),
    {},
    view,
  );
  const staleTopoText = await h.text(topoSel);
  assert.doesNotMatch(staleTopoText, /This connection/, "a stale report claims no This connection");
  assert.doesNotMatch(staleTopoText, /mysql\.test:3306/, "a stale report never borrows the connection in use's address");
  assert.match(staleTopoText, /Connected instance/, "a stale report with no reported address reads Connected instance");
  assert.equal(await page.$eval(topoSel, (e) => e.dataset.motion), "off", "a stale report runs no motion");
  assert.equal((await page.$$(`${topoSel} .cluster-use-node`)).length, 0, "a stale report renders no Use chips");
  await page.click(`${topoSel} button[data-row-key="upstream:mysql-replica:3306"]`);
  await page.waitForSelector(".view-inspector .inspector-title");
  const staleUseButton = await page.$(".view-inspector .review-actions button");
  assert.ok(staleUseButton, "the stale upstream inspector keeps the Use button");
  assert.equal(
    await page.$eval(".view-inspector .review-actions button", (b) => b.disabled),
    true,
    "a stale report disables the inspector Use button",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await pause(250);
  await refreshTo(R.lab);
  await tab("Caches");
  await tab("Topology");

  // S12: an upstream at a saved profile's address gets no Use chip from the
  // address alone; only a backend profile_id renders one.
  const noChip = structuredClone(R.lab);
  noChip.topology.upstreams = noChip.topology.upstreams.map((u, i) =>
    i === 0 ? { ...u, host: "127.0.0.1", address: "127.0.0.1:3306", profile_id: null, profile_name: null } : u,
  );
  await refreshTo(noChip);
  const chips = await page.$$eval(`${topoSel} .tier-node`, (els) =>
    els.map((e) => ({ name: e.querySelector(".cluster-name")?.textContent || "", use: !!e.querySelector(".cluster-use-node") })),
  );
  assert.deepEqual(
    chips.find((c) => c.name === "127.0.0.1:3306"),
    { name: "127.0.0.1:3306", use: false },
    "no Use chip from an address match alone",
  );
  assert.ok(chips.some((c) => c.use), "backend profile_ids still render Use chips");

  // S12: a peer pill labels the match evidence: an identity match reads
  // Verified, an address-only match reads Matched by address, never Verified.
  const peered = structuredClone(R.lab);
  peered.topology.database_peers = [
    {
      key: "replica:aaaa-1",
      relation: "replica",
      name: "Replica One",
      host: "mysql-replica",
      port: 3306,
      address: "mysql-replica:3306",
      io_running: "Yes",
      sql_running: "Yes",
      lag_s: 2,
      product: "MySQL",
      version: "8.4.11",
      profile_id: null,
      profile_name: null,
      verified: true,
      evidence: "server_uuid",
    },
    {
      key: "replica:bbbb-2",
      relation: "replica",
      name: "Replica Two",
      host: "mysql-replica2",
      port: 3306,
      address: "mysql-replica2:3306",
      io_running: "Yes",
      sql_running: "Yes",
      lag_s: 5,
      product: null,
      version: null,
      profile_id: null,
      profile_name: null,
      verified: true,
      evidence: "endpoint",
    },
    {
      key: "replica:cccc-3",
      relation: "replica",
      name: "Replica Three",
      host: "mysql-replica3",
      port: 3306,
      address: "mysql-replica3:3306",
      io_running: null,
      sql_running: null,
      lag_s: null,
      product: null,
      version: null,
      profile_id: null,
      profile_name: null,
      verified: false,
      evidence: null,
    },
  ];
  await refreshTo(peered);
  const peerPills = await page.$$eval(`${topoSel} .tier-node .cluster-flow .pill`, (els) =>
    els.map((e) => (e.textContent || "").trim()),
  );
  assert.deepEqual(
    peerPills.filter((t) => t === "Verified connection").length,
    1,
    `identity-matched peer reads Verified connection: ${JSON.stringify(peerPills)}`,
  );
  assert.deepEqual(
    peerPills.filter((t) => t === "Matched by address").length,
    1,
    `address-matched peer reads Matched by address: ${JSON.stringify(peerPills)}`,
  );
  assert.deepEqual(
    peerPills.filter((t) => t === "Discovered peer").length,
    1,
    `unverified peer reads Discovered peer: ${JSON.stringify(peerPills)}`,
  );
  await refreshTo(R.lab);

  // S12: a replication channel matched to the upstream by host:port alone
  // (channel_source, topology_links.py) reads Matched by address, never
  // Verified; only identity evidence (server_uuid) reads Verified.
  const chanPeer = structuredClone(peered);
  chanPeer.topology.database_peers = [{ ...peered.topology.database_peers[1], evidence: "channel_source" }];
  await refreshTo(chanPeer);
  const chanPills = await page.$$eval(`${topoSel} .tier-node .cluster-flow .pill`, (els) =>
    els.map((e) => (e.textContent || "").trim()),
  );
  assert.deepEqual(chanPills.filter((t) => t === "Verified connection").length, 0, "channel_source never reads Verified");
  assert.deepEqual(chanPills.filter((t) => t === "Matched by address").length, 1, "channel_source reads Matched by address");
  await refreshTo(R.lab);

  // S8: markup in a server name stays text, never an element or a script.
  const evil = structuredClone(R.lab);
  evil.topology.upstreams = evil.topology.upstreams.map((u, i) =>
    i === 0
      ? { ...u, profile_name: "</script><img src=x onerror=window.__rsEvil=1>", address: "</script><img src=x onerror=window.__rsEvil=1>" }
      : u,
  );
  evil.topology.proxysql_nodes = evil.topology.proxysql_nodes.map((p) => ({
    ...p,
    label: "</script><img src=x onerror=window.__rsEvil=1>",
    host: "<img src=x onerror=window.__rsEvil=1>",
  }));
  await refreshTo(evil);
  assert.equal(await page.$(`${topoSel} img`), null, "server text adds no element");
  assert.equal(await page.evaluate(() => window.__rsEvil || 0), 0, "server text runs no script");
  assert.match(await h.text(topoSel), /<img src=x/, "server text renders as text");
  await refreshTo(R.lab);

  // 4. Caches tab: inspector, Export SQL, Create cache with live EXPLAIN & collision warning, ActionReview.
  await tab("Caches");

  // S8: markup in a row name or a why_not stays text in the inspector's
  // action group; it adds no element and runs no script.
  const evilRow = structuredClone(R.lab);
  evilRow.caches[1].name = "</script><img src=x onerror=window.__rsEvilRow=1>";
  evilRow.caches[1].actions = evilRow.caches[1].actions.map((a) =>
    a.id === "drop_cache"
      ? { ...a, enabled: false, why_not: "</script><img src=x onerror=window.__rsEvilRow=1>", fix: "Run this change on Readyset by hand." }
      : a,
  );
  await refreshTo(evilRow);
  await selectRow("cache:</script><img src=x onerror=window.__rsEvilRow=1>");
  assert.equal(await page.$(".view-inspector img"), null, "server text adds no element");
  assert.equal(await page.evaluate(() => window.__rsEvilRow || 0), 0, "server text runs no script");
  assert.match(await h.inspector(), /<img src=x/, "server text renders as text");
  await refreshTo(R.lab);
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-detail")), false);
  const cacheInsp = await h.inspector();
  assert.match(cacheInsp, /Shallow cache efficiency/);
  assert.match(cacheInsp, /90%/);
  assert.match(cacheInsp, /540 \/ 60/);
  assert.match(cacheInsp, /Cached execution latency/);
  assert.deepEqual(await inspectorButtons(), ["Flush entries…", "Drop cache…"]);

  // Review Flush entries…, then Escape -> declined, nothing sent.
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  await clickAction("Flush entries…");
  await waitSheet();
  assert.equal(plans.length, 1);
  assert.equal(plans[0].action, "flush_cache");
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), R["plan-flush-cache"].statements.join("\n"));
  await page.keyboard.press("Escape");
  await noSheet();
  for (let i = 0; i < 50 && !declined.length; i++) await pause(20);
  assert.equal(declined.length, 1);
  assert.equal(declined[0].operation, "readyset_action");
  assert.equal(sends.length, 0);

  // Confirm Flush entries… -> Sending… -> Sent -> Done -> refreshes report.
  await clickAction("Flush entries…");
  await waitSheet();
  const readsBeforeFlush = state.seen.length;
  progressAnswer = () => ({ status: 200, body: R["progress-checking"] });
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"], delay: 1800 });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction(
    (s) => /Reading the target again/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  assert.equal(await page.$eval(`${sheet} .sheet-actions button:last-child`, (b) => b.textContent), "Sending…");
  assert.equal(await page.$eval(`${sheet} .sheet-actions button:last-child`, (b) => b.getAttribute("aria-disabled")), "true");
  progressAnswer = () => ({ status: 200, body: R["progress-sending"] });
  await page.waitForFunction(
    (s) => /Sent\. Waiting for the server's answer/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  assert.deepEqual(
    await page.$$eval(`${sheet} .action-steps li`, (els) => els.map((e) => e.dataset.state)),
    ["sent"],
  );
  const flushDone = await answered();
  assert.match(flushDone.outcome, /^Done/);
  assert.deepEqual(flushDone.steps, ["done"]);
  assert.equal(sends.length, 1);
  assert.equal(sends[0].review_token, R["plan-flush-cache"].review_token);
  assert.equal(sends[0].plan.review_token, undefined);
  await closeSheet();
  await page.waitForSelector(`${view} .action-result`);
  for (let i = 0; i < 50 && state.seen.length === readsBeforeFlush; i++) await pause(20);
  assert.equal(state.seen.length, readsBeforeFlush + 1, "Readyset refreshes after a confirmed write");
  // S2: a database change clears a plain answered result panel; only an
  // answered Outcome unknown stays visible across a resetKey reset.
  const staleUnderDone = structuredClone(R.lab);
  staleUnderDone.session_database = "otherdb";
  await refreshTo(staleUnderDone);
  assert.equal(await page.$(`${view} .action-result`), null, "a database change clears a plain result panel");
  await refreshTo(R.lab);
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });

  // Export SQL modal (EXPLAIN ALL CACHES).
  await backToList();
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} .readyset-strip-buttons button`)].find((b) => b.textContent.trim() === "Export SQL").click(),
    view,
  );
  await page.waitForSelector("dialog.readyset-export-dialog[open]", { visible: true });
  assert.equal(exportsSeen.length, 1);
  assert.match(await page.$eval("dialog.readyset-export-dialog[open]", (d) => d.textContent), /CREATE DEEP CACHE `c_orders_deep`/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.readyset-export-dialog[open]"));

  // Create cache sheet: validation, EXPLAIN dry-run, collision warning, review.
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} .readyset-strip-buttons button`)].find((b) => b.textContent.trim() === "Create cache…").click(),
    view,
  );
  const createDialog = "dialog.readyset-create-dialog[open]";
  await page.waitForSelector(createDialog, { visible: true });
  const createConfirm = `${createDialog} .sheet-actions button:last-child`;
  assert.equal(await page.$eval(createConfirm, (b) => b.disabled), true, "disabled before query entered");
  await page.type(`${createDialog} textarea`, "q_1111aaaa");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Collides with existing cache c_orders_deep/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);
  // S8: the server-reported collision name inside the sentence is bidi-isolated.
  assert.equal(
    await page.$eval(`${createDialog} .ops-warning bdi`, (b) => b.textContent),
    "c_orders_deep",
    "the collision name renders in a bdi",
  );
  assert.match(await page.$eval(createDialog, (d) => d.textContent), /Shallow: Supported/);
  assert.match(await page.$eval(createDialog, (d) => d.textContent), /Deep: Supported/);

  // Editing the draft discards the previous check: no stale support verdict
  // can stay bound to a query it was not run for.
  await page.$eval(`${createDialog} textarea`, (t) => {
    t.select();
  });
  await page.keyboard.type("SELECT id FROM orders");
  assert.equal(await page.$(`${createDialog} .readyset-explain-pills`), null, "editing the draft drops stale support results");

  // Unsupported query disables Review CREATE CACHE…
  await page.$eval(`${createDialog} textarea`, (t) => {
    t.select();
  });
  await page.keyboard.type("SELECT RAND(), id FROM orders");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Shallow: Unsupported/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);
  assert.equal(await page.$eval(createConfirm, (b) => b.disabled), true, "unsupported query cannot be reviewed");

  // Valid query -> Review CREATE CACHE… -> opens ActionReview with exact CREATE SHALLOW CACHE SQL.
  await page.$eval(`${createDialog} textarea`, (t) => {
    t.select();
  });
  await page.keyboard.type("q_3333cccc");
  await page.evaluate(
    (d) => [...document.querySelectorAll(`${d} .readyset-explain-bar button`)].find((b) => /Check support/.test(b.textContent)).click(),
    createDialog,
  );
  await page.waitForFunction((d) => /Shallow: Supported/.test(document.querySelector(d)?.textContent || ""), {}, createDialog);

  // Q2.1c: the cache name goes to the backend as typed: only ordinary spaces
  // are trimmed, so a boundary control character (a leading tab) is not
  // silently removed before the backend's own refusal can name it.
  await page.$eval(`${createDialog} input[placeholder="orders_by_customer"]`, (i) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(i, "\torders_q2");
    i.dispatchEvent(new Event("input", { bubbles: true }));
  });

  await shot("readyset-create-cache-1440.png");
  planAnswer = () => ({ status: 200, body: R["plan-create-cache"] });
  await page.click(createConfirm);
  await waitSheet();
  assert.equal(plans.at(-1).action, "create_cache");
  assert.equal(plans.at(-1).value.name, "\torders_q2", "the name is submitted as typed, boundary control characters kept");
  assert.match(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), /CREATE SHALLOW CACHE `c_orders_status`/);
  await page.keyboard.press("Escape");
  await noSheet();

  // A failed refresh keeps the last good report but marks it stale: Export
  // SQL and the catalog buttons stay disabled until a read succeeds (stale,
  // not only another connection's report).
  const seenBeforeFail = state.seen.length;
  state.fallback = { status: 500, body: { error: "boom" } };
  await h.refresh();
  for (let i = 0; i < 50 && state.seen.length === seenBeforeFail; i++) await pause(20);
  assert.equal(state.seen.length, seenBeforeFail + 1, "the failed refresh sent one read");
  await pause(300);
  const stripButtons = () =>
    page.$$eval(`${view} .readyset-strip-buttons button`, (els) =>
      els.map((e) => ({ label: e.textContent.trim(), off: e.disabled || e.getAttribute("aria-disabled") === "true" })),
    );
  const duringFail = await stripButtons();
  assert.equal(duringFail.find((b) => b.label === "Export SQL")?.off, true, "Export SQL stays disabled on a stale (failed) report");
  assert.equal(duringFail.find((b) => b.label === "Create cache…")?.off, true, "a stale report blocks Create cache");
  await refreshTo(R.lab);
  const afterFail = await stripButtons();
  assert.equal(afterFail.find((b) => b.label === "Export SQL")?.off, false, "Export SQL re-enables after a successful read");

  // P2b.2/3: a report read with another default database is stale like a
  // report from another connection: every action is blocked until a fresh
  // read, the marker is the group's blocked hint, and while blocked each
  // button's aria-describedby starts with that hint's id.
  const otherDb = structuredClone(R.lab);
  otherDb.session_database = "otherdb";
  await refreshTo(otherDb);
  const dbBlocked = await page.$eval(`${view} .readyset-strip-buttons`, (strip) => {
    const btn = [...strip.querySelectorAll(".review-actions button")].find((b) => b.textContent.trim() === "Create cache…");
    const ids = (btn?.getAttribute("aria-describedby") || "").split(" ").filter(Boolean);
    const hint = btn?.closest(".review-actions")?.querySelector("p.hint");
    return {
      off: btn?.getAttribute("aria-disabled") || "",
      ids,
      hintId: hint?.id || "",
      hintText: (hint?.textContent || "").trim(),
      firstText: (document.getElementById(ids[0] || "")?.textContent || "").trim(),
    };
  });
  assert.equal(dbBlocked.off, "true", "a database change blocks every action");
  assert.equal(
    dbBlocked.hintText,
    "This report was read with another default database. Refresh to read the connection in use.",
  );
  assert.ok(dbBlocked.ids.length > 0 && dbBlocked.ids[0] === dbBlocked.hintId, "the blocked hint id comes first in aria-describedby");
  assert.equal(dbBlocked.firstText, dbBlocked.hintText, "the describedby's first target is the blocked hint");
  const plansBeforeDb = plans.length;
  await page.evaluate(
    (sel) =>
      [...document.querySelectorAll(`${sel} .readyset-strip-buttons .review-actions button`)]
        .find((b) => b.textContent.trim() === "Create cache…")
        .click(),
    view,
  );
  await pause(200);
  assert.equal(plans.length, plansBeforeDb, "a database-stale report plans nothing");
  assert.equal(
    (await stripButtons()).find((b) => b.label === "Export SQL")?.off,
    true,
    "Export SQL stays disabled on a database-stale report",
  );
  // Q2.2b: a database-stale report gets the same visible stale banner and
  // is-stale dimming a failed read gets.
  assert.ok(await page.$(`${view}.is-stale`), "a database-stale report dims the place (is-stale)");
  assert.equal(
    await page.$eval(`${view} .gate.cluster-stale p`, (e) => (e.textContent || "").trim()),
    "This report was read with another default database. Refresh to read the connection in use.",
    "the database-stale banner names the database",
  );
  // Q2.2d: the sidebar treats a report from another default database as not read.
  assert.equal(await page.$(`${nav} .nav-marker`), null, "no sidebar Readyset marker for a database-stale report");
  await refreshTo(R.lab);
  assert.equal(await page.$(`${view}.is-stale`), null, "a fresh read clears the dimming");
  assert.equal(await page.$(`${view} .gate.cluster-stale`), null, "a fresh read clears the stale banner");
  assert.ok(await page.$(`${nav} .nav-marker .status-dot.is-ok`), "the sidebar marker returns with a fresh read");
  assert.equal(
    (await stripButtons()).find((b) => b.label === "Create cache…")?.off,
    false,
    "a fresh read with the connection's database unblocks actions",
  );

  // Q2.2a: the database compare is exact, never trimmed: a report read with
  // " shop" is stale on a "shop" connection.
  const spaceDb = structuredClone(R.lab);
  spaceDb.session_database = " shop";
  await refreshTo(spaceDb);
  assert.equal(
    (await stripButtons()).find((b) => b.label === "Create cache…")?.off,
    true,
    'a report read with " shop" is stale on a "shop" connection',
  );
  assert.ok(await page.$(`${view}.is-stale`), 'the " shop" report dims the place too');
  await refreshTo(R.lab);
  assert.equal(
    (await stripButtons()).find((b) => b.label === "Create cache…")?.off,
    false,
    "an exact database match unblocks actions",
  );

  // Q2.2c: a prompt opened before the database changed closes when the
  // report turns database-stale.
  await tab("Parameters");
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} button`)].find((b) => b.textContent.trim() === "Set log level…").click(),
    view,
  );
  await page.waitForSelector("dialog.action-value[open]", { visible: true });
  const staleUnderPrompt = structuredClone(R.lab);
  staleUnderPrompt.session_database = "otherdb";
  const seenBeforePromptStale = state.seen.length;
  state.fallback = { status: 200, body: staleUnderPrompt };
  await page.evaluate(() => [...document.querySelectorAll("[data-action=refresh]")].find((b) => b.offsetParent)?.click());
  await page.waitForFunction(() => !document.querySelector("dialog.action-value[open]"));
  for (let i = 0; i < 50 && state.seen.length === seenBeforePromptStale; i++) await pause(20);
  assert.equal(await page.$("dialog.action-value[open]"), null, "a database change closes an open prompt");

  // Q2.2c: an open ActionReview closes (recorded as declined) when the
  // database changes under it, and sends nothing.
  await refreshTo(R.lab);
  await tab("Caches");
  await selectRow("cache:c_recent_shallow");
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  const declinedBeforeDb = declined.length;
  const sendsBeforeDb = sends.length;
  await clickAction("Flush entries…");
  await waitSheet();
  const staleUnderReview = structuredClone(R.lab);
  staleUnderReview.session_database = "otherdb";
  state.fallback = { status: 200, body: staleUnderReview };
  await page.evaluate(() => [...document.querySelectorAll("[data-action=refresh]")].find((b) => b.offsetParent)?.click());
  await noSheet();
  for (let i = 0; i < 50 && declined.length === declinedBeforeDb; i++) await pause(20);
  assert.equal(declined.length, declinedBeforeDb + 1, "a database change declines an open review");
  assert.equal(sends.length, sendsBeforeDb, "a database change sends nothing");
  await backToList();
  await refreshTo(R.lab);

  // S2: a plan request still in flight when the default database changes
  // never opens its sheet afterwards: the answer belongs to the closed
  // database context, so it is dropped with no sheet and no audit row.
  await selectRow("cache:c_recent_shallow");
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"], delay: 1000 });
  const plansBeforeInFlight = plans.length;
  const declinedBeforeInFlight = declined.length;
  const sendsBeforeInFlight = sends.length;
  await clickAction("Flush entries…");
  for (let i = 0; i < 50 && plans.length === plansBeforeInFlight; i++) await pause(20);
  assert.equal(plans.length, plansBeforeInFlight + 1, "the plan request is in flight");
  const staleUnderPlan = structuredClone(R.lab);
  staleUnderPlan.session_database = "otherdb";
  const seenBeforeInFlight = state.seen.length;
  state.fallback = { status: 200, body: staleUnderPlan };
  await page.evaluate(() => [...document.querySelectorAll("[data-action=refresh]")].find((b) => b.offsetParent)?.click());
  for (let i = 0; i < 50 && state.seen.length === seenBeforeInFlight; i++) await pause(20);
  await page.waitForFunction(
    (sel) => /another default database/.test(document.querySelector(`${sel} .gate.cluster-stale p`)?.textContent || ""),
    {},
    view,
  );
  await pause(1200);
  assert.equal(await page.$(sheet), null, "a plan answered after the database change opens no sheet");
  assert.equal(declined.length, declinedBeforeInFlight, "a dropped plan writes no audit row");
  assert.equal(sends.length, sendsBeforeInFlight, "a dropped plan sends nothing");
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  await backToList();
  await refreshTo(R.lab);

  // Policy change after review -> Nothing sent.
  const rsLab = (await api("/api/profiles/list", {})).items.find((p) => p.name === "Readyset lab");
  await selectRow("cache:c_recent_shallow");
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  await clickAction("Flush entries…");
  await waitSheet();
  const sendsAtPolicy = sends.length;
  await api("/api/profiles/save", { id: rsLab.id, value: { ...rsLab, type: "production" } });
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction((s) => !document.querySelector(s) || document.querySelector(`${s} .action-outcome`), {}, sheet);
  if (await page.$(`${sheet} .action-outcome`)) {
    assert.match((await answered()).outcome, /Nothing sent/);
    await closeSheet();
  }
  assert.equal(sends.length, sendsAtPolicy, "nothing sent after policy changed");
  await api("/api/profiles/save", { id: rsLab.id, value: { ...rsLab, type: "development" } });
  await page.reload({ waitUntil: "networkidle0" });
  await page.click(nav);
  await page.waitForSelector(`${view} .summary-line`);
  await refreshTo(R.lab);
  await tab("Caches");
  await selectRow("cache:c_recent_shallow");

  // Read-only refusal opens no sheet.
  planAnswer = () => ({ status: 200, body: R["plan-read-only"] });
  await clickAction("Flush entries…");
  await page.waitForFunction((sel) => /read-only in myflames/.test(document.querySelector(sel)?.textContent || ""), {}, view);
  assert.equal(await page.$(sheet), null);

  // Lost reply -> Outcome unknown, never retried, Audit log jump.
  planAnswer = () => ({ status: 200, body: R["plan-flush-cache"] });
  sendAnswer = () => ({ status: 500, body: { error: "connection lost while waiting for a reply" }, delay: 1400 });
  progressAnswer = () => ({ status: 200, body: R["progress-finished-unknown"] });
  const sendsBeforeLost = sends.length;
  await clickAction("Flush entries…");
  await waitSheet();
  await page.click(`${sheet} .sheet-actions button:last-child`);
  await page.waitForFunction(
    (s) => /No answer arrived from the server/.test(document.querySelector(`${s} .action-phase`)?.textContent || ""),
    {},
    sheet,
  );
  const lost = await answered();
  assert.match(lost.outcome, /^Outcome unknown/);
  assert.deepEqual(lost.steps, ["unknown"]);
  assert.match(lost.text, /What to check/);
  await closeSheet();
  await page.waitForFunction((sel) => /Outcome unknown/.test(document.querySelector(`${sel} .action-result`)?.textContent || ""), {}, view);
  await pause(250);
  assert.equal(sends.length, sendsBeforeLost + 1, "Outcome unknown is never retried");
  await resultButton("Audit log");
  await page.waitForSelector("#audit-title", { visible: true });
  await page.click(nav);
  await page.waitForSelector(`${view} .action-result`, { visible: true });
  // S2: a database change (resetKey) must not clear a visible Outcome
  // unknown result panel: the Audit log row alone does not show whether the
  // change ran. It survives both turns of the reset (stale, then fresh).
  const staleUnderUnknown = structuredClone(R.lab);
  staleUnderUnknown.session_database = "otherdb";
  await refreshTo(staleUnderUnknown);
  assert.match(await h.text(`${view} .action-result`), /Outcome unknown/, "a database change keeps an Outcome unknown panel");
  await refreshTo(R.lab);
  assert.match(await h.text(`${view} .action-result`), /Outcome unknown/, "a fresh read keeps the Outcome unknown panel");
  await resultButton("Close");
  sendAnswer = () => ({ status: 200, body: R["result-flush-done"] });

  // 5. Proxied tab: filtering + Create cache… pre-fills query_id and runs EXPLAIN.
  await backToList();
  await tab("Proxied");
  assert.equal(await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.length), 2);
  await page.type(`${view} .readyset-strip-filters input[type="search"]`, "RAND");
  await pause(150);
  assert.equal(await page.$$eval(`${view} tbody tr[data-row-key]`, (els) => els.length), 1);
  await page.$eval(`${view} .readyset-strip-filters input[type="search"]`, (i) => {
    i.select();
  });
  await page.keyboard.press("Backspace");
  await pause(150);
  await selectRow("pq:q_3333cccc");
  const explainsBeforePre = explains.length;
  await clickAction("Create cache…");
  await page.waitForSelector(createDialog, { visible: true });
  assert.equal(await page.$eval(`${createDialog} textarea`, (t) => t.value), "q_3333cccc");
  for (let i = 0; i < 50 && explains.length === explainsBeforePre; i++) await pause(20);
  assert.equal(explains.at(-1).query_or_id, "q_3333cccc", "pre-filled query_id triggers EXPLAIN automatically");
  await page.keyboard.press("Escape");
  await page.waitForFunction((d) => !document.querySelector(d), {}, createDialog);

  // 6. Tables tab: mode-aware replication lag (transactions vs bytes) & typed-guard Re-snapshot.
  await backToList();
  await tab("Tables");
  assert.match(await h.text(`${view} .readyset-repl-bar`), /0 transactions/);
  await refreshTo(R["file-lag"]);
  assert.match(await h.text(`${view} .readyset-repl-bar`), /1,024 bytes/);
  await refreshTo(R.lab);

  // S8: markup in a table name stays text, never an element or a script.
  const evilTable = structuredClone(R.lab);
  evilTable.tables[0].table = "</script><img src=x onerror=window.__rsEvilTbl=1>";
  await refreshTo(evilTable);
  assert.equal(await page.$(`${view} img`), null, "table name server text adds no element");
  assert.equal(await page.evaluate(() => window.__rsEvilTbl || 0), 0, "table name server text runs no script");
  assert.match(await h.text(view), /<img src=x/, "table name server text renders as text");
  await refreshTo(R.lab);
  planAnswer = () => ({ status: 200, body: R["plan-resnapshot"] });
  await selectRow("tbl:`shop`.`orders`");
  await clickAction("Re-snapshot table…");
  await waitSheet();
  const resnapConfirm = `${sheet} .sheet-actions button:last-child`;
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), true, "typed guard disables Confirm initially");
  await page.type(`${sheet} .action-typed input`, "`shop`.");
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), true);
  await page.type(`${sheet} .action-typed input`, "`orders`");
  assert.equal(await page.$eval(resnapConfirm, (b) => b.disabled), false, "exact table name unlocks Confirm");
  await page.keyboard.press("Escape");
  await noSheet();

  // 7. Parameters tab: dynamic ALTER READYSET, allowlists, Maintenance mode, Admin 6036 gating.
  await backToList();
  await tab("Parameters");
  // On SQL port 3307, Shut down Readyset… is listed, aria-disabled, and its
  // reason is the button's aria-describedby text (the backend catalog).
  const shutdownBtn3307 = await page.$eval(
    `${view} .readyset-admin-lifecycle button`,
    (b) => ({
      ariaDisabled: b.getAttribute("aria-disabled"),
      reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    }),
  );
  assert.equal(shutdownBtn3307.ariaDisabled, "true");
  assert.match(shutdownBtn3307.reason, /6036/);

  // A disabled action sends nothing on click, Enter or Space.
  const plansBeforeKeys = plans.length;
  await page.click(`${view} .readyset-admin-lifecycle button`);
  await page.focus(`${view} .readyset-admin-lifecycle button`);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Space");
  await pause(200);
  assert.equal(plans.length, plansBeforeKeys, "a disabled action sends nothing on click, Enter or Space");

  // Maintenance mode on the lab fixture: Enter enabled, Exit disabled with its reason.
  const maintLab = await page.$$eval(`${view} [role="group"][aria-label="Maintenance mode"] button`, (els) =>
    els.map((b) => ({
      label: b.textContent.trim(),
      off: b.getAttribute("aria-disabled"),
      reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    })),
  );
  assert.deepEqual(
    maintLab.map((b) => [b.label, b.off]),
    [["Enter maintenance mode…", "false"], ["Exit maintenance mode…", "true"]],
    "lab fixture: Enter maintenance enabled, Exit disabled",
  );
  assert.match(maintLab[1].reason, /not in maintenance mode/);

  // Set log level… value prompt -> Review SQL… -> ActionReview with exact ALTER READYSET SQL.
  planAnswer = () => ({ status: 200, body: R["plan-log-level"] });
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} button`)].find((b) => b.textContent.trim() === "Set log level…").click(),
    view,
  );
  await page.waitForSelector("dialog.action-value[open]", { visible: true });
  await page.$eval("dialog.action-value[open] input", (i) => {
    i.select();
  });
  await page.keyboard.type("info,readyset_adapter=debug");
  await page.click("dialog.action-value[open] .sheet-actions button:last-child");
  await waitSheet();
  assert.equal(plans.at(-1).action, "set_log_level");
  assert.equal(
    await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent),
    "ALTER READYSET SET LOG LEVEL 'info,readyset_adapter=debug';",
  );
  await page.keyboard.press("Escape");
  await noSheet();

  // A prompt whose action becomes disabled before submit (the report was
  // swapped under it) shows the reason and sends nothing.
  await page.evaluate(
    (sel) => [...document.querySelectorAll(`${sel} button`)].find((b) => b.textContent.trim() === "Set log level…").click(),
    view,
  );
  await page.waitForSelector("dialog.action-value[open]", { visible: true });
  const noLog = structuredClone(R.lab);
  const noLogEntry = noLog.actions.find((a) => a.id === "set_log_level");
  noLogEntry.enabled = false;
  noLogEntry.why_not = "Readyset did not report its log level.";
  noLogEntry.fix = "Check the connection and refresh.";
  const plansBeforeSwap = plans.length;
  state.fallback = { status: 200, body: noLog };
  await page.evaluate(() => [...document.querySelectorAll("[data-action=refresh]")].find((b) => b.offsetParent)?.click());
  await page.waitForFunction(
    () => /did not report its log level/.test(document.querySelector("dialog.action-value[open]")?.textContent || ""),
  );
  assert.equal(
    await page.$eval("dialog.action-value[open] .sheet-actions button:last-child", (b) => b.disabled),
    true,
    "a blocked prompt cannot be confirmed",
  );
  await page.$eval("dialog.action-value[open] .sheet-actions button:last-child", (b) => b.click());
  await pause(200);
  assert.equal(plans.length, plansBeforeSwap, "a prompt whose action turned disabled sends nothing");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-value[open]"));
  await refreshTo(R.lab);
  await tab("Parameters");

  // Shallow allowlist Remove chip -> ActionReview.
  planAnswer = () => ({ status: 200, body: R["plan-drop-fn"] });
  await page.evaluate(
    (sel) =>
      [...document.querySelectorAll(`${sel} .readyset-allow-chip`)]
        .find((c) => c.textContent.includes("NOW"))
        .querySelector("button")
        .click(),
    view,
  );
  await waitSheet();
  assert.equal(plans.at(-1).action, "drop_shallow_allowed");
  assert.equal(
    await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent),
    "ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `NOW`;",
  );
  await page.keyboard.press("Escape");
  await noSheet();

  // F1: a report without allowlist_actions fails closed. The Remove chip is
  // off with the "not offered" reason and plans nothing on click, Enter, Space.
  const noAllow = structuredClone(R.lab);
  delete noAllow.allowlist_actions;
  await refreshTo(noAllow);
  const plansBeforeNoAllow = plans.length;
  const deadChip = await page.$eval(`${view} .readyset-allow-chip button`, (b) => ({
    ariaDisabled: b.getAttribute("aria-disabled"),
    title: b.title,
    reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
  }));
  assert.equal(deadChip.ariaDisabled, "true", "Remove fails closed when its catalog entry is missing");
  assert.match(deadChip.title, /did not offer/, "a missing catalog entry explains it was not offered");
  assert.match(deadChip.reason, /did not offer/, "a missing catalog entry's chip description names the visible not-offered hint");
  await page.click(`${view} .readyset-allow-chip button`);
  await page.focus(`${view} .readyset-allow-chip button`);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Space");
  await pause(200);
  assert.equal(plans.length, plansBeforeNoAllow, "a missing allowlist catalog entry plans nothing (click, Enter or Space)");
  await refreshTo(R.lab);

  // P2b.1: each allowlist kind renders its whole catalog through the shared
  // ActionGroup, so an empty list still shows the Remove… entry, aria-disabled,
  // with its why_not and fix as visible text (never hidden or title-only).
  const emptyAllow = structuredClone(R.lab);
  emptyAllow.shallow_allowlists.schemas = [];
  emptyAllow.allowlist_actions.schemas = emptyAllow.allowlist_actions.schemas.map((a) =>
    a.id === "drop_shallow_allowed"
      ? { ...a, enabled: false, why_not: "There are no allowed schemas to remove.", fix: "Add an allowed schema first." }
      : a,
  );
  await refreshTo(emptyAllow);
  const schemaRow = await page.$eval(`${view} .readyset-allowlist-row:last-child`, (row) => {
    const btn = [...row.querySelectorAll(".review-actions > button")].find((b) => b.textContent.trim() === "Remove…");
    return {
      labels: [...row.querySelectorAll(".review-actions > button")].map((b) => b.textContent.trim()),
      off: btn?.getAttribute("aria-disabled") || null,
      text: (row.textContent || "").replace(/\s+/g, " "),
      chips: row.querySelectorAll(".readyset-allow-chip").length,
    };
  });
  assert.equal(schemaRow.chips, 0, "no chips on an empty allowlist");
  assert.deepEqual(schemaRow.labels, ["Allow…", "Remove…"], "the kind's whole catalog is listed, always");
  assert.equal(schemaRow.off, "true", "Remove… stays listed, aria-disabled, on an empty allowlist");
  assert.match(schemaRow.text, /There are no allowed schemas to remove\./, "the disabled Remove shows its why_not as visible text");
  assert.match(schemaRow.text, /Add an allowed schema first\./, "the disabled Remove shows its fix as visible text");
  const plansBeforeEmptyAllow = plans.length;
  await page.evaluate((sel) => {
    const row = [...document.querySelectorAll(`${sel} .readyset-allowlist-row`)].find((r) =>
      /Allowed schemas/.test(r.textContent || ""),
    );
    [...(row?.querySelectorAll(".review-actions > button") || [])].find((b) => b.textContent.trim() === "Remove…")?.click();
  }, view);
  await pause(200);
  assert.equal(plans.length, plansBeforeEmptyAllow, "a disabled Remove… plans nothing");

  // Q2.1a: an enabled Remove… never plans a value-less drop (the chips carry
  // the item): clicking it moves focus to the first item chip and shows the
  // hint "Choose the item to remove.".
  const plansBeforeHint = plans.length;
  await page.evaluate((sel) => {
    const row = [...document.querySelectorAll(`${sel} .readyset-allowlist-row`)].find((r) =>
      /Allowed SQL functions/.test(r.textContent || ""),
    );
    [...(row?.querySelectorAll(".review-actions > button") || [])].find((b) => b.textContent.trim() === "Remove…")?.click();
  }, view);
  await pause(150);
  const hintState = await page.evaluate((sel) => {
    const row = [...document.querySelectorAll(`${sel} .readyset-allowlist-row`)].find((r) =>
      /Allowed SQL functions/.test(r.textContent || ""),
    );
    const firstChip = row?.querySelector(".readyset-allow-chip button") || null;
    return {
      hint: (row?.querySelector(".readyset-allowlist-hint")?.textContent || "").trim(),
      focused: !!firstChip && document.activeElement === firstChip,
    };
  }, view);
  assert.equal(hintState.hint, "Choose the item to remove.", "an enabled Remove… shows the chip hint");
  assert.equal(hintState.focused, true, "an enabled Remove… moves focus to the first item chip");
  assert.equal(plans.length, plansBeforeHint, "an enabled Remove… plans nothing on its own");

  // Q2.1b: with the drop entry disabled, each Remove chip's aria-describedby
  // names the kind's visible reason block, never only a title.
  const dropDisabled = structuredClone(R.lab);
  dropDisabled.allowlist_actions.functions = dropDisabled.allowlist_actions.functions.map((a) =>
    a.id === "drop_shallow_allowed"
      ? { ...a, enabled: false, why_not: "Cache DDL is blocked on this port.", fix: "Open the SQL port (3307) for cache DDL." }
      : a,
  );
  await refreshTo(dropDisabled);
  const chipWhy = await page.evaluate((sel) => {
    const row = [...document.querySelectorAll(`${sel} .readyset-allowlist-row`)].find((r) =>
      /Allowed SQL functions/.test(r.textContent || ""),
    );
    return [...(row?.querySelectorAll(".readyset-allow-chip button") || [])].map((b) => ({
      off: b.getAttribute("aria-disabled"),
      id: b.getAttribute("aria-describedby") || "",
      text: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    }));
  }, view);
  assert.ok(chipWhy.length > 0, "the functions allowlist renders chips");
  for (const c of chipWhy) {
    assert.equal(c.off, "true", "a disabled drop entry disables every Remove chip");
    assert.ok(c.id, "a disabled Remove chip has aria-describedby, not only a title");
    assert.match(c.text, /Cache DDL is blocked on this port\./, "the chip's description is the visible why_not");
    assert.match(c.text, /Open the SQL port \(3307\)/, "the chip's description carries the fix");
  }
  const plansBeforeChipOff = plans.length;
  await page.evaluate((sel) => {
    const row = [...document.querySelectorAll(`${sel} .readyset-allowlist-row`)].find((r) =>
      /Allowed SQL functions/.test(r.textContent || ""),
    );
    row?.querySelector(".readyset-allow-chip button")?.click();
  }, view);
  await pause(200);
  assert.equal(plans.length, plansBeforeChipOff, "a disabled Remove chip plans nothing");
  await refreshTo(R.lab);
  await tab("Parameters");

  await shot("readyset-parameters-1440.png");
  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await pause(200);
  await shot("readyset-parameters-1440-dark.png");
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await pause(150);

  // Maintenance mode & Admin 6036 port behavior.
  await refreshTo(R.maintenance);
  const maintRow = await page.$$eval(`${view} .grouped-item`, (els) =>
    els.filter((e) => /Maintenance mode/.test(e.textContent)).map((e) => e.textContent.replace(/\s+/g, " ").trim()),
  );
  assert.match(
    maintRow[0] || "",
    /Signals tools such as ProxySQL to stop sending queries; Readyset still serves what it receives/,
    "maintenance mode detail says what the mode does",
  );
  const maintFixture = await page.$$eval(`${view} [role="group"][aria-label="Maintenance mode"] button`, (els) =>
    els.map((b) => ({
      label: b.textContent.trim(),
      off: b.getAttribute("aria-disabled"),
      reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    })),
  );
  assert.deepEqual(
    maintFixture.map((b) => [b.label, b.off]),
    [["Enter maintenance mode…", "true"], ["Exit maintenance mode…", "false"]],
    "maintenance fixture: Exit maintenance enabled, Enter disabled",
  );
  assert.match(maintFixture[0].reason, /already in maintenance mode/);

  // Status Snapshot In Progress: Readyset cannot report Maintenance Mode until
  // the leader is ready, so the row reads "Not known" and Enter stays disabled.
  const snapMaint = structuredClone(R.lab);
  snapMaint.status_fields.status = "Snapshot In Progress";
  snapMaint.actions = snapMaint.actions.map((a) => {
    if (a.id === "enter_maintenance_mode") {
      return {
        ...a,
        enabled: false,
        why_not: "Readyset reports Maintenance Mode only when its Status is Online (now: Snapshot In Progress).",
        fix: "Wait until the snapshot finishes and Status is Online.",
      };
    }
    if (a.id === "exit_maintenance_mode") {
      return { ...a, enabled: false, why_not: "Readyset is not in maintenance mode.", fix: "Only a server in maintenance mode can exit it." };
    }
    return a;
  });
  await refreshTo(snapMaint);
  const maintSnapRow = await page.$$eval(`${view} .grouped-item`, (els) =>
    els.filter((e) => /Maintenance mode/.test(e.textContent)).map((e) => ({
      text: e.textContent.replace(/\s+/g, " ").trim(),
      pill: e.querySelector(".status-pill")?.className || "",
    })),
  );
  assert.match(maintSnapRow[0]?.text || "", /Not known until Status is Online/, "snapshot status: maintenance row is not known");
  assert.doesNotMatch(maintSnapRow[0]?.text || "", /Not in maintenance mode/, "snapshot status never reads as not in maintenance mode");
  assert.doesNotMatch(maintSnapRow[0]?.text || "", /Normal/, "a non-Online status is never Normal");
  assert.match(maintSnapRow[0]?.pill || "", /is-unknown/, "a non-Online status has the unknown tone");
  assert.doesNotMatch(maintSnapRow[0]?.pill || "", /is-ok/, "a non-Online status never has the OK tone");
  const maintSnap = await page.$$eval(`${view} [role="group"][aria-label="Maintenance mode"] button`, (els) =>
    els.map((b) => ({
      label: b.textContent.trim(),
      off: b.getAttribute("aria-disabled"),
      reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
    })),
  );
  assert.deepEqual(
    maintSnap.map((b) => [b.label, b.off]),
    [["Enter maintenance mode…", "true"], ["Exit maintenance mode…", "true"]],
    "snapshot status: both maintenance actions disabled",
  );
  assert.match(maintSnap[0].reason, /reports Maintenance Mode only when its Status is Online/);
  await refreshTo(R.maintenance);
  await chooseConnection(page, await connectionOption(page, "Readyset Admin"));
  await refreshTo(R.admin);
  assert.match((await visible(`${view} .summary-line`))[0], /admin SQL port/);
  assert.equal(
    await page.$eval(`${view} .readyset-admin-lifecycle button`, (b) => b.getAttribute("aria-disabled")),
    "false",
    "Admin SQL port 6036 enables Shut down Readyset…",
  );
  planAnswer = () => ({ status: 200, body: R["plan-shutdown"] });
  await page.click(`${view} .readyset-admin-lifecycle button`);
  await waitSheet();
  assert.equal(plans.at(-1).action, "shutdown");
  assert.equal(await page.$eval(`${sheet} .ops-pre code`, (e) => e.textContent), "ALTER READYSET SHUTDOWN;");
  await page.keyboard.press("Escape");
  await noSheet();
  // On Admin port 6036, Create cache… in Caches tab is listed, aria-disabled,
  // and its reason is the button's aria-describedby text.
  await tab("Caches");
  const createOnAdmin = await page.$eval(`${view} .readyset-strip-buttons button`, (b) => ({
    ariaDisabled: b.getAttribute("aria-disabled"),
    reason: (document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "").replace(/\s+/g, " ").trim(),
  }));
  assert.equal(createOnAdmin.ariaDisabled, "true");
  assert.match(createOnAdmin.reason, /3307/);

  // 8. Problem findings, other-connection greyed state, sidebar muting per connection, and 720x520.
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await refreshTo(R.problem);

  // Findings: the Cluster anatomy from report.findings, each title once.
  const findingTitles = await page.$$eval(`${view} .readyset-findings .cluster-finding-title`, (els) =>
    els.map((e) => (e.textContent || "").trim()),
  );
  assert.deepEqual(findingTitles, R.problem.findings.map((f) => f.title), "every finding title listed once");
  assert.equal(
    findingTitles.filter((t) => t === "Replication reported an error.").length,
    1,
    "replication_error is listed once",
  );
  assert.match(
    (await visible(`${view} .readyset-findings .cluster-mark`))[0] || "",
    /Problem/,
    "a problem finding carries the Problem mark",
  );
  // Its Details text stays hidden until toggled.
  assert.equal(
    await page.$eval(`${view} .readyset-findings .cluster-finding-text`, (e) => e.hidden),
    true,
    "finding text hidden until Details",
  );
  await page.click(`${view} .readyset-findings .cluster-finding-toggle`);
  const detailOpen = await page.$eval(`${view} .readyset-findings .cluster-finding-text`, (e) => ({
    hidden: e.hidden,
    text: (e.textContent || "").trim(),
  }));
  assert.equal(detailOpen.hidden, false, "Details shows the finding text");
  assert.match(detailOpen.text, /Binlog purged on upstream/);
  // The Base replication tile names the error, never a lag number.
  const probTiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      hint: e.querySelector(".tile-hint")?.textContent?.trim() || "",
    })),
  );
  const probRepl = probTiles.find((t) => t.label === "Base replication");
  assert.doesNotMatch(probRepl?.hint || "", /lag 0/, "an errored replication tile never claims lag 0");
  assert.match(probRepl?.hint || "", /Replication reported an error/);
  // Replication Status stays Running in the fixture (the engine emits only
  // Running, Stopped, Disabled); the problem tone comes from the reported
  // replicator error, never from a synthesized Error status.
  assert.equal(probRepl && R.problem.replication.status, "Running", "the fixture keeps a real Replication Status");
  assert.ok(
    await page.$$eval(`${view} .metric-tile`, (els) =>
      els.some((e) => e.querySelector(".tile-label")?.textContent?.trim() === "Base replication" && !!e.closest(".tile-item.is-problem")),
    ),
    "a reported replicator error gives the replication tile the problem tone",
  );
  // The CDC link breaks and reads Replication error although the status stays Running.
  await tab("Topology");
  assert.ok(await page.$(`${view} .readyset-topology .tier-link.is-error`), "a reported replicator error breaks the CDC link");
  assert.equal(await page.$(`${view} .readyset-topology .tier-link.is-running`), null, "an errored CDC link never runs");
  const probPlates = await page.$$eval(`${view} .readyset-topology .tier-link-plate`, (els) =>
    els.map((e) => (e.textContent || "").replace(/\s+/g, " ").trim()),
  );
  assert.ok(probPlates.includes("Replication error"), `an errored CDC link reads Replication error: ${JSON.stringify(probPlates)}`);
  assert.ok(
    !probPlates.some((p) => /CDC replication/.test(p)),
    `an errored CDC link never claims CDC replication: ${JSON.stringify(probPlates)}`,
  );

  // "Show in <Tab>" switches to the finding's tab.
  const tabbedProblem = structuredClone(R.problem);
  tabbedProblem.findings = tabbedProblem.findings.map((f) => ({ ...f, tab: "tables" }));
  await refreshTo(tabbedProblem);
  await page.evaluate(
    (sel) =>
      [...document.querySelectorAll(`${sel} .readyset-findings button`)]
        .find((b) => b.textContent.trim() === "Show in Tables")
        .click(),
    view,
  );
  await pause(250);
  assert.equal(
    await page.$eval(`${view} .segmented button[aria-pressed="true"]`, (b) => b.textContent.trim()),
    "Tables2",
    "Show in Tables switches to the Tables tab",
  );
  assert.ok(await page.$(`${view} .readyset-repl-bar`), "the Tables tab body renders");

  // S8: markup in a finding title or text stays text, never an element.
  const evilFinding = structuredClone(R.problem);
  evilFinding.findings = [
    {
      id: "replication_error",
      status: "problem",
      title: "</script><img src=x onerror=window.__rsEvilFinding=1>",
      text: "Details </script><img src=x onerror=window.__rsEvilFinding=1>",
      tab: "topology",
    },
  ];
  await refreshTo(evilFinding);
  await page.click(`${view} .readyset-findings .cluster-finding-toggle`);
  assert.equal(await page.$(`${view} .readyset-findings img`), null, "finding server text adds no element");
  assert.equal(await page.evaluate(() => window.__rsEvilFinding || 0), 0, "finding server text runs no script");
  assert.match(await h.text(`${view} .readyset-findings`), /<img src=x/, "finding server text renders as text");
  await refreshTo(R.lab);
  await tab("Caches");

  await chooseConnection(page, await connectionOption(page, "Plain MySQL"));
  await page.waitForFunction(
    (sel) => [...document.querySelectorAll(sel)].some((e) => e.offsetParent && e.hasAttribute("data-other-connection")),
    {},
    view,
  );
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector .review-actions button", (b) => b.getAttribute("aria-disabled")), "true");
  assert.match(await page.$eval(".view-inspector .review-actions", (e) => e.textContent), /read on Readyset lab/);
  const plansBeforeOther = plans.length;
  await clickAction("Flush entries…");
  await page.evaluate(() => {
    [...document.querySelectorAll(".view-inspector .review-actions button")]
      .find((b) => b.textContent.trim() === "Flush entries…")
      ?.focus();
  });
  await page.keyboard.press("Enter");
  await page.keyboard.press("Space");
  await pause(200);
  assert.equal(plans.length, plansBeforeOther, "nothing planned on another connection's report (click, Enter or Space)");

  await backToList();
  await refreshTo(R.not);
  assert.equal(await h.text(`${view} .view-empty h3`), "Not a Readyset instance");
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted), "true");
  await chooseConnection(page, await connectionOption(page, "Readyset lab"));
  await pause(250);
  assert.equal(await page.$eval(nav, (b) => b.dataset.muted || ""), "", "Readyset lab stays unmuted");
  await refreshTo(R.lab);

  // 9. Failed core reads (S4): SHOW CACHES and SHOW READYSET ALL TABLES
  // failed, so the sections read Not read, never zero or an empty state, and
  // the unread telemetry sources read Not read too.
  await refreshTo(R["read-fail"]);
  const failTiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      value: e.querySelector(".tile-value")?.textContent?.trim() || "",
    })),
  );
  assert.equal(failTiles.find((t) => t.label === "Active caches")?.value, "Not read", "failed cache reads read Not read, never 0");
  assert.equal(failTiles.find((t) => t.label === "Cached share")?.value, "Not read", "an unread cached source reads Not read, never 0%");
  assert.equal(failTiles.find((t) => t.label === "Shallow hit rate")?.value, "Not read", "unread shallow statistics read Not read");
  // The tab badges drop their counts rather than claim zero.
  const failTabs = await visible(`${view} .segmented button`);
  assert.ok(failTabs.includes("Caches"), `a failed cache read drops the tab count: ${JSON.stringify(failTabs)}`);
  assert.ok(failTabs.includes("Tables"), `a failed table read drops the tab count: ${JSON.stringify(failTabs)}`);
  assert.ok(!failTabs.some((t) => /^Caches0|^Tables0/.test(t)), "a failed read never shows a zero tab count");
  // CX-M13: the summary line and the Readyset topology card never claim zero
  // caches after a failed SHOW CACHES read.
  assert.match(
    (await visible(`${view} .summary-line`))[0],
    /caches not read/,
    "the summary line reads caches not read after a failed read",
  );
  await tab("Topology");
  assert.match(
    await h.text(`${view} .readyset-topology`),
    /Caches not read/,
    "the Readyset card reads caches not read after a failed read",
  );
  await tab("Caches");
  assert.equal(await h.text(`${view} .view-empty h3`), "Caches not read");
  assert.doesNotMatch(await h.text(view), /No active caches/, "a failed cache read never claims an empty result");
  await tab("Tables");
  assert.equal(await h.text(`${view} .view-empty h3`), "Tables not read");
  assert.doesNotMatch(await h.text(view), /No replicated base tables/, "a failed table read never claims an empty result");
  assert.doesNotMatch(await h.text(view), /0\/0 tables ready/, "a failed table read never claims zero of zero");
  await refreshTo(R.lab);
  await tab("Caches");

  // Zero samples are not unread statistics: reads that succeeded with zero
  // executions read "No traffic yet"; only an unread source reads "Not read".
  const zeroTraffic = structuredClone(R.lab);
  zeroTraffic.telemetry = {
    ...zeroTraffic.telemetry,
    cached_executions: 0,
    upstream_executions: 0,
    total_observed_executions: 0,
    cached_share_pct: null,
    shallow_hits: 0,
    shallow_misses: 0,
    shallow_hit_rate_pct: null,
    shallow_refreshes: 0,
  };
  await refreshTo(zeroTraffic);
  const zeroTiles = await page.$$eval(`${view} .metric-tile`, (els) =>
    els.map((e) => ({
      label: e.querySelector(".tile-label")?.textContent?.trim() || "",
      value: e.querySelector(".tile-value")?.textContent?.trim() || "",
      hint: e.querySelector(".tile-hint")?.textContent?.trim() || "",
    })),
  );
  assert.equal(
    zeroTiles.find((t) => t.label === "Cached share")?.value,
    "No traffic yet",
    "a read zero denominator is no traffic, not unread",
  );
  assert.equal(zeroTiles.find((t) => t.label === "Cached share")?.hint, "0 cached · 0 upstream");
  assert.equal(
    zeroTiles.find((t) => t.label === "Shallow hit rate")?.value,
    "No traffic yet",
    "zero hits and misses read no traffic, not unread",
  );

  // An unreported CDC role (active_cdc null) reads Not read in the topology
  // role label and in the inspector, never Query fallback or No.
  const cdcUnknown = structuredClone(R.lab);
  cdcUnknown.topology.upstreams = cdcUnknown.topology.upstreams.map((u) => ({ ...u, active_cdc: null }));
  cdcUnknown.upstreams = cdcUnknown.upstreams.map((u) => ({ ...u, active_cdc: null }));
  await refreshTo(cdcUnknown);
  await tab("Topology");
  const cdcTopoText = await h.text(`${view} .readyset-topology`);
  assert.doesNotMatch(
    cdcTopoText,
    /Query fallback|Active CDC primary|CDC candidate/,
    "an unreported CDC role never claims a topology role",
  );
  await page.evaluate(
    (sel, key) => document.querySelector(`${sel} .readyset-topology [data-row-key=${JSON.stringify(key)}]`)?.click(),
    view,
    "upstream:mysql-primary:3306",
  );
  await page.waitForSelector(".view-inspector .inspector-title");
  const cdcInspector = await h.text(".view-inspector");
  assert.match(cdcInspector, /Active CDC/);
  assert.match(cdcInspector, /Not read/, "an unreported CDC role reads Not read in the inspector");
  await backToList();
  await refreshTo(R.lab);
  await tab("Caches");

  // 720x520 responsive check: zero horizontal overflow and bottom-sheet inspector.
  await page.setViewport({ width: 720, height: 520 });
  await pause(350);
  const narrow = await page.evaluate(
    (sel) => ({
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      top: document.querySelector(`${sel} .data-table-wrap`)?.getBoundingClientRect().top,
      fold: document.querySelector(".window-statusbar").getBoundingClientRect().top,
    }),
    view,
  );
  assert.ok(narrow.overflow <= 0, `horizontal overflow at 720 px: ${narrow.overflow}`);
  assert.ok(narrow.top < narrow.fold, `table starts above fold at 720x520: ${JSON.stringify(narrow)}`);
  await selectRow("cache:c_recent_shallow");
  assert.equal(await page.$eval(".view-inspector", (e) => e.classList.contains("is-sheet")), true, "bottom-sheet inspector at 720 px");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.getAttribute("data-row-key") === "cache:c_recent_shallow");
  await shot("readyset-720.png");

  // Topology and Parameters at 720x520, light and dark.
  await tab("Topology");
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(200);
    assert.ok(
      (await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0,
      `no horizontal overflow on Topology at 720 px (${theme})`,
    );
    await shot(`readyset-topology-720-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await tab("Parameters");
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    await pause(200);
    assert.ok(
      (await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)) <= 0,
      `no horizontal overflow on Parameters at 720 px (${theme})`,
    );
    await shot(`readyset-parameters-720-${theme}.png`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await pause(150);

  assert.deepEqual(h.errors, []);
  console.log("readyset: ok");
} finally {
  await h.close();
}
