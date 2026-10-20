// Browser smoke test for the built 3D page (myflames/assets/teach3d-btree.html).
// Loads it under the workspace's iframe CSP, finishes every chapter and checks
// the readout against src/model.ts, plays one chapter for real, and checks
// reduced motion, the no-WebGL fallback and the 390 px layout.
// Screenshots go to $SHOTS (default: a temporary folder).
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import puppeteer from "puppeteer";
import * as model from "../src/model.ts";

const here = fileURLToPath(new URL("..", import.meta.url));
const asset = path.join(here, "../../myflames/assets/teach3d-btree.html");
const shots = process.env.SHOTS || (await fs.mkdtemp(path.join(os.tmpdir(), "teach3d-")));
await fs.mkdir(shots, { recursive: true });

// The same policy apps/workspace/src/api.ts safePreview() adds to lessons.
const POLICY =
  `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; ` +
  `style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'">`;
const html = (await fs.readFile(asset, "utf8")).replace(/<head[^>]*>/i, (m) => m + POLICY);
const page3d = path.join(shots, "teach3d-btree.csp.html");
await fs.writeFile(page3d, html);
const url = pathToFileURL(page3d).href;

// "shell" like the workspace suites: new headless Chrome produces no frames on some macOS hosts.
const browser = await puppeteer.launch({
  headless: "shell",
  args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"],
});
const errors = [];
let failed = false;

async function open(viewport, query = "") {
  const page = await browser.newPage();
  await page.setViewport(viewport);
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(String(err)));
  await page.goto(url + query, { waitUntil: "load" });
  await page.waitForFunction(() => window.teach3d && document.getElementById("phase-label").textContent);
  return page;
}

const text = (page, selector) => page.$eval(selector, (el) => el.textContent.trim());
const readPages = (page) => text(page, '[data-key="read"] .value');

function expected() {
  const s = model.indexShapes(model.DEFAULTS);
  const hc = s.clustered.levels.length;
  const hs = s.secondary.levels.length;
  const leaves = model.leafRangePages(model.RANGE_FIRST, model.rangeLast(model.DEFAULTS.rows), s.clustered.leafCapacity);
  return { shape: 0, lookup: hc, secondary: hs + hc, covering: hs, range: hc + leaves - 1, leaves, s };
}

try {
  const want = expected();
  const page = await open({ width: 1280, height: 900 });
  const gpu = await page.evaluate(() => window.teach3d.gpu);
  assert.ok(gpu, "WebGL should be available in the test browser");
  assert.equal(await page.$$eval("canvas", (c) => c.length), 1, "one WebGL canvas");

  // Finish every chapter: the final narration and counters use the model's numbers.
  const ids = await page.evaluate(() => window.teach3d.chapters);
  for (let i = 0; i < ids.length; i++) {
    await page.evaluate((n) => window.teach3d.open(n), i);
    await page.evaluate(() => window.teach3d.finish());
    await new Promise((r) => setTimeout(r, 150));
    const pages = Number((await readPages(page)).replace(/,/g, ""));
    assert.equal(pages, want[ids[i]], `${ids[i]}: pages read`);
    const said = await text(page, "#phase-label");
    assert.ok(said.length > 20, `${ids[i]}: narration present`);
    await page.screenshot({ path: path.join(shots, `desktop-${i + 1}-${ids[i]}.png`) });
    console.log(`ok ${ids[i]}: ${pages} pages · ${said}`);
  }
  const rangeSaid = await text(page, "#phase-label");
  assert.match(rangeSaid, new RegExp(`${want.leaves.toLocaleString("en-US")} leaf pages`));

  // Covering switch on the secondary chapter stops after one tree.
  await page.evaluate(() => window.teach3d.open(2));
  await page.click("#covering");
  await page.evaluate(() => window.teach3d.finish());
  assert.equal(Number((await readPages(page)).replace(/,/g, "")), want.covering, "covering: pages read");
  assert.match(await text(page, "#query"), /SELECT id, email FROM users/);
  await page.screenshot({ path: path.join(shots, "desktop-3b-covering.png") });
  await page.click("#covering");

  // Real playback: time advances, the token moves, nothing throws.
  await page.evaluate(() => window.teach3d.open(1));
  await page.click("#btn-play");
  await new Promise((r) => setTimeout(r, 1500));
  const mid = await page.evaluate(() => window.teach3d.state);
  assert.ok(mid.time > 0 && mid.time < mid.total, "playback advances");
  await page.screenshot({ path: path.join(shots, "desktop-2-lookup-playing.png") });
  await page.click("#btn-play"); // pause
  const paused = (await page.evaluate(() => window.teach3d.state)).time;
  await new Promise((r) => setTimeout(r, 400));
  assert.equal((await page.evaluate(() => window.teach3d.state)).time, paused, "pause holds the time");
  await page.click("#btn-play"); // resume
  await page.select("#speed", "4");
  await page.waitForFunction(() => window.teach3d.state.time >= window.teach3d.state.total, { timeout: 20000 });
  assert.equal(await text(page, "#btn-play"), "Replay");

  // Keyboard camera: the canvas takes focus; keys move the camera and hand it to the user.
  await page.focus("canvas");
  assert.equal(await page.$eval("#btn-follow", (b) => b.hidden), true);
  const shotOf = () => page.$eval("canvas", () => document.activeElement.tagName);
  assert.equal(await shotOf(), "CANVAS", "the 3D canvas is focusable");
  await page.keyboard.press("ArrowLeft");
  await page.keyboard.press("-");
  assert.equal(await page.$eval("#btn-follow", (b) => b.hidden), false, "keys take over the camera");
  await page.click("#btn-follow");
  assert.equal(await page.$eval("#btn-follow", (b) => b.hidden), true);

  // Changing an input rebuilds the tree with the new numbers.
  await page.evaluate(() => window.teach3d.open(1));
  await page.select("#in-rows", "1000000000");
  await page.evaluate(() => window.teach3d.finish());
  const big = model.indexShapes({ ...model.DEFAULTS, rows: 1000000000 });
  assert.equal(Number((await readPages(page)).replace(/,/g, "")), big.clustered.levels.length, "1B rows: pages read");
  assert.match(await text(page, "#drawn"), /blocks/);
  await page.screenshot({ path: path.join(shots, "desktop-2-lookup-1B.png") });
  await page.close();

  // Reduced motion: the finished state shows at once.
  const calm = await browser.newPage();
  await calm.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
  calm.on("pageerror", (err) => errors.push(String(err)));
  await calm.setViewport({ width: 1100, height: 800 });
  await calm.goto(url, { waitUntil: "load" });
  await calm.waitForFunction(() => window.teach3d);
  const st = await calm.evaluate(() => window.teach3d.state);
  assert.equal(st.time, st.total, "reduced motion starts finished");
  await calm.click("#btn-reset");
  await calm.click("#btn-play");
  const after = await calm.evaluate(() => window.teach3d.state);
  assert.equal(after.time, after.total, "reduced motion: Play jumps to the end");
  await calm.close();

  // No WebGL: narration and numbers still work.
  const flat = await open({ width: 1100, height: 800 }, "?nowebgl");
  assert.equal(await flat.$$eval("canvas", (c) => c.length), 0);
  assert.equal(await flat.$eval("#fallback", (el) => el.hidden), false);
  await flat.evaluate(() => window.teach3d.open(1));
  await flat.evaluate(() => window.teach3d.finish());
  assert.equal(Number((await readPages(flat)).replace(/,/g, "")), want.lookup, "fallback: pages read");
  await flat.screenshot({ path: path.join(shots, "fallback.png") });
  await flat.close();

  // Phone width: no sideways scroll.
  const phone = await open({ width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
  await phone.evaluate(() => window.teach3d.open(2));
  await phone.evaluate(() => window.teach3d.finish());
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(overflow <= 0, `no horizontal scroll at 390 px (overflow ${overflow})`);
  await phone.screenshot({ path: path.join(shots, "phone-3-secondary.png"), fullPage: true });
  await phone.close();

  assert.deepEqual(errors, [], "no console errors");
  console.log("smoke ok; screenshots in " + shots);
} catch (error) {
  failed = true;
  console.error(error);
  if (errors.length) console.error("console errors:", errors);
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
