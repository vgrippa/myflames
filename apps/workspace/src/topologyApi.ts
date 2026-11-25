// The Topology place's reads: POST /api/live/topology, one section per tab
// (myflames/topology_group.py read_group). A read happens only when a tab
// first comes on screen on a Group connection, and on Refresh; never on
// automatic refresh, never polling. Types follow
// docs/schemas/topology-group-1.0.json.
import { api, type Connection, type ConnectionType } from "./api";

export type TopologySection = "map" | "members" | "parameters" | "accounts" | "plugins";

export type TopologySafety = { type: ConnectionType; read_only: boolean };
export type MemberOutcome = "read" | "failed" | "not_reached";
export type MemberRole = "primary" | "secondary" | "node" | "source" | "replica" | "standalone" | "proxy" | "cache";

export type TopologyLag = {
  kind: "replica_seconds" | "gr_applier_queue" | "galera_recv_queue";
  value: number | null;
  message: string;
};

export type TopologyBehind = {
  kind: "mysql_gtid_transactions" | "mariadb_domain_sequence";
  value: number;
  message: string;
  domains?: Record<string, number>;
};

export type TopologyChannel = {
  name: string;
  source_host: string | null;
  source_port: number | null;
  source_uuid: string | null;
  source_server_id: number | null;
  io_running: string | null;
  sql_running: string | null;
  seconds_behind: number | null;
};

export type TopologyMember = {
  profile_id: string;
  profile_name: string;
  host: string;
  port: number;
  safety: TopologySafety;
  outcome: MemberOutcome;
  message: string;
  elapsed_s: number;
  engine: "mysql" | "mariadb" | null;
  product: string | null;
  version: string | null;
  version_comment: string | null;
  server_uuid: string | null;
  server_id: number | null;
  hostname: string | null;
  read_only: boolean | null;
  read_only_value: string | null;
  super_read_only: boolean | null;
  role: MemberRole | null;
  state: string | null;
  lag: TopologyLag | null;
  behind: TopologyBehind | null;
  behind_reason: string | null;
  channels: TopologyChannel[];
  /** Profile ID of the first read MySQL member with the same server_uuid
   * (for example a router or proxy port of that server); null otherwise. */
  duplicate_of: string | null;
};

export type TopologySummary = {
  read: number;
  failed: number;
  not_reached: number;
  versions: string[];
  text: string;
};

export type DriftStatus = "same" | "different" | "missing" | "per_server";

export type ParameterRow = {
  name: string;
  status: DriftStatus;
  values: Record<string, string | number | boolean | null>;
  apply_type: string | null;
  dynamic: boolean | null;
  reason?: string;
};

export type AccountDifference = {
  profile_id: string;
  kind: "privileges" | "roles" | "attributes";
  detail: string;
};

export type AccountRow = {
  account: string;
  status: "same" | "different" | "missing";
  present_on: string[];
  differences: AccountDifference[];
};

export type PluginRow = {
  name: string;
  kind: "plugin" | "component";
  status: "same" | "different" | "missing";
  values: Record<string, { status: string | null; version: string | null } | null>;
};

export type TopologyNote = { profile_id: string; message: string };

export type NotCompared = { profile_id: string; reason: string };

export type DriftSection<Row> = {
  rows: Row[];
  compared: string[];
  not_compared: NotCompared[];
  notes?: TopologyNote[];
};

export type MapMemberNode = {
  id: string;
  kind: "member";
  profile_id: string;
  label: string;
  host: string;
  port: number;
  engine: "mysql" | "mariadb" | null;
  product: string | null;
  version: string | null;
  role: MemberRole | null;
  state: string | null;
  outcome: MemberOutcome;
  server_uuid: string | null;
  cluster_id: string | null;
  /** Profile ID of the canonical member when this node is an alias (another
   * saved connection to the same server); null otherwise. */
  duplicate_of: string | null;
};

export type MapPeerNode = {
  id: string;
  kind: "peer";
  profile_id: null;
  label: string;
  host: string | null;
  port: number | null;
  server_uuid: string | null;
  seen_by: string[];
  reason: "Not in this group";
};

export type MapNode = MapMemberNode | MapPeerNode;

export type MapEdge = {
  from: string;
  to: string;
  kind: "replication" | "proxy_route" | "cache_upstream" | "alias";
  evidence: "server_uuid" | "server_id" | "endpoint" | "hostname_port" | null;
  verified: boolean;
  state: string | null;
  label: string;
  /** Why a ProxySQL route is in its state; the drawn label stays short and
   * the link list and tooltip carry this instead. */
  reason?: string;
};

export type MapCluster = {
  id: string;
  kind: "group_replication" | "innodb_cluster" | "galera" | "pxc";
  name: string;
  members: string[];
  seen_from: string[];
};

export type TopologyMap = {
  nodes: MapNode[];
  edges: MapEdge[];
  clusters: MapCluster[];
  notes: TopologyNote[];
};

export type TopologyGroupReport = {
  schema_version: "topology-group-1.0";
  section: TopologySection;
  group_name: string;
  taken_at: string;
  elapsed_s: number;
  effective_safety: TopologySafety;
  active_member_profile_id: string;
  primary_profile_id: string | null;
  members: TopologyMember[];
  summary: TopologySummary;
  parameters?: DriftSection<ParameterRow>;
  accounts?: DriftSection<AccountRow>;
  plugins?: DriftSection<PluginRow>;
  map?: TopologyMap;
};

/** One bounded read of the whole group, one section at a time. */
export const readTopology = (connection: Connection, section: TopologySection, signal?: AbortSignal) =>
  api<TopologyGroupReport>("live/topology", { connection, section }, signal);

// ---- Rolling variable change ------------------------------------------------
// One reviewed, rolling variable change over saved group members
// (myflames/topology_change.py). Plan reads each member and returns the exact
// statements with an immutable plan id; apply consumes it, one member at a
// time in the plan's order, stopping at the first failure; stop prevents the
// next member only. Types follow docs/schemas/topology-change-plan-1.0.json
// and docs/schemas/topology-change-1.0.json.

export type RollingChangeMode = "global" | "persist" | "persist_only";
export type RollingValue = string | number | boolean | null;

export type RollingPlanMember = {
  profile_id: string;
  profile_name: string;
  host: string;
  port: number;
  identity: Record<string, string | null> | null;
  status: "planned" | "unchanged" | "refused";
  statement: string | null;
  before: Record<string, unknown> | null;
  /** Only the scopes this member effectively changes (a MariaDB member in a
   * persist plan holds only `global`). Replies that predate the field keep
   * every scope of the plan's mode. */
  after: Record<string, unknown> | null;
  /** The effect as the backend reports it: an object with `now`,
   * `after_restart`, `needs_restart` and a ready `text` line. The UI shows
   * `text` only; older shapes (a string, an array, a plain object) still
   * render, never as raw object keys. */
  effect: unknown;
  /** The statement kind this member runs (a MariaDB member planned from a
   * persist mode is `global`); absent in replies that predate the field, then
   * the plan's mode names every member. */
  effective_mode?: RollingChangeMode;
  caveats: string[];
  privileges: Record<string, unknown> | null;
  config_line: string | null;
  reason: string | null;
};

export type RollingChangePlan = {
  schema_version: "topology-change-plan-1.0";
  plan_id: string;
  change_set_id: string;
  group_name: string;
  name: string;
  value: RollingValue;
  mode: RollingChangeMode;
  order_rule: string;
  effective_safety: TopologySafety;
  confirmation: { required: boolean; typed_name: string | null };
  members: RollingPlanMember[];
  expect: Record<string, unknown>;
};

export type RollingMemberResult = Omit<RollingPlanMember, "status"> & {
  status: "applied" | "unchanged" | "refused" | "not_sent" | "unknown";
  message: string | null;
  audit_id: string | null;
};

export type RollingChangeResult = {
  schema_version: "topology-change-1.0";
  plan_id: string | null;
  change_set_id: string | null;
  group_name: string | null;
  name: string | null;
  value: RollingValue;
  mode: RollingChangeMode | null;
  status: "applied" | "partial" | "refused" | "stopped" | "stale";
  message: string;
  members: RollingMemberResult[];
  audit_ids: string[];
};

/** The apply's progress snapshot: reviewed_writes.Progress plus the member
 * being changed (profile_id, profile_name; message names it). */
export type RollingProgress = {
  phase: string;
  states: string[];
  status: string | null;
  profile_id: string | null;
  profile_name: string | null;
  message: string | null;
};

export type RollingStop = { progress_id: string; status: "finished" | "stopping"; message: string };

/** The rolling endpoints accept only these connection fields (the backend
 * refuses any other, and each member's own saved login is used instead). */
function changeConnection(connection: Connection) {
  const out: Record<string, unknown> = {};
  const keys = ["host", "port", "user", "ssl_mode", "ssl_ca", "ssh_host", "ssh_user", "ssh_port",
    "profile_id", "group_name", "database", "type", "read_only", "name"] as const;
  for (const key of keys) {
    const value = connection[key as keyof Connection];
    if (value !== undefined) out[key] = value;
  }
  return out;
}

/** Review the change on the chosen members: reads only, nothing is sent. */
export const planTopologyChange = (
  connection: Connection, name: string, value: RollingValue, mode: RollingChangeMode, memberIds: string[], signal?: AbortSignal,
) => api<RollingChangePlan>("live/topology/variable/plan",
  { connection: changeConnection(connection), name, value, mode, member_ids: memberIds }, signal);

/** Send the reviewed plan, one member at a time. Never called without the
 * plan's own plan_id and expect. */
export const applyTopologyChange = (
  connection: Connection, plan: RollingChangePlan, confirmed: boolean, typedName: string | undefined, progressId: string,
) => api<RollingChangeResult>("live/topology/variable/apply", {
  connection: changeConnection(connection), plan_id: plan.plan_id, expect: plan.expect, confirmed,
  ...(typedName !== undefined ? { typed_name: typedName } : {}), progress_id: progressId,
});

/** Prevent the next member from starting; a statement already sent is never interrupted. */
export const stopTopologyChange = (connection: Connection, progressId: string) =>
  api<RollingStop>("live/topology/variable/stop", { connection: changeConnection(connection), progress_id: progressId });

/** The send's progress, polled by the sheet while it is open. */
export const rollingChangeProgress = (progressId: string) =>
  api<RollingProgress>("live/action/progress", { progress_id: progressId });
