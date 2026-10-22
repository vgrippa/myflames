// Inventory of visible controls per place and view (feedback audit helper).
import fs from "node:fs/promises";
import path from "node:path";
import { launch, openPage, pause, startWorkspace } from "./feedback-audit-lib.mjs";
import { chooseView } from "./chrome-helpers.mjs";

const out = process.argv[2];
await fs.mkdir(out, { recursive: true });
const ws = await startWorkspace("explore");
const browser = await launch();
const inventory = {};
async function dump(page, key) {
  await pause(1500);
  inventory[key] = await page.evaluate(() => {
    const shown = (el) => el.isConnected && !el.closest("[hidden]") && el.getClientRects().length > 0;
    return [...document.querySelectorAll("button, [role=menuitem], summary, a[href], [role=tab]")].filter(shown)
      .filter((e) => !e.closest(".main-nav, .sidebar"))
      .map((e) => `${(e.getAttribute("aria-label") || e.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60)}${e.dataset.action ? ` [${e.dataset.action}]` : ""}${e.disabled ? " (disabled)" : ""}`);
  });
  await ctx.page.screenshot({ path: path.join(out, `explore-${key}.png`) });
}
const ctx = await openPage(browser, ws.url);
const { page } = ctx;
try {
  for (const place of ["home", "plans", "query", "replication", "cluster", "proxysql", "tuning", "investigations", "learn"]) {
    await page.click(`.main-nav button[data-place="${place}"]`);
    await dump(page, place);
  }
  for (const [place, views] of [["server", ["health", "innodb", "errorlog", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"]],
    ["workload", ["top", "sessions", "waits", "io"]]]) {
    await page.click(`.main-nav button[data-place="${place}"]`);
    await pause(800);
    for (const v of views) {
      try { await chooseView(page, v); await dump(page, `${place}-${v}`); }
      catch (e) { inventory[`${place}-${v}`] = [`FAILED: ${e.message}`]; }
    }
  }
  console.log(JSON.stringify(inventory, null, 1));
  console.error("page errors:", ctx.errors);
} finally {
  await browser.close();
  await ws.close();
}
