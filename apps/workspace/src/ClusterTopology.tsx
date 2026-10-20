// ClusterTopology: a static picture of the cluster the connection in use
// belongs to. Every group member is a card joined to one line, the group
// (Galera's component, Group Replication's group); a configured Galera node
// that is not in the component stands on that line as a dashed placeholder;
// InnoDB Cluster read replicas hang below it apart from the group, on dashed
// neutral links, and routers under them. Nothing moves. Each state is drawn
// three ways, never by color alone: the state word beside its shape (a check
// for OK, a dot for Watch, a triangle for Problem, a ring for not judged), the
// card's edge, and the member's link to the group (solid while it takes part,
// dashed and broken when it does not). Queues and flow control are plain text
// on the card. Selecting a card selects the member (or router) in the
// inspector; arrow keys move between cards.
import type { ReactNode } from "react";
import { moveRowFocus, type Selection, type Tone } from "./kit";
import { engineName } from "./OpsShared";
import {
  flowPercent, isGalera, isReadReplica, kindLabel, memberDetached, memberNoun, stateWord,
  type ClusterMember, type ClusterReport, type ClusterRouter,
} from "./clusterApi";
import "./cluster.css";

const ROLE_WORD: Record<string, string> = { PRIMARY: "Primary", SECONDARY: "Secondary", READ_REPLICA: "Read replica" };
const TONE_WORD: Record<Tone, string> = { ok: "OK", watch: "Watch", problem: "Problem", unknown: "Not judged", info: "No threshold", off: "Off" };

export const memberKey = (m: ClusterMember) => m.key;
export const routerKey = (r: ClusterRouter, i: number) => `router:${r.id ?? i}`;

/** Glossary lines for the terms the place shows (progressive-ux: on hover
 * and focus, never as visible paragraphs). */
export const GLOSSARY = {
  component: "Primary component: the group of Galera nodes that holds a majority and accepts writes. A node outside it (non-Primary) refuses most queries.",
  flow: "Flow control paused: the share of time replication waited because a node fell behind, since the provider's statistics were last reset.",
  tolerates: "Failures tolerated: how many more members can fail (crash or partition) before the rest lose the majority and stop accepting writes.",
  galeraQueues: "Receive queue: write sets waiting to be applied on this node. Send queue: write sets waiting to be sent.",
  grQueues: "Queue: transactions waiting for certification (conflict checks). Applier: transactions certified and waiting to be applied.",
  ports: "RW: the read-write port, routed to the primary. RO: the read-only port, routed to secondaries. RW split: one port that sends reads and writes apart.",
};

function count(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "?";
}

/** The state's shape and word: a check for OK, a dot for Watch, a triangle
 * for Problem, a ring for not judged. The same mark on cards, in the Members
 * table and in the inspector. */
export function ClusterMark({ tone, label, title }: { tone: Tone; label: string; title?: string }) {
  return (
    <span className={`cluster-mark is-${tone}`} title={title}>
      <span className="cluster-shape" aria-hidden="true" />
      {label}
    </span>
  );
}

/** One line of queue and flow-control text for a card, from measured fields only. */
export function memberFlow(report: ClusterReport, m: ClusterMember): string {
  const mt = m.metrics || {};
  if (isGalera(report)) {
    if (!m.local) return "State and queues are read on that node; connect to it to inspect";
    if (report.galera?.wsrep_on && /^(off|0)$/i.test(report.galera.wsrep_on)) return "Queues are hidden while wsrep_on is OFF";
    return `Receive queue ${count(mt.local_recv_queue)} · send ${count(mt.local_send_queue)} · flow control paused ${flowPercent(mt.flow_control_paused)}`;
  }
  if (isReadReplica(m)) return m.local ? "Replicates asynchronously over read_replica_replication" : "Replicates asynchronously, not a group member";
  if (m.state === "MISSING") return "Not in the group";
  if (!("transactions_in_queue" in mt)) return m.state_source === "metadata" ? "State is read on that member" : "No queue figures";
  return `Queue ${count(mt.transactions_in_queue)} · applier ${count(mt.applier_queue)}`;
}

/** The engine and version a card or row names: the member's own (Group
 * Replication) or this server's (the connected node). Percona XtraDB Cluster
 * is PXC here (the summary names the product); another Galera node's version
 * is not visible, so nothing is shown. */
export function memberEngine(report: ClusterReport, m: ClusterMember): string {
  const engine = report.kind === "pxc" ? "PXC" : report.engine === "mariadb" ? "MariaDB" : "MySQL";
  if (m.version) return `${engine} ${m.version}`;
  if (m.local) return report.kind === "pxc" ? `${engine} ${report.server_version || ""}`.trim() : engineName(report.engine, report.server_version);
  return "";
}

/** The full engine and version, for titles and the inspector. */
export function memberEngineFull(report: ClusterReport, m: ClusterMember): string {
  const short = memberEngine(report, m);
  return report.kind === "pxc" && short ? short.replace(/^PXC/, "Percona XtraDB Cluster") : short;
}

/** host:port, or the host alone when no port is reported (Galera port 0;
 * a note explains it), as the CLI prints it (cluster.address_text). */
export function memberAddress(m: ClusterMember): string {
  if (m.host) return m.port ? `${m.host.includes(":") ? `[${m.host}]` : m.host}:${m.port}` : m.host;
  return m.address || "Address not reported";
}

/** The accessible name of a card: name, marker, role, state, the tone only
 * when the state word does not already say it, the address when it differs. */
export function memberLabel(m: ClusterMember, localWord: string | null): string {
  const role = m.role ? ROLE_WORD[m.role] || m.role : "";
  const word = stateWord(m);
  const address = memberAddress(m);
  return [m.name, m.local && localWord ? localWord.toLowerCase() : "", role !== word ? role : "", word,
    m.status === "unknown" ? "" : TONE_WORD[m.status], address !== m.name ? address : ""].filter(Boolean).join(", ");
}

function nodesText(size: number, expected: number | null, noun: string): string {
  const total = expected && expected !== size ? ` of ${expected}` : "";
  return `${size}${total} ${(expected || size) === 1 ? noun : `${noun}s`}`;
}

function groupLine(report: ClusterReport): { text: string; broken: boolean } {
  const c = report.cluster;
  const parts: string[] = [];
  const tolerance = c.failures_tolerated !== null ? `tolerates ${c.failures_tolerated} ${c.failures_tolerated === 1 ? "failure" : "failures"}` : "";
  if (isGalera(report)) {
    parts.push(c.component ? `${c.component} component` : "Component unknown");
    if (c.size !== null) parts.push(nodesText(c.size, c.expected_size, "node"));
    if (tolerance) parts.push(tolerance);
    return { text: parts.join(" · "), broken: c.quorum === false };
  }
  const me = report.members.find((m) => m.local);
  parts.push(c.topology === "multi_primary" ? "Multi-primary group" : c.topology === "single_primary" ? "Single-primary group" : "Group");
  if (me && isReadReplica(me)) parts.push("not visible from a read replica");
  else if (c.quorum === true) parts.push("quorum");
  else if (c.quorum === false) parts.push("no quorum");
  else parts.push("not visible from this member");
  if (tolerance) parts.push(tolerance);
  return { text: parts.join(" · "), broken: c.quorum === false };
}

function Card({ selection, rowKey, className, label, children }: {
  selection?: Selection; rowKey: string; className: string; label: string; children: ReactNode;
}) {
  const selected = selection?.key === rowKey;
  return (
    <button
      type="button"
      className={`cluster-card ${className}${selected ? " is-selected" : ""}`}
      data-row-key={rowKey}
      data-row-axis="both"
      aria-pressed={selected}
      aria-label={label}
      onClick={() => selection?.select(rowKey, true)}
      onKeyDown={(e) => {
        const next = moveRowFocus(e, "button.cluster-card[data-row-key]", ".cluster-map");
        if (next?.dataset.rowKey) selection?.select(next.dataset.rowKey, false);
      }}
    >
      {children}
    </button>
  );
}

/** The marker on the member this report was read from: "This connection"
 * while the report is the connection in use's, "Read here" on a report kept
 * from another connection or from before a failed read. */
export type LocalMarker = { word: string; title?: string };

function MemberCard({ report, m, selection, marker }: { report: ClusterReport; m: ClusterMember; selection?: Selection; marker: LocalMarker }) {
  const detached = memberDetached(m);
  const tone: Tone = m.status;
  const role = m.role ? ROLE_WORD[m.role] || m.role : null;
  const word = stateWord(m);
  const engine = memberEngine(report, m);
  const flow = memberFlow(report, m);
  const replica = isReadReplica(m);
  return (
    <Card
      selection={selection}
      rowKey={m.key}
      className={`tone-${tone}${detached ? " is-detached" : ""}${m.local ? " is-local" : ""}${replica ? " is-replica" : ""}`}
      label={memberLabel(m, marker.word)}
    >
      <span className="cluster-card-head">
        {role && role !== word ? <span className={`cluster-role is-${(m.role || "").toLowerCase()}`}>{role}</span>
          : <span className="cluster-role is-node">{replica ? "Read replica" : memberNoun(report)}</span>}
      </span>
      <strong className="cluster-name" title={m.name}>{m.name}</strong>
      {memberAddress(m) !== m.name && <span className="cluster-meta" title={memberAddress(m)}>{memberAddress(m)}</span>}
      {engine && <span className="cluster-meta" title={memberEngineFull(report, m)}>{engine}</span>}
      <ClusterMark tone={tone} label={word} />
      <span className="cluster-flow" title={isGalera(report) ? GLOSSARY.galeraQueues : GLOSSARY.grQueues}>{flow}</span>
      {m.local && <span className="cluster-local" title={marker.title}>{marker.word}</span>}
    </Card>
  );
}

export function ClusterTopology({ report, selection, marker = { word: "This connection" } }: {
  report: ClusterReport; selection?: Selection; marker?: LocalMarker;
}) {
  const line = groupLine(report);
  const routers = report.kind === "innodb_cluster" ? report.routers : [];
  const group = report.members.filter((m) => !isReadReplica(m));
  const replicas = report.members.filter(isReadReplica);
  const missing = isGalera(report) ? report.galera?.missing || [] : [];
  const noun = memberNoun(report);
  return (
    <figure className="cluster-topology" aria-label={`${kindLabel(report)} topology`}>
      <div className="cluster-map" data-rows="">
        <div className="cluster-group">
          <ul className="cluster-members" aria-label={`${noun}s`}>
            {group.map((m) => (
              <li key={m.key} className={`cluster-node${memberDetached(m) ? " is-detached" : ""}`}>
                <MemberCard report={report} m={m} selection={selection} marker={marker} />
                <span className={`cluster-stem${memberDetached(m) ? " is-broken" : ""}`} aria-hidden="true" />
              </li>
            ))}
            {missing.map((name) => (
              <li key={`missing:${name}`} className="cluster-node is-detached is-placeholder">
                <div className="cluster-card tone-watch is-detached is-placeholder" role="group"
                  aria-label={`${name}, configured in wsrep_cluster_address, not in the component`}>
                  <span className="cluster-card-head"><span className="cluster-role is-node">{noun}</span></span>
                  <strong className="cluster-name" title={name}>{name}</strong>
                  <ClusterMark tone="watch" label="Not in the component" />
                  <span className="cluster-flow">Named in wsrep_cluster_address</span>
                </div>
                <span className="cluster-stem is-broken" aria-hidden="true" />
              </li>
            ))}
          </ul>
          <div className={`cluster-bus${line.broken ? " is-broken" : ""}`}>
            <span className="cluster-bus-line" aria-hidden="true" />
            <span className="cluster-bus-label" title={isGalera(report) ? GLOSSARY.component : GLOSSARY.tolerates}>
              {line.broken && <span className="cluster-shape is-problem" aria-hidden="true" />}
              {line.text}
            </span>
          </div>
        </div>
        {replicas.length > 0 && (
          <div className="cluster-replicas">
            <span className="cluster-row-label">Read replicas · asynchronous, not group members</span>
            <ul className="cluster-members is-replicas" aria-label="Read replicas">
              {replicas.map((m) => (
                <li key={m.key} className="cluster-node is-replica">
                  <span className="cluster-stem is-async" aria-hidden="true" />
                  <MemberCard report={report} m={m} selection={selection} marker={marker} />
                </li>
              ))}
            </ul>
          </div>
        )}
        {routers.length > 0 && (
          <ul className="cluster-routers" aria-label="Routers">
            {routers.map((r, i) => {
              const key = routerKey(r, i);
              const ports = [r.ports.RWEndpoint && `RW ${r.ports.RWEndpoint}`, r.ports.ROEndpoint && `RO ${r.ports.ROEndpoint}`,
                r.ports.RWSplitEndpoint && `RW split ${r.ports.RWSplitEndpoint}`].filter(Boolean).join(" · ");
              return (
                <li key={key} className="cluster-node is-router">
                  <span className="cluster-stem" aria-hidden="true" />
                  <Card selection={selection} rowKey={key} className="tone-unknown is-router"
                    label={[r.name || r.address || "Router", r.product, r.version, ports].filter(Boolean).join(", ")}>
                    <span className="cluster-card-head"><span className="cluster-role is-router">Router</span></span>
                    <strong className="cluster-name">{r.name || r.address || "Router"}</strong>
                    <span className="cluster-meta">{[r.product, r.version].filter(Boolean).join(" ") || "Version not recorded"}</span>
                    <span className="cluster-flow" title={GLOSSARY.ports}>{ports || "Ports not recorded"}</span>
                  </Card>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </figure>
  );
}
