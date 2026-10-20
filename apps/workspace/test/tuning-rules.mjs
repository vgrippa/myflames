// Unit tests for tuningRules.ts (the MySQL Tuning scorecard).
// Run: node test/tuning-rules.mjs
//
// Inputs are real reports: Health computed by health.py from the captured
// samples in test/fixtures/health (MySQL 8.4 and 9.7, MariaDB 11.4), config-1.0
// replayed from test/fixtures/perf (MySQL 9.7, MariaDB 11.4), report-1.0 from
// the Full analysis replays (MySQL 8.4, MariaDB 11.4), and the workspace
// fixtures. MariaDB 10.6 and 11.8 have no captures: their cases reuse the
// MariaDB 11.4 reports with only the version changed, because the rules they
// test (innodb_log_file_size read-only before 10.9) depend on the version alone.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));
const load = (name) => JSON.parse(fs.readFileSync(path.join(fixtures, name), "utf8"));
const clone = (x) => JSON.parse(JSON.stringify(x));

async function bundle(entry) {
  const result = await build({
    entryPoints: [fileURLToPath(new URL(entry, import.meta.url))],
    bundle: true,
    format: "esm",
    platform: "neutral",
    target: "es2020",
    write: false,
    logLevel: "silent",
  });
  return import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64"));
}

const GENERATE = `
import json, os, sys
sys.path[:0] = [os.getcwd(), 'test']
from myflames import health
from myflames.config_review import config_report
from _perf_replay import ReplayConn, fixture as perf_fixture
import test_report
out = {}
for name in sys.argv[1].split(','):
    with open(os.path.join('test', 'fixtures', 'health', name + '.json'), encoding='utf-8') as h:
        d = json.load(h)
    a, b = d['samples']
    out['health:' + name] = health.compute_health(a, b, d['variables'], 5)
for server in sys.argv[2].split(','):
    out['config:' + server] = config_report(ReplayConn(perf_fixture('config', server)['calls']))
for name in sys.argv[3].split(','):
    out['report:' + name] = test_report.run_report(name)[0]
print(json.dumps(out))
`;
const home = fs.mkdtempSync(path.join(os.tmpdir(), "myflames-tuning-rules-"));
const generated = JSON.parse(
  execFileSync(
    process.env.PYTHON || "python3",
    [
      "-c",
      GENERATE,
      [
        "mysql-9.7",
        "mariadb-11.4",
        "scenario-mysql-8.4-redo",
        "scenario-mariadb-11.4-redo",
        "scenario-mysql-8.4-full",
        "scenario-mariadb-11.4-full",
        "scenario-mysql-8.4-threads",
        "scenario-mysql-8.4-table-cache",
      ].join(","),
      "mysql-9.7,mariadb-11.4",
      "mysql-8.4-replica,mariadb-11.4",
    ],
    // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
    { cwd: root, env: { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault" }, maxBuffer: 64 << 20 },
  ).toString(),
);
const H = (name) => clone(generated[`health:${name}`]);
const C = (name) => clone(generated[`config:${name}`]);
const R = (name) => clone(generated[`report:${name}`]);

const T = await bundle("../src/tuningRules.ts");
const { evaluateTuning, tuningInputs, tuningHtml, tuningMarkdown, escapeHtml, categoryScore, overallScore, letterGrade,
  globalChange, versionTuple, formatMetric, TUNING_CATEGORIES, CATEGORY_ORDER, POINTS,
  GRADE_SCALE, PROBLEM_GRADE_CAP, gradeRangeText, gradeScaleText, gradeReason, GRADE_RULE, SCORE_RULE, SCORING_RULE } = T;
const check = (report, key) => {
  const c = report.checks.find((x) => x.key === key);
  assert.ok(c, `check ${key} exists`);
  return c;
};
const allText = (report) => JSON.stringify(report);
const withVersion = (health, version) => {
  health.server_version = version;
  health.server.version = version;
  return health;
};
let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log(`✓ ${name}`);
}

test("weights sum to 1 and follow the documented split", () => {
  const sum = CATEGORY_ORDER.reduce((s, k) => s + TUNING_CATEGORIES[k].weight, 0);
  assert.equal(Math.round(sum * 100), 100);
  assert.deepEqual(CATEGORY_ORDER.map((k) => TUNING_CATEGORIES[k].weight), [0.3, 0.25, 0.2, 0.15, 0.1]);
  assert.deepEqual(POINTS, { ok: 100, watch: 50, problem: 0 });
});

test("nothing loaded: every check unknown with a reason, no score, no grade", () => {
  const r = evaluateTuning({});
  assert.equal(r.score, null);
  assert.equal(r.grade, null);
  assert.equal(r.provisional, true);
  assert.equal(r.label, "Not enough data");
  assert.equal(r.measured, 0);
  assert.equal(r.total, 19);
  assert.equal(r.recommendations.length, 0);
  for (const c of r.checks) {
    assert.equal(c.status, "unknown", c.key);
    assert.equal(c.value, "Not measured", c.key);
    assert.ok(c.unknownReason && c.unknownReason.length > 10, c.key);
    assert.ok(c.measureWith.length, c.key);
  }
  for (const c of r.categories) assert.equal(c.score, null);
});

test("unknown is excluded from the score; categories renormalize", () => {
  const mk = (category, status) => ({ category, status, applies: true });
  assert.equal(categoryScore([mk("innodb", "ok"), mk("innodb", "unknown"), mk("innodb", "watch")]), 75);
  assert.equal(categoryScore([mk("innodb", "unknown")]), null);
  assert.equal(categoryScore([{ ...mk("innodb", "problem"), applies: false }, mk("innodb", "ok")]), 100);
  // Only two categories measured: 100 at 0.3 and 0 at 0.1 -> 75, not diluted by the unmeasured ones.
  assert.equal(
    overallScore([
      { score: 100, weight: 0.3 },
      { score: null, weight: 0.25 },
      { score: 0, weight: 0.1 },
    ]),
    75,
  );
  assert.equal(overallScore([{ score: null, weight: 1 }]), null);
  assert.deepEqual([95, 85, 75, 55, 10].map(letterGrade), ["A", "B", "C", "D", "F"]);
  assert.deepEqual([100, 90, 89, 80, 79, 70, 69, 50, 49, 0].map(letterGrade), ["A", "A", "B", "B", "C", "C", "D", "D", "F", "F"]);
});

test("the grade scale is exported once and every text is rendered from it", () => {
  assert.deepEqual(GRADE_SCALE.map((g) => [g.grade, g.min]), [["A", 90], ["B", 80], ["C", 70], ["D", 50], ["F", 0]]);
  assert.equal(PROBLEM_GRADE_CAP, "C");
  assert.deepEqual(GRADE_SCALE.map((g) => gradeRangeText(g.grade)), ["90 and above", "80 to 89", "70 to 79", "50 to 69", "below 50"]);
  assert.equal(gradeScaleText(), "A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50");
  // Every range agrees with letterGrade at both ends.
  for (const g of GRADE_SCALE) assert.equal(letterGrade(g.min), g.grade);
  assert.match(GRADE_RULE, /A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50/);
  assert.match(GRADE_RULE, /A problem caps the grade at C\./);
  assert.match(GRADE_RULE, /provisional, with no grade, until at least half the checks are measured/);
  assert.equal(SCORING_RULE, `${SCORE_RULE} ${GRADE_RULE}`);
  assert.equal(evaluateTuning({}).scoring, SCORING_RULE, "the rule is shown and exported");
});

test("gradeReason: capped, uncapped, provisional", () => {
  const counts = (problem) => ({ problem, watch: 0, ok: 10, unknown: 0 });
  assert.equal(gradeReason({ score: 89, grade: "C", counts: counts(1) }), "Capped at C: 1 check has a problem (89 is in the B range).");
  assert.equal(gradeReason({ score: 94, grade: "C", counts: counts(2) }), "Capped at C: 2 checks have a problem (94 is in the A range).");
  assert.equal(gradeReason({ score: 89, grade: "B", counts: counts(0) }), "Score 89 is in the B range, 80 to 89.");
  assert.equal(gradeReason({ score: 74, grade: "C", counts: counts(1) }), "Score 74 is in the C range, 70 to 79.");
  assert.equal(gradeReason({ score: 95, grade: "A", counts: counts(0) }), "Score 95 is in the A range, 90 and above.");
  assert.equal(gradeReason({ score: 42, grade: "F", counts: counts(3) }), "Score 42 is in the F range, below 50.");
  assert.equal(gradeReason({ score: 61, grade: null, counts: counts(0) }), "", "provisional: no grade, no reason");
  assert.equal(gradeReason({ score: null, grade: null, counts: counts(0) }), "");
});

test("Health only (MySQL 8.4): unknowns, a provisional score and no grade", () => {
  const r = evaluateTuning({ health: load("health-mysql-8.4-full.json") });
  assert.ok(r.measured >= 10, `measured ${r.measured}`);
  assert.ok(r.measured < r.total);
  assert.equal(r.provisional, true, "Security has no measured check");
  assert.equal(r.grade, null);
  assert.equal(typeof r.score, "number");
  const security = r.categories.find((c) => c.key === "security");
  assert.equal(security.measured, 0);
  assert.equal(security.score, null);
  assert.equal(check(r, "no_password").status, "unknown");
  assert.match(check(r, "no_password").unknownReason, /full analysis/);
  assert.match(check(r, "secure_transport").unknownReason, /Parameters/);
  assert.equal(r.label, "Problems found", "the buffer pool problem is still reported");
  assert.equal(check(r, "buffer_pool").status, "problem");
  assert.ok(!/verified|optimal/i.test(allText(r)), "unknown is never verified or optimal");
});

test("Full analysis (MySQL 8.4 replica): measured from report findings and health_metrics", () => {
  const report = R("mysql-8.4-replica");
  const r = evaluateTuning({ report });
  assert.ok(r.measured >= 17, `measured ${r.measured} of ${r.total}`);
  assert.equal(check(r, "secure_transport").status, "unknown", "require_secure_transport is not collected");
  // health_metrics carries the ok metrics that findings leave out.
  for (const key of ["dirty_pages", "free_page_waits", "dropped_connections"]) {
    assert.notEqual(check(r, key).status, "unknown", key);
    assert.equal(check(r, key).source.view, "report");
  }
  // Without health_metrics (older reports) an absent finding is not evidence of ok.
  const old = clone(report);
  delete old.health_metrics;
  const o = evaluateTuning({ report: old });
  for (const key of ["dirty_pages", "free_page_waits", "dropped_connections"]) assert.equal(check(o, key).status, "unknown", key);
  // Accounts: the count comes from the finding's headline (str(len(names))), not numbers[0].value.
  const f = report.findings.find((x) => x.id === "checks.no_password");
  const nopw = check(r, "no_password");
  assert.equal(nopw.status, "problem");
  assert.equal(nopw.value, `${Number(f.headline)} account${Number(f.headline) === 1 ? "" : "s"}`);
  assert.ok(nopw.fix.sql.includes("ALTER USER"));
  assert.equal(check(r, "anonymous").status, "watch");
  assert.equal(check(r, "root_any_host").status, "watch");
  // Primary keys and MyISAM from the tables findings.
  assert.equal(check(r, "primary_keys").status, "problem");
  assert.equal(check(r, "myisam_tables").status, "watch");
  assert.equal(r.provisional, false);
  assert.ok(["C", "D", "F"].includes(r.grade), `a problem caps the grade at C, got ${r.grade}`);
  assert.equal(r.label, "Problems found");
});

test("problem caps the grade at C; all ok is Good", () => {
  const report = R("mysql-8.4-replica");
  for (const f of report.findings) f.status = "ok";
  for (const m of report.health_metrics) m.status = "ok";
  const good = evaluateTuning({ report });
  assert.equal(good.label, "Good");
  assert.equal(good.score, 100);
  assert.equal(good.grade, "A");
  report.findings.find((x) => x.id === "checks.no_password").status = "problem";
  const one = evaluateTuning({ report });
  assert.ok(one.score >= 90, `score ${one.score}`);
  assert.equal(one.grade, "C");
  assert.equal(gradeReason(one), `Capped at C: 1 check has a problem (${one.score} is in the A range).`);
  assert.equal(gradeReason(good), "Score 100 is in the A range, 90 and above.");
  assert.equal(one.label, "Problems found");
});

test("connections: current vs peak are labeled as such (health.py connections_used)", () => {
  const h = load("health-mysql-8.4-full.json");
  const c = check(evaluateTuning({ health: h }), "connections");
  const m = h.metrics.find((x) => x.key === "connections_used");
  assert.equal(c.value, `${m.inputs.Threads_connected} of ${m.inputs.max_connections} open, peak ${m.inputs.Max_used_connections}`);
  assert.equal(c.status, m.status);
});

test("dropped connections are a rate per second, not a percentage (aborted_clients unit /s)", () => {
  const h = load("health-mysql-8.4-full.json");
  const m = h.metrics.find((x) => x.key === "aborted_clients");
  m.value = 2.5;
  m.rate = 2.5;
  m.status = "watch";
  const c = check(evaluateTuning({ health: h }), "dropped_connections");
  assert.equal(c.value, "2.5/s");
  assert.equal(c.status, "watch");
  assert.ok(!c.fix.sql, "no wait_timeout or max_allowed_packet statement");
  assert.ok(!/28800|67108864|64MB/.test(JSON.stringify(c)));
});

test("temporary tables use the tmp_tables diagnostic (not a tmp_disk_tables metric)", () => {
  const maria = evaluateTuning({ health: H("mariadb-11.4") });
  const t = check(maria, "temp_tables");
  assert.equal(t.status, "problem");
  assert.match(t.fix.sql, /^SET SESSION tmp_memory_table_size = <bytes>;\nSET SESSION max_heap_table_size = <bytes>;$/);
  const h = load("health-mysql-8.4-full.json");
  const d = h.diagnostics.find((x) => x.key === "tmp_tables");
  d.status = "watch";
  const my = check(evaluateTuning({ health: h }), "temp_tables");
  assert.equal(my.status, "watch");
  // TempTable: tmp_table_size only; max_heap_table_size limits MEMORY tables only.
  assert.equal(my.fix.sql, "SET SESSION tmp_table_size = <bytes>;");
  d.settings.find((s) => s.name === "internal_tmp_mem_storage_engine").value = "MEMORY";
  assert.match(check(evaluateTuning({ health: h }), "temp_tables").fix.sql, /max_heap_table_size/);
  d.settings = d.settings.filter((s) => s.name !== "internal_tmp_mem_storage_engine");
  const unknownEngine = check(evaluateTuning({ health: h }), "temp_tables");
  assert.ok(!unknownEngine.fix.sql, "no statement before internal_tmp_mem_storage_engine is known");
  assert.match(unknownEngine.fix.text, /internal_tmp_mem_storage_engine/);
});

test("redo log: MySQL 8.4 and 9.7 SET PERSIST innodb_redo_log_capacity; 8.0.29 option file", () => {
  for (const [name, version] of [["scenario-mysql-8.4-redo", "8.4.11"], ["mysql-9.7", "9.7.2"]]) {
    const r = evaluateTuning({ health: withVersion(H(name), version) });
    const c = check(r, "redo_log");
    assert.equal(c.status, "watch", name);
    assert.match(c.fix.sql, /^SET PERSIST innodb_redo_log_capacity = \d+;$/, name);
    const bytes = Number(c.fix.sql.match(/= (\d+)/)[1]);
    assert.equal(bytes % (256 * 1024 * 1024), 0, "rounded up to 256 MiB");
    assert.ok(!c.fix.optionFile);
  }
  const old = check(evaluateTuning({ health: withVersion(H("scenario-mysql-8.4-redo"), "8.0.29") }), "redo_log");
  assert.ok(!old.fix.sql);
  assert.match(old.fix.optionFile, /innodb_log_file_size/);
});

test("redo log: MariaDB 11.8 SET GLOBAL plus option file; 10.6 read-only, option file only", () => {
  const maria118 = check(evaluateTuning({ health: withVersion(H("scenario-mariadb-11.4-redo"), "11.8.6-MariaDB") }), "redo_log");
  assert.match(maria118.fix.sql, /^SET GLOBAL innodb_log_file_size = \d+;$/);
  assert.match(maria118.fix.optionFile, /^innodb_log_file_size = \d+ {2}# under \[mysqld\]$/);
  const maria106 = check(evaluateTuning({ health: withVersion(H("scenario-mariadb-11.4-redo"), "10.6.25-MariaDB") }), "redo_log");
  assert.ok(!maria106.fix.sql, "innodb_log_file_size is read-only on MariaDB 10.6");
  assert.match(maria106.fix.optionFile, /^innodb_log_file_size = \d+/);
  assert.match(maria106.fix.text, /read-only/);
});

test("dirty pages: health status, no io_capacity advice on any engine", () => {
  const maria = check(evaluateTuning({ health: H("scenario-mariadb-11.4-redo") }), "dirty_pages");
  assert.equal(maria.status, "watch");
  const h = load("health-mysql-8.4-full.json");
  h.metrics.find((x) => x.key === "bp_dirty").status = "watch";
  const my = check(evaluateTuning({ health: h }), "dirty_pages");
  for (const c of [maria, my]) {
    assert.ok(!c.fix.sql);
    assert.ok(!/io_capacity/.test(JSON.stringify(c.fix)));
  }
});

test("free page waits: engine defaults for innodb_lru_scan_depth (1024 MySQL, 1536 MariaDB)", () => {
  for (const [h, expect, sql] of [
    [load("health-mysql-8.4-full.json"), /default 1024 per buffer pool instance/, /^SET PERSIST innodb_lru_scan_depth = <pages>;$/],
    [H("mariadb-11.4"), /default 1536/, /^SET GLOBAL innodb_lru_scan_depth = <pages>;$/],
  ]) {
    const m = h.metrics.find((x) => x.key === "bp_wait_free");
    m.status = "problem";
    const c = check(evaluateTuning({ health: h }), "free_page_waits");
    assert.equal(c.status, "problem");
    assert.match(c.fix.text, expect);
    assert.match(c.fix.sql, sql);
  }
});

test("buffer pool: full and missing is a problem, warming is not measured, no invented target", () => {
  for (const name of ["scenario-mysql-8.4-full", "scenario-mariadb-11.4-full"]) {
    const c = check(evaluateTuning({ health: H(name) }), "buffer_pool");
    assert.equal(c.status, "problem", name);
    assert.match(c.fix.sql, /innodb_buffer_pool_size = <bytes>;/);
    assert.ok(!/2147483648|(?<![\d.])2 GiB/.test(JSON.stringify(c.fix)), "no 2 GiB default target");
  }
  const maria = check(evaluateTuning({ health: H("scenario-mariadb-11.4-full") }), "buffer_pool");
  assert.equal(maria.fix.sql, "SET GLOBAL innodb_buffer_pool_size = <bytes>;");
  assert.match(maria.fix.optionFile, /^innodb_buffer_pool_size = <bytes>/);
  const warming = check(evaluateTuning({ health: H("mysql-9.7") }), "buffer_pool");
  assert.equal(warming.status, "unknown");
  assert.match(warming.unknownReason, /filling|warm/i);
});

test("joins without an index: honest wording, statements on request", () => {
  const c = check(evaluateTuning({ health: H("mysql-9.7") }), "joins_without_index");
  assert.equal(c.status, "problem");
  assert.match(c.why, /later table was read by a full scan/);
  assert.match(c.why, /hash join/);
  assert.ok(!/quadratic|catastrophic|for each row in the outer/i.test(JSON.stringify(c)));
});

test("slow query log: off is watch; MariaDB gets SET GLOBAL, the option file and the new-connection caveat", () => {
  const my = check(evaluateTuning({ config: C("mysql-9.7") }), "slow_log");
  assert.equal(my.status, "watch");
  assert.equal(my.fix.sql, "SET PERSIST slow_query_log = ON;\nSET PERSIST long_query_time = <seconds>;");
  assert.ok(!my.fix.optionFile);
  const maria = check(evaluateTuning({ config: C("mariadb-11.4") }), "slow_log");
  assert.equal(maria.fix.sql, "SET GLOBAL slow_query_log = ON;\nSET GLOBAL long_query_time = <seconds>;");
  assert.match(maria.fix.optionFile, /slow_query_log = ON/);
  assert.match(maria.fix.caveat, /copies the setting when it connects/);
  assert.match(my.why, /Slow_queries counts them even when the log is off/);
  // On with a 10 s threshold is still on: measured, ok.
  const cfg = C("mysql-9.7");
  const s = cfg.checks.find((x) => x.key === "slow_log");
  s.status = "info";
  assert.equal(check(evaluateTuning({ config: cfg }), "slow_log").status, "ok");
});

test("table open cache and table definition cache are separate checks", () => {
  const r = evaluateTuning({ health: H("scenario-mysql-8.4-table-cache") });
  const open = check(r, "table_open_cache");
  const defs = check(r, "table_definition_cache");
  assert.equal(open.status, "problem");
  assert.equal(open.fix.sql, "SET PERSIST table_open_cache = <n>;");
  assert.equal(defs.status, "ok");
  assert.ok(!/\.frm/.test(allText(r)));
});

test("thread cache: watch from health; a thread pool does not apply and is not counted", () => {
  const r = evaluateTuning({ health: H("scenario-mysql-8.4-threads") });
  const t = check(r, "thread_cache");
  assert.equal(t.status, "watch");
  assert.equal(t.fix.sql, "SET PERSIST thread_cache_size = <n>;");
  const h = load("health-mysql-8.4-full.json");
  const d = h.diagnostics.find((x) => x.key === "thread_cache");
  d.status = "info";
  d.state = "not_applicable";
  d.verdict = "Not used: thread_handling=pool-of-threads";
  const pooled = evaluateTuning({ health: h });
  assert.equal(check(pooled, "thread_cache").applies, false);
  assert.equal(pooled.total, 18);
});

test("tables: counts from the Tables report (MySQL 8.4, MariaDB 13.0), MyISAM never hard-coded", () => {
  const my = load("tables-mysql-8.4.json");
  const r = evaluateTuning({ tables: my });
  const pk = check(r, "primary_keys");
  assert.equal(pk.status, "problem");
  assert.match(pk.value, new RegExp(`^${my.summary.no_usable_key} tables without a key`));
  assert.match(pk.fix.sql, /ALTER TABLE/);
  assert.match(pk.fix.caveat, /blocks writes/);
  const myisam = check(r, "myisam_tables");
  const count = my.summary.engines.filter((e) => e.engine === "MyISAM").reduce((n, e) => n + e.tables, 0);
  assert.equal(myisam.value, `${count} MyISAM table${count === 1 ? "" : "s"}`);
  assert.equal(myisam.status, "watch");
  const none = clone(my);
  none.summary.engines = [{ engine: "InnoDB", tables: 18, bytes: 1 }];
  assert.equal(check(evaluateTuning({ tables: none }), "myisam_tables").status, "ok");
  const maria = evaluateTuning({ tables: load("tables-mariadb-13.0.json") });
  assert.notEqual(check(maria, "primary_keys").status, "unknown");
});

test("DATA_FREE is not scored and no OPTIMIZE advice is given", () => {
  const r = evaluateTuning({ report: R("mysql-8.4-replica"), tables: load("tables-mysql-8.4.json") });
  assert.ok(!r.checks.some((c) => /fragment|free_space/.test(c.key)));
  assert.ok(!/OPTIMIZE TABLE/.test(allText(r)));
});

test("encrypted connections: only from Parameters; MySQL SET PERSIST, MariaDB SET GLOBAL plus option file", () => {
  const params = (engine, items) => ({
    ...(engine === "mysql" ? load("config-mysql-8.4.json") : C("mariadb-11.4")),
    parameters: { schema_version: "parameters-1.0", complete: true, notes: [], items },
  });
  const item = (name, value) => ({ name, value });
  assert.equal(check(evaluateTuning({ config: load("config-mysql-8.4.json") }), "secure_transport").status, "unknown");
  const off = check(evaluateTuning({ config: params("mysql", [item("require_secure_transport", "OFF")]) }), "secure_transport");
  assert.equal(off.status, "watch");
  assert.equal(off.fix.sql, "SET PERSIST require_secure_transport = ON;");
  const on = check(evaluateTuning({ config: params("mysql", [item("require_secure_transport", "ON")]) }), "secure_transport");
  assert.equal(on.status, "ok");
  const maria = check(
    evaluateTuning({ config: params("mariadb", [item("require_secure_transport", "OFF"), item("have_ssl", "DISABLED")]) }),
    "secure_transport",
  );
  assert.equal(maria.fix.sql, "SET GLOBAL require_secure_transport = ON;");
  assert.match(maria.fix.optionFile, /require_secure_transport = ON/);
  assert.match(maria.fix.text, /Set up TLS first/);
  assert.match(maria.verdict, /have_ssl=DISABLED/);
});

test("globalChange per engine and versionTuple", () => {
  assert.deepEqual(globalChange("mysql", "max_connections", "<n>"), { sql: "SET PERSIST max_connections = <n>;" });
  assert.deepEqual(globalChange("mariadb", "max_connections", "<n>"), {
    sql: "SET GLOBAL max_connections = <n>;",
    optionFile: "max_connections = <n>  # under [mysqld]",
  });
  assert.deepEqual(versionTuple("11.4.13-MariaDB-ubu2404"), [11, 4, 13]);
  assert.deepEqual(versionTuple("5.5.5-10.6.25-MariaDB"), [10, 6, 25]);
  assert.deepEqual(versionTuple("26.7.0"), [26, 7, 0]);
  assert.equal(formatMetric({ value: 2.5, unit: "/s" }), "2.5/s");
  assert.equal(formatMetric({ value: 78.634, unit: "%" }), "78.6%");
});

test("MariaDB 11.4 Full analysis: every remediation is SET GLOBAL with an option-file line, never SET PERSIST", () => {
  const report = R("mariadb-11.4");
  for (const f of report.findings) if (f.id.startsWith("health.") || f.id.startsWith("config.")) f.status = "watch";
  for (const m of report.health_metrics) if (["bp_dirty", "bp_wait_free", "aborted_clients", "select_full_join", "connections_used"].includes(m.key)) m.status = "watch";
  const r = evaluateTuning({ report });
  assert.equal(r.server.engine, "mariadb");
  const fixes = r.recommendations.map((c) => c.fix).filter(Boolean);
  assert.ok(fixes.length > 8);
  for (const fx of fixes) {
    assert.ok(!/SET PERSIST/.test(fx.sql || ""), fx.sql);
    if (/SET GLOBAL (\w+)/.test(fx.sql || "")) assert.ok(fx.optionFile, `option file line for ${fx.sql}`);
  }
});

test("words: plain and sentence case", () => {
  const texts = [
    evaluateTuning({}),
    evaluateTuning({ report: R("mysql-8.4-replica") }),
    evaluateTuning({ health: H("mariadb-11.4"), config: C("mariadb-11.4") }),
  ].map(allText).join(" ");
  for (const bad of [/blinding/i, /catastrophic/i, /brownout/i, /Critical Vulnerabilit/i, /Optimal Health/i, /Select_key/, /< 1\.0%/, /\.frm/]) {
    assert.ok(!bad.test(texts), `no ${bad}`);
  }
  for (const c of evaluateTuning({}).checks) assert.match(c.title, /^[A-Za-z][a-z ]|^root /, c.title);
});

test("binding: only the connection in use is scored; another connection's reports are greyed", () => {
  const health = load("health-mysql-8.4-full.json");
  const report = R("mysql-8.4-replica");
  const a = (r) => ({ key: "A", label: "db-a", report: r });
  const b = (r) => ({ key: "B", label: "db-b", report: r });
  const here = tuningInputs({ activeKey: "A", report: a(report), health: b(health) });
  assert.equal(here.other, null);
  assert.ok(here.input.report && !here.input.health, "B's Health is ignored on A");
  const away = tuningInputs({ activeKey: "C", report: a(report), health: b(health) });
  assert.equal(away.other, "db-a");
  assert.ok(away.input.report && !away.input.health, "one connection's reports at a time");
  assert.deepEqual(tuningInputs({ activeKey: "A" }).input, {});
  // The first call site passes unbound reports: used only when no bound prop is given.
  assert.ok(tuningInputs({ activeKey: "A", legacy: { health } }).input.health);
  // With a bound prop the legacy one is ignored: B's Health shows greyed, never as A's.
  const greyed = tuningInputs({ activeKey: "A", health: b(health), legacy: { health } });
  assert.equal(greyed.other, "db-b");
  assert.equal(greyed.key, "B");
});

test("TempTable: SET SESSION tmp_table_size only from MySQL 8.0.28; before it temptable_max_ram decides", () => {
  const at = (version) => {
    const h = withVersion(load("health-mysql-8.4-full.json"), version);
    h.diagnostics.find((x) => x.key === "tmp_tables").status = "watch";
    return check(evaluateTuning({ health: h }), "temp_tables");
  };
  const old = at("8.0.27");
  assert.equal(old.fix.sql, "SET PERSIST temptable_max_ram = <bytes>;");
  assert.match(old.fix.text, /Before MySQL 8\.0\.28 TempTable has no per-table limit; temptable_max_ram \(global\) decides/);
  assert.match(old.fix.caveat, /shares temptable_max_ram/);
  for (const version of ["8.0.28", "8.4.11", "9.7.2"]) assert.equal(at(version).fix.sql, "SET SESSION tmp_table_size = <bytes>;", version);
});

test("TempTable: when only temptable_max_ram was hit, the global limit is offered, not tmp_table_size", () => {
  const h = load("health-mysql-8.4-full.json");
  const d = h.diagnostics.find((x) => x.key === "tmp_tables");
  d.status = "watch";
  d.numbers.push(
    { key: "hit_tmp_table_size", label: "Hit tmp_table_size (per table)", value: 0, unit: "", text: "0 in the window" },
    { key: "hit_max_ram", label: "Hit temptable_max_ram (all TempTable memory)", value: 4, unit: "", text: "4 in the window" },
  );
  const c = check(evaluateTuning({ health: h }), "temp_tables");
  assert.equal(c.fix.sql, "SET PERSIST temptable_max_ram = <bytes>;");
  assert.ok(!/tmp_table_size = /.test(c.fix.sql));
  assert.match(c.fix.caveat, /Every TempTable table shares temptable_max_ram/);
  // Both limits hit: the per-table limit stays the first statement.
  d.numbers.find((n) => n.key === "hit_tmp_table_size").value = 2;
  assert.equal(check(evaluateTuning({ health: h }), "temp_tables").fix.sql, "SET SESSION tmp_table_size = <bytes>;");
  // From Python's advice alone (a Full analysis finding keeps the text, not the keys).
  const h2 = load("health-mysql-8.4-full.json");
  const d2 = h2.diagnostics.find((x) => x.key === "tmp_tables");
  d2.status = "watch";
  d2.recommendation = { text: "Find the statements... Here TempTable's total memory ran out, so temptable_max_ram (global) is the limit to look at, not tmp_table_size.", caveat: "x" };
  assert.equal(check(evaluateTuning({ health: h2 }), "temp_tables").fix.sql, "SET PERSIST temptable_max_ram = <bytes>;");
});

test("redo: no size when Python offers none or its target is not above the capacity", () => {
  const base = () => {
    const h = H("scenario-mysql-8.4-redo");
    const d = h.diagnostics.find((x) => x.key === "redo");
    return { h, d };
  };
  // Checkpoint age raised the status and the capacity already covers the rate: Python has no recommendation.
  let { h, d } = base();
  d.recommendation = null;
  let c = check(evaluateTuning({ health: h }), "redo_log");
  assert.equal(c.status, "watch");
  assert.ok(!c.fix.sql && !c.fix.optionFile, "no statement without Python's target");
  assert.match(c.fix.text, /checkpoint age raised the status/);
  // Waits for the log buffer: innodb_log_buffer_size, not the size.
  ({ h, d } = base());
  d.recommendation = null;
  d.verdict += " 3 waits for space in the log buffer happened in the window; that points at innodb_log_buffer_size, not the redo capacity.";
  c = check(evaluateTuning({ health: h }), "redo_log");
  assert.ok(!c.fix.sql);
  assert.match(c.fix.text, /innodb_log_buffer_size, not the redo log size/);
  // A target at or below the capacity is never offered.
  ({ h, d } = base());
  const capacity = d.numbers.find((n) => n.key === "capacity").value;
  d.recommendation = { text: `If this rate is typical... SET GLOBAL innodb_redo_log_capacity = ${capacity}; resizes it online`, caveat: "x" };
  c = check(evaluateTuning({ health: h }), "redo_log");
  assert.ok(!c.fix.sql, `capacity ${capacity} is not a larger size`);
  // Python's larger target is used as is.
  d.recommendation.text = `If this rate is typical... SET GLOBAL innodb_redo_log_capacity = ${capacity + 268435456}; resizes it online`;
  c = check(evaluateTuning({ health: h }), "redo_log");
  assert.equal(c.fix.sql, `SET PERSIST innodb_redo_log_capacity = ${capacity + 268435456};`);
});

test("connections: a problem at 90% names refusals only when they were counted", () => {
  const at = (refused) => {
    const h = load("health-mysql-8.4-full.json");
    const m = h.metrics.find((x) => x.key === "connections_used");
    m.status = "problem";
    m.value = 92;
    m.inputs.Connection_errors_max_connections = refused;
    m.why = `92.0% of max_connections in use${refused ? `; ${refused} clients were refused at max_connections in the window (Connection_errors_max_connections)` : ""}. Rule of thumb: x.`;
    return check(evaluateTuning({ health: h }), "connections");
  };
  const full = at(0);
  assert.equal(full.status, "problem");
  assert.match(full.fix.text, /^Open connections reached 90% of max_connections\. /);
  assert.ok(!/refused/.test(full.fix.text));
  assert.match(at(3).fix.text, /^Clients were refused at max_connections\. /);
});

test("zero tables in scope: primary keys and MyISAM are unknown with the reason, never OK", () => {
  const empty = clone(load("tables-mysql-8.4.json"));
  Object.assign(empty.summary, { tables: 0, innodb: 0, other_engines: 0, no_usable_key: 0, not_null_unique: 0, engines: [] });
  empty.missing_primary_keys = [];
  empty.tables = [];
  const r = evaluateTuning({ tables: empty });
  for (const key of ["primary_keys", "myisam_tables"]) {
    const c = check(r, key);
    assert.equal(c.status, "unknown", key);
    assert.match(c.unknownReason, /No tables were read in every user database/, key);
  }
  const report = R("mysql-8.4-replica");
  const engines = report.findings.find((f) => f.id === "tables.engines");
  engines.headline = "no tables";
  engines.status = "info";
  for (const id of ["tables.no_primary_key", "tables.unique_stand_in"]) report.findings.find((f) => f.id === id).status = "ok";
  const fromReport = evaluateTuning({ report });
  for (const key of ["primary_keys", "myisam_tables"]) {
    assert.equal(check(fromReport, key).status, "unknown", key);
    assert.match(check(fromReport, key).unknownReason, /read no tables/, key);
  }
});

test("wording: slow log and encrypted connections", () => {
  const slow = check(evaluateTuning({ config: C("mysql-9.7") }), "slow_log");
  assert.match(slow.why, /only the log records each slow statement with its own text and timing/);
  const cfg = { ...load("config-mysql-8.4.json"), parameters: { schema_version: "parameters-1.0", complete: true, notes: [], items: [{ name: "require_secure_transport", value: "OFF" }] } };
  const tls = check(evaluateTuning({ config: cfg }), "secure_transport");
  assert.match(tls.why, /the login handshake and every query and result cross the network unencrypted/);
  assert.ok(!/password exchange/.test(tls.why));
});

test("HTML export escapes every server string; Markdown escapes too", () => {
  const tables = clone(load("tables-mysql-8.4.json"));
  tables.missing_primary_keys[0].schema = "<script>alert(1)</script>";
  tables.missing_primary_keys[0].table = 'x" onmouseover="y';
  tables.taken_at = "2099-01-01T00:00:00Z"; // newer than the Full analysis, so the Tables report is used
  const report = R("mysql-8.4-replica");
  report.findings.find((f) => f.id === "checks.no_password").action.sql = "ALTER USER '<img src=x onerror=alert(2)>'@'%';";
  report.findings.find((f) => f.id === "checks.anonymous").rule = "watch when free pages < 1024 & rising";
  const r = evaluateTuning({ tables, report, now: "2026-10-19T00:00:00Z" });
  const html = tuningHtml(r, "<b>prod</b>");
  assert.ok(!html.includes("<script>alert(1)"), "table name escaped");
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
  assert.ok(!html.includes('" onmouseover="'), "quotes escaped");
  assert.ok(!html.includes("<img src=x"), "statement text escaped");
  assert.ok(!html.includes("<b>prod</b>"), "connection label escaped");
  assert.ok(html.includes("watch when free pages &lt; 1024 &amp; rising"), "a threshold with < is escaped");
  assert.ok(html.includes("Inspired by Brendan Gregg&#39;s FlameGraph and Tanel Poder&#39;s SQL Plan FlameGraphs"));
  // Every < in the document is markup we wrote.
  const tags = html.match(/<\/?([a-zA-Z0-9!]+)/g).map((t) => t.replace(/[</]/g, "").toLowerCase());
  const allowed = new Set(["!doctype", "html", "head", "meta", "title", "style", "body", "main", "h1", "h2", "h3", "p", "table", "thead", "tbody", "tr", "th", "td", "section", "span", "strong", "pre", "code", "ul", "li", "footer"]);
  for (const t of tags) assert.ok(allowed.has(t), `unexpected tag <${t}>`);
  assert.equal(escapeHtml(`a<b>&"'`), "a&lt;b&gt;&amp;&quot;&#39;");
  const md = tuningMarkdown(r);
  // Fenced blocks are literal in Markdown; everything outside them is escaped.
  const prose = md.replace(/(`{3,})[a-z]*\n[\s\S]*?\n\1/g, "");
  assert.ok(md.includes("```sql"), "the statements are fenced");
  assert.ok(!prose.includes("<script>"), "Markdown escapes HTML outside code");
  assert.ok(md.includes("\\<script\\>"));
  assert.ok(md.includes("Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs."));
});

fs.rmSync(home, { recursive: true, force: true });
console.log(`\ntuning rules: ${passed} tests passed`);
