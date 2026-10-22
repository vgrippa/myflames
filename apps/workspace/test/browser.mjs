// End-to-end test against the actual packaged UI and Python server.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import ts from "typescript";

const root = fileURLToPath(new URL("../../../", import.meta.url));
// These controller checks run without Chrome. Keep a focused entry point for
// restricted build workers; the normal invocation also runs every UI check.
const zoomSource = await fs.readFile(path.join(root, "apps/workspace/src/charts/core/zoom.ts"), "utf8");
const { createZoom } = await import("data:text/javascript;base64," + Buffer.from(ts.transpileModule(zoomSource, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ES2020 },
}).outputText).toString("base64"));
class ZoomElement extends EventTarget {
  attributes = new Map();
  classList = { add() {}, remove() {} };
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key); }
}
function checkZoom(width, height, graphWidth, graphHeight, fitWidth) {
  const viewport = new ZoomElement();
  const buttons = Object.fromEntries(["fit", "zoomIn", "zoomOut", "reset"].map((key) => [key, new ZoomElement()]));
  const root = new ZoomElement();
  root.ownerDocument = { defaultView: { setTimeout, clearTimeout, cancelAnimationFrame() {} } };
  const zoom = createZoom({ root, viewport, width, height, graphWidth, graphHeight, buttons, fitWidth });
  const box = () => viewport.getAttribute("viewBox").split(" ").map(Number);
  const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-8, `${actual} != ${expected}`);
  zoom.fit();
  const initial = box();
  const ratio = fitWidth ? Math.max(1, Math.min(1 / 0.75, (graphWidth + 56) / width))
    : Math.max((graphWidth + 56) / width, (graphHeight + 56) / height);
  close(initial[2], Math.max(width / 5, width * ratio));
  if (fitWidth) {
    assert.equal(initial[1], -28, "first inputs start at the top, without a centered band");
    assert.ok(width / initial[2] >= 0.75 && width / initial[2] <= 1);
  }
  assert.equal(buttons.reset.textContent, Math.round(width / initial[2] * 100) + "%");
  zoom.zoom(0.8, 0.5, 0.5);
  const chosen = box();
  zoom.resize(width / 2, height + 100);
  close((width / 2) / box()[2], width / chosen[2]);
  close(box()[0] + box()[2] / 2, chosen[0] + chosen[2] / 2);
  close(box()[1] + box()[3] / 2, chosen[1] + chosen[3] / 2);
  const resized = box();
  zoom.resize(0, 0);
  zoom.resize(NaN, Infinity);
  assert.deepEqual(box(), resized, "hidden or invalid canvases do not destroy the viewport");
  zoom.resize(width, height);
  zoom.fit();
  assert.deepEqual(box(), initial, "fit returns to the same viewport after resizing");
  zoom.resize(width / 2, height);
  if (fitWidth) assert.ok((width / 2) / box()[2] >= 0.75, "a fitted canvas refits on resize");
  zoom.resize(width, height);
  zoom.zoom(1e-6, 0.5, 0.5);
  assert.equal(buttons.zoomIn.disabled, true);
  assert.equal(buttons.zoomOut.disabled, false);
  assert.equal(buttons.reset.textContent, "500%");
  zoom.zoom(1e6, 0.5, 0.5);
  assert.equal(buttons.zoomOut.disabled, true);
  assert.equal(buttons.zoomIn.disabled, false);
  assert.equal(buttons.reset.textContent, Math.round(width / box()[2] * 100) + "%");
  buttons.reset.dispatchEvent(new Event("click"));
  assert.equal(buttons.reset.textContent, "100%");
  assert.equal(buttons.zoomIn.disabled, false);
  assert.equal(buttons.zoomOut.disabled, false);
  buttons.fit.dispatchEvent(new Event("click"));
  assert.deepEqual(box(), initial);
  zoom.destroy();
}
for (const dimensions of [[904, 620, 920, 1416], [390, 420, 920, 1416], [320, 360, 6400, 220], [612, 460, 256, 216]]) {
  checkZoom(...dimensions, true);
  checkZoom(...dimensions, false);
}
console.log("Zoom controller checks passed (8 canvas and graph combinations).");
if (process.argv.includes("--zoom-only")) process.exit(0);

const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-browser-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
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
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
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
  // Views registered in the chart runtime are drawn in the page by
  // PlanChart (no iframe); C scopes selectors to that chart.
  const C = ".plan-canvas .plan-chart ";
  const M = ".plan-fold .inspector-metrics ";
  async function preview() {
    await page.waitForNetworkIdle();
    await page.waitForFunction(
      () =>
        !document.querySelector(".preview-state") &&
        !!document.querySelector(
          '.plan-canvas .plan-chart[data-mfc-mounted="true"] svg',
        ),
    );
    assert.equal(
      await page.$(".plan-canvas iframe"),
      null,
      "registered views are not iframed",
    );
  }
  const viewBox = () =>
    page.$eval(C + ".wb-viewport", (el) => el.getAttribute("viewBox"));
  async function fittedCanvas() {
    await page.waitForFunction((scope) => {
      const svg = document.querySelector(scope + "svg.wb-plan");
      if (!svg) return false;
      const rect = svg.getBoundingClientRect();
      return rect.height > 0 && Math.abs(svg.viewBox.baseVal.width - rect.width) < 1 &&
        Math.abs(svg.viewBox.baseVal.height - rect.height) < 1;
    }, {}, C);
    const fit = await page.$eval(C + "svg.wb-plan", (svg) => {
      const viewport = svg.querySelector(".wb-viewport");
      const box = viewport.viewBox.baseVal;
      const scale = svg.getScreenCTM().a;
      const zoom = svg.getBoundingClientRect().width / box.width;
      return { scale, zoom, top: box.y, y: viewport.y.baseVal.value,
        percent: svg.parentElement.querySelector('[data-action="reset"]').textContent,
        reportDetails: !!svg.querySelector(".wb-details") };
    });
    assert.ok(Math.abs(fit.scale - 1) < 0.01, "no second scaling of the chart inside a fixed SVG");
    assert.ok(fit.zoom >= 0.75 && fit.zoom <= 1, JSON.stringify(fit));
    assert.equal(fit.percent, Math.round(fit.zoom * 100) + "%");
    assert.equal(fit.top, -28);
    assert.equal(fit.y, 0);
    assert.equal(fit.reportDetails, false, "the workspace inspector owns operator details");
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
  async function exportPlan(format) {
    // The shared Export pull-down: its items name the format.
    await page.click(".plan-exports button.export-button");
    await page.click(`.plan-exports [role=menuitem][data-format="${format.toLowerCase()}"]`);
  }
  async function inspectorSection(section) {
    await page.click(`.plan-fold .segmented[aria-label="Inspector section"] button[data-value="${section}"]`);
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

  await page.waitForSelector('.home [aria-label="Get started"]');
  assert.ok(await page.$('.home [aria-label="Open plans"]') === null, "no plan rows in an empty workspace");
  assert.ok(await page.$('.topbar button[aria-label="Import plan"]'));
  // Home's one Import plan lives in the top bar; the empty Recent work
  // offers the sample instead of a second Import plan.
  assert.deepEqual((await page.$$eval('button', (els) => els.filter((e) => e.textContent.trim() === "Import plan")
    .map((e) => e.closest('.topbar') ? "top bar" : e.closest('[aria-label="Get started"]') ? "get started" : "elsewhere"))).sort(),
    ["top bar"]);
  assert.equal(await page.$('.topbar button[aria-label="New query tab"]'), null, "New query tab belongs to the Query editor");
  await button("Query editor", ".main-nav ");
  await page.waitForSelector('.editor-toolbar');
  for (const width of [390, 320]) {
    await page.setViewport({ width, height: 844 });
    const clipped = await page.$$eval('.editor-toolbar button', (els) => els.filter((el) => {
      const r = el.getBoundingClientRect();
      if (!r.width || !r.height) return false;
      const toolbar = el.closest('.editor-toolbar').getBoundingClientRect();
      return r.left < toolbar.left - 1 || r.right > toolbar.right + 1 ||
        r.top < toolbar.top - 1 || r.bottom > toolbar.bottom + 1;
    }).map((el) => el.textContent || el.getAttribute('aria-label')));
    assert.deepEqual(clipped, [], `all editor actions visible at ${width} px`);
    // The editor has no status bar of its own; the window's stays inside the
    // viewport with a long phrase and an unbroken server name.
    assert.equal(await page.$('.editor-statusbar'), null);
    const statusFits = await page.$eval('.window-statusbar[aria-label="Workspace status"]', (bar) => {
      const state = bar.querySelector('[data-status="connection"]');
      const original = state.textContent;
      state.textContent = 'Session open on ' + 'replica_'.repeat(30);
      const rect = bar.getBoundingClientRect();
      const fits = rect.left >= 0 && rect.right <= innerWidth + 1 && document.documentElement.scrollWidth <= innerWidth;
      state.textContent = original;
      return fits;
    });
    assert.equal(statusFits, true, `status text wraps inside ${width} px`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  }
  await page.setViewport({ width: 1440, height: 1050 });
  await button("Home", ".main-nav ");
  await page.waitForSelector('.home [aria-label="Get started"]');
  await button("Explore sample plan");
  await page.waitForSelector(".plan-heading");
  assert.equal(await page.$(".save-state"), null, "sample loading is not an investigation edit");
  assert.match(await page.$eval(".window-statusbar", e => e.textContent), /Sample plan/);
  await preview();
  assert.equal(await page.$eval(".plan-inspector .inspector-title h3", (el) => el.textContent), "Select an operator");
  assert.equal(await page.$(C + ".metric-panel"), null, "the operator list does not consume canvas height");
  assert.ok(await page.$(M + ".metric-panel"), "runtime metric rows are docked in the inspector");
  assert.equal(await page.$eval(".primary-finding", (el) => el.open), false, "summary starts collapsed");
  await page.click(".primary-finding > summary");
  assert.equal(await page.$eval(".primary-finding", (el) => el.open), true);
  await page.click(".primary-finding > summary");
  if (await page.$(".sql-card")) {
    await page.click(".sql-card > summary");
    assert.ok(await page.$eval(".sql-card pre", (el) => el.textContent.trim()));
    assert.equal(await page.$eval(".sql-row > button", (el) => el.textContent.trim()), "Copy SQL");
    await page.click(".sql-card > summary");
  }
  assert.ok(await page.$(C + '.chart-toolbar input[type="search"]'), "search shares the chart toolbar");
  assert.ok(await page.$(C + '.chart-toolbar [data-action="fit"]'), "zoom controls share the chart toolbar");
  await page.focus('.chart-views button[data-value="workbench"]');
  await page.keyboard.press("ArrowRight");
  await preview();
  assert.equal(await page.$eval('.chart-views button[data-value="flamegraph"]', (el) => el === document.activeElement), true, "view switching retains segmented-control focus");
  await page.keyboard.press("ArrowLeft");
  await preview();
  assert.equal(await page.$eval('.chart-views button[data-value="workbench"]', (el) => el === document.activeElement), true);
  await inspectorSection("operators");
  await page.locator(M + ".metric-row").click();
  assert.ok(await page.$(".operator-detail"));
  await page.focus(M + ".metric-row:last-of-type");
  await page.keyboard.press("Enter");
  assert.ok(await page.$eval(M + ".metric-row.is-selected", (el) => el === document.activeElement), "selecting a metric row retains keyboard focus");
  await page.locator(".operators .search input").fill("not-an-operator");
  assert.equal((await page.$$(M + ".metric-row:not([hidden])")).length, 0);
  assert.ok(await page.$(M + ".metric-empty:not([hidden])"));
  await page.locator(".operators .search input").fill("");
  for (const label of [
    "Visual Explain",
    "Execution tree",
    "Bar chart",
    "Flame graph",
  ]) {
    await button(label, ".chart-views ");
    await preview();
    assert.equal(
      await page.$eval(".plan-canvas .plan-chart", (el) => el.dataset.view),
      { "Visual Explain": "workbench", "Execution tree": "tree",
        "Bar chart": "bargraph", "Flame graph": "flamegraph" }[label],
    );
    if (label === "Visual Explain") {
      await page.waitForSelector(C + 'svg.wb-plan[data-wired="true"]');
      await fittedCanvas();
      const initial = await viewBox();
      await page.click(C + '[data-action="in"]');
      assert.notEqual(await viewBox(), initial);
      await page.click(C + '[data-action="fit"]');
      assert.equal(await viewBox(), initial);
      await page.locator(C + 'input[type="search"]').fill("not-an-operator");
      assert.equal(
        await page.$$eval(C + ".wb-node:not(.dim)", (nodes) => nodes.length),
        0,
      );
      await page.$eval(C + 'input[type="search"]', (el) => {
        el.value = "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
      });
      assert.equal(
        await page.$$eval(C + ".wb-node.dim", (nodes) => nodes.length),
        0,
      );
      // Chart -> page: keyboard selection drives the inspector and table.
      const chosen = await page.$eval(C + ".wb-node:last-of-type", (el) => {
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
      assert.ok(
        (await page.$eval(M + ".metric-row.is-selected", (el) => el.textContent))
          .includes(chosen),
      );
      // Page -> chart: the operator table selects the node and metric row.
      const picked = await page.$$eval(M + ".metric-row", (rows) => {
        rows[1].click();
        return rows[1].textContent;
      });
      await page.waitForFunction(
        (c) => {
          const node = document.querySelector(c + ".wb-node.selected");
          const row = document.querySelector(".plan-fold .inspector-metrics .metric-row.is-selected");
          return node && row && row.dataset.nodeId === node.dataset.nodeId;
        },
        {},
        C,
      );
      assert.ok(
        picked.includes(
          await page.$eval(C + ".wb-node.selected", (el) => el.dataset.label),
        ),
      );
      assert.equal(
        await page.$$eval(C + ".wb-node.selected", (nodes) => nodes.length),
        1,
      );
      // Metric list -> page.
      const metricLabel = await page.$eval(
        M + ".metric-row:last-of-type",
        (el) => {
          el.click();
          return el.querySelector(".metric-name").textContent;
        },
      );
      await page.waitForFunction(
        (label) =>
          label.startsWith(
            document.querySelector(".operator-detail h3").textContent,
          ),
        {},
        metricLabel,
      );
      await inspectorSection("measurements");
      assert.ok(await page.$('.plan-inspector .inspector-fields'), "measurements stay available for the selected node");
      await inspectorSection("findings");
      assert.ok(await page.$('.plan-inspector .inspector-block'), "findings have a dedicated section");
      await inspectorSection("lessons");
      assert.equal(await page.$eval('.plan-fold [data-value="lessons"]', (el) => el.getAttribute("aria-pressed")), "true");
      await inspectorSection("operators");
      assert.ok(await page.$(M + ".metric-row.is-selected"), "switching inspector sections preserves linked selection");
      // Exercise actual drag panning, then restore the overview.
      // The card's scroll margin keeps the controls clear of the sticky
      // top bar (a covered button would swallow the click).
      await page.$eval(".plan-canvas", (el) => {
        el.scrollIntoView({ block: "start" });
        el.querySelector(".plan-chart").scrollTop = 0;
      });
      // Start inside the responsive workspace canvas, below its HTML legend.
      const start = await page.$eval(C + "svg.wb-plan", (svg) => {
        const rect = svg.getBoundingClientRect();
        return { x: rect.left + 25, y: rect.top + 40 };
      });
      await page.mouse.move(start.x, start.y);
      await page.mouse.down();
      await page.mouse.move(start.x + 60, start.y + 30, { steps: 6 });
      await page.mouse.up();
      assert.notEqual(await viewBox(), initial);
      await page.click(C + '[data-action="fit"]');
      assert.equal(await viewBox(), initial);
      for (const width of [390, 320]) {
        await page.setViewport({ width, height: 844 });
        await fittedCanvas();
        await page.evaluate(() => window.scrollTo(0, 0));
        const phonePosition = await page.$eval(C + 'svg.wb-plan', svg => ({
          top: svg.getBoundingClientRect().top, height: innerHeight,
          summaryTop: document.querySelector('.primary-finding').getBoundingClientRect().top,
          controlsHeight: document.querySelector('.plan-fold .chart-toolbar .wb-controls').getBoundingClientRect().height,
          legendDisplay: getComputedStyle(document.querySelector('.plan-fold .wb-legend')).display,
          popoverDisplay: getComputedStyle(document.querySelector('.plan-fold .wb-legend-popover')).display,
        }));
        assert.ok(phonePosition.top <= phonePosition.height / 3, JSON.stringify(phonePosition));
        assert.ok(phonePosition.summaryTop < phonePosition.top, 'the first finding remains visible above the phone chart');
        assert.ok(phonePosition.controlsHeight <= 40, `Visual Explain phone controls fit on one row: ${phonePosition.controlsHeight}`);
        assert.equal(phonePosition.legendDisplay, 'none', 'inline legend hides on phone');
        assert.equal(phonePosition.popoverDisplay, 'block', 'compact Legend popover shows on phone');
        if (width === 390) console.log(JSON.stringify({ viewport: '390x844', beforeSvgTop: 467,
          afterSvgTop: phonePosition.top, pixelsRecovered: 467 - phonePosition.top }));
        // The narrowest layouts (PlanChart MIN_CHART_WIDTH): a narrower pane
        // scrolls sideways instead of shrinking the text.
        const minWidths = { tree: 489, flamegraph: 360, bargraph: 480 };
        for (const view of ['tree', 'flamegraph', 'bargraph']) {
          await page.select('select[aria-label="Chart view"]', view);
          await page.waitForSelector(`.plan-chart[data-view="${view}"] .chart-scroll > svg`);
          const scroll = `.plan-chart[data-view="${view}"] .chart-scroll`;
          await page.waitForFunction((selector, min) => {
            const box = document.querySelector(selector);
            const drawn = Number(box?.querySelector('svg')?.getAttribute('width'));
            return box.clientWidth > 0 && Math.abs(drawn - Math.max(min, box.clientWidth)) <= 1;
          }, {}, scroll, minWidths[view]);
          const geometry = await page.$eval(scroll, box => ({
            width: box.clientWidth, full: box.scrollWidth,
            svg: box.querySelector('svg').getBoundingClientRect().width,
            scale: box.querySelector('svg').getScreenCTM().a,
          }));
          assert.ok(geometry.width <= width && geometry.svg >= minWidths[view] - 1 && geometry.scale >= .99,
            `${view} keeps readable operator labels on a ${width}px phone: ${JSON.stringify(geometry)}`);
          if (geometry.full > geometry.width) {
            await page.$eval(scroll, box => { box.focus(); box.scrollLeft = 40; });
            assert.ok(await page.$eval(scroll, box => box.scrollLeft >= 39), `${view} can scroll to its remaining operators`);
          }
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        }
        await page.select('select[aria-label="Chart view"]', 'workbench');
        await fittedCanvas();
        await page.click('button[aria-label="Chart options"]');
        await page.waitForSelector('.popover[aria-label="Chart options"]');
        assert.ok(await page.$('.popover select[aria-label="Phone operator metric"]'));
        assert.ok(await page.$('.popover select[aria-label="Phone focused branch"]'));
        await page.keyboard.press('Escape');
        assert.equal(await page.$eval('button[aria-label="Chart options"]', el => el === document.activeElement), true);
        await page.click('button[aria-label="Plan actions"]');
        await page.waitForSelector('.popover[aria-label="Plan actions"]');
        assert.equal(await page.$$eval('button', buttons => buttons.filter(button =>
          button.textContent.trim() === 'Import plan' && button.getBoundingClientRect().width > 0).length), 1,
          'phone has one visible Import plan action');
        assert.ok(await page.$('.popover[aria-label="Plan actions"] [aria-label="Plan measurements"]'));
        await page.keyboard.press('Escape');
        const clipped = await page.$$eval(C + '.chart-toolbar button, ' + C + '.chart-toolbar input, ' + C + '.chart-toolbar select', (els) => els.filter((el) => {
          const rect = el.getBoundingClientRect();
          if (!rect.width || !rect.height) return false;
          const toolbar = el.closest('.chart-toolbar').getBoundingClientRect();
          return rect.left < toolbar.left - 1 || rect.right > toolbar.right + 1 ||
            rect.top < toolbar.top - 1 || rect.bottom > toolbar.bottom + 1;
        }).map((el) => el.textContent || el.getAttribute('aria-label')));
        assert.deepEqual(clipped, [], `all chart controls visible at ${width} px`);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      }
      await page.setViewport({ width: 1440, height: 1050 });
      await fittedCanvas();
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({
        path: path.join(directory, "workbench.png"),
        fullPage: true,
      });
      await exportPlan("HTML");
      const report = await downloaded("sample-plan-report.html");
      assert.match(report, /wb-plan|mfc-mount/);
      const exported = await browser.newPage();
      exported.on("pageerror", (error) => errors.push(String(error)));
      await exported.goto(
        "file://" + path.join(directory, "sample-plan-report.html"),
      );
      await exported.waitForSelector('svg.wb-plan[data-wired="true"]');
      assert.equal(
        await exported.$$eval(".wb-node", (nodes) => nodes.length),
        await page.$$eval(C + ".wb-node", (nodes) => nodes.length),
      );
      await exported.click('[data-action="in"]');
      await exported.close();
      await fs.unlink(path.join(directory, "sample-plan-report.html"));
    } else {
      assert.equal(
        await page.$(C + ".fg-title, " + C + ".fg-details, " + C + ".tree-details, " + C + "#bar-details"),
        null,
        `${label} omits standalone title and details box inside workspace`,
      );
      const topGap = await page.$eval(C + ".chart-scroll", (scroll) => {
        const toolbar = scroll.closest(".mfc-chart").querySelector(".chart-toolbar");
        return Math.abs(scroll.getBoundingClientRect().top - toolbar.getBoundingClientRect().bottom);
      });
      assert.ok(topGap <= 2, `${label} top-aligns directly below toolbar (gap=${topGap})`);
    }
    if (label === "Execution tree") {
      // A chart drawn for one width is drawn again when its pane resizes:
      // shrunk to the 720 px window (sidebar shown) the numbers stay on
      // screen with no sideways scroll, shortened labels carry a tooltip, and
      // the search, the selection, a row collapsed with its own toggle and
      // the focused row survive the redraw.
      const drawn = () => page.$eval(C + ".chart-scroll", (box) => ({
        svg: Number(box.querySelector("svg").getAttribute("width")),
        client: box.clientWidth, scroll: box.scrollWidth,
        numbers: Math.max(...Array.from(box.querySelectorAll(".row-time, .row-pct"), (t) => t.getBoundingClientRect().right)),
        right: box.getBoundingClientRect().left + box.clientWidth,
      }));
      const desktop = page.viewport();
      const wide = await drawn();
      assert.ok(Math.abs(wide.svg - wide.client) <= 1, `the tree fills its pane: ${JSON.stringify(wide)}`);
      await page.locator(C + ".mfc-toolbar input").fill("scan");
      const matching = await page.$$eval(C + ".tree-row:not(.mfc-dim)", (rows) => rows.map((row) => row.dataset.nodeId));
      assert.ok(matching.length > 0);
      const picked = await page.$eval(C + '.tree-row[data-has-children="1"]:not(.mfc-dim)', (row) => {
        row.focus();
        row.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        row.querySelector(".toggle").dispatchEvent(new MouseEvent("click", { bubbles: true }));
        return row.dataset.nodeId;
      });
      assert.equal(await page.$eval(C + `.tree-row[data-node-id="${picked}"]`, (row) => row.getAttribute("aria-expanded")), "false");
      await page.setViewport({ width: 720, height: 520 });
      await page.waitForFunction((c, before) => {
        const svg = document.querySelector(c + ".chart-scroll > svg");
        return !!svg && Number(svg.getAttribute("width")) < before;
      }, {}, C, wide.svg);
      const narrow = await drawn();
      assert.ok(narrow.scroll <= narrow.client && Math.abs(narrow.svg - narrow.client) <= 1, `no sideways scroll at 720 px: ${JSON.stringify(narrow)}`);
      assert.ok(narrow.numbers <= narrow.right + 0.5, `times and shares stay visible at 720 px: ${JSON.stringify(narrow)}`);
      assert.ok(await page.$$eval(C + ".tree-row > title", (titles) => titles.length) > 0, "shortened labels have a tooltip");
      assert.equal(await page.$eval(C + ".mfc-toolbar input", (el) => el.value), "scan", "the search survives the redraw");
      assert.deepEqual(await page.$$eval(C + ".tree-row:not(.mfc-dim)", (rows) => rows.map((row) => row.dataset.nodeId)), matching);
      assert.equal(await page.$eval(C + ".tree-row.is-selected", (row) => row.dataset.nodeId), picked, "the selection survives the redraw");
      assert.equal(await page.$eval(C + `.tree-row[data-node-id="${picked}"]`, (row) => row.getAttribute("aria-expanded")), "false", "a collapsed row stays collapsed");
      assert.equal(await page.evaluate(() => document.activeElement?.closest?.("[data-node-id]")?.getAttribute("data-node-id")), picked,
        "focus returns to the same row");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.setViewport(desktop);
      await page.waitForFunction((c, before) => Number(document.querySelector(c + ".chart-scroll > svg")?.getAttribute("width")) === before, {}, C, wide.svg);
      // Focus in the chart's search stays there through a redraw.
      await page.focus(C + ".mfc-toolbar input");
      await page.setViewport({ width: 720, height: 520 });
      await page.waitForFunction((c, before) => Number(document.querySelector(c + ".chart-scroll > svg")?.getAttribute("width")) < before, {}, C, wide.svg);
      assert.equal(await page.$eval(C + ".mfc-toolbar input", (el) => el === document.activeElement), true, "the search keeps focus");
      await page.setViewport(desktop);
      await page.waitForFunction((c, before) => Number(document.querySelector(c + ".chart-scroll > svg")?.getAttribute("width")) === before, {}, C, wide.svg);
      await page.$eval(C + ".mfc-toolbar input", (el) => {
        el.value = "";
        el.dispatchEvent(new Event("input", { bubbles: true }));
      });
    }
    if (label === "Flame graph") {
      // A zoomed flame graph stays zoomed to the same frame when redrawn for a new width.
      const desktop = page.viewport();
      const before = await page.$eval(C + ".chart-scroll > svg", (svg) => Number(svg.getAttribute("width")));
      const frame = await page.$eval(C + ".frames > g[data-node-id]:nth-child(2)", (g) => {
        g.dispatchEvent(new MouseEvent("click", { bubbles: true }));
        return g.dataset.nodeId;
      });
      assert.equal(await page.$eval(C + "svg.mfc-flame", (svg) => svg.getAttribute("data-zoom")), frame);
      await page.setViewport({ width: 720, height: 520 });
      await page.waitForFunction((c, width) => Number(document.querySelector(c + ".chart-scroll > svg")?.getAttribute("width")) < width, {}, C, before);
      assert.equal(await page.$eval(C + "svg.mfc-flame", (svg) => svg.getAttribute("data-zoom")), frame, "the zoom survives the redraw");
      assert.ok(await page.$(C + ".fg-reset:not(.fg-off)"), "Reset zoom stays offered");
      assert.equal(await page.$eval(C + ".chart-scroll", (box) => box.scrollWidth <= box.clientWidth), true, "no sideways scroll at 720 px");
      await page.setViewport(desktop);
      await page.waitForFunction((c, width) => Number(document.querySelector(c + ".chart-scroll > svg")?.getAttribute("width")) === width, {}, C, before);
      await page.$eval(C + ".fg-reset", (button) => button.click());
      assert.equal(await page.$eval(C + "svg.mfc-flame", (svg) => svg.hasAttribute("data-zoom")), false);
    }
    // Exercise the shared renderers' initialization and search navigation.
    // In the page, Ctrl+F and the n/N keys belong to the chart only while
    // focus is inside it; browser find keeps working everywhere else.
    if (label === "Bar chart") {
      const findOutside = await page.evaluate(() => {
        const event = new KeyboardEvent("keydown", {
          key: "f", ctrlKey: true, bubbles: true, cancelable: true,
        });
        document.querySelector(".operators .search input").dispatchEvent(event);
        return event.defaultPrevented;
      });
      assert.equal(findOutside, false, "browser find is not hijacked");
      await page.$eval(C + ".mfc-chart", (el) => el.focus());
      await page.keyboard.down("Control");
      await page.keyboard.press("f");
      await page.keyboard.up("Control");
      assert.ok(
        await page.$eval(
          C + ".mfc-toolbar input",
          (el) => el === document.activeElement,
        ),
        "Ctrl+F inside the chart focuses its search",
      );
      await page.keyboard.type("scan");
      await page.keyboard.press("Escape");
      assert.equal(await page.$eval(C + '.mfc-toolbar input', (el) => el.value), "", "Escape clears the relocated search");
      await page.keyboard.type("scan");
      await page.$eval(C + ".mfc-chart", (el) => el.focus());
      await page.keyboard.press("n");
      await page.keyboard.press("N");
      assert.ok(
        await page.$(C + '.bar[style*="stroke"]'),
        "Chart search highlights matches",
      );
    }
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "plan.png"),
    fullPage: true,
  });
  await exportPlan("JSON");
  const analysis = JSON.parse(await downloaded("sample-plan-analysis.json"));
  assert.ok(analysis.schema_version);
  assert.ok(analysis.plan_summary.operator_count > 0);
  // /api/analyze leaves the SQL out of each teach hook; Export JSON restores
  // it, so the file is the sidecar the CLI writes (teach hooks with query_sql).
  const sidecar = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c", `
import json, sys
from myflames.output_sidecar import build_sidecar
from myflames.parser import analyze_plan
from myflames.plan_input import _plan
text, root, query, engine = _plan({"plan": sys.stdin.read()})
print(json.dumps(build_sidecar(root, analyze_plan(root), source_type="file", engine=engine, query_raw=query)))
`], { cwd: root, input: JSON.stringify(JSON.parse(await fs.readFile(path.join(root, "apps/workspace/src/example.json"), "utf8"))) }));
  assert.ok(sidecar.teach_hooks.length > 0 && sidecar.teach_hooks.every((h) => h.query_sql === sidecar.query.raw));
  for (const value of [analysis, sidecar]) delete value.generated_at;
  assert.deepEqual(analysis, sidecar, "Export JSON writes the same sidecar as before");
  assert.deepEqual(analysis.teach_hooks.map(Object.keys), sidecar.teach_hooks.map(Object.keys), "hook keys keep their order");
  await exportPlan("HTML");
  assert.match(
    await downloaded("sample-plan-report.html"),
    /application\/ld\+json/,
  );

  // Teach must work inside the workspace without discarding the loaded plan.
  await button("Learn", ".main-nav ");
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
  const lessonView = '.segmented[aria-label="Lesson view"]';
  assert.equal(await page.$(lessonView), null, "lessons have one view");
  const btreeFrame = await (await page.$('iframe[title^="Lesson: B"]')).contentFrame();
  await btreeFrame.waitForSelector("#btn-play");
  await button("Download lesson");
  assert.match(await downloaded("btree.html"), /teachRuntime/);
  await button("Previous lesson");
  await page.waitForSelector('iframe[title^="Lesson: Full"]');
  assert.equal(await page.$(lessonView), null, "lessons have one view");
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
  await button("Plans", ".main-nav ");
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 1);

  await button("Import plan", ".plans-bar ");
  await page.waitForSelector("dialog[open]");
  assert.match(await page.$eval("#import-format-hint", e => e.textContent), /EXPLAIN ANALYZE FORMAT=JSON output, or a myflames capture \.json/);
  assert.equal(await page.$eval("dialog", e => e.getAttribute("aria-describedby")), "import-format-hint");
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
  await button("Import plan", ".plans-bar ");
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
  await button("Compare", ".plans-bar ");
  await page.waitForSelector(".comparison-row");
  // Each plan's inspector opens for its selection only, so two empty panes
  // never push Operator changes below the fold.
  assert.equal(await page.$$eval(".compare-pane .plan-inspector", (els) => els.length), 0, "no empty inspectors before a selection");
  assert.equal(await page.$(".compare-pane .plan-chart .metric-panel"), null, "comparison lists are docked too");
  await page.click('.compare-tools [data-value="tree"]');
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.compare-pane .plan-chart')).every((el) => el.dataset.view === "tree"));
  await page.select('select[aria-label="Comparison operator metric"]', "rows");
  await page.waitForFunction(() => Array.from(document.querySelectorAll('.compare-pane .metric-panel h2')).every((el) => el.textContent.includes("rows")));
  // A bar chart is laid out for its pane's width and redrawn when the window
  // narrows or widens, so its value labels stay inside the pane.
  await page.click('.compare-tools [data-value="bargraph"]');
  await page.waitForFunction(() => document.querySelectorAll('.compare-pane svg.mfc-bar').length === 2);
  const barFits = () => Array.from(document.querySelectorAll('.compare-pane svg.mfc-bar')).map((svg) =>
    ({ svg: Number(svg.getAttribute("width")), pane: svg.closest(".compare-pane").clientWidth }));
  const wideBars = await page.evaluate(barFits);
  assert.ok(wideBars.every((b) => b.svg <= b.pane && b.svg > 560), `Compare bar charts fill their panes: ${JSON.stringify(wideBars)}`);
  await page.setViewport({ width: 720, height: 520 });
  await page.waitForFunction(() => Array.from(document.querySelectorAll(".compare-pane svg.mfc-bar")).every((svg) =>
    Number(svg.getAttribute("width")) <= svg.closest(".compare-pane").clientWidth), { timeout: 5000 });
  await page.setViewport({ width: 1440, height: 1050 });
  await page.waitForFunction((before) => {
    const widths = Array.from(document.querySelectorAll('.compare-pane svg.mfc-bar')).map((svg) => Number(svg.getAttribute("width")));
    return widths.length === 2 && widths.every((w, i) => Math.abs(w - before[i]) < 16);
  }, { timeout: 5000 }, wideBars.map((b) => b.svg));
  await page.click('.compare-tools [data-value="workbench"]');
  await page.waitForSelector('.compare-pane .wb-node');
  const linked = await page.$eval('.comparison-row', (el) => { el.click(); return el.textContent; });
  assert.ok(linked);
  await page.waitForFunction(() => !!document.querySelector('.compare-pane .plan-inspector .operator-detail'));
  assert.ok(await page.$$eval(".compare-pane .plan-inspector", (els) => els.length) >= 1, "a selected pair opens its plans' inspectors");
  await exportPlan("JSON");
  assert.equal(
    JSON.parse(await downloaded("comparison.json")).schema_version,
    "compare-1.0",
  );
  await exportPlan("HTML");
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
  assert.equal((await page.$$(".chart-frame, .plan-chart")).length, 0);
  await button("Import plan", ".plans-bar ");
  const input = await page.$('input[type="file"]');
  await input.uploadFile(
    path.join(root, "test/mysql-explain-json-sample.json"),
  );
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await preview();
  assert.equal((await page.$$(".plan-item")).length, 2);
  // Autosave keeps the workspace across a reload (no leave-page prompt);
  // Start fresh, without a backup, returns to an empty workspace.
  await page.waitForFunction(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    return (await r.json()).state?.plans.length === 2;
  });
  let prompted = false;
  page.once("dialog", (dialog) => {
    prompted = true;
    dialog.accept();
  });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(".session-notice");
  assert.equal(prompted, false);
  assert.equal((await page.$$(".plan-item")).length, 2);
  await button("Start fresh", ".session-notice ");
  await button("Start fresh without a backup", ".session-notice ");
  await page.waitForSelector(".home");
  assert.equal((await page.$$(".plan-item")).length, 0);
  await page.evaluate(
    (raw) => {
      const transfer = new DataTransfer();
      transfer.items.add(
        new File([raw], "dropped-plan.json", { type: "application/json" }),
      );
      document
        .querySelector(".home")
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
  await button("Import plan", ".plans-bar ");
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
