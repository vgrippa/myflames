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
  kind: "replication" | "proxy_route" | "cache_upstream";
  evidence: "server_uuid" | "server_id" | "endpoint" | "hostname_port" | null;
  verified: boolean;
  state: string | null;
  label: string;
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
