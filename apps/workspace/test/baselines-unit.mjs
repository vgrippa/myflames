// Compare with baseline without a browser (src/baselineCompare.ts): the
// Health verdicts and the restart rule, and the Query digest rule (new since
// the baseline; average latency or calls per second up by more than 50%).
// Every expected number below is worked out by hand in the comments.
// Run: node test/baselines-unit.mjs
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { build } from "esbuild";

const result = await build({
  stdin: { contents: "export * from './src/baselineCompare'; export { digestReading } from './src/QueryDigest';", loader: "ts", resolveDir: new URL("..", import.meta.url).pathname },
  loader: { ".css": "empty" },
  bundle: true, platform: "node", format: "cjs", write: false, logLevel: "silent",
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const {
  compareHealth, compareDigests, restartedBetween, serverClock, perSecondText, growthText, verdictText, metricVerdict,
  diagnosticVerdict, HEALTH_GAUGES, GROWTH, MIN_CALLS, firstSeenMoved, baselineMismatch, serverName, digestReading,
} = out.exports;

assert.equal(GROWTH, 1.5, "the stated rule: more than 50% above the baseline");
assert.equal(MIN_CALLS, 10);

// ---- Restart: uptime went down, or the start moved by more than 120 s.
const at = (iso, uptime) => ({ at: iso, uptime_s: uptime });
assert.equal(restartedBetween(at("2026-11-08T13:00:00Z", 1000), at("2026-11-08T13:10:00Z", 500)), true, "uptime went down");
// Same start (13:00 - 1000 s = 13:10 - 1600 s = 12:43:20).
assert.equal(restartedBetween(at("2026-11-08T13:00:00Z", 1000), at("2026-11-08T13:10:00Z", 1600)), false);
// Start moved by 100 s (within the slack): clocks and one-second uptimes drift.
assert.equal(restartedBetween(at("2026-11-08T13:00:00Z", 1000), at("2026-11-08T13:10:00Z", 1500)), false);
// Up longer than at the baseline, but it started 53 min 20 s later: a restart in between.
assert.equal(restartedBetween(at("2026-11-08T13:00:00Z", 1000), at("2026-11-08T15:00:00Z", 5000)), true);
assert.equal(restartedBetween(at("2026-11-08T13:00:00Z", null), at("2026-11-08T13:10:00Z", 5)), false, "unknown uptime: no claim");

// ---- Health verdicts.
const metric = (key, value, status, unit = "/s") => ({ key, group: "g", label: key, unit, value, status });
const diag = (key, status, state, headline) => ({ key, title: key, status, state, headline, value: null, unit: "" });
const baseline = {
  schema_version: "health-1.0", engine: "mysql", server_product: "MySQL", server_version: "8.0.36", interval: 5,
  taken_at: "2026-11-08T13:00:00Z", uptime_s: 1000, summary: { status: "ok" },
  metrics: [metric("questions", 100, "info"), metric("threads_running", 2, "ok", "threads"), metric("bp_hit_ratio", 99.5, "ok", "%"),
    metric("select_full_join", 0, "ok"), metric("bp_free", 9000, "info", "pages")],
  diagnostics: [diag("buffer_pool", "info", "warming", "Warming: 40% full"), diag("redo", "ok", null, "Covers 2 h of writes")],
};
const report = (uptime, taken_at, version = "8.0.36") => ({
  schema_version: "health-1.0", engine: "mysql", server_version: version, taken_at, uptime_s: uptime,
  metrics: [metric("questions", 150, "info"), metric("threads_running", 40, "watch", "threads"), metric("bp_hit_ratio", 95, "watch", "%"),
    metric("select_full_join", 12, "problem"), metric("bp_free", 10, "info", "pages"), metric("log_waits", 0, "ok")],
  diagnostics: [diag("buffer_pool", "ok", "full", "Full, 0.2 MiB/s read from disk"), diag("redo", "watch", null, "Covers 20 min of writes"),
    diag("binlog_cache", "ok", null, "No spills")],
});
let health = compareHealth(baseline, report(1600, "2026-11-08T13:10:00Z"));
assert.equal(health.restarted, false);
// ok→watch (threads_running), ok→watch (bp_hit_ratio), ok→problem (select_full_join),
// Warming→OK (buffer_pool), OK→Watch (redo): five; info and a tile the baseline lacks have no verdict to change.
assert.equal(health.changed, 5);
assert.deepEqual(health.metrics.threads_running, { inBaseline: true, compared: true, before: 2, beforeUnit: "threads", beforeVerdict: "ok", verdictChanged: true });
assert.equal(health.metrics.questions.verdictChanged, false, "no threshold, no verdict");
assert.equal(health.metrics.questions.before, 100);
assert.equal(health.metrics.log_waits.inBaseline, false);
assert.equal(health.diagnostics.buffer_pool.beforeVerdict, "warming");
assert.equal(health.diagnostics.buffer_pool.beforeHeadline, "Warming: 40% full");
assert.equal(health.diagnostics.binlog_cache.inBaseline, false);
assert.equal(health.upgraded, null);
// After a restart: tiles built from counters are not compared (value or verdict);
// gauges (threads_running ok→watch, bp_free) and diagnostics (2) still are: three changes.
health = compareHealth(baseline, report(300, "2026-11-08T13:10:00Z", "8.4.3"));
assert.equal(health.restarted, true);
assert.equal(health.changed, 3);
assert.equal(health.metrics.bp_hit_ratio.compared, false);
assert.equal(health.metrics.bp_hit_ratio.verdictChanged, false);
assert.equal(health.metrics.select_full_join.compared, false);
assert.equal(health.metrics.threads_running.compared, true);
assert.equal(health.metrics.bp_free.compared, true);
assert.deepEqual(health.upgraded, { from: "8.0.36", to: "8.4.3" });
for (const key of ["threads_connected", "threads_running", "connections_used", "bp_dirty", "bp_free", "row_lock_current", "table_cache_used"]) {
  assert.ok(HEALTH_GAUGES.has(key), key);
}
assert.equal(HEALTH_GAUGES.has("bp_hit_ratio"), false, "a ratio of counters is not a gauge");
assert.equal(metricVerdict("info"), null);
assert.equal(diagnosticVerdict("info", "not_applicable"), "not_applicable");
assert.equal(diagnosticVerdict("unknown", "warming"), null);
assert.equal(verdictText("room"), "Not full");

// ---- Query digest.
const row = (digest, count, total_ms, first_seen, schema = "shop") => ({
  digest, schema, digest_text: `SELECT ${digest}`, count, total_ms, avg_ms: total_ms / count, max_ms: null, first_seen, last_seen: first_seen,
});
const digests = {
  schema_version: "top-1.0", engine: "mysql", server_version: "8.4.3", server_product: "MySQL", order: "time", limit: 50,
  read_at: "2026-11-08T13:00:00.000Z", server_time: "2026-11-08 10:00:00.000000",
  coverage: { uptime_s: 1000, counting_since: null, digest_lost: 0 }, totals: null,
  statements: [
    // A: 2 ms avg; counted 600 s (first seen 09:50) -> 1000 / 600 = 1.67 calls/s.
    row("a", 1000, 2000, "2026-11-08 09:50:00"),
    // B: 10 ms avg; counted min(uptime 1000, 1000 s) -> 0.1 calls/s.
    row("b", 100, 1000, "2026-11-08 09:43:20"),
    // C: 1 ms avg; counted 900 s -> 0.0556 calls/s.
    row("c", 50, 50, "2026-11-08 09:45:00"),
    row("f", 10, 10, "2026-11-08 09:45:00"),
    row("h", 100, 100, "2026-11-08 09:45:00"),
    row("g", 5, 5, "2026-11-08 09:45:00"),
  ],
};
const now = (statements, uptime = 1600, readAt = "2026-11-08T13:10:00.000Z", extra = {}) => ({
  statements, version: "8.4.3", coverage: { uptime_s: uptime, counting_since: null, ...extra }, readAt,
});
const cur = (digest, count, total_ms, first_seen, schema = "shop") => ({ ...row(digest, count, total_ms, first_seen, schema), digest_text: `SELECT ${digest}` });
// 600 s later on the same run (start 12:43:20 both times).
const statements = [
  cur("a", 1600, 3500, "2026-11-08 09:50:00"), // +600 calls (1/s, -40%), +1500 ms (2.5 ms, +25%): not listed
  cur("b", 400, 4600, "2026-11-08 09:43:20"), // +300 calls: 0.5/s vs 0.1/s = +400%; 12 ms vs 10 ms = +20%
  cur("c", 70, 110, "2026-11-08 09:45:00"), // +20 calls: 3 ms vs 1 ms = +200%; 0.033/s vs 0.056/s = -40%
  cur("d", 30, 90, "2026-11-08 10:05:00"), // new: first seen after 10:00 (the baseline's server clock)
  cur("e", 400, 400, "2026-11-08 09:00:00"), // not in the baseline, first seen before it: not judged new
  cur("f", 15, 500, "2026-11-08 09:45:00"), // +5 calls only (< 10): never judged, however slow
  cur("h", 200, 250, "2026-11-08 09:45:00"), // +100 calls, +150 ms: 1.5 ms vs 1 ms = exactly +50%, not more
];
let digest = compareDigests(digests, now(statements));
assert.equal(digest.mode, "since");
assert.equal(digest.seconds, 600);
assert.deepEqual(digest.changes.map((c) => [c.statement.digest, c.kinds]), [["d", ["new"]], ["b", ["more_calls"]], ["c", ["slower"]]]);
assert.equal(digest.newCount, 1);
assert.equal(digest.grewCount, 2);
assert.equal(digest.matched, 5, "a, b, c, f and h are in both readings");
assert.equal(digest.notInBaseline, 1, "e");
const [d, b, c] = digest.changes;
assert.deepEqual(d.after, { avg_ms: 3, per_s: 0.05, calls: 30 });
assert.equal(d.before, null);
assert.equal(b.after.calls, 300);
assert.ok(Math.abs(b.after.per_s - 0.5) < 1e-12);
assert.ok(Math.abs(b.before.per_s - 0.1) < 1e-12);
assert.ok(Math.abs(b.rateGrowth - 4) < 1e-9);
assert.equal(growthText(b.rateGrowth), "+400%");
assert.ok(Math.abs(b.avgGrowth - 0.2) < 1e-9);
assert.ok(Math.abs(c.avgGrowth - 2) < 1e-9);
assert.ok(Math.abs(c.before.per_s - 50 / 900) < 1e-12);
assert.equal(growthText(c.avgGrowth), "+200%");
// Another schema with the same digest is another statement.
digest = compareDigests(digests, now([cur("b", 400, 4600, "2026-11-08 09:43:20", "billing")]));
assert.deepEqual(digest.changes.map((x) => x.kinds), [], "billing.b first seen before the baseline: not new, not matched");
assert.equal(digest.notInBaseline, 1);

// After a restart: nothing is new, calls per second are not compared, and
// average latency compares the totals since the restart (from 10 calls).
digest = compareDigests(digests, now([
  cur("a", 100, 210, "2026-11-08 13:05:00"), // 2.1 ms vs 2 ms: +5%
  cur("b", 200, 4000, "2026-11-08 13:05:00"), // 20 ms vs 10 ms: +100%
  cur("c", 5, 500, "2026-11-08 13:05:00"), // 5 calls: not judged
  cur("d", 30, 90, "2026-11-08 13:05:00"), // first seen after the baseline, but the restart reset FIRST_SEEN
], 500));
assert.equal(digest.mode, "restarted");
assert.equal(digest.seconds, null);
assert.deepEqual(digest.changes.map((x) => [x.statement.digest, x.kinds]), [["b", ["slower"]]]);
assert.equal(digest.changes[0].after.per_s, null);
assert.equal(digest.notInBaseline, 1);
// Up longer than at the baseline, but started later: still a restart.
assert.equal(compareDigests(digests, now(statements, 5000, "2026-11-08T15:00:00.000Z")).mode, "restarted");
// A count that went down on the same run: the digest table was reset.
digest = compareDigests(digests, now([cur("a", 20, 200, "2026-11-08 10:30:00"), cur("d", 30, 90, "2026-11-08 10:31:00")]));
assert.equal(digest.mode, "reset");
assert.deepEqual(digest.changes.map((x) => [x.statement.digest, x.kinds]), [["a", ["slower"]]], "10 ms vs 2 ms over the calls since the reset");
// A reset discover.py does not name (counting began within an hour of the
// start, so no counting_since) whose counts grew back past the baseline's:
// FIRST_SEEN moved (TRUNCATE zeroes it; the next call sets it: pfs_digest.cc
// reset_data, both engines). Baseline: 10 calls, 10 ms in all (1 ms each).
// Now: 20 calls, 30 ms since the reset (1.5 ms each, exactly +50%, not more).
// Read as a difference it would be 10 more calls taking 20 ms: "+100%".
const resetBase = { ...digests, statements: [row("r", 10, 10, "2026-11-08 09:50:00")] };
digest = compareDigests(resetBase, now([cur("r", 20, 30, "2026-11-08 10:30:00")]));
assert.equal(digest.mode, "reset", "a moved FIRST_SEEN is a reset");
assert.deepEqual(digest.changes, [], "1.5 ms against 1 ms is +50%: not more than 50%");
// The same reset with 60 ms over the 20 calls: 3 ms against 1 ms, +200%, over the calls since the reset.
digest = compareDigests(resetBase, now([cur("r", 20, 60, "2026-11-08 10:30:00"), cur("n", 50, 50, "2026-11-08 10:31:00")]));
assert.equal(digest.mode, "reset");
assert.deepEqual(digest.changes.map((x) => [x.statement.digest, x.kinds]), [["r", ["slower"]]]);
assert.ok(Math.abs(digest.changes[0].avgGrowth - 2) < 1e-9);
assert.equal(digest.changes[0].after.per_s, null, "no rate over a reset");
assert.equal(digest.newCount, 0, "after a reset FIRST_SEEN says nothing about new");
assert.equal(digest.notInBaseline, 1);
// An unchanged FIRST_SEEN (MariaDB's whole seconds included) is no reset.
assert.equal(compareDigests(resetBase, now([cur("r", 30, 30, "2026-11-08 09:50:00")])).mode, "since");
assert.equal(firstSeenMoved("2026-11-08 10:00:00", "2026-11-08 10:00:00.000000"), false);
assert.equal(firstSeenMoved("2026-11-08 10:00:00", "2026-11-08 10:00:01"), true);
assert.equal(firstSeenMoved(null, "2026-11-08 10:00:01"), false, "an unknown time never says so");
// Counting since a time after the baseline also says reset.
assert.equal(compareDigests(digests, now(statements, 1600, "2026-11-08T13:10:00.000Z", { counting_since: "2026-11-08 10:02:00" })).mode, "reset");
// No uptime on either side: only latency.
assert.equal(compareDigests({ ...digests, coverage: { ...digests.coverage, uptime_s: null } }, now(statements)).mode, "unknown");
// MariaDB's FIRST_SEEN has whole seconds (TIMESTAMP(0)): 10:00:00 is not after 10:00:00.4.
digest = compareDigests({ ...digests, server_time: "2026-11-08 10:00:00.400000" }, now([cur("n", 12, 12, "2026-11-08 10:00:00")]));
assert.equal(digest.newCount, 0);
assert.equal(digest.notInBaseline, 1);
digest = compareDigests({ ...digests, server_time: "2026-11-08 10:00:00.400000" }, now([cur("n", 12, 12, "2026-11-08 10:00:01")]));
assert.equal(digest.newCount, 1);
// Without the baseline's server clock nothing can be judged new.
assert.equal(compareDigests({ ...digests, server_time: null }, now(statements)).newCount, 0);
assert.deepEqual(compareDigests({ ...digests, server_version: "8.0.36" }, now(statements)).upgraded, { from: "8.0.36", to: "8.4.3" });

// ---- The server a reading came from must be the baseline's.
const A = { engine: "mysql", product: "MySQL", version: "8.4.3", server_uuid: "3e11fa47-71ca-11e1-9e33-c80aa9429562", hostname: "db1", port: 3306 };
const B = { ...A, server_uuid: "8f0c2b6e-0a43-11ef-8d43-0242ac120002", hostname: "db2" };
const M = { engine: "mariadb", product: "MariaDB", version: "11.4.5", server_uuid: null, hostname: "db3", port: 3307 };
const saved = { server_key: `mysql/uuid/${A.server_uuid}`, server: A };
assert.equal(baselineMismatch({ verified: true, server_key: `mysql/uuid/${A.server_uuid}`, server: { ...A, hostname: "alias" } }, saved), "",
  "the same server through another address");
assert.equal(baselineMismatch({ verified: true, server_key: `mysql/uuid/${B.server_uuid}`, server: B }, saved),
  `this reading came from MySQL 8.4.3 (server_uuid ${B.server_uuid}), and the baseline belongs to MySQL 8.4.3 (server_uuid ${A.server_uuid})`);
assert.equal(baselineMismatch({ error: "Lost connection" }, saved), "the server's identity was not read with this reading");
assert.equal(baselineMismatch(null, saved), "the server's identity was not read with this reading");
assert.equal(serverName(M), "MariaDB 11.4.5 at db3:3307");

// ---- Formats.
assert.equal(serverClock("2026-11-08 10:00:00.5") - serverClock("2026-11-08 10:00:00"), 500);
assert.ok(Number.isNaN(serverClock("yesterday")));
assert.equal(perSecondText(0.004), "<0.01/s");
assert.equal(perSecondText(0.5), "0.50/s");
assert.equal(perSecondText(12.345), "12.3/s");
assert.equal(perSecondText(150), "150/s");
assert.equal(perSecondText(null), "—");

// A reset inside the first hour changes the whole table's oldest FIRST_SEEN,
// even when the top-N sets have no digest in common and counting_since is hidden.
for (const engine of ["mysql", "mariadb"]) {
  const base = { ...digests, engine, coverage: { ...digests.coverage, oldest_first_seen: "2026-11-08 09:45:00" } };
  const current = now([cur("brand-new", 10000, 10000, "2026-11-08 10:00:10")], 1600);
  current.coverage = { ...current.coverage, counting_since: null, oldest_first_seen: "2026-11-08 10:00:10" };
  const compared = compareDigests(base, current);
  assert.equal(compared.mode, "reset", engine);
  assert.equal(compared.matched, 0);
  assert.equal(compared.newCount, 0);
  assert.equal(compared.seconds, null);
  assert.equal(compared.notInBaseline, 1);
  current.coverage.oldest_first_seen = "2026-11-08 09:45:00";
  assert.equal(compareDigests(base, current).mode, "since", "top-N churn alone is not a reset");
  assert.equal(compareDigests(base, current).newCount, 1);
  current.coverage.oldest_first_seen = "2026-11-08 09:44:00";
  assert.equal(compareDigests(base, current).mode, "since", "an earlier oldest time is not a reset");
  current.coverage.oldest_first_seen = null;
  assert.equal(compareDigests(base, current).mode, "since", "missing metadata preserves legacy comparisons");
}
assert.notEqual(baselineMismatch({ server_key: saved.server_key, server: A }, saved), "", "legacy identity is unverified");
assert.notEqual(baselineMismatch({ verified: false, server_key: saved.server_key, server: A }, saved), "", "unverified identity is refused");

// Exercise the actual Save baseline conversion before comparing. MariaDB's
// FIRST_SEEN truncates a reset at 10:00:00.9 to the second, earlier than the
// baseline's 10:00:00.5 timestamp. Only the saved whole-table minimum proves
// the reset when the two top-N lists have no digests in common.
for (const engine of ["mysql", "mariadb"]) {
  const converted = digestReading({
    ...digests, engine, version: digests.server_version, filter: digests.order,
    coverage: { ...digests.coverage, oldest_first_seen: "2026-11-08 09:45:00" },
  });
  const savedPayload = JSON.parse(JSON.stringify({ ...converted, server_time: "2026-11-08 10:00:00.500000" }));
  const current = now([cur("disjoint", 50, 50, "2026-11-08 10:00:10")]);
  current.coverage.oldest_first_seen = engine === "mariadb" ? "2026-11-08 10:00:00" : "2026-11-08 10:00:00.900000";
  const compared = compareDigests(savedPayload, current);
  assert.equal(compared.mode, "reset", `${engine}: save conversion preserves the reset evidence`);
  assert.equal(compared.newCount, 0, `${engine}: no false New after a reset`);
  assert.equal(compared.matched, 0);
  assert.equal(savedPayload.coverage.oldest_first_seen, "2026-11-08 09:45:00");
}
assert.equal(digestReading({ ...digests, coverage: null }).coverage, null);
assert.equal(digestReading({ ...digests }).coverage.oldest_first_seen, null, "legacy reports keep unknown metadata");

console.log("baselines unit: ok");
