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
  state_source: "wsrep_local_state_comment" | "pxc_cluster_view" | "replication_group_members" | "metadata" | "replication_channel" | null;
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
  /** The check-in as UTC (ISO-8601 with Z). */
  last_check_in_utc?: string | null;
  /** Read from the metadata copy of a member that left the group. */
  from_local_copy?: boolean;
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
    wsrep_on?: string | null;
    members_source: "wsrep_cluster_members" | "wsrep_incoming_addresses" | null;
    address_nodes?: number | null;
    address_names_this_node?: boolean | null;
    /** wsrep_cluster_address entries not in the component, when they can be named. */
    missing?: string[];
    /** Required wsrep status not reported (nothing judged then). */
    incomplete?: string[];
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
    /** "left_group": this member's frozen copy; "read_replica": a read replica's replicated copy. */
    local_copy?: "left_group" | "read_replica" | null;
  } | null;
  routers: ClusterRouter[];
  findings: ClusterFinding[];
  summary: { members: number; ok: number; watch: number; problem: number; unknown: number };
  statements: string[];
  notes: string[];
};

/** A read of the connection *key* that failed, and the report on screen
 * when it did (kept, greyed, as the last report until a read succeeds). */
export type ClusterFailure = { key: string; message: string; over: Bound<ClusterReport> | null };

export const readCluster = (connection: Connection, signal?: AbortSignal) =>
  api<ClusterReport>("live/cluster", { connection }, signal);

export const KIND_LABEL: Record<ClusterKind, string> = {
  galera: "MariaDB Galera Cluster",
  pxc: "Percona XtraDB Cluster",
  innodb_cluster: "MySQL InnoDB Cluster",
  group_replication: "MySQL Group Replication",
  none: "Not part of a cluster",
};

/** The product name (cluster.kind_label): Galera on MySQL that is not
 * Percona XtraDB Cluster is Codership's Galera Cluster for MySQL. */
export function kindLabel(report: Pick<ClusterReport, "kind" | "engine">): string {
  return report.kind === "galera" && report.engine === "mysql" ? "Galera Cluster for MySQL" : KIND_LABEL[report.kind];
}

export const isGalera = (report: Pick<ClusterReport, "kind">) => report.kind === "galera" || report.kind === "pxc";
/** "Node" on Galera and Percona XtraDB Cluster, "Member" on Group Replication and InnoDB Cluster. */
export const memberNoun = (report: Pick<ClusterReport, "kind">) => (isGalera(report) ? "Node" : "Member");

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

export const isReadReplica = (m: ClusterMember) => m.instance_type === "read-replica" || m.role === "READ_REPLICA";

/** The connected member left the group (OFFLINE or ERROR) and cannot see it. */
export function blindMember(report: ClusterReport): ClusterMember | null {
  const me = report.members.find((m) => m.local);
  return me && !isGalera(report) && (me.state === "OFFLINE" || me.state === "ERROR") ? me : null;
}

/** The word a member's state reads as. Galera and PXC states arrive in the
 * node's own words (Synced, Donor/Desynced, Joiner); Group Replication states
 * stay upper case as MySQL Shell prints them. A state this server cannot
 * read says so, never a guess. */
export function stateWord(member: ClusterMember): string {
  const state = member.state;
  if (state) return state;
  if (isReadReplica(member)) return "Read replica";
  if (member.state_source === "metadata") return "Not visible here";
  return "In the component";
}

/** A member that is not taking part: drawn apart (dashed), not by color alone. */
export function memberDetached(member: ClusterMember): boolean {
  return ["UNREACHABLE", "MISSING", "OFFLINE", "ERROR"].includes((member.state || "").toUpperCase())
    || member.status === "problem";
}

/** wsrep_flow_control_paused as one percent text (cluster.flow_percent):
 * one decimal, cut rather than rounded up, "<0.1%" for a small non-zero value. */
export function flowPercent(value: unknown): string {
  if (typeof value !== "number") return "Unknown";
  const pct = value * 100;
  if (pct <= 0) return "0%";
  if (pct < 0.1) return "<0.1%";
  const tenths = Math.floor(Math.round(pct * 10 * 1e6) / 1e6);
  return `${tenths % 10 ? (tenths / 10).toFixed(1) : String(tenths / 10)}%`;
}

/** "4 s ago", "3 min ago", "2 h ago", "5 days ago" (cluster.age_text). */
export function ageText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Math.trunc(seconds));
  if (s < 120) return `${s} s ago`;
  if (s < 7200) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

/** "2026-10-20 15:47:23 UTC" from the ISO time. */
export function utcText(iso: string | null | undefined): string {
  return iso ? `${iso.replace("T", " ").replace(/Z$/, "").replace(/\.\d+$/, "")} UTC` : "";
}

/** A router's check-in as every surface shows it: its age and the UTC time
 * (cluster.check_in_text). */
export function checkInText(r: ClusterRouter): string {
  if (!r.last_check_in) return "Not recorded";
  const age = ageText(r.last_check_in_age_s);
  const when = r.last_check_in_utc ? utcText(r.last_check_in_utc) : `${r.last_check_in} server time`;
  return age ? `${age} (${when})` : when;
}

/** A failed read as one short sentence, without the client's wrapper. */
export function failureText(error: string): string {
  const raw = (error || "").replace(/^mysql client exited \d+:\s*/, "").trim();
  const code = raw.match(/ERROR (\d+)/)?.[1];
  const where = raw.match(/on '([^']+)'/)?.[1];
  const target = where ? ` ${where}` : " the server";
  if (code === "2003" || code === "2002") return `Could not connect to${target}: it refused the connection or did not answer. The server may be down.`;
  if (code === "2013") return `Lost the connection to${target} while reading: the server may be paused, restarting or overloaded.`;
  if (code === "2005") return `The host${target} could not be resolved.`;
  if (code === "1045") return "The server refused this account's user name or password.";
  if (/timed out/i.test(raw)) return "The read timed out: the server did not answer in time.";
  const line = raw.split("\n")[0];
  return line.length > 180 ? `${line.slice(0, 177)}…` : line || "The read failed.";
}
