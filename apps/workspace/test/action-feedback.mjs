// Action feedback: every converted action reacts at once and says what is
// happening and what happened (docs/DEVELOPER_REFERENCE.md, "Action feedback").
//
// The real workspace server runs on a temporary, seeded workspace database
// (HOME, USERPROFILE and the vault backend are temporary). Every live,
// lifecycle, editor and network endpoint is mocked. For each action the
// endpoint it calls is held, then answered three ways:
//   ok     held, then answered (a delayed reply),
//   error  held, then answered with HTTP 400 (or the scenario's errorStatus),
//   hang   never answered (the page clock is moved on to reach "slow").
// Each run asserts, measured in the page so a loaded machine does not skew it:
//   - a visible change within 150 ms of the click,
//   - aria-busy on the action's control (or its region) while it waits,
//   - one request for a double click,
//   - focus never falls to the page (unless the owner lane still disables it),
//   - afterwards a visible result (ok), the error (error), or, slow, the
//     seconds waited and the way out (Stop, Stop waiting, Close).
//
// Usage: node test/action-feedback.mjs [--only name,name] [--modes ok,error,hang] [--workers N]
// (npm run test:action-feedback builds first). Screenshots of failures go to
// ACTION_FEEDBACK_SHOTS or a temporary directory.
import fs from "node:fs/promises";
import puppeteer from "puppeteer";
import { SCENARIOS, runOne } from "./action-feedback-lib.mjs";

// ---- Runner ---------------------------------------------------------------------------

const args = process.argv.slice(2);
const opt = (name, fallback) => { const i = args.indexOf(`--${name}`); return i >= 0 ? args[i + 1] : fallback; };
const only = opt("only", "") ? opt("only", "").split(",") : null;
const modes = opt("modes", "ok,error,hang").split(",");
const workers = Math.max(1, Number(opt("workers", process.env.ACTION_FEEDBACK_WORKERS || "3")));
const shots = process.env.ACTION_FEEDBACK_SHOTS || "";
if (shots) await fs.mkdir(shots, { recursive: true });
const jobs = [];
for (const s of SCENARIOS) {
  if (only && !only.includes(s.name)) continue;
  for (const mode of modes) if (!s.modes || s.modes.includes(mode)) jobs.push({ s, mode });
}
console.log(`action-feedback: ${jobs.length} runs (${new Set(jobs.map((j) => j.s.name)).size} actions, modes ${modes.join(", ")}) on ${workers} workers`);
const results = [];
const failures = [];
const queue = [...jobs];
await Promise.all(Array.from({ length: Math.min(workers, jobs.length) }, async () => {
  const browser = await puppeteer.launch({ headless: "shell", args: ["--no-sandbox"] });
  try {
    while (queue.length) {
      const { s, mode } = queue.shift();
      // One retry for a setup that the loaded machine slowed down; an assertion
      // about feedback is never retried.
      let attempt = 0;
      for (;;) {
        try {
          const r = await runOne(browser, s, mode, shots);
          results.push(r);
          console.log(`ok   ${s.name} ${mode}: first change ${r.firstMs} ms`);
          break;
        } catch (e) {
          const setupOnly = /^(not visible|no visible control|confirm never enabled|server did not start|Navigation timeout|TimeoutError)/.test(e.message) &&
            !/aria-busy|visible change|one request|focus/.test(e.message);
          if (setupOnly && attempt++ === 0) { console.log(`retry ${s.name} ${mode}: ${e.message.split("\n")[0]}`); continue; }
          failures.push(`${s.name} ${mode}: ${e.message.split("\n")[0]}`);
          console.log(`FAIL ${s.name} ${mode}: ${e.message.split("\n")[0]}`);
          break;
        }
      }
    }
  } finally {
    await browser.close();
  }
}));
if (failures.length) {
  console.error(`action-feedback: ${failures.length} of ${jobs.length} runs failed:\n  ${failures.join("\n  ")}`);
  process.exit(1);
}
const worst = Math.max(...results.map((r) => r.firstMs));
console.log(`action-feedback: ${results.length} runs passed (${new Set(jobs.map((j) => j.s.name)).size} actions); slowest first change ${worst} ms`);
