// "Is this normal?" for Health and Query digest: Save baseline and Compare
// with baseline, two items of the view's Refresh menu (no toolbar button),
// the sheets they open, and the bar a view shows while it compares
// (Baseline → Now, like the Source → Target bar of compareShared.tsx).
// Baselines are kept on this computer (baselinesApi.ts); saving and listing
// read only the server's identity.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { Connection } from "./api";
import {
  deleteBaseline, getBaseline, listBaselines, saveBaseline,
  type Baseline, type BaselineItem, type BaselineKind, type ReadingServer,
} from "./baselinesApi";
import { baselineMismatch, savedAtText } from "./baselineCompare";
import { ActionButton, ActionStatus, ConfirmSheet, FormGrid, FormRow, closePopovers, useAction } from "./kit";
import "./baselines.css";

const KIND_LABEL: Record<BaselineKind, string> = { health: "Health", digest: "Query digest" };
export const BASELINE_NAME_MAX = 80;

/** A baseline being compared, bound to the connection it was chosen on. */
export type ActiveBaseline<P> = { key: string; baseline: Baseline<P> };

/** The two items of the view's Refresh menu. A reason disables an item and
 * shows in its place of the hint, so the menu never hides why. */
export function BaselineMenu({ saveReason, compareReason, onSave, onCompare }: {
  saveReason: string;
  compareReason: string;
  onSave: () => void;
  onCompare: () => void;
}) {
  return <>
    <button type="button" role="menuitem" data-action="save-baseline" disabled={!!saveReason}
      onClick={() => { closePopovers(); onSave(); }}>
      <span>Save baseline…</span>
      <span className="hint">{saveReason || "Keep this reading on this computer"}</span>
    </button>
    <button type="button" role="menuitem" data-action="compare-baseline" disabled={!!compareReason}
      onClick={() => { closePopovers(); onCompare(); }}>
      <span>Compare with baseline…</span>
      <span className="hint">{compareReason || "Is this normal? What changed since a saved reading"}</span>
    </button>
  </>;
}

/** "Baseline, 2026-11-08 14:02": the name Save baseline starts with. */
export function defaultBaselineName(at: string) {
  return `Baseline, ${savedAtText(at)}`;
}

/** Save baseline: one field, pre-filled; the server's identity is read and
 * the reading is kept. Errors (the limit of 20, a restart since the reading)
 * show in the sheet and keep the typed name. */
export function SaveBaselineSheet({ kind, connection, server, defaultName, report, onSaved, onClose, returnFocus }: {
  kind: BaselineKind;
  /** The connection the reading came from (snapshot taken when the sheet opened). */
  connection: Connection;
  /** The server's label, as the view names it. */
  server: string;
  defaultName: string;
  report: object;
  onSaved: (item: BaselineItem & { max: number }) => void;
  onClose: () => void;
  returnFocus?: () => HTMLElement | null | undefined;
}) {
  const [name, setName] = useState(defaultName);
  const [invalid, setInvalid] = useState("");
  const saving = useAction();
  const id = useId();
  const field = useRef<HTMLInputElement>(null);
  async function submit() {
    const value = name.trim();
    const problem = !value ? "Name the baseline." : value.length > BASELINE_NAME_MAX ? `Use at most ${BASELINE_NAME_MAX} characters.` : "";
    if (problem) {
      setInvalid(problem);
      field.current?.focus();
      return;
    }
    const result = await saving.run((signal) => saveBaseline(connection, kind, value, report, signal), {
      pending: `Saving the baseline of ${server}…`, done: "",
    });
    if (result.ok) onSaved(result.value);
  }
  return <ConfirmSheet eyebrow="Save baseline" title={`Save a ${KIND_LABEL[kind]} baseline`} className="baseline-sheet"
    description={kind === "health"
      ? <>Keeps this reading of <bdi>{server}</bdi> on this computer, to compare later readings with. Saving reads only the server's identity.</>
      : <>Keeps these statement totals of <bdi>{server}</bdi> on this computer, literal values hidden and no samples. Saving reads only the server's identity.</>}
    confirmLabel="Save baseline" confirmTone="primary" busy={saving.pending} busyLabel="Saving…" progress="Reading the server's identity"
    returnFocus={returnFocus} onCancel={onClose} onConfirm={() => void submit()}>
    <FormGrid>
      <FormRow label="Name" htmlFor={id} error={invalid || undefined} hint="For example the change it comes before.">
        <input id={id} ref={field} value={name} maxLength={BASELINE_NAME_MAX + 20} spellCheck={false} autoComplete="off"
          autoCapitalize="off" disabled={saving.pending}
          onChange={(e) => { setName(e.target.value.replace(/[\r\n\t]/g, " ")); setInvalid(""); }} />
      </FormRow>
    </FormGrid>
    {saving.phase === "failed" && <p className="form-error baseline-error" role="alert">{saving.message}</p>}
  </ConfirmSheet>;
}

/** One line about a saved baseline: its server version and what it held. */
function itemDetail(item: BaselineItem) {
  const server = `${item.server.product} ${item.server.version}`;
  if (item.kind === "health") {
    const counts = [item.problem ? `${item.problem} ${item.problem === 1 ? "problem" : "problems"}` : "",
      item.watch ? `${item.watch} to watch` : ""].filter(Boolean).join(", ");
    return `${server} · ${counts || "All OK"}`;
  }
  const n = item.statements ?? 0;
  return `${server} · ${n.toLocaleString()} ${n === 1 ? "statement" : "statements"}`;
}

/** Compare with baseline: this server's baselines of the kind, read when the
 * sheet opens, newest first and chosen; Delete… asks in its row. Compare
 * opens the chosen one, only when the reading on screen came from the
 * baseline's server; the view then compares the two. */
export function CompareBaselineSheet<P>({ kind, connection, server, current, reading, onCompare, onClose, onDeleted, returnFocus }: {
  kind: BaselineKind;
  connection: Connection;
  server: string;
  /** The baseline compared now, chosen again when it is still kept. */
  current?: string;
  /** The server the reading on screen came from. */
  reading: ReadingServer | null;
  onCompare: (baseline: Baseline<P>) => void;
  onClose: () => void;
  onDeleted?: (id: string) => void;
  returnFocus?: () => HTMLElement | null | undefined;
}) {
  const listing = useAction();
  const opening = useAction();
  const deleting = useAction();
  const [items, setItems] = useState<BaselineItem[] | null>(null);
  const [max, setMax] = useState(20);
  const [chosen, setChosen] = useState("");
  const [asking, setAsking] = useState("");
  const name = useId();
  async function load() {
    const result = await listing.run((signal) => listBaselines(connection, kind, signal), { pending: "Reading the saved baselines…", done: "" });
    if (!result.ok) return;
    setItems(result.value.items);
    setMax(result.value.max);
    setChosen(current && result.value.items.some((i) => i.id === current) ? current : result.value.items[0]?.id || "");
  }
  useEffect(() => { void load(); }, []);
  async function open() {
    if (!chosenKept) return;
    const result = await opening.run(async (signal) => {
      const baseline = await getBaseline<P>(chosen, signal);
      if (baseline.kind !== kind) throw new Error(`Could not open the baseline: it is not a ${KIND_LABEL[kind]} baseline.`);
      const problem = baselineMismatch(reading, baseline);
      if (problem) throw new Error(`Could not compare: ${problem}. Refresh, then compare.`);
      return baseline;
    }, { pending: "Opening the baseline…", done: "" });
    if (result.ok) onCompare(result.value);
  }
  async function remove(item: BaselineItem) {
    const result = await deleting.run((signal) => deleteBaseline(item.id, signal), {
      pending: `Deleting ${item.name}…`, done: `Deleted ${item.name} from this computer.`,
    });
    if (!result.ok) return;
    setAsking("");
    onDeleted?.(item.id);
    setItems((old) => (old || []).filter((i) => i.id !== item.id));
  }
  // The choice always names a kept baseline: the newest when the chosen one
  // went, none when the last one went (Compare is then off).
  useEffect(() => {
    if (!items) return;
    if (!items.length) setChosen((c) => (c ? "" : c));
    else if (!items.some((i) => i.id === chosen)) setChosen(items[0].id);
  }, [items, chosen]);
  const chosenKept = !!items && items.some((i) => i.id === chosen);
  const label = KIND_LABEL[kind];
  return <ConfirmSheet eyebrow="Compare with baseline" title="Choose a baseline" className="baseline-sheet"
    description={<>Saved on this computer for <bdi>{server}</bdi>. The reading on screen is compared with the one you choose.</>}
    confirmLabel="Compare" confirmTone="primary" confirmDisabled={!chosenKept || listing.pending || deleting.pending}
    busy={opening.pending} busyLabel="Opening…" returnFocus={returnFocus} onCancel={onClose} onConfirm={() => void open()}>
    <ActionStatus className="baseline-list-status" state={listing} onRetry={() => void load()} />
    {items && !items.length && <p className="baseline-empty" data-baselines-empty>
      No {label} baseline of this server is saved yet. Save baseline, in the same menu, keeps the reading on screen.
    </p>}
    {items && items.length > 0 && <fieldset className="baseline-choices" disabled={opening.pending}>
      <legend>{items.length} of {max} kept for this server, newest first</legend>
      {items.map((item) => <div key={item.id} className="baseline-choice" data-baseline-id={item.id}>
        <label>
          <input type="radio" name={name} value={item.id} checked={chosen === item.id} onChange={() => setChosen(item.id)} />
          <span className="baseline-choice-text">
            <bdi className="baseline-choice-name">{item.name}</bdi>
            <span className="hint">{savedAtText(item.created_at)} · {itemDetail(item)}</span>
          </span>
        </label>
        {asking === item.id ? <span className="baseline-delete-ask" role="group" aria-label={`Delete ${item.name}`}>
          <span className="hint">Delete from this computer?</span>
          <button type="button" className="button small" disabled={deleting.pending} onClick={() => setAsking("")}>Keep</button>
          <ActionButton small danger busy={deleting.pending} busyLabel="Deleting…" data-action="confirm-delete-baseline"
            onClick={() => void remove(item)}>Delete</ActionButton>
        </span> : <button type="button" className="text-button baseline-delete" aria-label={`Delete baseline ${item.name}`}
          disabled={deleting.pending} onClick={() => { deleting.reset(); setAsking(item.id); }}>Delete…</button>}
      </div>)}
    </fieldset>}
    <ActionStatus className="baseline-delete-status" state={deleting} onDismiss={deleting.reset} />
    {opening.phase === "failed" && <p className="form-error baseline-error" role="alert">{opening.message}</p>}
  </ConfirmSheet>;
}

/** The bar a view shows while it compares: Baseline (name, saved) → Now,
 * Change… (choose another) and Stop comparing; one short line per note
 * (a restart, an upgrade). */
export function BaselineBar({ name, savedAt, now, notes, onChange, onStop }: {
  name: string;
  savedAt: string;
  now: string;
  notes: ReactNode[];
  onChange: () => void;
  onStop: () => void;
}) {
  return <section className="baseline-bar" aria-label="Comparing with a baseline">
    <div className="baseline-bar-head">
      <div className="baseline-bar-names">
        <span title={`Baseline: ${name}, saved ${savedAt}`}><b>Baseline</b> <bdi>{name}</bdi> · {savedAt}</span>
        <span className="baseline-bar-arrow" aria-hidden="true">→</span>
        <span title={`Now: ${now}`}><b>Now</b> {now}</span>
      </div>
      <div className="baseline-bar-actions">
        <button type="button" className="button small" data-action="change-baseline" aria-haspopup="dialog" onClick={onChange}>Change…</button>
        <button type="button" className="button small" data-action="stop-comparing" onClick={onStop}>Stop comparing</button>
      </div>
    </div>
    {notes.filter(Boolean).map((note, i) => <p key={i} className="baseline-bar-note" data-baseline-note>{note}</p>)}
  </section>;
}
