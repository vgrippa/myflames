// The Cluster place's wire types (cluster-1.0, docs/schemas/cluster-1.0.json,
// myflames/cluster.py) and its one read. Small on purpose: the shell imports
// it to name the place's state in the sidebar without loading the place.
import { api, type Connection } from "./api";
import type { Bound } from "./binding";

export type ClusterStatus = "ok" | "watch" | "problem" | "unknown";
export type ClusterKind = "galera" | "pxc" | "innodb_cluster" | "group_replication" | "none";

export type ClusterMember = {
  key: string;
  name: string;
  address: string | null;
  host: string | null;
  port: number | null;
  uuid: string | null;
  state: string | null;
  state_source: "wsrep_local_state_comment" | "pxc_cluster_view" | "replication_group_members" | "metadata" | null;
  role: string | null;
  version: string | null;
  local: boolean;
  status: ClusterStatus;
  status_why: string[];
  metrics: Record<string, number | string | boolean | null>;
  label?: string | null;
  endpoint?: string | null;
  in_metadata?: boolean;
  instance_type?: string | null;
  communication_stack?: string | null;
  pxc_status?: string | null;
  segment?: number | null;
};

export type ClusterRouter = {
  id: number | null;
  name: string | null;
  product: string | null;
  address: string | null;
  version: string | null;
  last_check_in: string | null;
  last_check_in_age_s: number | null;
  ports: Partial<Record<"RWEndpoint" | "ROEndpoint" | "RWSplitEndpoint" | "RWXEndpoint" | "ROXEndpoint", string>>;
};

export type ClusterFinding = { id: string; status: "watch" | "problem"; title: string; text: string; member?: string };

export type ClusterReport = {
  schema_version: "cluster-1.0";
  engine: "mysql" | "mariadb";
  server_version: string | null;
  version_comment: string | null;
  captured_at: string;
  uptime_s: number | null;
  performance_schema: boolean;
  available: boolean;
  message: string | null;
  kind: ClusterKind;
  server: { hostname: string | null; port: number | null; report_host: string | null; server_uuid: string | null };
  cluster: {
    name: string | null;
    uuid: string | null;
    size: number | null;
    expected_size: number | null;
    expected_source: "wsrep_cluster_address" | "mysql_innodb_cluster_metadata" | null;
    component: string | null;
    quorum: boolean | null;
    failures_tolerated: number | null;
    topology: "single_primary" | "multi_primary" | null;
    provider: { name: string | null; version: string | null; vendor: string | null } | null;
  };
  status: ClusterStatus;
  status_why: string[];
  local: string | null;
  members: ClusterMember[];
  galera: {
    node_name: string | null;
    node_address: string | null;
    cluster_address: string | null;
    incoming_addresses: string[];
    gcomm_uuid: string | null;
    desync: string | null;
    pxc_strict_mode: string | null;
    pxc_maint_mode: string | null;
    members_source: "wsrep_cluster_members" | "wsrep_incoming_addresses" | null;
    metrics: Record<string, number | string | boolean | null>;
  } | null;
  group_replication: {
    group_name: string | null;
    single_primary_mode: string | null;
    local_address: string | null;
    group_seeds: string | null;
    view_id: string | null;
  } | null;
  metadata: {
    available: boolean;
    schema_version: string | null;
    cluster_id: string | null;
    cluster_name: string | null;
    primary_mode: "pm" | "mm" | null;
    instances: { label: string | null; server_uuid: string | null; address: string | null; endpoint: string | null; instance_type?: string | null }[];
    reason: string | null;
  } | null;
  routers: ClusterRouter[];
  findings: ClusterFinding[];
  summary: { members: number; ok: number; watch: number; problem: number; unknown: number };
  statements: string[];
  notes: string[];
};

export const readCluster = (connection: Connection, signal?: AbortSignal) =>
  api<ClusterReport>("live/cluster", { connection }, signal);

export const KIND_LABEL: Record<ClusterKind, string> = {
  galera: "MariaDB Galera Cluster",
  pxc: "Percona XtraDB Cluster",
  innodb_cluster: "MySQL InnoDB Cluster",
  group_replication: "MySQL Group Replication",
  none: "Not part of a cluster",
};

/** The sentence the sidebar item carries when the connection in use is known
 * not to be part of a cluster. */
export const NOT_A_CLUSTER = "This connection is not part of a cluster";

/** What the sidebar shows for the Cluster place, from a report of the
 * connection in use only (`key`): nothing before the first read, a muted item
 * when it is not part of a cluster, else the cluster's status. */
export function clusterNav(bound: Bound<ClusterReport> | null, key: string):
  | { state: "unread" }
  | { state: "none" }
  | { state: "cluster"; status: ClusterStatus; problems: number; text: string } {
  if (!bound || bound.key !== key || !bound.report) return { state: "unread" };
  const report = bound.report;
  if (report.available && report.kind === "none") return { state: "none" };
  if (!report.available) return { state: "cluster", status: "unknown", problems: 0, text: "cluster status not readable" };
  const problems = report.summary.problem;
  const text = report.status === "ok" ? "cluster OK" : report.status === "unknown" ? "cluster not judged"
    : `cluster ${report.status === "problem" ? "problem" : "to watch"}${problems ? `, ${problems} ${problems === 1 ? "member" : "members"} with a problem` : ""}`;
  return { state: "cluster", status: report.status, problems, text };
}

/** Galera and PXC report node states in their own case (Synced, SYNCED,
 * Donor/Desynced); Group Replication states stay upper case as MySQL Shell
 * prints them. */
export function stateWord(member: ClusterMember): string {
  const state = member.state;
  if (!state) {
    if (member.instance_type === "read-replica") return "Read replica";
    return member.state_source === "metadata" ? "Not visible here" : "Not judged here";
  }
  if (member.state_source === "pxc_cluster_view") return state.charAt(0) + state.slice(1).toLowerCase();
  return state;
}

/** A member that is not taking part: drawn apart (dashed), not by color alone. */
export function memberDetached(member: ClusterMember): boolean {
  return ["UNREACHABLE", "MISSING", "OFFLINE", "ERROR"].includes((member.state || "").toUpperCase())
    || member.status === "problem";
}
