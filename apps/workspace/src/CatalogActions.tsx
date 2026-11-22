// One action group for every backend action catalog (Cluster members and
// overview, Readyset server and rows): every action the backend lists,
// always. A disabled action stays visible, focusable and muted with
// aria-disabled (never the disabled attribute), its why_not and fix as
// visible text under the group (its accessible description, tied by
// aria-describedby). The client-side block (a stale report, a read-only
// connection) wins over catalog state and disables everything with one
// line; a disabled or blocked action sends nothing on click, Enter or
// Space. Reasons are React text nodes only: server text can never become
// markup (S8, S10).
import { Fragment, useId, type ReactNode } from "react";
import "./catalog-actions.css";

/** The fields every action catalog entry shares (cluster-action-1.1,
 * readyset-1.0); place-specific entries extend this. */
export type CatalogAction = {
  id: string;
  label: string;
  scope: string;
  danger: boolean;
  description: string;
  enabled: boolean;
  why_not: string | null;
  fix: string | null;
};

/** Per-action extras a place renders inside the reason block (Cluster's
 * Edit group, Use <connection>, GRANT Commands). */
export type ActionSlot = {
  /** Shown instead of the reason for an enabled action (a member note). */
  note?: string;
  /** Extra condition that forces the reason block and aria-describedby. */
  hasReason?: boolean;
  /** Rendered inside the reason block, after the why_not/fix sentence. */
  content?: ReactNode;
};

export function ActionGroup<A extends CatalogAction>({
  list,
  label,
  blocked,
  planning,
  busy,
  onStart,
  slot,
  idBase,
}: {
  list: A[];
  label: string;
  /** Why every change is blocked regardless of eligibility (a stale report, a read-only connection). */
  blocked: string;
  planning: string;
  busy: boolean;
  onStart: (a: A) => void;
  slot?: (a: A) => ActionSlot | null;
  /** Overrides the generated id prefix so a caller can point another control's
   * aria-describedby at an entry's reason block (Readyset's allowlist chips). */
  idBase?: string;
}) {
  const generatedBase = useId();
  const base = idBase || generatedBase;
  // The client-side block is the first thing every button's description names
  // while it holds: it wins over catalog state.
  const blockedId = `${base}-blocked`;
  return (
    <div className="review-actions" role="group" aria-label={label}>
      {list.map((a, i) => {
        const off = !!blocked || busy || !a.enabled;
        const reasonId = `${base}-why-${i}`;
        const extra = slot ? slot(a) : null;
        const hasReason = !a.enabled && !!(a.why_not || a.fix || extra?.hasReason);
        const note = extra?.note;
        const explanation = [...new Set([a.why_not, a.fix].filter(Boolean))].join(" ");
        const describedBy = blocked
          ? hasReason || note
            ? `${blockedId} ${reasonId}`
            : blockedId
          : hasReason || note
            ? reasonId
            : undefined;
        return (
          <Fragment key={a.id}>
            <button type="button" className={`button${a.danger ? " danger" : ""}`} title={blocked || a.description}
              aria-disabled={off} aria-describedby={describedBy}
              onClick={() => { if (!off) onStart(a); }}>
              {planning === a.id ? "Reading…" : a.label}
            </button>
            {note && <div className="cluster-action-why" id={reasonId}><p>{note}</p></div>}
            {hasReason && (
              <div className="cluster-action-why" id={reasonId}>
                {explanation && <p>{explanation}</p>}
                {extra?.content}
              </div>
            )}
          </Fragment>
        );
      })}
      {blocked && <p className="hint" id={blockedId}>{blocked}</p>}
    </div>
  );
}
