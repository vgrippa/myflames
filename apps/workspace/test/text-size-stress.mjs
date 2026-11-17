// Text size stress: the whole workspace at the largest Settings > Appearance
// text sizes (Interface text size 16, Code text size 20, with the included
// Inter and JetBrains Mono), and once more at the smallest (12 and 11). Every
// sidebar place, every Server and Workload view, the Query editor with
// results, each Plans view, every Settings section, the connection picker,
// the command palette and a review sheet, at 1440 and 720 px, light and dark:
// the page never scrolls sideways, the window status bar and the metric tiles
// never clip (their containers keep scrollWidth <= clientWidth). At the small
// sizes the narrow tile row keeps its designed sideways scroll, so the tile
// assertion runs at the largest sizes only.
//
// The real Python server on a throwaway store, live endpoints answered from
// fixtures (perf-harness.mjs); no database. Run after `npm run build`:
// node test/text-size-stress.mjs
// Screenshots: TEXT_SIZE_SHOTS=<dir> node test/text-size-stress.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { load, open, pause, setUiPreferences } from "./perf-harness.mjs";
import { chooseConnection, connectionOption, openConnectionPicker } from "./chrome-helpers.mjs";
import { EDITOR, setEditorText } from "./editor-helpers.mjs";

const shots = process.env.TEXT_SIZE_SHOTS || "";
if (shots) await fs.mkdir(shots, { recursive: true });

// ---- Fixtures and mocked endpoints -----------------------------------------
const fixture = async (name) => ({ status: 200, body: await load(name) });
const ok = (body) => ({ status: 200, body });
const diagram = {
  schema_version: "schema-diagram-1.0", relationships: [], warnings: [],
  tables: Array.from({ length: 12 }, (_, i) => ({ id: `t${i}`, schema: "shop", name: `table_${i}`,
    columns: [{ name: "id", data_type: "bigint", primary_key: true, nullable: false }] })),
};
const accounts = {
  schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, warnings: [], role_grants: [], default_roles: [],
  accounts: Array.from({ length: 20 }, (_, i) => ({ name: `user${i}`, host: i % 3 ? "%" : "localhost",
    plugin: "caching_sha2_password", locked: i % 7 === 0, expired: false, role: i % 9 === 0 })),
};
// The Query editor's session and run replies (editor-session-1.0, as
// editor.mjs models them).
const session = (over = {}) => ({
  schema_version: "editor-session-1.0", tab_id: "1", state: "idle",
  connection: { profile_id: null, name: "Primary", host: "db.example", port: 3306, user: "reader", type: "production", read_only: false },
  engine: "mysql", server_version: "8.4.3", connection_id: 42, autocommit: true, in_transaction: false,
  transaction_source: "performance_schema", transaction_note: null, sql_mode: "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES",
  backslash_escapes: true, safe_updates: true, safe_updates_default: true, read_only_transactions: false,
  started_at: "2026-10-15T12:00:00+00:00", idle_seconds: 0, idle_timeout_seconds: 1800,
  ended: false, ended_reason: null, message: null, notes: [], run: null, ...over,
});
const statement = (index, text, start, over = {}) =>
  ({ index, text, start, end: start + text.length, verb: text.split(/\s+/)[0].toLowerCase(), kind: "read", category: "read", status: "ok", duration_ms: 3, ...over });
const rows = (statementIndex, columns, data, over = {}) => ({
  type: "rows", statement: statementIndex,
  columns: columns.map((c) => ({ name: c, hex: false, type: null })), columns_known: true,
  rows: data, row_count: data.length, truncated: false, limit: 1000, stopped: null, label: null,
  server_rows: data.length, warnings: 0, duration_ms: 3, ...over,
});
let runN = 0;
const runReply = (statements, results) => ({
  schema_version: "editor-run-1.0", id: `run-${++runN}`, kind: "run", state: "complete",
  statements, results, stopped: null, message: null,
  finished_at: "2026-10-15T12:05:00.000Z",
  counts: { statements: statements.length, run: statements.length, rows: results.reduce((a, r) => a + r.row_count, 0), affected_rows: 0, warnings: 0, errors: 0 },
  audit_id: null, history_id: null,
});
const queuedRuns = [];
const editorRun = (body) => {
  if (body.decline) return ok({ status: "declined", message: "Nothing was sent.", session: session() });
  if (queuedRuns.length) return ok(queuedRuns.shift());
  const sql = body.sql || "SELECT 1";
  return ok({ status: "complete", session: session(),
    run: runReply([statement(0, sql, 0)], [rows(0, ["id", "name"], [[1, "Ada Lovelace"], [2, "Grace Hopper"], [3, "Katherine Johnson"]])]) });
};
const preview = ok({ status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "", warning: "" },
  policy: { action: "confirm", text: "Review before running." } });
// A Production connection's cached readiness facts (connections-readiness.mjs
// keeps them in localStorage; the read itself is mocked below).
const facts = { product: "MySQL", version: "8.4.3", read_only: false, super_read_only: false,
  performance_schema: true, replica: false, group_member: false, galera_member: false, checked_at: new Date().toISOString() };

// The Tables read, held back on demand: the status bar facts' worst case
// below keeps "Refreshing…" on the bar while the re-read is in flight, so
// the check does not depend on how fast a run happens to answer.
let holdTables = false;
let updateCheckFailed = false;
const tablesReport = await fixture("tables-mysql-8.4.json");
const extra = {
  "/api/live/innodb": await fixture("innodb-mysql-8.4.json"),
  "/api/live/error-log": await fixture("errorlog-mysql-8.4.json"),
  "/api/live/locks": await fixture("locks-mysql-8.4.json"),
  "/api/live/indexes": await fixture("indexes-mysql-8.4.json"),
  "/api/live/tables": () => (holdTables ? { ...tablesReport, delay: 4000 } : tablesReport),
  "/api/live/schemas": await fixture("schemas-mysql-8.4.json"),
  "/api/live/table-columns": (body) => ok({ schema: body.schema, table: body.table,
    columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment" }],
    indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }] }),
  "/api/schema-diagram/read": ok(diagram),
  "/api/live/config": await fixture("parameters-mysql.json"),
  "/api/live/instruments": await fixture("instruments-mysql-8.4.json"),
  "/api/lifecycle/tools": await fixture("lifecycle-tools-mysql.json"),
  "/api/lifecycle/jobs": ok({ jobs: [] }),
  "/api/accounts/list": ok(accounts),
  "/api/live/top": await fixture("top-mysql-8.4-lab.json"),
  "/api/live/sessions": await fixture("sessions-mysql-8.4.json"),
  "/api/live/memory": await fixture("memory-ok.json"),
  "/api/live/waits": await fixture("waits-mysql-8.4.json"),
  "/api/live/io": await fixture("io-mysql-8.4.json"),
  "/api/live/replication": await fixture("replication-mysql-8.4-healthy.json"),
  "/api/live/replication/skip-loop/status": ok({ loops: [] }),
  "/api/live/cluster": await fixture("cluster-ic-8.4-healthy.json"),
  "/api/live/proxysql": await fixture("proxysql-lab.json"),
  "/api/live/readyset": await fixture("readyset-lab.json"),
  "/api/live/report": await fixture("report-mysql-8.4.json"),
  // The update check, pinned so the version block in the status bar keeps
  // one width for the whole run instead of changing when the automatic
  // check lands (its timing is what made the facts clip come and go between
  // runs). The baseline is "Up to date"; the Tables worst case below flips
  // it to the widest realistic state, a failed check ("Could not check:
  // network offline", shown capped at 18ch).
  "/api/app/update/status": () => ok({ schema_version: "app-update-status-1.0", current_version: "2.5.0", auto_check: false, staging: null,
    last_check: { checked_at: new Date().toISOString(), outcome: updateCheckFailed ? "failed" : "up_to_date",
      current_version: "2.5.0", latest_version: null, reason: updateCheckFailed ? "network offline" : null } }),
  "/api/app/update/check": () => ok({ schema_version: "app-update-1.0", current_version: "2.5.0", latest_version: "2.5.0", update_available: false,
    release_title: "", release_notes: "", published_at: "2026-10-01T00:00:00+00:00",
    html_url: "https://github.com/vgrippa/myflames/releases" }),
  "/api/live/readiness": ok({ ...facts, engine: "mysql", flavor: "mysql", notes: [], privileges: {}, actions: {} }),
  "/api/editor/status": (body) => ok(body.run_id ? { status: "idle", session: session(), run: null } : { status: "none", session: null, run: null }),
  "/api/editor/preview": () => preview,
  "/api/editor/run": editorRun,
  "/api/editor/history": ok({ items: [] }),
  "/api/live/catalog": (body) => ok(body.table
    ? { schema_version: "catalog-1.0", level: "table", schema: body.schema, table: body.table,
        columns: [{ name: "id", type: "int", nullable: false, key: "PRI", default: null, extra: "auto_increment", generated: false }],
        indexes: [{ name: "PRIMARY", unique: true, primary: true, type: "BTREE", columns: [{ name: "id", expression: null, sub_part: null }] }] }
    : { schema_version: "catalog-1.0", level: "tables", schema: body.schema, truncated: false, limit: 200, prefix: null, after: null, next_after: null,
        tables: [{ name: "orders", type: "table" }, { name: "customers", type: "table" }] }),
};

const h = await open("/api/live/health", "text-size-stress", extra, {
  fallback: await fixture("health-mysql-8.4.json"),
});
const { page } = h;
const MOD = process.platform === "darwin" ? "Meta" : "Control";
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
const shown = (selector) => page.$eval(selector, (e) => !e.closest("[hidden]") && e.offsetParent !== null).catch(() => false);
const visibleText = (selector, text) => page.evaluate((s, t) =>
  [...document.querySelectorAll(s)].some((e) => !e.closest("[hidden]") && e.offsetParent !== null && e.textContent.trim().startsWith(t)), selector, text);

// ---- The sweep --------------------------------------------------------------
const report = [];
const failures = [];
let checkTiles = false; // the narrow tile row wraps only at the largest text
let passLabel = "large";
async function sweep(name, { prepare = null } = {}) {
  for (const width of [1440, 720]) {
    await page.setViewport({ width, height: width === 1440 ? 900 : 520 });
    if (prepare) await prepare(width);
    for (const theme of ["light", "dark"]) {
      await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
      await pause(200);
      const m = await page.evaluate(() => {
        const vis = (e) => e && !e.closest("[hidden]") && e.getClientRects().length;
        const doc = document.documentElement;
        const out = {
          page: doc.scrollWidth - doc.clientWidth,
          // Require the fact even when the whole status bar is missing or hidden.
          lastRunRequired: !!(vis(document.querySelector(".query-lab")) &&
            document.querySelector(".query-lab tbody tr")),
        };
        const bar = [...document.querySelectorAll(".window-statusbar")].find(vis);
        if (bar) {
          out.statusbar = bar.scrollWidth - bar.clientWidth;
          const facts = bar.querySelector(".window-statusbar-facts");
          if (facts) out.facts = facts.scrollWidth - facts.clientWidth;
          const updates = bar.querySelector(".window-statusbar-updates");
          if (updates) out.updates = updates.scrollWidth - updates.clientWidth;
          // Last run and the engine version are not repeated in the editor.
          // Ellipsis is allowed; display:none or a zero-width fact is not.
          if (out.lastRunRequired) {
            out.missingFacts = ["engine", "connection", "session", "last-run"].filter(status => {
              const fact = bar.querySelector(`[data-status="${status}"]`);
              return !vis(fact) || fact.getBoundingClientRect().width <= 0 || !fact.title;
            });
            const lastRun = bar.querySelector('[data-status="last-run"]');
            out.lastRunText = lastRun?.textContent.trim() || "";
            out.lastRunTextMatches = out.lastRunText ===
              `Last run ${new Date("2026-10-15T12:05:00.000Z").toLocaleTimeString()}`;
          }
          const safety = [...bar.querySelectorAll(".safety-status")].find(vis);
          if (safety) {
            const words = safety.querySelector(".safety-status-text");
            // A clipped label: the text is wider than what the bar shows it.
            out.safety = words ? words.scrollWidth - words.clientWidth : 0;
          }
        }
        const tiles = [...document.querySelectorAll(".tile-grid")].filter(vis).map((g) => g.scrollWidth - g.clientWidth);
        if (tiles.length) out.tiles = tiles;
        const dialog = [...document.querySelectorAll("dialog[open], [role=dialog][aria-modal=true], .picker-panel")].find(vis);
        if (dialog) {
          const r = dialog.getBoundingClientRect();
          out.dialog = Math.max(0, Math.round(r.right) - window.innerWidth, Math.round(-r.left));
        }
        return out;
      });
      const problems = [];
      if (m.page > 0) problems.push(`page scrolls sideways by ${m.page} px`);
      if (m.statusbar > 0) problems.push(`status bar clips ${m.statusbar} px`);
      if (m.facts > 0) problems.push(`status bar facts clip ${m.facts} px`);
      if (m.updates > 0) problems.push(`status bar version block clips ${m.updates} px`);
      if (m.missingFacts?.length) problems.push(`status facts hidden or missing tooltips: ${m.missingFacts.join(", ")}`);
      if (m.lastRunRequired && !m.lastRunTextMatches) problems.push(`missing or wrong Last run text: ${m.lastRunText || "(empty)"}`);
      if (m.safety > 0) problems.push(`write-safety label clips ${m.safety} px`);
      if (m.dialog > 0) problems.push(`dialog runs ${m.dialog} px past the window`);
      if (m.tiles && (checkTiles || width === 1440)) {
        const wide = m.tiles.filter((d) => d > 0);
        if (wide.length) problems.push(`tile grid clips ${Math.max(...wide)} px`);
      }
      report.push({ place: name, sizes: passLabel, width, theme, problem: problems.join("; ") || "" });
      if (shots) await page.screenshot({ path: path.join(shots, `${passLabel}-${name.replace(/[^a-z0-9]+/gi, "-")}-${width}-${theme}.png`) });
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => { document.documentElement.dataset.theme = "light"; });
  await pause(100);
}

/** A sidebar place of its own (not Server or Workload). */
async function place(key, ready) {
  await page.click(`.main-nav button[data-place="${key}"]`);
  await page.waitForSelector(ready, { visible: true });
  await pause(250);
}

async function sweepAll() {
  // Home, then every Server and Workload view.
  await place("home", ".home");
  await sweep("home");
  await h.server();
  for (const kind of ["health", "innodb", "errorlog", "locks", "indexes", "tables", "config", "instruments", "maintenance", "accounts"]) {
    await h.view(kind);
    await h.idle(`.server-view[data-view="${kind}"]`).catch(() => {});
    // A view that waits for its Load button gets it clicked (Error log).
    const loadButton = `.server-view[data-view="${kind}"] .view-empty .primary`;
    if (await shown(loadButton)) await page.click(loadButton).catch(() => {});
    await pause(350);
    await sweep(`server-${kind}`);
    // The status bar facts' worst case, deterministic: the widest realistic
    // version block (a failed update check) and a slow Tables re-read that
    // keeps "Refreshing…" next to the safety, engine, connection and
    // refreshed facts, the most the row ever holds. Large text only: the
    // smallest text fits this, and the fix is gated on the large scale.
    if (kind === "tables" && passLabel === "large") {
      updateCheckFailed = true;
      await page.reload({ waitUntil: "networkidle0" });
      await h.server();
      await h.view("tables");
      await h.idle('.server-view[data-view="tables"]').catch(() => {});
      await pause(350);
      holdTables = true;
      await page.click('.server-view[data-view="tables"] [data-action="refresh"]');
      await page.waitForSelector('.window-statusbar [data-status="refreshing"]', { visible: true });
      await sweep("server-tables-refreshing");
      holdTables = false;
      updateCheckFailed = false;
      await page.reload({ waitUntil: "networkidle0" });
      await h.server();
      await h.view("tables");
      await h.idle('.server-view[data-view="tables"]').catch(() => {});
      await pause(300);
    }
  }
  await h.workload();
  for (const kind of ["top", "sessions", "memory", "waits", "io"]) {
    await h.view(kind);
    await h.idle(`.server-view[data-view="${kind}"]`).catch(() => {});
    await pause(350);
    await sweep(`workload-${kind}`);
  }
  // The places of their own around a connection.
  await place("replication", '.server-view[data-view="replication"]');
  await h.idle('.server-view[data-view="replication"]').catch(() => {});
  await sweep("replication");
  await place("cluster", '.server-view[data-view="cluster"] .cluster-tiles');
  await sweep("cluster");
  await place("proxysql", '.server-view[data-view="proxysql"]');
  await h.idle('.server-view[data-view="proxysql"]').catch(() => {});
  await sweep("proxysql");
  await place("readyset", '.server-view[data-view="readyset"]');
  await h.idle('.server-view[data-view="readyset"]').catch(() => {});
  await sweep("readyset");
  await place("tuning", '.server-view[data-view="tuning"]');
  await h.idle('.server-view[data-view="tuning"]').catch(() => {});
  await sweep("tuning");
  await h.fullAnalysis().catch(() => {});
  await sweep("tuning-full-analysis");
  // The SQL workspace group.
  await place("query", ".query-lab .editor-toolbar");
  await setEditorText(page, "SELECT id, name FROM customers WHERE country = 'IT' ORDER BY name LIMIT 25;");
  await page.click("[data-action=run]");
  await page.waitForSelector(".query-lab tbody tr", { visible: true });
  await sweep("query-editor-results");
  // The review sheet of a write on a Production connection.
  queuedRuns.push({ status: "needs_confirmation", session: session(),
    confirmation: { token: "ts1", expires_in: 300, title: "Production connection",
      text: "This is a Production connection, so myflames asks before every write.",
      tone: "danger", kind: "write", category: "dml", reasons: [], warnings: [], in_transaction: false,
      statements: ["DELETE FROM sessions WHERE expires_at < NOW()"], count: 1, connection_type: "production",
      connection: { name: "Primary", host: "db.example", port: 3306, user: "reader" } } });
  await setEditorText(page, "DELETE FROM sessions WHERE expires_at < NOW()");
  await page.click("[data-action=run]");
  await page.waitForSelector(".editor-confirm", { visible: true });
  await sweep("review-sheet");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".editor-confirm"));
  await pause(200);
  // Plans: the sample plan, each chart view (the Plans empty state offers the
  // sample; on the second pass the session already has it open).
  await place("plans", ".plans-empty, .plan-screen");
  if (await shown(".plans-empty")) await h.button("Explore sample plan", ".plans-empty ");
  // The session restores the chart view that was open, which may not be the
  // Visual Explain; the view loop below picks each one.
  await page.waitForSelector(".plan-chart[data-view]", { visible: true });
  for (const [view, label] of [["workbench", "Visual Explain"], ["flamegraph", "Flame graph"], ["tree", "Execution tree"], ["bargraph", "Bar chart"]]) {
    await sweep(`plans-${view}`, { prepare: async (width) => {
      if (width === 720) {
        await page.waitForSelector(".chart-phone-options select", { visible: true });
        await page.select(".chart-phone-options select", view);
      } else {
        await h.button(label, ".chart-views ");
      }
      await page.waitForSelector(`.plan-chart[data-view="${view}"]`, { visible: true });
      await pause(250);
    } });
  }
  // The rest of the sidebar.
  await place("digest", ".qd-place");
  await h.idle(".qd-place").catch(() => {});
  await sweep("query-digest");
  await place("diagram", '.server-view[data-view="diagram"]');
  await pause(400);
  await sweep("database-diagram");
  await place("schema", ".schema-versions-place");
  await sweep("schema-versions");
  await place("files", ".files-place");
  await sweep("file-transfer");
  await place("investigations", "section.library");
  await sweep("investigations");
  await place("learn", ".teach-catalog");
  await sweep("learn");
  // Settings, every section (Appearance holds the Text group).
  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector(".settings-page", { visible: true });
  for (const section of ["Appearance", "Query editor", "Workspace", "Updates and news", "Client tools"]) {
    await page.locator(`.settings-page .source-item ::-p-text(${section})`).click();
    await page.waitForFunction((t) => document.querySelector(".settings-panel-title")?.textContent === t, {}, section);
    await pause(200);
    await sweep(`settings-${section.toLowerCase().replace(/[^a-z]+/g, "-")}`);
  }
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));
  // The connection picker and the command palette as overlays (the picker's
  // pill renders on the places that use a connection).
  await h.server();
  await openConnectionPicker(page);
  await sweep("connection-picker");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".window-toolbar .picker-panel"));
  await page.keyboard.down(MOD); await page.keyboard.press("k"); await page.keyboard.up(MOD);
  await page.waitForSelector("dialog.command-palette[open]", { visible: true });
  await page.type('input[aria-label="Search commands"]', "theme");
  await pause(200);
  await sweep("command-palette");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.command-palette[open]"));
}

try {
  // A Production connection in use, so the status bar carries the write
  // safety ("Production · changes ask first") next to the version block.
  await api("/api/profiles/save", { value: { name: "Primary", host: "db.example", port: 3306, user: "reader", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production" } });
  await page.reload({ waitUntil: "networkidle0" });
  // The connection pill renders on the places that use a connection.
  await h.server();
  await page.waitForSelector(".window-toolbar .connection-pill");
  const id = await connectionOption(page, "Primary");
  await page.evaluate((key, cached) => {
    localStorage.setItem("myflames.connectionReadiness.v1", JSON.stringify({ [key]: { facts: cached } }));
  }, JSON.stringify([id, "db.example", 3306, "reader", "PREFERRED", ""]), facts);
  // The readiness cache is read at startup, so reload before choosing.
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector(".window-toolbar .connection-pill");
  await chooseConnection(page, id);
  await h.server();
  await page.waitForSelector('.window-statusbar .safety-status[data-safety="production"]', { visible: true });

  // The largest text: Inter 16 for the interface, JetBrains Mono 20 for code.
  // A reload paints from the store the way startup does.
  await setUiPreferences(page, { ui_font: "inter", ui_text_size: 16, code_font: "jetbrains-mono", code_text_size: 20 });
  await page.reload({ waitUntil: "networkidle0" });
  // The reload restores the place that was open; the nav marks readiness.
  await page.waitForSelector(".main-nav button[data-place=home]", { visible: true });
  await page.waitForFunction(() => getComputedStyle(document.body).fontFamily.includes("Inter"));
  passLabel = "large";
  checkTiles = true;
  await sweepAll();

  // The smallest sizes once, the same matrix.
  await setUiPreferences(page, { ui_text_size: 12, code_text_size: 11 });
  await page.reload({ waitUntil: "networkidle0" });
  // The reload restores the place that was open; the nav marks readiness.
  await page.waitForSelector(".main-nav button[data-place=home]", { visible: true });
  passLabel = "small";
  checkTiles = false;
  await sweepAll();

  assert.deepEqual(h.errors, []);
  const bad = report.filter((r) => r.problem);
  for (const row of report) console.log(`${row.sizes}\t${row.width}\t${row.theme}\t${row.place}\t${row.problem || "ok"}`);
  assert.deepEqual(bad.map((r) => `${r.sizes} ${r.place} ${r.width} ${r.theme}: ${r.problem}`), []);
  console.log(`Text size stress passed: ${report.length} checks, no sideways scroll, no clipped status bar or tiles.`);
} finally {
  await h.close();
}
