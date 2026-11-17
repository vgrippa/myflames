// The Cluster place's wire types (cluster-1.0, docs/schemas/cluster-1.0.json,
// myflames/cluster.py) and its one read. Small on purpose: the shell imports
// it to name the place's state in the sidebar without loading the place.
import { api, type Connection, type ConnectionGroup } from "./api";
import type { Bound } from "./binding";
import type { ReviewedPlan, ReviewedResult } from "./ActionReview";
import type { DatabaseTopologyTiers } from "./readysetApi";

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
  /** Reviewed changes offered on this member (the workspace endpoint adds them). */
  actions?: ClusterAction[];
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
  /** mysql, percona or mariadb; Percona XtraDB Cluster is engine "mysql", flavor "percona". */
  flavor?: "mysql" | "percona" | "mariadb";
  /** MySQL, Percona Server, Percona XtraDB Cluster or MariaDB. */
  server_product?: string;
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
  /** InnoDB ClusterSet the cluster belongs to (absent outside one). */
  clusterset?: ClusterSet | null;
  routers: ClusterRouter[];
  findings: ClusterFinding[];
  summary: { members: number; ok: number; watch: number; problem: number; unknown: number };
  statements: string[];
  notes: string[];
  readyset_hint?: boolean;
  topology_tiers?: DatabaseTopologyTiers | null;
  connection_group?: ConnectionGroup | null;
  /** Cluster-wide changes the overview offers (the workspace endpoint adds
   * them; cluster.py overview_actions): mode switches and, in a ClusterSet,
   * the ClusterSet changes, each enabled or with its reason and fix. */
  actions?: ClusterAction[];
};

/** An instance of another cluster of the ClusterSet, from the shared
 * metadata: who it is, not its state (read on it). */
export type ClusterSetInstance = {
  label: string | null;
  address: string | null;
  server_uuid: string | null;
  instance_type: string | null;
  /** The instance this replica cluster's clusterset_replication channel connects to. */
  source: boolean;
};

export type ClusterSetCluster = {
  name: string | null;
  role: "PRIMARY" | "REPLICA" | string | null;
  invalidated: boolean;
  local: boolean;
  /** Empty for this cluster: its members are the report's members. */
  instances: ClusterSetInstance[];
};

export type ClusterSet = {
  name: string | null;
  local_cluster: string | null;
  local_role: string | null;
  invalidated: boolean;
  primary_cluster: string | null;
  clusters: ClusterSetCluster[];
  /** The clusterset_replication channel, read on a replica cluster's primary only. */
  channel: { receiver_state?: string; applier_state?: string; last_error_number?: number;
    last_error_message?: string; source?: string } | null;
  /** Read from a replica cluster's replicated copy of the metadata. */
  from_local_copy: boolean;
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
  if (code === "1045" && /access denied/i.test(raw)) return "The server refused this account's user name or password.";
  if (/timed out/i.test(raw)) return "The read timed out: the server did not answer in time.";
  const line = raw.split("\n")[0];
  return line.length > 180 ? `${line.slice(0, 177)}…` : line || "The read failed.";
}

/** A reviewed change the cluster offers (docs/schemas/cluster-1.0.json
 * #/$defs/action, cluster-action-1.1; cluster.py _evaluate_action). Every
 * action is listed, enabled or not; a disabled one keeps its reason
 * (why_not), the way to make it runnable (fix) and, when a privilege is
 * missing, the GRANT to run. */
export type ClusterAction = {
  schema_version: "cluster-action-1.1";
  /** A fixed id, or set_primary_cluster[:<cluster>] / rejoin_cluster[:<cluster>]. */
  id: string;
  label: string;
  /** overview: the Cluster overview band; member: that member's page. */
  scope: "overview" | "member";
  /** dbops classification; the cluster changes are risky. */
  kind: "write" | "risky";
  /** sql on the target member, a Group Replication UDF, or MySQL Shell. */
  via: "sql" | "udf" | "mysqlsh";
  danger: boolean;
  description: string;
  member_key: string | null;
  member_name: string | null;
  member_uuid: string | null;
  cluster_name: string | null;
  enabled: boolean;
  why_not: string | null;
  fix: string | null;
  /** [privilege, scope] pairs the account lacks (filled by the backend when it can read them). */
  missing_privileges: string[][];
  grant_sql: string | null;
  privileges_unreadable: boolean;
  /** Where the change runs. In a Group connection, profile_id is the
   * backend's identity-confirmed saved route, or null when none is usable. */
  target: { self: boolean; member_name: string | null; profile_id?: string | null };
};

/** The member-scope actions that change only the server they run on
 * (cluster.py _LOCAL_ACTIONS): the identity line names them. */
export const LOCAL_ACTION_IDS = new Set(["stop_member", "start_member", "desync_node", "resync_node", "bootstrap_pc"]);

/** The identity sentence under the Cluster summary: the server read here,
 * then either the Group connection's saved routes or the connected server's
 * local actions. A missing report field drops its part of the sentence. */
export function identityText(report: ClusterReport, groupName = ""): string {
  const route = groupName ? `Each member's actions run through its own saved connection in ${groupName}.` : "";
  const me = report.members.find((m) => m.local);
  if (!me) return route;
  const server = report.server || {};
  const address = me.host ? (me.port ? `${me.host.includes(":") ? `[${me.host}]` : me.host}:${me.port}` : me.host)
    : me.address || (server.hostname ? `${server.hostname}${server.port ? `:${server.port}` : ""}` : "");
  let text = `You are connected to ${me.name}${address && address !== me.name ? ` (${address})` : ""}`;
  const detail = [me.role ? `a ${me.role}` : "", me.state || ""].filter(Boolean).join(", ");
  text += `${detail ? `, ${detail}` : ""}.`;
  if (route) return `${text} ${route}`;
  const verbs = (me.actions || []).filter((a) => LOCAL_ACTION_IDS.has(a.id))
    .map((a) => a.label.replace(/[….]+$/, "").split(" ")[0]).filter(Boolean);
  if (verbs.length) {
    const list = verbs.length === 1 ? verbs[0] : `${verbs.slice(0, -1).join(", ")} and ${verbs[verbs.length - 1]}`;
    text += ` ${list} act${verbs.length === 1 ? "s" : ""} on this server only.`;
  }
  return text;
}

export const planClusterAction = (connection: Connection, action: string, memberKey: string | null, targetProfileId: string | null = null) =>
  api<ReviewedPlan>("live/cluster/plan", { connection, action, member_key: memberKey,
    ...(connection.group_name ? { target_profile_id: targetProfileId } : {}) });

export const sendClusterAction = (connection: Connection, plan: ReviewedPlan, token: string, typed: string, progressId: string, targetProfileId: string | null = null) =>
  api<ReviewedResult>("live/cluster/action", { connection, plan, review_token: token, typed, confirmed: true, progress_id: progressId,
    ...(connection.group_name ? { target_profile_id: targetProfileId } : {}) });
