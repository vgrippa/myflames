// Upstream ProxySQL and Readyset tiers rendered above Replication and Cluster
// topologies when multi-tier routing/caching sits in front of the database
// tier, on the shared tier module (TopologyTiers). Renders nothing when no
// upstream tiers or live footprint notes exist, keeping standalone database
// topologies unchanged. States are honest (S11): a ProxySQL node reports no
// own health here, so its card reads "Not read" and its link never runs; a
// Readyset link runs only on a reported Replication "Running".
import { Pill, type Tone } from "./kit";
import { TierCard, TierLink, TierNode, TierRow, matchEvidenceLabel, type TierLinkState, type TierStem, type TierUse } from "./TopologyTiers";
import type { DatabaseTopologyTiers } from "./readysetApi";

type ReadysetTierNode = DatabaseTopologyTiers["readyset"][number];

/** Readyset's own states: Status is only Online, Maintenance Mode or Snapshot
 * In Progress, and Replication Status only Running, Stopped or Disabled; a
 * problem lives in Database Connection "Unreachable" or a reported replicator
 * error (readyset-client/src/status.rs 86-123). */
function rsTone(rs: ReadysetTierNode): Tone {
  const s = (rs.status || "").toLowerCase();
  const r = (rs.replication_status || "").toLowerCase();
  const db = (rs.database_connection || "").toLowerCase();
  if (rs.replication_error || db === "unreachable" || s.includes("unavail") || r === "error") return "problem";
  if (s.includes("maintenance") || s.includes("snapshot") || r === "stopped" || r === "disabled") return "watch";
  if (s === "online" || s === "up") return "ok";
  return "unknown";
}

function useOf(node: { profile_id: string | null; profile_name: string | null }, fallback: string): TierUse | null {
  return node.profile_id ? { id: node.profile_id, name: node.profile_name || fallback } : null;
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

  const readysetLink: TierLinkState = readyset.some((rs) => rsTone(rs) === "problem")
    ? "error"
    : readyset.some((rs) => rs.replication_status === "Stopped" || rs.replication_status === "Disabled")
      ? "stopped"
      : readyset.length > 0 && readyset.every((rs) => rs.replication_status === "Running")
        ? "running"
        : "unread";
  // The visible and accessible label carries the computed state, never just
  // the running description (the stem alone is hidden from assistive tech).
  const readysetLinkLabel =
    readysetLink === "error"
      ? "Replication error or upstream unreachable · query fallback"
      : readysetLink === "stopped"
        ? readyset.some((rs) => rs.replication_status === "Stopped")
          ? "Replication stopped · query fallback"
          : "Replication disabled · query fallback"
        : readysetLink === "running"
          ? "CDC binlog stream & query fallback"
          : "Replication not read · query fallback";

  return (
    <div className="upstream-tiers" aria-label="Upstream routing and caching tiers">
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
            {proxysql.map((px) => (
              <TierNode
                key={px.key}
                stem={proxysql.length > 1 ? "plain" : null}
                use={useOf(px, px.label)}
                onSwitchConnection={onSwitchConnection}
              >
                <TierCard
                  role={px.version ? <>ProxySQL · <bdi>{px.version}</bdi></> : "ProxySQL"}
                  name={px.label}
                  metas={[px.address]}
                  tone="unknown"
                  mark="Not read"
                  markTitle="This view reads the database tier: ProxySQL's own health is not reported here."
                  flow={
                    px.querypilot_rule_count > 0
                      ? `${px.querypilot_rule_count} QueryPilot ${px.querypilot_rule_count === 1 ? "rule" : "rules"}`
                      : matchEvidenceLabel(px.evidence) === "Verified"
                        ? "Verified via mysql_servers"
                        : "Matched by address"
                  }
                  label={`${px.label}, ProxySQL`}
                  hasUse={!!(px.profile_id && onSwitchConnection)}
                />
              </TierNode>
            ))}
          </TierRow>
          <TierLink state="unread">{readyset.length > 0 ? "SQL routing & cache mirror" : "SQL routing"}</TierLink>
        </>
      )}

      {readyset.length > 0 && (
        <>
          <TierRow
            label="Readyset cache tier"
            bus={readyset.length > 1}
            jumps={
              onOpenReadyset && (
                <button type="button" className="text-button" onClick={onOpenReadyset}>
                  Open Readyset
                </button>
              )
            }
          >
            {readyset.map((rs) => {
              const tone = rsTone(rs);
              const stem: TierStem = tone === "problem" ? "broken" : rs.replication_status === "Running" ? "active" : "plain";
              return (
                <TierNode
                  key={rs.key}
                  stem={stem}
                  detached={tone === "problem"}
                  use={useOf(rs, rs.label)}
                  onSwitchConnection={onSwitchConnection}
                >
                  <TierCard
                    role={rs.version ? <>Readyset · <bdi>{rs.version}</bdi></> : "Readyset"}
                    name={rs.label}
                    metas={[rs.address]}
                    tone={tone}
                    detached={tone === "problem"}
                    mark={
                      rs.status
                        ? `${rs.status} · Repl ${rs.replication_status && rs.replication_status !== "Unknown" ? rs.replication_status : "not read"}`
                        : "Not read"
                    }
                    flow={
                      <>
                        <Pill tone="neutral">{rs.purpose || "cdc, proxy"}</Pill>
                        {rs.cache_count !== null && rs.cache_count !== undefined && (
                          <span>
                            {" "}
                            · {rs.cache_count} {rs.cache_count === 1 ? "cache" : "caches"}
                          </span>
                        )}
                      </>
                    }
                    label={`${rs.label}, Readyset`}
                    hasUse={!!(rs.profile_id && onSwitchConnection)}
                  />
                </TierNode>
              );
            })}
          </TierRow>
          <TierLink state={readysetLink}>{readysetLinkLabel}</TierLink>
        </>
      )}

      {notes.length > 0 && (
        <ul className="upstream-tier-notes">
          {notes.map((n) => (
            <li key={n}>
              <bdi>{n}</bdi>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
