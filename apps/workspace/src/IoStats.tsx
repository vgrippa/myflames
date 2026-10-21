// Server > I/O: the tables and files with the most I/O over a short window,
// server-wide InnoDB rates, and the same since server start (io-1.0 from
// myflames/io_stats.py). Per-table reads cost time on servers with many
// tables, so they run only on Refresh (no automatic refresh). Compact tiles
// for the server-wide rates, then one table at a time (tables, files, file
// kinds); the selected row's details and links are in the inspector.
import { useState } from "react";
import { useOpenRead } from "./viewOnScreen";
import { api, type Connection, type IoFile, type IoFileClass, type IoGlobal, type IoReport, type IoTable } from "./api";
import { WINDOW_INTERVALS } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { PriorityTable, CompactNumber, SchemaField, WindowSelect, bytes, count, engineLabel, ms } from "./PerfShared";
import { uptime } from "./Health";
import { InstrumentsLink } from "./Instruments";
import {
  Disclosure,
  EmptyState,
  FactStrip,
  Fields,
  InspectorSection,
  MetricTile,
  Pill,
  PrerequisiteGate,
  Segmented,
  SummaryLine,
  TileGrid,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
} from "./kit";

const SOURCES: Record<string, string> = {
  performance_schema: "Performance Schema",
  userstat: "User statistics (userstat)",
  status: "server-wide counters only",
};

/** Key server-wide InnoDB I/O figures shown as a compact summary row above the table. */
const SUMMARY_GLOBAL_KEYS = ["data_read", "data_written", "redo_written", "data_fsyncs"] as const;

/** A server-wide rate as value and unit: ["1.5", "MiB/s"], ["12", "/s"]. */
function rate(metric: IoGlobal): [string, string] {
  if (metric.rate === null) return ["—", ""];
  if (metric.unit === "B/s") {
    const [n, unit] = bytes(metric.rate).split(" ");
    return [n, `${unit}/s`];
  }
  if (metric.unit === "pages/s") return [count(metric.rate), "pages/s"];
  return [count(metric.rate), "/s"];
}

function fileName(f: IoFile) {
  if (f.kind === "table" && f.table) return `${f.schema}.${f.table}${f.partition ? ` (${f.partition})` : ""}`;
  return f.file.replace(/\\/g, "/").split("/").pop() || f.file;
}

type Show = "tables" | "files" | "kinds";
type Scope = "window" | "since";

export function IoView({
  connection,
  disabled,
  onBack,
  selection,
  interval,
  onIntervalChange,
  schema,
  onSchemaChange,
  report: bound,
  onReport,
  onOpenIndexes,
  onOpenTop,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  interval: number;
  onIntervalChange: (seconds: number) => void;
  schema: string;
  onSchemaChange: (schema: string) => void;
  report: Bound<IoReport> | null;
  onReport: (report: Bound<IoReport>) => void;
  /** Links that read with this report's schema; *from* is the report's connection key. */
  onOpenIndexes: (schema: string, from: string) => void;
  onOpenTop: (schema: string, from: string) => void;
}) {
  const [show, setShow] = useState<Show>("tables");
  const [scope, setScope] = useState<Scope>("window");
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;

  async function load(allInstances = false) {
    const target = schema.trim();
    await reads.run(
      `io:${interval}:${target}${allInstances ? ":all" : ""}`,
      `Sampling I/O for ${interval} s…`,
      (c, signal) => api<IoReport>("live/io", {
        connection: c, interval, ...(target ? { schema: target } : {}),
        ...(allInstances ? { all_instances: true } : {}),
      }, signal),
      onReport,
    );
  }

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!loading || reads.running(), read: () => { void load(); } });

  const w = report?.window;
  const since = report?.since_start;
  const timed = report?.source === "performance_schema";
  const perTable = !!report && report.source !== "status";
  const restarted = w?.status === "restarted";
  const tables: IoTable[] = scope === "window" ? (restarted ? [] : w?.tables || []) : since?.tables || [];
  const files: IoFile[] = scope === "window" ? (restarted ? [] : w?.files || []) : since?.files || [];
  const kinds: IoFileClass[] = scope === "window" ? w?.file_classes || [] : since?.file_classes || [];
  const showFiles = timed;
  const current: Show = !showFiles && show !== "tables" ? "tables" : show;
  const tablesOmitted = report?.collection?.omitted.some((kind) => kind === "tables" || kind === "userstat");
  const filesOmitted = report?.collection?.omitted.includes("files");
  const summaryGlobal = report
    ? SUMMARY_GLOBAL_KEYS.map((k) => report.global.find((m) => m.key === k)).filter((m): m is IoGlobal => !!m)
    : [];

  const tableColumns: Column<IoTable>[] = [
    { key: "table", flex: true, label: "Table", sort: (t) => `${t.schema}.${t.table}`, render: (t) => <code>{t.schema}.{t.table}</code> },
    { key: "read", label: "Rows read", num: true, sort: (t) => t.read_rows, render: (t) => <CompactNumber value={t.read_rows} /> },
    ...(timed ? [{ key: "readms", label: "Read time (ms)", num: true, sort: (t: IoTable) => t.read_ms, render: (t: IoTable) => <CompactNumber value={t.read_ms} /> }] : []),
    { key: "write", label: "Rows written", num: true, sort: (t) => t.write_rows, render: (t) => <CompactNumber value={t.write_rows} /> },
    ...(timed
      ? [
          { key: "writems", label: "Write time (ms)", num: true, sort: (t: IoTable) => t.write_ms, render: (t: IoTable) => <CompactNumber value={t.write_ms} /> },
          { key: "total", label: "Total time (ms)", num: true, sort: (t: IoTable) => t.total_ms, render: (t: IoTable) => <CompactNumber value={t.total_ms} /> },
        ]
      : []),
  ];
  const fileColumns: Column<IoFile>[] = [
    {
      key: "file", flex: true,
      label: "File",
      sort: (f) => fileName(f),
      render: (f) => (
        <span className="cell-name">
          <code title={f.file}>{fileName(f)}</code> <Pill>{f.label}</Pill>
        </span>
      ),
    },
    { key: "read", label: "Read (B)", num: true, sort: (f) => f.read_bytes, render: (f) => <CompactNumber value={f.read_bytes} /> },
    { key: "written", label: "Written (B)", num: true, sort: (f) => f.write_bytes, render: (f) => <CompactNumber value={f.write_bytes} /> },
    { key: "readms", label: "Read time (ms)", num: true, sort: (f) => f.read_ms, render: (f) => <CompactNumber value={f.read_ms} /> },
    { key: "writems", label: "Write time (ms)", num: true, sort: (f) => f.write_ms, render: (f) => <CompactNumber value={f.write_ms} /> },
    { key: "total", label: "Total time (ms)", num: true, sort: (f) => f.total_ms, render: (f) => <CompactNumber value={f.total_ms} /> },
  ];
  const kindColumns: Column<IoFileClass>[] = [
    { key: "kind", flex: true, label: "Kind", sort: (c) => c.event, render: (c) => <code>{c.event}</code> },
    { key: "read", label: "Read (B)", num: true, sort: (c) => c.read_bytes, render: (c) => <CompactNumber value={c.read_bytes} /> },
    { key: "written", label: "Written (B)", num: true, sort: (c) => c.write_bytes, render: (c) => <CompactNumber value={c.write_bytes} /> },
    { key: "total", label: "Total time (ms)", num: true, sort: (c) => c.total_ms, render: (c) => <CompactNumber value={c.total_ms} /> },
  ];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  if (report && type === "global") {
    const m = report.global.find((g) => g.key === name);
    if (m) {
      const [value, unit] = rate(m);
      inspector = {
        key: selection.key,
        title: m.label,
        meta: <span>{`${value} ${unit}`.trim()}</span>,
        body: (
          <>
            <InspectorSection title="What it measures">
              <p>{m.explain}</p>
            </InspectorSection>
            <InspectorSection title="Numbers">
              <Fields rows={[["Over the window", m.delta === null ? "—" : count(m.delta)], ["Rate", `${value} ${unit}`.trim()]]} />
            </InspectorSection>
            <p className="hint">From {m.sources.join(", ")}</p>
          </>
        ),
      };
    }
  } else if (report && type === "table") {
    const t = tables.find((x) => `${x.schema}.${x.table}` === name);
    if (t)
      inspector = {
        key: selection.key,
        title: `${t.schema}.${t.table}`,
        meta: <span>{scope === "window" ? "Over the window" : "Since server start"}</span>,
        actions: (
          <>
            <button type="button" className="button" disabled={!!other} onClick={() => bound && onOpenIndexes(t.schema, bound.key)}>
              Index health
            </button>
            <button type="button" className="button" disabled={!!other} onClick={() => bound && onOpenTop(t.schema, bound.key)}>
              Top statements
            </button>
          </>
        ),
        body: (
          <>
            <InspectorSection title="Rows">
              <Fields
                rows={[
                  ["Read", count(t.read_rows)],
                  ["Written", count(t.write_rows)],
                  t.inserts !== null && ["Inserted", count(t.inserts)],
                  t.updates !== null && ["Updated", count(t.updates)],
                  t.deletes !== null && ["Deleted", count(t.deletes)],
                ]}
              />
            </InspectorSection>
            {timed && (
              <InspectorSection title="Time">
                <Fields
                  rows={[
                    ["Read", ms(t.read_ms)],
                    ["Write", ms(t.write_ms)],
                    ["Total", ms(t.total_ms)],
                  ]}
                />
                <p className="hint">Includes the file I/O inside each call. Rows, not bytes: the files show bytes.</p>
              </InspectorSection>
            )}
          </>
        ),
      };
  } else if (report && type === "file") {
    const f = files.find((x) => `${x.file}|${x.event}` === name);
    if (f)
      inspector = {
        key: selection.key,
        title: fileName(f),
        meta: <Pill>{f.label}</Pill>,
        body: (
          <>
            <InspectorSection title="File">
              <p className="io-path">
                <code>{f.file}</code>
              </p>
              <p className="hint">
                <code>{f.event}</code>
              </p>
            </InspectorSection>
            <InspectorSection title="Reads and writes">
              <Fields
                rows={[
                  ["Read", `${bytes(f.read_bytes)} in ${count(f.read_ops)} reads, ${ms(f.read_ms)}`],
                  ["Written", `${bytes(f.write_bytes)} in ${count(f.write_ops)} writes, ${ms(f.write_ms)}`],
                  ["Other", `${count(f.misc_ops)} operations, ${ms(f.misc_ms)}`],
                  ["Total time", ms(f.total_ms)],
                ]}
              />
              <p className="hint">Asynchronous InnoDB reads and writes are timed only for submitting the request.</p>
            </InspectorSection>
          </>
        ),
      };
  } else if (report && type === "kind") {
    const c = kinds.find((x) => x.event === name);
    if (c)
      inspector = {
        key: selection.key,
        title: c.event,
        body: (
          <>
            {c.meaning && (
              <InspectorSection title="What it is">
                <p>{c.meaning}</p>
              </InspectorSection>
            )}
            <InspectorSection title="Reads and writes">
              <Fields
                rows={[
                  ["Read", `${bytes(c.read_bytes)} in ${count(c.read_ops)} reads`],
                  ["Written", `${bytes(c.write_bytes)} in ${count(c.write_ops)} writes`],
                  ["Total time", ms(c.total_ms)],
                ]}
              />
              <p className="hint">Includes temporary files that were closed during the window, such as sort and temporary-table spills.</p>
            </InspectorSection>
          </>
        ),
      };
  }

  return (
    <ViewLayout
      id="io"
      title="I/O"
      className="io-panel"
      onBack={onBack}
      info={
        <>
          <p>Which tables and files did the most reading and writing over a short window, with server-wide InnoDB rates that work without Performance Schema.</p>
          <p>Two reads a few seconds apart, only when you click: per-table reads cost time on servers with many tables. A schema keeps the table read small.</p>
        </>
      }
      controls={
        <>
          <SchemaField id="io-schema" value={schema} onChange={onSchemaChange} disabled={loading} />
          <WindowSelect value={interval} options={WINDOW_INTERVALS} onChange={onIntervalChange} disabled={loading} />
          {perTable && (
            <Segmented<Scope>
              label="Time range"
              value={scope}
              onChange={setScope}
              options={[
                { value: "window", label: "Over the window" },
                { value: "since", label: "Since server start", disabled: !since },
              ]}
            />
          )}
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a rate, a table or a file to see its numbers and related views.</p>}
      status={<ViewStatus loading={reads.loading} error={error} />}
    >
      {!report && !loading && !error && (
        <EmptyState
          icon="io"
          title="No I/O sample yet"
          action={
            <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
              Load I/O
            </button>
          }
        >
          The tables and files with the most reading and writing over {interval} s. It only reads.
        </EmptyState>
      )}
      {report && (
        <>
          {!!report.collection?.omitted.length && (
            <PrerequisiteGate action={!report.collection.all_instances && (
              <button type="button" className="button" disabled={disabled || loading || !!other} onClick={() => void load(true)}>
                Read all anyway
              </button>
            )}>
              {report.collection.omitted.map((kind) => kind === "userstat" ? "tables" : kind).join(" and ")} omitted by the {report.collection.all_instances ? "" : `${report.collection.instance_limit.toLocaleString()} instance limit or `}{report.collection.budget_s} s time budget.
            </PrerequisiteGate>
          )}
          {report.source === "status" && !report.performance_schema && (
            <PrerequisiteGate action={<InstrumentsLink row="ps" />}>
              {report.notes[0] || "Performance Schema is off, so only server-wide rates are shown."}
            </PrerequisiteGate>
          )}
          <SummaryLine notes={report.source === "status" && !report.performance_schema ? report.notes.slice(1) : report.notes}>
            {report.elapsed_s ? `${report.elapsed_s.toFixed(1)} s window` : "No window"} · tables from {SOURCES[report.source]}
            {report.scope.schema ? ` · schema ${report.scope.schema}` : ""}
          </SummaryLine>
          <FactStrip label="Server" facts={[["Server", engineLabel(report)], ["Uptime", uptime(report.uptime_s)]]} />
          {perTable && summaryGlobal.length > 0 && (
            <section className="tile-section io-summary-tiles" aria-labelledby="io-summary-h">
              <div className="tile-section-head">
                <h3 id="io-summary-h">Server-wide InnoDB I/O</h3>
              </div>
              <TileGrid label="Server-wide InnoDB I/O summary">
                {summaryGlobal.map((m) => {
                  const [value, unit] = rate(m);
                  return (
                    <MetricTile
                      key={m.key}
                      rowKey={`global:${m.key}`}
                      selection={selection}
                      label={m.label}
                      value={value}
                      unit={unit}
                      attrs={{ "data-metric": m.key }}
                    />
                  );
                })}
              </TileGrid>
            </section>
          )}
          {perTable && (
            <section className="view-section io-detail" aria-labelledby="io-detail-h">
              <div className="view-section-head">
                <h3 id="io-detail-h" className="visually-hidden">
                  I/O by table and file
                </h3>
                <Segmented<Show>
                  label="Show"
                  value={current}
                  onChange={setShow}
                  options={[
                    { value: "tables", label: "Tables", count: tables.length },
                    ...(showFiles
                      ? [
                          { value: "files" as Show, label: "Files", count: files.length },
                          { value: "kinds" as Show, label: "File kinds", count: kinds.length },
                        ]
                      : []),
                  ]}
                />

              </div>
              {scope === "window" && restarted ? (
                <div className="table-empty">The server restarted between the two reads. Refresh to sample again.</div>
              ) : current === "tables" ? (
                <PriorityTable
                  id="io.tables"
                  detailColumns={["readms", "writems"]}
                  label={scope === "window" ? "Tables over the window" : "Tables since server start"}
                  columns={tableColumns}
                  rows={tables}
                  rowKey={(t) => `table:${t.schema}.${t.table}`}
                  selection={selection}
                  rowAttrs={(t) => ({ "data-table": t.table })}
                  empty={tablesOmitted ? "Table I/O was not collected." : (
                    <>
                      No table I/O {scope === "window" ? "in this window" : "since server start"}
                      {report.scope.schema ? ` in ${report.scope.schema}` : ""}.
                      {scope === "window" && !!since?.tables.length && (
                        <>
                          {" "}
                          <button type="button" className="text-button" onClick={() => setScope("since")}>
                            Show the {since.tables.length} tables since server start
                          </button>
                        </>
                      )}
                    </>
                  )}
                />
              ) : current === "files" ? (
                <PriorityTable
                  id="io.files"
                  detailColumns={["readms", "writems"]}
                  label={scope === "window" ? "Files over the window" : "Files since server start"}
                  columns={fileColumns}
                  rows={files}
                  rowKey={(f) => `file:${f.file}|${f.event}`}
                  selection={selection}
                  rowAttrs={(f) => ({ "data-kind": f.kind })}
                  empty={filesOmitted ? "File I/O was not collected." : "No file I/O in this range."}
                />
              ) : (
                <PriorityTable
                  id="io.kinds"
                  detailColumns={[]}
                  label={scope === "window" ? "File kinds over the window" : "File kinds since server start"}
                  columns={kindColumns}
                  rows={kinds}
                  rowKey={(c) => `kind:${c.event}`}
                  selection={selection}
                  rowAttrs={(c) => ({ "data-event": c.event })}
                  empty="No file kinds in this range."
                />
              )}
            </section>
          )}
          {report.global.length > 0 && (
            <Disclosure title="Server-wide InnoDB I/O" className="io-global-rates" open={!perTable}>
              <TileGrid label="Server-wide InnoDB I/O">
                {report.global.map((m) => {
                  const [value, unit] = rate(m);
                  return (
                    <MetricTile
                      key={m.key}
                      rowKey={`global:${m.key}`}
                      selection={selection}
                      label={m.label}
                      value={value}
                      unit={unit}
                      attrs={{ "data-metric": m.key }}
                    />
                  );
                })}
              </TileGrid>
            </Disclosure>
          )}
        </>
      )}
    </ViewLayout>
  );
}
