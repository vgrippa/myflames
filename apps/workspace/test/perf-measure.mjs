#!/usr/bin/env node
// Responsiveness measurements for the workspace: how fast and how smooth it
// feels, measured, not judged. Not a pass/fail suite; it writes numbers.
//
//   npm run build && node test/perf-measure.mjs [--runs 3]
//     [--sizes 1440x900,720x520] [--only cold,places,views,tables,overlays,editor,charts,idle,resize,memory,desktop,probe]
//     [--cycles 10] [--dpr 2] [--cpu 1] [--plan FILE]
//     [--out DIR] [--assets DIR] [--profile] [--app PATH/myflames.app]
//
// Each run starts the real Python workspace server from this checkout on a
// temporary HOME (USERPROFILE=HOME, MYFLAMES_SECRET_BACKEND=vault, a fresh
// workspace database; never ~/.myflames) and a fresh headless Chrome. Live
// endpoints answer from the fixtures in test/fixtures, scaled up (2,000
// sessions, 1,000 digests, every variable of the generated catalog, 10,000
// tables and indexes); every other live read is refused, so nothing reaches a
// database. Only /api/ requests are intercepted (CDP Fetch with a pattern),
// so the app's own chunks load from the server as they would in the app.
//
// Measured in the page: input event timestamps, Event Timing (input to next
// paint, the INP measure), long tasks over 50 ms, layout shifts, frame
// intervals while scrolling (requestAnimationFrame), DOM mutations (settled =
// 250 ms without a change), and CDP Performance metrics (script, layout and
// style time) and heap and DOM counters after a forced GC.
//
// --profile also records a CPU profile per interaction and counts the React
// components each commit rendered (a DevTools hook installed before React
// loads), and writes the top functions next to the numbers. Use it with
// --assets pointing at an unminified build (vite build --minify false) so
// names are the source's.
//
// --only desktop launches the packaged app (apps/desktop/dist, or --app) with
// a temporary MYFLAMES_DESKTOP_DATA and a remote debugging port, and times
// spawn to the backend's launch, the first paint and the ready Home screen.
//
// Results: <out>/results.json (every run) and <out>/summary.md (medians).
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import { existsSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const here = fileURLToPath(new URL(".", import.meta.url));
const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = path.join(here, "fixtures");
const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] && !args[i + 1].startsWith("--") ? args[i + 1] : fallback;
};
const flag = (name) => args.includes(`--${name}`);
const RUNS = Number(opt("runs", "3"));
const SIZES = opt("sizes", "1440x900,720x520").split(",").map((s) => s.split("x").map(Number));
const ALL = ["cold", "places", "views", "tables", "overlays", "editor", "charts", "idle", "resize", "memory", "desktop"];
const ONLY = opt("only", ALL.join(",")).split(",");
const OUT = path.resolve(opt("out", path.join(os.tmpdir(), "myflames-perf")));
const ASSETS = opt("assets", "");
const PROFILE = flag("profile");
const APP = opt("app", path.join(root, "apps/desktop/dist/mac-arm64/myflames.app"));
const MEMORY_CYCLES = Number(opt("cycles", "10"));
const DPR = Number(opt("dpr", "2"));
const CPU = Number(opt("cpu", "1"));
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));
const log = (...a) => { if (!flag("quiet")) console.error(...a); };

// ---------------------------------------------------------------- data ---

const PLACES = ["home", "server", "workload", "replication", "cluster", "proxysql", "tuning", "query", "plans", "investigations", "learn"];
const SERVER_VIEWS = ["health", "innodb", "errorlog", "locks", "indexes", "tables", "diagram", "config", "instruments", "maintenance", "accounts"];
const WORKLOAD_VIEWS = ["top", "sessions", "waits", "io"];

function topReport(n) {
  const shapes = [
    "SELECT `o` . `id` , `o` . `total` , `c` . `name` FROM `orders` `o` JOIN `customers` `c` ON `c` . `id` = `o` . `customer_id` WHERE `o` . `status` = ? ORDER BY `o` . `total` DESC LIMIT ?",
    "UPDATE `inventory` SET `qty` = `qty` - ? WHERE `sku` = ?",
    "SELECT COUNT ( * ) FROM `orders` WHERE `created_at` > ?",
    "SELECT * FROM `products` WHERE `name` LIKE ?",
    "INSERT INTO `order_items` ( `order_id` , `sku` , `qty` , `price` ) VALUES (...)",
    "SELECT `c` . `name` , SUM ( `oi` . `qty` ) FROM `order_items` `oi` JOIN `categories` `c` ON `c` . `id` = `oi` . `category_id` GROUP BY `c` . `name`",
    "DELETE FROM `sessions` WHERE `expires_at` < ?",
    "SELECT `id` FROM `carts` WHERE `customer_id` = ? AND `updated_at` < ?",
  ];
  const flagSets = [["no_index_used", "low_selectivity"], [], ["tmp_disk_tables"], ["no_good_index_used"], ["sort_merge_passes", "no_index_used"]];
  const statements = Array.from({ length: n }, (_, i) => {
    const text = shapes[i % shapes.length].replace(/`(orders|products|sessions|carts)`/, (m, t) => `\`${t}_${i}\``);
    const count = 1000 + ((i * 7919) % 900000);
    const total = Math.round((((i * 104729) % 250000) + 10) * 10) / 10;
    const examined = (i * 15485863) % 900000000;
    const sent = (i * 32452843) % 1000000;
    const flags = flagSets[i % flagSets.length];
    return {
      digest: (i + 0x1000).toString(16).padStart(64, "a"), digest_text: text, schema: `shop${i % 12}`,
      count, total_ms: total, avg_ms: Math.round((total / count) * 1000) / 1000, max_ms: Math.round((total / count) * 3.2 * 1000) / 1000,
      rows_examined: examined, rows_sent: sent, examined_per_sent: sent ? Math.round((examined / sent) * 100) / 100 : null,
      no_index_used: flags.includes("no_index_used") ? count : 0, no_good_index_used: flags.includes("no_good_index_used") ? count : 0,
      tmp_disk_tables: flags.includes("tmp_disk_tables") ? count : 0, sort_merge_passes: flags.includes("sort_merge_passes") ? count * 3 : 0,
      sample_text: null, sample_truncated: false, first_seen: "2026-10-14 08:00:00", last_seen: "2026-10-16 11:59:00", flags,
      capture_problem: /^SELECT/.test(text) ? null : "Only SELECT statements can be explained here.",
      select_full_join: 0, select_range_check: 0, select_scan: flags.includes("no_index_used") ? count : 0,
    };
  });
  return {
    schema_version: "top-1.0", engine: "mysql", server_version: "8.4.11", order: "total", limit: n, schema: null,
    notes: ["Counters accumulate since the server started or the digest table was last truncated."],
    statements, table: null, coverage: { uptime_s: 192385, first_seen: "2026-10-14 08:00:00", digest_lost: 0, overflow: null },
  };
}

async function buildData() {
  const sessions = await load("sessions-mysql-8.4-active.json");
  const base = sessions.sessions;
  const users = ["app", "report", "etl", "root", "web", "batch"];
  sessions.sessions = Array.from({ length: 2000 }, (_, i) => {
    const b = structuredClone(base[i % base.length]);
    b.id = 10000 + i;
    b.user = users[i % users.length];
    b.host = `10.0.${(i >> 8) & 255}.${i & 255}:${40000 + i}`;
    b.client_host = `10.0.${(i >> 8) & 255}.${i & 255}`;
    b.client_port = 40000 + i;
    b.db = `shop${i % 12}`;
    b.time_s = (i * 37) % 4000;
    if (b.trx) b.trx = { ...b.trx, id: String(30000 + i), age_s: b.time_s };
    if (b.info) b.info = `${b.info} /* ${i} */`;
    return b;
  });
  sessions.matched = 2000;
  sessions.limit = 5000;
  sessions.truncated = false;
  sessions.summary.total = 2000;
  sessions.server_identity = { server_uuid: "perf-server-uuid" };

  const parameters = await load("parameters-mysql.json");
  const catalog = JSON.parse(execFileSync(process.env.PYTHON || "python3", ["-c",
    "import json; from myflames.mysql_sysvars import SYSVARS, VALUES; print(json.dumps({'vars': SYSVARS, 'values': {k: list(v) for k, v in VALUES.items()}}))"],
    { cwd: root, encoding: "utf8" }));
  const known = new Map(parameters.parameters.items.map((item) => [item.name, item]));
  let n = 0;
  parameters.parameters.items = Object.entries(catalog.vars).map(([name, [scope, type, flags]]) => {
    if (known.has(name)) return known.get(name);
    n++;
    const ro = /\bro\b/.test(flags), np = /\bnp\b/.test(flags), pro = /\bpro\b/.test(flags);
    const kind = ["integer", "boolean", "enum", "string", "set"].includes(type) ? (type === "set" ? "string" : type) : "string";
    return {
      value: kind === "boolean" ? (n % 2 ? "ON" : "OFF") : kind === "integer" ? String(1024 * n) : kind === "enum" ? (catalog.values[name] || ["DEFAULT"])[0] : `/var/lib/mysql/${name}`,
      truncated: false, scope: "global",
      apply_type: ro ? (pro ? "static" : null) : "dynamic",
      modifiable: ro ? (pro ? "restart" : np ? null : "option_file") : "now",
      type: kind, values: kind === "enum" ? catalog.values[name] || null : null,
      min: kind === "integer" ? 0 : null, max: kind === "integer" ? 4294967295 : null, block_size: kind === "integer" ? 1 : null,
      default: kind === "integer" ? String(512 * n) : kind === "boolean" ? "OFF" : "", source: n % 9 ? "COMPILED" : "DYNAMIC",
      source_label: n % 9 ? "Compiled default" : "SET GLOBAL", source_path: null, set_time: null, set_user: null, set_host: null,
      persisted: false, modified: n % 9 === 0, checks: [], name, scope_detail: scope,
      description: `Synthetic description of ${name}: ${type} with ${scope} scope, as the server catalog lists it, long enough to wrap in the inspector.`,
    };
  });

  const tables = await load("tables-mysql-8.4.json");
  const engines = ["InnoDB", "InnoDB", "InnoDB", "MyISAM"];
  tables.tables = Array.from({ length: 10000 }, (_, i) => ({
    schema: `app${i % 40}`, table: `table_${String(i).padStart(5, "0")}`, engine: engines[i % 4],
    rows: (i * 7919) % 5000000, data_bytes: ((i * 104729) % 900) * 16384, index_bytes: ((i * 31) % 400) * 16384,
    size_bytes: ((i * 104729) % 900 + (i * 31) % 400) * 16384, partitioned: i % 97 === 0,
  }));
  const frag = tables.fragmentation.find((f) => f.status === "info") || tables.fragmentation[0];
  tables.fragmentation = [...tables.fragmentation, ...Array.from({ length: 10000 }, (_, i) => ({
    ...frag, schema: `app${i % 40}`, table: `table_${String(i).padStart(5, "0")}`, rows: (i * 7919) % 5000000,
    free_bytes: ((i * 13) % 300) * 1048576, free_pct: (i * 13) % 90,
    fix_sql: `OPTIMIZE TABLE \`app${i % 40}\`.\`table_${String(i).padStart(5, "0")}\`;`,
  }))];
  tables.summary.tables = 10000;
  tables.more = { ...tables.more, tables: 0, fragmentation: 0 };

  const indexes = await load("indexes-mysql-8.4.json");
  const unused0 = indexes.unused[0];
  indexes.unused = Array.from({ length: 1000 }, (_, i) => ({ ...unused0, schema: `app${i % 40}`, table: `table_${String(i).padStart(5, "0")}`, index: `idx_${i}` }));
  indexes.indexes = Array.from({ length: 10000 }, (_, i) => ({
    ...unused0, schema: `app${i % 40}`, table: `table_${String(i >> 1).padStart(5, "0")}`, index: i % 2 ? `idx_${i}` : "PRIMARY",
    columns: i % 3 ? "(`a`, `b`)" : "(`id`)", type: "BTREE", unique: i % 2 === 0, visible: true,
  }));
  indexes.more = { ...indexes.more, unused: 0, indexes: 0 };
  indexes.summary = { ...indexes.summary, indexes: 10000, unused: 1000 };

  const accounts = {
    schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, warnings: [], role_grants: [], default_roles: [],
    accounts: Array.from({ length: 60 }, (_, i) => ({ name: `user${i}`, host: i % 3 ? "%" : "localhost", plugin: "caching_sha2_password", locked: i % 11 === 0, expired: false, role: i % 13 === 0 })),
  };
  const diagram = { schema_version: "schema-diagram-1.0", relationships: [], warnings: [],
    tables: Array.from({ length: 30 }, (_, i) => ({ id: `t${i}`, schema: "shop", name: `t${i}`, columns: [{ name: "id", data_type: "bigint", primary_key: true, nullable: false }] })) };
  const routes = {
    "/api/live/health": await load("health-mysql-8.4-full.json"),
    "/api/live/innodb": await load("innodb-mysql-8.4.json"),
    "/api/live/error-log": await load("errorlog-mysql-8.4.json"),
    "/api/live/locks": await load("locks-mysql-8.4.json"),
    "/api/live/indexes": indexes,
    "/api/live/tables": tables,
    "/api/live/schemas": await load("schemas-mysql-8.4.json"),
    "/api/schema-diagram/read": diagram,
    "/api/live/config": parameters,
    "/api/live/instruments": await load("instruments-mysql-8.4.json"),
    "/api/lifecycle/tools": await load("lifecycle-tools-mysql.json"),
    "/api/lifecycle/jobs": { jobs: [] },
    "/api/accounts/list": accounts,
    "/api/live/top": topReport(1000),
    "/api/live/sessions": sessions,
    "/api/live/waits": await load("waits-mysql-8.4.json"),
    "/api/live/io": await load("io-mysql-8.4.json"),
    "/api/live/replication": await load("replication-mysql-8.4-healthy.json"),
    "/api/live/replication/skip-loop/status": { loops: [] },
    "/api/live/cluster": await load("cluster-ic-8.4-healthy.json"),
    "/api/live/proxysql": await load("proxysql-lab.json"),
    "/api/live/report": await load("report-mysql-8.4.json"),
    "/api/live/privileges": { schema_version: "privileges-1.0", account: "root@localhost", actions: {} },
  };
  const encoded = {};
  for (const [p, body] of Object.entries(routes)) encoded[p] = Buffer.from(JSON.stringify(body)).toString("base64");
  const sizes = Object.fromEntries(Object.entries(routes).map(([p, body]) => [p, JSON.stringify(body).length]));
  return { encoded, sizes, counts: { sessions: 2000, top: 1000, parameters: parameters.parameters.items.length, tables: 10000, indexes: 10000 } };
}

/** A synthetic EXPLAIN ANALYZE FORMAT=JSON plan of about *leaves* × 2
 * operators: Limit, Sort and Aggregate over a balanced tree of nested loop
 * joins (a stress case; the largest committed fixture has 17 operators). */
function syntheticPlan(leaves) {
  let n = 0;
  const leaf = () => {
    const i = n++;
    const lookup = i % 3 !== 0;
    return { operation: lookup ? `Index lookup on t${i} using idx_k (k=t${i - 1}.id)` : `Table scan on t${i}`, table_name: `t${i}`, alias: `t${i}`,
      schema_name: "perf", access_type: lookup ? "index" : "table", index_name: lookup ? "idx_k" : undefined,
      actual_rows: 10 + (i % 50), actual_loops: 1 + (i % 7), estimated_rows: 12 + (i % 40), estimated_total_cost: 5 + i,
      actual_first_row_ms: 0.01 * (i % 9), actual_last_row_ms: 0.2 + 0.05 * (i % 30), used_columns: ["id", "k"] };
  };
  const join = (count) => {
    if (count === 1) return leaf();
    const half = count >> 1;
    const inputs = [join(half), join(count - half)];
    return { operation: "Nested loop inner join", access_type: "join", actual_rows: 100 * count, actual_loops: 1, estimated_rows: 90 * count,
      estimated_total_cost: 20 * count, actual_first_row_ms: 0.05, actual_last_row_ms: 0.4 * count, inputs };
  };
  const tree = join(leaves);
  return { query: `/* synthetic ${leaves}-table join */ select count(*) from ${Array.from({ length: Math.min(leaves, 8) }, (_, i) => `t${i}`).join(", ")} ...`,
    operation: "Limit: 20 row(s)", query_type: "select", access_type: "limit", actual_rows: 20, actual_loops: 1, limit: 20, limit_offset: 0,
    actual_first_row_ms: 800, actual_last_row_ms: 820, inputs: [{ operation: "Sort: count(*) DESC", access_type: "sort", actual_rows: 20, actual_loops: 1,
      actual_first_row_ms: 790, actual_last_row_ms: 810, inputs: [{ operation: "Aggregate using temporary table", access_type: "temp_table_aggregate",
        actual_rows: 400, actual_loops: 1, actual_first_row_ms: 780, actual_last_row_ms: 800, inputs: [tree] }] }] };
}

// ---------------------------------------------------------------- server ---

async function startServer() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-perf-"));
  const store = path.join(directory, "workspace.sqlite3");
  const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" };
  if (ASSETS) env.MYFLAMES_TEST_ASSETS = path.resolve(ASSETS);
  const proc = spawn(process.env.PYTHON || "python3", ["-c",
    "import os,sys; from pathlib import Path; import myflames.ui as u; u.ASSET_DIR=Path(os.environ.get('MYFLAMES_TEST_ASSETS',u.ASSET_DIR)); s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    store], { cwd: root, env });
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(`server did not start: ${text}`)), 20000);
    proc.stderr.on("data", (c) => {
      text += c;
      const m = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (m) { clearTimeout(timer); resolve(m[0]); }
    });
    proc.on("error", reject);
  });
  return { url, origin: new URL(url).origin, directory, async close() { proc.kill(); await fs.rm(directory, { recursive: true, force: true }); } };
}

// --------------------------------------------------------- instrumentation ---

/** What each view draws once its report is on screen (or the empty state of
 * a view that waits for a click). Checked with --only probe. */
const VIEW_MARKERS = {
  health: ".metric-tile", innodb: ".innodb-section", errorlog: ".view-empty .primary, tbody tr, .errorlog-entry",
  locks: "tbody tr, .view-empty", indexes: "[data-report-table] tbody tr", tables: "[data-report-table] tbody tr",
  diagram: ".diagram-source", config: "tbody tr .parameter-name", instruments: ".grouped-row, tbody tr",
  maintenance: ".view-empty .primary, .maintenance-section, .grouped-row", accounts: ".view-empty .primary, tbody tr",
  top: ".view-empty .primary, tbody tr[data-row-key]", sessions: "tbody tr[data-session]", waits: "tbody tr", io: ".metric-tile",
  replication: ".topology-node, .metric-tile", cluster: ".cluster-card, .cluster-tiles", proxysql: "tbody tr", tuning: ".tuning-score",
};
// Installed before any page script: observers, input timestamps, a mutation
// clock, frame helpers, and (with --profile) a React commit counter.
const INSTRUMENT = (profile) => `(() => {
  if (window.__perf) return;
  const P = window.__perf = { long: [], shifts: [], events: [], inputs: [], lastMut: 0, muts: 0, mo: null, commits: [], armT: 0 };
  const obs = (type, fn, extra) => { try { new PerformanceObserver((l) => l.getEntries().forEach(fn)).observe(Object.assign({ type, buffered: true }, extra || {})); } catch (e) {} };
  obs('longtask', (e) => P.long.push([e.startTime, e.duration]));
  obs('layout-shift', (e) => { if (!e.hadRecentInput) P.shifts.push([e.startTime, e.value]); });
  obs('event', (e) => P.events.push([e.name, e.startTime, e.duration, e.processingStart, e.processingEnd, e.interactionId || 0]), { durationThreshold: 16 });
  for (const t of ['pointerdown', 'keydown', 'wheel', 'input', 'change']) addEventListener(t, (e) => P.inputs.push([t, e.timeStamp]), { capture: true, passive: true });
  P.vis = (sel, root) => { for (const el of (root || document).querySelectorAll(sel)) if (!el.closest('[hidden]') && el.getClientRects().length) return el; return null; };
  P.busy = (root) => { const r = typeof root === 'string' ? P.vis(root) : root; if (!r) return true;
    for (const el of r.querySelectorAll('.spinner, [role=status]')) { if (el.closest('[hidden]') || !el.getClientRects().length) continue;
      if (el.classList.contains('spinner') || /^\\s*(Loading|Reading|Opening)/.test(el.textContent || '')) return true; } return false; };
  // A view is ready when its data (or its empty state with one action) is drawn and nothing is loading.
  P.markers = ${JSON.stringify(VIEW_MARKERS)};
  P.view = (kind) => { const r = P.vis('.server-view[data-view="' + kind + '"]'); return !!r && !P.busy(r) &&
    !!P.vis(P.markers[kind] || 'tbody tr, .view-empty', r); };
  // The visible Server or Workload view (a place reopens the view it showed last).
  P.anyView = () => { const r = P.vis('.server-view'); return !!r && P.view(r.dataset.view); };
  P.arm = () => { P.armT = performance.now(); P.inputs.length = 0; P.lastMut = P.armT; P.muts = 0; if (P.mo) P.mo.disconnect();
    P.mo = new MutationObserver((records) => { for (const r of records) { const t = r.target.nodeType === 1 ? r.target : r.target.parentElement;
      if (t && t.closest && t.closest('.sidebar, .save-status, .window-statusbar')) continue; P.muts++; P.lastMut = performance.now(); } });
    P.mo.observe(document.body, { subtree: true, childList: true, attributes: true, characterData: true }); return P.armT; };
  // First frame (after style, layout and paint work of that frame) where pred holds.
  P.until = (pred, timeout) => new Promise((resolve) => { const s = performance.now(); timeout = timeout || 20000;
    const tick = () => { let ok = false; try { ok = pred(); } catch (e) {} if (ok) { P.anims = document.getAnimations().length; return setTimeout(() => resolve(performance.now()), 0); }
      if (performance.now() - s > timeout) return resolve(-1); requestAnimationFrame(tick); }; requestAnimationFrame(tick); });
  P.settle = (quiet, max) => new Promise((resolve) => { const s = performance.now();
    const tick = () => { const now = performance.now(); if (now - P.lastMut >= quiet || now - s >= max) { if (P.mo) P.mo.disconnect(); return resolve({ last: P.lastMut, capped: now - s >= max, muts: P.muts }); } setTimeout(tick, 20); }; tick(); });
  P.window = (t0, t1) => ({ long: P.long.filter(([s, d]) => s + d > t0 && s < t1).map(([s, d]) => [s - t0, d]),
    events: P.events.filter((e) => e[1] >= t0 - 2 && e[1] <= t1), shifts: P.shifts.filter(([s]) => s >= t0 && s <= t1),
    commits: P.commits.filter((c) => c.t >= t0 - 2 && c.t <= t1) });
  // The nearest ancestor that really scrolls vertically (try one pixel).
  P.scroller = (el) => { for (let e = el; e && e !== document.body; e = e.parentElement) { const s = getComputedStyle(e);
    if (/(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 4) { const t = e.scrollTop; e.scrollTop = t + 1; const moved = e.scrollTop !== t; e.scrollTop = t; if (moved) return e; } }
    return document.scrollingElement; };
  // Scroll *el*'s scroller by *step* px per frame for *frames* frames; the frame times.
  P.scroll = (sel, step, frames) => new Promise((resolve) => { const start = P.vis(sel); if (!start) return resolve(null); const el = P.scroller(start);
    const top0 = el.scrollTop; const times = []; let i = 0; const t0 = performance.now();
    const tick = (ts) => { times.push(ts); if (i++ >= frames) return resolve({ times, moved: el.scrollTop - top0, t0, t1: performance.now(), scroller: el.className || el.tagName });
      el.scrollTop += step; if (el.scrollTop + el.clientHeight >= el.scrollHeight - 2) step = -Math.abs(step); requestAnimationFrame(tick); };
    requestAnimationFrame(tick); });
  P.frames = (ms) => new Promise((resolve) => { const times = []; const s = performance.now();
    const tick = (ts) => { times.push(ts); if (performance.now() - s >= ms) return resolve(times); requestAnimationFrame(tick); }; requestAnimationFrame(tick); });
  ${profile ? `
  // React commits: which components rendered (props or state changed, or new).
  const nameOf = (t) => !t ? '' : typeof t === 'string' ? '' : (t.displayName || t.name || (t.render && (t.render.displayName || t.render.name)) || (t.type && nameOf(t.type)) || '');
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = { supportsFiber: true, renderers: new Map(), inject(r) { const id = this.renderers.size + 1; this.renderers.set(id, r); return id; },
    onScheduleFiberRoot() {}, onCommitFiberUnmount() {}, onPostCommitFiberRoot() {}, checkDCE() {},
    onCommitFiberRoot(id, root) { const t = performance.now(); const counts = {}; let total = 0;
      const walk = (f) => { while (f) { const a = f.alternate; const rendered = !a || a.memoizedProps !== f.memoizedProps || a.memoizedState !== f.memoizedState;
          if (rendered && (f.tag === 0 || f.tag === 1 || f.tag === 11 || f.tag === 14 || f.tag === 15)) { const n = nameOf(f.type) || 'anon'; counts[n] = (counts[n] || 0) + 1; total++; }
          if (f.child) walk(f.child); f = f.sibling; } };
      try { walk(root.current.child); } catch (e) {}
      P.commits.push({ t, total, counts }); if (P.commits.length > 4000) P.commits.splice(0, 2000); } };` : ""}
})();`;

async function newPage(browser, server, data, [width, height]) {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.evaluateOnNewDocument(INSTRUMENT(PROFILE));
  const cdp = await page.createCDPSession();
  await cdp.send("Performance.enable", { timeDomain: "timeTicks" });
  const seen = [];
  await cdp.send("Fetch.enable", { patterns: [{ urlPattern: `${server.origin}/api/*`, requestStage: "Request" }] });
  cdp.on("Fetch.requestPaused", (e) => {
    const pathname = new URL(e.request.url).pathname;
    const body = data.encoded[pathname];
    if (body) {
      seen.push(pathname);
      return void cdp.send("Fetch.fulfillRequest", { requestId: e.requestId, responseCode: 200,
        responseHeaders: [{ name: "Content-Type", value: "application/json" }], body }).catch(() => {});
    }
    if ((pathname.startsWith("/api/live/") && pathname !== "/api/live/kill/prepare") || pathname.startsWith("/api/connection/") ||
        pathname.startsWith("/api/accounts/") || pathname.startsWith("/api/schema-diagram/")) {
      seen.push(`refused ${pathname}`);
      return void cdp.send("Fetch.fulfillRequest", { requestId: e.requestId, responseCode: 400,
        responseHeaders: [{ name: "Content-Type", value: "application/json" }],
        body: Buffer.from(JSON.stringify({ error: "Not mocked in the measurement." })).toString("base64") }).catch(() => {});
    }
    cdp.send("Fetch.continueRequest", { requestId: e.requestId }).catch(() => {});
  });
  // A Mac window: Retina pixels (deviceScaleFactor 2), unless --dpr says otherwise.
  await page.setViewport({ width, height, deviceScaleFactor: DPR });
  if (CPU > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate: CPU });
  return { page, cdp, errors, seen, origin: server.origin, size: `${width}x${height}` };
}

async function metrics(cdp) {
  const { metrics: list } = await cdp.send("Performance.getMetrics");
  return Object.fromEntries(list.map((m) => [m.name, m.value]));
}
const metricDelta = (a, b) => ({
  task_ms: (b.TaskDuration - a.TaskDuration) * 1000, script_ms: (b.ScriptDuration - a.ScriptDuration) * 1000,
  layout_ms: (b.LayoutDuration - a.LayoutDuration) * 1000, style_ms: (b.RecalcStyleDuration - a.RecalcStyleDuration) * 1000,
  layouts: b.LayoutCount - a.LayoutCount, styles: b.RecalcStyleCount - a.RecalcStyleCount, nodes: b.Nodes,
});

async function heap(cdp) {
  await cdp.send("HeapProfiler.enable");
  for (let i = 0; i < 3; i++) await cdp.send("HeapProfiler.collectGarbage");
  const usage = await cdp.send("Runtime.getHeapUsage");
  const dom = await cdp.send("Memory.getDOMCounters");
  return { heap_mb: usage.usedSize / 1048576, nodes: dom.nodes, listeners: dom.jsEventListeners, documents: dom.documents };
}

function summarizeEvents(events) {
  // Interactions: the longest event of each interaction id (Event Timing,
  // input to next paint); events without an id (scroll, pointermove) apart.
  const byId = new Map();
  for (const [name, start, duration, ps, pe, id] of events) {
    if (!id) continue;
    const cur = byId.get(id);
    if (!cur || duration > cur.duration) byId.set(id, { name, duration, input_delay: ps - start, processing: pe - ps });
  }
  return [...byId.values()];
}

const profiles = [];
/** Measure one interaction: *act* performs real input; *ready* is a page
 * expression true once the result is on screen. */
async function interact(s, name, act, ready, { quiet = 250, max = 4000, needInput = true, timeout = 20000 } = {}) {
  const { page, cdp } = s;
  await page.evaluate(() => __perf.arm());
  const before = await metrics(cdp);
  if (PROFILE) { await cdp.send("Profiler.enable"); await cdp.send("Profiler.setSamplingInterval", { interval: 200 }); await cdp.send("Profiler.start"); }
  const waiter = page.evaluate(`__perf.until(() => ${needInput ? "__perf.inputs.length > 0 && " : ""}(${ready}), ${timeout})`);
  waiter.catch(() => {});
  try { await act(); } catch (e) {
    if (PROFILE) await cdp.send("Profiler.stop").catch(() => {});
    return { name, error: String(e.message || e).split("\n")[0].slice(0, 160) };
  }
  const readyAt = await waiter;
  const settled = await page.evaluate((q, m) => __perf.settle(q, m), quiet, max);
  const after = await metrics(cdp);
  let profile = null;
  if (PROFILE) { profile = (await cdp.send("Profiler.stop")).profile; }
  const w = await page.evaluate((readyAt, last) => {
    const P = __perf; const t0 = P.inputs.length ? P.inputs[0][1] : P.armT;
    const end = Math.max(readyAt, last);
    // API reads made by this interaction: when the last answer arrived, and its size.
    const api = performance.getEntriesByType("resource").filter((x) => x.initiatorType === "fetch" && x.startTime >= t0 - 1 && x.name.includes("/api/") && !/session\/save|privileges/.test(x.name));
    const data = api.length ? Math.max(...api.map((x) => x.responseEnd)) - t0 : null;
    return { t0, content: readyAt < 0 ? null : readyAt - t0, settled: end - t0, data, anims: P.anims || 0, api_kb: api.reduce((n, x) => n + (x.decodedBodySize || 0), 0) / 1024, ...P.window(t0, end + 1) };
  }, readyAt, settled.last);
  const interactions = summarizeEvents(w.events);
  const result = {
    name, content_ms: w.content, settled_ms: w.settled, settle_capped: settled.capped,
    data_ms: w.data, render_after_data_ms: w.data === null || w.content === null ? null : w.content - w.data, api_kb: w.api_kb || undefined,
    long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0), long_max_ms: Math.max(0, ...w.long.map(([, d]) => d)),
    inp_ms: interactions.length ? Math.max(...interactions.map((i) => i.duration)) : null,
    cls: w.shifts.reduce((n, [, v]) => n + v, 0), animations: w.anims, ...metricDelta(before, after),
  };
  if (w.content === null) result.timed_out = true;
  if (PROFILE) {
    result.commits = w.commits.length;
    result.rendered = topCounts(w.commits);
    result.top_functions = topFunctions(profile, 12);
    profiles.push({ name: `${s.size} ${name}`, profile });
  }
  return result;
}

function topCounts(commits) {
  const counts = {};
  for (const c of commits) for (const [k, v] of Object.entries(c.counts)) counts[k] = (counts[k] || 0) + v;
  return Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 15).map(([k, v]) => `${k}×${v}`).join(" ");
}

function topFunctions(profile, n) {
  // Self and inclusive time per function (url:line), from the sampled profile.
  const nodes = new Map(profile.nodes.map((x) => [x.id, x]));
  const parent = new Map();
  for (const x of profile.nodes) for (const c of x.children || []) parent.set(c, x.id);
  const dt = new Map();
  for (let i = 0; i < profile.samples.length; i++) dt.set(profile.samples[i], (dt.get(profile.samples[i]) || 0) + (profile.timeDeltas[i] || 0) / 1000);
  const self = new Map(), total = new Map();
  const key = (x) => {
    const f = x.callFrame;
    return `${f.functionName || "(anonymous)"} ${f.url ? path.basename(f.url) : ""}:${f.lineNumber + 1}`;
  };
  for (const [id, ms] of dt) {
    const x = nodes.get(id);
    if (!x) continue;
    const k = key(x);
    self.set(k, (self.get(k) || 0) + ms);
    const onStack = new Set();
    for (let cur = id; cur !== undefined; cur = parent.get(cur)) {
      const kk = key(nodes.get(cur));
      if (!onStack.has(kk)) { onStack.add(kk); total.set(kk, (total.get(kk) || 0) + ms); }
    }
  }
  const skip = /^\((program|idle|root|garbage collector)\)|^\(anonymous\) :0$/;
  const bySelf = [...self].filter(([k]) => !skip.test(k)).sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${v.toFixed(1)}ms ${k}`);
  const byTotal = [...total].filter(([k]) => !skip.test(k) && !/^(\(anonymous\)|performWorkUntilDeadline|workLoop|flushWork|renderRootSync|workLoopSync|performUnitOfWork|performSyncWorkOnRoot|flushSyncCallbacks|batchedUpdates|dispatchEvent|dispatchDiscreteEvent)/.test(k))
    .sort((a, b) => b[1] - a[1]).slice(0, n).map(([k, v]) => `${v.toFixed(1)}ms ${k}`);
  return { self: bySelf, total: byTotal };
}

function frameStats(times) {
  const d = [];
  for (let i = 1; i < times.length; i++) d.push(times[i] - times[i - 1]);
  if (!d.length) return { frames: 0 };
  const sorted = [...d].sort((a, b) => a - b);
  // The display's frame interval (headless Chrome here runs at 120 Hz, like a
  // ProMotion Mac): the median interval, at most 16.7 ms.
  const base = Math.min(16.7, Math.max(4, sorted[Math.floor(sorted.length / 2)]));
  const dropped = d.reduce((n, x) => n + Math.max(0, Math.round(x / base) - 1), 0);
  return {
    frames: d.length, hz: Math.round(1000 / base), dropped, dropped_pct: (100 * dropped) / (d.length + dropped),
    p50_ms: sorted[Math.floor(sorted.length * 0.5)], p95_ms: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))],
    max_ms: sorted[sorted.length - 1], janky: d.filter((x) => x > base * 1.5).length,
  };
}

async function scrollMeasure(s, name, selector, step = 120, frames = 90) {
  const { page, cdp } = s;
  const before = await metrics(cdp);
  if (PROFILE) { await cdp.send("Profiler.enable"); await cdp.send("Profiler.start"); }
  const r = await page.evaluate((sel, step, frames) => __perf.scroll(sel, step, frames), selector, step, frames);
  await pause(80);
  const after = await metrics(cdp);
  let profile = null;
  if (PROFILE) profile = (await cdp.send("Profiler.stop")).profile;
  if (!r) return { name, error: `no ${selector}` };
  const w = await page.evaluate((t0, t1) => __perf.window(t0, t1), r.t0, r.t1);
  const result = { name, ...frameStats(r.times), moved_px: r.moved, scroller: String(r.scroller).slice(0, 40),
    long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0), ...metricDelta(before, after) };
  if (PROFILE) { result.commits = w.commits.length; result.rendered = topCounts(w.commits); result.top_functions = topFunctions(profile, 10); profiles.push({ name: `${s.size} ${name}`, profile }); }
  return result;
}

/** Type *text* one key at a time into the focused field: per key, keydown to
 * the frame after its input event (and the Event Timing duration). */
async function typeMeasure(s, name, selector, text, { gap = 60 } = {}) {
  const { page, cdp } = s;
  await page.focus(selector);
  const before = await metrics(cdp);
  if (PROFILE) { await cdp.send("Profiler.enable"); await cdp.send("Profiler.start"); }
  const t0 = await page.evaluate(() => __perf.arm());
  const own = [];
  for (const ch of text) {
    await page.evaluate(() => { __perf.inputs.length = 0; });
    const waiter = page.evaluate(`__perf.until(() => __perf.inputs.some((i) => i[0] === 'input'), 5000)`);
    await page.keyboard.type(ch);
    const at = await waiter;
    const down = await page.evaluate(() => (__perf.inputs.find((i) => i[0] === "keydown") || [0, 0])[1]);
    if (at > 0 && down) own.push(at - down);
    await pause(gap);
  }
  const settled = await page.evaluate(() => __perf.settle(250, 4000));
  const after = await metrics(cdp);
  let profile = null;
  if (PROFILE) profile = (await cdp.send("Profiler.stop")).profile;
  const w = await page.evaluate((t0, t1) => __perf.window(t0, t1), t0, settled.last + 1);
  const inter = summarizeEvents(w.events).filter((i) => /key|input|beforeinput/.test(i.name));
  const sorted = [...own].sort((a, b) => a - b);
  const result = {
    name, keys: text.length, key_p50_ms: sorted[Math.floor(sorted.length / 2)], key_p95_ms: sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))],
    key_max_ms: sorted[sorted.length - 1], inp_ms: inter.length ? Math.max(...inter.map((i) => i.duration)) : null,
    slow_keys: inter.length, long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0),
    long_max_ms: Math.max(0, ...w.long.map(([, d]) => d)), ...metricDelta(before, after),
  };
  if (PROFILE) { result.commits = w.commits.length; result.rendered = topCounts(w.commits); result.top_functions = topFunctions(profile, 12); profiles.push({ name: `${s.size} ${name}`, profile }); }
  return result;
}

/** Main-thread work while nobody touches anything (timers, autosave). */
async function idleMeasure(s, name, ms) {
  const { page, cdp } = s;
  const before = await metrics(cdp);
  const t0 = await page.evaluate(() => performance.now());
  const times = await page.evaluate((ms) => __perf.frames(ms), ms);
  const after = await metrics(cdp);
  const w = await page.evaluate((t0) => __perf.window(t0, performance.now()), t0);
  const saves = await page.evaluate((t0) => performance.getEntriesByType("resource").filter((x) => x.startTime >= t0 && /session\/save/.test(x.name))
    .map((x) => ({ kb: Math.round((x.encodedBodySize || 0) / 1024), ms: Math.round(x.responseEnd - x.startTime) })), t0);
  return { name, ...frameStats(times), saves: saves.length, long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0),
    long_max_ms: Math.max(0, ...w.long.map(([, d]) => d)), ...metricDelta(before, after) };
}

// ------------------------------------------------------------- navigation ---

const navButton = (place) => `.main-nav button[data-place="${place}"]`;
const PLACE_READY = {
  home: `!!__perf.vis('section.home .home-start, section.home .recent-work')`,
  server: `__perf.anyView() || !!__perf.vis('.server-layout.show-list .source-list')`,
  workload: `__perf.anyView() || !!__perf.vis('.server-layout.show-list .source-list')`,
  replication: `__perf.view('replication')`,
  cluster: `__perf.view('cluster')`,
  proxysql: `__perf.view('proxysql')`,
  tuning: `__perf.view('tuning')`,
  query: `!!__perf.vis('textarea.sql-editor')`,
  plans: `!!__perf.vis('.plans-empty, .plan-chart svg, .compare-empty, .chart-frame')`,
  investigations: `!!__perf.vis('section.library') && !__perf.busy('section.library')`,
  learn: `!!__perf.vis('.teach-catalog') && !__perf.busy('.teach-catalog')`,
};

/** Bring a place on screen; at narrow widths the places may sit in a drawer. */
async function clickPlace(page, place) {
  const sel = navButton(place);
  const shown = await page.$eval(sel, (e) => !!e.getClientRects().length && !e.closest("[hidden]")).catch(() => false);
  if (!shown) {
    const toggle = await page.$('button[aria-controls="workspace-sidebar"], .sidebar-toggle');
    if (toggle) { await toggle.click(); await pause(200); }
  }
  await page.click(sel);
}
async function goPlace(s, place, label) {
  return interact(s, label || `place ${place}`, () => clickPlace(s.page, place), PLACE_READY[place]);
}

async function openViewList(s, measure = false) {
  const { page } = s;
  if (await page.$(".view-switcher-popover") || await page.evaluate(() => !!__perf.vis(".server-layout.show-list .source-list"))) return null;
  // A narrow window (720 px and below) pushes the list as its own screen: Back opens it.
  await page.waitForFunction(() => !!__perf.vis(".server-view .server-back, .server-view .view-switcher"), { timeout: 10000 });
  const pushed = await page.evaluate(() => !!__perf.vis(".server-view .server-back"));
  const act = async () => {
    const handle = await page.evaluateHandle((sel) =>
      [...document.querySelectorAll(sel)].find((e) => !e.closest("[hidden]") && !!e.getClientRects().length) || null,
      pushed ? ".server-view .server-back" : ".server-view .view-switcher");
    try {
      const button = handle.asElement();
      if (button) { await button.click(); return; }
    } finally { await handle.dispose(); }
    await page.screenshot({ path: path.join(OUT, `no-switcher-${s.size}.png`) });
    const why = await page.evaluate(() => [...document.querySelectorAll(".server-back, .view-switcher")].map((e) => `${e.className} hidden=${!!e.closest("[hidden]")} rects=${e.getClientRects().length} view=${e.closest(".server-view")?.dataset.view}`).join("; "));
    throw new Error(`no view switcher on screen (pushed=${pushed}): ${why}`);
  };
  const ready = pushed ? `!!__perf.vis('.server-layout .source-list [aria-current=page], .server-layout .source-list .source-item')` : `!!__perf.vis('.view-switcher-popover .source-list [aria-current=page]')`;
  if (!measure) { await act(); await page.waitForFunction(ready); return null; }
  return interact(s, pushed ? "views list open (Back, narrow)" : "views list open", act, ready);
}

/** Open a Server or Workload view the way a person does and time the choice. */
async function goView(s, kind, label) {
  const { page } = s;
  const place = WORKLOAD_VIEWS.includes(kind) ? "workload" : "server";
  if (!(await page.$(`${navButton(place)}.active`))) { await clickPlace(page, place); await page.waitForFunction(() => !!__perf.vis(".server-view, .server-layout .source-list")); }
  const docked = await page.$eval(`#server-nav-${kind}`, (e) => !e.closest("[hidden]") && !!e.getClientRects().length).catch(() => false);
  if (!docked) await openViewList(s);
  return interact(s, label || `view ${kind}`, () => page.click(`#server-nav-${kind}`), `__perf.view('${kind}')`);
}

async function bootstrap(browser, server, data, size) {
  const s = await newPage(browser, server, data, size);
  await s.page.goto(server.url, { waitUntil: "networkidle0" });
  await s.page.waitForSelector(".home-start", { timeout: 20000 });
  await s.page.waitForFunction(() => !document.body.innerText.includes("Opening your workspace"));
  return s;
}

const launchBrowser = () => puppeteer.launch({ headless: "shell", args: [
  "--no-sandbox", "--disable-setuid-sandbox",
  "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
  "--enable-precise-memory-info"] });

// -------------------------------------------------------------- scenarios ---

async function coldLoad(data, size) {
  const server = await startServer();
  const browser = await launchBrowser();
  try {
    const s = await newPage(browser, server, data, size);
    const { page, cdp } = s;
    const navStart = Date.now();
    await page.goto(server.url, { waitUntil: "load" });
    const loadAt = Date.now() - navStart;
    const readyAt = await page.evaluate(`__perf.until(() => !!__perf.vis('.home-start') && !document.body.innerText.includes('Opening your workspace'), 20000)`);
    await pause(1500); // quiet window for the long-task based interactive time
    const r = await page.evaluate((readyAt) => {
      const nav = performance.getEntriesByType("navigation")[0];
      const paints = Object.fromEntries(performance.getEntriesByType("paint").map((p) => [p.name, p.startTime]));
      const lcp = performance.getEntriesByType("largest-contentful-paint").at(-1);
      const res = performance.getEntriesByType("resource");
      const js = res.filter((x) => /\.js$/.test(x.name)), css = res.filter((x) => /\.css$/.test(x.name));
      const P = __perf; const long = P.long.filter(([s0]) => s0 < readyAt + 1500);
      const lastLong = long.reduce((m, [s0, d]) => Math.max(m, s0 + d), 0);
      return {
        dcl_ms: nav.domContentLoadedEventEnd, load_ms: nav.loadEventEnd, fcp_ms: paints["first-contentful-paint"] ?? null,
        lcp_ms: lcp ? lcp.startTime : null, home_ready_ms: readyAt, interactive_ms: Math.max(readyAt, lastLong),
        long_tasks: long.length, long_ms: long.reduce((n, [, d]) => n + d, 0), long_max_ms: Math.max(0, ...long.map(([, d]) => d)),
        cls: P.shifts.reduce((n, [, v]) => n + v, 0), js_files: js.length, js_kb: js.reduce((n, x) => n + (x.decodedBodySize || 0), 0) / 1024,
        css_kb: css.reduce((n, x) => n + (x.decodedBodySize || 0), 0) / 1024,
        api_before_ready: res.filter((x) => x.name.includes("/api/") && x.startTime < readyAt).map((x) => new URL(x.name).pathname + " " + Math.round(x.responseEnd - x.startTime) + "ms"),
      };
    }, readyAt);
    const m = await metrics(cdp);
    return { name: "cold load", ...r, wall_load_ms: loadAt, heap_mb: m.JSHeapUsedSize / 1048576, nodes: m.Nodes };
  } finally { await browser.close(); await server.close(); }
}

async function placesScenario(s) {
  const out = [];
  // First visit (chunk load and mount and first read), then a warm visit.
  for (const place of PLACES.slice(1)) out.push(await goPlace(s, place, `place ${place} (first)`));
  await goPlace(s, "home");
  for (const place of PLACES.slice(1)) out.push(await goPlace(s, place, `place ${place} (warm)`));
  await goPlace(s, "home");
  out.push(await goPlace(s, "home", "place home (warm)"));
  return out;
}

async function viewsScenario(s) {
  const out = [];
  for (const kind of SERVER_VIEWS) out.push(await goView(s, kind, `view ${kind} (first)`));
  for (const kind of WORKLOAD_VIEWS) out.push(await goView(s, kind, `view ${kind} (first)`));
  for (const kind of SERVER_VIEWS) out.push(await goView(s, kind, `view ${kind} (warm)`));
  for (const kind of WORKLOAD_VIEWS) out.push(await goView(s, kind, `view ${kind} (warm)`));
  return out;
}

const viewSel = (kind) => `.server-view[data-view="${kind}"]`;
async function firstSortable(page, scope) {
  return page.evaluate((scope) => {
    const ths = [...document.querySelectorAll(`${scope} thead th[aria-sort="none"]`)].filter((e) => !e.closest("[hidden]") && e.getClientRects().length);
    const th = ths.find((e) => e.classList.contains("num")) || ths[0];
    return th ? th.dataset.col : null;
  }, scope);
}

async function tableScenario(s, { kind, label, filterLabel, filterText, table, list, loadButton }) {
  const { page } = s;
  const scope = table ? `${viewSel(kind)} [data-report-table="${table}"]` : viewSel(kind);
  const out = [];
  if (loadButton) {
    // A view that waits for its Load button: the first render is from that click.
    await goView(s, kind);
    out.push({ ...(await interact(s, `${label}: first render`, () => page.click(`${viewSel(kind)} ${loadButton}`),
      `!!__perf.vis('${scope} tbody tr[data-row-key]') && !__perf.busy('${viewSel(kind)}')`)),
    rows_drawn: await page.$$eval(`${scope} tbody tr`, (r) => r.length).catch(() => 0) });
  } else out.push({ ...(await goView(s, kind, `${label}: first render`)), rows_drawn: await page.$$eval(`${scope} tbody tr`, (r) => r.length).catch(() => 0) });
  if (list) {
    const sel = `${viewSel(kind)} .segmented button[data-value="${list}"]`;
    if (await page.$(sel)) await step(out, "step", async () => interact(s, `${label}: switch list`, () => page.click(sel), `!!__perf.vis('${scope} tbody tr')`));
  }
  const col = await firstSortable(page, scope);
  if (col) {
    const th = `${scope} thead th[data-col="${col}"] button`;
    out.push({ ...(await interact(s, `${label}: sort`, () => page.click(th),
      `document.querySelector('${scope} thead th[data-col="${col}"]')?.getAttribute('aria-sort') !== 'none'`)), column: col });
  }
  if (await page.$(`${scope} tbody tr`)) {
    const scrollSel = `${scope} tbody tr`;
    await step(out, "step", async () => scrollMeasure(s, `${label}: scroll`, scrollSel));
  }
  const row = `${scope} tbody tr[data-row-key]:nth-child(5)`;
  if (await page.$(row)) {
    await page.$eval(row, (e) => e.scrollIntoView({ block: "center" }));
    await pause(100);
    await step(out, "step", async () => interact(s, `${label}: select row`, () => page.click(`${row} td:nth-child(2)`),
      `!!__perf.vis('.view-inspector .inspector-title, .inspector-sheet .inspector-title')`));
    await page.keyboard.press("Escape");
    await pause(200);
  }
  const filter = `${viewSel(kind)} input[aria-label="${filterLabel}"]`;
  if (filterLabel && (await page.$(filter))) {
    await step(out, "step", async () => typeMeasure(s, `${label}: type in filter`, filter, filterText));
    // Clear the filter (Select all and Delete) as one measured edit.
    await page.focus(filter);
    await page.keyboard.down(process.platform === "darwin" ? "Meta" : "Control");
    await page.keyboard.press("a");
    await page.keyboard.up(process.platform === "darwin" ? "Meta" : "Control");
    await step(out, "step", async () => interact(s, `${label}: clear filter`, () => page.keyboard.press("Backspace"), "true"));
  }
  return out;
}

async function tablesScenario(s) {
  const out = [];
  await step(out, "table", () => tableScenario(s, { kind: "sessions", label: "Processlist 2,000", filterLabel: "Filter sessions", filterText: "report" }));
  await step(out, "table", () => tableScenario(s, { kind: "top", label: "Top statements 1,000", filterLabel: "Filter statements", filterText: "orders" }));  // Top statements reads when opened
  await step(out, "table", () => tableScenario(s, { kind: "config", label: "Parameters all", filterLabel: "Filter parameters", filterText: "innodb" }));
  await step(out, "table", () => tableScenario(s, { kind: "tables", label: "Tables 10,000", filterLabel: "Filter tables", filterText: "table_01", table: "tables.inventory" }));
  await step(out, "table", () => tableScenario(s, { kind: "indexes", label: "Indexes 10,000", filterLabel: "Filter indexes", filterText: "table_01", table: "indexes.inventory" }));
  // Ten pages of 200 rows: what a page change costs on the 10,000-row inventory.
  const size = `${viewSel("tables")} select[aria-label^="Rows per page"]`;
  await goView(s, "tables");
  if (await s.page.$(size)) {
    await step(out, "step", async () => interact(s, "Tables 10,000: 200 rows per page", () => s.page.select(size, "200"),
      `document.querySelectorAll('${viewSel("tables")} [data-report-table="tables.inventory"] tbody tr').length >= 200`, { needInput: false }));
    await step(out, "step", async () => scrollMeasure(s, "Tables 10,000: scroll 200 rows", `${viewSel("tables")} [data-report-table="tables.inventory"] tbody tr`));
    const next = `${viewSel("tables")} .report-table-pages button:nth-last-child(2)`;
    if (await s.page.$(next)) await step(out, "step", async () => interact(s, "Tables 10,000: next page", () => s.page.click(next), "true"));
  }
  // With every large report loaded and kept: what a place switch costs now.
  await step(out, "step", async () => goPlace(s, "home", "place home (large reports loaded)"));
  await step(out, "step", async () => goPlace(s, "server", "place server (large reports loaded)"));
  await step(out, "step", async () => goPlace(s, "query", "place query (large reports loaded)"));
  await step(out, "step", async () => goPlace(s, "workload", "place workload (large reports loaded)"));
  await step(out, "step", async () => goView(s, "sessions", "view sessions (large reports loaded)"));
  await step(out, "step", async () => goView(s, "tables", "view tables (large reports loaded)"));
  return out;
}

async function overlaysScenario(s) {
  const { page } = s;
  const out = [];
  const meta = process.platform === "darwin" ? "Meta" : "Control";
  const escape = async () => { await page.keyboard.press("Escape"); await pause(150); };
  await goPlace(s, "home");
  await step(out, "command palette open (click)", () => interact(s, "command palette open (click)", () => page.click(".command-trigger"), `!!document.querySelector('dialog.command-palette[open]')`));
  await step(out, "command palette filter (type q)", () => interact(s, "command palette filter (type q)", () => page.keyboard.type("q"), "true"));
  await step(out, "command palette close (Escape)", () => interact(s, "command palette close (Escape)", () => page.keyboard.press("Escape"), `!document.querySelector('dialog.command-palette[open]')`));
  await step(out, "command palette open (⌘K)", () => interact(s, "command palette open (⌘K)", async () => { await page.keyboard.down(meta); await page.keyboard.press("k"); await page.keyboard.up(meta); },
    `!!document.querySelector('dialog.command-palette[open]')`));
  await escape();
  await step(out, "settings menu open", () => interact(s, "settings menu open", () => page.click(".settings-button"), `!!__perf.vis('.settings-popover[role="dialog"]')`));
  await escape();
  await goView(s, "sessions");
  await step(out, "views list open", () => openViewList(s, true));
  await step(out, "views list close", async () => {
    // Escape closes the floating list; the pushed list (narrow) goes back by choosing the view again.
    if (await page.$(".view-switcher-popover")) return interact(s, "views list close (Escape)", () => page.keyboard.press("Escape"), `!document.querySelector('.view-switcher-popover')`);
    return interact(s, "views list choose (narrow)", () => page.click("#server-nav-sessions"), `__perf.view('sessions')`);
  });
  const info = `${viewSel("sessions")} button[aria-label="About Processlist"]`;
  await step(out, "info popover open", async () => (await page.$(info)) && interact(s, "info popover open", () => page.click(info), `!!__perf.vis('[role=dialog][aria-label="About Processlist"]')`));
  await step(out, "info popover close", () => interact(s, "info popover close", () => page.keyboard.press("Escape"), `!__perf.vis('[role=dialog][aria-label="About Processlist"]')`));
  await step(out, "sheet open (Kill sessions…)", async () => {
    const hasKill = await page.evaluate((v) => [...document.querySelectorAll(`${v} button`)].some((b) => /Kill sessions/.test(b.textContent) && b.getClientRects().length), viewSel("sessions"));
    if (!hasKill) return { name: "sheet open (Kill sessions…)", error: "no Kill sessions… button on screen" };
    const r = await interact(s, "sheet open (Kill sessions…)", () => page.click(`${viewSel("sessions")} button[data-action="kill-sessions"]`), `!!__perf.vis('dialog[open], [role=dialog][aria-modal=true]')`);
    const c = await interact(s, "sheet close (Escape)", () => page.keyboard.press("Escape"), `!__perf.vis('dialog[open], [role=dialog][aria-modal=true]')`);
    return [r, c];
  });
  await escape();
  // The inspector: a pane at 1440 px, a bottom sheet below 1040 px.
  await step(out, "inspector open (row)", async () => {
    const row = `${viewSel("sessions")} tbody tr[data-row-key]:nth-child(3) td:nth-child(2)`;
    const r = await interact(s, "inspector open (select row)", () => page.click(row), `!!__perf.vis('.view-inspector .inspector-title')`);
    const c = await interact(s, "inspector close (Escape)", () => page.keyboard.press("Escape"), `!__perf.vis('.view-inspector .inspector-title')`);
    return [r, c];
  });
  await goPlace(s, "plans");
  await step(out, "import dialog", async () => {
    const hasImport = await page.evaluate(() => [...document.querySelectorAll("button")].some((b) => b.textContent.trim() === "Import plan" && b.getClientRects().length));
    if (!hasImport) return { name: "import dialog open", error: "no Import plan button on screen" };
    const r = await interact(s, "import dialog open", () => page.click(".plans-import"), `!!document.querySelector('dialog[open]')`);
    const c = await interact(s, "import dialog close", () => page.keyboard.press("Escape"), `!document.querySelector('dialog[open]')`);
    return [r, c];
  });
  return out;
}

function script(lines) {
  const out = [];
  for (let i = 0; i < lines; i++) {
    const k = i % 5;
    out.push(k === 0 ? `SELECT o.id, o.total, c.name FROM orders o JOIN customers c ON c.id = o.customer_id WHERE o.status = 'open' AND o.id > ${i};`
      : k === 1 ? `-- step ${i}: refresh the summary for 'region_${i % 17}'`
      : k === 2 ? `UPDATE inventory SET qty = qty - 1 WHERE sku = 'SKU-${i}' AND qty > 0;`
      : k === 3 ? `INSERT INTO audit_log (id, note, created_at) VALUES (${i}, "line ${i}", NOW());`
      : `  /* block ${i} */ SELECT COUNT(*) FROM order_items WHERE order_id BETWEEN ${i} AND ${i + 100};`);
  }
  return out.join("\n");
}

async function editorScenario(s, bigPlanFile) {
  const { page } = s;
  const out = [];
  out.push(await goPlace(s, "query"));
  const text = script(5000);
  // A real paste (⌘V from the clipboard), the way a script arrives. Chrome's
  // execCommand("insertText") of the same text takes seconds even in a bare
  // textarea, so it would measure Chrome, not the editor.
  await page.browserContext().overridePermissions(s.origin, ["clipboard-read", "clipboard-write", "clipboard-sanitized-write"]);
  await page.focus("textarea.sql-editor");
  const copied = await page.evaluate((text) => navigator.clipboard.writeText(text).then(() => true, (e) => String(e)), text);
  if (copied !== true) out.push({ name: "editor paste 5,000 lines", error: `clipboard: ${copied}` });
  else out.push({ ...(await interact(s, "editor paste 5,000 lines", async () => {
    await page.keyboard.down("Meta");
    await page.keyboard.press("KeyV", { commands: ["selectAll", "paste"] });
    await page.keyboard.up("Meta");
  }, `document.querySelector('textarea.sql-editor').value.length > 300000`, { max: 8000, timeout: 60000 })), chars: text.length });
  // Type at the end, then in the middle (the highlight layers redraw what follows).
  await page.evaluate(() => { const t = document.querySelector("textarea.sql-editor"); t.focus(); t.setSelectionRange(t.value.length, t.value.length); });
  out.push(await typeMeasure(s, "editor type at end (5,000 lines)", "textarea.sql-editor", "\nSELECT 1 FROM dual;"));
  await page.evaluate(() => { const t = document.querySelector("textarea.sql-editor"); const at = Math.floor(t.value.length / 2); t.focus(); t.setSelectionRange(at, at); });
  out.push(await typeMeasure(s, "editor type in middle (5,000 lines)", "textarea.sql-editor", " AND x = 1"));
  out.push(await scrollMeasure(s, "editor scroll (5,000 lines)", "textarea.sql-editor", 200, 90));
  // Switching away and back with the big script open (the session snapshot, App re-render).
  out.push(await goPlace(s, "home", "place home (5,000-line script open)"));
  out.push(await goPlace(s, "query", "place query (5,000-line script open)"));
  // A small script with a large plan open: what each keystroke costs the rest of the workspace.
  await page.evaluate(() => { const t = document.querySelector("textarea.sql-editor"); t.focus(); t.select(); });
  await page.evaluate(() => document.execCommand("insertText", false, "SELECT 1;"));
  await pause(1500);
  out.push(await typeMeasure(s, "editor type, short script", "textarea.sql-editor", " -- note"));
  await goPlace(s, "plans");
  const importBtn = await page.evaluateHandle(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Import plan" && b.getClientRects().length));
  await importBtn.asElement().click();
  await page.waitForSelector("dialog[open] input[type=file]");
  await (await page.$("dialog[open] input[type=file]")).uploadFile(bigPlanFile);
  await page.waitForFunction(() => !document.querySelector("dialog[open]") && !!__perf.vis(".plan-chart svg, .chart-frame svg"), { timeout: 20000 });
  await pause(1500);
  await goPlace(s, "query");
  await page.evaluate(() => { const t = document.querySelector("textarea.sql-editor"); t.focus(); t.setSelectionRange(t.value.length, t.value.length); });
  out.push(await typeMeasure(s, "editor type, short script, 1,000-operator plan open", "textarea.sql-editor", " -- note"));
  // The autosave a second after the last key: the main-thread work it costs.
  out.push(await idleMeasure(s, "autosave after an edit (5,000-line tab, 1,000-operator plan)", 2500));
  return out;
}

async function chartsScenario(s, planFile, tag) {
  const { page } = s;
  const out = [];
  await goPlace(s, "plans");
  const importBtn = await page.evaluateHandle(() => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === "Import plan" && b.getClientRects().length));
  await importBtn.asElement().click();
  await page.waitForSelector("dialog[open] input[type=file]");
  const input = await page.$("dialog[open] input[type=file]");
  out.push(await interact(s, `plan import and first chart (${tag})`, () => input.uploadFile(planFile),
    `!document.querySelector('dialog[open]') && !!__perf.vis('.plan-chart svg, .chart-frame svg, .plan-chart canvas')`, { needInput: false, max: 6000 }));
  const views = [["flamegraph", "flame graph"], ["bargraph", "bar chart"], ["tree", "execution tree"], ["workbench", "diagram (Visual Explain)"]];
  for (const [value, label] of views) {
    const select = await page.$('select[aria-label="Chart view"]');
    if (!select) { out.push({ name: `chart ${label} (${tag})`, error: "no Chart view select" }); continue; }
    await page.evaluate(() => { window.__oldChart = __perf.vis(".plan-chart svg, .chart-frame svg"); });
    out.push(await interact(s, `chart ${label} (${tag}): render`, () => page.select('select[aria-label="Chart view"]', value),
      `(() => { const svg = __perf.vis('.plan-chart[data-view="${value}"] svg, .chart-frame svg'); return !!svg && svg !== window.__oldChart && !__perf.busy('main'); })()`,
      { max: 4000, timeout: 8000 }));
    // Hover: move the pointer across the chart in 30 steps; frame times and the tooltip.
    const box = await page.evaluate(() => { const e = __perf.vis('.plan-chart svg, .chart-frame svg'); if (!e) return null; const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: Math.min(r.height, innerHeight - r.y - 40) }; });
    if (box && box.w > 0 && box.h > 0) {
      const t0 = await page.evaluate(() => __perf.arm());
      const before = await metrics(s.cdp);
      const frames = page.evaluate(() => __perf.frames(1400));
      for (let i = 0; i < 30; i++) {
        await page.mouse.move(box.x + box.w * (0.1 + 0.8 * (i / 29)), box.y + box.h * (0.25 + 0.5 * ((i * 7) % 10) / 10));
        await pause(30);
      }
      const times = await frames;
      const after = await metrics(s.cdp);
      const w = await page.evaluate((t0) => __perf.window(t0, performance.now()), t0);
      const tip = await page.evaluate(() => !!__perf.vis('.chart-tooltip, .tooltip, [role=tooltip]'));
      out.push({ name: `chart ${label} (${tag}): hover`, ...frameStats(times), tooltip_seen: tip, long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0), ...metricDelta(before, after) });
      await page.mouse.move(5, 5);
    }
    // Zoom: the chart's own zoom in button when it has one, then wheel zoom over the chart.
    const zoomIn = await page.evaluateHandle(() => [...document.querySelectorAll('[data-action="in"], button[aria-label^="Zoom in"]')].find((e) => e.getClientRects().length && !e.disabled));
    if (zoomIn.asElement()) out.push(await interact(s, `chart ${label} (${tag}): zoom in (button)`, () => zoomIn.asElement().click(), "true", { max: 2000 }));
    if (box && box.w > 0) {
      await page.mouse.move(box.x + box.w / 2, box.y + box.h / 2);
      const t0 = await page.evaluate(() => __perf.arm());
      const frames = page.evaluate(() => __perf.frames(1200));
      for (let i = 0; i < 12; i++) { await page.mouse.wheel({ deltaY: -60 }); await pause(16); }
      const times = await frames;
      const w = await page.evaluate((t0) => __perf.window(t0, performance.now()), t0);
      out.push({ name: `chart ${label} (${tag}): wheel`, ...frameStats(times), long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0) });
    }
    const fit = await page.evaluateHandle(() => [...document.querySelectorAll('[data-action="fit"], [data-action="reset"]')].find((e) => e.getClientRects().length));
    if (fit.asElement()) { await fit.asElement().click(); await pause(300); }
  }
  return out;
}

/** Main-thread work while the window sits untouched on each screen (a Mac
 * app idles near zero: timers, polling and re-renders cost battery). */
async function idleScenario(s) {
  const out = [];
  for (const place of PLACES) {
    await clickPlace(s.page, place);
    await s.page.waitForFunction(PLACE_READY[place], { timeout: 20000 }).catch(() => {});
    await pause(2500); // past the autosave of the navigation
    out.push(await idleMeasure(s, `idle 5 s: ${place}`, 5000));
  }
  await goView(s, "sessions");
  await pause(2500);
  out.push(await idleMeasure(s, "idle 5 s: Processlist 2,000 loaded", 5000));
  return out;
}

/** Drag the window edge: 24 widths from 1440 to 1000 px and back, one per
 * frame, on Processlist with 2,000 sessions (every view kept mounted). */
async function resizeScenario(s, [width, height]) {
  const out = [];
  await goView(s, "sessions");
  await pause(1500);
  const { page, cdp } = s;
  for (const [label, from, to] of [["Processlist 2,000", width, Math.max(720, width - 440)]]) {
    const before = await metrics(cdp);
    const t0 = await page.evaluate(() => performance.now());
    const steps = [];
    for (let i = 0; i <= 24; i++) {
      const w = Math.round(from + ((to - from) * (i <= 12 ? i : 24 - i)) / 12);
      const s0 = Date.now();
      await page.setViewport({ width: w, height, deviceScaleFactor: DPR });
      await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => setTimeout(r, 0))));
      steps.push(Date.now() - s0);
    }
    const after = await metrics(cdp);
    const w = await page.evaluate((t0) => __perf.window(t0, performance.now()), t0);
    const sorted = [...steps].sort((a, b) => a - b);
    out.push({ name: `window resize: ${label}`, steps: steps.length, step_p50_ms: sorted[Math.floor(sorted.length / 2)], step_max_ms: sorted.at(-1),
      long_tasks: w.long.length, long_ms: w.long.reduce((n, [, d]) => n + d, 0), long_max_ms: Math.max(0, ...w.long.map(([, d]) => d)), ...metricDelta(before, after) });
  }
  await page.setViewport({ width, height, deviceScaleFactor: DPR });
  return out;
}

async function memoryScenario(s) {
  const out = [];
  const series = [];
  series.push({ cycle: 0, ...(await heap(s.cdp)) });
  for (let c = 1; c <= MEMORY_CYCLES; c++) {
    for (const place of [...PLACES.slice(1), "home"]) {
      await clickPlace(s.page, place);
      await s.page.waitForFunction(PLACE_READY[place], { timeout: 20000 }).catch(() => {});
      await pause(60);
    }
    if (c === 1 || c === 5 || c === MEMORY_CYCLES) series.push({ cycle: c, ...(await heap(s.cdp)) });
  }
  const first = series.find((x) => x.cycle === 1), last = series.at(-1);
  out.push({ name: `memory: every place ×${MEMORY_CYCLES}`, series,
    heap_growth_mb: last.heap_mb - first.heap_mb, node_growth: last.nodes - first.nodes, listener_growth: last.listeners - first.listeners,
    heap_after_mb: last.heap_mb, nodes_after: last.nodes, listeners_after: last.listeners });
  // Server and Workload views, every one ×10.
  const vseries = [{ cycle: 0, ...(await heap(s.cdp)) }];
  for (let c = 1; c <= MEMORY_CYCLES; c++) {
    for (const kind of [...SERVER_VIEWS, ...WORKLOAD_VIEWS]) {
      const place = WORKLOAD_VIEWS.includes(kind) ? "workload" : "server";
      const active = await s.page.$eval(`${navButton(place)}.active`, () => true).catch(() => false);
      if (!active) await clickPlace(s.page, place);
      await s.page.waitForFunction(() => !!__perf.vis(".server-view .view-switcher, .server-view .source-list, .server-layout .source-list"), { timeout: 10000 }).catch(() => {});
      const docked = await s.page.$eval(`#server-nav-${kind}`, (e) => !e.closest("[hidden]") && !!e.getClientRects().length).catch(() => false);
      if (!docked) await openViewList(s).catch(() => {});
      await s.page.click(`#server-nav-${kind}`).catch(() => {});
      await s.page.waitForFunction((k) => __perf.view(k), { timeout: 10000 }, kind).catch(() => {});
    }
    if (c === 1 || c === 5 || c === MEMORY_CYCLES) vseries.push({ cycle: c, ...(await heap(s.cdp)) });
  }
  const vf = vseries.find((x) => x.cycle === 1), vl = vseries.at(-1);
  out.push({ name: `memory: every Server and Workload view ×${MEMORY_CYCLES}`, series: vseries,
    heap_growth_mb: vl.heap_mb - vf.heap_mb, node_growth: vl.nodes - vf.nodes, listener_growth: vl.listeners - vf.listeners,
    heap_after_mb: vl.heap_mb, nodes_after: vl.nodes, listeners_after: vl.listeners });
  return out;
}

/** --only probe: open every place and view, wait, and record what is on
 * screen (classes, rows, status lines, the API reads) with a screenshot, to
 * check the readiness expressions against the real screens. */
async function probeScenario(s) {
  const out = [];
  const dir = path.join(OUT, "probe", s.size);
  await fs.mkdir(dir, { recursive: true });
  const snap = async (name, sel) => {
    await pause(1500);
    const info = await s.page.evaluate((sel) => {
      const r = __perf.vis(sel);
      if (!r) return { missing: sel };
      const classes = {};
      for (const e of r.querySelectorAll("*")) if (!e.closest("[hidden]")) for (const c of e.classList) classes[c] = (classes[c] || 0) + 1;
      return { rows: r.querySelectorAll("tbody tr").length, status: [...r.querySelectorAll("[role=status], [role=alert]")].filter((e) => e.getClientRects().length).map((e) => e.textContent.trim().slice(0, 80)),
        classes: Object.entries(classes).sort((a, b) => b[1] - a[1]).slice(0, 50).map(([k, v]) => `${k}:${v}`).join(" ") };
    }, sel);
    await s.page.screenshot({ path: path.join(dir, `${name}.png`) });
    out.push({ name: `probe ${name}`, ...info, api: s.seen.splice(0).join(" ") });
  };
  for (const place of PLACES.slice(1)) {
    await clickPlace(s.page, place);
    await snap(`place-${place}`, "main");
  }
  for (const kind of [...SERVER_VIEWS, ...WORKLOAD_VIEWS]) {
    const place = WORKLOAD_VIEWS.includes(kind) ? "workload" : "server";
    if (!(await s.page.$(`${navButton(place)}.active`))) { await clickPlace(s.page, place); await pause(800); }
    const docked = await s.page.$eval(`#server-nav-${kind}`, (e) => !e.closest("[hidden]") && !!e.getClientRects().length).catch(() => false);
    if (!docked) await openViewList(s).catch((e) => out.push({ name: `probe ${kind} list`, error: String(e) }));
    await s.page.click(`#server-nav-${kind}`).catch((e) => out.push({ name: `probe ${kind} click`, error: String(e) }));
    await snap(`view-${kind}`, viewSel(kind));
  }
  return out;
}

/** --only leak: visit every Server and Workload view *cycles* times, take a
 * heap snapshot, and report the detached DOM nodes still alive with the
 * retaining path of the most common ones (what keeps them). */
async function leakScenario(s) {
  const { page, cdp } = s;
  const visit = async () => {
    for (const kind of [...SERVER_VIEWS, ...WORKLOAD_VIEWS]) {
      const place = WORKLOAD_VIEWS.includes(kind) ? "workload" : "server";
      if (!(await page.$(`${navButton(place)}.active`))) await clickPlace(page, place);
      await page.waitForFunction(() => !!__perf.vis(".server-view .view-switcher, .server-view .server-back, .server-layout .source-list"), { timeout: 10000 }).catch(() => {});
      const docked = await page.$eval(`#server-nav-${kind}`, (e) => !e.closest("[hidden]") && !!e.getClientRects().length).catch(() => false);
      if (!docked) await openViewList(s).catch(() => {});
      await page.click(`#server-nav-${kind}`).catch(() => {});
      await page.waitForFunction((k) => __perf.view(k), { timeout: 10000 }, kind).catch(() => {});
    }
  };
  const cycles = Math.max(2, Math.min(MEMORY_CYCLES, 6));
  for (let i = 0; i < cycles; i++) await visit();
  await clickPlace(page, "home");
  await pause(500);
  await heap(cdp);
  const chunks = [];
  const onChunk = (e) => chunks.push(e.chunk);
  cdp.on("HeapProfiler.addHeapSnapshotChunk", onChunk);
  await cdp.send("HeapProfiler.takeHeapSnapshot", { reportProgress: false, captureNumericValue: false });
  cdp.off("HeapProfiler.addHeapSnapshotChunk", onChunk);
  const file = path.join(OUT, `heap-${s.size}.heapsnapshot`);
  const text = chunks.join("");
  await fs.writeFile(file, text);
  return [{ name: `leak: detached DOM after ${cycles} view cycles`, ...detachedReport(JSON.parse(text)), snapshot: file }];
}

function detachedReport(snap) {
  const m = snap.snapshot.meta;
  const nf = m.node_fields, ef = m.edge_fields, N = nf.length, E = ef.length;
  const nodeTypes = m.node_types[0], edgeTypes = m.edge_types[0];
  const nodes = snap.nodes, edges = snap.edges, strings = snap.strings;
  const iType = nf.indexOf("type"), iName = nf.indexOf("name"), iEdges = nf.indexOf("edge_count"), iDet = nf.indexOf("detachedness");
  const eType = ef.indexOf("type"), eName = ef.indexOf("name_or_index"), eTo = ef.indexOf("to_node");
  const count = nodes.length / N;
  const first = new Uint32Array(count + 1);
  for (let i = 0, e = 0; i < count; i++) { first[i] = e; e += nodes[i * N + iEdges] * E; first[i + 1] = e; }
  const name = (i) => strings[nodes[i * N + iName]];
  const type = (i) => nodeTypes[nodes[i * N + iType]];
  // Strong edges only (weak edges do not retain).
  const weak = edgeTypes.indexOf("weak"), shortcut = edgeTypes.indexOf("shortcut");
  const dist = new Int32Array(count).fill(-1);
  const parent = new Int32Array(count).fill(-1), via = new Int32Array(count).fill(-1);
  const queue = new Int32Array(count);
  let head = 0, tail = 0;
  dist[0] = 0; queue[tail++] = 0;
  while (head < tail) {
    const i = queue[head++];
    for (let e = first[i]; e < first[i + 1]; e += E) {
      const t = edges[e + eType];
      if (t === weak || t === shortcut) continue;
      const to = edges[e + eTo] / N;
      if (dist[to] !== -1) continue;
      dist[to] = dist[i] + 1; parent[to] = i; via[to] = e; queue[tail++] = to;
    }
  }
  const edgeName = (e) => { const t = edgeTypes[edges[e + eType]]; const n = edges[e + eName]; return t === "element" || t === "hidden" ? `[${n}]` : strings[n]; };
  const detached = [];
  for (let i = 0; i < count; i++) {
    const n = name(i);
    const isDetached = (iDet >= 0 && nodes[i * N + iDet] === 2) || n.startsWith("Detached ");
    if (isDetached && /HTML|SVG|Text|Element/.test(n) && dist[i] > 0) detached.push(i);
  }
  const byTag = {};
  for (const i of detached) { const n = name(i).replace(/^Detached /, ""); byTag[n] = (byTag[n] || 0) + 1; }
  // Retaining paths: from each detached node towards the root, the first JS
  // objects (closures, contexts, objects) that hold it, grouped.
  const paths = {};
  for (const i of detached.slice(0, 4000)) {
    const chain = [];
    for (let cur = i, k = 0; cur > 0 && k < 40; cur = parent[cur], k++) {
      const t = type(cur), n = name(cur);
      if (via[cur] >= 0 && t !== "native" && t !== "hidden" && t !== "array" && n !== "system / Context")
        chain.push(`${n || t}.${edgeName(via[cur])}`.slice(0, 80));
      else if (via[cur] >= 0 && n === "system / Context") chain.push(`context.${edgeName(via[cur])}`);
    }
    const key = chain.slice(0, 7).join(" <- ");
    paths[key] = (paths[key] || 0) + 1;
  }
  return {
    detached_nodes: detached.length,
    detached_by_tag: Object.entries(byTag).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([k, v]) => `${k}×${v}`).join(" "),
    retainers: Object.entries(paths).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([k, v]) => `${v}: ${k}`),
  };
}

// ---------------------------------------------------------------- desktop ---

const freePort = () => new Promise((resolve) => { const srv = net.createServer(); srv.listen(0, "127.0.0.1", () => { const { port } = srv.address(); srv.close(() => resolve(port)); }); });
async function desktopLaunch() {
  const executable = path.join(APP, "Contents/MacOS/myflames");
  if (!existsSync(executable)) return { name: "desktop launch", error: `no packaged app at ${APP}` };
  const data = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-desktop-perf-"));
  const port = await freePort();
  const env = { ...process.env, MYFLAMES_DESKTOP_DATA: data, PATH: "/usr/bin:/bin:/usr/sbin:/sbin", MYFLAMES_DESKTOP_PYTHON: "/missing/python-must-not-be-used" };
  delete env.ELECTRON_RUN_AS_NODE;
  const spawnAt = Date.now();
  const child = spawn(executable, [`--remote-debugging-port=${port}`, "--remote-debugging-address=127.0.0.1"], { env, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    let version = null;
    for (let i = 0; i < 400 && !version; i++) {
      version = await fetch(`http://127.0.0.1:${port}/json/version`).then((r) => r.json()).catch(() => null);
      if (!version) await pause(25);
    }
    if (!version) return { name: "desktop launch", error: "no remote debugging endpoint (fuse or switch blocked)" };
    const debuggerAt = Date.now() - spawnAt;
    browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${port}`, defaultViewport: null });
    let page = null;
    for (let i = 0; i < 600 && !page; i++) {
      for (const t of browser.targets()) if (t.type() === "page" && /^http:\/\/127\.0\.0\.1:\d+\//.test(t.url())) page = await t.page();
      if (!page) await pause(25);
    }
    if (!page) return { name: "desktop launch", error: "no workspace page" };
    const pageSeenAt = Date.now() - spawnAt;
    await page.waitForFunction(() => !!document.querySelector(".home-start") && !document.body.innerText.includes("Opening your workspace"), { timeout: 30000, polling: 16 });
    const readySeenAt = Date.now() - spawnAt;
    const t = await page.evaluate(() => {
      const nav = performance.getEntriesByType("navigation")[0];
      const fcp = performance.getEntriesByType("paint").find((p) => p.name === "first-contentful-paint");
      return { origin: performance.timeOrigin, fcp: fcp ? fcp.startTime : null, dcl: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, now: performance.now() };
    });
    const nav = t.origin - spawnAt;
    return { name: "desktop launch", debugger_ms: debuggerAt, navigation_start_ms: nav, fcp_ms: t.fcp === null ? null : nav + t.fcp,
      window_shown_ms: nav + t.load, home_ready_ms: readySeenAt, page_seen_ms: pageSeenAt, page_fcp_ms: t.fcp, page_load_ms: t.load };
  } finally {
    if (browser) await browser.disconnect().catch(() => {});
    child.kill("SIGTERM");
    await new Promise((r) => { const timer = setTimeout(() => { child.kill("SIGKILL"); r(); }, 15000); child.once("exit", () => { clearTimeout(timer); r(); }); });
    await fs.rm(data, { recursive: true, force: true });
  }
}

// ------------------------------------------------------------------- main ---

const median = (xs) => { const v = xs.filter((x) => typeof x === "number" && Number.isFinite(x)).sort((a, b) => a - b); return v.length ? v[Math.floor((v.length - 1) / 2)] : null; };

async function runOnce(run, size, data, planFile, bigPlanFile) {
  const results = [];
  const label = `${size[0]}x${size[1]}`;
  if (ONLY.includes("cold")) { log(`[${run}] ${label} cold`); results.push(await coldLoad(data, size)); }
  const groups = ["probe", "places", "views", "tables", "overlays", "editor", "charts", "idle", "resize", "memory", "leak"].filter((g) => ONLY.includes(g));
  if (groups.length) {
    const server = await startServer();
    const browser = await launchBrowser();
    try {
      for (const g of groups) {
        log(`[${run}] ${label} ${g}`);
        // Each group starts from its own fresh page on the same server.
        const s = await bootstrap(browser, server, data, size);
        if (g === "probe") await step(results, g, () => probeScenario(s));
        if (g === "places") await step(results, g, () => placesScenario(s));
        if (g === "views") await step(results, g, () => viewsScenario(s));
        if (g === "tables") await step(results, g, () => tablesScenario(s));
        if (g === "overlays") await step(results, g, () => overlaysScenario(s));
        if (g === "editor") await step(results, g, () => editorScenario(s, bigPlanFile));
        if (g === "charts") {
          await step(results, "charts fixture", () => chartsScenario(s, planFile, "fixture"));
          await step(results, "charts synthetic", () => chartsScenario(s, bigPlanFile, "synthetic 1,000 operators"));
        }
        if (g === "idle") await step(results, g, () => idleScenario(s));
        if (g === "resize" && size[0] > 1000) await step(results, g, () => resizeScenario(s, size));
        if (g === "memory" && size[0] === SIZES[0][0]) await step(results, g, () => memoryScenario(s));
        if (g === "leak") await step(results, g, () => leakScenario(s));
        if (s.errors.length) results.push({ name: `${g}: page errors`, errors: s.errors.slice(0, 5) });
        // A fresh session for the next group: clear what this one saved.
        await s.page.evaluate(async () => {
          const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
          await fetch("/api/session/clear", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}" });
        }).catch(() => {});
        await s.page.close();
        // The launch URL is one use: later pages open the origin with the cookie and proof of the first.
        server.url = server.origin + "/";
      }
    } finally { await browser.close(); await server.close(); }
  }
  return results.map((r) => ({ ...r, size: label, run }));
}

const FIELDS = ["step_p50_ms", "step_max_ms", "animations", "content_ms", "settled_ms", "data_ms", "render_after_data_ms", "api_kb", "saves", "inp_ms", "long_tasks", "long_ms", "long_max_ms", "task_ms", "script_ms", "layout_ms", "style_ms", "layouts",
  "dropped", "dropped_pct", "p95_ms", "max_ms", "janky", "key_p50_ms", "key_p95_ms", "key_max_ms", "slow_keys", "cls",
  "fcp_ms", "lcp_ms", "home_ready_ms", "interactive_ms", "dcl_ms", "load_ms", "js_kb", "heap_mb", "nodes",
  "heap_growth_mb", "node_growth", "listener_growth", "heap_after_mb", "nodes_after", "listeners_after", "rows_drawn",
  "debugger_ms", "navigation_start_ms", "window_shown_ms", "page_fcp_ms", "page_load_ms"];

// A failed step is recorded and the run goes on; a page closed under a
// pending evaluation must not end the process.
process.on("unhandledRejection", (e) => log("unhandled:", String(e && e.message || e).split("\n")[0]));
/** Run one step of a scenario; a failure becomes a row with its error. */
async function step(out, name, fn) {
  try { const r = await fn(); if (Array.isArray(r)) out.push(...r); else if (r) out.push(r); }
  catch (e) { out.push({ name, error: String(e.message || e).split("\n")[0].slice(0, 160) }); }
}

(async () => {
  await fs.mkdir(OUT, { recursive: true });
  const data = await buildData();
  log("route sizes (bytes):", JSON.stringify(data.sizes));
  const planFile = path.resolve(opt("plan", path.join(root, "test/fixtures/explain-099-hypergraph-on-5t-avg-salary.json")));
  const bigPlanFile = path.join(OUT, "synthetic-plan-1000.json");
  await fs.writeFile(bigPlanFile, JSON.stringify(syntheticPlan(512)));
  const all = [];
  const load0 = os.loadavg();
  for (let run = 1; run <= RUNS; run++) {
    for (const size of SIZES) all.push(...(await runOnce(run, size, data, planFile, bigPlanFile)));
    if (ONLY.includes("desktop")) { log(`[${run}] desktop`); all.push({ ...(await desktopLaunch()), size: "app", run }); }
    await fs.writeFile(path.join(OUT, "results.json"), JSON.stringify({ when: new Date().toISOString(), load: load0, counts: data.counts, results: all }, null, 1));
  }
  // Medians by size and name.
  const groups = new Map();
  for (const r of all) {
    const k = `${r.size}\u0000${r.name}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  const medians = [];
  for (const [k, rs] of groups) {
    const [size, name] = k.split("\u0000");
    const m = { size, name, runs: rs.length };
    for (const f of FIELDS) { const v = median(rs.map((r) => r[f])); if (v !== null) m[f] = Math.round(v * 10) / 10; }
    if (rs.some((r) => r.timed_out)) m.timed_out = rs.filter((r) => r.timed_out).length;
    if (rs[0].error) m.error = rs[0].error;
    if (rs[0].column) m.column = rs[0].column;
    if (PROFILE && rs[0].top_functions) { m.top_functions = rs[0].top_functions; m.rendered = rs[0].rendered; m.commits = rs[0].commits; }
    medians.push(m);
  }
  const loadEnd = os.loadavg();
  await fs.writeFile(path.join(OUT, "results.json"), JSON.stringify({ when: new Date().toISOString(), load: [load0, loadEnd], counts: data.counts, results: all, medians }, null, 1));
  const cols = ["content_ms", "settled_ms", "data_ms", "render_after_data_ms", "inp_ms", "long_tasks", "long_max_ms", "task_ms", "script_ms", "layout_ms", "style_ms", "dropped", "p95_ms", "key_p50_ms", "key_p95_ms", "key_max_ms",
    "cls", "animations", "step_p50_ms", "step_max_ms", "fcp_ms", "lcp_ms", "home_ready_ms", "interactive_ms", "js_kb", "heap_growth_mb", "node_growth", "listener_growth", "window_shown_ms", "navigation_start_ms"];
  const used = cols.filter((c) => medians.some((m) => m[c] !== undefined));
  const lines = [`# Workspace responsiveness (${new Date().toISOString()}, ${RUNS} runs, medians; load average ${load0.map((x) => x.toFixed(1)).join(" ")} → ${loadEnd.map((x) => x.toFixed(1)).join(" ")})`, "",
    `| size | measurement | ${used.join(" | ")} |`, `| --- | --- | ${used.map(() => "---").join(" | ")} |`];
  for (const m of medians) lines.push(`| ${m.size} | ${m.name}${m.timed_out ? " (timed out)" : ""}${m.error ? ` (${m.error})` : ""} | ${used.map((c) => m[c] ?? "").join(" | ")} |`);
  if (PROFILE) {
    lines.push("", "## Profiles (first run)");
    for (const m of medians) if (m.top_functions) lines.push("", `### ${m.size} ${m.name}`, `rendered (${m.commits} commits): ${m.rendered}`, "self:", ...m.top_functions.self.map((x) => `- ${x}`), "inclusive:", ...m.top_functions.total.map((x) => `- ${x}`));
    await fs.mkdir(path.join(OUT, "profiles"), { recursive: true });
    for (const [i, p] of profiles.entries()) await fs.writeFile(path.join(OUT, "profiles", `${String(i).padStart(3, "0")}-${p.name.replace(/[^a-z0-9]+/gi, "-").slice(0, 80)}.cpuprofile`), JSON.stringify(p.profile));
  }
  await fs.writeFile(path.join(OUT, "summary.md"), lines.join("\n") + "\n");
  console.log(lines.join("\n"));
  console.log(`\nWrote ${path.join(OUT, "summary.md")} and results.json`);
})().catch((e) => { console.error(e); process.exit(1); });
