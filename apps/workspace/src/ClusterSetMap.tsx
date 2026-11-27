// ClusterSetMap: the ClusterSet level of the Cluster place (the zoomable
// InnoDB ClusterSet view). One card per cluster: its name, role, the status
// word in plain words with the MySQL Shell term as secondary text, the member
// count and primary member when the cluster was read, and how it was read
// (this connection, a saved route, the metadata only, or the read error);
// the reason stays on demand behind Details, as findings do. An arrow from
// the primary cluster to each replica cluster carries the
// clusterset_replication receiver and applier state when read. A card's Open
// button, a double-click or Enter on the focused card zooms into the cluster;
// the place's breadcrumb zooms back out. Server text stays React text nodes.
import { useId, useState } from "react";
import { moveRowFocus, type Tone } from "./kit";
import { plural } from "./OpsShared";
import { ClusterMark, clusterSetLinkText, type ClusterSetLink } from "./ClusterTopology";
import type { ClusterSet, ClusterSetChannel, ClusterSetCluster } from "./clusterApi";
import "./cluster.css";

type ClusterSetStatusEntry = { word: string; shell: string; tone: Tone };
type ChannelDetail = ClusterSetChannel & { status?: string | null; source_uuid?: string | null };
type ClusterDetail = ClusterSetCluster & { super_read_only?: boolean | null };

/** Per-cluster status words in plain words, the MySQL Shell globalStatus term
 * (mysql-shell modules/adminapi/common/cluster_types.cc:155-175) when the
 * displayed state establishes it, and the card's tone (never OK without a read). */
export const CLUSTER_STATUS_WORD: Record<string, ClusterSetStatusEntry> = {
  // MySQL Shell Cluster_global_status values:
  OK: { word: "OK", shell: "OK", tone: "ok" },
  OK_NOT_REPLICATING: { word: "Not replicating", shell: "OK_NOT_REPLICATING", tone: "problem" },
  OK_NOT_CONSISTENT: { word: "Not consistent", shell: "OK_NOT_CONSISTENT", tone: "problem" },
  OK_MISCONFIGURED: { word: "Misconfigured", shell: "OK_MISCONFIGURED", tone: "watch" },
  OK_FENCED_WRITES: { word: "Writes fenced", shell: "OK_FENCED_WRITES", tone: "watch" },
  NOT_OK: { word: "Not OK", shell: "NOT_OK", tone: "problem" },
  INVALIDATED: { word: "Invalidated", shell: "INVALIDATED", tone: "problem" },
  UNKNOWN: { word: "Unknown", shell: "UNKNOWN", tone: "unknown" },
  // Application evidence words (myflames/cluster.py _clusterset_status):
  NOT_REPLICATING: { word: "Not replicating", shell: "", tone: "problem" },
  FENCED_WRITES: { word: "Writes fenced", shell: "OK_FENCED_WRITES", tone: "watch" },
  NOT_READ: { word: "Not read", shell: "", tone: "unknown" },
};

/** The set status word and its tone (design section 2). */
export const SET_STATUS_WORD: Record<string, { word: string; tone: Tone }> = {
  HEALTHY: { word: "Healthy", tone: "ok" },
  AVAILABLE: { word: "Available", tone: "watch" },
  UNAVAILABLE: { word: "Unavailable", tone: "problem" },
  UNKNOWN: { word: "Unknown", tone: "unknown" },
};

/** A cluster's status entry; shows a MySQL Shell globalStatus term only when
 * the observed state establishes that term under Shell's rules
 * (mysql-shell modules/adminapi/cluster_set/cluster_set_impl.cc:506-604,
 * mysqlshdk/libs/mysql/replication.cc:692-736). A report from before
 * cluster-1.1 has no status field, so only an invalidated cluster gets a word. */
export function clusterStatus(c: ClusterSetCluster): ClusterSetStatusEntry {
  const detail = c as ClusterDetail;
  const status: string | undefined = c.status;
  const ch = c.channel as ChannelDetail | null | undefined;
  const chStatus = ch?.status || "";
  const receiver = ch?.receiver_state || "";
  const applier = ch?.applier_state || "";

  if (!status) {
    return c.invalidated ? CLUSTER_STATUS_WORD.INVALIDATED : CLUSTER_STATUS_WORD.NOT_READ;
  }
  if (status === "NOT_REPLICATING") {
    if (chStatus === "MISSING" || ch?.present === false) {
      return { word: "Not replicating", shell: "OK_MISCONFIGURED", tone: "problem" };
    }
    if (chStatus === "MISCONFIGURED") {
      return { word: "Misconfigured", shell: "OK_MISCONFIGURED", tone: "watch" };
    }
    const applierBroken = applier === "OFF" || applier === "ERROR";
    if (chStatus === "CONNECTING" || (receiver === "CONNECTING" && !applierBroken)) {
      return { word: "Connecting", shell: "", tone: "watch" };
    }
    const stoppedOrErrored =
      receiver === "OFF" || receiver === "ERROR" || applierBroken ||
      (receiver === "ON" && !!ch?.last_error_number) ||
      ((chStatus === "STOPPED" || chStatus === "ERROR") && receiver !== "CONNECTING");
    return {
      word: "Not replicating",
      shell: stoppedOrErrored ? "OK_NOT_REPLICATING" : "",
      tone: "problem",
    };
  }
  if (status === "FENCED_WRITES") {
    if (chStatus === "MISCONFIGURED") {
      return { word: "Writes fenced", shell: "", tone: "watch" };
    }
    return CLUSTER_STATUS_WORD.FENCED_WRITES;
  }
  if (status === "NOT_OK") {
    if (c.role === "PRIMARY" && c.quorum === true && c.primary != null && detail.super_read_only === true) {
      return { word: "Not OK", shell: "", tone: "problem" };
    }
    return CLUSTER_STATUS_WORD.NOT_OK;
  }
  if (status === "NOT_READ") {
    if (c.quorum === true && c.primary != null) {
      return { word: "Not read", shell: "", tone: "unknown" };
    }
    return CLUSTER_STATUS_WORD.NOT_READ;
  }
  if (status === "OK") {
    if (chStatus === "MISCONFIGURED") {
      return CLUSTER_STATUS_WORD.OK_MISCONFIGURED;
    }
    // SQL thread state cannot establish Shell's GTID-consistency verdict.
    // The explicit Check action owns any Shell globalStatus it returns.
    if (c.role === "REPLICA") {
      return { word: "Not checked", shell: "", tone: "unknown" };
    }
    return CLUSTER_STATUS_WORD.OK;
  }
  return CLUSTER_STATUS_WORD[status] || (c.invalidated ? CLUSTER_STATUS_WORD.INVALIDATED : CLUSTER_STATUS_WORD.NOT_READ);
}

/** The role as the card names it: an invalidated cluster is that first. */
export function clusterRoleWord(c: ClusterSetCluster): string {
  if (c.invalidated) return "Invalidated";
  return c.role === "PRIMARY" ? "Primary cluster" : c.role === "REPLICA" ? "Replica cluster" : "Cluster";
}

/** Whether live group facts (quorum or member states) were read on this cluster. */
function hasGroupRead(c: ClusterSetCluster): boolean {
  if (c.read !== "this_connection" && c.read !== "route") return false;
  return (c.quorum !== null && c.quorum !== undefined) || (c.members || []).some((m) => m.state !== null);
}

/** How the cluster's facts were read, in one phrase. */
export function provenanceText(c: ClusterSetCluster): string {
  if (c.read === "this_connection") return hasGroupRead(c) ? "This connection" : "Metadata only (this connection)";
  if (c.read === "route") {
    const name = c.route?.profile_name || "a saved connection";
    return hasGroupRead(c) ? `Read through ${name}` : `Metadata only (${name})`;
  }
  if (c.read === "failed") return c.read_error || "The read failed";
  return "Metadata only";
}

/** The arrow to a replica cluster: its own channel when read, else the local
 * member's channel (cluster-1.0 reports) or the bare relation. A misconfigured,
 * missing, stopped or errored channel never draws green. */
export function setLinkText(cs: ClusterSet, c: ClusterSetCluster): ClusterSetLink {
  if (c.invalidated) return { text: "Invalidated · not replicating", tone: "problem", broken: true };
  const rawStatus: string | undefined = c.status;
  const ch = c.channel as ChannelDetail | null | undefined;
  if (!ch) {
    const fallback = clusterSetLinkText(cs, c);
    if (rawStatus === "OK_MISCONFIGURED") {
      return { text: `${fallback.text} · misconfigured`, tone: "problem", broken: true };
    }
    return fallback;
  }
  const chStatus = ch.status || "";
  if (chStatus === "MISSING" || ch.present === false) {
    return { text: "clusterset_replication · missing", tone: "problem", broken: true };
  }
  const receiver = ch.receiver_state || "?";
  const applier = ch.applier_state || "?";
  const error = ch.last_error_number ? ` · error ${ch.last_error_number}` : "";
  const misconfigured = chStatus === "MISCONFIGURED" || rawStatus === "OK_MISCONFIGURED";
  if (misconfigured) {
    const states = ch.receiver_state || ch.applier_state ? ` · receiver ${receiver} · applier ${applier}` : "";
    return { text: `clusterset_replication · misconfigured${states}${error}`, tone: "problem", broken: true };
  }
  const stopped = receiver === "OFF" || applier === "OFF" || chStatus === "STOPPED";
  const errored = receiver === "ERROR" || applier === "ERROR" ||
    (chStatus === "ERROR" && receiver !== "CONNECTING") ||
    (!!ch.last_error_number && receiver !== "CONNECTING");
  const connecting = receiver === "CONNECTING" || chStatus === "CONNECTING";
  const broken = stopped || errored;
  const tone: Tone = broken ? "problem"
    : connecting ? "watch"
      : receiver === "ON" && applier === "ON" && !ch.read_error ? "ok" : "unknown";
  return { text: `clusterset_replication · receiver ${receiver} · applier ${applier}${error}`, tone, broken };
}

/** Resolve a stored target_cluster Group Replication UUID to its cluster name
 * when clusters[].uuid carries a unique match; otherwise keep the stored text. */
function routingTargetLabel(cs: ClusterSet, target: string): string {
  if (!target || target === "primary") return target;
  if (cs.clusters.some((c) => c.name === target)) return target;
  const needle = target.trim().toLowerCase();
  const matches = cs.clusters.filter((c) => typeof c.uuid === "string" && c.uuid.trim().toLowerCase() === needle && c.name);
  return matches.length === 1 ? matches[0].name! : target;
}

/** A status reason behind a Details toggle, the Findings pattern. */
function Reason({ id, text }: { id: string; text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="clusterset-reason">
      <button type="button" className="text-button cluster-finding-toggle" aria-expanded={open} aria-controls={id}
        onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>
        {open ? "Hide details" : "Details"}
      </button>
      <span id={id} className="clusterset-reason-text" hidden={!open}>{text}</span>
    </span>
  );
}

function ClusterCard({ c, onOpen }: { c: ClusterSetCluster; onOpen: (c: ClusterSetCluster) => void }) {
  const base = useId();
  const status = clusterStatus(c);
  const role = clusterRoleWord(c);
  const members = c.members || [];
  const primary = c.primary;
  const read = hasGroupRead(c);
  const label = [c.name, role, status.word, read && members.length ? plural(members.length, "member") : "",
    provenanceText(c)].filter(Boolean).join(", ");
  return (
    <div
      className={`clusterset-cluster is-card${c.local ? " is-local" : ""}${c.invalidated ? " is-invalidated" : ""}`}
      role="button"
      tabIndex={0}
      aria-label={`${label}. Open the cluster`}
      data-cluster={c.name || ""}
      onDoubleClick={() => onOpen(c)}
      onKeyDown={(e) => {
        if (e.currentTarget !== e.target) return;
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(c); return; }
        moveRowFocus(e, ".clusterset-cluster.is-card", ".clusterset-map");
      }}
    >
      <span className="cluster-card-head">
        <span className={`cluster-role is-${c.role === "PRIMARY" ? "primary" : "secondary"}`}>{role}</span>
      </span>
      <strong className="cluster-name" title={c.name || ""}>{c.name || "Name not recorded"}</strong>
      <span className="clusterset-status-word">
        <ClusterMark tone={status.tone} label={status.word} title={c.status_reason || undefined} />
        {status.shell && <span className="clusterset-shell">{status.shell}</span>}
      </span>
      {read && members.length > 0 && (
        <span className="cluster-meta">{plural(members.length, "member")}{primary ? ` · primary ${primary.label || primary.address || "?"}` : ""}</span>
      )}
      <span className="cluster-flow">{provenanceText(c)}</span>
      {c.status_reason && <Reason id={`${base}-why`} text={c.status_reason} />}
      <button type="button" className="text-button clusterset-open" onClick={() => onOpen(c)}>Open</button>
    </div>
  );
}

/** The ClusterSet map: the set status line, the primary cluster, an arrow to
 * each replica cluster, and the stored Router configuration as plain lists. */
export function ClusterSetMap({ cs, onOpen }: {
  cs: ClusterSet;
  onOpen: (c: ClusterSetCluster) => void;
}) {
  const base = useId();
  const [setReasonOpen, setSetReasonOpen] = useState(false);
  const primaries = cs.clusters.filter((c) => c.role === "PRIMARY" && !c.invalidated);
  const others = cs.clusters.filter((c) => !primaries.includes(c));
  const set = cs.status ? SET_STATUS_WORD[cs.status] : undefined;
  const readOn = cs.metadata_read_on
    ? cs.metadata_read_on.self
      ? `Read on ${cs.metadata_read_on.profile_name ? `${cs.metadata_read_on.profile_name} (this connection)` : "this connection"}`
      : `Read on ${cs.metadata_read_on.profile_name || "a saved connection"}`
    : "";
  const copyNote = cs.from_local_copy ? "this cluster's metadata copy, may be behind" : "";
  const routers = cs.routers || [];
  const routingTarget = cs.routing_options?.options?.target_cluster;
  return (
    <section className="clusterset-map" aria-label={`InnoDB ClusterSet ${cs.name || ""}`.trim()}>
      <div className="clusterset-setline">
        <span className="cluster-row-label">ClusterSet {cs.name || "(unnamed)"}</span>
        {set && <ClusterMark tone={set.tone} label={set.word} title={cs.status_reason || undefined} />}
        {cs.status_reason && (
          <button type="button" className="text-button cluster-finding-toggle" aria-expanded={setReasonOpen}
            aria-controls={`${base}-set-why`} onClick={() => setSetReasonOpen((o) => !o)}>
            {setReasonOpen ? "Hide details" : "Details"}
          </button>
        )}
        {(readOn || copyNote) && (
          <span className="clusterset-read-on">
            {readOn}
            {readOn && copyNote ? "; " : ""}
            {copyNote && (
              <span className="clusterset-copy"
                title="A replica cluster's metadata is replicated from the primary cluster and can be behind. Connect to the primary cluster for the current view.">
                {copyNote}
              </span>
            )}
          </span>
        )}
        {cs.status_reason && (
          <span id={`${base}-set-why`} className="clusterset-reason-text" hidden={!setReasonOpen}>
            {cs.status_reason}
          </span>
        )}
      </div>
      <div className="clusterset-row">
        <div className="clusterset-primaries">
          {primaries.length ? primaries.map((c) => (
            <ClusterCard key={c.name || "primary"} c={c} onOpen={onOpen} />
          )) : <div className="clusterset-cluster is-invalidated" role="group" aria-label="No primary cluster">
            <ClusterMark tone="problem" label="No primary cluster in the metadata" /></div>}
        </div>
        <ul className="clusterset-replicas" aria-label="Replica clusters">
          {others.map((c) => {
            const link = setLinkText(cs, c);
            return (
              <li key={c.name || "replica"} className="clusterset-replica">
                <span className={`clusterset-link is-${link.tone}${link.broken ? " is-broken" : ""}`}>
                  <span className="clusterset-arrow" aria-hidden="true" />
                  <span className="clusterset-link-label">
                    {link.tone !== "unknown" && <span className={`cluster-shape is-${link.tone}`} aria-hidden="true" />}
                    {link.text}
                  </span>
                </span>
                <ClusterCard c={c} onOpen={onOpen} />
              </li>
            );
          })}
        </ul>
      </div>
      {routers.length > 0 && (
        <ul className="clusterset-plain" aria-label="Routers, stored configuration">
          {routers.map((r, i) => {
            const rawTarget = r.options && typeof r.options.target_cluster === "string" ? r.options.target_cluster : "";
            const target = rawTarget ? routingTargetLabel(cs, rawTarget) : "";
            return <li key={r.id ?? i}>{r.name || r.address || "Router"}{target ? ` → ${target}` : ""}</li>;
          })}
        </ul>
      )}
      {typeof routingTarget === "string" && routingTarget && (
        <ul className="clusterset-plain" aria-label="Routing options, stored configuration">
          <li>{`Routing options → ${routingTargetLabel(cs, routingTarget)}`}</li>
        </ul>
      )}
    </section>
  );
}
