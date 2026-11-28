// R-1 regression: a failed load of the lazily loaded WhyDisabled chunk never
// unmounts the workspace, sends nothing when a privilege-disabled button is
// clicked, and retries cleanly on the next open once the chunk is allowed.
// Run: node test/why-disabled-chunk.mjs
import assert from "node:assert/strict";
import { load, open, pause, privilegeAnswer } from "./perf-harness.mjs";

const active = await load("sessions-mysql-8.4-active.json");
active.server_identity = { server_uuid: "sessions-server-uuid" };
active.trx_checked = true;
const foreign = structuredClone(active);
foreign.sessions.forEach((s) => { s.user = "another_user"; });

const killGrant = "GRANT CONNECTION_ADMIN ON *.* TO `root`@`localhost`;";
const prepares = [];
const kills = [];
const h = await open("/api/live/sessions", "why-disabled-chunk", {
  "/api/live/sessions/history": (b) => ({
    status: 200,
    body: { id: b.id, available: true, reason: "none", statements: [], limit: 10 },
  }),
  "/api/live/kill/prepare": (b) => {
    prepares.push(b);
    return {
      status: 200,
      body: { review_token: "chunk-test-token", statements: [`KILL CONNECTION ${b.connection_id}`], expect: b.expect, connection_id: b.connection_id, mode: b.mode },
    };
  },
  "/api/live/kill": (b) => {
    kills.push(b);
    return {
      status: 200,
      body: { schema_version: "kill-1.0", status: "killed", connection_id: b.connection_id, mode: b.mode, statement: `KILL CONNECTION ${b.connection_id}`, verified: [], message: "Ended.", target: null, after: { present: false } },
    };
  },
  "/api/audit/declined": { status: 200, body: { audit_id: "a1", status: "declined" } },
}, { list: "docked", stampReport: "taken_at", offline: true });

try {
  const { page, state } = h;
  state.fallback = { status: 200, body: foreign };
  const origin = new URL(page.url()).origin;
  const chunkPattern = /\/WhyDisabled-[^/]*\.js$/;
  const chunkRequests = [];
  let failedChunkRequests = 0;
  let idlePreloadReachedEnd = false;
  page.on("request", (request) => {
    const url = request.url();
    const pathname = new URL(url).pathname;
    if (chunkPattern.test(pathname) || /WhyDisabled/i.test(url)) chunkRequests.push(url);
    if (/\/PlanScreen-[^/]*\.js$/.test(pathname)) idlePreloadReachedEnd = true;
  });
  page.on("requestfailed", (request) => {
    if (chunkPattern.test(new URL(request.url()).pathname)) failedChunkRequests += 1;
  });

  // Block the WhyDisabled chunk from initial load (fresh module cache).
  state.failAssets = chunkPattern;
  await page.setCacheEnabled(false);
  await page.reload({ waitUntil: "networkidle0" });

  await h.workload();
  await h.view("sessions");
  const view = ".server-view[data-view=sessions]";
  await page.waitForSelector(`${view} tbody tr`);
  await h.setPrivileges({
    kill: privilegeAnswer("denied", "CONNECTION_ADMIN", { grant_sql: killGrant }),
    kill_own: privilegeAnswer("allowed"),
    sessions_all: privilegeAnswer("allowed"),
    explain_connection: privilegeAnswer("allowed"),
  });
  await h.select(`${view} tbody tr[data-session="100"]`);
  const gatedKill = '.view-inspector [aria-label="Kill: connection 100"]';
  await page.waitForSelector(gatedKill, { visible: true });
  assert.equal(await page.$eval(gatedKill, (b) => b.getAttribute("aria-disabled")), "true");
  assert.equal(await page.$(".why-disabled"), null, "WhyDisabled host is not mounted before first click");

  // Wait for the one-time startup idle preload queue to finish so it does not
  // overlap with the idle and per-click assertions below.
  for (let i = 0; i < 60 && !idlePreloadReachedEnd; i++) await pause(250);
  await pause(400);

  // R-1 follow-up: 0 chunk requests while idle before any user click.
  const idleBeforeClicks = chunkRequests.length;
  await pause(2000);
  assert.equal(chunkRequests.length, idleBeforeClicks, "0 WhyDisabled chunk requests while idle before clicks");

  // Click while the chunk is blocked: each click starts at most 1 chunk
  // request, the error boundary renders nothing, the workspace stays mounted,
  // and nothing is sent. Test past all retry variants (6 clicks).
  for (let click = 1; click <= 6; click += 1) {
    const reqsBefore = chunkRequests.length;
    const failedBefore = failedChunkRequests;
    await page.click(gatedKill);
    await pause(400);
    const delta = chunkRequests.length - reqsBefore;
    assert.ok(delta <= 1, `click ${click} started at most 1 chunk request (got ${delta})`);
    assert.equal(delta, 1, `click ${click} attempted 1 fresh chunk fetch while blocked`);
    assert.ok(failedChunkRequests > failedBefore, `click ${click} blocked WhyDisabled chunk request actually failed`);
    if (click === 1) {
      const aliveAfterFirst = await page.evaluate((sel) => ({
        workspace: !!document.querySelector(".workspace"),
        sessionsView: !!document.querySelector(".server-view[data-view=sessions]"),
        inspector: !!document.querySelector(".view-inspector"),
        killButton: !!document.querySelector(sel),
        killAriaDisabled: document.querySelector(sel)?.getAttribute("aria-disabled"),
        whyOpen: !!document.querySelector(".why-disabled:popover-open"),
        dialogOpen: !!document.querySelector("dialog.kill-dialog[open]"),
      }), gatedKill);
      assert.deepEqual(aliveAfterFirst, {
        workspace: true,
        sessionsView: true,
        inspector: true,
        killButton: true,
        killAriaDisabled: "true",
        whyOpen: false,
        dialogOpen: false,
      }, "workspace stays rendered after WhyDisabled chunk load failure");
    }
    assert.equal(prepares.length, 0, `blocked click ${click} prepares nothing`);
    assert.equal(kills.length, 0, `blocked click ${click} sends nothing`);
  }

  // Keyboard burst while still blocked also stays bounded (at most 1 per
  // activation) and leaves the workspace intact.
  const reqsBeforeKeys = chunkRequests.length;
  const failedBeforeKeys = failedChunkRequests;
  await page.focus(gatedKill);
  await page.keyboard.press("Enter");
  await page.keyboard.press("Space");
  await pause(400);
  const keyDelta = chunkRequests.length - reqsBeforeKeys;
  assert.ok(keyDelta >= 1 && keyDelta <= 2, `2 key activations started at most 1 chunk request each (got ${keyDelta})`);
  assert.ok(failedChunkRequests > failedBeforeKeys, "keyboard retry while still blocked attempts a fresh chunk fetch");
  assert.ok(await page.$(gatedKill), "workspace and Kill button survive repeated blocked clicks");
  assert.equal(await page.$(".why-disabled:popover-open"), null);
  assert.equal(await page.$("dialog.kill-dialog[open]"), null);
  assert.equal(prepares.length, 0, "repeated blocked clicks prepare nothing");
  assert.equal(kills.length, 0, "repeated blocked clicks send nothing");
  assert.deepEqual(h.errors, [], "error boundary catches chunk failure with no uncaught page errors");

  // R-1 follow-up: 0 chunk requests while idle after repeated failures (no retry loop).
  const idleAfterClicks = chunkRequests.length;
  await pause(2000);
  assert.equal(chunkRequests.length, idleAfterClicks, "0 WhyDisabled chunk requests while idle after blocked clicks");

  // Allow the WhyDisabled chunk again: the next click on the same button retries
  // the import (at most 1 request) and opens the message with the backend GRANT.
  state.failAssets = null;
  const reqsBeforeRecover = chunkRequests.length;
  await page.click(gatedKill);
  await page.waitForSelector(".why-disabled:popover-open", { visible: true, timeout: 10000 });
  const recoverDelta = chunkRequests.length - reqsBeforeRecover;
  assert.ok(recoverDelta >= 1 && recoverDelta <= 1, `recovery click started at most 1 chunk request (got ${recoverDelta})`);
  const why = await page.$eval(".why-disabled:popover-open", (e) => ({
    role: e.getAttribute("role"),
    text: e.textContent || "",
    grant: e.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.equal(why.role, "dialog");
  assert.match(why.text, /CONNECTION_ADMIN/);
  assert.match(why.text, /Run it as an administrator, then Refresh\./);
  assert.equal(why.grant, killGrant);
  assert.equal(prepares.length, 0, "retrying and opening the message prepares nothing");
  assert.equal(kills.length, 0, "retrying and opening the message sends nothing");
  assert.equal(await page.$("dialog.kill-dialog[open]"), null);

  // R-1 follow-up: every WhyDisabled chunk request stayed on the workspace origin.
  assert.ok(chunkRequests.length > 0, "observed WhyDisabled chunk requests");
  assert.deepEqual(chunkRequests.filter((u) => new URL(u).origin !== origin), [],
    "every WhyDisabled chunk request went to the workspace origin only");
  assert.deepEqual(state.blocked, [], "no non-local requests were attempted");

  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Kill: connection 100",
    "Escape returns focus to the disabled Kill button");

  // Once WhyDisabledHost is mounted, a subsequent click commits synchronously
  // inside the click event: popovertargetaction="show" prevents native toggle
  // activation from closing the just-opened popover, a second click toggles it
  // closed, and closing restores both popovertarget attributes.
  await page.click(gatedKill);
  await page.waitForSelector(".why-disabled:popover-open .why-disabled-close", { visible: true });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0))));
  assert.ok(await page.$(".why-disabled:popover-open .why-disabled-close"),
    "mounted WhyDisabledHost keeps popover open after synchronous click render");
  assert.equal(await page.$eval(gatedKill, (b) => b.getAttribute("popovertargetaction")), "show");
  await page.click(gatedKill);
  await page.waitForFunction(() => !document.querySelector(".why-disabled"));
  assert.deepEqual(
    await page.$eval(gatedKill, (b) => ({ pt: b.getAttribute("popovertarget"), pta: b.getAttribute("popovertargetaction") })),
    { pt: null, pta: null },
    "closing WhyDisabled restores popovertarget and popovertargetaction",
  );
  assert.deepEqual(h.errors, [], "no uncaught page errors after retry recovery");
  console.log("why-disabled-chunk: ok");
} finally {
  await h.close();
}
