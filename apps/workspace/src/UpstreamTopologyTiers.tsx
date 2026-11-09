// Upstream ProxySQL and Readyset tiers rendered above Replication and Cluster
// topologies when multi-tier routing/caching sits in front of the database tier.
// Renders nothing when no upstream tiers or live footprint notes exist, keeping
// standalone database topologies unchanged.
import { Pill, type Tone } from "./kit";
import { ServerGlyph } from "./TopologyPresentation";
import type { DatabaseTopologyTiers } from "./readysetApi";

function rsTone(status: string | null | undefined, replStatus?: string | null): Tone {
  const s = (status || "").toLowerCase();
  const r = (replStatus || "").toLowerCase();
  if (s.includes("unavail") || s.includes("error") || r === "error") return "problem";
  if (s.includes("maintenance") || s.includes("snapshot") || r === "stopped") return "watch";
  if (s === "online" || s === "up") return "ok";
  return "unknown";
}

export function UpstreamTopologyTiers({
  tiers,
  onSwitchConnection,
  onOpenReadyset,
  onOpenProxySql,
}: {
  tiers?: DatabaseTopologyTiers | null;
  onSwitchConnection?: (profileId: string) => void;
  onOpenReadyset?: () => void;
  onOpenProxySql?: () => void;
}) {
  const proxysql = tiers?.proxysql || [];
  const readyset = tiers?.readyset || [];
  const notes = tiers?.notes || [];
  if (!proxysql.length && !readyset.length && !notes.length) return null;

  return (
    <div className="upstream-tiers" aria-label="Upstream routing and caching tiers">
      {proxysql.length > 0 && (
        <>
          <div className="upstream-tier-group" role="group" aria-label="ProxySQL routing tier">
            <div className="upstream-tier-heading">ProxySQL routing tier</div>
            <div className="upstream-tier-cards">
              {proxysql.map((px) => (
                <div key={px.key} className="upstream-tier-item">
                  <div className="cluster-card upstream-tier-card">
                    <span className="cluster-local topology-eyebrow">
                      <ServerGlyph />
                      <span>ProxySQL{px.version ? ` · ${px.version}` : ""}</span>
                    </span>
                    <span className="cluster-name">{px.label}</span>
                    <span className="cluster-address">{px.address}</span>
                    <span className="cluster-mark is-ok">
                      <span className="cluster-shape" aria-hidden="true" />
                      {px.routes_to_readyset ? "Routes to Readyset & DB" : "Routes to database tier"}
                    </span>
                    <span className="cluster-flow">
                      {px.querypilot_rule_count > 0
                        ? `${px.querypilot_rule_count} QueryPilot ${px.querypilot_rule_count === 1 ? "rule" : "rules"}`
                        : "Verified via mysql_servers"}
                    </span>
                  </div>
                  <div className="upstream-tier-actions">
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
              ))}
            </div>
          </div>
          <div className="topology-link is-running upstream-tier-connector" aria-hidden="true">
            <span className="topology-link-state">
              {readyset.length > 0 ? "SQL routing & cache mirror" : "SQL routing"}
            </span>
            <span className="topology-line" />
          </div>
        </>
      )}

      {readyset.length > 0 && (
        <>
          <div className="upstream-tier-group" role="group" aria-label="Readyset cache tier">
            <div className="upstream-tier-heading">Readyset cache tier</div>
            <div className="upstream-tier-cards">
              {readyset.map((rs) => {
                const tone = rsTone(rs.status, rs.replication_status);
                return (
                  <div key={rs.key} className="upstream-tier-item">
                    <div className={`cluster-card upstream-tier-card${tone === "watch" ? " tone-watch" : ""}`}>
                      <span className="cluster-local topology-eyebrow">
                        <ServerGlyph />
                        <span>Readyset{rs.version ? ` · ${rs.version}` : ""}</span>
                      </span>
                      <span className="cluster-name">{rs.label}</span>
                      <span className="cluster-address">{rs.address}</span>
                      <span className={`cluster-mark is-${tone}`}>
                        <span className="cluster-shape" aria-hidden="true" />
                        {rs.status}
                        {rs.replication_status ? ` · Repl ${rs.replication_status}` : ""}
                      </span>
                      <span className="cluster-flow">
                        <Pill tone="neutral">{rs.purpose || "cdc, proxy"}</Pill>
                        {rs.cache_count !== null && rs.cache_count !== undefined && (
                          <span> · {rs.cache_count} {rs.cache_count === 1 ? "cache" : "caches"}</span>
                        )}
                      </span>
                    </div>
                    <div className="upstream-tier-actions">
                      {rs.profile_id && onSwitchConnection && (
                        <button type="button" className="text-button" onClick={() => onSwitchConnection(rs.profile_id!)}>
                          Use {rs.profile_name || rs.label}
                        </button>
                      )}
                      {onOpenReadyset && (
                        <button type="button" className="text-button" onClick={onOpenReadyset}>
                          Open Readyset
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
          <div
            className={`topology-link ${readyset.every((r) => rsTone(r.status, r.replication_status) === "ok") ? "is-running" : "is-stopped"} upstream-tier-connector`}
            aria-hidden="true"
          >
            <span className="topology-link-state">CDC binlog stream &amp; query fallback</span>
            <span className="topology-line" />
          </div>
        </>
      )}

      {notes.length > 0 && (
        <ul className="upstream-tier-notes">
          {notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
