// Instruments view of the Server place: mocked /api/live/instruments with
// instruments-1.0 reports replayed from live MySQL 8.4 and MariaDB 13.0
// captures (fixtures/), and the links from Top statements, Waits and Locks.
// Toolbar presets and a checklist of what each view needs come first;
// instrument groups and consumers wait behind Advanced; gain, loss, cost and
// the statements are in the inspector.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open, pause, SERVER_GROUPS } from "./perf-harness.mjs";

const mysql = await load("instruments-mysql-8.4.json");
const mariadb = await load("instruments-mariadb-13.0.json");
const off = await load("instruments-mariadb-13.0-default.json");
const changed = await load("instruments-mysql-8.4-changed.json");
const waitsOff = await load("waits-mariadb-13.0-default.json");
const locksOff = await load("locks-mariadb-13.0-ps-off.json");
// Synthetic advisory entry supplements the older live capture without changing it.
const receiver = {
  key: "replication_receiver", label: "Replication: receiver reconnect guard", view: "replication",
  advisory: true, status: "off",
  explain: "Optional receiver reconnect monitoring for Skip until stopped.",
  missing: ["stage/sql/Checking source version is off"], optional_missing: [], rows: ["group:stage"],
};
mysql.features.push(receiver);
const preset = (report, key) => report.presets.find((p) => p.key === key);

const extra = {
  "/api/live/top": {
    status: 400,
    body: {
      error:
        "Statement digests are not being collected (consumer disabled: statements_digest). Enable them with: " +
        "UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE NAME IN " +
        "('global_instrumentation','statements_digest'); then run the workload again.",
    },
  },
  "/api/live/waits": { status: 200, body: waitsOff },
  "/api/live/locks": { status: 200, body: locksOff },
};
const h = await open("/api/live/instruments", "instruments", extra);
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=instruments]";
const inspectorTitle = (title) => page.waitForFunction((t) => document.querySelector(".view-inspector h3")?.textContent === t, {}, title);
/** Wait until a link revealed the row: selected, in the inspector, and focused. */
const revealed = (row) =>
  page.waitForFunction(
    (r) => {
      const element = document.querySelector(`.server-view [data-row="${r}"]`);
      if (!element) return false;
      const focus = element.matches("[data-row-key]") ? element : element.querySelector("[data-row-key]");
      return !!focus && focus.classList.contains("is-selected") && document.activeElement === focus;
    },
    {},
    row,
  );
try {
  await page.evaluate(() => {
    window.__copied = [];
    navigator.clipboard.writeText = (t) => (window.__copied.push(t), Promise.resolve());
  });
  await h.server();
  assert.deepEqual(await page.$$eval(".source-list .source-group", groups => [...groups.find(g => g.querySelector("h3").textContent === "Setup").querySelectorAll(".source-label")].map(e => e.textContent)), SERVER_GROUPS.Setup,
    "Instruments is in Setup, after Configuration");
  await h.view("instruments");
  assert.match(await text(`${view} .view-empty`), /what each instrument costs, and ready-made presets/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");

  // Load: Performance Schema as one row, presets in the toolbar, the checklist; Advanced collapsed.
  state.delay = 300;
  await button("Load instruments", view + " ");
  await page.waitForSelector(`${view} .capture-status`);
  await page.waitForSelector("#instr-ps");
  await h.idle(view);
  state.delay = 0;
  assert.equal(state.seen.length, 1);
  assert.match(await text("#instr-ps"), /On\s*Performance Schema\s*MySQL 8\.4\.11 · its own memory \d+\.\d MiB/);
  assert.match(await text(`${view} .summary-line`), /Performance Schema is on · 8 views ready/);
  assert.match(await h.popover("About presets"), /myflames only shows these statements; it never runs them/);
  assert.deepEqual(await page.$$eval(`${view} .grouped-head h3`, (els) => els.filter((e) => e.checkVisibility()).map((e) => e.textContent)),
    ["Server", "What myflames can show"]);
  assert.equal(await page.$eval(`${view} .instr-advanced`, (e) => e.open), false, "Advanced starts collapsed");
  const statuses = await page.$$eval(`${view} .instr-features .status-pill`, (els) => els.map((e) => e.textContent.trim()));
  assert.deepEqual([...new Set(statuses)], ["Ready", "Optional"]);
  assert.doesNotMatch(await text(`${view} .summary-line`), /partly|off/, "optional coverage does not count as an off view");
  // No paragraph per row: the checklist rows are one line.
  const heights = await page.$$eval(`${view} .instr-features .grouped-row`, (els) => els.map((e) => e.getBoundingClientRect().height));
  assert.ok(heights.every((x) => x <= 40), `one-line rows (${heights.join(", ")})`);
  // Optional coverage keeps its missing requirements and enable guidance on request.
  const receiverRow = `${view} [data-feature="replication_receiver"]`;
  assert.equal(await text(`${receiverRow} .status-pill`), "Optional");
  await h.select(receiverRow);
  await inspectorTitle(receiver.label);
  assert.match(await h.inspector(), /Optional.*Missing: stage\/sql\/Checking source version is off/s);
  assert.equal(await text(".view-inspector .status-pill.is-info"), "Optional");
  await page.locator(".view-inspector button ::-p-text(Show stage)").click();
  await revealed("group:stage");
  assert.match(await h.inspector(), /UPDATE performance_schema\.setup_instruments SET ENABLED = 'YES'/);
  await page.click(`${view} .instr-advanced summary`);
  // The Performance Schema row opens its state, memory, and how to turn it off.
  await h.select("#instr-ps");
  await inspectorTitle("Performance Schema");
  assert.match(await h.inspector(), /read-only at runtime/);
  assert.match(await h.inspector(), /Its own memory\s*\d+\.\d MiB/);
  assert.match(await h.inspector(), /Turn it off/);

  // Presets use the toolbar, retain every engine-provided choice, and only
  // preview SQL. Selecting a preset never reads or writes the server.
  const readsBeforePreset = state.seen.length;
  assert.deepEqual(await page.$$eval(`${view} .view-toolbar select[aria-label="Preset"] option`, (els) =>
    els.map((e) => [e.value, e.textContent])), mysql.presets.map((p) => [p.key, `${p.label}${p.applied === true ? " (current)" : ""}`]));
  assert.match(await text(`${view} .instr-preset-status`), /Already in place/);
  await page.select(`${view} select[aria-label="Preset"]`, "waits");
  assert.match(await text(`${view} .instr-preset-status[role=status]`), /Would change this server/);
  assert.match(await text(`${view} .instr-preset-status .instr-cost`), /High cost/);
  await page.click(`${view} .view-toolbar .instr-show-statements`);
  assert.equal(state.seen.length, readsBeforePreset, "preset selection and Show statements are local");
  await inspectorTitle("Wait analysis");
  const sql = await page.$$eval(".view-inspector .perf-sql pre", (els) => els.map((e) => e.textContent));
  assert.deepEqual(sql, [preset(mysql, "waits").runtime, preset(mysql, "waits").undo, preset(mysql, "waits").config]);
  await page.locator('.view-inspector [aria-label="Copy: Wait analysis: runtime statements"]').click();
  await page.waitForFunction(() => window.__copied.length === 1);
  assert.equal(await page.evaluate(() => window.__copied[0]), preset(mysql, "waits").runtime);
  assert.match(await text(".view-inspector .perf-sql [role=status]"), /Run it yourself/);
  assert.match(await h.inspector(), /my\.cnf/);
  assert.match(await h.inspector(), /it never runs them/);

  // Advanced: instrument groups as a table, gain/loss/cost and statements in the inspector.
  await page.click(`${view} .instr-advanced summary`);
  const groups = await page.$$eval(`${view} [aria-label="Instrument groups"] tbody tr`, (els) => els.map((e) => e.dataset.row));
  assert.equal(groups.length, 12);
  assert.equal(groups[0], "group:statement");
  const synch = `${view} tr[data-row="group:wait/synch"]`;
  assert.match(await text(synch), /Internal locks \(mutex, rwlock, cond\)\s*wait\/synch\/%/);
  assert.match(await text(synch), /Off \(0 of \d+\)/);
  assert.match(await text(synch), /High cost/);
  await page.focus(synch);
  await page.keyboard.press("Enter");
  await inspectorTitle("Internal locks (mutex, rwlock, cond)");
  const body = await h.inspector();
  assert.match(body, /You gain, when it is on/);
  assert.match(body, /You lose, when it is off/);
  assert.match(body, /Cost \(rule of thumb\)/);
  assert.match(body, /Waits \(optional\)/);
  assert.deepEqual(await page.$$eval(".view-inspector .perf-sql pre", (els) => els.map((e) => e.textContent)), [
    "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', TIMED = 'YES' WHERE NAME LIKE 'wait/synch/%';",
    "performance-schema-instrument = 'wait/synch/%=ON'",
    "UPDATE performance_schema.setup_instruments SET ENABLED = 'NO', TIMED = 'NO' WHERE NAME LIKE 'wait/synch/%';",
    "performance-schema-instrument = 'wait/synch/%=OFF'",
  ]);
  // "Used by" opens the view it names.
  await page.click(".view-inspector button.instr-feature");
  await page.waitForSelector("#server-nav-waits[aria-current=page]");
  await h.view("instruments");
  assert.ok(await page.$("#instr-ps"), "the report stays while the Server place is open");

  // Consumers show what they depend on.
  await page.click(`${view} .instr-advanced summary`);
  const history = `${view} tr[data-row="consumer:events_statements_history"]`;
  assert.match(await text(history), /events_statements_history\s*On\s*On/);
  await h.select(history);
  await inspectorTitle("events_statements_history");
  assert.match(await h.inspector(),
    /Records only while these are on: events_statements_current, thread_instrumentation, global_instrumentation\./);
  assert.match(await h.inspector(), /Capture statistics/);
  assert.match(await text(`${view} [data-row="actors"]`), /Default\s*setup_actors/);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "instruments.png") });

  // A changed server: changes from the default, a blocked consumer, a restore.
  state.queue.push({ status: 200, body: changed });
  await h.refresh();
  await page.waitForFunction(() => /Changed/.test(document.querySelector('.server-view [data-row="actors"]')?.textContent || ""));
  await h.idle(view);
  assert.match(await text(synch), /Partly on/);
  assert.match(await text(synch), /\d+ changed from default/);
  assert.match(await text(history), /On, but blocked by events_statements_current/);
  assert.match(await text(`${view} [data-feature="capture"]`), /Off/);
  assert.match(await text(`${view} [data-feature="capture"]`), /needs consumer events_statements_current is off and 1 more/);
  await h.select(`${view} [data-feature="capture"]`);
  await inspectorTitle("Capture statistics");
  assert.match(await h.inspector(), /Missing: .*blocked by events_statements_current/);
  await h.select(`${view} [data-row="actors"]`);
  await inspectorTitle("setup_actors");
  assert.match(await h.inspector(), /TRUNCATE TABLE performance_schema\.setup_actors;/);
  await page.select(`${view} select[aria-label="Preset"]`, "defaults");
  assert.match(await text(`${view} .instr-preset-status`), /Would change this server/);
  assert.match(await text(`${view} .view-toolbar`), /Custom/, "no preset matches: Custom");
  // "Show" in a view's checklist row reveals the instrument row it is missing.
  await h.select(`${view} [data-feature="locks_mdl"]`);
  await page.locator(".view-inspector button ::-p-text(Show wait/lock/metadata)").click();
  await revealed("group:wait/lock/metadata");

  // MariaDB with Performance Schema on: the metadata lock instrument is off by default.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 13\.0\.2/.test(document.querySelector("#instr-ps")?.textContent || ""));
  await h.idle(view);
  assert.match(await text(`${view} [data-feature="locks_mdl"]`), /Off\s*Locks: metadata locks\s*needs wait\/lock\/metadata\/sql\/mdl is off/);
  await h.select(`${view} [data-feature="locks_mdl"]`);
  assert.match(await h.inspector(), /Missing: wait\/lock\/metadata\/sql\/mdl is off/);
  assert.match(await text(`${view} [data-feature="locks_rows"]`), /Not needed/);
  assert.equal(await page.$(`${view} [data-row="group:error"]`), null, "MariaDB has no error instrument");
  const mdlRow = `${view} tr[data-row="group:wait/lock/metadata"]`;
  assert.match(await text(mdlRow), /Off\s*off/);
  await page.select(`${view} select[aria-label="Preset"]`, "minimal");
  await page.click(`${view} .view-toolbar .instr-show-statements`);
  await inspectorTitle("Minimal for myflames");
  assert.match(await h.inspector(), /performance-schema-instrument = 'wait\/lock\/metadata\/sql\/mdl=ON'/);

  // Performance Schema off: the row says how to turn it on; nothing else is known.
  state.queue.push({ status: 200, body: off });
  await h.refresh();
  await page.waitForSelector("#instr-ps .status-pill.is-off");
  await h.idle(view);
  assert.match(await text("#instr-ps"), /Off\s*Performance Schema/);
  await h.select("#instr-ps");
  await page.waitForFunction(() => /MariaDB's default/.test(document.querySelector(".view-inspector")?.textContent || ""));
  assert.equal(await text(".view-inspector .perf-sql pre"), "[mysqld]\nperformance_schema = ON");
  assert.doesNotMatch(await h.inspector(), /PERSIST_ONLY/, "SET PERSIST_ONLY is MySQL only");
  assert.match(await text(synch), /Unknown\s*off/);
  assert.match(await text(`${view} [data-feature="top"]`), /Unavailable/);
  assert.equal(await page.$eval("#server-nav-instruments .status-dot", (e) => e.className), "status-dot is-off");

  // Error keeps the report.
  state.queue.push({ status: 400, body: { error: "Could not read the Performance Schema setup: ERROR 2003 (HY000): Can't connect" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.ok(await page.$("#instr-ps"));

  await h.narrow("instruments-390.png");

  // Links from other views open the Instruments row they name, with focus on it.
  state.fallback = { status: 200, body: mariadb };
  await h.view("top");
  await button("Load top statements", ".server-view ");
  await page.waitForSelector(".server-view[data-view=top] .gate .instr-link");
  assert.match(await text(".server-view[data-view=top] .gate"), /the statements_digest consumer is off/);
  await page.click(".server-view[data-view=top] .gate .instr-link");
  await page.waitForSelector("#server-nav-instruments[aria-current=page]");
  await revealed("consumer:statements_digest");

  await h.view("waits");
  await button("Load wait events", ".server-view ");
  await page.waitForSelector(".server-view[data-view=waits] .gate .instr-link");
  await page.click(".server-view[data-view=waits] .gate .instr-link");
  await page.waitForSelector("#server-nav-instruments[aria-current=page]");
  await revealed("ps");

  await h.view("locks");
  await button("Load locks", ".server-view ");
  await page.waitForSelector(".server-view[data-view=locks] .summary-line");
  await page.click(".server-view[data-view=locks] .segmented button[data-value=mdl]");
  await page.waitForSelector('.server-view [data-instruments-row="ps"]');
  await page.click('.server-view [data-instruments-row="ps"]');
  await page.waitForSelector("#server-nav-instruments[aria-current=page]");
  await revealed("ps");
  await pause(50);

  const saved = await h.savedSlow((s) => s.kind === "instruments");
  assert.equal(JSON.stringify(saved.state).includes("instruments-1.0"), false, "reports are not saved");
  // Advisory entries never add to ready or partial counts either.
  state.queue.push({ status: 200, body: { ...mysql, features: [...mysql.features,
    { ...receiver, key: "advisory_ready", status: "ready", missing: [], rows: [] },
    { ...receiver, key: "advisory_partial", status: "partial" },
  ] } });
  await h.refresh();
  await page.waitForSelector(`${view} [data-feature="advisory_partial"]`);
  await h.idle(view);
  for (const key of ["replication_receiver", "advisory_ready", "advisory_partial"])
    assert.equal(await text(`${view} [data-feature="${key}"] .status-pill`), "Optional");
  assert.match(await text(`${view} .summary-line`), /8 views ready/);
  assert.doesNotMatch(await text(`${view} .summary-line`), /partly|off/);
  assert.deepEqual(h.errors, []);
  console.log("instruments view: ok");
} finally {
  await h.close();
}
