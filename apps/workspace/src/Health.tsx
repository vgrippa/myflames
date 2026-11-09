// Server > Health: two SHOW GLOBAL STATUS samples a few seconds apart
// (health-1.0 from myflames/health.py). One summary line and the server facts,
// the diagnostics as one-line rows sorted by severity, then compact metric
// tiles by group. Every explanation (what it measures, the rule, the numbers,
// the settings, the suggestion) opens in the inspector. The sample history for
// the sparklines and the live charts (the last 60 readings of the connection
// in use) lives in memory only, like Capture when… (checks while Health is on
// screen, then saves the processlist, InnoDB status and locks as an
// investigation); the view and the sampling window are restored with the
// workspace.
import { useEffect, useMemo, useRef, useState } from "react";
import { useViewOnScreen } from "./viewOnScreen";
import {
  baselineMismatch, compareHealth, diagnosticVerdict, metricVerdict, savedAtText, verdictText, type MetricComparison,
} from "./baselineCompare";
import { readingServerOf, type HealthBaselinePayload } from "./baselinesApi";
import {
  BaselineBar, BaselineMenu, CompareBaselineSheet, SaveBaselineSheet, defaultBaselineName, type ActiveBaseline,
} from "./Baselines";
import {
  api,
  type Bundle,
  type Connection,
  type EvidenceKind,
  type HealthDiagnostic,
  type HealthMetric,
  type HealthReport,
  type HealthStatus,
  type InnodbReport,
  type InvestigationEvidence,
  type LocksReport,
  type SessionsReport,
  type TopOrder,
  type TopStatement,
  type WatchReport,
} from "./api";
import { HEALTH_INTERVALS, type ServerKind } from "./session";
import { connectionLabel, useConnectionReads, type Bound } from "./binding";
import { StatementsBehind, type DigestAction, type LabSettings, type OpenTop } from "./SlowQueries";
import {
  ActionStatus,
  Checkbox,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  MetricTile,
  NumberField,
  Popover,
  Select,
  Sparkline,
  StatusPill,
  SummaryLine,
  TileGrid,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  closePopovers,
  errorText,
  isIdentifier,
  toneRank,
  trendRange,
  useAction,
  type InspectorContent,
  type Selection,
  type TrendPoint,
} from "./kit";
import "./health.css";

/** Automatic refresh choices (seconds); -1 follows the sampling window. */
const AUTO_INTERVALS = [10, 30, 60];
export const HEALTH_AUTO_KEY = "myflames.health.auto.";
export function healthAutoPreference(value: string | null): number {
  const n = value === null || value.trim() === "" ? -1 : Number(value);
  return n === -1 || n === 0 || AUTO_INTERVALS.includes(n) ? n : -1;
}

/** One full interval after each completed read, with no queued catch-up ticks.
 * Control changes update the schedule without requesting a read. Manual reads
 * share started/finished with automatic reads. A tick that starts no read
 * (another connection's greyed report on screen) is deferred, not dropped. */
export function createHealthRefresh({ every, enabled = true, read, now, later, cancel, backoff }: {
  every: number;
  enabled?: boolean;
  read: () => void;
  now: () => number;
  later: (fn: () => void, ms: number) => number;
  cancel: (id: number) => void;
  backoff: (seconds: number) => void;
}) {
  let visible = false;
  let pending = false;
  let disposed = false;
  let failures = 0;
  let retryAt = 0;
  let timer: number | undefined;
  const clear = () => { if (timer !== undefined) cancel(timer); timer = undefined; };
  const schedule = (ms: number) => {
    clear();
    if (visible && enabled && !pending && !disposed) timer = later(tick, ms);
  };
  const tick = () => {
    clear();
    if (!visible || !enabled || pending || disposed) return;
    if (retryAt > now()) { schedule(retryAt - now()); return; }
    // Claim the flight before invoking read, including synchronous callbacks.
    pending = true;
    read();
  };
  return {
    setVisible(next: boolean) {
      if (disposed || next === visible) return;
      visible = next;
      clear();
      if (visible) tick();
    },
    configure(nextEvery: number, nextEnabled: boolean) {
      if (disposed || (nextEvery === every && nextEnabled === enabled)) return;
      every = nextEvery;
      enabled = nextEnabled;
      // Enabling automatic refresh is a preference change, not a visibility
      // return. Its first read waits too; an existing read keeps its flight.
      clear();
      schedule(Math.max(every, retryAt - now()));
    },
    started() { pending = true; clear(); backoff(0); },
    /** A tick that starts no read (another connection's greyed report stays
     * on screen): the claim is released and the next tick waits a full
     * interval; an outstanding failure backoff still wins. */
    deferred() {
      if (disposed) return;
      pending = false;
      schedule(Math.max(every, retryAt - now()));
    },
    finished(success: boolean) {
      if (disposed) return;
      pending = false;
      failures = success ? 0 : failures + 1;
      const delay = success ? every
        : Math.min(60000, every * 2 ** Math.min(failures, 16));
      // Only failures carry a deadline across a visibility change. Returning
      // after a successful read asks once now, then waits a full interval.
      retryAt = success ? 0 : now() + delay;
      backoff(success ? 0 : delay / 1000);
      schedule(delay);
    },
    dispose() { disposed = true; clear(); },
  };
}
/** Capture when…: check interval choices (seconds). */
const WATCH_INTERVALS = [5, 10, 30, 60];
/** Samples kept per connection in memory for live charts and sparklines. */
export const HISTORY_LIMIT = 60;

export type HealthSample = {
  at: string;
  values: Record<string, number | null>;
};
/** What a watch checks, fixed when it starts (null: that condition is off). */
export type WatchRules = {
  /** Threads running at or above. */
  threads: number | null;
  /** An InnoDB row lock waited for longer than this many seconds (trx_wait_started). */
  lockWait: number | null;
  /** A statement running longer than this many seconds. */
  statement: number | null;
  /** Seconds between checks. */
  interval: number;
  stopAfterFirst: boolean;
};
/** A capture's reads and save: pending, then what happened. */
export type WatchCapture = {
  phase: "pending" | "done" | "failed";
  message: string;
  startedAt: number | null;
  /** The saved investigation. */
  saved: { id: string; name: string } | null;
  /** Reads that failed while the rest was saved. */
  missing: string;
};
/** Capture when…: kept by Server while the workspace is open, never saved.
 * It belongs to the connection it started on (key, label). */
export type WatchState = {
  /** A new number for every Start: answers of an earlier watch are dropped. */
  run: number;
  key: string;
  label: string;
  rules: WatchRules;
  /** Checking (paused while Health is off screen). */
  active: boolean;
  checked: number;
  /** read_at of the last check. */
  lastAt: string;
  /** After a capture, the next one waits until a check finds no condition met. */
  armed: boolean;
  /** What the last check could not judge, or why it failed. */
  note: string;
  capture: WatchCapture | null;
};
/** What Health keeps while the Server place is open (never saved): the last
 * sample and the history of the connection it was read from (binding.ts),
 * and Capture when…. */
export type HealthMemory = {
  key: string;
  label: string;
  report: HealthReport | null;
  history: HealthSample[];
  watch?: WatchState | null;
  /** Compare with baseline: the baseline chosen on the connection in use
   * (kept while the Server place is open, never saved; cleared by a switch). */
  baseline?: ActiveBaseline<HealthBaselinePayload> | null;
};
export const emptyHealth: HealthMemory = { key: "", label: "", report: null, history: [], watch: null, baseline: null };

/** Keep a new sample (from Health's Refresh or from Refresh all): the
 * report, and its rates appended to the history of its connection. The
 * same answer twice (one request shared by both) is kept once, and the same
 * sample again (the same taken_at) replaces the report without adding a
 * reading, so one reading never draws as two. */
export function rememberHealth(previous: HealthMemory, { key, label, report }: Bound<HealthReport>): HealthMemory {
  if (previous.key === key && previous.report === report) return previous;
  const values: Record<string, number | null> = Object.fromEntries(
    report.metrics.map((m) => [m.key, m.value]),
  );
  for (const d of report.diagnostics ?? []) if (d.trend) values[d.trend.key] = d.trend.value;
  const sample: HealthSample = { at: report.taken_at, values };
  const history = previous.key === key && !report.restarted ? previous.history : [];
  const again = previous.key === key && !!report.taken_at && previous.report?.taken_at === report.taken_at;
  return {
    key, label, report, history: again ? history : [...history, sample].slice(-HISTORY_LIMIT), watch: previous.watch ?? null,
    // A comparison goes on with each new reading of the same connection.
    baseline: previous.baseline && previous.baseline.key === key ? previous.baseline : null,
  };
}

/** The connection in use changed: the readings of the previous one are
 * cleared, and a watch started on another connection stops checking. A
 * capture already on its way finishes for the connection it started on and
 * keeps its outcome (it names that connection). */
export function switchHealthConnection(previous: HealthMemory, activeKey: string): HealthMemory {
  const history = previous.key && previous.key !== activeKey && previous.history.length ? [] : previous.history;
  const before = previous.watch ?? null;
  let watch = before;
  if (before && before.key !== activeKey && (before.active || !before.capture)) {
    watch = before.capture ? { ...before, active: false, note: "" } : null;
  }
  // A baseline belongs to the server it was chosen on.
  const kept = previous.baseline ?? null;
  const baseline = kept && kept.key !== activeKey ? null : kept;
  if (history === previous.history && watch === before && baseline === kept) return previous;
  return { ...previous, history, watch, baseline };
}

/** The conditions of *rules* in words: "threads running at or above 32". */
export function watchConditions(rules: WatchRules) {
  const parts: string[] = [];
  if (rules.threads !== null) parts.push(`threads running at or above ${rules.threads}`);
  if (rules.lockWait !== null) parts.push(`a row lock wait older than ${rules.lockWait} s`);
  if (rules.statement !== null) parts.push(`a statement running longer than ${rules.statement} s`);
  return parts.join("; ");
}

/** One check against *rules*: the first condition met ("" for none), and
 * the conditions this read could not judge (no value) or judged only in part
 * (a value from the sources it could read, and the reason for the rest). A
 * value that was not read is never taken as zero. The lock wait condition is
 * InnoDB's own row lock wait time; a statement waiting for a metadata or
 * table lock has no recorded wait start and is caught by the statement
 * condition, by its running time. */
export function judgeWatch(rules: WatchRules, snap: WatchReport): { tripped: string; note: string } {
  const errors: Partial<WatchReport["errors"]> = snap.errors || {};
  const none: string[] = [];
  const part: string[] = [];
  let tripped = "";
  if (rules.threads !== null) {
    if (typeof snap.threads_running !== "number") none.push(`threads running (${errors.threads_running || snap.reason || "not read"})`);
    else if (snap.threads_running >= rules.threads) tripped = `threads running ${snap.threads_running}`;
  }
  if (rules.lockWait !== null) {
    const wait = snap.longest_lock_wait;
    if (!tripped && wait && wait.seconds >= rules.lockWait) tripped = `row lock wait ${Math.round(wait.seconds)} s`;
    if (errors.longest_lock_wait) (wait ? part : none).push(`row lock waits (${errors.longest_lock_wait})`);
  }
  if (rules.statement !== null) {
    const statement = snap.longest_statement;
    if (!tripped && statement && statement.seconds >= rules.statement) tripped = `statement running ${Math.round(statement.seconds)} s`;
    if (errors.longest_statement) (statement ? part : none).push(`statements (${errors.longest_statement})`);
  }
  const note = [
    none.length ? `Not judged: ${none.join("; ")}.` : "",
    part.length ? `Partly judged: ${part.join("; ")}.` : "",
  ].filter(Boolean).join(" ");
  return { tripped, note };
}

function pad2(n: number) {
  return String(n).padStart(2, "0");
}
/** A time of day on this computer's clock, 24-hour: "14:02:31". */
export function clockTime(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
/** A date and minute on this computer's clock: "2026-10-27 14:02". */
export function localMinute(iso: string) {
  const parsed = new Date(iso);
  const d = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/** Captures on their way, by watch run: Stop reaches a capture started
 * before Health was opened again. */
const captureControllers = new Map<number, AbortController>();
let watchRuns = 0;

/** Numbers whose statements Performance Schema can name (workload
 * attribution): the metric or diagnostic key and the Top statements order. */
export const STATEMENTS_BEHIND: Record<string, TopOrder> = {
  "metric:select_full_join": "full_join",
  "metric:select_scan": "scan",
  "diag:full_scans": "no_index",
};

/** Lessons the report links to, with the title the link shows. */
export const LESSONS: Record<string, string> = {
  lru: "How the buffer pool keeps pages",
  buffer_pool_warmup: "Cold and warm buffer pools",
  tmp: "Temporary tables",
  filesort: "Filesort",
  join: "Block nested loop and hash join",
  full_scan: "Full table scan",
};

/** Badge text for a diagnostic whose status has no threshold but a state. */
const STATE_TEXT: Record<string, string> = {
  warming: "Warming",
  room: "Not full",
  not_applicable: "Not applicable",
};

function count(value: number) {
  const abs = Math.abs(value);
  const digits = abs >= 100 || Number.isInteger(value) ? 0 : abs >= 10 ? 1 : 2;
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function bytes(value: number) {
  const units: [number, string][] = [
    [2 ** 40, "TiB"],
    [2 ** 30, "GiB"],
    [2 ** 20, "MiB"],
    [2 ** 10, "KiB"],
  ];
  for (const [limit, unit] of units) if (Math.abs(value) >= limit) return [(value / limit).toFixed(1), unit];
  return [String(Math.round(value)), "B"];
}

/** The tile's value and unit, kept apart so the unit can be styled. */
export function healthValue(metric: HealthMetric): [string, string] {
  const { value, unit } = metric;
  if (value === null || value === undefined) return ["—", ""];
  if (unit === "%") return [value.toFixed(1), "%"];
  if (unit === "B/s") {
    const [n, u] = bytes(value);
    return [n, `${u}/s`];
  }
  if (unit === "/s") return [count(value), "/s"];
  const n = count(value);
  // "1 connection", never "1 connections": a counted noun (connections,
  // threads, pages, sessions) is singular after exactly one.
  return [n, n === "1" && /^[a-z]{3,}s$/.test(unit) ? unit.slice(0, -1) : unit];
}

/** The value and unit as one string: "12/s", "99.2%", "3 ms". */
export function healthText(metric: HealthMetric, value: number | null = metric.value) {
  const [n, unit] = healthValue({ ...metric, value });
  if (!unit) return n;
  return unit === "%" || unit.startsWith("/") ? `${n}${unit}` : `${n} ${unit}`;
}

/** Seconds as "3 d 4 h", "2 h 05 min", "4 min". */
export function uptime(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min`;
  return `${Math.round(seconds)} s`;
}

/** Bytes per second as "12.4 MiB/s". */
export function bytesPerSecond(value: number) {
  const [n, unit] = bytes(value);
  return `${n} ${unit}/s`;
}

function plural(n: number, one: string, many: string) {
  return `${n} ${n === 1 ? one : many}`;
}

/** The points of one series across the kept samples. */
function series(history: HealthSample[], name: string): TrendPoint[] {
  return history
    .map((s) => ({ at: s.at, value: s.values[name] }))
    .filter((p): p is TrendPoint => typeof p.value === "number");
}

function diagnosticLabel(d: HealthDiagnostic) {
  return d.status === "info" && d.state ? STATE_TEXT[d.state] : undefined;
}

type Link = { view: string; label: string } | null;
function Links({
  link,
  learn,
  onOpenView,
  onOpenLesson,
}: {
  link: Link;
  learn: string | null;
  onOpenView: (view: ServerKind | string) => void;
  onOpenLesson?: (lesson: string) => void;
}) {
  const lesson = learn && LESSONS[learn] && onOpenLesson ? learn : "";
  if (!link && !lesson) return null;
  return (
    <div className="inspector-links health-links">
      {link && (
        <button type="button" className="button" onClick={() => onOpenView(link.view)}>
          {link.label}
        </button>
      )}
      {lesson && (
        <button type="button" className="button" onClick={() => onOpenLesson?.(lesson)}>
          Lesson: {LESSONS[lesson]}
        </button>
      )}
    </div>
  );
}

/** Until the next reading the chart waits, without drawing a one-point chart. */
export function pendingTrendText(_auto: number) {
  return "Collecting…";
}

/** The inspector's trend: the sparkline once there are two readings, with
 * "Steady" when they all show the same value; before that the value alone
 * and one muted line, never a chart of one point. */
function Trend({ points, label, format, auto }: { points: TrendPoint[]; label: string; format: (v: number) => string; auto: number }) {
  const range = trendRange(points, format);
  if (!range || range.readings < 2) {
    return (
      <p className="health-trend-pending" data-trend="pending">
        {range && <strong className="health-trend-value">{range.current}</strong>}
        <span className="hint">{pendingTrendText(auto)}</span>
      </p>
    );
  }
  return (
    <>
      <Sparkline points={points} label={label} format={format} width={240} height={40} className="is-large health-spark" />
      {range.steady && (
        <p className="hint health-trend-steady" data-trend="steady">
          Steady at {range.current} over the last {range.readings} readings.
        </p>
      )}
    </>
  );
}

/** The live charts above Diagnostics: health-1.0 metric keys, never a
 * number computed here. Connections in use is the count of open connections
 * (Threads_connected), not the share of max_connections. */
export const LIVE_CHART_SPECS: { key: string; title: string }[] = [
  { key: "queries", title: "Queries per second" },
  { key: "threads_running", title: "Threads running" },
  { key: "bp_disk_reads", title: "Buffer pool reads from disk per second" },
  { key: "threads_connected", title: "Connections in use" },
];

/** One series over the kept readings: a 2 px line over a light wash, from a
 * zero baseline so a small change never looks like a jump, the latest
 * reading marked, and each reading's time and value on hover. With one
 * reading the card holds only its title and value (no chart, no Min and
 * Max); when every reading shows the same value it says Steady instead of a
 * Min equal to its Max. */
function LiveTrendChart({
  chartKey,
  title,
  metric,
  points,
}: {
  chartKey: string;
  title: string;
  metric: HealthMetric;
  points: TrendPoint[];
}) {
  const text = (v: number) => healthText(metric, v);
  const range = trendRange(points, text);
  const titleId = `health-chart-${chartKey}`;
  if (!range) return null;
  const head = (
    <div className="health-chart-head">
      <span className="health-chart-title" id={titleId}>{title}</span>
      <span className="health-chart-current" data-chart-current>{range.current}</span>
    </div>
  );
  if (range.readings < 2) {
    return (
      <div className="health-live-chart is-pending" data-chart={chartKey} role="group" aria-labelledby={titleId}>
        {head}
        <span className="visually-hidden health-chart-summary">{`${title}: ${range.current}, one reading so far.`}</span>
      </div>
    );
  }
  const values = points.map((p) => p.value);
  const n = values.length;
  const current = values[n - 1];
  const max = Math.max(...values);
  const summary = range.steady
    ? `${title}: steady at ${range.current} over the last ${n} readings.`
    : `${title}: current ${range.current}, min ${range.low}, max ${range.high} over the last ${n} readings.`;
  // Drawn stretched to the card's width (preserveAspectRatio none); strokes
  // and the end dot keep their size (non-scaling stroke, round caps).
  const width = 240;
  const height = 40;
  const inset = 6;
  const ceiling = max > 0 ? max : 1;
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * width : width / 2);
  const y = (v: number) => inset + (height - 2 * inset) * (1 - Math.max(0, v) / ceiling);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(2)} ${y(v).toFixed(2)}`).join(" ");
  const base = height - inset;
  const area = `${line} L${x(n - 1).toFixed(2)} ${base} L${x(0).toFixed(2)} ${base} Z`;
  const end = `M${x(n - 1).toFixed(2)} ${y(current).toFixed(2)} l0 0`;
  const step = n > 1 ? width / (n - 1) : width;
  return (
    <div className={`health-live-chart${range.steady ? " is-steady" : ""}`} data-chart={chartKey} role="group" aria-labelledby={titleId}>
      {head}
      <svg
        className="health-chart-svg"
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${title}, last ${n} readings`}
      >
        <line className="health-chart-base" x1={0} x2={width} y1={base} y2={base} vectorEffect="non-scaling-stroke" />
        <path className="health-chart-area" d={area} />
        <path className="health-chart-line" d={line} vectorEffect="non-scaling-stroke" />
        <path className="health-chart-ring" d={end} vectorEffect="non-scaling-stroke" />
        <path className="health-chart-dot" d={end} vectorEffect="non-scaling-stroke" />
        {points.map((p, i) => (
          <rect key={i} className="health-chart-hit" x={x(i) - step / 2} y={0} width={step} height={height}>
            <title>{`${clockTime(p.at)}: ${text(p.value)}`}</title>
          </rect>
        ))}
      </svg>
      <div className="health-chart-range" data-chart-range>
        {range.steady ? (
          <span>Steady over the last {n} readings</span>
        ) : (
          <>
            <span>Min {range.low}</span>
            <span>Max {range.high}</span>
          </>
        )}
      </div>
      <span className="visually-hidden health-chart-summary">{summary}</span>
    </div>
  );
}

/** "1 problem, 1 to watch" for the tiles of one group: the share of the
 * summary line's count a group holds, so the numbers add up when the tiles
 * form a strip that scrolls (900 px and below). Empty when all is well. */
export function groupStatusText(statuses: HealthStatus[]) {
  const problem = statuses.filter((v) => v === "problem").length;
  const watch = statuses.filter((v) => v === "watch").length;
  return [problem ? plural(problem, "problem", "problems") : "", watch ? `${watch} to watch` : ""].filter(Boolean).join(", ");
}

/** A tile's line under its value while Health compares: "was 283/s", or
 * why there is none (a counter after a restart, a tile the baseline lacks). */
export function baselineLine(metric: HealthMetric, then: MetricComparison | undefined) {
  if (!then || !then.inBaseline) return "Not in the baseline";
  if (!then.compared) {
    return (
      <span className="baseline-not-compared" title="Not compared: the server restarted since">
        Not compared<span className="visually-hidden">: the server restarted since</span>
      </span>
    );
  }
  return `was ${then.before === null ? "—" : healthText({ ...metric, unit: then.beforeUnit || metric.unit }, then.before)}`;
}

export function HealthView({
  connection,
  disabled,
  onBack,
  selection,
  interval,
  onIntervalChange,
  memory,
  onMemory,
  onOpenView,
  onOpenLesson,
  onOpenInvestigation,
  performanceSchema = null,
  settings,
  onDigest,
  onOpenTop,
}: {
  connection: Connection;
  /** Query editor settings for Run analysis on a statement behind a number. */
  settings: LabSettings;
  onDigest: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onOpenTop: OpenTop;
  /** Whether Performance Schema is on, when another loaded report says so (never read for this). */
  performanceSchema?: boolean | null;
  /** The Query editor is capturing: wait. */
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  interval: number;
  onIntervalChange: (seconds: number) => void;
  memory: HealthMemory;
  onMemory: (next: (previous: HealthMemory) => HealthMemory) => void;
  onOpenView: (view: ServerKind | string) => void;
  onOpenLesson?: (lesson: string) => void;
  onOpenInvestigation?: (id: string) => void;
}) {
  const preferenceKey = HEALTH_AUTO_KEY + (connection.type || "production");
  const [preference, setPreference] = useState<{ key: string; value: number } | null>(null);
  const storedAuto = () => {
    try { return healthAutoPreference(localStorage.getItem(preferenceKey)); } catch { return -1; }
  };
  const auto = preference?.key === preferenceKey ? preference.value : storedAuto();
  const setAuto = (value: number) => {
    setPreference({ key: preferenceKey, value });
    try { localStorage.setItem(preferenceKey, String(value)); } catch { /* Best effort. */ }
  };
  const [retrySeconds, setRetrySeconds] = useState(0);
  const refreshLoop = useRef<ReturnType<typeof createHealthRefresh> | null>(null);
  const readSequence = useRef(0);
  const onScreen = useViewOnScreen();
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const root = useRef<HTMLDivElement>(null);
  const report = memory.report;
  const key = reads.key;

  // Save baseline and Compare with baseline (the Refresh menu): each sheet
  // holds the connection, and Save the reading, of the moment it opened; a
  // connection switch closes it.
  const [baselineSheet, setBaselineSheet] = useState<
    null | { kind: "save"; key: string; connection: Connection; report: HealthReport } | { kind: "compare"; key: string; connection: Connection }
  >(null);
  const visibilitySettings = useRef({ onScreen, disabled, baselineSheet });
  visibilitySettings.current = { onScreen, disabled, baselineSheet };
  const updateRefreshVisibility = useRef(() => {});
  const baselineSaved = useAction(key);
  useEffect(() => setBaselineSheet(null), [key]);
  const activeBaseline = memory.baseline && memory.baseline.key === key ? memory.baseline : null;
  const refreshOptions = () => root.current?.closest(".server-view")?.querySelector<HTMLElement>('button[aria-label="Refresh options"]');

  // Capture when…: the watch lives in Server's memory (it outlives this view
  // while the Server place is open); the form below is a draft until Start.
  const watch = memory.watch ?? null;
  const watchRef = useRef(watch);
  watchRef.current = watch;
  const latestConnection = useRef(connection);
  latestConnection.current = connection;
  const draft = watch?.rules;
  const [condThreads, setCondThreads] = useState(draft ? draft.threads !== null : true);
  const [threadsText, setThreadsText] = useState(String(draft?.threads ?? 32));
  const [condLockWait, setCondLockWait] = useState(draft ? draft.lockWait !== null : false);
  const [lockWaitText, setLockWaitText] = useState(String(draft?.lockWait ?? 10));
  const [condStatement, setCondStatement] = useState(draft ? draft.statement !== null : false);
  const [statementText, setStatementText] = useState(String(draft?.statement ?? 30));
  const [watchInterval, setWatchInterval] = useState<number>(draft?.interval ?? 10);
  const [stopAfterFirst, setStopAfterFirst] = useState(draft?.stopAfterFirst ?? true);
  const [formError, setFormError] = useState("");
  /** A form change clears the error Start showed. */
  const edit = <T,>(set: (value: T) => void) => (value: T) => {
    setFormError("");
    set(value);
  };
  const tickAbort = useRef<AbortController | null>(null);
  const tickBusy = useRef(false);
  const watchHere = !!watch && watch.key === key;
  const checking = watchHere && !!watch?.active;
  const capturePending = watch?.capture?.phase === "pending";

  /** Change the watch of *run* (an earlier watch's late answer changes nothing). */
  function updateWatch(run: number, change: (w: WatchState) => WatchState | null) {
    onMemory((previous) => {
      const w = previous.watch;
      if (!w || w.run !== run) return previous;
      return { ...previous, watch: change(w) };
    });
  }

  // A check on its way is dropped when the connection changes or Health closes.
  useEffect(() => {
    tickAbort.current?.abort();
  }, [key]);
  useEffect(() => () => tickAbort.current?.abort(), []);

  /** After a condition was met: read the processlist, InnoDB status and locks of
   * the connection the watch started on, then save them as a new investigation. It goes on
   * when Health closes (the outcome stays in the status line); Stop ends it. */
  async function capture(w: WatchState, snap: WatchReport, tripped: string, c: Connection) {
    const run = w.run;
    const controller = new AbortController();
    captureControllers.set(run, controller);
    const step = (message: string) =>
      updateWatch(run, (x) => ({
        ...x,
        capture: { phase: "pending", message, startedAt: x.capture?.startedAt ?? Date.now(), saved: null, missing: "" },
      }));
    const evidence: InvestigationEvidence[] = [
      { id: "watch-1", kind: "watch", captured_at: snap.read_at, title: "Watch trigger snapshot", text: "", data: snap as unknown as Record<string, unknown> },
    ];
    const missing: string[] = [];
    const steps: [string, EvidenceKind, string, string, Record<string, unknown>][] = [
      ["sessions-1", "sessions", "Processlist", "live/sessions", { active_only: false }],
      ["innodb-1", "innodb", "InnoDB status", "live/innodb", { raw: false }],
      ["locks-1", "locks", "Locks", "live/locks", {}],
    ];
    try {
      for (const [index, [id, kind, title, path, params]] of steps.entries()) {
        step(`Capturing ${title === "Processlist" ? "the processlist" : title} (${index + 1} of ${steps.length})…`);
        try {
          const data = await api<SessionsReport | InnodbReport | LocksReport>(path, { connection: c, ...params }, controller.signal);
          evidence.push({ id, kind, captured_at: new Date().toISOString(), title, text: "", data: data as unknown as Record<string, unknown> });
        } catch (e) {
          if (controller.signal.aborted) throw e;
          missing.push(`${title} (${errorText(e)})`);
        }
      }
      if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
      step("Saving the investigation…");
      const name = `Capture: ${tripped} on ${w.label}, ${localMinute(snap.read_at)}`;
      // Not stopped once sent: the save is local and its answer is shown.
      const saved = await api<Bundle>("investigations/save", {
        value: {
          schema_version: "investigation-1.0",
          name,
          notes: `Captured automatically on ${w.label} when ${tripped}. Conditions: ${watchConditions(w.rules)}.` +
            (missing.length ? ` Not captured: ${missing.join("; ")}.` : ""),
          tags: ["watch", "capture"],
          baseline_id: "",
          plans: [],
          evidence,
        },
      });
      updateWatch(run, (x) => ({
        ...x,
        capture: {
          phase: "done",
          message: "Saved capture.",
          startedAt: null,
          saved: { id: saved.id || "", name: saved.name },
          missing: missing.length ? `Not captured: ${missing.join("; ")}.` : "",
        },
      }));
    } catch (e) {
      const stopped = controller.signal.aborted;
      updateWatch(run, (x) => ({
        ...x,
        armed: true,
        capture: {
          phase: "failed",
          message: stopped ? "Capture stopped. Nothing was saved." : `Could not save the capture: ${errorText(e)}`,
          startedAt: null,
          saved: null,
          missing: "",
        },
      }));
    } finally {
      if (captureControllers.get(run) === controller) captureControllers.delete(run);
    }
  }

  /** One check of the watch *w* (or the one in memory) on the connection in use. */
  async function check(given?: WatchState) {
    const w = given ?? watchRef.current;
    if (!w || !w.active || tickBusy.current || w.capture?.phase === "pending" || reads.currentKey() !== w.key) return;
    tickBusy.current = true;
    const controller = new AbortController();
    tickAbort.current = controller;
    const c = latestConnection.current;
    try {
      const snap = await api<WatchReport>("live/watch", { connection: c }, controller.signal);
      const now = watchRef.current;
      if (controller.signal.aborted || !now || now.run !== w.run || !now.active || reads.currentKey() !== w.key) return;
      const { tripped, note } = judgeWatch(w.rules, snap);
      const fire = !!tripped && now.armed;
      updateWatch(w.run, (x) => ({
        ...x,
        checked: x.checked + 1,
        lastAt: snap.read_at,
        note,
        armed: tripped ? x.armed && !fire : true,
        active: fire && x.rules.stopAfterFirst ? false : x.active,
        capture: fire ? { phase: "pending", message: "Capturing…", startedAt: Date.now(), saved: null, missing: "" } : x.capture,
      }));
      if (fire) void capture(w, snap, tripped, c);
    } catch (e) {
      if (!controller.signal.aborted && reads.currentKey() === w.key) {
        updateWatch(w.run, (x) => ({ ...x, note: `The check at ${clockTime(new Date().toISOString())} failed: ${errorText(e)}` }));
      }
    } finally {
      tickBusy.current = false;
      if (tickAbort.current === controller) tickAbort.current = null;
    }
  }
  const checkRef = useRef(check);
  checkRef.current = check;

  function startWatch() {
    const limit = (on: boolean, value: string, max: number, what: string): number | null | string => {
      if (!on) return null;
      const n = Number(value.trim());
      return Number.isInteger(n) && n >= 1 && n <= max ? n : `Enter a whole number from 1 to ${max.toLocaleString()} for ${what}.`;
    };
    const threads = limit(condThreads, threadsText, 100000, "threads running");
    const lockWait = limit(condLockWait, lockWaitText, 86400, "the row lock wait");
    const statement = limit(condStatement, statementText, 86400, "the statement time");
    const problem = [threads, lockWait, statement].find((v) => typeof v === "string");
    if (typeof problem === "string") {
      setFormError(problem);
      return;
    }
    if (threads === null && lockWait === null && statement === null) {
      setFormError("Choose at least one condition.");
      return;
    }
    setFormError("");
    closePopovers();
    tickAbort.current?.abort();
    tickBusy.current = false;
    const previous = watchRef.current;
    if (previous) captureControllers.get(previous.run)?.abort();
    const next: WatchState = {
      run: ++watchRuns,
      key,
      label: connection.name?.trim() || connection.host || "server",
      rules: {
        threads: threads as number | null,
        lockWait: lockWait as number | null,
        statement: statement as number | null,
        interval: watchInterval,
        stopAfterFirst,
      },
      active: true,
      checked: 0,
      lastAt: "",
      armed: true,
      note: "",
      capture: null,
    };
    onMemory((prev) => ({ ...prev, watch: next }));
    watchRef.current = next;
    if (onScreen && !disabled && !document.hidden) void checkRef.current(next);
  }

  /** Stop checking, and stop a capture on its way (nothing is saved). */
  function stopWatch() {
    const w = watchRef.current;
    if (!w) return;
    tickAbort.current?.abort();
    captureControllers.get(w.run)?.abort();
    updateWatch(w.run, (x) => (x.capture && x.capture.phase !== "pending" ? { ...x, active: false, note: "" } : null));
  }

  // Checks run only while Health is on screen, the page is visible and the
  // Query editor is not capturing; the first one runs at Start.
  const watchEvery = (watch?.rules.interval ?? 10) * 1000;
  const watchRun = watch?.run ?? 0;
  useEffect(() => {
    if (!checking || !onScreen || disabled) return;
    const timer = window.setInterval(() => {
      const view = root.current?.closest<HTMLElement>(".server-view");
      if (!document.hidden && !!view && view.offsetParent !== null) void checkRef.current();
    }, watchEvery);
    return () => window.clearInterval(timer);
  }, [checking, onScreen, disabled, watchEvery, watchRun]);

  async function load(automatic = false) {
    if (automatic && reads.running()) return;
    // An automatic read never replaces another connection's greyed report:
    // it waits for the view's own Refresh. The tick is deferred a full
    // interval, not dropped, so a manual Refresh resumes the schedule.
    if (automatic && memory.report && memory.key !== reads.currentKey()) {
      refreshLoop.current?.deferred();
      return;
    }
    const sequence = ++readSequence.current;
    const readKey = reads.currentKey();
    refreshLoop.current?.started();
    let failed = false;
    const result = await reads.run(
      `health:${interval}`,
      `Sampling the server for ${interval} s…`,
      // with_identity: the server's identity read with the reading, so a
      // baseline is filed under (and compared with) the server it came from.
      (c, signal) => api<HealthReport>("live/health", { connection: c, interval, with_identity: true }, signal),
      (b) => onMemory((previous) => rememberHealth(previous, b)),
      // Explicit reads supersede even the same window, including a read
      // shared with Refresh all. The binding's generation drops its answer.
      { force: !automatic, onError: () => { failed = true; } },
    );
    // An aborted or late read must not release a newer read's scheduler slot.
    if (sequence === readSequence.current && reads.currentKey() === readKey)
      refreshLoop.current?.finished(!!result || !failed);
  }
  const loadRef = useRef(load);
  loadRef.current = load;

  // Observe visibility on the actual view and its ancestors too: the Server
  // place stays mounted while hidden, and narrow navigation can hide a view.
  useEffect(() => {
    const loop = createHealthRefresh({
      every: (auto === -1 ? interval : auto || interval) * 1000,
      enabled: auto !== 0,
      read: () => { void loadRef.current(true); },
      now: () => performance.now(),
      // setInterval, not setTimeout: the tick cancels its own interval on
      // entry and the next one is registered by finished/configure, so the
      // cadence is fixed-rate per interval and test clocks that shorten long
      // intervals (window.setInterval) apply to it.
      later: (fn, ms) => window.setInterval(fn, ms),
      cancel: (id) => window.clearInterval(id),
      backoff: setRetrySeconds,
    });
    refreshLoop.current = loop;
    setRetrySeconds(0);
    if (reads.running()) loop.started();
    const update = () => {
      const state = visibilitySettings.current;
      const view = root.current?.closest<HTMLElement>(".server-view");
      const sheetOpen = !!view?.querySelector('dialog[open], .capture-when-form');
      loop.setVisible(state.onScreen && !state.disabled && !state.baselineSheet && !sheetOpen &&
        document.visibilityState === "visible" && !!view && view.offsetParent !== null &&
        getComputedStyle(view).visibility !== "hidden");
    };
    const observer = new MutationObserver(update);
    // Child changes detect Capture when… opening and closing. Attribute
    // changes catch a hidden place or a modal, without polling the DOM.
    const view = root.current?.closest<HTMLElement>(".server-view");
    if (view) observer.observe(view, { childList: true, subtree: true, attributes: true, attributeFilter: ["open", "hidden", "style", "class"] });
    for (let el: HTMLElement | null = view?.parentElement || null; el; el = el.parentElement)
      observer.observe(el, { attributes: true, attributeFilter: ["hidden", "style", "class"] });
    document.addEventListener("visibilitychange", update);
    window.addEventListener("resize", update);
    updateRefreshVisibility.current = update;
    update();
    return () => {
      loop.dispose();
      observer.disconnect();
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("resize", update);
      if (refreshLoop.current === loop) refreshLoop.current = null;
    };
  }, [key]);
  useEffect(() => {
    refreshLoop.current?.configure((auto === -1 ? interval : auto || interval) * 1000, auto !== 0);
  }, [auto, interval, key]);
  useEffect(() => { updateRefreshVisibility.current(); }, [onScreen, disabled, baselineSheet]);
  useEffect(() => {
    // Refresh all may finish while Health is between automatic reads. Its
    // accepted report starts a full interval without simulating a reopening.
    if (report && memory.key === key && !reads.running()) refreshLoop.current?.finished(true);
  }, [report, memory.key, key]);

  const stale = !!report && memory.key !== key;
  const history = memory.key === key ? memory.history : [];
  const readingServer = readingServerOf(report);
  // A reading from another server than the baseline's is never compared.
  const mismatch = activeBaseline && report && !stale ? baselineMismatch(readingServer, activeBaseline.baseline) : "";
  const comparison = useMemo(
    () => (activeBaseline && report && !stale && !mismatch ? compareHealth(activeBaseline.baseline.payload, report) : null),
    [activeBaseline, report, stale, mismatch],
  );
  const baselineReason = disabled ? "The Query editor is capturing; wait." : !report ? "Load health first."
    : stale ? `From ${memory.label}; refresh to read this server.`
      : !readingServer || readingServer.error !== undefined ? "Refresh first: the server's identity was not read with this reading." : "";
  const serverLabel = connectionLabel(connection);
  const stopComparing = () => onMemory((previous) => (previous.baseline ? { ...previous, baseline: null } : previous));
  const s = report?.summary;
  const d = s?.diagnostics;
  const engine = report ? report.server_product || (report.engine === "mariadb" ? "MariaDB" : "MySQL") : "";
  // Problems first, then watch, then the rest (stable within a status).
  const diagnostics = [...(report?.diagnostics ?? [])].sort((a, b) => toneRank(a.status) - toneRank(b.status));
  const byGroup = report
    ? report.groups
        .map((g) => ({ ...g, metrics: report.metrics.filter((m) => m.group === g.key) }))
        .filter((g) => g.metrics.length)
    : [];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  // Joins without an index, first-table scans and rows read by full scans:
  // the statements Performance Schema counted the same events for.
  const behindOrder = STATEMENTS_BEHIND[selection.key];
  const behind = report && behindOrder && (
    <InspectorSection
      title="Statements behind it"
      info={
        <p>
          The statements Performance Schema counted the same events for, heaviest first. Their counters run since the
          server started or the digest table was truncated, not over this window.
        </p>
      }
    >
      <StatementsBehind
        key={`${selection.key}|${key}`}
        connection={connection}
        disabled={disabled}
        other={stale}
        from={memory.key}
        order={behindOrder}
        settings={settings}
        onDigest={onDigest}
        onOpenTop={onOpenTop}
      />
    </InspectorSection>
  );
  if (report && type === "diag") {
    const x = report.diagnostics?.find((v) => v.key === name);
    if (x) {
      const trend = x.trend;
      const link = behind && x.link?.view === "top" ? null : x.link;
      const hasLesson = x.learn && LESSONS[x.learn] && onOpenLesson;
      inspector = {
        key: selection.key,
        title: x.title,
        meta: (
          <>
            <StatusPill tone={x.status} label={diagnosticLabel(x)} />
            <span>{x.headline}</span>
          </>
        ),
        actions: link || hasLesson ? <Links link={link} learn={x.learn} onOpenView={onOpenView} onOpenLesson={onOpenLesson} /> : undefined,
        body: (
          <>
            <InspectorSection title="Verdict">
              <p className="health-verdict-text">{x.verdict}</p>
            </InspectorSection>
            {comparison && activeBaseline && (() => {
              const then = comparison.diagnostics[x.key];
              const now = diagnosticVerdict(x.status, x.state);
              return (
                <InspectorSection title="Since the baseline">
                  <Fields
                    label={`${x.title}: since the baseline`}
                    rows={[
                      [<bdi key="n">{activeBaseline.baseline.name}</bdi>, !then?.inBaseline ? "Not in the baseline"
                        : `${then.beforeHeadline || "No reading"}${then.beforeVerdict ? ` · ${verdictText(then.beforeVerdict)}` : ""}`],
                      ["Now", `${x.headline}${now ? ` · ${verdictText(now)}` : ""}`],
                    ]}
                  />
                </InspectorSection>
              );
            })()}
            {behind}
            {x.recommendation && (x.status === "watch" || x.status === "problem") && (
              <InspectorSection title="What to do">
                <p className="health-suggestion">{x.recommendation.text}</p>
                <p className="hint health-caveat">Caveat: {x.recommendation.caveat}</p>
              </InspectorSection>
            )}
            {trend && (
              <InspectorSection title={trend.label}>
                <Trend
                  points={series(history, trend.key)}
                  label={trend.label}
                  format={trend.unit === "B/s" ? bytesPerSecond : (v) => count(v)}
                  auto={auto}
                />
              </InspectorSection>
            )}
            <InspectorSection title="Rule">
              <p className="health-rule">{x.rule}</p>
            </InspectorSection>
            <InspectorSection title="Why it matters">
              <p>{x.why}</p>
            </InspectorSection>
            {x.numbers.length > 0 && (
              <InspectorSection title="Numbers">
                <Fields
                  label={`${x.title}: numbers`}
                  rows={x.numbers.map((n) => [
                    <>
                      {isIdentifier(n.label) ? <code>{n.label}</code> : n.label}
                      {n.estimate && <span className="health-estimate">estimate</span>}
                    </>,
                    n.text,
                  ])}
                />
              </InspectorSection>
            )}
            {x.settings.length > 0 && (
              <InspectorSection title="Settings">
                <Fields
                  label={`${x.title}: settings`}
                  rows={x.settings.map((v) => [
                    <code key="n">{v.name}</code>,
                    <>
                      {v.display}
                      {v.default !== null && <span className="hint"> · default {v.default}</span>}
                    </>,
                  ])}
                />
              </InspectorSection>
            )}
            <p className="hint health-sources">From {x.sources.join(", ")}</p>
          </>
        ),
      };
    }
  } else if (report && type === "metric") {
    const m = report.metrics.find((v) => v.key === name);
    if (m) {
      const link = behind && m.link?.view === "top" ? null : m.link;
      const hasLesson = m.learn && LESSONS[m.learn] && onOpenLesson;
      inspector = {
        key: selection.key,
        title: m.label,
        meta: (
          <>
            <StatusPill tone={m.status} />
            <span>{healthText(m)}</span>
          </>
        ),
        actions: link || hasLesson ? <Links link={link} learn={m.learn} onOpenView={onOpenView} onOpenLesson={onOpenLesson} /> : undefined,
        body: (
          <>
            <InspectorSection title="What it measures">
              <p>{m.explain}</p>
            </InspectorSection>
            <InspectorSection title="Why this status">
              <p className={m.status === "watch" || m.status === "problem" ? "health-why-flag" : ""}>{m.why}</p>
            </InspectorSection>
            {comparison && activeBaseline && (() => {
              const then = comparison.metrics[m.key];
              const now = metricVerdict(m.status);
              const before = !then?.inBaseline ? "Not in the baseline" : !then.compared ? "Not compared: the server restarted since"
                : `${then.before === null ? "—" : healthText({ ...m, unit: then.beforeUnit || m.unit }, then.before)}${then.beforeVerdict ? ` · ${verdictText(then.beforeVerdict)}` : ""}`;
              return (
                <InspectorSection title="Since the baseline">
                  <Fields
                    label={`${m.label}: since the baseline`}
                    rows={[[<bdi key="n">{activeBaseline.baseline.name}</bdi>, before], ["Now", `${healthText(m)}${now ? ` · ${verdictText(now)}` : ""}`]]}
                  />
                </InspectorSection>
              );
            })()}
            {m.detail && (
              <InspectorSection title="In this window">
                <p>{m.detail}</p>
              </InspectorSection>
            )}
            {behind}
            {m.sparkline && (
              <InspectorSection title="Trend">
                <Trend points={series(history, m.key)} label={m.label} format={(v) => healthText(m, v)} auto={auto} />
              </InspectorSection>
            )}
            <p className="hint health-sources">From {m.sources.join(", ")}</p>
          </>
        ),
      };
    }
  }

  return (
    <ViewLayout
      id="health"
      title="Health"
      className={`health-panel server-aligned-values${comparison ? " is-comparing" : ""}`}
      onBack={onBack}
      info={
        <>
          <p>
            Two samples of SHOW GLOBAL STATUS a few seconds apart: diagnostics (is the buffer pool full or still
            warming, does the redo log fit the write rate) and the key rates, each with a rule of thumb.
          </p>
          <p>Statuses are rules of thumb, not verdicts. Select a row or a tile for what it measures and its rule. Performance Schema is not needed.</p>
        </>
      }
      controls={
        <>
          <ToolbarSelect
            label="Window"
            className="health-window"
            value={interval}
            disabled={disabled}
            options={HEALTH_INTERVALS.map((v) => [v, `${v} s`])}
            onChange={(v) => onIntervalChange(+v)}
          />
          <ToolbarSelect label="Auto-refresh" className="auto-refresh" value={auto}
            options={[[-1, "While visible"], [0, "Off"], ...AUTO_INTERVALS.map((v): [number, string] => [v, `Every ${v} s`])]}
            onChange={(v) => setAuto(+v)} />
          <Popover
            label="Capture when conditions"
            trigger="Capture when…"
            buttonClassName="button"
            className="health-capture-when"
          >
            <form
              className="capture-when-form"
              noValidate
              onSubmit={(e) => {
                e.preventDefault();
                startWatch();
              }}
            >
              <div className="capture-when-title">
                <strong>Capture when…</strong>
                <span className="hint">
                  Checks only while Health is on screen, then saves the processlist, InnoDB status and locks as an
                  investigation. It only reads.
                </span>
              </div>
              <div className="capture-when-conditions">
                <div className="capture-when-row">
                  <Checkbox checked={condThreads} onChange={edit(setCondThreads)}>Threads running at or above</Checkbox>
                  <NumberField
                    label="Threads running threshold"
                    value={threadsText}
                    min={1}
                    max={100000}
                    disabled={!condThreads}
                    onChange={edit(setThreadsText)}
                  />
                </div>
                <div className="capture-when-row">
                  <Checkbox checked={condLockWait} onChange={edit(setCondLockWait)}>A row lock wait older than</Checkbox>
                  <NumberField
                    label="Row lock wait threshold in seconds"
                    unit="s"
                    value={lockWaitText}
                    min={1}
                    max={86400}
                    disabled={!condLockWait}
                    onChange={edit(setLockWaitText)}
                  />
                </div>
                <div className="capture-when-row">
                  <Checkbox checked={condStatement} onChange={edit(setCondStatement)}>A statement running longer than</Checkbox>
                  <NumberField
                    label="Statement duration threshold in seconds"
                    unit="s"
                    value={statementText}
                    min={1}
                    max={86400}
                    disabled={!condStatement}
                    onChange={edit(setStatementText)}
                  />
                </div>
              </div>
              <div className="capture-when-row">
                <span>Check interval</span>
                <Select
                  label="Check interval"
                  value={watchInterval}
                  options={WATCH_INTERVALS.map((v) => [v, `${v} s`])}
                  onChange={(v) => setWatchInterval(Number(v))}
                />
              </div>
              <Checkbox className="capture-when-stop-first" checked={stopAfterFirst} onChange={setStopAfterFirst}>
                Stop after the first capture
              </Checkbox>
              {formError && (
                <p className="error capture-when-error" role="alert">
                  {formError}
                </p>
              )}
              <div className="capture-when-actions">
                {checking && (
                  <button type="button" className="button" onClick={() => { stopWatch(); closePopovers(); }}>
                    Stop
                  </button>
                )}
                <button
                  type="submit"
                  className="button primary"
                  disabled={disabled || capturePending || (!condThreads && !condLockWait && !condStatement)}
                  title={capturePending ? "A capture is on its way." : disabled ? "The Query editor is capturing; wait." : undefined}
                >
                  {checking ? "Start again" : "Start"}
                </button>
              </div>
            </form>
          </Popover>
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      refreshMenu={
        <BaselineMenu
          saveReason={baselineReason}
          compareReason={baselineReason}
          onSave={() => report && setBaselineSheet({ kind: "save", key, connection: { ...connection }, report })}
          onCompare={() => setBaselineSheet({ kind: "compare", key, connection: { ...connection } })}
        />
      }
      // Refresh stays live while a read is on its way (the button's busy
      // state would swallow the click): pressing it supersedes that read,
      // newest wins. The status line below carries the read's progress.
      loading={false}
      otherConnection={stale ? memory.label : null}
      inspector={inspector}
      inspectorEmpty={<p>Select a diagnostic or a tile to see what it measures, its rule and its numbers.</p>}
      status={
        <>
          <ViewStatus
            loading={reads.loading}
            error={error}
            onStop={reads.stop}
            onRetry={error ? () => void load() : undefined}
            doneAt={reads.doneAt}
            slowAfter={(interval + 20) * 1000}
          />
          <ActionStatus className="baseline-status" state={baselineSaved} onDismiss={baselineSaved.reset} />
          {auto !== 0 && retrySeconds > 0 && (
            <div className="hint health-refresh-backoff" role="status">Automatic refresh backed off: retry in {retrySeconds} s while visible.</div>
          )}
          {activeBaseline && report && !stale && (
            <BaselineBar
              name={activeBaseline.baseline.name}
              savedAt={savedAtText(activeBaseline.baseline.created_at)}
              now={clockTime(report.taken_at)}
              notes={[
                mismatch ? `Not compared: ${mismatch}.` : "",
                comparison?.restarted ? "Not compared: the server restarted since the baseline, so tiles built from counters show no earlier value." : "",
                comparison?.upgraded ? `Upgraded since the baseline: ${comparison.upgraded.from} → ${comparison.upgraded.to}.` : "",
              ]}
              onChange={() => setBaselineSheet({ kind: "compare", key, connection: { ...connection } })}
              onStop={stopComparing}
            />
          )}
          {baselineSheet?.kind === "save" && (
            <SaveBaselineSheet
              kind="health"
              connection={baselineSheet.connection}
              server={serverLabel}
              defaultName={defaultBaselineName(baselineSheet.report.taken_at)}
              report={baselineSheet.report}
              returnFocus={refreshOptions}
              onClose={() => setBaselineSheet(null)}
              onSaved={(item) => {
                setBaselineSheet(null);
                baselineSaved.show("done", `Saved the baseline \u2068${item.name}\u2069.`);
              }}
            />
          )}
          {baselineSheet?.kind === "compare" && (
            <CompareBaselineSheet<HealthBaselinePayload>
              kind="health"
              connection={baselineSheet.connection}
              server={serverLabel}
              current={activeBaseline?.baseline.id}
              reading={readingServer}
              returnFocus={refreshOptions}
              onClose={() => setBaselineSheet(null)}
              onCompare={(baseline) => {
                const chosenOn = baselineSheet.key;
                setBaselineSheet(null);
                baselineSaved.reset();
                onMemory((previous) => ({ ...previous, baseline: { key: chosenOn, baseline } }));
              }}
            />
          )}
          {watch && (checking || watch.capture) && (
            <div className="health-watch-status">
              {checking && (
                <span className="health-watch-line" role="status" aria-live="polite">
                  <span>
                    {onScreen && !disabled ? "Watching" : disabled ? "Watching paused while the Query editor captures" : "Watching paused while Health is off screen"}
                    : checked {watch.checked} {watch.checked === 1 ? "time" : "times"}
                    {watch.lastAt ? `, last at ${clockTime(watch.lastAt)}` : ""}.
                    {!watch.armed && " The next capture waits until the condition clears."}
                  </span>{" "}
                  <button type="button" className="text-button" onClick={stopWatch}>
                    Stop
                  </button>
                </span>
              )}
              {checking && watch.note && <span className="health-watch-note">{watch.note}</span>}
              {watch.capture && (
                <ActionStatus
                  className="health-capture-status"
                  state={{
                    phase: watch.capture.phase,
                    message: watchHere || watch.capture.phase !== "done" ? watch.capture.message : `Saved capture of ${watch.label}.`,
                    startedAt: watch.capture.startedAt,
                  }}
                  onStop={stopWatch}
                  onDismiss={() => updateWatch(watch.run, (x) => (x.active ? { ...x, capture: null } : null))}
                >
                  {watch.capture.saved && onOpenInvestigation && (
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => onOpenInvestigation(watch.capture?.saved?.id || "")}
                    >
                      Open investigation
                    </button>
                  )}
                </ActionStatus>
              )}
              {watch.capture?.missing && (
                <span className="health-watch-note" role="alert">
                  {watch.capture.missing}
                </span>
              )}
            </div>
          )}
        </>
      }
    >
      <div ref={root}>
        {!report && !loading && !error && (
          <EmptyState
            icon="health"
            title="No health sample yet"
            action={
              <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
                Load health
              </button>
            }
          >
            Samples SHOW GLOBAL STATUS twice, {interval} s apart. It only reads.
          </EmptyState>
        )}
        {report && s && (
          <>
            <SummaryLine tone={s.status} notes={report.notes} >
              {/* One count for every check, metrics and diagnostics alike, so
                  the line agrees with its tone; the Diagnostics heading breaks
                  its own share down. */}
              {plural(s.problem + (d?.problem || 0), "problem", "problems")}, {s.watch + (d?.watch || 0)} to watch, {s.ok + (d?.ok || 0)} ok
              <span className="summary-muted health-last">
                {" "}
                · {(report.elapsed_s ?? report.interval).toFixed(1)} s window at {new Date(report.taken_at).toLocaleTimeString()}
              </span>
              {comparison && activeBaseline && (
                <span className="baseline-summary" data-baseline-summary>
                  {" "}
                  · {comparison.changed ? `${plural(comparison.changed, "verdict", "verdicts")} changed` : "No verdict changed"} since{" "}
                  <bdi>{activeBaseline.baseline.name}</bdi>, {savedAtText(activeBaseline.baseline.created_at)}
                </span>
              )}
            </SummaryLine>
            <FactStrip
              label="Server"
              facts={[
                ["Server", `${engine} ${report.server_version || "?"}`],
                ["Host", stale ? memory.label : `${connection.host}:${connection.port}`],
                ["Uptime", uptime(report.uptime_s)],
                ...(report.server.buffer_pool_bytes !== null
                  ? ([["Buffer pool", bytes(report.server.buffer_pool_bytes).join(" ")]] as [string, string][])
                  : []),
                ...(report.server.max_connections !== null
                  ? ([["max_connections", report.server.max_connections.toLocaleString()]] as [string, string][])
                  : []),
                ...(performanceSchema !== null
                  ? ([["Performance Schema", performanceSchema ? "On" : "Off"]] as [string, string][])
                  : []),
              ]}
            />
            {!stale && history.length >= 1 && (
              <section className={`tile-section health-live-charts${history.length < 2 ? " is-pending" : ""}`} aria-labelledby="health-live-h">
                <div className="tile-section-head">
                  <h3 id="health-live-h">Live charts</h3>
                  {history.length < 2 ? (
                    <span className="health-live-charts-note">
                      Collecting…
                    </span>
                  ) : (
                    <span className="health-live-charts-count">
                      Last {history.length} readings
                    </span>
                  )}
                </div>
                <div className="health-live-charts-grid">
                  {LIVE_CHART_SPECS.map((spec) => {
                    const m = report.metrics.find((v) => v.key === spec.key);
                    if (!m) return null;
                    const pts = series(history, spec.key);
                    if (!pts.length) return null;
                    return <LiveTrendChart key={spec.key} chartKey={spec.key} title={spec.title} metric={m} points={pts} />;
                  })}
                </div>
              </section>
            )}
            {diagnostics.length > 0 && (
              <GroupedList
                id="health-diagnostics-h"
                title="Diagnostics"
                className="health-diagnostics"
                aside={
                  d && (
                    <span className="health-diag-count">
                      {plural(d.problem, "problem", "problems")}, {d.watch} to watch, {d.ok} ok
                      {d.info + d.unknown > 0 && `, ${d.info + d.unknown} without a threshold or data`}
                    </span>
                  )
                }
              >
                {diagnostics.map((x) => (
                  <GroupedRow
                    key={x.key}
                    rowKey={`diag:${x.key}`}
                    selection={selection}
                    tone={x.status as HealthStatus}
                    toneLabel={diagnosticLabel(x)}
                    label={x.title}
                    detail={
                      comparison?.diagnostics[x.key]?.verdictChanged ? (
                        <span className="verdict-was" data-verdict-was={comparison.diagnostics[x.key].beforeVerdict || ""}>
                          was {verdictText(comparison.diagnostics[x.key].beforeVerdict)}
                        </span>
                      ) : undefined
                    }
                    value={<span title={x.headline}>{x.headline}</span>}
                    attrs={{ "data-diagnostic": x.key }}
                    className="health-diag"
                    wrap
                  />
                ))}
              </GroupedList>
            )}
            {byGroup.map((group) => (
              <section key={group.key} className="tile-section health-group" aria-labelledby={`health-${group.key}`}>
                <div className="tile-section-head">
                  <h3 id={`health-${group.key}`}>{group.label}</h3>
                  {(() => {
                    // 900 px and below the tiles form a strip that scrolls:
                    // the heading carries the group's share of the summary's
                    // count, a dot plus words, so a tile out of view still adds up.
                    const statuses = group.metrics.map((m) => m.status);
                    const words = groupStatusText(statuses);
                    return words ? (
                      <span className="tile-section-status" data-group-status>
                        <StatusPill tone={statuses.includes("problem") ? "problem" : "watch"} label={words} />
                      </span>
                    ) : null;
                  })()}
                </div>
                <TileGrid label={group.label}>
                  {group.metrics.map((m) => {
                    const [value, unit] = healthValue(m);
                    const points = m.sparkline ? series(history, m.key) : [];
                    return (
                      <MetricTile
                        key={m.key}
                        rowKey={`metric:${m.key}`}
                        selection={selection}
                        label={m.label}
                        value={value}
                        unit={unit}
                        tone={m.status}
                        attrs={{ "data-metric": m.key }}
                        compare={comparison ? baselineLine(m, comparison.metrics[m.key]) : undefined}
                        badge={
                          comparison?.metrics[m.key]?.verdictChanged ? (
                            <span className="tile-badge verdict-was" data-verdict-was={comparison.metrics[m.key].beforeVerdict || ""}>
                              was {verdictText(comparison.metrics[m.key].beforeVerdict)}
                            </span>
                          ) : undefined
                        }
                        trend={
                          points.length > 1 ? (
                            <Sparkline points={points} label={m.label} format={(v) => healthText(m, v)} className="health-spark" />
                          ) : undefined
                        }
                      />
                    );
                  })}
                </TileGrid>
              </section>
            ))}
          </>
        )}
      </div>
    </ViewLayout>
  );
}
