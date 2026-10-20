// Individual Performance Schema selection and reviewed writes. Fixtures retain
// live engine defaults; instruments-selection adds one synthetic disabled row.
// --unit runs the exported pure SQL builder without a browser or local server.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import ts from "typescript";
import { transformWithEsbuild } from "vite";
import { load, open, pause, SERVER_GROUPS } from "./perf-harness.mjs";

// Extract the exported function through the TS AST, so unit tests exercise its
// actual implementation without importing React, CSS or the workspace shell.
const source = await fs.readFile(new URL("../src/Instruments.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("Instruments.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const builder = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "buildInstrumentChange");
assert.ok(builder, "the pure builder is exported from Instruments.tsx");
const { code } = await transformWithEsbuild(builder.getText(ast), "instrument-change.ts", { loader: "ts", format: "esm", target: "es2020" });
const { buildInstrumentChange: change } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const mysql = await load("instruments-mysql-8.4.json");
const selected = await load("instruments-selection.json");
const receiver = {
  key: "replication_receiver", label: "Replication: receiver reconnect guard", view: "replication",
  advisory: true, status: "off", explain: "Optional receiver reconnect monitoring for Skip until stopped.",
  missing: ["stage/sql/Checking source version is off"], optional_missing: [], rows: ["group:stage"],
};
selected.features = [...selected.features.filter(feature => feature.key !== receiver.key), receiver];
const maria = await load("instruments-mariadb-13.0.json");
const off = await load("instruments-mariadb-13.0-default.json");
const timed = ["stage/sql/Waiting for table metadata lock", "statement/sql/select"];
const memory = "memory/sql/THD::main_mem_root";
const builtin = "memory/performance_schema/accounts";
const picked = [...timed, memory];
const expectedEnable = "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', TIMED = 'YES' WHERE NAME IN ('stage/sql/Waiting for table metadata lock', 'statement/sql/select');\n" +
  "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES' WHERE NAME IN ('memory/sql/THD::main_mem_root');";
assert.equal(change(selected, "instruments", picked, "enable").sql, expectedEnable);
const counted = change(selected, "instruments", picked, "counted");
assert.equal(counted.sql, expectedEnable.replace("TIMED = 'YES'", "TIMED = 'NO'"));
assert.ok(counted.config.includes("performance-schema-instrument='statement/sql/select=COUNTED'"));
assert.ok(counted.config.includes(`performance-schema-instrument='${memory}=ON'`));
const disabled = change(selected, "instruments", [...picked, builtin], "disable");
assert.deepEqual(disabled.skippedBuiltin, [builtin]);
assert.equal(disabled.sql, expectedEnable.replaceAll("'YES'", "'NO'"));
assert.ok(!disabled.config.includes(builtin));
assert.equal(change(selected, "instruments", [builtin], "disable").sql, "");
assert.equal(change(selected, "instruments", [], "enable").sql, "");
assert.equal(change(selected, "instruments", [memory, memory], "enable").sql.split(";").length, 2);
assert.throws(() => change(selected, "instruments", ["not in this report"], "enable"), /not in this report/);
const strangeName = "memory/sql/quote'and\\backslash";
const strange = { ...selected, instruments: [{ ...selected.instruments.find(row => row.name === memory), name: strangeName }] };
// A quote is doubled; a backslash is refused because its meaning depends on NO_BACKSLASH_ESCAPES.
const quoted = { ...selected, instruments: [{ ...selected.instruments.find(row => row.name === memory), name: "memory/sql/it's" }] };
assert.equal(change(quoted, "instruments", ["memory/sql/it's"], "enable").sql,
  "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES' WHERE NAME IN ('memory/sql/it''s');");
assert.throws(() => change(strange, "instruments", [strangeName], "enable"), /backslash/);
const many = { ...selected, instruments: Array.from({ length: 1001 }, (_, i) => ({ ...selected.instruments.find(row => row.name === timed[1]), name: `statement/sql/test${String(i).padStart(4, "0")}` })) };
const batches = change(many, "instruments", many.instruments.map(row => row.name), "enable").sql.split("\n");
assert.equal(batches.length, 3);
assert.deepEqual(batches.map(sql => (sql.match(/statement\/sql\/test/g) || []).length), [500, 500, 1]);
const parents = ["events_statements_current", "global_instrumentation", "thread_instrumentation"];
const consumerPlan = change(selected, "consumers", ["events_statements_history"], "enable");
assert.deepEqual(consumerPlan.alsoEnables, parents);
assert.equal(consumerPlan.sql, "UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' WHERE NAME IN ('events_statements_current', 'events_statements_history', 'global_instrumentation', 'thread_instrumentation');");
assert.equal(change(selected, "consumers", ["events_statements_history"], "disable").sql,
  "UPDATE performance_schema.setup_consumers SET ENABLED = 'NO' WHERE NAME IN ('events_statements_history');");
assert.throws(() => change(selected, "consumers", ["not a consumer"], "enable"), /not readable/);
assert.throws(() => change(selected, "consumers", ["events_statements_history"], "counted"), /no timing/);
const unknownParent = { ...selected, consumers: selected.consumers.filter(row => row.name !== "global_instrumentation") };
assert.throws(() => change(unknownParent, "consumers", ["events_statements_history"], "enable"), /not readable/);
for (const report of [mysql, maria, off]) {
  assert.ok(Array.isArray(report.instruments));
  assert.equal(change(report, "instruments", report.instruments.map(row => row.name), "enable").sql.includes("TIMED = 'YES'"), report.status.enabled);
}
console.log("instruments SQL builder: passed (quoting, allowlist, timing, defaults, built-ins, dependencies, batching)");
if (process.argv.includes("--unit")) process.exit(0);

const calls = [];
const response = body => ({ status: 200, body });
const extras = {
  "/api/live/top": { status: 400, body: { error: "Statement digests are not being collected (consumer disabled: statements_digest). Enable them with: UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE NAME IN ('global_instrumentation','statements_digest'); then run the workload again." } },
  "/api/live/waits": response(await load("waits-mariadb-13.0-default.json")),
  "/api/live/locks": response(await load("locks-mariadb-13.0-ps-off.json")),
  "/api/editor/preview": request => {
    calls.push(["preview", request]);
    return response({ status: "ready", statements: [], aggregate: { state: "unknown", reason: "Authority is unverified.", missing: [], warning: "" } });
  },
  "/api/editor/run": request => {
    calls.push(["run", request]);
    assert.equal(request.require_confirmation, true);
    if (request.decline) return response({ status: "declined" });
    if (!request.confirmed) return response({ status: "needs_confirmation", confirmation: {
      token: "review-once", text: "Confirm the exact SQL.", tone: "danger", statements: [request.sql],
      warnings: [], connection_type: "production", connection: { user: "operator", host: "test", port: 3306 },
    } });
    assert.equal(request.confirm_token, "review-once");
    return response({ status: "complete", run: { id: "instrument-run", state: "complete", error: null,
      results: [], counts: { statements: 2, run: 2, rows: 0, affected_rows: 3, warnings: 0, errors: 0 } } });
  },
  "/api/editor/close": request => { calls.push(["close", request]); return response({ closed: true }); },
};
const h = await open("/api/live/instruments", "instruments", extras);
const { page, state, text, button } = h;
state.fallback = response(selected);
const view = ".server-view[data-view=instruments]";
const tab = value => page.click(`${view} .instr-tabs button[data-value=${value}]`);
const search = async value => {
  await page.$eval(`${view} input[aria-label="Search instruments"]`, (input, next) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, next); input.dispatchEvent(new Event("input", { bubbles: true }));
  }, value);
  await pause(30);
};
const rows = () => page.$$eval(`${view} [aria-label="Instruments"] tbody tr[data-row]`, elements => elements.map(row => row.dataset.row.replace(/^instrument:/, "")));
const pick = async name => {
  await search(name);
  const row = await page.waitForSelector(`${view} [aria-label="Instruments"] tbody tr[data-row]`);
  await row.focus(); await page.keyboard.press("Space");
};
const action = (kind, label) => page.click(`${view} [data-instrument-actions=${kind}] [data-reviewed-sql="${label}"]`);
const review = () => page.waitForSelector("dialog.confirm-sheet[open]");
const back = async () => { if (await page.$(`${view} .detail-back`)) await page.click(`${view} .detail-back`); };
const decline = async () => {
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.confirm-sheet[open]"));
  await page.waitForFunction(() => /Cancelled/.test(document.querySelector("[data-reviewed-status]")?.textContent || ""));
};
const refresh = async report => {
  state.queue.push(response(report));
  await h.refresh(); await h.idle(view);
};
try {
  await page.evaluate(() => {
    window.__copied = [];
    navigator.clipboard.writeText = value => (window.__copied.push(value), Promise.resolve());
  });
  await h.server();
  assert.deepEqual(await page.$$eval(".source-list .source-group", groups => [...groups.find(g => g.querySelector("h3").textContent === "Setup").querySelectorAll(".source-label")].map(e => e.textContent)), SERVER_GROUPS.Setup);
  await h.view("instruments");
  await page.waitForSelector("#instr-ps"); await h.idle(view);
  assert.equal(state.seen.length, 1, "opening loads once without a Load button press");
  assert.deepEqual(await page.$$eval(`${view} .instr-tabs button`, els => els.map(e => e.textContent)), ["Features", "Instruments", "Consumers", "Actors and objects"]);
  assert.match(await text(`${view} .summary-line`), /Performance Schema is on/);
  assert.ok(await page.$(`${view} .instr-features`));
  assert.equal(await text(`${view} [data-feature=replication_receiver] .status-pill`), "Optional");
  assert.match(await text(`${view} .summary-line`), /8 views ready/);
  assert.doesNotMatch(await text(`${view} .summary-line`), /partly|off/);
  assert.match(await h.popover("About presets"), /reviewed Apply action/);
  // Presets and feature links retain their inspector details.
  const beforePreset = state.seen.length;
  await page.select(`${view} select[aria-label=Preset]`, "waits");
  await page.click(`${view} .instr-show-statements`);
  await page.waitForSelector(".view-inspector .perf-sql pre");
  assert.equal(await text(".view-inspector .perf-sql pre"), selected.presets.find(p => p.key === "waits").runtime);
  assert.equal(state.seen.length, beforePreset);
  await back();
  await h.select(`${view} [data-feature=replication_receiver]`);
  assert.match(await h.inspector(), /Missing: stage\/sql\/Checking source version is off/);
  await page.locator('.view-inspector button ::-p-text(Show stage)').click();
  await page.waitForFunction(title => document.querySelector('.view-inspector h3')?.textContent === title, {}, selected.groups.find(group => group.key === "stage").label);
  assert.match(await h.inspector(), /You gain, when it is on/);
  assert.match(await h.inspector(), /You lose, when it is off/);
  assert.match(await h.inspector(), /UPDATE performance_schema\.setup_instruments/);
  await back();
  await tab("setup");
  await h.select(`${view} [data-row=actors]`);
  assert.match(await h.inspector(), /setup_actors/);
  await back();

  await tab("instruments");
  await page.select(`${view} select[aria-label="Instrument group"]`, "");
  await search("metadata lock"); assert.deepEqual(await rows(), [timed[0]]);
  await search("");
  await page.select(`${view} select[aria-label="Instrument group"]`, "memory");
  assert.deepEqual(await rows(), [builtin, memory]);
  await page.select(`${view} select[aria-label="Instrument group"]`, "");
  await page.select(`${view} select[aria-label="Instrument state"]`, "different");
  assert.deepEqual(await rows(), [timed[1]]);
  await page.select(`${view} select[aria-label="Instrument state"]`, "disabled");
  assert.ok((await rows()).includes(timed[1]));
  await page.select(`${view} select[aria-label="Instrument state"]`, "enabled");
  assert.ok(!(await rows()).includes(timed[1]));
  await page.select(`${view} select[aria-label="Instrument state"]`, "all");
  await search("memory/"); await button("Select all shown", `${view} `);
  assert.match(await text(`${view} .instr-batch`), /2 selected/);
  await button("Clear selection", `${view} `);
  // Visible checkboxes: each adds its row, the header picks every row shown.
  await page.click(`${view} input[aria-label="Select ${memory}"]`);
  await page.click(`${view} input[aria-label="Select ${builtin}"]`);
  assert.match(await text(`${view} .instr-batch`), /2 selected/);
  assert.equal(await page.$eval(`${view} [aria-label="Instruments"] input[aria-label="Select every row shown"]`, e => e.checked), true);
  await page.click(`${view} [aria-label="Instruments"] input[aria-label="Select every row shown"]`);
  assert.match(await text(`${view} .instr-batch`), /0 selected/);
  await page.click(`${view} [aria-label="Instruments"] input[aria-label="Select every row shown"]`);
  assert.match(await text(`${view} .instr-batch`), /2 selected/);
  await button("Clear selection", `${view} `);
  for (const name of picked) await pick(name);
  await search("");
  assert.match(await text(`${view} .instr-batch`), /3 selected/);
  assert.equal(await page.$eval(`${view} tr[data-row="instrument:${memory}"] td[data-col=timed]`, e => e.textContent), "");
  await action("instruments", "Enable"); await review();
  assert.equal(await text("dialog.confirm-sheet .sql-preview"), expectedEnable);
  assert.match(await text("dialog.confirm-sheet"), /Runtime changes are lost at restart/);
  assert.equal(calls.filter(([kind, request]) => kind === "run" && request.confirmed).length, 0);
  await decline();
  assert.equal(calls.filter(([kind, request]) => kind === "run" && request.decline).length, 1);
  await pick(builtin); await search("");
  await action("instruments", "Disable"); await review();
  assert.equal(await text("dialog.confirm-sheet .sql-preview"), disabled.sql);
  assert.match(await text("dialog.confirm-sheet"), /Skips 1 built-in/);
  await decline();
  await action("instruments", "Enable without timing"); await review();
  assert.equal(await text("dialog.confirm-sheet .sql-preview"), change(selected, "instruments", [...picked, builtin], "counted").sql);
  await decline();
  await page.click(`${view} .instr-batch summary`);
  await page.click(`${view} [aria-label="Copy: Enable: option file lines"]`);
  assert.equal(await page.evaluate(() => window.__copied.at(-1)), change(selected, "instruments", [...picked, builtin], "enable").config);
  // Only confirming sends the write; completion refreshes and clears selection.
  const beforeApply = state.seen.length;
  await action("instruments", "Enable"); await review();
  await page.click("dialog.confirm-sheet button.danger-solid");
  await page.waitForFunction(() => /completed/.test(document.querySelector("[data-reviewed-status]")?.textContent || ""));
  await page.waitForFunction(() => /0 selected/.test(document.querySelector(".instr-batch")?.textContent || ""));
  await h.idle(view);
  assert.equal(state.seen.length, beforeApply + 1);
  assert.match(await text(`${view} .instr-batch`), /0 selected/);
  assert.ok(calls.some(([kind]) => kind === "close"));

  await tab("consumers");
  const history = `${view} tr[data-row="consumer:events_statements_history"]`;
  await page.click(history);
  assert.match(await text(`${view} .instr-batch`), /1 selected/);
  await action("consumers", "Enable"); await review();
  assert.equal(await text("dialog.confirm-sheet .sql-preview"), consumerPlan.sql);
  assert.match(await text("dialog.confirm-sheet"), /Also enables events_statements_current, global_instrumentation, thread_instrumentation/);
  await decline();
  await page.focus(history); await page.keyboard.press("Enter");
  assert.match(await h.inspector(), /Records only while these are on/);
  await back();
  // Select all shown is bounded even when the table windows a larger report.
  const large = { ...selected, instruments: Array.from({ length: 2001 }, (_, i) => ({
    ...selected.instruments.find(row => row.name === timed[1]), name: `statement/sql/test${String(i).padStart(4, "0")}`,
  })) };
  await refresh(large); await tab("instruments");
  await button("Select all shown", `${view} `);
  assert.match(await text(`${view} .instr-batch`), /2,000 selected/);
  assert.match(await text(`${view} .instr-list`), /limited to the first 2,000/);
  await button("Clear selection", `${view} `);
  // Report-bound selection is dropped on refresh, including an engine switch.
  await refresh(maria);
  await tab("instruments");
  await search("wait/lock/metadata/sql/mdl");
  assert.deepEqual(await rows(), ["wait/lock/metadata/sql/mdl"]);
  assert.equal(await page.$eval(`${view} tr[data-row] td[data-col=enabled]`, e => e.textContent), "No");
  await search("");
  await tab("features");
  await h.select(`${view} [data-feature=locks_mdl]`);
  await page.locator('.view-inspector button ::-p-text(Show wait/lock/metadata)').click();
  await page.waitForFunction(title => document.querySelector('.view-inspector h3')?.textContent === title, {}, maria.groups.find(group => group.key === "wait/lock/metadata").label);
  await back();
  assert.equal(await page.$eval(`${view} select[aria-label="Instrument group"]`, select => select.value), "wait/lock/metadata");
  await page.select(`${view} select[aria-label="Instrument group"]`, "");

  for (const theme of ["light", "dark"]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
    for (const section of ["features", "instruments", "consumers", "setup"]) {
      await tab(section);
      await page.screenshot({ path: path.join(h.shots, `instruments-${section}-${theme}-1440.png`) });
      await h.narrow(`instruments-${section}-${theme}-390.png`);
    }
  }
  // A failed refresh keeps data and never starts an automatic retry loop.
  state.queue.push({ status: 400, body: { error: "Could not read setup tables" } });
  await h.refresh(); await page.waitForSelector(`${view} [role=alert]`); await h.idle(view);
  const failedCount = state.seen.length;
  await tab("features"); await tab("consumers"); await pause(150);
  assert.equal(state.seen.length, failedCount);
  assert.ok(await page.$("#instr-ps"));
  await refresh(off);
  for (const section of ["instruments", "consumers"]) {
    await tab(section);
    assert.match(await text(`${view} .gate`), /Performance Schema is off/);
    assert.equal(await page.$(`${view} table`), null);
  }
  // Links still open the named inspector and focus Back, including repeats.
  for (const [section, loadLabel, linkRow] of [["top", "Load top statements", "consumer:statements_digest"], ["waits", null, "ps"]]) {
    // Top statements waits for Load; Waits reads once when it opens on screen.
    await h.view(section); if (loadLabel) await button(loadLabel, `.server-view[data-view=${section}] `);
    await page.waitForSelector(`.server-view[data-view=${section}] .instr-link`);
    await page.click(`.server-view[data-view=${section}] .instr-link`);
    await page.waitForSelector("#server-nav-instruments[aria-current=page]");
    await page.waitForFunction(row => document.querySelector('.view-inspector')?.dataset.key === row ||
      document.querySelector('.view-inspector h3')?.textContent === (row === "ps" ? "Performance Schema" : "statements_digest"), {}, linkRow);
    await page.waitForFunction(() => document.activeElement?.classList.contains("detail-back"));
  }
  assert.deepEqual(h.errors, []);
  console.log("instruments browser: auto load, tabs, filters, selection, review, dependency enable, completion, gates, links, light/dark and 390 px passed");
} finally { await h.close(); }
