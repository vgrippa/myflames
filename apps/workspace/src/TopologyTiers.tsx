// One tier module for the Readyset topology, the ProxySQL topology and the
// upstream Readyset/ProxySQL tiers drawn above the Cluster and Replication
// topologies: the Cluster card anatomy (role badge, name, meta, state mark,
// one flow line, "This connection" last) on fixed-width cards, tiers as
// labelled rows, and the links between tiers as vertical stems with the label
// on a plate beside the line. States are honest (S11): a stem is active or a
// mark is OK only on a reported running state, a reported error or stop draws
// the stem broken or dashed, and a state the server did not report reads
// "Not read", never OK. Server values render as React text inside <bdi>.
import type { ReactNode } from "react";
import type { Tone } from "./kit";
import { ServerGlyph } from "./TopologyPresentation";
import "./cluster.css";
import "./readyset.css";

/** A saved connection a card offers to switch to: the profile id the backend
 * put on the node (or a connection_group.suggestions entry). Never an address
 * matched in TypeScript (S12). */
export type TierUse = { id: string; name: string };

/** The link between two tiers: running only on a reported running state,
 * error or stopped on a reported error or stop, unread when nothing was read. */
export type TierLinkState = "running" | "error" | "stopped" | "unread";

/** A node's stem up to the tier line: active on a reported healthy state,
 * broken when the node does not take part, async for asynchronous replicas. */
export type TierStem = "active" | "broken" | "async" | "plain" | null;

/** S12: what a backend profile match proves. Identity evidence (the server's
 * own uuid) reads "Verified"; a replication channel matched to the upstream by
 * host:port alone ("channel_source", topology_links.py) is an address match
 * and is labelled as such, never Verified. */
const IDENTITY_EVIDENCE = new Set(["server_uuid"]);
export function matchEvidenceLabel(evidence: string | null | undefined): "Verified" | "Matched by address" {
  return evidence && IDENTITY_EVIDENCE.has(evidence) ? "Verified" : "Matched by address";
}

/** The state shape and word, as on Cluster cards: a check for OK, a dot for
 * Watch, a triangle for Problem, a ring for not judged, never color alone. */
export function TierMark({ tone, label, title }: { tone: Tone; label: ReactNode; title?: string }) {
  return (
    <span className={`cluster-mark is-${tone}`} title={title}>
      <span className="cluster-shape" aria-hidden="true" />
      {typeof label === "string" ? <bdi>{label}</bdi> : label}
    </span>
  );
}

/** One card of a tier row: the Cluster MemberCard anatomy. A card with
 * onSelect is a button that selects its row; without it, a plain group. */
export function TierCard({
  role,
  name,
  metas = [],
  tone,
  mark,
  markTitle,
  flow,
  local = null,
  detached = false,
  selected = false,
  rowKey,
  onSelect,
  label,
  hasUse = false,
}: {
  /** The badge in the card head (a fixed role word; server values inside it
   * sit in <bdi>). */
  role: ReactNode;
  name: string;
  /** One line each: the address when it differs from the name, then product
   * and version. Strings render bidi-isolated. */
  metas?: string[];
  tone: Tone;
  /** The state words beside the shape; "Not read" when nothing was reported. */
  mark: ReactNode;
  markTitle?: string;
  flow?: ReactNode;
  /** The "This connection" chip, last on the card. */
  local?: string | null;
  detached?: boolean;
  selected?: boolean;
  rowKey?: string;
  onSelect?: (key: string) => void;
  label?: string;
  hasUse?: boolean;
}) {
  const className = `cluster-card tier-card tone-${tone}${detached ? " is-detached" : ""}${selected ? " is-selected" : ""}${hasUse ? " has-use" : ""}`;
  const body = (
    <>
      <span className="cluster-card-head">
        <ServerGlyph />
        <span className="cluster-role is-node">{role}</span>
      </span>
      <strong className="cluster-name" title={name}>
        <bdi>{name}</bdi>
      </strong>
      {metas.map((meta, i) => (
        <span key={i} className="cluster-meta" title={meta}>
          <bdi>{meta}</bdi>
        </span>
      ))}
      <TierMark tone={tone} label={mark} title={markTitle} />
      {flow !== null && flow !== undefined && flow !== "" && (
        <span className="cluster-flow">{typeof flow === "string" ? <bdi>{flow}</bdi> : flow}</span>
      )}
      {local && <span className="cluster-local">{local}</span>}
    </>
  );
  if (onSelect && rowKey) {
    return (
      <button
        type="button"
        className={className}
        data-row-key={rowKey}
        aria-pressed={selected}
        aria-label={label}
        onClick={() => onSelect(rowKey)}
      >
        {body}
      </button>
    );
  }
  return (
    <div className={className} role="group" aria-label={label}>
      {body}
    </div>
  );
}

/** One node of a tier row: the card, its stem up to the tier line, and the
 * "Use <connection>" switch inside the card when the backend named a saved
 * connection for the node. */
export function TierNode({
  stem = null,
  detached = false,
  use = null,
  onSwitchConnection,
  children,
}: {
  stem?: TierStem;
  detached?: boolean;
  use?: TierUse | null;
  onSwitchConnection?: (profileId: string) => void;
  children: ReactNode;
}) {
  return (
    <li className={`cluster-node tier-node${detached ? " is-detached" : ""}`}>
      {stem && (
        <span
          className={`cluster-stem${stem === "active" ? " is-active" : stem === "broken" ? " is-broken" : stem === "async" ? " is-async" : ""}`}
          aria-hidden="true"
        />
      )}
      {children}
      {use && onSwitchConnection && (
        <button type="button" className="text-button cluster-use-node" onClick={() => onSwitchConnection(use.id)}>
          Use <bdi>{use.name}</bdi>
        </button>
      )}
    </li>
  );
}

/** One tier: a row label with its jump buttons, then the cards on their
 * stems. With more than one card a bus line joins the stems, as the Cluster
 * group line does. */
export function TierRow({
  label,
  jumps = null,
  bus = false,
  ariaLabel,
  children,
}: {
  label: ReactNode;
  jumps?: ReactNode;
  bus?: boolean;
  ariaLabel?: string;
  children: ReactNode;
}) {
  return (
    <div className="tier-row" role="group" aria-label={ariaLabel || (typeof label === "string" ? label : undefined)}>
      <div className="tier-row-head">
        <span className="cluster-row-label">{label}</span>
        {jumps && <span className="tier-row-jumps">{jumps}</span>}
      </div>
      <div className="tier-group">
        {bus && (
          <div className="cluster-bus tier-bus" aria-hidden="true">
            <span className="cluster-bus-line" />
          </div>
        )}
        <ul className="cluster-members tier-members">{children}</ul>
      </div>
    </div>
  );
}

/** The link between two tiers: a vertical stem, its label on a plate beside
 * the line, never crossing it, and readable by screen readers. */
export function TierLink({ state, children }: { state: TierLinkState; children: ReactNode }) {
  return (
    <div className={`tier-link is-${state}`}>
      <span
        className={`cluster-stem tier-link-stem${state === "running" ? " is-active" : state === "error" ? " is-broken" : ""}`}
        aria-hidden="true"
      />
      <span className="cluster-bus-label tier-link-plate">{children}</span>
    </div>
  );
}
