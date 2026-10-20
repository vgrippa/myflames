// Mass Kill Sessions dialog: filter and terminate multiple client sessions.
// Inspired by Percona Toolkit pt-kill (busy-time, idle-time, match-user,
// ignore-user, match-host, match-db, match-command, match-info, lock-wait).
// Excludes server/system threads and current connection for safety.
import { useMemo, useRef, useState } from "react";
import {
  prepareBulkKill,
  killBulkSessions,
  recordDeclined,
  type BulkKillReview,
  type BulkKillTarget,
  type Connection,
  type KillServerIdentity,
  type Session,
} from "./api";
import { killVia, Sql, type KillNote } from "./Locks";
import { connectionKey } from "./binding";
import { useWritePolicy } from "./Connections";
import { Icon } from "./components";
import { CompactNumber } from "./PerfShared";
import { ConfirmSheet, NumberField, Segmented, useFocusScope } from "./kit";

export function MassKillDialog({
  sessions,
  connection,
  serverIdentity,
  onClose,
  onSuccess,
}: {
  sessions: Session[];
  connection: Connection;
  serverIdentity?: KillServerIdentity;
  onClose: () => void;
  onSuccess: (note: KillNote) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const policy = useWritePolicy();

  // Filters inspired by pt-kill
  const [mode, setMode] = useState<"connection" | "query">("query");
  const [timeType, setTimeType] = useState<"busy" | "idle" | "any">("busy");
  const [minSeconds, setMinSeconds] = useState<number>(30);
  const [selectedUser, setSelectedUser] = useState<string>("all");
  const [ignoreUser, setIgnoreUser] = useState<string>("");
  const [selectedCommand, setSelectedCommand] = useState<string>("all");
  const [selectedDb, setSelectedDb] = useState<string>("all");
  const [matchHost, setMatchHost] = useState<string>("");
  const [matchInfo, setMatchInfo] = useState<string>("");
  const [lockWaitsOnly, setLockWaitsOnly] = useState<boolean>(false);
  const [activePreset, setActivePreset] = useState<"runaway" | "sleep" | "locks" | "custom">("runaway");

  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string>("");
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set());
  const [userOverridden, setUserOverridden] = useState<boolean>(false);
  // The reviewed KILL statements, shown in a confirmation sheet before anything is sent.
  const [pending, setPending] = useState<{ review: BulkKillReview; targets: BulkKillTarget[]; label: string } | null>(null);

  useFocusScope(dialog, {
    open: true, onClose: () => { if (!busy && !pending) onClose(); },
    initialFocus: () => closeButton.current,
  });

  // Distinct users and databases from eligible client sessions
  const clientSessions = useMemo(
    () =>
      sessions.filter(
        (s) =>
          s.thread_kind === "client" &&
          !s.flags.includes("own_connection") &&
          !s.flags.includes("system_thread"),
      ),
    [sessions],
  );

  const distinctUsers = useMemo(() => {
    const set = new Set<string>();
    clientSessions.forEach((s) => {
      if (s.user) set.add(s.user);
    });
    return Array.from(set).sort();
  }, [clientSessions]);

  const distinctDbs = useMemo(() => {
    const set = new Set<string>();
    clientSessions.forEach((s) => {
      if (s.db) set.add(s.db);
    });
    return Array.from(set).sort();
  }, [clientSessions]);

  function applyPreset(preset: "runaway" | "sleep" | "locks" | "custom") {
    setActivePreset(preset);
    setUserOverridden(false);
    if (preset === "runaway") {
      setMode("query");
      setTimeType("busy");
      setMinSeconds(30);
      setSelectedCommand("Query");
      setLockWaitsOnly(false);
    } else if (preset === "sleep") {
      setMode("connection");
      setTimeType("idle");
      setMinSeconds(60);
      setSelectedCommand("Sleep");
      setLockWaitsOnly(false);
    } else if (preset === "locks") {
      setMode("query");
      setTimeType("any");
      setMinSeconds(0);
      setSelectedCommand("all");
      setLockWaitsOnly(true);
    } else if (preset === "custom") {
      setTimeType("any");
      setMinSeconds(0);
      setSelectedCommand("all");
      setLockWaitsOnly(false);
    }
  }

  // Compute matched sessions based on active pt-kill filters
  const matchedSessions = useMemo(() => {
    return clientSessions.filter((s) => {
      // User match
      if (selectedUser !== "all" && s.user !== selectedUser) return false;

      // Ignore user (pt-kill --ignore-user)
      if (ignoreUser.trim()) {
        const ignored = ignoreUser.toLowerCase().split(/[\s,]+/).filter(Boolean);
        if (s.user && ignored.includes(s.user.toLowerCase())) return false;
      }

      // Host match (pt-kill --match-host)
      if (matchHost.trim()) {
        const host = (s.client_host || s.host || "").toLowerCase();
        if (!host.includes(matchHost.trim().toLowerCase())) return false;
      }

      // Database match (pt-kill --match-db)
      if (selectedDb !== "all" && s.db !== selectedDb) return false;

      // Command match (pt-kill --match-command)
      if (selectedCommand !== "all") {
        if (selectedCommand === "Sleep" && s.command !== "Sleep") return false;
        if (selectedCommand === "Query" && s.command !== "Query") return false;
        if (selectedCommand !== "Sleep" && selectedCommand !== "Query" && s.command !== selectedCommand) {
          return false;
        }
      }

      // Time match (pt-kill --busy-time / --idle-time)
      const time = s.time_s ?? 0;
      if (timeType === "busy") {
        if (s.command === "Sleep") return false;
        if (minSeconds > 0 && time < minSeconds) return false;
      } else if (timeType === "idle") {
        if (s.command !== "Sleep") return false;
        if (minSeconds > 0 && time < minSeconds) return false;
      } else {
        if (minSeconds > 0 && time < minSeconds) return false;
      }

      // Lock waiters only: holding locks does not mean the session is waiting.
      if (lockWaitsOnly) {
        const isWaiting = !!s.lock_wait || s.lock_status === "waiting";
        if (!isWaiting) return false;
      }

      // Query pattern match (pt-kill --match-info)
      if (matchInfo.trim()) {
        const queryText = (s.info || s.info_short || s.last_statement || "").toLowerCase();
        const pattern = matchInfo.trim().toLowerCase();
        try {
          const rx = new RegExp(pattern, "i");
          if (!rx.test(queryText)) return false;
        } catch {
          if (!queryText.includes(pattern)) return false;
        }
      }

      return true;
    });
  }, [
    clientSessions,
    selectedUser,
    ignoreUser,
    matchHost,
    selectedDb,
    selectedCommand,
    timeType,
    minSeconds,
    lockWaitsOnly,
    matchInfo,
  ]);

  // Auto-select all matching when filters change unless manually overridden
  const activeIds = useMemo(() => {
    if (!userOverridden) {
      return new Set(matchedSessions.map((s) => s.id));
    }
    const available = new Set(matchedSessions.map((s) => s.id));
    return new Set(Array.from(checkedIds).filter((id) => available.has(id)));
  }, [matchedSessions, userOverridden, checkedIds]);

  const selectedCount = activeIds.size;
  const isReadOnly = connection.read_only || policy?.blocked;

  function toggleId(id: number) {
    setUserOverridden(true);
    const next = new Set(activeIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setCheckedIds(next);
  }

  function toggleAll() {
    setUserOverridden(true);
    if (activeIds.size === matchedSessions.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(matchedSessions.map((s) => s.id)));
    }
  }

  async function handleConfirm() {
    if (!serverIdentity) {
      setError("Server identity is missing; please refresh the sessions first.");
      return;
    }
    const toKill = matchedSessions.filter((s) => activeIds.has(s.id));
    if (!toKill.length) return;
    if (toKill.length > 200) {
      setError("Please select at most 200 sessions at once.");
      return;
    }

    setBusy(true);
    setError("");

    try {
      const via = killVia(connection);
      const targets = toKill.map((s) => ({
        connection_id: s.id,
        mode,
        expect: {
          user: s.user,
          host: s.host,
          trx_started: s.trx?.started ?? null,
          ps_thread_id: null,
          connection_identity: via.key,
          server_identity: serverIdentity,
        },
      }));

      const review = await prepareBulkKill(connection, targets);
      setPending({ review, targets, label: via.label });
    } catch (e) {
      setError((e as Error).message || "Mass kill failed.");
    } finally {
      setBusy(false);
    }
  }

  function declineBulk() {
    const p = pending;
    setPending(null);
    if (!p) return;
    void recordDeclined(
      p.review.mode === "query" ? "kill_query" : "kill",
      p.review.statements,
      connection,
      { mode: p.review.mode, connection_ids: p.targets.map((t) => t.connection_id) },
    );
  }

  async function sendBulk() {
    const p = pending;
    if (!p || busy) return;
    setBusy(true);
    setError("");
    try {
      // Re-read the type and read-only flag: they may have changed since the review.
      const active = policy ? await policy.check() : connection;
      if (connectionKey(active) !== connectionKey(connection)) {
        setPending(null);
        setError("The connection's type or read-only setting changed after this review, so nothing was sent. Check it and try again.");
        return;
      }
      const res = await killBulkSessions(connection, p.targets, p.review.review_token);
      setPending(null);
      onSuccess({ status: res.killed > 0 ? "ok" : "warn", message: res.message, outcome: [] });
      onClose();
    } catch (e) {
      setPending(null);
      setError((e as Error).message || "Mass kill failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog
      ref={dialog}
      className="mass-kill-dialog"
      tabIndex={-1}
      aria-labelledby="mass-kill-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy && !pending) onClose();
      }}
      onClick={(e) => {
        if (e.target === dialog.current && !busy && !pending) onClose();
      }}
    >
      <div className="mass-kill-header">
        <div className="mass-kill-title-wrap">
          <h2 id="mass-kill-title">
            <Icon name="stop" /> Mass Kill
          </h2>
          <p className="mass-kill-subtitle">
            Filter and terminate client sessions. System threads and your own connection are excluded.
          </p>
        </div>
        <div className="mass-kill-header-actions">
          <Segmented
            label="Kill Mode"
            value={mode}
            onChange={(v) => setMode(v as "connection" | "query")}
            options={[
              { value: "query", label: "Kill query" },
              { value: "connection", label: "Kill connection" },
            ]}
          />
          <button
            ref={closeButton}
            type="button"
            className="icon-button"
            aria-label="Close dialog"
            disabled={busy || !!pending}
            onClick={onClose}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      </div>

      <div className="mass-kill-presets">
        <span className="presets-label">Presets</span>
        <button
          type="button"
          className={`button small${activePreset === "runaway" ? " active" : " ghost"}`}
          aria-pressed={activePreset === "runaway"}
          onClick={() => applyPreset("runaway")}
        >
          Runaway queries (&gt; 30s)
        </button>
        <button
          type="button"
          className={`button small${activePreset === "sleep" ? " active" : " ghost"}`}
          aria-pressed={activePreset === "sleep"}
          onClick={() => applyPreset("sleep")}
        >
          Idle sleeping (&gt; 60s)
        </button>
        <button
          type="button"
          className={`button small${activePreset === "locks" ? " active" : " ghost"}`}
          aria-pressed={activePreset === "locks"}
          onClick={() => applyPreset("locks")}
        >
          Lock waiters
        </button>
        <button
          type="button"
          className={`button small${activePreset === "custom" ? " active" : " ghost"}`}
          aria-pressed={activePreset === "custom"}
          onClick={() => applyPreset("custom")}
        >
          All clients
        </button>
      </div>

      {error && <div className="note danger mass-kill-error" style={{ margin: "var(--space-2) var(--space-5)" }}>{error}</div>}
      {isReadOnly && (
        <div className="note danger" style={{ margin: "var(--space-2) var(--space-5)" }}>
          This connection is read-only. Mass kill is disabled.
        </div>
      )}

      <div className="mass-kill-form">
        <div className="mass-kill-grid">
          <div className="mass-kill-field">
            <span className="field-label">Time condition</span>
            <div className="mass-kill-time-input">
              <select
                aria-label="Time condition type"
                value={timeType}
                onChange={(e) => {
                  setActivePreset("custom");
                  setUserOverridden(false);
                  setTimeType(e.target.value as "busy" | "idle" | "any");
                }}
              >
                <option value="busy">Busy time (Query &gt;)</option>
                <option value="idle">Idle time (Sleep &gt;)</option>
                <option value="any">Time in state &gt;</option>
              </select>
              <div style={{ width: 100 }}>
                <NumberField
                  label="Minimum duration in seconds"
                  value={minSeconds}
                  min={0}
                  step={5}
                  unit="s"
                  onChange={(v) => {
                    setActivePreset("custom");
                    setUserOverridden(false);
                    setMinSeconds(Math.max(0, Math.round(+v) || 0));
                  }}
                />
              </div>
            </div>
          </div>

          <div className="mass-kill-field">
            <label htmlFor="mk-user" className="field-label">Match user</label>
            <select
              id="mk-user"
              value={selectedUser}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setSelectedUser(e.target.value);
              }}
            >
              <option value="all">All users ({distinctUsers.length})</option>
              {distinctUsers.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
          </div>

          <div className="mass-kill-field">
            <label htmlFor="mk-ignore-user" className="field-label">Ignore users (comma-separated)</label>
            <input
              id="mk-ignore-user"
              type="text"
              placeholder="e.g. repl, root, proxysql"
              value={ignoreUser}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setIgnoreUser(e.target.value);
              }}
            />
          </div>
        </div>

        <div className="mass-kill-grid">
          <div className="mass-kill-field">
            <label htmlFor="mk-cmd" className="field-label">Command</label>
            <select
              id="mk-cmd"
              value={selectedCommand}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setSelectedCommand(e.target.value);
              }}
            >
              <option value="all">All commands</option>
              <option value="Query">Query (running)</option>
              <option value="Sleep">Sleep (idle)</option>
              <option value="Binlog Dump">Binlog Dump</option>
              <option value="Connect">Connect</option>
            </select>
          </div>

          <div className="mass-kill-field">
            <label htmlFor="mk-db" className="field-label">Database</label>
            <select
              id="mk-db"
              value={selectedDb}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setSelectedDb(e.target.value);
              }}
            >
              <option value="all">All databases ({distinctDbs.length})</option>
              {distinctDbs.map((d) => (
                <option key={d} value={d}>
                  {d}
                </option>
              ))}
            </select>
          </div>

          <div className="mass-kill-field">
            <label htmlFor="mk-host" className="field-label">Match host</label>
            <input
              id="mk-host"
              type="text"
              placeholder="e.g. 10.0.0.%, app-server"
              value={matchHost}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setMatchHost(e.target.value);
              }}
            />
          </div>
        </div>

        <div className="mass-kill-grid-full">
          <div className="mass-kill-field">
            <label htmlFor="mk-query" className="field-label">Match query pattern (regex or text)</label>
            <input
              id="mk-query"
              type="text"
              className="cell-sql"
              placeholder="e.g. SELECT.*FROM orders|UPDATE users"
              value={matchInfo}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setMatchInfo(e.target.value);
              }}
            />
          </div>

          <div className="mass-kill-field">
            <span className="field-label">Lock conditions</span>
            <label className="mass-kill-checkbox-label">
              <input
                type="checkbox"
                checked={lockWaitsOnly}
                onChange={(e) => {
                  setActivePreset("custom");
                  setUserOverridden(false);
                  setLockWaitsOnly(e.target.checked);
                }}
              />
              <span>Lock waits only</span>
            </label>
          </div>
        </div>
      </div>

      <div className="mass-kill-preview-section">
        <div className="mass-kill-preview-header">
          <div className="preview-title">
            <strong>Preview: {matchedSessions.length} matching sessions</strong>
            {matchedSessions.length > 0 && (
              <span className="preview-selected-pill">
                {selectedCount} of {matchedSessions.length} selected
              </span>
            )}
          </div>
          {matchedSessions.length > 0 && (
            <button type="button" className="text-button" onClick={toggleAll}>
              {activeIds.size === matchedSessions.length ? "Deselect all" : "Select all"}
            </button>
          )}
        </div>

        <div className="mass-kill-preview-table-wrap">
          {matchedSessions.length === 0 ? (
            <div className="empty-preview">
              <p>No client sessions match these filters.</p>
              <p className="hint">Try lowering the minimum time or clearing user/query filters.</p>
            </div>
          ) : (
            <table className="mass-kill-table">
              <thead>
                <tr>
                  <th scope="col" style={{ width: 36 }}><span className="visually-hidden">Select</span></th>
                  <th style={{ width: 70 }}>ID</th>
                  <th style={{ width: 150 }}>User@host</th>
                  <th style={{ width: 100 }}>DB</th>
                  <th style={{ width: 90 }}>Command</th>
                  <th style={{ width: 75 }}>Time</th>
                  <th style={{ width: 100 }}>Lock / State</th>
                  <th>Statement / Last Query</th>
                </tr>
              </thead>
              <tbody>
                {matchedSessions.map((s) => {
                  const checked = activeIds.has(s.id);
                  const isLocked = s.is_locked || s.lock_status === "holding";
                  const isWaiting = !!s.lock_wait || s.lock_status === "waiting";
                  return (
                    <tr
                      key={s.id}
                      className={checked ? "is-selected" : ""}
                      onClick={() => toggleId(s.id)}
                    >
                      <td onClick={(e) => e.stopPropagation()}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleId(s.id)}
                          aria-label={`Select session ${s.id}`}
                        />
                      </td>
                      <td>
                        <strong>{s.id}</strong>
                      </td>
                      <td>
                        {s.user || "?"}
                        <span className="cell-muted">@{s.client_host || s.host || "?"}</span>
                      </td>
                      <td>{s.db || <span className="cell-muted">—</span>}</td>
                      <td>
                        <span className={`cmd-pill is-${(s.command || "").toLowerCase()}`}>
                          {s.command || "—"}
                        </span>
                      </td>
                      <td>
                        <CompactNumber value={s.time_s} />s
                      </td>
                      <td>
                        {isWaiting ? (
                          <span className="pill is-danger" title="Waiting for lock">Lock wait</span>
                        ) : isLocked ? (
                          <span className="pill is-warn" title="Holding lock">Locked</span>
                        ) : (
                          <span className="cell-muted">{s.state || "—"}</span>
                        )}
                      </td>
                      <td>
                        {s.info_short ? (
                          <code className="cell-truncate cell-sql" title={s.info || s.info_short}>
                            {s.info_short}
                          </code>
                        ) : s.last_statement ? (
                          <code className="cell-truncate cell-sql cell-muted" title={s.last_statement}>
                            Last: {s.last_statement}
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
          )}
        </div>
      </div>

      {pending && (
        <ConfirmSheet
          className="kill-dialog mass-kill-confirm"
          eyebrow="KILL"
          title={`${pending.review.mode === "query" ? "End the running query of" : "End"} ${pending.targets.length} ${pending.targets.length === 1 ? "connection" : "connections"}?`}
          description="Check these statements and the server. Before sending anything, myflames reads each session again and stops for any that changed or ended. Cancel sends nothing."
          busy={busy}
          confirmLabel={busy ? "Terminating sessions…" : `Send ${pending.review.statements.length} ${pending.review.statements.length === 1 ? "statement" : "statements"}`}
          onCancel={declineBulk}
          onConfirm={() => void sendBulk()}
        >
          <Sql text={pending.review.statements.join(";\n")} label="Statements to send" />
          <dl className="kill-facts">
            <div>
              <dt>Server</dt>
              <dd>{pending.label}</dd>
            </div>
            <div>
              <dt>Sessions</dt>
              <dd>{pending.targets.length}</dd>
            </div>
          </dl>
        </ConfirmSheet>
      )}
      <div className="mass-kill-footer">
        <span className="mass-kill-footer-safety">
          System threads, replication, and your active connection are excluded.
        </span>
        <div className="mass-kill-footer-actions">
          <button type="button" className="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="button danger"
            onClick={handleConfirm}
            disabled={busy || selectedCount === 0 || !!isReadOnly}
          >
            {busy
              ? "Terminating sessions…"
              : `Kill ${selectedCount} ${
                  mode === "query"
                    ? selectedCount === 1
                      ? "query"
                      : "queries"
                    : selectedCount === 1
                    ? "connection"
                    : "connections"
                }`}
          </button>
        </div>
      </div>
    </dialog>
  );
}
