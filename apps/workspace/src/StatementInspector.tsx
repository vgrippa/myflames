import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { number, readDigestHistogram, time, type Connection, type DigestHistogram, type TopStatement } from "./api";
import { connectionKey } from "./binding";
import { CopySql } from "./PerfShared";
import { DataTable, Fields, InspectorSection, ViewStatus } from "./kit";
import { IndexSuggestion } from "./IndexSuggestion";
import { STATEMENT_FLAGS, statementDetails, type SqlOpenOptions, type StatementSource } from "./statementThread";

/** Both ranked statement views keep their own tables and source-specific
 * sections; this is the common reading and action order for one statement. */
export function StatementInspector({ row, source, connection, disabled, actions, notes, statementExtra, latencyDetails, children, onOpenSql }: {
  row: TopStatement;
  source: StatementSource;
  connection: Connection;
  disabled: boolean;
  actions: ReactNode;
  notes: ReactNode;
  statementExtra?: ReactNode;
  latencyDetails?: ReactNode;
  children?: ReactNode;
  onOpenSql?: (sql: string, options?: SqlOpenOptions) => void;
}) {
  const detail = statementDetails(row, source);
  const histogramId = useId();
  const hasQuantiles = [row.p95_ms, row.p99_ms, row.p999_ms].some(n => n != null);
  return <div data-statement-inspector data-statement-source={source}>
    <InspectorSection title="Statement">
      {detail.text ? <CopySql sql={detail.text} label="Statement SQL" /> : <p className="hint">No statement text was recorded.</p>}
      {!row.sample_text && detail.text && <p className="hint top-normalized">Normalized text: ? marks each literal value. Explain and Run analysis open it with parameters to fill in first.</p>}
      {statementExtra}
    </InspectorSection>
    <InspectorSection title="Latency" info={hasQuantiles ? <p>P95, P99 and P99.9 are the upper bounds of the histogram buckets containing 95, 99 and 99.9 percent of calls. {source === "pfs" ? "Counted since the statement was first seen." : "Counted from the log in the selected range, capped at the slowest call."}</p> : undefined}>
      <Fields rows={[
        ["Total", time(row.total_ms)], ["Average", time(detail.avgMs)], ["Longest", time(row.max_ms)],
        row.p95_ms != null && [<span>P95</span>, time(row.p95_ms)],
        row.p99_ms != null && [<span>P99</span>, time(row.p99_ms)],
        row.p999_ms != null && [<span>P99.9</span>, time(row.p999_ms)],
      ]} />
      {latencyDetails ? <>
        <button className="text-button" onClick={() => document.getElementById(histogramId)?.scrollIntoView({ block: "nearest" })}>Latency histogram</button>
        <div id={histogramId}>{latencyDetails}</div>
      </> : hasQuantiles && source === "pfs" && <StatementHistogram row={row} connection={connection} disabled={disabled} />}
    </InspectorSection>
    <InspectorSection title="Rows">
      <Fields rows={[
        ["Examined per execution", number(detail.examinedPerExecution)],
        ["Examined per row returned", number(detail.examinedPerReturned)],
        ["Rows examined", number(row.rows_examined)], ["Rows sent", number(row.rows_sent)],
      ]} />
      {row.rows_sent === 0 && <p className="hint">No rows were returned; examined per returned row is not defined.</p>}
    </InspectorSection>
    <InspectorSection title="Flags">
      {detail.flags.length ? <ul className="top-flags">{detail.flags.map(flag => {
        const [label, why] = STATEMENT_FLAGS[flag] || [flag.replace(/_/g, " "), ""];
        return <li key={flag}><strong><bdi>{label}</bdi></strong>{why && <span className="hint"> <bdi>{why}</bdi></span>}</li>;
      })}</ul> : <p className="hint">No flags recorded.</p>}
    </InspectorSection>
    {detail.suggestIndex && <IndexSuggestion row={row} connection={connection} disabled={disabled} onOpenSql={onOpenSql} />}
    <InspectorSection title="Actions">{actions}</InspectorSection>
    <InspectorSection title="Source notes">{notes}</InspectorSection>
    {children}
  </div>;
}

/** Top statements uses the same bounded histogram endpoint as Query digest;
 * its inline link reads only on request and cancels when the source changes. */
function StatementHistogram({ row, connection, disabled }: { row: TopStatement; connection: Connection; disabled: boolean }) {
  const key = JSON.stringify([connectionKey(connection), row.schema, row.digest]);
  const controller = useRef<AbortController | null>(null);
  const [state, setState] = useState<{ key: string; pending?: boolean; data?: DigestHistogram; error?: string }>({ key });
  useEffect(() => { controller.current?.abort(); setState({ key }); return () => controller.current?.abort(); }, [key, disabled]);
  const current = state.key === key ? state : { key };
  async function read() {
    if (disabled || !row.digest || current.pending) return;
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setState({ key, pending: true });
    try {
      const data = await readDigestHistogram(connection, row.schema, row.digest, request.signal);
      if (!request.signal.aborted) setState({ key, data });
    } catch (e) { if (!request.signal.aborted) setState({ key, error: e instanceof Error ? e.message : String(e) }); }
  }
  return <div>
    <button className="text-button" disabled={disabled || current.pending} onClick={() => void read()}>Latency histogram</button>
    <ViewStatus loading={current.pending ? "Reading the latency histogram…" : ""} error={current.error || ""} />
    {current.data && (current.data.available ? <DataTable label="Latency histogram" compact
      rows={current.data.buckets.filter(bucket => bucket.count > 0)} rowKey={bucket => String(bucket.bucket)}
      columns={[
        { key: "range", label: "Time", render: bucket => bucket.high_ms === null ? `${time(bucket.low_ms)} or more` : `${time(bucket.low_ms)} to ${time(bucket.high_ms)}` },
        { key: "calls", label: "Calls", num: true, render: bucket => number(bucket.count) },
      ]} /> : <p className="hint"><bdi>{current.data.reason}</bdi></p>)}
  </div>;
}
