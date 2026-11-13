// Server > Locks (locks-1.0 / locks-1.1 from myflames/locks.py): who is
// blocking whom. One summary line, then waiting DDL statements and the
// blocking tree as outlines with disclosure triangles (Finder), and one table
// at a time for transactions, row locks, metadata locks and the latest
// deadlock. The selected session's details, its plan, and Kill are in the
// inspector; Kill opens a confirmation sheet with Cancel as the default.
// It only reads, except a Kill the user confirms.
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { useOpenRead } from "./viewOnScreen";
import { durationText } from "./duration";
import { createPortal } from "react-dom";
import {
  api,
  killLockSession,
  prepareKill,
  type KillAfter,
  type KillReview,
  type KillTargetRead,
  number,
  recordDeclined,
  type Connection,
  type DdlBlocker,
  type DdlWait,
  type DeadlockLock,
  type KillServerIdentity,
  type LockEdge,
  type LockNode,
  type LocksReport,
  type LockTransaction,
  type MetadataLock,
  type RowLock,
  type XaPrepared,
} from "./api";
import { Icon } from "./components";
import { PrivilegeGate, PrivilegeConfirmLabel, targetPrivilege, useLoginUser, usePrivilege } from "./privileges";
import { InstrumentsLink } from "./Instruments";
import { PriorityTable, CompactNumber, Duration } from "./PerfShared";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { ConnectionTypeBand, useWritePolicy, type WritePolicy } from "./Connections";
import {
  ActionStatus,
  AutoRefresh,
  Checkbox,
  ConfirmSheet,
  Disclosure,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  Pill,
  PrerequisiteGate,
  Segmented,
  SummaryLine,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  moveRowFocus,
  useScrollEdges,
  writeFailure,
  type Column,
  type InspectorContent,
  type Selection,
} from "./kit";
import "./locks.css";

/** Presentation adapters for the server write controls. Keep the shared gate's
 * guards and description IDs; only move its feedback outside the button. */
export function VisiblePrivilegeGate(props: ComponentProps<typeof PrivilegeGate>) {
  return <span className="visible-privilege">
    <PrivilegeGate {...props} />
    <span className="privilege-status-note" aria-hidden="true">Unverified</span>
  </span>;
}

export function VisiblePrivilegeConfirmLabel(props: ComponentProps<typeof PrivilegeConfirmLabel>) {
  const label = useRef<HTMLSpanElement>(null);
  const [actions, setActions] = useState<HTMLElement | null>(null);
  useLayoutEffect(() => { setActions(label.current?.closest<HTMLElement>(".sheet-actions") || null); }, []);
  const note = props.gate.disabledReason || (props.gate.state === "unknown" ? "Unverified" : "");
  return <>
    <span ref={label} className="visible-privilege-confirm"><PrivilegeConfirmLabel {...props} showStatus={false} /></span>
    {actions && note && createPortal(<span className="privilege-confirm-note" aria-hidden="true">{note}</span>, actions)}
  </>;
}

/** A duration in words, the same everywhere (see duration.ts). */
export function seconds(value: number | null | undefined) {
  return durationText(value);
}

const INTERVALS = [10, 30, 60];
const PAGE = 200;

function objectName(schema?: string | null, table?: string | null) {
  return [schema, table].filter(Boolean).join(".") || "—";
}

function flat(text: string) {
  return text.replace(/\s+/g, " ").trim();
}

/** A statement, labeled ("Running", "Last statement"), whole and selectable. */
export function Sql({ text, label }: { text: string; label?: string }) {
  return (
    <div className="lock-sql">
      {label && <span className="lock-sql-label">{label}</span>}
      <pre className="inspector-sql">
        <code>{text}</code>
      </pre>
    </div>
  );
}

/** Lock mode with its plain-language meaning (visible label, full sentence on hover and for screen readers). */
function Mode({ mode, label, explain }: { mode?: string | null; label?: string | null; explain?: string | null }) {
  return (
    <span className="lock-mode" title={explain || undefined}>
      <code>{mode || "—"}</code>
      {label && <span className="lock-mode-label">{label}</span>}
      {explain && <span className="visually-hidden">: {explain}</span>}
    </span>
  );
}

export function CopyCommand({ command, onCopied }: { command: string; onCopied: (text: string) => void }) {
  return (
    <span className="kill-command">
      <code>{command}</code>
      <button
        type="button"
        className="text-button"
        aria-label={`Copy ${command}`}
        onClick={() => {
          navigator.clipboard
            .writeText(command)
            .then(() => onCopied(`Copied ${command}. Run it yourself in a SQL client if you decide to.`))
            .catch(() => onCopied(`Select ${command} to copy it.`));
        }}
      >
        Copy
      </button>
    </span>
  );
}

function waitText(edge: LockEdge) {
  const lock = edge.lock;
  const where = objectName(lock.schema, lock.table);
  if (edge.kind === "metadata")
    return `metadata ${lock.lock_type} on ${(lock.object_type || "").toLowerCase()} ${where}${edge.reason === "queued" ? ", queued behind a waiting request" : ""}`;
  return `row lock ${lock.mode}${lock.mode_label ? ` (${lock.mode_label})` : ""} on ${where}${lock.index ? ` index ${lock.index}` : ""}${lock.lock_data ? ` [${lock.lock_data}]` : ""}`;
}

function edgeSeconds(edge: LockEdge) {
  if (edge.wait_s !== null && edge.wait_s !== undefined) return seconds(edge.wait_s);
  if (edge.statement_s !== null && edge.statement_s !== undefined) return `~${seconds(edge.statement_s)}`;
  return "?";
}

/** A blocker that changed at least this many rows gets a long-rollback warning (locks.LARGE_ROLLBACK_ROWS). */
const LARGE_ROLLBACK_ROWS = 10000;

/** The session a Kill dialog describes (a tree node, a DDL blocker, or a Sessions row). */
export type KillTarget = {
  connection_id: number;
  user: string | null;
  host: string | null;
  db: string | null;
  command: string | null;
  time_s: number | null;
  statement: string | null;
  last_statement: string | null;
  running: boolean;
  idle: boolean;
  trx_age_s: number | null;
  trx_started: string | null;
  rows_modified: number | null;
  ps_thread_id: number | null;
  xa_prepared: XaPrepared | null;
  /** "multi": the open transaction spans more than the running statement, so
   * KILL QUERY leaves it open with its locks. */
  trx_scope?: "multi" | "statement" | null;
  /** "timing": the transaction started at least 2 s before the statement. */
  trx_scope_source?: "performance_schema" | "earlier_changes" | "timing" | null;
  rows_locked?: number | null;
  /** Whether the report read INNODB_TRX (null: the report does not say). */
  trx_checked?: boolean | null;
  /** The connections it blocks (lock-tree children and DDL waiters in Locks,
   * blocks_ids in Processlist). */
  waiting?: number[];
  /** The connection the report was read from and the dialog was opened on:
   * the KILL goes there, and only while it is still the connection in use. */
  via?: KillVia;
  /** Target hint from the lock report, retained through confirmation. */
  kill_allowed?: boolean | null;
  kill_need?: string;
  /** Identity from the report the user selected, never from a later refresh. */
  server_identity?: KillServerIdentity;
};
export type KillVia = { connection: Connection; key: string; label: string };
/** The connection a Kill dialog opened now is bound to. */
export function killVia(connection: Connection): KillVia {
  return { connection: { ...connection }, key: connectionKey(connection), label: connectionLabel(connection) };
}

function serverIdentityLabel(identity?: KillServerIdentity): string {
  const unavailable = "Unavailable; refresh before using Kill.";
  if (!identity) return unavailable;
  if ("server_uuid" in identity) return identity.server_uuid || unavailable;
  return identity.hostname
    ? `${identity.hostname} · server_id ${identity.server_id} · ${identity.version}`
    : unavailable;
}

function targetFromNode(n: LockNode): KillTarget {
  return {
    connection_id: n.connection_id as number,
    kill_allowed: n.kill_allowed,
    user: n.user,
    host: n.host,
    db: n.db,
    command: n.command,
    time_s: n.time_s,
    statement: n.query,
    last_statement: n.last_statement,
    running: n.command !== "Sleep" && !!n.query,
    idle: n.idle_in_transaction,
    trx_age_s: n.trx_age_s,
    trx_started: n.trx_started ?? null,
    rows_modified: n.rows_modified ?? null,
    ps_thread_id: n.ps_thread_id ?? null,
    xa_prepared: n.xa_prepared ?? null,
    trx_scope: n.trx_scope ?? null,
    trx_scope_source: n.trx_scope_source ?? null,
    rows_locked: n.rows_locked ?? null,
  };
}

function targetFromBlocker(b: DdlBlocker): KillTarget {
  return {
    connection_id: b.connection_id as number,
    kill_allowed: b.kill_allowed,
    user: b.user,
    host: b.host,
    db: b.db,
    command: b.command,
    time_s: b.time_s,
    statement: b.statement,
    last_statement: b.last_statement,
    running: b.running,
    idle: b.idle_in_transaction,
    trx_age_s: b.trx_age_s,
    trx_started: b.trx_started,
    rows_modified: b.rows_modified,
    rows_locked: b.rows_locked,
    ps_thread_id: b.ps_thread_id,
    xa_prepared: b.xa_prepared ?? null,
    trx_scope: b.trx_scope ?? null,
    trx_scope_source: b.trx_scope_source ?? null,
  };
}

/** Sessions waiting on `id`: below it in the blocking tree, and DDL statements that list it (locks.blocked_by). */
function blockingOf(report: LocksReport, id: number) {
  const ids: number[] = [];
  const walk = (node: LockNode, under: boolean) => {
    const mine = under || node.connection_id === id;
    for (const child of node.blocked) {
      const cid = child.connection_id;
      if (mine && cid && cid !== id && !ids.includes(cid)) ids.push(cid);
      if (!child.repeat) walk(child, mine);
    }
  };
  report.blocking_tree.forEach((root) => walk(root, false));
  const ddls = (report.ddl_waits || []).filter((d) => d.blockers.some((b) => b.connection_id === id));
  for (const d of ddls) if (!ids.includes(d.connection_id)) ids.push(d.connection_id);
  return { ids, ddls };
}

export type Outcome = { unknown?: boolean; id: number; waiting: boolean; blockedBy: number[]; ddl: string | null; likely: boolean };

/** Every cap or materialization budget can hide evidence of a continuing wait. */
function locksReadTruncated(report: LocksReport): boolean {
  return !!(report.waits_truncated || report.row_locks_truncated || report.blocking_tree_budget?.truncated ||
    report.metadata_locks?.truncated || report.metadata_locks?.waits_truncated ||
    report.metadata_locks?.waits.some(w => w.truncated) || report.ddl_waits?.some(d => d.blockers_truncated));
}

/** Waits-only output omits transactions not involved in the visible waits. */
function completeLockTransactions(report: LocksReport): boolean {
  return report.trx_checked === true && report.scope === "all" && !locksReadTruncated(report);
}

/** A fresh read with these settings can resolve transaction uncertainty. */
export function locksCanRecheck(report: LocksReport): boolean {
  return completeLockTransactions(report) && !report.capabilities.data_locks_global_latch;
}

/** Locks has no session list: even an empty full read cannot prove disconnect. */
function killSessionState(report: LocksReport, id: number): KillObservation {
  const nodes: LockNode[] = [];
  const walk = (n: LockNode) => {
    nodes.push(n);
    if (!n.repeat) n.blocked.forEach(walk);
  };
  report.blocking_tree.forEach(walk);
  const node = nodes.find(n => n.connection_id === id);
  const blocker = (report.ddl_waits || []).flatMap(d => d.blockers).find(b => b.connection_id === id);
  const t = report.transactions.find(x => x.connection_id === id);
  const complete = completeLockTransactions(report);
  return {
    present: !!(t || node || blocker), connectionEvidence: "unknown",
    transactionEvidence: t ? (t.state === "ROLLING BACK" ? "rolling_back" : "open") : complete ? "none" : "unknown",
    command: node?.command ?? blocker?.command ?? t?.command ?? null,
    trx_checked: report.trx_checked === true,
    trx_state: t?.state ?? null, trx_started: t?.started ?? null,
    rows_modified: t?.rows_modified ?? null, rows_locked: t?.rows_locked ?? null,
    statement: node ? node.query : blocker ? blocker.statement : t ? t.current_statement : undefined,
  };
}

/** Reconcile only observations supplied by this report, retaining the write's message. */
export function reconcileKillNote(note: KillNote, report: LocksReport): KillNote {
  if (!killReportMatches(note, report.captured_at, report.server_identity)) return note;
  if (note.status !== "killed" || note.connectionId === undefined) return note.refreshing ? { ...note, refreshing: false } : note;
  let state = killSessionState(report, note.connectionId);
  const detached = report.transactions.find(t => !t.connection_id && note.target?.trx?.trx_id != null && t.trx_id === note.target.trx.trx_id);
  if (detached) state = { ...state, transactionEvidence: "survived", trx_survived: true,
    trx_survival_checked: true, rows_locked: detached.rows_locked, rows_modified: detached.rows_modified };
  // Only a complete transaction-id read can disprove earlier detached XA evidence.
  const trxId = note.target?.trx?.trx_id;
  if (!detached && trxId != null && completeLockTransactions(report) && !report.transactions.some(t => t.trx_id === trxId))
    state = { ...state, trx_survival_checked: true, trx_survived: false,
      ...(note.after?.trx_survived === true ? { transactionEvidence: "none", xaCleared: true, trx_state: null,
        trx_started: null, rows_modified: null, rows_locked: null } : {}) };
  // A complete id lookup also resolves a failed XA check when the original
  // transaction is still attached. Confirmed detached survival remains sticky.
  if (!detached && trxId != null && completeLockTransactions(report) && note.after?.trx_survived !== true)
    state = { ...state, trx_survival_checked: true };
  const t = report.transactions.find(x => x.connection_id === note.connectionId);
  const after = reconcileTransaction(note, state, t?.trx_id);
  const kinds = new Map(note.outcome.flatMap(o => o.ddl ? [[o.id, o.ddl] as const] : []));
  const next = reconcileKillState({ ...note, outcome: outcomeOf(report, note.outcome.map(o => o.id), kinds) }, after);
  return !locksCanRecheck(report) && !killPollDone(next) ? manualKillRefresh(next) : next;
}

/** After a Kill: for each session that waited, is it still waiting, and on whom (locks.kill_outcome)? */
function outcomeOf(report: LocksReport, ids: number[], kinds: Map<number, string>): Outcome[] {
  const parents = new Map<number, Set<number>>();
  const walk = (node: LockNode, parent: number | null) => {
    const cid = node.connection_id;
    if (parent && cid) {
      if (!parents.has(cid)) parents.set(cid, new Set());
      parents.get(cid)?.add(parent);
    }
    if (!node.repeat) node.blocked.forEach((child) => walk(child, cid));
  };
  report.blocking_tree.forEach((root) => walk(root, null));
  const ddl = new Map((report.ddl_waits || []).map((d) => [d.connection_id, d] as const));
  const complete = report.scope !== "deadlock" && report.capabilities.lock_waits === true &&
    report.capabilities.metadata_locks === true && !locksReadTruncated(report);
  return ids.map((id) => {
    const blockers = new Set(parents.get(id) || []);
    const entry = ddl.get(id);
    entry?.blockers.forEach((b) => {
      if (b.connection_id) blockers.add(b.connection_id);
    });
    const waiting = parents.has(id) || !!entry || report.transactions.some(t => t.connection_id === id && t.state === "LOCK WAIT");
    return {
      id,
      unknown: !waiting && !complete,
      waiting,
      blockedBy: Array.from(blockers).sort((a, b) => a - b),
      ddl: entry?.kind ?? kinds.get(id) ?? null,
      likely: entry?.precision === "likely",
    };
  });
}

function list(ids: number[]) {
  return ids.length > 1 ? `connections ${ids.slice(0, -1).join(", ")} and ${ids[ids.length - 1]}` : `connection ${ids[0]}`;
}

export function outcomeText(o: Outcome) {
  if (o.unknown) return `Connection ${o.id}: this read cannot show whether it still waits.`;
  const what = o.ddl ? `The ${o.ddl} on connection ${o.id}` : `Connection ${o.id}`;
  if (!o.waiting) return `${what} no longer waits${o.ddl ? "; it is running or done" : ""}.`;
  if (o.blockedBy.length) return `${what} still waits, ${o.likely ? "probably for" : "now for"} ${list(o.blockedBy)}.`;
  return `${what} still waits.`;
}

/** The tag every KILL carries (locks.KILL_TAG), so the audit trail shows the
 * statement exactly as it would have been sent. */
const KILL_TAG = "/*myflames:kill*/ ";
/** The KILL a confirmation would send. */
export function killStatement(id: number, mode: "connection" | "query") {
  return `${KILL_TAG}${mode === "query" ? "KILL QUERY" : "KILL CONNECTION"} ${id}`;
}
/** The identity the person was asked to confirm (locks._confirmed). */
function confirmedTarget(target: KillTarget, mode: "connection" | "query") {
  const out: Record<string, unknown> = { connection_id: target.connection_id, mode };
  out.connection_identity = target.via?.key;
  out.server_identity = target.server_identity;
  for (const [key, value] of [
    ["user", target.user],
    ["host", target.host],
    ["trx_started", target.trx_started],
    ["ps_thread_id", target.ps_thread_id],
  ] as const)
    if (value !== null && value !== undefined) out[key] = value;
  return out;
}
/** A cancelled Kill (Cancel or Escape) is recorded as declined in the audit
 * trail, for the connection the dialog was opened on: nothing was sent. */
export function declineKill(target: KillTarget, mode: "connection" | "query") {
  if (!target.via) return;
  void recordDeclined(
    mode === "query" ? "kill_query" : "kill",
    [killStatement(target.connection_id, mode)],
    target.via.connection,
    confirmedTarget(target, mode),
  );
}

/** Own-user KILL uses its own backend action. A report's target answer
 * wins even when the generic new-session answer disagrees. */
export function useKillPrivilege(target: Pick<KillTarget, "user" | "kill_allowed" | "kill_need">, loginUser?: string) {
  const other = usePrivilege("kill");
  const own = usePrivilege("kill_own");
  // No user name typed: the client logs in as the OS user, named by the privileges read.
  const login = useLoginUser(loginUser);
  const generic = target.user && target.user === login ? own : other;
  return target.kill_allowed === undefined ? generic : targetPrivilege(generic, {
    allowed: target.kill_allowed, need: target.kill_need || generic.missing.join(" or "),
  });
}

/** The confirmation for ending one session: what it is, what it blocks, and
 * which KILL. Cancel has focus; Escape and Cancel send nothing (and are
 * recorded as declined in the audit trail). */
export function KillDialog({
  target,
  blocking,
  busy,
  initialMode,
  onCancel,
  onConfirm,
  onLeave,
}: {
  target: KillTarget;
  /** What the session blocks (Locks), or nothing when unknown. */
  blocking?: ReactNode | ((mode: "connection" | "query") => ReactNode);
  busy: boolean;
  /** The KILL chosen before the sheet opened (a row menu's Kill query… or
   * Kill connection…); KILL QUERY only while the session runs a statement. */
  initialMode?: "connection" | "query";
  onCancel: () => void;
  onConfirm: (mode: "connection" | "query", reviewToken: string) => void;
  /** Close the sheet while a KILL without a reply goes on (offered when slow);
   * the owner shows its outcome when it arrives. */
  onLeave?: () => void;
}) {
  // A row menu's choice wins; otherwise an idle session or one whose
  // transaction spans more than the running statement (KILL QUERY cannot free
  // its locks) starts on Kill connection.
  const [mode, setMode] = useState<"connection" | "query">(
    initialMode === "connection" || !target.running
      ? "connection"
      : initialMode === "query"
        ? "query"
        : target.trx_scope === "multi"
          ? "connection"
          : "query");
  const [review, setReview] = useState<KillReview | null>(null);
  const [reviewError, setReviewError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    setReview(null);
    setReviewError("");
    if (!target.via || !target.server_identity) {
      setReviewError("Refresh this server before reviewing the action.");
      return () => abort.abort();
    }
    prepareKill(target.via.connection, target.connection_id, mode, {
      user: target.user, host: target.host, trx_started: target.trx_started, ps_thread_id: target.ps_thread_id,
      connection_identity: target.via.key, server_identity: target.server_identity,
    }, abort.signal).then(value => { if (!abort.signal.aborted) setReview(value); })
      .catch(error => { if (!abort.signal.aborted) setReviewError((error as Error).message); });
    return () => abort.abort();
  }, [target, mode]);
  const gate = useKillPrivilege(target, target.via?.connection.user);
  const policy = useWritePolicy();
  const blocked = policy?.blocked || (target.via?.connection.read_only ? "This connection is read-only." : "") || reviewError || (!review ? "Preparing the action for review." : "");
  const confirmGate = blocked ? { ...gate, disabledReason: blocked } : gate;
  const id = target.connection_id;
  const rows = target.rows_modified;
  const waiting = target.waiting ?? [];
  const hasTrx = target.trx_started != null || target.trx_age_s != null;
  // The waiting-sessions clause of the Kill connection warning.
  const waiters = waiting.length
    ? `, so ${list(waiting)} ${waiting.length > 1 ? "keep" : "keeps"} waiting until then`
    : "";
  // Under Kill query only: why its locks stay (a longer transaction). Name the
  // evidence trx_scope came from: the start times, or rows a read cannot change.
  const queryWait =
    target.trx_scope_source === "timing" && target.trx_age_s != null && target.time_s != null
      ? ` that started ${seconds(Math.max(0, target.trx_age_s - target.time_s))} before it`
      : target.trx_scope_source === "earlier_changes" && rows
        ? ` that already changed ${rows.toLocaleString("en-US")} ${rows === 1 ? "row" : "rows"} before it`
        : "";
  const state = target.idle
    ? `Idle in transaction for ${seconds(target.time_s)}`
    : target.running
      ? `Running a statement for ${seconds(target.time_s)}`
      : `${target.command || "Unknown"} for ${seconds(target.time_s)}`;
  return (
    <ConfirmSheet
      className="kill-dialog"
      band={<ConnectionTypeBand connection={target.via?.connection} label={target.via?.label} />}
      eyebrow="KILL"
      title={`End connection ${id}?`}
      description="Check that this is the session you mean. Before sending anything, myflames reads it again and stops if it has changed or ended."
      busy={busy}
      progress={`Waiting for the ${mode === "query" ? "KILL QUERY" : "KILL"} reply`}
      onLeave={onLeave}
      leaveText="The KILL may still be running. Close does not stop it; its outcome shows in the view."
      confirmLabel={<VisiblePrivilegeConfirmLabel gate={confirmGate}>{busy ? `Ending connection ${id}…` : mode === "query" ? `Kill query on connection ${id}` : `Kill connection ${id}`}</VisiblePrivilegeConfirmLabel>}
      onCancel={() => {
        declineKill(target, mode);
        onCancel();
      }}
      onConfirm={() => { if (!busy && !confirmGate.disabledReason && review) onConfirm(mode, review.review_token); }}
    >
      {reviewError && <p role="alert" className="error kill-review-error">{reviewError}</p>}
      {review && <Sql text={review.statements.join(";\n")} label="Statement to send" />}
      <fieldset className="kill-choices" disabled={busy}>
        <legend>What to end</legend>
        <label>
          <input type="radio" name="kill-mode" value="connection" checked={mode === "connection"} onChange={() => setMode("connection")} />
          <span>
            <strong>Kill connection</strong> (rolls back its open transaction)
            <span className="hint">
              {target.running
                ? "Closes the connection and rolls back its transaction; its locks are released when the rollback ends."
                : "The session is idle, so KILL QUERY would not help: there is no statement to stop, and its transaction keeps its locks."}
            </span>
          </span>
        </label>
        {target.running && (
          <label>
            <input type="radio" name="kill-mode" value="query" checked={mode === "query"} onChange={() => setMode("query")} />
            <span>
              <strong>Kill query only</strong>
              <span className="hint">
                Stops the running statement. Its transaction stays open with its locks unless the session runs in autocommit
                mode, so the sessions it blocks may keep waiting.
              </span>
            </span>
          </label>
        )}
      </fieldset>
      {mode === "connection" && hasTrx && (
        <div className="kill-caution" role="note">
          <p className="kill-caution-head">
            <Icon name="warning" /> Kill connection {id} rolls back its open transaction.
          </p>
          <ul>
            <li>
              {rows !== null && rows > 0
                ? `Its ${number(rows)} uncommitted row ${rows === 1 ? "change" : "changes"}${rows >= LARGE_ROLLBACK_ROWS ? " (InnoDB's undo record count, so a row changed twice counts twice)" : ""} ${rows === 1 ? "is" : "are"} undone and lost; the application cannot commit ${rows === 1 ? "it" : "them"}.`
                : "Any uncommitted changes in it are undone and lost; the application cannot commit them."}
            </li>
            <li>
              The rollback runs before the connection closes and can take a long time for a large transaction. Connection{" "}
              {id} keeps its locks until it ends{waiters}.
            </li>
            <li>The application loses its connection and gets an error.</li>
          </ul>
        </div>
      )}
      {mode === "connection" && !hasTrx && target.trx_checked === true && (
        <p className="hint kill-note-plain">
          Closes the connection. It had no open transaction when the list was read, so nothing would be rolled back. The application loses its connection
          and gets an error.
        </p>
      )}
      {mode === "connection" && !hasTrx && target.trx_checked !== true && (
        <div className="kill-caution" role="note">
          <p className="kill-caution-head">
            <Icon name="warning" /> Kill connection {id} closes the connection.
          </p>
          <p>
            Could not read its transaction. If one is open, its uncommitted changes are rolled back and lost, and it keeps
            its locks until the rollback ends. The application loses its connection and gets an error.
          </p>
        </div>
      )}
      {mode === "query" && target.trx_scope === "multi" && (
        <p className="kill-caution-line" role="note">
          <Icon name="warning" /> {waiting.length ? `${list(waiting).replace(/^c/, "C")} will keep waiting: this` : "This"}{" "}
          statement runs inside a longer transaction{queryWait}, so its locks stay after the statement stops.
        </p>
      )}
      {blocking && (
        <div className="kill-blocking">
          <span className="lock-sql-label">Blocking</span>
          {typeof blocking === "function" ? blocking(mode) : blocking}
        </div>
      )}
      <dl className="kill-facts">
        {target.via && (
          <div>
            <dt>Server</dt>
            <dd>{target.via.label}</dd>
          </div>
        )}
        <div>
          <dt>Server identity</dt>
          <dd>{serverIdentityLabel(target.server_identity)}</dd>
        </div>
        <div>
          <dt>Connection</dt>
          <dd>{id}</dd>
        </div>
        <div>
          <dt>User</dt>
          <dd>
            {target.user || "?"}@{target.host || "?"}
            {target.db ? ` · ${target.db}` : ""}
          </dd>
        </div>
        <div>
          <dt>State</dt>
          <dd>{state}</dd>
        </div>
        <div>
          <dt>Transaction</dt>
          <dd>{target.trx_age_s !== null ? `Open ${seconds(target.trx_age_s)}` : "None seen"}</dd>
        </div>
        <div>
          <dt>Rows changed</dt>
          <dd>{rows === null ? "Unknown" : number(rows)}</dd>
        </div>
      </dl>
      {target.statement ? (
        <Sql text={target.statement} label="Running" />
      ) : target.last_statement ? (
        <Sql text={target.last_statement} label="Last statement" />
      ) : null}
    </ConfirmSheet>
  );
}

/** What Locks shows under "Blocking" in the Kill dialog. */
function BlockingList({ report, id }: { report: LocksReport; id: number }) {
  const { ids, ddls } = useMemo(() => blockingOf(report, id), [report, id]);
  // Sessions queued behind a DDL are counted with that DDL, not listed again.
  const queued = new Set(ddls.flatMap((d) => d.queued.map((q) => q.connection_id)));
  const others = ids.filter((i) => !queued.has(i) && !ddls.some((d) => d.connection_id === i));
  if (!ids.length) return <span>No session is waiting for it right now.</span>;
  return (
    <ul>
      {ddls.map((d) => (
        <li key={d.connection_id}>
          {d.precision === "likely" ? "Probably the " : "The "}
          {d.kind} on connection {d.connection_id} ({objectName(d.schema, d.table)})
          {d.queued.length > 0 && `, and ${d.queued.length} ${d.queued.length === 1 ? "session" : "sessions"} queued behind it`}
        </li>
      ))}
      {others.length > 0 && <li>{list(others).replace(/^c/, "C")}</li>}
    </ul>
  );
}

/** Send one confirmed KILL with the session's fingerprint (locks.kill_session),
 * to the connection the dialog was confirmed for. Nothing is sent when the
 * connection in use (*active*) is no longer that one. */
export async function sendKill(active: Connection, target: KillTarget, mode: "connection" | "query", policy?: WritePolicy | null, reviewToken = ""): Promise<Omit<KillNote, "outcome">> {
  // Every answer names the connection the dialog was confirmed for, so a note
  // shown after a switch says which server it is about (KillResultNote).
  const on = target.via ? { key: target.via.key, label: target.via.label } : undefined;
  if (policy) {
    // The type and read-only flag as they are now (another tab or the CLI
    // may have changed them): the dialog was confirmed under the old ones.
    try {
      active = await policy.check();
    } catch (e) {
      return {
        status: "error",
        message: `Could not read the connection's settings, so nothing was sent: ${(e as Error).message}`,
        on,
      };
    }
  }
  if (!target.via || connectionKey(active) !== target.via.key)
    return {
      status: "refused",
      refresh: true,
      on,
      message:
        target.via && connectionKey({ ...active, type: undefined, read_only: undefined }) === connectionKey({ ...target.via.connection, type: undefined, read_only: undefined })
          ? "The connection's type or read-only setting changed after this dialog opened, so nothing was sent. Check it and try again."
          : "The connection in use changed after this dialog opened, so nothing was sent. Refresh to load this server.",
    };
  try {
    if (!target.server_identity || !("server_uuid" in target.server_identity
      ? target.server_identity.server_uuid
      : target.server_identity.server_id && target.server_identity.hostname && target.server_identity.version))
      return { status: "refused", message: "The report has no server identity, so nothing was sent. Refresh to load this server.", refresh: true, on };
    const result = await killLockSession(target.via.connection, target.connection_id, mode, {
      user: target.user,
      host: target.host,
      trx_started: target.trx_started,
      ps_thread_id: target.ps_thread_id,
      connection_identity: target.via.key,
      server_identity: target.server_identity,
    }, reviewToken);
    return { status: result.status as string, message: result.message, refresh: result.status === "refused", after: result.after ?? null, target: result.target ?? null, on };
  } catch (e) {
    // The server answered with an error (nothing was ended), or no answer
    // arrived: then the KILL may have run, and it is never sent again.
    const failure = writeFailure(e, { audited: true });
    return failure.phase === "unknown"
      ? { status: "unknown", message: failure.message, refresh: true, on }
      : { status: "error", message: failure.message, on };
  }
}

export type TransactionEvidence = "unknown" | "none" | "open" | "same" | "other" | "rolling_back" | "survived";
/** Front-end observations extend kill-1.0 without changing its wire contract. */
export type KillObservation = KillAfter & {
  connectionEvidence?: "present" | "gone" | "unknown";
  transactionEvidence?: TransactionEvidence;
  /** A complete transaction-id read cleared previously confirmed XA survival. */
  xaCleared?: boolean;
};

export type KillNote = {
  status: string; message: string; outcome: Outcome[]; refresh?: boolean;
  /** Which KILL was sent (one session). */
  mode?: "connection" | "query";
  /** The single target, retained even when the server could not return its before-read. */
  connectionId?: number;
  /** Client receipt time of the Kill answer; older reports cannot verify it. */
  answeredAt?: number;
  /** Snapshot of target.server_identity from the reviewed Kill, never the active connection. */
  reviewedServerIdentity?: KillServerIdentity;
  /** Automatic verification cannot resolve this view's uncertainty. */
  manualRefresh?: boolean;
  /** Latest read observation, separate from the original server message. */
  update?: string;
  /** kill-1.0: the session read right after the KILL. */
  after?: KillObservation | null;
  /** At least one read actually observed rollback, including the kill answer. */
  rollbackObserved?: boolean;
  /** kill-1.0: the session read just before the KILL. */
  target?: KillTargetRead | null;
  /** Follow-up re-reads of a rolling-back kill: rows still left to undo. */
  rollbackLeft?: number | null;
  /** The follow-up re-reads ended with the rollback still running. */
  rollbackStale?: boolean;
  /** The list is being read again after the KILL (the view's loading line
   * says so; the note marks it with data-refreshing). */
  refreshing?: boolean;
  /** The connection the KILL was reviewed for (its key and label). */
  on?: { key: string; label: string };
  /** False only when the write endpoint was never called (policy check failed). */
  sent?: boolean;
  /** It arrived after its sheet was closed, maybe after a switch: named even
   * when the owner cannot say which connection is in use (no currentKey). */
  late?: boolean;
};
/** Bind reconciliation to a post-answer read of the exact reviewed server.
 * Compare identity fields explicitly so object key order has no meaning. */
export function killReportMatches(note: KillNote, timestamp: string, identity?: KillServerIdentity): boolean {
  const expected = note.reviewedServerIdentity;
  if (note.answeredAt === undefined || !(Date.parse(timestamp) > note.answeredAt) || !expected || !identity) return false;
  return "server_uuid" in expected
    ? "server_uuid" in identity && !!expected.server_uuid && expected.server_uuid === identity.server_uuid
    : "server_id" in identity && !!expected.server_id && !!expected.hostname && !!expected.version &&
      expected.server_id === identity.server_id && expected.hostname === identity.hostname && expected.version === identity.version;
}

/** Keep a manual way to verify when repeating this view cannot settle the question. */
export function manualKillRefresh(note: KillNote): KillNote {
  return { ...note, refreshing: false, refresh: true, manualRefresh: true };
}

/** One acceptance path for view loads and Server Refresh all. Awaiting its
 * acknowledgement keeps polling independent of React's commit scheduling. */
export function useKillReport<T extends object>(bound: Bound<T> | null, currentNote: React.MutableRefObject<KillNote | null>,
  setNote: (note: KillNote) => void, reconcile: (note: KillNote, report: T) => KillNote) {
  const mounted = useRef(true);
  const observed = useRef(new WeakSet<T>());
  const waiting = useRef(new Set<() => void>());
  const noteAtRender = currentNote.current;
  useLayoutEffect(() => {
    const current = currentNote.current;
    if (bound && current && current === noteAtRender && current.on === noteAtRender.on && current.on?.key === bound.key)
      setNote(reconcile(current, bound.report));
    if (bound) observed.current.add(bound.report);
    // A newer committed report also settles a superseded accepted report.
    waiting.current.forEach(resolve => resolve());
    waiting.current.clear();
  }, [bound]);
  useLayoutEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      waiting.current.forEach(resolve => resolve());
      waiting.current.clear();
    };
  }, []);
  return async (read: () => Promise<T | null>): Promise<T | null> => {
    const fresh = await read();
    // The exact report might already have committed before this continuation.
    if (fresh && mounted.current && !observed.current.has(fresh))
      await new Promise<void>(resolve => { waiting.current.add(resolve); });
    return fresh;
  };
}

/** Normalize the initial kill-1.0 answer or use explicit report evidence. */
export function killTransactionEvidence(note: KillNote): TransactionEvidence {
  const a = note.after;
  if (!a) return "unknown";
  if (a.trx_survived === true) return "survived";
  if (a.transactionEvidence) return a.transactionEvidence;
  if (!a.present) return a.trx_survival_checked === false ? "unknown" : "none";
  if (a.trx_state === "ROLLING BACK") return "rolling_back";
  if (a.trx_checked !== true) return "unknown";
  if (!a.trx_state && !a.trx_started) return "none";
  if (a.command === "Killed") return "rolling_back";
  return a.same_trx === true ? "same" : a.same_trx === false ? "other" : "open";
}

/** Match starts and ids when both ids are known; an unknown identity stays open. */
export function reconcileTransaction(note: KillNote, state: KillObservation, trxId?: string | null): KillObservation {
  // Neither a process list nor a connection reused with this id disproves XA survival.
  if (note.after?.trx_survived === true && state.transactionEvidence !== "survived" && state.trx_survival_checked !== true)
    return { ...state, transactionEvidence: "survived", trx_survived: true,
      trx_survival_checked: true, rows_locked: note.after.rows_locked };
  // Processlist cannot observe detached XA, even after it proves a disconnect.
  if (note.after?.trx_survival_checked === false && state.trx_survival_checked !== true)
    return { ...state, transactionEvidence: "unknown", trx_survival_checked: false };
  const before = note.target?.trx;
  let same: boolean | null = null;
  if (["open", "rolling_back"].includes(state.transactionEvidence || "") && before?.started && state.trx_started) {
    same = before.started === state.trx_started;
    if (before.trx_id != null && trxId != null) same = same && before.trx_id === trxId;
  }
  return { ...state, same_trx: same,
    transactionEvidence: state.transactionEvidence === "open" && same !== null ? (same ? "same" : "other") : state.transactionEvidence };
}

/** One fresh observation supplies the update; never infer statement completion. */
export function reconcileKillState(note: KillNote, after: KillObservation): KillNote {
  const next = { ...note, after, refreshing: false, rollbackLeft: after.rows_modified ?? null,
    rollbackObserved: note.rollbackObserved || killRollingBack(note), rollbackStale: false, refresh: false, manualRefresh: false };
  const evidence = killTransactionEvidence(next);
  next.rollbackObserved = next.rollbackObserved || evidence === "rolling_back";
  // Refresh stays on the note while anything is still open: a rollback, an
  // unknown transaction, or a waiter that still waits or cannot be seen.
  next.refresh = evidence === "unknown" || evidence === "rolling_back" ||
    next.outcome.some(o => o.unknown || o.waiting);
  if (evidence === "rolling_back") {
    next.update = next.rollbackLeft === null ? "Checked again: still rolling back."
      : `Checked again: still rolling back, about ${number(next.rollbackLeft)} changes left to undo.`;
  } else if (evidence === "survived") {
    next.update = "Checked again: its transaction is still open as a prepared XA transaction.";
  } else if (note.after?.trx_survived === true && after.trx_survived === false) {
    next.update = "Checked again: its prepared XA transaction is no longer listed.";
  } else if (after.connectionEvidence === "gone") {
    next.update = evidence === "unknown"
      ? "Checked again: the connection has ended; whether its transaction survived as a prepared XA transaction is unknown."
      : "Checked again: the connection has ended.";
  } else if (evidence === "none") {
    next.update = "Checked again: its transaction is no longer open.";
  } else if (evidence === "same" || evidence === "open") {
    next.update = "Checked again: its transaction is still open.";
  } else if (evidence === "other") {
    next.update = "Checked again: it has started another transaction.";
  } else {
    next.update = "Checked again: this list cannot show whether its transaction is still open.";
  }
  return next;
}

/** The first words of a Kill note: what happened, never more than is known. */
export function killStatusWord(status: string): string {
  switch (status) {
    // The server accepted the KILL; the message says what it changed.
    case "killed":
    case "ok":
      return "Done.";
    case "pending":
      return "Waiting for the reply.";
    case "warn":
      return "Nothing was ended.";
    case "gone":
      return "Already ended.";
    case "denied":
      return "The server refused.";
    case "error":
      return "Could not end it.";
    case "unknown":
      return "Outcome unknown.";
    default:
      return "Nothing was sent.";
  }
}
/** Older callers can also describe a lost reply as an error in the message. */
export function killHeadline(note: Pick<KillNote, "status" | "message" | "mode">): string {
  return note.status === "error" && /outcome is unknown|may have reached the server/i.test(note.message)
    ? "Outcome unknown." : killStatusWord(note.status);
}

/** Re-read delays after a kill whose connection is still ending (KILL
 * CONNECTION rolls its transaction back first): 2, 4, 8 and 16 s, then stop. */
export const KILL_FOLLOW_UP_S = [2, 4, 8, 16];

/** The after-read says the connection is ending: it is rolling back. */
export function killRollingBack(note: KillNote): boolean {
  return killTransactionEvidence(note) === "rolling_back";
}

/** An open question is pollable only when the view can supply complete evidence. */
export function killPollDone(note: KillNote): boolean {
  const evidence = killTransactionEvidence(note);
  return evidence !== "rolling_back" && evidence !== "unknown";
}

/** Green requires a verification read and no remaining or unknown held state. */
export function killHeld(note: KillNote): boolean {
  const evidence = killTransactionEvidence(note);
  return !note.after || !["none", "other"].includes(evidence) || note.outcome.some(o => o.unknown || o.waiting);
}

/** Timed re-reads while a kill rolls back. Single-flight: the next wait starts
 * when the previous read ends. *step* answers true when the rollback is done
 * or the note is stale; on the last delay it marks the note instead. The
 * returned function cancels the rest. */
export function followKill(step: (last: boolean) => Promise<boolean>, delays: number[] = [...KILL_FOLLOW_UP_S]): () => void {
  let cancelled = false;
  let index = 0;
  let timer = 0;
  const run = () => {
    if (cancelled) return;
    void step(index === delays.length - 1).then((done) => {
      if (cancelled || done) return;
      index += 1;
      if (index < delays.length) timer = window.setTimeout(run, delays[index] * 1000);
    });
  };
  timer = window.setTimeout(run, delays[0] * 1000);
  return () => {
    cancelled = true;
    window.clearTimeout(timer);
  };
}

/** The state-colored line of a Kill note, when the after-read (or a follow-up
 * re-read) proves one: amber while the transaction or its rollback goes on,
 * green once the waiters are free. */
export function killStateLine(note: KillNote): { tone: "warn" | "ok"; text: string } | null {
  const after = note.after;
  if (after?.trx_survived === true)
    return { tone: "warn", text: "Prepared XA transaction and locks still open." };
  if (killRollingBack(note)) {
    // trx_rows_modified falls as the rollback undoes records; rows_locked does not.
    const left = note.rollbackLeft ?? after?.rows_modified;
    return left !== null && left !== undefined
      ? { tone: "warn", text: `Rolling back: about ${number(left)} changes left to undo.` }
      : { tone: "warn", text: "Rolling back." };
  }
  if (note.mode === "query" && after?.same_trx === true &&
      (after.statement === null || after.statement === "" || (after.statement !== undefined && after.statement !== note.target?.info)))
    return { tone: "warn", text: "Query ended; transaction and locks still open." };
  if (!killHeld(note) && note.outcome.length > 0) {
    const ids = note.outcome.map((o) => o.id);
    return { tone: "ok", text: `Lock released: ${list(ids)} no longer ${ids.length > 1 ? "wait" : "waits"}.` };
  }
  return null;
}

/** One line of the running statement, clipped for the evidence list. */
function clipStatement(text: string | null | undefined): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > 80 ? `${flat.slice(0, 79)}…` : flat;
}

/** The before/after evidence of a Kill note (from the kill-1.0 target and
 * after reads); the waiting sessions are the outcome list below it. */
export function killEvidence(note: KillNote): { label: string; before: string; after: string }[] {
  const target = note.target;
  if (!target) return [];
  const after = note.after ?? null;
  const evidence = killTransactionEvidence(note);
  const gone = after?.connectionEvidence === "gone" || (after && !after.connectionEvidence && !after.present);
  const rows: { label: string; before: string; after: string }[] = [];
  rows.push({ label: "Statement", before: clipStatement(target.info) || "none",
    after: !after ? "unknown" : gone ? "none" : after.statement !== undefined
      ? clipStatement(after.statement) || "none" : "unknown" });
  const beforeTrx = target.trx ? `open since ${target.trx.started || "unknown"}` : target.trx_checked ? "none" : "unknown";
  const afterTrx = evidence === "survived" ? "prepared XA still open"
    : evidence === "rolling_back" ? "rolling back"
    : evidence === "same" ? "still open"
    : evidence === "none" ? (after?.xaCleared ? "none"
      : target.trx && (gone || note.rollbackObserved) ? "rolled back" : "none")
    : evidence === "open" || evidence === "other" ? `open since ${after?.trx_started || "unknown"}` : "unknown";
  rows.push({ label: "Transaction", before: beforeTrx, after: afterTrx });
  rows.push({ label: "Rows locked", before: target.trx?.rows_locked != null ? number(target.trx.rows_locked) : "unknown",
    after: !after ? "unknown" : evidence === "none" ? "none" : evidence === "unknown" ? "unknown"
      : after.rows_locked != null ? number(after.rows_locked) : "unknown" });
  return rows;
}

/** The answer to a Kill, announced and focused. Status "pending": the sheet
 * was closed before the reply came (Kill sessions); the outcome replaces it.
 * A note about another connection than the one in use (*currentKey*) names
 * it and offers no Refresh: a refresh would read the connection in use. */
export function KillResultNote({ note, onDismiss, noteRef, onRefresh, currentKey }: {
  note: KillNote; onDismiss: () => void; noteRef: React.RefObject<HTMLDivElement>; onRefresh?: () => void;
  /** The key of the connection in use; without it only a late note is named. */
  currentKey?: string;
}) {
  const known = currentKey !== undefined;
  // Another connection is in use: its Refresh would read that one, not this.
  const differs = !!note.on && known && note.on.key !== currentKey;
  const named = differs || (!known && !!note.on && !!note.late) ? note.on!.label : "";
  const done = note.status === "killed" || note.status === "ok" || note.status === "pending";
  const stateLine = killStateLine(note);
  const evidence = killEvidence(note);
  return (
    <div ref={noteRef} tabIndex={-1} className={`kill-result ${note.status}${killHeld(note) ? " held" : ""}`} role={done ? "status" : "alert"}
      aria-busy={note.status === "pending" || undefined} data-connection={note.on?.key}
      data-status={note.status} data-refreshing={note.refreshing ? "true" : undefined}>
      <p>
        {note.status === "pending" && <><span className="spinner" aria-hidden="true" />{" "}</>}
        <strong>{killHeadline(note)}</strong>{" "}
        {note.message}
      </p>
      {note.update && <p className="hint kill-update">{note.update}</p>}
      {stateLine && <p className={`kill-state ${stateLine.tone}`}>{stateLine.text}</p>}
      {named && (
        <p className="hint kill-result-on">
          {note.sent === false ? "Reviewed on" : "Sent on"} {named}{differs ? ", not the connection in use" : ""}.
        </p>
      )}
      {note.refreshing && <p className="hint kill-result-rereading"><span className="spinner" aria-hidden="true" /> Reading the sessions again to show what changed…</p>}
      {evidence.length > 0 && (
        <dl className="kill-evidence">
          {evidence.map((row) => (
            <div key={row.label}>
              <dt>{row.label}</dt>
              <dd>
                <bdi>{row.before}</bdi> → <bdi>{row.after}</bdi>
              </dd>
            </div>
          ))}
        </dl>
      )}
      {note.outcome.length > 0 && (
        <ul>
          {note.outcome.map((o) => (
            <li key={o.id} className={o.unknown ? "unknown" : o.waiting ? "still" : "free"}>
              {outcomeText(o)}
            </li>
          ))}
        </ul>
      )}
      {note.manualRefresh && <p className="hint kill-manual-refresh">Refresh to check again.</p>}
      {note.rollbackStale && <p className="hint kill-stale">Still rolling back after 30 s. Refresh to check again.</p>}
      <button type="button" className="text-button" onClick={onDismiss}>
        Dismiss
      </button>
      {note.refresh && onRefresh && !differs && <button type="button" className="text-button" onClick={onRefresh}>Refresh</button>}
    </div>
  );
}

/** Keep denied Kill reachable, with its reason and a copyable statement. */
function KillControl({
  allowed,
  connection,
  need,
  target,
  busy,
  onKill,
  onCopied,
  commands,
}: {
  allowed: boolean | null | undefined;
  commands?: LockNode["kill"];
  connection: Connection;
  need?: string;
  target: KillTarget;
  busy: boolean;
  onKill: (target: KillTarget, trigger: HTMLElement) => void;
  onCopied: (text: string) => void;
}) {
  const candidate = { ...target, kill_allowed: allowed, kill_need: need };
  const gate = useKillPrivilege(candidate, connection.user);
  if (target.xa_prepared)
    return (
      <div className="kill-row">
        <span className="hint">
          Prepared XA transaction: its locks belong to the transaction, which survives the end of the session, so ending this
          session would release nothing. End the transaction instead:
        </span>
        {target.xa_prepared.commit && target.xa_prepared.rollback ? (
          <span className="kill-commands">
            <CopyCommand command={target.xa_prepared.rollback} onCopied={onCopied} />
            <CopyCommand command={target.xa_prepared.commit} onCopied={onCopied} />
          </span>
        ) : (
          <span className="hint">XA COMMIT or XA ROLLBACK with its xid (XA RECOVER lists it; it needs XA_RECOVER_ADMIN).</span>
        )}
      </div>
    );
  return (
    <div className="kill-row">
      <VisiblePrivilegeGate action="kill" gate={gate} className="danger"
        disabled={busy || !target.user || !target.host}
        blocked={connection.read_only ? "This connection is read-only." : ""}
        aria-label={`Kill blocker: connection ${target.connection_id}`}
        onClick={(e) => onKill(candidate, e.currentTarget)}>Kill…</VisiblePrivilegeGate>
      {gate.disabledReason ? <CopyCommand command={`KILL ${target.connection_id}`} onCopied={onCopied} /> : commands && (
        <Disclosure className="kill-manual" title="End it yourself">
          <p className="hint">{gate.state === "allowed" ? "Or run it yourself in a SQL client:" : "To end it, run this yourself in a SQL client:"}</p>
          <div className="kill-commands">
            {commands.query && <CopyCommand command={commands.query} onCopied={onCopied} />}
            <CopyCommand command={commands.connection} onCopied={onCopied} />
          </div>
          <p className="hint">{commands.query
            ? "KILL QUERY stops the statement; its transaction stays open with its row and metadata locks unless it runs in autocommit mode. KILL ends the connection and rolls the transaction back."
            : "KILL ends the connection and rolls its open transaction back, which can take a while after many changes."}</p>
        </Disclosure>
      )}
    </div>
  );
}

const DURATION_TEXT: Record<string, string> = {
  TRANSACTION: "until its transaction ends",
  STATEMENT: "until its statement ends",
  EXPLICIT: "until it releases it",
};

function blockerReason(b: DdlBlocker, d: DdlWait) {
  const where = objectName(d.schema, d.table);
  if (b.reason === "chain") return `Blocks connection ${b.via}, which the ${d.kind} waits for.`;
  if (b.reason === "queued") return "Its own lock request is ahead of the DDL in the queue.";
  if (b.reason === "open_transaction")
    return b.started_before_ddl === true
      ? "Open transaction that started before the DDL."
      : b.started_before_ddl === false
        ? "Open transaction that started after the DDL; if the DDL is an ALTER TABLE, it can still block the final step."
        : "Open transaction.";
  const held = b.locks
    .map((l) => `${l.lock_type || "a lock"}${l.duration && DURATION_TEXT[l.duration] ? ` ${DURATION_TEXT[l.duration]}` : ""}`)
    .join(", ");
  return `Holds ${held} on ${where}.`;
}

const PRECISION_HEAD: Record<DdlWait["precision"], string> = {
  exact: "Blocked by",
  granted_locks: "Holding a lock on the table",
  likely: "Likely blockers",
};

/** One line of what a DDL statement waits for. */
function ddlLine(d: DdlWait) {
  const where = objectName(d.schema, d.table);
  const named = d.statement_table ? objectName(d.statement_schema, d.statement_table) : "";
  // A foreign key or RENAME also locks other tables (MySQL): say which one it waits on.
  const other = !!named && !!d.table && named !== where;
  return (
    `${d.kind} ${d.table ? `on ${other ? named : where} ` : ""}waits for ` +
    `${d.requested ? `an ${d.requested} metadata lock` : "a metadata lock"}` +
    `${other ? ` on ${where}, a table its foreign key or rename also locks` : ""}` +
    `${d.object_type && d.object_type !== "TABLE" ? ` on ${d.object_type.toLowerCase()} ${where}` : ""}.`
  );
}

type TreeRow = { node: LockNode; path: string; depth: number; parent: string | null };
function flatten(nodes: LockNode[], collapsed: Set<string>, depth = 0, prefix = "", parent: string | null = null): TreeRow[] {
  const rows: TreeRow[] = [];
  nodes.forEach((node, i) => {
    const path = prefix ? `${prefix}-${i}` : String(i);
    rows.push({ node, path, depth, parent });
    if (node.blocked.length && !collapsed.has(path)) rows.push(...flatten(node.blocked, collapsed, depth + 1, path, path));
  });
  return rows;
}
function findNode(nodes: LockNode[], path: string): LockNode | null {
  const parts = path.split("-").map(Number);
  let level = nodes;
  let node: LockNode | null = null;
  for (const i of parts) {
    node = level[i] || null;
    if (!node) return null;
    level = node.blocked;
  }
  return node;
}

type Detail = "transactions" | "rows" | "mdl" | "deadlock";

export function LocksView({
  connection,
  disabled,
  onBack,
  selection,
  report: bound,
  onReport,
  onShowPlan,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<LocksReport> | null;
  onReport: (report: Bound<LocksReport>) => void;
  /** Opens the plan of a connection's running statement, read on *from*; rejects with a readable error. */
  onShowPlan: (id: number, from: Connection) => Promise<void>;
}) {
  const reads = useConnectionReads(connection);
  const policy = useWritePolicy();
  const report = bound?.report ?? null;
  // A report read from another connection stays on screen, greyed, and none
  // of its actions run until it is refreshed on the connection in use.
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [planLoading, setPlanLoading] = useState("");
  const [planError, setPlanError] = useState("");
  const loading = reads.loading || planLoading;
  const error = reads.error || planError;
  const [status, setStatus] = useState("");
  const [waitsOnly, setWaitsOnly] = useState(report?.scope === "waits");
  const [auto, setAuto] = useState(0);
  const [detail, setDetail] = useState<Detail>("transactions");
  const [rowTable, setRowTable] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [rawOpen, setRawOpen] = useState(false);
  const [killTarget, setKillTarget] = useState<KillTarget | null>(null);
  const [killing, setKilling] = useState(false);
  // The Kill sheet was closed while its KILL had no reply yet: the view says so.
  const [killLeft, setKillLeft] = useState<{ id: number; startedAt: number; key: string; label: string } | null>(null);
  const killSentAt = useRef(0);
  const [killNote, setKillNoteState] = useState<KillNote | null>(null);
  // Update synchronously so an in-flight answer observes Dismiss or replacement.
  const currentKillNote = useRef<KillNote | null>(null);
  function setKillNote(update: KillNote | null | ((current: KillNote | null) => KillNote | null)) {
    const next = typeof update === "function" ? update(currentKillNote.current) : update;
    currentKillNote.current = next;
    setKillNoteState(next);
  }
  const killMounted = useRef(false);
  // The follow-up re-reads of a rolling-back kill: cancelled when the note is
  // dismissed or replaced, or when the view unmounts; each re-read also stops
  // when the connection in use is no longer the one the Kill was sent on.
  const killFollowCancel = useRef<(() => void) | null>(null);
  useEffect(() => {
    killMounted.current = true;
    return () => {
      killMounted.current = false;
      killFollowCancel.current?.();
    };
  }, []);
  function cancelKillFollow() {
    killFollowCancel.current?.();
    killFollowCancel.current = null;
  }
  const root = useRef<HTMLDivElement>(null);
  const killTrigger = useRef<HTMLElement | null>(null);
  const killNoteRef = useRef<HTMLDivElement>(null);
  const busy = disabled || !!loading || killing || !!other;

  /** Read locks; a read with other options replaces one still running. */
  async function load(force = false): Promise<LocksReport | null> {
    setPlanError("");
    const fresh = await reads.run(
      `locks:${waitsOnly}`,
      "Reading locks…",
      (c, signal) => api<LocksReport>("live/locks", { connection: c, limit: 500, waits_only: waitsOnly }, signal),
      onReport,
      { force, onError: () => setAuto(0) },
    );
    return fresh ? fresh.report : null;
  }
  const reportReconciled = useKillReport(bound, currentKillNote, setKillNote, reconcileKillNote);
  // Timers use the current scope/report, including changes made after confirmation.
  const canRecheckKill = useRef<(note?: KillNote) => boolean>(() => false);
  canRecheckKill.current = note => !!report && !other && !waitsOnly && locksCanRecheck(report) &&
    (!note || killReportMatches(note, report.captured_at, report.server_identity));

  const loadRef = useRef(load);
  loadRef.current = load;

  // Automatic refresh is opt-in, never below 10 seconds, and pauses while the page
  // is hidden, the view is not on screen (another place, or the list of views
  // on a narrow window), a read runs, or a Kill dialog is open (it never ends
  // a session by itself).
  const dialogOpen = useRef(false);
  dialogOpen.current = !!killTarget;
  const running = reads.running;
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => {
      const view = root.current?.closest<HTMLElement>(".server-view");
      const visible = !!view && view.offsetParent !== null;
      if (!document.hidden && visible && !disabled && !dialogOpen.current && !running()) loadRef.current();
    }, auto * 1000);
    return () => window.clearInterval(timer);
  }, [auto, disabled, running]);

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!loading || running(), read: () => { void loadRef.current(); } });

  function openKill(target: KillTarget, trigger: HTMLElement) {
    if (other) return;
    killTrigger.current = trigger;
    cancelKillFollow();
    setKillNote(null);
    setKillTarget({
      ...target,
      waiting: report ? blockingOf(report, target.connection_id).ids : [],
      trx_checked: report?.trx_checked ?? null,
      via: killVia(connection),
      server_identity: report?.server_identity ? { ...report.server_identity } : undefined,
    });
  }
  function closeKill() {
    setKillTarget(null);
    window.setTimeout(() => killTrigger.current?.focus(), 0);
  }
  /** One confirmed KILL: its answer shows at once, then a fresh report says
   * what changed for the sessions it blocked. */
  async function confirmKill(mode: "connection" | "query", reviewToken: string) {
    if (!killTarget || !report || killing) return;
    const target = killTarget;
    const { ids } = blockingOf(report, target.connection_id);
    const kinds = new Map((report.ddl_waits || []).map((d) => [d.connection_id, d.kind] as const));
    setKilling(true);
    killSentAt.current = Date.now();
    const sent = await sendKill(connection, target, mode, policy, reviewToken);
    if (!killMounted.current) return;
    // Re-read and infer the outcome only while the connection in use is still
    // the one the Kill was confirmed for (checked now, after the await); a
    // report of another server says nothing about this Kill. The answer is
    // shown at once, and says the list is read again meanwhile.
    const changed = !target.via || reads.currentKey() !== target.via.key;
    // The report is always read once after the answer (the tree shows the
    // change); only the timed follow-ups need a read that can settle it.
    const reread = !changed && !sent.refresh;
    let note: KillNote = { ...sent, mode, connectionId: target.connection_id,
      answeredAt: Date.now(), reviewedServerIdentity: target.server_identity ? { ...target.server_identity } : undefined,
      outcome: sent.status === "killed" ? outcomeOf(report, ids, kinds) : [], refreshing: reread };
    setKillTarget(null);
    setKillLeft(null);
    setKilling(false);
    setKillNote(note);
    window.setTimeout(() => killNoteRef.current?.focus(), 0);
    if (!reread) return;
    const fresh = await reportReconciled(() => loadRef.current(true));
    if (!killMounted.current || currentKillNote.current?.on !== note.on) return;
    const answered = currentKillNote.current!;
    if (!fresh || answered.refreshing) setKillNote({ ...answered, refreshing: false });
    // Only a KILL the server ran can still change: never time follow-ups for a
    // refused, denied, gone or failed answer.
    if (answered.status !== "killed" || killPollDone(answered) || !target.via) return;
    if (!canRecheckKill.current(answered)) { setKillNote(manualKillRefresh(answered)); return; }
    const key = target.via.key;
    cancelKillFollow();
    killFollowCancel.current = followKill(async (last) => {
      const current = currentKillNote.current;
      if (!killMounted.current || !current || current.on !== note.on || reads.currentKey() !== key) return true;
      if (killPollDone(current)) return true;
      if (!canRecheckKill.current(current)) { setKillNote(manualKillRefresh(current)); return true; }
      setKillNote({ ...current, refreshing: true });
      const again = await reportReconciled(() => loadRef.current(true));
      const latest = currentKillNote.current;
      if (!killMounted.current || !latest || latest.on !== note.on) return true;
      if (reads.currentKey() !== key) {
        setKillNote({ ...latest, refreshing: false });
        return true;
      }
      // A superseded read is not completion; the accepted bound report reconciles it.
      if (!again) { setKillNote({ ...latest, refreshing: false }); return false; }
      const rolling = killRollingBack(latest);
      const done = killPollDone(latest) || !canRecheckKill.current(latest);
      setKillNote({ ...latest, refreshing: false, rollbackStale: last && rolling, refresh: (last && rolling) || latest.refresh });
      return done;
    });
  }

  async function showPlan(id: number) {
    if (!bound || other) return;
    setPlanError("");
    reads.clearError();
    setPlanLoading(`Reading the plan of connection ${id}…`);
    try {
      await onShowPlan(id, connection);
    } catch (e) {
      setPlanError((e as Error).message);
    } finally {
      setPlanLoading("");
    }
  }

  // These labels describe the legacy target hint. SUPER-only grants on
  // newer MariaDB are ambiguous because SHOW GRANTS omits the grant's age.
  const killNeed = report?.engine === "mariadb" ? "CONNECTION ADMIN" : "CONNECTION_ADMIN or SUPER";
  const killAllowed = (allowed: boolean | null | undefined) => allowed === false && report?.kill_privilege?.super_only ? null : allowed;
  const caution = report?.capabilities.data_locks_global_latch;
  const s = report?.summary;
  const mdl = report?.metadata_locks;
  const ddls = report?.ddl_waits || [];
  const treeRows = report ? flatten(report.blocking_tree, collapsed) : [];
  const rowTables = useMemo(
    () => Array.from(new Set((report?.row_locks || []).map((l) => objectName(l.schema, l.table)))).sort(),
    [report],
  );
  const rowLocks = (report?.row_locks || []).filter((l) => !rowTable || objectName(l.schema, l.table) === rowTable);
  const transactions = report?.transactions || [];
  const deadlock = report?.deadlock || null;
  const engineName = report ? `${report.server_product || (report.engine === "mariadb" ? "MariaDB" : "MySQL")} ${report.server_version}` : "";
  const idleRoot = report?.blocking_tree.find((n) => n.root && n.idle_in_transaction);

  // ---- The inspector for the selected row ----
  let inspector: InspectorContent | null = null;
  const key = selection.key;
  if (report && key.startsWith("ddl:")) {
    const [, cid, part, index] = key.split(":");
    const d = ddls.find((x) => String(x.connection_id) === cid);
    if (d && !part) {
      inspector = {
        key,
        title: `Waiting DDL on connection ${d.connection_id}`,
        meta: (
          <>
            <Pill tone="danger">Waiting DDL</Pill>
            <span>waits ~{seconds(d.wait_s)}</span>
            {d.user && <span>· {d.user}@{d.host || "?"}</span>}
          </>
        ),
        body: (
          <div className="ddl-card">
            <InspectorSection
              title="What it waits for"
              info={
                <p>
                  A DDL statement (ALTER TABLE, CREATE INDEX, DROP, RENAME, TRUNCATE, OPTIMIZE) needs exclusive access to its
                  table, at least briefly. It waits until every transaction or running statement that has used the table ends,
                  and while it waits, new reads and writes of that table queue behind it.
                </p>
              }
            >
              <p className="lock-wait">{ddlLine(d)}</p>
            </InspectorSection>
            {d.statement && <Sql text={d.statement} label="DDL" />}
            <InspectorSection title={PRECISION_HEAD[d.precision]}>
              {d.precision === "likely" && (
                <p className="hint">
                  This server is not recording metadata locks, so myflames cannot see exactly which session holds{" "}
                  {d.table ? objectName(d.schema, d.table) : "the table"}. These are the open transactions, idle ones and ones
                  that started before the DDL first. The notes say how to get exact blockers.
                </p>
              )}
              {d.precision === "granted_locks" && (
                <p className="hint">
                  From information_schema.METADATA_LOCK_INFO, which lists granted locks but not waits: one of these is what the
                  DDL waits for.
                </p>
              )}
              {d.blockers.length === 0 ? (
                <p className="hint">
                  {d.precision === "exact" ? "No conflicting lock found; refresh in a moment." : "No open transaction found (see the notes)."}
                </p>
              ) : (
                <ul className="ddl-blocker-list">
                  {d.blockers.map((b, i) => (
                    <li key={i}>
                      <button type="button" className="text-button" onClick={() => selection.select(`ddl:${d.connection_id}:blocker:${i}`, true)}>
                        {b.connection_id ? `Connection ${b.connection_id}` : "No client connection"}
                      </button>{" "}
                      <span className="hint">{blockerReason(b, d)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {d.blockers_truncated && <p className="hint">Showing the first {d.blockers.length}.</p>}
            </InspectorSection>
            {d.queued.length > 0 && <QueuedSection d={d} onCopied={setStatus} />}
          </div>
        ),
      };
    } else if (d && part === "blocker") {
      const b = d.blockers[Number(index)];
      if (b) {
        const who = [b.user && `${b.user}@${b.host || "?"}`, b.db].filter(Boolean).join(" · ");
        const facts = [
          b.trx_age_s !== null ? `transaction open ${seconds(b.trx_age_s)}` : "",
          b.rows_modified !== null ? `${number(b.rows_modified)} ${b.rows_modified === 1 ? "row" : "rows"} changed` : "",
        ].filter(Boolean);
        inspector = {
          key,
          title: b.connection_id ? `Connection ${b.connection_id}` : "No client connection",
          meta: (
            <>
              {b.culprit && <Pill tone="danger">Usual culprit</Pill>}
              {b.idle_in_transaction ? (
                <Pill tone="warn" title="The session is idle (Sleep) but its transaction is still open and keeps its locks. Usually the application forgot to commit.">
                  Idle in transaction {seconds(b.time_s)}
                </Pill>
              ) : b.running ? (
                <span>running {seconds(b.time_s)}</span>
              ) : null}
              {who && <span>{who}</span>}
              {!b.connection_id && b.ps_thread_id && <span>server thread {b.ps_thread_id}</span>}
            </>
          ),
          actions: b.connection_id
            ? (
              <KillControl connection={connection} need={killNeed} allowed={killAllowed(b.kill_allowed)} target={targetFromBlocker(b)} busy={busy} onKill={openKill} onCopied={setStatus} />
            )
            : undefined,
          body: (
            <div className="ddl-blocker">
              <InspectorSection title={`Why it blocks the ${d.kind}`}>
                <p className="lock-meta">
                  {blockerReason(b, d)}
                  {b.connection_id && facts.length > 0 && ` ${facts.join(" · ").replace(/^t/, "T")}.`}
                  {!b.connection_id &&
                    (report.engine === "mariadb"
                      ? " It belongs to a background server thread."
                      : " It belongs to a background server thread, or to a prepared XA transaction that is no longer attached to a session. XA RECOVER lists prepared XA transactions (it needs XA_RECOVER_ADMIN), and XA COMMIT 'xid' or XA ROLLBACK 'xid' ends one.")}
                </p>
                {b.waits_for.length > 0 && (
                  <p className="lock-wait">
                    It is itself waiting for {list(b.waits_for)}, so ending {b.waits_for.length > 1 ? "those" : "that one"} may let it
                    finish.
                  </p>
                )}
              </InspectorSection>
              {b.statement ? (
                <Sql text={b.statement} label="Running" />
              ) : b.last_statement ? (
                <Sql text={b.last_statement} label="Last statement" />
              ) : null}
            </div>
          ),
        };
      }
    } else if (d && part === "queued") {
      inspector = {
        key,
        title: `${d.queued.length} ${d.queued.length === 1 ? "session" : "sessions"} queued behind connection ${d.connection_id}`,
        body: (
          <div className="ddl-queue">
            <QueuedSection d={d} onCopied={setStatus} />
          </div>
        ),
      };
    }
  } else if (report && key.startsWith("tree:")) {
    const node = findNode(report.blocking_tree, key.slice(5));
    if (node) {
      const running = node.command !== "Sleep" && !!node.query;
      const who = [node.user && `${node.user}@${node.host || "?"}`, node.db].filter(Boolean).join(" · ");
      // One age per inspector, from one read: the transaction's own (Open
      // for, from INNODB_TRX) when known; the session's idle time (the
      // process list, read a moment apart) only when it is not.
      const openFor = node.trx_id && node.trx_age_s !== null && node.trx_age_s !== undefined;
      inspector = {
        key,
        title: node.connection_id ? `Connection ${node.connection_id}` : `Transaction ${node.trx_id} (no session)`,
        meta: (
          <>
            {node.root && <Pill tone="danger">Root blocker</Pill>}
            {node.cycle && <Pill tone="danger">Wait cycle</Pill>}
            {node.idle_in_transaction && (
              <Pill tone="warn">{openFor ? "Idle in transaction" : `Idle in transaction ${seconds(node.time_s)}`}</Pill>
            )}
            {who && <span>{who}</span>}
          </>
        ),
        actions: node.connection_id ? (
          <>
            {running ? (
              <button
                type="button"
                className="button"
                disabled={busy}
                aria-label={`Show plan for connection ${node.connection_id}`}
                onClick={() => void showPlan(node.connection_id as number)}
              >
                Show plan
              </button>
            ) : null}
            {node.kill && (
              <KillControl
                connection={connection} need={killNeed}
                allowed={killAllowed(node.kill_allowed)}
                target={targetFromNode(node)}
                commands={node.kill}
                busy={busy}
                onKill={openKill}
                onCopied={setStatus}
              />
            )}
          </>
        ) : undefined,
        body: (
          <div className="lock-node-detail">
            {(node.waits_for || []).length > 0 && (
              <InspectorSection title="Waits for">
                {(node.waits_for || []).map((edge, i) => (
                  <p key={i} className="lock-wait">
                    Waits <strong>{edgeSeconds(edge)}</strong> for {waitText(edge)}
                  </p>
                ))}
              </InspectorSection>
            )}
            {node.root && node.blocked_count > 0 && (
              <p className="lock-meta">Blocks {node.blocked_count} {node.blocked_count === 1 ? "session" : "sessions"}.</p>
            )}
            {node.trx_id && (
              <InspectorSection title="Transaction">
                <Fields
                  rows={[
                    ["ID", node.trx_id],
                    node.trx_state && ["State", node.trx_state],
                    node.trx_age_s !== null && ["Open for", seconds(node.trx_age_s)],
                    node.rows_locked !== null && ["Rows locked", number(node.rows_locked)],
                    node.rows_modified !== null && node.rows_modified !== undefined && ["Rows changed", number(node.rows_modified)],
                  ]}
                />
              </InspectorSection>
            )}
            {node.query ? (
              <Sql text={node.query} label="Running" />
            ) : node.last_statement ? (
              <Sql text={node.last_statement} label="Last statement" />
            ) : null}
            {node.connection_id && !running && (
              <p className="hint lock-no-plan">
                Connection {node.connection_id} is not running a statement, so there is no plan to show.
              </p>
            )}
            {node.repeat && <p className="hint">Also shown elsewhere in the tree.</p>}
          </div>
        ),
      };
    }
  } else if (report && key.startsWith("trx:")) {
    const t = transactions.find((x) => `trx:${x.trx_id}:${x.connection_id}` === key);
    if (t)
      inspector = {
        key,
        title: `Transaction ${t.trx_id}`,
        meta: (
          <>
            {t.idle_in_transaction && <Pill tone="warn">Idle {seconds(t.time_s)}</Pill>}
            {t.connection_id !== null && <span>connection {t.connection_id}</span>}
          </>
        ),
        body: (
          <>
            <Fields
              rows={[
                ["State", t.state || "—"],
                ["Open for", seconds(t.age_s)],
                ["Started", t.started || "—"],
                t.wait_s !== null && ["Waiting for", seconds(t.wait_s)],
                ["Rows locked", t.rows_locked === null ? "—" : number(t.rows_locked)],
                ["Rows changed", t.rows_modified === null ? "—" : number(t.rows_modified)],
                ["Lock structs", t.lock_structs === null ? "—" : number(t.lock_structs)],
                ["Isolation", t.isolation_level || "—"],
                ["User", t.user ? `${t.user}@${t.host || "?"}` : "—"],
                ["Session", t.command || "—"],
              ]}
            />
            {t.current_statement ? (
              <Sql text={t.current_statement} label="Running" />
            ) : t.last_statement ? (
              <Sql text={t.last_statement} label="Last statement" />
            ) : null}
          </>
        ),
      };
  } else if (report && key.startsWith("row:")) {
    const l = rowLocks.find((x, i) => `row:${x.lock_id || ""}:${i}` === key);
    if (l)
      inspector = {
        key,
        title: `${l.status === "WAITING" ? "Waiting" : "Granted"} row lock`,
        meta: <span>{objectName(l.schema, l.table)}</span>,
        body: (
          <>
            <InspectorSection title="Mode">
              <p>
                <Mode mode={l.mode} label={l.mode_label} /> {l.explain}
              </p>
            </InspectorSection>
            <Fields
              rows={[
                ["Index", l.index || (l.lock_type === "TABLE" ? "table lock" : "—")],
                ["Lock data", l.lock_data ?? (l.lock_data_missing ? "not in memory (the page is not in the buffer pool)" : "—")],
                ["Transaction", l.trx_id || "—"],
                ["Connection", l.connection_id ?? "—"],
              ]}
            />
          </>
        ),
      };
  } else if (report && mdl && key.startsWith("mdl:")) {
    const m = mdl.locks[Number(key.slice(4))];
    if (m)
      inspector = {
        key,
        title: `${m.status === "PENDING" ? "Waiting" : "Granted"} metadata lock`,
        meta: <span>{`${(m.object_type || "").toLowerCase()} ${objectName(m.schema, m.name)}`}</span>,
        body: (
          <>
            <InspectorSection title="Lock">
              <p>
                <Mode mode={m.short} label={m.lock_type?.toLowerCase().replace(/_/g, " ")} /> {m.explain}
              </p>
            </InspectorSection>
            <Fields rows={[["Duration", (m.duration || "—").toLowerCase()], ["Connection", m.connection_id ?? "—"]]} />
          </>
        ),
      };
  } else if (report && deadlock && key.startsWith("dl:")) {
    const t = deadlock.transactions.find((x) => `dl:${x.n}` === key);
    if (t)
      inspector = {
        key,
        title: `(${t.n}) Transaction ${t.trx_id}`,
        meta: (
          <>
            {t.victim && <Pill tone="danger">Rolled back</Pill>}
            {t.connection_id && <span>connection {t.connection_id}</span>}
            {t.user && <span>{t.user}@{t.host || "?"}</span>}
          </>
        ),
        actions: (
          <button type="button" className="button deadlock-raw-button" aria-expanded={rawOpen} onClick={() => setRawOpen((o) => !o)}>
            {rawOpen ? "Hide raw output" : "Show raw output"}
          </button>
        ),
        body: (
          <div className="deadlock-card">
            {t.statement && <Sql text={t.statement} label="Statement" />}
            <InspectorSection title="Locks">
              <ul className="deadlock-locks">
                {t.holds.map((l, i) => (
                  <DeadlockLockLine key={i} lock={l} verb="Held" />
                ))}
                {t.waiting_for && <DeadlockLockLine lock={t.waiting_for} verb="Waited for" />}
              </ul>
            </InspectorSection>
            {rawOpen && (
              <InspectorSection title="Raw text from SHOW ENGINE INNODB STATUS">
                <pre className="inspector-sql deadlock-raw">{deadlock.raw}</pre>
              </InspectorSection>
            )}
          </div>
        ),
      };
  }

  const tone = !report ? undefined : s && (s.blocked_sessions > 0 || ddls.length > 0) ? "problem" : "ok";
  const summaryText = !report || !s
    ? ""
    : [
        ddls.length ? `${ddls.length} DDL ${ddls.length === 1 ? "statement" : "statements"} waiting` : "",
        s.blocked_sessions ? `${s.blocked_sessions} blocked ${s.blocked_sessions === 1 ? "session" : "sessions"}` : "",
        s.root_blockers ? `${s.root_blockers} root ${s.root_blockers === 1 ? "blocker" : "blockers"}${idleRoot ? " (idle in transaction)" : ""}` : "",
      ]
        .filter(Boolean)
        .join(" · ") || "Nothing is waiting for a lock";

  const trxColumns: Column<LockTransaction>[] = [
    { key: "trx", label: "Transaction", sort: (t) => t.trx_id, render: (t) => t.trx_id },
    { key: "conn", label: "Connection", title: "Connection ID (the processlist ID)", num: true, sort: (t) => t.connection_id, render: (t) => t.connection_id ?? "—" },
    {
      key: "stmt",
      label: "Statement",
      flex: true,
      render: (t) =>
        t.current_statement ? (
          <code className="cell-truncate cell-sql" title={t.current_statement}>
            {flat(t.current_statement)}
          </code>
        ) : t.last_statement ? (
          <code className="cell-truncate cell-sql cell-muted" title={t.last_statement}>
            Last: {flat(t.last_statement)}
          </code>
        ) : (
          "—"
        ),
    },
    { key: "state", label: "State", sort: (t) => t.state || "", render: (t) => t.state || "—" },
    { key: "age", label: "Open for", title: "How long the transaction has been open", num: true, sort: (t) => t.age_s, render: (t) => <Duration value={t.age_s} /> },
    {
      key: "session",
      label: "Session",
      render: (t) => (t.idle_in_transaction ? <Pill tone="warn">Idle <Duration value={t.time_s} /></Pill> : t.command || "—"),
    },
    { key: "locked", label: "Rows locked", title: "Row locks the transaction holds (trx_rows_locked, approximate)", num: true, sort: (t) => t.rows_locked, render: (t) => <CompactNumber value={t.rows_locked} /> },
    { key: "structs", label: "Lock structs", title: "Lock structures the transaction holds (trx_lock_structs); one can cover many rows of a page", num: true, sort: (t) => t.lock_structs, render: (t) => <CompactNumber value={t.lock_structs} /> },
    { key: "iso", label: "Isolation", title: "Transaction isolation level", sort: (t) => t.isolation_level || "", render: (t) => t.isolation_level || "—" },
  ];
  type IndexedRowLock = RowLock & { at: number };
  const rowColumns: Column<IndexedRowLock>[] = [
    {
      key: "status",
      label: "Status",
      render: (l) => (
        <span className={`lock-status ${l.status === "WAITING" ? "waiting" : "granted"}`}>{l.status === "WAITING" ? "Waiting" : "Granted"}</span>
      ),
    },
    { key: "table", label: "Table", flex: true, render: (l) => `${objectName(l.schema, l.table)}${l.partition ? ` (${l.partition})` : ""}` },
    { key: "index", label: "Index", render: (l) => l.index || (l.lock_type === "TABLE" ? "table lock" : "—") },
    { key: "mode", label: "Mode", render: (l) => <Mode mode={l.mode} label={l.mode_label} explain={l.explain} /> },
    {
      key: "data",
      label: "Lock data",
      className: "lock-data",
      render: (l) => (
        <>
          {l.lock_data !== null ? (
            <code>{l.lock_data}</code>
          ) : l.lock_data_missing ? (
            <span className="hint" title="The page is not in the buffer pool, so the server does not show the value.">
              not in memory
            </span>
          ) : (
            "—"
          )}
          {l.lock_data_truncated && <span className="hint"> (cut)</span>}
        </>
      ),
    },
    { key: "trx", label: "Transaction", num: true, render: (l) => l.trx_id || "—" },
    { key: "conn", label: "Connection", num: true, render: (l) => l.connection_id ?? "—" },
  ];
  type IndexedMdl = MetadataLock & { at: number };
  const mdlColumns: Column<IndexedMdl>[] = [
    {
      key: "status",
      label: "Status",
      render: (m) => (
        <span className={`lock-status ${m.status === "PENDING" ? "waiting" : "granted"}`}>{m.status === "PENDING" ? "Waiting" : "Granted"}</span>
      ),
    },
    { key: "object", label: "Object", flex: true, render: (m) => `${(m.object_type || "").toLowerCase()} ${m.schema || m.name ? objectName(m.schema, m.name) : ""}` },
    { key: "lock", label: "Lock", render: (m) => <Mode mode={m.short} label={m.lock_type?.toLowerCase().replace(/_/g, " ")} explain={m.explain} /> },
    { key: "duration", label: "Duration", render: (m) => (m.duration || "—").toLowerCase() },
    { key: "conn", label: "Connection", num: true, render: (m) => m.connection_id ?? "—" },
  ];

  return (
    <ViewLayout
      id="locks"
      title="Locks"
      className="locks-panel"
      onBack={onBack}
      info={
        <>
          <p>Who is blocking whom: waiting DDL statements, the blocking tree with its root blockers, open transactions, row and metadata locks, and the latest deadlock.</p>
          <p>It only reads. The one exception is Kill, which ends a single session after you confirm, and needs the privilege to do so.</p>
        </>
      }
      controls={
        <>
          <Checkbox className="toolbar-check lock-check" checked={waitsOnly} onChange={setWaitsOnly}>
            Waits only
          </Checkbox>
          <AutoRefresh
            value={auto}
            options={INTERVALS}
            onChange={setAuto}
            disabled={!report}
            describedBy={caution ? "locks-caution" : undefined}
          />
        </>
      }
      onRefresh={() => void loadRef.current()}
      refreshDisabled={disabled || !!loading || killing}
      // Refresh reads "Refreshing…" only for its own read, not for Show plan.
      loading={!!reads.loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a session, a transaction or a lock to see its details, its plan, and Kill.</p>}
      status={<><ViewStatus loading={loading} error={error} errorCode={reads.error ? reads.errorCode : ""} onStop={reads.loading ? reads.stop : undefined}
        onRetry={reads.error ? () => void load() : undefined} doneAt={reads.doneAt} />
        {/* A KILL whose sheet was closed, and its answer, name their connection once another is in use. */}
        {killLeft && <div className="view-status"><ActionStatus state={{ phase: "pending", startedAt: killLeft.startedAt,
          message: `Waiting for the reply to the KILL of connection ${killLeft.id}${killLeft.key !== reads.key ? ` on ${killLeft.label}` : ""}. Its outcome shows here.` }} /></div>}
        {killNote && <KillResultNote note={killNote} noteRef={killNoteRef} currentKey={reads.key} onDismiss={() => { cancelKillFollow(); setKillNote(null); }} onRefresh={() => void loadRef.current(true)} />}</>}
    >
      <div ref={root}>
      {caution && (
        <p id="locks-caution" className="lock-caution" role="note">
          On MySQL {report?.server_version}, each read of the lock tables briefly stalls row locking for the whole server.
          Refresh by hand on a busy server, tick Waits only to read less, and keep automatic refresh off.
        </p>
      )}
      <p className="visually-hidden" role="status" aria-live="polite">
        {status}
      </p>
      {status && <p className="hint lock-copied">{status}</p>}

      {killTarget && report && !killLeft && (
        <KillDialog
          target={killTarget}
          blocking={<BlockingList report={report} id={killTarget.connection_id} />}
          busy={killing}
          onCancel={closeKill}
          onConfirm={(mode, token) => void confirmKill(mode, token)}
          onLeave={() => {
            const via = killTarget.via || killVia(connection);
            setKillLeft({ id: killTarget.connection_id, startedAt: killSentAt.current || Date.now(), key: via.key, label: via.label });
            window.setTimeout(() => killTrigger.current?.focus(), 0);
          }}
        />
      )}
      {!report && !loading && !error && (
        <EmptyState
          icon="lock"
          title="No lock report yet"
          action={
            <button type="button" className="button primary" disabled={busy} onClick={() => void load()}>
              Load locks
            </button>
          }
        >
          Which sessions are waiting, who blocks them, open transactions, and the latest deadlock. It only reads.
        </EmptyState>
      )}
      {report && s && mdl && (
        <>
          <SummaryLine tone={tone} notes={report.notes}>
            {tone === "ok" ? "Clear" : "Blocked"} · {summaryText}
          </SummaryLine>
          <FactStrip
            label="Lock summary"
            facts={[
              ["Blocked sessions", String(s.blocked_sessions)],
              ["Root blockers", String(s.root_blockers)],
              ["Longest wait", <Duration value={s.longest_wait_s} />],
              ["Open transactions", String(s.transactions)],
              ["Oldest transaction", <Duration value={s.oldest_transaction_s} />],
              ["Read", `${engineName} at ${new Date(report.captured_at).toLocaleTimeString()}`],
            ]}
          />
          {ddls.length > 0 && (
            <GroupedList
              id="locks-ddl-h"
              title="Waiting DDL"
              count={ddls.length}
              className="ddl-section"
              info={
                <p>
                  A DDL statement (ALTER TABLE, CREATE INDEX, DROP, RENAME, TRUNCATE, OPTIMIZE) needs exclusive access to its
                  table, at least briefly. It waits until every transaction or running statement that has used the table ends,
                  and while it waits, new reads and writes of that table queue behind it.
                </p>
              }
            >
              {ddls.map((d) => (
                <Fragment key={d.connection_id}>
                  <GroupedRow
                    rowKey={`ddl:${d.connection_id}`}
                    selection={selection}
                    tone="problem"
                    toneLabel="Waiting DDL"
                    label={`Connection ${d.connection_id}`}
                    detail={ddlLine(d)}
                    value={`waits ~${seconds(d.wait_s)}`}
                    className="ddl-card-row"
                    attrs={{ "data-ddl": String(d.connection_id) }}
                  />
                  {d.blockers.map((b, i) => (
                    <GroupedRow
                      key={i}
                      depth={1}
                      rowKey={`ddl:${d.connection_id}:blocker:${i}`}
                      selection={selection}
                      tone={b.culprit ? "problem" : "watch"}
                      toneLabel={b.culprit ? "Usual culprit" : PRECISION_HEAD[d.precision] === "Likely blockers" ? "Likely" : "Blocker"}
                      label={b.connection_id ? `Connection ${b.connection_id}` : "No client connection"}
                      detail={
                        b.idle_in_transaction
                          ? `Idle in transaction ${seconds(b.time_s)} · ${blockerReason(b, d)}`
                          : blockerReason(b, d)
                      }
                      value={b.user ? `${b.user}@${b.host || "?"}` : b.ps_thread_id ? `server thread ${b.ps_thread_id}` : ""}
                      className="ddl-blocker-row"
                      attrs={{ "data-blocker": String(b.connection_id ?? "") }}
                    />
                  ))}
                  {d.queued.length > 0 && (
                    <GroupedRow
                      depth={1}
                      rowKey={`ddl:${d.connection_id}:queued`}
                      selection={selection}
                      tone="watch"
                      toneLabel="Queued"
                      label={`${d.queued.length} ${d.queued.length === 1 ? "session" : "sessions"} queued behind it${d.precision === "exact" ? "" : " (likely)"}`}
                      detail={d.queued.map((q) => `connection ${q.connection_id}`).join(", ")}
                      className="ddl-queued-row"
                    />
                  )}
                </Fragment>
              ))}
            </GroupedList>
          )}
          <section className="view-section lock-tree-section" aria-labelledby="locks-tree-h">
            <div className="view-section-head">
              <h3 id="locks-tree-h">Blocking tree</h3>
            </div>
            {treeRows.length === 0 ? (
              <div className="table-empty">No session is waiting for a lock right now.</div>
            ) : (
              <BlockingTree
                rows={treeRows}
                collapsed={collapsed}
                onToggle={(path) =>
                  setCollapsed((c) => {
                    const next = new Set(c);
                    if (next.has(path)) next.delete(path);
                    else next.add(path);
                    return next;
                  })
                }
                selection={selection}
              />
            )}
          </section>
          <section className="view-section" aria-labelledby="locks-detail-h">
            <div className="view-section-head">
              <h3 id="locks-detail-h" className="visually-hidden">
                Transactions and locks
              </h3>
              <Segmented<Detail>
                label="Show"
                value={detail}
                onChange={setDetail}
                options={[
                  { value: "transactions", label: "Transactions", count: report.scope === "deadlock" ? null : transactions.length },
                  { value: "rows", label: "Row locks", count: report.scope === "deadlock" ? null : (report.row_locks || []).length },
                  { value: "mdl", label: "Metadata locks", count: report.scope === "deadlock" ? null : mdl.locks.length },
                  { value: "deadlock", label: "Latest deadlock", count: deadlock ? 1 : 0 },
                ]}
              />
              {detail === "rows" && rowTables.length > 1 && (
                <ToolbarSelect
                  label="Table"
                  className="lock-filter"
                  value={rowTable}
                  onChange={(v) => {
                    setRowTable(v);
                    setShown(PAGE);
                  }}
                  options={[["", "All tables"], ...rowTables.map((t) => [t, t] as [string, string])]}
                />
              )}
            </div>
            {detail === "transactions" && (
              <div aria-labelledby="locks-trx-h" role="group">
                <span id="locks-trx-h" className="visually-hidden">
                  Transactions
                </span>
                {transactions.length === 0 ? (
                  <div className="table-empty">
                    {report.capabilities.transactions
                      ? waitsOnly
                        ? "No transaction is part of a wait."
                        : "No open InnoDB transactions."
                      : "Transactions are not available on this connection (see the notes)."}
                  </div>
                ) : (
                  <PriorityTable
                    id="locks.transactions"
                    detailColumns={["trx", "state", "session", "locked", "structs", "iso"]}
                    // Lock structs and Isolation start off so the list fits at
                    // 1440 px; the Columns menu shows them and the details list both.
                    defaultHidden={["structs", "iso"]}
                    label="Transactions"
                    columns={trxColumns}
                    rows={transactions}
                    rowKey={(t) => `trx:${t.trx_id}:${t.connection_id}`}
                    selection={selection}
                  />
                )}
              </div>
            )}
            {detail === "rows" && (
              <div aria-labelledby="locks-rows-h" role="group">
                <span id="locks-rows-h" className="visually-hidden">
                  Row locks
                </span>
                {report.capabilities.row_locks === "waits_only" && (
                  <p className="grouped-footer lock-rows-note">
                    MariaDB shows only the locks involved in a wait. Rows locked in the transactions table counts the rest.
                  </p>
                )}
                {rowLocks.length === 0 ? (
                  <div className="table-empty">
                    {report.capabilities.row_locks === "none" && !report.capabilities.lock_waits
                      ? "Row locks are not available on this connection (see the notes)."
                      : "No row locks right now."}
                  </div>
                ) : (
                  <PriorityTable
                    id="locks.rows"
                    detailColumns={["index", "data", "trx"]}
                    label="Row locks"
                    columns={rowColumns}
                    rows={rowLocks.slice(0, shown).map((l, i) => ({ ...l, at: i }))}
                    rowKey={(l) => `row:${l.lock_id || ""}:${l.at}`}
                    selection={selection}
                    rowClass={(l) => (l.status === "WAITING" ? "waiting" : "")}
                  />
                )}
                {rowLocks.length > shown && (
                  <button type="button" className="button small table-more" onClick={() => setShown(shown + PAGE)}>
                    Show {Math.min(PAGE, rowLocks.length - shown)} more of {number(rowLocks.length - shown)}
                  </button>
                )}
              </div>
            )}
            {detail === "mdl" && (
              <div aria-labelledby="locks-mdl-h" role="group">
                <span id="locks-mdl-h" className="visually-hidden">
                  Metadata locks
                </span>
                {!report.capabilities.metadata_locks ? (
                  <PrerequisiteGate
                    action={<InstrumentsLink row={report.capabilities.performance_schema ? "group:wait/lock/metadata" : "ps"} />}
                  >
                    Metadata locks are not available on this connection.
                  </PrerequisiteGate>
                ) : mdl.locks.length === 0 ? (
                  <div className="table-empty">No metadata locks held by other sessions.</div>
                ) : (
                  <>
                    {mdl.waits.length > 0 && (
                      <ul className="mdl-waits">
                        {mdl.waits.map((w, i) => (
                          <li key={i}>
                            Connection {w.waiting.connection_id ?? "?"} waits <strong>~{seconds(w.statement_s)}</strong> for{" "}
                            <Mode mode={w.waiting.short} label={w.waiting.lock_type?.toLowerCase().replace(/_/g, " ")} explain={w.waiting.explain} /> on{" "}
                            {(w.waiting.object_type || "").toLowerCase()} {objectName(w.waiting.schema, w.waiting.name)};{" "}
                            {w.blocked_by.length
                              ? w.blocked_by
                                  .map((b) => `${b.reason === "queued" ? "queued behind" : "blocked by"} ${b.lock.short} of connection ${b.lock.connection_id ?? "?"}`)
                                  .join(", ")
                              : "no conflicting lock found"}
                            {!w.certain && " (possible blockers)"}
                          </li>
                        ))}
                      </ul>
                    )}
                    <PriorityTable
                      id="locks.metadata"
                      detailColumns={["duration"]}
                      label="Metadata locks"
                      columns={mdlColumns}
                      rows={mdl.locks.slice(0, PAGE).map((m, i) => ({ ...m, at: i }))}
                      rowKey={(m) => `mdl:${m.at}`}
                      selection={selection}
                      rowClass={(m) => (m.status === "PENDING" ? "waiting" : "")}
                    />
                  </>
                )}
              </div>
            )}
            {detail === "deadlock" && (
              <div aria-labelledby="locks-deadlock-h" role="group">
                <span id="locks-deadlock-h" className="visually-hidden">
                  Latest deadlock
                </span>
                {!deadlock ? (
                  <div className="table-empty">
                    {report.scope === "waits"
                      ? "Not read with Waits only. Clear it and refresh to see the latest deadlock."
                      : report.capabilities.deadlock
                        ? "No deadlock recorded since the server started."
                        : "Not available on this connection (see the notes)."}
                  </div>
                ) : (
                  <GroupedList
                    id="locks-deadlock-list"
                    title={`Detected ${deadlock.detected_at || "at an unknown time"}`}
                    footer={`InnoDB rolled back transaction (${deadlock.victim ?? "?"}) so the other could continue.`}
                    className="deadlock-list"
                  >
                    {deadlock.transactions.map((t) => (
                      <GroupedRow
                        key={t.n}
                        rowKey={`dl:${t.n}`}
                        selection={selection}
                        tone={t.victim ? "problem" : "info"}
                        toneLabel={t.victim ? "Rolled back" : "Continued"}
                        label={`(${t.n}) Transaction ${t.trx_id}`}
                        detail={t.statement ? flat(t.statement) : undefined}
                        value={t.connection_id ? `connection ${t.connection_id}` : ""}
                        className={t.victim ? "victim" : ""}
                      />
                    ))}
                  </GroupedList>
                )}
              </div>
            )}
          </section>
        </>
      )}
      </div>
    </ViewLayout>
  );
}

/** The sessions queued behind a waiting DDL, and how to cancel the DDL. */
function QueuedSection({ d, onCopied }: { d: DdlWait; onCopied: (text: string) => void }) {
  return (
    <InspectorSection title={`${d.queued.length} ${d.queued.length === 1 ? "session" : "sessions"} queued behind it${d.precision === "exact" ? "" : " (likely)"}`}>
      <p className="hint">
        They wait for the DDL, not for the blocker: once a DDL waits for its exclusive lock, new reads and writes of the table
        line up behind it. That is how one idle transaction and one blocked ALTER TABLE can stop an application. Cancelling the
        DDL also releases them, but an ALTER TABLE stopped at its final step loses the work it has done and must run again:
      </p>
      <CopyCommand command={`KILL QUERY ${d.connection_id}`} onCopied={onCopied} />
      <ul className="ddl-queued">
        {d.queued.map((q) => (
          <li key={q.connection_id}>
            <strong>Connection {q.connection_id}</strong>
            {q.user && <span className="hint"> {q.user}@{q.host || "?"}</span>}
            <span className="hint"> · waiting {seconds(q.wait_s)}</span>
            {q.statement && <Sql text={q.statement} />}
          </li>
        ))}
      </ul>
    </InspectorSection>
  );
}

function DeadlockLockLine({ lock, verb }: { lock: DeadlockLock; verb: string }) {
  const key = lock.records.find((r) => !r.supremum);
  const sup = lock.records.some((r) => r.supremum);
  return (
    <li>
      <span className="lock-sql-label">{verb}</span> <Mode mode={lock.mode} label={lock.mode_label} explain={lock.explain} /> on{" "}
      {objectName(lock.schema, lock.table)}
      {lock.index ? ` index ${lock.index}` : ""}
      {key && key.fields[0]?.hex && (
        <span className="hint">
          {" "}
          · first key column hex {key.fields[0].hex}
          {key.as_signed_int !== null ? ` (${key.as_signed_int} if it is a signed integer)` : ""}
        </span>
      )}
      {sup && <span className="hint"> · supremum (end of page)</span>}
    </li>
  );
}

/** The blocking tree as an outline (treegrid): disclosure triangles, one row per session. */
function BlockingTree({
  rows,
  collapsed,
  onToggle,
  selection,
}: {
  rows: TreeRow[];
  collapsed: Set<string>;
  onToggle: (path: string) => void;
  selection: Selection;
}) {
  const focusKey = rows.some((r) => `tree:${r.path}` === selection.key) ? selection.key : `tree:${rows[0]?.path}`;
  // Long lock modes scroll sideways; the frame shades an edge with more beyond it.
  const scroller = useRef<HTMLDivElement>(null);
  const edges = useRef<HTMLDivElement>(null);
  useScrollEdges(scroller, edges);
  return (
    <div ref={edges} className="scroll-edges lock-tree-frame">
    <div ref={scroller} className="data-table-wrap lock-tree" role="region" aria-label="Blocking tree" data-rows>
      <table className="data-table lock-tree-table" role="treegrid" aria-label="Blocking tree">
        {/* Identity and role pills keep their intrinsic width. The outline
            scrolls inside its pane when the inspector takes the remaining room. */}
        <colgroup>
          <col className="lock-col-session" />
          <col className="lock-col-target" />
          <col className="lock-col-wait" />
          <col className="lock-col-statement" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">Session</th>
            <th scope="col">Waits for</th>
            <th scope="col" className="num" title="How long this session has been waiting for the lock">
              Wait
            </th>
            <th scope="col" className="tree-statement">Statement</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ node, path, depth }) => {
            const key = `tree:${path}`;
            const selected = selection.key === key;
            const hasChildren = node.blocked.length > 0;
            const open = hasChildren && !collapsed.has(path);
            const edge = (node.waits_for || [])[0];
            const who = [node.user && `${node.user}@${node.host || "?"}`, node.db].filter(Boolean).join(" · ");
            return (
              <tr
                key={key}
                data-row-key={key}
                data-connection={node.connection_id ?? undefined}
                className={[selected ? "is-selected" : "", node.root ? "lock-node root" : "lock-node"].join(" ")}
                aria-level={depth + 1}
                aria-expanded={hasChildren ? open : undefined}
                aria-selected={selected}
                tabIndex={key === focusKey ? 0 : -1}
                onClick={() => selection.select(key, true)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    selection.select(key, true);
                  } else if (e.key === "ArrowRight" && hasChildren && !open) {
                    e.preventDefault();
                    onToggle(path);
                  } else if (e.key === "ArrowLeft" && hasChildren && open) {
                    e.preventDefault();
                    onToggle(path);
                  } else {
                    const next = moveRowFocus(e);
                    if (next?.dataset.rowKey) selection.select(next.dataset.rowKey, false);
                  }
                }}
              >
                <td title={who || undefined}>
                  <span className="tree-cell" style={{ paddingLeft: depth * 18 }}>
                    {hasChildren ? (
                      <button
                        type="button"
                        className="tree-toggle"
                        aria-label={open ? `Collapse connection ${node.connection_id}` : `Expand connection ${node.connection_id}`}
                        tabIndex={-1}
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggle(path);
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={open ? "is-open" : ""}>
                          <path d="m9 6 6 6-6 6" />
                        </svg>
                      </button>
                    ) : (
                      <span className="tree-spacer" />
                    )}
                    <span className="lock-conn">
                      {node.connection_id ? `Connection ${node.connection_id}` : `Transaction ${node.trx_id} (no session)`}
                    </span>
                    {node.root ? <Pill tone="danger">Blocker</Pill> : edge && <Pill tone="warn">Waiting</Pill>}
                    {node.cycle && <Pill tone="danger">Wait cycle</Pill>}
                    {node.idle_in_transaction && <Pill tone="warn" title={`Idle in transaction ${seconds(node.time_s)}`}>Idle transaction</Pill>}
                    {node.root && node.blocked_count > 0 && <span className="lock-count">blocks {node.blocked_count}</span>}
                  </span>
                </td>
                <td className="cell-flex">
                  {edge ? (
                    <span className="cell-truncate" title={waitText(edge)}>
                      {waitText(edge)}
                    </span>
                  ) : (
                    <span className="cell-muted">—</span>
                  )}
                </td>
                <td className="num">{edge ? <Duration value={edge.wait_s ?? edge.statement_s} approx={edge.wait_s == null && edge.statement_s != null} /> : "—"}</td>
                <td className="tree-statement">
                  {node.query || node.last_statement ? (
                    <code className={`cell-truncate cell-sql${node.query ? "" : " cell-muted"}`} title={node.query || node.last_statement || ""}>
                      {node.query ? flat(node.query) : `Last: ${flat(node.last_statement || "")}`}
                    </code>
                  ) : (
                    <span className="cell-muted">—</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
    </div>
  );
}
