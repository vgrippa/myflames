// Real workspace shell and detail navigation, with source-captured table data.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open, load } from "./perf-harness.mjs";

const baseline = process.env.SHELL_BASELINE === "1";
const shots = path.resolve(process.env.PERF_SHOTS || "../../local/desktop-integration/shell-after");
await fs.mkdir(shots, { recursive: true });
const h = await open("/api/live/tables", "desktop-shell", {
  "/api/live/schemas": { status: 200, body: await load("schemas-mysql-8.4.json") },
});
const { page, state } = h;
state.fallback = { status: 200, body: await load("tables-mysql-8.4.json") };
const view = '.server-view[data-view="tables"]';
async function shot(name, theme, width) {
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
  await page.setViewport({ width, height: width === 390 ? 844 : 1000 });
  await page.waitForFunction(() => (document.querySelector("#window-sidebar-toggle")?.getAttribute("aria-expanded") === "true") === (innerWidth > 650));
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
  await page.screenshot({ path: path.join(shots, `${name}-${theme}-${width}.png`), fullPage: true });
}
try {
  await h.server(); await h.view("tables"); await h.refresh();
  await page.waitForSelector(`${view} tr[data-table="mftab.no_key"]`);
  for (const theme of ["light", "dark"]) for (const width of [1440, 390]) await shot("tables", theme, width);
  await page.setViewport({ width: 1440, height: 1000 });
  await page.type(`${view} input[aria-label="Filter tables"]`, "no_key");
  await h.select(`${view} tr[data-table="mftab.no_key"]`);
  for (const theme of ["light", "dark"]) for (const width of [1440, 390]) await shot("table-detail", theme, width);
  if (!baseline) {
    assert.equal(await page.$(`${view} .view-inspector.is-sheet`), null, "details never become a cramped sheet");
    assert.equal(await page.$eval(`${view} .view-main`, element => element.hidden), true);
    await page.click(`${view} .detail-back`);
    await page.waitForFunction(selector => !document.querySelector(`${selector} .view-inspector`), {}, view);
    assert.equal(await page.$eval(`${view} input[aria-label="Filter tables"]`, element => element.value), "no_key");
    assert.equal(await page.$eval(`${view} tr[data-table="mftab.no_key"]`, element => element.classList.contains("is-selected")), true);
    await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
    await page.waitForSelector("dialog.command-palette[open]");
    await page.type('input[aria-label="Search commands"]', "database diagram");
    assert.equal(await page.$$eval('.command-option', elements => elements.length), 1);
    await page.keyboard.press("Enter");
    await page.waitForSelector('.server-view[data-view="diagram"]');
    assert.equal(await page.$('dialog.command-palette'), null);
    await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
    await page.waitForSelector("dialog.command-palette[open]");
    await page.type('input[aria-label="Search commands"]', "not-a-command");
    assert.match(await page.$eval('.command-empty', element => element.textContent), /No commands/);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
    for (const theme of ["light", "dark"]) for (const width of [1440, 390]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
      await page.setViewport({ width, height: width === 390 ? 844 : 1000 });
      await page.click('.command-trigger');
      await page.waitForSelector('dialog.command-palette[open]');
      await page.screenshot({ path: path.join(shots, `commands-${theme}-${width}.png`) });
      await page.keyboard.press('Escape');
    }
    assert.equal(state.other.some(request => /editor\/run|live\/start|lifecycle\/start/.test(request.path)), false, "navigation never starts SQL");
  }
  assert.deepEqual(h.errors, []);
  console.log(`Desktop shell ${baseline ? "baseline" : "checks"} passed. ${shots}`);
} finally { await h.close(); }
