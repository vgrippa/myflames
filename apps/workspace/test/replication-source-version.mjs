// Replication > Topology: each source shows its engine and version like the
// replica does, with where the version came from in its tooltip, in the
// connector's details and in the inspector; "Version unknown" carries the
// reason and the hint. Mocked /api/live/replication with replication-1.0
// reports the backend computed from live recordings (2026-10-20; written by
// ops-fixtures.py): MySQL 9.7.2 from the relay log, from the saved connection
// "Source 9.7" when the relay log gives none, unknown without REPLICATION
// SLAVE, and MariaDB 11.8.9 from the relay log and from the saved connection
// "Source 11.8" (its server ID alone compared, which the text says). Light
// and dark, 1440 and 390 px.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const relay = await load("replication-mysql-9.7-srcver-relay.json");
const profile = await load("replication-mysql-9.7-srcver-profile.json");
const denied = await load("replication-mysql-9.7-srcver-denied.json");
const maria = await load("replication-mariadb-11.8-srcver-relay.json");
const mariaProfile = await load("replication-mariadb-11.8-srcver-profile.json");
const view = '.server-view[data-view="replication"]';
const topology = ".replication-topology-container";

const h = await open("/api/live/replication", "replication-source-version", {
  "/api/live/replication/skip-loop/status": () => ({ status: 200, body: { schema_version: "replication-skip-loop-1.0", loops: [] } }),
}, { fallback: { status: 200, body: relay } });
const { page, state, errors } = h;

/** The visible topology's source node and connector details. */
const shown = () => page.evaluate((q) => {
  const root = [...document.querySelectorAll(q)].find((e) => e.offsetParent);
  if (!root) return null;
  const version = root.querySelector(".topology-source .topology-version");
  const detail = root.querySelector(".topology-link-wrap [role=tooltip]");
  const row = [...(detail?.querySelectorAll(".topology-detail-row") || [])].find((r) => r.firstElementChild?.textContent === "Source version");
  const box = version?.getBoundingClientRect();
  return {
    source: root.querySelector(".topology-source").textContent,
    replica: root.querySelector(".topology-replica").textContent,
    version: version?.textContent, title: version?.getAttribute("title"), unknown: version?.classList.contains("is-unknown"),
    visible: !!box && box.width > 0 && box.height > 0 && box.right <= window.innerWidth,
    detail: row?.lastElementChild?.textContent,
    overflow: document.documentElement.scrollWidth > window.innerWidth,
  };
}, topology);

async function showReport(report, text) {
  state.queue.push({ status: 200, body: report });
  await h.refresh();
  await page.waitForFunction((q, t) => [...document.querySelectorAll(q)].some((e) => e.offsetParent && e.querySelector(".topology-source")?.textContent.includes(t)), {}, topology, text);
}

try {
  await h.view("replication");
  await page.waitForFunction((q) => [...document.querySelectorAll(q)].some((e) => e.offsetParent && e.querySelector(".topology-source")), {}, topology);

  // From the relay log: the engine and version beside the port, the provenance on hover.
  let s = await shown();
  assert.equal(s.version, "MySQL 9.7.2");
  assert.match(s.source, /Port 33297 · MySQL 9\.7\.2/);
  assert.match(s.source, /Server ID 301/);
  assert.match(s.replica, /MySQL 9\.7\.2/);
  assert.match(s.title, /^MySQL 9\.7\.2, as recorded in the relay log\. This is the version of the source server/);
  assert.equal(s.unknown, false);
  assert.ok(s.visible, "the version is drawn");
  assert.equal(s.detail, "MySQL 9.7.2, as recorded in the relay log", "keyboard users get it in the connector's details");
  // The connector's details open on keyboard focus with the source version.
  await page.evaluate((q) => [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-link").focus(), topology);
  await page.waitForFunction((q) => [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-detail")?.textContent.includes("Source version"), {}, topology);
  // Every row of the details is drawn, not clipped by the map's scroll box.
  const hidden = await page.evaluate((q) => {
    const d = [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-detail");
    return [...d.querySelectorAll(".topology-detail-row")].filter((r) => {
      const b = r.getBoundingClientRect();
      const hit = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
      return !hit || !d.contains(hit);
    }).map((r) => r.firstElementChild?.textContent);
  }, topology);
  assert.deepEqual(hidden, [], "the connector's details are not clipped");
  await page.screenshot({ path: path.join(h.shots, "replication-source-version-relay.png") });
  await page.keyboard.press("Escape");

  // The inspector: a Source version row and the provenance with its caveat.
  await page.evaluate((q) => [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-link").click(), topology);
  await page.waitForSelector(".view-inspector .inspector-title");
  const inspector = await h.inspector();
  assert.match(inspector, /Source versionMySQL 9\.7\.2As recorded in the relay log\. This is the version of the source server that wrote the binary log/);
  await page.screenshot({ path: path.join(h.shots, "replication-source-version-inspector.png") });
  // Full-width details cover the map: go back to it.
  const back = await page.$(".view-inspector.is-detail .detail-back");
  if (back) {
    await back.click();
    await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  }

  // From a saved connection: named in the tooltip.
  await showReport(profile, "MySQL 9.7.2");
  await page.waitForFunction((q) => /saved connection/.test([...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-version")?.title || ""), {}, topology);
  s = await shown();
  assert.equal(s.version, "MySQL 9.7.2");
  assert.equal(s.title, "MySQL 9.7.2, from the saved connection Source 9.7.");
  assert.equal(s.detail, "MySQL 9.7.2, from the saved connection Source 9.7");

  // Unknown: the reason and the hint, never a guessed engine.
  await showReport(denied, "Version unknown");
  s = await shown();
  assert.equal(s.version, "Version unknown");
  assert.equal(s.unknown, true);
  assert.equal(s.title, "Reading the relay log (SHOW RELAYLOG EVENTS) needs the REPLICATION SLAVE privilege. Add a saved connection for 127.0.0.1:33297 to show its version.");
  assert.equal(s.detail, s.title);
  assert.doesNotMatch(s.source, /MySQL|MariaDB/);
  await page.screenshot({ path: path.join(h.shots, "replication-source-version-unknown.png") });

  // MariaDB: the engine name and the short version; the full string in the tooltip.
  await showReport(maria, "MariaDB 11.8.9");
  s = await shown();
  assert.equal(s.version, "MariaDB 11.8.9");
  assert.match(s.title, /^MariaDB 11\.8\.9 \(11\.8\.9-MariaDB-ubu2404-log\), as recorded in the relay log\./);

  // A MariaDB saved connection is confirmed by its server ID alone, and every place says so.
  await showReport(mariaProfile, "MariaDB 11.8.9");
  await page.waitForFunction((q) => /saved connection/.test([...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-version")?.title || ""), {}, topology);
  s = await shown();
  assert.equal(s.version, "MariaDB 11.8.9");
  assert.equal(s.title, "MariaDB 11.8.9 (11.8.9-MariaDB-ubu2404-log), from the saved connection Source 11.8 (only its server ID could be compared).");
  assert.equal(s.detail, "MariaDB 11.8.9, from the saved connection Source 11.8 (only its server ID could be compared)");
  await page.evaluate((q) => [...document.querySelectorAll(q)].find((e) => e.offsetParent).querySelector(".topology-link").click(), topology);
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.match(await h.inspector(), /Source versionMariaDB 11\.8\.9From the saved connection Source 11\.8 \(only its server ID could be compared\)\./);
  const backFromMaria = await page.$(".view-inspector.is-detail .detail-back");
  if (backFromMaria) {
    await backFromMaria.click();
    await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  }

  // Dark appearance and a phone width: still drawn, no page overflow.
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  await showReport(relay, "MySQL 9.7.2");
  await page.screenshot({ path: path.join(h.shots, "replication-source-version-dark.png") });
  await page.setViewport({ width: 390, height: 844 });
  await pause(300);
  s = await shown();
  assert.ok(s.visible, "the version stays visible at 390 px");
  assert.equal(s.overflow, false, "no sideways page scroll at 390 px");
  await page.screenshot({ path: path.join(h.shots, "replication-source-version-390.png"), fullPage: false });
  assert.deepEqual(errors, []);
  console.log("replication source version: ok");
} finally {
  await h.close();
}
