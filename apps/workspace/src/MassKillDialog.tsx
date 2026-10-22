// Kill sessions dialog (Processlist > Kill sessions…): filter client sessions
// and kill several after one review of the exact KILL statements.
// Inspired by Percona Toolkit pt-kill (busy-time, idle-time, match-user,
// ignore-user, match-host, match-db, match-command, match-info, lock-wait).
// Excludes server/system threads and current connection for safety.
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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
import { killStatusWord, killVia, Sql, type KillNote } from "./Locks";
import { connectionKey } from "./binding";
import { useWritePolicy } from "./Connections";
import { Icon } from "./components";
import { CompactNumber } from "./PerfShared";
import { ActionButton, ActionStatus, ConfirmSheet, NumberField, Segmented, errorText, useFocusScope, writeFailure } from "./kit";

/** The Kill sessions send that is still waiting for its reply, across dialogs:
 * a sheet closed before the reply leaves it running, and a dialog opened
 * meanwhile sends nothing until it ends (one bulk KILL at a time). */
let bulkInFlight: { label: string; count: number } | null = null;
const bulkListeners = new Set<() => void>();
function setBulkInFlight(value: typeof bulkInFlight) {
  bulkInFlight = value;
  bulkListeners.forEach((listener) => listener());
}
function useBulkInFlight() {
  const [, rerender] = useState(0);
  useEffect(() => {
    const listener = () => rerender((n) => n + 1);
    bulkListeners.add(listener);
    return () => { bulkListeners.delete(listener); };
  }, []);
  return bulkInFlight;
}
/** The open Kill sessions dialogs, newest last. The late outcome of a send
 * whose sheet was closed goes to the newest one, which shows it and passes it
 * on to Processlist when it closes: a late reply never closes a dialog opened
 * since (the owner closes the dialog when it gets an outcome). */
const openDialogs: ((note: KillNote) => void)[] = [];

/** Processlist's note while a send whose dialog was closed waits for its reply. */
function waitingNote(count: number, label: string): KillNote {
  return {
    status: "pending",
    message: `Waiting for the reply to ${count} KILL ${count === 1 ? "statement" : "statements"} on ${label}. The outcome shows here when it arrives.`,
    outcome: [],
  };
}

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

  // Preparing the review (nothing is sent) or sending the reviewed KILLs.
  const [phase, setPhase] = useState<"idle" | "preparing" | "sending">("idle");
  const [phaseSince, setPhaseSince] = useState<number | null>(null);
  const busy = phase !== "idle";
  const [error, setError] = useState<string>("");
  // A send from a dialog closed before its reply: this one waits for it.
  const earlier = useBulkInFlight();
  const waitingForEarlier = !!earlier && phase !== "sending";
  const prepareRead = useRef<AbortController | null>(null);
  // The sheet was closed while the KILLs had no reply: the outcome goes to the
  // Processlist note, and this dialog is not closed again by the late reply.
  const left = useRef(false);
  useEffect(() => () => prepareRead.current?.abort(), []);
  // The outcome of an earlier send that arrived while this dialog is open:
  // shown here, and passed on to Processlist when this dialog closes.
  const [earlierNote, setEarlierNote] = useState<KillNote | null>(null);
  const earlierOutcome = useRef<KillNote | null>(null);
  const receive = useRef((note: KillNote) => {
    earlierOutcome.current = note;
    setEarlierNote(note);
  });
  const onSuccessRef = useRef(onSuccess);
  onSuccessRef.current = onSuccess;
  function takeEarlier() {
    const note = earlierOutcome.current;
    earlierOutcome.current = null;
    return note;
  }
  // The send waiting for its reply, if any: a dialog closed by its owner
  // meanwhile (the connection in use changed) is left like Close on its sheet.
  const sending = useRef<{ count: number; label: string } | null>(null);
  useEffect(() => {
    const own = receive.current;
    openDialogs.push(own);
    return () => {
      const index = openDialogs.indexOf(own);
      if (index >= 0) openDialogs.splice(index, 1);
      const note = earlierOutcome.current;
      earlierOutcome.current = null;
      if (sending.current && !left.current) {
        // Its outcome goes where a late one goes (a dialog opened since, or
        // Processlist), and Processlist says it is waiting for it.
        left.current = true;
        onSuccessRef.current(waitingNote(sending.current.count, sending.current.label));
      } else if (note) {
        // Closed some other way: the earlier outcome still reaches Processlist.
        onSuccessRef.current(note);
      }
    };
  }, []);
  /** Close by the user (Close, Cancel, Escape, outside): an earlier outcome
   * shown here goes to Processlist. */
  function close() {
    const note = takeEarlier();
    if (note) onSuccess(note);
    onClose();
  }
  function begin(next: "preparing" | "sending") {
    setPhase(next);
    setPhaseSince(Date.now());
  }
  function end() {
    setPhase("idle");
    setPhaseSince(null);
  }
  const [checkedIds, setCheckedIds] = useState<Set<number>>(new Set());
  const [userOverridden, setUserOverridden] = useState<boolean>(false);
  // The reviewed KILL statements, shown in a confirmation sheet before anything is sent.
  const [pending, setPending] = useState<{ review: BulkKillReview; targets: BulkKillTarget[]; label: string; key: string } | null>(null);
  const [previewScrollTop, setPreviewScrollTop] = useState(0);
  const previewWrap = useRef<HTMLDivElement>(null);
  const previewRaf = useRef<number | null>(null);

  useFocusScope(dialog, {
    open: true, onClose: () => { if (!busy && !pending) close(); },
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

  const queryPattern = useMemo(() => {
    if (!matchInfo) return { regex: null, invalid: false };
    try { return { regex: new RegExp(matchInfo, "i"), invalid: false }; }
    catch { return { regex: null, invalid: true }; }
  }, [matchInfo]);

  // Compute matched sessions based on active pt-kill filters
  const matchedSessions = useMemo(() => {
    if (queryPattern.invalid) return [];
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
      if (queryPattern.regex && !queryPattern.regex.test(s.info || s.info_short || s.last_statement || "")) return false;

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
    queryPattern,
  ]);

  useLayoutEffect(() => {
    // A filter or a fresh report starts at its first match. Cancel a queued
    // scroll frame as well, so it cannot restore the previous list's offset.
    if (previewWrap.current) previewWrap.current.scrollTop = 0;
    setPreviewScrollTop(0);
    return () => {
      if (previewRaf.current !== null) window.cancelAnimationFrame(previewRaf.current);
      previewRaf.current = null;
    };
  }, [matchedSessions]);

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

    begin("preparing");
    setError("");
    const controller = new AbortController();
    prepareRead.current = controller;

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

      const review = await prepareBulkKill(connection, targets, controller.signal);
      if (controller.signal.aborted) return;
      setPending({ review, targets, label: via.label, key: via.key });
    } catch (e) {
      if (!controller.signal.aborted) setError(`Could not prepare the review: ${errorText(e)} Nothing was sent.`);
    } finally {
      if (prepareRead.current === controller) {
        prepareRead.current = null;
        end();
      }
    }
  }
  /** Stop waiting for the review: nothing was sent, and its answer is dropped. */
  function stopPreparing() {
    prepareRead.current?.abort();
    prepareRead.current = null;
    end();
    setError("Stopped waiting for the review. Nothing was sent.");
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
    if (bulkInFlight) return;
    begin("sending");
    setError("");
    left.current = false;
    const on = { key: p.key, label: p.label };
    sending.current = { count: p.review.statements.length, label: p.label };
    /** The outcome: to Processlist, closing this dialog; after its sheet was
     * closed, to the newest dialog opened since, else to Processlist. */
    const deliver = (note: KillNote) => {
      if (!left.current) {
        // This dialog's own outcome replaces an earlier one it was showing.
        takeEarlier();
        onSuccess(note);
        onClose();
        return;
      }
      const late = { ...note, late: true };
      const newest = openDialogs.filter((d) => d !== receive.current).pop();
      if (newest) newest(late);
      else onSuccess(late);
    };
    setBulkInFlight({ label: p.label, count: p.review.statements.length });
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
      // Ended at least one: "Done."; none, with an error: "Could not end it.";
      // none, refused or gone before sending: "Nothing was sent."
      const status = res.killed > 0 ? "killed" : res.errors > 0 ? "error" : res.denied > 0 ? "denied" : "refused";
      deliver({ status, message: res.message, outcome: [], on });
    } catch (e) {
      setPending(null);
      // The server answered with an error (the dialog stays, with it), or no
      // reply arrived: the KILLs may have run, and they are never sent again.
      const failure = writeFailure(e, { audited: true });
      if (failure.phase === "unknown" || left.current)
        deliver({ status: failure.phase === "unknown" ? "unknown" : "error", message: failure.message, outcome: [], refresh: true, on });
      else setError(`Could not send the KILL statements: ${failure.message}`);
    } finally {
      sending.current = null;
      setBulkInFlight(null);
      end();
    }
  }
  /** Close the dialog while the KILLs have no reply yet (offered when slow). */
  function leave() {
    const p = pending;
    if (!p) return;
    left.current = true;
    // An earlier outcome shown here was seen; this send's note replaces it.
    takeEarlier();
    onSuccess(waitingNote(p.review.statements.length, p.label));
    onClose();
  }

  return (
    <dialog
      ref={dialog}
      className="mass-kill-dialog"
      tabIndex={-1}
      aria-labelledby="mass-kill-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy && !pending) close();
      }}
      onClick={(e) => {
        if (e.target === dialog.current && !busy && !pending) close();
      }}
    >
      <div className="mass-kill-header">
        <div className="mass-kill-title-wrap">
          <h2 id="mass-kill-title">
            <Icon name="stop" /> Kill sessions
          </h2>
          <p className="mass-kill-subtitle">
            Filter and terminate client sessions. System threads and your own connection are excluded.
          </p>
        </div>
        <div className="mass-kill-header-actions">
          <Segmented
            label="Kill mode"
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
            onClick={close}
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

      {error && <div className="note danger mass-kill-error" role="alert" style={{ margin: "var(--space-2) var(--space-5)" }}>{error}</div>}
      {waitingForEarlier && (
        <div className="note mass-kill-earlier" role="status" style={{ margin: "var(--space-2) var(--space-5)" }}>
          The earlier Kill sessions request ({earlier.count} KILL {earlier.count === 1 ? "statement" : "statements"} on {earlier.label}) has
          not answered yet. New KILLs wait for it; its outcome shows here when it arrives.
        </div>
      )}
      {!earlier && earlierNote && phase !== "sending" && (
        <div className={`note mass-kill-earlier is-answered${earlierNote.status === "killed" ? "" : " danger"}`} role="status"
          style={{ margin: "var(--space-2) var(--space-5)" }}>
          The earlier Kill sessions request{earlierNote.on ? ` on ${earlierNote.on.label}` : ""} answered:{" "}
          <strong>{killStatusWord(earlierNote.status)}</strong> {earlierNote.message}
        </div>
      )}
      {isReadOnly && (
        <div className="note danger mass-kill-blocked" style={{ margin: "var(--space-2) var(--space-5)" }}>
          {connection.read_only ? "This connection is read-only, so no session can be killed from here." : policy?.blocked}
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
              aria-invalid={queryPattern.invalid}
              aria-describedby={queryPattern.invalid ? "mk-query-error" : undefined}
              onChange={(e) => {
                setActivePreset("custom");
                setUserOverridden(false);
                setMatchInfo(e.target.value);
              }}
            />
            {queryPattern.invalid && <span id="mk-query-error" role="alert">Not a valid pattern</span>}
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

        <div
          ref={previewWrap}
          className="mass-kill-preview-table-wrap"
          onScroll={matchedSessions.length > 80 ? (e) => {
            const wrap = e.currentTarget;
            if (previewRaf.current !== null) return;
            previewRaf.current = window.requestAnimationFrame(() => {
              previewRaf.current = null;
              setPreviewScrollTop(wrap.scrollTop);
            });
          } : undefined}
        >
          {matchedSessions.length === 0 ? (
            <div className="empty-preview">
              <p>No client sessions match these filters.</p>
              <p className="hint">Try lowering the minimum time or clearing user/query filters.</p>
            </div>
          ) : (() => {
            const windowed = matchedSessions.length > 80;
            const rowH = 32;
            const first = windowed ? Math.min(Math.max(0, matchedSessions.length - 36), Math.max(0, Math.floor(previewScrollTop / rowH) - 10)) : 0;
            const last = windowed ? Math.min(matchedSessions.length, first + 36) : matchedSessions.length;
            const shown = windowed ? matchedSessions.slice(first, last) : matchedSessions;
            return (
              <table className="mass-kill-table" aria-rowcount={windowed ? matchedSessions.length + 1 : undefined}>
                <thead>
                  <tr aria-rowindex={windowed ? 1 : undefined}>
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
                  {windowed && first > 0 && (
                    <tr className="row-spacer" aria-hidden="true">
                      <td colSpan={8} style={{ height: first * rowH, padding: 0, border: 0 }} />
                    </tr>
                  )}
                  {shown.map((s, idx) => {
                    const checked = activeIds.has(s.id);
                    const isLocked = s.is_locked || s.lock_status === "holding";
                    const isWaiting = !!s.lock_wait || s.lock_status === "waiting";
                    return (
                      <tr
                        key={s.id}
                        aria-rowindex={windowed ? first + idx + 2 : undefined}
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
                  {windowed && last < matchedSessions.length && (
                    <tr className="row-spacer" aria-hidden="true">
                      <td colSpan={8} style={{ height: (matchedSessions.length - last) * rowH, padding: 0, border: 0 }} />
                    </tr>
                  )}
                </tbody>
              </table>
            );
          })()}
        </div>
      </div>

      {pending && (
        <ConfirmSheet
          className="kill-dialog mass-kill-confirm"
          eyebrow="KILL"
          title={`${pending.review.mode === "query" ? "End the running query of" : "End"} ${pending.targets.length} ${pending.targets.length === 1 ? "connection" : "connections"}?`}
          description="Check these statements and the server. Before sending anything, myflames reads each session again and stops for any that changed or ended. Cancel sends nothing."
          busy={phase === "sending"}
          busyLabel={pending.review.mode === "query"
            ? `Stopping ${pending.targets.length} ${pending.targets.length === 1 ? "query" : "queries"}…`
            : `Ending ${pending.targets.length} ${pending.targets.length === 1 ? "connection" : "connections"}…`}
          progress="Waiting for the KILL replies"
          onLeave={leave}
          leaveText="The KILLs may still be running. Close does not stop them; the outcome shows in Processlist."
          confirmLabel={`Send ${pending.review.statements.length} ${pending.review.statements.length === 1 ? "statement" : "statements"}`}
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
        {/* Always mounted (its live region announces the start); idle, it takes no room.
            Preparing sends nothing: it reads each session and builds the statements to review. */}
        <ActionStatus
          state={{ phase: phase === "preparing" ? "pending" : "idle", message: "Preparing the statements for review. Nothing is sent yet.", startedAt: phaseSince }}
          onStop={stopPreparing}
          stopLabel="Stop waiting"
        />
        {phase !== "preparing" && (
          <span className="mass-kill-footer-safety">
            System threads, replication, and your active connection are excluded.
          </span>
        )}
        <div className="mass-kill-footer-actions">
          <button type="button" className="button" onClick={close} disabled={busy}>
            Cancel
          </button>
          <ActionButton
            danger
            onClick={handleConfirm}
            busy={phase === "preparing"}
            busyLabel="Preparing review…"
            disabled={phase !== "preparing" && (busy || selectedCount === 0 || !!isReadOnly || waitingForEarlier)}
          >
            {`Kill ${selectedCount} ${
                  mode === "query"
                    ? selectedCount === 1
                      ? "query"
                      : "queries"
                    : selectedCount === 1
                    ? "connection"
                    : "connections"
                }`}
          </ActionButton>
        </div>
      </div>
    </dialog>
  );
}
