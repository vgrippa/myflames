import type { Capture, Connection, EditorRun, TopStatement } from "./api";
import { connectionKey, connectionLabel } from "./binding";

export type StatementSource = "pfs" | "table" | "file";
export type StatementBefore = { avgMs: number | null; examinedPerExecution: number | null };
/** Page memory only: source SQL and counters never extend the saved session. */
export type SuggestedIndexOrigin = {
  statement: TopStatement;
  before: StatementBefore;
  target: { key: string; label: string };
};
export type SqlOpenOptions = { append?: boolean; indexOrigin?: SuggestedIndexOrigin };
const measured = (n: unknown): number | null =>
  typeof n === "number" && Number.isFinite(n) && n >= 0 ? n : null;

export function statementDetails(row: TopStatement, source: StatementSource) {
  const examined = measured(row.rows_examined), calls = measured(row.count), returned = measured(row.rows_sent);
  return {
    text: row.sample_text || row.digest_text || "",
    avgMs: measured(row.avg_ms),
    examinedPerExecution: examined !== null && calls !== null && calls > 0 ? examined / calls : null,
    examinedPerReturned: examined !== null && returned !== null && returned > 0 ? examined / returned : null,
    flags: row.flags || [],
    suggestIndex: source === "pfs" && !!row.digest && (row.flags || []).some(f => f === "no_index_used" || f === "full_scan"),
  };
}
export function suggestedIndexOrigin(row: TopStatement, connection: Connection): SuggestedIndexOrigin {
  const { avgMs, examinedPerExecution } = statementDetails(row, "pfs");
  return { statement: { ...row, flags: [...(row.flags || [])] }, before: { avgMs, examinedPerExecution },
    target: { key: connectionKey(connection), label: connectionLabel(connection) } };
}
const statementText = (sql: string) => sql.trim().replace(/;$/, "").trimEnd();
/** Attribution uses the prepared SQL before parameter binding; history uses
 * the bound query. Session captures may resolve different temporary objects. */
export function measurementMatches(sourceSql: string | undefined, preparedSql: string, inSession: boolean): boolean {
  return !!sourceSql && !inSession && statementText(sourceSql) === statementText(preparedSql);
}
/** Match the actual successful statement, never merely the tab's edited text. */
export function indexWasApplied(run: EditorRun | null, ddl: string): boolean {
  if (!run || run.kind !== "run" || run.state !== "complete" || run.error || run.stopped || run.statements.length !== 1) return false;
  const statement = run.statements[0];
  return statement.status === "ok" && statementText(statement.sent || statement.text) === statementText(ddl);
}
/** Use the last captured statement event. Plan iterator timings and summed
 * plan rows are different measurements and cannot replace missing counters. */
export function captureMeasurement(capture: Capture) {
  const stats = capture.mode === "analyze" ? capture.runs[capture.runs.length - 1]?.statistics : null;
  const timer = measured(stats?.timer_wait_ps);
  return { ms: timer === null ? null : timer / 1e9, rows: measured(stats?.rows_examined) };
}
/** Exact SQL plus database and connection: similar digests can have different
 * literal values and must never be silently paired. */
export function captureThreadKey(connection: Connection, capture: Capture): string {
  return JSON.stringify([connectionKey(connection), connection.database, statementText(capture.query)]);
}

export const STATEMENT_FLAGS: Record<string, [string, string]> = {
  no_index_used: ["No index used", "At least one execution read a table without any index (a full scan)."],
  no_good_index_used: ["No good index", "The server found no good index for at least one execution."],
  tmp_disk_tables: ["Temp table on disk", "Built an internal temporary table on disk instead of in memory."],
  sort_merge_passes: ["Sort merge passes", "Sorting outgrew sort_buffer_size and needed merge passes."],
  low_selectivity: [
    "Reads ≫ returns",
    "Examines more than twice the rows it returns (rows sent ÷ rows examined below 0.5).",
  ],
};
