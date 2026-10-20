import type { ChartData } from "./charts/core/types";

export type View =
  | "flamegraph"
  | "tree"
  | "bargraph"
  | "workbench";
export type Finding = {
  text: string;
  severity: string;
  /** Sidecar category: full_scan, filesort, temp_table, … */
  category?: string;
  node_ids?: string[];
  node_labels?: string[];
};
export type Suggestion = { action: string; why?: string; severity: string };
export type Analysis = {
  schema_version: string;
  source: { engine?: string };
  plan_summary: {
    /** Null when the plan carries no measurement (sidecar 1.4). */
    total_time_ms: number | null;
    rows_sent: number | null;
    rows_examined_estimate: number | null;
    operator_count: number;
  };
  executive_summary: string;
  primary_action?: { ref: string };
  warnings: Finding[];
  suggestions: Suggestion[];
  query?: { raw?: string };
  /** Sidecar teach hooks; the workspace leaves `query_sql` to Export JSON. */
  teach_hooks?: (Record<string, unknown> & { query_sql?: string })[];
};
export type Operator = {
  node_id: string;
  label: string;
  depth: number;
  self_time_ms: number;
  total_time_ms: number;
  rows: number;
  loops: number;
  details: Record<string, unknown>;
  parent_id?: string | null;
  children?: string[];
};
export type Plan = {
  id: string;
  name: string;
  raw: string;
  analysis: Analysis;
  operators: Operator[];
  query: string;
  teach_hooks?: TeachHook[];
  capture?: Capture;
  notes?: string;
  /** chart-data-2.0 for the full tree (the bundle also reads 1.0); drawn in-page by PlanChart. */
  chart_data?: ChartData;
};
export type Comparison = {
  analysis: {
    before: { total_time_ms: number; timing_available?: boolean };
    after: { total_time_ms: number; timing_available?: boolean };
    deltas: MatchDelta[];
    summary: {
      time_delta_ms: number;
      time_delta_pct: number | null;
      regressions: number;
      improvements: number;
    };
  };
  html: string;
};
/** A failed API request. `code` is a machine-readable reason when the server sends one. */
export class ApiError extends Error {
  status: number;
  code: string;
  data: Record<string, unknown>;
  constructor(message: string, status: number, data: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = typeof data.code === "string" ? data.code : "";
    this.data = data;
  }
}
// Capture and erase the fragment as soon as this module loads. It is never
// written to storage, the DOM, a request URL, or the saved workspace.
let launchSecret = "";
let authentication: Promise<string> | null = null;
const SESSION_PROOF = "myflames.session-proof";
function sessionProof(): string {
  try { return window.localStorage.getItem(SESSION_PROOF) || ""; }
  catch { return ""; }
}
function captureLaunchSecret() {
  if (typeof window === "undefined") return;
  const secret = new URLSearchParams(window.location.hash.slice(1)).get("launch");
  if (secret) {
    launchSecret = secret;
    authentication = null;
  }
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
}
captureLaunchSecret();
export function authenticate(): Promise<string> {
  captureLaunchSecret();
  if (!authentication) {
    const body = JSON.stringify(launchSecret ? { secret: launchSecret } : {});
    launchSecret = "";
    authentication = fetch("/api/auth/session", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": sessionProof() }, body,
    }).then(async response => {
      const result = await response.json();
      if (!response.ok || typeof result.token !== "string" || !result.token)
        throw new ApiError(result.error || "Open the launch URL printed by myflames ui.", response.status, result);
      // Unlike cookies, this storage belongs to the exact origin including
      // its port. It is useful only together with the HttpOnly session cookie.
      try { window.localStorage.setItem(SESSION_PROOF, result.token); }
      catch { /* This tab still works when browser storage is unavailable. */ }
      return result.token;
    });
  }
  return authentication;
}
/** The installed myflames version, filled in by ui.py ("" under the Vite
 * dev server, where the placeholder is left as is). */
export function appVersion() {
  if (typeof document === "undefined") return "";
  const value =
    document.querySelector<HTMLMetaElement>('meta[name="myflames-version"]')
      ?.content || "";
  return value.startsWith("__") ? "" : value;
}
export async function api<T>(
  path: string,
  body: object | string,
  signal?: AbortSignal,
  keepalive = false,
): Promise<T> {
  const csrf = await authenticate();
  const response = await fetch(`/api/${path}`, {
    credentials: "same-origin",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Myflames-Token": csrf,
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
    signal,
    keepalive,
  });
  let result: Record<string, unknown> = {};
  try {
    result = await response.json();
  } catch {
    /* an empty or non-JSON error body keeps the generic message */
  }
  if (!response.ok)
    throw new ApiError(
      typeof result.error === "string"
        ? result.error
        : "The request failed. Try again.",
      response.status,
      result,
    );
  return result as T;
}
export function download(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const numberFormats = new Map<string, Intl.NumberFormat>();
/** The same text as value.toLocaleString(undefined, options), with one
 * formatter per options instead of one per call (building a formatter per
 * number was the largest cost of drawing a table of numbers). */
export function formatNumber(value: number, options: Intl.NumberFormatOptions) {
  const key = JSON.stringify(options);
  let format = numberFormats.get(key);
  if (!format) numberFormats.set(key, (format = new Intl.NumberFormat(undefined, options)));
  return format.format(value);
}
export function time(ms: number | null | undefined) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  if (ms > 0 && ms < 1) return `${(ms * 1000).toFixed(1)} µs`;
  return `${formatNumber(ms, { maximumFractionDigits: 2 })} ms`;
}
export function number(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return formatNumber(value, { maximumFractionDigits: 0 });
}
export function safePreview(html: string) {
  // The iframe has an opaque origin. This policy also keeps report/plan content
  // from making external requests or submitting forms inside the preview.
  const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'">`;
  return html.replace(/<head[^>]*>/i, (match) => match + policy);
}

export type TeachHook = {
  lesson: string;
  match: { short_label?: string };
  note: string;
};
export type Capture = {
  raw: string;
  query: string;
  mode: string;
  engine: string;
  server_version: string;
  captured_at: string;
  runs: {
    total_time_ms: number | null;
    statistics: Record<string, number> | null;
    session_status_delta: Record<string, number>;
    plan: string;
  }[];
  measurements: {
    count: number;
    median_ms: number | null;
    min_ms: number | null;
    max_ms: number | null;
    stdev_ms: number | null;
  };
  schema: Record<string, unknown>;
  table_stats: Record<string, unknown>;
  variables: Record<string, unknown>;
  trace: {
    data: unknown;
    missing_bytes: number;
    insufficient_privileges: number;
  } | null;
  warnings: string[];
  conditions: string;
  /** Where the evidence came from: a digest, or a label such as
   * "mysqlsh-diagnostics" (import-shell). */
  source?: CaptureSource | string | null;
};
/** The digest source of a capture, or null for any other origin. */
export function digestSource(capture?: Capture | null): CaptureSource | null {
  const source = capture?.source;
  return source && typeof source === "object" && source.kind === "digest"
    ? source
    : null;
}
/** A capture started from a Performance Schema statement digest. */
export type CaptureSource = {
  kind: "digest";
  digest: string;
  schema: string | null;
};
export type Connection = {
  name: string;
  host: string;
  port: number;
  user: string;
  password?: string;
  database: string;
  ssl_mode: string;
  ssl_ca?: string;
  /** Selected saved profile; the server fills a remembered password from it. */
  profile_id?: string;
  /** Write safety (dbops.connection_safety): what the connection in use is
   * judged by. The server decides; a request never lowers the protection
   * (an unsaved connection is Production whatever it claims). */
  type?: ConnectionType;
  /** Refuse every write sent through myflames. */
  read_only?: boolean;
};
/** Production asks before every write; Development only before risky ones. */
export type ConnectionType = "production" | "development";
export type Profile = Connection & {
  id: string;
  remember_password?: boolean;
  has_saved_password?: boolean;
  secret_backend?: string;
  secret_backend_label?: string;
};

// ---- Audit trail of server writes (dbops.py, /api/audit/*) ------------------

/** pending: written before sending, never finished; unknown: interrupted
 * after the statement may have reached the server. */
export type AuditStatus = "pending" | "ok" | "error" | "declined" | "refused" | "unknown";
/** One audit-1.0 record: statements as stored (redacted), never a password. */
export type AuditRecord = {
  id: string;
  attempted_at: string;
  finished_at: string | null;
  status: AuditStatus;
  outcome: string | null;
  outcome_unknown: boolean;
  surface: "ui" | "cli";
  os_user: string;
  operation: string;
  connection: {
    profile_id: string | null;
    profile_name: string | null;
    type: string | null;
    read_only: boolean | null;
    host: string | null;
    port: number | null;
    user: string | null;
    engine: string | null;
    server_version: string | null;
  };
  statements: string[];
  target: unknown;
  verified: unknown;
  policy: unknown;
  error_code: number | null;
  message: string | null;
  rows_affected: number | null;
  after: unknown;
  duration_ms: number | null;
};
export type AuditReport = {
  schema_version: "audit-1.0";
  retention_days: number;
  records: AuditRecord[];
  /** Older records match the filters (list with `before`: the last id). */
  has_more?: boolean;
};
export type AuditExport = { format: "json" | "csv"; content: string; mime: string; filename: string };

/**
 * Record that a write's confirmation was cancelled (Cancel or Escape):
 * nothing was sent. *statements* are the ones the sheet would have sent and
 * *connection* the same identity the write would have used. Best effort: a
 * failure to record never keeps the sheet open.
 */
export function recordDeclined(operation: string, statements: string[], connection: Connection, target?: Record<string, unknown>) {
  return api<{ audit_id: string; status: "declined" }>("audit/declined", { operation, statements, connection, target }).catch(
    () => null,
  );
}
/** Where remembered passwords go on this computer. Never contains secrets. */
export type SecretStore = {
  backend: string;
  label: string;
  available: boolean;
  error?: string;
};
export type Bundle = {
  schema_version: "investigation-1.0";
  id?: string;
  name: string;
  notes: string;
  tags: string[];
  baseline_id: string;
  plans: {
    id: string;
    name: string;
    raw: string;
    notes?: string;
    capture?: Capture;
  }[];
  updated_at?: string;
};
/**
 * investigation-summary-1.0: one row of `investigations/list`. Everything
 * Home and Library show, without the saved plans; `getInvestigation` returns
 * the complete bundle for Open, Compare and Export.
 */
export type InvestigationSummary = Omit<Bundle, "schema_version" | "plans"> & {
  schema_version: "investigation-summary-1.0";
  id: string;
  plans: { id: string; name: string; capture?: { mode?: string | null } }[];
};
export type InvestigationCursor = { updated_at: string; id: string };
/** investigation-list-1.0: newest first; `next_cursor` is null on the last page. */
export type InvestigationPage = {
  schema_version: "investigation-list-1.0";
  items: InvestigationSummary[];
  next_cursor: InvestigationCursor | null;
};
/** One page of saved investigations (1 to 50), filtered like Library's search. */
export function listInvestigations(limit: number, cursor: InvestigationCursor | null = null,
  search = "", signal?: AbortSignal) {
  return api<InvestigationPage>("investigations/list", { limit, cursor, search }, signal);
}
/** The complete saved bundle; a bundle read from a file is already complete. */
export function getInvestigation(item: InvestigationSummary | Bundle, signal?: AbortSignal): Promise<Bundle> {
  return item.schema_version === "investigation-1.0" ? Promise.resolve(item) :
    api<Bundle>("investigations/get", { id: item.id }, signal);
}
/**
 * The analysis as Export JSON writes it. `/api/analyze` sends the SQL once
 * (`query.raw`) instead of in every teach hook; the exported sidecar keeps
 * `query_sql` on each hook, as the CLI's does.
 */
export function exportedAnalysis(analysis: Analysis): Analysis {
  const sql = analysis.query?.raw;
  if (!sql || !Array.isArray(analysis.teach_hooks)) return analysis;
  return {
    ...analysis,
    teach_hooks: analysis.teach_hooks.map((hook) =>
      "query_sql" in hook ? hook : { ...hook, query_sql: sql }),
  };
}
export type MatchDelta = {
  before_node_id: string | null;
  after_node_id: string | null;
  before_label: string;
  after_label: string;
  short_label: string;
  classification: string;
  matching: { method: string; confidence: string; status: string };
  self_time_ms: {
    before: number | null;
    after: number | null;
    change_pct: number | null;
  };
};
export type TopStatement = {
  digest: string | null;
  digest_text: string | null;
  schema: string | null;
  count: number;
  total_ms: number;
  avg_ms: number;
  max_ms: number;
  rows_examined: number;
  rows_sent: number;
  examined_per_sent: number | null;
  no_index_used: number;
  no_good_index_used: number;
  tmp_disk_tables: number;
  tmp_tables?: number;
  lock_time_ms?: number;
  sort_merge_passes: number;
  sample_text: string | null;
  sample_truncated: boolean;
  first_seen: string | null;
  last_seen: string | null;
  flags: string[];
  /** Why Explain and Run analysis refuse this statement, or null. The
   * server applies capture's own SELECT-only rule. */
  capture_problem?: string | null;
  analysis_sql?: string | null;
  analysis_warning?: string | null;
  /** Workload attribution counters (absent in older replies): executions
   * that joined a later table with a full scan or a range check for each
   * row, and executions whose first table was scanned. */
  select_full_join?: number;
  select_range_check?: number;
  select_scan?: number;
  /** With a table filter: how the digest text names the table (a text match, not proof). */
  table_match?: { schema: string; table: string; kind: "qualified" | "unqualified"; label: string };
  /** Origin source e.g. "mysql.slow_log" or "performance_schema" */
  source?: string;
};
/** What a digest list covers: uptime, the oldest FIRST_SEEN listed, the
 * overflow row (DIGEST IS NULL) and Performance_schema_digest_lost. */
export type TopCoverage = {
  uptime_s: number | null;
  first_seen: string | null;
  digest_lost: number | null;
  overflow: { executions: number; total_ms: number } | null;
};
/** Orders of Top statements: the four rankings plus the workload attribution
 * filters (only digests whose counter is above zero). */
export type TopOrder =
  | "total"
  | "time"
  | "avg"
  | "examined"
  | "rows"
  | "count"
  | "exec_count"
  | "full_join"
  | "scan"
  | "scans"
  | "no_index"
  | "tmp_tables"
  | "locks";
export type TopTable = { schema: string; table: string };
export type TopReport = {
  schema_version: "top-1.0";
  engine: string;
  server_version: string;
  order: string;
  notes: string[];
  statements: TopStatement[];
  limit?: number;
  schema?: string | null;
  /** The table filter the list was read with, or null. */
  table?: TopTable | null;
  coverage?: TopCoverage;
  /** With a table filter: the label, its caveats and how many digests were checked. */
  match?: { label: string; caveats: string[]; candidates: number; capped: boolean };
};
export type SlowQueriesReport = {
  schema_version: "slow-queries-1.0";
  engine: string;
  server_version: string;
  source: string;
  min_seconds?: number;
  statements: TopStatement[];
};
export type RunningStatement = {
  id: number;
  user: string | null;
  host: string | null;
  db: string | null;
  command: string;
  time_s: number | null;
  state: string | null;
  info: string | null;
  explainable: boolean;
};
export type RunningReport = {
  schema_version: "running-1.0";
  engine: string;
  source: string;
  notes: string[];
  statements: RunningStatement[];
};
export type ConnectionPlan = {
  raw: string;
  mode: "explain" | "analyze-partial";
  connection_id: number;
  query: string;
  warnings: string[];
  capture: Capture;
};
/** locks-1.0: one row lock, as data_locks (MySQL) or INNODB_LOCKS (MariaDB) reports it. */
export type RowLock = {
  lock_id: string | null;
  trx_id: string | null;
  connection_id: number | null;
  schema: string | null;
  table: string | null;
  partition: string | null;
  index: string | null;
  lock_type: string | null;
  mode: string | null;
  status: string | null;
  lock_data: string | null;
  lock_data_truncated: boolean;
  lock_data_missing: boolean;
  mode_label: string;
  explain: string;
};
export type LockEdge = {
  kind: "row" | "metadata";
  reason: "granted" | "queued";
  lock: {
    lock_type?: string | null;
    mode?: string | null;
    mode_label?: string | null;
    object_type?: string | null;
    schema?: string | null;
    table?: string | null;
    index?: string | null;
    lock_data?: string | null;
  };
  wait_s: number | null;
  statement_s: number | null;
  certain?: boolean;
};
export type LockNode = {
  key: string;
  connection_id: number | null;
  trx_id: string | null;
  user: string | null;
  host: string | null;
  db: string | null;
  command: string | null;
  state: string | null;
  time_s: number | null;
  query: string | null;
  last_statement: string | null;
  idle_in_transaction: boolean;
  trx_state: string | null;
  trx_age_s: number | null;
  rows_locked: number | null;
  root: boolean;
  cycle: boolean;
  blocked_count: number;
  kill: { query: string | null; connection: string } | null;
  waits_for: LockEdge[] | null;
  blocked: LockNode[];
  repeat?: boolean;
  truncated?: boolean;
  omitted_branches?: number;
  truncation_reason?: "depth";
  /** locks-1.1: the fingerprint sent with a Kill, and whether this account may end it. */
  trx_started?: string | null;
  rows_modified?: number | null;
  ps_thread_id?: number | null;
  kill_allowed?: boolean | null;
  xa_prepared?: XaPrepared | null;
};
/** A prepared XA transaction (MySQL): ending its session releases nothing; XA COMMIT or XA ROLLBACK does. */
export type XaPrepared = { xid: string | null; commit: string | null; rollback: string | null };
export type LockTransaction = {
  trx_id: string;
  state: string | null;
  started: string | null;
  age_s: number | null;
  connection_id: number | null;
  wait_s: number | null;
  rows_locked: number | null;
  rows_modified: number | null;
  lock_structs: number | null;
  tables_locked: number | null;
  isolation_level: string | null;
  user: string | null;
  host: string | null;
  db: string | null;
  command: string | null;
  time_s: number | null;
  current_statement: string | null;
  last_statement: string | null;
  query_truncated: boolean;
  idle_in_transaction: boolean;
};
export type MetadataLock = {
  object_type: string | null;
  schema: string | null;
  name: string | null;
  lock_type: string | null;
  short: string | null;
  duration: string | null;
  status: string | null;
  connection_id: number | null;
  explain: string;
};
export type DeadlockLock = {
  lock_type: string;
  schema: string | null;
  table: string | null;
  index: string | null;
  mode: string;
  mode_label: string;
  explain: string;
  waiting: boolean;
  records: {
    heap_no: number;
    supremum: boolean;
    as_signed_int: number | null;
    fields: { hex: string | null; asc: string | null }[];
  }[];
};
export type Deadlock = {
  detected_at: string | null;
  victim: number | null;
  victim_trx_id: string | null;
  raw: string;
  raw_truncated: boolean;
  transactions: {
    n: number;
    trx_id: string | null;
    connection_id: number | null;
    user: string | null;
    host: string | null;
    active_s: number | null;
    statement: string | null;
    row_locks: number | null;
    holds: DeadlockLock[];
    waiting_for: DeadlockLock | null;
    victim: boolean;
  }[];
};
export type LocksReport = {
  /** Server observed while reading the report; absent in older reports. */
  server_identity?: KillServerIdentity;
  schema_version: "locks-1.0" | "locks-1.1";
  engine: string;
  server_version: string;
  captured_at: string;
  scope: "all" | "waits" | "deadlock";
  limit: number;
  capabilities: {
    performance_schema: boolean;
    process_privilege: boolean | null;
    transactions: boolean;
    row_locks: "all" | "waits_only" | "none";
    lock_waits: boolean;
    metadata_locks: boolean;
    mdl_instrument: boolean | null;
    last_statement: boolean;
    deadlock: boolean;
    data_locks_global_latch: boolean;
  };
  notes: string[];
  summary: {
    transactions: number;
    row_lock_waits: number;
    metadata_lock_waits: number;
    blocked_sessions: number;
    root_blockers: number;
    idle_in_transaction_blockers: number;
    longest_wait_s: number | null;
    oldest_transaction_s: number | null;
    row_locks: number;
    deadlock: boolean;
  };
  blocking_tree: LockNode[];
  blocking_tree_budget?: {
    node_limit: number; edge_limit: number; depth_limit: number;
    nodes: number; edges: number; omitted_roots: number; omitted_branches: number; truncated: boolean;
  };
  waits_truncated?: boolean;
  transactions: LockTransaction[];
  row_locks: RowLock[];
  row_locks_truncated: boolean;
  metadata_locks: {
    locks: MetadataLock[];
    waits: {
      waiting: MetadataLock;
      statement: string | null;
      statement_s: number | null;
      certain: boolean;
      blocked_by: { reason: string; lock: MetadataLock; statement: string | null }[];
      blocker_groups?: { group_id: number; reason: string; total: number; omitted: number }[];
      blockers_total?: number;
      blockers_omitted?: number;
      truncated?: boolean;
    }[];
    truncated: boolean;
    blocker_groups?: {
      id: number; object_type: string; schema: string | null; name: string | null;
      status: string; lock_type: string; locks: number; owners: number;
    }[];
    total_edges?: number;
    materialized_edges?: number;
    edge_limit?: number;
    totals_scope?: "collected_locks";
    waits_truncated?: boolean;
  };
  deadlock: Deadlock | null;
  /** locks-1.1 (absent in locks-1.0 reports). */
  ddl_waits?: DdlWait[];
  kill_privilege?: KillPrivilege | null;
};
/** locks-1.1: what this account may KILL, read from SHOW GRANTS (a hint; the server decides). */
export type KillPrivilege = {
  checked: boolean;
  user: string | null;
  account: string | null;
  can_kill_others: boolean | null;
  via: string | null;
  system_user: boolean | null;
  super_only?: boolean;
  error?: string;
};
/** A session that holds up a waiting DDL statement (exact, holding a lock, or likely). */
export type DdlBlocker = {
  connection_id: number | null;
  user: string | null;
  host: string | null;
  db: string | null;
  command: string | null;
  state: string | null;
  time_s: number | null;
  statement: string | null;
  last_statement: string | null;
  ps_thread_id: number | null;
  trx_id: string | null;
  trx_state: string | null;
  trx_started: string | null;
  trx_age_s: number | null;
  rows_locked: number | null;
  rows_modified: number | null;
  idle_in_transaction: boolean;
  running: boolean;
  kill_allowed: boolean | null;
  xa_prepared: XaPrepared | null;
  reason: "holds" | "queued" | "chain" | "open_transaction";
  certain: boolean;
  locks: { lock_type: string | null; short: string | null; duration: string | null; status: string | null }[];
  started_before_ddl: boolean | null;
  via: number | null;
  waits_for: number[];
  culprit: boolean;
};
export type DdlWait = {
  connection_id: number;
  user: string | null;
  host: string | null;
  db: string | null;
  statement: string | null;
  kind: string;
  state: string | null;
  wait_s: number | null;
  object_type: string | null;
  schema: string | null;
  table: string | null;
  /** The table the statement names; differs from `table` when a foreign key or RENAME locks another one. */
  statement_schema?: string | null;
  statement_table?: string | null;
  requested: string | null;
  requested_short: string | null;
  precision: "exact" | "granted_locks" | "likely";
  blockers: DdlBlocker[];
  blockers_truncated: boolean;
  queued: {
    connection_id: number;
    user: string | null;
    host: string | null;
    statement: string | null;
    wait_s: number | null;
    lock_type: string | null;
    short: string | null;
    certain: boolean;
  }[];
};
/** kill-1.0: the answer of POST /api/live/kill (refusals are results, not errors). */
export type KillServerIdentity = { server_uuid: string } | { server_id: string; hostname: string; version: string };
export type KillExpect = {
  user: string | null;
  host: string | null;
  trx_started: string | null;
  ps_thread_id: number | null;
  connection_identity: string;
  server_identity: KillServerIdentity;
};
export type KillReview = { review_token: string; statements: string[]; expect: KillExpect; connection_id: number; mode: "connection" | "query" };
export function prepareKill(connection: Connection, connection_id: number, mode: "connection" | "query", expect: KillExpect, signal?: AbortSignal) {
  return api<KillReview>("live/kill/prepare", { connection, connection_id, mode, expect }, signal);
}
/** Only called after the user confirms the captured connection and server. */
export function killLockSession(connection: Connection, connection_id: number, mode: "connection" | "query", expect: KillExpect, review_token: string) {
  return api<KillResult>("live/kill", { connection, connection_id, mode, confirmed: true, expect, review_token });
}
export type BulkKillTarget = {
  connection_id: number;
  mode: "connection" | "query";
  expect: KillExpect;
};
export type BulkKillReview = {
  review_token: string;
  targets: BulkKillTarget[];
  mode: "connection" | "query";
  statements: string[];
};
export type BulkKillResult = {
  schema_version: "kill-bulk-1.0";
  total: number;
  killed: number;
  gone: number;
  refused: number;
  denied: number;
  errors: number;
  message: string;
  results: Array<{ connection_id: number; status: string; message: string }>;
};
export function prepareBulkKill(connection: Connection, targets: BulkKillTarget[], signal?: AbortSignal) {
  return api<BulkKillReview>("live/kill/bulk/prepare", { connection, targets, mode: targets[0]?.mode || "connection" }, signal);
}
export function killBulkSessions(connection: Connection, targets: BulkKillTarget[], review_token: string) {
  return api<BulkKillResult>("live/kill/bulk", { connection, targets, mode: targets[0]?.mode || "connection", confirmed: true, review_token });
}
export type TableColumnInfo = {
  name: string;
  type: string;
  nullable: boolean;
  default: string | null;
  key: string;
  extra: string;
};
export function fetchTableColumns(connection: Connection, schema: string, table: string) {
  return api<{ schema: string; table: string; columns: TableColumnInfo[] }>("live/table-columns", { connection, schema, table });
}
export type KillResult = {
  schema_version: "kill-1.0";
  status: "killed" | "refused" | "denied" | "gone";
  message: string;
  statement: string | null;
  connection_id: number;
  mode: "connection" | "query";
  verified: string[];
  after: { present: boolean; command?: string | null; trx_state?: string | null; rows_modified?: number | null } | null;
};
/** health-1.0: a status from two SHOW GLOBAL STATUS samples (rules of thumb). */
export type HealthStatus = "ok" | "watch" | "problem" | "info" | "unknown";
export type HealthMetric = {
  key: string;
  group: string;
  label: string;
  /** What the metric measures, in plain language. */
  explain: string;
  unit: string;
  /** The headline number: a per-second rate, a current value, or a share. */
  value: number | null;
  delta: number | null;
  rate: number | null;
  max: number | null;
  status: HealthStatus;
  /** The observation and the rule of thumb behind the status. */
  why: string;
  rule: string | null;
  detail: string;
  /** Another Server view that shows the cause (Locks, Top statements…). */
  link: { view: "top" | "running" | "locks"; label: string } | null;
  /** A Learn lesson key, or null. */
  learn: string | null;
  sources: string[];
  inputs: Record<string, number | null>;
  /** Key rate worth a small trend line across refreshes. */
  sparkline: boolean;
};
export type HealthReport = {
  schema_version: "health-1.0";
  engine: string;
  server_version: string | null;
  interval: number;
  elapsed_s: number | null;
  started_at: string;
  taken_at: string;
  uptime_s: number | null;
  restarted: boolean;
  server: {
    engine: string;
    version: string | null;
    uptime_s: number | null;
    max_connections: number | null;
    table_open_cache: number | null;
    buffer_pool_bytes: number | null;
    page_size: number | null;
    redo_capacity_bytes: number | null;
  };
  summary: {
    /** The worst of the metrics and the diagnostics. */
    status: "ok" | "watch" | "problem";
    problem: number;
    watch: number;
    ok: number;
    info: number;
    unknown: number;
    /** Counts per status for the diagnostics (absent in older reports). */
    diagnostics?: Record<HealthStatus, number>;
  };
  groups: { key: string; label: string }[];
  /** Explained answers built from several deltas (absent in older reports). */
  diagnostics?: HealthDiagnostic[];
  metrics: HealthMetric[];
  notes: string[];
};
/** One health diagnostic: the numbers, the rule, a verdict, and the why. */
export type HealthDiagnostic = {
  key: string;
  title: string;
  status: HealthStatus;
  /** "full", "warming" or "room" (buffer pool), "not_applicable" (binary log off, thread pool), or null. */
  state: string | null;
  /** The key number as text: "12.4 MiB/s read from disk". */
  headline: string;
  value: number | null;
  unit: string;
  verdict: string;
  why: string;
  rule: string;
  numbers: {
    key: string;
    label: string;
    value: number | null;
    unit: string;
    text: string;
    /** A projection, not a measurement. */
    estimate?: boolean;
  }[];
  settings: { name: string; value: string; display: string; default: string | null }[];
  recommendation: { text: string; caveat: string } | null;
  sources: string[];
  learn: string | null;
  link: { view: "top" | "running" | "locks"; label: string } | null;
  /** A rate worth a small trend line across refreshes. */
  trend: { key: string; label: string; unit: string; value: number | null } | null;
};
/** Shared by the Performance workspace's live views (waits, indexes, io, config). */
type PerfBase = {
  engine: string;
  server_version: string | null;
  taken_at: string;
  uptime_s: number | null;
  performance_schema: boolean;
  notes: string[];
};
/** waits-1.0: wait events over a window and since server start. */
export type WaitEvent = {
  event: string;
  class: string;
  class_label: string;
  meaning: string;
  count: number;
  wait_ms: number;
  avg_ms: number | null;
  timed: boolean;
  /** Window only: milliseconds of waiting per second of wall time, summed across threads. */
  wait_ms_per_s?: number | null;
  /** Since start only: the longest single wait. */
  max_ms?: number | null;
};
export type WaitClass = {
  class: string;
  label: string;
  explain: string;
  events: number;
  count: number;
  wait_ms: number;
  wait_ms_per_s?: number | null;
};
export type WaitCoverage = {
  class: string;
  label: string;
  explain: string;
  enabled: number;
  timed: number;
  total: number;
  state: "on" | "partial" | "off";
  enable?: { runtime: string; config: string };
};
export type WaitsReport = PerfBase & {
  schema_version: "waits-1.0";
  interval: number;
  elapsed_s: number | null;
  started_at: string | null;
  restarted: boolean;
  available: boolean;
  unavailable: string | null;
  coverage: WaitCoverage[] | null;
  /** Shown with every enable statement. */
  enable_caveat: string;
  window: {
    status: "ok" | "reset" | "restarted";
    events: WaitEvent[];
    classes: WaitClass[];
    untimed: { event: string; count: number }[];
    more?: number;
  } | null;
  since_start: {
    uptime_s: number | null;
    events: WaitEvent[];
    classes: WaitClass[];
    more: number;
  } | null;
};
/** indexes-1.0: unused and redundant indexes, full scans. */
export type UnusedIndex = {
  schema: string;
  table: string;
  index: string;
  columns: string;
  visible: boolean;
  foreign_keys: string[];
  verdict: "candidate" | "keep";
  why: string;
  test_sql?: string;
  drop_sql?: string;
  caveat?: string;
};
export type RedundantIndex = {
  schema: string;
  table: string;
  index: string;
  columns: string;
  unique: boolean;
  visible: boolean;
  dominant: string;
  dominant_columns: string;
  reason: string;
  foreign_keys: string[];
  why: string;
  test_sql: string;
  drop_sql: string;
  caveat: string;
};
export type FullScanTable = {
  schema: string;
  table: string;
  rows_without_index: number;
  rows_with_index: number;
  share_rows: number;
  fetch_ms_without_index: number | null;
  fetch_ms_with_index: number | null;
  share_time: number | null;
  mostly: boolean;
  why: string;
};
export type IndexInventoryRow = { schema: string; table: string; index: string; columns: string; unique: boolean; type: string; visible: boolean };
export type IndexesReport = PerfBase & {
  indexes?: IndexInventoryRow[];
  schema_version: "indexes-1.0";
  scope: { schema: string | null; schemas?: string[] | null; server_wide: boolean };
  source: "performance_schema" | "userstat" | null;
  available: boolean;
  unused: UnusedIndex[];
  redundant: RedundantIndex[];
  full_scans: FullScanTable[];
  no_data: { schema: string; table: string; indexes: number; reason: string }[];
  foreign_keys: number;
  summary: {
    tables: number;
    indexes: number;
    unused: number;
    unused_candidates: number;
    keep_foreign_key: number;
    redundant: number;
    full_scans: number;
    mostly_full_scans: number;
    no_data: number;
  };
  more: { indexes?: number; unused: number; redundant: number; full_scans: number; no_data: number };
};
/** io-1.0: table and file I/O over a window and since server start. */
export type IoTable = {
  schema: string;
  table: string;
  ops: number | null;
  total_ms: number | null;
  read_rows: number;
  read_ms: number | null;
  write_rows: number;
  write_ms: number | null;
  inserts: number | null;
  updates: number | null;
  deletes: number | null;
  total_ms_per_s?: number | null;
};
export type IoFile = {
  file: string;
  event: string;
  kind: string;
  label: string;
  schema: string | null;
  table: string | null;
  partition: string | null;
  read_ops: number;
  read_bytes: number;
  read_ms: number;
  write_ops: number;
  write_bytes: number;
  write_ms: number;
  misc_ops: number;
  misc_ms: number;
  total_ms: number;
};
export type IoFileClass = Omit<IoFile, "file" | "kind" | "label" | "schema" | "table" | "partition"> & {
  meaning: string;
};
export type IoGlobal = {
  key: string;
  label: string;
  unit: string;
  explain: string;
  sources: string[];
  delta: number | null;
  rate: number | null;
};
export type IoReport = PerfBase & {
  schema_version: "io-1.0";
  collection?: {
    instance_limit: number;
    budget_s: number;
    all_instances: boolean;
    counts: Partial<Record<"tables" | "files" | "userstat", number>>;
    omitted: ("tables" | "files" | "userstat")[];
    complete: boolean;
  };
  interval: number;
  elapsed_s: number | null;
  started_at: string | null;
  restarted: boolean;
  scope: { schema: string | null };
  source: "performance_schema" | "userstat" | "status";
  global: IoGlobal[];
  window: {
    status: "ok" | "reset" | "restarted";
    tables: IoTable[];
    files: IoFile[];
    file_classes: IoFileClass[];
    more: Record<string, number>;
  } | null;
  since_start: {
    uptime_s: number | null;
    tables: IoTable[];
    files: IoFile[];
    file_classes: IoFileClass[];
    more: Record<string, number>;
  } | null;
};
/** config-1.0: settings with engine defaults and rule-of-thumb statuses. */
export type ConfigSetting = {
  name: string;
  value: string | null;
  display: string | null;
  default: string | null;
  set_by: string | null;
  alias?: string;
  dynamic?: boolean | null;
};
export type ConfigCheck = {
  key: string;
  group: string;
  label: string;
  explain: string;
  value: string | null;
  status: HealthStatus;
  why: string;
  rule: string | null;
  caveat: string;
  action: { sql?: string; config?: string; text?: string } | null;
  settings: ConfigSetting[];
  sources: string[];
};
/** parameters-1.0: catalog metadata; null preserves an unknown or SQL NULL. */
export type ConfigParameter = {
  name: string;
  value: string | null;
  truncated: boolean;
  scope: "global" | "both" | null;
  apply_type: "dynamic" | "static" | null;
  /** none: set by the server; no option or statement changes it. */
  modifiable: "now" | "restart" | "option_file" | "none" | null;
  type: "bytes" | "integer" | "double" | "boolean" | "enum" | "set" | "flagset" | "string" | null;
  values: string[] | null;
  min: number | null;
  max: number | null;
  block_size: number | null;
  default: string | null;
  source: string | null;
  source_label: string | null;
  source_path: string | null;
  set_time: string | null;
  set_user: string | null;
  set_host: string | null;
  persisted: boolean | null;
  modified: boolean | null;
  description: string | null;
  checks: string[];
};
export type ConfigParameters = {
  schema_version: "parameters-1.0";
  complete: boolean;
  notes: string[];
  items: ConfigParameter[];
};
export type ConfigReport = PerfBase & {
  schema_version: "config-1.0";
  summary: HealthReport["summary"];
  groups: { key: string; label: string }[];
  checks: ConfigCheck[];
  parameters?: ConfigParameters;
};
/** instruments-1.0: Performance Schema setup with gain, loss, cost and the statements to run (ps_instruments.py). */
export type InstrumentCost = { level: "low" | "moderate" | "high"; text: string };
export type InstrumentSql = { on: { runtime: string; config: string }; off: { runtime: string; config: string } };
export type InstrumentFeature = { key: string; label: string; view: string; required: boolean };
export type InstrumentGroup = {
  key: string;
  id: string;
  label: string;
  match: string | null;
  enabled: number | null;
  timed: number | null;
  total: number | null;
  state: "on" | "off" | "partial" | "counted" | "absent" | "unknown";
  changed: number | null;
  changed_names?: string[];
  builtin?: number;
  kinds?: { kind: string; enabled: number; total: number }[] | null;
  default: string;
  timed_applies: boolean;
  gain: string;
  lose: string;
  cost: InstrumentCost | null;
  sql: InstrumentSql | null;
  features: InstrumentFeature[];
};
export type Instrument = {
  name: string;
  group: string;
  enabled: boolean;
  timed: boolean | null;
  timed_applies: boolean;
  default_enabled: boolean | null;
  default_timed: boolean | null;
  builtin: boolean;
  properties: string;
};
export type InstrumentConsumer = {
  name: string;
  id: string;
  enabled: boolean | null;
  effective: boolean | null;
  blocked_by: string[];
  default: boolean;
  requires: string[];
  gain: string;
  lose: string;
  cost: InstrumentCost;
  sql: InstrumentSql;
  features: InstrumentFeature[];
};
export type InstrumentsSetup = {
  rows: Record<string, string | boolean>[];
  state: "default" | "custom" | "empty";
  restore: string | null;
  explain: string;
  default_text: string;
  disabled_table_schemas?: string[];
};
export type InstrumentsPreset = {
  key: string;
  label: string;
  summary: string;
  gain: string;
  cost: InstrumentCost;
  runtime: string;
  config: string;
  undo: string | null;
  notes: string[];
  applied: boolean | null;
  restore_setup?: string | null;
};
export type InstrumentsReport = PerfBase & {
  schema_version: "instruments-1.0";
  available: boolean;
  unavailable: string | null;
  status: {
    id: string;
    enabled: boolean;
    default: boolean;
    text: string;
    enable: { config: string; persist: string | null; persist_text: string | null } | null;
    disable: { config: string } | null;
    memory_bytes: number | null;
    sizes: Record<string, number>;
    lost: string[];
    cost: string;
  };
  features: {
    key: string;
    label: string;
    view: string;
    explain: string;
    status: "ready" | "partial" | "off" | "unavailable" | "unknown" | "not_needed";
    missing: string[];
    optional_missing: string[];
    rows: string[];
  }[];
  instruments: Instrument[];
  groups: InstrumentGroup[];
  consumers: InstrumentConsumer[];
  actors: InstrumentsSetup | null;
  objects: InstrumentsSetup | null;
  presets: InstrumentsPreset[];
  rules: string[];
  privileges: string;
  read_only_note: string;
};
/** schemas-1.0: the schema picker's list (perf_common.schema_catalog). */
export type SchemaInfo = { name: string; tables: number; system: boolean };
export type SchemaCatalog = PerfBase & {
  schema_version: "schemas-1.0";
  schemas: SchemaInfo[];
  /** Base tables in every non-system schema. */
  user_tables: number;
  /** Indexes reads every user schema by default only up to this many tables. */
  all_limit: number;
  all_note: string;
  system: string[];
};
/** schema-2.0: complete inventory, primary keys and nullable InnoDB free-space ratios. */
export type TableStatement = { label: string; sql: string; template?: boolean };
export type MissingPrimaryKey = {
  schema: string;
  table: string;
  engine: string | null;
  rows: number | null;
  size_bytes: number | null;
  partitioned: boolean;
  kind: "no_usable_key" | "not_null_unique";
  status: "problem" | "watch";
  /** The index InnoDB clusters on, "hidden row ID", or null (not InnoDB). */
  clustered_on: string | null;
  unique_keys: { index: string; columns: string; usable: boolean; why_not: string | null }[];
  why: string;
  checks: TableStatement[];
  fixes: TableStatement[];
  caveat: string;
};
export type FreeSpaceRow = {
  schema: string;
  table: string;
  rows: number | null;
  data_bytes: number | null;
  index_bytes: number | null;
  free_bytes: number | null;
  free_pct: number | null;
  partitioned: boolean;
  tablespace: "file_per_table" | "system" | "general" | null;
  tablespace_name: string | null;
  status: "ok" | "watch" | "problem" | "shared" | "unknown" | "info";
  why: string;
  fix_sql?: string;
  online_sql?: string;
  caveat?: string;
};
export type TableInventoryRow = {
  schema: string; table: string; engine: string | null; rows: number | null;
  data_bytes: number | null; index_bytes: number | null; size_bytes: number | null; partitioned: boolean;
};
export type TablesReport = PerfBase & {
  /** Complete scoped inventory on current servers; absent in older saved reports. */
  tables?: TableInventoryRow[];
  schema_version: "schema-1.0" | "schema-2.0";
  scope: { schemas: string[] | null; all_user_schemas: boolean; excluded: string[] };
  statistics: { fresh_requested: boolean; fresh: boolean; cache_expiry_s: number | null; cached: boolean };
  tablespaces_known: boolean;
  tablespace_collection?: { show_create_limit: number; deadline_s: number; unresolved: number };
  summary: {
    status: "ok" | "watch" | "problem";
    tables: number;
    innodb: number;
    other_engines: number;
    no_usable_key: number;
    not_null_unique: number;
    gipk: number;
    free_problem: number;
    free_watch: number;
    free_info?: number;
    shared: number;
    unknown_tablespace: number;
  };
  thresholds: Partial<Record<"watch" | "problem", { free_pct: number; free_bytes: number }>>;
  rules: Record<string, string>;
  why: { key: string; title: string; text: string }[];
  missing_primary_keys: MissingPrimaryKey[];
  generated_primary_keys: { schema: string; table: string; engine: string | null; rows: number | null; size_bytes: number | null }[];
  fragmentation: FreeSpaceRow[];
  shared_tablespaces: { name: string; type: string; free_bytes: number; tables: string[]; why: string }[];
  more: { tables?: number; missing_primary_keys: number; generated_primary_keys: number; fragmentation: number };
};
/** sessions-1.0: the process list, post-processed (sessions.py). Replaces running-1.0. */
export type SessionFlag = "long_running" | "idle_in_trx" | "lock_wait" | "system_thread" | "own_connection";
export type SessionTrx = {
  id: string;
  state: string | null;
  started: string | null;
  age_s: number | null;
  wait_started: string | null;
  wait_s: number | null;
  rows_locked: number | null;
  rows_modified: number | null;
  lock_structs: number | null;
  tables_locked: number | null;
  isolation_level: string | null;
  read_only: boolean | null;
  operation_state: string | null;
};
export type Session = {
  id: number;
  user: string | null;
  host: string | null;
  client_host: string | null;
  client_port: number | null;
  db: string | null;
  command: string | null;
  time_s: number | null;
  state: string | null;
  info: string | null;
  info_short: string | null;
  info_truncated: boolean;
  last_statement: string | null;
  thread_kind: "client" | "daemon" | "replication";
  trx: SessionTrx | null;
  /** innodb: a row lock or an InnoDB table lock (INNODB_TRX LOCK WAIT); metadata; table (table-level lock). */
  lock_wait: { kind: "innodb" | "metadata" | "table"; wait_s: number | null; detail: string | null } | null;
  progress: { stage: number; max_stage: number; pct: number } | null;
  memory_used: number | null;
  rows_examined: number | null;
  explainable: boolean;
  flags: SessionFlag[];
  status: HealthStatus;
  why: string | null;
  active: boolean;
  is_locked?: boolean;
  lock_status?: "waiting" | "holding" | null;
  lock_detail?: string | null;
};
export type SessionBreakdown = { key: string | null; count: number; other?: boolean }[];
export type SessionsReport = PerfBase & {
  /** Server observed while reading the report; absent in older reports. */
  server_identity?: KillServerIdentity;
  schema_version: "sessions-1.0";
  source: string;
  source_mode?: "auto" | "show_processlist";
  ps_show_processlist?: "ON" | "OFF" | null;
  processlist_source?: string;
  capabilities: { process_privilege: boolean | null; transactions: boolean; history_list_length: boolean };
  filters: { active_only: boolean; user: string | null; host: string | null; db: string | null; min_seconds: number; locked_only?: boolean };
  limit: number;
  thresholds: { long_running_s: number[]; trx_age_s: number[] };
  rules: Record<string, string>;
  summary: {
    status: HealthStatus;
    text: string;
    total: number;
    active: number;
    idle: number;
    system: number;
    running_statements: number;
    long_running: { watch: number; problem: number };
    idle_in_trx: number;
    lock_wait: { innodb: number; metadata: number; table: number; total: number };
    locked?: number;
    waiting_locks?: number;
    connections: {
      current: number | null;
      max: number | null;
      max_used: number | null;
      running: number | null;
      listed: number;
      used_pct: number | null;
      peak_pct: number | null;
      status: HealthStatus;
      rule: string | null;
    };
    by_command: SessionBreakdown;
    by_state: SessionBreakdown;
    by_user: SessionBreakdown;
    by_host: SessionBreakdown;
    by_db: SessionBreakdown;
    oldest_transaction: {
      connection_id: number | null;
      trx_id: string | null;
      age_s: number | null;
      state: string | null;
      read_only: boolean | null;
      isolation_level: string | null;
      rows_locked: number | null;
      rows_modified: number | null;
      session_command: string | null;
      user: string | null;
      client_host: string | null;
      idle: boolean | null;
      status: HealthStatus;
      rule: string | null;
    } | null;
    history_list_length: { value: number | null; source: string | null; status: HealthStatus; rule: string | null } | null;
  };
  attention: Record<"long_running" | "idle_in_trx" | "lock_wait", { id: number; seconds: number | null; status: HealthStatus; kind?: string }[]>;
  matched: number;
  truncated: boolean;
  sessions: Session[];
  links: Record<string, { view: string; label: string }>;
};
/** innodb-1.0: SHOW ENGINE INNODB STATUS parsed into sections (innodb_status.py). */
export type InnodbItem = {
  key: string;
  label: string;
  value: number | string | null;
  unit: string | null;
  text: string;
  status: HealthStatus | null;
  rule: string | null;
};
export type InnodbSection = {
  key: string;
  title: string;
  present: boolean;
  status: HealthStatus;
  summary: string;
  items: InnodbItem[];
  data: Record<string, unknown> | null;
  links: { view: string; label: string }[];
};
export type InnodbReport = PerfBase & {
  schema_version: "innodb-1.0";
  available: boolean;
  error: { code: string; message: string } | null;
  status: HealthStatus;
  header: { printed_at: string | null; window_s: number | null } | null;
  summary: { status: HealthStatus; problem: number; watch: number; ok: number; info: number; unknown: number; text: string };
  sections: InnodbSection[];
  parse: { complete: boolean; sections_found: string[]; unknown_sections: string[]; truncated_by_server: boolean } | null;
  rules: Record<string, string>;
  raw: { text: string; truncated: boolean } | null;
};

/** error-log-1.0: read-only server error log; messages are already decoded by the backend. */
export type ErrorLogEntry = {
  logged: string;
  thread_id: number | null;
  prio: "System" | "Error" | "Warning" | "Note";
  error_code: string | null;
  subsystem: string | null;
  message: string;
  previous_run: boolean;
};
export type ErrorLogCode = {
  error_code: string | null;
  prio: string;
  subsystem: string | null;
  count: number;
  first_logged: string;
  last_logged: string;
  sample: string;
};
export type ErrorLogReport = PerfBase & {
  schema_version: "error-log-1.0";
  available: boolean;
  reason: null | "performance_schema_off" | "denied" | "no_pfs_sink" | "log_to_stderr" | "remote"
    | "other_host" | "not_found" | "not_readable" | "identity_mismatch" | "identity_unverified" | "unrecognized_format";
  reason_text: string | null;
  how_to_view: string | null;
  source: null | "performance_schema.error_log" | "file";
  settings: {
    log_error: string | null;
    log_error_verbosity: number | null;
    log_error_services: string | null;
    log_warnings: number | null;
    datadir: string | null;
  };
  buffer: null | { bytes: number; events: number; expired_events: number; latest_write: string | null; capacity_bytes: 5242880 };
  file: null | { path: string; size_bytes: number; read_bytes: number; truncated: boolean; banner_found: boolean };
  server_started: string | null;
  counts: { system: number; error: number; warning: number; note: number };
  counts_current_run: { system: number; error: number; warning: number; note: number };
  codes: ErrorLogCode[];
  entries: ErrorLogEntry[];
  entries_total: number;
  limit: number;
  levels: string[];
  summary: { status: "ok" | "watch" | "unknown"; text: string };
};

/** report-1.0: the Full analysis (myflames/report.py), one read-only run of
 * every Server view composed into ranked findings, sections and the
 * statements behind the numbers. Recommendations are text, never applied. */
export type AnalysisStatus = "problem" | "watch" | "unknown" | "info" | "ok";
export type AnalysisLink = {
  /** A Server view id (health, config, top…). */
  view: string;
  label: string;
  /** The row of that view (a check, a metric or diagnostic key). */
  key?: string;
  /** Top statements links: the attribution order and table ("schema.table"). */
  order?: string;
  table?: string;
};
export type AnalysisFinding = {
  /** Health diagnostic state; absent in older report-1.0 payloads. */
  state?: string | null;
  id: string;
  family: string;
  status: AnalysisStatus;
  title: string;
  headline: string;
  /** What this server shows, without the rule. */
  verdict: string;
  why: string;
  rule: string | null;
  caveat: string | null;
  action: { text?: string; sql?: string; config?: string } | null;
  numbers: { label: string; text: string; value?: number; unit?: string }[];
  settings: { name: string; value?: string; display?: string; default?: string | null }[];
  links: AnalysisLink[];
  source: { view: string; kind: string; key: string } | null;
  learn: string | null;
  impact: number | null;
  attribution: "full_joins" | "full_scans" | null;
};
export type AnalysisSection = {
  key: string;
  title: string;
  status: AnalysisStatus;
  summary: string;
  counts: Record<AnalysisStatus, number>;
  findings: string[];
  views: string[];
  notes: string[];
};
export type AnalysisView = {
  key: string;
  label: string;
  status: "ok" | "limited" | "failed" | "skipped";
  schema_version: string | null;
  started_at: string | null;
  taken_at: string | null;
  elapsed_s: number | null;
  error: string | null;
  notes: string[];
};
export type AnalysisScanTable = {
  schema: string;
  table: string;
  label: string;
  rows_without_index: number | null;
  fetch_ms_without_index: number | null;
  share_time: number | null;
  statements: TopStatement[];
  capped: boolean;
  error: string | null;
};
export type AnalysisAttribution = {
  available: boolean;
  /** One sentence when statements could not be linked (Performance Schema off…). */
  gate: string | null;
  fix: string | null;
  label: string;
  caveats: string[];
  coverage: TopCoverage | null;
  health_window: { select_full_join: number | null; rate: number | null; elapsed_s: number | null; status: string | null } | null;
  full_joins: { order: string; statements: TopStatement[]; taken_at: string | null } | null;
  full_scans: AnalysisScanTable[];
  notes: string[];
};
export type AnalysisReport = {
  schema_version: "report-1.0";
  myflames_version: string;
  engine: string;
  server_version: string | null;
  taken_at: string;
  uptime_s: number | null;
  performance_schema: boolean;
  header: {
    server: { engine: string; version: string | null; version_comment: string | null; uptime_s: number | null; performance_schema: boolean };
    started_at: string | null;
    elapsed_s: number | null;
    interval: number;
    scope: { schemas: string[] | null; all_user_schemas: boolean; text: string };
    views: AnalysisView[];
    failures: { view: string; label: string; status: "failed" | "skipped"; error: string }[];
  };
  summary: { problem: number; watch: number; unknown: number; info: number; ok: number; findings: number; status: AnalysisStatus; text: string };
  look_first: string | null;
  findings: AnalysisFinding[];
  sections: AnalysisSection[];
  attribution: AnalysisAttribution;
  checklist: { covered: { key: string; label: string }[]; not_checked: { label: string; reason: string }[] };
  notes: string[];
};
/** The connection a Full analysis was read from, without secrets: an export names it. */
export type AnalysisSource = { label: string; host: string; port: number; user: string; type?: "production" | "development" };
/** POST /api/report/export: a report-1.0 report as a file, generated from the JSON. */
export type AnalysisExport = { format: string; content: string; mime: string; filename: string };

// ---- Query editor sessions (/api/editor/*, editor_session.py) ----------------

/** editor-session-1.0: one tab's client session (one server session). */
export type EditorSession = {
  schema_version: "editor-session-1.0";
  tab_id: string;
  state: "starting" | "idle" | "running" | "ended";
  connection: {
    profile_id: string | null;
    name: string | null;
    host: string;
    port: number;
    user: string;
    type: ConnectionType;
    read_only: boolean;
  };
  engine: "mysql" | "mariadb" | null;
  server_version: string | null;
  connection_id: number | null;
  autocommit: boolean | null;
  /** True or false, or null when this server cannot tell. */
  in_transaction: boolean | null;
  transaction_source: string | null;
  transaction_note: string | null;
  sql_mode: string | null;
  backslash_escapes: boolean | null;
  safe_updates: boolean | null;
  safe_updates_default: boolean;
  read_only_transactions: boolean;
  started_at: string;
  idle_seconds: number;
  idle_timeout_seconds: number;
  ended: boolean;
  /** closed, idle, connection_lost, client_exited, unresponsive, stopped, protocol, reconnected, server_exit, start_failed */
  ended_reason: string | null;
  message: string | null;
  notes: string[];
  run: { id: string; state: string } | null;
};
/** A result column: the name from the first row (types are not in the client's XML output). */
export type EditorColumn = { name: string; hex: boolean; type: string | null };
export type EditorRows = {
  type: "rows";
  statement: number;
  columns: EditorColumn[];
  /** False for an empty result set: the XML output names no columns then. */
  columns_known: boolean;
  /** null is SQL NULL; every other value is the server's text. */
  rows: (string | null)[][];
  row_count: number;
  truncated: boolean;
  limit: number;
  /** row_limit / size_limit: one read stopped with KILL QUERY; *_read: read to its end, extra rows dropped. */
  stopped: "row_limit" | "row_limit_read" | "size_limit" | "size_limit_read" | null;
  label: string | null;
  server_rows: number | null;
  warnings: number | null;
  duration_ms: number | null;
};
export type EditorOk = {
  type: "ok";
  statement: number;
  affected_rows: number | null;
  info: string | null;
  warnings: number | null;
  duration_ms: number | null;
};
export type EditorError = {
  type: "error";
  statement: number;
  code: number | null;
  sqlstate: string | null;
  message: string;
  /** Interrupted by Stop (and undone). */
  interrupted: boolean;
  duration_ms: number | null;
  /** UTF-16 offset in the sql sent, with its line and column. */
  position: { offset: number; line: number; column: number; exact?: boolean } | null;
};
export type EditorResult = EditorRows | EditorOk | EditorError;
export type EditorStatementStatus = "pending" | "running" | "ok" | "error" | "stopped" | "not_run" | "session_ended";
export type EditorStatement = {
  index: number;
  text: string;
  start: number;
  end: number;
  verb: string | null;
  kind: string | null;
  category: string | null;
  status: EditorStatementStatus;
  duration_ms: number | null;
  sent?: string;
};
/** editor-run-1.0: one run (Run, Run all, Commit, Rollback). */
export type EditorRun = {
  schema_version: "editor-run-1.0";
  id: string;
  kind: "run" | "action";
  state: "running" | "complete" | "error" | "stopped" | "session_ended";
  scope: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  limit: number;
  statements: EditorStatement[];
  results: EditorResult[];
  error: EditorError | null;
  stopped: { by: string; interrupted: boolean; message: string } | null;
  message: string | null;
  counts: { statements: number; run: number; rows: number; affected_rows: number; warnings: number; errors: number };
  audit_id: string | null;
  history_id: string | null;
};
/** What a write needs before it is sent: a one-time token for exactly these statements. */
export type EditorConfirmation = {
  token: string;
  expires_in: number;
  title: string;
  text: string;
  tone: "danger" | "primary";
  kind: string;
  category: string;
  reasons: string[];
  /** Statements that commit a transaction that is (or may be) open: they always ask. */
  warnings: string[];
  in_transaction: boolean | null;
  statements: string[];
  count: number;
  connection_type: ConnectionType;
  connection: { name: string; host: string; port: number; user: string };
};
export type EditorReply = {
  status: "complete" | "error" | "stopped" | "running" | "session_ended" | "needs_confirmation" | "refused" | "declined" | "invalid" | "idle" | "starting" | "ended" | "none";
  session?: EditorSession | null;
  run?: EditorRun | null;
  confirmation?: EditorConfirmation;
  message?: string;
  code?: string;
  audit_id?: string;
};
export type EditorStop = { stopped: boolean; stop_requested?: boolean; message: string; session: EditorSession | null };
export type EditorClose = { closed: boolean; reason?: "transaction_open" | "transaction_unknown"; message?: string; session: EditorSession | null };
export type EditorHistoryItem = {
  at: string;
  tab_id: string;
  connection: { profile_id: string | null; name: string | null; host: string; port: number };
  engine: string | null;
  kind: string;
  scope: string;
  sql: string;
  sql_truncated: boolean;
  statements: number;
  statements_run: number;
  duration_ms: number | null;
  rows: number;
  affected_rows: number;
  status: string;
  error_code: number | null;
};
export type EditorExport = { format: string; content: string; mime: string; filename: string };
/** catalog-1.0: the schema browser's tables of one schema, or one table's columns and indexes. */
export type CatalogTables = {
  schema_version: "catalog-1.0";
  level: "tables";
  schema: string;
  tables: { name: string; type: "table" | "view" | "system view" | "sequence" }[];
  truncated: boolean;
  limit: number;
  prefix: string | null;
  after: string | null;
  next_after: string | null;
};
export type CatalogColumn = {
  name: string;
  type: string;
  nullable: boolean;
  key: string;
  default: string | null;
  extra: string;
  generated: boolean;
};
export type CatalogIndex = {
  name: string;
  unique: boolean;
  primary: boolean;
  type: string;
  columns: { name: string | null; expression: string | null; sub_part: number | null }[];
};
export type CatalogTable = {
  schema_version: "catalog-1.0";
  level: "table";
  schema: string;
  table: string;
  columns: CatalogColumn[];
  indexes: CatalogIndex[];
};

export type AppUpdateInfo = {
  schema_version: "app-update-1.0";
  current_version: string;
  latest_version: string;
  update_available: boolean;
  release_title: string;
  release_notes: string;
  published_at: string;
  html_url: string;
  asset_name?: string | null;
  asset_url?: string | null;
  asset_size?: number | null;
  /** When the release metadata was read (UTC, ISO 8601). */
  checked_at?: string;
  error?: string;
  /** A short plain reason when the check failed. */
  reason?: string;
  /** The outcome kept in the workspace store (present from the workspace). */
  last_check?: AppUpdateLastCheck | null;
};

/** The last update check kept on this computer (never release notes or URLs). */
export type AppUpdateLastCheck = {
  checked_at: string;
  outcome: "up_to_date" | "available" | "failed";
  current_version: string;
  latest_version: string | null;
  reason: string | null;
};

/** Asks GitHub for the latest release: only when the user clicks Check for updates. */
export const checkAppUpdate = (force = false, signal?: AbortSignal) =>
  api<AppUpdateInfo>("app/update/check", { force }, signal);

/** The stored last check; local only, never the network. */
export const appUpdateStatus = (signal?: AbortSignal) =>
  api<{ schema_version: "app-update-status-1.0"; current_version: string; last_check: AppUpdateLastCheck | null }>(
    "app/update/status", {}, signal);

export const downloadAppUpdate = (asset_url?: string | null, release_url?: string | null, signal?: AbortSignal) =>
  api<{ status: string; download_path?: string; message: string; html_url?: string }>(
    "app/update/download",
    { asset_url, release_url },
    signal
  );
