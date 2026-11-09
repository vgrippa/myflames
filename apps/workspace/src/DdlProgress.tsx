/** Text and native progress elements keep the number accessible without color. */
import type { DdlProgress, DdlProgressItem } from "./api";
import { InstrumentsLink } from "./Instruments";
import { durationText } from "./duration";
import { Fields } from "./kit";
import { Duration } from "./PerfShared";
import { number } from "./api";

/** Counters with grouping, like every other count; an exact string count stays as sent. */
const units = (value: number | string | null) =>
  value === null ? "?" : typeof value === "number" ? number(value)
    : /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) ? number(Number(value)) : value;

export function ddlLeft(item: DdlProgressItem) {
  return item.left_s === null ? "Time left unknown" : `About ${durationText(item.left_s)} left (estimate)`;
}
export function DdlMeter({ item }: { item: DdlProgressItem }) {
  const text = item.percent === null ? "Unknown" : `${item.percent}%`;
  return <span className="ddl-meter" title={`${item.stage_label} (${item.stage}). ${item.note || ddlLeft(item)}`}>
    <progress max={100} value={item.percent ?? undefined} aria-label={`DDL progress on connection ${item.id}`} />
    <span>{text}</span>
  </span>;
}
export function DdlDetails({ item }: { item: DdlProgressItem }) {
  return <div className="ddl-details">
    <DdlMeter item={item} />
    <p>{item.stage_label}</p>
    {item.note && <p className="hint">{item.note}</p>}
    <code className="ddl-stage">{item.stage}</code>
    <Fields rows={[
      ["Elapsed", item.elapsed_s === null ? "Unknown" : <Duration value={item.elapsed_s} />],
      ["Time left (estimate)", item.left_s === null ? "Unknown" : <>About <Duration value={item.left_s} note="Estimate" /></>],
      item.units !== null && [item.units === "rows" ? "Rows (estimate)" : "Work units (estimate)",
        `${units(item.completed)} of ${units(item.estimated)}`],
    ]} />
  </div>;
}
/** Shared quiet diagnostic. Navigation delegates to the existing reviewed view. */
export function DdlUnavailable({ progress, onOpenInstruments }: {
  progress: DdlProgress;
  onOpenInstruments?: (row: string) => void;
}) {
  if (progress.available && !progress.missing.length && !progress.reason?.startsWith("Could not read")) return null;
  const row = progress.missing.includes("performance_schema") ? "ps" :
    progress.missing.find(n => !n.startsWith("stage/")) ?
      `consumer:${progress.missing.find(n => !n.startsWith("stage/"))}` :
      progress.missing.length ? `instrument:${progress.missing[0]}` : null;
  return <p className="hint ddl-unavailable">
    <span>{progress.reason || "DDL progress is unavailable."}</span>{" "}
    {row && (onOpenInstruments ?
      <button type="button" className="text-button instr-link" data-instruments-row={row}
        onClick={() => onOpenInstruments(row)}>Turn on stage tracking…</button> :
      <InstrumentsLink row={row}>Turn on stage tracking…</InstrumentsLink>)}
  </p>;
}
