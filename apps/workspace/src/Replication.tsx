// Server > Workload > Replication: a read-only table of channels (MySQL) or
// connections (MariaDB) with an inspector. Recovery is always a separately
// confirmed action; Python owns the statements, the final re-read, the
// connection's write policy and the audit record.
import { useEffect, useRef, useState } from "react";
import type { ActionPrivilege } from "./accountsApi";
import { privilegeResult, usePrivilege } from "./privileges";
import { recordDeclined, type Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import { InstrumentsLink } from "./Instruments";
import {
  Checkbox, ConfirmSheet, DataTable, EmptyState, Fields,
  InspectorSection, NumberField, PrerequisiteGate, StatusPill, SummaryLine,
  ToolbarButton, ViewLayout, ViewStatus, worstTone,
  type Column, type InspectorContent, type Selection, type Tone,
} from "./kit";
import { Commands, FactsList, channelCell, channelPhrase, engineName, plural } from "./OpsShared";
import {
  factText, loopActive, readReplication, readSkipLoops, skipTransaction,
  startSkipLoop, stopSkipLoop, useOpsScope,
  type Json, type LoopOptions, type MissingPrivileges, type ReplicationChannel,
  type ReplicationReport, type SkipLoop, type SkipPlan, type SkipResult,
} from "./opsApi";
import { VisiblePrivilegeGate, VisiblePrivilegeConfirmLabel } from "./Locks";
import "./ops.css";

const SKIP_TAG = "/*myflames:replication-skip*/ ";
/** A confirmation older than this is planned again before anything is sent. */
export const PLAN_TTL_MS = 60_000;
/** Loop status is read this often while a loop runs (no database connection). */
const LOOP_POLL_MS = 2000;
const COMMON_ERRORS: [number, string][] = [[1062, "Duplicate key"], [1032, "Row not found"], [1452, "Foreign key child row"]];
const lag = (value: number | null | undefined) => (value === null || value === undefined ? "Not available" : `${value.toLocaleString()} s`);
const channelKey = (channel: ReplicationChannel) => `channel:${channel.name}`;
const SKIP_STATUS: Record<string, [Tone, string]> = {
  skipped: ["ok", "Skipped"],
  refused: ["watch", "Refused"],
  needs_confirmation: ["watch", "Not confirmed"],
  denied: ["problem", "Denied"],
  refused_by_server: ["problem", "Refused by the server"],
  failed: ["problem", "Failed"],
};
function time(iso: Json | undefined) {
  if (typeof iso !== "string") return "";
  const at = Date.parse(iso);
  return Number.isFinite(at) ? new Date(at).toLocaleTimeString() : iso;
}

/** One line per skip loop of the connection in use: what it skips, how many,
 * the last one, and Stop while it runs. Used in this view and in Server's
 * persistent status area. Stop cancels a loop the person already authorized,
 * so it never goes through the write policy or a second prompt. */
export function ReplicationLoopBanner({ loops, disabled, stopping, onStop }: {
  loops: SkipLoop[]; disabled?: boolean; stopping?: string; onStop: (loop: SkipLoop) => void;
}) {
  return (
    <>
      {loops.map((loop) => {
        const last = loop.skips?.length ? loop.skips[loop.skips.length - 1] : null;
        const active = loopActive(loop);
        return (
          <div
            className={`ops-loop${active ? "" : " is-stopped"}${loop.needs_attention ? " needs-attention" : ""}`}
            key={loop.job_id || loop.channel}
            role="status"
            data-loop={loop.state}
          >
            <span>
              <strong>{loop.message}</strong>
              {active && loop.receiver_guard === "off" && <span className="ops-last" title={loop.receiver_guard_reason || loop.receiver_guard_note}>Reconnect detection off</span>}
              {last && (
                <span className="ops-last">
                  Last skip: {factText(last.gtid || last.exec_pos)}
                  {last.at ? ` at ${time(last.at)}` : ""}
                </span>
              )}
            </span>
            {active && (
              <ToolbarButton disabled={disabled || !loop.job_id || stopping === loop.job_id} onClick={() => onStop(loop)}>
                {stopping === loop.job_id || loop.state === "stopping" ? "Stopping…" : "Stop"}
              </ToolbarButton>
            )}
          </div>
        );
      })}
    </>
  );
}

type Confirmation = {
  kind: "one" | "loop"; connection: Connection; key: string; label: string;
  engine: string; plan: SkipPlan; at: number; options: LoopOptions;
};

export function ReplicationView({ connection, disabled, onBack, selection, report: bound, onReport, loops: boundLoops, onLoops, restoredLoop = false, onLoopActivity, missingPrivileges = {} }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  report: Bound<ReplicationReport> | null; onReport: (report: Bound<ReplicationReport>) => void;
  /** Memory only, owned by Server. Keep this view mounted while hidden. */
  loops: Bound<SkipLoop[]> | null; onLoops: (loops: Bound<SkipLoop[]>) => void;
  /** The only loop state a restored workspace may retain is this warning flag. */
  restoredLoop?: boolean; onLoopActivity?: (active: boolean) => void;
  missingPrivileges?: MissingPrivileges;
}) {
  const reads = useConnectionReads(connection);
  const captureScope = useOpsScope(connection);
  const policy = useWritePolicy();
  const report = bound?.report || null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const loops = boundLoops?.key === reads.key ? boundLoops.report : [];
  // The loop registry matches a loop by its connection's endpoint
  // (dbops.identity_endpoint), so after a change of type or Read-only on the
  // same saved connection the loop is read again under the new key and keeps
  // its Stop. Any other connection has its own loops.
  const endpointOf = (value: string) => {
    try {
      const parts: unknown = JSON.parse(value);
      return Array.isArray(parts) ? JSON.stringify(parts.slice(0, 6)) : value;
    } catch {
      return value;
    }
  };
  const sameEndpoint = !!boundLoops && endpointOf(boundLoops.key) === endpointOf(reads.key);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const confirmationRef = useRef(confirmation);
  confirmationRef.current = confirmation;
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [stopping, setStopping] = useState("");
  const stoppingRef = useRef(new Set<string>());
  const [failure, setFailure] = useState<{ key: string; text: string; loop?: boolean } | null>(null);
  const [notice, setNotice] = useState("");
  const [outcome, setOutcome] = useState<Bound<SkipResult> | null>(null);
  const gtidGate = usePrivilege("skip_gtid");
  const counterGate = usePrivilege("skip_counter");
  function skipGate(plan?: SkipPlan) {
    if (!plan?.method) return privilegeResult();
    // Execution returns a complete target answer, including tagged GTIDs.
    // Reuse it only for exactly the failure it checked on this connection.
    const result = outcome?.key === reads.key ? outcome.report : null;
    const checked = (result as (SkipResult & { privilege_check?: ActionPrivilege }) | null)?.privilege_check;
    if (checked && JSON.stringify(result?.plan?.identity) === JSON.stringify(plan.identity)) return privilegeResult(checked);
    const generic = plan.method === "empty_gtid_transaction" ? gtidGate : plan.method === "skip_counter" ? counterGate : privilegeResult();
    // A tagged GTID needs an additional target check. A base denial is still
    // definitive; a generic allowance alone cannot verify the whole script.
    return plan.method === "empty_gtid_transaction" && String(plan.skip.gtid || "").split(":").length > 2 && !generic.disabledReason ? privilegeResult() : generic;
  }
  const actionBlock = (kind: Confirmation["kind"], plan?: SkipPlan) => missingPrivileges[kind === "one" ? "replication_skip" : "replication_skip_loop"] || skipGate(plan).disabledReason;
  const publishLoops = useRef(onLoops);
  const activity = useRef(onLoopActivity);
  publishLoops.current = onLoops;
  activity.current = onLoopActivity;
  const key = reads.key;
  const cRef = useRef(connection);
  cRef.current = connection;
  // Loop status reads no database: it lists the loops this myflames holds for
  // the connection. It is read once a report shows a replica (or a restored
  // workspace says a loop ran), again while a loop runs, and after Start or
  // Stop. Polling never starts a loop; a loop whose Start reply was lost is
  // found here, without sending Start again.
  const pollEnabled = !!(bound?.key === key && report?.available && report.replica) || sameEndpoint || restoredLoop;
  const [restoredKey] = useState(key);
  const [pollRetry, setPollRetry] = useState(0);
  const [loopChecked, setLoopChecked] = useState("");
  const [loopFailure, setLoopFailure] = useState("");
  useEffect(() => {
    if (!pollEnabled) return;
    const controller = new AbortController();
    let timer: number | undefined;
    const c = { ...cRef.current };
    async function poll() {
      try {
        const answer = await readSkipLoops(c, controller.signal);
        if (controller.signal.aborted) return;
        publishLoops.current({ key, label: connectionLabel(c), report: answer.loops });
        const running = answer.loops.some(loopActive);
        activity.current?.(running);
        setLoopChecked(key);
        setLoopFailure("");
        setFailure((f) => (f?.loop ? null : f));
        if (running) timer = window.setTimeout(() => { void poll(); }, LOOP_POLL_MS);
      } catch (e) {
        if (!controller.signal.aborted) {
          setLoopFailure(key);
          setFailure({ key, loop: true, text: `Skip loop status could not be read: ${(e as Error).message}. Recovery stays off until it is read.` });
        }
      }
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [key, pollEnabled, pollRetry]);

  function decline(c: Confirmation) {
    void recordDeclined(c.kind === "one" ? "replication_skip" : "replication_skip_loop",
      c.plan.statements.map((s) => SKIP_TAG + s), c.connection,
      { ...c.plan.identity, ...(c.kind === "loop" ? c.options : {}) });
  }
  // Switching the connection closes and audits an open sheet. A request
  // already sent belongs to its original connection and is never repeated.
  useEffect(() => {
    const c = confirmationRef.current;
    if (c && c.key !== key && !sending.current) { decline(c); setConfirmation(null); }
    setNotice("");
  }, [key]);
  useEffect(() => () => {
    const c = confirmationRef.current;
    if (c && !sending.current) decline(c);
  }, []);

  async function load() {
    if (disabled || sending.current) return;
    await reads.run("replication", "Reading replication…", readReplication, onReport);
  }
  async function open(kind: Confirmation["kind"], channel: ReplicationChannel) {
    if (disabled || other || sending.current || policy?.blocked || connection.read_only || loopFailure === key || loopChecked !== key || actionBlock(kind, channel.skip_plan)) return;
    // Read again so the sheet shows the failure as it is now.
    const current = await reads.run("replication", "Checking the failed transaction…", readReplication, onReport, { force: true });
    if (!current) return;
    const plan = current.report.channels.find((c) => c.name === channel.name)?.skip_plan;
    if (!plan?.available) { setFailure({ key: current.key, text: plan?.message || "The failed transaction changed. Select the channel again." }); return; }
    if (actionBlock(kind, plan)) { setFailure({ key: current.key, text: actionBlock(kind, plan) }); return; }
    setNotice("");
    setFailure(null);
    setConfirmation({
      kind, connection: { ...connection }, key: current.key, label: current.label, engine: current.report.engine,
      plan, at: Date.now(), options: { codes: [], max_skips: 10, max_minutes: 10, poll_s: 2 },
    });
  }
  async function confirm() {
    const c = confirmation;
    if (!c || sending.current || c.key !== reads.currentKey() || blocked || actionBlock(c.kind, c.plan) || loopFailure === key || loopChecked !== key) return;
    sending.current = true;
    setBusy(true);
    const live = captureScope();
    try {
      const checked = policy ? await policy.check() : c.connection;
      if (!live() || connectionKey(checked) !== c.key) throw new Error("The connection or its write policy changed. Nothing was sent. Refresh and confirm again.");
      // An old or changed plan is shown again and needs another click. Python
      // repeats this check immediately before sending the script.
      const fresh = await readReplication(checked);
      if (!live()) return;
      onReport({ key: c.key, label: c.label, report: fresh });
      const plan = fresh.channels.find((row) => row.name === c.plan.channel)?.skip_plan;
      if (!plan?.available) { setConfirmation(null); throw new Error(plan?.message || "There is no failed transaction to skip now. Nothing was sent."); }
      if (actionBlock(c.kind, plan)) { setConfirmation({ ...c, plan }); setNotice(actionBlock(c.kind, plan)); return; }
      if (Date.now() - c.at > PLAN_TTL_MS || JSON.stringify(plan.identity) !== JSON.stringify(c.plan.identity) || JSON.stringify(plan.statements) !== JSON.stringify(c.plan.statements)) {
        setConfirmation({ ...c, connection: checked, plan, at: Date.now() });
        setNotice("The failed transaction was read again and the plan was refreshed. Review it and confirm again. Nothing was sent.");
        return;
      }
      if (c.kind === "one") {
        const result = await skipTransaction(checked, c.plan);
        if (!live()) return;
        setOutcome({ key: c.key, label: c.label, report: result });
        setConfirmation(null);
        await reads.run("replication", "Reading the result…", readReplication, onReport, { force: true });
      } else {
        // Keep the mode the person reviewed, even if coverage changed during
        // the fresh read. Python requires an acknowledged active guard.
        const result = await startSkipLoop(checked, c.plan.channel, { ...c.options, receiver_guard: c.plan.receiver_guard });
        if (!live()) return;
        setConfirmation(null);
        const merged = [...loops.filter((l) => l.channel !== result.channel || loopActive(l)), result];
        publishLoops.current({ key: c.key, label: c.label, report: merged });
        activity.current?.(merged.some(loopActive));
        if (result.state === "refused" || result.state === "needs_confirmation") {
          setFailure({ key: c.key, text: [result.message, result.policy?.text].filter(Boolean).join(" ") });
        }
        // Read the registry rather than trusting this reply alone.
        setPollRetry((n) => n + 1);
      }
    } catch (e) {
      if (live()) {
        setFailure({ key: c.key, text: (e as Error).message });
        setConfirmation(null);
        // A loop whose Start reply was lost shows up in the status read.
        if (c.kind === "loop") setPollRetry((n) => n + 1);
      }
    } finally { sending.current = false; setBusy(false); if (!live()) setConfirmation(null); }
  }
  async function stop(loop: SkipLoop) {
    const id = loop.job_id;
    if (!id || stoppingRef.current.has(id) || boundLoops?.key !== reads.currentKey()) return;
    stoppingRef.current.add(id); setStopping(id);
    const live = captureScope();
    try {
      await stopSkipLoop({ ...connection }, id);
      if (live()) setPollRetry((n) => n + 1);
    } catch (e) { if (live()) setFailure({ key, text: (e as Error).message }); }
    finally { stoppingRef.current.delete(id); setStopping((pending) => pending === id ? "" : pending); }
  }

  const engine = report?.engine;
  const chosen = report?.channels.find((c) => channelKey(c) === selection.key);
  const blocked = policy?.blocked || (connection.read_only ? "Read-only: myflames refuses every change on this connection." : "");
  const loopGate = loopFailure === key ? "Skip loop status could not be read. Use Retry status." : loopChecked !== key ? "Reading skip loop status…" : "";
  const channelBusy = !!chosen && loops.some((l) => l.channel === chosen.name && loopActive(l));
  const unavailable = chosen?.kind === "group_replication" ? "Group Replication channels are not judged and cannot be skipped here." : chosen?.skip_plan?.message || (chosen?.sql.error.errno ? "No safe skip plan for this failure." : "Nothing to skip: the applier reports no error.");
  const chosenGate = skipGate(chosen?.skip_plan);
  const recoveryPrivileges: MissingPrivileges = {
    ...missingPrivileges,
    replication_skip: actionBlock("one", chosen?.skip_plan),
    replication_skip_loop: actionBlock("loop", chosen?.skip_plan),
  };

  const hasError = !!chosen && (!!chosen.sql.error.errno || !!chosen.io.error.errno);
  // Error 3030: a worker stopped because an earlier transaction failed; the
  // failure to act on is the worker's that failed first.
  const workerError = (chosen?.worker as { error?: { errno?: number; message?: string } } | undefined)?.error;
  const firstFailure = chosen && chosen.sql.error.errno === 3030 && workerError?.errno && workerError.errno !== 3030 ? { errno: workerError.errno, message: workerError.message || "" } : null;
  const inspector: InspectorContent | null = chosen ? {
    key: channelKey(chosen), title: channelCell(chosen.name),
    meta: <StatusPill tone={chosen.judged ? chosen.status : "unknown"} label={chosen.judged ? undefined : "Not judged"} />,
    actions: <>
      <VisiblePrivilegeGate action={chosen.skip_plan?.method === "empty_gtid_transaction" ? "skip_gtid" : "skip_counter"} gate={chosenGate}
        blocked={blocked || loopGate || recoveryPrivileges.replication_skip}
        disabled={disabled || !!other || busy || channelBusy || !chosen.skip_plan?.available}
        onClick={() => { void open("one", chosen); }}>Skip this transaction…</VisiblePrivilegeGate>
      <VisiblePrivilegeGate action={chosen.skip_plan?.method === "empty_gtid_transaction" ? "skip_gtid" : "skip_counter"} gate={chosenGate}
        blocked={blocked || loopGate || recoveryPrivileges.replication_skip_loop}
        disabled={disabled || !!other || busy || channelBusy || !chosen.skip_plan?.available}
        onClick={() => { void open("loop", chosen); }}>Skip until stopped…</VisiblePrivilegeGate>
    </>,
    body: <>
      <InspectorSection title="Status">
        <Fields rows={[
          ["Source", `${chosen.source.host || "Unknown"}:${chosen.source.port ?? "?"}`],
          ["I/O thread", [chosen.io.running || "Unknown", chosen.io.state].filter(Boolean).join(": ")],
          ["SQL thread", [chosen.sql.running || "Unknown", chosen.sql.state].filter(Boolean).join(": ")],
          ["Lag", lag(chosen.lag_s)],
          !!chosen.sql.delay_s && ["Configured delay", lag(chosen.sql.delay_s)],
        ]} />
        {!hasError && chosen.status_why.map((s, i) => <p key={i}>{s}</p>)}
        {!chosen.judged && <p>Group Replication channels are listed without a verdict.</p>}
      </InspectorSection>
      {hasError && <InspectorSection title="Last error">
        <Fields rows={[
          !!chosen.sql.error.errno && ["SQL thread", `Error ${chosen.sql.error.errno}${chosen.sql.error.timestamp ? ` at ${chosen.sql.error.timestamp}` : ""}`],
          !!chosen.io.error.errno && ["I/O thread", `Error ${chosen.io.error.errno}${chosen.io.error.timestamp ? ` at ${chosen.io.error.timestamp}` : ""}`],
          !!chosen.sql.error.table && ["Table", chosen.sql.error.table],
          firstFailure && ["Failed first", `Error ${firstFailure.errno} (worker ${String(chosen.worker.failing_worker_id ?? "?")})`],
          !!chosen.failing_gtid && ["Failing GTID", <code className="ops-wrap">{chosen.failing_gtid}</code>],
        ]} />
        <pre className="ops-pre">{chosen.sql.error.message || chosen.io.error.message}</pre>
        {firstFailure && <pre className="ops-pre">{firstFailure.message}</pre>}
      </InspectorSection>}
      <InspectorSection title="Lag signals"><FactsList facts={chosen.lag_signals} />{chosen.lag_notes.map((s, i) => <p className="hint" key={i}>{s}</p>)}</InspectorSection>
      <InspectorSection title="GTID and positions">
        {chosen.agat && <Fields rows={[["Assign GTIDs to anonymous transactions", chosen.agat]]} />}
        <FactsList facts={chosen.position} />
        {report && <FactsList facts={report.gtid} />}
      </InspectorSection>
      {(chosen.skip_plan || blocked) && <InspectorSection title="Recovery">
        {blocked && <p>{blocked}</p>}
        <p>{chosen.skip_plan?.available ? chosen.skip_plan.privileges : unavailable}</p>
        {chosen.skip_plan?.available && <p>{chosen.skip_plan.drift_warning}</p>}
        {chosen.skip_plan?.notes.map((note, i) => <p className="hint" key={i}>{note}</p>)}
      </InspectorSection>}
      <details className="ops-raw"><summary>Raw channel status</summary><pre className="ops-pre">{JSON.stringify(chosen, null, 2)}</pre></details>
    </>,
  } : null;
  const columns: Column<ReplicationChannel>[] = [
    { key: "name", label: engine === "mariadb" ? "Connection" : "Channel", primary: true, flex: true, sort: (r) => r.name, render: (r) => channelCell(r.name) },
    { key: "status", label: "Status", sort: (r) => r.status, render: (r) => <StatusPill tone={r.judged ? r.status : "unknown"} label={r.judged ? undefined : "Not judged"} /> },
    { key: "source", label: "Source", sort: (r) => r.source.host, render: (r) => `${r.source.host || "Unknown"}:${r.source.port ?? "?"}` },
    { key: "io", label: "I/O", sort: (r) => r.io.running, render: (r) => <span title={r.io.state || undefined}>{r.io.running || "Unknown"}</span> },
    { key: "sql", label: "SQL", sort: (r) => r.sql.running, render: (r) => <span title={r.sql.state || undefined}>{r.sql.running || "Unknown"}</span> },
    { key: "lag", label: "Lag (s)", num: true, title: "Seconds behind the source; see the inspector for the clock, receiver and delay caveats.", sort: (r) => r.lag_s, render: (r) => <abbr title={r.lag_notes.join(" ") || "Seconds behind the source alone does not show that the replica is caught up."}>{r.lag_s === null ? "Unknown" : r.lag_s.toLocaleString()}</abbr> },
    { key: "error", label: "Error", num: true, sort: (r) => r.sql.error.errno || r.io.error.errno, render: (r) => r.sql.error.errno || r.io.error.errno || "None" },
  ];
  const error = failure?.key === key ? failure.text : reads.error;
  const result = outcome?.key === key ? outcome.report : null;
  const resultTone = result ? SKIP_STATUS[result.status] || (["unknown", result.status] as [Tone, string]) : null;
  const c = confirmation;
  const errorCodes = c ? [...new Set([...COMMON_ERRORS.map(([code]) => code), c.plan.error.errno])] : [];
  const validLoop = !!c && c.options.codes.length > 0 && Number.isInteger(c.options.max_skips) && c.options.max_skips >= 1 && c.options.max_skips <= 1000 && Number.isInteger(c.options.max_minutes) && c.options.max_minutes >= 1 && c.options.max_minutes <= 1440;
  const word = engine === "mariadb" ? "connection" : "channel";
  const summary = report
    ? !report.available
      ? "Replication status not available"
      : !report.replica
        ? "Not a replica"
        : [
            engineName(report.engine, report.server_version),
            plural(report.summary.channels, word),
            report.summary.failed ? `${report.summary.failed.toLocaleString()} failed` : "",
            report.summary.lag_max_s === null ? "lag unknown" : `max lag ${report.summary.lag_max_s.toLocaleString()} s`,
          ].filter(Boolean).join(" · ")
    : "";
  const judged = report ? report.channels.filter((r) => r.judged) : [];
  // Loops are read for the connection in use (by endpoint), apart from the report.
  const loopsShown = loops;
  // The restored flag belongs to the connection the workspace was saved with.
  const restartNote = restoredLoop && key === restoredKey && loopChecked === key && !loops.length;
  return <>
    <ViewLayout id="replication" title="Replication" className="ops-view" onBack={onBack} onRefresh={() => { void load(); }} refreshDisabled={disabled || busy} loading={!!reads.loading} otherConnection={other} inspector={inspector}
      inspectorEmpty={<p>Select a {word} to see its threads, last error, lag signals and recovery.</p>}
      info={<><p>Each replication channel (MySQL) or connection (MariaDB) of the connection in use: thread states, the last error, lag signals and positions. Reading changes nothing.</p><p>Skip this transaction and Skip until stopped are changes: each asks first, shows the exact statements, and is recorded in the Audit log. A skipped transaction is missing on the replica.</p></>}
      status={<ViewStatus loading={reads.loading} error={error}>{failure?.key === key && failure.loop && <button className="text-button" type="button" onClick={() => { setFailure(null); setPollRetry((n) => n + 1); }}>Retry status</button>}</ViewStatus>}>
      {loopsShown.length > 0 && <ReplicationLoopBanner loops={loopsShown} stopping={stopping} onStop={(l) => { void stop(l); }} />}
      {restartNote && <p className="ops-note" role="status">Stopped when myflames restarted. Skip loops never resume from a restored workspace.</p>}
      {report ? <>
        <SummaryLine tone={!report.available || !report.replica ? "unknown" : judged.length ? worstTone(judged.map((r) => r.status)) : "unknown"} toneLabel={judged.length || !report.replica ? undefined : "Not judged"} notes={report.notes.length ? report.notes : undefined}>
          {summary}
        </SummaryLine>
        {!report.available || !report.replica ? <PrerequisiteGate>{report.message || "Replication status is not available for this connection."}</PrerequisiteGate> : <DataTable id="replication.channels" label={engine === "mariadb" ? "Replication connections" : "Replication channels"} columns={columns} rows={report.channels} rowKey={channelKey} selection={selection} />}
        {result && resultTone && <section className="ops-result" aria-label="Skip result" role="status" data-status={result.status}>
          <div className="ops-result-head"><StatusPill tone={resultTone[0]} label={resultTone[1]} /><strong>{channelPhrase(engine, result.channel)}</strong></div>
          <p>{result.message}</p>
          {result.policy?.text && <p>{result.policy.text}</p>}
          {result.after && <p className="hint">{result.after.passed ? "Checked after: the applier moved past the skipped transaction." : "Checked after: the applier did not move past it. Read the status again."}</p>}
          {result.status === "skipped" && result.plan && <details><summary>What to do next</summary><p>{result.plan.drift_warning}</p><ul>{result.plan.after.map((s, i) => <li key={i}>{s}</li>)}</ul></details>}
        </section>}
      </> : <EmptyState icon="replication" title="Read replication status" action={<ToolbarButton primary disabled={disabled || !!reads.loading} onClick={() => { void load(); }}>Load</ToolbarButton>}>Channels, thread states, the last error and lag signals of the connection in use. It only reads.</EmptyState>}
    </ViewLayout>
    {c && <ConfirmSheet className="ops-confirm" eyebrow={c.kind === "one" ? "SKIP TRANSACTION" : "SKIP UNTIL STOPPED"}
      title={`${c.kind === "one" ? "Skip the failed transaction" : "Skip failures until stopped"} on ${channelPhrase(c.engine, c.plan.channel)}?`}
      description="myflames reads the failure again right before sending and stops if it changed. A skipped transaction cannot be undone."
      busy={busy} confirmLabel={<VisiblePrivilegeConfirmLabel gate={{ ...skipGate(c.plan), disabledReason: blocked || loopGate || actionBlock(c.kind, c.plan) }}>{busy ? "Working…" : c.kind === "one" ? "Skip transaction" : "Start skipping"}</VisiblePrivilegeConfirmLabel>}
      confirmDisabled={c.key !== key || !!loopGate || (c.kind === "loop" && !validLoop)}
      onCancel={() => { decline(c); setConfirmation(null); setNotice(""); }} onConfirm={() => { void confirm(); }}>
      <Fields rows={[
        ["Connection", c.label],
        ["Server", engineName(c.plan.engine, c.plan.server_version)],
        ["Failing transaction", <code className="ops-wrap">{c.plan.identity.gtid || factText(c.plan.identity.position)}</code>],
        ["Error", `${c.plan.error.errno}${c.plan.error.table ? ` on ${c.plan.error.table}` : ""}`],
        ["Privileges", c.plan.privileges],
      ]} />
      <p className="ops-drift">{c.plan.drift_warning}</p>
      {c.kind === "loop" && c.plan.receiver_guard && <p className="hint ops-receiver-guard" title={c.plan.receiver_guard_reason || undefined}>
        {c.plan.receiver_guard === "on" ? "Stops if a receiver reconnects." : <>
          {c.plan.receiver_guard_note}{" "}
          {!busy && <span onClick={() => { decline(c); setConfirmation(null); setNotice(""); }}>
            <InstrumentsLink row="feature:replication_receiver" />
          </span>}
        </>}
      </p>}
      {c.plan.notes.map((note, i) => <p className="hint ops-plan-note" key={i}>{note}</p>)}
      <Commands label="Exact skip statements" text={c.plan.statements.map((s) => SKIP_TAG + s).join(";\n") + ";"} />
      {c.kind === "loop" && <fieldset className="ops-loop-options" disabled={busy}><legend>Skip only these errors</legend>
        {errorCodes.map((code) => <Checkbox key={code} checked={c.options.codes.includes(code)} onChange={(checked) => setConfirmation({ ...c, options: { ...c.options, codes: checked ? [...c.options.codes, code] : c.options.codes.filter((v) => v !== code) } })}>{code} {COMMON_ERRORS.find(([n]) => n === code)?.[1] || "The error now"}</Checkbox>)}
        <label>Stop after this many skips<NumberField value={c.options.max_skips} min={1} max={1000} onChange={(v) => setConfirmation({ ...c, options: { ...c.options, max_skips: +v } })} /></label>
        <label>Stop after this many minutes<NumberField value={c.options.max_minutes} min={1} max={1440} onChange={(v) => setConfirmation({ ...c, options: { ...c.options, max_minutes: +v } })} /></label>
        <p className="hint">The statements above skip the failure there now. Each later failure is planned again, checked, skipped and recorded on its own. It stops at the first other error, at either limit, or when you press Stop, and never resumes after myflames restarts.</p>
      </fieldset>}
      {notice && <p className="ops-notice" role="status">{notice}</p>}
      <details><summary>After skipping</summary><ul>{c.plan.after.map((s, i) => <li key={i}>{s}</li>)}</ul></details>
    </ConfirmSheet>}
  </>;
}
