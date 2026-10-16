import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
// Configuration view of the Server place: mocked /api/live/config with
// config-1.0 reports replayed from live captures (fixtures/). Grouped one-line
// rows; the why, the rule, the statements and the caveat open in the inspector.
// Change… in the inspector opens the Change sheet (variables.py, mocked with
// variable-*.json computed from live recordings): the modes each engine and
// variable allow, the exact statement checked by the server, Cancel and Escape
// recorded as declined, the confirmed change with before and after, and Change
// turned off on a report from another connection.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege, privilegeAnswer, load, open, pause } from "./perf-harness.mjs";

// Execute the production SQL handoff without a browser or server. The
// shell owns callback wiring; no execution API participates in this path.
if (process.argv.includes("--unit")) {
  const { build } = await import("vite");
  const fs = await import("node:fs/promises");
  const { fileURLToPath, pathToFileURL } = await import("node:url");
  const workspace = fileURLToPath(new URL("../", import.meta.url));
  const entry = "\0config-unit";
  const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
    plugins: [{ name: "config-unit", resolveId(id) { if (id === entry) return entry; }, load(id) {
      if (id === entry) return `export { openSessionSql } from ${JSON.stringify(path.join(workspace, "src/Config.tsx"))};`;
    } }], build: { ssr: true, write: false, rollupOptions: { input: entry, output: { inlineDynamicImports: true } } } });
  const temporary = await fs.mkdtemp(path.join(workspace, ".config-unit-"));
  try {
    const module = path.join(temporary, "unit.mjs");
    await fs.writeFile(module, compiled.output.find(f => f.type === "chunk").code);
    const { openSessionSql } = await import(pathToFileURL(module));
    globalThis.fetch = () => { throw new Error("A handoff must never call an API"); };
    const opened = [], callback = sql => opened.push(sql);
    for (const statement of ["SET SESSION character_set_results = NULL", "SET SESSION character_set_results = 'NULL'", "SET SESSION sql_mode = 'STRICT_TRANS_TABLES';"]) {
      assert.equal(openSessionSql({ mode: "session", status: "ready", statement }, callback), true);
      assert.equal(opened.at(-1), statement.endsWith(";") ? statement : statement + ";");
    }
    const base = { mode: "session", status: "ready", statement: "SET SESSION sort_buffer_size = 1048576" };
    assert.equal(openSessionSql(base), false);
    for (const patch of [{ status: "refused" }, { mode: "global" }, { statement: null }])
      assert.equal(openSessionSql({ ...base, ...patch }, callback), false);
    assert.equal(opened.length, 3);
    console.log("config unit: exact Session SQL, NULL vs text, terminators, missing callback and refused-plan guards passed");
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  process.exit(0);
}

const mysql = await load("config-mysql-8.4.json");
const mariadb = await load("config-mariadb-13.0-default.json");
const mariadb114 = await load("config-mariadb-11.4.json");
const variables = {
  "mysql:max_connections": await load("variable-mysql-8.4-global.json"),
  "mysql:innodb_log_file_size": await load("variable-mysql-8.4-persist-only.json"),
  "mariadb:max_connections": await load("variable-mariadb-11.4-global.json"),
  "mariadb:performance_schema": await load("variable-mariadb-11.4-readonly.json"),
};
const VERB = { global: "SET GLOBAL", persist: "SET PERSIST", persist_only: "SET PERSIST_ONLY", session: "SET SESSION" };
let engine = "mysql";
const applied = [];
const planned = [];
let variableReply = null;
let planReply = null;
let applyReply = null;
const fx = (name) => variables[`${engine}:${name}`];
// The recordings predate these additive fields. Supply deterministic metadata
// so the UI regressions can distinguish the initial read from a later plan.
const readAt = "2026-10-15T14:30:00+00:00";
const persistedState = (v) => ({
  state: !v.persisted.supported ? "unsupported" : v.persisted.present === null ? "unreadable" : v.persisted.present ? "present" : "absent",
  present: v.persisted.present, value: v.persisted.value,
});
for (const f of Object.values(variables)) {
  for (const v of [f.variable, f.plan.variable]) Object.assign(v, { nullable: false, read_at: readAt });
  f.plan.before.persisted_state = persistedState(f.plan.variable);
}
// A synthetic nullable session-variable envelope exercises JSON null and the
// literal string "NULL" separately; it makes no assertion about server writes.
const nullable = structuredClone(variables["mysql:max_connections"]);
Object.assign(nullable.variable, {
  name: "character_set_results", type: "string", scope: "both", nullable: true,
  min: null, max: null, global_value: "utf8mb4", session_value: "latin1", default: "utf8mb4",
});
for (const [m, data] of Object.entries(nullable.variable.modes)) {
  Object.assign(data, { available: m === "session", recommended: m === "session", reason: null });
}
Object.assign(nullable.plan, {
  name: "character_set_results", mode: "session", requested: "latin1", value: "latin1", display: "latin1",
  statement: "SET SESSION character_set_results = 'latin1'", variable: structuredClone(nullable.variable),
  before: { global: "utf8mb4", session: "latin1", persisted: null, persisted_state: persistedState(nullable.variable) },
  undo: ["SET SESSION character_set_results = 'latin1';"], caveats: [],
  effect: { now: true, after_restart: false, needs_restart: false, text: "Changes this myflames session." },
  privileges: { need: "no privilege", note: "", checked: true, allowed: true, via: null },
});
nullable.plan.expect = { name: "character_set_results", mode: "session", statement: nullable.plan.statement, global_value: "utf8mb4", session_value: "latin1", persisted_value: null };
variables["mysql:character_set_results"] = nullable;
/** The plan the server would return for this request (the recorded one, with the value asked for). */
function planFor(body) {
  const plan = structuredClone(fx(body.name).plan);
  if (plan.requested === body.value && plan.mode === body.mode) return plan;
  plan.requested = body.value;
  plan.value = body.value;
  plan.display = body.value;
  plan.mode = body.mode;
  if (plan.status === "ready") {
    const literal = body.value === null ? "NULL" : plan.variable.type === "string" ? `'${body.value}'` : body.value;
    plan.statement = body.mode === "reset_persist" ? `RESET PERSIST ${body.name}` : `${VERB[body.mode]} ${body.name} = ${literal}`;
    plan.expect = { ...plan.expect, mode: body.mode, statement: plan.statement };
  }
  return plan;
}

const h = await open("/api/live/config", "config", {
  "/api/live/variable": (body) => variableReply ? variableReply(body) : ({ status: 200, body: fx(body.name).variable }),
  "/api/live/variable/plan": (body) => {
    planned.push(body);
    return planReply ? planReply(body) : { status: 200, body: planFor(body), delay: 50 };
  },
  "/api/live/variable/apply": (body, request) => {
    // The declined branch only records the intent; exercise the real audit
    // endpoint against the isolated store, never a database connection.
    if (body.declined) { void request.continue(); return null; }
    applied.push(body);
    return applyReply ? applyReply(body) : { status: 200, body: fx(body.name).change, delay: 100 };
  },
});
const { page, state, button, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = ".server-view[data-view=config]";
const row = (key) => `${view} li[data-check="${key}"]`;
const sheet = "dialog.variable-sheet[open]";
const confirmButton = `${sheet} .sheet-actions button:last-of-type`;
const audit = () =>
  page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch("/api/audit/list", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}",
    });
    return (await response.json()).records;
  });
const statement = () => page.$eval(`${sheet} .variable-statement`, (e) => e.textContent).catch(() => "");
async function setValue(name, value) {
  const input = `${sheet} #variable-value-${name}`;
  await page.click(input, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.type(input, value);
}
async function openChange(name) {
  await page.click(`.view-inspector button[data-change="${name}"]`);
  await page.waitForSelector(sheet);
  await page.waitForFunction((s) => document.querySelector(`${s} .variable-facts`) || document.querySelector(`${s} [role=alert]`), {}, sheet);
}
async function cancelAndAudit({ operation = "set_global", name = "max_connections", requested, statements = [], key = "Escape" }) {
  const before = (await audit()).length;
  if (key === "Escape") await page.keyboard.press("Escape");
  else await page.click(`${sheet} .sheet-actions button:first-child`);
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  let records = [];
  for (let i = 0; i < 50; i++) {
    records = await audit();
    if (records.length > before) break;
    await pause(50);
  }
  assert.equal(records.length, before + 1, "every cancelled sheet adds exactly one audit record");
  assert.equal(records[0].status, "declined");
  assert.equal(records[0].operation, operation);
  assert.equal(records[0].target.name, name);
  assert.equal(records[0].target.requested, requested);
  assert.deepEqual(records[0].statements, statements);
  if (!statements.length) assert.equal(records[0].target.statement, undefined, "no stale expect statement in an unreviewed intent");
  return records[0];
}
try {
  // A saved connection to switch to at the end (the page starts on the unsaved default).
  await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    await fetch("/api/profiles/save", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: JSON.stringify({ value: { name: "Staging", host: "db.staging", port: 3307, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } }),
    });
  });
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await h.view("config");
  assert.equal(await text(`${view} h2`), "Configuration", "the view is named Configuration");
  assert.match(await text(`${view} .view-empty`), /compares them with the engine's defaults/);
  assert.equal(state.seen.length, 0, "nothing is read until asked");
  assert.equal(await page.$(`${view} .auto-refresh`), null, "no automatic refresh");
  // The explanation is behind the view's ⓘ, not on screen.
  assert.match(await h.popover("About Configuration"), /rules of thumb, not verdicts.*never changes a setting.*asks before sending it/s);

  state.delay = 300;
  await button("Load configuration", view + " ");
  await page.waitForSelector(`${view} .capture-status`);
  await page.waitForSelector(`${view} .summary-line`);
  await h.idle(view);
  state.delay = 0;
  assert.equal(state.seen.length, 1);
  assert.match(await text(`${view} .summary-line`), /Watch\s*0 problems, 1 to watch, 9 ok/);
  assert.match(await text(`${view} .fact-strip`), /MySQL 8\.4\.11/);
  assert.deepEqual(await texts(`${view} .grouped-head h3`),
    ["Memory4", "Durability3", "Redo log1", "Connections and caches2", "Observability2", "Server facts3"]);
  // No explanatory paragraph on screen: rows are one line each.
  assert.equal(await page.$$eval(`${view} .view-main p`, (els) => els.filter((e) => !e.closest(".summary-line") && e.textContent.length > 80).length), 0);
  // A row: status, label, settings, value, default.
  const pool = row("buffer_pool");
  assert.match(await text(pool), /OK\s*Buffer pool size\s*innodb_buffer_pool_size.*128\.0 MiB\s*default/);
  assert.match(await text(row("sort_buffer_size")), /256\.0 KiB\s*default/);
  const slow = row("slow_log");
  assert.equal(await page.$eval(`${slow} .status-pill`, (e) => e.className), "status-pill is-watch");
  // The badge in the sub-navigation shows the worst status.
  assert.equal(await page.$eval("#server-nav-config .status-dot", (e) => e.className), "status-dot is-watch");
  assert.match(await text("#server-nav-config"), /Configuration, Watch/);

  // The inspector: why it is flagged with the rule, current values with default
  // and source, the statement to copy, and the caveat. Enter opens it.
  await page.focus(`${slow} button`);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Slow query log");
  const slowText = await h.inspector();
  assert.match(slowText, /Why it is flagged.*slow_query_log=OFF/s);
  assert.match(slowText, /Rule of thumb: /);
  assert.equal(await text(".view-inspector .perf-sql pre"), "SET GLOBAL slow_query_log = ON; SET GLOBAL long_query_time = 1;");
  assert.match(slowText, /Caveat: .*new connections only/);
  assert.match(slowText, /From /);
  // Arrow keys move along the rows; the inspector follows the selection.
  await page.keyboard.press("ArrowUp");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent !== "Slow query log");
  await page.keyboard.press("ArrowDown");
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Slow query log");
  await h.select(pool);
  await page.waitForFunction(() => document.querySelector(".view-inspector h3")?.textContent === "Buffer pool size");
  assert.match(await h.inspector(), /innodb_buffer_pool_size128\.0 MiB · default 128\.0 MiB · set by default/);
  assert.match(await h.inspector(), /Why this status/);
  await h.select(row("io_capacity"));
  await page.waitForFunction(() => /No threshold/.test(document.querySelector(".view-inspector .inspector-meta")?.textContent || ""));
  assert.match(await h.inspector(), /10000/);
  // Escape closes the inspector and returns focus to the row.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction(() => document.activeElement?.closest("li")?.dataset.check === "io_capacity");
  // The filter narrows the rows on screen (no server read).
  await page.type(`${view} input[aria-label="Filter settings"]`, "binlog");
  assert.deepEqual(await page.$$eval(`${view} li[data-check]`, (els) => els.map((e) => e.dataset.check)), ["sync_binlog"]);
  await page.click(`${view} input[aria-label="Filter settings"]`, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  assert.equal(state.seen.length, 1);
  // Notes are one "N notes" control.
  assert.match(await h.notes(), /Notes about this read/);
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "config.png") });

  // ---- Change: MySQL, a dynamic variable ----
  await h.select(row("max_connections"));
  assert.match(await text('.view-inspector button[data-change="max_connections"]'), /^Change…Unverified/);
  assert.equal(await page.$('.view-inspector button[data-reset="max_connections"]'), null, "nothing persisted, no reset");
  assert.equal(state.other.length, 0, "nothing is read before Change");
  await openChange("max_connections");
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel", "Cancel has focus");
  assert.equal(await text(`${sheet} .eyebrow`), "SET GLOBAL");
  assert.equal(await text(`${sheet} h2`), "Change max_connections?");
  // The modes this server and variable allow: global only, so no session; nothing persisted, so no reset.
  assert.deepEqual(await texts(`${sheet} .variable-modes label strong`), ["Now only", "Now and after restart", "After restart only"]);
  assert.equal(await page.$eval(`${sheet} #variable-value-max_connections`, (e) => e.value), "151");
  await page.waitForFunction(() => /SET GLOBAL max_connections = 151;/.test(document.querySelector("dialog.variable-sheet .variable-statement")?.textContent || ""));
  assert.match(await text(`${sheet} .variable-facts`), /Server.*Local MySQL.*MySQL 8\.4\.11.*Global before151.*Global sourcedefault.*mysqld-auto\.cnfno value.*Read at2026-10-15T14:30:00/s);
  await setValue("max_connections", "300");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true, "off until the server checked the new value");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
  const facts = await text(`${sheet} .variable-plan`);
  assert.match(facts, /EffectChanges the running value now, until the server restarts\./);
  assert.match(facts, /PrivilegesSYSTEM_VARIABLES_ADMIN or SUPER \(your grants include SYSTEM_VARIABLES_ADMIN\)/);
  assert.match(facts, /UndoSET GLOBAL max_connections = 151;/);
  assert.match(facts, /raises the worst-case memory/);
  assert.match(await text(`${sheet} .variable-policy`), /Production connection, so myflames asks before every write/);
  assert.equal(await h.label(confirmButton), "Set globally");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), false);
  assert.match(await page.$eval(confirmButton, (e) => e.className), /danger-solid/, "SET GLOBAL is confirmed in the danger tone");
  await page.screenshot({ path: path.join(h.shots, "config-change.png") });
  // At 390 px the sheet fits the screen (no sideways scroll inside or outside it).
  await page.setViewport({ width: 390, height: 844 });
  await pause(200);
  const fit = await page.$eval(sheet, (d) => ({ width: d.getBoundingClientRect().width, scroll: d.scrollWidth - d.clientWidth }));
  assert.ok(fit.width <= 390 && fit.scroll <= 0, `the sheet fits at 390 px: ${JSON.stringify(fit)}`);
  await page.screenshot({ path: path.join(h.shots, "config-change-390.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  await pause(150);
  // Switching mode plans again: the statement follows.
  await page.click(`${sheet} input[value=persist]`);
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST max_connections = 300;");
  assert.equal(await h.label(confirmButton), "Set and persist");
  assert.equal(await text(`${sheet} .eyebrow`), "SET PERSIST");
  await page.click(`${sheet} input[value=global]`);
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
  // Escape sends nothing and is recorded as declined, with the statement shown.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, 0);
  await page.waitForFunction(() => document.activeElement?.dataset?.change === "max_connections");
  let records = [];
  for (let i = 0; i < 50 && !records.length; i++) {
    records = await audit();
    if (!records.length) await pause(100);
  }
  assert.equal(records.length, 1);
  assert.equal(records[0].status, "declined");
  assert.equal(records[0].operation, "set_global");
  assert.deepEqual(records[0].statements, ["/*myflames:variable*/ SET GLOBAL max_connections = 300"]);

  // Every cancellation is audited, including an initial read that is still
  // pending or failed, a pending plan, and a refused or failed plan.
  variableReply = (body) => ({ status: 200, body: fx(body.name).variable, delay: 600 });
  await page.click('.view-inspector button[data-change="max_connections"]');
  await page.waitForSelector(sheet);
  await cancelAndAudit({ requested: "" });
  variableReply = () => ({ status: 400, body: { error: "Variable read failed" } });
  await openChange("max_connections");
  assert.match(await text(`${sheet} [role=alert]`), /Variable read failed/);
  await cancelAndAudit({ requested: "", key: "Cancel" });
  variableReply = null;

  planReply = (body) => ({ status: 200, body: planFor(body), delay: 600 });
  await openChange("max_connections");
  assert.equal(await statement(), "", "no statement has been reviewed before the plan arrives");
  await cancelAndAudit({ requested: "151" });
  planReply = null;

  await openChange("max_connections");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  planReply = (body) => ({ status: 200, body: planFor(body), delay: 600 });
  await page.click(`${sheet} input[value=persist]`);
  await setValue("max_connections", "401");
  assert.equal(await statement(), "", "a previous statement disappears as the intent changes");
  await cancelAndAudit({ operation: "set_persist", requested: "401", key: "Cancel" });
  planReply = (body) => body.value === "451"
    ? { status: 400, body: { error: "Plan read failed" } }
    : { status: 200, body: planFor(body) };
  await openChange("max_connections");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  await setValue("max_connections", "451");
  await page.waitForFunction(() => /Plan read failed/.test(document.querySelector("dialog.variable-sheet")?.textContent || ""));
  await cancelAndAudit({ requested: "451" });

  planReply = (body) => ({ status: 200, body: {
    ...planFor(body), status: "refused", statement: null, expect: null,
    problems: [{ code: "invalid_value", message: "Value rejected during planning" }],
  } });
  await openChange("max_connections");
  await page.waitForFunction(() => /Value rejected during planning/.test(document.querySelector("dialog.variable-sheet")?.textContent || ""));
  await cancelAndAudit({ requested: "151", key: "Cancel" });
  planReply = null;
  assert.equal(applied.length, 0, "all cancellation paths send no write");

  // The initial read is 151, but the reviewed plan sees 205 with another
  // source and read time. The facts must be the same snapshot as expect.
  planReply = (body) => {
    const p = planFor(body);
    p.before.global = "205";
    p.expect.global_value = "205";
    Object.assign(p.variable, { global_value: "205", set_by: "SET GLOBAL", source: "DYNAMIC", read_at: "2026-10-15T14:31:00+00:00" });
    return { status: 200, body: p };
  };
  await openChange("max_connections");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  const reviewedFacts = await text(`${sheet} .variable-facts`);
  assert.match(reviewedFacts, /Global before205.*Global sourceSET GLOBAL \(DYNAMIC\).*Read at2026-10-15T14:31:00/s);
  assert.doesNotMatch(reviewedFacts, /151|default|COMPILED/);
  await page.screenshot({ path: path.join(h.shots, "config-change-reviewed.png") });
  // A fresh plan may report a present SQL NULL. That is different from an
  // absent entry and from a persisted entry that could not be read.
  planReply = (body) => {
    const p = planFor(body);
    p.before.persisted_state = { state: "present", present: true, value: null };
    p.variable.persisted = { ...p.variable.persisted, present: true, value: null };
    p.variable.read_at = "2026-10-15T14:32:00+00:00";
    return { status: 200, body: p };
  };
  await setValue("max_connections", "152");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  assert.match(await text(`${sheet} .variable-facts`), /mysqld-auto\.cnfSQL NULL.*Read at2026-10-15T14:32:00/s);
  planReply = (body) => {
    const p = planFor(body);
    p.before.global = "205";
    p.expect.global_value = "205";
    p.variable.global_value = "205";
    return { status: 200, body: p };
  };
  await setValue("max_connections", "151");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  const reviewedCancel = await cancelAndAudit({ requested: "151", statements: ["/*myflames:variable*/ SET GLOBAL max_connections = 151"] });
  assert.equal(reviewedCancel.target.global_value, "205", "audit uses the newly reviewed snapshot");
  planReply = null;

  // Confirm: one apply with the expect identity; the answer shows before and after; the row is read again.
  const reads = state.seen.length;
  await openChange("max_connections");
  await setValue("max_connections", "300");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
  await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
  await page.click(confirmButton);
  await page.waitForSelector(`${view} .variable-result[data-status=changed]`);
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, 1);
  assert.equal(applied[0].confirmed, true);
  assert.equal(applied[0].mode, "global");
  assert.equal(applied[0].value, "300");
  assert.equal(applied[0].expect.statement, "SET GLOBAL max_connections = 300");
  assert.equal(applied[0].expect.global_value, "151");
  const result = await text(`${view} .variable-result`);
  assert.match(result, /Changed\. max_connections, now only\./);
  assert.match(result, /Global151 → 300/);
  assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("variable-result")), true, "the answer is focused");
  await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "config-changed.png") });
  for (let i = 0; i < 50 && state.seen.length === reads; i++) await pause(50);
  assert.equal(state.seen.length, reads + 1, "the configuration is read again after the change");
  await page.click(`${view} .variable-result .text-button`);
  await page.waitForFunction(() => !document.querySelector(".variable-result"));

  // A read-only MySQL variable: Change at next restart, SET PERSIST_ONLY only.
  await h.select(row("redo"));
  assert.match(await text('.view-inspector button[data-change="innodb_log_file_size"]'), /^Change at next restart…Unverified/);
  await openChange("innodb_log_file_size");
  assert.deepEqual(await texts(`${sheet} .variable-modes label strong`), ["After restart only"]);
  assert.equal(await text(`${sheet} .eyebrow`), "SET PERSIST_ONLY");
  await setValue("innodb_log_file_size", "64M");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST_ONLY innodb_log_file_size = 67108864;");
  assert.match(await text(`${sheet} .variable-field .hint`), /Sends 67,108,864 \(64\.0 MiB\)\./);
  const restart = await text(`${sheet} .variable-plan`);
  assert.match(restart, /The running value does not change; the new value applies at the next restart\./);
  assert.match(restart, /SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN/);
  assert.match(restart, /does not check the value of SET PERSIST_ONLY/);
  assert.equal(await h.label(confirmButton), "Persist for next restart");
  await page.click(`${sheet} .sheet-actions button:first-child`);
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, 1, "Cancel sends nothing");

  // MariaDB 13.0 defaults: Performance Schema off is watched; aliases are named.
  state.queue.push({ status: 200, body: mariadb });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 13\.0\.2/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  assert.match(await text(`${view} .summary-line`), /2 to watch/);
  assert.match(await text(row("performance_schema")), /Watch/);
  await h.select(row("performance_schema"));
  await page.waitForFunction(() => /MariaDB's default/.test(document.querySelector(".view-inspector")?.textContent || ""));
  await h.select(row("slow_log"));
  await page.waitForFunction(() => /log_slow_query \(alias slow_query_log\)/.test(document.querySelector(".view-inspector")?.textContent || ""));
  await h.select(row("sync_binlog"));
  await page.waitForFunction(() => /default 0/.test(document.querySelector(".view-inspector")?.textContent || ""));

  // ---- Change: MariaDB 11.4 has no PERSIST; the option file line is offered instead ----
  engine = "mariadb";
  state.queue.push({ status: 200, body: mariadb114 });
  await h.refresh();
  await page.waitForFunction(() => /MariaDB 11\.4/.test(document.querySelector(".server-view .fact-strip")?.textContent || ""));
  await h.idle(view);
  await h.select(row("max_connections"));
  await openChange("max_connections");
  assert.deepEqual(await texts(`${sheet} .variable-modes label strong`), ["Now only"], "no PERSIST modes on MariaDB");
  assert.match(await text(sheet), /MariaDB has no SET PERSIST/);
  await setValue("max_connections", "300");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
  assert.equal(await text(`${sheet} .variable-plan .perf-sql pre`), "max_connections = 300", "the option file line to copy");
  assert.match(await text(`${sheet} .variable-plan`), /CONNECTION ADMIN/);
  await page.screenshot({ path: path.join(h.shots, "config-change-mariadb.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  // A read-only MariaDB variable: no runtime change, the option file line and a restart.
  await h.select(row("performance_schema"));
  assert.equal(await text('.view-inspector button[data-change="performance_schema"]'), "Option file line…");
  await openChange("performance_schema");
  assert.equal(await page.$(`${sheet} .variable-modes`), null, "no mode can change it now");
  assert.match(await text(`${sheet} .variable-note`), /read-only while the server runs\. It changes only at a restart\./);
  await page.select(`${sheet} #variable-value-performance_schema`, "ON");
  await page.waitForFunction(() => /restart the server/.test(document.querySelector("dialog.variable-sheet .variable-alternative")?.textContent || ""));
  assert.equal(await text(`${sheet} h2`), "Change performance_schema at the next restart?");
  assert.equal(await text(`${sheet} .eyebrow`), "OPTION FILE");
  assert.equal(await page.$(`${sheet} .variable-problems`), null, "the reason is said once, in the note");
  assert.equal(await page.$(`${sheet} .variable-policy`), null, "nothing to send, so no policy line");
  assert.equal(await text(`${sheet} .variable-alternative .perf-sql pre`), "performance_schema = ON");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true);
  assert.equal(await h.label(confirmButton), "No change at runtime");
  await page.screenshot({ path: path.join(h.shots, "config-change-readonly.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, 1);

  // Session offers an editor handoff, using the planning connection value
  // only as a snapshot. It must never execute through variable/apply.
  engine = "mysql";
  const nullableReport = structuredClone(mysql);
  nullableReport.checks.find((c) => c.key === "max_connections").settings = [{
    name: "character_set_results", value: "latin1", display: "latin1", default: "utf8mb4", set_by: "default", dynamic: true,
  }];
  state.queue.push({ status: 200, body: nullableReport });
  await h.refresh();
  await h.idle(view);
  await h.select(row("max_connections"));
  await openChange("character_set_results");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement"));
  assert.match(await text(`${sheet} .variable-facts`), /Planning session valuelatin1.*Read at2026-10-15T14:30:00/s);
  assert.doesNotMatch(await text(`${sheet} .variable-facts`), /Global before|Global source|utf8mb4/);
  const nullChoice = `${sheet} .variable-field input[type=checkbox]`;
  assert.equal(await page.$eval(nullChoice, (e) => e.checked), false);
  await setValue("character_set_results", "NULL");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET SESSION character_set_results = 'NULL';");
  assert.equal(planned.at(-1).value, "NULL", "typing NULL sends text");
  await page.click(nullChoice);
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET SESSION character_set_results = NULL;");
  assert.equal(planned.at(-1).value, null, "the explicit choice sends JSON null");
  assert.equal(await page.$eval(`${sheet} #variable-value-character_set_results`, (e) => e.disabled), true);
  await page.setViewport({ width: 390, height: 844 });
  const nullableFit = await page.$eval(sheet, (d) => ({ width: d.getBoundingClientRect().width, scroll: d.scrollWidth - d.clientWidth }));
  assert.ok(nullableFit.width <= 390 && nullableFit.scroll <= 0, "SQL NULL controls fit at 390 px");
  await page.screenshot({ path: path.join(h.shots, "config-change-null-390.png") });
  await page.setViewport({ width: 1440, height: 1000 });
  assert.equal(await h.label(confirmButton), "Open in Query editor");
  assert.match(await text(sheet), /current Query editor tab without running/);
  assert.doesNotMatch(await text(`${sheet} .variable-plan`), /Changes this myflames session/);
  const writesBeforeHandoff = applied.length, auditBeforeHandoff = (await audit()).length;
  // The shell wires the handoff: the statement lands in the current Query
  // editor tab, selected, and nothing runs.
  assert.equal(await page.$eval(confirmButton, e => e.disabled), false);
  await page.click(confirmButton);
  await page.waitForSelector(".query-lab:not([hidden]) .editor-toolbar, .query-lab .editor-toolbar", { visible: true });
  await page.waitForFunction(() => {
    const e = document.querySelector('[aria-label="SQL editor"]');
    return !!e && e.value.endsWith("SET SESSION character_set_results = NULL;") &&
      e.value.slice(e.selectionStart, e.selectionEnd) === "SET SESSION character_set_results = NULL";
  });
  assert.equal(state.other.some(r => r.path === "/api/editor/run"), false);
  await h.server(); await h.view("config");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, writesBeforeHandoff, "Session never sends variable/apply");
  assert.equal((await audit()).length, auditBeforeHandoff, "opening or cancelling an SQL draft is not a write attempt");

  // A sent write whose persisted value cannot be read is not "nothing sent".
  state.queue.push({ status: 200, body: mysql });
  await h.refresh();
  await h.idle(view);
  await h.select(row("max_connections"));
  await openChange("max_connections");
  await setValue("max_connections", "300");
  await page.click(`${sheet} input[value=persist]`);
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST max_connections = 300;");
  applyReply = () => ({ status: 200, body: {
    ...variables["mysql:max_connections"].change, mode: "persist", status: "applied_unverified",
    message: "The statement succeeded, but the persisted value could not be read.",
    before: { global: "151", session: null, persisted: null, persisted_state: { state: "absent", present: false, value: null } },
    after: { global: "300", session: null, persisted: null, persisted_state: { state: "unreadable", present: null, value: null } },
  } });
  await page.click(confirmButton);
  await page.waitForSelector(`${view} .variable-result[data-status=applied_unverified]`);
  const unverified = await text(`${view} .variable-result`);
  assert.match(unverified, /Applied; verification incomplete\./);
  assert.match(unverified, /mysqld-auto\.cnfno value → not readable/);
  assert.doesNotMatch(unverified, /Nothing was sent|SQL NULL/);
  await page.screenshot({ path: path.join(h.shots, "config-change-unverified.png") });
  await h.idle(view);
  await page.click(`${view} .variable-result .text-button`);
  applyReply = null;

  // A denied mode cannot lock the Change entry: another mode may work.
  await h.setPrivileges({ set_global: privilegeAnswer("denied", "SYSTEM_VARIABLES_ADMIN"),
    set_persist: privilegeAnswer("denied", "SYSTEM_VARIABLES_ADMIN"),
    set_session_restricted: privilegeAnswer("denied", "SESSION_VARIABLES_ADMIN"),
    config: privilegeAnswer("denied", "PROCESS", { grey_out: false }) });
  assert.equal(await page.$eval(`${view} [data-action=refresh]`, e => e.disabled), false);
  await h.select(row("max_connections"));
  await assertPrivilege(page, '.view-inspector button[data-change="max_connections"]', "unknown");
  for (const [mode, wanted] of [["global", "denied"], ["persist", "allowed"], ["persist_only", "unknown"], ["reset_persist", "denied"]]) {
    planReply = body => {
      const p = planFor(body);
      p.privileges = { need: "SYSTEM_VARIABLES_ADMIN", note: "", checked: wanted !== "unknown",
        allowed: wanted === "allowed" ? true : wanted === "denied" ? false : null, via: null };
      p.variable.modes.reset_persist.available = true;
      return { status: 200, body: p };
    };
    await openChange("max_connections");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
    if (mode !== "global") {
      await page.click(`${sheet} input[value=${mode}]`);
      await page.waitForFunction((s, m) => document.querySelector(`${s} .variable-modes input:checked`)?.value === m, {}, sheet, mode);
      await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, confirmButton);
    }
    await assertPrivilege(page, confirmButton, wanted, wanted === "denied" ? "SYSTEM_VARIABLES_ADMIN" : undefined);
    if (wanted === "denied") {
      const before = applied.length;
      await page.click(confirmButton); await page.keyboard.press("Enter");
      assert.equal(applied.length, before, "a denied mode sends no write");
    }
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  }
  planReply = null;
  // Drafting SESSION SQL does not use the generic restricted-session gate.
  state.queue.push({ status: 200, body: nullableReport });
  await h.refresh(); await h.idle(view); await h.select(row("max_connections"));
  await openChange("character_set_results");
  await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement"));
  assert.equal(await h.label(confirmButton), "Open in Query editor");
  assert.equal(await page.$eval(confirmButton, e => e.getAttribute("aria-disabled")), null);
  const writesBeforeCancel = applied.length, auditBeforeCancel = (await audit()).length;
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  assert.equal(applied.length, writesBeforeCancel);
  assert.equal((await audit()).length, auditBeforeCancel);
  state.fallback = { status: 200, body: mysql };
  await h.refresh(); await h.idle(view);
  await h.setPrivileges({});

  // Error keeps the report.
  state.queue.push({ status: 400, body: { error: "Could not read the configuration: ERROR 2003 (HY000): Can't connect" } });
  await h.refresh();
  await page.waitForSelector(`${view} [role=alert]`);
  assert.ok(await page.$(`${view} .summary-line`));

  // A report from another connection is greyed and Change is off.
  const staging = await connectionOption(page, "Staging");
  await chooseConnection(page, staging);
  await page.waitForSelector(`${view}[data-other-connection=true]`);
  await h.select(row("max_connections"));
  assert.equal(await page.$eval('.view-inspector button[data-change="max_connections"]', (e) => e.disabled), true);
  assert.match(await page.$eval('.view-inspector button[data-change="max_connections"]', (e) => e.title), /refresh to load this server/);

  await h.narrow("config-390.png");

  const saved = await h.savedSlow((s) => s.kind === "config");
  assert.equal(saved.state.query_lab.slow.inspector, true, "the open inspector is part of the workspace");
  assert.equal(JSON.stringify(saved.state).includes("config-1.0"), false, "reports are not saved");
  assert.equal(JSON.stringify(saved.state).includes("variable-plan"), false, "plans are not saved");
  assert.deepEqual(h.errors, []);
  console.log("configuration view: ok");
} finally {
  await h.close();
}
