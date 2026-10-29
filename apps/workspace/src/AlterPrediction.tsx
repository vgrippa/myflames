/** What myflames predicts the server does with the reviewed ALTER
 * (alter_algorithm.py): the algorithm, whether it rebuilds, whether writes
 * continue, and why for each clause. Advisory; never changes the statement. */
export type AlterPrediction = {
  predicted: boolean;
  algorithm: "INSTANT" | "INPLACE" | "COPY" | null;
  rebuild: boolean | null;
  concurrent_dml: boolean | null;
  summary: string | null;
  text: string | null;
  reason: string;
  source?: string;
  notes?: string[];
  clauses: { text: string; algorithm: string | null; summary: string | null; outcome: string | null;
    reason: string; source: string }[];
};

/** Why a prediction could not be made, as a sentence of its own: the reason
 * without its "Not predicted:" lead, starting with a capital letter. */
export function notPredictedReason(reason: string) {
  const text = reason.replace(/^Not predicted:?\s*/, "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Shared evidence for the editor review and snapshot-based sync predictions. */
export function AlterPredictionWhy({ prediction }: {
  prediction: Pick<AlterPrediction, "predicted" | "reason" | "source" | "clauses" | "notes">;
}) {
  return <details>
    <summary>Why: {prediction.predicted ? "the prediction for each clause" : "what could not be predicted"}</summary>
    <div className="alter-prediction-details" aria-label="ALTER prediction details">
      <p>{prediction.reason}</p>
      {prediction.source && <p className="alter-prediction-source">Source: {prediction.source}</p>}
      {prediction.clauses.length > 0 && <ul>
        {prediction.clauses.map((clause, i) => <li key={i}>
          <code>{clause.text}</code>: {clause.summary ?? "not predicted"}. {clause.reason}
          {clause.source && <span className="alter-prediction-source"> Source: {clause.source}</span>}
        </li>)}
      </ul>}
      {prediction.notes?.map((note, i) => <p key={i} className="hint">{note}</p>)}
      <p className="hint">A prediction from the table definition and the server's rules; the server decides when the ALTER runs.</p>
    </div>
  </details>;
}
