// The parameter step before a ClusterSet action's review (the Cluster place's
// set level): removal's two variants (removeCluster dissolve true/false) plus
// force for an unreachable target, create replica cluster (target, name,
// recovery method), Router routing (option and value), invalidating
// unreachable replica clusters on a switchover or failover, and force on a
// dissolve. Each sheet only chooses what the backend's plan needs
// (myflames/cluster.py _clusterset_plan_parameters); confirming it starts the
// normal reviewed flow (ActionReview.tsx), which plans from a fresh read.
// Nothing is sent from these sheets. Choices that the catalog disables stay
// visible with their reason on click (WhyDisabled, as in CatalogActions).
import { useId, useRef, useState } from "react";
import { Checkbox, ConfirmSheet, NumberField, Select } from "./kit";
import { WhyDisabled } from "./WhyDisabled";
import type { ClusterAction, ClusterSet } from "./clusterApi";

/** What the set-level action buttons open before review. */
export type ClusterSetFormRequest =
  | { kind: "remove"; name: string }
  | { kind: "create"; action: ClusterAction }
  | { kind: "routing"; action: ClusterAction }
  | { kind: "promote"; action: ClusterAction }
  | { kind: "dissolve"; action: ClusterAction };

/** MySQL Shell's cluster-name rule (mysql-shell modules/adminapi/common/
 * common.cc validate_name): 1-63 characters, starting with a letter or
 * underscore, then letters, digits, '_', '-' or '.'. */
export const CLUSTER_NAME = /^[A-Za-z_][A-Za-z0-9_.-]{0,62}$/;

const REMOVE_CONSEQUENCE = {
  dissolve: "MySQL Shell removes the cluster from this set and dissolves it into standalone instances; user data is kept. This is MySQL Shell's default.",
  keep: "MySQL Shell removes the cluster from this set and keeps it running as a standalone InnoDB Cluster. This introduces divergent transactions, so it cannot simply be re-added to this set.",
};

/** The one-sentence consequence of each force or invalidation choice. */
const FORCE_CONSEQUENCE = {
  remove: "Force removes the cluster without contacting it: its servers can keep stale metadata and replication configuration, which must then be cleaned up by hand.",
  dissolve: "Force dissolves the ClusterSet without reaching every cluster: servers it cannot contact keep stale metadata and replication configuration, which must be cleaned up by hand.",
  invalidate: "Invalidating marks the cluster out of the ClusterSet without contacting it; it cannot take part again until it is repaired or removed.",
};

/** The report shows a cluster as unreachable when every probe of its saved
 * routes failed in this read (cluster.py records read "failed" only when no
 * route answered). The backend re-checks reachability at plan time and stays
 * the arbiter: it refuses force without it and requires it with it. */
function clusterUnreachable(cluster: { read?: string } | undefined): boolean {
  return cluster?.read === "failed";
}

/** The select value a stored target_cluster maps to. MySQL Shell resolves a
 * cluster name to its Group Replication UUID when it stores the option
 * (mysql-shell modules/adminapi/common/router.cc:382-406), so a stored value
 * can be the UUID; the backend's plan accepts only "primary" or a cluster
 * name. When the stored value cannot be mapped to a cluster name, the choice
 * starts empty so the operator must pick primary or a cluster explicitly. */
function targetPrefill(cs: ClusterSet, now: unknown): { value: string; unmapped: string | null } {
  if (now === undefined || now === null || now === "" || now === "primary") {
    return { value: "primary", unmapped: null };
  }
  if (typeof now !== "string") return { value: "", unmapped: String(now) };
  const byName = cs.clusters.some((c) => c.name === now);
  if (byName) return { value: now, unmapped: null };
  const needle = now.trim().toLowerCase();
  const matches = cs.clusters.filter((c) => typeof c.uuid === "string" && c.uuid.trim().toLowerCase() === needle && c.name);
  if (matches.length === 1) return { value: matches[0].name!, unmapped: null };
  return { value: "", unmapped: now };
}

const RECOVERY_CHOICES: { value: "clone" | "incremental"; label: string; text: string }[] = [
  { value: "clone", label: "Clone", text: "The target's data is replaced with a full copy of a donor and the server restarts." },
  { value: "incremental", label: "Incremental", text: "The target applies only the transactions it misses; MySQL Shell refuses when they are no longer available." },
];

/** The routing options this form offers, with the values the backend accepts
 * (cluster.py _shell_set_routing_option). tags and guideline need values this
 * report cannot enumerate, so they stay in MySQL Shell. */
const ROUTING_VALUES: Record<string, [string, string][] | "seconds" | "boolean"> = {
  target_cluster: [], // Filled with the ClusterSet's clusters.
  invalidated_cluster_policy: [["drop_all", "drop_all (refuse connections)"], ["accept_ro", "accept_ro (read-only)"]],
  stats_updates_frequency: "seconds",
  use_replica_primary_as_rw: "boolean",
  read_only_targets: [["all", "all"], ["read_replicas", "read_replicas"], ["secondaries", "secondaries"]],
};

function reason(action: ClusterAction | undefined): string {
  return [action?.why_not, action?.fix].filter(Boolean).join(" ");
}

/** One offered choice (a removal variant, a recovery method): a toggle button
 * with its consequence sentence; a catalog-disabled choice stays visible and
 * opens its reason on click. */
function Choice({ id, label, text, action, selected, onChoose }: {
  id: string;
  label: string;
  text: string;
  /** The catalog entry behind the choice; undefined or disabled keeps the
   * choice visible with its reason on click. */
  action?: ClusterAction;
  selected: boolean;
  onChoose: () => void;
}) {
  const anchor = useRef<HTMLButtonElement | null>(null);
  const [whyOpen, setWhyOpen] = useState(false);
  const enabled = !!action?.enabled;
  return (
    <div className="clusterset-choice" data-choice={id}>
      <button type="button" className="button" ref={anchor}
        aria-pressed={enabled ? selected : undefined} aria-disabled={enabled ? undefined : true}
        onClick={() => { if (enabled) onChoose(); }}>
        {label}
      </button>
      <p className="hint">{text}</p>
      {!enabled && (
        <WhyDisabled anchor={anchor} open={whyOpen} label={`Why ${label} is unavailable`}
          onOpen={() => setWhyOpen(true)} onClose={() => setWhyOpen(false)}>
          <p>{reason(action) || "This choice is not available now."}</p>
        </WhyDisabled>
      )}
    </div>
  );
}

/** A valid, unused suggestion for the new replica cluster's name. */
export function suggestClusterName(cs: ClusterSet): string {
  const taken = new Set(cs.clusters.map((c) => (c.name || "").toLowerCase()));
  const base = (cs.name || "cluster").replace(/[^A-Za-z0-9_.-]/g, "-").replace(/^[^A-Za-z_]+/, "") || "cluster";
  for (let n = 1; ; n++) {
    const suffix = `-replica${n}`;
    const name = `${base.slice(0, 63 - suffix.length)}${suffix}`;
    if (!taken.has(name.toLowerCase())) return name;
  }
}

function RemoveChoice({ name, catalog, cs, onCancel, onReview }: {
  name: string; catalog: ClusterAction[]; cs: ClusterSet;
  onCancel: () => void; onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  const forceId = useId();
  const dissolve = catalog.find((a) => a.id === `remove_cluster:${name}`);
  const keep = catalog.find((a) => a.id === `remove_cluster_keep:${name}`);
  const [chosen, setChosen] = useState<ClusterAction | null>(null);
  // Force is offered only when this read could not reach the cluster at all;
  // the backend plans it only then too (cluster.py _plan_clusterset).
  const unreachable = clusterUnreachable(cs.clusters.find((c) => c.name === name));
  const [force, setForce] = useState(false);
  return (
    <ConfirmSheet className="clusterset-form" eyebrow="ClusterSet.removeCluster"
      title={`Remove ${name} from the ClusterSet`}
      description="Choose how MySQL Shell removes the replica cluster; the next step is the normal review with the exact call."
      confirmTone="primary" confirmLabel="Review the removal…" confirmDisabled={!chosen}
      onCancel={onCancel} onConfirm={() => { if (chosen) onReview(chosen, force ? { force: true } : {}); }}>
      <div className="clusterset-choices" role="group" aria-label="How to remove the cluster">
        <Choice id="dissolve" label="Remove and dissolve the cluster" text={REMOVE_CONSEQUENCE.dissolve}
          action={dissolve} selected={chosen === dissolve} onChoose={() => setChosen(dissolve || null)} />
        <Choice id="keep" label="Remove, keep the cluster running" text={REMOVE_CONSEQUENCE.keep}
          action={keep} selected={chosen === keep} onChoose={() => setChosen(keep || null)} />
      </div>
      {unreachable && (
        <div className="clusterset-field">
          <Checkbox id={forceId} checked={force} onChange={setForce}>
            Force: remove without contacting {name}
          </Checkbox>
          <p className="hint">{FORCE_CONSEQUENCE.remove}</p>
        </div>
      )}
    </ConfirmSheet>
  );
}

function CreateForm({ action, catalog, cs, onCancel, onReview }: {
  action: ClusterAction; catalog: ClusterAction[]; cs: ClusterSet;
  onCancel: () => void; onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  const nameId = useId();
  // One catalog entry per offered target (a saved connection in the active
  // group that is not in any cluster); the target is chosen from this list,
  // never typed.
  const targets = catalog.filter((a) => a.enabled && /^create_replica_cluster:.+$/.test(a.id));
  const [targetId, setTargetId] = useState(action.id);
  const [name, setName] = useState(() => suggestClusterName(cs));
  const [method, setMethod] = useState<"" | "clone" | "incremental">("");
  const target = targets.find((a) => a.id === targetId) || action;
  const nameOk = CLUSTER_NAME.test(name) && !cs.clusters.some((c) => (c.name || "").toLowerCase() === name.toLowerCase());
  const ready = nameOk && method !== "";
  const targetLabel = (a: ClusterAction) => a.label.replace(/^Create replica cluster on /, "").replace(/…$/, "");
  return (
    <ConfirmSheet className="clusterset-form" eyebrow="ClusterSet.createReplicaCluster"
      title="Create a replica cluster"
      description="The target is a saved connection of this group that is not in any cluster; the next step is the normal review with the exact call."
      confirmTone="primary" confirmLabel="Review the change…" confirmDisabled={!ready}
      onCancel={onCancel} onConfirm={() => { if (ready) onReview(target, { name, recoveryMethod: method }); }}>
      <label className="clusterset-field">
        <span>Target (a saved connection of this group)</span>
        <Select value={target.id} options={targets.map((a) => [a.id, targetLabel(a)] as [string, string])}
          onChange={setTargetId} />
      </label>
      <label className="clusterset-field" htmlFor={nameId}>
        <span>New cluster name</span>
        <input id={nameId} autoComplete="off" spellCheck={false} value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      {!nameOk && (
        <p className="hint">Use at most 63 letters, digits, underscores, hyphens or dots, starting with a letter or underscore, and not the name of a cluster already in this set.</p>
      )}
      <fieldset className="clusterset-field clusterset-recovery">
        <legend>Recovery method (MySQL Shell requires an explicit choice)</legend>
        <div className="clusterset-choices" role="group" aria-label="Recovery method">
          {RECOVERY_CHOICES.map((c) => (
            <div key={c.value} className="clusterset-choice" data-choice={c.value}>
              <button type="button" className="button" aria-pressed={method === c.value} onClick={() => setMethod(c.value)}>
                {c.label}
              </button>
              <p className="hint">{c.text}</p>
            </div>
          ))}
        </div>
      </fieldset>
    </ConfirmSheet>
  );
}

/** The value an option's control starts with: the stored value when read,
 * else the option's default. */
function prefillRouting(option: string, now: unknown): string {
  if (typeof now === "string" && now) return now;
  if (option === "invalidated_cluster_policy") return "drop_all";
  if (option === "read_only_targets") return "secondaries";
  return "primary";
}

function RoutingForm({ action, cs, onCancel, onReview }: {
  action: ClusterAction; cs: ClusterSet;
  onCancel: () => void; onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  const valueId = useId();
  // The catalog entry id carries the Router's address::name; without one the
  // entry changes the ClusterSet defaults.
  const router = action.id.includes(":") ? action.id.slice(action.id.indexOf(":") + 1) : "";
  const stored = (router
    ? (cs.routers || []).find((r) => r.name === router)?.options
    : cs.routing_options?.options) || undefined;
  const storedFor = (name: string): unknown => (stored ? stored[name] : undefined);
  const gates = action.options || {};
  const offered = Object.keys(ROUTING_VALUES).filter((name) => gates[name]?.enabled);
  const [option, setOption] = useState(offered[0] || "");
  const baseTargetOptions: [string, string][] = [
    ["primary", "primary (follows the primary cluster)"],
    ...cs.clusters.filter((c) => c.name).map((c) => [c.name!, c.name!] as [string, string]),
  ];
  const firstNow = storedFor(offered[0] || "");
  const firstTarget = targetPrefill(cs, firstNow);
  const [value, setValue] = useState<string>(() =>
    (offered[0] === "target_cluster" ? firstTarget.value : prefillRouting(offered[0] || "", firstNow)));
  /** A stored target_cluster this report cannot map to a cluster name. */
  const [unmappedTarget, setUnmappedTarget] = useState<string | null>(() =>
    (offered[0] === "target_cluster" ? firstTarget.unmapped : null));
  const targetOptions: [string, string][] = value === ""
    ? [["", "Choose a target cluster…"], ...baseTargetOptions]
    : baseTargetOptions;
  const [seconds, setSeconds] = useState<string>(() => {
    const now = storedFor(option);
    return typeof now === "number" ? String(now) : "0";
  });
  const [flag, setFlag] = useState<boolean>(() => storedFor(option) === true);
  const pick = (next: string) => {
    setOption(next);
    const now = storedFor(next);
    if (next === "target_cluster") {
      const target = targetPrefill(cs, now);
      setValue(target.value);
      setUnmappedTarget(target.unmapped);
    } else {
      setValue(prefillRouting(next, now));
      setUnmappedTarget(null);
    }
    setSeconds(typeof now === "number" ? String(now) : "0");
    setFlag(now === true);
  };
  const parameters = (): Record<string, unknown> | null => {
    if (option === "stats_updates_frequency") {
      if (!/^\d+$/.test(seconds) || Number(seconds) > 2147483647) return null;
      return { option, value: Number(seconds) };
    }
    if (option === "use_replica_primary_as_rw") return { option, value: flag };
    if (option === "target_cluster") {
      // Only "primary" or a cluster name of this set is ever sent; a stored
      // group UUID is mapped back to its name (or shown as unmapped), never sent.
      const known = value === "primary" || cs.clusters.some((c) => c.name === value);
      return known ? { option, value } : null;
    }
    if (option) return value ? { option, value } : null;
    return null;
  };
  const ready = !!option && parameters() !== null;
  return (
    <ConfirmSheet className="clusterset-form" eyebrow="ClusterSet.setRoutingOption"
      title={router ? `Change routing for ${router}` : "Change ClusterSet routing defaults"}
      description="This changes stored configuration; Routers apply it on their next refresh. The next step is the normal review with the exact call."
      confirmTone="primary" confirmLabel="Review the change…" confirmDisabled={!ready}
      onCancel={onCancel} onConfirm={() => { const p = parameters(); if (p) onReview(action, p); }}>
      {offered.length === 0 ? (
        <p className="hint">No routing option is offered with this MySQL Shell version.</p>
      ) : (
        <>
          <label className="clusterset-field">
            <span>Routing option</span>
            <Select value={option} options={offered.map((name) => [name, name] as [string, string])} onChange={pick} />
          </label>
          {option === "target_cluster" && (
            <label className="clusterset-field" htmlFor={valueId}>
              <span>Target cluster</span>
              <Select id={valueId} value={value} options={targetOptions} onChange={setValue} />
            </label>
          )}
          {option === "target_cluster" && unmappedTarget && (
            <p className="hint">The stored target is {unmappedTarget}, which this report cannot match to a cluster name. Choose primary or a cluster by name to change it.</p>
          )}
          {option === "invalidated_cluster_policy" && (
            <label className="clusterset-field" htmlFor={valueId}>
              <span>Invalidated cluster policy</span>
              <Select id={valueId} value={value} options={ROUTING_VALUES.invalidated_cluster_policy as [string, string][]} onChange={setValue} />
            </label>
          )}
          {option === "read_only_targets" && (
            <label className="clusterset-field" htmlFor={valueId}>
              <span>Read-only targets</span>
              <Select id={valueId} value={value} options={ROUTING_VALUES.read_only_targets as [string, string][]} onChange={setValue} />
            </label>
          )}
          {option === "stats_updates_frequency" && (
            <label className="clusterset-field" htmlFor={valueId}>
              <span>Stats updates frequency</span>
              <NumberField id={valueId} value={seconds} min={0} max={2147483647} step={1} unit="s" onChange={setSeconds} />
            </label>
          )}
          {option === "use_replica_primary_as_rw" && (
            <Checkbox id={valueId} checked={flag} onChange={setFlag}>
              Send writes to a replica cluster's primary
            </Checkbox>
          )}
        </>
      )}
    </ConfirmSheet>
  );
}

/** Switchover (set_primary_cluster) or failover (force_primary_cluster): the
 * only choice is which unreachable replica clusters to invalidate, offered
 * only when the report shows one (the backend plans invalidation only for a
 * replica confirmed unreachable). */
function PromoteForm({ action, cs, onCancel, onReview }: {
  action: ClusterAction; cs: ClusterSet;
  onCancel: () => void; onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  const failover = action.id.startsWith("force_primary_cluster");
  const name = action.id.includes(":") ? action.id.slice(action.id.indexOf(":") + 1) : (action.cluster_name || "");
  const candidates = cs.clusters.filter((c) =>
    c.role === "REPLICA" && !c.invalidated && !!c.name && c.name !== name && clusterUnreachable(c));
  const [invalidating, setInvalidating] = useState<Set<string>>(new Set());
  const toggle = (cluster: string, on: boolean) =>
    setInvalidating((cur) => { const next = new Set(cur); if (on) next.add(cluster); else next.delete(cluster); return next; });
  const parameters = (): Record<string, unknown> =>
    invalidating.size ? { invalidateReplicaClusters: [...invalidating] } : {};
  return (
    <ConfirmSheet className="clusterset-form"
      eyebrow={failover ? "ClusterSet.forcePrimaryCluster" : "ClusterSet.setPrimaryCluster"}
      title={failover ? `Fail over the ClusterSet to ${name}` : `Make ${name} the primary cluster`}
      description={failover
        ? "MySQL Shell promotes the target without the old primary and invalidates the old primary in metadata; the next step is the normal review with the exact call."
        : "MySQL Shell synchronizes the target with the current primary, promotes it and repoints replica channels; the next step is the normal review with the exact call."}
      confirmTone="primary" confirmLabel="Review the change…"
      onCancel={onCancel} onConfirm={() => onReview(action, parameters())}>
      {candidates.length === 0 ? (
        <p className="hint">No replica cluster is unreachable in this read, so no invalidation is offered.</p>
      ) : (
        <fieldset className="clusterset-field">
          <legend>Unreachable replica clusters</legend>
          {candidates.map((c) => (
            <div key={c.name} className="clusterset-choice" data-choice={`invalidate:${c.name}`}>
              <Checkbox checked={invalidating.has(c.name!)} onChange={(on) => toggle(c.name!, on)}>
                Invalidate {c.name}
              </Checkbox>
              <p className="hint">{FORCE_CONSEQUENCE.invalidate}</p>
            </div>
          ))}
        </fieldset>
      )}
    </ConfirmSheet>
  );
}

/** Dissolve the whole ClusterSet: the only choice is force, offered only when
 * this read shows a cluster it could not reach (the backend plans force only
 * then). */
function DissolveForm({ action, cs, onCancel, onReview }: {
  action: ClusterAction; cs: ClusterSet;
  onCancel: () => void; onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  const forceId = useId();
  const unreachable = cs.clusters.filter((c) => clusterUnreachable(c));
  const [force, setForce] = useState(false);
  return (
    <ConfirmSheet className="clusterset-form" eyebrow="ClusterSet.dissolve"
      title={`Dissolve ClusterSet ${cs.name || ""}`}
      description="MySQL Shell drops the ClusterSet metadata and dissolves its clusters while keeping user data; the next step is the normal review with the exact call."
      confirmTone="primary" confirmLabel="Review the dissolve…"
      onCancel={onCancel} onConfirm={() => onReview(action, force ? { force: true } : {})}>
      {unreachable.length > 0 && (
        <div className="clusterset-field">
          <Checkbox id={forceId} checked={force} onChange={setForce}>
            Force: dissolve without reaching {unreachable.map((c) => c.name || "?").join(", ")}
          </Checkbox>
          <p className="hint">{FORCE_CONSEQUENCE.dissolve}</p>
        </div>
      )}
    </ConfirmSheet>
  );
}

export function ClusterSetActionForm({ request, catalog, cs, onCancel, onReview }: {
  request: ClusterSetFormRequest;
  /** The set-level catalog entries (to resolve a choice's action and gates). */
  catalog: ClusterAction[];
  cs: ClusterSet;
  onCancel: () => void;
  onReview: (action: ClusterAction, parameters: Record<string, unknown>) => void;
}) {
  if (request.kind === "remove") return <RemoveChoice name={request.name} catalog={catalog} cs={cs} onCancel={onCancel} onReview={onReview} />;
  if (request.kind === "create") return <CreateForm action={request.action} catalog={catalog} cs={cs} onCancel={onCancel} onReview={onReview} />;
  if (request.kind === "promote") return <PromoteForm action={request.action} cs={cs} onCancel={onCancel} onReview={onReview} />;
  if (request.kind === "dissolve") return <DissolveForm action={request.action} cs={cs} onCancel={onCancel} onReview={onReview} />;
  return <RoutingForm action={request.action} cs={cs} onCancel={onCancel} onReview={onReview} />;
}
