// Check for updates in Home's version footer and About myflames: no request
// to GitHub until the button is clicked, progress, up to date / newer
// release / failure, Download opening the update dialog, the last check
// time kept in the workspace store across a reload, and a result older than
// 24 hours shown as a past result. The real Python server answers the stored
// status and one check (MYFLAMES_TEST_UPDATE, no network); other checks are mocked.
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
});
const { page, state } = h;
const shots = path.resolve(process.env.PERF_SHOTS || h.shots);
await fs.mkdir(shots, { recursive: true });
const footer = ".home-version";
// The visible line: status and time, joined by the separator CSS draws between them.
const status = (scope = footer) => page.$eval(`${scope} .update-check`, (e) => [...e.children]
  .map((c) => c.textContent.replace(/\s+/g, " ").trim()).join(" · "));
const checkCalls = () => state.other.filter((r) => r.path === "/api/app/update/check").length;
const iso = (offsetMs = 0) => new Date(Date.now() - offsetMs).toISOString().replace(/\.\d+Z$/, "Z");
const record = (outcome, extra = {}) => ({ checked_at: iso(), outcome, current_version: "3.1.0",
  latest_version: outcome === "failed" ? null : outcome === "available" ? "3.2.0" : "3.1.0", reason: null, ...extra });
const info = (last, extra = {}) => ({ schema_version: "app-update-1.0", current_version: "3.1.0",
  latest_version: last.latest_version || "3.1.0", update_available: last.outcome === "available",
  release_title: "myflames 3.2.0", release_notes: "Notes", published_at: "", html_url: "https://github.com/vgrippa/myflames/releases",
  asset_name: null, asset_url: null, asset_size: null, checked_at: last.checked_at, last_check: last, ...extra });
async function waitStatus(pattern, scope = footer) {
  await page.waitForFunction((s, p) => new RegExp(p).test([...(document.querySelector(`${s} .update-check`)?.children || [])]
    .map((c) => c.textContent.replace(/\s+/g, " ").trim()).join(" · ")),
    { timeout: 10000 }, scope, pattern.source);
}
async function shoot(name) {
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
    await page.screenshot({ path: path.join(shots, `update-${name}-${theme}-${width}.png`) });
    await page.$eval(footer, (e) => e.scrollIntoView({ block: "end" }));
    await (await page.$(footer)).screenshot({ path: path.join(shots, `update-footer-${name}-${theme}-${width}.png`) });
    const box = await page.$eval(footer, (e) => ({ overflow: e.scrollWidth > e.clientWidth,
      rows: new Set([...e.querySelectorAll("strong, .update-check-status, .update-check-time, .update-check-download, .update-check-button, a")]
        .map((c) => Math.round(c.getBoundingClientRect().top + c.getBoundingClientRect().height / 2))).size,
      heights: [...e.children].map((c) => [c.className || c.tagName, c.getBoundingClientRect().height]) }));
    assert.equal(box.overflow, false, `${name} footer has no overflow at ${width}`);
    if (width === 1440) assert.equal(box.rows, 1, `${name} footer is one line at 1440 ${JSON.stringify(box.heights)}`);
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 900 });
}

try {
  // 1. Before the first check: the stored status is read, GitHub is not.
  await waitStatus(/Not checked yet/);
  assert.equal(checkCalls(), 0, "no update check without a click");
  assert.ok(state.other.some((r) => r.path === "/api/app/update/status"), "the stored status was read");
  assert.equal(await page.$(`${footer} .update-check-download`), null);
  assert.equal(await page.$eval(`${footer} a`, (e) => e.href), "https://github.com/vgrippa/myflames/releases");
  await shoot("never");

  // 2. Up to date, with progress while the check runs; focus stays on the button.
  checks.push({ status: 200, body: info(record("up_to_date")), delay: 500 });
  await page.click(`${footer} .update-check-button`);
  await waitStatus(/Checking for updates…/);
  assert.equal(await page.$eval(`${footer} .update-check-button`, (e) => e.getAttribute("aria-disabled")), "true");
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("update-check-button")), true);
  await page.click(`${footer} .update-check-button`); // ignored while checking
  await waitStatus(/^Up to date/);
  assert.equal(checkCalls(), 1, "a click while checking does not start a second check");
  assert.match(await status(), /^Up to date · Checked today at \d{1,2}:\d{2}/);
  const title = await page.$eval(`${footer} .update-check-time`, (e) => e.title);
  assert.match(title, new RegExp(String(new Date().getFullYear())), "the tooltip has the full timestamp");
  assert.equal(await page.$eval(`${footer} .update-check-button`, (e) => e.getAttribute("aria-disabled")), null);
  await shoot("up-to-date");

  // 3. A failed check says why, never "up to date".
  checks.push({ status: 200, body: info(record("failed", { reason: "GitHub could not be reached" }),
    { error: "Could not check for updates: GitHub could not be reached.",
      reason: "GitHub could not be reached" }) });
  await page.click(`${footer} .update-check-button`);
  await waitStatus(/^Could not check/);
  assert.match(await status(), /^Could not check: GitHub could not be reached · Tried today at/);
  assert.equal(await page.$(`${footer} .update-check-download`), null);
  await shoot("failed");

  // 4. A newer release: Download opens the update dialog with the versions.
  checks.push({ status: 200, body: info(record("available")) });
  await page.click(`${footer} .update-check-button`);
  await waitStatus(/^3\.2\.0 available/);
  assert.match(await status(), /^3\.2\.0 available · Checked today at/);
  await shoot("available");
  await page.click(`${footer} .update-check-download`);
  await page.waitForSelector("dialog.update-dialog[open]");
  const dialogText = await page.$eval("dialog.update-dialog", (e) => e.textContent);
  assert.match(dialogText, /Software update/);
  assert.match(dialogText, /myflames 3\.2\.0 is available\./);
  assert.match(dialogText, /Download update/);
  await page.screenshot({ path: path.join(shots, "update-dialog-light-1440.png") });
  await page.locator("dialog.update-dialog button ::-p-text(Later)").click();
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("update-check-download")), true,
    "focus returns to Download");
  // While a new check runs, Download is unavailable; a check that then fails opens nothing.
  checks.push({ status: 200, delay: 600, body: info(record("failed", { reason: "GitHub could not be reached" }),
    { error: "Could not check for updates: GitHub could not be reached.", reason: "GitHub could not be reached" }) });
  await page.click(`${footer} .update-check-button`);
  await waitStatus(/Checking for updates…/);
  assert.equal(await page.$eval(`${footer} .update-check-download`, (e) => e.getAttribute("aria-disabled")), "true");
  await page.click(`${footer} .update-check-download`);
  await waitStatus(/^Could not check/);
  await pause(100);
  assert.equal(await page.$("dialog.update-dialog"), null, "no dialog from a click during a check");

  // 5. The real server answers one check and keeps it in the workspace store.
  checks.push("real");
  await page.click(`${footer} .update-check-button`);
  await waitStatus(/^99\.0\.0 available/);
  const callsBeforeReload = checkCalls();
  await page.reload({ waitUntil: "networkidle0" });
  await waitStatus(/^99\.0\.0 available · Checked today at/);
  assert.equal(checkCalls(), callsBeforeReload, "reload reads the stored check, never GitHub");

  // 6. Download after a reload asks again (a click), then opens the dialog.
  await page.click(`${footer} .update-check-download`);
  await page.waitForSelector("dialog.update-dialog[open]");
  assert.equal(checkCalls(), callsBeforeReload + 1);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.update-dialog"));

  // 7. Older than 24 hours: a past result with its date, not current, no Download.
  const old = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000);
  statusMock = { status: 200, body: { schema_version: "app-update-status-1.0", current_version: "3.1.0",
    last_check: record("up_to_date", { checked_at: old.toISOString().replace(/\.\d+Z$/, "Z") }) } };
  await page.reload({ waitUntil: "networkidle0" });
  await waitStatus(/^Last result: up to date/);
  const day = old.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  assert.equal(await status(), `Last result: up to date · Checked ${day}`);
  assert.equal(await page.$eval(`${footer} .update-check`, (e) => e.dataset.stale), "true");
  assert.match(await page.$eval(`${footer} .update-check-time`, (e) => e.title), /More than a day ago/);
  await shoot("stale");
  statusMock = { status: 200, body: { schema_version: "app-update-status-1.0", current_version: "3.1.0",
    last_check: record("available", { checked_at: new Date(Date.now() - 25 * 60 * 60 * 1000).toISOString() }) } };
  await page.reload({ waitUntil: "networkidle0" });
  await waitStatus(/^Last result: 3\.2\.0 available/);
  assert.equal(await page.$(`${footer} .update-check-download`), null, "no Download for a stale result");
  statusMock = null;

  // 8. A failed status read is unknown, not "Not checked yet", and About retries it.
  statusMock = { status: 500, body: { error: "Could not read the workspace." } };
  await page.reload({ waitUntil: "networkidle0" });
  await waitStatus(/^Last check unknown$/);
  statusMock = null;

  // 9. About myflames: the same status and button, sharing one check.
  await page.click(".settings-button");
  await page.waitForSelector("#settings-popover");
  await page.locator("#settings-popover button ::-p-text(About myflames)").click();
  const about = "dialog.about-dialog";
  await page.waitForSelector(about + "[open]");
  await waitStatus(/^99\.0\.0 available · Checked today at/, about);
  checks.push({ status: 200, body: info(record("up_to_date")), delay: 300 });
  await page.click(`${about} .update-check-button`);
  await waitStatus(/Checking for updates…/, about);
  await waitStatus(/^Up to date · Checked today at/, about);
  await waitStatus(/^Up to date · Checked today at/);
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 720 ? 520 : 900 });
    await pause(50);
    const box = await page.$eval(about, (e) => ({ fits: e.scrollWidth <= e.clientWidth, bottom: e.getBoundingClientRect().bottom }));
    assert.ok(box.fits && box.bottom <= (width === 720 ? 520 : 900), JSON.stringify(box));
    await page.screenshot({ path: path.join(shots, `update-about-${theme}-${width}.png`) });
  }
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, about);

  assert.deepEqual(h.errors, []);
  console.log(`Check for updates passed (${checkCalls()} checks, all from clicks). Screenshots: ${shots}`);
} finally { await h.close(); }
