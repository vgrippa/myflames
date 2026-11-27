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
  import { ActionButton, ActionStatus, useAction } from "./kit";
  import { Commands } from "./Commands";
  import { KillResultNote, type KillNote } from "./Locks";
  import { privilegeButtonProps, privilegeResult } from "./privileges";
  import { WhyDisabledHost } from "./WhyDisabled";
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
  function Demo() {
    return <WhyDisabledHost>
      <Act id="throws-done" done={() => { throw new Error("formatter bug"); }} />
      <Act id="throws-failed" fail failed={() => { throw new Error("formatter bug"); }} />
      <Act id="plain" done={(n) => n + " rows."} />
      <Note id="note-other" n={note({})} currentKey="B" />
      <Note id="note-same" n={note({})} currentKey="A" />
      <Note id="note-late" n={note({ late: true })} />
      <Note id="note-unknown" n={note({})} />
      <WhyProbe />
      <CommandsProbe />
    </WhyDisabledHost>;
  }
  createRoot(document.getElementById("root")!).render(<Demo />);
`,
  },
  bundle: true, platform: "browser", format: "iife", write: false, jsx: "automatic", loader: { ".css": "empty" },
  define: { "process.env.NODE_ENV": '"production"' },
});
const css = (await Promise.all(["style.css", "kit.css", "catalog-actions.css"].map((file) => readFile(path.join(here, "../src", file), "utf8")))).join("\n");
const browser = await puppeteer.launch({ headless: "shell", pipe: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setRequestInterception(true);
  page.on("request", (request) => request.respond({ status: 200, contentType: "text/html", body: `<style>${css}</style><div id="root"></div>` }));
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

  assert.deepEqual(errors, [], "no page errors");
  console.log("action-feedback-unit: 6 checks passed");
} finally {
  await browser.close();
}
