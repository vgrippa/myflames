// Real Home navigation and Mermaid parser, with all database traffic refused.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open } from "./perf-harness.mjs";

const shots = path.resolve(process.env.PERF_SHOTS || "../../local/fixes-2026-10-16/home");
await fs.mkdir(shots, { recursive: true });
const h = await open("/api/unused-home-test", "home-workspace", {
  "/api/live/schemas": { status: 200, body: { schemas: [], notes: [] } },
});
const { page } = h;
async function click(text) {
  const handle = await page.evaluateHandle(text => [...document.querySelectorAll("button")]
    .find(el => el.textContent.trim() === text && el.getClientRects().length), text);
  assert.ok(handle.asElement(), `Button ${text}`);
  await handle.asElement().click(); await handle.dispose();
}
async function screenshots(name) {
  for (const theme of ["light", "dark"]) for (const width of [1440, 390]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    await page.setViewport({ width, height: width === 390 ? 844 : 1000 });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${name} fits ${width}`);
    await page.screenshot({ path: path.join(shots, `${name}-${theme}-${width}.png`), fullPage: true });
  }
  await page.setViewport({ width: 1440, height: 1000 });
}
try {
  await page.waitForSelector(".home-version");
  assert.match(await page.$eval(".home-version", el => el.textContent), /myflames \d+\.\d+\.\d+.*Updates not checked/);
  assert.equal(await page.$eval(".home-version a", el => el.href), "https://github.com/vgrippa/myflames/releases");
  assert.deepEqual(await page.$$eval(".nav-group-label", els => els.map(el => el.textContent)), ["SQL workspace", "Library"]);
  await screenshots("home");
  await page.click(".home-workflows button:first-child");
  await page.waitForFunction(() => document.querySelector(".main-nav button.active")?.textContent === "Query editor");
  await click("Home");
  await page.click(".home-workflows button:nth-child(2)");
  await page.waitForFunction(() => document.querySelector(".main-nav button.active")?.textContent === "Server");
  await click("Home");
  await page.click(".home-workflows button:nth-child(3)");
  await page.waitForSelector('.server-view[data-view="diagram"]');
  await click("View Mermaid");
  assert.equal(await page.$eval('[aria-label="Import format"]', el => el.value), "mermaid");
  await page.type(".diagram-paste textarea", "erDiagram\n  customers ||--o{ orders : places\n  customers {\n    int id PK\n    string name\n  }\n  orders {\n    int id PK\n    int customer_id FK\n  }\n");
  await click("Import diagram");
  await page.waitForSelector(".diagram-table");
  assert.equal(await page.$$eval(".diagram-table", els => els.length), 2);
  assert.match(await page.$eval(".diagram-canvas", el => el.textContent), /customers/);
  assert.match(await page.$eval(".diagram-canvas", el => el.textContent), /orders/);
  await screenshots("mermaid");
  assert.equal(h.state.other.some(item => /editor\/run|live\/start|lifecycle\/start/.test(item.path)), false);
  assert.deepEqual(h.errors, []);
  console.log("Home, grouped SQL navigation, and real Mermaid rendering passed.");
} finally { await h.close(); }
