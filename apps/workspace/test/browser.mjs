// End-to-end test against the actual packaged UI and Python server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-browser-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.origin, file=sys.stderr, flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root },
);
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(
      () => reject(new Error("UI did not start: " + output)),
      10000,
    );
    server.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    server.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`UI exited: ${code}: ${output}`));
    });
    server.stderr.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: directory,
  });
  await page.setViewport({ width: 1440, height: 1050 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.screenshot({
    path: path.join(directory, "welcome.png"),
    fullPage: true,
  });

  async function button(label, scope = "") {
    await page.locator(`${scope}button ::-p-text(${label})`).click();
  }
  async function preview(selector = "svg") {
    await page.waitForNetworkIdle();
    await page.waitForFunction(
      () =>
        !document.querySelector(".preview-state") &&
        !!document.querySelector(".chart-frame"),
    );
    const element = await page.$(".chart-frame");
    const frame = await element.contentFrame();
    await frame.waitForSelector(selector);
    return frame;
  }
  async function paste(raw, name) {
    await page.waitForFunction(
      () => !document.querySelector("dialog textarea").disabled,
    );
    await page.locator("dialog textarea").fill("");
    await page.focus("dialog textarea");
    await page.keyboard.sendCharacter(raw);
    if (name) await page.locator("dialog .field input").fill(name);
    await button("Analyze plan");
  }
  async function downloaded(filename) {
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      try {
        return await fs.readFile(path.join(directory, filename), "utf8");
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    throw new Error("Download missing: " + filename);
  }

  await button("Explore sample plan");
  await preview();
  await page.locator(".operator-row").click();
  assert.ok(await page.$(".operator-detail"));
  await page.locator(".operators .search input").fill("not-an-operator");
  assert.equal((await page.$$(".operator-row")).length, 0);
  await page.locator(".operators .search input").fill("");
  for (const label of [
    "Visual Explain",
    "Execution tree",
    "Bar chart",
    "Treemap",
    "Flame graph",
  ]) {
    await button(label, ".view-tabs ");
    const frame = await preview();
    if (label === "Visual Explain") {
      await frame.waitForSelector('svg.wb-plan[data-wired="true"]');
      const initial = await frame.$eval(".wb-viewport", (el) =>
        el.getAttribute("viewBox"),
      );
      await frame.click('[data-action="in"]');
      assert.notEqual(
        await frame.$eval(".wb-viewport", (el) => el.getAttribute("viewBox")),
        initial,
      );
      await frame.click('[data-action="fit"]');
      assert.equal(
        await frame.$eval(".wb-viewport", (el) => el.getAttribute("viewBox")),
        initial,
      );
      await frame.locator('input[type="search"]').fill("not-an-operator");
      assert.equal(
        await frame.$$eval(".wb-node:not(.dim)", (nodes) => nodes.length),
        0,
      );
      await frame.$eval('input[type="search"]', (el) => {
        el.value = "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
      });
      assert.equal(
        await frame.$$eval(".wb-node.dim", (nodes) => nodes.length),
        0,
      );
      const chosen = await frame.$eval(".wb-node:last-of-type", (el) => {
        el.dispatchEvent(
          new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
        );
        return el.dataset.label;
      });
      await page.waitForFunction(
        (label) =>
          document.querySelector(".operator-detail h3").textContent === label,
        {},
        chosen,
      );
      // Exercise actual drag panning, then restore the overview.
      const chart = await (await frame.$("svg.wb-plan")).boundingBox();
      const canvasTop = chart.y + (160 * chart.width) / 1200;
      await page.mouse.move(chart.x + 25, canvasTop);
      await page.mouse.down();
      await page.mouse.move(chart.x + 85, canvasTop + 30, { steps: 6 });
      await page.mouse.up();
      assert.notEqual(
        await frame.$eval(".wb-viewport", (el) => el.getAttribute("viewBox")),
        initial,
      );
      await frame.click('[data-action="fit"]');
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({
        path: path.join(directory, "workbench.png"),
        fullPage: true,
      });
      await button("Export HTML");
      const report = await downloaded("sample-plan-report.html");
      assert.match(report, /wb-plan/);
      const exported = await browser.newPage();
      exported.on("pageerror", (error) => errors.push(String(error)));
      await exported.goto(
        "file://" + path.join(directory, "sample-plan-report.html"),
      );
      await exported.waitForSelector('svg.wb-plan[data-wired="true"]');
      assert.equal(
        await exported.$$eval(".wb-node", (nodes) => nodes.length),
        await frame.$$eval(".wb-node", (nodes) => nodes.length),
      );
      await exported.click('[data-action="in"]');
      await exported.close();
      await fs.unlink(path.join(directory, "sample-plan-report.html"));
    }
    // Exercise the shared renderers' initialization and search navigation.
    if (label === "Bar chart" || label === "Treemap") {
      page.once("dialog", (dialog) => dialog.accept("scan"));
      await frame.evaluate(() => {
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "/" }));
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "n" }));
        document.dispatchEvent(new KeyboardEvent("keydown", { key: "N" }));
      });
      assert.ok(
        await frame.$('.highlight-active, .bar[style*="stroke"]'),
        "Chart search highlights matches",
      );
    }
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "plan.png"),
    fullPage: true,
  });
  await button("Export JSON");
  const analysis = JSON.parse(await downloaded("sample-plan-analysis.json"));
  assert.ok(analysis.schema_version);
  assert.ok(analysis.plan_summary.operator_count > 0);
  await button("Export HTML");
  assert.match(
    await downloaded("sample-plan-report.html"),
    /application\/ld\+json/,
  );

  // Teach must work inside the workspace without discarding the loaded plan.
  await button("Teach", ".main-nav ");
  await page.waitForSelector('.lesson-card[data-lesson="full_scan"]');
  const lessonCount = (await page.$$(".lesson-card")).length;
  assert.ok(lessonCount >= 23);
  await page
    .locator('input[aria-label="Search lessons"]')
    .fill("no-such-lesson");
  await page.waitForFunction(
    () => document.querySelectorAll(".lesson-card").length === 0,
  );
  await button("Clear filters");
  await page.select('select[aria-label="Lesson family"]', "planner_family");
  await page.waitForSelector('.lesson-card[data-lesson="join_order"]');
  assert.equal((await page.$$(".lesson-card")).length, 1);
  await page.select('select[aria-label="Lesson family"]', "");
  await page.screenshot({
    path: path.join(directory, "teach-catalog.png"),
    fullPage: true,
  });
  await button("Start learning");
  await page.waitForSelector(".lesson-frame");
  let lessonFrame = await (await page.$(".lesson-frame")).contentFrame();
  await lessonFrame.waitForSelector("#btn-play");
  await lessonFrame.waitForFunction(() => typeof teachRuntime !== "undefined");
  const beforeRows = await lessonFrame.$eval(
    "#out-read",
    (element) => element.textContent,
  );
  await lessonFrame.$eval("#rows", (element) => {
    element.value = "500000";
    element.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await lessonFrame.waitForFunction(
    (previous) => document.querySelector("#out-read").textContent !== previous,
    {},
    beforeRows,
  );
  await lessonFrame.click("#btn-play");
  await lessonFrame.waitForFunction(
    () =>
      document.querySelector("#phase-label").textContent !==
      "Ready — press Play",
  );
  await button("Download lesson");
  assert.match(await downloaded("full_scan.html"), /teachRuntime/);
  await page.screenshot({
    path: path.join(directory, "teach-lesson.png"),
    fullPage: true,
  });
  await button("Next lesson");
  await page.waitForSelector('iframe[title^="Lesson: B"]');
  await button("Previous lesson");
  await page.waitForSelector('iframe[title^="Lesson: Full"]');
  await button("All lessons");
  await page.waitForSelector(".lesson-card");
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(directory, "teach-mobile.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 1440, height: 1050 });
  await button("Plan explorer", ".main-nav ");
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 1);

  await button("Import plan", ".heading-actions ");
  await page.waitForSelector("dialog[open]");
  await paste("{bad");
  await page.waitForSelector('dialog [role="alert"]');
  // Keep the native file input accessible without painting over the custom
  // picker. Exercise error content and short screens while the dialog is open.
  for (const size of [
    { width: 1440, height: 1050 },
    { width: 390, height: 844 },
    { width: 320, height: 568 },
  ]) {
    await page.setViewport(size);
    const layout = await page.evaluate(() => {
      const dialog = document.querySelector("dialog");
      const bounds = dialog.getBoundingClientRect();
      const picker = dialog.querySelector('.file-picker input[type="file"]');
      const input = picker.getBoundingClientRect();
      return {
        inViewport: bounds.left >= 0 && bounds.top >= 0 &&
          bounds.right <= innerWidth && bounds.bottom <= innerHeight,
        noHorizontalOverflow: dialog.scrollWidth <= dialog.clientWidth,
        clippedInput: input.width <= 1 && input.height <= 1 &&
          getComputedStyle(picker).clipPath !== "none",
        scrollable: getComputedStyle(dialog).overflowY === "auto",
      };
    });
    assert.deepEqual(layout, {
      inViewport: true, noHorizontalOverflow: true,
      clippedInput: true, scrollable: true,
    });
    await page.screenshot({
      path: path.join(directory, `import-error-${size.width}.png`),
      fullPage: true,
    });
  }
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await page.setViewport({ width: 1440, height: 1050 });
  await button("Import plan", ".heading-actions ");
  await page.waitForSelector("dialog[open]");
  await paste(
    await fs.readFile(
      path.join(root, "test/mariadb-explain-join.json"),
      "utf8",
    ),
    "mariadb-after.json",
  );
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 2);
  await button("Compare plans", ".main-nav ");
  await page.waitForSelector(".comparison-row");
  await button("Export JSON");
  assert.equal(
    JSON.parse(await downloaded("comparison.json")).schema_version,
    "compare-1.0",
  );
  await button("Export HTML");
  assert.match(await downloaded("myflames-comparison.html"), /<!DOCTYPE html>/);
  await page.setViewport({ width: 390, height: 844 });
  await page.waitForSelector(".comparison-row");
  await page.screenshot({
    path: path.join(directory, "mobile.png"),
    fullPage: true,
  });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );

  await page.setViewport({ width: 1440, height: 1050 });
  await page.locator('button[aria-label="Remove mariadb-after.json"]').click();
  await page.waitForSelector(".compare-empty");
  assert.equal((await page.$$(".chart-frame")).length, 0);
  await button("Import plan", ".page-heading ");
  const input = await page.$('input[type="file"]');
  await input.uploadFile(
    path.join(root, "test/mysql-explain-json-sample.json"),
  );
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 2);
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload({ waitUntil: "networkidle0" });
  assert.equal((await page.$$(".plan-item")).length, 0);
  await page.evaluate(
    (raw) => {
      const transfer = new DataTransfer();
      transfer.items.add(
        new File([raw], "dropped-plan.json", { type: "application/json" }),
      );
      document
        .querySelector(".import-card")
        .dispatchEvent(
          new DragEvent("drop", { bubbles: true, dataTransfer: transfer }),
        );
    },
    await fs.readFile(
      path.join(root, "test/mysql-explain-json-sample.json"),
      "utf8",
    ),
  );
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 1);
  await button("Import plan", ".heading-actions ");
  await page.waitForSelector("dialog[open]");
  assert.ok(
    await page.evaluate(() =>
      document.querySelector("dialog").contains(document.activeElement),
    ),
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog"));
  assert.deepEqual(errors, []);
  console.log("Browser checks passed. Screenshots and downloads: " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
