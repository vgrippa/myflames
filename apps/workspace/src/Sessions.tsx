// Server > Sessions (sessions-1.0 from myflames/sessions.py): the process list,
// joined with InnoDB transactions, flagged and summarized. Replaces Running
// queries. A counter strip and one summary line, Active / All, a breakdown by
// state, user, host, database and command (collapsed), and a table of
// sessions; the selected session's statement, transaction, lock wait, Show
// plan and Kill are in the inspector. Kill asks for confirmation (Cancel is
// the default) and is never offered for server threads.
import { useEffect, useRef, useState } from "react";
import { api, number, type Connection, type Session, type SessionBreakdown, type SessionFlag, type SessionsReport } from "./api";
import type { ServerKind } from "./session";
import { useConnectionReads, type Bound } from "./binding";
import { PriorityTable, CompactNumber, engineLabel } from "./PerfShared";
import { KillDialog, KillResultNote, killVia, useKillPrivilege, seconds, sendKill, type KillNote, type KillTarget } from "./Locks";
import { PrivilegeGate, privilegeResult, useLoginUser, usePrivilege } from "./privileges";
import { useWritePolicy } from "./Connections";
import {
  AutoRefresh,
  NumberField,
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
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<SessionsReport> | null;
  onReport: (report: Bound<SessionsReport>) => void;
  scope: "active" | "all";
  onScopeChange: (scope: "active" | "all") => void;
  minSeconds: number;
  onMinSecondsChange: (seconds: number) => void;
  /** Opens the plan of a connection's statement, read on *from*. */
  onShowPlan: (id: number, from: Connection) => Promise<void>;
  onOpenView: (view: ServerKind) => void;
}) {
  const [planLoading, setPlanLoading] = useState("");
  const [planError, setPlanError] = useState("");
  const [auto, setAuto] = useState(0);
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState<KillTarget | null>(null);
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
  async function load(activeOnly = scope === "active", force = false) {
    setPlanError("");
    return reads.run(
      `sessions:${activeOnly}:${minSeconds}`,
      "Reading the sessions…",
      (c) => api<SessionsReport>("live/sessions", { connection: c, active_only: activeOnly, min_seconds: minSeconds }),
      onReport,
      { force, onError: () => setAuto(0) },
    );
  }
  const loadRef = useRef(load);
  loadRef.current = load;
  const running = reads.running;
  useEffect(() => {
    if (!auto) return;
    const timer = window.setInterval(() => {
      const visible = !!root.current && root.current.offsetParent !== null;
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
    // Re-read only while the connection in use is still the one the Kill was
    // confirmed for (checked now, after the await, not with this render's
    // connection); after a switch there is nothing of that server to show.
    if (target.via && reads.currentKey() === target.via.key && !sent.refresh) await load(scope === "active", true);
    setNote({ ...sent, outcome: [] });
    window.setTimeout(() => noteRef.current?.focus(), 0);
  }

  const s = report?.summary;
  const needle = search.trim().toLowerCase();
  const rows = (report?.sessions || []).filter(
    (x) =>
      !needle ||
      [String(x.id), x.user, x.host, x.db, x.command, x.state, x.info_short, x.last_statement]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(needle),
  );
  const noProcess = report?.capabilities.process_privilege === false;

  const columns: Column<Session>[] = [
    {
      key: "status",
      label: <span className="visually-hidden">Status</span>,
      sort: (x) => ({ problem: 0, watch: 1 } as Record<string, number>)[x.status] ?? 2,
      render: (x) => <StatusDot tone={tone(x.status)} label={x.status === "ok" ? "OK" : x.status === "watch" ? "Watch" : x.status === "problem" ? "Problem" : "No threshold"} />,
      className: "cell-dot",
    },
    { key: "id", label: "ID", num: true, width: 96, sort: (x) => x.id, render: (x) => x.id },
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
    { key: "time", label: "Time (s)", num: true, width: 112, sort: (x) => x.time_s, render: (x) => <CompactNumber value={x.time_s} /> },
    {
      key: "statement",
      label: "Statement",
      flex: true,
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
          <StatusPill tone={tone(x.status)} />
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
            blocked={policy?.blocked || (connection.read_only ? "This connection is read-only." : "") ||
              (system ? "A server thread, not a client session: myflames does not end it." : own ? "Another myflames tool is running it." : "")}
            onClick={(e) => {
              if (other) return;
              trigger.current = e.currentTarget;
              setNote(null);
              setTarget({ ...killTarget(x), via: killVia(connection), server_identity: report?.server_identity ? { ...report.server_identity } : undefined });
            }}>Kill…</PrivilegeGate>
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
      title="Sessions"
      className="sessions-panel"
      onBack={onBack}
      info={
        <>
          <p>
            Every connection to the server, from the process list joined with InnoDB transactions: what each runs, how
            long, idle transactions, and lock waits. Active hides sleeping sessions without a transaction.
          </p>
          <p>Statuses are rules of thumb. Select a session for its statement, transaction, plan, and Kill (it asks first).</p>
        </>
      }
      controls={
        <>
          <Segmented
            label="Sessions to list"
            value={scope}
            onChange={(v) => {
              onScopeChange(v);
              // Latest wins: a read still running for the other scope is replaced.
              if (report) void load(v === "active");
            }}
            options={[
              { value: "active", label: "Active" },
              { value: "all", label: "All" },
            ]}
          />
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
          {report && <SearchField label="Filter sessions" value={search} onChange={setSearch} placeholder="Filter" />}
          <AutoRefresh value={auto} options={AUTO_INTERVALS} disabled={!report} onChange={setAuto} />
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={busy}
      loading={!!loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a session to see its statement, its transaction, Show plan and Kill.</p>}
      status={<ViewStatus loading={loading} error={error} />}
    >
      <div ref={root}>
        {note && <KillResultNote note={note} noteRef={noteRef} onDismiss={() => setNote(null)} onRefresh={() => void load(scope === "active", true)} />}
        {target && (
          <KillDialog
            target={target}
            busy={killing}
            onCancel={() => {
              setTarget(null);
              window.setTimeout(() => trigger.current?.focus(), 0);
            }}
            onConfirm={(mode, token) => void confirmKill(mode, token)}
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
              <PriorityTable
                id="sessions.sessions"
                detailColumns={["user", "db", "command", "flags"]}
                label="Sessions"
                columns={columns}
                rows={rows}
                rowKey={(v) => String(v.id)}
                selection={selection}
                rowAttrs={(v) => ({ "data-session": String(v.id), "data-status": v.status })}
                empty={
                  needle
                    ? `No session matches “${search.trim()}”.`
                    : minSeconds
                      ? `No session has been in its state for ${minSeconds} seconds or longer.`
                      : scope === "active"
                        ? "No active sessions right now."
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
