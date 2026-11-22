// Multi-tier topology diagram for the Readyset place, on the shared tier
// module (TopologyTiers): [ProxySQL routing tier] -> Readyset (this
// connection) -> [Upstream MySQL/MariaDB + cluster peers & async replicas].
// States come only from reported server states (S11): a link runs when
// replication or the ProxySQL backend was reported running, a reported error
// or stop draws it broken or dashed, and a state the server did not report
// reads "Not read", never OK.
import { Pill, type Tone } from "./kit";
import { proxySqlBackendState } from "./ProxySqlTopology";
import { TierCard, TierLink, TierNode, TierRow, matchEvidenceLabel, type TierLinkState, type TierStem, type TierUse } from "./TopologyTiers";
import { cachesReadFailed } from "./readysetApi";
import type {
  ReadysetReport,
  ReadysetTopologyPeerNode,
  ReadysetTopologyProxyNode,
  ReadysetTopologyUpstreamNode,
} from "./readysetApi";

const ROUTING_LABELS: Record<ReadysetTopologyProxyNode["routing_mode"], string> = {
  cached_and_mirrored: "Cached reads + QueryPilot mirror",
  shadow_mirror: "QueryPilot shadow mirror",
  cached_reads: "Cached reads",
  backend: "Backend server",
};

function upstreamTone(u: ReadysetTopologyUpstreamNode): Tone {
  const h = (u.health || "").toLowerCase();
  if (h === "down") return u.active_cdc ? "problem" : "watch";
  if (h === "up" || h === "healthy" || h === "online") return "ok";
  return "unknown";
}

function peerTone(p: ReadysetTopologyPeerNode): Tone {
  if (p.relation === "cluster_member") {
    const st = (p.state || "").toUpperCase();
    if (st === "ONLINE" || st === "SYNCED") return "ok";
    if (st === "RECOVERING" || st === "DONOR" || st === "JOINING") return "watch";
    if (st === "ERROR" || st === "UNREACHABLE" || st === "OFFLINE") return "problem";
    return "unknown";
  }
  if (p.io_running === "No" || p.sql_running === "No") return "problem";
  if (p.io_running === "Connecting" && p.sql_running === "Yes") return "watch";
  if (p.io_running === "Yes" && p.sql_running === "Yes") {
    return typeof p.lag_s === "number" && p.lag_s >= 30 ? "watch" : "ok";
  }
  return "unknown";
}

function clusterKindLabel(kind: string | null | undefined): string {
  if (kind === "innodb_cluster") return "InnoDB Cluster";
  if (kind === "group_replication") return "Group Replication";
  if (kind === "pxc") return "Percona XtraDB Cluster";
  if (kind === "galera") return "Galera Cluster";
  return kind || "Cluster";
}

/** ProxySQL -> Readyset: running only when a ProxySQL node reported the
 * Readyset backend ONLINE; a reported hard failure is an error, a reported
 * soft-offline or shunned state a stop; nothing reported is not read. The
 * per-node rule is the shared proxySqlBackendState from ProxySqlTopology. */
function proxyLinkState(nodes: ReadysetTopologyProxyNode[]): TierLinkState {
  const states = nodes.map((n) => proxySqlBackendState(n.backend_status).state);
  if (!states.length || states.every((s) => s === "unread")) return "unread";
  if (states.some((s) => s === "running")) return "running";
  if (states.some((s) => s === "error")) return "error";
  if (states.some((s) => s === "stopped")) return "stopped";
  return "unread";
}

/** True when a replicator error was reported, whatever the Replication
 * Status: the engine reports only Running, Stopped or Disabled there and
 * carries the failure in Last replicator error (and the replication_error
 * finding). */
export function replicationErrored(report: ReadysetReport): boolean {
  return (
    !!report.replication?.last_error ||
    report.replication?.status === "Error" ||
    report.findings.some((f) => f.id === "replication_error")
  );
}

/** Readyset -> upstreams: running only on Replication "Running"; a reported
 * replicator error breaks the stem whatever the status, a reported Stop or
 * Disabled dashes it; anything unreported (null or the backend's "Unknown"
 * placeholder) is unread. */
function replicationLinkState(report: ReadysetReport): TierLinkState {
  if (replicationErrored(report)) return "error";
  const status = report.replication?.status ?? report.status_fields.replication_status ?? null;
  if (!status || status === "Unknown") return "unread";
  if (status === "Running") return "running";
  if (status === "Stopped" || status === "Disabled") return "stopped";
  return "unread";
}

/** The backend reports an unread Replication Status as "Unknown"; both that
 * and null mean not read, never a state word. */
function replicationKnown(status: string | null | undefined): status is string {
  return !!status && status !== "Unknown";
}

function useOf(node: { profile_id: string | null; profile_name: string | null }, fallback: string): TierUse | null {
  return node.profile_id ? { id: node.profile_id, name: node.profile_name || fallback } : null;
}

export function ReadysetTopologyDiagram({
  report,
  current = true,
  selectedKey,
  onSelectUpstream,
  onSwitchConnection,
  onOpenProxySql,
  onOpenCluster,
  onOpenReplication,
  connectionAddress,
}: {
  report: ReadysetReport;
  current?: boolean;
  selectedKey: string | null;
  onSelectUpstream: (key: string) => void;
  onSwitchConnection?: (profileId: string) => void;
  onOpenProxySql?: () => void;
  onOpenCluster?: () => void;
  onOpenReplication?: () => void;
  /** host:port of the connection in use: the Readyset card's address when the
   * report has none and the report is current (a stale report never borrows
   * it). */
  connectionAddress?: string;
}) {
  const topo = report.topology;
  const proxysql = topo?.proxysql_nodes || [];
  const upstreams = topo?.upstreams || [];
  const peers = topo?.database_peers || [];
  const clusterMembers = peers.filter((p) => p.relation === "cluster_member");
  const replicas = peers.filter((p) => p.relation === "replica");

  const shallowCount = report.caches.filter((c) => c.cache_type === "shallow").length;
  const deepCount = report.caches.filter((c) => c.cache_type === "deep").length;
  const selfStatus = report.status_fields.status ?? null;
  const replStatus = report.replication?.status ?? report.status_fields.replication_status ?? null;
  const selfTone: Tone = !selfStatus
    ? "unknown"
    : report.status === "problem"
      ? "problem"
      : report.status === "watch"
        ? "watch"
        : report.status === "unknown"
          ? "unknown"
          : "ok";

  return (
    <section
      className="readyset-topology"
      aria-label="Readyset multi-tier topology"
      data-motion={current ? "on" : "off"}
    >
      <div className="tier-stack">
        {proxysql.length > 0 && (
          <>
            <TierRow
              label="ProxySQL routing tier"
              bus={proxysql.length > 1}
              jumps={
                onOpenProxySql && (
                  <button type="button" className="text-button" onClick={onOpenProxySql}>
                    Open ProxySQL
                  </button>
                )
              }
            >
              {proxysql.map((px) => {
                // The shared ProxySQL backend rule: every SHUNNED variant is
                // a reported stop, and the UNKNOWN placeholder reads Not read.
                const backend = proxySqlBackendState(px.backend_status);
                const tone = backend.tone;
                return (
                  <TierNode
                    key={px.key}
                    stem={proxysql.length > 1 ? (tone === "ok" ? "active" : tone === "problem" ? "broken" : "plain") : null}
                    detached={tone === "problem"}
                    use={useOf(px, px.label)}
                    onSwitchConnection={onSwitchConnection}
                  >
                    <TierCard
                      role={px.version ? <>ProxySQL · <bdi>{px.version}</bdi></> : "ProxySQL"}
                      name={px.label}
                      metas={[`${px.address}${px.hostgroup_id !== null ? ` · HG ${px.hostgroup_id}` : ""}`]}
                      tone={tone}
                      detached={tone === "problem"}
                      mark={backend.word || "Not read"}
                      flow={ROUTING_LABELS[px.routing_mode]}
                      label={`${px.label}, ProxySQL`}
                      hasUse={!!(px.profile_id && onSwitchConnection)}
                    />
                  </TierNode>
                );
              })}
            </TierRow>
            <TierLink state={proxyLinkState(proxysql)}>
              Routed reads &amp; QueryPilot traffic
              {proxysql.reduce((n, px) => n + px.querypilot_rule_count, 0) > 0
                ? ` · ${proxysql.reduce((n, px) => n + px.querypilot_rule_count, 0)} QueryPilot ${proxysql.reduce((n, px) => n + px.querypilot_rule_count, 0) === 1 ? "rule" : "rules"}`
                : ""}
            </TierLink>
          </>
        )}

        <TierRow label="Readyset cache engine" ariaLabel="Readyset cache engine">
          <TierNode>
            <TierCard
              role={report.port_role === "admin" ? "Admin SQL port" : report.port_role === null ? "Port not read" : "SQL port"}
              name={`Readyset ${report.version || ""}`.trim()}
              metas={[
                // A stale report never borrows the connection in use's
                // address: only the address the report itself read.
                `${topo?.this_server.address || (current ? connectionAddress : undefined) || "Connected instance"} · ${report.connections.length} client ${report.connections.length === 1 ? "session" : "sessions"}`,
              ]}
              tone={selfTone}
              mark={selfStatus ? `${selfStatus} · Repl ${replicationKnown(replStatus) ? replStatus : "not read"}` : "Not read"}
              flow={
                // A failed SHOW CACHES read leaves an empty list: not read,
                // never zero caches.
                cachesReadFailed(report)
                  ? "Caches not read"
                  : `${report.caches.length} ${report.caches.length === 1 ? "cache" : "caches"} (${shallowCount} shallow, ${deepCount} deep)`
              }
              local={current ? "This connection" : null}
              label="This Readyset connection"
            />
          </TierNode>
        </TierRow>

        <TierLink state={replicationLinkState(report)}>
          {replicationErrored(report) ? (
            "Replication error"
          ) : replStatus === "Stopped" ? (
            "Replication stopped"
          ) : replStatus === "Disabled" ? (
            // Disabled means the replicator is not started: query fallback
            // stands on its own, never as a CDC connection.
            "Replication disabled · query fallback"
          ) : replicationKnown(replStatus) ? (
            // One inline child of the flex plate: no flex gap can land
            // between the text and the bidi-isolated mode value.
            <span>
              CDC replication (<bdi>{report.replication?.mode || "binlog"}</bdi>) &amp; query fallback
            </span>
          ) : (
            "Replication not read"
          )}
        </TierLink>

        <TierRow
          label={
            <>
              Upstream database tier
              {topo?.cluster && (
                <>
                  {" · "}
                  <bdi>
                    {clusterKindLabel(topo.cluster.kind)}
                    {topo.cluster.name ? ` (${topo.cluster.name})` : ""}
                  </bdi>
                </>
              )}
            </>
          }
          ariaLabel="Upstream database tier"
          bus={upstreams.length > 1}
          jumps={
            <>
              {topo?.cluster && onOpenCluster && (
                <button type="button" className="text-button" onClick={onOpenCluster}>
                  Open Cluster
                </button>
              )}
              {replicas.length > 0 && onOpenReplication && (
                <button type="button" className="text-button" onClick={onOpenReplication}>
                  Open Replication
                </button>
              )}
            </>
          }
        >
          {upstreams.length === 0 ? (
            <li className="readyset-tier-empty">
              No upstream server rows reported (<bdi>{report.status_fields.cdc_server || "upstream unknown"}</bdi>).
            </li>
          ) : (
            upstreams.map((u) => {
              const tone = upstreamTone(u);
              const selected = selectedKey === u.key;
              // active_cdc null means Readyset did not report the CDC role:
              // unknown reads "Not read", never a fallback claim.
              const roleLabel =
                u.active_cdc === null || u.active_cdc === undefined
                  ? "Not read"
                  : u.active_cdc
                    ? "Active CDC primary"
                    : u.is_cdc
                      ? "CDC candidate"
                      : "Query fallback";
              const name = u.profile_name || u.address;
              const stem: TierStem = tone === "problem" ? "broken" : tone === "ok" ? "active" : "plain";
              // Cluster card anatomy: one meta line each, the address only
              // when it differs from the name, then product and version, so
              // nothing truncates at the fixed card width.
              const metas = [
                ...(u.address !== name ? [u.address] : []),
                ...(u.product ? [`${u.product}${u.version ? ` ${u.version}` : ""}`] : []),
              ];
              return (
                <TierNode
                  key={u.key}
                  stem={stem}
                  detached={tone === "problem"}
                  use={useOf(u, u.address)}
                  onSwitchConnection={onSwitchConnection}
                >
                  <TierCard
                    role={roleLabel}
                    name={name}
                    metas={metas}
                    tone={tone}
                    detached={tone === "problem"}
                    mark={
                      u.health
                        ? `${u.health.toUpperCase()}${u.cluster_kind && u.cluster_kind !== "none" ? ` · ${clusterKindLabel(u.cluster_kind)}` : ""}`
                        : "Not read"
                    }
                    flow={`Purpose: ${u.purpose || "proxy"}`}
                    selected={selected}
                    rowKey={u.key}
                    onSelect={onSelectUpstream}
                    label={`${name}, ${roleLabel}`}
                    hasUse={!!(u.profile_id && onSwitchConnection)}
                  />
                </TierNode>
              );
            })
          )}
        </TierRow>

        {(clusterMembers.length > 0 || replicas.length > 0) && (
          <TierRow
            label={
              clusterMembers.length > 0 && replicas.length > 0
                ? "Cluster members & async replicas"
                : clusterMembers.length > 0
                  ? "Other cluster members"
                  : "Downstream async replicas"
            }
            ariaLabel="Cluster members and async replicas"
          >
            {peers.map((p) => {
              const tone = peerTone(p);
              const name = p.profile_name || p.name || p.address || "Peer";
              const stem: TierStem =
                p.relation === "replica" ? "async" : tone === "problem" ? "broken" : tone === "ok" ? "active" : "plain";
              const mark =
                p.relation === "cluster_member"
                  ? p.state || "Not read"
                  : p.io_running === "Yes" && p.sql_running === "Yes"
                    ? `Replicating${typeof p.lag_s === "number" ? ` · lag ${p.lag_s}s` : ""}`
                    : p.io_running === "No" || p.sql_running === "No"
                      ? "Replication stopped"
                      : p.io_running === "Connecting" && p.sql_running === "Yes"
                        ? "Connecting"
                        : "Not read";
              const peerMetas = [
                ...(p.address && p.address !== name ? [p.address] : []),
                ...(p.product ? [`${p.product}${p.version ? ` ${p.version}` : ""}`] : []),
              ];
              return (
                <TierNode
                  key={p.key}
                  stem={stem}
                  detached={tone === "problem"}
                  use={useOf(p, p.name || p.address || "connection")}
                  onSwitchConnection={onSwitchConnection}
                >
                  <TierCard
                    role={
                      p.relation === "cluster_member" ? (
                        <>
                          {clusterKindLabel(p.cluster_kind)} · <bdi>{p.role || "Member"}</bdi>
                        </>
                      ) : (
                        "Async replica"
                      )
                    }
                    name={name}
                    metas={peerMetas.length ? peerMetas : p.address ? [] : ["Endpoint unverified"]}
                    tone={tone}
                    detached={tone === "problem"}
                    mark={mark}
                    flow={
                      <Pill tone="neutral">
                        {p.verified ? (matchEvidenceLabel(p.evidence) === "Verified" ? "Verified connection" : "Matched by address") : "Discovered peer"}
                      </Pill>
                    }
                    label={name}
                    hasUse={!!(p.profile_id && onSwitchConnection)}
                  />
                </TierNode>
              );
            })}
          </TierRow>
        )}
      </div>
    </section>
  );
}
