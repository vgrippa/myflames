// HTTP client and types for the Readyset place (myflames/readyset.py,
// docs/schemas/readyset-1.0.json). Types only for main.tsx besides small
// navigation helpers, so ReadysetPlace stays in its lazy chunk.
import { api, type Connection } from "./api";
import type { Bound } from "./binding";
import type { ReviewedPlan, ReviewedResult } from "./ActionReview";

export type ReadysetAction = {
  id: string;
  label: string;
  tone: "danger" | "default";
};

export type ReadysetVerdict = "ok" | "watch" | "problem" | "unknown";
export type ReadysetPortRole = "adapter" | "admin" | null;
export type ReadysetSupportVerdict = "yes" | "cached" | "pending" | "skipped" | "unsupported" | "error" | "unknown";

export type ReadysetQueryStats = {
  total_count: number;
  avg_ms: number | null;
  min_ms: number | null;
  max_ms: number | null;
  p50_ms: number | null;
  p90_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  p999_ms: number | null;
  last_executed_s: number | null;
};

export type ReadysetCache = {
  query_id: string;
  name: string;
  cache_type: "shallow" | "deep";
  query: string;
  properties: string;
  database: string | null;
  count: number | null;
  always: boolean | null;
  until_write: boolean | null;
  ttl_ms: number | null;
  refresh_ms: number | null;
  coalesce_ms: number | null;
  adaptive: boolean | null;
  schedule: string | null;
  hits: number | null;
  misses: number | null;
  hit_rate_pct: number | null;
  refreshes: number | null;
  coalesces: number | null;
  wasted_refreshes: number | null;
  dropped_refreshes: number | null;
  over_cap: boolean | null;
  upstream_saved_ms: number | null;
  cached_stats: ReadysetQueryStats | null;
  upstream_stats: ReadysetQueryStats | null;
};

export type ReadysetProxiedQuery = {
  query_id: string;
  query: string;
  count: number | null;
  deep_support: ReadysetSupportVerdict;
  deep_reason: string | null;
  shallow_support: ReadysetSupportVerdict;
  shallow_reason: string | null;
  already_cached: boolean;
  upstream_stats: ReadysetQueryStats | null;
  cached_stats: ReadysetQueryStats | null;
};

export type ReadysetTable = {
  table: string;
  status: string;
  bucket: "snapshotted" | "snapshotting" | "ignored" | "error" | "other";
  description: string | null;
};

export type ReadysetUpstream = {
  host: string;
  port: number;
  address: string;
  database: string | null;
  user: string | null;
  weight: number | null;
  purpose: string;
  active_cdc: boolean;
  health: string | null;
  down_since: string | null;
  last_ok: string | null;
  connections: number | null;
};

export type ReadysetClientConnection = {
  client_addr: string | null;
  username: string | null;
  upstream: string | null;
};

export type ReadysetReplication = {
  status: string;
  cdc_server: string | null;
  max_offset: string | null;
  min_offset: string | null;
  last_error: string | null;
  last_started: string | null;
  last_snapshot: string | null;
  mode: string | null;
  lag_unit: "bytes" | "transactions" | "events" | null;
  consume_lag: number | null;
  persist_lag: number | null;
  upstream_offset: string | null;
  replicator_offset: string | null;
  persisted_offset: string | null;
  heartbeat_staleness_s: number | null;
};

export type ReadysetTelemetry = {
  cached_executions: number;
  upstream_executions: number;
  total_observed_executions: number;
  cached_share_pct: number | null;
  shallow_hits: number;
  shallow_misses: number;
  shallow_hit_rate_pct: number | null;
  shallow_refreshes: number;
  wasted_refreshes: number;
  dropped_refreshes: number;
  coalesces: number;
  upstream_saved_ms: number;
};

export type ReadysetTopologyProxyNode = {
  key: string;
  label: string;
  host: string | null;
  port: number | null;
  address: string;
  version: string | null;
  routing_mode: "cached_and_mirrored" | "shadow_mirror" | "cached_reads" | "backend";
  backend_status: string | null;
  hostgroup_id: number | null;
  querypilot_rule_count: number;
  profile_id: string | null;
  profile_name: string | null;
  verified: boolean;
  evidence: string | null;
};

export type ReadysetTopologyUpstreamNode = {
  key: string;
  host: string;
  port: number;
  address: string;
  database: string | null;
  user: string | null;
  weight: number | null;
  purpose: string;
  is_cdc: boolean;
  is_proxy: boolean;
  active_cdc: boolean;
  health: string | null;
  connections: number | null;
  product: string | null;
  version: string | null;
  engine: string | null;
  read_only: boolean | null;
  cluster_kind: string | null;
  cluster_name: string | null;
  profile_id: string | null;
  profile_name: string | null;
  verified: boolean;
  evidence: string | null;
};

export type ReadysetTopologyPeerNode = {
  key: string;
  relation: "cluster_member" | "replica";
  cluster_kind?: string | null;
  source_profile_id?: string | null;
  name: string | null;
  host: string | null;
  port: number | null;
  address: string | null;
  state?: string | null;
  role?: string | null;
  lag_s?: number | null;
  io_running?: string | null;
  sql_running?: string | null;
  product: string | null;
  version: string | null;
  profile_id: string | null;
  profile_name: string | null;
  verified: boolean;
  evidence: string | null;
};

export type ReadysetTopology = {
  this_server: {
    kind: "readyset";
    port_role: ReadysetPortRole;
    version: string | null;
    address: string | null;
    status: string;
    replication_status: string | null;
    connection_count: number;
  };
  proxysql_nodes: ReadysetTopologyProxyNode[];
  upstreams: ReadysetTopologyUpstreamNode[];
  database_peers: ReadysetTopologyPeerNode[];
  cluster: { kind: string; name: string | null; member_count: number } | null;
  notes: string[];
};

/** Upstream Readyset / ProxySQL tiers discovered when viewing a MySQL/MariaDB server, replica or cluster. */
export type DatabaseTopologyTiers = {
  readyset: Array<{
    key: string;
    label: string;
    host: string | null;
    port: number | null;
    address: string;
    version: string | null;
    status: string;
    replication_status: string | null;
    cache_count: number | null;
    purpose: string;
    profile_id: string | null;
    profile_name: string | null;
    verified: boolean;
    evidence: string | null;
  }>;
  proxysql: Array<{
    key: string;
    label: string;
    host: string | null;
    port: number | null;
    address: string;
    version: string | null;
    routes_to_readyset: boolean;
    querypilot_rule_count: number;
    profile_id: string | null;
    profile_name: string | null;
    verified: boolean;
    evidence: string | null;
  }>;
  live_footprint?: {
    readyset_connected: boolean;
    readyset_programs: string[];
    readyset_heartbeat_table: boolean;
    proxysql_connected: boolean;
  };
  notes: string[];
};

export type ReadysetReport = {
  schema_version: string;
  captured_at: string;
  available: boolean;
  is_readyset: boolean;
  version: string | null;
  port_role: ReadysetPortRole;
  in_readyset_schema: boolean;
  readyset_schema: string;
  message: string | null;
  status: ReadysetVerdict;
  status_why: string[];
  status_fields: {
    database_connection?: string | null;
    connection_count?: number | null;
    status?: string | null;
    replication_status?: string | null;
    cdc_server?: string | null;
    max_replication_offset?: string | null;
    min_replication_offset?: string | null;
    controller_status?: string | null;
    last_started_controller?: string | null;
    last_completed_snapshot?: string | null;
    last_started_replication?: string | null;
    last_replicator_error?: string | null;
    enabled_features?: string | null;
    host_cpus?: number | null;
    host_memory?: string | null;
    host_disk?: string | null;
    host_arch?: string | null;
    host_os?: string | null;
    host_kernel?: string | null;
    host_container?: string | null;
    raw?: Record<string, string | null>;
  };
  capabilities: {
    can_explain_cache: boolean;
    can_create_cache: boolean;
    can_manage_caches: boolean;
    can_alter_readyset: boolean;
    can_shutdown: boolean;
    virtual_tables: boolean;
  };
  replication: ReadysetReplication | null;
  telemetry: ReadysetTelemetry;
  caches: ReadysetCache[];
  proxied_queries: ReadysetProxiedQuery[];
  tables: ReadysetTable[];
  table_counts: {
    total: number;
    snapshotted: number;
    snapshotting: number;
    ignored: number;
    error: number;
    other: number;
  };
  shallow_allowlists: {
    functions: string[];
    variables: string[];
    schemas: string[];
  };
  connections: ReadysetClientConnection[];
  upstreams: ReadysetUpstream[];
  topology: ReadysetTopology | null;
  fingerprints: Record<string, string | null>;
  actions: ReadysetAction[];
  notes: string[];
  statements: string[];
};

export type ReadysetFailure = { key: string; message: string; over: Bound<ReadysetReport> | null };

export type ReadysetExplainOne = {
  cache_type: "shallow" | "deep";
  supported: ReadysetSupportVerdict;
  raw_support: string | null;
  reason: string | null;
  query_id: string | null;
  query: string | null;
  statement: string;
};

export type ReadysetExplainResponse = {
  schema_version: string;
  version: string | null;
  input_kind: "sql" | "query_id";
  target: string;
  shallow: ReadysetExplainOne | null;
  deep: ReadysetExplainOne | null;
  collisions: Array<{ query_id: string; name: string; properties: string }>;
  notes: string[];
};

export type ReadysetExportResponse = {
  schema_version: string;
  version: string | null;
  count: number;
  caches: Array<{ database: string | null; statement: string }>;
  sql: string;
  notes: string[];
};

export const NOT_A_READYSET = "This connection is not a Readyset instance";

export type ReadysetNav = "unread" | "none" | "unknown" | "ok" | "watch" | "problem";
export function readysetNav(bound: Bound<ReadysetReport> | null | undefined, currentKey: string): ReadysetNav {
  if (!bound || bound.key !== currentKey || !bound.report) return "unread";
  const r = bound.report;
  if (!r.available) return r.is_readyset ? "unknown" : "none";
  if (!r.is_readyset) return "none";
  return r.status === "problem" || r.status === "watch" || r.status === "ok" ? r.status : "unknown";
}

export const readReadyset = (connection: Connection, signal?: AbortSignal) =>
  api<ReadysetReport>("live/readyset", { connection }, signal);

export const explainReadysetCache = (
  connection: Connection,
  query_or_id: string,
  cache_type: "both" | "shallow" | "deep" = "both",
  signal?: AbortSignal,
) => api<ReadysetExplainResponse>("live/readyset/explain-cache", { connection, query_or_id, cache_type }, signal);

export const exportReadysetCaches = (connection: Connection, signal?: AbortSignal) =>
  api<ReadysetExportResponse>("live/readyset/export-caches", { connection }, signal);

export const planReadysetAction = (
  connection: Connection,
  action: string,
  target?: Record<string, unknown> | null,
  value?: unknown,
  signal?: AbortSignal,
) => api<ReviewedPlan>("live/readyset/plan", { connection, action, target: target ?? null, ...(value === undefined ? {} : { value }) }, signal);

export const sendReadysetAction = (
  connection: Connection,
  plan: ReviewedPlan,
  review_token: string,
  typed: string,
  progress_id: string,
) => api<ReviewedResult>("live/readyset/action", { connection, plan, review_token, typed, progress_id, confirmed: true });
