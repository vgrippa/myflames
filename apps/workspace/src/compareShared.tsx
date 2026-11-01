// Comparison describes a relationship, never a server's health. Keep this
// mapping shared by result rows, inspectors and individual schema changes.
import { useEffect, useId, useRef, type ReactNode } from "react";
import { ActionButton } from "./kit";
import { Icon } from "./components";
import type { CompareStatus } from "./compareApi";

type Status = CompareStatus | "moved";
const STATUS: Record<Status, { label: string; tone: "accent" | "outline" | "muted" | "faint" }> = {
  different: { label: "Different", tone: "accent" },
  only_in_source: { label: "Only in source", tone: "outline" },
  only_in_target: { label: "Only in target", tone: "outline" },
  not_compared: { label: "Not compared", tone: "muted" },
  identical: { label: "Identical", tone: "faint" },
  moved: { label: "Moved", tone: "outline" },
};
export function ComparisonBadge({ status, children, className = "" }: { status: Status; children?: ReactNode; className?: string }) {
  return <span data-comparison-status={status} className={`pill comparison-status comparison-status--${STATUS[status].tone} ${className}`}>
    {children ?? STATUS[status].label}
  </span>;
}

/** One compact bar at every width after a read. The parent closes the setup
 * only after a successful Compare, so failed reads preserve the user's work.
 * Escape stays local to the setup and restores focus to Change. */
export function ComparisonSetup({ className, runClassName, source, target, context, contextText, hasResult, expanded, onExpanded,
  onSwap, busy, disabled, problem, onCompare, children }: {
  className: string; runClassName: string; source: string; target: string; context?: ReactNode; contextText?: string;
  hasResult: boolean; expanded: boolean; onExpanded: (value: boolean) => void; onSwap: () => void;
  busy: boolean; disabled?: boolean; problem: string; onCompare: () => void; children: ReactNode;
}) {
  const id = useId();
  const change = useRef<HTMLButtonElement>(null);
  const actions = useRef<HTMLDivElement>(null);
  // The first result replaces the setup, Compare button included: a keyboard
  // user who pressed it lands on the bar's Compare, not on the page.
  const hadResult = useRef(hasResult);
  useEffect(() => {
    const first = hasResult && !hadResult.current;
    hadResult.current = hasResult;
    const focused = document.activeElement;
    if (first && (!focused || focused === document.body || !focused.isConnected)) {
      actions.current?.querySelector<HTMLButtonElement>(`.${runClassName} button`)?.focus();
    }
  }, [hasResult, runClassName]);
  return <section className={`comparison-setup ${className}`} aria-label="What to compare" onKeyDown={event => {
    if (event.key === "Escape" && hasResult && expanded && !event.defaultPrevented) {
      event.preventDefault(); event.stopPropagation(); onExpanded(false); change.current?.focus();
    }
  }}>
    {hasResult && <div className="comparison-header" title={contextText}>
      <div className="comparison-names">
        <span title={`Source: ${source}`}><b>Source</b> {source}</span>
        <span className="comparison-arrow" aria-hidden="true">→</span>
        <span title={`Target: ${target}`}><b>Target</b> {target}</span>
      </div>
      <div className="comparison-actions" ref={actions}>
        <button type="button" className="button" aria-label="Swap source and target" title="Swap source and target" onClick={onSwap}>
          <Icon name="compare" />Swap
        </button>
        <button type="button" className="button" ref={change} aria-expanded={expanded} aria-controls={id}
          onClick={() => onExpanded(!expanded)}>Change…</button>
        <div className={runClassName}>
          <ActionButton primary icon="compare" busy={busy} busyLabel="Comparing…" disabled={disabled || !!problem}
            title={problem || "Compare the source with the target"} onClick={onCompare}>Compare</ActionButton>
        </div>
      </div>
      <div className="comparison-context visually-hidden">{context}</div>
    </div>}
    <div id={id} hidden={hasResult && !expanded}>{(!hasResult || expanded) && children}</div>
  </section>;
}
