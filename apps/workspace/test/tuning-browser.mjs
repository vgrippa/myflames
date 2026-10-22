// MySQL Tuning in the workspace: the scorecard scores only measured checks.
// Nothing loaded shows "Not enough data"; Health alone shows unknowns and a
// provisional score with no grade; the Full analysis and Parameters measure
// more. Checks open in the inspector with the evidence first and the
// statements to copy; the Full analysis tab shows the report. Mocked
// endpoints only (perf-harness.mjs); screenshots go to PERF_SHOTS when set
// (light and dark at 1440, 720 and 390 px).
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
    // 720 x 520 is the desktop app's minimum window.
    for (const width of [1440, 720, 390]) {
      await page.setViewport({ width, height: width === 390 ? 844 : width === 720 ? 520 : 1000 });
      await pause(250);
      await page.screenshot({ path: path.join(shots, `${name}.${width}.${scheme}.png`), fullPage: true });
    }
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
}
const coverage = () => text(".tuning-score-coverage");
const reason = () => text(".tuning-grade-reason");
const LEGEND = '.tuning-grade-help button[aria-label="How the grade works"]';
const LEGEND_PANEL = '.popover[role=dialog][aria-label="How the grade works"]';
// The grade legend, opened from the keyboard: its rows, the current one and its text.
async function legend() {
  await page.focus(LEGEND);
  await page.keyboard.press("Enter");
  await page.waitForSelector(`${LEGEND_PANEL} .tuning-grade-legend`);
  const rows = await page.$$eval(`${LEGEND_PANEL} tbody tr`, (els) =>
    els.map((e) => [e.querySelector("th").textContent, e.querySelector("td").firstChild.textContent]));
  const current = await page.$$eval(`${LEGEND_PANEL} tbody tr[aria-current=true]`, (els) => els.map((e) => e.dataset.grade));
  const all = await page.$eval(LEGEND_PANEL, (e) => e.textContent);
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(sel), {}, LEGEND_PANEL);
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "How the grade works", "focus returns to the ⓘ");
  return { rows, current, all };
}
const SCALE = [["A", "90 and above"], ["B", "80 to 89"], ["C", "70 to 79"], ["D", "50 to 69"], ["F", "below 50"]];
const row = (key) => `.tuning-check[data-check="${key}"]`;

try {
  // ---- Nothing loaded: no score, every check unknown ----
  await openTuning();
  // Tuning is a place of its own: full width, no Server list of views, the
  // window footer with the connection's write safety.
  assert.equal(await text('.main-nav button[data-place="tuning"]'), "Tuning");
  assert.ok(await page.$(".server-place.is-tuning-place"), "the Tuning place");
  assert.equal(await page.$(".server-place.is-tuning-place .source-list"), null, "no Server sub-navigation");
  assert.equal(await page.$(".server-place.is-tuning-place .view-switcher"), null, "its title switches no views");
  assert.equal(await page.$(".server-place.is-tuning-place .place-toolbar"), null, "no Server place toolbar");
  assert.equal(await text(".topbar-title"), "Tuning");
  await page.waitForSelector('.window-statusbar [data-status="safety"]');
  const content = await page.$eval(".server-place.is-tuning-place .server-content", (e) => e.getBoundingClientRect().left);
  const sidebar = await page.$eval(".sidebar", (e) => e.getBoundingClientRect().right);
  assert.ok(content - sidebar <= 1, `Tuning starts at the sidebar edge (${content} vs ${sidebar})`);
  assert.equal(await text(".tuning-score-label"), "Not enough data");
  assert.equal(await text(".tuning-score-number"), "—");
  assert.equal(await text(".tuning-grade"), "No score");
  assert.equal(await page.$(".tuning-grade-reason"), null, "no grade, no reason line");
  // What the grades mean: a keyboard-reachable ⓘ beside the grade, from GRADE_SCALE.
  {
    const l = await legend();
    assert.deepEqual(l.rows, SCALE);
    assert.deepEqual(l.current, [], "no grade yet");
    assert.match(l.all, /A problem caps the grade at C, whatever the score\./);
    assert.match(l.all, /No grade \(provisional\) until at least half the checks, and one in each category, are measured/);
  }
  // The view's ⓘ explains the score and the grade too.
  const about = await h.popover("About Tuning");
  assert.match(about, /Each measured check scores 100 when OK, 50 to watch and 0 for a problem/);
  assert.match(about, /Grades: A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50\. A problem caps the grade at C\./);
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
  assert.equal(await page.$(".tuning-grade-reason"), null, "provisional: the hint says why there is no grade");
  const measured = Number((await coverage()).match(/^(\d+) of 19/)[1]);
  assert.ok(measured >= 10 && measured < 19, `measured ${measured}`);
  assert.equal(await text(".tuning-score-label"), "Problems found");
  assert.match(await text(".tuning-score-hint"), /Not graded until at least half the checks, and one in each category.*Run the full analysis to grade\./);
  assert.equal(await page.$(".tuning-score-hint button"), null, "Run full analysis lives in the toolbar only");
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
  // Uncapped: no problem, so the grade is the score's range, and the legend marks it.
  {
    const score = Number(await page.$eval(".tuning-score-number", (e) => e.firstChild.textContent));
    const grade = (await text(".tuning-grade")).slice(-1);
    assert.doesNotMatch(await coverage(), /problem/);
    const [, range] = SCALE.find(([g]) => g === grade);
    assert.equal(await reason(), `Score ${score} is in the ${grade} range, ${range}.`);
    assert.equal(await page.$eval(".tuning-grade-reason", (e) => e.dataset.capped), "false");
    const l = await legend();
    assert.deepEqual(l.current, [grade]);
    assert.match(l.all, new RegExp(`${grade}${range} · this server`));
  }
  await shoot("tuning-measured");

  // Capped: one problem (an account without a password) keeps a high score at C,
  // and the reason line says so beside the grade.
  const capped = JSON.parse(JSON.stringify(report));
  Object.assign(capped.findings.find((f) => f.id === "checks.no_password"), { status: "problem", headline: "1" });
  state.fallback = { status: 200, body: capped };
  await page.click("#tuning-tab-analysis");
  await page.waitForSelector("#tuning-panel-analysis .report-look");
  await page.click(".server-view[data-view=tuning] [data-action=refresh]");
  for (let i = 0; i < 200 && state.seen.length < 2; i++) await pause(50);
  assert.equal(state.seen.length, 2, "the Full analysis read again");
  await page.click("#tuning-tab-scorecard");
  await page.waitForFunction(() => document.querySelector(".tuning-grade-reason")?.dataset.capped === "true");
  {
    const score = Number(await page.$eval(".tuning-score-number", (e) => e.firstChild.textContent));
    assert.ok(score >= 80, `score ${score}`);
    assert.equal(await text(".tuning-grade"), "Grade C");
    assert.equal(await reason(), `Capped at C: 1 check has a problem (${score} is in the ${score >= 90 ? "A" : "B"} range).`);
    assert.match(await coverage(), /1 problem/);
    const l = await legend();
    assert.deepEqual(l.current, ["C"]);
  }
  await shoot("tuning-capped");
  // At the 720 x 520 minimum window the legend ends above the fixed window
  // footer (which paints over popovers), so its last line stays readable.
  {
    await page.setViewport({ width: 720, height: 520 });
    await pause(250);
    await page.focus(LEGEND);
    await page.keyboard.press("Enter");
    await page.waitForSelector(`${LEGEND_PANEL} .tuning-grade-legend`);
    await pause(100);
    const fit = await page.$eval(LEGEND_PANEL, (p) => {
      const footer = document.querySelector(".window-statusbar").getBoundingClientRect().top;
      const last = p.querySelector("p:last-of-type");
      last.scrollIntoView({ block: "nearest" });
      const r = last.getBoundingClientRect();
      return { bottom: p.getBoundingClientRect().bottom, footer, hit: p.contains(document.elementFromPoint(r.left + 4, r.bottom - 4)) };
    });
    assert.ok(fit.bottom <= fit.footer, `legend ends above the footer (${fit.bottom} vs ${fit.footer})`);
    assert.ok(fit.hit, "the legend's last line is not under the footer");
    await page.keyboard.press("Escape");
    await page.waitForFunction((sel) => !document.querySelector(sel), {}, LEGEND_PANEL);
    await page.setViewport({ width: 1440, height: 1000 });
    await pause(150);
  }

  // ---- Full analysis tab: the report, ranked ----
  // Scorecard | Full analysis is the shared segmented control in the header
  // row, with tab semantics.
  assert.ok(await page.$(".server-view[data-view=tuning] .view-toolbar .segmented[role=tablist] #tuning-tab-analysis[role=tab]"), "the tabs sit in the header");
  assert.equal(await page.$eval("#tuning-tab-scorecard", (e) => e.tabIndex), 0);
  assert.equal(await page.$eval("#tuning-tab-analysis", (e) => e.tabIndex), -1);
  await page.focus("#tuning-tab-scorecard");
  await page.keyboard.press("End");
  await page.waitForFunction(() => document.activeElement?.id === "tuning-tab-analysis" && document.activeElement.getAttribute("aria-selected") === "true");
  await page.keyboard.press("Home");
  await page.waitForFunction(() => document.activeElement?.id === "tuning-tab-scorecard" && document.activeElement.getAttribute("aria-selected") === "true");
  await page.click("#tuning-tab-analysis");
  await page.waitForSelector("#tuning-panel-analysis .report-look");
  assert.equal(await page.$eval("#tuning-tab-analysis", (e) => e.getAttribute("aria-selected")), "true");
  assert.ok(await page.$(".server-view[data-view=tuning] [data-action=refresh]"), "the tab's Refresh runs it again");
  await shoot("tuning-full-analysis");
  await page.click("#tuning-tab-scorecard");
  await page.waitForSelector("#tuning-panel-scorecard");

  // Server opens its own view list again, never the Tuning view.
  await page.click('.main-nav button[data-place="server"]');
  await h.views();
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
