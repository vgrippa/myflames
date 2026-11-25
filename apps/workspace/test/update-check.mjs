// Check for updates in Settings > Updates and news and About myflames: the
// installed version beside the sidebar brand (its control opens the section;
// on the 720 px icon rail the text hides and the brand tooltip names the
// version), no request to GitHub until the button is clicked, progress, up
// to date / newer release / failure, the update badge beside the logo per
// phase, Download opening the update dialog, the last check kept in the
// workspace store across a reload, and a result older than 24 hours shown as
// a past result. The real Python server answers the stored status and one
// check (MYFLAMES_TEST_UPDATE, no network); other checks are mocked.
// "Check for updates automatically" is turned off first, so the automatic
// check's start-up tick never races the steps below.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, pause } from "./perf-harness.mjs";

process.env.MYFLAMES_TEST_UPDATE = "99.0.0";
const checks = [];
let statusMock = null;
const h = await open("/api/unused-update-test", "update-check", {
  "/api/app/update/check": (body, request) => {
    const next = checks.shift();
    if (next === "real" || !next) { request.continue(); return null; }
    return next;
  },
  "/api/app/update/status": (body, request) => {
    if (statusMock) return statusMock;
    request.continue();
    return null;
  },
  "/api/app/update/settings": (body, request) => {
    request.continue();
    return null;
  },
});
const { page, state } = h;
const shots = path.resolve(process.env.PERF_SHOTS || h.shots);
await fs.mkdir(shots, { recursive: true });
const updates = ".settings-page .settings-update-status";
// The visible line: status and time, joined by the gap the row's CSS draws.
const status = (scope = updates) => page.$eval(`${scope} .update-check`, (e) => [...e.children]
  .map((c) => c.textContent.replace(/\s+/g, " ").trim()).join(" · "));
const checkCalls = () => state.other.filter((r) => r.path === "/api/app/update/check").length;
const iso = (offsetMs = 0) => new Date(Date.now() - offsetMs).toISOString().replace(/\.\d+Z$/, "Z");
const record = (outcome, extra = {}) => ({ checked_at: iso(), outcome, current_version: "3.1.0",
  latest_version: outcome === "failed" ? null : outcome === "available" ? "3.2.0" : "3.1.0", reason: null, ...extra });
const info = (last, extra = {}) => ({ schema_version: "app-update-1.0", current_version: "3.1.0",
  latest_version: last.latest_version || "3.1.0", update_available: last.outcome === "available",
  release_title: "myflames 3.2.0", release_notes: "## Release highlights\n\n- **Group connections** open a group as one connection, with `sql_mode` inline code.\n- A second bullet with <img src=x onerror=alert(1)> inside.\n\nA closing paragraph.", published_at: "", html_url: "https://github.com/vgrippa/myflames/releases",
  asset_name: null, asset_url: null, asset_size: null, checked_at: last.checked_at, last_check: last, ...extra });
async function waitStatus(pattern, scope = updates) {
  await page.waitForFunction((s, p) => new RegExp(p).test([...(document.querySelector(`${s} .update-check`)?.children || [])]
    .map((c) => c.textContent.replace(/\s+/g, " ").trim()).join(" · ")),
    { timeout: 10000 }, scope, pattern.source);
}
// The brand's version control opens Settings at Updates and news.
async function openUpdates() {
  await page.click(".brand-version");
  await page.waitForSelector(".settings-page");
  assert.equal(await page.$eval(".settings-panel-title", (e) => e.textContent), "Updates and news",
    "the version control opens Updates and news");
  await page.waitForSelector(updates);
}
async function closeSettings() {
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));
}
async function shoot(name) {
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
    const row = await page.$eval(updates, (e) => {
      const r = e.getBoundingClientRect();
      return { fits: e.scrollWidth <= e.clientWidth, left: r.left, right: r.right };
    });
    assert.ok(row.fits && row.left >= 0 && row.right <= width,
      `${name}: the Settings update row fits at ${width}: ${JSON.stringify(row)}`);
    // On the 720 px icon rail the version text is not drawn; the brand's
    // tooltip still names the version.
    assert.equal(await page.evaluate(() => (document.querySelector(".brand-version")?.getClientRects().length || 0) > 0),
      width > 900, `${name}: the brand version shows at 1440 and hides on the rail`);
    await page.screenshot({ path: path.join(shots, `update-${name}-${theme}-${width}.png`) });
    await (await page.$(updates)).screenshot({ path: path.join(shots, `update-settings-${name}-${theme}-${width}.png`) });
    await (await page.$(".sidebar-head")).screenshot({ path: path.join(shots, `update-brand-${name}-${theme}-${width}.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
}

try {
  // 0. The brand names the installed version, and its control opens Settings
  // > Updates and news. Then turn "Check for updates automatically" off so
  // the automatic check's start-up tick (10 s after load) never races the
  // steps below.
  const metaVersion = await page.$eval('meta[name="myflames-version"]', (e) => e.content);
  assert.match(metaVersion, /^\d+\.\d+\.\d+$/, "the server fills in the package version");
  assert.equal(await page.$eval(".brand", (e) => e.title), `myflames ${metaVersion}`, "the brand tooltip names the version");
  assert.equal(await page.$eval(".brand", (e) => e.getAttribute("aria-label")), "myflames Home", "the brand still goes Home");
  assert.equal(await page.$eval(".brand-version", (e) => e.textContent), metaVersion, "the version beside the brand");
  assert.equal(await page.$eval(".brand-version", (e) => e.getAttribute("aria-label")),
    `myflames ${metaVersion}: Updates and news settings`);
  assert.equal(await page.$eval(".brand-version", (e) => e.title),
    `myflames ${metaVersion}: Updates and news settings`, "the version tooltip retains its full text");
  await openUpdates();
  await page.waitForSelector(".settings-page .update-auto-toggle input:not([disabled])");
  await page.click(".settings-page .update-auto-toggle");
  await page.waitForFunction(() => document.querySelector(".update-auto-toggle input")?.checked === false);
  assert.ok(state.other.some((r) => r.path === "/api/app/update/settings" && r.body.auto_check === false),
    "the automatic check setting was turned off");
  await closeSettings();

  // 1. Before the first check: the stored status is read, GitHub is not.
  await openUpdates();
  await waitStatus(/Not checked yet/);
  assert.equal(checkCalls(), 0, "no update check without a click");
  assert.ok(state.other.some((r) => r.path === "/api/app/update/status"), "the stored status was read");
  assert.match(await page.$eval(updates, (e) => e.textContent), new RegExp(`myflames ${metaVersion.replace(/\./g, "\\.")}`),
    "the section names the installed version");
  assert.equal(await page.$(`${updates} .update-check-download`), null);
  assert.equal(await page.$eval(`${updates} a`, (e) => e.href), "https://github.com/vgrippa/myflames/releases");
  await shoot("never");
  await closeSettings();

  // 2. Up to date, with progress while the check runs; focus stays on the
  // button, and no badge appears beside the brand.
  await openUpdates();
  checks.push({ status: 200, body: info(record("up_to_date")), delay: 500 });
  await page.click(`${updates} .update-check-button`);
  await waitStatus(/Checking for updates…/);
  assert.equal(await page.$eval(`${updates} .update-check-button`, (e) => e.getAttribute("aria-disabled")), "true");
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("update-check-button")), true);
  await page.click(`${updates} .update-check-button`); // ignored while checking
  await waitStatus(/^Up to date/);
  assert.equal(checkCalls(), 1, "a click while checking does not start a second check");
  assert.match(await status(), /^Up to date · Checked today at \d{1,2}:\d{2}/);
  const title = await page.$eval(`${updates} .update-check-time`, (e) => e.title);
  assert.match(title, new RegExp(String(new Date().getFullYear())), "the tooltip has the full timestamp");
  assert.equal(await page.$eval(`${updates} .update-check-button`, (e) => e.getAttribute("aria-disabled")), null);
  assert.equal(await page.$(".sidebar .update-badge"), null, "nothing beside the brand when up to date");
  await shoot("up-to-date");
  await closeSettings();

  // 3. A failed check says why, never "up to date".
  await openUpdates();
  checks.push({ status: 200, body: info(record("failed", { reason: "GitHub could not be reached" }),
    { error: "Could not check for updates: GitHub could not be reached.",
      reason: "GitHub could not be reached" }) });
  await page.click(`${updates} .update-check-button`);
  await waitStatus(/^Could not check/);
  assert.match(await status(), /^Could not check: GitHub could not be reached · Tried today at/);
  assert.equal(await page.$(`${updates} .update-check-download`), null);
  assert.equal(await page.$(".sidebar .update-badge"), null, "no badge for a failed check");
  await shoot("failed");
  await closeSettings();

  // 4. A newer release: the badge beside the brand names the update, and
  // Download opens the update dialog with the versions.
  await openUpdates();
  checks.push({ status: 200, body: info(record("available")) });
  await page.click(`${updates} .update-check-button`);
  await waitStatus(/^3\.2\.0 available/);
  assert.match(await status(), /^3\.2\.0 available · Checked today at/);
  await page.waitForSelector(".sidebar .brand-group button.update-badge[data-phase=available]");
  assert.equal(await page.$eval(".update-badge", (e) => e.getAttribute("aria-label")), "Update available: myflames 3.2.0");
  assert.equal(await page.$eval(".update-badge", (e) => e.title), "Update available: myflames 3.2.0");
  await shoot("available");
  // On the 720 px rail the version text hides but the badge stays on the logo.
  await page.setViewport({ width: 720, height: 520 });
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  assert.ok(await page.evaluate(() => (document.querySelector(".sidebar .update-badge")?.getClientRects().length || 0) > 0),
    "the badge stays on the logo at 720 px");
  await page.setViewport({ width: 1440, height: 900 });
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector("dialog.update-dialog[open]");
  const dialogText = await page.$eval("dialog.update-dialog", (e) => e.textContent);
  assert.match(dialogText, /Software update/);
  assert.match(dialogText, /myflames 3\.2\.0 is available\./);
  assert.match(dialogText, /Download update/);
  // The release notes render as text (headings, list, bold, code): no raw
  // Markdown marks, and HTML in the notes stays text, never an element.
  const notes = await page.$eval("dialog.update-dialog .update-notes-content", (e) => ({
    h3: e.querySelectorAll("h3").length, li: e.querySelectorAll("li").length,
    strong: e.querySelectorAll("strong").length, code: e.querySelectorAll("code").length,
    img: e.querySelectorAll("img").length, text: e.textContent,
  }));
  assert.ok(notes.h3 >= 1, "a rendered heading");
  assert.ok(notes.li >= 2, "rendered list items");
  assert.ok(notes.strong >= 1 && notes.code >= 1, "rendered bold and inline code");
  assert.equal(notes.img, 0, "HTML in the notes is text, never an element");
  assert.ok(!notes.text.includes("**") && !notes.text.includes("## "), "no raw Markdown marks");
  await page.screenshot({ path: path.join(shots, "update-dialog-light-1440.png") });
  await page.locator("dialog.update-dialog button ::-p-text(Later)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("update-check-download")), true,
    "focus returns to Download");
  // While a new check runs, Download is unavailable; a check that then fails opens nothing.
  checks.push({ status: 200, delay: 600, body: info(record("failed", { reason: "GitHub could not be reached" }),
    { error: "Could not check for updates: GitHub could not be reached.", reason: "GitHub could not be reached" }) });
  await page.click(`${updates} .update-check-button`);
  await waitStatus(/Checking for updates…/);
  assert.equal(await page.$eval(`${updates} .update-check-download`, (e) => e.getAttribute("aria-disabled")), "true");
  await page.click(`${updates} .update-check-download`);
  await waitStatus(/^Could not check/);
  await pause(100);
  assert.equal(await page.$("dialog.update-dialog"), null, "no dialog from a click during a check");
  await closeSettings();

  // 5. The real server answers one check and keeps it in the workspace store.
  checks.push("real");
  await openUpdates();
  await page.click(`${updates} .update-check-button`);
  await waitStatus(/^99\.0\.0 available/);
  const callsBeforeReload = checkCalls();
  await page.reload({ waitUntil: "networkidle0" });
  await openUpdates();
  await waitStatus(/^99\.0\.0 available · Checked today at/);
  assert.equal(checkCalls(), callsBeforeReload, "reload reads the stored check, never GitHub");

  // 6. Download after a reload asks again (a click), then opens the dialog.
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(checkCalls(), callsBeforeReload + 1);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await closeSettings();

  // 7. One shared opener. While the dialog's chunk is delayed, the launching
  // control and the Settings gear stay busy until the dialog is open; a chunk
  // that cannot be read keeps the dialog closed and leaves a dismissible error
  // with Retry at the launching control, and Retry with the request allowed
  // opens the dialog. Every Retry starts a fresh load (three failures, then
  // the third Retry opens it), Dismiss returns focus to the launcher, a
  // failed Retry keeps focus on Retry, and the badge's busy state also shows
  // on the 720 px icon rail.
  await page.reload({ waitUntil: "networkidle0" });
  state.slowAssets = /UpdateDialog-/;
  state.slowAssetsMs = 3000;
  await openUpdates();
  await waitStatus(/^99\.0\.0 available/);
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector(`${updates} .update-check-download[aria-busy="true"]`);
  await page.waitForSelector(".settings-button[aria-busy=\"true\"]");
  assert.equal(await page.$("dialog.update-dialog"), null, "no dialog before its chunk arrives");
  state.slowAssets = null;
  state.slowAssetsMs = 1500;
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(await page.$eval(`${updates} .update-check-download`, (e) => e.getAttribute("aria-busy")), null,
    "the busy state ends when the dialog is open");
  assert.equal(await page.$eval(".settings-button", (e) => e.getAttribute("aria-busy")), null,
    "the gear's busy state ends when the dialog is open");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await closeSettings();
  await page.reload({ waitUntil: "networkidle0" });
  state.failAssets = /UpdateDialog-/;
  await openUpdates();
  await waitStatus(/^99\.0\.0 available/);
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector(`${updates} .update-open-error[role="alert"]`, { timeout: 15000 });
  assert.match(await page.$eval(`${updates} .update-open-error`, (e) => e.textContent),
    /Software update window could not be loaded/);
  assert.equal(await page.$("dialog.update-dialog"), null, "a failed chunk load opens no dialog");
  assert.equal(await page.$eval(`${updates} .update-check-download`, (e) => e.getAttribute("aria-busy")), null,
    "the busy state ends with the failure");
  await page.screenshot({ path: path.join(shots, "update-open-error-light-1440.png") });
  state.failAssets = null;
  await page.click(`${updates} .update-open-error .update-open-retry`);
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(await page.$(`${updates} .update-open-error`), null, "the error clears when the dialog opens");
  await page.waitForFunction(() => !!document.activeElement?.closest("dialog.update-dialog"), { polling: 100 });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await closeSettings();

  // 7b. Every Retry starts a fresh load: the first two opens and the first
  // two Retries fail, the third Retry opens the dialog. Dismiss returns
  // focus to the control that opened the dialog; a failed Retry keeps focus
  // on Retry.
  await page.reload({ waitUntil: "networkidle0" });
  state.failAssets = /UpdateDialog-/;
  await openUpdates();
  await waitStatus(/^99\.0\.0 available/);
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector(`${updates} .update-open-error[role="alert"]`, { timeout: 15000 });
  await page.locator(`${updates} .update-open-error button ::-p-text(Dismiss)`).click();
  await page.waitForFunction((sel) => !document.querySelector(sel), { polling: 100 }, `${updates} .update-open-error`);
  await page.waitForFunction(() => document.activeElement?.classList.contains("update-check-download"), { polling: 100 });
  await page.click(`${updates} .update-check-download`);
  await page.waitForSelector(`${updates} .update-open-error[role="alert"] .update-open-retry`, { timeout: 15000 });
  const retryFails = async () => {
    await page.click(`${updates} .update-open-error .update-open-retry`);
    await page.waitForFunction((sel) => !document.querySelector(sel), { polling: 100 }, `${updates} .update-open-error`);
    await page.waitForSelector(`${updates} .update-open-error[role="alert"] .update-open-retry`, { timeout: 15000 });
    await page.waitForFunction(() => document.activeElement?.classList.contains("update-open-retry"), { polling: 100 });
  };
  await retryFails();
  await retryFails();
  state.failAssets = null;
  await page.click(`${updates} .update-open-error .update-open-retry`);
  await page.waitForSelector("dialog.update-dialog[open]");
  await page.waitForFunction(() => !!document.activeElement?.closest("dialog.update-dialog"), { polling: 100 });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await closeSettings();

  // 7c. The badge's busy state shows on the 720 px icon rail too: the same
  // aria-busy, and the spinner visible in the dot.
  await page.reload({ waitUntil: "networkidle0" });
  state.slowAssets = /UpdateDialog-/;
  state.slowAssetsMs = 3000;
  await openUpdates();
  await waitStatus(/^99\.0\.0 available/);
  await page.setViewport({ width: 720, height: 520 });
  await pause(150);
  await page.click(".sidebar .brand-group .update-badge");
  await page.waitForSelector(".sidebar .update-badge[aria-busy=\"true\"]");
  const railBusy = await page.evaluate(() => {
    const badge = document.querySelector(".sidebar .update-badge");
    const spinner = badge?.querySelector(".spinner");
    return { busy: badge?.getAttribute("aria-busy"), dot: (badge?.getBoundingClientRect().width || 0) <= 14,
      spinnerVisible: !!spinner && getComputedStyle(spinner).display !== "none" && spinner.getClientRects().length > 0 };
  });
  assert.equal(railBusy.busy, "true", "the rail badge keeps the same aria-busy");
  assert.ok(railBusy.dot, `the rail badge stays a dot: ${JSON.stringify(railBusy)}`);
  assert.ok(railBusy.spinnerVisible, `the rail badge shows the busy spinner: ${JSON.stringify(railBusy)}`);
  await page.screenshot({ path: path.join(shots, "update-rail-badge-busy-light-720.png") });
  await (await page.$(".sidebar-head")).screenshot({ path: path.join(shots, "update-rail-badge-busy-brand-light-720.png") });
  state.slowAssets = null;
  state.slowAssetsMs = 1500;
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(await page.$eval(".sidebar .update-badge", (e) => e.getAttribute("aria-busy")), null,
    "the rail badge's busy state ends when the dialog is open");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await page.setViewport({ width: 1440, height: 900 });
  await closeSettings();

  // 8. Older than 24 hours: a past result with its date, not current, no Download.
  const old = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000);
  statusMock = { status: 200, body: { schema_version: "app-update-status-1.0", current_version: "3.1.0",
    last_check: record("up_to_date", { checked_at: old.toISOString().replace(/\.\d+Z$/, "Z") }) } };
  await page.reload({ waitUntil: "networkidle0" });
  await openUpdates();
  await waitStatus(/^Last result: up to date/);
  const day = old.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  assert.equal(await status(), `Last result: up to date · Checked ${day}`);
  assert.equal(await page.$eval(`${updates} .update-check`, (e) => e.dataset.stale), "true");
  assert.match(await page.$eval(`${updates} .update-check-time`, (e) => e.title), /More than a day ago/);
  await shoot("stale");
  await closeSettings();
  statusMock = { status: 200, body: { schema_version: "app-update-status-1.0", current_version: "3.1.0",
    last_check: record("available", { checked_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString() }) } };
  await page.reload({ waitUntil: "networkidle0" });
  await openUpdates();
  await waitStatus(/^Last result: 3\.2\.0 available/);
  assert.equal(await page.$(`${updates} .update-check-download`), null, "no Download for a stale result");
  await closeSettings();
  statusMock = null;

  // 9. A failed status read is unknown, not "Not checked yet", and About retries it.
  statusMock = { status: 500, body: { error: "Could not read the workspace." } };
  await page.reload({ waitUntil: "networkidle0" });
  await openUpdates();
  await waitStatus(/^Last check unknown$/);
  await closeSettings();
  statusMock = null;

  // 10. About myflames: the same status and button, sharing one check, and its
  // Download opens the dialog through the same shared opener.
  await page.click(".settings-button");
  await page.waitForSelector(".settings-page");
  await page.locator(".settings-page .source-item ::-p-text(About myflames)").click();
  const about = "dialog.about-dialog";
  await page.waitForSelector(about + "[open]");
  await waitStatus(/^99\.0\.0 available · Checked today at/, about);
  await page.click(`${about} .update-check-download`);
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.match(await page.$eval("dialog.update-dialog", (e) => e.textContent), /Software update/);
  assert.ok(await page.$(about + "[open]"), "About stays open behind Software update");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  await page.waitForSelector(about + "[open]");
  checks.push({ status: 200, body: info(record("up_to_date")), delay: 300 });
  await page.click(`${about} .update-check-button`);
  await waitStatus(/Checking for updates…/, about);
  await waitStatus(/^Up to date · Checked today at/, about);
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await pause(50);
    const box = await page.$eval(about, (e) => ({ fits: e.scrollWidth <= e.clientWidth, bottom: e.getBoundingClientRect().bottom }));
    assert.ok(box.fits && box.bottom <= (width === 720 ? 520 : 900), JSON.stringify(box));
    await page.screenshot({ path: path.join(shots, `update-about-${theme}-${width}.png`) });
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, about);
  // The section reads the same stored check once About closes.
  await page.locator(".settings-page .source-item ::-p-text(Updates and news)").click();
  await waitStatus(/^Up to date · Checked today at/);
  await closeSettings();

  assert.deepEqual(h.errors, []);
  console.log(`Check for updates passed (${checkCalls()} checks, all from clicks). Screenshots: ${shots}`);
} finally { await h.close(); }
