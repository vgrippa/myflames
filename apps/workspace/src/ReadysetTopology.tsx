// Multi-tier topology diagram for the Readyset place:
// [ProxySQL routing tier] -> Readyset (this connection) -> [Upstream MySQL/MariaDB + Cluster peers & Replicas].
import { useState } from "react";
import { Pill, type Tone } from "./kit";
import { ServerGlyph, TopologyControls } from "./TopologyPresentation";
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

export function ReadysetTopologyDiagram({
  report,
  current = true,
  selectedKey,
  onSelectUpstream,
  onSwitchConnection,
  onOpenProxySql,
  onOpenCluster,
  onOpenReplication,
}: {
  report: ReadysetReport;
  current?: boolean;
  selectedKey: string | null;
  onSelectUpstream: (key: string) => void;
  onSwitchConnection?: (profileId: string) => void;
  onOpenProxySql?: () => void;
  onOpenCluster?: () => void;
  onOpenReplication?: () => void;
}) {
  const [motion, setMotion] = useState(true);
  const [depth, setDepth] = useState(true);
  const topo = report.topology;
  const proxysql = topo?.proxysql_nodes || [];
  const upstreams = topo?.upstreams || [];
  const peers = topo?.database_peers || [];
  const clusterMembers = peers.filter((p) => p.relation === "cluster_member");
  const replicas = peers.filter((p) => p.relation === "replica");

  const shallowCount = report.caches.filter((c) => c.cache_type === "shallow").length;
  const deepCount = report.caches.filter((c) => c.cache_type === "deep").length;
  const selfStatus = report.status_fields.status || "Online";
  const replStatus = report.replication?.status || report.status_fields.replication_status || "Unknown";
  const selfTone: Tone = report.status === "problem" ? "problem" : report.status === "watch" ? "watch" : "ok";
  const cdcHealthy =
    selfTone !== "problem" &&
    replStatus !== "Error" &&
    replStatus !== "Stopped" &&
    upstreams.some((u) => upstreamTone(u) === "ok");

  return (
    <section
      className="readyset-topology"
      aria-label="Readyset multi-tier topology"
      data-depth={depth ? "on" : "off"}
      data-motion={motion && current ? "on" : "off"}
    >
      <div className="topology-presentation-bar">
        <TopologyControls
          motion={motion}
          depth={depth}
          onMotion={() => setMotion(!motion)}
          onDepth={() => setDepth(!depth)}
        />
      </div>

      <div className="readyset-tier-stack">
        {proxysql.length > 0 && (
          <>
            <div className="readyset-tier" role="group" aria-label="ProxySQL routing tier">
              <div className="readyset-tier-label">ProxySQL routing tier</div>
              <div className="readyset-tier-grid">
                {proxysql.map((px) => {
                  const online = (px.backend_status || "ONLINE").toUpperCase() === "ONLINE";
                  return (
                    <div key={px.key} className="readyset-node-wrap">
                      <div className={`cluster-card readyset-node${online ? "" : " tone-watch"}`}>
                        <span className="cluster-local topology-eyebrow">
                          <ServerGlyph />
                          <span>ProxySQL{px.version ? ` · ${px.version}` : ""}</span>
                        </span>
                        <span className="cluster-name">{px.label}</span>
                        <span className="cluster-address">
                          {px.address}
                          {px.hostgroup_id !== null ? ` · HG ${px.hostgroup_id}` : ""}
                        </span>
                        <span className={`cluster-mark is-${online ? "ok" : "watch"}`}>
                          <span className="cluster-shape" aria-hidden="true" />
                          {px.backend_status || "ONLINE"} · {ROUTING_LABELS[px.routing_mode]}
                        </span>
                        <span className="cluster-flow">
                          {px.querypilot_rule_count > 0
                            ? `${px.querypilot_rule_count} QueryPilot ${px.querypilot_rule_count === 1 ? "rule" : "rules"}`
                            : "Routes SQL traffic to this Readyset"}
                        </span>
                      </div>
                      <div className="readyset-node-links">
                        {px.profile_id && onSwitchConnection && (
                          <button type="button" className="text-button" onClick={() => onSwitchConnection(px.profile_id!)}>
                            Use {px.profile_name || px.label}
                          </button>
                        )}
                        {onOpenProxySql && (
                          <button type="button" className="text-button" onClick={onOpenProxySql}>
                            Open ProxySQL
                          </button>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            <div
              className={`topology-link ${proxysql.some((p) => (p.backend_status || "ONLINE") === "ONLINE") ? "is-running" : "is-stopped"} readyset-connector`}
              aria-hidden="true"
            >
              <span className="topology-link-state">Routed reads &amp; QueryPilot traffic</span>
              <span className="topology-line" />
            </div>
          </>
        )}

        <div className="readyset-tier" role="group" aria-label="Readyset cache instance">
          <div className="readyset-tier-label">Readyset cache engine</div>
          <div className="readyset-tier-grid is-single">
            <div className="readyset-node-wrap">
              <div className={`cluster-card is-local readyset-node${selfTone === "watch" ? " tone-watch" : ""}`}>
                <span className="cluster-local topology-eyebrow">
                  <ServerGlyph />
                  <span>
                    This connection · {report.port_role === "admin" ? "Admin SQL port" : "SQL port"}
                  </span>
                </span>
                <span className="cluster-name">Readyset {report.version || ""}</span>
                <span className="cluster-address">
                  {topo?.this_server.address || "Connected instance"} · {report.connections.length} client{" "}
                  {report.connections.length === 1 ? "session" : "sessions"}
                </span>
                <span className={`cluster-mark is-${selfTone}`}>
                  <span className="cluster-shape" aria-hidden="true" />
                  {selfStatus} · Replication {replStatus}
                </span>
                <span className="cluster-flow">
                  {report.caches.length} {report.caches.length === 1 ? "cache" : "caches"} ({shallowCount} shallow,{" "}
                  {deepCount} deep)
                  {report.telemetry.cached_share_pct !== null ? ` · ${report.telemetry.cached_share_pct}% cached` : ""}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div
          className={`topology-link ${cdcHealthy ? "is-running" : replStatus === "Error" ? "is-error" : "is-stopped"} readyset-connector`}
          aria-hidden="true"
        >
          <span className="topology-link-state">
            {replStatus === "Error"
              ? "Replication error"
              : `CDC replication (${report.replication?.mode || "binlog"}) & query fallback`}
          </span>
          <span className="topology-line" />
        </div>

        <div className="readyset-tier" role="group" aria-label="Upstream database tier">
          <div className="readyset-tier-head">
            <span className="readyset-tier-label">
              Upstream database tier
              {topo?.cluster ? ` · ${clusterKindLabel(topo.cluster.kind)}${topo.cluster.name ? ` (${topo.cluster.name})` : ""}` : ""}
            </span>
            <div className="readyset-tier-jumps">
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
            </div>
          </div>

          {upstreams.length === 0 ? (
            <div className="readyset-tier-empty">
              No upstream server rows reported ({report.status_fields.cdc_server || "upstream unknown"}).
            </div>
          ) : (
            <div className="readyset-tier-grid">
              {upstreams.map((u) => {
                const tone = upstreamTone(u);
                const selected = selectedKey === u.key;
                const roleLabel = u.active_cdc
                  ? "Active CDC primary"
                  : u.is_cdc
                    ? "CDC candidate"
                    : "Query fallback";
                return (
                  <div key={u.key} className="readyset-node-wrap">
                    <button
                      type="button"
                      className={`cluster-card readyset-node${selected ? " is-selected" : ""}${tone === "watch" ? " tone-watch" : ""}`}
                      data-row-key={u.key}
                      aria-pressed={selected}
                      onClick={() => onSelectUpstream(u.key)}
                    >
                      <span className="cluster-local topology-eyebrow">
                        <ServerGlyph />
                        <span>{roleLabel}</span>
                      </span>
                      <span className="cluster-name">{u.profile_name || u.address}</span>
                      <span className="cluster-address">
                        {u.address}
                        {u.product ? ` · ${u.product}${u.version ? ` ${u.version}` : ""}` : ""}
                      </span>
                      <span className={`cluster-mark is-${tone}`}>
                        <span className="cluster-shape" aria-hidden="true" />
                        {(u.health || "unknown").toUpperCase()}
                        {u.cluster_kind && u.cluster_kind !== "none" ? ` · ${clusterKindLabel(u.cluster_kind)}` : ""}
                      </span>
                      <span className="cluster-flow">
                        Purpose: {u.purpose || "proxy"}
                        {u.connections !== null ? ` · ${u.connections} pool conn` : ""}
                      </span>
                    </button>
                    {u.profile_id && onSwitchConnection && (
                      <div className="readyset-node-links">
                        <button type="button" className="text-button" onClick={() => onSwitchConnection(u.profile_id!)}>
                          Use {u.profile_name || u.address}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {(clusterMembers.length > 0 || replicas.length > 0) && (
            <div className="readyset-peers">
              <div className="readyset-tier-label">
                {clusterMembers.length > 0 && replicas.length > 0
                  ? "Cluster members & async replicas"
                  : clusterMembers.length > 0
                    ? "Other cluster members"
                    : "Downstream async replicas"}
              </div>
              <div className="readyset-tier-grid">
                {peers.map((p) => {
                  const tone = peerTone(p);
                  return (
                    <div key={p.key} className="readyset-node-wrap">
                      <div className={`cluster-card readyset-node${tone === "watch" ? " tone-watch" : ""}`}>
                        <span className="cluster-local topology-eyebrow">
                          <ServerGlyph />
                          <span>
                            {p.relation === "cluster_member"
                              ? `${clusterKindLabel(p.cluster_kind)} · ${p.role || "Member"}`
                              : "Async replica"}
                          </span>
                        </span>
                        <span className="cluster-name">{p.profile_name || p.name || p.address || "Peer"}</span>
                        <span className="cluster-address">
                          {p.address || "Endpoint unverified"}
                          {p.product ? ` · ${p.product}${p.version ? ` ${p.version}` : ""}` : ""}
                        </span>
                        <span className={`cluster-mark is-${tone}`}>
                          <span className="cluster-shape" aria-hidden="true" />
                          {p.relation === "cluster_member"
                            ? p.state || "MEMBER"
                            : p.io_running === "Yes" && p.sql_running === "Yes"
                              ? `Replicating${typeof p.lag_s === "number" ? ` · lag ${p.lag_s}s` : ""}`
                              : "Replication stopped"}
                        </span>
                        <span className="cluster-flow">
                          <Pill tone="neutral">{p.verified ? "Verified connection" : "Discovered peer"}</Pill>
                        </span>
                      </div>
                      {p.profile_id && onSwitchConnection && (
                        <div className="readyset-node-links">
                          <button type="button" className="text-button" onClick={() => onSwitchConnection(p.profile_id!)}>
                            Use {p.profile_name || p.name || "connection"}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
