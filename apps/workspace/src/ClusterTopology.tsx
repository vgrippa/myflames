// ClusterTopology: a static picture of the cluster the connection in use
// belongs to. Every member is a card joined to one line, the group (Galera's
// component, Group Replication's group); InnoDB Cluster routers hang under
// that line. Nothing moves. Each state is drawn three ways, never by color
// alone: the state word beside a dot, the card's edge, and the member's link
// to the group (solid while it takes part, dashed and broken when it does not).
// Queues and flow control are plain text on the card. Selecting a card selects
// the member (or router) in the inspector; arrow keys move between cards.
import type { ReactNode } from "react";
import { Icon } from "./components";
import { moveRowFocus, type Selection, type Tone } from "./kit";
import { engineName } from "./OpsShared";
import {
  KIND_LABEL, memberDetached, stateWord,
  type ClusterMember, type ClusterReport, type ClusterRouter,
} from "./clusterApi";
import "./cluster.css";

const ROLE_WORD: Record<string, string> = { PRIMARY: "Primary", SECONDARY: "Secondary", READ_REPLICA: "Read replica" };
const TONE_WORD: Record<Tone, string> = { ok: "OK", watch: "Watch", problem: "Problem", unknown: "Not judged", info: "No threshold", off: "Off" };

export const memberKey = (m: ClusterMember) => m.key;
export const routerKey = (r: ClusterRouter, i: number) => `router:${r.id ?? i}`;

function count(value: unknown): string {
  return typeof value === "number" ? value.toLocaleString() : "?";
}

/** One line of queue and flow-control text for a card, from measured fields only. */
export function memberFlow(report: ClusterReport, m: ClusterMember): string {
  const mt = m.metrics || {};
  if (report.kind === "galera" || report.kind === "pxc") {
    if (!m.local) return "Queues are read on that node";
    const paused = typeof mt.flow_control_paused === "number" ? `${Math.round(mt.flow_control_paused * 100)}%` : "?";
    return `Recv queue ${count(mt.local_recv_queue)} · send ${count(mt.local_send_queue)} · flow control paused ${paused}`;
  }
  if (!("transactions_in_queue" in mt)) return m.state_source === "metadata" ? "Not in the group" : "No queue figures";
  return `Queue ${count(mt.transactions_in_queue)} · applier ${count(mt.applier_queue)}`;
}

/** The engine and version a card names: the member's own (Group Replication)
 * or this server's (the connected node); unknown for another Galera node. */
export function memberEngine(report: ClusterReport, m: ClusterMember): string {
  const engine = report.kind === "pxc" ? "Percona XtraDB Cluster" : report.engine === "mariadb" ? "MariaDB" : "MySQL";
  if (m.version) return `${engine} ${m.version}`;
  if (m.local) return report.kind === "pxc" ? `${engine} ${report.server_version || ""}`.trim() : engineName(report.engine, report.server_version);
  return engine;
}

export function memberAddress(m: ClusterMember): string {
  if (m.host && m.port) return `${m.host}:${m.port}`;
  if (m.host) return m.address && /:0$/.test(m.address) ? `${m.host} · port not reported` : m.host;
  return m.address || "Address not reported";
}

function groupLine(report: ClusterReport): { text: string; broken: boolean } {
  const c = report.cluster;
  const parts: string[] = [];
  if (report.kind === "galera" || report.kind === "pxc") {
    parts.push(c.component ? `${c.component} component` : "Component unknown");
    if (c.size !== null) parts.push(`${c.size}${c.expected_size ? ` of ${c.expected_size}` : ""} ${(c.expected_size || c.size) === 1 ? "node" : "nodes"}`);
    const paused = report.galera?.metrics?.flow_control_paused;
    if (typeof paused === "number") parts.push(`flow control paused ${Math.round(paused * 100)}% on this node`);
    return { text: parts.join(" · "), broken: c.quorum === false };
  }
  parts.push(c.topology === "multi_primary" ? "Multi-primary group" : c.topology === "single_primary" ? "Single-primary group" : "Group");
  if (c.quorum === true) parts.push("quorum");
  else if (c.quorum === false) parts.push("no quorum");
  else parts.push("quorum not visible from here");
  if (c.failures_tolerated !== null) parts.push(`tolerates ${c.failures_tolerated} ${c.failures_tolerated === 1 ? "failure" : "failures"}`);
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

export function ClusterTopology({ report, selection }: { report: ClusterReport; selection?: Selection }) {
  const line = groupLine(report);
  const routers = report.kind === "innodb_cluster" ? report.routers : [];
  return (
    <figure className="cluster-topology" aria-label={`${KIND_LABEL[report.kind]} topology`}>
      <div className="cluster-map" data-rows="">
        <div className="cluster-group">
        <ul className="cluster-members" aria-label="Members">
          {report.members.map((m) => {
            const detached = memberDetached(m);
            const tone: Tone = m.status;
            const role = m.role ? ROLE_WORD[m.role] || m.role : null;
            const word = stateWord(m);
            return (
              <li key={m.key} className={`cluster-node${detached ? " is-detached" : ""}`}>
                <Card
                  selection={selection}
                  rowKey={m.key}
                  className={`tone-${tone}${detached ? " is-detached" : ""}${m.local ? " is-local" : ""}`}
                  label={[m.name, m.local ? "this connection" : "", role || "", word, TONE_WORD[tone], memberAddress(m)].filter(Boolean).join(", ")}
                >
                  <span className="cluster-card-head">
                    {role ? <span className={`cluster-role is-${(m.role || "").toLowerCase()}`}>{role}</span> : <span className="cluster-role is-node">{report.kind === "galera" || report.kind === "pxc" ? "Node" : "Member"}</span>}
                    {m.local && <span className="cluster-local">This connection</span>}
                  </span>
                  <strong className="cluster-name" title={m.name}>{m.name}</strong>
                  {memberAddress(m) !== m.name && <span className="cluster-meta" title={memberAddress(m)}>{memberAddress(m)}</span>}
                  <span className="cluster-meta" title={memberEngine(report, m)}>{memberEngine(report, m)}</span>
                  <span className={`cluster-state tone-${tone}`}>
                    {detached ? <Icon name="warning" size={13} /> : <span className="cluster-dot" aria-hidden="true" />}
                    {word}
                  </span>
                  <span className="cluster-flow">{memberFlow(report, m)}</span>
                </Card>
                <span className={`cluster-stem${detached ? " is-broken" : ""}`} aria-hidden="true" />
              </li>
            );
          })}
        </ul>
        <div className={`cluster-bus${line.broken ? " is-broken" : ""}`}>
          <span className="cluster-bus-line" aria-hidden="true" />
          <span className="cluster-bus-label">
            {line.broken && <Icon name="warning" size={13} />}
            {line.text}
          </span>
        </div>
        </div>
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
                    <span className="cluster-flow">{ports || "Ports not recorded"}</span>
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
