import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  api, formatNumber, number, readDigestHistogram, readDigestTables, time, type Connection, type DigestHistogram, type DigestTables,
  type SlowQueriesReport, type TopCoverage, type TopOrder, type TopReport, type TopStatement, type TopTotals,
} from "./api";
import { connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import {
  ConfirmSheet, DataTable, Disclosure, EmptyState, ExportMenu, Fields, InspectorContext, InspectorSection, Pill,
  SearchField, Select, SummaryLine, ToolbarButton, ViewLayout, ViewStatus, copyToClipboard, errorText,
  storedInspectorWidth, useWindowStatus, type Column, type InspectorContent, type InspectorShared,
} from "./kit";
import { CompactNumber, bytes, compact, count } from "./PerfShared";
import { durationText } from "./duration";
import { csvCell } from "./reportTableExport";
import { engineName } from "./OpsShared";
import { InstrumentsLink, InstrumentsLinkContext } from "./Instruments";
import { binLow, boundText, latencyBins, latencySummary, rangeText, BINS_PER_DECADE, type LatencyBins } from "./latencyHistogram";
import "./query-digest.css";

export type DigestFilterMode = "time" | "scans" | "full_join" | "tmp_tables" | "locks" | "slow_queries";
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
  ["tmp_tables", "Temp tables on disk"], ["locks", "Lock time"], ["slow_queries", "Slow queries over 1 s"],
];
const DIGEST_TABLE = "performance_schema.events_statements_summary_by_digest";
// The slow endpoint sends notes at runtime, though its shared type predates that field.
type SlowDigestReport = SlowQueriesReport & { notes?: string[] };
type DigestReport = {
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
function sourceOf(report: DigestReport) {
  return sourceTable(report) === "mysql.slow_log" ? "Slow query log (mysql.slow_log table)" : "Performance Schema statement digests";
}
function sourceTable(report: DigestReport) { return report.source === "mysql.slow_log" || report.source === "slow_log" ? "mysql.slow_log" : DIGEST_TABLE; }
/** The ⓘ sentence for the report on screen: which table was read and the
 * period its counters cover. The summary line keeps only "since … ago". */
function sourceSentence(report: DigestReport): ReactNode {
  if (report.filter === "slow_queries") {
    return sourceTable(report) === "mysql.slow_log"
      ? <>This report reads <code>mysql.slow_log</code>: statements of at least 1 s.</>
      : <>This report reads <code>{DIGEST_TABLE}</code>, the digest table the slow-query read falls back to: digests with a recorded execution of at least 1 s.</>;
  }
  const age = report.coverage?.uptime_s;
  return <>This report reads <code>{DIGEST_TABLE}</code>: Performance Schema statement digests counted since the server started{age != null ? ` ${durationText(age)} ago` : ""} or the digest table was last reset.</>;
}
function periodOf(report: DigestReport) {
  if (report.filter === "slow_queries") {
    return sourceTable(report) === "mysql.slow_log" ? "statements of at least 1 s" : "digests with a recorded execution of at least 1 s";
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
function SeenAt({ value }: { value: string | null }) {
  if (!value) return <>—</>;
  // Built and written in UTC, so no local zone rule (a daylight saving gap)
  // can move the server's digits.
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(value);
  const at = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0))) : null;
  if (!at || Number.isNaN(at.getTime())) return <>{value}</>;
  return <time dateTime={value} title={`Server time: ${value}`}>
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

export function QueryDigestView({ connection, onOpenInEditor, onExplain, onOpenInstruments, onOpenIndexes }: QueryDigestProps) {
  const [filter, setFilter] = useState<DigestFilterMode>("time");
  const [search, setSearch] = useState("");
  /** The schema shown: "" for all, NO_SCHEMA, or a name (filters the loaded rows; never a read). */
  const [schemaFilter, setSchemaFilter] = useState("");
  const [limit, setLimit] = useState(50);
  const reads = useConnectionReads(connection);
  const [bound, setBound] = useState<Bound<DigestReport> | null>(null);
  const boundKey = useRef("");
  boundKey.current = bound?.key || "";
  const report = bound?.report;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const actionsOff = !bound || !!other;
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
  const rows = useMemo<DigestRow[]>(() => (report?.statements || []).map((statement, i) => ({
    statement, rank: i + 1,
    // A digest may appear in multiple schemas; slow-log entries have synthetic ids.
    key: JSON.stringify([statement.schema, statement.digest, statement.digest_text, i]),
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
  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return rows.filter(({ statement: s }) => (!schemaFilter || (schemaFilter === NO_SCHEMA ? !s.schema : s.schema === schemaFilter)) &&
      (!needle || [s.digest_text, s.sample_text, s.schema, s.digest].some((text) => text?.toLowerCase().includes(needle))));
  }, [rows, search, schemaFilter]);
  const filtered = !!search.trim() || !!schemaFilter;
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
  const params = `query-digest:${filter}:${limit}:1:${connection.database || ""}`;
  const load = () => reads.run(params, filter === "slow_queries" ? "Reading slow statements…" : "Reading statement digests…",
    async (c, signal): Promise<DigestReport> => {
      const res = filter === "slow_queries"
        ? await api<SlowDigestReport>("live/slow-queries", { connection: c, min_seconds: 1, limit, source: "auto" }, signal)
        : await api<TopReport>("live/top", { connection: c, order: filter as TopOrder, limit, extra_metrics: true }, signal);
      return {
        statements: res.statements || [], engine: res.engine, version: res.server_version,
        server_product: res.server_product, source: "source" in res ? res.source : undefined,
        coverage: "coverage" in res ? res.coverage : undefined, notes: res.notes || [], filter,
        totals: "totals" in res ? res.totals : undefined, others: "others" in res ? res.others : undefined,
        cpu: "cpu" in res ? res.cpu : undefined,
      };
    }, (result) => {
      // Another connection's databases differ: its statements start with All schemas.
      if (result.key !== boundKey.current) setSchemaFilter("");
      setBound(result); setSelectedKey(""); setInspectorOpen(false);
    });
  const onScreen = useViewOnScreen();
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: true, busy: !!reads.loading || reads.running(), read: () => { void load(); } });
  const choices = useRef(params);
  useEffect(() => {
    if (onScreen && choices.current !== params) { choices.current = params; void load(); }
  }, [params, onScreen]);
  const current = bound?.key === reads.key ? report : null;
  useWindowStatus(`digest:${reads.key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current?.version ? engineName(current.engine, current.version, current.server_product) : undefined,
    refreshing: !!reads.loading,
  });
  function openRow(row: DigestRow, callback: QueryDigestProps["onExplain"], explain = false) {
    // A menu or inspector can outlive a connection switch: check again at dispatch.
    if (!bound || bound.key !== reads.currentKey() || (explain && row.statement.capture_problem)) return;
    callback?.(row.statement, row.rank, bound.key);
  }
  const editorReason = other ? `From ${other}; refresh to load this server.` : !onOpenInEditor ? "Query editor is unavailable." : "";
  const explainReason = (row: DigestRow) => other ? `From ${other}; refresh to load this server.`
    : row.statement.capture_problem || (!onExplain ? "Explain is unavailable." : "");
  // Performance Schema rankings compare each statement with every digest the
  // server keeps; without those totals (slow log, an older reply), with the
  // loaded statements.
  const serverMs = report?.totals && report.totals.total_ms > 0 ? report.totals.total_ms : null;
  const shareBase = serverMs ?? totals.ms;
  const share = (s: TopStatement) => shareBase > 0 ? Math.max(0, Math.min(100, s.total_ms / shareBase * 100)) : 0;
  const shareOf = serverMs ? "of the server's total statement time" : "of loaded total time";
  const cpu = report?.cpu || null;
  const others = report?.totals && report.others && report.others.digests > 0 ? report.others : null;
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
  }, [bound]);
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
  const columns: Column<DigestRow>[] = [
    { key: "statement", label: "Statement", primary: true, flex: true, minWidth: 200,
      sort: ({ statement: s }) => s.digest_text || "", render: ({ statement: s }) => <span className="qd-statement-cell">
        {s.new && <NewBadge statement={s} />}
        <code className="qd-statement" title={s.digest_text || "No normalized SQL recorded"}>{normalized(s).replace(/\s+/g, " ")}</code>
      </span> },
    { key: "schema", label: "Schema", sort: ({ statement: s }) => s.schema || "", render: ({ statement: s }) => <span className="muted">{s.schema || "—"}</span> },
    { key: "share", label: "Share of total time", num: true,
      title: serverMs ? "Share of the server's total statement time: every statement the digest table holds, not only the loaded ones"
        : "Share of the total time of the loaded statements, before filtering",
      sort: ({ statement: s }) => s.total_ms, render: ({ statement: s }) => <span className="qd-share">
        <progress max={100} value={share(s)} aria-label={`${share(s).toFixed(1)}% ${shareOf}`} />{share(s).toFixed(1)}%
      </span> },
    { key: "total", label: "Total time", num: true, sort: ({ statement: s }) => s.total_ms,
      render: ({ statement: s }) => <Ms value={s.total_ms} /> },
    { key: "calls", label: "Calls", num: true, sort: ({ statement: s }) => s.count, render: ({ statement: s }) => <CompactNumber value={s.count} /> },
    { key: "avg", label: "Avg", num: true, sort: ({ statement: s }) => s.avg_ms, render: ({ statement: s }) => <Ms value={s.avg_ms} /> },
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
  const inspector: InspectorContent | null = selected ? {
    key: selected.key, title: `#${selected.rank} · ${selected.statement.schema || "No default schema"}`,
    meta: selected.statement.new ? <NewBadge statement={selected.statement} /> : undefined,
    actions: <div className="qd-actions">
      <ToolbarButton primary icon="query" disabled={actionsOff || !!editorReason} title={editorReason || "Open in Query editor"}
        onClick={() => openRow(selected, onOpenInEditor)}>Open in Query editor</ToolbarButton>
      <ToolbarButton icon="plan" disabled={actionsOff || !!explainReason(selected)} title={explainReason(selected) || "Explain this statement"}
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
          ["Temp tables (on disk)", <><CompactNumber value={selected.statement.tmp_tables} /> (<CompactNumber value={selected.statement.tmp_disk_tables} />)</>],
          ["Full scans", <CompactNumber value={selected.statement.select_scan} />],
          ["Full joins", <CompactNumber value={selected.statement.select_full_join} />],
          ["Schema", selected.statement.schema || "—"],
          ["Digest", selected.statement.digest ? <DigestValue key={selected.key} digest={selected.statement.digest} /> : "—"],
          ["First seen", <SeenAt value={selected.statement.first_seen} />], ["Last seen", <SeenAt value={selected.statement.last_seen} />],
        ]} />
      </InspectorSection>
      {report?.engine === "mysql" && report.filter !== "slow_queries" && selected.statement.p95_ms != null &&
        <InspectorSection title="Latency" info={<p>P95, P99 and P99.9: 95, 99 and 99.9 percent of calls took at most this long.
          Each is the upper edge of the histogram bucket that holds it. Counted since the statement was first seen.</p>}>
          <LatencySection key={selected.key} statement={selected.statement} read={histograms[selected.key]}
            enabled={onScreen && !other} blocked={other ? `From ${other}; refresh to read this server.` : ""}
            onRead={() => readHistogram(selected)} onStop={() => stopHistogram(selected)} />
        </InspectorSection>}
      <InspectorSection title="Tables" info={<p>A text match, not proof of access. Views and routines may refer to other tables.
        Sizes come from information_schema and rows are estimates. No table rows are read.</p>}>
        <TablesSection key={selected.key} read={tableReports[selected.key]} enabled={onScreen && !other} blocked={other ? `From ${other}; refresh to read this server.` : ""}
          onRead={() => readTables(selected)} onStop={() => stopTables(selected)} onLeave={() => stopTables(selected, true)}
          onOpenIndexes={onOpenIndexes ? (schema, table) => {
            if (bound && bound.key === reads.currentKey()) onOpenIndexes(schema, table, bound.key);
          } : undefined} />
      </InspectorSection>
      <InspectorSection title="Normalized SQL"><CodeBlock key={selected.key} text={normalized(selected.statement)} label="normalized SQL" /></InspectorSection>
      <InspectorSection title="Sample">{selected.statement.sample_text
        ? <CodeBlock key={selected.key} text={selected.statement.sample_text} label="sample SQL" /> : <p className="muted">No sample recorded.</p>}</InspectorSection>
    </>,
  } : null;

  function textReport() {
    if (!report) return "";
    const lines = ["# myflames query digest",
      `# Server: ${engineName(report.engine, report.version, report.server_product)}`,
      `# Connection: ${bound?.label}`, `# Generated at: ${new Date().toISOString()}`,
      `# Filter mode: ${report.filter}`, `# Source: ${sourceOf(report)} (${sourceTable(report)})`,
      `# Coverage: ${periodOf(report)}`,
      `# Loaded scope: ${rows.length} statements, ${number(totals.calls)} executions, ${time(totals.ms)} total`,
      ...(serverMs && report.totals ? [
        `# Server: ${number(report.totals.digests)} statements, ${number(report.totals.executions)} executions, ${time(serverMs)} total`,
        ...(others ? [`# Not loaded: ${number(others.digests)} other statements, ${percent(others.total_ms, serverMs)} of total time, ${number(others.executions)} calls`] : []),
        "# Time shares are of the server's total statement time; totals cover the loaded statements. Unknown means not collected.",
      ] : ["# Totals and time shares cover the loaded statements only. Unknown means not collected."]),
      ...reportNotes(report).map((note) => `# Note: ${note}`)];
    rows.forEach(({ statement: s, rank }) => lines.push("", `# Statement ${rank} · ${s.schema || "No default schema"}${s.new ? " · New" : ""}`,
      `# Digest: ${s.digest || "unknown"}`, `# Calls: ${number(s.count)}`,
      `# Total time: ${time(s.total_ms)} (${share(s).toFixed(1)}%) · Avg: ${time(s.avg_ms)} · Max: ${time(s.max_ms)}`,
      ...(s.p95_ms != null ? [`# P95: ${time(s.p95_ms)} · P99: ${time(s.p99_ms)} · P99.9: ${time(s.p999_ms)}`] : []),
      ...(cpu ? [`# CPU time: ${cpu.collected === false ? "not collected (events_statements_cpu consumer is off)" : cpu.collected === null && !s.cpu_ms ? "unknown" : time(s.cpu_ms)}`] : []),
      `# Lock time: ${s.lock_time_ms == null ? "unknown" : time(s.lock_time_ms)}`,
      `# Rows examined: ${number(s.rows_examined)} · Rows sent: ${number(s.rows_sent)} · Ratio: ${ratio(s.rows_examined, s.rows_sent)}`,
      `# Temp tables: ${count(s.tmp_tables)} · On disk: ${count(s.tmp_disk_tables)}`,
      `# Full scans: ${count(s.select_scan)} · Full joins: ${count(s.select_full_join)}`,
      `# First seen: ${s.first_seen || "unknown"} · Last seen: ${s.last_seen || "unknown"}`,
      "# Normalized SQL:", s.digest_text || "# Not recorded", "# Sample:", s.sample_text || "# Not recorded"));
    return lines.join("\n");
  }
  function csvReport() {
    const header = ["Rank", "Query Digest", "Schema", "Calls", "Total Time (ms)", "Avg Time (ms)", "Max Time (ms)",
      "Lock Time (ms)", "Rows Examined", "Rows Sent", "Examined/Sent Ratio", "Disk Temp Tables", "Total Temp Tables",
      "Full Table Scans", "Full Joins", "Fingerprint", "Sample SQL", "P95 (ms)", "P99 (ms)", "P99.9 (ms)", "CPU Time (ms)",
      "First Seen", "New"];
    return [header, ...rows.map(({ statement: s, rank }) => [rank, s.digest, s.schema, s.count, s.total_ms, s.avg_ms,
      s.max_ms, s.lock_time_ms, s.rows_examined, s.rows_sent, s.rows_sent > 0 ? s.rows_examined / s.rows_sent : null,
      s.tmp_disk_tables, s.tmp_tables, s.select_scan, s.select_full_join, s.digest_text, s.sample_text,
      s.p95_ms, s.p99_ms, s.p999_ms, cpu && cpu.collected !== false ? s.cpu_ms : null, s.first_seen, s.new ? "yes" : ""])]
      .map((row) => row.map((value) => csvCell(value == null ? "" : value)).join(",")).join("\n");
  }
  function filename(extension: string) { return `query-digest-${Date.now()}.${extension}`; }
  const notes = report ? reportNotes(report) : [];
  const age = report?.coverage?.uptime_s;
  const summary = report ? <SummaryLine notes={notes}>
    {rows.length} statements · {time(totals.ms)} total · {number(totals.calls)} executions
    {report.filter !== "slow_queries" && age != null &&
      <> · <span title={`Counted since the server started ${durationText(age)} ago, or since the digest table was last reset.`}>since {durationText(age)} ago</span></>}
    {shown.length !== rows.length ? ` · ${shown.length} shown` : ""}
  </SummaryLine> : null;
  return <InspectorContext.Provider value={shared}>
    <InstrumentsLinkContext.Provider value={onOpenInstruments || null}>
    <div className="qd-place" ref={root}>
      <ViewLayout id="query-digest" title="Query digest" className="qd-view" detailBackLabel=""
        otherConnection={other} inspector={inspector} onRefresh={() => { void load(); }} loading={!!reads.loading}
        info={<>
          <p>{report ? sourceSentence(report) : <>Reads <code>{DIGEST_TABLE}</code> or, for slow queries, <code>mysql.slow_log</code>.</>}</p>
          <p>Share of total time compares each statement with every digest the server keeps; the totals cover the loaded statements.</p>
        </>}
        controls={<>
          <label className="toolbar-field">Rank by <Select label="Rank by" value={filter} options={MODES} onChange={(v) => setFilter(v as DigestFilterMode)} /></label>
          <label className="toolbar-field qd-schema">Schema <Select label="Schema" value={schemaFilter} options={schemaOptions} disabled={!report} onChange={setSchemaFilter} /></label>
          <Select label="Statements to load" value={limit} options={[[20, "Top 20"], [50, "Top 50"], [100, "Top 100"]]} onChange={(v) => setLimit(Number(v))} />
          <span className="qd-toolbar-break" aria-hidden="true" />
          <SearchField label="Filter statements" placeholder="Filter statements" value={search} onChange={setSearch} />
          <ExportMenu label="Export query digest" disabled={!report} formats={[
            { key: "text", label: "Text report", onSelect: () => { setPreview({ text: textReport(), filename: filename("txt") }); setExportOpen(true); } },
            { key: "csv", label: "CSV", onSelect: () => { const name = filename("csv"); downloadFile(name, csvReport(), "text/csv"); setExportNote(`Saved ${name}.`); } },
          ]} />
        </>}
        status={<>{summary && <div className="qd-summary">{summary}</div>}
          <ViewStatus loading={reads.loading} error={reads.error} onStop={reads.stop} onRetry={() => { void load(); }} doneAt={reads.doneAt} />
          {exportNote && <p className="qd-feedback" role="status">{exportNote}</p>}
        </>}>
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
            { label: "Copy digest", disabled: !r.statement.digest,
              onClick: () => { void copy(r.statement.digest || ""); close(); } },
          ]}
          empty={<EmptyState icon="statements" title={filtered && rows.length ? "No matching statements" : "No statements recorded"}>
            {filtered && rows.length ? "Change the schema or the filter to show other loaded statements." : reads.loading ? "Reading statements…" : "Refresh to read the selected source again."}
          </EmptyState>} />
        {others && serverMs && <p className="qd-others">
          {`${number(others.digests)} other ${others.digests === 1 ? "statement" : "statements"} · ${percent(others.total_ms, serverMs)} of total time · ${compact(others.executions)} ${others.executions === 1 ? "call" : "calls"}`}
        </p>}
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
