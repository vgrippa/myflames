// Server > Overview > Error log. Reads when it opens on screen (once per
// connection; Refresh reads again, and so does Refresh all); a server whose
// error log myflames cannot read says why. Filters by level, run, search, and
// date/time range. Exports to CSV, .log, and JSON.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api, download, type Connection, type ErrorLogCode, type ErrorLogEntry, type ErrorLogReport } from "./api";
import { useConnectionReads, type Bound } from "./binding";
import { useOpenRead } from "./viewOnScreen";
import { Icon } from "./components";
import { bytes, CompactNumber, count, engineLabel } from "./PerfShared";
import {
  Checkbox, copyToClipboard, DataTable, Disclosure, EmptyState, FactStrip, Fields, InspectorSection,
  Popover, PrerequisiteGate, SearchField, Segmented, StatusPill, SummaryLine, ToolbarSelect, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Segment, type Selection, type Tone,
} from "./kit";
import "./errorlog.css";

type Level = "all" | ErrorLogEntry["prio"];
type Grouping = "entries" | "codes";
export type TimePreset = "all" | "1h" | "6h" | "24h" | "custom";

const LEVELS: Segment<Level>[] = [
  { value: "all", label: "All" }, { value: "Error", label: "Errors" },
  { value: "Warning", label: "Warnings" }, { value: "Note", label: "Notes" },
  { value: "System", label: "System" },
];

export const TIME_PRESETS: [TimePreset, string][] = [
  ["all", "All time"],
  ["1h", "Last hour"],
  ["6h", "Last 6 hours"],
  ["24h", "Last 24 hours"],
  ["custom", "Custom range"],
];

const levelTone = (prio: string): Tone => prio === "Error" ? "problem" : prio === "Warning" ? "watch" : "info";
const levelBadge = (prio: string) => <StatusPill tone={levelTone(prio)} label={prio} />;

/** Shared with Refresh all: the exact same read is single-flight in binding.ts. */
export const ERROR_LOG_READ = "errorlog:200";
export const errorLogBody = (connection: Connection) => ({ connection, limit: 200 });

// Include the message in the identity: different events can share a timestamp
// and thread. Occurrence is added below for completely identical entries.
const entryIdentity = (entry: ErrorLogEntry) => JSON.stringify([
  entry.logged, entry.thread_id, entry.prio, entry.error_code, entry.subsystem, entry.message,
]);
export type EntryRow = ErrorLogEntry & { key: string };
export type CodeRow = ErrorLogCode & { key: string };

export function parseLogTimestamp(val: string): number {
  if (!val) return NaN;
  const trimmed = val.trim();
  let parsed = Date.parse(trimmed);
  if (!isNaN(parsed)) return parsed;
  const normalized = trimmed.replace(
    /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$/,
    (_, d, tm, ms, rest) => {
      const millis = ms ? ms.slice(0, 4) : "";
      return `${d}T${tm}${millis}${rest || ""}`;
    }
  );
  parsed = Date.parse(normalized);
  if (!isNaN(parsed)) return parsed;
  return NaN;
}

export function escapeCsv(val: unknown): string {
  if (val === null || val === undefined) return "";
  const str = String(val);
  if (/[",\r\n]/.test(str)) {
    return `"${str.replace(/"/g, '""')}"`;
  }
  return str;
}

export function formatErrorLogLine(e: ErrorLogEntry): string {
  const thread = e.thread_id !== null && e.thread_id !== undefined ? String(e.thread_id) : "0";
  const code = e.error_code ? `[${e.error_code}] ` : "";
  const subsys = e.subsystem ? `[${e.subsystem}] ` : "";
  return `${e.logged} ${thread} [${e.prio}] ${code}${subsys}${e.message}`;
}

export function exportCsv(entries: EntryRow[], dateStr: string) {
  const headers = ["Logged", "Level", "Code", "Subsystem", "Message"];
  const rows = entries.map((e) =>
    [e.logged, e.prio, e.error_code ?? "", e.subsystem ?? "", e.message]
      .map(escapeCsv)
      .join(",")
  );
  const csvContent = [headers.join(","), ...rows].join("\r\n");
  download(csvContent, `error-log-${dateStr}.csv`, "text/csv;charset=utf-8");
}

export function exportLog(entries: EntryRow[], dateStr: string) {
  const lines = entries.map(formatErrorLogLine);
  download(lines.join("\n") + (lines.length ? "\n" : ""), `error-log-${dateStr}.log`, "text/plain;charset=utf-8");
}

export function exportJson(report: ErrorLogReport, entries: EntryRow[], dateStr: string, connection: Connection) {
  const data = {
    schema_version: "error-log-1.0",
    exported_at: new Date().toISOString(),
    connection: connection.name || `${connection.user ? `${connection.user}@` : ""}${connection.host}${connection.port ? `:${connection.port}` : ""}`,
    engine: report.engine,
    server_version: report.server_version,
    source: report.source,
    settings: report.settings,
    total_entries: entries.length,
    entries: entries.map((e) => ({
      logged: e.logged,
      thread_id: e.thread_id,
      prio: e.prio,
      error_code: e.error_code,
      subsystem: e.subsystem,
      message: e.message,
      previous_run: e.previous_run,
    })),
  };
  download(JSON.stringify(data, null, 2), `error-log-${dateStr}.json`, "application/json");
}

function Settings({ report }: { report: ErrorLogReport }) {
  const rows: [ReactNode, ReactNode][] = Object.entries(report.settings)
    .filter(([, value]) => value !== null)
    .map(([name, value]) => [name, value === "" ? "(empty)" : String(value)]);
  return rows.length ? <Fields label="Error log settings" rows={rows} /> : null;
}

export function ErrorLogView({ connection, disabled, onBack, selection, report: bound, onReport }: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<ErrorLogReport> | null;
  onReport: (report: Bound<ErrorLogReport>) => void;
}) {
  const reads = useConnectionReads(connection);
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [level, setLevel] = useState<Level>("all");
  const [filter, setFilter] = useState("");
  const [thisRun, setThisRun] = useState(false);
  const [grouping, setGrouping] = useState<Grouping>("entries");
  const [copyStatus, setCopyStatus] = useState("");
  const [timePreset, setTimePreset] = useState<TimePreset>("all");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [exported, setExported] = useState("");

  // MariaDB has no System level: drop a System selection when its report
  // arrives, so it cannot come back unseen with the next MySQL report.
  const mariadb = report?.engine === "mariadb";
  useEffect(() => { if (mariadb) setLevel((old) => old === "System" ? "all" : old); }, [mariadb]);
  const selectedLevel = mariadb && level === "System" ? "all" : level;
  const needle = filter.trim().toLocaleLowerCase();
  const matches = (prio: string, code: string | null, subsystem: string | null, message: string) =>
    (selectedLevel === "all" || selectedLevel === prio) &&
    (!needle || [message, code, subsystem].some(value => value?.toLocaleLowerCase().includes(needle)));

  async function load() {
    setCopyStatus("");
    setExported("");
    await reads.run(ERROR_LOG_READ, "Reading the error log…",
      (c, signal) => api<ErrorLogReport>("live/error-log", errorLogBody(c), signal), onReport);
  }

  // Opening the view reads it: MySQL's performance_schema.error_log, or on
  // MariaDB the log file when the server is on this computer and its identity
  // is verified; otherwise the report says why it is not available.
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!reads.loading || reads.running(), read: () => { void load(); } });

  const occurrences = new Map<string, number>();
  const entries: EntryRow[] = (report?.entries || []).map(entry => {
    const identity = entryIdentity(entry);
    const occurrence = occurrences.get(identity) || 0;
    occurrences.set(identity, occurrence + 1);
    return { ...entry, key: `entry:${identity}:${occurrence}` };
  });
  const codes: CodeRow[] = (report?.codes || []).map(code => ({ ...code,
    // MariaDB has no code: the first 80 message characters are its group key.
    key: `code:${JSON.stringify([code.error_code, code.prio, code.subsystem, report?.engine === "mariadb" ? code.sample.slice(0, 80) : null])}`,
  }));

  const latestEntryTime = useMemo(() => {
    let max = 0;
    for (const e of entries) {
      const t = parseLogTimestamp(e.logged);
      if (!isNaN(t) && t > max) max = t;
    }
    return max;
  }, [entries]);

  const { filterStart, filterEnd } = useMemo(() => {
    if (timePreset === "all") return { filterStart: null, filterEnd: null };
    if (timePreset === "custom") {
      const s = customStart ? parseLogTimestamp(customStart) : NaN;
      let e = customEnd ? parseLogTimestamp(customEnd) : NaN;
      if (!isNaN(e) && customEnd.length === 16) {
        e += 59_999;
      }
      return {
        filterStart: !isNaN(s) ? s : null,
        filterEnd: !isNaN(e) ? e : null,
      };
    }
    const now = Date.now();
    const ref = latestEntryTime > 0 ? (Math.abs(now - latestEntryTime) < 30 * 86400_000 ? Math.max(now, latestEntryTime) : latestEntryTime) : now;
    const windowMs =
      timePreset === "1h" ? 3600_000 :
      timePreset === "6h" ? 6 * 3600_000 :
      24 * 3600_000;
    return {
      filterStart: ref - windowMs,
      filterEnd: null,
    };
  }, [timePreset, customStart, customEnd, latestEntryTime]);

  const inTimeRange = (timeStr: string) => {
    if (filterStart === null && filterEnd === null) return true;
    const t = parseLogTimestamp(timeStr);
    if (isNaN(t)) return true;
    if (filterStart !== null && t < filterStart) return false;
    if (filterEnd !== null && t > filterEnd) return false;
    return true;
  };

  const inCodeTimeRange = (code: CodeRow) => {
    if (filterStart === null && filterEnd === null) return true;
    const first = parseLogTimestamp(code.first_logged);
    const last = parseLogTimestamp(code.last_logged);
    if (filterStart !== null && !isNaN(last) && last < filterStart) return false;
    if (filterEnd !== null && !isNaN(first) && first > filterEnd) return false;
    return true;
  };

  const visibleEntries = entries.filter(e => (!thisRun || !e.previous_run) && matches(e.prio, e.error_code, e.subsystem, e.message) && inTimeRange(e.logged));
  const visibleCodes = codes.filter(c => matches(c.prio, c.error_code, c.subsystem, c.sample) && inCodeTimeRange(c));

  const handleExport = (format: "csv" | "log" | "json") => {
    const dateStr = new Date().toISOString().slice(0, 10);
    if (format === "csv") {
      exportCsv(visibleEntries, dateStr);
      setExported(`error-log-${dateStr}.csv`);
    } else if (format === "log") {
      exportLog(visibleEntries, dateStr);
      setExported(`error-log-${dateStr}.log`);
    } else if (format === "json") {
      exportJson(report!, visibleEntries, dateStr, connection);
      setExported(`error-log-${dateStr}.json`);
    }
  };

  const entryColumns: Column<EntryRow>[] = [
    { key: "time", label: "Time", sort: e => e.logged, render: e => e.logged },
    { key: "level", label: "Level", sort: e => e.prio, render: e => levelBadge(e.prio) },
    { key: "code", label: "Code", sort: e => e.error_code, render: e => e.error_code ?? "—" },
    { key: "subsystem", label: "Subsystem", sort: e => e.subsystem, render: e => e.subsystem ?? "—" },
    { key: "message", label: "Message", flex: true, sort: e => e.message, render: e => <span className="cell-truncate">{e.message}</span> },
  ];
  const codeColumns: Column<CodeRow>[] = [
    { key: "code", label: "Code", sort: c => c.error_code, render: c => c.error_code ?? "—" },
    { key: "level", label: "Level", sort: c => c.prio, render: c => levelBadge(c.prio) },
    { key: "subsystem", label: "Subsystem", sort: c => c.subsystem, render: c => c.subsystem ?? "—" },
    { key: "count", label: "Count", num: true, sort: c => c.count, render: c => <CompactNumber value={c.count} /> },
    { key: "last", label: "Last seen", sort: c => c.last_logged, render: c => c.last_logged },
    { key: "sample", label: "Sample", flex: true, sort: c => c.sample, render: c => <span className="cell-truncate">{c.sample}</span> },
  ];

  let inspector: InspectorContent | null = null;
  const entry = report?.available && entries.find(e => e.key === selection.key);
  const code = report?.available && codes.find(c => c.key === selection.key);
  if (entry) inspector = {
    key: entry.key, title: entry.error_code || "Log entry", meta: levelBadge(entry.prio),
    body: <>
      <InspectorSection title="Message"><pre className="inspector-sql">{entry.message}</pre></InspectorSection>
      <InspectorSection title="Entry"><Fields rows={[
        ["Time", entry.logged], ["Thread id", entry.thread_id ?? "—"],
        ["Code", entry.error_code ?? "—"], ["Subsystem", entry.subsystem ?? "—"],
      ]} />{entry.previous_run && <p className="hint">From a previous run</p>}</InspectorSection>
    </>,
  };
  else if (code) inspector = {
    key: code.key, title: code.error_code || "Log group", meta: levelBadge(code.prio),
    body: <>
      <InspectorSection title="Sample message"><pre className="inspector-sql">{code.sample}</pre></InspectorSection>
      <InspectorSection title="Group"><Fields rows={[
        ["Code", code.error_code ?? "—"], ["Subsystem", code.subsystem ?? "—"], ["Count", count(code.count)],
        ["First seen", code.first_logged], ["Last seen", code.last_logged],
      ]} /><p className="hint">Counts cover all entries in the source, including previous runs. Select an entry for its thread id and run.</p></InspectorSection>
    </>,
  };

  // The filters sit on the row above the list, like Indexes and Tables, so
  // the view toolbar keeps one row at 1440 px; Export ends the row.
  const tools = <div className="report-table-tools errorlog-tools" role="group" aria-label="Error log filters">
      <Segmented<Grouping> label="Error log grouping" value={grouping} onChange={setGrouping}
        options={[{ value: "entries", label: "Entries" }, { value: "codes", label: "By code" }]} />
      <Segmented<Level> label="Log level" value={selectedLevel} onChange={setLevel}
        options={LEVELS.filter(l => !mariadb || l.value !== "System")} />
      <ToolbarSelect
        label="Time"
        className="errorlog-time-select"
        value={timePreset}
        onChange={(val) => setTimePreset(val as TimePreset)}
        options={TIME_PRESETS}
      />
      {timePreset === "custom" && (
        <div className="errorlog-custom-range" role="group" aria-label="Custom time range">
          <label className="toolbar-field errorlog-datetime-field">
            <span>Start</span>
            <input
              type="datetime-local"
              aria-label="Start time"
              value={customStart}
              onChange={(e) => setCustomStart(e.target.value)}
            />
          </label>
          <label className="toolbar-field errorlog-datetime-field">
            <span>End</span>
            <input
              type="datetime-local"
              aria-label="End time"
              value={customEnd}
              onChange={(e) => setCustomEnd(e.target.value)}
            />
          </label>
        </div>
      )}
      <SearchField label="Filter error log" placeholder="Filter entries" value={filter} onChange={setFilter} />
      <Checkbox checked={thisRun} onChange={setThisRun} disabled={grouping === "codes"}
        title={grouping === "codes" ? "Grouped counts include previous runs; use Entries to filter by run." : undefined}>This run only</Checkbox>
      <Popover
        label="Export error log"
        className="errorlog-export"
        buttonClassName="button"
        disabled={!report?.available || !visibleEntries.length}
        buttonTitle="Export the entries that match these filters"
        trigger={
          <>
            <Icon name="export" size={15} />
            <span>Export</span>
          </>
        }
      >
        <h3 className="popover-title">Export error log</h3>
        <div className="errorlog-export-formats" role="group" aria-label="Export formats">
          <button type="button" className="button" onClick={() => handleExport("csv")}>
            CSV
          </button>
          <button type="button" className="button" onClick={() => handleExport("log")}>
            Log (.log)
          </button>
          <button type="button" className="button" onClick={() => handleExport("json")}>
            JSON
          </button>
        </div>
        <p className="hint">
          Exports {visibleEntries.length} matching {visibleEntries.length === 1 ? "entry" : "entries"}.
        </p>
        {exported && (
          <p className="hint" role="status">
            Saved {exported}
          </p>
        )}
      </Popover>
  </div>;

  return <ViewLayout id="errorlog" title="Error log" onBack={onBack}
    info={<><p>MySQL reads performance_schema.error_log. MariaDB reads the error log file only when the server runs on this computer.</p>
      <p>Filters apply to the loaded entries; the text filter matches the message, code and subsystem. By code uses the server's grouped counts across the source, including previous runs.</p></>}
    onRefresh={() => void load()} refreshDisabled={disabled} loading={!!reads.loading} otherConnection={other}
    inspector={inspector} inspectorEmpty={<p>Select an entry or code to see the full message and details.</p>}
    status={<ViewStatus loading={reads.loading} error={reads.error} />}>
    {!report && !reads.loading && !reads.error && <EmptyState icon="errorlog" title="Error log">
      {disabled ? "Read when the Query editor finishes its capture." : "MySQL: performance_schema.error_log. MariaDB: the error log file, only when the server runs on this computer."}
    </EmptyState>}
    {report && <>
      <SummaryLine tone={report.summary.status} notes={report.notes}>{report.summary.text}</SummaryLine>
      <FactStrip label="Error log source" facts={[
        ["Server", engineLabel(report)], ["Source", report.source ?? "Not available"], ["Server start", report.server_started ?? "—"],
        ...(report.buffer ? [["Buffer", `${count(report.buffer.events)} events · ${bytes(report.buffer.bytes)} of ${bytes(report.buffer.capacity_bytes)}`] as [string, ReactNode]] : []),
        ...(report.file ? [["File read", `${bytes(report.file.read_bytes)} of ${bytes(report.file.size_bytes)}`] as [string, ReactNode]] : []),
      ]} />
      {!report.available ? <>
        <PrerequisiteGate>{report.reason_text || "The error log is not available on this connection."}</PrerequisiteGate>
        <Settings report={report} />
        {report.how_to_view && <section className="view-section" aria-label="How to view the error log">
          <h3>How to view</h3><pre className="inspector-sql">{report.how_to_view}</pre>
          <button type="button" className="button" onClick={() => {
            void copyToClipboard(report.how_to_view!).then(ok => setCopyStatus(ok ? "Copied" : "Could not copy; select the text to copy it."));
          }}>Copy instructions</button><span className="hint" role="status">{copyStatus}</span>
        </section>}
      </> : <>
        <section className="view-section" aria-label="Error log rows">
          {tools}
          {grouping === "entries" ? <DataTable key="entries" id="errorlog.entries" label="Error log entries" columns={entryColumns}
            rows={visibleEntries} rowKey={e => e.key} selection={selection} defaultSort={{ key: "time", dir: "desc" }}
            rowAttrs={e => ({ "data-previous-run": String(e.previous_run), "data-level": e.prio })}
            empty="No loaded entries match these filters." />
            : <DataTable key="codes" id="errorlog.codes" label="Error log by code" columns={codeColumns}
              rows={visibleCodes} rowKey={c => c.key} selection={selection} defaultSort={{ key: "count", dir: "desc" }}
              empty="No code groups match these filters." />}
          <p className="grouped-footer">{grouping === "entries"
            ? `${count(visibleEntries.length)} shown · ${count(entries.length)} loaded of ${count(report.entries_total)} matching entries · limit ${count(report.limit)}`
            : "Grouped counts cover all entries in the source, including previous runs."}</p>
        </section>
        <Disclosure title="Source details">
          <Settings report={report} />
          <Fields rows={[
            ["Levels read", report.levels.join(", ")],
            ...(["Error", "Warning", "Note", ...(report.engine === "mariadb" ? [] : ["System"])]).map(label => {
              const key = label.toLowerCase() as keyof ErrorLogReport["counts"];
              return [label, `${count(report.counts_current_run[key])} this run · ${count(report.counts[key])} in source`] as [ReactNode, ReactNode];
            }),
            ...(report.buffer ? [["Expired events", count(report.buffer.expired_events)], ["Latest write", report.buffer.latest_write ?? "—"]] as [ReactNode, ReactNode][] : []),
            ...(report.file ? [["Path", report.file.path], ["Tail only", report.file.truncated ? "Yes" : "No"], ["Startup banner found", report.file.banner_found ? "Yes" : "No"]] as [ReactNode, ReactNode][] : []),
          ]} />
        </Disclosure>
      </>}
    </>}
  </ViewLayout>;
}
