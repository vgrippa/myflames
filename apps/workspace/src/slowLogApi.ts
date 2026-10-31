// Query digest's slow log sources: typed calls for /api/slow-log/* (a slow
// query log file on this computer, slowlog_api.py) and /api/live/slow-log
// (mysql.slow_log over the connection in use, slow_queries.py). Both answer
// slow-log-1.0 (docs/schemas/slow-log-1.0.json); a file read with a
// progress_id reports slow-log-progress-1.0. Python writes every number and
// sentence; the view lays them out.
import { api, type Connection, type DigestHistogramBucket, type TopStatement } from "./api";

export type SlowLogOrder = "total" | "count" | "avg" | "max" | "p95" | "lock" | "examined" | "sent";
export type SlowLogName = { name: string; count: number };
export type SlowLogSample = {
  text: string;
  truncated: boolean;
  db: string | null;
  user: string | null;
  host: string | null;
  /** As the log writes it, in the report's clock. */
  time: string | null;
  thread_id: number | null;
  query_time_ms: number;
  /** The entry's first line in the file; null for the table. */
  line: number | null;
};
/** One statement shape: a TopStatement (so the table, the inspector and the
 * Query editor read it the same way) plus what a slow log adds. */
export type SlowLogStatement = Omit<TopStatement, "no_index_used" | "no_good_index_used" | "tmp_disk_tables" | "sort_merge_passes" | "rows_examined" | "rows_sent"> & {
  digest: null;
  fingerprint_id: string;
  rows_sent: number;
  rows_examined: number;
  min_ms: number;
  rows_affected: number | null;
  bytes_sent: number | null;
  no_index_used: null;
  no_good_index_used: null;
  tmp_disk_tables: number | null;
  sort_merge_passes: number | null;
  filesort: number | null;
  filesort_on_disk: number | null;
  errors: number | null;
  killed: number | null;
  sample: SlowLogSample | null;
  users: SlowLogName[];
  hosts: SlowLogName[];
  histogram: DigestHistogramBucket[];
  admin: boolean;
  source: "slow_log_file" | "mysql.slow_log";
};
export type SlowLogClock = {
  kind: "utc" | "offset" | "local" | "mixed" | "none" | "server";
  /** A short name: "UTC", "UTC-03:00", "Server local time", "Server time (UTC-03:00)". */
  label: string | null;
  offsets: string[];
  /** One sentence on what the times mean. */
  text: string;
};
export type SlowLogFileSource = { kind: "file"; name: string; size_bytes: number; modified: string | null; file_id?: string };
export type SlowLogTableSource = { kind: "table"; table: "mysql.slow_log"; rows: number | null; rows_read: number; capped: boolean };
export type SlowLogReport = {
  schema_version: "slow-log-1.0";
  source: SlowLogFileSource | SlowLogTableSource;
  engine: "mysql" | "mariadb" | null;
  flavor: string | null;
  server_product: string | null;
  server_version: string | null;
  clock: SlowLogClock;
  time_meaning: { event: "end" | "start" | null; text: string };
  range: { from: string | null; to: string | null; applied: boolean };
  filters: { user: string | null; database: string | null };
  order: SlowLogOrder;
  limit: number;
  /** The earliest and latest time in the whole source, whatever the range. */
  span: { first: string | null; last: string | null };
  /** The newest time the source knows (quick ranges end here). */
  anchor: string | null;
  totals: { statements: number; executions: number; total_ms: number; in_range: number; out_of_range: number };
  others: { statements: number; executions: number; total_ms: number } | null;
  users: SlowLogName[];
  databases: SlowLogName[];
  parse: {
    bytes: number; lines: number; entries: number; malformed: number;
    problems: { line: number | null; reason: string }[];
    long_lines: number; truncated_statements: number; admin_commands: number; excluded_own: number;
    no_time: number; inferred_times: number;
  };
  notes: string[];
  statements: SlowLogStatement[];
};
export type SlowLogFile = { file_id: string; name: string; size: number; modified: string | null };
export type SlowLogChoice = SlowLogFile | { cancelled: true };
export type SlowLogProgress = {
  schema_version: "slow-log-progress-1.0";
  phase: "reading" | "done" | "stopped" | "failed" | "unknown";
  bytes_read: number;
  bytes_total: number | null;
  percent: number | null;
  elapsed_s: number | null;
  stop_requested: boolean;
};
/** What a slow log read asks for (both sources). */
export type SlowLogQuery = {
  order: SlowLogOrder;
  limit: number;
  /** "YYYY-MM-DDTHH:MM[:SS]" or "" (no bound), in the source's clock. */
  from: string;
  to: string;
  /** "" for every user. */
  user: string;
};

function body(query: SlowLogQuery) {
  return {
    order: query.order, limit: query.limit,
    from: query.from || null, to: query.to || null, user: query.user || null,
  };
}

/** The native file chooser (any file: slow logs often have no extension). */
export const chooseSlowLog = () => api<SlowLogChoice>("slow-log/choose", {});
/** A typed path, checked the same way. */
export const openSlowLogPath = (path: string) => api<SlowLogFile>("slow-log/open-path", { path });
/** Read the whole file into a report; progress_id lets slowLogProgress and stopSlowLog follow it. */
export const readSlowLog = (file: SlowLogFile, query: SlowLogQuery, progressId: string, signal?: AbortSignal) =>
  api<SlowLogReport>("slow-log/read", { file_id: file.file_id, progress_id: progressId, ...body(query) }, signal);
export const slowLogProgress = (progressId: string, signal?: AbortSignal) =>
  api<SlowLogProgress>("slow-log/progress", { progress_id: progressId }, signal);
export const stopSlowLog = (progressId: string) =>
  api<{ stop_requested: boolean; message: string }>("slow-log/stop", { progress_id: progressId });
/** mysql.slow_log of the connection in use, inside the range. */
export const readSlowLogTable = (connection: Connection, query: SlowLogQuery, signal?: AbortSignal) =>
  api<SlowLogReport>("live/slow-log", { connection, ...body(query) }, signal);

/** A random id for one read's progress (8 to 64 characters of [A-Za-z0-9_-]). */
export function progressId() {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
