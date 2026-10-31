import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  api, formatNumber, number, readDigestHistogram, readDigestTables, time, type Connection, type DigestHistogram, type DigestTables,
  type TopCoverage, type TopOrder, type TopReport, type TopStatement, type TopTotals,
} from "./api";
import { connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import {
  ActionButton, ActionStatus, ConfirmSheet, DataTable, Disclosure, EmptyState, ExportMenu, Fields, IDLE_ACTION, InfoPopover,
  InspectorContext, InspectorSection, Pill, SearchField, Select, SummaryLine, ToolbarButton, ViewLayout, ViewStatus,
  copyToClipboard, errorText, storedInspectorWidth, useWindowStatus, type ActionState, type Column, type InspectorContent,
  type InspectorShared,
} from "./kit";
import { CompactNumber, bytes, compact, count } from "./PerfShared";
import { durationText } from "./duration";
import { csvCell } from "./reportTableExport";
import { engineName } from "./OpsShared";
import { InstrumentsLink, InstrumentsLinkContext } from "./Instruments";
import { binLow, boundText, latencyBins, latencySummary, rangeText, BINS_PER_DECADE, type LatencyBins } from "./latencyHistogram";
import { ANY_TIME, inputValue, overlaps, rangeActive, rangeText as timeRangeText, wallText, type TimeRange } from "./digestTimes";
import { TimeRangeButton } from "./DigestRange";
import {
  chooseSlowLog, openSlowLogPath, progressId, readSlowLog, readSlowLogTable, slowLogProgress, stopSlowLog,
  type SlowLogFile, type SlowLogOrder, type SlowLogProgress, type SlowLogQuery, type SlowLogReport, type SlowLogStatement,
} from "./slowLogApi";
import "./query-digest.css";

export type DigestFilterMode = "time" | "scans" | "full_join" | "tmp_tables" | "locks";
export type QueryDigestProps = {
  connection: Connection;
  onOpenInEditor?: (row: TopStatement, rank: number, from: string) => void;
  onExplain?: (row: TopStatement, rank: number, from: string) => void;
  /** Opens Server > Instruments at one row (the CPU time note's link). */
  onOpenInstruments?: (row: string) => void;
  onOpenIndexes?: (schema: string, table: string, from: string) => void;
};
const MODES: [DigestFilterMode, string][] = [
  ["time", "Total time"], ["scans", "Full scans"], ["full_join", "Joins without an index"],
  ["tmp_tables", "Temp tables on disk"], ["locks", "Lock time"],
];
/** Where the statements come from: Performance Schema statement digests,
 * the mysql.slow_log table of the connection in use, or a slow query log
 * file on this computer (no server). */
export type DigestSource = "pfs" | "table" | "file";
const SOURCES: [DigestSource, string][] = [["pfs", "Statement digests"], ["table", "Slow log table"], ["file", "Slow log file"]];
const SLOW_ORDERS: [SlowLogOrder, string][] = [
  ["total", "Total time"], ["count", "Calls"], ["avg", "Average time"], ["max", "Slowest call"], ["p95", "P95"],
  ["lock", "Lock time"], ["examined", "Rows examined"], ["sent", "Rows sent"],
];
const DIGEST_TABLE = "performance_schema.events_statements_summary_by_digest";
type DigestReport = {
  /** pfs for Performance Schema statement digests; table and file for slow-log-1.0. */
  kind: DigestSource;
  /** The slow-log-1.0 report of the table and file sources. */
  slow?: SlowLogReport;
  statements: TopStatement[];
  engine: string;
  version: string;
  server_product?: string | null;
  source?: string;
  filter: DigestFilterMode;
  coverage?: TopCoverage;
  /** Performance Schema rankings only: the whole server, the digests the
   * list does not hold, and the CPU consumer (see TopReport). */
  totals?: TopTotals | null;
  others?: TopTotals | null;
  cpu?: { collected: boolean | null } | null;
  notes: string[];
};
type DigestRow = { key: string; rank: number; statement: TopStatement };
/** The Schema choice for statements recorded without a default schema (no
 * database name can contain NUL). */
const NO_SCHEMA = "\u0000none";

/** A statement latency as Top statements writes it ("59.04 ms", "1.20 s"),
 * with the exact milliseconds on hover. Columns sort on the raw value. */
function Ms({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined || !Number.isFinite(value)) return <>—</>;
  return <span title={`${formatNumber(value, { maximumFractionDigits: 20 })} ms`}>{time(value)}</span>;
}

function ratio(examined: number, sent: number) {
  if (sent <= 0) return examined > 0 ? `${number(examined)}:0` : "—";
  return `${count(examined / sent)}:1`;
}
function normalized(row: TopStatement) { return row.digest_text || "No normalized SQL recorded"; }
function slowOf(row: TopStatement) { return row as unknown as SlowLogStatement; }
/** The report's source in a few words: "Slow log file: host-slow.log". */
function sourceOf(report: DigestReport) {
  if (report.kind === "file" && report.slow?.source.kind === "file") return `Slow log file: ${report.slow.source.name}`;
  if (report.kind === "table") return "Slow log table (mysql.slow_log)";
  return "Performance Schema statement digests";
}
function sourceTable(report: DigestReport) {
  if (report.kind === "file") return "slow log file";
  return report.kind === "table" ? "mysql.slow_log" : DIGEST_TABLE;
}
/** A slow-log-1.0 report as the view's report. */
function fromSlow(res: SlowLogReport, kind: "table" | "file"): DigestReport {
  return {
    kind, slow: res, statements: res.statements as unknown as TopStatement[], engine: res.engine || "",
    version: res.server_version || "", server_product: res.server_product, source: kind === "table" ? "mysql.slow_log" : "slow_log_file",
    filter: "time", notes: res.notes || [], totals: null, others: null, cpu: null,
  };
}
/** Times in a slow log report: the clock's short name ("UTC"). */
function clockLabel(report: DigestReport | null | undefined) {
  if (!report) return "in the source's own clock";
  if (report.kind === "pfs") return "the server's clock (FIRST_SEEN and LAST_SEEN)";
  return report.slow?.clock.label || "as the log writes them";
}
/** The ⓘ sentence for the report on screen: which table was read and the
 * period its counters cover. The summary line keeps only "since … ago". */
function sourceSentence(report: DigestReport): ReactNode {
  const slow = report.slow;
  if (slow) {
    const span = slow.span.first ? ` from ${wallText(slow.span.first, true)} to ${wallText(slow.span.last, true)}` : "";
    const what = slow.source.kind === "file"
      ? <>This report reads the slow log file <code>{slow.source.name}</code> ({bytes(slow.source.size_bytes)}): {number(slow.parse.entries)} entries{span}.</>
      : <>This report reads <code>mysql.slow_log</code> of the connection in use: {number(slow.source.rows_read)} rows{slow.range.applied ? " in the range" : ""}{slow.source.rows != null ? ` of ${number(slow.source.rows)}` : ""}.</>;
    return <>{what} {slow.clock.text} {slow.time_meaning.text} Statements are grouped by a normalized fingerprint (literals as ?, lists of values as (...)).</>;
  }
  const age = report.coverage?.uptime_s;
  return <>This report reads <code>{DIGEST_TABLE}</code>: Performance Schema statement digests counted since the server started{age != null ? ` ${durationText(age)} ago` : ""} or the digest table was last reset.</>;
}
function periodOf(report: DigestReport) {
  if (report.slow) {
    const span = report.slow.span;
    return span.first ? `entries from ${span.first} to ${span.last} (${report.slow.clock.label || "as written"})` : "entries without times";
  }
  const age = report.coverage?.uptime_s;
  return `counted since the server started${age != null ? ` ${durationText(age)} ago` : ""} or the digest table was last reset`;
}
function reportNotes(report: DigestReport): string[] {
  const notes: string[] = [];
  const lost = report.coverage?.digest_lost;
  if (lost != null && lost > 0) notes.push(`${number(lost)} statements were not counted: the digest table was full.`);
  const overflow = report.coverage?.overflow;
  if (overflow) notes.push(`${number(overflow.executions)} executions (${time(overflow.total_ms)}) are in the digest table's overflow row, outside this list.`);
  if (report.slow) {
    const slow = report.slow;
    notes.push(slow.clock.text, slow.time_meaning.text);
    for (const problem of slow.parse.problems.slice(0, 5)) notes.push(problem.line != null ? `Line ${number(problem.line)}: ${problem.reason}` : problem.reason);
    if (slow.parse.problems.length > 5) notes.push(`${number(slow.parse.malformed - 5)} more parts of the log could not be read.`);
  }
  // Keep the server's access, instrumentation and truncation notes, including failed slow-log reads.
  return [...new Set([...notes, ...report.notes])];
}
function downloadFile(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function CodeBlock({ text, label }: { text: string; label: string }) {
  const [feedback, setFeedback] = useState("");
  return <div className="qd-code">
    <pre tabIndex={0} aria-label={label}><code>{text}</code></pre>
    <button type="button" className="text-button" aria-label={`Copy ${label}`} onClick={() => {
      void copyToClipboard(text).then((ok) => setFeedback(ok ? "Copied." : "Select the text to copy it."));
    }}>Copy</button>
    {feedback && <span className="muted" role="status">{feedback}</span>}
  </div>;
}

/** The digest hash on one line, middle-truncated with the first and last
 * characters kept; the full value is on hover and Copy copies it. */
function DigestValue({ digest }: { digest: string }) {
  const [feedback, setFeedback] = useState("");
  const short = digest.length > 34 ? `${digest.slice(0, 16)}…${digest.slice(-8)}` : digest;
  return <span className="qd-digest">
    <code title={digest}>{short}</code>
    <button type="button" className="text-button" onClick={() => {
      void copyToClipboard(digest).then((ok) => setFeedback(ok ? "Copied." : "Select the text to copy it."));
    }}>Copy</button>
    {feedback && <span className="muted" role="status">{feedback}</span>}
  </span>;
}

/** A FIRST_SEEN or LAST_SEEN value written as a short date and time. The
 * server sends its own wall-clock time without a time zone, so the digits are
 * kept as they are (never shifted to this computer's zone) and the hover says
 * it is the server's clock, with the exact value. */
function SeenAt({ value, clock = "Server time" }: { value: string | null; clock?: string }) {
  if (!value) return <>—</>;
  // Built and written in UTC, so no local zone rule (a daylight saving gap)
  // can move the server's digits.
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(value);
  const at = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0))) : null;
  if (!at || Number.isNaN(at.getTime())) return <>{value}</>;
  return <time dateTime={value} title={`${clock}: ${value}`}>
    {at.toLocaleString(undefined, { timeZone: "UTC", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" })}
  </time>;
}

/** A share as the Share column writes it: "4.2%", "<0.1%" for a sliver. */
function percent(part: number, whole: number) {
  if (!(whole > 0)) return "—";
  const value = Math.max(0, Math.min(100, part / whole * 100));
  return value > 0 && value < 0.05 ? "<0.1%" : `${value.toFixed(1)}%`;
}

/** First seen in the last 24 hours (decided on the server, and only while the
 * counters cover more than a day). */
function NewBadge({ statement }: { statement: TopStatement }) {
  const seen = (statement.first_seen || "").replace(/\.\d+$/, "");
  return <Pill tone="info" title={seen ? `First seen ${seen}` : "First seen in the last 24 hours"}>New</Pill>;
}

/** CPU time of a digest (MySQL with SUM_CPU_TIME). The column grows only
 * while the events_statements_cpu consumer is on, which it is not by default. */
function CpuTime({ statement, collected }: { statement: TopStatement; collected: boolean | null }) {
  if (collected === false) {
    return <span className="muted" title="events_statements_cpu consumer is off">Not collected</span>;
  }
  if (collected === null && !statement.cpu_ms) {
    return <span className="muted" title="Could not read the events_statements_cpu consumer">Unknown</span>;
  }
  return <Ms value={statement.cpu_ms} />;
}

/** Whether *ref* is on screen (an inspector section scrolled into view, in a
 * place that is shown). Without IntersectionObserver, always. */
function useOnScreen(ref: RefObject<HTMLElement>) {
  const [visible, setVisible] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver((entries) => setVisible(entries.some((e) => e.isIntersecting)));
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return visible;
}

/** The latency histogram: one bar per tenth of a decade on a log time axis,
 * counts as bar heights, each bar's range and calls on hover, and one
 * sentence that says the same for screen readers. */
function LatencyChart({ data, maxMs }: { data: LatencyBins; maxMs: number | null | undefined }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(280);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => { if (el.clientWidth > 0) setWidth(Math.floor(el.clientWidth)); };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const summary = latencySummary(data, maxMs);
  const n = data.bins.length;
  const height = 76;
  const axis = 54;
  const step = n ? width / n : width;
  // Thin bars, centred in their slot, with a surface gap between neighbours.
  const barWidth = Math.max(1, Math.min(16, step - 2));
  const inset = (step - barWidth) / 2;
  // Decade ticks (10 µs, 100 µs, 1 ms, ...) on every bin edge that starts a
  // decade, the right end included; every other label when crowded.
  const start = data.bins.length ? data.bins[0].index : 0;
  const decades: { index: number; x: number }[] = [];
  for (let index = Math.max(0, start); index <= start + n; index++) {
    if (index % BINS_PER_DECADE === 0) decades.push({ index, x: (index - start) * step });
  }
  const every = step * BINS_PER_DECADE < 44 ? 2 : 1;
  const bar = (x: number, h: number) => {
    const r = Math.min(2, barWidth / 2, h);
    const y = axis - h;
    return `M${x} ${axis}V${y + r}Q${x} ${y} ${x + r} ${y}H${x + barWidth - r}Q${x + barWidth} ${y} ${x + barWidth} ${y + r}V${axis}Z`;
  };
  return <figure className="qd-hist">
    <div ref={box} className="qd-hist-plot">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Latency histogram. ${summary}`}>
        {data.bins.map((b, i) => b.count > 0 &&
          <path key={`bar${b.index}`} className="qd-hist-bar" d={bar(i * step + inset, Math.max(2, b.count / data.peak * (axis - 4)))} />)}
        <line className="qd-hist-axis" x1={0} x2={width} y1={axis + 0.5} y2={axis + 0.5} />
        {decades.map(({ index, x }, k) => {
          const label = k % every === 0;
          const anchor = x < 20 ? "start" : x > width - 20 ? "end" : "middle";
          const at = Math.min(Math.max(x, 0.5), width - 0.5);
          return <g key={`tick${index}`}>
            <line className="qd-hist-tick" x1={at} x2={at} y1={axis} y2={axis + 4} />
            {label && <text className="qd-hist-label" x={at} y={axis + 17} textAnchor={anchor}>{boundText(binLow(index))}</text>}
          </g>;
        })}
        {data.bins.map((b, i) => b.count > 0 &&
          <rect key={`hit${b.index}`} className="qd-hist-hit" x={i * step} y={0} width={step} height={axis}>
            <title>{`${rangeText(b.low_ms, b.high_ms)}: ${number(b.count)} ${b.count === 1 ? "call" : "calls"} (${percent(b.count, data.total)})`}</title>
          </rect>)}
      </svg>
    </div>
    <figcaption className="qd-hist-summary">{summary}</figcaption>
    <Disclosure title="Buckets" className="qd-hist-buckets">
      <table className="qd-hist-table">
        <thead><tr><th scope="col">Time</th><th scope="col" className="num">Calls</th><th scope="col" className="num">Share</th></tr></thead>
        <tbody>{data.bins.filter((b) => b.count > 0).map((b) => <tr key={b.index}>
          <td>{rangeText(b.low_ms, b.high_ms)}</td><td className="num">{number(b.count)}</td><td className="num">{percent(b.count, data.total)}</td>
        </tr>)}</tbody>
      </table>
    </Disclosure>
  </figure>;
}

type HistogramRead = { phase: "loading" | "done" | "failed"; result?: DigestHistogram; error?: string };

/** P95, P99 and P99.9 from the digest row, then the histogram, read when
 * this section is on screen (once per statement and report). */
function LatencySection({ statement, read, enabled, blocked, onRead, onStop }: {
  statement: TopStatement;
  read: HistogramRead | undefined;
  /** The report is the connection in use's and its view is on screen. */
  enabled: boolean;
  /** Why the histogram is not read (another connection's report), or "". */
  blocked: string;
  onRead: () => void;
  onStop: () => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const visible = useOnScreen(root);
  useEffect(() => {
    if (visible && enabled && !blocked && !read) onRead();
  }, [visible, enabled, blocked, read]);
  const data = read?.result?.available ? latencyBins(read.result.buckets) : null;
  return <div ref={root} className="qd-latency">
    {/* Spans: a plain "P99.9" label would read as an identifier (code). */}
    <Fields label="Latency percentiles" rows={[
      [<span>P95</span>, <Ms value={statement.p95_ms} />],
      [<span>P99</span>, <Ms value={statement.p99_ms} />],
      [<span>P99.9</span>, <Ms value={statement.p999_ms} />],
    ]} />
    {blocked ? <p className="muted qd-hist-note">{blocked}</p> : <>
      <ViewStatus loading={read?.phase === "loading" ? "Reading the latency histogram…" : ""} error={read?.phase === "failed" ? read.error : ""}
        onStop={onStop} onRetry={onRead} slowAfter={10_000} />
      {read?.phase === "done" && read.result && !read.result.available && <p className="muted qd-hist-note">{read.result.reason || "This server keeps no latency histogram."}</p>}
      {data && (data.total ? <LatencyChart data={data} maxMs={statement.max_ms} /> : <p className="muted qd-hist-note">No calls were timed.</p>)}
    </>}
  </div>;
}

type TablesRead = { phase: "loading" | "done" | "failed"; result?: DigestTables; error?: string };

/** The section may sit below the inspector's fold. Opening the inspector
 * alone must not read metadata; leaving it also cancels pending reads. */
function TablesSection({ read, enabled, blocked, onRead, onStop, onLeave, onOpenIndexes }: {
  read: TablesRead | undefined;
  enabled: boolean;
  blocked: string;
  onRead: () => void;
  onStop: () => void;
  onLeave: () => void;
  onOpenIndexes?: (schema: string, table: string) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const visible = useOnScreen(root);
  useEffect(() => {
    if (visible && enabled && !blocked && !read) onRead();
  }, [visible, enabled, blocked, read]);
  useEffect(() => {
    if (!visible || !enabled) onLeave();
    return () => onLeave();
  }, [visible, enabled]);
  return <div ref={root} className="qd-tables">
    <p className="qd-tables-note">Found in the statement text</p>
    {blocked ? <p className="muted">{blocked}</p> : <>
      <ViewStatus loading={read?.phase === "loading" ? "Reading table metadata…" : ""}
        error={read?.phase === "failed" ? read.error : ""} onStop={onStop} onRetry={onRead} slowAfter={10_000} />
      {read?.result && <>
        {read.result.tables.length === 0 && <p className="muted">No table names found.</p>}
        <ul className="qd-table-list" aria-label="Tables found in the statement text">
          {read.result.tables.map((table) => <li key={JSON.stringify([table.schema, table.name])}>
            <p className="qd-table-name"><code>{table.schema && <span className="muted">{table.schema}.</span>}{table.name}</code>
              <span className="muted">{table.found ? [table.type, table.engine].filter(Boolean).join(" · ") : "Metadata unavailable"}</span></p>
            {table.found && <>
              <Fields rows={[
                [<span title="TABLE_ROWS from information_schema.TABLES is an estimate. No rows were counted.">Rows (estimate)</span>, <CompactNumber value={table.rows_estimate} />],
                ["Data", bytes(table.data_bytes)], ["Indexes", bytes(table.index_bytes)],
              ]} />
              {table.indexes.length > 0 ? <ul className="qd-index-list" aria-label={`Indexes of ${table.name}`}>
                {table.indexes.map((index) => <li key={index.name}><code>{index.name} ({index.columns.join(", ")})</code>
                  {index.unique && <span className="muted">unique</span>}{index.visible === false && <span className="muted">invisible</span>}</li>)}
              </ul> : <p className="muted">{table.type?.includes("view") ? "Views have no indexes of their own." : "No indexes listed."}</p>}
              {table.type === "table" && table.schema && onOpenIndexes &&
                <button type="button" className="text-button" disabled={!enabled} onClick={() => onOpenIndexes(table.schema!, table.name)}>Open in Indexes</button>}
            </>}
          </li>)}
        </ul>
        {read.result.notes.map((note, i) => <p key={i} className="qd-tables-note">{note}</p>)}
      </>}
    </>}
  </div>;
}

/** Latency of a slow log statement: the percentiles and the histogram the
 * report already holds (Performance Schema's buckets, counted from the log:
 * no read). */
function SlowLatency({ statement }: { statement: SlowLogStatement }) {
  const data = latencyBins(statement.histogram);
  return <div className="qd-latency">
    <Fields label="Latency percentiles" rows={[
      [<span>Min</span>, <Ms value={statement.min_ms} />],
      [<span>P95</span>, <Ms value={statement.p95_ms} />],
      [<span>P99</span>, <Ms value={statement.p99_ms} />],
      [<span>P99.9</span>, <Ms value={statement.p999_ms} />],
    ]} />
    {data.total ? <LatencyChart data={data} maxMs={statement.max_ms} /> : <p className="muted qd-hist-note">No calls were timed.</p>}
  </div>;
}

/** "app (120), report (3)": who ran a statement, the busiest first. */
function namesText(names: { name: string; count: number }[], empty: string) {
  if (!names.length) return empty;
  return names.map((n) => `${n.name || "(none)"} (${number(n.count)})`).join(", ");
}

type FileRead = { loading: string; error: string; doneAt?: number; progress: SlowLogProgress | null; stopped?: string };
const FILE_IDLE: FileRead = { loading: "", error: "", progress: null };

export function QueryDigestView({ connection, onOpenInEditor, onExplain, onOpenInstruments, onOpenIndexes }: QueryDigestProps) {
  const [source, setSource] = useState<DigestSource>("pfs");
  // A reply that lands after the Source changed keeps its report but never
  // touches the selection or filters of the Source on screen.
  const sourceNow = useRef(source);
  sourceNow.current = source;
  const [filter, setFilter] = useState<DigestFilterMode>("time");
  const [slowOrder, setSlowOrder] = useState<SlowLogOrder>("total");
  /** From and To in the report's own clock (Performance Schema: a filter on
   * the loaded rows; the slow log sources: part of the read). */
  const [range, setRange] = useState<TimeRange>(ANY_TIME);
  /** The slow log sources' user filter ("" for every user; part of the read). */
  const [runBy, setRunBy] = useState("");
  const [search, setSearch] = useState("");
  /** The schema shown: "" for all, NO_SCHEMA, or a name (filters the loaded rows; never a read). */
  const [schemaFilter, setSchemaFilter] = useState("");
  const [limit, setLimit] = useState(50);
  const reads = useConnectionReads(connection);
  const [bound, setBound] = useState<Bound<DigestReport> | null>(null);
  const boundKey = useRef("");
  boundKey.current = bound?.key || "";
  // The slow log file: chosen on this computer, read without a server.
  const [file, setFile] = useState<SlowLogFile | null>(null);
  const [fileReport, setFileReport] = useState<DigestReport | null>(null);
  const [fileRead, setFileRead] = useState<FileRead>(FILE_IDLE);
  const fileCtl = useRef<{ controller: AbortController; id: string } | null>(null);
  const [choosing, setChoosing] = useState<ActionState>(IDLE_ACTION);
  const [pathShown, setPathShown] = useState(false);
  const [path, setPath] = useState("");
  const [pathError, setPathError] = useState("");
  const [opening, setOpening] = useState(false);
  const pathId = useId();
  const pathInput = useRef<HTMLInputElement>(null);
  const isFile = source === "file";
  // Statement digests and the slow log table share one read slot: a report
  // is shown only under the Source it was read for, never under the other.
  const sourceBound = !isFile && bound?.report.kind === source ? bound : null;
  const report = isFile ? fileReport : sourceBound?.report;
  const other = sourceBound && sourceBound.key !== reads.key ? sourceBound.label : null;
  const actionsOff = isFile ? !fileReport : !sourceBound || !!other;
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
  const root = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const measure = () => { if (el.clientWidth > 0) setWidth(el.clientWidth); };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const slow = report?.slow || null;
  const rows = useMemo<DigestRow[]>(() => (report?.statements || []).map((statement, i) => ({
    statement, rank: i + 1,
    // A digest may appear in multiple schemas; slow-log entries have synthetic ids.
    key: JSON.stringify([statement.schema, statement.digest || slowOf(statement).fingerprint_id || null, statement.digest_text, i]),
  })), [report]);
  // The schemas of the loaded statements, for the Schema choice; a chosen one
  // a newer read no longer has stays listed (it shows no rows).
  const schemaOptions = useMemo<[string, string][]>(() => {
    const names = [...new Set(rows.map(({ statement: s }) => s.schema || "").filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    if (schemaFilter && schemaFilter !== NO_SCHEMA && !names.includes(schemaFilter)) names.unshift(schemaFilter);
    const none = schemaFilter === NO_SCHEMA || rows.some(({ statement: s }) => !s.schema);
    return [["", "All schemas"], ...names.map((name): [string, string] => [name, name]), ...(none ? [[NO_SCHEMA, "No default schema"] as [string, string]] : [])];
  }, [rows, schemaFilter]);
  // Performance Schema keeps one total per statement: the range keeps the
  // statements seen in it (first seen before To, last seen after From).
  const pfsRange = report?.kind === "pfs" && rangeActive(range);
  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return rows.filter(({ statement: s }) => (!schemaFilter || (schemaFilter === NO_SCHEMA ? !s.schema : s.schema === schemaFilter)) &&
      (!pfsRange || overlaps(s.first_seen, s.last_seen, range)) &&
      (!needle || [s.digest_text, s.sample_text, s.schema, s.digest].some((text) => text?.toLowerCase().includes(needle))));
  }, [rows, search, schemaFilter, pfsRange, range]);
  const filtered = !!search.trim() || !!schemaFilter || pfsRange;
  const selected = shown.find((r) => r.key === selectedKey) || null;
  const shared = useMemo<InspectorShared>(() => ({
    open: inspectorOpen && !!selected, setOpen: setInspectorOpen,
    width: inspectorWidth, setWidth: setInspectorWidth, sheet: width < 1040,
  }), [inspectorOpen, selected, inspectorWidth, width]);
  const totals = useMemo(() => rows.reduce((sum, { statement: s }) => ({
    ms: sum.ms + s.total_ms, calls: sum.calls + s.count,
  }), { ms: 0, calls: 0 }), [rows]);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportNote, setExportNote] = useState("");
  // Freeze the preview and its origin while open, even if a read finishes behind it.
  const [preview, setPreview] = useState({ text: "", filename: "" });
  const slowQuery: SlowLogQuery = { order: slowOrder, limit, from: range.from, to: range.to, user: runBy };
  const params = source === "table"
    ? `query-digest:table:${slowOrder}:${limit}:${range.from}:${range.to}:${runBy}`
    : `query-digest:${filter}:${limit}:1:${connection.database || ""}`;
  const load = () => {
    if (isFile) { void readFile(); return; }
    return reads.run(params, source === "table" ? "Reading mysql.slow_log…" : "Reading statement digests…",
      async (c, signal): Promise<DigestReport> => {
        if (source === "table") return fromSlow(await readSlowLogTable(c, slowQuery, signal), "table");
        const res = await api<TopReport>("live/top", { connection: c, order: filter as TopOrder, limit, extra_metrics: true }, signal);
        return {
          kind: "pfs", statements: res.statements || [], engine: res.engine, version: res.server_version,
          server_product: res.server_product,
          coverage: "coverage" in res ? res.coverage : undefined, notes: res.notes || [], filter,
          totals: "totals" in res ? res.totals : undefined, others: "others" in res ? res.others : undefined,
          cpu: "cpu" in res ? res.cpu : undefined,
        };
      }, (result) => {
        // Another connection's databases differ: its statements start with All schemas.
        const here = sourceNow.current === result.report.kind;
        if (here && result.key !== boundKey.current) setSchemaFilter("");
        setBound(result);
        if (here) { setSelectedKey(""); setInspectorOpen(false); }
      });
  };
  const onScreen = useViewOnScreen();
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !isFile, busy: !!reads.loading || reads.running(), read: () => { void load(); } });
  const choices = useRef(params);
  useEffect(() => {
    if (!isFile && onScreen && choices.current !== params) { choices.current = params; void load(); }
  }, [params, onScreen, isFile]);
  // The file is read again when its choices change, only while on screen.
  const fileParams = file ? JSON.stringify([file.file_id, slowOrder, limit, range.from, range.to, runBy]) : "";
  const fileChoices = useRef("");
  useEffect(() => {
    if (isFile && onScreen && fileParams && fileChoices.current !== fileParams) { fileChoices.current = fileParams; void readFile(); }
  }, [fileParams, onScreen, isFile]);
  useEffect(() => () => stopFile(true), []);
  /** Read the chosen file into a report, following its progress. */
  async function readFile(target = file) {
    if (!target) return;
    stopFile(true);
    const controller = new AbortController();
    const id = progressId();
    fileCtl.current = { controller, id };
    setFileRead({ loading: `Reading ${target.name}…`, error: "", progress: null });
    const poll = window.setInterval(() => {
      slowLogProgress(id, controller.signal).then((p) => {
        if (fileCtl.current?.id === id && p.phase === "reading") setFileRead((r) => (r.loading ? { ...r, progress: p } : r));
      }, () => undefined);
    }, 500);
    try {
      const res = await readSlowLog(target, slowQuery, id, controller.signal);
      if (fileCtl.current?.id !== id) return;
      setFileReport(fromSlow(res, "file"));
      setFileRead({ loading: "", error: "", progress: null, doneAt: Date.now() });
      if (sourceNow.current === "file") { setSelectedKey(""); setInspectorOpen(false); }
    } catch (e) {
      if (fileCtl.current?.id !== id || controller.signal.aborted) return;
      setFileRead({ loading: "", error: errorText(e), progress: null });
    } finally {
      window.clearInterval(poll);
      if (fileCtl.current?.id === id) fileCtl.current = null;
    }
  }
  /** Stop the file read on its way (the server stops reading too). */
  function stopFile(quiet = false) {
    const running = fileCtl.current;
    if (!running) return;
    fileCtl.current = null;
    void stopSlowLog(running.id).catch(() => undefined);
    running.controller.abort();
    if (!quiet) setFileRead({ loading: "", error: "", progress: null, stopped: "Stopped reading the file. The view keeps what it showed before." });
  }
  function adopt(chosen: SlowLogFile) {
    // Another file: its clock, users and schemas are its own, and the last
    // file's statements go (its read follows from the effect below).
    setFile(chosen); setRange(ANY_TIME); setRunBy(""); setSchemaFilter("");
    setFileReport(null); setSelectedKey(""); setInspectorOpen(false);
    setPathShown(false); setPathError("");
  }
  async function chooseFile() {
    if (choosing.phase === "pending" || opening) return;
    setChoosing({ phase: "pending", message: "Choose a file in the system dialog.", startedAt: Date.now(), endedAt: null });
    try {
      const choice = await chooseSlowLog();
      if ("cancelled" in choice) {
        setChoosing({ phase: "done", message: file ? `No other file chosen; ${file.name} stays.` : "No file chosen.", startedAt: null, endedAt: Date.now() });
        return;
      }
      setChoosing(IDLE_ACTION);
      adopt(choice);
    } catch (e) {
      setChoosing({ phase: "failed", message: errorText(e), startedAt: null, endedAt: Date.now() });
      setPathShown(true);
    }
  }
  async function openPath() {
    const typed = path.trim();
    if (!typed) { setPathError("Enter the full path of the slow log file."); return; }
    if (opening) return;
    setOpening(true);
    setPathError("");
    try {
      adopt(await openSlowLogPath(typed));
      setChoosing(IDLE_ACTION);
    } catch (e) {
      setPathError(errorText(e));
    } finally {
      setOpening(false);
    }
  }
  // The typed path field takes focus when it appears (asked for, or after the chooser failed).
  useEffect(() => { if (pathShown) pathInput.current?.focus(); }, [pathShown]);
  function changeSource(next: DigestSource) {
    if (next === source) return;
    // Each source has its own clock and users: start the range and the user over.
    setSource(next); setRange(ANY_TIME); setRunBy(""); setSelectedKey(""); setInspectorOpen(false);
  }
  const current = isFile ? null : bound?.key === reads.key ? report : null;
  useWindowStatus(`digest:${reads.key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current?.version ? engineName(current.engine, current.version, current.server_product) : undefined,
    refreshing: !!reads.loading || !!fileRead.loading,
  });
  function openRow(row: DigestRow, callback: QueryDigestProps["onExplain"], explain = false) {
    if (explain && row.statement.capture_problem) return;
    // A statement from a file is not bound to a server: it opens on the
    // connection in use. A menu or inspector can outlive a connection
    // switch: check again at dispatch.
    if (report?.kind === "file") { callback?.(row.statement, row.rank, reads.currentKey()); return; }
    if (!bound || bound.key !== reads.currentKey()) return;
    callback?.(row.statement, row.rank, bound.key);
  }
  const editorReason = other ? `From ${other}; refresh to load this server.` : !onOpenInEditor ? "Query editor is unavailable." : "";
  const explainReason = (row: DigestRow) => other ? `From ${other}; refresh to load this server.`
    : row.statement.capture_problem || (!onExplain ? "Explain is unavailable." : "");
  // Performance Schema rankings compare each statement with every digest the
  // server keeps; the slow log sources with every statement in the range;
  // without those totals (the older slow-query read), with the loaded ones.
  const serverMs = slow ? (slow.totals.total_ms > 0 ? slow.totals.total_ms : null)
    : report?.totals && report.totals.total_ms > 0 ? report.totals.total_ms : null;
  const shareBase = serverMs ?? totals.ms;
  const share = (s: TopStatement) => shareBase > 0 ? Math.max(0, Math.min(100, s.total_ms / shareBase * 100)) : 0;
  const shareOf = slow ? "of the total time in the range" : serverMs ? "of the server's total statement time" : "of loaded total time";
  const cpu = report?.cpu || null;
  const others = slow ? (slow.others && slow.others.statements > 0
    ? { digests: slow.others.statements, total_ms: slow.others.total_ms, executions: slow.others.executions } : null)
    : report?.totals && report.others && report.others.digests > 0 ? report.others : null;
  // Latency histograms and table metadata of the selected statements, per
  // report: a new report reads them again; a newer read stops the one before
  // it. Every connection switch and report replacement starts a new
  // generation: reads of an older one are aborted and their answers dropped,
  // even after switching back (A, B, A), and a read they interrupted starts
  // over when its statement is shown again.
  const [histograms, setHistograms] = useState<Record<string, HistogramRead>>({});
  const histogramReads = useRef(new Map<string, AbortController>());
  const [tableReports, setTableReports] = useState<Record<string, TablesRead>>({});
  const tableReads = useRef(new Map<string, AbortController>());
  const detailGeneration = useRef(0);
  const settled = <R extends { phase: string }>(all: Record<string, R>) => {
    const kept = Object.fromEntries(Object.entries(all).filter(([, r]) => r.phase !== "loading"));
    return Object.keys(kept).length === Object.keys(all).length ? all : kept;
  };
  function stopDetailReads() {
    detailGeneration.current += 1;
    for (const running of [histogramReads.current, tableReads.current]) {
      running.forEach((c) => c.abort());
      running.clear();
    }
  }
  useEffect(() => {
    stopDetailReads();
    setHistograms({});
    setTableReports({});
    return stopDetailReads;
  }, [bound, source]);
  // A layout effect: it runs before the sections' own effects of the same
  // switch, so a section shown again on the connection starts one fresh read.
  useLayoutEffect(() => {
    // Another connection: the reads on their way were sent to the previous
    // one. Finished results stay with the report they belong to.
    stopDetailReads();
    setHistograms(settled);
    setTableReports(settled);
  }, [reads.key]);
  function readHistogram(row: DigestRow) {
    const statement = row.statement;
    if (!bound || bound.key !== reads.currentKey() || !statement.digest) return;
    const id = row.key;
    histogramReads.current.forEach((c) => c.abort());
    histogramReads.current.clear();
    const controller = new AbortController();
    histogramReads.current.set(id, controller);
    const generation = detailGeneration.current;
    setHistograms((h) => ({
      // Reads stopped by this one start over when their statement is shown again.
      ...settled(h), [id]: { phase: "loading" },
    }));
    const settle = (next: HistogramRead) => {
      if (controller.signal.aborted || generation !== detailGeneration.current) return;
      histogramReads.current.delete(id);
      setHistograms((h) => ({ ...h, [id]: next }));
    };
    readDigestHistogram(connection, statement.schema, statement.digest, controller.signal)
      .then((result) => settle({ phase: "done", result }), (e) => settle({ phase: "failed", error: errorText(e) }));
  }
  function stopHistogram(row: DigestRow) {
    histogramReads.current.get(row.key)?.abort();
    histogramReads.current.delete(row.key);
    setHistograms((h) => ({ ...h, [row.key]: { phase: "failed", error: "Stopped waiting for the latency histogram." } }));
  }
  function readTables(row: DigestRow) {
    if (!bound || bound.key !== reads.currentKey() || !onScreen || tableReads.current.has(row.key)) return;
    const controller = new AbortController();
    tableReads.current.set(row.key, controller);
    const generation = detailGeneration.current;
    setTableReports((h) => ({ ...h, [row.key]: { phase: "loading" } }));
    const settle = (next: TablesRead) => {
      if (controller.signal.aborted || generation !== detailGeneration.current) return;
      tableReads.current.delete(row.key);
      setTableReports((h) => ({ ...h, [row.key]: next }));
    };
    // Normalized text covers the digest, while a sample may be truncated.
    readDigestTables(connection, row.statement.schema, row.statement.digest_text || row.statement.sample_text || "", controller.signal)
      .then((result) => settle({ phase: "done", result }), (e) => settle({ phase: "failed", error: errorText(e) }));
  }
  function stopTables(row: DigestRow, quiet = false) {
    const pending = tableReads.current.get(row.key);
    if (!pending) return;
    pending.abort();
    tableReads.current.delete(row.key);
    setTableReports((h) => {
      const next = { ...h };
      if (quiet) delete next[row.key];
      else next[row.key] = { phase: "failed", error: "Stopped waiting for table metadata." };
      return next;
    });
  }
  const seenClock = slow ? (slow.clock.label ? `Log time (${slow.clock.label})` : "Log time") : "Server time";
  const columns: Column<DigestRow>[] = [
    { key: "statement", label: "Statement", primary: true, flex: true, minWidth: 200,
      sort: ({ statement: s }) => s.digest_text || "", render: ({ statement: s }) => <span className="qd-statement-cell">
        {s.new && <NewBadge statement={s} />}
        <code className="qd-statement" title={s.digest_text || "No normalized SQL recorded"}>{normalized(s).replace(/\s+/g, " ")}</code>
      </span> },
    { key: "schema", label: "Schema", sort: ({ statement: s }) => s.schema || "", render: ({ statement: s }) => <span className="muted">{s.schema || "—"}</span> },
    { key: "share", label: "Share of total time", num: true,
      title: slow ? "Share of the total time of every statement in the range, not only the loaded ones"
        : serverMs ? "Share of the server's total statement time: every statement the digest table holds, not only the loaded ones"
          : "Share of the total time of the loaded statements, before filtering",
      sort: ({ statement: s }) => s.total_ms, render: ({ statement: s }) => <span className="qd-share">
        <progress max={100} value={share(s)} aria-label={`${share(s).toFixed(1)}% ${shareOf}`} />{share(s).toFixed(1)}%
      </span> },
    { key: "total", label: "Total time", num: true, sort: ({ statement: s }) => s.total_ms,
      render: ({ statement: s }) => <Ms value={s.total_ms} /> },
    { key: "calls", label: "Calls", num: true, sort: ({ statement: s }) => s.count, render: ({ statement: s }) => <CompactNumber value={s.count} /> },
    { key: "avg", label: "Avg", num: true, sort: ({ statement: s }) => s.avg_ms, render: ({ statement: s }) => <Ms value={s.avg_ms} /> },
    ...(slow ? [{ key: "p95", label: "P95", num: true,
      title: "95 percent of calls took at most this long (the upper edge of its histogram bucket, never above the slowest call)",
      sort: ({ statement: s }: DigestRow) => s.p95_ms ?? null, render: ({ statement: s }: DigestRow) => <Ms value={s.p95_ms} /> }] : []),
    { key: "max", label: "Max", num: true, sort: ({ statement: s }) => s.max_ms, render: ({ statement: s }) => <Ms value={s.max_ms} /> },
    // Off by default (Columns menu): MySQL with SUM_CPU_TIME only.
    ...(cpu ? [{ key: "cpu", label: "CPU time", num: true,
      title: "SUM_CPU_TIME: counted only while the events_statements_cpu consumer is on",
      sort: ({ statement: s }: DigestRow) => cpu.collected === false ? null : s.cpu_ms ?? null,
      render: ({ statement: s }: DigestRow) => <CpuTime statement={s} collected={cpu.collected} /> }] : []),
    { key: "ratio", label: "Rows examined per row sent", num: true,
      sort: ({ statement: s }) => s.rows_sent > 0 ? s.rows_examined / s.rows_sent : s.rows_examined > 0 ? Number.MAX_VALUE : null,
      render: ({ statement: s }) => {
        const high = s.rows_sent > 0 ? s.rows_examined / s.rows_sent : s.rows_examined > 0 ? Infinity : 0;
        return <span className={high >= 1000 ? "qd-ratio-danger" : high >= 50 ? "qd-ratio-warn" : undefined}>{ratio(s.rows_examined, s.rows_sent)}</span>;
      } },
  ];
  const fileHint = "This statement comes from a file: it opens and runs on the connection in use.";
  const slowSelected = selected && slow ? slowOf(selected.statement) : null;
  const inspector: InspectorContent | null = selected ? {
    key: selected.key, title: `#${selected.rank} · ${selected.statement.schema || "No default schema"}`,
    meta: selected.statement.new ? <NewBadge statement={selected.statement} /> : undefined,
    actions: <div className="qd-actions">
      <ToolbarButton primary icon="query" disabled={actionsOff || !!editorReason} title={editorReason || (report?.kind === "file" ? fileHint : "Open in Query editor")}
        onClick={() => openRow(selected, onOpenInEditor)}>Open in Query editor</ToolbarButton>
      <ToolbarButton icon="plan" disabled={actionsOff || !!explainReason(selected)} title={explainReason(selected) || (report?.kind === "file" ? fileHint : "Explain this statement")}
        onClick={() => openRow(selected, onExplain, true)}>Explain</ToolbarButton>
    </div>,
    body: <>
      <InspectorSection title="Statement">
        <Fields rows={[
          ["Total time", <Ms value={selected.statement.total_ms} />],
          ["Calls", <CompactNumber value={selected.statement.count} />],
          ["Avg", <Ms value={selected.statement.avg_ms} />],
          ["Max", <Ms value={selected.statement.max_ms} />],
          cpu && ["CPU time", <CpuTime statement={selected.statement} collected={cpu.collected} />],
          cpu && cpu.collected === false && ["", <span className="qd-cpu-note">events_statements_cpu is off. <InstrumentsLink row="consumer:events_statements_cpu" /></span>],
          ["Lock time", <Ms value={selected.statement.lock_time_ms} />],
          ["Rows examined", <CompactNumber value={selected.statement.rows_examined} />],
          ["Rows sent", <CompactNumber value={selected.statement.rows_sent} />],
          slowSelected && slowSelected.rows_affected != null && ["Rows affected", <CompactNumber value={slowSelected.rows_affected} />],
          (!slowSelected || slowSelected.tmp_tables != null) && ["Temp tables (on disk)", <><CompactNumber value={selected.statement.tmp_tables} /> (<CompactNumber value={selected.statement.tmp_disk_tables} />)</>],
          (!slowSelected || slowSelected.select_scan != null) && ["Full scans", <CompactNumber value={selected.statement.select_scan} />],
          (!slowSelected || slowSelected.select_full_join != null) && ["Full joins", <CompactNumber value={selected.statement.select_full_join} />],
          slowSelected && slowSelected.errors != null && ["Errors", <CompactNumber value={slowSelected.errors} />],
          ["Schema", selected.statement.schema || "—"],
          slowSelected ? ["Fingerprint", <DigestValue key={selected.key} digest={slowSelected.fingerprint_id} />]
            : ["Digest", selected.statement.digest ? <DigestValue key={selected.key} digest={selected.statement.digest} /> : "—"],
          ["First seen", <SeenAt value={selected.statement.first_seen} clock={seenClock} />],
          ["Last seen", <SeenAt value={selected.statement.last_seen} clock={seenClock} />],
        ]} />
      </InspectorSection>
      {slowSelected && <InspectorSection title="Latency" info={<p>P95, P99 and P99.9: 95, 99 and 99.9 percent of calls took at most this long.
          Each is the upper edge of the histogram bucket that holds it, never above the slowest call. Counted from the log, in the range.</p>}>
        <SlowLatency key={selected.key} statement={slowSelected} />
      </InspectorSection>}
      {slowSelected && <InspectorSection title="Who ran it">
        <Fields rows={[
          ["Users", namesText(slowSelected.users, "Not logged")],
          ["Hosts", namesText(slowSelected.hosts, "Not logged")],
        ]} />
      </InspectorSection>}
      {!slow && report?.engine === "mysql" && selected.statement.p95_ms != null &&
        <InspectorSection title="Latency" info={<p>P95, P99 and P99.9: 95, 99 and 99.9 percent of calls took at most this long.
          Each is the upper edge of the histogram bucket that holds it. Counted since the statement was first seen.</p>}>
          <LatencySection key={selected.key} statement={selected.statement} read={histograms[selected.key]}
            enabled={onScreen && !other} blocked={other ? `From ${other}; refresh to read this server.` : ""}
            onRead={() => readHistogram(selected)} onStop={() => stopHistogram(selected)} />
        </InspectorSection>}
      {report?.kind !== "file" && <InspectorSection title="Tables" info={<p>A text match, not proof of access. Views and routines may refer to other tables.
        Sizes come from information_schema and rows are estimates. No table rows are read.</p>}>
        <TablesSection key={selected.key} read={tableReports[selected.key]} enabled={onScreen && !other} blocked={other ? `From ${other}; refresh to read this server.` : ""}
          onRead={() => readTables(selected)} onStop={() => stopTables(selected)} onLeave={() => stopTables(selected, true)}
          onOpenIndexes={onOpenIndexes ? (schema, table) => {
            if (bound && bound.key === reads.currentKey()) onOpenIndexes(schema, table, bound.key);
          } : undefined} />
      </InspectorSection>}
      <InspectorSection title="Normalized SQL"><CodeBlock key={selected.key} text={normalized(selected.statement)} label="normalized SQL" /></InspectorSection>
      <InspectorSection title={slowSelected ? "Sample (the slowest call)" : "Sample"}>{selected.statement.sample_text
        ? <>
          {slowSelected?.sample && <p className="qd-sample-meta">{[
            time(slowSelected.sample.query_time_ms),
            [slowSelected.sample.user, slowSelected.sample.host].filter(Boolean).join("@"),
            slowSelected.sample.time ? wallText(slowSelected.sample.time, true) : "",
            slowSelected.sample.line != null ? `line ${number(slowSelected.sample.line)}` : "",
          ].filter(Boolean).join(" · ")}{slowSelected.sample.truncated ? " · cut" : ""}</p>}
          <CodeBlock key={selected.key} text={selected.statement.sample_text} label="sample SQL" />
        </> : <p className="muted">No sample recorded.</p>}</InspectorSection>
    </>,
  } : null;

  function textReport() {
    if (!report) return "";
    const lines = ["# myflames query digest",
      ...(report.kind === "file" ? [] : [`# Server: ${engineName(report.engine, report.version, report.server_product)}`,
        `# Connection: ${bound?.label}`]),
      `# Generated at: ${new Date().toISOString()}`,
      ...(slow ? [`# Rank by: ${SLOW_ORDERS.find(([k]) => k === slow.order)?.[1] || slow.order}`] : [`# Filter mode: ${report.filter}`]),
      `# Source: ${sourceOf(report)} (${sourceTable(report)})`,
      ...(slow && slow.engine ? [`# Logged by: ${engineName(slow.engine, slow.server_version || "", slow.server_product)}`] : []),
      `# Coverage: ${periodOf(report)}`,
      ...(slow ? [`# Clock: ${slow.clock.text}`,
        `# Time range: ${slow.range.applied ? `${slow.range.from || "the start"} to ${slow.range.to || "the end"}` : "any time"}`,
        ...(slow.filters.user ? [`# User: ${slow.filters.user}`] : [])] : []),
      ...(report.kind === "pfs" && rangeActive(range) ? [`# Time range: ${timeRangeText(range)} (statements seen in it; numbers are totals since counting began)`] : []),
      `# Loaded scope: ${rows.length} statements, ${number(totals.calls)} executions, ${time(totals.ms)} total`,
      ...(slow ? [
        `# In the range: ${number(slow.totals.statements)} statements, ${number(slow.totals.executions)} executions, ${time(slow.totals.total_ms)} total`,
        ...(others ? [`# Not loaded: ${number(others.digests)} other statements, ${percent(others.total_ms, slow.totals.total_ms)} of total time, ${number(others.executions)} calls`] : []),
        "# Time shares are of the total time in the range. Unknown means the log does not record it.",
      ] : serverMs && report.totals ? [
        `# Server: ${number(report.totals.digests)} statements, ${number(report.totals.executions)} executions, ${time(serverMs)} total`,
        ...(others ? [`# Not loaded: ${number(others.digests)} other statements, ${percent(others.total_ms, serverMs)} of total time, ${number(others.executions)} calls`] : []),
        "# Time shares are of the server's total statement time; totals cover the loaded statements. Unknown means not collected.",
      ] : ["# Totals and time shares cover the loaded statements only. Unknown means not collected."]),
      ...reportNotes(report).map((note) => `# Note: ${note}`)];
    rows.forEach(({ statement: s, rank }) => {
      const x = slow ? slowOf(s) : null;
      lines.push("", `# Statement ${rank} · ${s.schema || "No default schema"}${s.new ? " · New" : ""}`,
        x ? `# Fingerprint: ${x.fingerprint_id}` : `# Digest: ${s.digest || "unknown"}`, `# Calls: ${number(s.count)}`,
        `# Total time: ${time(s.total_ms)} (${share(s).toFixed(1)}%) · Avg: ${time(s.avg_ms)} · Max: ${time(s.max_ms)}`,
        ...(x ? [`# Fastest call: ${time(x.min_ms)}`] : []),
        ...(s.p95_ms != null ? [`# P95: ${time(s.p95_ms)} · P99: ${time(s.p99_ms)} · P99.9: ${time(s.p999_ms)}`] : []),
        ...(cpu ? [`# CPU time: ${cpu.collected === false ? "not collected (events_statements_cpu consumer is off)" : cpu.collected === null && !s.cpu_ms ? "unknown" : time(s.cpu_ms)}`] : []),
        `# Lock time: ${s.lock_time_ms == null ? "unknown" : time(s.lock_time_ms)}`,
        `# Rows examined: ${number(s.rows_examined)} · Rows sent: ${number(s.rows_sent)} · Ratio: ${ratio(s.rows_examined, s.rows_sent)}`,
        `# Temp tables: ${s.tmp_tables == null ? "unknown" : count(s.tmp_tables)} · On disk: ${s.tmp_disk_tables == null ? "unknown" : count(s.tmp_disk_tables)}`,
        `# Full scans: ${s.select_scan == null ? "unknown" : count(s.select_scan)} · Full joins: ${s.select_full_join == null ? "unknown" : count(s.select_full_join)}`,
        `# First seen: ${s.first_seen || "unknown"} · Last seen: ${s.last_seen || "unknown"}`,
        ...(x ? [`# Users: ${namesText(x.users, "not logged")}`, `# Hosts: ${namesText(x.hosts, "not logged")}`] : []),
        "# Normalized SQL:", s.digest_text || "# Not recorded", "# Sample:", s.sample_text || "# Not recorded");
    });
    return lines.join("\n");
  }
  function csvReport() {
    const header = ["Rank", "Query Digest", "Schema", "Calls", "Total Time (ms)", "Avg Time (ms)", "Max Time (ms)",
      "Lock Time (ms)", "Rows Examined", "Rows Sent", "Examined/Sent Ratio", "Disk Temp Tables", "Total Temp Tables",
      "Full Table Scans", "Full Joins", "Fingerprint", "Sample SQL", "P95 (ms)", "P99 (ms)", "P99.9 (ms)", "CPU Time (ms)",
      "First Seen", "New", "Last Seen", "Min Time (ms)", "Users", "Fingerprint ID"];
    return [header, ...rows.map(({ statement: s, rank }) => {
      const x = slow ? slowOf(s) : null;
      return [rank, s.digest, s.schema, s.count, s.total_ms, s.avg_ms,
        s.max_ms, s.lock_time_ms, s.rows_examined, s.rows_sent, s.rows_sent > 0 ? s.rows_examined / s.rows_sent : null,
        s.tmp_disk_tables, s.tmp_tables, s.select_scan, s.select_full_join, s.digest_text, s.sample_text,
        s.p95_ms, s.p99_ms, s.p999_ms, cpu && cpu.collected !== false ? s.cpu_ms : null, s.first_seen, s.new ? "yes" : "",
        s.last_seen, x ? x.min_ms : null, x ? x.users.map((u) => `${u.name || "(none)"}:${u.count}`).join(" ") : null,
        x ? x.fingerprint_id : null];
    })].map((row) => row.map((value) => csvCell(value == null ? "" : value)).join(",")).join("\n");
  }
  function filename(extension: string) { return `query-digest-${Date.now()}.${extension}`; }
  const notes = report ? reportNotes(report) : [];
  const age = report?.coverage?.uptime_s;
  const openAnother = <ActionButton small className="qd-open-another" busy={choosing.phase === "pending"} busyLabel="Choosing…"
    title="Choose another slow query log file on this computer" onClick={() => { void chooseFile(); }}>Open another file…</ActionButton>;
  const summary = report ? <SummaryLine notes={notes} extra={report.kind === "file" ? openAnother : undefined}>
    {slow && <>{slow.source.kind === "file" ? `Slow log file: ${slow.source.name}` : "mysql.slow_log"} · </>}
    {rows.length} statements · {time(totals.ms)} total · {number(totals.calls)} executions
    {report.kind === "pfs" && age != null &&
      <> · <span title={`Counted since the server started ${durationText(age)} ago, or since the digest table was last reset.`}>since {durationText(age)} ago</span></>}
    {slow && slow.range.applied && <> · <span title={`${slow.range.from || "the start"} to ${slow.range.to || "the end"} (${slow.clock.label || "as written"})`}>
      {timeRangeText({ from: inputValue(slow.range.from), to: inputValue(slow.range.to) })}</span></>}
    {shown.length !== rows.length ? ` · ${shown.length} shown` : ""}
  </SummaryLine> : null;
  const rangeScope = isFile || source === "table"
    ? "Only entries logged in the range are counted."
    : "Performance Schema keeps totals since counting began: the range keeps the statements seen in it.";
  // Quick ranges end at the newest time the report knows: the file's last
  // entry, the server's NOW() for the table, the latest last seen for
  // Performance Schema.
  const anchor = slow ? slow.anchor
    : report?.kind === "pfs" ? rows.reduce<string | null>((a, { statement: s }) => (s.last_seen && (!a || s.last_seen > a) ? s.last_seen : a), null) : null;
  const userOptions = useMemo<[string, string][]>(() => {
    const names = slow?.users || [];
    const options: [string, string][] = [["", "All users"], ...names.map((u): [string, string] => [u.name, `${u.name || "No user"} (${number(u.count)})`])];
    if (runBy && !names.some((u) => u.name === runBy)) options.push([runBy, runBy]);
    return options;
  }, [slow, runBy]);
  const fileProgress = fileRead.progress;
  const emptyFile = isFile && !file;
  return <InspectorContext.Provider value={shared}>
    <InstrumentsLinkContext.Provider value={onOpenInstruments || null}>
    <div className="qd-place" ref={root} data-source={source}>
      <ViewLayout id="query-digest" title="Query digest" className="qd-view" detailBackLabel=""
        otherConnection={other} inspector={inspector} onRefresh={() => { void load(); }} loading={!!reads.loading || !!fileRead.loading}
        refreshDisabled={isFile && !file}
        info={<>
          <p>{report ? sourceSentence(report) : isFile
            ? <>Reads a slow query log file on this computer (MySQL 5.7 to 9.x, Percona Server or MariaDB). Nothing is sent to a server.</>
            : source === "table" ? <>Reads <code>mysql.slow_log</code> of the connection in use.</>
              : <>Reads <code>{DIGEST_TABLE}</code> or, for slow queries, <code>mysql.slow_log</code>.</>}</p>
          <p>{slow ? "Share of total time compares each statement with every statement in the range; the totals cover the loaded statements."
            : "Share of total time compares each statement with every digest the server keeps; the totals cover the loaded statements."}</p>
        </>}
        controls={<>
          <label className="toolbar-field qd-source"><span className="qd-field-word">Source</span> <Select label="Source" value={source} options={SOURCES} onChange={(v) => changeSource(v as DigestSource)} /></label>
          {source === "pfs"
            ? <label className="toolbar-field">Rank by <Select label="Rank by" value={filter} options={MODES} onChange={(v) => setFilter(v as DigestFilterMode)} /></label>
            : <label className="toolbar-field">Rank by <Select label="Rank by" value={slowOrder} options={SLOW_ORDERS} onChange={(v) => setSlowOrder(v as SlowLogOrder)} /></label>}
          <label className="toolbar-field qd-schema"><span className="qd-field-word">Schema</span> <Select label="Schema" value={schemaFilter} options={schemaOptions} disabled={!report} onChange={setSchemaFilter} /></label>
          <Select label="Statements to load" value={limit} options={[[20, "Top 20"], [50, "Top 50"], [100, "Top 100"]]} onChange={(v) => setLimit(Number(v))} />
          <span className="qd-toolbar-break" aria-hidden="true" />
          <TimeRangeButton value={range} onChange={setRange} anchor={anchor} clock={clockLabel(report)} scope={rangeScope}
            disabled={emptyFile} />
          {source !== "pfs" && <Select label="User" value={runBy} options={userOptions} disabled={!slow} onChange={setRunBy} />}
          <SearchField label="Filter statements" placeholder="Filter statements" value={search} onChange={setSearch} />
          <ExportMenu label="Export query digest" disabled={!report} formats={[
            { key: "text", label: "Text report", onSelect: () => { setPreview({ text: textReport(), filename: filename("txt") }); setExportOpen(true); } },
            { key: "csv", label: "CSV", onSelect: () => { const name = filename("csv"); downloadFile(name, csvReport(), "text/csv"); setExportNote(`Saved ${name}.`); } },
          ]} />
        </>}
        status={<>{summary && <div className="qd-summary">{summary}</div>}
          {pfsRange && <p className="qd-range-note">
            Numbers are totals since counting began, not only this range: the range keeps the statements seen in it.
            <InfoPopover label="About time ranges and Performance Schema">
              <p>Performance Schema keeps one running total per statement since the server started or the digest table was reset.
                The digest table does not record when each call ran.</p>
              <p>A range keeps the statements first seen before its end and last seen after its start. Their calls and times still
                cover everything since counting began. Times are the server's clock. For exact counts in a range, use a slow log.</p>
            </InfoPopover>
          </p>}
          {isFile && <ActionStatus className="qd-file-line" state={choosing} />}
          {isFile ? <ViewStatus loading={fileRead.loading} error={fileRead.error} onStop={() => stopFile()} onRetry={() => { void readFile(); }}
            doneAt={fileRead.doneAt} slowAfter={2000} slowText="Reading the whole file." />
            : <ViewStatus loading={reads.loading} error={reads.error} onStop={reads.stop} onRetry={() => { void load(); }} doneAt={reads.doneAt} />}
          {isFile && fileRead.loading && fileProgress && fileProgress.percent != null && <p className="qd-file-progress">
            <progress max={100} value={fileProgress.percent} aria-label={`Read ${fileProgress.percent}% of the file`} />
            <span>{fileProgress.percent.toFixed(0)}% · {bytes(fileProgress.bytes_read)} of {bytes(fileProgress.bytes_total)}</span>
          </p>}
          {isFile && fileRead.stopped && !fileRead.loading && <p className="qd-feedback" role="status">{fileRead.stopped}</p>}
          {exportNote && <p className="qd-feedback" role="status">{exportNote}</p>}
        </>}>
        {emptyFile ? <div className="qd-file-empty">
          <EmptyState icon="import" title="Open a slow query log" action={<div className="qd-file-actions">
            <ActionButton primary busy={choosing.phase === "pending"} busyLabel="Choosing…" onClick={() => { void chooseFile(); }}>Choose file…</ActionButton>
            {!pathShown && <button type="button" className="text-button" onClick={() => setPathShown(true)}>Type a path instead</button>}
          </div>}>
            The file is read on this computer and nothing is sent to a server. MySQL 5.7 to 9.x, Percona Server and MariaDB slow logs are read.
          </EmptyState>
          {pathShown && <form className="qd-path" noValidate onSubmit={(e) => { e.preventDefault(); void openPath(); }}>
            <label htmlFor={pathId}>Path of the slow log file</label>
            <div className="qd-path-row">
              <input id={pathId} ref={pathInput} value={path} spellCheck={false} autoComplete="off" placeholder="/var/log/mysql/slow.log"
                aria-describedby={`${pathId}-hint`} aria-invalid={pathError ? true : undefined}
                onChange={(e) => { setPath(e.target.value); setPathError(""); }} />
              <ActionButton type="submit" busy={opening} busyLabel="Opening…">Open</ActionButton>
            </div>
            <p id={`${pathId}-hint`} className={pathError ? "form-error" : "hint"} role={pathError ? "alert" : undefined}>
              {pathError || "The full path of a slow query log on this computer (at most 4 GiB)."}
            </p>
          </form>}
        </div> : isFile && !fileReport ? <EmptyState icon="import" title={fileRead.loading ? `Reading ${file?.name}…` : fileRead.error ? `Could not read ${file?.name}` : `${file?.name} is not read yet`}
          action={fileRead.loading ? undefined : <div className="qd-file-actions">{openAnother}</div>}>
          {fileRead.loading ? "The whole file is read on this computer; the progress shows above." : "Read it again with Refresh, or choose another file."}
        </EmptyState> : <>
          <DataTable label="Query digest statements" columns={columns} rows={shown} rowKey={(r) => r.key}
            compact detailColumns={["cpu"]}
            selection={{ key: selectedKey, select: (key, open) => { setSelectedKey(key); if (open !== false) setInspectorOpen(true); } }}
            rowContextMenu={(r, { copy, close }) => [
              { label: "Open in Query editor", disabled: actionsOff || !!editorReason, hint: editorReason,
                onClick: () => { close(); openRow(r, onOpenInEditor); } },
              { label: "Explain", disabled: actionsOff || !!explainReason(r), hint: explainReason(r),
                onClick: () => { close(); openRow(r, onExplain, true); } },
              { divider: true },
              { label: "Copy statement", disabled: !r.statement.digest_text && !r.statement.sample_text,
                onClick: () => { void copy(r.statement.digest_text || r.statement.sample_text || ""); close(); } },
              slow ? { label: "Copy fingerprint ID", onClick: () => { void copy(slowOf(r.statement).fingerprint_id); close(); } }
                : { label: "Copy digest", disabled: !r.statement.digest,
                  onClick: () => { void copy(r.statement.digest || ""); close(); } },
            ]}
            empty={<EmptyState icon="statements" title={filtered && rows.length ? "No matching statements" : slow && slow.range.applied ? "No statements in this range" : "No statements recorded"}>
              {filtered && rows.length ? (pfsRange ? "No loaded statement was seen in this range. Change the range, the schema or the filter." : "Change the schema or the filter to show other loaded statements.")
                : reads.loading || fileRead.loading ? "Reading statements…"
                  : slow && slow.range.applied ? `The log has entries from ${wallText(slow.span.first, true) || "—"} to ${wallText(slow.span.last, true) || "—"}. Change the range.`
                    : isFile ? "This file has no slow log entries." : "Refresh to read the selected source again."}
            </EmptyState>} />
          {others && shareBase > 0 && <p className="qd-others">
            {`${number(others.digests)} other ${others.digests === 1 ? "statement" : "statements"} · ${percent(others.total_ms, shareBase)} of total time · ${compact(others.executions)} ${others.executions === 1 ? "call" : "calls"}`}
          </p>}
        </>}
        {exportOpen && <ConfirmSheet title="Text report" eyebrow="Export query digest" confirmTone="primary"
          confirmLabel="Download text report" onCancel={() => setExportOpen(false)}
          onConfirm={() => { downloadFile(preview.filename, preview.text, "text/plain"); setExportNote(`Saved ${preview.filename}.`); setExportOpen(false); }}>
          <CodeBlock text={preview.text} label="text report" />
        </ConfirmSheet>}
      </ViewLayout>
    </div>
    </InstrumentsLinkContext.Provider>
  </InspectorContext.Provider>;
}
