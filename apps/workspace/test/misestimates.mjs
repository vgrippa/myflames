import { setUiPreferences } from "./perf-harness.mjs";
// Plans: the "Misestimates N" chip in the plan header and the inspector's
// Row estimate section, on the packaged UI with an isolated server (temporary
// HOME and USERPROFILE, vault backend). Plans come from files through the
// Import dialog: the recorded MySQL 9.7 capture (stale statistics, with the
// advisor's evidence), a synthetic plan with two misestimates, and a plan
// with none. Nothing reaches a database: the capture is analyzed locally.
//   Chip: only when N > 0; each press selects the next misestimated operator
//     (worst first), wraps, Shift goes back, keyboard focus stays on the
//     chip, a status line announces it, Visual Explain pans to the card.
//   Inspector: estimate against actual, the reason, the statistics evidence
//     and the ANALYZE TABLE statement to copy (never sent to the server).
//   720 px window and dark appearance: visible, no page overflow, contrast.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-misestimates-"));
const capture = path.join(root, "test/fixtures/captures/mysql-9.7-stale-stats.capture.json");
const mariaCapture = path.join(root, "test/fixtures/captures/mariadb-11.8-stale-stats.capture.json");
const aliasCollision = path.join(directory, "mariadb-alias-collision.capture.json");
await fs.writeFile(aliasCollision, JSON.stringify({
  schema_version: "capture-1.0", mode: "analyze", engine: "mariadb",
  query: "SELECT * FROM a.items AS orders JOIN b.orders AS actual_orders ON orders.id=actual_orders.id",
  raw: JSON.stringify({ query_block: { select_id: 1, r_total_time_ms: 2, nested_loop: [
    { table: { table_name: "orders", access_type: "ref", key: "k", rows: 1, r_rows: 1000, r_loops: 1, r_table_time_ms: 1 } },
    { table: { table_name: "actual_orders", access_type: "eq_ref", key: "PRIMARY", rows: 1, r_rows: 1, r_loops: 1000, r_table_time_ms: 1 } }
  ] } }),
  stats_freshness: { "a.items": { n_rows: 10 }, "b.orders": { n_rows: 20 } }
}));
const plain = path.join(root, "test/fixtures/explain-063-limit-no-order-users.json");
// Two misestimates: the filter's selectivity (180x under, the access below
// it is accurate) and an index lookup (40x under, 9,000 loops).
const two = path.join(directory, "two-misestimates.json");
await fs.writeFile(two, JSON.stringify({
  operation: "Nested loop inner join", access_type: "join", join_algorithm: "nested_loop",
  estimated_rows: 50, actual_rows: 360000, actual_loops: 1, actual_last_row_ms: 30, inputs: [
    { operation: "Filter: (o.total > 490)", access_type: "filter", condition: "(o.total > 490)",
      estimated_rows: 50, actual_rows: 9000, actual_loops: 1, actual_last_row_ms: 12, inputs: [
        { operation: "Table scan on o", access_type: "table", table_name: "orders",
          estimated_rows: 20000, actual_rows: 20000, actual_loops: 1, actual_last_row_ms: 10 }] },
    { operation: "Index lookup on i using idx_order (order_id = o.id)", access_type: "index",
      index_access_type: "index_lookup", table_name: "items", index_name: "idx_order",
      estimated_rows: 1, actual_rows: 40, actual_loops: 9000, actual_last_row_ms: 0.002 }] }));

// The filter takes exactly its child's time: it has a row misestimate but
// no self-time bar. The chip must retain the inspector and explain this.
const zeroSelf = path.join(directory, "zero-self-time.json");
await fs.writeFile(zeroSelf, JSON.stringify({
  operation: "Filter: (t.x > 0)", access_type: "filter", condition: "(t.x > 0)", estimated_rows: 1,
  actual_rows: 1000, actual_loops: 1, actual_last_row_ms: 1, inputs: [
    { operation: "Table scan on t", access_type: "table", table_name: "t",
      estimated_rows: 2000, actual_rows: 2000, actual_loops: 1, actual_last_row_ms: 1 }] }));

const server = spawn(process.env.PYTHON || "python3", ["-c",
  "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()",
  path.join(directory, "workspace.sqlite3")],
{ cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } });
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("UI did not start: " + output)), 15000);
    server.once("exit", (code) => { clearTimeout(timer); reject(new Error(`UI exited: ${code}: ${output}`)); });
    server.stderr.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const context = browser.defaultBrowserContext();
  await context.overridePermissions(new URL(url).origin, ["clipboard-read", "clipboard-write", "clipboard-sanitized-write"]);
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  // No database: every server read (the app's own startup privileges read
  // on the default connection included) is refused here and recorded.
  const refused = [];
  const requests = [];
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const where = new URL(request.url()).pathname;
    if (where.startsWith("/api/")) requests.push({ path: where, body: request.postData() || "" });
    if (where.startsWith("/api/live/") || where.startsWith("/api/connection/")) {
      refused.push(where);
      request.respond({ status: 400, contentType: "application/json", body: JSON.stringify({ error: "No database in this test." }) });
    } else request.continue();
  });
  // Only the refusals above may log a failed load.
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location()?.url || "";
    if (/^Failed to load resource: the server responded with a status of 400/.test(message.text()) &&
        /\/api\/(live|connection)\//.test(where)) return;
    errors.push(message.text());
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const deadline = (ms) => ({ timeout: ms });

  async function importPlan(file) {
    await page.locator(".plans-bar .plans-import").click();
    const input = await page.waitForSelector('dialog input[type="file"]', deadline(10000));
    await input.uploadFile(file);
    await page.waitForFunction(() => !document.querySelector("dialog[open]"), deadline(20000));
    await page.waitForSelector('.plan-canvas .plan-chart[data-mfc-mounted="true"] svg', deadline(20000));
  }
  const chip = () => page.$(".plan-heading .misestimate-chip");
  const selected = () => page.$eval(".plan-inspector .inspector-title h3", (el) => el.textContent);
  const status = () => page.$eval(".misestimate-status[role=status]", (el) => el.textContent);

  await page.locator(".main-nav button ::-p-text(Plans)").click();
  await page.waitForSelector(".plans-bar .plans-import", deadline(10000));

  // A plan without a misestimate has no chip, but the status line is mounted.
  await importPlan(plain);
  assert.equal(await chip(), null, "no chip when nothing is misestimated");
  assert.equal(await status(), "", "the announcement region is mounted and empty");

  // The recorded capture: one misestimate, with statistics evidence.
  await importPlan(capture);
  await page.waitForSelector(".plan-heading .plan-stats .misestimate-chip", deadline(10000));
  assert.equal(await page.$eval(".misestimate-chip", (el) => el.textContent.trim()), "Misestimates 1");
  assert.match(await page.$eval(".misestimate-chip", (el) => el.getAttribute("aria-label")), /^Misestimates 1: /,
    "the accessible name starts with the visible text");
  assert.equal(await page.$eval(".misestimate-chip", (el) => el.tagName), "BUTTON");
  const before = requests.length;
  await page.focus(".misestimate-chip");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".plan-inspector .inspector-title h3")?.textContent !== "Select an operator", deadline(10000));
  assert.equal(await selected(), "Index lookup [orders.idx_customer]");
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("misestimate-chip")), true, "focus stays on the chip");
  assert.equal(await status(), "Misestimate 1 of 1: Index lookup [orders.idx_customer], rows ×451 under");
  assert.equal(await page.$eval('.plan-chart .wb-node.selected', (el) => el.getAttribute("aria-label")),
    "Index lookup [orders.idx_customer], rows ×451 under", "the chart selects the same operator");
  // The inspector's Row estimate section: verdict, reason, evidence, statement.
  const inspector = await page.$eval(".plan-inspector", (panel) => {
    const block = Array.from(panel.querySelectorAll(".inspector-block")).find((b) => b.querySelector("h4")?.textContent.startsWith("Row estimate"));
    const fields = Object.fromEntries(Array.from(block.querySelectorAll(".inspector-fields > div"), (row) => [row.querySelector("dt").textContent, row.querySelector("dd").textContent]));
    return { tag: block.querySelector(".misestimate-tag")?.textContent, verdict: block.querySelector(".misestimate-verdict")?.textContent,
      fields, sql: block.querySelector(".perf-sql pre")?.textContent, time: block.querySelector("dd span[title]")?.getAttribute("title"),
      first: panel.querySelector(".plan-inspector-body .inspector-block h4")?.textContent };
  });
  assert.equal(inspector.first.startsWith("Row estimate"), true, "the verdict comes before the measurements");
  assert.equal(inspector.tag, "rows ×451 under");
  assert.equal(inspector.verdict, "rows ×451 under451× under (estimated 1, actual 451 rows per loop)");
  assert.deepEqual(inspector.fields, { Table: "myflames_w2c_stale.orders", "Statistics updated": "2026-11-08 22:10:20",
    "Rows in statistics": "6,852", Histograms: "None" });
  assert.equal(inspector.time, "Server time: 2026-11-08 22:10:20", "the server's clock, not converted");
  assert.equal(inspector.sql, "ANALYZE TABLE `myflames_w2c_stale`.`orders`;");
  // The reason is one click away (ⓘ), not a paragraph.
  await page.click('.plan-inspector button[aria-label="About Row estimate"]');
  const reason = await page.waitForSelector('.popover[role=dialog][aria-label="About Row estimate"]', deadline(5000));
  assert.match(await reason.evaluate((el) => el.textContent), /Rows returned per loop differ from the optimizer's estimate by 10× or more/);
  await page.keyboard.press("Escape");
  // Copy puts the statement on the clipboard; myflames sends nothing.
  await page.click('.plan-inspector button[aria-label="Copy: ANALYZE TABLE statement"]');
  await page.waitForFunction(() => /Copied/.test(document.querySelector(".plan-inspector .perf-sql .hint")?.textContent || ""), deadline(5000));
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "ANALYZE TABLE `myflames_w2c_stale`.`orders`;");
  const after = requests.slice(before);
  assert.deepEqual(after.filter((r) => /ANALYZE TABLE/i.test(r.body)), [], "the statement is never sent");
  assert.deepEqual(after.filter((r) => r.path.startsWith("/api/live/") || r.path.startsWith("/api/editor/")), [], "no server read or write");

  // Two misestimates: worst first, wraps, Shift goes back, the chart pans.
  await importPlan(two);
  await page.waitForFunction(() => document.querySelector(".misestimate-chip")?.textContent.trim() === "Misestimates 2", deadline(10000));
  // Zoom in so the cards sit outside the view; each press brings one back.
  for (let i = 0; i < 3; i++) await page.click('.plan-chart [data-action="in"]');
  const order = [];
  for (let press = 0; press < 3; press++) {
    await page.click(".misestimate-chip");
    await page.waitForFunction((n) => (document.querySelector(".misestimate-status[role=status]")?.textContent || "").startsWith(`Misestimate ${n} of 2`),
      deadline(5000), (press % 2) + 1);
    const state = await page.evaluate(() => {
      const card = document.querySelector(".plan-chart .wb-node.selected");
      const viewport = document.querySelector(".plan-chart .wb-viewport");
      const [x, y] = card.getAttribute("transform").match(/[-\d.]+/g).map(Number);
      const h = Number(card.querySelector(".wb-box").getAttribute("height"));
      const b = viewport.viewBox.baseVal;
      return { title: document.querySelector(".plan-inspector .inspector-title h3").textContent,
        visible: x >= b.x && y >= b.y && x + 256 <= b.x + b.width && y + h <= b.y + b.height };
    });
    assert.ok(state.visible, "press " + (press + 1) + ": Visual Explain shows the selected card");
    order.push(state.title);
  }
  assert.deepEqual(order, ["Filter: ((o.total > 490))", "Index lookup [items.idx_order]", "Filter: ((o.total > 490))"],
    "worst first (180x, then 40x), then around again");
  await page.keyboard.down("Shift");
  await page.click(".misestimate-chip");
  await page.keyboard.up("Shift");
  await page.waitForFunction(() => document.querySelector(".plan-inspector .inspector-title h3")?.textContent === "Index lookup [items.idx_order]", deadline(5000));
  // The filter's record names the selectivity, and no evidence is claimed.
  await page.click(".misestimate-chip");
  await page.waitForFunction(() => document.querySelector(".plan-inspector .inspector-title h3")?.textContent === "Filter: ((o.total > 490))", deadline(5000));
  assert.equal(await page.$(".plan-inspector .perf-sql"), null, "without statistics evidence there is no statement");
  // Another view: the chip still steps, the tree row is selected.
  await page.click('.plan-fold .chart-views button[data-value="tree"]');
  await page.waitForSelector('.plan-chart .mfc-tree[data-wired="true"]', deadline(10000));
  assert.equal(await page.$$eval(".plan-chart .mfc-tree .mfc-mis-chip", (els) => els.length), 2, "tree: a chip on each misestimated row");
  await page.click(".misestimate-chip");
  await page.waitForFunction(() => document.querySelector(".plan-chart .tree-row.is-selected")?.getAttribute("aria-label") === "Index lookup [items.idx_order]", deadline(5000));

  await importPlan(zeroSelf);
  await page.waitForFunction(() => document.querySelector(".misestimate-chip")?.textContent.trim() === "Misestimates 1", deadline(10000));
  await page.click('.plan-fold .chart-views button[data-value="bargraph"]');
  await page.waitForSelector('.plan-chart .mfc-bar[data-wired="true"]', deadline(10000));
  await page.click(".misestimate-chip");
  const absent = "Not shown in the bar chart (no self time); open Visual Explain or the execution tree to see it";
  await page.waitForFunction((message) => document.querySelector(".misestimate-status")?.textContent === message,
    deadline(5000), absent);
  assert.equal(await selected(), "Filter: ((t.x > 0))", "the absent operator stays selected in the inspector");
  assert.equal(await page.$(".plan-chart .bar.is-selected"), null, "no unrelated bar is selected");
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("misestimate-chip")), true);
  await page.click(".misestimate-chip");
  await page.waitForFunction((message) => document.querySelector(".misestimate-status")?.textContent === message,
    deadline(5000), absent);
  // Selecting a visible operator must not inherit the previous one's
  // missing-bar explanation. The chip can then return to the filter.
  await page.click(".plan-chart .bar");
  await page.waitForFunction(() => document.querySelector(".plan-inspector .inspector-title h3")?.textContent === "Table scan [t]", deadline(5000));
  // The note clears in an effect after the selection renders (one frame later).
  await page.waitForFunction(() => document.querySelector(".misestimate-status[role=status]")?.textContent === "", deadline(5000));
  assert.equal(await status(), "");
  await page.click(".misestimate-chip");
  await page.waitForFunction((message) => document.querySelector(".misestimate-status")?.textContent === message,
    deadline(5000), absent);

  // 720 px window, light and dark: the chip is on screen, the page never
  // scrolls sideways, the chip text keeps contrast on its tint.
  for (const theme of ["light", "dark"]) {
    await setUiPreferences(page, { theme });
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: width === 720 ? 520 : 900 });
      // Controls ease their colors: wait for the theme's color to settle.
      await page.waitForFunction((color) => {
        const chip = document.querySelector(".misestimate-chip");
        return !!chip && chip.getBoundingClientRect().width > 0 && getComputedStyle(chip).color === color;
      }, deadline(5000), theme === "dark" ? "rgb(179, 164, 255)" : "rgb(91, 60, 196)");
      const look = await page.evaluate(() => {
        const el = document.querySelector(".misestimate-chip");
        const r = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const parse = (c) => c.match(/[\d.]+/g).map(Number);
        const lum = ([r, g, b]) => [r, g, b].map((v) => v / 255).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
          .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
        // The tint is translucent: composite it over the header behind it.
        const [br, bg, bb, ba = 1] = parse(style.backgroundColor);
        const under = parse(getComputedStyle(document.querySelector(".plan-heading")).backgroundColor === "rgba(0, 0, 0, 0)"
          ? getComputedStyle(document.body).backgroundColor : getComputedStyle(document.querySelector(".plan-heading")).backgroundColor);
        const shown = [br, bg, bb].map((v, i) => v * ba + under[i] * (1 - ba));
        const [hi, lo] = [lum(parse(style.color)), lum(shown)].sort((a, b) => b - a);
        // The chip never clips the Plan summary or SQL controls beside it.
        const clipped = Array.from(document.querySelectorAll(".plan-header-tools summary"))
          .filter((summary) => summary.scrollWidth > summary.clientWidth + 1).map((summary) => summary.textContent.trim());
        return { left: r.left, right: r.right, top: r.top, height: r.height, width: r.width, color: style.color,
          word: el.querySelector(".misestimate-chip-label").getBoundingClientRect().width > 1, clipped,
          screen: document.querySelector(".plan-screen").getBoundingClientRect().width,
          contrast: (hi + 0.05) / (lo + 0.05), overflow: document.documentElement.scrollWidth > innerWidth };
      });
      const at = theme + " " + width;

      assert.ok(look.left >= 0 && look.right <= width, at + ": the chip is on screen " + JSON.stringify(look));
      assert.ok(look.height >= 24 && look.width >= 24, at + ": a 24 px target");
      assert.deepEqual(look.clipped, [], at + ": Plan summary and SQL keep their words");
      // Up to 1200 px the measurements have their own row, with room for the
      // word; above it they share the full first row, so the chip keeps its
      // mark and count unless the plan screen is 1,500 px or wider.
      assert.equal(look.word, width <= 1200 || look.screen >= 1500, at + ": Misestimates word " + JSON.stringify(look));
      assert.equal(look.overflow, false, at + ": no sideways page scroll");
      assert.equal(look.color, theme === "dark" ? "rgb(179, 164, 255)" : "rgb(91, 60, 196)", at + ": --estimate");
      assert.ok(look.contrast >= 4.5, at + ": chip text contrast " + look.contrast.toFixed(2));
      const feedback = await page.$eval(".misestimate-status", (el) => {
        const box = el.getBoundingClientRect();
        return { hidden: el.classList.contains("visually-hidden"), height: box.height,
          left: box.left, right: box.right, clipped: el.scrollHeight > el.clientHeight };
      });
      assert.equal(feedback.hidden, false, at + ": the missing-bar message is visible");
      assert.ok(feedback.height >= 18 && feedback.left >= 0 && feedback.right <= width, at + ": status fits");
      assert.equal(feedback.clipped, false, at + ": status text wraps without clipping");
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  // Switching to a view that includes zero-time operators clears the
  // unavailable-bar feedback and lets the chip reveal the same selection.
  await page.click('.plan-fold .chart-views button[data-value="tree"]');
  await page.waitForSelector('.plan-chart .mfc-tree[data-wired="true"]', deadline(10000));
  await page.click(".misestimate-chip");
  await page.waitForFunction(() => document.querySelector(".plan-chart .tree-row.is-selected")?.getAttribute("aria-label") === "Filter: ((t.x > 0))", deadline(5000));
  assert.match(await status(), /^Misestimate 1 of 1: Filter:/);
  assert.equal(await page.$eval(".misestimate-status", (el) => el.classList.contains("visually-hidden")), true);

  // MariaDB's unaliased capture has a proven schema; an alias collision
  // keeps its mark without borrowing another table's evidence or SQL.
  for (const [file, expectedSql] of [[mariaCapture, "ANALYZE TABLE `myflames_w2c_stale`.`orders`;"], [aliasCollision, null]]) {
    await importPlan(file);
    await page.click(".misestimate-chip");
    await page.waitForFunction((index) => document.querySelector(".plan-inspector .inspector-title h3")?.textContent.includes("orders." + index),
      deadline(10000), file === mariaCapture ? "idx_customer" : "k");
    const fields = await page.$eval(".plan-inspector", (panel) => {
      const block = Array.from(panel.querySelectorAll(".inspector-block")).find((b) => b.querySelector("h4")?.textContent.startsWith("Row estimate"));
      return { text: block.textContent, sql: block.querySelector(".perf-sql pre")?.textContent ?? null };
    });
    assert.equal(fields.sql, expectedSql, path.basename(file));
    if (expectedSql === null) {
      assert.doesNotMatch(fields.text, /b\.orders|Rows in statistics|Statistics updated|ANALYZE TABLE/);
      assert.match(fields.text, /rows ×1,000 under/);
    } else {
      assert.match(fields.text, /19,907/);
    }
  }
  assert.deepEqual([...new Set(refused)].filter((where) => where !== "/api/live/privileges"), [],
    "the only server read is the app's own startup privileges read, refused here");
  assert.deepEqual(errors, []);
  console.log("Misestimates: chip cycle and reveal, zero-self-time recovery, MySQL and MariaDB evidence, alias collision without borrowed statistics or SQL, 720 px and dark contrast passed");
} finally {
  if (browser) await browser.close();
  server.kill();
  await fs.rm(directory, { recursive: true, force: true });
}
