import type { Capture } from "./api";
import { InfoPopover } from "./kit";
import { captureMeasurement, type StatementBefore } from "./statementThread";

const value = (n: number | null) => n === null ? "Not collected" : n.toLocaleString(undefined, { maximumFractionDigits: 2 });
const latency = (n: number | null) => n === null ? "Not collected" : `${value(n)} ms`;
export function DigestBefore({ before }: { before: StatementBefore }) {
  return <span>Digest average before {latency(before.avgMs)}, rows examined {value(before.examinedPerExecution)}</span>;
}
export function MeasuredStatement({ before, capture, onOpenInstruments }: {
  before: StatementBefore; capture: Capture; onOpenInstruments?: () => void;
}) {
  const after = captureMeasurement(capture);
  return <div className="editor-insight" data-measure-result>
    <span className="editor-insight-text"><DigestBefore before={before} /> → after {latency(after.ms)}, {value(after.rows)} (this run; caches may be warm)</span>
    <InfoPopover label="About this measurement">
      <p>The before numbers average the digest's recorded executions. After is one instrumented Run analysis, with results discarded; parameters, concurrency and cache conditions can differ. It does not establish application latency or prove that the index caused a change.</p>
      <p>The after timer and rows examined come from the same statement history event. Plan iterator times and row estimates are not substitutes.</p>
      {(after.ms === null || after.rows === null) && <p>Statement history counters were not collected. Check Server &gt; Instruments.</p>}
      {(after.ms === null || after.rows === null) && onOpenInstruments && <button className="button small" onClick={onOpenInstruments}>Server &gt; Instruments</button>}
    </InfoPopover>
  </div>;
}
