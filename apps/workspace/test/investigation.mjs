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
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
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
  // The inspect chart is drawn in the page (PlanChart), not in an iframe; its
  // operator rows are docked in the inspector's Operators section.
  const M = ".plan-fold .inspector-metrics .metric-row";
  const inspectorSection = (section) =>
    page.click(`.plan-fold .segmented[aria-label="Inspector section"] button[data-value="${section}"]`);
  async function frame() {
    await page.waitForNetworkIdle();
    await page.waitForSelector(M);
    assert.equal(await page.$(".plan-canvas iframe"), null);
  }
  // Focus/collapse/metric re-project the chart client-side; wait for it.
  async function metricRows(min, max = min) {
    await page.waitForFunction(
      (selector, low, high) => {
        const n = document.querySelectorAll(selector).length;
        return n >= low && n <= high;
      },
      {},
      M,
      min,
      max,
    );
    return page.$$eval(M, (els) => els.length);
  }
  async function select(label) {
    await page.$$eval(
      M,
      (els, text) => els.find((e) => e.textContent.includes(text)).click(),
      label,
    );
  }
  await button("Explore sample plan");
  await frame();
  assert.equal(await page.$(".save-state"), null, "the read-only sample is clean");
  await select("Table scan [categories]");
  await inspectorSection("lessons");
  await button("Learn this operator");
  await page.waitForSelector(".lesson-frame");
  const operatorLesson = await page.$eval('.lesson-frame', el => el.title);
  await button("Next lesson");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title !== title,
    {}, operatorLesson);
  // The next lesson is B+tree, which has a 3D view. Pick it, then come back
  // through the plan: the operator's lesson (2D only) must open, and be
  // requested, in 2D.
  const lessonRequests = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/teach/lesson")) lessonRequests.push(JSON.parse(request.postData() || "{}"));
  });
  await page.click('.segmented[aria-label="Lesson view"] button[data-value="3d"]');
  await page.waitForSelector('.lesson-frame[title$="(3D view)"]');
  await button("Back to plan");
  await frame();
  await inspectorSection("lessons");
  await button("Learn this operator");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title === title,
    {}, operatorLesson);
  assert.deepEqual(
    lessonRequests.filter((body) => body.view === "3d" && body.lesson !== "btree"),
    [],
    "a lesson without a 3D view is never requested in 3D",
  );
  await button("Back to plan");
  await frame();
  assert.match(
    await page.$eval(".operator-detail h3", (e) => e.textContent),
    /categories/,
  );
  await select("Nested loop inner join");
  await button("Focus branch");
  const focused = await metricRows(2, 7);
  assert.ok(focused < 8 && focused > 1);
  await button("Collapse branch");
  assert.equal(await metricRows(1), 1);
  // The Operators list holds the runtime's rows, so only operators the
  // projection shows can be picked there: expand the branch, pick a
  // descendant, then leave the focus for the entire plan.
  await button("Expand branch");
  assert.equal(await metricRows(2, 7), focused);
  await select("Table scan [categories]");
  await page.waitForFunction(
    (selector) =>
      document.querySelector(selector + ".is-selected")?.textContent
        .includes("Table scan [categories]"),
    {},
    M,
  );
  await page.select('[aria-label="Focused branch"]', "");
  assert.equal(await metricRows(8), 8);
  await select("Limit: 20 rows");
  await page.select('[aria-label="Operator metric"]', "estimate_error");
  await page.waitForFunction(() =>
    /Estimate error/.test(
      document.querySelector(".plan-fold .inspector-metrics .metric-panel h2")?.textContent,
    ),
  );
  // Projection changes keep the page selection on the chart.
  assert.match(
    await page.$eval(M + ".is-selected", (e) => e.textContent),
    /Limit: 20 rows/,
  );
  // The notebook is a sheet opened by Save investigation, not a form under the chart.
  assert.equal(await page.$(".investigation-notes"), null, "no notebook form under the chart");
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open] .investigation-notes");
  await page
    .locator(".notebook-grid .field:nth-child(1) input")
    .fill("Index investigation");
  await page
    .locator(".investigation-notes textarea")
    .fill("Baseline and candidate. Check warm-cache timings.");
  await page
    .locator(".notebook-grid .field:nth-child(4) input")
    .fill("index, review");
  assert.equal(await page.$eval(".save-state", (e) => e.textContent), "Unsaved changes");
  await button("Save", "dialog.notebook-sheet .sheet-actions ");
  await page.waitForFunction(
    () => document.querySelector(".save-state")?.textContent === "Investigation saved",
  );
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  // Autosave restores the open plan and notebook after a reload.
  await page.waitForFunction(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    const state = (await r.json()).state;
    return !!state?.investigation.id && !state.investigation.dirty;
  });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".session-notice");
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 1);
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open]");
  assert.equal(
    await page.$eval(".investigation-notes textarea", (e) => e.value),
    "Baseline and candidate. Check warm-cache timings.",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  await button("Investigations", ".main-nav ");
  await page.waitForSelector(".library-item");
  await page.locator('[aria-label="Search investigations"]').fill('no-matching-audit');
  await page.waitForSelector(".library-list .view-empty");
  assert.equal((await page.$$('.library-item')).length, 0);
  await button('Clear search');
  await page.waitForSelector('.library-item');
  assert.match(
    await page.$eval(".library-item", (e) => e.textContent),
    /Index investigation/,
  );
  await page.click(".library-item");
  assert.match(await page.$eval(".library-detail h2", (e) => e.textContent), /Index investigation/);
  await button("Open all", ".library-actions ");
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 1);
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open]");
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
  await button("Close", "dialog.notebook-sheet ");
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  await button("Import plan", ".plans-bar ");
  await page.waitForSelector("dialog");
  await (
    await page.$("dialog input[type=file]")
  ).uploadFile(path.join(root, "test/mariadb-explain-join.json"));
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 2);
  await button("Compare", ".plans-bar ");
  await page.waitForSelector(".comparison-row");
  async function compareMeasurements(width) {
    await page.setViewport({ width, height: 900 });
    const measured = await page.evaluate(() => {
      const rect = selector => {
        const r = document.querySelector(selector).getBoundingClientRect();
        return { top: r.top + scrollY, bottom: r.bottom + scrollY, left: r.left, right: r.right };
      };
      return { pickers: [...document.querySelectorAll(".compare-selectors .toolbar-field")].map(e => {
        const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom };
      }), tools: rect(".compare-tools"), stats: rect(".compare-stats"),
        chart: rect(".comparison-canvases"), firstRow: rect(".comparison-row"),
        overflow: document.documentElement.scrollWidth > innerWidth };
    });
    assert.equal(measured.overflow, false, JSON.stringify(measured));
    assert.ok(Math.abs(measured.pickers[0].top - measured.pickers[1].top) < 1, "the two plan pickers share a row");
    if (width === 1440) assert.ok(measured.tools.top < measured.stats.bottom && measured.stats.top < measured.tools.bottom,
      "desktop controls and headline measurements share one bar: " + JSON.stringify(measured));
    console.log("Compare geometry " + width + "x900: " + JSON.stringify(measured));
    await page.screenshot({ path: path.join(directory, "compare-compact-" + width + ".png"), fullPage: false });
    return measured;
  }
  await compareMeasurements(390);
  await compareMeasurements(1440);
  // Both plans are drawn in the page; the pair follows either side.
  const charts = ".comparison-canvases .plan-chart";
  await page.waitForFunction(
    (selector) =>
      [...document.querySelectorAll(selector)].filter((el) =>
        el.querySelector(".wb-node"),
      ).length === 2,
    {},
    charts,
  );
  assert.equal(await page.$(".comparison-canvases iframe"), null);
  assert.equal(await page.$(".comparison-canvases .wb-details"), null, "Compare has no empty report detail boxes");
  assert.equal(await page.$$eval(".compare-pane", (panes) => {
    const [a, b] = panes.map((el) => el.getBoundingClientRect());
    return Math.abs(a.top - b.top) < 1 && Math.abs(a.width - b.width) < 1;
  }), true, "desktop comparison panes share the row equally");
  const selectedIds = () =>
    page.$$eval(charts, (els) =>
      els.map((el) =>
        [...el.querySelectorAll(".wb-node.selected")].map(
          (node) => node.dataset.nodeId,
        ),
      ),
    );
  const rowSelected = (index) =>
    page.waitForFunction(
      (i) =>
        document
          .querySelectorAll(".comparison-row")
          [i].classList.contains("selected"),
      {},
      index,
    );
  const rows = await page.$$(".comparison-row");
  // Row -> charts. A removed operator has no counterpart: After clears.
  await rows[0].click();
  await rowSelected(0);
  const [beforeSel, afterSel] = await selectedIds();
  assert.equal(beforeSel.length, 1);
  assert.deepEqual(afterSel, []);
  // A pair with the same label on both sides is titled once; its badge says
  // it was matched (not Added or Removed).
  const matched = await page.$$eval(".comparison-row", (els) =>
    els.findIndex((el) =>
      el.querySelector("strong").textContent === "Nested loop inner join" &&
      !["added", "removed"].includes(el.querySelector(".match-badge").dataset.status),
    ),
  );
  assert.ok(matched > 0);
  await rows[matched].click();
  await rowSelected(matched);
  const pairIds = await selectedIds();
  assert.equal(pairIds[0].length, 1);
  assert.equal(pairIds[1].length, 1);
  assert.notDeepEqual(pairIds[0], beforeSel);
  // Chart -> row and counterpart, from the After side.
  await rows[0].click();
  await rowSelected(0);
  await page.$$eval(
    charts,
    (els, id) =>
      els[1]
        .querySelector('.wb-node[data-node-id="' + id + '"]')
        .dispatchEvent(new MouseEvent("click", { bubbles: true })),
    pairIds[1][0],
  );
  await rowSelected(matched);
  assert.deepEqual(await selectedIds(), pairIds);
  // ...and from the Before side, back to the removed operator.
  await page.$$eval(
    charts,
    (els, id) =>
      els[0]
        .querySelector('.wb-node[data-node-id="' + id + '"]')
        .dispatchEvent(new MouseEvent("click", { bubbles: true })),
    beforeSel[0],
  );
  await rowSelected(0);
  assert.deepEqual(await selectedIds(), [beforeSel, []]);
  await page.setViewport({ width: 390, height: 844 });
  await page.waitForFunction(() => [...document.querySelectorAll('.compare-pane svg.wb-plan')].every((svg) =>
    Math.abs(svg.getBoundingClientRect().width - svg.viewBox.baseVal.width) < 1));
  const narrowPanes = await page.$$eval(".compare-pane", (panes) => panes.map((el) => {
    const rect = el.getBoundingClientRect();
    const svg = el.querySelector('svg.wb-plan');
    const canvas = svg.getBoundingClientRect();
    const viewport = svg.querySelector('.wb-viewport').viewBox.baseVal;
    return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
      width: rect.width, canvasWidth: canvas.width, zoom: canvas.width / viewport.width, y: viewport.y };
  }));
  assert.ok(narrowPanes[1].top >= narrowPanes[0].bottom - 1, "phone comparison stacks Before then After");
  for (const pane of narrowPanes) {
    assert.ok(pane.left >= 0 && pane.right <= 390 && Math.abs(pane.canvasWidth - pane.width) < 2);
    // The zoom floor is 0.75; the measured ratio of two rendered widths can
    // land a rounding error below it (0.74999998).
    assert.ok(pane.zoom >= 0.75 - 1e-6 && pane.zoom <= 1, JSON.stringify(pane));
    assert.equal(pane.y, -28, "each comparison starts at the inputs");
  }
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
  await button("Explore", ".plans-bar ");
  await frame();
  // The phone picker opens the same investigation plans; projection remains
  // controlled by the shared chart state even when its controls are in a menu.
  await page.setViewport({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  const phonePlanIds = await page.$$eval('select[aria-label="Open plan"] option', options => options.map(option => option.value));
  assert.equal(phonePlanIds.length, 2);
  await page.select('select[aria-label="Open plan"]', phonePlanIds[1]);
  await frame();
  assert.equal(await page.$eval('select[aria-label="Open plan"]', select => select.value), phonePlanIds[1]);
  await page.click('button[aria-label="Chart options"]');
  await page.select('select[aria-label="Phone operator metric"]', 'rows');
  await page.waitForFunction(() => document.querySelector('.plan-inspector .metric-panel h2')?.textContent.includes('rows'));
  await page.keyboard.press('Escape');
  await page.select('select[aria-label="Open plan"]', phonePlanIds[0]);
  await frame();
  assert.equal(await page.$eval('select[aria-label="Operator metric"]', select => select.value), 'rows', 'phone and desktop share the metric');
  await page.setViewport({ width: 1512, height: 1050 });
  await page.select('select[aria-label="Operator metric"]', 'self_time');
  await frame();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "plan.png"),
    fullPage: true,
  });
  if (process.env.MYFLAMES_TEST_PORT) {
    // Set up the connection on Home and put it in use, then capture.
    await button("Home", ".main-nav ");
    await button("Add connection");
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
        .querySelector(".connections .notice")
        ?.textContent.includes("Connected"),
    );
    await button("Use without saving", ".connection-panel ");
    await button("Query editor", ".main-nav ");
    await page.waitForSelector(".query-lab");
    await page
      .locator('[aria-label="SQL editor"]')
      .fill("SELECT category, SUM(price) FROM items GROUP BY category");
    // Tabs rename inline: F2 on the selected tab, Enter keeps the name.
    await page.focus(".query-tabs [role=tab][aria-selected=true]");
    await page.keyboard.press("F2");
    await page
      .locator('[aria-label="Query name"]')
      .fill("Live experiment");
    await page.keyboard.press("Enter");
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
  await button("Query editor", ".main-nav ");
  await page.waitForSelector(".query-lab");
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
