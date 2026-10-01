// Complete investigation workflow. Optional live DB is an explicitly supplied test server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(
  path.join(os.tmpdir(), "myflames-investigation-"),
);
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.origin,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root },
);
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    server.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1512, height: 1050 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: directory,
  });
  const button = (text, scope = "") =>
    page.locator(`${scope}button ::-p-text(${text})`).click();
  async function frame() {
    await page.waitForNetworkIdle();
    await page.waitForSelector(".chart-frame");
    const f = await (await page.$(".chart-frame")).contentFrame();
    await f.waitForSelector(".metric-row");
    return f;
  }
  async function select(label) {
    await page.$$eval(
      ".operator-row",
      (els, text) => els.find((e) => e.textContent.includes(text)).click(),
      label,
    );
  }
  await button("Explore sample plan");
  await frame();
  await select("Table scan [categories]");
  await button("Learn this operator");
  await page.waitForSelector(".lesson-frame");
  const operatorLesson = await page.$eval('.lesson-frame', el => el.title);
  await button("Next lesson");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title !== title,
    {}, operatorLesson);
  await button("Back to investigation");
  await frame();
  await button("Learn this operator");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title === title,
    {}, operatorLesson);
  await button("Back to investigation");
  await frame();
  assert.match(
    await page.$eval(".operator-detail h3", (e) => e.textContent),
    /categories/,
  );
  await select("Nested loop inner join");
  await button("Focus branch");
  let f = await frame();
  const focused = await f.$$eval(".metric-row", (els) => els.length);
  assert.ok(focused < 8 && focused > 1);
  await button("Collapse branch");
  f = await frame();
  assert.equal(await f.$$eval(".metric-row", (els) => els.length), 1);
  // Selecting a hidden descendant reveals it; an outside node exits focus.
  await select("Table scan [categories]");
  f = await frame();
  assert.equal(await f.$$eval(".metric-row", els => els.length), focused);
  await select("Limit: 20 rows");
  f = await frame();
  assert.equal(await f.$$eval(".metric-row", (els) => els.length), 8);
  await page.select('[aria-label="Operator metric"]', "estimate_error");
  f = await frame();
  assert.match(
    await f.$eval(".metric-panel h2", (e) => e.textContent),
    /Estimate error/,
  );
  await page
    .locator(".notebook-grid .field:nth-child(1) input")
    .fill("Index investigation");
  await page
    .locator(".investigation-notes textarea")
    .fill("Baseline and candidate. Check warm-cache timings.");
  await page
    .locator(".notebook-grid .field:nth-child(4) input")
    .fill("index, review");
  await button("Save investigation", ".investigation-notes ");
  await page.waitForFunction(
    () => document.querySelector(".save-state")?.textContent === "Saved",
  );
  await page.reload({ waitUntil: "networkidle0" });
  assert.equal((await page.$$(".plan-item")).length, 0);
  await button("Investigations", ".main-nav ");
  await page.waitForSelector(".library-card");
  await page.locator('[aria-label="Search investigations"]').fill('no-matching-audit');
  await page.waitForSelector('.empty-small[role="status"]');
  assert.equal((await page.$$('.library-card')).length, 0);
  await button('Clear search');
  await page.waitForSelector('.library-card');
  assert.match(
    await page.$eval(".library-card", (e) => e.textContent),
    /Index investigation/,
  );
  await button("Open investigation");
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 1);
  assert.equal(
    await page.$eval(".investigation-notes textarea", (e) => e.value),
    "Baseline and candidate. Check warm-cache timings.",
  );
  await button("Export investigation bundle");
  await page.waitForNetworkIdle();
  const filename = path.join(directory, "Index-investigation.json");
  await fs.access(filename);
  const bundle = JSON.parse(await fs.readFile(filename, "utf8"));
  assert.equal(bundle.schema_version, "investigation-1.0");
  assert.equal(bundle.plans.length, 1);
  assert.equal(bundle.baseline_id, bundle.plans[0].id);
  await button("Import plan", ".heading-actions ");
  await page.waitForSelector("dialog");
  await (
    await page.$("dialog input[type=file]")
  ).uploadFile(path.join(root, "test/mariadb-explain-join.json"));
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 2);
  await button("Compare plans", ".main-nav ");
  await page.waitForSelector(".comparison-row");
  await page.locator(".comparison-row").click();
  await page.waitForFunction(() =>
    document.querySelector(".comparison-row.selected"),
  );
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(directory, "compare-mobile.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 1512, height: 1050 });
  await button("Plan explorer", ".main-nav ");
  await frame();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "plan.png"),
    fullPage: true,
  });
  await button("Query laboratory", ".main-nav ");
  await page.waitForSelector(".query-lab");
  if (process.env.MYFLAMES_TEST_PORT) {
    await page.locator(".connection-panel label ::-p-text(Host)").wait();
    await page.$eval(
      ".connection-panel",
      (panel, port) => {
        const set = (label, value) => {
          const e = [...panel.querySelectorAll("label")]
            .find((e) => e.firstChild.textContent.trim() === label)
            ?.querySelector("input");
          if (!e) throw Error(label);
          const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype,
            "value",
          ).set;
          setter.call(e, value);
          e.dispatchEvent(new Event("input", { bubbles: true }));
        };
        set("Port", port);
        set("User", "root");
        set("Password", "myflames-validation-only");
        set("Schema", "lab");
      },
      process.env.MYFLAMES_TEST_PORT,
    );
    await button("Test connection");
    await page.waitForFunction(() =>
      document
        .querySelector(".query-lab .notice")
        ?.textContent.includes("Connected"),
    );
    await page
      .locator('[aria-label="SQL editor"]')
      .fill("SELECT category, SUM(price) FROM items GROUP BY category");
    await page
      .locator('[aria-label="Experiment name"]')
      .fill("Live experiment");
    await page.$eval(".execution-options input[type=checkbox]", (e) =>
      e.click(),
    );
    await button("Run analysis");
    await page.waitForFunction(
      () =>
        document.querySelector(".plan-heading h1")?.textContent ===
        "Live experiment",
      { timeout: 30000 },
    );
    await frame();
    await button("Measurements", ".context-panel ");
    assert.match(
      await page.$eval(".context-body", (e) => e.textContent),
      /Measured runs/,
    );
    await button("Optimizer trace", ".context-panel ");
    assert.ok(await page.$(".trace-branch"));
    await page.locator(".context-panel input[type=search]").fill("sum");
    assert.match(
      await page.$eval(".context-body", (e) => e.textContent),
      /items/,
    );
  }
  await button("Query laboratory", ".main-nav ");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "query-lab.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(directory, "query-lab-mobile.png"),
    fullPage: true,
  });
  assert.deepEqual(errors, []);
  console.log("Investigation workflow passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
