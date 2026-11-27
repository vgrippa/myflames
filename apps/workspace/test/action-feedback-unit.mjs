// Action feedback primitives in a real browser, without a workspace server:
// useAction always ends (a summary that throws never leaves a spinner), and a
// Kill note names the connection it was sent on when another one is in use.
// Run: node test/action-feedback-unit.mjs (npm run test:action-feedback runs it first).
import assert from "node:assert/strict";
import { build } from "esbuild";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const here = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({
  stdin: {
    resolveDir: path.join(here, "../src"),
    loader: "tsx",
    contents: `
  import React, { useRef, useState } from "react";
  import { createRoot } from "react-dom/client";
  import { ActionButton, ActionStatus, ConfirmSheet, useAction } from "./kit";
  import { Commands } from "./Commands";
  import { KillResultNote, type KillNote } from "./Locks";
  import { PrivilegeConfirmLabel, privilegeButtonProps, privilegeResult } from "./privileges";
  import { WhyDisabledHost } from "./WhyDisabled";
  import { useReviewedActions, type ReviewedPlan, type ReviewedResult } from "./ActionReview";
  function Act({ id, done, failed, fail }: { id: string; done?: (n: number) => string; failed?: (e: unknown) => string; fail?: boolean }) {
    const action = useAction();
    const [runs, setRuns] = useState(0);
    return <div id={id}>
      <ActionButton busy={action.pending} busyLabel="Working…" onClick={() => {
        setRuns((n) => n + 1);
        void action.run(async () => { await new Promise((r) => setTimeout(r, 30)); if (fail) throw new Error("Refused by the server."); return 3; },
          { pending: "Working on it…", done, failed });
      }}>Run</ActionButton>
      <output className="runs">{runs}</output>
      <ActionStatus state={action} />
    </div>;
  }
  const note = (over: Partial<KillNote>): KillNote => ({ status: "killed", message: "Ended connection 53.", outcome: [], refresh: true,
    on: { key: "A", label: "Prod (app@db1:3306)" }, ...over });
  function Note({ id, n, currentKey }: { id: string; n: KillNote; currentKey?: string }) {
    const ref = useRef<HTMLDivElement>(null);
    return <div id={id}><KillResultNote note={n} noteRef={ref} currentKey={currentKey} onDismiss={() => {}} onRefresh={() => {}} /></div>;
  }
  function WhyProbe() {
    const [runs, setRuns] = useState(0);
    const denied = privilegeResult({
      state: "denied",
      missing: [["CONNECTION_ADMIN"], ["SUPER"]],
      reason: "Needs CONNECTION_ADMIN or SUPER.",
      grey_out: true,
      grant_sql: "GRANT CONNECTION_ADMIN ON *.* TO 'dba'@'%';",
    });
    const deniedNoGrant = privilegeResult({
      state: "denied",
      missing: [["GRANT OPTION"]],
      reason: "Needs GRANT OPTION.",
      grey_out: true,
      grant_sql: null,
    });
    const abuseGrant = "GRANT CONNECTION_ADMIN ON *.* TO '<img src=x onerror=window.__abuseGrant=1><svg onload=window.__abuseGrant=2>\\u200b\\u202e'@'%';";
    const deniedAbuse = privilegeResult({
      state: "denied",
      missing: [["CONNECTION_ADMIN"]],
      reason: "<img src=x onerror=window.__abuseFix=1><svg onload=window.__abuseFix=2>Needs \\u200b\\u202e privilege.",
      grey_out: true,
      grant_sql: abuseGrant,
    });
    return <div id="why-probe">
      <ActionButton id="why-denied-btn" {...privilegeButtonProps(denied, () => setRuns((n) => n + 1))}>Run gated</ActionButton>
      <ActionButton id="why-no-grant-btn" {...privilegeButtonProps(deniedNoGrant, () => setRuns((n) => n + 1))}>No grant gated</ActionButton>
      <ActionButton id="why-abuse-btn" {...privilegeButtonProps(deniedAbuse, () => setRuns((n) => n + 1))}>Abuse gated</ActionButton>
      <ActionButton id="why-busy-btn" busy busyLabel="Working…" {...privilegeButtonProps(privilegeResult(), () => setRuns((n) => n + 1), "Working…")}>Busy gated</ActionButton>
      <output className="runs">{runs}</output>
    </div>;
  }
  function CommandsProbe() {
    const [text, setText] = useState("GRANT SELECT ON *.* TO 'a'@'%';");
    return <div id="commands-probe">
      <Commands text={text} label="Grant probe" />
      <button id="cmd-set-a" type="button" onClick={() => setText("GRANT SELECT ON *.* TO 'a'@'%';")}>Set A</button>
      <button id="cmd-set-b" type="button" onClick={() => setText("GRANT UPDATE ON *.* TO 'b'@'%';")}>Set B</button>
    </div>;
  }
  function Round2Probe({ onResetKey }: { onResetKey: () => void }) {
    const [variant, setVariant] = useState<"a" | "b" | "allowed">("a");
    const [showInspector, setShowInspector] = useState(true);
    const [showDialog, setShowDialog] = useState(false);
    const [dialogMode, setDialogMode] = useState<"denied" | "pending" | "allowed" | "plain">("denied");
    const [confirms, setConfirms] = useState(0);
    const nativeDialogRef = useRef<HTMLDialogElement>(null);
    const gate = variant === "a"
      ? privilegeResult({ state: "denied", missing: [["CONNECTION_ADMIN"]], reason: "Needs CONNECTION_ADMIN.", grey_out: true, grant_sql: "GRANT CONNECTION_ADMIN ON *.* TO 'dba'@'%';" })
      : variant === "b"
        ? privilegeResult({ state: "denied", missing: [["SYSTEM_USER"]], reason: "Needs SYSTEM_USER.", grey_out: true, grant_sql: "GRANT SYSTEM_USER ON *.* TO 'dba'@'%';" })
        : privilegeResult({ state: "allowed", missing: [], reason: "Allowed." });
    const confirmGate = dialogMode === "denied"
      ? privilegeResult({ state: "denied", missing: [["SYSTEM_USER"]], reason: "Needs SYSTEM_USER.", grey_out: true, grant_sql: "GRANT SYSTEM_USER ON *.* TO 'dba'@'%';" })
      : dialogMode === "pending"
        ? { ...privilegeResult({ state: "allowed", missing: [], reason: "Allowed." }), disabledReason: "Preparing the action for review." }
        : privilegeResult({ state: "allowed", missing: [], reason: "Allowed." });
    return <div id="round2-probe">
      <button id="r2-set-a" type="button" onClick={() => setVariant("a")}>Set variant A</button>
      <button id="r2-set-b" type="button" onClick={() => setVariant("b")}>Set variant B</button>
      <button id="r2-set-allowed" type="button" onClick={() => setVariant("allowed")}>Set allowed</button>
      <button id="r2-reset-key" type="button" onClick={onResetKey}>Bump resetKey</button>
      <button id="r2-toggle-inspector" type="button" onClick={() => setShowInspector((v) => !v)}>Toggle inspector</button>
      <button id="r2-open-sheet" type="button" onClick={() => { setDialogMode("denied"); setShowDialog(true); }}>Open sheet</button>
      <button id="r2-sheet-pending" type="button" onClick={() => setDialogMode("pending")}>Sheet pending</button>
      <button id="r2-sheet-allowed" type="button" onClick={() => setDialogMode("allowed")}>Sheet allowed</button>
      <button id="r2-sheet-plain" type="button" onClick={() => setDialogMode("plain")}>Sheet plain</button>
      <button id="r2-open-native" type="button" onClick={() => nativeDialogRef.current?.showModal()}>Open native</button>
      <button id="r2-close-native" type="button" onClick={() => nativeDialogRef.current?.close()}>Close native</button>
      <output id="r2-confirms">{confirms}</output>
      <button id="r2-aria-btn" type="button" aria-haspopup="menu" aria-expanded={false}
        {...privilegeButtonProps(gate, () => {})}>Menu gated</button>
      {showInspector && (
        <div className="view-inspector" id="r2-inspector">
          <button id="r2-inspector-btn" type="button" {...privilegeButtonProps(gate, () => {})}>Inspector gated</button>
        </div>
      )}
      <dialog id="r2-native-dialog" ref={nativeDialogRef}>
        <button id="r2-native-btn" type="button" {...privilegeButtonProps(gate, () => {})}>Native dialog gated</button>
      </dialog>
      {showDialog && (
        <ConfirmSheet
          className="r2-sheet"
          title="Confirm probe?"
          confirmLabel={dialogMode === "plain"
            ? "Plain confirm"
            : <PrivilegeConfirmLabel gate={confirmGate} showStatus={false}>Run confirm</PrivilegeConfirmLabel>}
          onCancel={() => setShowDialog(false)}
          onConfirm={() => { if (dialogMode === "allowed" || dialogMode === "plain") setConfirms((n) => n + 1); }}
        >
          <p>Probe sheet body.</p>
        </ConfirmSheet>
      )}
    </div>;
  }
  function ActionReviewProbe() {
    const conn = { host: "127.0.0.1", port: 6032, user: "radmin", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "development" as const };
    const resolverRef = useRef<((value: ReviewedResult) => void) | null>(null);
    const actions = useReviewedActions({
      connection: conn,
      operation: "proxysql_action",
      send: (_c, plan) => new Promise<ReviewedResult>((resolve) => {
        (window as unknown as { __reviewResolve?: (r: ReviewedResult) => void }).__reviewResolve = resolve;
        (window as unknown as { __lastSentPlan?: ReviewedPlan }).__lastSentPlan = plan;
        resolverRef.current = resolve;
      }),
    });
    const guardedPlan: ReviewedPlan = {
      schema_version: "proxysql-action-1.0",
      action: "server_offline_soft",
      eyebrow: "UPDATE",
      title: "Set mf-ic-2:3306 to OFFLINE_SOFT?",
      confirm_label: "Set OFFLINE_SOFT",
      description: "No new traffic goes to mf-ic-2:3306.",
      target_text: "mf-ic-2:3306 (hostgroup 30)",
      session_guard: [
        "INSERT INTO global_variables (variable_name, variable_value) SELECT NULL, NULL WHERE (SELECT variable_value FROM disk.global_settings WHERE variable_name = 'uuid') IS NOT '11111111-2222-3333-4444-555555555555';",
      ],
      statements: [
        "/*myflames:proxysql_op*/ UPDATE mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostgroup_id = 30 AND hostname = 'mf-ic-2' AND port = 3306;",
        "LOAD MYSQL SERVERS TO RUNTIME;",
      ],
      facts: [["ProxySQL", "3.0.11"]],
      warnings: [],
      typed_guard: null,
      undo: "UPDATE mysql_servers SET status = 'ONLINE' WHERE hostgroup_id = 30 AND hostname = 'mf-ic-2' AND port = 3306; then LOAD MYSQL SERVERS TO RUNTIME;",
      expect: { server_identity: { uuid: "11111111-2222-3333-4444-555555555555" } },
      timeout_s: 60,
      policy: { action: "confirm", tone: "danger", title: "Risky write", text: "Development connection.", connection_type: "development", read_only: false },
      review_token: "unit-token-guarded",
    };
    const unguardedPlan: ReviewedPlan = {
      ...guardedPlan,
      schema_version: "readyset-action-1.0",
      action: "flush_cache",
      eyebrow: "FLUSH CACHE",
      title: "Flush shallow cache c_orders?",
      confirm_label: "Flush cache",
      target_text: "Shallow cache c_orders",
      session_guard: [],
      statements: ["FLUSH CACHE \`c_orders\`;"],
      undo: "Entries repopulate automatically.",
      review_token: "unit-token-unguarded",
    };
    // A SQL Cluster action: the backend DELIMITER-frames this guard into the
    // same packet as each statement (dbops.target_guarded_script).
    const clusterGuardedPlan: ReviewedPlan = {
      schema_version: "cluster-action-1.1",
      action: "switchover",
      eyebrow: "SELECT group_replication_set_as_primary",
      title: "Make mf-ic-1:3306 the primary?",
      confirm_label: "Make mf-ic-1:3306 primary",
      description: "The current primary becomes a read-only secondary.",
      target_text: "mf-ic-1:3306",
      session_guard: [
        "SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE @@global.server_uuid <> '1062e681-cc92-11f1-83dd-b6daec340a27');",
      ],
      statements: ["/*myflames:cluster_op*/ SELECT group_replication_set_as_primary('1062e681-cc92-11f1-83dd-b6daec340a27');"],
      facts: [["Server", "MySQL 8.4.11"]],
      warnings: [],
      typed_guard: null,
      undo: "SELECT group_replication_set_as_primary('10a527c5-cc92-11f1-956e-2a01c1402350'); makes mf-ic-2:3306 the primary again.",
      expect: { engine: "mysql", server: { server_uuid: "1062e681-cc92-11f1-83dd-b6daec340a27" } },
      timeout_s: 120,
      policy: { action: "confirm", tone: "danger", title: "Risky write", text: "Development connection.", connection_type: "development", read_only: false },
      review_token: "unit-token-cluster",
    };
    return <div id="action-review-probe">
      <button id="ar-open-guarded" type="button" onClick={() => { void actions.review("guarded", async () => guardedPlan); }}>Open guarded</button>
      <button id="ar-open-unguarded" type="button" onClick={() => { void actions.review("unguarded", async () => unguardedPlan); }}>Open unguarded</button>
      <button id="ar-open-cluster" type="button" onClick={() => { void actions.review("cluster", async () => clusterGuardedPlan); }}>Open cluster</button>
      {actions.sheet}
    </div>;
  }
  function Demo() {
    const [resetKey, setResetKey] = useState(0);
    return <WhyDisabledHost resetKey={String(resetKey)}>
      <Act id="throws-done" done={() => { throw new Error("formatter bug"); }} />
      <Act id="throws-failed" fail failed={() => { throw new Error("formatter bug"); }} />
      <Act id="plain" done={(n) => n + " rows."} />
      <Note id="note-other" n={note({})} currentKey="B" />
      <Note id="note-same" n={note({})} currentKey="A" />
      <Note id="note-late" n={note({ late: true })} />
      <Note id="note-unknown" n={note({})} />
      <WhyProbe />
      <CommandsProbe />
      <Round2Probe onResetKey={() => setResetKey((n) => n + 1)} />
      <ActionReviewProbe />
    </WhyDisabledHost>;
  }
  createRoot(document.getElementById("root")!).render(<Demo />);
`,
  },
  bundle: true, platform: "browser", format: "iife", write: false, jsx: "automatic", loader: { ".css": "empty" },
  define: { "process.env.NODE_ENV": '"production"' },
});
const css = (await Promise.all(["style.css", "kit.css", "catalog-actions.css", "ops.css"].map((file) => readFile(path.join(here, "../src", file), "utf8")))).join("\n");
const browser = await puppeteer.launch({ headless: "shell", pipe: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/auth/session") {
      return request.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ token: "unit-token" }) });
    }
    if (pathname === "/api/live/action/progress") {
      return request.respond({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          schema_version: "action-progress-1.0",
          phase: "sending",
          states: ["sent", "sent"],
          status: null,
          elapsed_s: 0.4,
          waiting_s: 0.4,
          timeout_s: 60,
        }),
      });
    }
    if (pathname === "/api/audit/declined") {
      return request.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ audit_id: "declined-1", status: "declined" }) });
    }
    return request.respond({ status: 200, contentType: "text/html", body: `<style>${css}</style><div id="root"></div>` });
  });
  await page.goto("http://feedback.test/");
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  await page.waitForSelector("#plain button");

  const settle = (id) => page.waitForFunction((sel) => {
    const button = document.querySelector(`${sel} button`);
    return button && button.getAttribute("aria-busy") !== "true" && document.querySelector(`${sel} .action-status`).dataset.phase !== "pending";
  }, { timeout: 3000 }, `#${id}`).catch(() => { throw Error(`${id} is still pending`); });
  const line = (id) => page.$eval(`#${id} .action-status`, (e) => ({ phase: e.dataset.phase, text: e.textContent.trim() }));

  // 1. A done summary that throws: the action still ends, with plain words, and runs again.
  await page.click("#throws-done button");
  await settle("throws-done");
  assert.deepEqual(await line("throws-done"), { phase: "done", text: "Done." }, "a throwing done summary ends as Done.");
  await page.click("#throws-done button");
  await settle("throws-done");
  assert.equal(await page.$eval("#throws-done .runs", (e) => e.textContent), "2", "the action runs again");

  // 2. A failed summary that throws: the error's own words.
  await page.click("#throws-failed button");
  await settle("throws-failed");
  assert.deepEqual(await line("throws-failed"), { phase: "failed", text: "Refused by the server." });

  // 3. The ordinary path is unchanged.
  await page.click("#plain button");
  await settle("plain");
  assert.deepEqual(await line("plain"), { phase: "done", text: "3 rows." });

  // 4. Kill notes: another connection in use is named, without Refresh; the
  // same one is not; without the key, only a late note is named.
  const kill = (id) => page.$eval(`#${id} .kill-result`, (e) => ({
    on: e.querySelector(".kill-result-on")?.textContent || "",
    refresh: [...e.querySelectorAll("button")].some((b) => b.textContent === "Refresh"),
  }));
  assert.deepEqual(await kill("note-other"), { on: "Sent on Prod (app@db1:3306), not the connection in use.", refresh: false });
  assert.deepEqual(await kill("note-same"), { on: "", refresh: true });
  assert.deepEqual(await kill("note-late"), { on: "Sent on Prod (app@db1:3306).", refresh: true });
  assert.deepEqual(await kill("note-unknown"), { on: "", refresh: true });

  // 5. Privilege-disabled ActionButton opens WhyDisabled with reason, backend
  // grant_sql (never browser-built SQL) and accessible non-modal dialog role;
  // Escape returns focus; null grant_sql renders no GRANT; busy opens nothing.
  await page.click("#why-denied-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  const whyText = await page.$eval(".why-disabled:popover-open", (e) => ({
    role: e.getAttribute("role"),
    ariaLabel: e.getAttribute("aria-label"),
    ariaModal: e.getAttribute("aria-modal"),
    text: e.textContent || "",
    grant: e.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.equal(whyText.role, "dialog", "WhyDisabled exposes a non-modal dialog role");
  assert.equal(whyText.ariaLabel, "Why Run gated is unavailable", "WhyDisabled exposes an accessible name");
  assert.equal(whyText.ariaModal, null, "WhyDisabled is non-modal");
  assert.match(whyText.text, /CONNECTION_ADMIN/);
  assert.equal(whyText.grant, "GRANT CONNECTION_ADMIN ON *.* TO 'dba'@'%';");
  // W5: verify the Chromium accessibility tree exposes the named dialog and its controls.
  const findAx = (node, pred) => {
    if (!node) return null;
    if (pred(node)) return node;
    for (const child of node.children || []) {
      const hit = findAx(child, pred);
      if (hit) return hit;
    }
    return null;
  };
  const ax = await page.accessibility.snapshot();
  const dialogAx = findAx(ax, (n) => n.role === "dialog" && n.name === "Why Run gated is unavailable");
  assert.ok(dialogAx, `accessibility tree exposes named dialog: ${JSON.stringify(ax)}`);
  assert.ok(findAx(dialogAx, (n) => n.role === "button" && n.name === "Copy: Grant for Run gated"), "Copy button is inside the named dialog");
  assert.ok(findAx(dialogAx, (n) => n.role === "button" && n.name === "Close"), "Close button is inside the named dialog");
  assert.equal(await page.$eval("#why-probe .runs", (e) => e.textContent), "0");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  assert.equal(await page.evaluate(() => document.activeElement?.id), "why-denied-btn", "Escape returns focus to the disabled ActionButton");
  // Null grant_sql renders the reason without any GRANT block.
  await page.click("#why-no-grant-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  const noGrant = await page.$eval(".why-disabled:popover-open", (e) => ({
    text: e.textContent || "",
    pre: e.querySelector(".ops-pre"),
  }));
  assert.match(noGrant.text, /GRANT OPTION/);
  assert.equal(noGrant.pre, null, "null grant_sql renders no GRANT block");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  // W11: markup + U+200B + U+202E in reason and grant_sql stay literal text,
  // and Copy returns grant_sql verbatim; double click + Enter/Space burst runs 0 times.
  await page.evaluate(() => {
    window.__copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (t) => { window.__copied.push(t); return Promise.resolve(); } },
    });
  });
  await page.click("#why-abuse-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  const abuseCheck = await page.$eval(".why-disabled:popover-open", (e) => ({
    elements: e.querySelectorAll("img, script, svg:not(.why-disabled-close svg)").length,
    grant: e.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.equal(abuseCheck.elements, 0, "markup in reason and grant_sql never becomes DOM elements");
  assert.equal(await page.evaluate(() => window.__abuseFix || window.__abuseGrant || 0), 0, "markup never executes");
  await page.click(".why-disabled:popover-open .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector(".why-disabled:popover-open .ops-commands [role=status]")?.textContent || ""));
  const expectedAbuseGrant = "GRANT CONNECTION_ADMIN ON *.* TO '<img src=x onerror=window.__abuseGrant=1><svg onload=window.__abuseGrant=2>\u200b\u202e'@'%';";
  assert.equal(await page.evaluate(() => window.__copied.at(-1)), expectedAbuseGrant, "Copy returns grant_sql verbatim including U+200B and U+202E");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  await page.click("#why-denied-btn", { clickCount: 2 });
  await page.focus("#why-denied-btn");
  for (const k of ["Enter", "Space", "Enter", "Space", "Enter"]) await page.keyboard.press(k);
  assert.equal(await page.$eval("#why-probe .runs", (e) => e.textContent), "0", "double click + Enter/Space burst never runs the gated handler");
  await page.keyboard.press("Escape").catch(() => {});
  await page.click("#why-busy-btn");
  assert.equal(await page.$(".why-disabled:popover-open"), null, "a busy ActionButton opens no disabled message");
  assert.equal(await page.$eval("#why-probe .runs", (e) => e.textContent), "0");

  // 6. W10: Commands resets Copied status on text change and ignores late
  // clipboard completions for older text.
  await page.evaluate(() => {
    window.__clipCalls = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: (t) => new Promise((resolve, reject) => {
          window.__clipCalls.push({ text: t, resolve, reject });
        }),
      },
    });
  });
  await page.click("#commands-probe .ops-commands button");
  await page.evaluate(() => window.__clipCalls[0].resolve());
  await page.waitForFunction(() => document.querySelector("#commands-probe [role=status]")?.textContent === "Copied.");
  // Text changes after settled copy: status resets immediately and stays reset if toggled back.
  await page.click("#cmd-set-b");
  assert.equal(await page.$("#commands-probe [role=status]"), null, "Commands resets Copied status when text changes");
  await page.click("#cmd-set-a");
  assert.equal(await page.$("#commands-probe [role=status]"), null, "toggling back to prior text does not restore stale Copied status");
  // Pending copy for text A, text changes to B before A resolves: late completion is ignored.
  await page.click("#commands-probe .ops-commands button");
  await page.click("#cmd-set-b");
  await page.evaluate(() => window.__clipCalls[1].resolve());
  await new Promise((r) => setTimeout(r, 40));
  assert.equal(await page.$("#commands-probe [role=status]"), null, "late clipboard completion for older text is ignored");
  // Copying the new text B still settles cleanly.
  await page.click("#commands-probe .ops-commands button");
  await page.evaluate(() => window.__clipCalls[2].resolve());
  await page.waitForFunction(() => document.querySelector("#commands-probe [role=status]")?.textContent === "Copied.");
  assert.equal(await page.evaluate(() => window.__clipCalls[2].text), "GRANT UPDATE ON *.* TO 'b'@'%';");

  // 7. Round-2 fixes: V8 (restore React's aria-haspopup & aria-expanded),
  // V1 (live reason/GRANT update while disabled + resetKey closes host),
  // V2 (parent removal observer + dialog open attribute), V6 (PrivilegeConfirmLabel
  // unmount/gate cleanup), S-F1 + V9 (focus moves to Cancel, never enabled confirm;
  // Copy inside modal dialog; ConfirmSheet bursts).
  assert.deepEqual(
    await page.$eval("#r2-aria-btn", (b) => ({ hp: b.getAttribute("aria-haspopup"), exp: b.getAttribute("aria-expanded") })),
    { hp: "menu", exp: "false" },
    "V8: button starts with React's aria-haspopup and aria-expanded",
  );
  await page.click("#r2-aria-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  // V1: live update reason + GRANT while disabled.
  await page.evaluate(() => document.getElementById("r2-set-b").click());
  await page.waitForFunction(() => document.querySelector(".why-disabled:popover-open .ops-pre code")?.textContent === "GRANT SYSTEM_USER ON *.* TO 'dba'@'%';");
  assert.match(await page.$eval(".why-disabled:popover-open", (e) => e.textContent || ""), /Needs SYSTEM_USER\./);
  // Close via Escape -> V8 restores React's aria-haspopup="menu" and aria-expanded="false".
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  assert.deepEqual(
    await page.$eval("#r2-aria-btn", (b) => ({ hp: b.getAttribute("aria-haspopup"), exp: b.getAttribute("aria-expanded") })),
    { hp: "menu", exp: "false" },
    "V8: closing WhyDisabled restores React's aria-haspopup and aria-expanded instead of removing them",
  );
  // V1: resetKey change closes an open host message (even without outside pointerdown).
  await page.click("#r2-aria-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  await page.evaluate(() => document.getElementById("r2-reset-key").click());
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  // V2a: removing removalRoot (.view-inspector itself) from its parent closes the host message.
  await page.click("#r2-inspector-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  await page.evaluate(() => document.getElementById("r2-toggle-inspector").click());
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  await page.evaluate(() => document.getElementById("r2-toggle-inspector").click());
  // V2b: closing a <dialog> (removing its open attribute) closes the host message.
  await page.evaluate(() => document.getElementById("r2-open-native").click());
  await page.waitForSelector("#r2-native-dialog[open]", { visible: true });
  await page.click("#r2-native-btn");
  await page.waitForSelector("#r2-native-dialog .why-disabled:popover-open", { visible: true });
  await page.evaluate(() => document.getElementById("r2-close-native").click());
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  // V6 + S-F1 + V9 on ConfirmSheet with PrivilegeConfirmLabel:
  await page.evaluate(() => {
    window.__r2Copied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (t) => { window.__r2Copied.push(t); return Promise.resolve(); } },
    });
  });
  await page.click("#r2-open-sheet");
  await page.waitForSelector("dialog.r2-sheet[open]", { visible: true });
  const r2Confirm = "dialog.r2-sheet .sheet-actions button:last-of-type";
  await page.focus(r2Confirm);
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.r2-sheet .why-disabled:popover-open", { visible: true });
  await page.click("dialog.r2-sheet .why-disabled:popover-open .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector("dialog.r2-sheet .why-disabled:popover-open .ops-commands [role=status]")?.textContent || ""));
  assert.equal(await page.evaluate(() => window.__r2Copied.at(-1)), "GRANT SYSTEM_USER ON *.* TO 'dba'@'%';", "V9: Copy works inside ConfirmSheet modal");
  // S1 + H-1: focus on GRANT text, gate flips to allowed -> focus lands on Cancel,
  // never even transiently hits the enabled confirm during hidePopover, and Enter sends 0 confirms.
  await page.focus("dialog.r2-sheet .why-disabled:popover-open .ops-pre");
  await page.evaluate((s) => {
    window.__h1EnabledFocus = [];
    const onFocusIn = (e) => {
      if (e.target instanceof HTMLElement && e.target.matches(s) && e.target.getAttribute("aria-disabled") !== "true") {
        window.__h1EnabledFocus.push("s1");
      }
    };
    window.__h1Cleanup = () => document.removeEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusin", onFocusIn, true);
  }, r2Confirm);
  await page.evaluate(() => document.getElementById("r2-sheet-allowed").click());
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet .why-disabled:popover-open"));
  assert.deepEqual(await page.evaluate(() => { window.__h1Cleanup(); return window.__h1EnabledFocus; }), [],
    "H-1 S1: enabled confirm never receives even transient focusin during hidePopover");
  assert.equal(await page.evaluate((s) => document.activeElement?.matches(s), r2Confirm), false, "S-F1 S1: focus never lands on enabled confirm");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim()), "Cancel", "S-F1 S1: focus moves to Cancel");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet[open]"));
  assert.equal(await page.$eval("#r2-confirms", (e) => e.textContent), "0", "S-F1 S1: Enter after gate opened sends 0 confirms");
  // S2 + H-1: focus on Close while review pending, review arrives -> focus lands on Cancel,
  // never transiently hits enabled confirm, and Space sends 0 confirms.
  await page.click("#r2-open-sheet");
  await page.waitForSelector("dialog.r2-sheet[open]", { visible: true });
  await page.evaluate(() => document.getElementById("r2-sheet-pending").click());
  await page.focus(r2Confirm);
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.r2-sheet .why-disabled:popover-open", { visible: true });
  await page.focus("dialog.r2-sheet .why-disabled:popover-open .why-disabled-close");
  await page.evaluate((s) => {
    window.__h1EnabledFocus = [];
    const onFocusIn = (e) => {
      if (e.target instanceof HTMLElement && e.target.matches(s) && e.target.getAttribute("aria-disabled") !== "true") {
        window.__h1EnabledFocus.push("s2");
      }
    };
    window.__h1Cleanup = () => document.removeEventListener("focusin", onFocusIn, true);
    document.addEventListener("focusin", onFocusIn, true);
  }, r2Confirm);
  await page.evaluate(() => document.getElementById("r2-sheet-allowed").click());
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet .why-disabled:popover-open"));
  assert.deepEqual(await page.evaluate(() => { window.__h1Cleanup(); return window.__h1EnabledFocus; }), [],
    "H-1 S2: enabled confirm never receives even transient focusin during hidePopover");
  assert.equal(await page.evaluate((s) => document.activeElement?.matches(s), r2Confirm), false, "S-F1 S2: focus never lands on enabled confirm");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent?.trim()), "Cancel", "S-F1 S2: focus moves to Cancel");
  await page.keyboard.press("Space");
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet[open]"));
  assert.equal(await page.$eval("#r2-confirms", (e) => e.textContent), "0", "S-F1 S2: Space after review arrives sends 0 confirms");
  // H-1 closeToAnchor: if the anchor is already enabled when Close runs (before
  // the observer dismisses), focus moves to Cancel before hidePopover so the
  // enabled confirm never receives focusin.
  await page.click("#r2-open-sheet");
  await page.waitForSelector("dialog.r2-sheet[open]", { visible: true });
  await page.focus(r2Confirm);
  await page.keyboard.press("Enter");
  await page.waitForSelector("dialog.r2-sheet .why-disabled:popover-open", { visible: true });
  await page.focus("dialog.r2-sheet .why-disabled:popover-open .why-disabled-close");
  const closeToAnchorResult = await page.evaluate((s) => {
    const confirmBtn = document.querySelector(s);
    const closeBtn = document.querySelector("dialog.r2-sheet .why-disabled:popover-open .why-disabled-close");
    const enabledFocus = [];
    const onFocusIn = (e) => {
      if (e.target === confirmBtn && confirmBtn.getAttribute("aria-disabled") !== "true") enabledFocus.push("close");
    };
    document.addEventListener("focusin", onFocusIn, true);
    confirmBtn.setAttribute("aria-disabled", "false");
    closeBtn.click();
    document.removeEventListener("focusin", onFocusIn, true);
    return { enabledFocus, activeText: (document.activeElement?.textContent || "").trim() };
  }, r2Confirm);
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet .why-disabled:popover-open"));
  assert.deepEqual(closeToAnchorResult, { enabledFocus: [], activeText: "Cancel" },
    "H-1 closeToAnchor: moves focus to Cancel before hidePopover when anchor is no longer disabled");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet[open]"));
  // V6: unmounting PrivilegeConfirmLabel restores all attributes it set on the confirm button.
  await page.click("#r2-open-sheet");
  await page.waitForSelector("dialog.r2-sheet[open]", { visible: true });
  assert.equal(await page.$eval(r2Confirm, (b) => b.getAttribute("aria-disabled")), "true");
  assert.equal(await page.$eval(r2Confirm, (b) => b.getAttribute("data-privilege-state")), "denied");
  await page.evaluate(() => document.getElementById("r2-sheet-plain").click());
  await page.waitForFunction((s) => document.querySelector(s)?.textContent?.trim() === "Plain confirm", {}, r2Confirm);
  assert.deepEqual(
    await page.$eval(r2Confirm, (b) => ({
      ariaDisabled: b.getAttribute("aria-disabled"),
      privilegeState: b.getAttribute("data-privilege-state"),
      title: b.getAttribute("title"),
      describedBy: b.getAttribute("aria-describedby"),
      whyReason: b.getAttribute("data-why-reason"),
      whyGrant: b.getAttribute("data-why-grant"),
      hasClass: b.classList.contains("privilege-action"),
    })),
    {
      ariaDisabled: null,
      privilegeState: null,
      title: null,
      describedBy: null,
      whyReason: null,
      whyGrant: null,
      hasClass: false,
    },
    "V6: unmounting PrivilegeConfirmLabel restores all button attributes",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.r2-sheet[open]"));

  // 8. WB6: ActionReview shows and copies plan.session_guard lines first,
  // labelled as the identity check sent before the statements, while progress
  // states still map to plan.statements alone.
  await page.evaluate(() => {
    window.__arCopied = [];
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: (t) => { window.__arCopied.push(t); return Promise.resolve(); } },
    });
  });
  await page.click("#ar-open-guarded");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  const guardLine = "INSERT INTO global_variables (variable_name, variable_value) SELECT NULL, NULL WHERE (SELECT variable_value FROM disk.global_settings WHERE variable_name = 'uuid') IS NOT '11111111-2222-3333-4444-555555555555';";
  const stmts = [
    "/*myflames:proxysql_op*/ UPDATE mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostgroup_id = 30 AND hostname = 'mf-ic-2' AND port = 3306;",
    "LOAD MYSQL SERVERS TO RUNTIME;",
  ];
  const expectedGuarded = [guardLine, ...stmts].join("\n");
  const guardedView = await page.$eval("dialog.action-review[open]", (d) => ({
    note: d.querySelector(".action-guard-note")?.textContent || "",
    preLabel: d.querySelector(".ops-pre")?.getAttribute("aria-label") || "",
    copyLabel: d.querySelector(".ops-commands button")?.getAttribute("aria-label") || "",
    code: d.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.match(guardedView.note, /Identity check sent first.*before the statements/i);
  assert.match(guardedView.preLabel, /Identity check sent first, before the statements/i);
  assert.match(guardedView.copyLabel, /Identity check sent first, before the statements/i);
  assert.equal(guardedView.code, expectedGuarded, "review sheet shows session_guard lines before statements");
  await page.click("dialog.action-review[open] .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector("dialog.action-review[open] .ops-commands [role=status]")?.textContent || ""));
  assert.equal(await page.evaluate(() => window.__arCopied.at(-1)), expectedGuarded, "Copy includes session_guard lines before statements");
  await page.click("dialog.action-review[open] .sheet-actions button:last-child");
  await page.waitForFunction(() => /Sent\. Waiting for the server's answer/.test(document.querySelector("dialog.action-review[open] .action-phase")?.textContent || ""));
  const sendingSteps = await page.$$eval("dialog.action-review[open] .action-steps li", (els) =>
    els.map((li) => ({ state: li.dataset.state, code: li.querySelector("code")?.textContent || "" })));
  assert.deepEqual(sendingSteps, stmts.map((s) => ({ state: "sent", code: s })), "progress states map 1:1 to reviewed statements without session_guard");
  await page.evaluate((statements) => {
    window.__reviewResolve({
      schema_version: "proxysql-action-1.0",
      action: "server_offline_soft",
      status: "done",
      message: "Changed and loaded to runtime.",
      statements,
      sent: statements,
      not_sent: [],
      error: null,
      after: null,
      undo: null,
      next: null,
      audit_id: "audit-wb6",
      states: ["done", "done"],
    });
  }, stmts);
  await page.waitForSelector("dialog.action-review[open] .action-outcome");
  const doneSteps = await page.$$eval("dialog.action-review[open] .action-steps li", (els) =>
    els.map((li) => ({ state: li.dataset.state, code: li.querySelector("code")?.textContent || "" })));
  assert.deepEqual(doneSteps, stmts.map((s) => ({ state: "done", code: s })), "final states map 1:1 to reviewed statements");
  await page.click("dialog.action-review[open] .sheet-actions button:last-child");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));

  // Unguarded plans omit the guard note and show/copy only statements.
  await page.click("#ar-open-unguarded");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  assert.equal(await page.$("dialog.action-review[open] .action-guard-note"), null, "no guard note when session_guard is empty");
  assert.equal(await page.$eval("dialog.action-review[open] .ops-pre code", (e) => e.textContent), "FLUSH CACHE `c_orders`;");
  await page.click("dialog.action-review[open] .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector("dialog.action-review[open] .ops-commands [role=status]")?.textContent || ""));
  assert.equal(await page.evaluate(() => window.__arCopied.at(-1)), "FLUSH CACHE `c_orders`;");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));

  // 9. WB round 2 #4: a MySQL/MariaDB guard travels DELIMITER-framed in the
  // same packet as each statement (dbops.target_guarded_script), so the
  // joined text cannot reproduce the send. The sheet shows and copies the
  // check first, labelled as the reviewed statements for reading, never
  // "exactly as sent" and never a script to paste.
  await page.click("#ar-open-cluster");
  await page.waitForSelector("dialog.action-review[open]", { visible: true });
  const clusterGuardLine = "SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE @@global.server_uuid <> '1062e681-cc92-11f1-83dd-b6daec340a27');";
  const clusterStmt = "/*myflames:cluster_op*/ SELECT group_replication_set_as_primary('1062e681-cc92-11f1-83dd-b6daec340a27');";
  const expectedCluster = [clusterGuardLine, clusterStmt].join("\n");
  const clusterView = await page.$eval("dialog.action-review[open]", (d) => ({
    note: d.querySelector(".action-guard-note")?.textContent || "",
    preLabel: d.querySelector(".ops-pre")?.getAttribute("aria-label") || "",
    copyLabel: d.querySelector(".ops-commands button")?.getAttribute("aria-label") || "",
    code: d.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.match(clusterView.note, /Identity check \(first line below\): the backend sends it in the same packet as each statement/i);
  assert.match(clusterView.note, /not a script to paste/i);
  assert.match(clusterView.preLabel, /not a script to paste/i);
  assert.match(clusterView.copyLabel, /not a script to paste/i);
  assert.doesNotMatch([clusterView.note, clusterView.preLabel, clusterView.copyLabel].join(" "), /exactly as sent/i,
    "a packet-framed guard never claims the shown text is exactly as sent");
  assert.equal(clusterView.code, expectedCluster, "the packet-framed sheet still shows the check before the statements");
  await page.click("dialog.action-review[open] .ops-commands button");
  await page.waitForFunction(() => /Copied\./.test(document.querySelector("dialog.action-review[open] .ops-commands [role=status]")?.textContent || ""));
  assert.equal(await page.evaluate(() => window.__arCopied.at(-1)), expectedCluster, "Copy still puts the reviewed text on the clipboard");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.action-review[open]"));

  assert.deepEqual(errors, [], "no page errors");
  console.log("action-feedback-unit: 9 checks passed");
} finally {
  await browser.close();
}
