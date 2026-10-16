// Server > Top statements: Performance Schema statement digests, heaviest
// first (top-1.0). A dense sortable table; the selected statement's full SQL,
// numbers, flags and actions (Explain, Run analysis, Use this query) are in the
// inspector. Explain top N runs EXPLAIN for the first N runnable SELECTs and
// adds their plans without leaving Server.
//
// useTopStatements keeps the report and the batch in the Server place, so they
// survive switching views (and the batch keeps running).
import { useEffect, useId, useRef, useState } from "react";
import {
  api,
  number,
  time,
  type Capture,
  type Connection,
  type Plan,
  type TopCoverage,
  type TopOrder,
  type TopReport,
  type TopStatement,
  type TopTable,
} from "./api";
import { normalizeSource } from "./session";
import { InstrumentsLink } from "./Instruments";
import { PriorityTable, CompactNumber, CopySql, SchemaField } from "./PerfShared";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { Icon } from "./components";
import {
  NumberField,
  EmptyState,
  Fields,
  InspectorSection,
  NotesButton,
  PrerequisiteGate,
  SearchField,
  SummaryLine,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
} from "./kit";
import { digestName, digestQuery } from "./placeholders";
import { isPrivilegeDenial, privilegeButtonProps, usePrivilege, usePrivileges } from "./privileges";
import "./digest.css";

/** What a Top statements row asks the editor to do. */
export type DigestAction = "open" | "explain" | "analyze";
/** Query editor settings a row's Run analysis confirmation and Explain top N use. */
export type LabSettings = { timeout: number; repeat: number; server: string };
type LiveJob = {
  id: string;
  state: string;
  error: string;
  result: Capture | null;
};
/** One statement in the Explain top N summary. */
type BatchItem = {
  rank: number;
  name: string;
  sql: string;
  planId?: string;
  rows?: number | null;
  findings?: string[];
  total?: number;
  error?: string;
};
type Batch = {
  state: "running" | "done" | "cancelled";
  /** The connection the batch runs on (fixed when it starts). */
  server: string;
  total: number;
  done: number;
  current: string;
  waiting: boolean;
  items: BatchItem[];
  notSelect: number[];
  needValues: number[];
};
export const BATCH_MAX = 20;
/** Plan-shape findings worth a chip in the Explain top N summary. */
const KEY_FINDINGS: Record<string, string> = {
  full_scan: "Full scan",
  filesort: "Filesort",
  temp_table: "Temporary table",
  hash_join: "Hash join",
  bnl: "Block nested loop",
  semijoin: "Semijoin",
  index_merge: "Index merge",
  nonsargable_join: "Non-sargable join",
  misestimate: "Misestimate",
};
class Cancelled extends Error {}
/** One Explain top N run: its own cancel flag and the job it waits for. */
/** The running capture and the connection it was started on: the server
 * answers its status and Cancel only for that connection. */
type BatchControl = { cancelled: boolean; job: string; via?: unknown };
/** What a batch runs with, fixed when it starts. */
type BatchTarget = { connection: Connection; settings: LabSettings; key: string; label: string };
/** A statement's identity in the digest table: schema and digest (Performance
 * Schema keeps one row per pair); the rank only when there is no digest. The
 * connection is part of the report the row came from. */
export function statementKey(row: TopStatement, rank: number) {
  return JSON.stringify([row.schema || "", row.digest || `#${rank}`]);
}
const pause = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
/** Sum of the optimizer's row estimates over table accesses (leaf operators). */
function estimatedRows(plan: Plan): number | null {
  let sum = 0;
  let seen = false;
  for (const op of plan.operators) {
    if (op.children && op.children.length) continue;
    const rows = op.details.estimated_rows;
    if (typeof rows === "number" && Number.isFinite(rows)) {
      sum += rows;
      seen = true;
    }
  }
  return seen ? sum : null;
}
function rankList(ranks: number[]) {
  return ranks.map((r) => `#${r}`).join(", ");
}

// Plain-English meaning of each Performance Schema flag (hover, inspector).
const FLAGS: Record<string, [string, string]> = {
  no_index_used: ["No index used", "At least one execution read a table without any index (a full scan)."],
  no_good_index_used: ["No good index", "The server found no good index for at least one execution."],
  tmp_disk_tables: ["Temp table on disk", "Built an internal temporary table on disk instead of in memory."],
  sort_merge_passes: ["Sort merge passes", "Sorting outgrew sort_buffer_size and needed merge passes."],
  low_selectivity: [
    "Reads ≫ returns",
    "Examines more than twice the rows it returns (rows sent ÷ rows examined below 0.5).",
  ],
};
export const ORDERS: [TopOrder, string][] = [
  ["total", "Total time"],
  ["avg", "Average time"],
  ["examined", "Rows examined"],
  ["count", "Calls"],
  // Workload attribution: only statements whose counter is above zero.
  ["full_join", "Joins without an index"],
  ["scan", "Full scans of the first table"],
  ["no_index", "Reads without an index"],
];
/** The counter an attribution order lists statements by, as a short label and a value. */
const ORDER_COUNTERS: Partial<Record<TopOrder, [string, (row: TopStatement) => number]>> = {
  full_join: ["joins without an index", (r) => (r.select_full_join || 0) + (r.select_range_check || 0)],
  scan: ["first-table scans", (r) => r.select_scan || 0],
  no_index: ["calls without an index", (r) => r.no_index_used],
};
export function orderLabel(order: string) {
  return ORDERS.find(([v]) => v === order)?.[1] || "Total time";
}
/** "schema.table" of a table filter. */
export function tableName(table: TopTable) {
  return `${table.schema}.${table.table}`;
}

/** Seconds as "3 d 4 h", "2 h 05 min", "4 min". */
function duration(seconds: number | null) {
  if (seconds === null || !Number.isFinite(seconds)) return "unknown";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min`;
  return `${Math.round(seconds)} s`;
}
/**
 * What a digest list covers, as notes: digest counters run since the server
 * started or the table was truncated (uptime, the oldest FIRST_SEEN listed),
 * the overflow row the list leaves out, and statements never recorded
 * (Performance_schema_digest_lost). The server's own notes say the rest.
 */
export function coverageNotes(coverage: TopCoverage | null | undefined): string[] {
  if (!coverage) return [];
  const out = [
    `Server uptime ${duration(coverage.uptime_s)}` +
      (coverage.first_seen ? `; the oldest statement listed was first seen ${coverage.first_seen}.` : "."),
  ];
  if (coverage.digest_lost)
    out.push(
      `Performance_schema_digest_lost is ${number(coverage.digest_lost)}: those statements were not recorded at all.`,
    );
  return out;
}
/** Performance Schema or its digest consumer is off: one sentence and the Instruments row that turns it on. */
export function digestGate(error: string): { text: string; row: string } | null {
  if (/Performance Schema is off/.test(error)) return { text: "Performance Schema is off, so there are no statement digests.", row: "ps" };
  if (/consumer disabled/.test(error)) {
    const consumer = (error.match(/consumer disabled: ([a-z_]+)/) || [])[1] || "statements_digest";
    return { text: `Statement digests are not being collected: the ${consumer} consumer is off.`, row: `consumer:${consumer}` };
  }
  return null;
}

function flat(text: string) {
  return text.replace(/\s+/g, " ").trim();
}
function preview(text: string, width = 140) {
  const f = flat(text);
  return f.length > width ? f.slice(0, width - 1) + "…" : f;
}

export type TopState = ReturnType<typeof useTopStatements>;

/** One Top statements read: its parameters (binding.ts single flight) and its request body. */
export function topRead(order: string, schema: string, table: TopTable | null, limit = 25) {
  const target = schema.trim();
  return {
    params: `top:${order}:${target}${table ? `:${JSON.stringify([table.schema, table.table])}` : ""}${limit === 25 ? "" : `:${limit}`}`,
    body: (c: Connection) => ({
      connection: c,
      order,
      limit,
      ...(target ? { schema: target } : {}),
      ...(table ? { table: { schema: table.schema, table: table.table } } : {}),
    }),
  };
}

/** The Top statements report and the Explain top N batch, owned by the Server place. */
export function useTopStatements({
  connection,
  settings,
  order,
  onAddPlan,
  onBatchChange,
}: {
  connection: Connection;
  settings: LabSettings;
  order: string;
  /** Adds a plan; *cancelled* is asked again once its analysis returns, so a
   * cancelled batch adds nothing (the result is then undefined). */
  onAddPlan: (capture: Capture, name: string, cancelled?: () => boolean) => Promise<Plan | undefined>;
  onBatchChange?: (running: boolean) => void;
}) {
  /** The report and the connection it was read from (binding.ts). */
  const [top, setTop] = useState<Bound<TopReport> | null>(null);
  const [refreshed, setRefreshed] = useState<{ key: string; at: number } | null>(null);
  /** The schema filter the shown statements were read with. */
  const [scope, setScope] = useState("");
  /** The table filter for the next read ("Table: schema.table" chip), from a
   * link (Indexes full scans, the Full analysis); null lists every statement. */
  const [table, setTable] = useState<TopTable | null>(null);
  const tableRef = useRef(table);
  tableRef.current = table;
  const [actionError, setActionError] = useState("");
  const privileges = usePrivileges();
  const privilegesRef = useRef(privileges);
  privilegesRef.current = privileges;
  const explainGate = usePrivilege("explain");
  const explainGateRef = useRef(explainGate);
  explainGateRef.current = explainGate;
  const [batchSize, setBatchSize] = useState(5);
  const [batch, setBatch] = useState<Batch | null>(null);
  const batchRunning = batch?.state === "running";
  const reads = useConnectionReads(connection);
  const control = useRef<BatchControl>({ cancelled: false, job: "" });
  const latest = useRef({ connection, settings, order, onAddPlan });
  latest.current = { connection, settings, order, onAddPlan };
  const batchRef = useRef(onBatchChange);
  batchRef.current = onBatchChange;
  useEffect(() => {
    batchRef.current?.(batchRunning);
  }, [batchRunning]);
  // Leaving the workspace (Start fresh remounts Server) stops the batch and
  // the capture it waits for.
  useEffect(
    () => () => {
      const ctl = control.current;
      ctl.cancelled = true;
      if (ctl.job) void api("live/cancel", { id: ctl.job, connection: ctl.via }).catch(() => undefined);
    },
    [],
  );

  /** Read the digests; a read with another order, schema or table replaces
   * one still running. A link passes its own order and table (state updates
   * land on the next render). */
  async function load(schema: string, link?: { order: string; table: TopTable | null }) {
    setActionError("");
    const target = schema.trim();
    const o = link ? link.order : latest.current.order;
    const t = link ? link.table : tableRef.current;
    if (link) setTable(link.table);
    const read = topRead(o, target, t);
    await reads.run(read.params, t ? `Reading statements that name ${tableName(t)}…` : "Reading statement digests…",
      (c, signal) => api<TopReport>("live/top", read.body(c), signal),
      (b) => accept(b, target),
    );
  }
  /** Show a report (from this view's read or from Refresh all). */
  function accept(b: Bound<TopReport>, target: string) {
    setTop(b);
    setRefreshed({ key: b.key, at: Date.now() });
    setScope(target);
  }
  /** Remove the table filter and read the list again (when one is on screen). */
  function clearTable(schema: string) {
    setTable(null);
    tableRef.current = null;
    if (top) void load(schema, { order: latest.current.order, table: null });
  }

  /** EXPLAIN one statement through the capture engine and wait for it, on the
   * connection and with the settings the batch started with. */
  async function explainOne(row: TopStatement, sql: string, target: BatchTarget, ctl: BatchControl): Promise<Capture> {
    const { connection: c, settings: s } = target;
    const database = c.database || row.schema || "";
    const source = normalizeSource({ kind: "digest", digest: row.digest, schema: row.schema });
    const payload = {
      connection: database === c.database ? c : { ...c, database },
      query: sql,
      parameters: {},
      mode: "explain",
      timeout: s.timeout,
      repeat: 1,
      trace: false,
      conditions: "",
      ...(source ? { source } : {}),
    };
    let id = "";
    // The server runs at most two captures at once; wait for a free slot.
    while (!id) {
      if (ctl.cancelled) throw new Cancelled();
      if (privilegesRef.current?.key === target.key && explainGateRef.current.disabledReason) throw new Error(explainGateRef.current.disabledReason);
      try {
        id = (await api<{ id: string }>("live/start", payload)).id;
      } catch (e) {
        if (privilegesRef.current?.key === target.key && isPrivilegeDenial(e)) void privilegesRef.current.accountChanged();
        if (!/Two captures are running/.test((e as Error).message)) throw e;
        setBatch((b) => b && { ...b, waiting: true });
        await pause(1000);
      }
    }
    // Cancelled while the start was on its way: stop the job it started.
    if (ctl.cancelled) {
      void api("live/cancel", { id, connection: payload.connection }).catch(() => undefined);
      throw new Cancelled();
    }
    ctl.job = id;
    ctl.via = payload.connection;
    setBatch((b) => b && { ...b, waiting: false });
    try {
      for (;;) {
        await pause(400);
        if (ctl.cancelled) throw new Cancelled();
        const job = await api<LiveJob>("live/status", { id, connection: payload.connection });
        // A result that arrives after Cancel is not accepted.
        if (ctl.cancelled || job.state === "cancelled") throw new Cancelled();
        if (job.state === "complete" && job.result) return job.result;
        if (job.state !== "queued" && job.state !== "running") {
          if (privilegesRef.current?.key === target.key && isPrivilegeDenial(job.error)) void privilegesRef.current.accountChanged();
          throw new Error(job.error || "The capture did not complete.");
        }
      }
    } finally {
      if (ctl.job === id) ctl.job = "";
    }
  }

  /** Explain top N: estimated plans for the first N runnable SELECTs. */
  async function explainTop() {
    if (!top || batchRunning) return;
    if (explainGateRef.current.disabledReason) { setActionError(explainGateRef.current.disabledReason); return; }
    // The connection and settings are fixed for the whole batch, and must be
    // the ones the statements were read from.
    const { connection: c, settings: s } = latest.current;
    const target: BatchTarget = { connection: c, settings: s, key: connectionKey(c), label: connectionLabel(c) };
    if (top.key !== target.key) {
      setActionError(`These statements are from ${top.label}. Refresh to load this server before Explain top N.`);
      return;
    }
    setActionError("");
    const picked: { row: TopStatement; rank: number; sql: string }[] = [];
    const notSelect: number[] = [];
    const needValues: number[] = [];
    top.report.statements.forEach((row, i) => {
      if (picked.length >= batchSize) return;
      const q = digestQuery(row);
      if (row.capture_problem || q.reason === "missing") notSelect.push(i + 1);
      else if (q.names.length) needValues.push(i + 1);
      else picked.push({ row, rank: i + 1, sql: q.sql });
    });
    const ctl: BatchControl = { cancelled: false, job: "" };
    control.current = ctl;
    const items: BatchItem[] = [];
    setBatch({
      state: "running",
      server: target.label,
      total: picked.length,
      done: 0,
      current: "",
      waiting: false,
      items,
      notSelect,
      needValues,
    });
    let cancelled = false;
    for (const [index, p] of picked.entries()) {
      if (ctl.cancelled) {
        cancelled = true;
        break;
      }
      const name = digestName(p.rank, p.row);
      setBatch((b) => b && { ...b, done: index, current: name });
      try {
        const capture = await explainOne(p.row, p.sql, target, ctl);
        // Cancelled while the plan was on its way: it is not imported.
        if (ctl.cancelled) throw new Cancelled();
        const plan = await latest.current.onAddPlan(capture, name, () => ctl.cancelled);
        // Cancelled while the plan was being analyzed: the importer added
        // nothing, and the batch ends as cancelled.
        if (ctl.cancelled) throw new Cancelled();
        if (!plan) throw new Error("The plan could not be opened.");
        const keys = [...new Set(plan.analysis.warnings.map((w) => w.category || "").filter((c) => c in KEY_FINDINGS))];
        items.push({
          rank: p.rank,
          name,
          sql: p.sql,
          planId: plan.id,
          rows: estimatedRows(plan),
          findings: keys.map((k) => KEY_FINDINGS[k]),
          total: plan.analysis.warnings.length,
        });
      } catch (e) {
        if (e instanceof Cancelled || ctl.cancelled) {
          cancelled = true;
          break;
        }
        items.push({ rank: p.rank, name, sql: p.sql, error: (e as Error).message });
      }
      setBatch((b) => b && { ...b, done: index + 1, items: [...items] });
    }
    setBatch((b) => b && { ...b, state: cancelled ? "cancelled" : "done", current: "", waiting: false, items: [...items] });
  }

  async function cancelBatch() {
    const ctl = control.current;
    ctl.cancelled = true;
    const id = ctl.job;
    if (id)
      try {
        await api("live/cancel", { id, connection: ctl.via });
      } catch (e) {
        setActionError((e as Error).message);
      }
  }

  return {
    top,
    refreshedAt: refreshed?.key === connectionKey(connection) ? refreshed.at : undefined,
    scope,
    table,
    clearTable,
    accept,
    loading: reads.loading,
    error: reads.error || actionError,
    load,
    batch,
    batchRunning,
    batchSize,
    setBatchSize,
    explainTop,
    cancelBatch,
    dismissBatch: () => setBatch(null),
  };
}

function BatchSummary({
  batch,
  onOpenPlan,
  onDismiss,
}: {
  batch: Batch;
  onOpenPlan?: (id: string) => void;
  onDismiss?: () => void;
}) {
  const added = batch.items.filter((i) => i.planId).length;
  const failed = batch.items.filter((i) => i.error).length;
  const title =
    batch.state === "running"
      ? `Explaining ${Math.min(batch.done + 1, batch.total)} of ${batch.total}`
      : `Explain top N: ${added} ${added === 1 ? "plan" : "plans"} added to the open plans` +
        (failed ? `, ${failed} failed` : "") +
        (batch.state === "cancelled" ? " (cancelled)" : "");
  return (
    <section className="batch-summary" aria-labelledby="batch-title">
      <div className="batch-heading">
        <h3 id="batch-title">{title}</h3>
        {onDismiss && (
          <button className="text-button" onClick={onDismiss}>
            Dismiss
          </button>
        )}
      </div>
      {batch.state === "running" && (
        <p role="status" className="capture-status">
          <span className="spinner" />
          {batch.waiting
            ? "Waiting for a free capture slot (at most two captures run at once)…"
            : batch.current
              ? `Explaining ${batch.current} on ${batch.server}…`
              : `Starting on ${batch.server}…`}
        </p>
      )}
      {batch.state !== "running" && batch.total === 0 && <p className="hint">No statement in the list can be explained as recorded.</p>}
      {batch.items.length > 0 && (
        <div className="data-table-wrap batch-table-wrap" role="region" aria-label="Explain top N results" tabIndex={0}>
          <table className="data-table batch-table">
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Statement</th>
                <th scope="col" className="num" title="Sum of the optimizer's row estimates for each table access">
                  Est. rows
                </th>
                <th scope="col">Key findings</th>
                <th scope="col">
                  <span className="visually-hidden">Plan</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {batch.items.map((item) => (
                <tr key={item.rank}>
                  <td className="num">{item.rank}</td>
                  <td className="batch-sql">
                    <code className="cell-truncate cell-sql" title={item.sql}>
                      {preview(item.sql, 90)}
                    </code>
                  </td>
                  <td className="num">{item.error ? "—" : item.rows == null ? "—" : number(item.rows)}</td>
                  <td>
                    {item.error ? (
                      <span className="batch-error">Failed: {item.error}</span>
                    ) : item.findings && item.findings.length ? (
                      <ul className="pill-list" aria-label="Key findings">
                        {item.findings.map((f) => (
                          <li key={f} className="pill is-warn">
                            {f}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="hint">None</span>
                    )}
                    {!item.error && !!item.total && (
                      <span className="hint batch-count">
                        {" "}
                        {item.total} {item.total === 1 ? "finding" : "findings"} in total
                      </span>
                    )}
                  </td>
                  <td>
                    {item.planId && onOpenPlan && (
                      <button className="text-button" aria-label={`Open plan ${item.name}`} onClick={() => onOpenPlan(item.planId!)}>
                        Open plan
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(batch.notSelect.length > 0 || batch.needValues.length > 0) && (
        <ul className="batch-skipped">
          {batch.needValues.length > 0 && (
            <li className="hint">
              Skipped {rankList(batch.needValues)}: only normalized text was recorded, so{" "}
              {batch.needValues.length === 1 ? "it needs" : "they need"} parameter values. Use Explain on{" "}
              {batch.needValues.length === 1 ? "that row" : "those rows"} to fill them in.
            </li>
          )}
          {batch.notSelect.length > 0 && <li className="hint">Skipped {rankList(batch.notSelect)}: not a read-only SELECT.</li>}
        </ul>
      )}
    </section>
  );
}

/** What a Run analysis confirmation was given for: the statement (its SQL
 * as it will run), the connection, the schema it runs in and the settings.
 * Any change closes the confirmation, so a confirmed run is never applied to
 * another statement or server. */
type RunConfirmation = {
  identity: string;
  /** statementKey() of the row it was opened on. */
  statement: string;
  row: TopStatement;
  rank: number;
  sql: string;
  server: string;
  schema: string;
  timeout: number;
  repeat: number;
};

/** Explain, Run analysis (with its confirmation) and Use this query for one
 * row. Lists of statements behind a number (Health, Indexes, the Full
 * analysis) show it *compact*: Explain and Run analysis only. */
export function TopActions({
  row,
  rank,
  disabled,
  settings,
  connection,
  onAction,
  compact = false,
  idPrefix = "top-",
  name = "",
}: {
  row: TopStatement;
  rank: number;
  disabled: boolean;
  settings: LabSettings;
  /** The connection in use (the report is from it, or the actions are off). */
  connection: Connection;
  onAction: (action: DigestAction, row: TopStatement, rank: number) => void;
  compact?: boolean;
  /** Keeps element ids unique when several lists share one inspector. */
  idPrefix?: string;
  /** Added to the buttons' accessible names ("statement 1 that names shop.orders"). */
  name?: string;
}) {
  const explainGate = usePrivilege("explain");
  const analysisGate = usePrivilege("run_analysis");
  const privilegeId = useId();
  const text = row.sample_text || row.digest_text || "";
  const q = digestQuery(row);
  const problem = row.capture_problem || (q.reason === "missing" ? "No statement text was recorded." : "");
  const schema = connection.database || row.schema || "";
  const identity = JSON.stringify([
    connectionKey(connection),
    row.schema || "",
    row.digest || "",
    q.sql,
    schema,
    settings.timeout,
    settings.repeat,
  ]);
  const statement = statementKey(row, rank);
  const [confirming, setConfirming] = useState<RunConfirmation | null>(null);
  const [voided, setVoided] = useState(false);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const analyzeButton = useRef<HTMLButtonElement>(null);
  // Another row selected: start clean.
  useEffect(() => setVoided(false), [statement]);
  // Another statement, SQL, connection, schema or setting: the confirmation
  // no longer applies. When it is still the same statement, say nothing ran.
  useEffect(() => {
    if (confirming && confirming.identity !== identity) {
      setConfirming(null);
      setVoided(confirming.statement === statement);
    }
  }, [identity, confirming, statement]);
  useEffect(() => {
    if (confirming) confirmButton.current?.focus();
  }, [confirming]);
  const reasonId = `${idPrefix}reason-${rank}`;
  const confirmId = `${idPrefix}confirm-${rank}`;
  const label = `statement ${rank}${name ? ` ${name}` : ""}`;
  const needsValues = q.names.length > 0;
  function closeConfirm() {
    setConfirming(null);
    window.setTimeout(() => analyzeButton.current?.focus());
  }
  function openConfirm() {
    if (disabled || problem || analysisGate.disabledReason) return;
    setVoided(false);
    setConfirming({
      identity,
      statement,
      row,
      rank,
      sql: q.sql,
      server: `${connection.host}:${connection.port}`,
      schema,
      timeout: settings.timeout,
      repeat: settings.repeat,
    });
  }
  function execute() {
    const c = confirming;
    setConfirming(null);
    if (!c || c.identity !== identity || disabled || problem || analysisGate.disabledReason) {
      setVoided(true);
      return;
    }
    onAction("analyze", c.row, c.rank);
  }
  const runs = (n: number) => (n === 1 ? "1 run" : `${n} runs`);
  return (
    <div className="top-actions">
      <div className="button-row">
        <button
          className="button"
          disabled={disabled || !!problem}
          aria-label={`Explain ${label}`}
          aria-describedby={problem ? reasonId : undefined}
          title={
            needsValues
              ? "Open in a new tab with parameters to fill in. Nothing runs until you do."
              : "Open in a new tab and capture the estimated plan (EXPLAIN only)"
          }
          data-privilege-state={explainGate.state}
          {...privilegeButtonProps(explainGate, () => onAction("explain", row, rank), "", `${privilegeId}-explain${problem ? ` ${reasonId}` : ""}`)}
        >
          Explain{explainGate.state === "unknown" && <span className="privilege-unverified">Unverified</span>}
        </button>
        <button
          ref={analyzeButton}
          className="button"
          disabled={disabled || !!problem}
          aria-label={`Run analysis on ${label}`}
          aria-describedby={problem ? reasonId : undefined}
          aria-expanded={needsValues ? undefined : !!confirming}
          title={
            needsValues
              ? "Open in a new tab with parameters to fill in. Nothing runs until you do."
              : `Executes the statement in a read-only transaction, ${settings.timeout} s limit per run`
          }
          data-privilege-state={analysisGate.state}
          {...privilegeButtonProps(analysisGate, () => (needsValues ? onAction("analyze", row, rank) : openConfirm()), "", `${privilegeId}-analysis${problem ? ` ${reasonId}` : ""}`)}
        >
          Run analysis{analysisGate.state === "unknown" && <span className="privilege-unverified">Unverified</span>}
        </button>
        {!compact && (
          <button
            className="button"
            disabled={disabled || !text}
            aria-label={`Use ${label} in the editor`}
            onClick={() => onAction("open", row, rank)}
          >
            Use this query
          </button>
        )}
      </div>
      <span id={`${privilegeId}-explain`} className="account-sr">{explainGate.reason}</span>
      <span id={`${privilegeId}-analysis`} className="account-sr">{analysisGate.reason}</span>
      {problem && (
        <p className="hint top-reason" id={reasonId}>
          Explain and Run analysis are off for this statement: {problem}
        </p>
      )}
      {voided && !confirming && (
        <p className="hint top-voided" role="status">
          The statement, the connection or the settings changed, so nothing ran. Choose Run analysis again to confirm.
        </p>
      )}
      {confirming && (
        <div
          className="run-confirm"
          role="group"
          aria-labelledby={confirmId}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              closeConfirm();
            }
          }}
        >
          <p id={confirmId}>
            Run analysis executes this statement on {confirming.server}: {runs(confirming.repeat)} inside a read-only
            transaction that is rolled back, each stopped after {confirming.timeout} s.
            {confirming.schema && ` Schema: ${confirming.schema}.`}
          </p>
          <p className="hint">{analysisGate.reason}</p>
          <div className="button-row">
            <button ref={confirmButton} className="button primary" disabled={disabled} {...privilegeButtonProps(analysisGate, execute, "", `${privilegeId}-analysis`)}>
              Execute and analyze
            </button>
            <button className="button" onClick={closeConfirm}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

type Row = TopStatement & { rank: number };

export function TopStatementsView({
  connection,
  disabled,
  onBack,
  selection,
  state,
  order,
  onOrderChange,
  schema,
  onSchemaChange,
  settings,
  onDigest,
  onOpenPlan,
}: {
  connection: Connection;
  /** The Query editor is capturing: row actions and Explain top N wait. */
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  state: TopState;
  order: string;
  onOrderChange: (order: string) => void;
  schema: string;
  onSchemaChange: (schema: string) => void;
  settings: LabSettings;
  /** Open a row in a new editor tab, and explain or analyze it, on the
   * connection *expect* (connectionKey) only. */
  onDigest: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onOpenPlan?: (id: string) => void;
}) {
  const explainGate = usePrivilege("explain");
  const privilegeId = useId();
  const { scope, loading, error, batch, batchRunning, batchSize } = state;
  const activeKey = connectionKey(connection);
  const top = state.top?.report ?? null;
  // Statements read from another connection stay on screen, greyed; Explain,
  // Run analysis, Use this query and Explain top N stay off until a refresh.
  const other = state.top && state.top.key !== activeKey ? state.top.label : null;
  const [search, setSearch] = useState("");
  const busy = disabled || !!loading || batchRunning;
  const actionsOff = busy || !!other;
  const rows: Row[] = (top?.statements || []).map((s, i) => ({ ...s, rank: i + 1 }));
  const needle = search.trim().toLowerCase();
  const shown = needle
    ? rows.filter((r) => `${r.sample_text || ""} ${r.digest_text || ""} ${r.schema || ""}`.toLowerCase().includes(needle))
    : rows;
  const flagged = rows.filter((r) => r.flags.length).length;
  // The order and table the list on screen was read with.
  const shownOrder = top?.order || order;
  const ranked = orderLabel(shownOrder).toLowerCase();
  const shownTable = top?.table || null;
  const counter = ORDER_COUNTERS[shownOrder as TopOrder];
  // A Performance Schema prerequisite is a gate with one action, not an error.
  const gate = digestGate(error);

  const columns: Column<Row>[] = [
    { key: "rank", label: "#", num: true, sort: (r) => r.rank, render: (r) => r.rank },
    {
      key: "statement",
      label: "Statement",
      flex: true,
      sort: (r) => flat(r.sample_text || r.digest_text || "").toLowerCase(),
      render: (r) => {
        const text = r.sample_text || r.digest_text || "";
        return (
          <code className="cell-truncate cell-sql" title={text}>
            {text ? preview(text, 200) : "No statement text"}
          </code>
        );
      },
    },
    { key: "count", label: "Calls", num: true, sort: (r) => r.count, render: (r) => <CompactNumber value={r.count} /> },
    { key: "total", label: "Total (ms)", num: true, sort: (r) => r.total_ms, render: (r) => <CompactNumber value={r.total_ms} /> },
    { key: "avg", label: "Average (ms)", num: true, sort: (r) => r.avg_ms, render: (r) => <CompactNumber value={r.avg_ms} /> },
    { key: "examined", label: "Rows examined", num: true, sort: (r) => r.rows_examined, render: (r) => <CompactNumber value={r.rows_examined} /> },
    { key: "sent", label: "Rows sent", num: true, sort: (r) => r.rows_sent, render: (r) => <CompactNumber value={r.rows_sent} /> },
    ...(counter
      ? [
          {
            key: `counter_${shownOrder}`,
            label: counter[0][0].toUpperCase() + counter[0].slice(1),
            name: counter[0],
            num: true,
            sort: (r: Row) => counter[1](r),
            render: (r: Row) => <CompactNumber value={counter[1](r)} />,
          },
        ]
      : []),
    { key: "schema", label: "Schema", sort: (r) => r.schema || "", render: (r) => r.schema || <span className="cell-muted">—</span> },
    {
      key: "flags",
      label: "Flags",
      sort: (r) => r.flags.length,
      render: (r) =>
        r.flags.length ? (
          <ul className="pill-list flag-chips" aria-label="Flags">
            {r.flags.slice(0, 3).map((f) => {
              const [label, why] = FLAGS[f] || [f.replace(/_/g, " "), f];
              return (
                <li key={f} className="pill is-warn flag-chip" title={why}>
                  {label}
                  <span className="visually-hidden">: {why}</span>
                </li>
              );
            })}
            {r.flags.length > 3 && <li className="pill">+{r.flags.length - 3}</li>}
          </ul>
        ) : null,
    },
  ];

  let inspector: InspectorContent | null = null;
  const row = rows.find((r) => statementKey(r, r.rank) === selection.key);
  if (row) {
    const text = row.sample_text || row.digest_text || "";
    inspector = {
      key: selection.key,
      title: `Statement ${row.rank}`,
      meta: (
        <>
          <span>{time(row.total_ms)} total</span>
          <span>· {number(row.count)} calls</span>
          {row.schema && <span>· {row.schema}</span>}
        </>
      ),
      actions: (
        <TopActions
          row={row}
          rank={row.rank}
          disabled={actionsOff}
          settings={settings}
          connection={connection}
          onAction={(action, r, rank) => {
            if (!other) onDigest(r, rank, action, activeKey);
          }}
        />
      ),
      body: (
        <>
          <InspectorSection title={row.sample_text ? "Statement" : "Normalized statement"}>
            {text ? <CopySql sql={text} label={`Statement ${row.rank}`} /> : <p className="hint">No statement text was recorded.</p>}
            {!row.sample_text && (
              <p className="hint top-normalized">
                Normalized text: ? marks each literal value. Explain and Run analysis open it with parameters to fill in
                first.
              </p>
            )}
            {row.sample_text && row.sample_truncated && digestQuery(row).names.length > 0 && (
              <p className="hint">The sample was cut off, so Explain and Run analysis use the normalized text with parameters to fill in.</p>
            )}
          </InspectorSection>
          {row.flags.length > 0 && (
            <InspectorSection title="Flags">
              <ul className="top-flags">
                {row.flags.map((f) => {
                  const [label, why] = FLAGS[f] || [f.replace(/_/g, " "), ""];
                  return (
                    <li key={f}>
                      <strong>{label}</strong>
                      {why && <span className="hint"> {why}</span>}
                    </li>
                  );
                })}
              </ul>
            </InspectorSection>
          )}
          <InspectorSection title="Latency">
            <Fields
              rows={[
                ["Total", time(row.total_ms)],
                ["Average", time(row.avg_ms)],
                ["Longest", time(row.max_ms)],
              ]}
            />
          </InspectorSection>
          <InspectorSection title="Rows">
            <Fields
              rows={[
                ["Rows examined", number(row.rows_examined)],
                ["Rows sent", number(row.rows_sent)],
                row.examined_per_sent !== null && ["Examined per row sent", number(row.examined_per_sent)],
              ]}
            />
          </InspectorSection>
          <InspectorSection title="Access and sorting">
            <Fields
              rows={[
                ["Without an index", `${number(row.no_index_used)} of ${number(row.count)} calls`],
                ["Without a good index", number(row.no_good_index_used)],
                ["Temporary tables on disk", number(row.tmp_disk_tables)],
                ["Sort merge passes", number(row.sort_merge_passes)],
                row.select_full_join !== undefined && ["Joins without an index", number(row.select_full_join)],
                !!row.select_range_check && ["Range checks for each row", number(row.select_range_check)],
                row.select_scan !== undefined && ["Full scans of the first table", number(row.select_scan)],
              ]}
            />
            {row.table_match && (
              <p className="hint top-match">
                Names <code>{row.table_match.schema}.{row.table_match.table}</code>{" "}
                {row.table_match.kind === "qualified" ? "with its schema" : "without a schema"}: {row.table_match.label}.
              </p>
            )}
          </InspectorSection>
          <InspectorSection title="Activity">
            <Fields
              rows={[
                ["Calls", number(row.count)],
                ["First seen", row.first_seen || "—"],
                ["Last seen", row.last_seen || "—"],
                ["Schema", row.schema || "—"],
                ["Digest", row.digest ? <code key="d">{row.digest}</code> : "—"],
              ]}
            />
          </InspectorSection>
        </>
      ),
    };
  }

  return (
    <ViewLayout
      id="top"
      title="Top statements"
      className="top-panel"
      onBack={onBack}
      info={
        <>
          <p>
            Performance Schema statement digests: every normalized statement since the server started (or since the
            digest table was truncated), heaviest first.
          </p>
          <p>Select a statement for its full text, numbers and flags, then Explain it, run an analysis, or send it to the Query editor.</p>
        </>
      }
      controls={
        <>
          <ToolbarSelect label="Rank by" value={order} options={ORDERS} onChange={onOrderChange} />
          <SchemaField id="top-schema" value={schema} onChange={onSchemaChange} />
          {state.table && (
            <p className="perf-focus top-table-chip" data-table={tableName(state.table)}>
              Table: <code>{tableName(state.table)}</code>
              <button
                type="button"
                className="text-button"
                aria-label={`Remove the table filter ${tableName(state.table)}`}
                title="Remove the table filter"
                disabled={busy}
                onClick={() => state.clearTable(schema)}
              >
                <Icon name="close" size={12} />
              </button>
            </p>
          )}
          {top && top.statements.length > 0 && (
            <SearchField label="Filter statements" value={search} onChange={setSearch} placeholder="Filter" />
          )}
        </>
      }
      onRefresh={() => void state.load(schema)}
      refreshDisabled={busy}
      loading={!!loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a statement to see its full text, its numbers, and Explain or Run analysis.</p>}
      status={<ViewStatus loading={loading} error={gate ? "" : error} />}
    >
      {gate && (
        <PrerequisiteGate action={<InstrumentsLink row={gate.row} />}>{gate.text}</PrerequisiteGate>
      )}
      {!top && !loading && !error && (
        <EmptyState
          icon="statements"
          title="No statements loaded"
          action={
            <button type="button" className="button primary" disabled={busy} onClick={() => void state.load(schema)}>
              Load top statements
            </button>
          }
        >
          The statements that used the most server time since Performance Schema started recording.
        </EmptyState>
      )}
      {top && (
        <>
          <SummaryLine
            notes={[...top.notes, ...coverageNotes(top.coverage), ...(top.match?.caveats || [])]}
            extra={
              top.statements.length > 0 ? (
                <span className="batch-controls">
                  <label className="toolbar-field batch-size">
                    Top N
                    <NumberField
                      min={1}
                      max={BATCH_MAX}
                      value={batchSize}
                      describedBy="batch-hint"
                      onChange={(v) => state.setBatchSize(Math.min(BATCH_MAX, Math.max(1, Math.round(+v) || 1)))}
                    />
                  </label>
                  <button
                    type="button"
                    className="button small"
                    disabled={actionsOff}
                    aria-describedby="batch-hint"
                    title="Estimated plans only: runs EXPLAIN, never executes the statements."
                    data-privilege-state={explainGate.state}
                    {...privilegeButtonProps(explainGate, () => void state.explainTop(), "", `batch-hint ${privilegeId}`)}
                  >
                    Explain top {batchSize}{explainGate.state === "unknown" && <span className="privilege-unverified">Unverified</span>}
                  </button>
                  {batchRunning && (
                    <button type="button" className="button small danger" onClick={() => void state.cancelBatch()}>
                      Cancel Explain top {batchSize}
                    </button>
                  )}
                  <span className="account-sr" id={privilegeId}>{explainGate.reason}</span>
                  <span className="visually-hidden" id="batch-hint">
                    Estimated plans only: runs EXPLAIN, never executes the statements. Skips statements that are not a
                    SELECT or that need parameter values.
                  </span>
                </span>
              ) : undefined
            }
          >
            {top.statements.length} {top.statements.length === 1 ? "statement" : "statements"}
            {flagged > 0 && ` · ${flagged} flagged`} · {counter ? "with" : "ranked by"} {ranked}
            {shownTable && ` · naming ${tableName(shownTable)} (${top.match?.label || "text match, not proof"})`}
            {scope && ` · run with schema ${scope}`}
          </SummaryLine>
          {batch && <BatchSummary batch={batch} onOpenPlan={onOpenPlan} onDismiss={batchRunning ? undefined : state.dismissBatch} />}
          {top.statements.length === 0 ? (
            <div className="table-empty">
              {shownTable
                ? `No recorded statement names ${tableName(shownTable)} and reads without an index.`
                : counter
                  ? `No recorded statement has ${counter[0]}.`
                  : "No statements recorded yet. Run the workload, then refresh."}
            </div>
          ) : (
            <PriorityTable
              id="top.statements"
              detailColumns={["rank", "count", "avg", "examined", "sent", "schema", "flags"]}
              label="Top statements"
              columns={columns}
              rows={shown}
              rowKey={(r) => statementKey(r, r.rank)}
              selection={selection}
              rowAttrs={(r) => ({ "data-rank": String(r.rank), "data-digest": r.digest || undefined })}
              empty={<>No statement matches “{search.trim()}”.</>}
            />
          )}
        </>
      )}
    </ViewLayout>
  );
}

// ---- The statements behind a number ----------------------------------------------

/** A short list of statements behind a number: one line of SQL, the counts,
 * and Explain and Run analysis (with its confirmation) on each. */
export function StatementRows({
  statements,
  order,
  table = null,
  connection,
  disabled,
  settings,
  onAction,
}: {
  statements: TopStatement[];
  order: string;
  table?: TopTable | null;
  /** The connection in use; the caller turns the actions off for another connection's list. */
  connection: Connection;
  disabled: boolean;
  settings: LabSettings;
  onAction: (action: DigestAction, row: TopStatement, rank: number) => void;
}) {
  if (!statements.length) return null;
  const counter = ORDER_COUNTERS[order as TopOrder];
  const idPrefix = `behind-${order}-${table ? tableName(table).replace(/[^A-Za-z0-9]+/g, "-") : "all"}-`;
  const name = table ? `that names ${tableName(table)}` : counter ? `with ${counter[0]}` : "";
  return (
    <ol className="statements-behind-list" aria-label={`Statements ${name}`.trim()}>
      {statements.map((row, i) => {
        const text = row.sample_text || row.digest_text || "";
        return (
          <li key={statementKey(row, i + 1)} data-digest={row.digest || undefined}>
            <code className="cell-sql statements-behind-sql" title={text}>
              {text ? preview(text, 180) : "No statement text"}
            </code>
            <span className="hint statements-behind-meta">
              {number(row.count)} calls · {time(row.total_ms)} total
              {counter && ` · ${number(counter[1](row))} ${counter[0]}`}
              {row.schema && ` · ${row.schema}`}
            </span>
            <TopActions
              compact
              row={row}
              rank={i + 1}
              disabled={disabled}
              settings={settings}
              connection={connection}
              idPrefix={idPrefix}
              name={name}
              onAction={onAction}
            />
          </li>
        );
      })}
    </ol>
  );
}

/** Statements read for a number, kept while the workspace is open (never saved). */
const behindCache = new Map<string, Bound<TopReport>>();
const BEHIND_CACHE_LIMIT = 50;
/** How many statements a list behind a number shows. */
export const BEHIND_LIMIT = 5;
/** Start fresh: forget the lists read so far. */
export function clearStatementsBehind() {
  behindCache.clear();
}
/** Open Top statements with an attribution order and table filter; *from* is
 * the connection key of the report the link is on. */
export type OpenTop = (order: TopOrder, table: TopTable | null, from: string) => void;

/**
 * The statements behind a server number (workload attribution): the top
 * digests whose counter for *order* is above zero or, with *table*, the ones
 * that read some table without an index and name it (a text match, not
 * proof). Read only when asked (Show statements), each with Explain and Run
 * analysis (with its confirmation), and Open in Top statements for the whole
 * list. Digest counters run since the server started, unlike Health's
 * window; the notes say what the list covers.
 */
export function StatementsBehind({
  connection,
  disabled,
  other,
  from,
  order,
  table = null,
  settings,
  onDigest,
  onOpenTop,
}: {
  connection: Connection;
  disabled: boolean;
  /** The report this number is on was read from another connection: everything is off. */
  other: boolean;
  /** Connection key of that report. */
  from: string;
  order: TopOrder;
  table?: TopTable | null;
  settings: LabSettings;
  onDigest: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onOpenTop: OpenTop;
}) {
  const reads = useConnectionReads(connection);
  const tableKey = table ? JSON.stringify([table.schema, table.table]) : "";
  const [, setVersion] = useState(0);
  const bound = other ? null : behindCache.get(`${reads.key}|${order}|${tableKey}`) || null;
  const report = bound?.report ?? null;
  const statements = report?.statements || [];
  const counter = ORDER_COUNTERS[order];
  const busy = disabled || other || !!reads.loading;
  const gate = digestGate(reads.error);

  async function show() {
    if (other) return;
    const read = topRead(order, "", table, BEHIND_LIMIT);
    await reads.run(
      `statements:${order}:${tableKey}`,
      table ? `Reading statements that name ${tableName(table)}…` : "Reading statement digests…",
      (c, signal) => api<TopReport>("live/top", read.body(c), signal),
      (b) => {
        behindCache.delete(`${b.key}|${order}|${tableKey}`);
        behindCache.set(`${b.key}|${order}|${tableKey}`, b);
        while (behindCache.size > BEHIND_CACHE_LIMIT) behindCache.delete(behindCache.keys().next().value as string);
        setVersion((v) => v + 1);
      },
    );
  }

  return (
    <div className="statements-behind" data-order={order} data-table={table ? tableName(table) : undefined}>
      <div className="button-row">
        <button type="button" className="button" disabled={busy} onClick={() => void show()}>
          {report ? "Show again" : "Show statements"}
        </button>
        <button type="button" className="button" disabled={disabled || other} onClick={() => onOpenTop(order, table, from)}>
          Open in Top statements
        </button>
      </div>
      {!!reads.loading && (
        <p role="status" className="capture-status">
          <span className="spinner" /> {reads.loading}
        </p>
      )}
      {gate ? (
        <PrerequisiteGate action={<InstrumentsLink row={gate.row} />}>{gate.text}</PrerequisiteGate>
      ) : (
        !!reads.error && (
          <p role="alert" className="error">
            {reads.error}
          </p>
        )
      )}
      {report && (
        <>
          <p className="hint statements-behind-summary">
            <span>
              {statements.length
                ? `${statements.length === 1 ? "1 statement" : `Top ${statements.length} statements`}${counter ? ` by ${counter[0]}` : ""}, since ${report.coverage?.first_seen || "the server started"}`
                : table
                  ? `No recorded statement names ${tableName(table)} and reads without an index.`
                  : `No recorded statement has ${counter ? counter[0] : "this counter"}.`}
              {table && ` · ${report.match?.label || "text match, not proof"}`}
            </span>
            <NotesButton
              label="Notes about these statements"
              notes={[...report.notes, ...coverageNotes(report.coverage), ...(report.match?.caveats || [])]}
            />
          </p>
          <StatementRows
            statements={statements}
            order={order}
            table={table}
            connection={connection}
            disabled={busy}
            settings={settings}
            onAction={(action, r, rank) => {
              if (bound && bound.key === connectionKey(connection)) onDigest(r, rank, action, bound.key);
            }}
          />
        </>
      )}
    </div>
  );
}
