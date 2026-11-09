// The sheet that asks for the values a Top statements digest left out (its
// :p1, :p2 … markers). "Use values" writes them into the tab's Parameters
// JSON, which stays the source of truth; "Later" keeps the Parameters panel
// for editing by hand. The sheet never runs anything.
import { useId, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Select, useFocusScope } from "./kit";
import { applyEntries, startEntries, type FillEntry, type FillField, type FillType } from "./fillValues";
import "./editor-panels.css";

const TYPES: [FillType, string][] = [["number", "Number"], ["text", "Text"], ["null", "NULL"]];

export function FillParametersSheet({
  tabName,
  fields,
  parameters,
  returnFocus,
  onUse,
  onLater,
  onClose,
}: {
  tabName: string;
  fields: FillField[];
  /** The tab's Parameters JSON now (values already there are shown). */
  parameters: string;
  /** Where focus goes when the sheet closes: the SQL editor. */
  returnFocus: () => HTMLElement | null | undefined;
  /** The new Parameters JSON, the names given a value, and those left empty. */
  onUse: (parameters: string, filled: string[], remaining: string[]) => void;
  /** Close and edit the Parameters JSON by hand (today's path). */
  onLater: () => void;
  /** Escape: close; focus returns to the editor. */
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const use = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const leadId = useId();
  const base = useId();
  const [entries, setEntries] = useState<Record<string, FillEntry>>(() => startEntries(fields, parameters));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const valueInput = (name: string) => document.getElementById(`${base}-${name}`) as HTMLInputElement | null;
  useFocusScope(dialog, {
    open: true,
    onClose,
    // The first value to type (a NULL field has none: its type is first).
    initialFocus: () => {
      const first = fields.find((f) => entries[f.name]?.locked === undefined);
      if (!first) return use.current;
      return entries[first.name]?.type === "null" ? document.getElementById(`${base}-${first.name}-type`) : valueInput(first.name);
    },
    returnFocus,
  });
  function change(name: string, patch: Partial<FillEntry>) {
    // Only edited entries are written back; the others keep their exact value.
    setEntries((all) => ({ ...all, [name]: { ...all[name], ...patch, edited: true } }));
    setErrors((all) => {
      if (!all[name]) return all;
      const next = { ...all };
      delete next[name];
      return next;
    });
  }
  /** Enter moves to the next value; after the last one, to Use values. */
  function onValueKey(e: KeyboardEvent<HTMLInputElement>, index: number) {
    if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
    e.preventDefault();
    const next = fields.slice(index + 1).find((f) => entries[f.name]?.type !== "null" && entries[f.name]?.locked === undefined);
    (next ? valueInput(next.name) : use.current)?.focus();
  }
  function submit() {
    const outcome = applyEntries(parameters, fields, entries);
    if (!outcome.ok) {
      setErrors(outcome.errors);
      const first = fields.find((f) => outcome.errors[f.name]);
      if (first) valueInput(first.name)?.focus();
      return;
    }
    onUse(outcome.parameters, outcome.filled, outcome.remaining);
  }
  const lists = fields.filter((f) => f.list).length;
  return createPortal(
    <dialog
      ref={dialog}
      className="confirm-sheet fill-sheet"
      role="dialog"
      tabIndex={-1}
      aria-labelledby={titleId}
      aria-describedby={leadId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">Parameters</div>
          <h2 id={titleId}>Fill in the values of {tabName}</h2>
        </div>
      </div>
      <div className="sheet-body">
        <p id={leadId} className="sheet-lead">
          The statement was recorded without these values. They go into Parameters for Run analysis; nothing runs yet.
          Explain sends SQL as written, so put its values in the SQL text first.{lists ? " A list takes several values separated by commas." : ""}
        </p>
        <div className="fill-fields">
          {fields.map((f, i) => {
            const entry = entries[f.name] || { type: f.type, value: "" };
            const id = `${base}-${f.name}`;
            const error = errors[f.name];
            const hintId = `${id}-hint`;
            const errorId = `${id}-error`;
            if (entry.locked !== undefined) {
              // The field cannot show this value without changing it: read-only
              // JSON, never written back (Use values keeps it exactly).
              return (
                <div className="fill-field is-locked" key={f.name} data-parameter={f.name}>
                  <div className="fill-label">
                    <span className="fill-name">{f.name}</span>
                    {f.context && <code className="fill-context" title={f.context}>{f.context}</code>}
                  </div>
                  <code id={id} className="fill-json" tabIndex={0} aria-label={`${f.name} in Parameters`} aria-describedby={hintId}>
                    {entry.locked}
                  </code>
                  <p id={hintId} className="hint">{entry.reason} stays as it is. Choose Later to edit it in Parameters.</p>
                </div>
              );
            }
            return (
              <div className={`fill-field${error ? " has-error" : ""}`} key={f.name} data-parameter={f.name}>
                <label className="fill-label" htmlFor={id}>
                  <span className="fill-name">{f.name}</span>
                  {f.context && <code className="fill-context" title={f.context}>{f.context}</code>}
                </label>
                <div className="fill-controls">
                  <Select<FillType>
                    id={`${id}-type`}
                    className="fill-type"
                    label={`Type of ${f.name}`}
                    value={entry.type}
                    options={TYPES}
                    onChange={(v) => change(f.name, { type: v as FillType })}
                  />
                  <input
                    id={id}
                    className="fill-value"
                    value={entry.type === "null" ? "" : entry.value}
                    disabled={entry.type === "null"}
                    placeholder={entry.type === "null" ? "NULL" : f.list ? (entry.type === "number" ? "1, 2, 3" : "a, b, c") : ""}
                    inputMode={entry.type === "number" && !f.list ? "decimal" : undefined}
                    spellCheck={false}
                    autoComplete="off"
                    aria-invalid={error ? true : undefined}
                    aria-describedby={error ? errorId : f.list ? hintId : undefined}
                    onChange={(e) => change(f.name, { value: e.target.value })}
                    onKeyDown={(e) => onValueKey(e, i)}
                  />
                </div>
                {error ? (
                  <p id={errorId} className="form-error" role="alert">{error}</p>
                ) : f.list ? (
                  <p id={hintId} className="hint">A collapsed list: several values separated by commas.</p>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
      <div className="dialog-actions sheet-actions">
        <button type="button" className="button" onClick={onLater} title="Close and edit the Parameters JSON by hand">
          Later
        </button>
        <button ref={use} type="button" className="button primary" data-action="use-values" onClick={submit}>
          Use values
        </button>
      </div>
    </dialog>,
    document.body,
  );
}
