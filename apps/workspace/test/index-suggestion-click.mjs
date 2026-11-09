// Run after npm --prefix apps/workspace run build:
// node apps/workspace/test/index-suggestion-click.mjs
// Synthetic reports, isolated workspace, and intercepted server requests.
import assert from "node:assert/strict";
import path from "node:path";
import { open } from "./perf-harness.mjs";

delete process.env.MYFLAMES_VAULT_PASSPHRASE;
const endpoint = "/api/live/index-suggestion";
const attack = "Full scan on </script><img src=x onerror=alert(1)> אבג";
const hint = { status: "suggestion", reason: attack,
  ddl: "CREATE INDEX `idx_orders_status` ON `shop`.`orders` (`status`);",
  caveats: "Heuristic: check with Explain first; indexes add write cost." };
const base = {
  schema: "shop", count: 20, total_ms: 900, avg_ms: 45, max_ms: 80,
  rows_examined: 6000, rows_sent: 200, examined_per_sent: 30,
  no_index_used: 20, no_good_index_used: 0, tmp_disk_tables: 0, sort_merge_passes: 0,
  sample_text: "SELECT * FROM orders WHERE status = 'new'", sample_truncated: false,
  digest_text: "SELECT * FROM `orders` WHERE `status` = ?", flags: ["no_index_used"],
};
let pending;
const h = await open("/api/live/top", "index-suggestion-click", {
  [endpoint]: (_body, request) => { pending = request; return null; },
  "/api/live/schemas": { status: 200, body: { schemas: ["shop"], notes: [] } },
}, { list: "docked" });
const { page, state } = h;
const reads = () => state.other.filter(request => request.path === endpoint);
const view = '.server-view[data-view="top"]';
const readButton = '.view-inspector [data-action="read-index-suggestion"]';
const errors = [], failures = [], httpErrors = [];
page.on("console", message => { if (["error", "warn"].includes(message.type())) errors.push(message.text()); });
page.on("requestfailed", request => failures.push({ url: request.url(), error: request.failure()?.errorText }));
page.on("response", response => { if (response.status() >= 400) httpErrors.push(response.url()); });

async function settled() {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await page.waitForNetworkIdle({ idleTime: 100, timeout: 5000 });
}
async function respond(body) {
  const response = page.waitForResponse(response => new URL(response.url()).pathname === endpoint);
  assert.ok(pending, "the explicit click issued a request");
  await pending.respond({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  pending = null;
  assert.equal((await response).status(), 200);
  await page.waitForFunction(() => !document.querySelector('.view-inspector .action-status[data-phase="pending"]'));
}
try {
  // Separate digest keys ensure the second engine cannot inherit cached advice.
  for (const [engine, version, digest] of [["mysql", "8.4.3", "a"], ["mariadb", "11.4.5", "b"]]) {
    const top = { schema_version: "top-1.0", engine, server_version: version, order: "total", notes: [],
      statements: [{ ...base, digest: digest.repeat(64) }] };
    state.fallback = { status: 200, body: top };
    if (await page.$('.view-inspector.is-detail .detail-back')) await page.click('.view-inspector.is-detail .detail-back');
    await h.view("top");
    await settled();
    const loaded = page.waitForResponse(response => new URL(response.url()).pathname === "/api/live/top");
    await h.refresh();
    await loaded;
    await page.waitForSelector(`${view} tr[data-digest="${digest.repeat(64)}"]`);
    const before = reads().length;
    await h.select(`${view} tr[data-rank="1"]`);
    await page.waitForSelector(readButton, { visible: true });
    await settled();
    assert.equal(reads().length, before, "opening a statement inspector must not run EXPLAIN");
    for (const width of [1440, 720]) {
      for (const theme of ["light", "dark"]) {
        await page.setViewport({ width, height: 1000 });
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
        await settled();
        await page.$eval(readButton, button => button.scrollIntoView({ block: "center" }));
        await page.waitForFunction(selector => {
          const button = document.querySelector(selector), rect = button?.getBoundingClientRect();
          return rect && rect.left >= 0 && rect.right <= innerWidth && rect.top >= 0 && rect.bottom <= innerHeight;
        }, {}, readButton);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        await page.screenshot({ path: path.join(h.shots, `suggestion-${engine}-${width}-${theme}.png`), fullPage: true });
        assert.equal(reads().length, before, "layout and theme changes do not authorize a read");
      }
    }
    // Two synchronous clicks must still issue only one request.
    const sent = page.waitForRequest(request => new URL(request.url()).pathname === endpoint);
    await page.$eval(readButton, button => { button.click(); button.click(); });
    await sent;
    await page.waitForFunction(() => document.querySelector('.view-inspector')?.textContent.includes("Reading an estimated plan"));
    assert.equal(reads().length, before + 1);
    assert.equal(reads().at(-1).body.digest, digest.repeat(64));
    assert.equal(reads().at(-1).body.schema, "shop");
    assert.equal(reads().at(-1).body.sql, undefined);
    await respond(hint);
    await page.waitForSelector('.view-inspector [data-action="open-index-suggestion"]');
    await settled();
    assert.equal(reads().length, before + 1, "one clicked request completes without an automatic duplicate");
    assert.equal(await page.$$eval('.view-inspector bdi', (elements, text) => elements.some(e => e.textContent === text), attack), true);
    assert.equal(await page.$('.view-inspector img, .view-inspector script'), null);
    await h.select(`${view} tr[data-rank="1"]`);
    await page.waitForSelector('.view-inspector [data-action="open-index-suggestion"]');
    await settled();
    assert.equal(reads().length, before + 1, "reopening keeps the completed cached result");
    const reread = page.waitForRequest(request => new URL(request.url()).pathname === endpoint);
    await h.button("Read again", ".view-inspector ");
    await reread;
    await page.waitForFunction(() => document.querySelector('.view-inspector')?.textContent.includes("Reading an estimated plan"));
    assert.equal(reads().length, before + 2);
    await respond({ status: "unknown", reason: "The statement is no longer recorded. Refresh Top statements." });
    await page.waitForFunction(() => document.querySelector('.view-inspector')?.textContent.includes("no longer recorded"));
    await h.select(`${view} tr[data-rank="1"]`);
    await page.waitForSelector(readButton);
    await settled();
    assert.equal(reads().length, before + 2, "an unknown result requires another explicit click after reopening");
    await page.setViewport({ width: 1440, height: 1000 });
  }
  assert.deepEqual(h.errors, []);
  assert.deepEqual(errors, []);
  assert.deepEqual(failures, []);
  assert.deepEqual(httpErrors, []);
  console.log("Index suggestion explicit-click gate: ok");
} finally {
  if (pending) await pending.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "unknown", reason: "Test cleanup" }) }).catch(() => {});
  await h.close();
}
