// Reviewed changes that the backend plans from a fresh read (the ProxySQL
// and Cluster places; myflames/reviewed_writes.py, db-operations skill
// section 6). Planning reads only. The sheet shows the statements exactly as
// they will be sent, the target, the connection and its write policy, the
// facts and warnings, the undo, and a typed name for destructive changes.
// Cancel and Escape record a declined attempt in the Audit log. Confirm reads
// the saved connections again (useWritePolicy) and sends the reviewed plan
// with its token; the backend reads the target again and sends nothing when
// it changed. The result is a panel in the view, never a toast, and a
// request without a reply is "Outcome unknown": nothing is sent again.
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
  policy?: ReviewPolicy;
  review_token?: string;
  [key: string]: unknown;
};
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
};
export type ShownResult = { key: string; label: string; title: string; value: ReviewedResult };

const RESULT_TONE: Record<ReviewedResult["status"], [Tone, string]> = {
  done: ["ok", "Done"],
  refused: ["unknown", "Nothing sent"],
  failed: ["problem", "Failed"],
  partial: ["problem", "Partly applied"],
  unknown: ["watch", "Outcome unknown"],
};

type Pending = { plan: ReviewedPlan; connection: Connection; key: string; label: string };
type Ask = { title: string; label: string; min: number; max: number; current: number | null; run: (value: number) => void };

/**
 * The review flow of one place. *operation* names declined attempts in the
 * Audit log (proxysql_action, cluster_action, like the backend). *send*
 * posts the reviewed plan; *onSent* runs after a send, answered or not
 * (read the place again).
 */
export function useReviewedActions({ connection, operation, send, onSent }: {
  connection: Connection;
  operation: string;
  send: (connection: Connection, plan: ReviewedPlan, token: string, typed: string) => Promise<ReviewedResult>;
  onSent?: () => void;
}) {
  const key = connectionKey(connection);
  const policy = useWritePolicy();
  const captureScope = useOpsScope(connection);
  const [pending, setPending] = useState<Pending | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState("");
  const [error, setError] = useState<{ key: string; text: string } | null>(null);
  const [result, setResult] = useState<ShownResult | null>(null);
  const sending = useRef(false);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const sentRef = useRef(onSent);
  sentRef.current = onSent;

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
  // Another connection closes (and audits) an open sheet; a request already
  // sent belongs to its own connection and is never repeated.
  useEffect(() => {
    const p = pendingRef.current;
    if (p && p.key !== key && !sending.current) { decline(p); setPending(null); }
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
    if (sending.current || pendingRef.current || planning) return;
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
    if (!p || sending.current || p.key !== key) return;
    if (p.plan.typed_guard && typed !== p.plan.typed_guard) return;
    const live = captureScope();
    sending.current = true;
    setBusy(true);
    let sent = false;
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
      const value = await send(checked, reviewed as ReviewedPlan, token || "", typed);
      setResult({ key: p.key, label: p.label, title: p.plan.title, value });
      setPending(null);
      if (live()) sentRef.current?.();
    } catch (e) {
      setPending(null);
      // A refusal with a reply happened before anything was sent (the
      // backend sends only after its own checks); no reply is unknown.
      const replied = e instanceof ApiError && e.status < 500;
      if (!sent || replied) {
        setError({ key: p.key, text: `${(e as Error).message}${/nothing was sent/i.test((e as Error).message) ? "" : " Nothing was sent."}` });
      } else {
        setResult({ key: p.key, label: p.label, title: p.plan.title, value: {
          schema_version: p.plan.schema_version, action: p.plan.action, status: "unknown", statements: p.plan.statements,
          sent: [], not_sent: [], error: (e as Error).message, after: null, undo: p.plan.undo, next: null, audit_id: null,
          message: `No reply arrived (${(e as Error).message}), so whether it ran is unknown. Nothing will be sent again: refresh and check the state, and the Audit log, before you do anything else.`,
        } });
        if (live()) sentRef.current?.();
      }
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  const guard = pending?.plan.typed_guard || "";
  const sheet: ReactNode = <>
    {ask && <ValueSheet ask={ask} onCancel={() => setAsk(null)} onReview={(v) => { const run = ask.run; setAsk(null); run(v); }} />}
    {pending && <ReviewSheet p={pending} busy={busy} typed={typed} onTyped={setTyped}
      confirmDisabled={pending.key !== key || (!!guard && typed !== guard) || !!policy?.blocked}
      blocked={policy?.blocked || (pending.key !== key ? "The connection changed. Review the change again." : "")}
      onCancel={() => { if (!busy) { decline(pending); setPending(null); setTyped(""); } }}
      onConfirm={() => { void confirm(); }} />}
  </>;
  const shownError = error && error.key === key ? error.text : "";
  const shownResult = result && result.key === key ? result : null;
  return { review, askValue, sheet, planning, error: shownError, result: shownResult,
    clearResult: () => setResult(null), busy: busy || !!planning || !!pending };
}

function ReviewSheet({ p, busy, typed, onTyped, confirmDisabled, blocked, onCancel, onConfirm }: {
  p: Pending; busy: boolean; typed: string; onTyped: (v: string) => void; confirmDisabled: boolean; blocked: string;
  onCancel: () => void; onConfirm: () => void;
}) {
  const plan = p.plan;
  const typedId = useId();
  const type = plan.policy?.connection_type || p.connection.type || "production";
  const readOnly = plan.policy?.read_only === true;
  const endpoint = `${p.connection.host || "127.0.0.1"}:${p.connection.port || 3306}`;
  return (
    <ConfirmSheet className="ops-confirm action-review" eyebrow={plan.eyebrow} title={plan.title} description={plan.description}
      busy={busy} confirmTone={plan.policy?.tone || "danger"} confirmDisabled={confirmDisabled}
      confirmLabel={busy ? "Sending…" : plan.confirm_label} onCancel={onCancel} onConfirm={onConfirm}>
      <div className="ops-target"><span>Target</span><strong>{plan.target_text}</strong></div>
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
