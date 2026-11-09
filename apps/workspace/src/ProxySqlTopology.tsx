// Multi-tier topology diagram for the ProxySQL place:
// ProxySQL Admin (this connection) -> [Readyset cache & mirror tier] -> [Hostgroups & MySQL/MariaDB backends].
import { useState } from "react";
import { Pill, type Tone } from "./kit";
import { ServerGlyph, TopologyControls } from "./TopologyPresentation";
import type { ProxySqlReport, ProxySqlReadysetNode, ProxySqlTopologyBackendNode } from "./proxysqlApi";

const RS_MODE_LABEL: Record<string, string> = {
  cached_and_mirrored: "Cached reads + QueryPilot mirror",
  shadow_mirror: "Shadow mirror (QueryPilot)",
  cached_reads: "Cached reads",
  hostgroup_member: "Hostgroup member",
};

function serverStatusTone(status: string | null | undefined): Tone {
  const s = (status || "").toUpperCase();
  if (s === "ONLINE") return "ok";
  if (s === "OFFLINE_SOFT" || s === "SHUNNED") return "watch";
  if (s === "OFFLINE_HARD") return "problem";
  return "unknown";
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
  const [motion, setMotion] = useState(true);
  const [depth, setDepth] = useState(true);
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
      status: s.runtime_status || s.config?.status || "UNKNOWN",
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
  const anyOnline = report.servers.some((s) => (s.runtime_status || s.config?.status) === "ONLINE");
  const hasCluster = dbNodes.some((d) => d.cluster_kind && d.cluster_kind !== "none");

  return (
    <section
      className="readyset-topology proxysql-topology"
      aria-label="ProxySQL routing topology"
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
        <div className="readyset-tier" role="group" aria-label="ProxySQL instance">
          <div className="readyset-tier-label">ProxySQL routing layer</div>
          <div className="readyset-tier-grid is-single">
            <div className="readyset-node-wrap">
              <div className={`cluster-card is-local readyset-node${report.status === "watch" ? " tone-watch" : ""}`}>
                <span className="cluster-local topology-eyebrow">
                  <ServerGlyph />
                  <span>This connection · ProxySQL Admin</span>
                </span>
                <span className="cluster-name">ProxySQL {report.version || ""}</span>
                <span className="cluster-address">
                  {report.hostgroups.length} {report.hostgroups.length === 1 ? "hostgroup" : "hostgroups"} ·{" "}
                  {activeRules} active {activeRules === 1 ? "rule" : "rules"}
                  {(tiers?.querypilot_rule_count || 0) > 0 ? ` (${tiers!.querypilot_rule_count} QueryPilot)` : ""}
                </span>
                <span className={`cluster-mark is-${report.status === "problem" ? "problem" : report.status === "watch" ? "watch" : "ok"}`}>
                  <span className="cluster-shape" aria-hidden="true" />
                  {report.summary.ONLINE} of {report.summary.backends} backends ONLINE
                </span>
                <span className="cluster-flow">
                  {report.metrics.Client_Connections_connected !== null &&
                  report.metrics.Client_Connections_connected !== undefined
                    ? `${report.metrics.Client_Connections_connected.toLocaleString()} client connections`
                    : "Admin interface"}
                </span>
              </div>
            </div>
          </div>
        </div>

        {rsNodes.length > 0 && (
          <>
            <div
              className={`topology-link ${rsNodes.some((n) => serverStatusTone(n.status) === "ok") ? "is-running" : "is-stopped"} readyset-connector`}
              aria-hidden="true"
            >
              <span className="topology-link-state">
                {(tiers?.querypilot_rule_count || 0) > 0
                  ? `Readyset cache & QueryPilot routing (${tiers!.querypilot_rule_count} rules)`
                  : "Readyset hostgroup routing"}
              </span>
              <span className="topology-line" />
            </div>

            <div className="readyset-tier" role="group" aria-label="Readyset cache tier">
              <div className="readyset-tier-head">
                <span className="readyset-tier-label">Readyset cache &amp; mirror tier</span>
                {onOpenReadyset && (
                  <div className="readyset-tier-jumps">
                    <button type="button" className="text-button" onClick={onOpenReadyset}>
                      Open Readyset
                    </button>
                  </div>
                )}
              </div>
              <div className="readyset-tier-grid">
                {rsNodes.map((rs) => {
                  const tone = serverStatusTone(rs.status);
                  const selected = selectedServerKey === rs.server_key;
                  return (
                    <div key={rs.key} className="readyset-node-wrap">
                      <button
                        type="button"
                        className={`cluster-card readyset-node${selected ? " is-selected" : ""}${tone === "watch" ? " tone-watch" : ""}`}
                        data-row-key={rs.server_key}
                        aria-pressed={selected}
                        onClick={() => onSelectServer(rs.server_key)}
                      >
                        <span className="cluster-local topology-eyebrow">
                          <ServerGlyph />
                          <span>
                            HG {rs.hostgroup_id ?? "—"} · {RS_MODE_LABEL[rs.routing_mode] || rs.routing_mode}
                          </span>
                        </span>
                        <span className="cluster-name">{rs.profile_name || `Readyset ${rs.address}`}</span>
                        <span className="cluster-address">
                          {rs.address}
                          {rs.version ? ` · Readyset ${rs.version}` : ""}
                        </span>
                        <span className={`cluster-mark is-${tone}`}>
                          <span className="cluster-shape" aria-hidden="true" />
                          {rs.status}
                          {rs.readyset_status ? ` · ${rs.readyset_status}` : ""}
                        </span>
                        <span className="cluster-flow">
                          {rs.cache_count !== null && rs.cache_count !== undefined
                            ? `${rs.cache_count} ${rs.cache_count === 1 ? "cache" : "caches"} · `
                            : ""}
                          {rs.queries !== null ? `${rs.queries.toLocaleString()} queries` : `Weight ${rs.weight ?? 1}`}
                          {(rs.upstreams || []).length > 0 ? ` → ${(rs.upstreams || []).map((u) => u.address).join(", ")}` : ""}
                        </span>
                      </button>
                      {rs.profile_id && onSwitchConnection && (
                        <div className="readyset-node-links">
                          <button type="button" className="text-button" onClick={() => onSwitchConnection(rs.profile_id!)}>
                            Use {rs.profile_name || rs.address}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </>
        )}

        <div className={`topology-link ${anyOnline ? "is-running" : "is-stopped"} readyset-connector`} aria-hidden="true">
          <span className="topology-link-state">
            {rsNodes.length > 0 ? "Direct writes, uncached reads & Readyset CDC" : "Hostgroup SQL routing"}
          </span>
          <span className="topology-line" />
        </div>

        <div className="readyset-tier" role="group" aria-label="Database backend hostgroups">
          <div className="readyset-tier-head">
            <span className="readyset-tier-label">MySQL &amp; MariaDB backend tier</span>
            <div className="readyset-tier-jumps">
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
            </div>
          </div>

          {dbNodes.length === 0 ? (
            <div className="readyset-tier-empty">No MySQL/MariaDB backend servers configured.</div>
          ) : (
            <div className="readyset-tier-grid">
              {dbNodes.map((db) => {
                const tone = serverStatusTone(db.status);
                const selected = selectedServerKey === db.server_key;
                const cbadge = clusterBadge(db.cluster_kind, db.cluster_name);
                return (
                  <div key={db.key} className="readyset-node-wrap">
                    <button
                      type="button"
                      className={`cluster-card readyset-node${selected ? " is-selected" : ""}${tone === "watch" ? " tone-watch" : ""}`}
                      data-row-key={db.server_key}
                      aria-pressed={selected}
                      onClick={() => onSelectServer(db.server_key)}
                    >
                      <span className="cluster-local topology-eyebrow">
                        <ServerGlyph />
                        <span>
                          HG {db.hostgroup_id ?? "—"}
                          {db.hostgroup_roles.length > 0 ? ` · ${db.hostgroup_roles.join(", ")}` : ""}
                        </span>
                      </span>
                      <span className="cluster-name">{db.profile_name || db.address}</span>
                      <span className="cluster-address">
                        {db.address}
                        {db.product ? ` · ${db.product}${db.version ? ` ${db.version}` : ""}` : ""}
                      </span>
                      <span className={`cluster-mark is-${tone}`}>
                        <span className="cluster-shape" aria-hidden="true" />
                        {db.status}
                        {db.read_only === true ? " · read_only" : db.read_only === false ? " · read-write" : ""}
                      </span>
                      <span className="cluster-flow">
                        {cbadge ? <Pill tone="neutral">{cbadge}</Pill> : null}
                        {cbadge ? " " : ""}
                        {db.queries !== null ? `${db.queries.toLocaleString()} queries` : `Weight ${db.weight ?? 1}`}
                        {db.latency_us !== null ? ` · ${(db.latency_us / 1000).toFixed(2)} ms` : ""}
                      </span>
                    </button>
                    {db.profile_id && onSwitchConnection && (
                      <div className="readyset-node-links">
                        <button type="button" className="text-button" onClick={() => onSwitchConnection(db.profile_id!)}>
                          Use {db.profile_name || db.address}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
