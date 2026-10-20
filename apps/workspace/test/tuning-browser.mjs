// MySQL Tuning in the workspace: the scorecard scores only measured checks.
// Nothing loaded shows "Not enough data"; Health alone shows unknowns and a
// provisional score with no grade; the Full analysis and Parameters measure
// more. Checks open in the inspector with the evidence first and the
// statements to copy; the Full analysis tab shows the report. Mocked
// endpoints only (perf-harness.mjs); screenshots go to PERF_SHOTS when set.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause } from "./perf-harness.mjs";

const report = await load("report-mysql-8.4.json");
const health = await load("health-mysql-8.4-full.json");
const parameters = await load("parameters-mysql.json");
parameters.parameters.items.push({
  ...parameters.parameters.items.find((i) => i.name === "slow_query_log"),
  name: "require_secure_transport", value: "OFF", default: "OFF", checks: [], description: "Require TLS or a local socket.",
});

// Health reads itself once when its view mounts (Health.tsx); until the test
// turns it on, that read fails, so the scorecard starts with nothing measured.
let healthOn = false;
const h = await open("/api/live/report", "tuning-browser", {
  "/api/live/health": () => (healthOn ? { status: 200, body: health }
    : { status: 400, body: { error: "Could not read the server status: not yet (test)." } }),
  "/api/live/config": { status: 200, body: parameters },
});
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: report };
const shots = process.env.PERF_SHOTS || "";

async function openTuning() {
  // The place's sidebar entry (its label belongs to the shell; match the word).
  const clicked = await page.evaluate(() => {
    const b = [...document.querySelectorAll(".main-nav button")].find((x) => /Tuning/.test(x.textContent || ""));
    b?.click();
    return !!b;
  });
  assert.ok(clicked, "a Tuning entry in the sidebar");
  await page.waitForSelector(".server-view[data-view=tuning] .tuning", { visible: true });
}
async function shoot(name) {
  if (!shots) return;
  for (const scheme of ["light", "dark"]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
    for (const width of [1440, 390]) {
      await page.setViewport({ width, height: width === 390 ? 844 : 1000 });
      await pause(250);
      await page.screenshot({ path: path.join(shots, `${name}.${width}.${scheme}.png`), fullPage: true });
    }
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
}
const coverage = () => text(".tuning-score-coverage");
const row = (key) => `.tuning-check[data-check="${key}"]`;

try {
  // ---- Nothing loaded: no score, every check unknown ----
  await openTuning();
  // Tuning is a place of its own: full width, no Server list of views, the
  // window footer with the connection's write safety.
  assert.equal(await text('.main-nav button[data-place="tuning"]'), "Tuning");
  assert.ok(await page.$(".server-place.is-tuning-place"), "the Tuning place");
  assert.equal(await page.$(".server-place.is-tuning-place .source-list"), null, "no Server sub-navigation");
  assert.equal(await page.$(".server-place.is-tuning-place .place-toolbar"), null, "no Server place toolbar");
  assert.equal(await text(".topbar-title"), "Tuning");
  await page.waitForSelector('.window-statusbar [data-status="safety"]');
  const content = await page.$eval(".server-place.is-tuning-place .server-content", (e) => e.getBoundingClientRect().left);
  const sidebar = await page.$eval(".sidebar", (e) => e.getBoundingClientRect().right);
  assert.ok(content - sidebar <= 1, `Tuning starts at the sidebar edge (${content} vs ${sidebar})`);
  assert.equal(await text(".tuning-score-label"), "Not enough data");
  assert.equal(await text(".tuning-score-number"), "—");
  assert.equal(await text(".tuning-grade"), "No score");
  assert.match(await coverage(), /^0 of 19 checks measured/);
  assert.deepEqual(await texts(".tuning-category-name"), [
    "Buffer pool and redo", "Queries", "Connections and threads", "Tables and caches", "Security",
  ]);
  assert.deepEqual(await texts(".tuning-category-none"), Array(5).fill("Not measured"));
  assert.match(await text(".tuning-empty"), /Nothing measured yet/);
  assert.equal(state.seen.length, 0, "nothing was read on open");
  assert.ok(!state.other.some((r) => r.path === "/api/live/top"), "no Top statements read on open");

  // ---- Health only: unknowns and a provisional score, no grade ----
  healthOn = true;
  await h.view("health");
  await h.refresh();
  await page.waitForSelector('.server-view[data-view=health] li[data-diagnostic="buffer_pool"]');
  await openTuning();
  await page.waitForFunction(() => /^\d+$/.test(document.querySelector(".tuning-score-number")?.firstChild?.textContent || ""));
  assert.equal(await text(".tuning-grade"), "Provisional");
  const measured = Number((await coverage()).match(/^(\d+) of 19/)[1]);
  assert.ok(measured >= 10 && measured < 19, `measured ${measured}`);
  assert.equal(await text(".tuning-score-label"), "Problems found");
  assert.match(await text(".tuning-score-hint"), /Not graded until at least half the checks, and one in each category/);
  assert.equal(await text('.tuning-category[data-category="security"] .tuning-category-score'), "Not measured");
  // Needs attention lists the buffer pool problem with its measured value.
  assert.ok(await page.$(`${row("buffer_pool")} .status-pill.is-problem`));
  assert.match(await text(`${row("buffer_pool")} .row-value`), /read from disk/);
  // Not measured shows each unknown with its reason.
  await page.click('.tuning-filters .segmented button[data-value="unknown"]');
  await page.waitForSelector(row("no_password"));
  assert.equal(await text(`${row("no_password")} .tuning-needs`), "Needs Full analysis");
  assert.equal(await text(`${row("secure_transport")} .tuning-needs`), "Needs Parameters");
  // The reason and the way to measure it are in the inspector.
  await h.select(row("no_password"));
  assert.match(await h.inspector(), /Run the full analysis to read the accounts/);
  assert.ok(await page.$(".view-inspector ::-p-text(Run full analysis)"));
  await page.click(".view-inspector .detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));
  assert.equal(await page.$eval(row("no_password"), (e) => e.dataset.status), "unknown");
  await shoot("tuning-health-only");

  // ---- A check in the inspector: evidence first, then what to do ----
  await page.click('.tuning-filters .segmented button[data-value="attention"]');
  await h.select(row("buffer_pool"));
  const inspector = await h.inspector();
  assert.match(inspector, /Measured/);
  assert.match(inspector, /Rule/);
  assert.match(inspector, /What to do/);
  assert.match(inspector, /SET PERSIST innodb_buffer_pool_size = <bytes>;/);
  assert.ok(await page.$(".view-inspector .perf-sql button"), "Copy");
  assert.ok(await page.$(".view-inspector ::-p-text(Open in Query editor)"), "Open in Query editor");
  await shoot("tuning-check-inspector");
  await page.click(".view-inspector .detail-back");
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-detail"));

  // Category filter: Security has nothing measured yet.
  await page.click('.tuning-category[data-category="queries"]');
  assert.equal(await page.$eval('.tuning-category[data-category="queries"]', (e) => e.getAttribute("aria-pressed")), "true");
  assert.ok((await texts(".tuning-group h3")).every((t) => t === "Queries"));
  await page.click('.tuning-category[data-category="queries"]');

  // ---- Run the full analysis from the scorecard: more checks measured ----
  await page.click('[data-action="run-analysis"]');
  await page.waitForFunction((n) => Number((document.querySelector(".tuning-score-coverage")?.textContent || "").match(/^(\d+)/)?.[1]) > n, {}, measured);
  assert.equal(state.seen.length, 1, "one report read");
  // Parameters: require_secure_transport is read only there.
  await h.view("config");
  await page.waitForSelector(".server-view[data-view=config] .grouped-row, .server-view[data-view=config] tr", { timeout: 10000 });
  await openTuning();
  await page.waitForFunction(() => document.querySelector('.tuning-check[data-check="secure_transport"]')?.dataset.status === "watch" ||
    !document.querySelector('.tuning-check[data-check="secure_transport"]'));
  await page.click('.tuning-filters .segmented button[data-value="all"]');
  await page.waitForSelector(row("secure_transport"));
  assert.equal(await page.$eval(row("secure_transport"), (e) => e.dataset.status), "watch");
  assert.match(await text(".tuning-grade"), /^Grade [A-F]$/);
  await shoot("tuning-measured");

  // ---- Full analysis tab: the report, ranked ----
  await page.click("#tuning-tab-analysis");
  await page.waitForSelector("#tuning-panel-analysis .report-look");
  assert.equal(await page.$eval("#tuning-tab-analysis", (e) => e.getAttribute("aria-selected")), "true");
  assert.ok(await page.$(".server-view[data-view=tuning] [data-action=refresh]"), "the tab's Refresh runs it again");
  await shoot("tuning-full-analysis");
  await page.click("#tuning-tab-scorecard");
  await page.waitForSelector("#tuning-panel-scorecard");

  // Server opens its own view list again, never the Tuning view.
  await page.click('.main-nav button[data-place="server"]');
  await page.waitForSelector(".server-place:not(.is-tuning-place) .source-list [aria-current=page]");
  assert.equal(await page.$(".server-place:not(.is-tuning-place) .server-view[data-view=tuning]"), null, "Server does not embed Tuning");
  // The command palette keeps "mysql tuning" as a keyword for the place.
  await page.click(".command-trigger");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "mysql tuning");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => /^Tuning/.test(e.textContent.trim())));
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette"));
  // The Full analysis has one home: the Tuning place's second tab.
  await page.click(".command-trigger");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "full analysis");
  await page.waitForFunction(() => [...document.querySelectorAll(".command-option")].some((e) => /Full analysis/.test(e.textContent)));
  await page.keyboard.press("Enter");
  await page.waitForSelector(".server-place.is-tuning-place #tuning-panel-analysis");
  assert.equal(await page.$eval("#tuning-tab-analysis", (e) => e.getAttribute("aria-selected")), "true");
  assert.ok(await page.$('.main-nav button[data-place="tuning"].active'), "Tuning is the place on screen");
  await page.click("#tuning-tab-scorecard");
  await page.waitForSelector("#tuning-panel-scorecard");

  // No horizontal page scroll at 390 px. Tuning is a full-width place with no
  // Server list or Back button, so h.narrow()'s Server layout check does not apply.
  await page.setViewport({ width: 390, height: 844 });
  await pause(250);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 390 px`);
  assert.ok(await page.$eval(".server-view[data-view=tuning] .tuning", (e) => !!e.offsetParent), "the scorecard is on screen");
  assert.ok(!(await page.$(".server-place.is-tuning-place .source-list")), "no Server list of views in Tuning");
  if (shots) await page.screenshot({ path: path.join(shots, "tuning-narrow.png"), fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
  assert.deepEqual(h.errors, []);
  console.log("tuning browser suite: ok");
} finally {
  await h.close();
}
