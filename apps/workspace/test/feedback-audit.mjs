// Action feedback audit runner. Every scenario × mode runs on its own fresh
// server and page. Usage:
//   node test/feedback-audit.mjs OUT_DIR [--only a,b] [--modes ok,error,hang] [--workers 4]
// Writes OUT_DIR/results/<scenario>-<mode>.json and screenshots.
import fs from "node:fs/promises";
import path from "node:path";
import { launch, measure, openPage, pause, startWorkspace, switchAway, switchConnection, tryDismiss } from "./feedback-audit-lib.mjs";
import { SCENARIOS as A } from "./feedback-audit-scenarios.mjs";
import { SCENARIOS_B } from "./feedback-audit-scenarios-b.mjs";
import { SCENARIOS_C } from "./feedback-audit-scenarios-c.mjs";
const SCENARIOS = [...A, ...SCENARIOS_B, ...SCENARIOS_C];

const args = process.argv.slice(2);
const out = args[0];
const opt = (name, fallback) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : fallback; };
const only = opt("only", "") ? opt("only", "").split(",") : null;
const modes = opt("modes", "ok,error,hang").split(",");
const workers = Number(opt("workers", "4"));
const resultsDir = path.join(out, "results");
const shots = path.join(out, "shots");
await fs.mkdir(resultsDir, { recursive: true });
await fs.mkdir(shots, { recursive: true });

const jobs = [];
for (const s of SCENARIOS) {
  if (only && !only.some((o) => s.name === o || s.name.startsWith(o + "*") || (o.endsWith("*") && s.name.startsWith(o.slice(0, -1))))) continue;
  for (const mode of modes) if (!(s.modes && !s.modes.includes(mode))) jobs.push({ s, mode });
}
console.error(`${jobs.length} runs on ${workers} workers`);

async function runOne(browser, { s, mode }) {
  const ws = await startWorkspace(s.name);
  let ctx;
  const file = path.join(resultsDir, `${s.name}-${mode}.json`);
  try {
    ctx = await openPage(browser, ws.url, s.viewport || {});
    if (s.mocks) Object.assign(ctx.mocks, s.mocks);
    await s.setup(ctx);
    await pause(400);
    const result = await measure(ctx, {
      name: s.name, mode, shots, target: s.target, paths: s.paths,
      settle: s.settle, noSecondClick: s.noSecondClick,
      after: mode === "hang" && s.place
        ? async (page) => ({ dismiss: s.sheet ? await tryDismiss(page) : undefined,
          placeSwitch: await switchAway(page, s.place, s.away || "home").catch((e) => `place switch failed: ${e.message.split("\n")[0]}`),
          connectionSwitch: s.connection === false ? "not applicable" : await switchConnection(page).catch((e) => `connection switch failed: ${e.message.split("\n")[0]}`) })
        : s.afterOk && mode !== "hang" ? s.afterOk : undefined,
    });
    if (mode === "hang") await ctx.page.screenshot({ path: path.join(shots, `${s.name}-hang-after-switch.png`) });
    result.owner = s.owner; result.label = s.label; result.place = s.place; result.pageErrors = ctx.errors;
    await fs.writeFile(file, JSON.stringify(result, null, 1));
    console.error(`done ${s.name} ${mode}: first ${result.firstChangeMs} ms, ${result.requests} req`);
  } catch (e) {
    await fs.writeFile(file, JSON.stringify({ name: s.name, mode, owner: s.owner, label: s.label, setupError: String(e.stack || e) }, null, 1));
    if (ctx) await ctx.page.screenshot({ path: path.join(shots, `${s.name}-${mode}-FAILED.png`) }).catch(() => {});
    console.error(`FAILED ${s.name} ${mode}: ${e.message}`);
  } finally {
    if (ctx) await ctx.page.close().catch(() => {});
    await ws.close();
  }
}

const queue = [...jobs];
await Promise.all(Array.from({ length: workers }, async () => {
  const browser = await launch();
  try {
    while (queue.length) await runOne(browser, queue.shift());
  } finally {
    await browser.close();
  }
}));
