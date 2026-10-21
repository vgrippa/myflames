// Wire types for replication.py and lifecycle.py. Keep opaque, engine-specific
// facts as JSON; statements, bindings and identities are round-tripped intact.
import { useEffect, useRef } from "react";
import { api, type Connection, type ConnectionType } from "./api";
import { connectionKey } from "./binding";

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Facts = { [key: string]: Json };
export type OpsOperation = "replication_skip" | "replication_skip_loop" | "dump" | "restore" | "clone" | "upgrade_check";
/** TODO(privileges): wire to the place's privilege context when one exists.
 * Values name the missing privilege, e.g. "Requires CLONE_ADMIN". Absence
 * means unknown, never permission granted; Python still gates every write. */
export type MissingPrivileges = Partial<Record<OpsOperation, string>>;
/** Optional policy envelope for integration. Current handlers return gate
 * text in message/error; they do not currently include this object. */
export type OpsPolicy = { text?: string; title?: string; action?: string };
export type ReplicationError = { errno: number; message: string | null; timestamp: string | null; table?: string | null; end_log_pos?: number | null };
export type ReceiverGuard = {
  receiver_guard: "on" | "off";
  receiver_guard_reason: string | null;
  receiver_guard_instrument: string | null;
  receiver_guard_note: string;
};
export type SkipIdentity = {
  channel: string;
  method: string | null;
  last_sql_errno: number;
  last_sql_error: string | null;
  last_sql_error_timestamp: string | null;
  gtid: string | null;
  position: Facts;
  source: Facts;
};
/** The product family: Percona Server and Percona XtraDB Cluster are MySQL builds (engine "mysql"). */
export type Flavor = "mysql" | "percona" | "mariadb";
export type SkipPlan = Partial<ReceiverGuard> & {
  review_token?: string;
  channel: string; engine: string; server_version: string | null;
  available: boolean; message: string | null; method: string | null;
  statements: string[]; identity: SkipIdentity; skip: Facts;
  error: ReplicationError; drift_warning: string; after: string[];
  privileges: string; notes: string[];
};
/** The source server's version and how myflames knows it (replication.attach_source_versions). */
export type SourceVersion = {
  version: string | null; version_comment: string | null; engine: "mysql" | "mariadb" | null;
  /** Percona Server keeps engine "mysql"; a relay log names it by its release suffix. */
  flavor?: Flavor | null; product?: string | null;
  provenance: "saved_connection" | "relay_log" | null; label: string;
  profile_id: string | null; profile_name: string | null; relay_log_file: string | null;
  verified: boolean; caveat: string | null; reason: string | null; hint: string | null; attempts: string[];
};
export type ReplicationChannel = {
  name: string; kind: "replication" | "group_replication"; judged: boolean;
  source: { host: string | null; port: number | null; uuid: string | null; server_id: number | null };
  io: { running: string | null; state: string | null; error: ReplicationError };
  sql: { running: string | null; state: string | null; error: ReplicationError; delay_s: number | null; remaining_delay_s: number | null };
  lag_s: number | null; lag_notes: string[]; lag_signals: Facts;
  position: Facts; worker: Facts; agat: string | null; failing_gtid: string | null;
  relay?: Facts | null; source_version?: SourceVersion | null; status: "ok" | "watch" | "problem" | "unknown";
  status_why: string[]; skip_plan?: SkipPlan;
};
export type ReplicationReport = Partial<ReceiverGuard> & {
  schema_version: "replication-1.0"; engine: string; server_version: string | null;
  flavor?: Flavor | null; server_product?: string | null;
  captured_at: string; uptime_s: number | null; available: boolean;
  message: string | null; replica: boolean; statement: string;
  gtid: Facts; settings: Facts; channels: ReplicationChannel[];
  /** Group Replication's own channels, never listed in channels (they are
   * not asynchronous replication; the Cluster place shows the group). */
  group_replication_channels?: string[];
  /** For a server that is not a replica: where its own replication is shown
   * (a Galera provider loaded, or Group Replication configured), never a
   * verdict on the cluster. */
  cluster_hint?: "galera" | "group_replication" | null;
  capabilities: Facts; notes: string[];
  summary: { channels: number; judged: number; ok: number; watch: number; problem: number; unknown: number; failed: number; lag_max_s: number | null };
};
/** The Replication sidebar item's tooltip once a read says the connection in
 * use has no asynchronous replication channel. */
export const NOT_A_REPLICA = "This connection is not a replica";
type NavTone = "ok" | "watch" | "problem" | "unknown";
/** What the sidebar's Replication item says for the connection in use: nothing
 * before a read of it, muted when it is not a replica (a neutral state, never
 * unknown), unknown only when the status could not be read, otherwise the
 * worst channel and the number of failed ones. */
export function replicationNav(bound: { key: string; report: ReplicationReport } | null | undefined, key: string):
  | { state: "unread" }
  | { state: "none" }
  | { state: "replica"; tone: NavTone; failed: number; text: string } {
  if (!bound || bound.key !== key || !bound.report) return { state: "unread" };
  const report = bound.report;
  if (!report.available) return { state: "replica", tone: "unknown", failed: 0, text: "replication status not readable" };
  if (!report.replica) return { state: "none" };
  const failed = report.summary.failed;
  if (failed > 0) return { state: "replica", tone: "problem", failed, text: `Problem, ${failed} failed` };
  const tones = report.channels.filter((c) => c.judged).map((c) => c.status);
  const tone: NavTone = tones.includes("problem") ? "problem" : tones.includes("watch") ? "watch"
    : tones.includes("unknown") || !tones.length ? "unknown" : "ok";
  return { state: "replica", tone, failed: 0, text: tone === "ok" ? "replicating" : tone === "unknown" ? "lag or state not judged" : `replication ${tone === "problem" ? "problem" : "to watch"}` };
}
export type SkipResult = {
  schema_version: "replication-skip-1.0"; channel: string; status: string;
  message: string | null; statements: string[]; target: SkipIdentity;
  plan: SkipPlan | null; after: Facts | null; verified: Json[];
  policy?: OpsPolicy;
};
export type SkipLoop = Partial<ReceiverGuard> & {
  schema_version: "replication-skip-loop-1.0";
  // Refusal envelopes contain only id, state, channel, message and audit id.
  job_id: string | null; state: "starting" | "running" | "stopping" | "stopped" | "refused" | "needs_confirmation";
  channel: string; message: string; codes?: number[]; max_skips?: number;
  max_duration_s?: number | null; started_at?: string | null; stopped_at?: string | null;
  skips_done?: number; skips?: Facts[]; last_error?: Facts | null;
  stop_reason?: string | null; needs_attention?: boolean | null;
  drift_warning?: string; error?: string | null; policy?: OpsPolicy;
};
export type LoopOptions = { codes: number[]; max_skips: number; max_minutes: number; poll_s: number; receiver_guard?: ReceiverGuard["receiver_guard"] };
export function loopActive(loop: SkipLoop) {
  return loop.state === "starting" || loop.state === "running" || loop.state === "stopping";
}
export const readReplication = (connection: Connection, signal?: AbortSignal) => api<ReplicationReport>("live/replication", { connection }, signal);
export const skipTransaction = (connection: Connection, plan: SkipPlan) => api<SkipResult>("live/replication/skip", {
  connection, channel: plan.channel, expect: plan.identity, statements: plan.statements, review_token: plan.review_token, confirmed: true,
});
export const startSkipLoop = (connection: Connection, channel: string, options: LoopOptions) => api<SkipLoop>("live/replication/skip-loop/start", { connection, channel, ...options, confirmed: true });
export const readSkipLoops = (connection: Connection, signal?: AbortSignal) => api<{ schema_version: string; loops: SkipLoop[] }>("live/replication/skip-loop/status", { connection }, signal);
export const stopSkipLoop = (connection: Connection, job_id: string) => api<SkipLoop>("live/replication/skip-loop/stop", { connection, job_id });

export type ToolUpdate = {
  status: "not_checked" | "available" | "current" | "unknown" | "not_installed" | "deprecated";
  latest_version: string | null; checked_at: string | null; source_url: string; message: string;
};
/** One allowlisted option (dump_options.capability_catalog). group places it in
 * a form section; a boolean's row is the label its checkbox shares. */
export type DumpOption = {key: string; label: string; type: "boolean" | "choice" | "text" | "integer"; default: string | boolean | number | null; choices?: [string,string][]; min?: number; max?: number; hint?: string;
  group?: string; row?: string; placeholder?: string; unit?: string};
export type DumpCapabilities = {options: DumpOption[]; groups?: [string, string][]; encryption: {supported: boolean; summary?: string; message: string}};
/** catalog-1.0 tables of one schema with stats: true (schema_browser.tables_of). */
export type SchemaTable = { name: string; type: "table" | "view" | "system view" | "sequence"; engine?: string | null; rows?: number | null; bytes?: number | null };
export type SchemaTables = { schema_version: "catalog-1.0"; level: "tables"; schema: string; tables: SchemaTable[]; truncated: boolean; limit: number; next_after: string | null; stats?: boolean };
export type DumpOptions = Record<string, string | boolean | number>;
export type Tool = { dump_capabilities?: DumpCapabilities; dump_table_capabilities?: DumpCapabilities; download_url?: string; update?: ToolUpdate; available: boolean; deprecated: boolean; path?: string; version?: string; version_tuple?: number[]; engine?: string; error?: string; name?: string };
export type LifecycleServer = {
  engine: string; version: string; identity?: Facts; variables?: Facts;
  /** mysql, percona or mariadb, and the product name (Percona Server keeps engine "mysql"). */
  flavor?: string; product?: string;
};
export type LifecycleTools = {
  schema_version: "lifecycle-tools-1.0"; tools: Record<string, Tool>;
  server: LifecycleServer;
  offered: { dump: string[]; upgrade: string; clone: boolean; physical_backup: boolean; physical_backup_requires_local_files: boolean };
};
export type Verification = { check: string; status: "passed" | "failed" | "not_checked"; reason: string; [key: string]: Json };
export type UpgradeFinding = {
  id: string; title: string; status?: string; severity: "error" | "warning" | "notice";
  affected_objects: string[]; doc_link: string; description?: string;
  problems?: Json[]; details?: Json;
};
export type UpgradeReport = {
  schema_version: "upgrade-check-1.0"; server: LifecycleServer; target_version: string;
  checks: UpgradeFinding[]; counts: { error: number; warning: number; notice: number };
  manual_checks: Json[]; method: string; partial: boolean; status: string; limitations: string[];
};
export type LifecycleOperation = "dump" | "restore" | "clone";
export type PrepareRequest =
  | { operation: "dump"; directory: string; schemas: string[]; tables: string[]; threads: number; prefer_shell: boolean; options?: DumpOptions }
  | { operation: "restore"; directory: string; schema: string; existing: "refuse" | "ignore" | "drop"; allow_source: boolean; resume: boolean; threads: number }
  | { operation: "clone"; donor: Connection; donor_address: string; data_directory: string; require_ssl: boolean };
export type CloneEndpoint = { endpoint: string; connection_key: string; type: ConnectionType; read_only: boolean };
export type LifecyclePlan = {
  review_token?: string;
  schema_version: "lifecycle-plan-1.0"; operation: LifecycleOperation;
  request: Facts; server: LifecycleServer; connection_key: string; tool: Tool;
  target: string; commands: string[]; checks: Verification[]; warnings: string[];
  ready: boolean; binding: string; donor?: LifecycleServer; policy?: OpsPolicy;
  expect?: { donor: CloneEndpoint; recipient: CloneEndpoint; donor_address: string; require_ssl: boolean };
};
export type CloneProgress = { schema_version: "clone-progress-1.0"; progress: Json[][]; status: Json[][] };
export type LifecycleResult = {
  status: string; verification?: Verification[]; manifest?: Json; directory?: string;
  partial_state?: string; cancel_error?: string; audit_id?: string;
};
export type LifecycleJob = {
  schema_version: "lifecycle-job-1.0"; id: string | null; operation: LifecycleOperation;
  state: "queued" | "running" | "done" | "failed" | "refused" | "cancelled";
  progress: number | null; detail: CloneProgress | null;
  events: { at: string; stream: string; text: string }[];
  result: LifecycleResult | null; error: string; policy?: OpsPolicy;
};
export function jobActive(job: LifecycleJob) { return job.state === "queued" || job.state === "running"; }
export const readLifecycleTools = (connection: Connection, signal?: AbortSignal) => api<LifecycleTools>("lifecycle/tools", { connection, check_updates: true }, signal);
export const checkUpgrade = (connection: Connection, target_version: string, prefer_shell: boolean, signal?: AbortSignal) => api<UpgradeReport>("lifecycle/upgrade-check", { connection, target_version, prefer_shell }, signal);
export const prepareLifecycle = (connection: Connection, request: PrepareRequest, signal?: AbortSignal) => api<LifecyclePlan>("lifecycle/prepare", { connection, ...request }, signal);
export const startLifecycle = (connection: Connection, plan: LifecyclePlan, typed_target: string, donor?: Connection) => api<LifecycleJob>("lifecycle/start", { connection, operation: plan.operation, plan, typed_target, donor, confirmed: true });
export const readLifecycleJob = (connection: Connection, id: string, signal?: AbortSignal) => api<LifecycleJob>("lifecycle/status", { id, connection }, signal);
export const cancelLifecycleJob = (connection: Connection, id: string) => api<LifecycleJob>("lifecycle/cancel", { id, connection });
/** The jobs this myflames holds for the connection's endpoint (no database
 * read): how a reloaded page, or one whose Start reply was lost, finds them. */
export const listLifecycleJobs = (connection: Connection, signal?: AbortSignal) =>
  api<{ schema_version: "lifecycle-jobs-1.0"; jobs: LifecycleJob[] }>("lifecycle/jobs", { connection }, signal);

/** Write replies cannot be cancelled as if that undid the operation. Ignore
 * obsolete replies instead. The revision also rejects switch A → B → A and
 * unmounts, which a comparison of connection keys alone would miss. */
export function useOpsScope(connection: Connection) {
  const key = connectionKey(connection);
  const scope = useRef({ key, revision: 0, mounted: true });
  if (scope.current.key !== key) scope.current = { key, revision: scope.current.revision + 1, mounted: true };
  useEffect(() => {
    scope.current.mounted = true;
    return () => { scope.current.mounted = false; scope.current.revision += 1; };
  }, []);
  return () => {
    const revision = scope.current.revision;
    return () => scope.current.mounted && scope.current.revision === revision && scope.current.key === key;
  };
}
/** Engine-specific data stays readable without guessing its semantics. */
export function factText(value: Json | undefined): string {
  return value === null || value === undefined ? "Not available" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}
/** A server-supplied documentation URL must not become a script URL. */
export function documentationUrl(value: string): string | undefined {
  try { const u = new URL(value); return u.protocol === "https:" || u.protocol === "http:" ? u.href : undefined; }
  catch { return undefined; }
}

/** Only fixed official vendor destinations can become tool download links. */
export function toolDownloadUrl(value?: string): string | undefined {
  try {
    const url = new URL(value || "");
    return url.protocol === "https:" && !url.username && !url.password && !url.port &&
      ["dev.mysql.com", "downloads.mysql.com", "mariadb.org", "mariadb.com"].includes(url.hostname) ? url.href : undefined;
  } catch { return undefined; }
}
export const chooseBackupDirectory = () => api<{path: string | null; cancelled: boolean}>("lifecycle/choose-directory", {});
