// One action group for every backend action catalog (Cluster members and
// overview, Readyset server and rows): every action the backend lists,
// always. A disabled action stays visible, focusable and muted with
// aria-disabled (never the disabled attribute), its why_not and fix in a
// visually hidden description (still the button's aria-describedby, so a
// screen reader hears the reason on focus) and in a message anchored to the
// button that click, Enter or Space opens (WhyDisabled.tsx): never a caption
// under the buttons. The client-side block (a stale report, a read-only
// connection) wins over catalog state and disables everything with one
// visible line under the group; a blocked action's message leads with that
// line. A disabled or blocked action sends nothing on click, Enter or Space,
// and a busy group shows nothing on click. Reasons are React text nodes
// only: server text can never become markup (S8, S10).
import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { WhyDisabled } from "./WhyDisabled";
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

/** Per-action extras a place renders inside the message (Cluster's
 * Edit group, Use <connection>, GRANT Commands). */
export type ActionSlot = {
  /** Shown instead of the reason for an enabled action (a member note):
   * visible text under the group, not a message. */
  note?: string;
  /** Extra condition that forces the reason and aria-describedby. */
  hasReason?: boolean;
  /** Rendered inside the message, after the why_not/fix sentence. */
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
  /** The action whose message is open, with the text it opened with. */
  const [openWhy, setOpenWhy] = useState<{ id: string; text: string } | null>(null);
  // One stable ref object per action: WhyDisabled resolves .current at effect
  // time (refs attach after the render that passes them).
  const buttons = useRef(new Map<string, { current: HTMLButtonElement | null }>());
  const buttonRef = (id: string) => {
    let ref = buttons.current.get(id);
    if (!ref) { ref = { current: null }; buttons.current.set(id, ref); }
    return ref;
  };

  const explanationOf = (a: A) => [...new Set([a.why_not, a.fix].filter(Boolean))].join(" ");
  /** The message's sentences: the client block first, then the action's own reason. */
  const messageText = (a: A) => [blocked, a.enabled ? "" : explanationOf(a)].filter(Boolean).join(" ");

  // A new report closes an open message when its action is now enabled or its
  // text changed. The caller remounts this per connection, so a message never
  // carries over to another cluster.
  useEffect(() => {
    if (!openWhy) return;
    const a = list.find((x) => x.id === openWhy.id);
    if (!a) { setOpenWhy(null); return; }
    const extra = slot ? slot(a) : null;
    const hasReason = !a.enabled && !!(a.why_not || a.fix || extra?.hasReason);
    if ((!blocked && !hasReason) || messageText(a) !== openWhy.text) setOpenWhy(null);
  });

  return (
    <div className="review-actions" role="group" aria-label={label}>
      {list.map((a, i) => {
        const off = !!blocked || busy || !a.enabled;
        const reasonId = `${base}-why-${i}`;
        const extra = slot ? slot(a) : null;
        const hasReason = !a.enabled && !!(a.why_not || a.fix || extra?.hasReason);
        const note = extra?.note;
        const explanation = explanationOf(a);
        const describedBy = blocked
          ? hasReason || note
            ? `${blockedId} ${reasonId}`
            : blockedId
          : hasReason || note
            ? reasonId
            : undefined;
        return (
          <Fragment key={a.id}>
            <button type="button" className={`button${a.danger ? " danger" : ""}`}
              title={blocked || (a.enabled ? a.description : explanation || a.description)}
              ref={buttonRef(a.id)}
              aria-disabled={off} aria-describedby={describedBy}
              onClick={() => { if (!off) onStart(a); }}>
              {planning === a.id ? "Reading…" : a.label}
            </button>
            {(!!blocked || hasReason) && (
              <WhyDisabled anchor={buttonRef(a.id)} open={openWhy?.id === a.id}
                mayOpen={off && !busy}
                onOpen={() => setOpenWhy({ id: a.id, text: messageText(a) })}
                onClose={() => setOpenWhy((cur) => (cur?.id === a.id ? null : cur))}>
                {blocked && <p>{blocked}</p>}
                {hasReason && explanation && <p>{explanation}</p>}
                {hasReason && extra?.content}
              </WhyDisabled>
            )}
            {note && <div className="cluster-action-why" id={reasonId}><p>{note}</p></div>}
            {hasReason && (
              <div className="visually-hidden" id={reasonId}>
                {explanation && <p>{explanation}</p>}
              </div>
            )}
          </Fragment>
        );
      })}
      {blocked && <p className="hint" id={blockedId}>{blocked}</p>}
    </div>
  );
}
