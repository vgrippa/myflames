// Multi-tier topology diagram for the ProxySQL place, on the shared tier
// module (TopologyTiers): ProxySQL Admin (this connection) -> [Readyset cache
// & mirror tier] -> [Hostgroups & MySQL/MariaDB backends]. States come only
// from reported server states (S11): a link runs on a reported ONLINE runtime
// row, a reported OFFLINE_HARD breaks it, and a config-only row, the UNKNOWN
// placeholder or an unreported status reads "Not read", never OK.
import { Pill, type Tone } from "./kit";
import { TierCard, TierLink, TierNode, TierRow, type TierLinkState, type TierStem, type TierUse } from "./TopologyTiers";
import type {
  ProxySqlMonitor,
  ProxySqlReport,
  ProxySqlReadysetNode,
  ProxySqlTopologyBackendNode,
} from "./proxysqlApi";

const RS_MODE_LABEL: Record<string, string> = {
  cached_and_mirrored: "Cached reads + QueryPilot mirror",
  shadow_mirror: "Shadow mirror (QueryPilot)",
  cached_reads: "Cached reads",
  hostgroup_member: "Hostgroup member",
};

type StatusNode = {
  status: string | null | undefined;
  status_source?: "runtime" | "config" | null;
  hostgroup_id?: number | null;
  host?: string;
  port?: number;
  hostgroup_roles?: string[];
};

/** Only a runtime row is a reported state: a config-only row and the UNKNOWN
 * placeholder are "Not read", never ONLINE (proxysql.py _server_tones). */
function isReportedStatus(status: string | null | undefined, statusSource: string | null | undefined): boolean {
  const s = (status || "").toUpperCase();
  return !!s && s !== "UNKNOWN" && statusSource !== "config";
}

/** The status word to show on a card, or null for "Not read". */
function statusWord(status: string | null | undefined, statusSource: string | null | undefined): string | null {
  return isReportedStatus(status, statusSource) ? (status as string) : null;
}

/** Whether ProxySQL serves traffic through the node. The status word follows
 * the shared backend rule (proxySqlBackendState, mirroring _server_tones in
 * myflames/proxysql.py); the tier's own addition is the monitor's offline
 * hostgroup: ONLINE there is out of traffic, and a standby writer ONLINE in
 * the backup hostgroup is its own row with its own ONLINE state, never a
 * reason to call the SHUNNED row running. */
export function servingState(node: StatusNode): "running" | "stopped" | "error" | "unread" {
  if (!isReportedStatus(node.status, node.status_source)) return "unread";
  const state = proxySqlBackendState(node.status).state;
  return state === "running" && (node.hostgroup_roles || []).includes("offline") ? "stopped" : state;
}

/** The honest ProxySQL backend-status rule, shared with the Readyset
 * topology's ProxySQL tier: null and the UNKNOWN placeholder are not read
 * (word null, never shown), ONLINE runs, OFFLINE_HARD breaks, and every
 * SHUNNED variant (SHUNNED_REPLICATION_LAG included) is a reported stop with
 * the watch tone. A reported word outside this list stays unread but shows. */
export type ProxySqlBackendState = {
  state: "running" | "stopped" | "error" | "unread";
  tone: Tone;
  word: string | null;
};

export function proxySqlBackendState(status: string | null | undefined): ProxySqlBackendState {
  const s = (status || "").toUpperCase();
  if (!s || s === "UNKNOWN") return { state: "unread", tone: "unknown", word: null };
  if (s === "ONLINE") return { state: "running", tone: "ok", word: status as string };
  if (s === "OFFLINE_HARD") return { state: "error", tone: "problem", word: status as string };
  if (s.startsWith("SHUNNED") || s === "OFFLINE_SOFT") return { state: "stopped", tone: "watch", word: status as string };
  return { state: "unread", tone: "unknown", word: status as string };
}

/** True when the same address is ONLINE in the monitor's backup writer
 * hostgroup: standby health to describe on a SHUNNED writer's card, never a
 * claim that the shunned route serves traffic. */
function standbyOnline(node: StatusNode, pool: StatusNode[], monitors: ProxySqlMonitor[]): boolean {
  return monitors.some(
    (m) =>
      m.active &&
      m.writer_hostgroup === node.hostgroup_id &&
      m.backup_writer_hostgroup !== null &&
      m.backup_writer_hostgroup !== undefined &&
      pool.some(
        (d) =>
          d !== node &&
          d.hostgroup_id === m.backup_writer_hostgroup &&
          d.host === node.host &&
          d.port === node.port &&
          (d.status || "").toUpperCase() === "ONLINE" &&
          d.status_source !== "config",
      ),
  );
}

function serverStatusTone(serving: string): Tone {
  return serving === "running" ? "ok" : serving === "error" ? "problem" : serving === "stopped" ? "watch" : "unknown";
}

function clusterBadge(kind: string | null | undefined, name: string | null | undefined): string | null {
  if (!kind || kind === "none") return null;
  const label =
    kind === "innodb_cluster"
      ? "InnoDB Cluster"
      : kind === "group_replication"
        ? "Group Replication"
        : kind === "pxc"
          ? "PXC"
          : kind === "galera"
            ? "Galera"
            : kind;
  return name ? `${label} (${name})` : label;
}

/** A tier link runs on a node that serves traffic, breaks on a reported
 * OFFLINE_HARD, dashes on a reported out-of-traffic state, and is unread when
 * no node reported a runtime status. */
function backendLinkState(states: ("running" | "stopped" | "error" | "unread")[]): TierLinkState {
  if (!states.length || states.every((s) => s === "unread")) return "unread";
  if (states.some((s) => s === "running")) return "running";
  if (states.some((s) => s === "error")) return "error";
  if (states.some((s) => s === "stopped")) return "stopped";
  return "unread";
}

function stemOf(tone: Tone): TierStem {
  return tone === "ok" ? "active" : tone === "problem" ? "broken" : "plain";
}

function useOf(node: { profile_id: string | null; profile_name: string | null }, fallback: string): TierUse | null {
  return node.profile_id ? { id: node.profile_id, name: node.profile_name || fallback } : null;
}

export function ProxySqlTopologyDiagram({
  report,
  current = true,
  selectedServerKey,
  onSelectServer,
  onSwitchConnection,
  onOpenReadyset,
  onOpenCluster,
  onOpenReplication,
}: {
  report: ProxySqlReport;
  current?: boolean;
  selectedServerKey: string | null;
  onSelectServer: (key: string) => void;
  onSwitchConnection?: (profileId: string) => void;
  onOpenReadyset?: () => void;
  onOpenCluster?: () => void;
  onOpenReplication?: () => void;
}) {
  const tiers = report.topology;
  const rsNodes: ProxySqlReadysetNode[] = tiers?.readyset_nodes || [];
  const dbNodes: ProxySqlTopologyBackendNode[] =
    tiers?.database_nodes ||
    report.servers.map((s) => ({
      key: `db:${s.key}`,
      server_key: s.key,
      kind: "database",
      host: s.hostname,
      port: s.port,
      address: `${s.hostname}:${s.port}`,
      hostgroup_id: s.hostgroup_id,
      hostgroup_roles: s.role ? [s.role] : [],
      // A config-only row has no runtime health: never borrow its status word.
      status: s.runtime_status,
      status_source: s.runtime_status ? ("runtime" as const) : s.config ? ("config" as const) : null,
      weight: typeof s.settings.weight === "number" ? s.settings.weight : (s.config?.weight ?? null),
      comment: typeof s.settings.comment === "string" && s.settings.comment ? s.settings.comment : (s.config?.comment || null),
      queries: s.pool?.queries ?? null,
      conn_used: s.pool?.conn_used ?? null,
      conn_free: s.pool?.conn_free ?? null,
      latency_us: s.pool?.latency_us ?? null,
      product: null,
      version: null,
      read_only: null,
      cluster_kind: null,
      cluster_name: null,
      profile_id: null,
      profile_name: null,
      verified: false,
      evidence: null,
    }));

  const activeRules = report.query_rules.filter((r) => r.runtime_active ?? r.active).length;
  const hasCluster = dbNodes.some((d) => d.cluster_kind && d.cluster_kind !== "none");
  const monitors = report.monitors || [];
  const rsStates = rsNodes.map((n) => servingState(n));
  const dbStates = dbNodes.map((d) => servingState(d));

  return (
    <section
      className="readyset-topology proxysql-topology"
      aria-label="ProxySQL routing topology"
      data-motion={current ? "on" : "off"}
    >
      <div className="tier-stack">
        <TierRow label="ProxySQL routing layer" ariaLabel="ProxySQL routing layer">
          <TierNode>
            <TierCard
              role="ProxySQL Admin"
              name={`ProxySQL ${report.version || ""}`.trim()}
              metas={[
                `${report.hostgroups.length} ${report.hostgroups.length === 1 ? "hostgroup" : "hostgroups"} · ${activeRules} active ${activeRules === 1 ? "rule" : "rules"}${(tiers?.querypilot_rule_count || 0) > 0 ? ` (${tiers!.querypilot_rule_count} QueryPilot)` : ""}`,
              ]}
              tone={
                report.status === "problem" ? "problem" : report.status === "watch" ? "watch" : report.status === "unknown" ? "unknown" : "ok"
              }
              mark={`${report.summary.ONLINE} of ${report.summary.backends} backends ONLINE`}
              flow={
                report.metrics.Client_Connections_connected !== null &&
                report.metrics.Client_Connections_connected !== undefined
                  ? `${report.metrics.Client_Connections_connected.toLocaleString()} client connections`
                  : "Admin interface"
              }
              local="This connection"
              label="This ProxySQL connection"
            />
          </TierNode>
        </TierRow>

        {rsNodes.length > 0 && (
          <>
            <TierLink state={backendLinkState(rsStates)}>
              {(tiers?.querypilot_rule_count || 0) > 0
                ? `Readyset cache & QueryPilot routing (${tiers!.querypilot_rule_count} rules)`
                : "Readyset hostgroup routing"}
            </TierLink>
            <TierRow
              label="Readyset cache & mirror tier"
              ariaLabel="Readyset cache tier"
              bus={rsNodes.length > 1}
              jumps={
                onOpenReadyset && (
                  <button type="button" className="text-button" onClick={onOpenReadyset}>
                    Open Readyset
                  </button>
                )
              }
            >
              {rsNodes.map((rs, i) => {
                const tone = serverStatusTone(rsStates[i]);
                const shownStatus = statusWord(rs.status, rs.status_source);
                const selected = selectedServerKey === rs.server_key;
                const name = rs.profile_name || `Readyset ${rs.address}`;
                const flowParts = [RS_MODE_LABEL[rs.routing_mode] || rs.routing_mode];
                if (rs.cache_count !== null && rs.cache_count !== undefined)
                  flowParts.push(`${rs.cache_count} ${rs.cache_count === 1 ? "cache" : "caches"}`);
                flowParts.push(rs.queries !== null ? `${rs.queries.toLocaleString()} queries` : `Weight ${rs.weight ?? 1}`);
                return (
                  <TierNode
                    key={rs.key}
                    stem={stemOf(tone)}
                    detached={tone === "problem"}
                    use={useOf(rs, rs.address)}
                    onSwitchConnection={onSwitchConnection}
                  >
                    <TierCard
                      role={`HG ${rs.hostgroup_id ?? "—"}`}
                      name={name}
                      metas={[`${rs.address}${rs.version ? ` · Readyset ${rs.version}` : ""}`]}
                      tone={tone}
                      detached={tone === "problem"}
                      mark={shownStatus ? `${shownStatus}${rs.readyset_status ? ` · ${rs.readyset_status}` : ""}` : "Not read"}
                      flow={
                        <>
                          {flowParts.join(" · ")}
                          {(rs.upstreams || []).length > 0 && (
                            <>
                              {" → "}
                              <bdi>{(rs.upstreams || []).map((u) => u.address).join(", ")}</bdi>
                            </>
                          )}
                        </>
                      }
                      selected={selected}
                      rowKey={rs.server_key}
                      onSelect={onSelectServer}
                      label={`${name}, Readyset`}
                      hasUse={!!(rs.profile_id && onSwitchConnection)}
                    />
                  </TierNode>
                );
              })}
            </TierRow>
          </>
        )}

        <TierLink state={backendLinkState(dbStates)}>
          {rsNodes.length > 0 ? "Direct writes, uncached reads & Readyset CDC" : "Hostgroup SQL routing"}
        </TierLink>

        <TierRow
          label="MySQL & MariaDB backend tier"
          ariaLabel="Database backend hostgroups"
          bus={dbNodes.length > 1}
          jumps={
            <>
              {hasCluster && onOpenCluster && (
                <button type="button" className="text-button" onClick={onOpenCluster}>
                  Open Cluster
                </button>
              )}
              {onOpenReplication && (
                <button type="button" className="text-button" onClick={onOpenReplication}>
                  Open Replication
                </button>
              )}
            </>
          }
        >
          {dbNodes.length === 0 ? (
            <li className="readyset-tier-empty">No MySQL/MariaDB backend servers configured.</li>
          ) : (
            dbNodes.map((db, i) => {
              const tone = serverStatusTone(dbStates[i]);
              const shownStatus = statusWord(db.status, db.status_source);
              const selected = selectedServerKey === db.server_key;
              const cbadge = clusterBadge(db.cluster_kind, db.cluster_name);
              const name = db.profile_name || db.address;
              const meta = [
                db.address !== name ? db.address : "",
                db.product ? `${db.product}${db.version ? ` ${db.version}` : ""}` : "",
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <TierNode
                  key={db.key}
                  stem={stemOf(tone)}
                  detached={tone === "problem"}
                  use={useOf(db, db.address)}
                  onSwitchConnection={onSwitchConnection}
                >
                  <TierCard
                    role={`HG ${db.hostgroup_id ?? "—"}${db.hostgroup_roles.length > 0 ? ` · ${db.hostgroup_roles.join(", ")}` : ""}`}
                    name={name}
                    metas={meta ? [meta] : []}
                    tone={tone}
                    detached={tone === "problem"}
                    mark={
                      shownStatus
                        ? `${shownStatus}${db.read_only === true ? " · read_only" : db.read_only === false ? " · read-write" : ""}${
                            shownStatus.toUpperCase().startsWith("SHUNNED") && standbyOnline(db, dbNodes, monitors)
                              ? " · standby ONLINE"
                              : ""
                          }`
                        : "Not read"
                    }
                    flow={
                      <>
                        {cbadge ? <Pill tone="neutral">{cbadge}</Pill> : null}
                        {cbadge ? " " : ""}
                        {db.queries !== null ? `${db.queries.toLocaleString()} queries` : `Weight ${db.weight ?? 1}`}
                        {db.latency_us !== null ? ` · ${(db.latency_us / 1000).toFixed(2)} ms` : ""}
                      </>
                    }
                    selected={selected}
                    rowKey={db.server_key}
                    onSelect={onSelectServer}
                    label={`${name}, HG ${db.hostgroup_id ?? "—"}`}
                    hasUse={!!(db.profile_id && onSwitchConnection)}
                  />
                </TierNode>
              );
            })
          )}
        </TierRow>
      </div>
    </section>
  );
}
