// "Is this normal?": a reading compared with a baseline saved on this
// computer (baselinesApi.ts, myflames/baselines.py). Pure functions, no
// React, so test/baselines-unit.mjs runs them in Node.
//
// Health compares each tile's value and each verdict with the baseline's.
// Query digest lists the statements that are new since the baseline and the
// ones whose average latency or calls per second grew by more than half.
import type { HealthReport, HealthStatus, TopStatement } from "./api";
import type { BaselineServer, DigestBaselinePayload, DigestBaselineRow, HealthBaselinePayload, ReadingServer } from "./baselinesApi";

/** A server start that moved by more than this (seconds), from two
 * readings' own clocks and uptimes, means it restarted in between, even when
 * it has been up longer since. */
export const RESTART_SLACK_S = 120;
/** Grew: more than 50% above the baseline (after > before × 1.5). */
export const GROWTH = 1.5;
/** Calls since the baseline needed before latency or rate growth is judged. */
export const MIN_CALLS = 10;
/** Floating-point slack: exactly +50%, computed as 1.5000000000000002, is not more. */
const EPSILON = 1e-9;

/** Health metrics read from a gauge or a variable at the second sample
 * (myflames/health.py: Threads_connected, Threads_running, Open_tables over
 * table_open_cache, the buffer pool's dirty and free pages,
 * Innodb_row_lock_current_waits). A restart leaves them comparable. Every
 * other metric is a rate or a ratio of counters, which a restart starts over. */
export const HEALTH_GAUGES = new Set([
  "threads_connected", "threads_running", "connections_used", "bp_dirty", "bp_free", "row_lock_current", "table_cache_used",
]);

/** Verdict words of the Health statuses and states (the status pill's text). */
const VERDICT_TEXT: Record<string, string> = {
  ok: "OK", watch: "Watch", problem: "Problem", warming: "Warming", room: "Not full", not_applicable: "Not applicable",
};
export function verdictText(verdict: string | null) {
  return verdict ? VERDICT_TEXT[verdict] || verdict : "";
}
/** A metric's verdict: OK, Watch or Problem; none without a threshold or data. */
export function metricVerdict(status: HealthStatus | null | undefined): string | null {
  return status === "ok" || status === "watch" || status === "problem" ? status : null;
}
/** A diagnostic's verdict: its status, or its state when it has no threshold
 * but a state (Warming, Not full, Not applicable); none without data. */
export function diagnosticVerdict(status: HealthStatus | null | undefined, state: string | null | undefined): string | null {
  if (status === "ok" || status === "watch" || status === "problem") return status;
  return status === "info" && state && VERDICT_TEXT[state] ? state : null;
}

/** Whether the server restarted between two readings: its uptime went down,
 * or its start (each reading's time less its uptime) moved by more than
 * RESTART_SLACK_S. Unknown uptimes compare as no restart. */
export function restartedBetween(
  before: { uptime_s: number | null | undefined; at: string | null | undefined },
  after: { uptime_s: number | null | undefined; at: string | null | undefined },
): boolean {
  const b = before.uptime_s, a = after.uptime_s;
  if (typeof b !== "number" || typeof a !== "number") return false;
  if (a < b) return true;
  const tb = Date.parse(before.at || ""), ta = Date.parse(after.at || "");
  if (!Number.isFinite(tb) || !Number.isFinite(ta)) return false;
  return (ta / 1000 - a) - (tb / 1000 - b) > RESTART_SLACK_S;
}

export type MetricComparison = {
  /** The baseline holds this metric. */
  inBaseline: boolean;
  /** Its value is compared (false for a counter after a restart). */
  compared: boolean;
  before: number | null;
  beforeUnit: string;
  beforeVerdict: string | null;
  verdictChanged: boolean;
};
export type DiagnosticComparison = {
  inBaseline: boolean;
  beforeHeadline: string;
  beforeVerdict: string | null;
  verdictChanged: boolean;
};
export type HealthComparison = {
  restarted: boolean;
  /** The server version changed since the baseline. */
  upgraded: { from: string; to: string } | null;
  metrics: Record<string, MetricComparison>;
  diagnostics: Record<string, DiagnosticComparison>;
  /** Verdicts that changed, tiles and diagnostics together. */
  changed: number;
};

/** The current Health reading against a baseline: per tile the value then
 * and its verdict, per diagnostic its verdict. After a restart a tile built
 * from counters is not compared (value and verdict); gauges and diagnostics
 * still are. A verdict changed when both readings have one and they differ. */
export function compareHealth(baseline: HealthBaselinePayload, report: HealthReport): HealthComparison {
  const restarted = restartedBetween({ uptime_s: baseline.uptime_s, at: baseline.taken_at }, { uptime_s: report.uptime_s, at: report.taken_at });
  const beforeMetrics = new Map((baseline.metrics || []).map((m) => [m.key, m]));
  const beforeDiagnostics = new Map((baseline.diagnostics || []).map((d) => [d.key, d]));
  const metrics: Record<string, MetricComparison> = {};
  const diagnostics: Record<string, DiagnosticComparison> = {};
  let changed = 0;
  for (const m of report.metrics) {
    const b = beforeMetrics.get(m.key);
    const compared = !!b && !(restarted && !HEALTH_GAUGES.has(m.key));
    const beforeVerdict = b ? metricVerdict(b.status) : null;
    const now = metricVerdict(m.status);
    const verdictChanged = compared && !!beforeVerdict && !!now && beforeVerdict !== now;
    if (verdictChanged) changed += 1;
    metrics[m.key] = { inBaseline: !!b, compared, before: b ? b.value : null, beforeUnit: b ? b.unit : "", beforeVerdict, verdictChanged };
  }
  for (const d of report.diagnostics ?? []) {
    const b = beforeDiagnostics.get(d.key);
    const beforeVerdict = b ? diagnosticVerdict(b.status, b.state) : null;
    const now = diagnosticVerdict(d.status, d.state);
    const verdictChanged = !!beforeVerdict && !!now && beforeVerdict !== now;
    if (verdictChanged) changed += 1;
    diagnostics[d.key] = { inBaseline: !!b, beforeHeadline: b ? b.headline : "", beforeVerdict, verdictChanged };
  }
  const from = baseline.server_version || "", to = report.server_version || "";
  return { restarted, upgraded: from && to && from !== to ? { from, to } : null, metrics, diagnostics, changed };
}

/** A server in words: "MySQL 8.4.3 (server_uuid 3e11fa47-...)" or
 * "MariaDB 11.4.5 at db2:3306" (baselines.describe). */
export function serverName(server: Partial<BaselineServer> | null | undefined) {
  const name = `${server?.product || server?.engine || "?"} ${server?.version || "?"}`;
  return server?.server_uuid ? `${name} (server_uuid ${server.server_uuid})` : `${name} at ${server?.hostname || "?"}:${server?.port || "?"}`;
}

/** Why a reading cannot be compared with a baseline: "" when it came from
 * the baseline's server (the same server key), else what is wrong. A reading
 * without the server's identity is never compared. */
export function baselineMismatch(reading: ReadingServer | null | undefined, baseline: { server_key: string; server: BaselineServer }) {
  if (!reading || reading.error !== undefined || reading.verified !== true) return "the server's identity was not read with this reading";
  if (reading.server_key === baseline.server_key) return "";
  return `this reading came from ${serverName(reading.server)}, and the baseline belongs to ${serverName(baseline.server)}`;
}

/** "YYYY-MM-DD HH:MM:SS[.ffffff]" on the server's clock as milliseconds,
 * read as UTC so no zone rule of this computer moves it (both sides of a
 * comparison are the same server's clock). NaN when it is not one. */
export function serverClock(value: string | null | undefined): number {
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/.exec(value || "");
  if (!m) return NaN;
  const micro = Number((m[7] || "").padEnd(6, "0"));
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) + micro / 1000;
}

/** Whether a digest's FIRST_SEEN differs between two readings of the same
 * server: its row was started over (unknown values never say so). */
export function firstSeenMoved(before: string | null | undefined, after: string | null | undefined) {
  const b = serverClock(before), a = serverClock(after);
  return Number.isFinite(b) && Number.isFinite(a) && a !== b;
}

/** How a statement is matched across readings: schema and digest. */
export function digestKey(schema: string | null | undefined, digest: string | null | undefined) {
  return digest ? `${schema ?? ""}\u0000${digest}` : "";
}

export type DigestChangeKind = "new" | "slower" | "more_calls";
export type DigestRate = { avg_ms: number | null; per_s: number | null };
export type DigestChange = {
  key: string;
  statement: TopStatement;
  kinds: DigestChangeKind[];
  /** The baseline's average latency and calls per second (null for a new statement). */
  before: DigestRate | null;
  /** Since the baseline (or since counting began again, after a restart or a reset). */
  after: DigestRate & { calls: number };
  /** after / before - 1, when both are known. */
  avgGrowth: number | null;
  rateGrowth: number | null;
};
/** since: the counters' difference between the readings; restarted or reset:
 * the totals since counting began again (latency only); unknown: an uptime is
 * missing (latency only). */
export type DigestCompareMode = "since" | "restarted" | "reset" | "unknown";
export type DigestComparison = {
  mode: DigestCompareMode;
  /** Server uptime between the two readings (since mode). */
  seconds: number | null;
  changes: DigestChange[];
  newCount: number;
  grewCount: number;
  /** Statements found in both readings. */
  matched: number;
  /** Listed now, not in the baseline, and not judged new (first seen before
   * it, or new cannot be judged): left out of the changes. */
  notInBaseline: number;
  upgraded: { from: string; to: string } | null;
};
export type DigestNow = {
  statements: TopStatement[];
  version: string;
  coverage?: { uptime_s: number | null; counting_since?: string | null; oldest_first_seen?: string | null } | null;
  /** When this computer received the reading (ISO). */
  readAt: string;
};

/** Seconds the baseline counted a statement for: from its first seen (or
 * the server's start, whichever is later) to the baseline, at least 1. */
function beforeSeconds(row: DigestBaselineRow, baseline: DigestBaselinePayload): number | null {
  const uptime = baseline.coverage?.uptime_s ?? null;
  const span = (serverClock(baseline.server_time) - serverClock(row.first_seen)) / 1000;
  const counted = Number.isFinite(span) ? (typeof uptime === "number" ? Math.min(uptime, span) : span) : uptime;
  return typeof counted === "number" && Number.isFinite(counted) ? Math.max(1, counted) : null;
}

/** The current statement digests against a baseline (the rule the view states):
 * - New: not in the baseline, and first seen (FIRST_SEEN, the server's clock)
 *   after the baseline was read. Judged only while the server has kept running
 *   and the digest table was not reset since.
 * - Grew: average latency or calls per second more than 50% above the
 *   baseline's (GROWTH), from MIN_CALLS calls since the baseline. Before: the
 *   baseline's totals (calls over the seconds they were counted). After: the
 *   counters' difference between the readings over the server's uptime
 *   between them. After a restart or a reset only average latency is
 *   compared, over the totals since counting began again. */
export function compareDigests(baseline: DigestBaselinePayload, now: DigestNow): DigestComparison {
  const uptimeB = baseline.coverage?.uptime_s ?? null, uptimeN = now.coverage?.uptime_s ?? null;
  const restarted = restartedBetween({ uptime_s: uptimeB, at: baseline.read_at }, { uptime_s: uptimeN, at: now.readAt });
  const before = new Map<string, DigestBaselineRow>();
  for (const row of baseline.statements || []) {
    const key = digestKey(row.schema, row.digest);
    if (key) before.set(key, row);
  }
  // A reset of the digest table (TRUNCATE) starts every row over, FIRST_SEEN
  // included (pfs_digest.cc reset_data, both engines). The whole table's
  // oldest FIRST_SEEN moving forward detects resets even without overlapping
  // top-N rows, including the first hour after startup. Keep the older
  // counting_since and matched-row checks for baselines without this field.
  const since = now.coverage?.counting_since;
  const oldestBefore = serverClock(baseline.coverage?.oldest_first_seen);
  const oldestNow = serverClock(now.coverage?.oldest_first_seen);
  const oldestMoved = Number.isFinite(oldestBefore) && Number.isFinite(oldestNow) && oldestNow > oldestBefore;
  const beganAfterBaseline = Number.isFinite(oldestNow) && oldestNow > serverClock(baseline.server_time);
  let reset = !restarted && (oldestMoved || beganAfterBaseline
    || (!!since && serverClock(since) > serverClock(baseline.server_time)));
  if (!restarted && !reset) {
    for (const s of now.statements) {
      const b = before.get(digestKey(s.schema, s.digest));
      if (b && (s.count < b.count || firstSeenMoved(b.first_seen, s.first_seen))) { reset = true; break; }
    }
  }
  const mode: DigestCompareMode = restarted ? "restarted" : reset ? "reset"
    : typeof uptimeB === "number" && typeof uptimeN === "number" ? "since" : "unknown";
  const seconds = mode === "since" ? Math.max(0, (uptimeN as number) - (uptimeB as number)) : null;
  const t0 = serverClock(baseline.server_time);
  const changes: DigestChange[] = [];
  let matched = 0, notInBaseline = 0;
  for (const s of now.statements) {
    const key = digestKey(s.schema, s.digest);
    const b = key ? before.get(key) : undefined;
    if (!b) {
      const first = serverClock(s.first_seen);
      if (mode === "since" && Number.isFinite(t0) && Number.isFinite(first) && first > t0) {
        changes.push({
          key, statement: s, kinds: ["new"], before: null,
          after: { avg_ms: s.count > 0 ? s.total_ms / s.count : null, per_s: seconds ? s.count / seconds : null, calls: s.count },
          avgGrowth: null, rateGrowth: null,
        });
      } else notInBaseline += 1;
      continue;
    }
    matched += 1;
    const avgBefore = b.count > 0 ? b.total_ms / b.count : null;
    let after: DigestChange["after"];
    let perBefore: number | null = null;
    if (mode === "since") {
      const calls = s.count - b.count;
      if (calls < MIN_CALLS) continue;
      after = { avg_ms: (s.total_ms - b.total_ms) / calls, per_s: seconds ? calls / seconds : null, calls };
      const counted = beforeSeconds(b, baseline);
      perBefore = counted ? b.count / counted : null;
    } else {
      if (s.count < MIN_CALLS) continue;
      after = { avg_ms: s.total_ms / s.count, per_s: null, calls: s.count };
    }
    const kinds: DigestChangeKind[] = [];
    const avgGrowth = avgBefore && avgBefore > 0 && after.avg_ms !== null ? after.avg_ms / avgBefore - 1 : null;
    const rateGrowth = perBefore && perBefore > 0 && after.per_s !== null ? after.per_s / perBefore - 1 : null;
    if (avgGrowth !== null && avgGrowth > GROWTH - 1 + EPSILON) kinds.push("slower");
    if (rateGrowth !== null && rateGrowth > GROWTH - 1 + EPSILON) kinds.push("more_calls");
    if (!kinds.length) continue;
    changes.push({ key, statement: s, kinds, before: { avg_ms: avgBefore, per_s: perBefore }, after, avgGrowth, rateGrowth });
  }
  // New first (heaviest first), then the largest growth.
  const weight = (c: DigestChange) => Math.max(c.avgGrowth ?? 0, c.rateGrowth ?? 0);
  changes.sort((x, y) => {
    const nx = x.kinds[0] === "new" ? 1 : 0, ny = y.kinds[0] === "new" ? 1 : 0;
    if (nx !== ny) return ny - nx;
    return nx ? y.statement.total_ms - x.statement.total_ms : weight(y) - weight(x);
  });
  const from = baseline.server_version || "", to = now.version || "";
  return {
    mode, seconds, changes,
    newCount: changes.filter((c) => c.kinds[0] === "new").length,
    grewCount: changes.filter((c) => c.kinds[0] !== "new").length,
    matched, notInBaseline,
    upgraded: from && to && from !== to ? { from, to } : null,
  };
}

/** A growth as "+180%". */
export function growthText(growth: number | null) {
  if (growth === null || !Number.isFinite(growth)) return "";
  return `+${Math.round(growth * 100).toLocaleString()}%`;
}
/** Calls per second as the change lists write them: "12.0/s", "0.05/s", "<0.01/s". */
export function perSecondText(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value > 0 && value < 0.01) return "<0.01/s";
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })}/s`;
}
/** When a baseline was saved, on this computer's clock: "2026-11-08 14:02". */
export function savedAtText(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
