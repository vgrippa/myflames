// Query digest's time range picker: one toolbar button ("Any time", "Last
// hour", "Oct 31, 9:00 AM to 10:00 AM") that opens a small dialog with quick
// ranges and From and To fields. The fields are native datetime-local inputs
// (the system calendar with date and time, the keyboard and screen readers
// as the platform gives them), in the report's own clock, which the dialog
// names. Nothing reads here: the view decides what a range does.
import { useId, useState } from "react";
import { Icon } from "./components";
import { Popover, closePopovers } from "./kit";
import {
  ANY_TIME, QUICK_RANGES, inputValue, quickRange, rangeActive, rangeProblem, rangeText, wallText,
  type QuickRange, type TimeRange,
} from "./digestTimes";

type RangeProps = {
  value: TimeRange;
  onChange: (range: TimeRange) => void;
  /** The newest time the report knows, in its clock: quick ranges end here. */
  anchor: string | null;
  /** What the times are ("UTC", "Server time (UTC-03:00)"), one short label. */
  clock: string;
  /** One sentence: what a range does for this source. */
  scope: string;
};

export function TimeRangeButton({ disabled = false, ...props }: RangeProps & { disabled?: boolean }) {
  const id = useId();
  const { value, clock } = props;
  const label = rangeText(value);
  const active = rangeActive(value);
  return <Popover label="Time range" className="qd-range-anchor" buttonClassName={`button qd-range-button${active ? " is-active" : ""}`}
    buttonTitle={active ? `Time range: ${label} (${clock})` : `Time range: any time (${clock})`} disabled={disabled} width={340}
    initialFocus={`#${CSS.escape(id)}-from`}
    trigger={<><Icon name="history" size={14} /><span className="qd-range-label">{label}</span><Icon name="chevron-down" size={12} /></>}>
    <RangeForm id={id} {...props} />
  </Popover>;
}

/** The dialog's form: mounted each time the dialog opens, so its fields
 * start from the range in use (an edit left unapplied is dropped). */
function RangeForm({ id, value, onChange, anchor, clock, scope }: RangeProps & { id: string }) {
  const [from, setFrom] = useState(() => inputValue(value.from));
  const [to, setTo] = useState(() => inputValue(value.to));
  const [error, setError] = useState("");
  const active = rangeActive(value);
  function apply(next: TimeRange) {
    const problem = rangeProblem(next);
    if (problem) { setError(problem); return; }
    setError("");
    onChange(next);
    closePopovers();
  }
  function quick(kind: QuickRange) {
    const next = quickRange(kind, anchor);
    if (next) apply(next);
  }
  return <form className="qd-range" noValidate onSubmit={(e) => { e.preventDefault(); apply({ from, to }); }}>
    <h3 className="popover-title">Time range</h3>
    <div className="qd-range-quick" role="group" aria-label="Quick ranges">
      <button type="button" className="button small" aria-pressed={!active} onClick={() => apply(ANY_TIME)}>Any time</button>
      {QUICK_RANGES.map(([kind, text]) => <button key={kind} type="button" className="button small"
        aria-pressed={value.quick === kind} disabled={!anchor}
        title={anchor ? `Ends at ${wallText(anchor, true)}, the newest time in this report` : "Load a report first: quick ranges end at its newest time"}
        onClick={() => quick(kind)}>{text}</button>)}
    </div>
    <div className="qd-range-fields">
      <label htmlFor={`${id}-from`}>From</label>
      <input id={`${id}-from`} type="datetime-local" step={1} value={from}
        aria-describedby={`${id}-clock`} aria-invalid={error && error.startsWith("From") ? true : undefined}
        onChange={(e) => { setFrom(e.target.value); setError(""); }} />
      <label htmlFor={`${id}-to`}>To</label>
      <input id={`${id}-to`} type="datetime-local" step={1} value={to}
        aria-describedby={`${id}-clock`} aria-invalid={error ? true : undefined}
        onChange={(e) => { setTo(e.target.value); setError(""); }} />
    </div>
    <p id={`${id}-clock`} className="hint">Times are {clock}. Both ends are included. {scope}</p>
    {anchor && <p className="hint">Newest time in this report: {wallText(anchor, true)}.</p>}
    {error && <p className="form-error" role="alert">{error}</p>}
    <div className="qd-range-actions">
      <button type="button" className="button" onClick={() => { setFrom(""); setTo(""); apply(ANY_TIME); }}>Clear</button>
      <button type="submit" className="button primary">Apply</button>
    </div>
  </form>;
}
