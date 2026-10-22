// Server > Workload > Replication: a read-only table of channels (MySQL) or
// connections (MariaDB) with an inspector. Recovery is always a separately
// confirmed action; Python owns the statements, the final re-read, the
// connection's write policy and the audit record.
import { useEffect, useRef, useState } from "react";
import { durationText } from "./duration";
import { Duration } from "./PerfShared";
import { useViewOnScreen } from "./viewOnScreen";
import type { ActionPrivilege } from "./accountsApi";
import { privilegeResult, usePrivilege } from "./privileges";
import { recordDeclined, type Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import { InstrumentsLink } from "./Instruments";
import {
  ActionButton, ActionStatus, Checkbox, ConfirmSheet, DataTable, Disclosure, EmptyState, FactStrip, Fields,
  InspectorSection, MetricTile, NumberField, PrerequisiteGate, Segmented, StatusPill, SummaryLine,
  TileGrid, ToolbarButton, ViewLayout, ViewStatus, worstTone, useInspector, useSince, writeFailure,
  type Column, type InspectorContent, type Selection, type Tone,
} from "./kit";
import { Commands, FactsList, channelCell, channelPhrase, engineName, factRows, formatGtidSet, plural } from "./OpsShared";
import {
  factText, loopActive, readReplication, readSkipLoops, skipTransaction,
  startSkipLoop, stopSkipLoop, useOpsScope,
  type Json, type LoopOptions, type MissingPrivileges, type ReplicationChannel,
  type ReplicationReport, type SkipLoop, type SkipPlan, type SkipResult,
} from "./opsApi";
import { VisiblePrivilegeGate, VisiblePrivilegeConfirmLabel } from "./Locks";
import type { ClusterKind } from "./clusterApi";
import { ReplicationTopology, sourceVersionNote, sourceVersionText } from "./ReplicationTopology";
import "./ops.css";
import "./replication.css";

const SKIP_TAG = "/*myflames:replication-skip*/ ";
/** A confirmation older than this is planned again before anything is sent. */
export const PLAN_TTL_MS = 60_000;
/** Loop status is read this often while a loop runs (no database connection). */
const LOOP_POLL_MS = 2000;
const COMMON_ERRORS: [number, string][] = [[1062, "Duplicate key"], [1032, "Row not found"], [1452, "Foreign key child row"]];
const lag = (value: number | null | undefined) => (value === null || value === undefined ? "Not available" : <Duration value={value} />);
const lagCell = (channel: ReplicationChannel) => {
  const note = channel.lag_notes.join(" ") || "Seconds behind the source alone does not show that the replica is caught up.";
  return channel.lag_s === null ? <abbr title={note}>Unknown</abbr> : <Duration value={channel.lag_s} note={note} />;
};
const channelKey = (channel: ReplicationChannel) => `channel:${channel.name}`;
/** Server-wide GTID facts named the way the server names them, so they read
 * unambiguously beside the channel's own positions. */
const GTID_LABELS: Record<string, string> = {
  mode: "GTID mode", gtid_mode: "GTID mode", enforce_consistency: "Enforce GTID consistency",
  slave_pos: "GTID slave position", current_pos: "GTID current position",
  domain_id: "GTID domain ID", strict_mode: "GTID strict mode",
};
/** "binlog.000003:3720", from either the normalized or the server's own keys. */
function logPosition(position: ReplicationChannel["position"] | undefined, file: string[], pos: string[]) {
  const pick = (keys: string[]) => keys.map((k) => position?.[k]).find((v) => v !== undefined && v !== null && v !== "");
  const f = pick(file);
  if (f === undefined) return "Unknown";
  const at = pick(pos);
  return at === undefined ? String(f) : `${String(f)}:${String(at)}`;
}
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
  return <>{loops.map(loop => <ReplicationLoopRow key={loop.job_id || loop.channel} loop={loop}
    disabled={disabled} stopping={stopping === loop.job_id} onStop={onStop} />)}</>;
}

function ReplicationLoopRow({ loop, disabled, stopping, onStop }: {
  loop: SkipLoop; disabled?: boolean; stopping: boolean; onStop: (loop: SkipLoop) => void;
}) {
  const last = loop.skips?.length ? loop.skips[loop.skips.length - 1] : null;
  const active = loopActive(loop);
  const since = useSince(stopping);
  const row = useRef<HTMLDivElement>(null);
  const stopHadFocus = useRef(false);
  // If the status poll removes Stop, keep focus on its result rather than body.
  useEffect(() => {
    if (stopHadFocus.current && !active && !stopping && document.activeElement === document.body) row.current?.focus();
    if (!active && !stopping) stopHadFocus.current = false;
  }, [active, stopping]);
  return (
    <div ref={row} tabIndex={-1}
      className={`ops-loop${active ? "" : " is-stopped"}${loop.needs_attention ? " needs-attention" : ""}`}
      role="status" data-loop={loop.state}>
      <div className="ops-loop-copy">
        <strong>{loop.message}</strong>
        {active && loop.receiver_guard === "off" && <span className="ops-last" title={loop.receiver_guard_reason || loop.receiver_guard_note}>Reconnect detection off</span>}
        {last && <span className="ops-last">
          Last skip: {factText(last.gtid || last.exec_pos)}
          {last.at ? ` at ${time(last.at)}` : ""}
        </span>}
        <ActionStatus state={{ phase: stopping ? "pending" : "idle", startedAt: since, message: "Waiting for the reply to Stop." }}
          slowAfter={10_000} slowText="No reply to Stop yet; the loop may still be running." />
      </div>
      {(active || stopping) && <ActionButton busy={stopping || loop.state === "stopping"} busyLabel="Stopping…"
        disabled={!stopping && (disabled || !loop.job_id)}
        onClick={() => { stopHadFocus.current = true; onStop(loop); }}>Stop</ActionButton>}
    </div>
  );
}

type Confirmation = {
  kind: "one" | "loop"; connection: Connection; key: string; label: string;
  engine: string; plan: SkipPlan; at: number; options: LoopOptions;
};

export type ReplicationSubTab = "topology" | "channels" | "lag" | "gtid" | "recovery";

export function ReplicationView({ connection, disabled, selection, report: bound, onReport, loops: boundLoops, onLoops, restoredLoop = false, onLoopActivity, missingPrivileges = {}, activeTab = "topology", onTabChange, clusterKind = null, onOpenCluster }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  report: Bound<ReplicationReport> | null; onReport: (report: Bound<ReplicationReport>) => void;
  /** Memory only, owned by Server. Keep this view mounted while hidden. */
  loops: Bound<SkipLoop[]> | null; onLoops: (loops: Bound<SkipLoop[]>) => void;
  /** The only loop state a restored workspace may retain is this warning flag. */
  restoredLoop?: boolean; onLoopActivity?: (active: boolean) => void;
  missingPrivileges?: MissingPrivileges;
  activeTab?: ReplicationSubTab;
  onTabChange?: (tab: ReplicationSubTab) => void;
  /** The cluster the connection in use belongs to, when known: a node that
   * is not a replica says where its own replication is shown. */
  clusterKind?: ClusterKind | null;
  onOpenCluster?: () => void;
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
  const [checking, setChecking] = useState<Confirmation["kind"] | null>(null);
  const checkingRef = useRef(false);
  const reviewOpener = useRef<HTMLButtonElement | null>(null);
  const inspectorState = useInspector();
  const [left, setLeft] = useState(false);
  const since = useSince(busy);
  const readSince = useSince(!!reads.loading);
  const viewRef = useRef<HTMLDivElement>(null);
  const sending = useRef(false);
  const [stopping, setStopping] = useState("");
  const stoppingRef = useRef(new Set<string>());
  const [failure, setFailure] = useState<{ key: string; text: string; loop?: boolean; write?: boolean; label?: string } | null>(null);
  const [notice, setNotice] = useState("");
  const [outcome, setOutcome] = useState<Bound<SkipResult> | null>(null);
  const [loopResult, setLoopResult] = useState<Bound<SkipLoop> | null>(null);
  function focusResult(selector = ".ops-result, .replication-write-result, .ops-loop") {
    window.setTimeout(() => viewRef.current?.querySelector<HTMLElement>(selector)?.focus(), 0);
  }
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

  const onScreen = useViewOnScreen();
  // Reads when the view opens on screen: with no report yet (once per
  // connection), or, whatever was read before, when the report shown is from
  // another connection (the place stays mounted, hidden, while another
  // connection is chosen, or Refresh all read one). Choosing another
  // connection while the view stays on screen keeps the old report greyed
  // until Refresh, like the Server views.
  const autoLoaded = useRef<string | null>(null);
  const openedRead = useRef(false);
  useEffect(() => {
    if (!onScreen) { openedRead.current = false; return; }
    if (disabled || !reads.key) return;
    const staleOnOpen = !openedRead.current && !!bound && bound.key !== reads.key;
    openedRead.current = true;
    if (staleOnOpen || (!report && autoLoaded.current !== reads.key)) {
      if (reads.loading || sending.current) return;
      autoLoaded.current = reads.key;
      void load();
    }
  }, [reads.key, report, bound?.key, reads.loading, disabled, onScreen]);
  async function open(kind: Confirmation["kind"], channel: ReplicationChannel, opener: HTMLButtonElement) {
    if (disabled || other || sending.current || checkingRef.current || loops.some((l) => l.channel === channel.name && loopActive(l)) || policy?.blocked || connection.read_only || loopFailure === key || loopChecked !== key || actionBlock(kind, channel.skip_plan)) return;
    checkingRef.current = true;
    reviewOpener.current = opener;
    setChecking(kind);
    // Read again so the sheet shows the failure as it is now.
    try {
      const current = await reads.run("replication", "Checking the failed transaction…", readReplication, onReport, { force: true });
      if (!current) return;
      const plan = current.report.channels.find((c) => c.name === channel.name)?.skip_plan;
      if (!plan?.available) { setFailure({ key: current.key, text: plan?.message || "The failed transaction changed. Select the channel again." }); return; }
      if (actionBlock(kind, plan)) { setFailure({ key: current.key, text: actionBlock(kind, plan) }); return; }
      setNotice("");
      setFailure(null);
      setOutcome(null);
      setLoopResult(null);
      setLeft(false);
      setConfirmation({
        kind, connection: { ...connection }, key: current.key, label: current.label, engine: current.report.engine,
        plan, at: Date.now(), options: { codes: [], max_skips: 10, max_minutes: 10, poll_s: 2 },
      });
    } finally { checkingRef.current = false; setChecking(null); }
  }
  async function confirm() {
    const c = confirmation;
    if (!c || sending.current || c.key !== reads.currentKey() || blocked || actionBlock(c.kind, c.plan) || loopFailure === key || loopChecked !== key) return;
    sending.current = true;
    setBusy(true);
    const live = captureScope();
    let sent = false;
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
        sent = true;
        const result = await skipTransaction(checked, c.plan);
        setOutcome({ key: c.key, label: c.label, report: result });
        setConfirmation(null);
        focusResult(".ops-result");
        // The reply is the result; a hanging verification read must not hide it.
        if (live()) void reads.run("replication", "Reading the result…", readReplication, onReport, { force: true });
      } else {
        // Keep the mode the person reviewed, even if coverage changed during
        // the fresh read. Python requires an acknowledged active guard.
        sent = true;
        const result = await startSkipLoop(checked, c.plan.channel, { ...c.options, receiver_guard: c.plan.receiver_guard });
        setConfirmation(null);
        setLoopResult({ key: c.key, label: c.label, report: result });
        if (!live()) { focusResult(".replication-write-result"); return; }
        const merged = [...loops.filter((l) => l.channel !== result.channel || loopActive(l)), result];
        publishLoops.current({ key: c.key, label: c.label, report: merged });
        activity.current?.(merged.some(loopActive));
        if (result.state === "refused" || result.state === "needs_confirmation") {
          setFailure({ key: c.key, label: c.label, write: true, text: [result.message, result.policy?.text].filter(Boolean).join(" ") });
        }
        focusResult(".replication-write-result, .ops-loop");
        // Read the registry rather than trusting this reply alone.
        setPollRetry((n) => n + 1);
      }
    } catch (e) {
      if (sent || live()) {
        const failure = sent ? writeFailure(e, { audited: true }) : null;
        setFailure({ key: c.key, label: c.label, write: true, text: failure
          ? `${failure.phase === "unknown" ? "Outcome unknown. " : ""}${failure.message}`
          : `Nothing was sent. ${(e as Error).message}` });
        setConfirmation(null);
        focusResult(".replication-write-result");
        // A loop whose Start reply was lost shows up in the status read.
        if (c.kind === "loop" && live()) setPollRetry((n) => n + 1);
      }
    } finally { sending.current = false; setBusy(false); setLeft(false); if (!live()) setConfirmation(null); }
  }
  async function stop(loop: SkipLoop) {
    const id = loop.job_id;
    if (!id || stoppingRef.current.has(id) || boundLoops?.key !== reads.currentKey()) return;
    stoppingRef.current.add(id); setStopping(id);
    setFailure(null);
    const live = captureScope();
    try {
      await stopSkipLoop({ ...connection }, id);
      if (live()) setPollRetry((n) => n + 1);
    } catch (e) {
      const failure = writeFailure(e);
      setFailure({ key, label: connectionLabel(connection), write: true,
        text: `${failure.phase === "unknown" ? "Outcome unknown. " : ""}${failure.message}` });
      focusResult(".replication-write-result");
    }
    finally { stoppingRef.current.delete(id); setStopping((pending) => pending === id ? "" : pending); }
  }

  const engine = report?.engine;
  const chosen = report?.channels.find((c) => channelKey(c) === selection.key);
  // Progress below the initiating controls keeps a second pointer click on
  // the same button. If the inspector closes, progress returns to the view.
  const checkingInInspector = !!checking && !!inspectorState?.open && !!chosen &&
    !!reviewOpener.current?.closest(".view-inspector");
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
  // Recovery belongs to a channel whose applier stopped (or has a failure to
  // skip, or a skip loop of its own): a healthy channel shows no skip actions.
  const recoverable = !!chosen && chosen.kind !== "group_replication" &&
    (!!chosen.skip_plan?.available || !!chosen.sql.error.errno || chosen.sql.running === "No" || channelBusy);
  const inspector: InspectorContent | null = chosen ? {
    key: channelKey(chosen), title: channelCell(chosen.name),
    meta: <StatusPill tone={chosen.judged ? chosen.status : "unknown"} label={chosen.judged ? undefined : "Not judged"} />,
    actions: recoverable ? <>
      <VisiblePrivilegeGate action={chosen.skip_plan?.method === "empty_gtid_transaction" ? "skip_gtid" : "skip_counter"} gate={chosenGate}
        className={checking ? "replication-checking" : undefined}
        blocked={blocked || loopGate || recoveryPrivileges.replication_skip || (checking ? "Checking the failed transaction…" : "")}
        aria-busy={checking === "one" || undefined}
        disabled={disabled || !!other || busy || channelBusy || !chosen.skip_plan?.available}
        onClick={(e) => { void open("one", chosen, e.currentTarget); }}><span className="action-button-label" data-reserve={checking === "one" ? "Skip this transaction…" : "Checking…"}>
          <span>{checking === "one" ? <><span className="spinner" aria-hidden="true" />Checking…</> : "Skip this transaction…"}</span>
        </span></VisiblePrivilegeGate>
      <VisiblePrivilegeGate action={chosen.skip_plan?.method === "empty_gtid_transaction" ? "skip_gtid" : "skip_counter"} gate={chosenGate}
        className={checking ? "replication-checking" : undefined}
        blocked={blocked || loopGate || recoveryPrivileges.replication_skip_loop || (checking ? "Checking the failed transaction…" : "")}
        aria-busy={checking === "loop" || undefined}
        disabled={disabled || !!other || busy || channelBusy || !chosen.skip_plan?.available}
        onClick={(e) => { void open("loop", chosen, e.currentTarget); }}><span className="action-button-label" data-reserve={checking === "loop" ? "Skip until stopped…" : "Checking…"}>
          <span>{checking === "loop" ? <><span className="spinner" aria-hidden="true" />Checking…</> : "Skip until stopped…"}</span>
        </span></VisiblePrivilegeGate>
    </> : undefined,
    body: <>
      <ViewStatus loading={checkingInInspector ? reads.loading : undefined} startedAt={readSince ?? undefined}
        onStop={checkingInInspector ? reads.stop : undefined} />
      <InspectorSection title="Status">
        <Fields rows={[
          ["Source", `${chosen.source.host || "Unknown"}:${chosen.source.port ?? "?"}`],
          !!chosen.source_version && ["Source version", <>{sourceVersionText(chosen.source_version)}
            <span className="source-version-note">{sourceVersionNote(chosen.source_version)}</span></>],
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
          !!chosen.failing_gtid && ["Failing GTID", formatGtidSet(chosen.failing_gtid)],
        ]} />
        <pre className="ops-pre">{chosen.sql.error.message || chosen.io.error.message}</pre>
        {firstFailure && <pre className="ops-pre">{firstFailure.message}</pre>}
      </InspectorSection>}
      <InspectorSection title="Lag signals"><FactsList facts={chosen.lag_signals} />{chosen.lag_notes.map((s, i) => <p className="hint" key={i}>{s}</p>)}</InspectorSection>
      <InspectorSection title="GTID and positions">
        {/* One list: the channel's positions, then the server's GTID state. */}
        <Fields rows={[
          chosen.agat && ["Assign GTIDs to anonymous transactions", chosen.agat],
          ...factRows(chosen.position),
          ...(report ? factRows(report.gtid, GTID_LABELS) : []),
        ]} />
      </InspectorSection>
      {(chosen.skip_plan || blocked) && <InspectorSection title="Recovery">
        {blocked && <p>{blocked}</p>}
        <p>{chosen.skip_plan?.available ? chosen.skip_plan.privileges : unavailable}</p>
        {chosen.skip_plan?.available && <p>{chosen.skip_plan.drift_warning}</p>}
        {chosen.skip_plan?.notes.map((note, i) => <p className="hint" key={i}>{note}</p>)}
      </InspectorSection>}
      <Disclosure className="ops-raw" title="Raw channel status"><pre className="ops-pre">{JSON.stringify(chosen, null, 2)}</pre></Disclosure>
    </>,
  } : null;
  const channelColumns: Column<ReplicationChannel>[] = [
    { key: "name", label: engine === "mariadb" ? "Connection" : "Channel", primary: true, flex: true, sort: (r) => r.name, render: (r) => channelCell(r.name) },
    { key: "status", label: "Status", sort: (r) => r.status, render: (r) => <StatusPill tone={r.judged ? r.status : "unknown"} label={r.judged ? undefined : "Not judged"} /> },
    { key: "source", label: "Source", sort: (r) => r.source.host, render: (r) => `${r.source.host || "Unknown"}:${r.source.port ?? "?"}` },
    { key: "io", label: "I/O", sort: (r) => r.io.running, render: (r) => <span title={r.io.state || undefined}>{r.io.running || "Unknown"}</span> },
    { key: "sql", label: "SQL", sort: (r) => r.sql.running, render: (r) => <span title={r.sql.state || undefined}>{r.sql.running || "Unknown"}</span> },
    { key: "lag", label: "Lag", num: true, title: "Seconds behind the source; see the inspector for the clock, receiver and delay caveats.", sort: (r) => r.lag_s, render: lagCell },
    { key: "error", label: "Error", num: true, sort: (r) => r.sql.error.errno || r.io.error.errno, render: (r) => r.sql.error.errno || r.io.error.errno || "None" },
  ];
  const lagColumns: Column<ReplicationChannel>[] = [
    { key: "name", label: engine === "mariadb" ? "Connection" : "Channel", primary: true, flex: true, sort: (r) => r.name, render: (r) => channelCell(r.name) },
    { key: "status", label: "Status", sort: (r) => r.status, render: (r) => <StatusPill tone={r.judged ? r.status : "unknown"} label={r.judged ? undefined : "Not judged"} /> },
    { key: "lag", label: "Lag", num: true, title: "How far behind the source the replica is", sort: (r) => r.lag_s, render: lagCell },
    { key: "delay", label: "Configured delay", num: true, sort: (r) => r.sql.delay_s, render: (r) => r.sql.delay_s ? <Duration value={r.sql.delay_s} /> : "None" },
    { key: "remaining_delay", label: "Remaining delay", num: true, sort: (r) => r.sql.remaining_delay_s, render: (r) => r.sql.remaining_delay_s ? <Duration value={r.sql.remaining_delay_s} /> : "None" },
    { key: "signals", label: "Worker signals", render: (r) => Object.keys(r.lag_signals || {}).length ? factRows(r.lag_signals).slice(0, 2).map(([label, value], i) => <span key={label}>{i ? ", " : ""}{label}: {value}</span>) : "Normal" },
    { key: "notes", label: "Lag caveats", render: (r) => r.lag_notes?.length ? r.lag_notes.join("; ") : "No delay caveats" },
  ];
  const gtidColumns: Column<ReplicationChannel>[] = [
    { key: "name", label: engine === "mariadb" ? "Connection" : "Channel", primary: true, flex: true, sort: (r) => r.name, render: (r) => channelCell(r.name) },
    { key: "status", label: "Status", sort: (r) => r.status, render: (r) => <StatusPill tone={r.judged ? r.status : "unknown"} label={r.judged ? undefined : "Not judged"} /> },
    { key: "failing_gtid", label: "Failing GTID", render: (r) => r.failing_gtid ? formatGtidSet(r.failing_gtid) : "None" },
    { key: "read_pos", label: "Read position", title: "Source binary log position the receiver has read",
      render: (r) => logPosition(r.position, ["source_log_file", "Master_Log_File", "Source_Log_File"], ["read_source_log_pos", "Read_Master_Log_Pos", "Read_Source_Log_Pos"]) },
    { key: "exec_pos", label: "Executed position", title: "Source binary log position the applier has executed",
      render: (r) => logPosition(r.position, ["exec_file", "Relay_Master_Log_File", "Relay_Source_Log_File"], ["exec_pos", "Exec_Master_Log_Pos", "Exec_Source_Log_Pos"]) },
    // MySQL only: ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS of the channel.
    ...(engine === "mariadb" ? [] : [{ key: "agat", label: "Assign GTIDs", title: "ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS", render: (r: ReplicationChannel) => r.agat || "Unknown" }]),
  ];
  const recoveryColumns: Column<ReplicationChannel>[] = [
    { key: "name", label: engine === "mariadb" ? "Connection" : "Channel", primary: true, flex: true, sort: (r) => r.name, render: (r) => channelCell(r.name) },
    { key: "status", label: "Status", sort: (r) => r.status, render: (r) => <StatusPill tone={r.judged ? r.status : "unknown"} label={r.judged ? undefined : "Not judged"} /> },
    { key: "error", label: "Last error", flex: true, sort: (r) => r.sql.error.errno || r.io.error.errno, render: (r) => {
        const err = r.sql.error.errno ? r.sql.error : r.io.error;
        return err.errno ? <span className="error-text" title={err.message || undefined}>Error {err.errno}{err.table ? ` on ${err.table}` : ""}: {err.message ? (err.message.length > 55 ? err.message.slice(0, 55) + "…" : err.message) : ""}</span> : "No errors reported";
      }
    },
    { key: "skip_plan", label: "Skip plan", render: (r) => r.skip_plan?.available ? <StatusPill tone="ok" label={r.skip_plan.method === "empty_gtid_transaction" ? "Empty GTID" : "Skip counter"} /> : r.sql.error.errno ? <StatusPill tone="problem" label="Not available" /> : "—" },
    { key: "actions", label: "Action", render: (r) => r.skip_plan?.available ? (
        <span onClick={(e) => e.stopPropagation()}>
          <VisiblePrivilegeGate action={r.skip_plan.method === "empty_gtid_transaction" ? "skip_gtid" : "skip_counter"} gate={skipGate(r.skip_plan)}
            blocked={blocked || loopGate || actionBlock("one", r.skip_plan)}
            disabled={disabled || !!other || busy || loops.some((l) => l.channel === r.name && loopActive(l))}
            onClick={(e) => { void open("one", r, e.currentTarget); }}>Skip…</VisiblePrivilegeGate>
        </span>
      ) : "—"
    },
  ];
  const columns: Column<ReplicationChannel>[] =
    activeTab === "lag" ? lagColumns :
    activeTab === "gtid" ? gtidColumns :
    activeTab === "recovery" ? recoveryColumns :
    channelColumns;
  const error = failure?.key === key && !failure.write ? failure.text : reads.error;
  const result = outcome?.report ?? null;
  const resultTone = result ? SKIP_STATUS[result.status] || (["unknown", result.status] as [Tone, string]) : null;
  const c = confirmation;
  const errorCodes = c ? [...new Set([...COMMON_ERRORS.map(([code]) => code), c.plan.error.errno])] : [];
  const validLoop = !!c && c.options.codes.length > 0 && Number.isInteger(c.options.max_skips) && c.options.max_skips >= 1 && c.options.max_skips <= 1000 && Number.isInteger(c.options.max_minutes) && c.options.max_minutes >= 1 && c.options.max_minutes <= 1440;
  const word = engine === "mariadb" ? "connection" : "channel";
  const judged = report ? report.channels.filter((r) => r.judged) : [];
  // Loops are read for the connection in use (by endpoint), apart from the report.
  const loopsShown = loops;
  // The restored flag belongs to the connection the workspace was saved with.
  const restartNote = restoredLoop && key === restoredKey && loopChecked === key && !loops.length;
  // Only the backend's judged channels contribute a health status. Group
  // Replication channels have a different contract and remain unjudged here.
  const replicaFailed = judged.some(ch => ch.status === "problem" && !!(ch.io.error.errno || ch.sql.error.errno));
  const replicaProblem = judged.some(ch => ch.status === "problem");
  const replicaStopped = judged.some(ch => ch.status === "problem" && (ch.io.running === "No" || ch.sql.running === "No"));
  const replicaWatch = judged.some(ch => ch.status === "watch");
  const problemCount = judged.filter(ch => ch.status === "problem").length;
  const watchCount = judged.filter(ch => ch.status === "watch").length;
  const unjudged = (report?.channels.length || 0) - judged.length;
  const replicaUnknown = !judged.length || unjudged > 0 || judged.some(ch => ch.status === "unknown");
  const replicaTone: Tone = !report?.replica ? "unknown" : replicaProblem ? "problem" : replicaWatch ? "watch" : replicaUnknown ? "unknown" : "ok";
  const replicaLabel = !report?.replica ? "Standalone" : replicaFailed ? "Failing" : replicaStopped ? "Stopped" : replicaProblem ? "Problem" : replicaWatch ? "Needs attention" : !judged.length ? "Not judged" : replicaUnknown ? "Unknown" : "Running";
  const measuredLag = judged.filter(ch => ch.lag_s != null).length;
  const unknownLag = judged.length - measuredLag;
  // One unknown channel makes the maximum unknown: a measured 0 s beside a
  // failed channel never reads as "caught up".
  const lagUnknown = !report || report.summary.lag_max_s === null || unknownLag > 0;
  const gtidMode = String(report?.gtid?.mode ?? report?.gtid?.gtid_mode ?? "Not available");
  const failingGtids = report ? report.channels.filter((ch) => ch.failing_gtid).length : 0;
  const summary = report
    ? !report.available
      ? "Replication status not available"
      : !report.replica
        ? `Not a replica · ${engineName(report.engine, report.server_version)}`
        : [
            engineName(report.engine, report.server_version, report.server_product),
            plural(report.summary.channels, word),
            report.summary.failed ? `${report.summary.failed.toLocaleString()} failed` : "",
            lagUnknown ? "lag unknown" : `max lag ${durationText(report.summary.lag_max_s)}`,
          ].filter(Boolean).join(" · ")
    : "";
  const topologyReport = report && replicaProblem
    ? {
        ...report,
        summary: { ...report.summary, lag_max_s: null },
        channels: report.channels.map((ch) =>
          ch.status === "problem" || Boolean(ch.io.error.errno || ch.sql.error.errno) || ch.io.running !== "Yes" || ch.sql.running !== "Yes"
            ? { ...ch, lag_s: null }
            : ch,
        ),
      }
    : report;
  return <>
    <ViewLayout id="replication" title="Replication" detailBackLabel="" className="ops-view" onRefresh={() => { void load(); }} refreshDisabled={disabled || busy} loading={!!reads.loading} otherConnection={other} inspector={inspector}
      inspectorEmpty={<p>Select a {word} to see its threads, last error, lag signals and recovery.</p>}
      controls={onTabChange && (!report || !report.available || report.replica) ? (
        <Segmented<ReplicationSubTab>
          label="Replication sub-tabs"
          value={activeTab}
          onChange={onTabChange}
          options={[
            { value: "topology", label: "Topology", title: `Sources and this replica, one line per ${word}` },
            { value: "channels", label: engine === "mariadb" ? "Connections" : "Channels", title: "Thread states, lag and the last error" },
            { value: "lag", label: "Lag", title: "Lag, configured delay and worker signals" },
            { value: "gtid", label: "GTID", title: "GTID state and binary log positions" },
            { value: "recovery", label: "Recovery", title: "Failures and their skip plans", count: report?.summary?.failed || undefined },
          ]}
        />
      ) : undefined}
      info={<><p>Each replication channel (MySQL) or connection (MariaDB) of the connection in use: thread states, the last error, lag signals and positions. Reading changes nothing.</p><p>Seconds behind the source alone does not show that a replica is caught up: compare heartbeats, receiver and worker lag in the inspector.</p><p>Skip this transaction and Skip until stopped are changes: each asks first, shows the exact statements, and is recorded in the Audit log. A skipped transaction is missing on the replica.</p></>}
      status={<div ref={viewRef}><ViewStatus loading={checkingInInspector ? undefined : reads.loading} error={error} startedAt={readSince ?? undefined}
        onStop={reads.loading ? reads.stop : undefined} onRetry={reads.error ? () => void load() : undefined} doneAt={reads.doneAt}>
        {failure?.key === key && failure.loop && <button className="text-button" type="button" onClick={() => { setFailure(null); setPollRetry((n) => n + 1); }}>Retry status</button>}
      </ViewStatus>
      {left && c && <div className="view-status replication-pending"><ActionStatus state={{ phase: "pending", startedAt: since,
        message: `Waiting for the reply to ${c.kind === "one" ? "Skip transaction" : "Start skipping"}${c.key !== key ? ` on ${c.label}` : ""}. Its outcome shows here.` }} /></div>}
      {failure?.write && <section tabIndex={-1} className="ops-result replication-write-result" role="alert">
        <p>{failure.text}</p>{failure.key !== key && <p className="hint">Sent on {failure.label}, not the connection in use.</p>}
      </section>}
      {loopResult && loopResult.key !== key && <section tabIndex={-1} className="ops-result replication-write-result" role="status">
        <p>{loopResult.report.message}</p><p className="hint">Sent on {loopResult.label}, not the connection in use.</p>
      </section>}
      {loopsShown.length > 0 && <ReplicationLoopBanner loops={loopsShown} stopping={stopping} onStop={(l) => { void stop(l); }} />}
      {result && resultTone && <section tabIndex={-1} className="ops-result" aria-label="Skip result" role="status" data-status={result.status}>
          <div className="ops-result-head"><StatusPill tone={resultTone[0]} label={resultTone[1]} /><strong>{channelPhrase(engine, result.channel)}</strong></div>
          <p>{result.message}</p>
          {outcome && outcome.key !== key && <p className="hint">Sent on {outcome.label}, not the connection in use.</p>}
          {result.policy?.text && <p>{result.policy.text}</p>}
          {result.after && <p className="hint">{result.after.passed ? "Checked after: the applier moved past the skipped transaction." : "Checked after: the applier did not move past it. Read the status again."}</p>}
          {result.status === "skipped" && result.plan && <details><summary>What to do next</summary><p>{result.plan.drift_warning}</p><ul>{result.plan.after.map((s, i) => <li key={i}>{s}</li>)}</ul></details>}
        </section>}</div>}>

      {restartNote && <p className="ops-note" role="status">Stopped when myflames restarted. Skip loops never resume from a restored workspace.</p>}
      {report ? <>
        {/* Not a replica is a state of its own, not an unknown one: no status pill. */}
        <SummaryLine tone={!report.available ? "unknown" : !report.replica ? undefined : judged.length ? worstTone(judged.map((r) => r.status)) : "unknown"} toneLabel={judged.length || !report.replica ? undefined : "Not judged"} notes={report.notes.length ? report.notes : undefined}>
          {summary}
        </SummaryLine>
        {report.available && report.replica && (
          <section className="replication-tiles" aria-label="Replication summary">
            <TileGrid label="Replication summary">
              <MetricTile rowKey="replica-state" label="Replica state" value={replicaLabel}
                tone={replicaTone === "unknown" ? undefined : replicaTone}
                // The value is the state, so the pill counts the channels behind it
                // instead of repeating it ("Failing" over "Problem").
                toneLabel={replicaTone === "problem" ? `${problemCount.toLocaleString()} of ${plural(judged.length, word)}`
                  : replicaTone === "watch" ? `${watchCount.toLocaleString()} to watch` : undefined} />
              <MetricTile rowKey="channels" label={engine === "mariadb" ? "Connections" : "Channels"} value={report.summary.channels.toLocaleString()}
                trend={<span className="tile-hint">{`${report.summary.ok.toLocaleString()} OK`}</span>} />
              <MetricTile rowKey="lag" label="Max applier lag"
                value={lagUnknown ? "Unknown" : durationText(report.summary.lag_max_s)}
                attrs={{ title: [lagUnknown ? "" : `${report.summary.lag_max_s} seconds.`, "Seconds behind the source; compare receiver and worker lag in the inspector."].filter(Boolean).join(" ") }}
                trend={<span className="tile-hint">{[
                  measuredLag && lagUnknown && report.summary.lag_max_s !== null ? `${measuredLag} measured (max ${durationText(report.summary.lag_max_s)})` : `${measuredLag} measured`,
                  unknownLag ? `${unknownLag} unknown` : "",
                  unjudged ? `${unjudged} not judged` : "",
                ].filter(Boolean).join(" · ")}</span>} />
              <MetricTile rowKey="gtid" label="GTID mode" value={gtidMode}
                trend={failingGtids ? <span className="tile-hint">{failingGtids === 1 ? "1 failing GTID" : `${failingGtids} failing GTIDs`}</span> : undefined} />
            </TileGrid>
          </section>
        )}
        {report.available && report.replica && activeTab === "gtid" && Object.keys(report.gtid || {}).some((k) => k !== "mode" && k !== "gtid_mode") && (
          // GTID mode is the tile above; the strip carries the rest of the server's GTID state.
          <FactStrip label="Server GTID state" facts={factRows(Object.fromEntries(Object.entries(report.gtid).filter(([k]) => k !== "mode" && k !== "gtid_mode")), GTID_LABELS)} />
        )}
        {!report.available ? (
          <PrerequisiteGate>{report.message || "Replication status is not available for this connection."}</PrerequisiteGate>
        ) : !report.replica ? (
          <NotAReplica report={report} clusterKind={other ? null : clusterKind} onOpenCluster={other ? undefined : onOpenCluster} />
        ) : activeTab === "topology" ? (
          <ReplicationTopology
            report={topologyReport!}
            connection={connection}
            selection={selection}
            onSelectChannel={(name) => selection.select(channelKey({ name } as any))}
          />
        ) : (
          <DataTable id="replication.channels" label={engine === "mariadb" ? "Replication connections" : "Replication channels"} columns={columns} rows={report.channels} rowKey={channelKey} selection={selection} />
        )}

      </> : <EmptyState icon="replication" title="Read replication status" action={<ToolbarButton primary disabled={disabled || !!reads.loading} onClick={() => { void load(); }}>Load</ToolbarButton>}>Channels, thread states, the last error and lag signals of the connection in use. It only reads.</EmptyState>}
    </ViewLayout>
    {c && !left && <ConfirmSheet className="ops-confirm" eyebrow={c.kind === "one" ? "SKIP TRANSACTION" : "SKIP UNTIL STOPPED"}
      returnFocus={() => reviewOpener.current?.isConnected ? reviewOpener.current : null}
      title={`${c.kind === "one" ? "Skip the failed transaction" : "Skip failures until stopped"} on ${channelPhrase(c.engine, c.plan.channel)}?`}
      description="myflames reads the failure again right before sending and stops if it changed. A skipped transaction cannot be undone."
      busy={busy} busyLabel={c.kind === "one" ? "Skipping…" : "Starting…"}
      progress={c.kind === "one" ? "Waiting for the server to skip the transaction" : "Waiting for the server to start skipping"}
      onLeave={() => { setLeft(true); focusResult(".replication-pending .action-status"); }}
      leaveText="The request may still be running. Close does not stop it; its outcome shows in Replication."
      confirmLabel={<VisiblePrivilegeConfirmLabel gate={{ ...skipGate(c.plan), disabledReason: blocked || loopGate || actionBlock(c.kind, c.plan) }}>{c.kind === "one" ? "Skip transaction" : "Start skipping"}</VisiblePrivilegeConfirmLabel>}
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

/** The calm state of a server with no asynchronous replication channel: why
 * (the backend's sentence without its lead, which names Cluster for a Group
 * Replication member or a Galera node), and Open Cluster for those. */
function NotAReplica({ report, clusterKind, onOpenCluster }: { report: ReplicationReport; clusterKind: ClusterKind | null; onOpenCluster?: () => void }) {
  const text = (report.message || "").replace(/^This server is not a replica:\s*/, "");
  const reason = text ? text.charAt(0).toUpperCase() + text.slice(1) : "It has no replication channel.";
  // The backend's hint (a Galera provider, Group Replication configured or
  // its channels), or the Cluster place's own report for this connection.
  const member = !!report.cluster_hint || (report.group_replication_channels || []).length > 0 || !!clusterKind;
  return (
    <EmptyState icon="replication" title="Not a replica"
      action={member && onOpenCluster ? <ToolbarButton onClick={onOpenCluster}>Open Cluster</ToolbarButton> : undefined}>
      {reason}
    </EmptyState>
  );
}
