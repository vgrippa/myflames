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
    const denied = privilegeResult({ state: "denied", missing: [["CONNECTION_ADMIN"], ["SUPER"]], reason: "Needs CONNECTION_ADMIN or SUPER.", grey_out: true });
    return <div id="why-probe">
      <ActionButton id="why-denied-btn" {...privilegeButtonProps(denied, () => setRuns((n) => n + 1))}>Run gated</ActionButton>
      <ActionButton id="why-busy-btn" busy busyLabel="Working…" {...privilegeButtonProps(privilegeResult(), () => setRuns((n) => n + 1), "Working…")}>Busy gated</ActionButton>
      <output className="runs">{runs}</output>
    </div>;
  }
  function Demo() {
    return <WhyDisabledHost account="dba@%">
      <Act id="throws-done" done={() => { throw new Error("formatter bug"); }} />
      <Act id="throws-failed" fail failed={() => { throw new Error("formatter bug"); }} />
      <Act id="plain" done={(n) => n + " rows."} />
      <Note id="note-other" n={note({})} currentKey="B" />
      <Note id="note-same" n={note({})} currentKey="A" />
      <Note id="note-late" n={note({ late: true })} />
      <Note id="note-unknown" n={note({})} />
      <WhyProbe />
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

  // 5. Privilege-disabled ActionButton opens WhyDisabled with reason and GRANT
  // without invoking onClick; Escape returns focus; busy ActionButton opens nothing.
  await page.click("#why-denied-btn");
  await page.waitForSelector(".why-disabled:popover-open", { visible: true });
  const whyText = await page.$eval(".why-disabled:popover-open", (e) => ({
    text: e.textContent || "",
    grant: e.querySelector(".ops-pre code")?.textContent || "",
  }));
  assert.match(whyText.text, /CONNECTION_ADMIN/);
  assert.equal(whyText.grant, "GRANT CONNECTION_ADMIN ON *.* TO `dba`@`%`;");
  assert.equal(await page.$eval("#why-probe .runs", (e) => e.textContent), "0");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".why-disabled:popover-open"));
  assert.equal(await page.evaluate(() => document.activeElement?.id), "why-denied-btn", "Escape returns focus to the disabled ActionButton");
  await page.click("#why-busy-btn");
  assert.equal(await page.$(".why-disabled:popover-open"), null, "a busy ActionButton opens no disabled message");
  assert.equal(await page.$eval("#why-probe .runs", (e) => e.textContent), "0");

  assert.deepEqual(errors, [], "no page errors");
  console.log("action-feedback-unit: 5 checks passed");
} finally {
  await browser.close();
}
