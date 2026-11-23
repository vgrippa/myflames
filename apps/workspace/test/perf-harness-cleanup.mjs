// perf-harness close(): when the browser never answers Browser.close (the
// force path, the stall that health.mjs hit in release runs), the harness
// stops the browser's whole process tree, not only its parent: renderer, GPU
// and utility children must not outlive the suite. Checked by the browser's
// process group on POSIX (launchBrowser starts the browser detached, as its
// own group leader): members are listed before and after the forced close.
// Run after `npm run build`: node test/perf-harness-cleanup.mjs
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { launchBrowser, closeBrowser, killBrowserTree, pause } from "./perf-harness.mjs";

/** Live (non-zombie) members of the process group *pgid*, as the runner's own
 * processTable reads them. */
function groupMembers(pgid) {
  const result = spawnSync("ps", ["-A", "-o", "pid=,pgid=,stat="], { encoding: "utf8", timeout: 15000 });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.split("\n").flatMap((line) => {
    const match = /^\s*(\d+)\s+(\d+)\s+(\S+)/.exec(line);
    return match && Number(match[2]) === pgid && !match[3].startsWith("Z") ? [Number(match[1])] : [];
  });
}

async function gone(pgid, ms = 10000) {
  for (let i = 0; i < ms / 50; i += 1) {
    if (!groupMembers(pgid).length) return true;
    await pause(50);
  }
  return false;
}

const browser = await launchBrowser();
const proc = browser.process();
assert.ok(proc && proc.pid > 1, "the browser process is known");
try {
  if (process.platform === "win32") {
    // taskkill /T reaps the tree (the suite runner's stopWorker does the same);
    // there is no ps process group to enumerate here, so the tree assertion is
    // POSIX-only below.
    const stuck = () => new Promise(() => {});
    browser.close = stuck;
    await closeBrowser(browser, { forceMs: 250 });
    for (let i = 0; i < 200 && proc.exitCode === null && proc.signalCode === null; i++) await pause(50);
    assert.ok(proc.exitCode !== null || proc.signalCode !== null, "the force path stopped the browser");
  } else {
    // The browser leads its own group: detached at launch.
    assert.ok(groupMembers(proc.pid).includes(proc.pid), "the browser leads its process group");
    // A page gives the browser renderer/utility children to reap.
    const page = await browser.newPage();
    await page.goto("about:blank");
    let children = [];
    for (let i = 0; i < 100 && !children.length; i++) {
      children = groupMembers(proc.pid).filter((pid) => pid !== proc.pid);
      if (!children.length) await pause(50);
    }
    assert.ok(children.length > 0, "the browser has child processes to reap");
    // The incident: browser.close() waits forever on Browser.close. Force it.
    browser.close = () => new Promise(() => {});
    await closeBrowser(browser, { forceMs: 250 });
    assert.ok(await gone(proc.pid), `no Chrome child outlives the forced close (had ${children.join(", ")})`);
  }
  console.log("perf-harness-cleanup: the force path of close() reaps the browser's whole process tree");
} finally {
  killBrowserTree(browser.process());
}
