// Server > Locks (locks-1.0 / locks-1.1 from myflames/locks.py): who is
// blocking whom. One summary line, then waiting DDL statements and the
// blocking tree as outlines with disclosure triangles (Finder), and one table
// at a time for transactions, row locks, metadata locks and the latest
// deadlock. The selected session's details, its plan, and Kill are in the
// inspector; Kill opens a confirmation sheet with Cancel as the default.
// It only reads, except a Kill the user confirms.
import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { useOpenRead } from "./viewOnScreen";
import { createPortal } from "react-dom";
import {
  api,
  killLockSession,
  prepareKill,
  type KillReview,
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
import { PrivilegeGate, PrivilegeConfirmLabel, targetPrivilege, useLoginUser, usePrivilege } from "./privileges";
import { InstrumentsLink } from "./Instruments";
import { PriorityTable, CompactNumber } from "./PerfShared";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy, type WritePolicy } from "./Connections";
import {
  AutoRefresh,
  Checkbox,
  ConfirmSheet,
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
    <span ref={label} className="visible-privilege-confirm"><PrivilegeConfirmLabel {...props} /></span>
    {actions && note && createPortal(<span className="privilege-confirm-note" aria-hidden="true">{note}</span>, actions)}
  </>;
}

/** Seconds as a short duration: 45 s, 3 m 05 s, 2 h 10 m. */
export function seconds(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const s = Math.max(0, Math.round(value));
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")} m`;
  if (s >= 60) return `${Math.floor(s / 60)} m ${String(s % 60).padStart(2, "0")} s`;
  return `${s} s`;
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
    ps_thread_id: b.ps_thread_id,
    xa_prepared: b.xa_prepared ?? null,
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

export type Outcome = { id: number; waiting: boolean; blockedBy: number[]; ddl: string | null; likely: boolean };

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
  return ids.map((id) => {
    const blockers = new Set(parents.get(id) || []);
    const entry = ddl.get(id);
    entry?.blockers.forEach((b) => {
      if (b.connection_id) blockers.add(b.connection_id);
    });
    return {
      id,
      waiting: parents.has(id) || !!entry,
      blockedBy: Array.from(blockers).sort((a, b) => a - b),
      ddl: entry?.kind ?? kinds.get(id) ?? null,
      likely: entry?.precision === "likely",
    };
  });
}

function list(ids: number[]) {
  return ids.length > 1 ? `connections ${ids.slice(0, -1).join(", ")} and ${ids[ids.length - 1]}` : `connection ${ids[0]}`;
}

function outcomeText(o: Outcome) {
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
  onCancel,
  onConfirm,
}: {
  target: KillTarget;
  /** What the session blocks (Locks), or nothing when unknown. */
  blocking?: ReactNode;
  busy: boolean;
  onCancel: () => void;
  onConfirm: (mode: "connection" | "query", reviewToken: string) => void;
}) {
  const [mode, setMode] = useState<"connection" | "query">(target.running ? "query" : "connection");
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
  const state = target.idle
    ? `Idle in transaction for ${seconds(target.time_s)}`
    : target.running
      ? `Running a statement for ${seconds(target.time_s)}`
      : `${target.command || "Unknown"} for ${seconds(target.time_s)}`;
  return (
    <ConfirmSheet
      className="kill-dialog"
      eyebrow="KILL"
      title={`End connection ${id}?`}
      description="Check that this is the session you mean. Before sending anything, myflames reads it again and stops if it has changed or ended."
      busy={busy}
      confirmLabel={<VisiblePrivilegeConfirmLabel gate={confirmGate}>{busy ? `Ending connection ${id}…` : mode === "query" ? `Kill query on connection ${id}` : `Kill connection ${id}`}</VisiblePrivilegeConfirmLabel>}
      onCancel={() => {
        declineKill(target, mode);
        onCancel();
      }}
      onConfirm={() => { if (!busy && !confirmGate.disabledReason && review) onConfirm(mode, review.review_token); }}
    >
      {reviewError && <p role="alert">{reviewError}</p>}
      {review && <Sql text={review.statements.join(";\n")} label="Statement to send" />}
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
      {rows !== null && rows >= LARGE_ROLLBACK_ROWS && (
        <p className="kill-warning" role="note">
          It changed about {number(rows)} rows (InnoDB's undo record count, so a row changed twice counts twice). Ending the
          connection rolls them all back, which can take a long time, and it keeps its locks until the rollback finishes, so
          the sessions it blocks keep waiting until then.
        </p>
      )}
      {target.statement ? (
        <Sql text={target.statement} label="Running" />
      ) : target.last_statement ? (
        <Sql text={target.last_statement} label="Last statement" />
      ) : null}
      {blocking && (
        <div className="kill-blocking">
          <span className="lock-sql-label">Blocking</span>
          {blocking}
        </div>
      )}
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
  if (policy) {
    // The type and read-only flag as they are now (another tab or the CLI
    // may have changed them): the dialog was confirmed under the old ones.
    try {
      active = await policy.check();
    } catch (e) {
      return {
        status: "error",
        message: `Could not read the connection's settings, so nothing was sent: ${(e as Error).message}`,
      };
    }
  }
  if (!target.via || connectionKey(active) !== target.via.key)
    return {
      status: "refused",
      refresh: true,
      message:
        target.via && connectionKey({ ...active, type: undefined, read_only: undefined }) === connectionKey({ ...target.via.connection, type: undefined, read_only: undefined })
          ? "The connection's type or read-only setting changed after this dialog opened, so nothing was sent. Check it and try again."
          : "The connection in use changed after this dialog opened, so nothing was sent. Refresh to load this server.",
    };
  try {
    if (!target.server_identity || !("server_uuid" in target.server_identity
      ? target.server_identity.server_uuid
      : target.server_identity.server_id && target.server_identity.hostname && target.server_identity.version))
      return { status: "refused", message: "The report has no server identity, so nothing was sent. Refresh to load this server.", refresh: true };
    const result = await killLockSession(target.via.connection, target.connection_id, mode, {
      user: target.user,
      host: target.host,
      trx_started: target.trx_started,
      ps_thread_id: target.ps_thread_id,
      connection_identity: target.via.key,
      server_identity: target.server_identity,
    }, reviewToken);
    return { status: result.status as string, message: result.message, refresh: result.status === "refused" };
  } catch (e) {
    return { status: "error", message: (e as Error).message };
  }
}

export type KillNote = { status: string; message: string; outcome: Outcome[]; refresh?: boolean };
/** The answer to a Kill, announced and focused. */
export function KillResultNote({ note, onDismiss, noteRef, onRefresh }: { note: KillNote; onDismiss: () => void; noteRef: React.RefObject<HTMLDivElement>; onRefresh?: () => void }) {
  return (
    <div ref={noteRef} tabIndex={-1} className={`kill-result ${note.status}`} role={note.status === "killed" ? "status" : "alert"}>
      <p>
        <strong>
          {note.status === "killed"
            ? "Ended."
            : note.status === "denied"
              ? "The server refused."
              : note.status === "error"
                ? "Could not end it."
                : "Nothing was sent."}
        </strong>{" "}
        {note.message}
      </p>
      {note.outcome.length > 0 && (
        <ul>
          {note.outcome.map((o) => (
            <li key={o.id} className={o.waiting ? "still" : "free"}>
              {outcomeText(o)}
            </li>
          ))}
        </ul>
      )}
      <button type="button" className="text-button" onClick={onDismiss}>
        Dismiss
      </button>
      {note.refresh && onRefresh && <button type="button" className="text-button" onClick={onRefresh}>Refresh</button>}
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
        <details className="kill-manual"><summary>End it yourself</summary>
          <p className="hint">{gate.state === "allowed" ? "Or run it yourself in a SQL client:" : "To end it, run this yourself in a SQL client:"}</p>
          <div className="kill-commands">
            {commands.query && <CopyCommand command={commands.query} onCopied={onCopied} />}
            <CopyCommand command={commands.connection} onCopied={onCopied} />
          </div>
          <p className="hint">{commands.query
            ? "KILL QUERY stops the statement; its transaction stays open with its row and metadata locks unless it runs in autocommit mode. KILL ends the connection and rolls the transaction back."
            : "KILL ends the connection and rolls its open transaction back, which can take a while after many changes."}</p>
        </details>
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
  const [killNote, setKillNote] = useState<KillNote | null>(null);
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
      (c) => api<LocksReport>("live/locks", { connection: c, limit: 500, waits_only: waitsOnly }),
      onReport,
      { force, onError: () => setAuto(0) },
    );
    return fresh ? fresh.report : null;
  }
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
    setKillNote(null);
    setKillTarget({ ...target, via: killVia(connection), server_identity: report?.server_identity ? { ...report.server_identity } : undefined });
  }
  function closeKill() {
    setKillTarget(null);
    window.setTimeout(() => killTrigger.current?.focus(), 0);
  }
  /** One confirmed KILL, then a fresh report to show what changed. */
  async function confirmKill(mode: "connection" | "query", reviewToken: string) {
    if (!killTarget || !report) return;
    const target = killTarget;
    const { ids } = blockingOf(report, target.connection_id);
    const kinds = new Map((report.ddl_waits || []).map((d) => [d.connection_id, d.kind] as const));
    setKilling(true);
    const sent = await sendKill(connection, target, mode, policy, reviewToken);
    const note: KillNote = { ...sent, outcome: [] };
    setKillTarget(null);
    setKilling(false);
    // Re-read and infer the outcome only while the connection in use is still
    // the one the Kill was confirmed for (checked now, after the await); a
    // report of another server says nothing about this Kill.
    const changed = !target.via || reads.currentKey() !== target.via.key;
    const fresh = changed || note.refresh ? null : await load(true);
    if (note.status === "killed" && fresh && ids.length) note.outcome = outcomeOf(fresh, ids, kinds);
    setKillNote(note);
    window.setTimeout(() => killNoteRef.current?.focus(), 0);
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
  const engineName = report ? `${report.engine === "mariadb" ? "MariaDB" : "MySQL"} ${report.server_version}` : "";
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
      inspector = {
        key,
        title: node.connection_id ? `Connection ${node.connection_id}` : `Transaction ${node.trx_id} (no session)`,
        meta: (
          <>
            {node.root && <Pill tone="danger">Root blocker</Pill>}
            {node.cycle && <Pill tone="danger">Wait cycle</Pill>}
            {node.idle_in_transaction && <Pill tone="warn">Idle in transaction {seconds(node.time_s)}</Pill>}
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
                    ["Transaction", node.trx_id],
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
    { key: "conn", label: "Connection", num: true, sort: (t) => t.connection_id, render: (t) => t.connection_id ?? "—" },
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
    { key: "age", label: "Open (s)", num: true, sort: (t) => t.age_s, render: (t) => <CompactNumber value={t.age_s} /> },
    {
      key: "session",
      label: "Session",
      render: (t) => (t.idle_in_transaction ? <Pill tone="warn">Idle {seconds(t.time_s)}</Pill> : t.command || "—"),
    },
    { key: "locked", label: "Rows locked", num: true, sort: (t) => t.rows_locked, render: (t) => <CompactNumber value={t.rows_locked} /> },
    { key: "structs", label: "Lock structs", num: true, sort: (t) => t.lock_structs, render: (t) => <CompactNumber value={t.lock_structs} /> },
    { key: "iso", label: "Isolation", render: (t) => t.isolation_level || "—" },
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
      onRefresh={() => void load()}
      refreshDisabled={disabled || !!loading || killing}
      loading={!!loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a session, a transaction or a lock to see its details, its plan, and Kill.</p>}
      status={<><ViewStatus loading={loading} error={error} />{killNote && <KillResultNote note={killNote} noteRef={killNoteRef} onDismiss={() => setKillNote(null)} onRefresh={() => void load(true)} />}</>}
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

      {killTarget && report && (
        <KillDialog
          target={killTarget}
          blocking={<BlockingList report={report} id={killTarget.connection_id} />}
          busy={killing}
          onCancel={closeKill}
          onConfirm={(mode, token) => void confirmKill(mode, token)}
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
          <SummaryLine tone={tone} toneLabel={tone === "ok" ? "Clear" : "Blocked"} notes={report.notes}>
            {summaryText}
          </SummaryLine>
          <FactStrip
            label="Lock summary"
            facts={[
              ["Blocked sessions", String(s.blocked_sessions)],
              ["Root blockers", String(s.root_blockers)],
              ["Longest wait", seconds(s.longest_wait_s)],
              ["Open transactions", String(s.transactions)],
              ["Oldest transaction", seconds(s.oldest_transaction_s)],
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
            <th scope="col" className="num">
              Wait (s)
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
                <td className="num">{edge ? <span title={edgeSeconds(edge)}>{edge.wait_s == null && edge.statement_s != null ? "~" : ""}<CompactNumber value={edge.wait_s ?? edge.statement_s} /></span> : "—"}</td>
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
