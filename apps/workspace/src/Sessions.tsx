// Server > Sessions (sessions-1.0 from myflames/sessions.py): the process list,
// joined with InnoDB transactions, flagged and summarized. Replaces Running
// queries. A counter strip and one summary line, Active / All, a breakdown by
// state, user, host, database and command (collapsed), and a table of
// sessions; the selected session's statement, transaction, lock wait, Show
// plan and Kill are in the inspector, and Show plan and Kill are also in each
// row's ⋯ menu. Kill asks for confirmation (Cancel is the default) and is
// never offered for server threads.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { useOpenRead } from "./viewOnScreen";
import { api, number, type Connection, type Session, type SessionBreakdown, type SessionFlag, type SessionsReport } from "./api";
import type { ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { PriorityTable, CompactNumber, engineLabel } from "./PerfShared";
import { KillDialog, KillResultNote, killVia, useKillPrivilege, seconds, sendKill, type KillNote, type KillTarget } from "./Locks";
import { MassKillDialog } from "./MassKillDialog";
import { PrivilegeGate, privilegeResult, useLoginUser, usePrivilege, type PrivilegeGateResult } from "./privileges";
import { useWritePolicy } from "./Connections";
import {
  AutoRefresh,
  closePopovers,
  NumberField,
  Popover,
  Disclosure,
  EmptyState,
  FactStrip,
  Fields,
  InspectorSection,
  PrerequisiteGate,
  SearchField,
  Segmented,
  StatusDot,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
  type Tone,
} from "./kit";
import "./sessions.css";
import { Icon } from "./components";

const AUTO_INTERVALS = [10, 30, 60];
const FLAGS: Record<SessionFlag, [string, "warn" | "danger" | "neutral" | "info"]> = {
  long_running: ["Long running", "warn"],
  idle_in_trx: ["Idle in transaction", "warn"],
  lock_wait: ["Lock wait", "danger"],
  system_thread: ["Server thread", "neutral"],
  own_connection: ["myflames", "info"],
};

/** What a lock wait waits for, by sessions-1.0 lock_wait.kind. */
const LOCK_KIND: Record<string, string> = {
  innodb: "an InnoDB lock (a row lock, or a table lock such as AUTO-INC)",
  metadata: "a metadata lock",
  table: "a table-level lock",
};

function tone(status: string | null | undefined): Tone {
  return status === "ok" || status === "watch" || status === "problem" || status === "info" ? status : "unknown";
}

/** The flags of a session as words ("Long running, Lock wait"). */
function flagText(flags: SessionFlag[]) {
  return flags.map((f) => (FLAGS[f] || [f.replace(/_/g, " ")])[0]).join(", ");
}

function lockStatus(s: Session): "waiting" | "holding" | null {
  if (s.lock_status === "waiting" || s.lock_status === "holding" || s.lock_status === null) return s.lock_status;
  if (s.lock_wait) return "waiting";
  if ((s.trx?.rows_locked ?? 0) > 0 || (s.trx?.tables_locked ?? 0) > 0) return "holding";
  return null;
}

function isLocked(s: Session): boolean {
  return s.is_locked ?? (lockStatus(s) !== null || s.trx?.state === "LOCK WAIT");
}

function Breakdown({ title, rows }: { title: string; rows: SessionBreakdown }) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <section className="breakdown" aria-label={`Sessions by ${title.toLowerCase()}`}>
      <h4>{title}</h4>
      <ul>
        {rows.map((r, i) => (
          <li key={`${r.key}-${i}`}>
            <span className="breakdown-label" title={r.key || undefined}>
              {r.other ? "Other" : r.key === null ? <span className="cell-muted">none</span> : r.key}
            </span>
            <span className="breakdown-bar" aria-hidden="true">
              <span style={{ width: `${(r.count / max) * 100}%` }} />
            </span>
            <span className="breakdown-count">{r.count}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function killTarget(s: Session): KillTarget {
  return {
    connection_id: s.id,
    user: s.user,
    host: s.host,
    db: s.db,
    command: s.command,
    time_s: s.time_s,
    statement: s.info,
    last_statement: s.last_statement,
    running: s.command !== "Sleep" && !!s.info,
    idle: s.flags.includes("idle_in_trx"),
    trx_age_s: s.trx?.age_s ?? null,
    trx_started: s.trx?.started ?? null,
    rows_modified: s.trx?.rows_modified ?? null,
    ps_thread_id: null,
    xa_prepared: null,
  };
}

export function SessionsView({
  connection,
  disabled,
  onBack,
  selection,
  report: bound,
  onReport,
  scope,
  onScopeChange,
  minSeconds,
  onMinSecondsChange,
  onShowPlan,
  onOpenView,
  onOpenSql,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<SessionsReport> | null;
  onReport: (report: Bound<SessionsReport>) => void;
  scope: "active" | "all" | "locked";
  onScopeChange: (scope: "active" | "all" | "locked") => void;
  minSeconds: number;
  onMinSecondsChange: (seconds: number) => void;
  /** Opens the plan of a connection's statement, read on *from*. */
  onShowPlan: (id: number, from: Connection) => Promise<void>;
  onOpenView: (view: ServerKind) => void;
  /** Put a statement in the Query editor's current tab, without running it. */
  onOpenSql?: (sql: string) => void;
}) {
  const [sourceMode, setSourceMode] = useState<"auto" | "show_processlist">("show_processlist");
  const [massKillOpen, setMassKillOpen] = useState(false);
  const [planLoading, setPlanLoading] = useState("");
  const [planError, setPlanError] = useState("");
  const [auto, setAuto] = useState(0);
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState<KillTarget | null>(null);
  // The KILL a row menu chose (Kill query… or Kill connection…); the sheet
  // can still switch it.
  const [targetMode, setTargetMode] = useState<"connection" | "query" | undefined>(undefined);
  const [killing, setKilling] = useState(false);
  const [note, setNote] = useState<KillNote | null>(null);
  const reads = useConnectionReads(connection);
  const policy = useWritePolicy();
  const report = bound?.report ?? null;
  // A report read from another connection stays on screen, greyed, and none
  // of its actions run until it is refreshed on the connection in use.
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const loading = reads.loading || planLoading;
  const error = reads.error || planError;
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const noteRef = useRef<HTMLDivElement>(null);
  const busy = disabled || !!loading || killing;
  const actionsOff = busy || !!other;

  /** Read the sessions; a newer scope or filter replaces a read still running. */
  async function load(
    activeOnly = scope === "active",
    force = false,
    source = sourceMode,
    lockedOnly = scope === "locked",
  ) {
    setPlanError("");
    return reads.run(
      `sessions:${activeOnly}:${minSeconds}:${source}:${lockedOnly}`,
      "Reading the sessions…",
      (c, signal) =>
        api<SessionsReport>("live/sessions", {
          connection: c,
          active_only: activeOnly,
          min_seconds: minSeconds,
          source_mode: source,
          locked_only: lockedOnly,
        }, signal),
      onReport,
      { force, onError: () => setAuto(0) },
    );
  }

  const loadRef = useRef(load);
  loadRef.current = load;

  // A bulk kill sheet holds the sessions and review of the connection it was
  // opened on: close it when the connection in use changes.
  useEffect(() => { setMassKillOpen(false); }, [reads.key]);

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!loading || reads.running(), read: () => { void load(); } });

  const running = reads.running;
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => {
      const view = root.current?.closest<HTMLElement>(".server-view");
      const visible = !!view && view.offsetParent !== null;
      if (!document.hidden && visible && !disabled && !running() && !target) loadRef.current();
    }, auto * 1000);
    return () => window.clearInterval(timer);
  }, [auto, disabled, target, running]);

  async function showPlan(id: number) {
    if (!bound || other) return;
    const from = connection;
    setPlanError("");
    reads.clearError();
    setPlanLoading(`Reading the plan of connection ${id}…`);
    try {
      await onShowPlan(id, from);
    } catch (e) {
      setPlanError((e as Error).message);
    } finally {
      setPlanLoading("");
    }
  }
  async function confirmKill(mode: "connection" | "query", reviewToken: string) {
    if (!target) return;
    setKilling(true);
    const sent = await sendKill(connection, target, mode, policy, reviewToken);
    setTarget(null);
    setKilling(false);
    // The answer is shown at once (and focused). The list is read again only
    // while the connection in use is still the one the Kill was confirmed for
    // (checked now, after the await, not with this render's connection);
    // after a switch there is nothing of that server to show.
    const reread = !!target.via && reads.currentKey() === target.via.key && !sent.refresh;
    const shown: KillNote = { ...sent, mode, outcome: [] };
    setNote({ ...shown, refreshing: reread });
    window.setTimeout(() => noteRef.current?.focus(), 0);
    if (!reread) return;
    await load(scope === "active", true);
    setNote((n) => (n && n.status === shown.status && n.message === shown.message ? shown : n));
  }

  /** Open the Kill sheet for *x*; focus returns to *opener* when it closes.
   * *mode* preselects the KILL (a row menu's Kill query… or Kill connection…). */
  function openKill(x: Session, opener: HTMLElement | null, mode?: "connection" | "query") {
    if (other) return;
    trigger.current = opener;
    setNote(null);
    setTargetMode(mode);
    setTarget({ ...killTarget(x), via: killVia(connection), server_identity: report?.server_identity ? { ...report.server_identity } : undefined });
  }
  // The generic Kill privilege answers (own sessions need none), for the row
  // menu, which cannot call a hook per row; the sheet checks its own target.
  const killOtherGate = usePrivilege("kill");
  const killOwnGate = usePrivilege("kill_own");
  const killLogin = useLoginUser(connection.user);
  function rowKillGate(x: Session): PrivilegeGateResult {
    return x.user && x.user === killLogin ? killOwnGate : killOtherGate;
  }
  /** Why the row menu's Kill query… or Kill connection… is off, or "". */
  function rowKillReason(x: Session, mode: "connection" | "query") {
    if (other) return "These sessions are from another connection: refresh to load this server.";
    const blocked = killBlocked(x);
    if (blocked) return blocked;
    if (disabled) return "The Query editor is capturing; Kill waits until it ends.";
    if (killing) return "A Kill is being sent.";
    if (loading) return "The list is being read.";
    const denied = rowKillGate(x).disabledReason;
    if (denied) return denied;
    if (mode === "query" && !(x.command !== "Sleep" && !!x.info))
      return `Connection ${x.id} runs no statement now; Kill connection… ends it.`;
    return "";
  }
  /** Why Kill is off for *x* (empty when it is offered). */
  function killBlocked(x: Session) {
    const system = x.thread_kind !== "client";
    const own = x.flags.includes("own_connection");
    return policy?.blocked || (connection.read_only ? "This connection is read-only." : "") ||
      (system ? "A server thread, not a client session: myflames does not end it." : own ? "Another myflames tool is running it." : "");
  }
  /** The row menu (the trailing ⋯ column): Show plan and Kill, which used to
   * be buttons in the row. Kill opens the same confirmation sheet as the
   * inspector's, after the menu has closed, and Cancel returns focus to ⋯. */
  function rowMenu(v: Session) {
    const blocked = killBlocked(v);
    return (
      <span className="session-row-menu-anchor" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
        <Popover
          menu
          label={`Actions for connection ${v.id}`}
          buttonTitle={`Actions for connection ${v.id}`}
          buttonClassName="icon-button row-more"
          disabled={actionsOff}
          trigger={<Icon name="more" />}
        >
          <div className="session-row-menu">
            {v.explainable && (
              <button type="button" role="menuitem" disabled={actionsOff} onClick={() => { closePopovers(); void showPlan(v.id); }}>
                Show plan
              </button>
            )}
            <button
              type="button"
              role="menuitem"
              className="is-danger"
              disabled={actionsOff || !!blocked}
              title={blocked || `Kill connection ${v.id} or its query; asks first`}
              onClick={(e) => {
                const opener = e.currentTarget.closest(".popover-anchor")?.querySelector<HTMLElement>("button") || null;
                const chosenOn = reads.key;
                closePopovers();
                // After the menu has closed and handed focus back to ⋯, and
                // only while the connection in use is still the one the row
                // was read from (a switch in between opens nothing).
                window.setTimeout(() => {
                  if (reads.currentKey() === chosenOn) openKill(v, opener);
                }, 0);
              }}
            >
              Kill…
            </button>
          </div>
        </Popover>
      </span>
    );
  }

  const s = report?.summary;
  const rawDeferredSearch = useDeferredValue(search);
  const deferredSearch = search === "" ? "" : rawDeferredSearch;
  const needle = deferredSearch.trim().toLowerCase();
  // Filtered once per report and search, not on every render (a selection in
  // thousands of sessions).
  const rows = useMemo(() => {
    const all = report?.sessions || [];
    if (!needle) return all;
    return all.filter(
      (x) =>
        [String(x.id), x.user, x.host, x.db, x.command, x.state, x.info_short, x.last_statement]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(needle),
    );
  }, [report, needle]);
  const noProcess = report?.capabilities.process_privilege === false;

  const columns: Column<Session>[] = [
    {
      key: "status",
      label: <span className="visually-hidden">Status</span>,
      sort: (x) => ({ problem: 0, watch: 1 } as Record<string, number>)[x.status] ?? 2,
      // The Flags column starts off: a flagged status names its flags on
      // hover, and an OK row with flags (a server thread, myflames' own
      // connection) shows them as quiet words. A fixed width keeps a long
      // word from widening the list; it truncates with the full text on hover.
      width: 88,
      render: (x) =>
        x.status === "problem" || x.status === "watch" ? (
          <span title={flagText(x.flags) || undefined}>
            <StatusDot tone={tone(x.status)} label={x.status === "watch" ? "Watch" : "Problem"} />
          </span>
        ) : x.flags.length ? (
          <span className="cell-truncate cell-muted" title={flagText(x.flags)}>{flagText(x.flags)}</span>
        ) : null,
      className: "cell-dot",
    },
    {
      key: "id",
      label: "ID",
      title: "Connection ID (the processlist ID that KILL takes)",
      num: true,
      width: 96,
      sort: (x) => x.id,
      render: (x) => {
        const ls = lockStatus(x);
        return (
          <>
            {x.id}
            {(ls === "waiting" || ls === "holding") && (
              <span
                className="session-lock-marker"
                role="img"
                aria-label={ls === "waiting" ? "Waiting for a lock" : "Holding locks"}
                title={ls === "waiting" ? "Waiting for a lock" : "Holding locks"}
              >
                <Icon name={ls === "waiting" ? "lock" : "key"} size={12} />
              </span>
            )}
          </>
        );
      },
    },
    {
      key: "user",
      label: "User@host",
      sort: (x) => `${x.user || ""}@${x.client_host || ""}`,
      render: (x) =>
        x.user || x.client_host ? (
          <>
            {x.user || "?"}
            <span className="cell-muted">@{x.client_host || "?"}</span>
          </>
        ) : (
          <span className="cell-muted">—</span>
        ),
    },
    { key: "db", label: "Database", sort: (x) => x.db || "", render: (x) => x.db || <span className="cell-muted">—</span> },
    { key: "command", label: "Command", sort: (x) => x.command || "", render: (x) => x.command || "—" },
    { key: "time", label: "Time", name: "Time (s)", title: "Time in the current state, in seconds", num: true, width: 112, sort: (x) => x.time_s, render: (x) => <CompactNumber value={x.time_s} /> },
    {
      key: "statement",
      label: "Statement",
      flex: true,
      minWidth: 220,
      sort: (x) => x.info_short || x.last_statement || "",
      render: (x) =>
        x.info_short ? (
          <code className="cell-truncate cell-sql" title={x.info || x.info_short}>
            {x.info_short}
          </code>
        ) : x.last_statement ? (
          <code className="cell-truncate cell-sql cell-muted" title={x.last_statement}>
            Last: {x.last_statement}
          </code>
        ) : x.state ? (
          <span className="cell-muted">{x.state}</span>
        ) : (
          <span className="cell-muted">—</span>
        ),
    },
    {
      key: "lock",
      label: "Lock / Trx",
      width: 140,
      sort: (x) => {
        const ls = lockStatus(x);
        return ls === "waiting" ? 0 : ls === "holding" ? 1 : 2;
      },
      render: (x) => {
        const ls = lockStatus(x);
        if (ls === "waiting") {
          return (
            <span className="pill is-danger" title={x.lock_detail || "Waiting on lock"}>
              <Icon name="lock" size={12} /> Wait {x.lock_wait?.wait_s != null ? `(${x.lock_wait.wait_s}s)` : ""}
            </span>
          );
        }
        if (ls === "holding") {
          return (
            <span className="pill is-warn" title={x.lock_detail || "Holding locks"}>
              <Icon name="key" size={12} /> Locks ({x.trx?.rows_locked ?? 0} rows)
            </span>
          );
        }
        if (x.trx) {
          return <span className="cell-muted" title={`Trx ${x.trx.id}: ${x.trx.state || "active"}`}>Trx {x.trx.id}</span>;
        }
        return <span className="cell-muted">—</span>;
      },
    },
    {
      key: "flags",
      label: "Flags",
      sort: (x) => x.flags.length,
      render: (x) =>
        x.flags.length ? (
          <ul className="pill-list" aria-label="Flags">
            {x.flags.slice(0, 3).map((f) => (
              <li key={f} className={`pill is-${(FLAGS[f] || [f, "neutral"])[1]}`}>
                {(FLAGS[f] || [f.replace(/_/g, " ")])[0]}
              </li>
            ))}
          </ul>
        ) : null,
    },
  ];

  let inspector: InspectorContent | null = null;
  const x = report?.sessions.find((v) => String(v.id) === selection.key);
  const killGate = useKillPrivilege(x ? killTarget(x) : { user: null }, connection.user);
  const explainGeneric = usePrivilege("explain_connection");
  // Own-user EXPLAIN needs no additional privilege. Other targets remain
  // governed by the backend read hint (grey_out:false keeps reads enabled).
  const loginUser = useLoginUser(connection.user);
  const explainGate = !!x?.user && x.user === loginUser ? privilegeResult({ state: "allowed", missing: [], reason: "No additional privilege needed for this user's session." }) : explainGeneric;
  if (report && x) {
    const system = x.thread_kind !== "client";
    const own = x.flags.includes("own_connection");
    inspector = {
      key: selection.key,
      title: `Connection ${x.id}`,
      meta: (
        <>
          {x.status !== "ok" && <StatusPill tone={tone(x.status)} />}
          <span>{x.user || "?"}@{x.client_host || "?"}</span>
        </>
      ),
      actions: (
        <>
          {x.explainable && (
            <PrivilegeGate action="explain_connection" gate={explainGate} write={false}
              disabled={actionsOff} aria-label={`Show plan for connection ${x.id}`}
              onClick={() => void showPlan(x.id)}>Show plan</PrivilegeGate>
          )}
          <PrivilegeGate action="kill" gate={killGate} className="danger"
            disabled={actionsOff || system || own}
            aria-label={`Kill: connection ${x.id}`}
            blocked={killBlocked(x)}
            onClick={(e) => openKill(x, e.currentTarget)}>Kill</PrivilegeGate>
        </>
      ),
      body: (
        <>
          {x.why && x.status !== "ok" && (
            <InspectorSection title="Why it is flagged">
              <p>{x.why}</p>
            </InspectorSection>
          )}
          {x.lock_wait && (
            <InspectorSection title="Waiting">
              <p className="session-waiting">
                Waiting {seconds(x.lock_wait.wait_s)} for {LOCK_KIND[x.lock_wait.kind] || "a lock"}
                {x.lock_wait.detail ? ` (${x.lock_wait.detail})` : ""}.
              </p>
              <button type="button" className="text-button" onClick={() => onOpenView("locks")}>
                Open Locks to see who blocks it
              </button>
            </InspectorSection>
          )}
          {x.info ? (
            <InspectorSection title="Statement">
              <pre className="inspector-sql">
                <code>{x.info}</code>
              </pre>
              {x.info_truncated && <p className="hint">The server keeps only the start of long statements.</p>}
              {!x.explainable && (
                <p className="hint">Only text-protocol statements can be explained while running (not prepared statements or idle commands).</p>
              )}
            </InspectorSection>
          ) : x.last_statement ? (
            <InspectorSection title="Last statement">
              <pre className="inspector-sql">
                <code>{x.last_statement}</code>
              </pre>
            </InspectorSection>
          ) : null}
          <InspectorSection title="Session">
            <Fields
              rows={[
                ["Command", x.command || "—"],
                ["State", x.state || "—"],
                ["Time in state", seconds(x.time_s)],
                ["Database", x.db || "—"],
                ["Host", x.host || "—"],
                ["Thread", x.thread_kind === "client" ? "Client" : x.thread_kind === "daemon" ? "Server (daemon)" : "Replication"],
                x.progress && ["Progress", `stage ${x.progress.stage} of ${x.progress.max_stage}, ${x.progress.pct.toFixed(1)}%`],
                x.rows_examined !== null && ["Rows examined", number(x.rows_examined)],
                x.memory_used !== null && ["Memory", `${number(x.memory_used)} B`],
              ]}
            />
          </InspectorSection>
          {x.trx && (
            <InspectorSection title="Transaction">
              <Fields
                rows={[
                  ["Transaction", x.trx.id],
                  ["State", x.trx.state || "—"],
                  ["Started", x.trx.started || "—"],
                  ["Open for", seconds(x.trx.age_s)],
                  x.trx.wait_s !== null && ["Lock wait", seconds(x.trx.wait_s)],
                  ["Rows locked", x.trx.rows_locked === null ? "—" : number(x.trx.rows_locked)],
                  ["Rows changed", x.trx.rows_modified === null ? "—" : number(x.trx.rows_modified)],
                  ["Lock structs", x.trx.lock_structs === null ? "—" : number(x.trx.lock_structs)],
                  ["Tables locked", x.trx.tables_locked === null ? "—" : number(x.trx.tables_locked)],
                  ["Isolation", x.trx.isolation_level || "—"],
                  x.trx.read_only !== null && ["Read only", x.trx.read_only ? "Yes" : "No"],
                  x.trx.operation_state && ["Doing", x.trx.operation_state],
                ]}
              />
            </InspectorSection>
          )}
          {x.flags.length > 0 && (
            <InspectorSection title="Rules">
              <ul className="session-rules">
                {x.flags
                  .filter((f) => report.rules[f])
                  .map((f) => (
                    <li key={f}>
                      <strong>{FLAGS[f]?.[0] || f}:</strong> {report.rules[f]}
                    </li>
                  ))}
              </ul>
            </InspectorSection>
          )}
        </>
      ),
    };
  }

  const summaryParts = s
    ? [
        `${s.active} active`,
        s.locked ? `${s.locked} locked` : "",
        s.idle_in_trx ? `${s.idle_in_trx} idle in transaction` : "",
        s.lock_wait.total ? `${s.lock_wait.total} waiting for a lock` : "",
        s.long_running.watch + s.long_running.problem
          ? `${s.long_running.watch + s.long_running.problem} running ${report!.thresholds.long_running_s[0]} s or more`
          : "",
        s.oldest_transaction?.age_s != null ? `oldest transaction ${seconds(s.oldest_transaction.age_s)}` : "",
      ].filter(Boolean)
    : [];
  const c = s?.connections;
  const hll = s?.history_list_length;

  return (
    <ViewLayout
      id="sessions"
      title="Processlist"
      className="sessions-panel"
      onBack={onBack}
      info={
        <>
          <p>
            Every connection to the server, from the process list joined with InnoDB transactions: what each runs, how
            long, idle transactions, and lock waits. Active hides sleeping sessions without a transaction.
          </p>
          <p>Statuses are rules of thumb. Select a session for its statement, transaction, plan, and Kill (it asks first); a row's ⋯ menu also has Show plan and Kill, and right click (or Shift+F10) on a row has Kill query… and Kill connection….</p>
        </>
      }
      controls={
        <>
          <button
            type="button"
            className="button"
            disabled={busy || !report || !report.sessions.length}
            data-action="kill-sessions"
            aria-haspopup="dialog"
            onClick={() => setMassKillOpen(true)}
          >
            Kill sessions…
          </button>
          <AutoRefresh value={auto} options={AUTO_INTERVALS} disabled={!report} onChange={setAuto} />
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={busy}
      loading={!!loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a session to see its statement, its transaction, Show plan and Kill.</p>}
      status={<><ViewStatus loading={loading} error={error} />{note && <KillResultNote note={note} noteRef={noteRef} onDismiss={() => setNote(null)} onRefresh={() => void load(scope === "active", true)} />}</>}
    >
      <div ref={root}>

        {target && (
          <KillDialog
            target={target}
            busy={killing}
            initialMode={targetMode}
            onCancel={() => {
              setTarget(null);
              window.setTimeout(() => trigger.current?.focus(), 0);
            }}
            onConfirm={(mode, token) => void confirmKill(mode, token)}
          />
        )}
        {massKillOpen && report && (
          <MassKillDialog
            sessions={report.sessions}
            connection={connection}
            serverIdentity={report.server_identity}
            onClose={() => setMassKillOpen(false)}
            onSuccess={(n) => {
              setMassKillOpen(false);
              setNote(n);
              void load(scope === "active", true);
            }}
          />
        )}
        {!report && !loading && !error && (
          <EmptyState
            icon="sessions"
            title="No sessions loaded"
            action={
              <button type="button" className="button primary" disabled={busy} onClick={() => void load()}>
                Load sessions
              </button>
            }
          >
            Every connection, what it runs, idle transactions and lock waits. It only reads.
          </EmptyState>
        )}
        {report && s && (
          <>
            {noProcess && (
              <PrerequisiteGate>
                Without the PROCESS privilege only this account's own sessions are listed, with no transactions.
              </PrerequisiteGate>
            )}
            <SummaryLine tone={tone(s.status)} notes={report.notes}>
              {summaryParts.join(" · ")}
            </SummaryLine>
            <FactStrip
              label="Connections"
              facts={[
                ["Connected", c?.current != null ? number(c.current) : "—"],
                ["Limit", c?.max != null ? number(c.max) : "—"],
                ["Max seen", c?.max_used != null ? number(c.max_used) : "—"],
                ["Running", c?.running != null ? number(c.running) : "—"],
                ...(hll && hll.value !== null ? ([["History list", number(hll.value)]] as [string, string][]) : []),
                ["Server", engineLabel(report)],
                ...(report.ps_show_processlist != null
                  ? ([["P_S processlist", report.ps_show_processlist]] as [string, string][])
                  : []),
              ]}
            />
            <Disclosure title="Breakdown" className="sessions-breakdown">
              <div className="breakdown-grid">
                <Breakdown title="State" rows={s.by_state} />
                <Breakdown title="User" rows={s.by_user} />
                <Breakdown title="Host" rows={s.by_host} />
                <Breakdown title="Database" rows={s.by_db} />
                <Breakdown title="Command" rows={s.by_command} />
              </div>
            </Disclosure>
            <section className="view-section" aria-label="Sessions">
            {/* The list's filters sit on the row above it, like Indexes and
                Tables, so the view toolbar keeps one row at 1440 px. */}
            <div className="report-table-tools sessions-tools" role="group" aria-label="Session filters">
              <Segmented
                label="Sessions to list"
                value={scope}
                onChange={(v) => {
                  const next = v as "active" | "all" | "locked";
                  onScopeChange(next);
                  // Latest wins: a read still running for the other scope is replaced.
                  if (report) void load(next === "active", false, sourceMode, next === "locked");
                }}
                options={[
                  { value: "active", label: "Active" },
                  { value: "all", label: "All" },
                  { value: "locked", label: s?.locked ? `Locked (${s.locked})` : "Locked" },
                ]}
              />
              <label className="toolbar-field">
                <span>Source</span>
                <select
                  className="toolbar-select"
                  aria-label="Processlist source"
                  value={sourceMode}
                  onChange={(e) => {
                    const mode = e.target.value as "auto" | "show_processlist";
                    setSourceMode(mode);
                    if (report) void load(scope === "active", true, mode, scope === "locked");
                  }}
                >
                  <option value="auto">Auto (P_S / I_S)</option>
                  <option value="show_processlist">SHOW PROCESSLIST</option>
                </select>
              </label>
              <label className="toolbar-field">
                <span>Running at least</span>
                <NumberField
                  min={0}
                  step={1}
                  unit="s"
                  label="Running at least (seconds)"
                  value={minSeconds}
                  onChange={(v) => onMinSecondsChange(Math.max(0, Math.round(+v) || 0))}
                />
              </label>
              <SearchField label="Filter sessions" value={search} onChange={setSearch} placeholder="Filter" />
            </div>
              <PriorityTable
                id="sessions.sessions"
                detailColumns={["user", "db", "command", "statement", "lock", "flags"]}
                // Flags start off so the list fits beside the list of views at
                // 1440 px: the Status dot names them on hover, and the details
                // explain each one (Why it is flagged, Rules).
                defaultHidden={["flags"]}
                rowAction={rowMenu}
                label="Sessions"
                columns={columns}
                rows={rows}
                rowKey={(v) => String(v.id)}
                selection={selection}
                rowAttrs={(v) => ({
                  "data-session": String(v.id),
                  "data-status": v.status,
                  "data-locked": isLocked(v) ? "true" : "false",
                  "data-lock-wait": lockStatus(v) === "waiting" ? "true" : "false",
                })}
                // A class from rowAttrs would replace the selection class; rowClass joins it.
                rowClass={(v) => {
                  const ls = lockStatus(v);
                  return ls === "waiting" ? "row-lock-wait" : ls === "holding" ? "row-lock-holding" : "";
                }}
                rowContextMenu={(v, { copy, close }) => {
                  // Right click, Shift+F10 or the context menu key on a row:
                  // the same actions as the inspector and the ⋯ menu, with
                  // Kill query… and Kill connection… for this row last. Each
                  // opens the single Kill sheet (exact statement, identity
                  // check, write policy, audit); an action that is off says why.
                  const statement = v.info || v.last_statement || "";
                  const opener = () => root.current?.querySelector<HTMLElement>(`tr[data-session="${v.id}"]`) || null;
                  const kill = (mode: "connection" | "query") => {
                    const reason = rowKillReason(v, mode);
                    const chosenOn = reads.key;
                    return {
                      label: mode === "query" ? "Kill query…" : "Kill connection…",
                      icon: mode === "query" ? "stop" : "block",
                      danger: true,
                      disabled: !!reason,
                      hint: reason || undefined,
                      onClick: () => {
                        close();
                        // After the menu has closed and handed focus back to
                        // the row, and only while the connection in use is
                        // still the one the row was read from.
                        window.setTimeout(() => {
                          if (reads.currentKey() === chosenOn) openKill(v, opener(), mode);
                        }, 0);
                      },
                    };
                  };
                  return [
                    ...(v.explainable
                      ? [{ label: "Show plan", icon: "plan", disabled: actionsOff, onClick: () => { close(); void showPlan(v.id); } }]
                      : []),
                    ...(statement && onOpenSql
                      ? [{ label: "Open in Query editor", icon: "query", onClick: () => { close(); onOpenSql(statement); } }]
                      : []),
                    ...(isLocked(v) || v.trx
                      ? [{ label: "Open in Locks", icon: "lock", onClick: () => { close(); onOpenView("locks"); } }]
                      : []),
                    ...(v.user ? [{ label: `Filter by user ${v.user}`, icon: "users", onClick: () => { close(); setSearch(v.user || ""); } }] : []),
                    ...(v.db ? [{ label: `Filter by database ${v.db}`, icon: "database", onClick: () => { close(); setSearch(v.db || ""); } }] : []),
                    ...(v.client_host ? [{ label: `Filter by host ${v.client_host}`, icon: "globe", onClick: () => { close(); setSearch(v.client_host || ""); } }] : []),
                    ...(v.command ? [{ label: `Filter by command ${v.command}`, icon: "run", onClick: () => { close(); setSearch(v.command || ""); } }] : []),
                    { divider: true as const },
                    ...(statement
                      ? [{ label: "Copy query", icon: "copy", onClick: () => void copy(statement, "Query copied") }]
                      : []),
                    { label: "Copy connection ID", icon: "copy", onClick: () => void copy(String(v.id), "Connection ID copied") },
                    ...(v.thread_kind === "client" ? [{ divider: true as const }, kill("query"), kill("connection")] : []),
                  ];
                }}
                empty={
                  needle
                    ? `No session matches “${search.trim()}”.`
                    : minSeconds
                      ? `No session has been in its state for ${minSeconds} seconds or longer.`
                      : scope === "active"
                        ? "No active sessions right now."
                        : scope === "locked"
                          ? "No locked transactions right now."
                          : "No sessions."
                }
              />
              {report.truncated && (
                <p className="hint table-more">
                  Showing {report.sessions.length} of {report.matched}.
                </p>
              )}
            </section>
          </>
        )}
      </div>
    </ViewLayout>
  );
}
