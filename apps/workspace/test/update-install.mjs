// In-place updates in the workspace: "Check for updates automatically"
// (on by default, off persists across a reload through the real server, the
// automatic check runs once when it is due and not while the last check is
// fresh), the update badge beside the sidebar logo (available, its layout
// at 1440 px and as a dot on the 720 px icon rail, the Settings menu item,
// the Software update dialog it opens, nothing for a stale result), and the
// desktop Update / Restart to update flow through a fake desktop bridge
// (download progress, ready, the dump warning before a restart, a prepare
// failure, an unsupported copy). Every check, stage and status is mocked or
// answered by the real Python server (MYFLAMES_TEST_UPDATE): no network.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, pause } from "./perf-harness.mjs";

process.env.MYFLAMES_TEST_UPDATE = "99.0.0";
let statusMock = null; // {status, body} or () => it, for /api/app/update/status
let checkMock = null;  // {status, body} for /api/app/update/check
let stageMock = null;  // {status, body} for /api/app/update/stage
let saveFails = false; // session saves refused: the workspace then holds unsaved changes
const h = await open("/api/unused-update-install-test", "update-install", {
  "/api/session/save": (body, request) => {
    if (saveFails) return { status: 500, body: { error: "Could not write the workspace store." } };
    request.continue();
    return null;
  },
  "/api/app/update/status": (body, request) => {
    const mock = typeof statusMock === "function" ? statusMock() : statusMock;
    if (mock) return mock;
    request.continue();
    return null;
  },
  "/api/app/update/check": (body, request) => {
    if (checkMock) return checkMock;
    request.continue();
    return null;
  },
  "/api/app/update/stage": (body, request) => {
    if (stageMock) return stageMock;
    request.continue();
    return null;
  },
  // The setting is persisted by the real server; recorded here for the waits.
  "/api/app/update/settings": (body, request) => {
    request.continue();
    return null;
  },
});
const { page, state } = h;
const shots = path.resolve(process.env.PERF_SHOTS || h.shots);
await fs.mkdir(shots, { recursive: true });
// Nothing the page does may reach outside this machine (GitHub included).
const external = [];
page.on("request", (r) => {
  const host = new URL(r.url()).hostname;
  if (host && host !== "127.0.0.1" && host !== "localhost") external.push(r.url());
});

const iso = (offsetMs = 0) => new Date(Date.now() - offsetMs).toISOString().replace(/\.\d+Z$/, "Z");
const record = (outcome, extra = {}) => ({ checked_at: iso(), outcome, current_version: "3.1.0",
  latest_version: outcome === "failed" ? null : outcome === "available" ? "3.2.0" : "3.1.0", reason: null, ...extra });
const info = (last, extra = {}) => ({ schema_version: "app-update-1.0", current_version: "3.1.0",
  latest_version: last.latest_version || "3.1.0", update_available: last.outcome === "available",
  release_title: "myflames 3.2.0", release_notes: "Notes", published_at: "", html_url: "https://github.com/vgrippa/myflames/releases",
  asset_name: null, asset_url: null, asset_size: null, checked_at: last.checked_at, last_check: last, ...extra });
const updateFields = { update_asset_name: "myflames-3.2.0-macos-arm64.zip",
  update_asset_url: "https://example.test/myflames-3.2.0-macos-arm64.zip", update_asset_size: 100,
  asset_checksum_url: "https://example.test/myflames-3.2.0.sha256",
  update_checksum_url: "https://example.test/myflames-3.2.0-update.sha256" };
const statusBody = (last, extra = {}) => ({ status: 200, body: { schema_version: "app-update-status-1.0",
  current_version: "3.1.0", last_check: last, auto_check: false, staging: null, staged: null, active_operations: [], ...extra } });
const staging = (stageState, received, total) => ({ schema_version: "app-update-stage-1.0",
  state: stageState, version: "3.2.0", received, total, reason: null });

const countPath = (p) => state.other.filter((r) => r.path === p).length;
async function waitFor(check, what, timeout = 10000) {
  const start = Date.now();
  while (!check()) {
    if (Date.now() - start > timeout) assert.fail(`timed out waiting for ${what}`);
    await pause(50);
  }
}
async function openSettings() {
  await page.click(".settings-button");
  await page.waitForSelector("#settings-popover .update-auto-toggle input:not([disabled])");
}
async function closeSettings() {
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#settings-popover"));
}
const autoChecked = () => page.$eval(".update-auto-toggle input", (e) => e.checked);
const badgePhase = (phase) => page.waitForFunction(
  (p) => document.querySelector(".sidebar .brand-group .update-badge")?.dataset.phase === p, { timeout: 10000 }, phase);
const footerText = (pattern) => page.waitForFunction(
  (p) => new RegExp(p).test(document.querySelector(".home-version .update-check")?.textContent || ""),
  { timeout: 10000 }, pattern.source);
const dialogButtons = () => page.$$eval("dialog.update-dialog .dialog-actions button", (els) => els.map((e) => e.textContent.trim()));
async function shootBadge(name) {
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
    await page.screenshot({ path: path.join(shots, `update-install-${name}-${theme}-${width}.png`) });
    await (await page.$(".sidebar-head")).screenshot({ path: path.join(shots, `update-install-brand-${name}-${theme}-${width}.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
}

try {
  // A1. "Check for updates automatically" is on by default; turning it off
  // is written to the real server and is still off after a reload.
  await openSettings();
  assert.equal(await autoChecked(), true, "automatic checks on by default");
  await page.click("#settings-popover .update-auto-toggle");
  await page.waitForFunction(() => document.querySelector(".update-auto-toggle input")?.checked === false);
  assert.ok(state.other.some((r) => r.path === "/api/app/update/settings" && r.body.auto_check === false),
    "the off choice was sent to the real server");
  await closeSettings();
  await page.reload({ waitUntil: "networkidle0" });
  await openSettings();
  assert.equal(await autoChecked(), false, "the off choice survives a reload");
  await page.click("#settings-popover .update-auto-toggle");
  await page.waitForFunction(() => document.querySelector(".update-auto-toggle input")?.checked === true);
  assert.ok(state.other.some((r) => r.path === "/api/app/update/settings" && r.body.auto_check === true),
    "the on choice was sent to the real server");
  await closeSettings();

  // A2. On, and never checked: exactly one automatic check after the
  // start-up tick (10 s after load).
  statusMock = statusBody(null, { auto_check: true });
  checkMock = { status: 200, body: info(record("up_to_date")) };
  const checksBeforeAuto = countPath("/api/app/update/check");
  const reloadedAt = Date.now();
  await page.reload({ waitUntil: "networkidle0" });
  await waitFor(() => countPath("/api/app/update/check") === checksBeforeAuto + 1, "the automatic check", 15000);
  await pause(Math.max(0, 15000 - (Date.now() - reloadedAt)));
  assert.equal(countPath("/api/app/update/check"), checksBeforeAuto + 1, "exactly one automatic check");

  // A3. On, but the last check is from today: the tick finds nothing due.
  statusMock = statusBody(record("up_to_date"), { auto_check: true });
  const checksBeforeFresh = countPath("/api/app/update/check");
  await page.reload({ waitUntil: "networkidle0" });
  await pause(12000);
  assert.equal(countPath("/api/app/update/check"), checksBeforeFresh, "no automatic check while the last one is fresh");

  // B1. An available result from today: the badge beside the logo.
  statusMock = statusBody(record("available"));
  checkMock = { status: 200, body: info(record("available"), updateFields) };
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  assert.equal(await page.$eval(".update-badge", (e) => e.getAttribute("aria-label")), "myflames 3.2.0 is available");

  // B2. At 1440 px the badge sits right of the name on the brand row; at
  // 720 px (the 48 px icon rail) it is a dot on the logo's top-right corner.
  await page.setViewport({ width: 1440, height: 900 });
  const wide = await page.evaluate(() => {
    const b = document.querySelector(".update-badge").getBoundingClientRect();
    const n = document.querySelector(".brand-name").getBoundingClientRect();
    const head = document.querySelector(".sidebar-head");
    return { badgeLeft: b.left, badgeMid: b.top + b.height / 2, nameRight: n.right, nameMid: n.top + n.height / 2,
      headOverflow: head.scrollWidth > head.clientWidth };
  });
  assert.equal(wide.headOverflow, false, "the sidebar head does not overflow at 1440");
  assert.ok(wide.badgeLeft >= wide.nameRight - 1, `badge right of the name: ${JSON.stringify(wide)}`);
  assert.ok(Math.abs(wide.badgeMid - wide.nameMid) < 6, `badge on the brand row: ${JSON.stringify(wide)}`);
  await page.setViewport({ width: 720, height: 520 });
  await pause(150);
  const rail = await page.evaluate(() => {
    const b = document.querySelector(".update-badge").getBoundingClientRect();
    const logo = document.querySelector(".sidebar .brand img").getBoundingClientRect();
    return { width: b.width, height: b.height, right: b.right, top: b.top, left: b.left, bottom: b.bottom,
      logoRight: logo.right, logoTop: logo.top, logoLeft: logo.left, logoBottom: logo.bottom,
      pageScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth };
  });
  assert.equal(rail.pageScroll, false, "no horizontal page scroll at 720");
  assert.ok(rail.width <= 14 && rail.height <= 14, `a dot no larger than 14 px: ${JSON.stringify(rail)}`);
  assert.ok(rail.right > rail.logoLeft && rail.left < rail.logoRight && rail.bottom > rail.logoTop && rail.top < rail.logoBottom,
    `the dot overlaps the logo: ${JSON.stringify(rail)}`);
  assert.ok(rail.right >= rail.logoRight - 8 && rail.top <= rail.logoTop + 8,
    `the dot sits on the logo's top-right corner: ${JSON.stringify(rail)}`);
  await shootBadge("badge-available");

  // B3. Settings lists the pending update; the badge opens the Software
  // update dialog, checking first (nothing fresh is known after a reload).
  await openSettings();
  assert.deepEqual(await page.$$eval("#settings-popover .update-menu-item", (els) => els.map((e) => e.textContent.trim())),
    ["Update available…"]);
  await closeSettings();
  const checksBeforeBadge = countPath("/api/app/update/check");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(countPath("/api/app/update/check"), checksBeforeBadge + 1, "a check runs before the dialog opens");
  assert.match(await page.$eval("dialog.update-dialog", (e) => e.textContent), /myflames 3\.2\.0 is available\./);
  await page.screenshot({ path: path.join(shots, "update-install-dialog-available-light-1440.png") });
  await page.locator("dialog.update-dialog button ::-p-text(Later)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));

  // B4. A result 25 hours old is a past result: no badge.
  statusMock = statusBody(record("available", { checked_at: iso(25 * 60 * 60 * 1000) }));
  await page.reload({ waitUntil: "networkidle0" });
  await footerText(/Last result: 3\.2\.0 available/);
  assert.equal(await page.$(".sidebar .brand-group .update-badge"), null, "no badge for a stale result");

  // C. The desktop flow, through a fake bridge that records its calls. The
  // answers live in localStorage so they survive the reloads.
  await page.evaluateOnNewDocument(() => {
    const read = (key, fallback) => {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch { return fallback; }
    };
    window.__bridgeCalls = [];
    window.myflamesDesktop = {
      pathForFile: (file) => file,
      update: {
        support: () => { window.__bridgeCalls.push("support"); return Promise.resolve(read("__bridgeSupport", { ok: true, supported: true, reason: null, ready: null })); },
        prepare: () => { window.__bridgeCalls.push("prepare"); return Promise.resolve(read("__bridgePrepare", { ok: true, version: "3.2.0" })); },
        restart: () => { window.__bridgeCalls.push("restart"); return Promise.resolve(read("__bridgeRestart", { ok: true })); },
      },
    };
  });

  // C1. Supported: Update stages the download (its progress on the badge
  // and in the dialog), prepare is called once, and the update is ready.
  let stagingState = null;
  let activeOps = [];
  statusMock = () => statusBody(record("available"), { staging: stagingState, active_operations: activeOps });
  stageMock = { status: 200, body: staging("running", 0, 100) };
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.waitForSelector("dialog.update-dialog .dialog-actions .button.primary:not([disabled])");
  assert.ok((await dialogButtons()).includes("Update"), "the dialog offers Update");
  const stagesBefore = countPath("/api/app/update/stage");
  await page.locator("dialog.update-dialog .dialog-actions .button.primary").click();
  await waitFor(() => countPath("/api/app/update/stage") === stagesBefore + 1, "the stage request");
  stagingState = staging("running", 40, 100);
  await badgePhase("downloading");
  await page.waitForFunction(() => document.querySelector(".update-badge")?.getAttribute("aria-label")?.includes("40%"),
    { timeout: 10000 });
  await page.waitForFunction(() => /Downloading: 40%/.test(
    document.querySelector("dialog.update-dialog .update-progress")?.textContent || ""), { timeout: 10000 });
  await page.screenshot({ path: path.join(shots, "update-install-dialog-downloading-light-1440.png") });
  stagingState = staging("done", 100, 100);
  await badgePhase("ready");
  assert.equal(await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "prepare").length), 1, "prepare called once");
  assert.equal(await page.$eval(".update-badge", (e) => e.getAttribute("aria-label")),
    "myflames 3.2.0 is ready: restart to update");
  assert.match(await page.$eval("dialog.update-dialog", (e) => e.textContent),
    /myflames 3\.2\.0 is ready\. Restart now, or it installs when you quit myflames\./);
  assert.ok((await dialogButtons()).includes("Restart to update"), "the dialog offers Restart to update");
  await footerText(/3\.2\.0 ready to install/);

  // C2. Restart while a dump runs: the warning first, then Restart anyway.
  activeOps = ["dump"];
  await page.locator("dialog.update-dialog button ::-p-text(Restart to update)").click();
  await page.waitForSelector("dialog.update-dialog p.form-error[role=alert]");
  assert.match(await page.$eval("dialog.update-dialog p.form-error[role=alert]", (e) => e.textContent),
    /A dump is running\. Restarting stops it before it finishes\./);
  assert.ok((await dialogButtons()).includes("Restart anyway"), "the dialog offers Restart anyway");
  assert.equal(await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "restart").length), 0,
    "the warning comes before any restart");
  await page.locator("dialog.update-dialog button ::-p-text(Restart anyway)").click();
  await page.waitForFunction(() => window.__bridgeCalls.filter((c) => c === "restart").length === 1, { timeout: 10000 });

  // C3. With nothing running, Restart to update calls restart at once.
  activeOps = [];
  await page.locator("dialog.update-dialog button ::-p-text(Later)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.locator("dialog.update-dialog button ::-p-text(Restart to update)").click();
  await page.waitForFunction(() => window.__bridgeCalls.filter((c) => c === "restart").length === 2, { timeout: 10000 });
  assert.equal(await page.$("dialog.update-dialog p.form-error"), null, "no warning when nothing is running");

  // C3b. Unsaved changes: the desktop app closes the window to restart, so
  // the same rule as the page's beforeunload guard asks first.
  await page.locator("dialog.update-dialog button ::-p-text(Later)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  saveFails = true;
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForFunction(() => /Not saved/.test(document.querySelector(".session-status")?.textContent || ""), { timeout: 15000 });
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.locator("dialog.update-dialog button ::-p-text(Restart to update)").click();
  await page.waitForFunction(() => /Some changes are not saved yet\. Restarting now loses them\./
    .test(document.querySelector("dialog.update-dialog")?.textContent || ""), { timeout: 10000 });
  assert.equal(await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "restart").length), 2,
    "no restart before the person confirms losing unsaved changes");
  await page.locator("dialog.update-dialog button ::-p-text(Restart anyway)").click();
  await page.waitForFunction(() => window.__bridgeCalls.filter((c) => c === "restart").length === 3, { timeout: 10000 });
  // The changes stay unsaved, so the page's own beforeunload guard now asks
  // before each reload that follows: leave, as a person who chose to would.
  saveFails = false;
  page.on("dialog", (dialog) => { if (dialog.type() === "beforeunload") void dialog.accept(); });

  // C4. prepare refuses: its reason, Try again and Download instead, and
  // the badge is available again.
  await page.evaluate(() => localStorage.setItem("__bridgePrepare", JSON.stringify({ ok: false,
    reason: "The update is not signed by the developer of this copy of myflames, so it was not installed." })));
  stagingState = null;
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.waitForSelector("dialog.update-dialog .dialog-actions .button.primary:not([disabled])");
  const stagesBeforeRetry = countPath("/api/app/update/stage");
  await page.locator("dialog.update-dialog .dialog-actions .button.primary").click();
  await waitFor(() => countPath("/api/app/update/stage") === stagesBeforeRetry + 1, "the stage request");
  stagingState = staging("done", 100, 100);
  await page.waitForSelector("dialog.update-dialog p.form-error[role=alert]", { timeout: 15000 });
  assert.match(await page.$eval("dialog.update-dialog p.form-error[role=alert]", (e) => e.textContent),
    /The update is not signed by the developer of this copy of myflames, so it was not installed\./);
  const retryButtons = await dialogButtons();
  assert.ok(retryButtons.includes("Try again") && retryButtons.includes("Download instead"),
    `Try again and Download instead: ${retryButtons}`);
  await badgePhase("available");

  // C5. An unsupported copy: the reason and Download update, no Update.
  await page.evaluate(() => {
    localStorage.setItem("__bridgeSupport", JSON.stringify({ ok: true, supported: false,
      reason: "Move myflames to the Applications folder to update it in place.", ready: null }));
    localStorage.removeItem("__bridgePrepare");
  });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.waitForSelector("dialog.update-dialog p.update-in-place-hint");
  assert.equal(await page.$eval("dialog.update-dialog p.update-in-place-hint", (e) => e.textContent),
    "Move myflames to the Applications folder to update it in place. Download update saves the installer in your Downloads folder instead.");
  const unsupportedButtons = await dialogButtons();
  assert.ok(unsupportedButtons.includes("Download update"), `Download update offered: ${unsupportedButtons}`);
  assert.ok(!unsupportedButtons.includes("Update"), `no Update button: ${unsupportedButtons}`);
  await page.screenshot({ path: path.join(shots, "update-install-dialog-unsupported-light-1440.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));

  // R1 (review C1). A refresh that fails when the dialog opens: the dialog
  // says why and offers Check again, never "up to date".
  await page.evaluate(() => { localStorage.removeItem("__bridgeSupport"); localStorage.removeItem("__bridgePrepare"); });
  statusMock = statusBody(record("available"));
  checkMock = { status: 200, body: info(record("failed", { reason: "GitHub could not be reached" }),
    { error: "Could not check for updates: GitHub could not be reached.", reason: "GitHub could not be reached" }) };
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  const failedText = await page.$eval("dialog.update-dialog", (e) => e.textContent);
  assert.match(failedText, /Could not check for updates: GitHub could not be reached\./);
  assert.doesNotMatch(failedText, /up to date/i, "a failed refresh is never shown as up to date");
  assert.ok((await dialogButtons()).includes("Check again"), "Check again is offered");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));

  // R2 (review C2). Restart when the running operations cannot be read: ask first.
  checkMock = { status: 200, body: info(record("available"), updateFields) };
  await page.evaluate(() => localStorage.setItem("__bridgeSupport",
    JSON.stringify({ ok: true, supported: true, reason: null, ready: "3.2.0" })));
  statusMock = statusBody(record("available"));
  await page.reload({ waitUntil: "networkidle0" });
  await badgePhase("ready");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  const restartsBefore = await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "restart").length);
  statusMock = { status: 500, body: { error: "Could not read the workspace." } };
  await page.locator("dialog.update-dialog button ::-p-text(Restart to update)").click();
  await page.waitForFunction(() => /could not check whether a dump, restore or clone is running/
    .test(document.querySelector("dialog.update-dialog")?.textContent || ""), { timeout: 10000 });
  assert.equal(await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "restart").length), restartsBefore,
    "no restart while the running operations are unknown");
  await page.locator("dialog.update-dialog button ::-p-text(Restart anyway)").click();
  await page.waitForFunction((n) => window.__bridgeCalls.filter((c) => c === "restart").length === n + 1, { timeout: 10000 }, restartsBefore);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));

  // R3 (review C4). The stage request's answer is lost but the download
  // started: the page adopts it, and the update still becomes ready.
  await page.evaluate(() => localStorage.removeItem("__bridgeSupport"));
  let lostStaging = null;
  statusMock = () => statusBody(record("available"), { staging: lostStaging });
  stageMock = { status: 502, body: { error: "The connection was reset." } };
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.waitForSelector("dialog.update-dialog .dialog-actions .button.primary:not([disabled])");
  lostStaging = staging("running", 10, 100);
  const preparesBefore = await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "prepare").length);
  await page.locator("dialog.update-dialog .dialog-actions .button.primary").click();
  await badgePhase("downloading");
  lostStaging = staging("done", 100, 100);
  await badgePhase("ready");
  assert.equal(await page.evaluate(() => window.__bridgeCalls.filter((c) => c === "prepare").length), preparesBefore + 1,
    "prepare ran for the adopted download");
  stageMock = null;

  assert.deepEqual(external, [], "no request ever leaves this machine (GitHub included)");
  assert.deepEqual(h.errors, []);
  console.log(`Update install passed (${countPath("/api/app/update/check")} checks, all mocked or local). Screenshots: ${shots}`);
} finally { await h.close(); }
