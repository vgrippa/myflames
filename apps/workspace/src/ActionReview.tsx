// Reviewed changes that the backend plans from a fresh read (the ProxySQL
// and Cluster places; myflames/reviewed_writes.py, db-operations skill
// section 6). Planning reads only. The sheet shows the statements exactly as
// they will be sent, the target, the connection and its write policy, the
// facts and warnings, the undo, and a typed name for destructive changes.
// Cancel and Escape record a declined attempt in the Audit log. Confirm reads
// the saved connections again (useWritePolicy) and sends the reviewed plan
// with its token; the backend reads the target again and sends nothing when
// it changed.
//
// After Confirm the same sheet follows the send: the button reads
// "Applying…" and nothing can be clicked twice; the backend's progress
// (/api/live/action/progress, reviewed_writes.Progress) names the step and
// each statement's state (Waiting, Sent, Done, Failed, Not run, Not sent,
// Outcome unknown). A statement is Done only once the server answered. Then
// the sheet shows the result: what changed (read again after the write), the
// Audit log record, and for an unknown outcome what to check. Close leaves the
// result as a panel in the view, never a toast. A request without a reply is
// "Outcome unknown": nothing is sent again.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, ApiError, recordDeclined, type Connection } from "./api";
import { connectionKey, connectionLabel } from "./binding";
import { TYPE_LABEL, useWritePolicy } from "./Connections";
import { ConfirmSheet, Fields, NumberField, StatusPill, type Tone } from "./kit";
import { Commands } from "./OpsShared";
import { useOpsScope } from "./opsApi";
import "./ops.css";

export type ReviewPolicy = {
  action: "allow" | "confirm" | "refuse";
  tone: "danger" | "primary";
  title: string;
  text: string;
  connection_type?: "production" | "development";
  read_only?: boolean;
};
/** A plan as the backend returned it (proxysql-action-1.0, cluster-action-1.0). */
export type ReviewedPlan = {
  schema_version: string;
  action: string;
  eyebrow: string;
  title: string;
  confirm_label: string;
  description: string;
  target_text: string;
  statements: string[];
  facts: [string, string][];
  warnings: string[];
  typed_guard: string | null;
  undo: string | null;
  changes?: string[] | null;
  next?: { action: string; label: string } | null;
  expect: unknown;
  /** How long the backend waits for the server's answer. */
  timeout_s?: number;
  /** What to look at when the outcome is unknown. */
  checks?: string[];
  policy?: ReviewPolicy;
  review_token?: string;
  [key: string]: unknown;
};
export type StatementState = "waiting" | "sent" | "done" | "failed" | "not_run" | "not_sent" | "unknown";
export type ReviewedResult = {
  schema_version: string;
  action: string;
  status: "done" | "refused" | "failed" | "partial" | "unknown";
  message: string;
  statements: string[];
  sent: string[];
  not_sent: string[];
  error: string | null;
  after: Record<string, unknown> | null;
  undo: string | null;
  next: { action: string; label: string } | null;
  audit_id: string | null;
  /** Each statement's final state, in order. */
  states?: StatementState[];
  /** What changed: what, before, now (read again after the write). */
  effect?: [string, string, string][] | null;
  checks?: string[];
};
/** reviewed_writes.Progress as /api/live/action/progress returns it. */
export type ActionProgress = {
  schema_version: string;
  phase: "received" | "checking" | "sending" | "reading" | "finished" | "unknown";
  states: StatementState[];
  status: string | null;
  elapsed_s: number | null;
  waiting_s: number | null;
  timeout_s: number | null;
};
export type ShownResult = { key: string; label: string; title: string; value: ReviewedResult };
export type SendReviewed = (connection: Connection, plan: ReviewedPlan, token: string, typed: string, progressId: string) => Promise<ReviewedResult>;

const RESULT_TONE: Record<ReviewedResult["status"], [Tone, string]> = {
  done: ["ok", "Done"],
  refused: ["unknown", "Nothing sent"],
  failed: ["problem", "Failed"],
  partial: ["problem", "Partly applied"],
  unknown: ["watch", "Outcome unknown"],
};
const STATE_WORD: Record<StatementState, [Tone, string]> = {
  waiting: ["unknown", "Waiting"],
  sent: ["info", "Sent"],
  done: ["ok", "Done"],
  failed: ["problem", "Failed"],
  not_run: ["unknown", "Not run"],
  not_sent: ["unknown", "Not sent"],
  unknown: ["watch", "Outcome unknown"],
};
const POLL_MS = 400;
/** Opens the Audit log, with the record selected when an id is given (main.tsx listens). */
export const OPEN_AUDIT_EVENT = "myflames-open-audit";
export function openAuditLog(id?: string | null) {
  window.dispatchEvent(new CustomEvent(OPEN_AUDIT_EVENT, { detail: id || "" }));
}

type Pending = { plan: ReviewedPlan; connection: Connection; key: string; label: string };
type Ask = { title: string; label: string; min: number; max: number; current: number | null; run: (value: number) => void };
/** A confirmed send, from Confirm to Close: its progress, then its result. */
type Run = { p: Pending; id: string; progress: ActionProgress | null; result: ReviewedResult | null; sentAt: number | null };

function progressId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * The review flow of one place. *operation* names declined attempts in the
 * Audit log (proxysql_action, cluster_action, like the backend). *send*
 * posts the reviewed plan with the progress id; *onSent* runs after a send,
 * answered or not (read the place again); *onNext* starts the change a done
 * result offers next (Save … to disk…).
 */
export function useReviewedActions({ connection, operation, send, onSent, onNext }: {
  connection: Connection;
  operation: string;
  send: SendReviewed;
  onSent?: () => void;
  onNext?: (action: string) => void;
}) {
  const key = connectionKey(connection);
  const policy = useWritePolicy();
  const captureScope = useOpsScope(connection);
  const [pending, setPending] = useState<Pending | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState("");
  const [error, setError] = useState<{ key: string; text: string } | null>(null);
  const [result, setResult] = useState<ShownResult | null>(null);
  const sending = useRef(false);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const runRef = useRef(run);
  runRef.current = run;
  const sentRef = useRef(onSent);
  sentRef.current = onSent;
  const nextRef = useRef(onNext);
  nextRef.current = onNext;

  function decline(p: Pending) {
    void recordDeclined(operation, p.plan.statements, p.connection, {
      action: p.plan.action, target: p.plan.target_text, expect: p.plan.expect,
    });
  }
  /** A confirmed sheet stopped here before sending (the saved connections
   * could not be read, or name another policy): recorded as refused. */
  function refuseLocally(p: Pending, reason: "policy_changed" | "policy_unknown") {
    void api("audit/declined", { operation, statements: p.plan.statements, connection: p.connection, reason,
      target: { action: p.plan.action, target: p.plan.target_text, expect: p.plan.expect } }).catch(() => null);
  }
  // Another connection closes (and audits) an open review, and closes an
  // answered result; a request already sent belongs to its own connection.
  useEffect(() => {
    const p = pendingRef.current;
    if (p && p.key !== key && !sending.current) { decline(p); setPending(null); }
    const r = runRef.current;
    if (r && r.p.key !== key && r.result) setRun(null);
    setAsk(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => () => {
    const p = pendingRef.current;
    if (p && !sending.current) decline(p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Plan *what* from a fresh read and open the sheet (or say why not). */
  async function review(what: string, planned: (c: Connection) => Promise<ReviewedPlan>) {
    if (sending.current || pendingRef.current || runRef.current || planning) return;
    const c = { ...connection };
    const live = captureScope();
    setError(null);
    setPlanning(what);
    try {
      const plan = await planned(c);
      if (!live()) return;
      if (plan.policy?.action === "refuse") { setError({ key: connectionKey(c), text: plan.policy.text }); return; }
      setTyped("");
      setPending({ plan, connection: c, key: connectionKey(c), label: connectionLabel(c) });
    } catch (e) {
      if (live()) setError({ key: connectionKey(c), text: (e as Error).message });
    } finally {
      setPlanning("");
    }
  }
  function askValue(next: Ask) { setAsk(next); }

  async function confirm() {
    const p = pending;
    if (!p || sending.current || runRef.current || p.key !== key) return;
    if (p.plan.typed_guard && typed !== p.plan.typed_guard) return;
    const live = captureScope();
    sending.current = true;
    setBusy(true);
    const id = progressId();
    // The same sheet follows the send (no second dialog): the review is over.
    setRun({ p, id, progress: null, result: null, sentAt: null });
    setPending(null);
    let sent = false;
    let stopped = false;
    let lastPhase: ActionProgress["phase"] | null = null;
    const finish = (value: ReviewedResult, keepResult = true) => {
      // A send keeps its audit record, but its completed sheet must not move
      // to another connection (including a switch away and back).
      setRun((r) => (r && r.id === id ? (live() ? { ...r, result: value } : null) : r));
      if (live() && keepResult) setResult({ key: p.key, label: p.label, title: p.plan.title, value });
    };
    async function poll() {
      while (!stopped) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
        if (stopped) return;
        try {
          const snap = await api<ActionProgress>("live/action/progress", { progress_id: id });
          if (stopped) return;
          if (snap.phase !== "unknown") lastPhase = snap.phase;
          setRun((r) => (r && r.id === id && !r.result
            ? { ...r, progress: snap, sentAt: r.sentAt ?? (snap.phase === "sending" ? Date.now() - (snap.waiting_s || 0) * 1000 : null) }
            : r));
        } catch {
          // Progress only describes the send; the send's own answer decides.
        }
      }
    }
    void poll();
    try {
      let checked: Connection;
      try {
        checked = policy ? await policy.check() : p.connection;
      } catch (e) {
        refuseLocally(p, "policy_unknown");
        throw new Error(`The saved connections could not be read (${(e as Error).message}). Nothing was sent.`);
      }
      if (connectionKey(checked) !== p.key) {
        refuseLocally(p, "policy_changed");
        throw new Error("The connection or its write policy changed. Nothing was sent. Review the change again.");
      }
      const { review_token: token, ...reviewed } = p.plan;
      sent = true;
      const value = await send(checked, reviewed as ReviewedPlan, token || "", typed, id);
      stopped = true;
      finish(value);
      if (live()) sentRef.current?.();
    } catch (e) {
      stopped = true;
      // A refusal with a reply happened before anything was sent (the
      // backend sends only after its own checks), unless the progress said
      // the statements were already with the server; no reply is unknown.
      const replied = e instanceof ApiError && e.status < 500;
      const withServer = lastPhase === "sending" || lastPhase === "reading";
      if (!sent || (replied && !withServer)) {
        const text = `${(e as Error).message}${/nothing was sent/i.test((e as Error).message) ? "" : " Nothing was sent."}`;
        if (live()) setError({ key: p.key, text });
        finish(localResult(p.plan, "refused", text, null), false);
      } else {
        const value = localResult(p.plan, "unknown",
          `No reply arrived (${(e as Error).message}), so whether it ran is unknown. Nothing will be sent again: refresh and check the state, and the Audit log, before you do anything else.`,
          (e as Error).message);
        finish(value);
        if (live()) sentRef.current?.();
      }
    } finally {
      stopped = true;
      sending.current = false;
      setBusy(false);
    }
  }

  const guard = pending?.plan.typed_guard || "";
  const shown = run ? run.p : pending;
  const closeRun = () => setRun(null);
  const sheet: ReactNode = <>
    {ask && <ValueSheet ask={ask} onCancel={() => setAsk(null)} onReview={(v) => { const go = ask.run; setAsk(null); go(v); }} />}
    {shown && <ReviewSheet p={shown} run={run} busy={busy} typed={typed} onTyped={setTyped}
      confirmDisabled={!!run || shown.key !== key || (!!guard && typed !== guard) || !!policy?.blocked}
      blocked={run ? "" : policy?.blocked || (shown.key !== key ? "The connection changed. Review the change again." : "")}
      onCancel={() => {
        if (busy) return;
        if (run) { closeRun(); return; }
        if (pending) { decline(pending); setPending(null); setTyped(""); }
      }}
      onConfirm={() => { void confirm(); }}
      onNext={run?.p.key === key && run.result?.status === "done" && run.result.next && nextRef.current
        ? () => { const next = run.result!.next!.action; closeRun(); setResult(null); nextRef.current?.(next); } : undefined}
      onAudit={(auditId) => { closeRun(); openAuditLog(auditId); }} />}
  </>;
  const shownError = error && error.key === key ? error.text : "";
  const shownResult = result && result.key === key ? result : null;
  return { review, askValue, sheet, planning, error: shownError, result: shownResult,
    clearResult: () => setResult(null), busy: busy || !!planning || !!pending || !!run };
}

/** A result the workspace words itself: refused before sending, or no reply. */
function localResult(plan: ReviewedPlan, status: "refused" | "unknown", message: string, error: string | null): ReviewedResult {
  return {
    schema_version: plan.schema_version, action: plan.action, status, statements: plan.statements, sent: [],
    not_sent: status === "refused" ? plan.statements : [], error, after: null, undo: plan.undo, next: null, audit_id: null,
    message, states: plan.statements.map(() => (status === "refused" ? "not_sent" : "unknown")), effect: null,
    checks: plan.checks || [],
  };
}

function phaseText(run: Run, now: number): string {
  const phase = run.progress?.phase;
  if (phase === "checking") return "Reading the target again before sending…";
  if (phase === "sending") {
    const waited = run.sentAt ? Math.max(0, Math.round((now - run.sentAt) / 1000)) : 0;
    const limit = run.progress?.timeout_s || run.p.plan.timeout_s;
    return `Sent. Waiting for the server's answer: ${waited.toLocaleString()} s${limit ? ` of at most ${limit.toLocaleString()} s` : ""}.`;
  }
  if (phase === "reading") return "The server answered. Reading the result back…";
  if (phase === "finished") {
    const status = run.progress?.status;
    return status === "unknown" ? "No answer arrived from the server. Finishing…"
      : status === "refused" ? "Nothing was sent. Finishing…"
        : status === "done" || status === "partial" ? "The server answered. Finishing…" : "Finishing…";
  }
  if (phase === "received") return "Checking the review and the connection's write policy…";
  return "Sending the reviewed change…";
}

/** Each statement with its state; *spinning* marks the ones the server has now. */
function StatementSteps({ statements, states, spinning }: { statements: string[]; states: StatementState[]; spinning: boolean }) {
  return (
    <ol className="action-steps" aria-label="Statements and their state">
      {statements.map((s, i) => {
        const state = states[i] || "waiting";
        const [tone, word] = STATE_WORD[state] || STATE_WORD.unknown;
        return (
          <li key={i} data-state={state}>
            <code>{s}</code>
            <span className="action-step-state">
              {spinning && state === "sent" && <span className="spinner" aria-hidden="true" />}
              <StatusPill tone={tone} label={word} />
            </span>
          </li>
        );
      })}
    </ol>
  );
}

/** What changed, read again after the write. */
export function EffectTable({ rows }: { rows: [string, string, string][] }) {
  return (
    <table className="action-effect">
      <caption>What changed</caption>
      <thead><tr><th scope="col">What</th><th scope="col">Before</th><th scope="col">Now</th></tr></thead>
      <tbody>
        {rows.map(([what, before, now], i) => (
          <tr key={i} data-changed={before !== now ? "true" : undefined}>
            <th scope="row">{what}</th><td>{before}</td><td>{now}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function auditText(r: ReviewedResult): string {
  if (r.audit_id) return r.status === "unknown" ? "Recorded in the Audit log as Outcome unknown." : "Recorded in the Audit log.";
  return r.status === "refused" ? "Recorded in the Audit log as refused." : "The Audit log has the attempt.";
}

function ReviewSheet({ p, run, busy, typed, onTyped, confirmDisabled, blocked, onCancel, onConfirm, onNext, onAudit }: {
  p: Pending; run: Run | null; busy: boolean; typed: string; onTyped: (v: string) => void; confirmDisabled: boolean; blocked: string;
  onCancel: () => void; onConfirm: () => void; onNext?: () => void; onAudit: (id: string | null) => void;
}) {
  const plan = p.plan;
  const typedId = useId();
  const type = plan.policy?.connection_type || p.connection.type || "production";
  const readOnly = plan.policy?.read_only === true;
  const endpoint = `${p.connection.host || "127.0.0.1"}:${p.connection.port || 3306}`;
  const [now, setNow] = useState(() => Date.now());
  const applying = !!run && !run.result;
  useEffect(() => {
    if (!applying) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [applying]);
  const r = run?.result || null;
  const title = run ? plan.title.replace(/\?$/, "") : plan.title;
  const states: StatementState[] = r?.states && r.states.length === plan.statements.length ? r.states
    : run?.progress?.states && run.progress.states.length === plan.statements.length ? run.progress.states
      : plan.statements.map(() => "waiting");
  const [tone, word] = r ? RESULT_TONE[r.status] || RESULT_TONE.unknown : ["unknown", ""];
  const checks = r?.checks?.length ? r.checks : plan.checks || [];
  return (
    <ConfirmSheet className="ops-confirm action-review" eyebrow={plan.eyebrow} title={title}
      description={run ? undefined : plan.description}
      busy={busy} confirmTone={plan.policy?.tone || "danger"} confirmDisabled={confirmDisabled}
      confirmLabel={busy ? "Applying…" : plan.confirm_label} onCancel={onCancel} onConfirm={onConfirm}
      doneLabel={r ? "Close" : undefined}
      secondary={r && onNext && r.next ? { label: r.next.label, onClick: onNext } : undefined}>
      <div className="ops-target"><span>Target</span><strong>{plan.target_text}</strong></div>
      {run ? <>
        {r ? (
          <div className="action-outcome" role="status" data-status={r.status}>
            <StatusPill tone={tone as Tone} label={word} />
            <p>{r.message}</p>
          </div>
        ) : (
          <p className="action-phase" role="status" aria-live="polite">
            <span className="spinner" aria-hidden="true" /> {phaseText(run, now)}
          </p>
        )}
        <StatementSteps statements={plan.statements} states={states} spinning={applying} />
        {applying && <p className="hint">Sent once, never again. Close is offered when the server has answered.</p>}
        {r?.effect && r.effect.length > 0 && <EffectTable rows={r.effect} />}
        {r?.status === "unknown" && checks.length > 0 && (
          <section className="action-checks" aria-label="What to check">
            <h3>What to check</h3>
            <ol>{checks.map((c, i) => <li key={i}>{c}</li>)}</ol>
          </section>
        )}
        {r && (r.status === "partial" || r.status === "unknown") && r.undo && <p className="hint">Undo, after checking: {r.undo}</p>}
        {r && (
          <p className="hint action-audit">
            {auditText(r)}{" "}
            <button type="button" className="text-button" onClick={() => onAudit(r.audit_id)}>Open the Audit log</button>
          </p>
        )}
      </> : <>
        <Commands label="Statements to send, exactly as sent" text={plan.statements.join("\n")} />
        <Fields rows={[
          ["Connection", p.label.includes(endpoint) ? p.label : `${p.label} (${endpoint})`],
          ["Type", `${TYPE_LABEL[type]}${readOnly ? ", read-only" : ""}`],
          ...plan.facts,
          ["Undo", plan.undo || "Cannot be undone."],
        ]} />
        {plan.policy?.text && <p>{plan.policy.text}</p>}
        {blocked && <p className="ops-warning" role="alert">{blocked}</p>}
        {plan.warnings.map((line, i) => <p className="ops-warning" key={i}>{line}</p>)}
        {plan.changes && (
          <details open={plan.changes.length > 0 && plan.changes.length <= 6}>
            <summary>{plan.changes.length ? `${plan.changes.length.toLocaleString()} ${plan.changes.length === 1 ? "difference" : "differences"} this applies` : "No differences found"}</summary>
            {plan.changes.length > 0 && <ul>{plan.changes.map((line, i) => <li key={i}>{line}</li>)}</ul>}
          </details>
        )}
        {plan.typed_guard && (
          <label className="ops-field action-typed" htmlFor={typedId}>
            <span>Type {plan.typed_guard} to confirm</span>
            <input id={typedId} autoComplete="off" spellCheck={false} value={typed} disabled={busy} onChange={(e) => onTyped(e.target.value)} />
          </label>
        )}
        <p className="hint">Before sending, myflames reads the target again and sends nothing if it changed.</p>
      </>}
    </ConfirmSheet>
  );
}

function ValueSheet({ ask, onCancel, onReview }: { ask: Ask; onCancel: () => void; onReview: (value: number) => void }) {
  const [text, setText] = useState(ask.current === null ? "" : String(ask.current));
  const id = useId();
  const value = /^\d+$/.test(text.trim()) ? Number(text.trim()) : NaN;
  const valid = Number.isSafeInteger(value) && value >= ask.min && value <= ask.max && value !== ask.current;
  return (
    <ConfirmSheet className="ops-confirm action-value" title={ask.title} confirmTone="primary" confirmLabel="Review the change…"
      confirmDisabled={!valid} onCancel={onCancel} onConfirm={() => { if (valid) onReview(value); }}>
      <label className="ops-field" htmlFor={id}>
        <span>{ask.label}</span>
        <NumberField id={id} value={text} min={ask.min} max={ask.max} step={1} onChange={setText} />
      </label>
      <p className="hint">{`A whole number from ${ask.min.toLocaleString()} to ${ask.max.toLocaleString()}${ask.current !== null ? `; now ${ask.current.toLocaleString()}` : ""}. Nothing is sent yet: the next step shows the exact statements.`}</p>
    </ConfirmSheet>
  );
}

/** The result of the last reviewed change on this connection, in the view. */
export function ActionResult({ shown, onClose, onNext, extra }: {
  shown: ShownResult; onClose: () => void; onNext?: (action: string) => void; extra?: ReactNode;
}) {
  const r = shown.value;
  const [tone, word] = RESULT_TONE[r.status] || RESULT_TONE.unknown;
  return (
    <section className="ops-result action-result" aria-label="Result of the change" role="status" data-status={r.status}>
      <div className="ops-result-head">
        <StatusPill tone={tone} label={word} />
        <strong>{shown.title.replace(/\?$/, "")}</strong>
        <button type="button" className="text-button" onClick={() => openAuditLog(r.audit_id)}>Audit log</button>
        <button type="button" className="text-button" onClick={onClose}>Close</button>
      </div>
      <p>{r.message}</p>
      {extra}
      {(r.status === "partial" || r.status === "unknown") && r.undo && <p className="hint">Undo, after checking: {r.undo}</p>}
      {r.status === "done" && r.next && onNext && (
        <div className="ops-actions action-next">
          <button type="button" className="button" onClick={() => onNext(r.next!.action)}>{r.next.label}</button>
          <span className="hint">A separate change, reviewed on its own.</span>
        </div>
      )}
    </section>
  );
}
