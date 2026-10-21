// The ProxySQL place's wire types (proxysql-1.0 and proxysql-action-1.0,
// myflames/proxysql.py; proxysql-digests-1.0, myflames/proxysql_digests.py;
// docs/schemas/proxysql-1.0.json and proxysql-digests-1.0.json) and its API calls.
import { api, type Connection } from "./api";
import type { Bound } from "./binding";
import type { ReviewedPlan, ReviewedResult } from "./ActionReview";

export type ProxySqlAction = {
  id: string;
  label: string;
  danger: boolean;
  description: string;
  value?: { label: string; min: number; max: number; current: number | null };
};
export type ProxySqlStatus = "ONLINE" | "SHUNNED" | "OFFLINE_SOFT" | "OFFLINE_HARD" | string;
export type ProxySqlConfigRow = {
  hostgroup_id: number; hostname: string; port: number; status: ProxySqlStatus | null; weight: number | null;
  max_connections: number | null; max_replication_lag: number | null; use_ssl: number | null;
  max_latency_ms: number | null; compression: number | null; gtid_port: number | null; comment: string;
};
export type ProxySqlPool = {
  conn_used?: number | null; conn_free?: number | null; conn_ok?: number | null; conn_err?: number | null;
  max_conn_used?: number | null; queries?: number | null; bytes_sent?: number | null; bytes_recv?: number | null;
  latency_us?: number | null; status?: string | null;
};
export type ProxySqlServer = {
  key: string;
  hostgroup_id: number;
  hostname: string;
  port: number;
  /** What ProxySQL runs (the pool's word); null when only configured. */
  runtime_status: ProxySqlStatus | null;
  status_detail: string | null;
  settings: Record<string, number | string | null>;
  pool: ProxySqlPool | null;
  config: ProxySqlConfigRow | null;
  config_key: [number, string, number] | null;
  config_note: string | null;
  monitor: "galera" | "group_replication" | "replication" | "aws_aurora" | null;
  role: string | null;
  tone: "ok" | "watch" | "problem" | "unknown";
  why: string[];
  actions: ProxySqlAction[];
  action_notes?: string[];
};
export type ProxySqlHostgroup = {
  id: number;
  monitor: ProxySqlServer["monitor"];
  role: string | null;
  servers: number;
  status: Record<"ONLINE" | "SHUNNED" | "OFFLINE_SOFT" | "OFFLINE_HARD", number>;
  standby: number;
  queries: number;
  conn_used: number;
  conn_free: number;
  conn_err: number;
  tone: "ok" | "watch" | "problem" | "unknown";
  why: string[];
};
export type ProxySqlMonitor = {
  kind: NonNullable<ProxySqlServer["monitor"]>;
  source: "runtime" | "memory";
  active: boolean;
  writer_hostgroup: number | null;
  backup_writer_hostgroup?: number | null;
  reader_hostgroup: number | null;
  offline_hostgroup?: number | null;
  max_writers: number | null;
  writer_is_also_reader: number | null;
  max_transactions_behind: number | null;
  comment: string;
};
export type ProxySqlQueryRule = {
  rule_id: number;
  active: boolean;
  username: string | null;
  schemaname: string | null;
  match_digest: string | null;
  match_pattern: string | null;
  negate_match_pattern: boolean;
  destination_hostgroup: number | null;
  apply: boolean;
  comment: string;
  hits: number | null;
  runtime_active: boolean | null;
  fields: Record<string, string | null>;
  actions: ProxySqlAction[];
  /** Keys of the report's digests this rule applies to, evaluated here (null: not evaluated). */
  digests?: string[] | null;
  digest_count?: number | null;
  /** Digests that ran on its destination while a condition could not be tested here. */
  possible_digests?: string[] | null;
};
/** Where the active rules send a digest, evaluated here (proxysql_digests.Routing). */
export type ProxySqlRouting = {
  observed: number | null;
  expected: number | null;
  source: "rule" | "fast_routing" | "default" | null;
  rules: number[];
  decisive: number | null;
  approximate: boolean;
  possible: number[];
  unknown_rules: { rule_id: number; why: string }[];
  default_hostgroup: number | null;
  verdict: "match" | "mismatch" | "possible" | "local" | "cache" | "unknown";
  text: string;
  why: string[];
};
export type ProxySqlRoutingContext = {
  evaluated: boolean;
  regex_engine: "PCRE" | "RE2" | null;
  regex_variable?: string | null;
  iterations: number;
  fast_routing_read?: boolean;
  users_read?: boolean;
  track_hostname?: boolean;
  active_rules?: number;
  note: string | null;
};
export type ProxySqlDigest = {
  hostgroup: number | null;
  schemaname: string | null;
  username: string | null;
  client_address: string | null;
  digest: string | null;
  digest_text: string;
  count_star: number | null;
  sum_time_us: number | null;
  min_time_us: number | null;
  max_time_us: number | null;
  avg_time_us: number | null;
  first_seen: number | null;
  last_seen: number | null;
  rows_sent: number | null;
  rows_affected: number | null;
  key: string;
  seen_span_s?: number | null;
  /** Executions per second between the first and the last execution (at least one second). */
  rate_qps?: number | null;
  routing: ProxySqlRouting;
};
export type ProxySqlPending = { runtime: string[] | null; disk: string[] | null };
export type ProxySqlReport = {
  schema_version: "proxysql-1.0";
  captured_at: string;
  available: boolean;
  is_proxysql: boolean;
  version: string | null;
  uptime_s: number | null;
  message: string | null;
  status: "ok" | "watch" | "problem" | "unknown";
  status_why: string[];
  config_readable: boolean;
  servers: ProxySqlServer[];
  hostgroups: ProxySqlHostgroup[];
  monitors: ProxySqlMonitor[];
  query_rules: ProxySqlQueryRule[];
  query_digests: ProxySqlDigest[];
  pending: { servers: ProxySqlPending; query_rules: ProxySqlPending };
  metrics: Record<string, number | null>;
  summary: { backends: number; ONLINE: number; SHUNNED: number; OFFLINE_SOFT: number; OFFLINE_HARD: number; not_in_runtime: number };
  actions: ProxySqlAction[];
  notes: string[];
  statements: string[];
  routing?: ProxySqlRoutingContext | null;
};
export type DigestOrder = "slowest" | "time" | "frequent" | "rate" | "rows";
export type DigestFilters = {
  order: DigestOrder;
  limit: 10 | 25 | 50 | 100;
  hostgroup: number | null;
  schema: string | null;
  user: string | null;
  text: string | null;
  digest: string | null;
  rule_id: number | null;
};
export type DigestFacet = { value: number | string | null; digests: number; executions: number };
/** The Query digests tab (proxysql-digests-1.0). */
export type ProxySqlDigestsReport = {
  schema_version: "proxysql-digests-1.0";
  captured_at: string;
  available: boolean;
  is_proxysql: boolean;
  version: string | null;
  message: string | null;
  filters: DigestFilters;
  orders: { id: DigestOrder; label: string; available: boolean; why: string | null }[];
  limits: number[];
  digests: ProxySqlDigest[];
  more: boolean;
  scanned?: number | null;
  facets: { hostgroups: DigestFacet[]; schemas: DigestFacet[]; users: DigestFacet[]; complete: boolean } | null;
  totals: { digests: number; executions: number; sum_time_us: number } | null;
  window: { first_seen: number | null; last_seen: number | null } | null;
  rules: { rule_id: number; destination_hostgroup: number | null; apply: boolean; match_digest: string | null; match_pattern: string | null; comment: string }[];
  rule_hits: Record<string, number | null> | null;
  routing: ProxySqlRoutingContext | null;
  notes: string[];
  statements: string[];
};
export type ProxySqlFailure = { key: string; message: string; over: Bound<ProxySqlReport> | null };
/** The target of a change: a configuration row, a query rule, or none (Load and Save). */
export type ProxySqlTarget = { config: [number, string, number] } | { rule_id: number } | null;

export const NOT_A_PROXYSQL = "This connection is not a ProxySQL Admin interface";

export const readProxySql = (connection: Connection, signal?: AbortSignal) =>
  api<ProxySqlReport>("live/proxysql", { connection }, signal);

export const planProxySqlAction = (connection: Connection, action: string, target: ProxySqlTarget, value?: number) =>
  api<ReviewedPlan>("live/proxysql/plan", { connection, action, target, ...(value === undefined ? {} : { value }) });

export const sendProxySqlAction = (connection: Connection, plan: ReviewedPlan, token: string, typed: string, progressId: string) =>
  api<ReviewedResult>("live/proxysql/action", { connection, plan, review_token: token, typed, confirmed: true, progress_id: progressId });

/** One bounded read of the Query digests tab (ORDER BY and LIMIT run in Admin). */
export const readProxySqlDigests = (connection: Connection, filters: Partial<DigestFilters>, signal?: AbortSignal) =>
  api<ProxySqlDigestsReport>("live/proxysql/digests", { connection, ...filters }, signal);

/** The sidebar's state for ProxySQL: unread until the connection in use is
 * read, "none" when that read said it is not ProxySQL Admin. */
export function proxySqlNav(bound: Bound<ProxySqlReport> | null, key: string): "unread" | "none" | "proxysql" {
  if (!bound || bound.key !== key || !bound.report) return "unread";
  return bound.report.is_proxysql ? "proxysql" : "none";
}
