// Server > Health: two SHOW GLOBAL STATUS samples a few seconds apart
// (health-1.0 from myflames/health.py). One summary line and the server facts,
// the diagnostics as one-line rows sorted by severity, then compact metric
// tiles by group. Every explanation (what it measures, the rule, the numbers,
// the settings, the suggestion) opens in the inspector. The sample history for
// the sparklines lives in memory only; the view and the sampling window are
// restored with the workspace.
import { useEffect, useRef, useState } from "react";
import { useOpenRead } from "./viewOnScreen";
import {
  api,
  type Connection,
  type HealthDiagnostic,
  type HealthMetric,
  type HealthReport,
  type HealthStatus,
  type TopOrder,
  type TopStatement,
} from "./api";
import { HEALTH_INTERVALS, type ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { StatementsBehind, type DigestAction, type LabSettings, type OpenTop } from "./SlowQueries";
import {
  AutoRefresh,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  MetricTile,
  Sparkline,
  StatusPill,
  SummaryLine,
  TileGrid,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  isIdentifier,
  toneRank,
  type InspectorContent,
  type Selection,
  type TrendPoint,
} from "./kit";
import "./health.css";

/** Automatic refresh choices (seconds); off by default. */
const AUTO_INTERVALS = [10, 30, 60];
/** Samples kept for the sparklines. */
export const HISTORY_LIMIT = 20;

export type HealthSample = {
  at: string;
  values: Record<string, number | null>;
};
/** What Health keeps while the Server place is open (never saved): the last
 * sample and the history of the connection it was read from (binding.ts). */
export type HealthMemory = {
  key: string;
  label: string;
  report: HealthReport | null;
  history: HealthSample[];
};
export const emptyHealth: HealthMemory = { key: "", label: "", report: null, history: [] };

/** Keep a new sample (from Health's Refresh or from Refresh all): the
 * report, and its key rates appended to the history of its connection. The
 * same answer twice (one request shared by both) is kept once. */
export function rememberHealth(previous: HealthMemory, { key, label, report }: Bound<HealthReport>): HealthMemory {
  if (previous.key === key && previous.report === report) return previous;
  const values: Record<string, number | null> = Object.fromEntries(
    report.metrics.filter((m) => m.sparkline).map((m) => [m.key, m.value]),
  );
  for (const d of report.diagnostics ?? []) if (d.trend) values[d.trend.key] = d.trend.value;
  const sample: HealthSample = { at: report.taken_at, values };
  const history = previous.key === key ? previous.history : [];
  return { key, label, report, history: [...history, sample].slice(-HISTORY_LIMIT) };
}

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
  return [count(value), unit];
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

function Trend({ points, label, format }: { points: TrendPoint[]; label: string; format: (v: number) => string }) {
  if (points.length < 2) return <p className="hint health-trend-empty">Trend appears after the next refresh.</p>;
  return <Sparkline points={points} label={label} format={format} width={240} height={40} className="is-large health-spark" />;
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
}) {
  const [auto, setAuto] = useState(0);
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const root = useRef<HTMLDivElement>(null);
  const report = memory.report;
  const key = reads.key;

  async function load() {
    await reads.run(
      `health:${interval}`,
      `Sampling the server for ${interval} s…`,
      (c, signal) => api<HealthReport>("live/health", { connection: c, interval }, signal),
      (b) => onMemory((previous) => rememberHealth(previous, b)),
      { onError: () => setAuto(0) },
    );
  }
  const loadRef = useRef(load);
  loadRef.current = load;

  // Automatic refresh is opt-in and skips ticks while a sample is running,
  // the page is hidden, or the view is not on screen.
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => {
      const view = root.current?.closest<HTMLElement>(".server-view");
      const visible = !!view && view.offsetParent !== null;
      if (!document.hidden && visible && !disabled && !reads.running()) loadRef.current();
    }, auto * 1000);
    return () => window.clearInterval(timer);
  }, [auto, disabled]);

  useOpenRead({ key: reads.key, reportKey: memory.report ? memory.key : null, ready: !disabled, busy: !!loading || reads.running(), read: () => { void loadRef.current(); } });

  const stale = !!report && memory.key !== key;
  const history = memory.key === key ? memory.history : [];
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
            {m.detail && (
              <InspectorSection title="In this window">
                <p>{m.detail}</p>
              </InspectorSection>
            )}
            {behind}
            {m.sparkline && (
              <InspectorSection title="Trend">
                <Trend points={series(history, m.key)} label={m.label} format={(v) => healthText(m, v)} />
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
      className="health-panel server-aligned-values"
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
            disabled={loading}
            options={HEALTH_INTERVALS.map((v) => [v, `${v} s`])}
            onChange={(v) => onIntervalChange(+v)}
          />
          <AutoRefresh value={auto} options={AUTO_INTERVALS} disabled={!report} onChange={setAuto} />
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={loading}
      otherConnection={stale ? memory.label : null}
      inspector={inspector}
      inspectorEmpty={<p>Select a diagnostic or a tile to see what it measures, its rule and its numbers.</p>}
      status={<ViewStatus loading={reads.loading} error={error} onStop={reads.stop} onRetry={error ? () => void load() : undefined}
        doneAt={reads.doneAt} slowAfter={(interval + 20) * 1000} />}
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
