// One stable runner per Server view. Inspector selection changes only replace
// buttons; the reviewed request, its session and Stop remain owned by the view.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type Connection, type EditorConfirmation, type EditorReply } from "./api";
import { connectionKey } from "./binding";
import { ActionStatus, ConfirmSheet, Disclosure, writeFailure, type ActionPhase } from "./kit";
import { editorPrivilegeGate, PrivilegeGate, type EditorPrivilegePreview } from "./privileges";

type Request = { tab_id: string; connection: Connection; sql: string; require_confirmation: true; wait: number; limit: number };
type Review = EditorConfirmation & { typed_target?: string | null };
type Action = {
  request: Request; key: string; label: string; warning: string; phase: "review" | "confirm" | "running" | "unknown" | "done";
  confirmation?: Review; reply?: EditorReply; message: string; error: boolean;
  /** Date.now() when the current step began (the review, or the confirmed send). */
  since: number;
  /** The confirmed statements were sent: from then on an answer that never
   * comes is an unknown outcome, never "nothing was sent". */
  sent?: boolean;
  /** The sheet was closed while the confirmed send had no reply yet. */
  left?: boolean;
};
type Actions = { start(sql: string, label: string, warning?: string): void; blocked: boolean; active: Action | null; stop(): void; check(): void; dismiss(): void;
  /** Stop waiting for a review that is still being prepared (nothing was sent). */
  abandon(): void };
const Context = createContext<Actions | null>(null);

export function ReviewedSqlProvider({ connection, disabled, onComplete, children }: {
  connection: Connection; disabled: boolean; onComplete(): void; children: ReactNode;
}) {
  const [active, setActive] = useState<Action | null>(null);
  const [typed, setTyped] = useState("");
  const current = useRef<Action | null>(null);
  const latest = useRef({ connection, disabled, onComplete });
  latest.current = { connection, disabled, onComplete };
  const alive = useRef(true);
  const pending = useRef(false);
  const polling = useRef(new Set<string>());
  const anchor = useRef<HTMLSpanElement>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  function update(action: Action, changes: Partial<Action>) {
    if (!alive.current || current.current?.request.tab_id !== action.request.tab_id) return;
    const next = { ...current.current, ...changes };
    current.current = next;
    setActive(next);
  }
  function compatible(action: Action) {
    return action.key === connectionKey(latest.current.connection) && !latest.current.disabled && !latest.current.connection.read_only;
  }
  async function close(action: Action) {
    try {
      // Never force-close an active or unknown transaction. The normal editor
      // close checks transaction state and the dedicated session's ownership.
      await api("editor/close", { tab_id: action.request.tab_id, connection: action.request.connection });
    } catch { /* Its normal idle timeout remains the fallback. No write retry. */ }
  }
  async function receive(action: Action, reply: EditorReply) {
    if (!alive.current || current.current?.request.tab_id !== action.request.tab_id) return;
    if (reply.status === "needs_confirmation" && reply.confirmation) {
      setTyped("");
      update(action, { phase: "confirm", confirmation: reply.confirmation, reply, message: "", error: false });
      return;
    }
    if (reply.run?.state === "running" || reply.status === "running") {
      update(action, { phase: "running", confirmation: undefined, reply, message: `${action.label} is running.`, error: false });
      void poll(action);
      return;
    }
    // Maintenance statements may report failure as an ordinary result row.
    const maintenanceError = reply.run?.results.flatMap(result => {
      if (result.type !== "rows") return [];
      const kind = result.columns.findIndex(column => column.name.toLowerCase() === "msg_type");
      return kind < 0 ? [] : result.rows.filter(row => row[kind]?.toLowerCase() === "error").map(row => row.join(" · "));
    })[0];
    const succeeded = reply.run?.state === "complete" && !reply.run.error && !reply.run.counts.errors && !maintenanceError;
    const cancelled = reply.status === "declined";
    const message = succeeded
      ? `${action.label} completed. ${reply.run!.counts.affected_rows} rows affected.`
      : cancelled ? "Cancelled. Nothing was sent."
        : maintenanceError || reply.run?.error?.message || reply.message || reply.run?.message || `Operation ${reply.status}.`;
    update(action, { phase: "done", confirmation: undefined, reply, message, error: !succeeded && !cancelled });
    await close(action);
    if (succeeded && alive.current && current.current?.request.tab_id === action.request.tab_id && compatible(action)) latest.current.onComplete();
  }
  async function poll(action: Action) {
    const owner = action.request.tab_id;
    if (polling.current.has(owner)) return;
    polling.current.add(owner);
    try {
      while (alive.current && current.current?.request.tab_id === action.request.tab_id) {
        let reply: EditorReply;
        try {
          reply = await api<EditorReply>("editor/status", { tab_id: action.request.tab_id,
            connection: action.request.connection, wait: 2 });
        } catch (error) {
          update(action, { phase: "unknown", message: `Could not read the outcome: ${(error as Error).message}. Check status before trying again.`, error: true });
          return;
        }
        if (!reply.run) {
          update(action, { phase: "unknown", message: "The server did not return a run. Its outcome is unknown; nothing was retried.", error: true });
          return;
        }
        if (reply.run.state !== "running") { await receive(action, reply); return; }
        update(action, { phase: "running", reply, message: `${action.label} is running.`, error: false });
        // Status normally long-polls. Also bound immediate test/server replies.
        await new Promise(resolve => window.setTimeout(resolve, 250));
      }
    } finally { polling.current.delete(owner); }
  }
  async function send(action: Action, extra: Record<string, unknown> = {}, confirmed = false) {
    if (pending.current) return;
    pending.current = true;
    try {
      const reply = await api<EditorReply>("editor/run", { ...action.request, ...extra });
      await receive(action, reply);
    } catch (error) {
      // A confirmed send the server answered with an error (4xx: its words, a
      // failure) differs from one with no reply (it may have run: check the
      // existing run, never send it again).
      const failure = confirmed ? writeFailure(error) : null;
      if (failure?.phase === "unknown") {
        update(action, { phase: "unknown", confirmation: undefined, since: Date.now(),
          message: "The execution reply was lost. Checking the existing run without sending it again.", error: true });
        void poll(action);
      } else if (failure) {
        update(action, { phase: "done", confirmation: undefined, message: `${action.label} failed: ${failure.message}`, error: true });
        await close(action);
      } else {
        update(action, { phase: "done", confirmation: undefined, message: (error as Error).message, error: true });
        await close(action);
      }
    } finally { if (!current.current || current.current.request.tab_id === action.request.tab_id) pending.current = false; }
  }
  function start(sql: string, label: string, warning = "") {
    if (!sql.trim() || latest.current.disabled || latest.current.connection.read_only || pending.current ||
        (current.current && current.current.phase !== "done")) return;
    const snapshot = { ...latest.current.connection };
    const action: Action = { request: { tab_id: `view-${crypto.randomUUID()}`, connection: snapshot,
      sql, require_confirmation: true, wait: 1, limit: 1000 }, key: connectionKey(snapshot), label, warning,
      phase: "review", message: `Preparing the SQL review of ${label}. Nothing is sent yet.`, error: false, since: Date.now() };
    current.current = action;
    setActive(action);
    void prepare(action);
  }
  async function prepare(action: Action) {
    pending.current = true;
    try {
      const preview = await api<EditorPrivilegePreview>("editor/preview", action.request);
      const reason = editorPrivilegeGate(preview).disabledReason;
      if (reason) { update(action, { phase: "done", message: reason, error: true }); return; }
    } catch { /* Unverifiable privileges remain enabled; the server decides. */ }
    // Only this action's own flag: an abandoned review never clears a newer one's.
    finally { if (!current.current || current.current.request.tab_id === action.request.tab_id) pending.current = false; }
    // Stop waiting (abandon) while the preview was out: nothing more is sent.
    if (current.current?.request.tab_id !== action.request.tab_id) return;
    if (!alive.current || !compatible(action)) {
      update(action, { phase: "done", message: "The connection or policy changed. Nothing was sent.", error: true });
      return;
    }
    await send(action);
  }
  function confirm() {
    const action = current.current;
    if (!action?.confirmation || !compatible(action)) return;
    update(action, { phase: "review", message: `Sending the reviewed statements of ${action.label}…`, sent: true, since: Date.now() });
    void send(action, { confirmed: true, confirm_token: action.confirmation.token, typed_target: typed }, true);
  }
  /** Close the sheet while its confirmed send has no reply (offered when slow);
   * the line under the toolbar keeps the progress and shows the outcome. */
  function leave() {
    const action = current.current;
    if (action?.sent) update(action, { left: true });
  }
  /** Stop waiting for the review being prepared: nothing was sent, and the
   * session it opened is closed. A confirmed send is never abandoned. */
  function abandon() {
    const action = current.current;
    if (!action || action.sent || action.confirmation || action.phase !== "review") return;
    current.current = null;
    setActive({ ...action, phase: "done", message: "Stopped waiting for the review. Nothing was sent.", error: false });
    pending.current = false;
    void close(action);
  }
  function decline() {
    const action = current.current;
    if (!action?.confirmation || pending.current) return;
    update(action, { phase: "review", message: "Cancelling…" });
    void send(action, { decline: true, confirm_token: action.confirmation.token });
  }
  useEffect(() => {
    if (active?.phase !== "confirm") return;
    const check = () => { if (anchor.current?.closest("[hidden]")) decline(); };
    const observer = new MutationObserver(check);
    observer.observe(document.body, { attributes: true, attributeFilter: ["hidden"], subtree: true });
    queueMicrotask(check);
    return () => observer.disconnect();
  }, [active?.phase, active?.request.tab_id]);
  async function stop() {
    const action = current.current;
    if (!action) return;
    try {
      await api("editor/stop", { tab_id: action.request.tab_id, connection: action.request.connection });
      update(action, { message: "Stop requested. Waiting for the server's result." });
      void poll(action);
    } catch (error) { update(action, { message: (error as Error).message, error: true }); }
  }
  const blocked = disabled || !!connection.read_only || !!active && active.phase !== "done";
  return <Context.Provider value={{ start, blocked, active, stop: () => void stop(), abandon,
    check: () => { if (current.current) void poll(current.current); },
    dismiss: () => {
      if (current.current?.phase === "done") { current.current = null; setActive(null); }
      else if (!current.current && active?.phase === "done") setActive(null);
    } }}>
    <span ref={anchor} style={{ display: "none" }} />{children}
    {active?.confirmation && !active.left && <ConfirmSheet title={`${active.label}?`} description={active.confirmation.text}
      busy={active.phase !== "confirm"} busyLabel="Sending…"
      onLeave={active.sent ? leave : undefined}
      leaveText="It may still be running on the server. Close does not stop it; the outcome shows under the toolbar."
      confirmLabel={active.label} confirmTone={active.confirmation.tone}
      confirmDisabled={!compatible(active) || (active.confirmation.typed_target != null && typed !== active.confirmation.typed_target)}
      onConfirm={confirm} onCancel={decline}>
      <dl className="sheet-facts"><div><dt>Connection</dt><dd>{active.confirmation.connection.user}@{active.confirmation.connection.host}:{active.confirmation.connection.port}</dd></div>
        <div><dt>Type</dt><dd>{active.confirmation.connection_type}</dd></div></dl>
      {!compatible(active) && <p role="alert">The active connection or its policy changed. Cancel this review and prepare it again.</p>}
      {active.warning && <p className="hint">{active.warning}</p>}
      {active.confirmation.warnings.map((warning, index) => <p className="hint" key={index}>{warning}</p>)}
      <pre className="sql-preview" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{active.confirmation.statements.join("\n\n")}</pre>
      {active.confirmation.typed_target != null && <label>Type {active.confirmation.typed_target}<input value={typed} onChange={event => setTyped(event.target.value)} /></label>}
    </ConfirmSheet>}
  </Context.Provider>;
}

export function ReviewedSqlAction({ sql, label, warning = "", disabled = false, action = "instruments_presets" }: { sql: string; label: string; warning?: string; disabled?: boolean; action?: string }) {
  const context = useContext(Context);
  if (!context || !sql.trim()) return null;
  return <PrivilegeGate action={action} className="small" data-reviewed-sql={label}
    blocked={disabled || context.blocked ? "This action is unavailable while the view is stale, read-only, or another action is active." : ""}
    onClick={() => context.start(sql, label, warning)}>{label}…</PrivilegeGate>;
}

/** What the reviewed action is doing, under the view's toolbar: preparing the
 * review (Stop waiting when slow; nothing was sent), sending or running (with
 * the seconds and Stop), then what happened. Always mounted, so its polite
 * live region announces the start and the outcome. */
export function ReviewedSqlStatus() {
  const context = useContext(Context);
  if (!context) return null;
  const action = context.active;
  const phase: ActionPhase = !action ? "idle"
    : action.phase === "review" || action.phase === "running" ? "pending"
      : action.phase === "confirm" ? "idle"
        : action.phase === "unknown" ? "unknown"
          : action.error ? "failed" : "done";
  const preparing = !!action && action.phase === "review" && !action.sent;
  return <div className={`view-status reviewed-sql-status${phase === "idle" ? " is-idle" : ""}`} data-reviewed-status=""
    aria-busy={phase === "pending" || undefined}>
    <ActionStatus state={{ phase, message: action?.message || "", startedAt: action?.since ?? null }}
      onStop={preparing ? context.abandon : undefined} stopLabel="Stop waiting"
      onDismiss={action?.phase === "done" ? context.dismiss : undefined}>
      {action && (action.phase === "running" || action.phase === "unknown") && <button className="button small" onClick={context.stop}>Stop</button>}
      {action?.reply?.run && action.phase === "done" && action.reply.run.results.some(result => result.type === "rows") &&
        <Disclosure className="reviewed-messages" title="Server messages"><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{action.reply.run.results.flatMap(result => result.type === "rows" ? result.rows.map(row => row.map(value => value ?? "NULL").join(" · ")) : []).join("\n")}</pre></Disclosure>}
    </ActionStatus>
  </div>;
}

export function useReviewedSql() {
  return useContext(Context);
}

export function ReviewedSqlBridger({ bridgerRef }: { bridgerRef: { current: Actions | null } }) {
  const context = useContext(Context);
  bridgerRef.current = context;
  return null;
}


