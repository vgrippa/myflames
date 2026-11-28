// Live MySQL Shell progress and completed Shell output in ActionReview / ActionResult.
// Uses the real built workspace and Python server on a throwaway HOME, with
// intercepted /api/live/cluster*, /api/live/proxysql* and /api/live/action/progress.
import assert from "node:assert/strict";
import { load, open, pause } from "./perf-harness.mjs";
import { openConnectionPicker } from "./chrome-helpers.mjs";

const clusterReport = await load("cluster-ic-8.4-clusterset-primary.json");
const proxyLab = await load("proxysql-lab.json");
const proxyPlan = await load("proxysql-plan-offline-soft.json");
const proxyResult = await load("proxysql-result-done.json");
const proxyProgress = await load("proxysql-progress-sending.json");

// Exercise the backend's catalog entry instead of adding a synthetic Check.
const checkAction = clusterReport.actions.find((a) => a.id === "check_clusterset");
assert.equal(checkAction?.scope, "clusterset");
assert.equal(checkAction?.enabled, true);

const switchGuard =
  '/*myflames:clusterset {"adminapi":{"groups":{"drcluster":"dddddddd-2222-11f0-8a00-0242ac130002","mfcluster":"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"},"invalidated":{"drcluster":false,"mfcluster":false},"kind":"clusterset","metadata_addresses":["mf-dr-1:3306","mf-ic-1:3306"],"name":"mfcs","primary_cluster":"mfcluster","roles":{"drcluster":"REPLICA","mfcluster":"PRIMARY"},"view_id":"4294967297"},"server_uuid":"11111111-1111-11f0-8a00-0242ac130002"}*/ ' +
  'if (session.runSql("SELECT @@global.server_uuid").fetchOne()[0] !== "11111111-1111-11f0-8a00-0242ac130002") { throw new Error("MYFLAMES_IDENTITY_MISMATCH"); } ' +
  'var myflamesClusterSet = dba.getClusterSet();';

const switchPlan = {
  schema_version: "cluster-action-1.1",
  action: "set_primary_cluster:drcluster",
  scope: "clusterset",
  via: "mysqlsh",
  eyebrow: "ClusterSet.setPrimaryCluster",
  title: "Make drcluster the primary cluster?",
  confirm_label: "Make drcluster the primary cluster",
  target_text: "drcluster",
  description: "MySQL Shell promotes drcluster and repoints replica channels.",
  session_guard: [switchGuard],
  statements: ['/*myflames:cluster_op*/ myflamesClusterSet.setPrimaryCluster("drcluster");'],
  facts: [["ClusterSet", "mfcs"], ["New primary cluster", "drcluster"]],
  warnings: [],
  typed_guard: null,
  undo: 'dba.getClusterSet().setPrimaryCluster("mfcluster");',
  next: null,
  expect: { clusterset_view_id: "4294967297" },
  timeout_s: 300,
  checks: ["Refresh ClusterSet status after the switchover."],
  policy: {
    action: "confirm",
    tone: "danger",
    title: "Production connection",
    text: "Production connection asks before every write.",
    connection_type: "production",
    read_only: false,
  },
  review_token: "token-switch-clusterset",
};

const sqlGuard =
  "SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE @@global.server_uuid <> '11111111-1111-11f0-8a00-0242ac130002');";

const sqlGuardedPlan = {
  schema_version: "cluster-action-1.1",
  action: "stop_member",
  scope: "member",
  via: "sql",
  eyebrow: "STOP GROUP_REPLICATION",
  title: "Stop Group Replication on mf-ic-1:3306?",
  confirm_label: "Stop Group Replication",
  target_text: "mf-ic-1:3306",
  description: "This member leaves the group and becomes read-only.",
  session_guard: [sqlGuard],
  statements: ["/*myflames:cluster_op*/ STOP GROUP_REPLICATION;"],
  facts: [["Runs on", "mf-ic-1:3306"]],
  warnings: [],
  typed_guard: null,
  undo: "START GROUP_REPLICATION;",
  next: null,
  expect: { engine: "mysql" },
  timeout_s: 120,
  checks: [],
  policy: {
    action: "confirm",
    tone: "danger",
    title: "Production connection",
    text: "Production connection asks before every write.",
    connection_type: "production",
    read_only: false,
  },
  review_token: "token-stop-sql",
};

const checkPlan = {
  schema_version: "cluster-action-1.1",
  action: "check_clusterset",
  scope: "clusterset",
  via: "mysqlsh",
  read_only: true,
  eyebrow: "ClusterSet.status",
  title: "Check with MySQL Shell",
  confirm_label: "Check status",
  target_text: "mfcs",
  description: "MySQL Shell opens connections to every member and reads ClusterSet status with extended level 1.",
  statements: ['print(JSON.stringify(myflamesClusterSet.status({"extended": 1}), null, 2));'],
  facts: [["ClusterSet", "mfcs"]],
  warnings: [],
  typed_guard: null,
  undo: "No change is made by this status call.",
  next: null,
  expect: { clusterset_view_id: "4294967297" },
  timeout_s: 300,
  checks: [],
  policy: {
    action: "confirm",
    tone: "primary",
    title: "Read-only check",
    text: "Runs clusterSet.status({extended: 1}) through MySQL Shell.",
    connection_type: "production",
    read_only: false,
  },
  review_token: "token-check-clusterset",
};

const extendedStatus = {
  domainName: "mfcs",
  primaryCluster: "mfcluster",
  globalPrimaryInstance: "mf-ic-1:3306",
  metadataServer: "mf-ic-1:3306",
  status: "AVAILABLE",
  statusText: "Primary Cluster available, there are issues with a Replica cluster.",
  clusters: {
    "dr<cluster>": {
      clusterRole: "REPLICA",
      globalStatus: "OK_NOT_REPLICATING",
      status: "OK",
      statusText: "Cluster is ONLINE and can tolerate up to ONE failure.",
      clusterSetReplicationStatus: "ERROR",
      clusterSetReplication: {
        receiverStatus: "ON",
        applierStatus: "ERROR",
        source: "mf-ic-1:3306",
      },
      transactionSetConsistencyStatus: "INCONSISTENT",
      transactionSetConsistencyStatusText: "There are 2 transactions that did not originate from the PRIMARY.",
      clusterErrors: [
        "WARNING: Replication from the Primary Cluster not in expected state",
        "ERROR: Errant transactions detected <script>window.__xss=2</script>",
      ],
    },
    mfcluster: {
      clusterRole: "PRIMARY",
      globalStatus: "OK",
      status: "OK",
      statusText: "Cluster is ONLINE and can tolerate up to ONE failure.",
      primary: "mf-ic-1:3306",
    },
  },
};

// ShellWriter preserves these scalar observations before redacting the
// display JSON. Stable fact labels survive passwords matching JSON keys.
const statusFacts = [
  { server: "mfcs", fact: "MySQL Shell ClusterSet status", actual: "AVAILABLE" },
  { server: "dr<cluster>", fact: "MySQL Shell globalStatus", actual: "OK_NOT_REPLICATING" },
  { server: "dr<cluster>", fact: "MySQL Shell status", actual: "OK" },
  { server: "dr<cluster>", fact: "MySQL Shell clusterRole", actual: "REPLICA" },
  { server: "dr<cluster>", fact: "MySQL Shell clusterSetReplicationStatus", actual: "ERROR" },
  { server: "dr<cluster>", fact: "MySQL Shell transactionSetConsistencyStatus", actual: "INCONSISTENT" },
  { server: "mfcluster", fact: "MySQL Shell globalStatus", actual: "OK" },
  { server: "mfcluster", fact: "MySQL Shell status", actual: "OK" },
  { server: "mfcluster", fact: "MySQL Shell clusterRole", actual: "PRIMARY" },
].map((f) => ({ ...f, expected: "reported (health is not an action effect)", status: "verified" }));
const statusVerification = { facts: statusFacts, missing: [], mismatches: [],
  summary: "MySQL Shell returned the requested diagnostic report; health is reported, not required." };

const profiles = [
  { id: "local", name: "Local member", host: "mf-ic-1" },
  { id: "remote", name: "DR member", host: "mf-dr-1" },
].map((p) => ({ ...p, port: 3306, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "",
  group: "production", type: "production", read_only: false, has_saved_password: true }));

let activeProgress = () => ({
  schema_version: "action-progress-1.0",
  phase: "checking",
  states: ["waiting"],
  status: null,
  elapsed_s: 0.4,
  waiting_s: null,
  timeout_s: 300,
  output: [],
});
let heldReply = null;
let clusterActionReply = null;
let proxyActionHeld = false;
let heldProxyReply = null;
const progressCalls = [];

const h = await open("/api/live/cluster", "action-output", {
  "/api/profiles/list": { status: 200, body: { items: profiles,
    secret_store: { backend: "vault", label: "myflames encrypted vault", available: true } } },
  "/api/connect": (body) => {
    assert.equal(body.group_name, "production");
    return { status: 200, body: { version: "8.4.3", engine: "mysql", flavor: "mysql", product: "MySQL",
      databases: [], schemas: [], group_connection: { group_name: "production",
        effective_safety: { type: "production", read_only: false }, active_member_profile_id: "local", primary_profile_id: "local",
        members: profiles.map((p, i) => ({ profile_id: p.id, profile_name: p.name, host: p.host, port: p.port,
          safety: { type: p.type, read_only: false }, server_uuid: null, role: i ? "SECONDARY" : "PRIMARY", state: null, reachable: true })) } } };
  },
  "/api/live/cluster/plan": (body) => ({
    status: 200,
    body: body.action === "check_clusterset" ? checkPlan
      : body.action === "stop_member" ? sqlGuardedPlan
        : switchPlan,
  }),
  "/api/live/cluster/action": (_body, request) => {
    if (clusterActionReply) {
      const reply = clusterActionReply;
      return { status: 200, body: reply };
    }
    heldReply = (payload) =>
      request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
    return null;
  },
  "/api/live/action/progress": (body) => {
    progressCalls.push(body);
    return { status: 200, body: activeProgress(body) };
  },
  "/api/live/proxysql": { status: 200, body: proxyLab },
  "/api/live/proxysql/plan": { status: 200, body: proxyPlan },
  "/api/live/proxysql/action": (_body, request) => {
    if (proxyActionHeld) {
      heldProxyReply = () =>
        request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(proxyResult) });
      return null;
    }
    return { status: 200, body: proxyResult };
  },
  "/api/audit/declined": { status: 200, body: { audit_id: "d1", status: "declined" } },
}, { fallback: { status: 200, body: clusterReport } });

const { page } = h;
const clusterView = '.server-view[data-view="cluster"]';
const proxyView = '.server-view[data-view="proxysql"]';
const dialogSel = "dialog.action-review[open]";

async function clickClusterSetAction(label) {
  // Opening the Group connection changes the report key. Wait for its
  // current catalog rather than clicking the retained, disabled old report.
  await page.waitForFunction((text) => [...document.querySelectorAll('.cluster-actions .review-actions > button')]
    .some((b) => b.textContent.trim() === text && b.getAttribute("aria-disabled") !== "true"), {}, label)
    .catch(async (error) => {
      const buttons = await page.$$eval('.cluster-actions .review-actions > button', (els) => els.map((b) => ({
        label: b.textContent.trim(), disabled: b.getAttribute("aria-disabled"),
        reason: document.getElementById(b.getAttribute("aria-describedby") || "")?.textContent || "" })));
      throw new Error(`${label} unavailable: ${JSON.stringify(buttons)}. ${error.message}`);
    });
  await page.evaluate((text) => {
    const btn = [...document.querySelectorAll('.cluster-actions .review-actions > button')]
      .find((b) => b.textContent.trim() === text);
    if (!btn) throw new Error(`Missing cluster action button: ${text}`);
    btn.click();
  }, label);
  await page.waitForSelector(dialogSel, { visible: true });
}

async function confirmDialog() {
  await page.click(`${dialogSel} .sheet-actions button:last-child`);
}

async function closeDialog() {
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll("dialog.action-review[open] .sheet-actions button")]
      .find((b) => b.textContent.trim() === "Close");
    if (!btn) throw new Error("Missing Close button in review sheet");
    btn.click();
  });
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
}

try {
  // Spy clipboard writes via navigator.clipboard.writeText.
  await page.evaluate(() => {
    window.__copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: (text) => {
          window.__copied.push(text);
          return Promise.resolve();
        },
      },
    });
  });

  // 1. Open a Group connection, then trigger a ClusterSet switchover action.
  await page.click('.main-nav button[data-place="cluster"]');
  await page.waitForSelector(`${clusterView} .cluster-actions`, { visible: true });
  await page.waitForSelector(".window-toolbar .connection-pill", { visible: true });
  await openConnectionPicker(page);
  await page.click('button[aria-label="Open group connection production"]');
  await page.waitForFunction(() => document.querySelector(".window-toolbar .connection-pill")?.textContent.includes("Group connection production"));
  // Loaded views retain the previous connection's report until Refresh.
  await h.refresh();
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .spinner`), {}, clusterView);

  let progressChunks = [];
  let progressPhase = "checking";
  activeProgress = () => ({
    schema_version: "action-progress-1.0",
    phase: progressPhase,
    states: [progressPhase === "checking" ? "waiting" : "sent"],
    status: null,
    elapsed_s: 1.0,
    waiting_s: progressPhase === "sending" ? 0.5 : null,
    timeout_s: 300,
    output: [...progressChunks],
  });

  await clickClusterSetAction("Make drcluster the primary cluster…");

  // C17: review shows the JavaScript identity guard sent before the ClusterSet call,
  // labelled "Identity check", and Copy copies the complete script defining myflamesClusterSet.
  const reviewGuard = await page.$eval(dialogSel, (d) => {
    const call = d.querySelector(".ops-pre code")?.textContent || "";
    const guard = d.querySelector(".action-guard");
    return {
      call,
      guardOpen: !!guard?.open,
      guardTitle: guard?.querySelector("summary")?.textContent?.replace(/\s+/g, " ").trim() || "",
      guardText: guard?.querySelector(".ops-pre code")?.textContent || "",
    };
  });
  assert.equal(reviewGuard.call, switchPlan.statements[0], "action call stays visible above the identity check");
  assert.equal(reviewGuard.guardOpen, true, "identity check is visible in the review");
  assert.equal(reviewGuard.guardTitle, "Identity check", "JS guard is labelled Identity check");
  assert.equal(reviewGuard.guardText, `${switchGuard}\n${switchPlan.statements[0]}`,
    "identity check shows the complete Shell script defining myflamesClusterSet");
  await page.click(`${dialogSel} .action-guard .ops-commands button`);
  await page.waitForFunction(() => document.querySelector("dialog.action-review[open] .action-guard [role=status]")?.textContent === "Copied.");
  assert.equal(await page.evaluate(() => window.__copied.at(-1)), `${switchGuard}\n${switchPlan.statements[0]}`,
    "Copy copies the full bound Shell script including the identity guard");
  // Responsive check of the review sheet with the open JavaScript identity guard.
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: 700 });
      await pause(40);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
      assert.equal(overflow, false, `no horizontal overflow in guarded ReviewSheet at ${width}px (${theme})`);
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  await confirmDialog();

  // Wait for at least one progress poll with empty output: live region must stay hidden.
  const startCalls = progressCalls.length;
  await page.waitForFunction((n) => window.performance && true, {}, startCalls);
  for (let i = 0; i < 40 && progressCalls.length === startCalls; i++) await pause(50);
  assert.ok(progressCalls.length > startCalls, "existing progress poll runs");
  assert.equal(await page.$(`${dialogSel} .shell-output-live`), null, "no live output region when output is empty");

  // Emit initial stdout, stderr and HTML-like server text.
  progressPhase = "sending";
  const htmlLiteral = '<img src=x onerror="window.__xss=1"><b>raw</b>';
  progressChunks = [
    { stream: "stdout", text: "Validating cluster drcluster..." },
    { stream: "stderr", text: "WARNING: Replica channel lag detected" },
    { stream: "stdout", text: htmlLiteral },
  ];
  await page.waitForSelector(`${dialogSel} .shell-output-live`, { visible: true });

  const initialLive = await page.$eval(`${dialogSel} .shell-output-live`, (sec) => {
    const log = sec.querySelector(".shell-output-log");
    const lines = [...sec.querySelectorAll(".shell-output-line")].map((el) => ({
      stream: el.dataset.stream,
      tag: el.querySelector(".shell-output-stream")?.textContent || "",
      text: el.querySelector(".shell-output-text")?.textContent || "",
    }));
    const sr = sec.querySelector('.shell-output-sr[aria-live="polite"]');
    const cs = getComputedStyle(log);
    return {
      lines,
      maxHeight: cs.maxHeight,
      overflowY: cs.overflowY,
      imgCount: log.querySelectorAll("img").length,
      boldCount: log.querySelectorAll("b").length,
      xss: window.__xss,
      srText: sr?.textContent || "",
      hasSr: !!sr,
    };
  });
  assert.equal(initialLive.lines.length, 3);
  assert.deepEqual(initialLive.lines[0], { stream: "stdout", tag: "", text: "Validating cluster drcluster..." });
  assert.deepEqual(initialLive.lines[1], { stream: "stderr", tag: "stderr", text: "WARNING: Replica channel lag detected" });
  assert.deepEqual(initialLive.lines[2], { stream: "stdout", tag: "", text: htmlLiteral });
  assert.notEqual(initialLive.maxHeight, "none", "live output region is height-bounded");
  assert.equal(initialLive.imgCount, 0, "HTML tags in server text never render as elements");
  assert.equal(initialLive.boldCount, 0, "HTML formatting tags in server text stay plain text");
  assert.equal(initialLive.xss, undefined, "no script execution from server text");
  assert.equal(initialLive.hasSr, true, "aria-live polite status region exists");
  assert.equal(initialLive.srText, htmlLiteral, "leading-edge aria-live announcement updates immediately");

  // Push burst updates quickly (within the 1500 ms throttle window) to overflow the box.
  for (let i = 1; i <= 35; i++) {
    progressChunks.push({ stream: "stdout", text: `Cloning chunk ${i} of 35...` });
  }
  await page.waitForFunction(() => document.querySelectorAll("dialog.action-review[open] .shell-output-line").length === 38);

  // Announcement should be throttled right after the burst, then update once the window passes.
  const immediateSr = await page.$eval(`${dialogSel} .shell-output-sr`, (el) => el.textContent);
  assert.equal(immediateSr, htmlLiteral, "rapid updates within the throttle window do not spam aria-live");

  // Autoscroll keeps the viewport pinned to the newest line at the bottom.
  const bottomState = await page.$eval(`${dialogSel} .shell-output-log`, (el) => ({
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
    scrollTop: el.scrollTop,
    gap: el.scrollHeight - el.scrollTop - el.clientHeight,
  }));
  assert.ok(bottomState.scrollHeight > bottomState.clientHeight, "live log overflows vertically");
  assert.ok(bottomState.gap <= 8, `autoscroll pins to bottom (gap=${bottomState.gap})`);

  // Wait for trailing throttled aria-live update.
  await page.waitForFunction(
    () => document.querySelector("dialog.action-review[open] .shell-output-sr")?.textContent === "Cloning chunk 35 of 35...",
    { timeout: 4000 },
  );

  // User scrolls up: autoscroll must pause when more output arrives.
  await page.$eval(`${dialogSel} .shell-output-log`, (el) => {
    el.scrollTop = 0;
    el.dispatchEvent(new Event("scroll"));
  });
  await page.waitForSelector(`${dialogSel} .shell-output-follow`, { visible: true });

  progressChunks.push({ stream: "stdout", text: "Step 36: repointing replica channels..." });
  await page.waitForFunction(() => document.querySelectorAll("dialog.action-review[open] .shell-output-line").length === 39);
  const scrolledState = await page.$eval(`${dialogSel} .shell-output-log`, (el) => ({
    scrollTop: el.scrollTop,
    lastLine: el.querySelector(".shell-output-line:last-child .shell-output-text")?.textContent,
  }));
  assert.equal(scrolledState.lastLine, "Step 36: repointing replica channels...", "newest line is at the bottom");
  assert.ok(scrolledState.scrollTop <= 5, `autoscroll stays paused when user scrolled up (scrollTop=${scrolledState.scrollTop})`);

  // Clicking "Jump to latest" returns to the bottom and re-enables autoscroll.
  await page.click(`${dialogSel} .shell-output-follow`);
  const afterJump = await page.$eval(`${dialogSel} .shell-output-log`, (el) => el.scrollHeight - el.scrollTop - el.clientHeight);
  assert.ok(afterJump <= 8, `Jump to latest returns to bottom (gap=${afterJump})`);

  // 2. Finish cluster action with 450 stdout lines + stderr warning (tests cap + Copy).
  const manyLines = Array.from({ length: 450 }, (_, i) => `Shell step ${i + 1}: applied`).join("\n") + "\n";
  const shellStderr = "WARNING: Router refresh is asynchronous.\n";
  assert.ok(heldReply, "cluster action reply was held");
  heldReply({
    schema_version: "cluster-action-1.1",
    action: "set_primary_cluster:drcluster",
    status: "done",
    message: "Verified by SQL on every required route.",
    statements: switchPlan.statements,
    sent: switchPlan.statements,
    not_sent: [],
    error: null,
    after: null,
    undo: switchPlan.undo,
    next: null,
    audit_id: "audit-cs-1",
    states: ["done"],
    effect: [["Primary cluster", "mfcluster", "drcluster"]],
    checks: switchPlan.checks,
    shell_output: manyLines,
    shell_stderr: shellStderr,
    outcome_class: "verified",
  });
  heldReply = null;

  await page.waitForSelector(`${dialogSel} .action-outcome[data-status="done"]`);
  assert.equal(await page.$(`${dialogSel} .shell-output-live`), null, "live region hides once finished");
  const donePill = await page.$eval(`${dialogSel} .action-outcome .status-pill`, (el) => ({ text: el.textContent, cls: el.className }));
  assert.equal(donePill.text, "Done", "a fully verified result keeps the green Done pill");
  assert.match(donePill.cls, /is-ok/);
  assert.equal(await page.$(`${dialogSel} .action-verification`), null, "no verification block without verification facts");

  const detailsCollapsed = await page.$eval(`${dialogSel} .shell-output-details`, (el) => ({
    open: el.open,
    summary: el.querySelector("summary")?.textContent?.replace(/\s+/g, " ").trim(),
  }));
  assert.equal(detailsCollapsed.open, false, "MySQL Shell output is collapsed by default");
  assert.match(detailsCollapsed.summary, /MySQL Shell output/, "summary labels MySQL Shell output");

  // Expand and verify 400-line cap, stderr marker and Copy (full text).
  await page.click(`${dialogSel} .shell-output-details > summary`);
  const expandedResult = await page.$eval(`${dialogSel} .shell-output-details`, (el) => ({
    open: el.open,
    capNote: el.querySelector(".shell-output-cap")?.textContent || "",
    count: el.querySelectorAll(".shell-output-line").length,
    first: el.querySelector(".shell-output-line:first-child .shell-output-text")?.textContent,
    last: el.querySelector(".shell-output-line:last-child .shell-output-text")?.textContent,
    lastStream: el.querySelector(".shell-output-line:last-child")?.dataset.stream,
  }));
  assert.equal(expandedResult.open, true);
  assert.match(expandedResult.capNote, /Showing the last 400 lines \(of 451\)/);
  assert.equal(expandedResult.count, 400);
  assert.equal(expandedResult.first, "Shell step 52: applied", "head lines beyond cap are omitted");
  assert.equal(expandedResult.last, "WARNING: Router refresh is asynchronous.", "stderr line appears at bottom");
  assert.equal(expandedResult.lastStream, "stderr");

  await page.click(`${dialogSel} .shell-output-copy button`);
  await page.waitForFunction(() => document.querySelector("dialog.action-review[open] .shell-output-copy [role=status]")?.textContent === "Copied.");
  const copiedCluster = await page.evaluate(() => window.__copied.at(-1));
  assert.equal(copiedCluster, `${manyLines}[stderr]\n${shellStderr}`, "Copy copies the full uncapped stdout and stderr");

  // Close sheet -> persistent ActionResult in view keeps the collapsible block.
  await closeDialog();
  await page.waitForSelector(`${clusterView} .action-result .shell-output-details`, { visible: true });
  assert.equal(await page.$eval(`${clusterView} .action-result .shell-output-details`, (el) => el.open), false);

  // 3. check_clusterset: readable ClusterSet summary ABOVE collapsible raw JSON output.
  const statusJson = JSON.stringify(extendedStatus, null, 2) + "\n";
  clusterActionReply = {
    schema_version: "cluster-action-1.1",
    action: "check_clusterset",
    status: "done",
    message: "MySQL Shell status output is included.",
    statements: checkPlan.statements,
    sent: checkPlan.statements,
    not_sent: [],
    error: null,
    after: null,
    undo: checkPlan.undo,
    next: null,
    audit_id: "audit-cs-check",
    states: ["done"],
    effect: null,
    checks: [],
    shell_output: statusJson,
    shell_stderr: "",
    outcome_class: "verified",
    verification: statusVerification,
  };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-check-summary`, { visible: true });

  const checkSheet = await page.$eval(dialogSel, (dialog) => {
    const summary = dialog.querySelector(".shell-check-summary");
    const details = dialog.querySelector(".shell-output-details");
    const rows = [...summary.querySelectorAll(".shell-check-clusters tbody tr")].map((tr) => ({
      cluster: tr.dataset.cluster,
      text: tr.textContent.replace(/\s+/g, " ").trim(),
    }));
    const errors = [...summary.querySelectorAll(".shell-check-errors li")].map((li) => li.textContent.replace(/\s+/g, " ").trim());
    return {
      summaryBeforeRaw: !!(summary.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING),
      summaryText: summary.textContent.replace(/\s+/g, " ").trim(),
      rows,
      errors,
      scriptNodes: summary.querySelectorAll("script").length,
      xss: window.__xss,
      rawOpen: details.open,
    };
  });
  assert.equal(checkSheet.summaryBeforeRaw, true, "check_clusterset summary appears above raw MySQL Shell output");
  assert.match(checkSheet.summaryText, /ClusterSet mfcs/);
  assert.match(checkSheet.summaryText, /AVAILABLE/);
  assert.equal(checkSheet.rows.length, 2);
  assert.equal(checkSheet.rows[0].cluster, "mfcluster", "primary cluster listed first");
  assert.match(checkSheet.rows[0].text, /Primary.*OK/);
  assert.equal(checkSheet.rows[1].cluster, "dr<cluster>", "literal angle brackets preserved as text");
  assert.match(checkSheet.rows[1].text, /Replica/);
  assert.match(checkSheet.rows[1].text, /OK_NOT_REPLICATING/);
  assert.match(checkSheet.rows[1].text, /ERROR/);
  assert.match(checkSheet.rows[1].text, /GTID consistency: INCONSISTENT/);
  assert.equal(checkSheet.errors.length, 0, "diagnostic text stays in the raw output rather than being parsed after redaction");
  assert.equal(checkSheet.scriptNodes, 0);
  assert.equal(checkSheet.xss, undefined);
  assert.equal(checkSheet.rawOpen, false);

  // Expand raw output and verify Copy copies valid verbatim JSON.
  await page.click(`${dialogSel} .shell-output-details > summary`);
  await page.click(`${dialogSel} .shell-output-copy button`);
  await page.waitForFunction(() => document.querySelector("dialog.action-review[open] .shell-output-copy [role=status]")?.textContent === "Copied.");
  const copiedJson = await page.evaluate(() => window.__copied.at(-1));
  assert.equal(copiedJson, statusJson, "check_clusterset Copy copies raw JSON verbatim");
  assert.deepEqual(JSON.parse(copiedJson), extendedStatus);

  // Responsive layout check (1440px and 720px, light and dark) with check summary + expanded raw output open.
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: 700 });
      await pause(60);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
      assert.equal(overflow, false, `no horizontal overflow in ReviewSheet at ${width}px (${theme})`);
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });

  // Close sheet -> persistent ActionResult also leads with summary above raw output.
  await closeDialog();
  await page.waitForSelector(`${clusterView} .action-result .shell-check-summary`, { visible: true });
  const resultOrder = await page.$eval(`${clusterView} .action-result`, (sec) => {
    const summary = sec.querySelector(".shell-check-summary");
    const details = sec.querySelector(".shell-output-details");
    return !!(summary && details && (summary.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING));
  });
  assert.equal(resultOrder, true, "ActionResult leads with check summary above MySQL Shell output");

  // Passwords can match JSON keys or punctuation. Only the backend's private
  // parse supplies summary evidence; the display text can even be invalid JSON.
  const validatedCheckReply = structuredClone(clusterActionReply);
  for (const secret of ["status", "clusters", '"']) {
    clusterActionReply = {
      ...validatedCheckReply,
      shell_output: statusJson.split(secret).join("[REDACTED]"),
      verification: { ...statusVerification, facts: statusFacts.map((f) => ({ ...f,
        server: f.server.split(secret).join("[REDACTED]"), actual: f.actual.split(secret).join("[REDACTED]") })) },
    };
    await clickClusterSetAction(checkAction.label);
    await confirmDialog();
    await page.waitForSelector(`${dialogSel} .shell-check-summary`, { visible: true });
    const redactedSummary = await h.text(`${dialogSel} .shell-check-summary`);
    assert.match(redactedSummary, /ClusterSet mfcs.*AVAILABLE/, `summary survives password ${JSON.stringify(secret)}`);
    assert.match(redactedSummary, /OK_NOT_REPLICATING/);
    assert.match(redactedSummary, /GTID consistency: INCONSISTENT/);
    assert.equal(await page.$$eval(`${dialogSel} .shell-check-summary .shell-check-clusters tbody tr`, (els) => els.length), 2);
    await closeDialog();
  }

  // Two distinct cluster names can redact to the same display value. Keep
  // separate observations instead of merging them under a name-based key.
  clusterActionReply = { ...validatedCheckReply, shell_output: "[REDACTED]",
    verification: { ...statusVerification, facts: statusFacts.map((f) => ({ ...f,
      server: f.fact === "MySQL Shell ClusterSet status" ? f.server : "[REDACTED]" })) } };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-check-summary`, { visible: true });
  const redactedRows = await page.$$eval(`${dialogSel} .shell-check-summary tbody tr`, (rows) => rows.map((r) => r.textContent));
  assert.equal(redactedRows.length, 2, "redaction does not merge distinct clusters");
  assert.match(redactedRows[0], /Primary.*OK/);
  assert.match(redactedRows[1], /Replica.*OK_NOT_REPLICATING.*INCONSISTENT/);
  await closeDialog();

  clusterActionReply = { ...validatedCheckReply, verification: undefined };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-output-details`, { visible: true });
  assert.equal(await page.$(`${dialogSel} .shell-check-summary`), null,
    "even plausible display JSON cannot replace missing backend facts");
  assert.match(await h.text(`${dialogSel} .shell-result-output`), /No structured MySQL Shell status facts were returned/);
  await closeDialog();

  // 4. check_clusterset with non-JSON shell_output falls back cleanly to raw output only,
  // and "done, not fully verified" is not a green Done: the pill says so and the
  // collapsible SQL verification block lists the facts (verified, missing, mismatch).
  clusterActionReply = {
    ...clusterActionReply,
    message: "Shell did not return the requested status report.",
    shell_output: "Shell finished with a warning\n",
    outcome_class: "done, not fully verified",
    verification: {
      summary: "Not read: DR member: live state of mf-dr-1:3306. Check mfcs: primary cluster",
      facts: [
        { server: "mfcs", fact: "ClusterSet identity", expected: "mfcs", actual: "mfcs", status: "verified" },
        { server: "mfcs", fact: "member clusters", expected: ["mfcluster", "drcluster"], actual: ["mfcluster", "drcluster"], status: "verified" },
        { server: "DR member", fact: "live state of mf-dr-1:3306", expected: "read successfully", actual: "No saved route answered", status: "missing" },
        { server: "mfcs", fact: "primary cluster", expected: "mfcluster", actual: "drcluster", status: "mismatch" },
      ],
      missing: ["DR member: live state of mf-dr-1:3306"],
      mismatches: ["mfcs: primary cluster"],
    },
  };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-output-details`, { visible: true });
  assert.equal(await page.$(`${dialogSel} .shell-check-summary`), null, "non-JSON check output shows no bogus summary");
  const partialPill = await page.$eval(`${dialogSel} .action-outcome .status-pill`, (el) => ({ text: el.textContent, cls: el.className }));
  assert.equal(partialPill.text, "Done, not fully verified", "not fully verified is not a green Done");
  assert.match(partialPill.cls, /is-watch/);
  const verification = await page.$eval(`${dialogSel} .action-verification`, (el) => ({
    open: el.open,
    summary: el.querySelector("summary")?.textContent?.replace(/\s+/g, " ").trim() || "",
    summaryText: el.querySelector(".hint")?.textContent || "",
    rows: [...el.querySelectorAll("tbody tr")].map((tr) => ({
      status: tr.dataset.status,
      text: tr.textContent.replace(/\s+/g, " ").trim(),
    })),
  }));
  assert.match(verification.summary, /SQL verification/, "the collapsible block is labelled SQL verification");
  assert.equal(verification.open, true, "verification opens when facts are missing or mismatched");
  assert.match(verification.summaryText, /Not read: DR member/);
  assert.deepEqual(verification.rows.map((r) => r.status), ["verified", "verified", "missing", "mismatch"]);
  assert.match(verification.rows[0].text, /Verified/);
  assert.match(verification.rows[1].text, /\["mfcluster","drcluster"\]/, "list values render as JSON");
  assert.match(verification.rows[2].text, /Not read/);
  assert.match(verification.rows[2].text, /No saved route answered/, "a missing fact shows why it was not read");
  assert.match(verification.rows[3].text, /Mismatch/);
  assert.match(verification.rows[3].text, /mfcluster.*drcluster/, "a mismatch shows expected and actual");
  await page.click(`${dialogSel} .shell-output-details > summary`);
  assert.match(await page.$eval(`${dialogSel} .shell-output-log`, (el) => el.textContent), /Shell finished with a warning/);
  await closeDialog();

  // The persistent result panel in the view keeps the pill and the verification block.
  await page.waitForSelector(`${clusterView} .action-result .action-verification`, { visible: true });
  assert.match(await h.text(`${clusterView} .action-result .ops-result-head .status-pill`), /^Done, not fully verified$/);
  const resultVerification = await page.$eval(`${clusterView} .action-result .action-verification`, (el) => ({
    open: el.open,
    rows: el.querySelectorAll("tbody tr").length,
  }));
  assert.equal(resultVerification.open, true);
  assert.equal(resultVerification.rows, 4, "the result panel lists every verification fact");

  // 4b. G12/C18: backend 1 MiB truncation (shell_output_truncated / shell_stderr_truncated)
  // is explicitly labelled and never claims Copy copies the full output.
  // Case A: short retained tail (under 400-line UI cap), stdout truncated by backend.
  const shortTail = "last line 1\nlast line 2\n";
  clusterActionReply = {
    schema_version: "cluster-action-1.1",
    action: "set_primary_cluster:drcluster",
    status: "done",
    message: "Verified by SQL on every required route.",
    statements: switchPlan.statements,
    sent: switchPlan.statements,
    not_sent: [],
    error: null,
    after: null,
    undo: switchPlan.undo,
    next: null,
    audit_id: "audit-cs-trunc-1",
    states: ["done"],
    effect: [["Primary cluster", "mfcluster", "drcluster"]],
    checks: [],
    shell_output: shortTail,
    shell_output_truncated: true,
    shell_stderr: "",
    shell_stderr_truncated: false,
    outcome_class: "verified",
  };
  await clickClusterSetAction("Make drcluster the primary cluster…");
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-output-details`, { visible: true });
  const truncSummary = await page.$eval(`${dialogSel} .shell-output-details > summary`, (el) => el.textContent.replace(/\s+/g, " ").trim());
  assert.match(truncSummary, /MySQL Shell output \(retained tail\)/, "truncated disclosure marks retained tail");
  await page.click(`${dialogSel} .shell-output-details > summary`);
  const truncBar = await page.$eval(`${dialogSel} .shell-output-details`, (el) => ({
    cap: el.querySelector(".shell-output-cap")?.textContent || "",
    copyBtn: el.querySelector(".shell-output-copy button")?.textContent?.trim() || "",
    copyAria: el.querySelector(".shell-output-copy button")?.getAttribute("aria-label") || "",
  }));
  assert.match(truncBar.cap, /retained tail/, "short backend-truncated tail says retained tail");
  assert.match(truncBar.cap, /stdout exceeded the 1 MiB limit/);
  assert.match(truncBar.cap, /Copy copies only the retained tail/);
  assert.doesNotMatch(truncBar.cap, /full output/i, "never claims Copy copies the full output when backend truncated");
  assert.equal(truncBar.copyBtn, "Copy retained tail");
  assert.equal(truncBar.copyAria, "Copy retained MySQL Shell output tail");
  await page.click(`${dialogSel} .shell-output-copy button`);
  await page.waitForFunction(() => document.querySelector("dialog.action-review[open] .shell-output-copy [role=status]")?.textContent === "Copied retained tail.");
  assert.equal(await page.evaluate(() => window.__copied.at(-1)), shortTail);
  await closeDialog();

  // Case B: both stdout & stderr truncated by backend AND over 400 lines (UI cap + backend cap).
  clusterActionReply = {
    ...clusterActionReply,
    audit_id: "audit-cs-trunc-2",
    shell_output: manyLines,
    shell_output_truncated: true,
    shell_stderr: shellStderr,
    shell_stderr_truncated: true,
  };
  await clickClusterSetAction("Make drcluster the primary cluster…");
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-output-details`, { visible: true });
  await page.click(`${dialogSel} .shell-output-details > summary`);
  const bothCap = await page.$eval(`${dialogSel} .shell-output-details .shell-output-cap`, (el) => el.textContent || "");
  assert.match(bothCap, /Showing the last 400 lines of the 451-line retained tail/);
  assert.match(bothCap, /stdout and stderr exceeded the 1 MiB limit/);
  assert.match(bothCap, /Copy copies only the retained tail/);
  assert.doesNotMatch(bothCap, /full output/i);
  await closeDialog();

  // Case C: truncated check output without backend facts has no summary.
  clusterActionReply = {
    schema_version: "cluster-action-1.1",
    action: "check_clusterset",
    status: "done",
    message: "Shell did not return the requested status report.",
    statements: checkPlan.statements,
    sent: checkPlan.statements,
    not_sent: [],
    error: null,
    after: null,
    undo: checkPlan.undo,
    next: null,
    audit_id: "audit-cs-check-trunc",
    states: ["done"],
    effect: null,
    checks: [],
    shell_output: statusJson,
    shell_output_truncated: true,
    shell_stderr: "",
    shell_stderr_truncated: false,
    outcome_class: "done, not fully verified",
  };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-output-details`, { visible: true });
  assert.equal(await page.$(`${dialogSel} .shell-check-summary`), null, "truncated display output supplies no status evidence");
  assert.match(await h.text(`${dialogSel} .shell-result-output`), /stdout exceeded the 1 MiB retention limit/);
  await closeDialog();

  // Retention truncates only the display copy, not the validated observations.
  clusterActionReply = { ...validatedCheckReply, shell_output: "... retained JSON tail }\n",
    shell_output_truncated: true };
  await clickClusterSetAction(checkAction.label);
  await confirmDialog();
  await page.waitForSelector(`${dialogSel} .shell-check-summary`, { visible: true });
  assert.match(await h.text(`${dialogSel} .shell-check-summary`), /ClusterSet mfcs.*AVAILABLE/);
  assert.match(await h.text(`${dialogSel} .shell-result-output`), /stdout exceeded the 1 MiB retention limit/);
  assert.doesNotMatch(await h.text(`${dialogSel} .shell-result-output`), /no structured summary/i);
  await closeDialog();

  // 4c. C17 consistency: a SQL action with session_guard uses the exact same "Identity check" label.
  await page.click(`${clusterView} .clusterset-cluster[data-cluster="mfcluster"] .clusterset-open`);
  await page.waitForSelector(`${clusterView} .cluster-card.is-local`, { visible: true });
  await page.click(`${clusterView} .cluster-card.is-local`);
  await page.waitForSelector(".view-inspector .review-actions button", { visible: true });
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll(".view-inspector .review-actions > button")]
      .find((b) => b.textContent.trim() === "Stop Group Replication…");
    if (!btn) throw new Error("Missing Stop Group Replication button");
    btn.click();
  });
  await page.waitForSelector(dialogSel, { visible: true });
  const sqlGuardCheck = await page.$eval(dialogSel, (d) => ({
    call: d.querySelector(".ops-pre code")?.textContent || "",
    title: d.querySelector(".action-guard summary")?.textContent?.replace(/\s+/g, " ").trim() || "",
    full: d.querySelector(".action-guard .ops-pre code")?.textContent || "",
  }));
  assert.equal(sqlGuardCheck.call, `${sqlGuard}\n${sqlGuardedPlan.statements[0]}`,
    "SQL review shows the identity check before the statements");
  assert.equal(sqlGuardCheck.title, "Identity check", "SQL guard uses the exact same Identity check label as JS guard");
  assert.equal(sqlGuardCheck.full, `${sqlGuard}\n${sqlGuardedPlan.statements[0]}`);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));
  await page.keyboard.press("Escape");
  await page.waitForFunction((sel) => !document.querySelector(`${sel} .view-inspector`), {}, clusterView);

  // 5. Non-cluster action (ProxySQL) shows NO live or result output region.
  activeProgress = () => proxyProgress;
  proxyActionHeld = true;
  await page.click('.main-nav button[data-place="proxysql"]');
  await page.waitForSelector(`${proxyView} tr[data-row-key="rt|32|mf-ic-2|3306"] td`, { visible: true });
  await page.click(`${proxyView} tr[data-row-key="rt|32|mf-ic-2|3306"] td`);
  await page.waitForSelector(".view-inspector .review-actions button", { visible: true });
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll(".view-inspector .review-actions button")]
      .find((b) => b.textContent.trim() === "Set OFFLINE_SOFT…");
    if (!btn) throw new Error("Missing ProxySQL action button");
    btn.click();
  });
  await page.waitForSelector(dialogSel, { visible: true });
  await confirmDialog();

  // Wait for ProxySQL progress poll to run; verify no live output region appears.
  const proxyCallsBefore = progressCalls.length;
  for (let i = 0; i < 40 && progressCalls.length === proxyCallsBefore; i++) await pause(50);
  assert.ok(progressCalls.length > proxyCallsBefore, "ProxySQL progress polled");
  assert.equal(await page.$(`${dialogSel} .shell-output-live`), null, "ProxySQL running action shows no live Shell output");

  assert.ok(heldProxyReply, "ProxySQL action reply was held");
  heldProxyReply();
  heldProxyReply = null;
  await page.waitForSelector(`${dialogSel} .action-outcome[data-status="done"]`);
  assert.equal(await page.$(`${dialogSel} .shell-result-output`), null, "ProxySQL ReviewSheet result has no Shell output");
  assert.equal(await page.$(`${dialogSel} .shell-output-details`), null);
  assert.equal(await page.$(`${dialogSel} .shell-check-summary`), null);
  assert.doesNotMatch(await h.text(dialogSel), /MySQL Shell output/);

  await closeDialog();
  await page.waitForSelector(`${proxyView} .action-result`, { visible: true });
  assert.equal(await page.$(`${proxyView} .action-result .shell-result-output`), null, "ProxySQL ActionResult has no Shell output");
  assert.doesNotMatch(await h.text(`${proxyView} .action-result`), /MySQL Shell output/);

  assert.deepEqual(h.errors, [], `unexpected page errors: ${h.errors.join("; ")}`);
  console.log("Action output: live progress, autoscroll, throttling, result cap/Copy, check_clusterset summary, and ProxySQL isolation passed.");
} finally {
  await h.close();
}
