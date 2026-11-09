import { useEffect, useRef, useState } from "react";
import { indexSuggestion, type Connection, type IndexHint, type TopStatement } from "./api";
import { connectionKey } from "./binding";
import { CopySql } from "./PerfShared";
import { ActionStatus, InspectorSection } from "./kit";
import { suggestedIndexOrigin, type SqlOpenOptions } from "./statementThread";
import { usePrivilege } from "./privileges";

// Memory only, bounded and connection/schema scoped. Reopening a digest soon
// after does not repeat server reads; a result expires after CACHE_MS (an
// index may have been created since) and an unknown result is never kept.
// SQL and credentials never go into local storage.
const CACHE_MS = 120_000;
const cache = new Map<string, { result: IndexHint; at: number }>();

export function IndexSuggestion({ row, connection, disabled, onOpenSql }: {
  row: TopStatement;
  connection: Connection;
  disabled: boolean;
  onOpenSql?: (sql: string, options?: SqlOpenOptions) => void;
}) {
  const gate = usePrivilege("explain");
  const key = JSON.stringify([connectionKey(connection), row.digest, row.schema]);
  const request = useRef<AbortController | null>(null);
  const local = row.index_hint;
  const [state, setState] = useState<{ key: string; result?: IndexHint; error?: string; pending?: boolean }>({ key });
  const blocked = disabled || (!local && !!gate.disabledReason);
  useEffect(() => {
    setState({ key });
    return () => { request.current?.abort(); request.current = null; };
  }, [key, local, blocked]);
  function read() {
    if (blocked || local || request.current) return;
    if (!row.digest) {
      setState({ key, error: "No statement digest was recorded. Open it in the Query editor." });
      return;
    }
    // EXPLAIN can evaluate expressions while planning, including inside a
    // view. Only an explicit click authorizes this server request.
    cache.delete(key);
    const controller = new AbortController();
    request.current = controller;
    setState({ key, pending: true });
    void indexSuggestion(connection, row.digest, row.schema, controller.signal).then(result => {
      if (controller.signal.aborted) return;
      if (result.status !== "unknown") {
        if (cache.size >= 200) cache.delete(cache.keys().next().value!);
        cache.set(key, { result, at: Date.now() });
      }
      setState({ key, result });
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setState({ key, error: error instanceof Error ? error.message : String(error) });
    }).finally(() => { if (request.current === controller) request.current = null; });
  }
  const kept = cache.get(key);
  const saved = kept && Date.now() - kept.at <= CACHE_MS ? kept.result : undefined;
  const current = state.key === key ? state : { key };
  const result = local || current.result || saved;
  const error = current.error;
  const pending = !blocked && !!current.pending;
  return <InspectorSection title="Suggested index">
    <ActionStatus state={{ phase: pending ? "pending" : error ? "failed" : "idle",
      message: pending ? "Reading an estimated plan and existing indexes…" : error || "", startedAt: null }}
      onRetry={error ? read : undefined} />
    {disabled ? <p className="hint">Refresh statements on this connection to read a suggestion.</p>
      : blocked ? <p className="hint"><bdi>{gate.disabledReason}</bdi></p>
      : !result && !error && !pending ? <button type="button" className="button ghost" data-action="read-index-suggestion"
        onClick={read}>Read suggestion</button>
      : result && <>
        <p className="hint"><bdi>{result.reason}</bdi></p>
        {result.ddl && <CopySql sql={result.ddl} label="Suggested index SQL" />}
        {result.caveats && <p className="hint"><bdi>{result.caveats}</bdi></p>}
        {result.ddl && onOpenSql && <button type="button" className="button ghost" data-action="open-index-suggestion"
          onClick={() => onOpenSql(result.ddl!, { indexOrigin: suggestedIndexOrigin(row, connection) })}>Open in Query editor</button>}
        {!local && <button type="button" className="text-button"
          onClick={read}>Read again</button>}
      </>}
  </InspectorSection>;
}
