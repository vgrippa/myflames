// Reviewed changes that the backend plans from a fresh read (the ProxySQL,
// Cluster and Readyset places; myflames/reviewed_writes.py, db-operations
// skill section 6). Planning reads only. The sheet shows any identity check
// (`session_guard`) and the statements, the target, the connection and its
// write policy, the facts and warnings, the undo, and a typed name for
// destructive changes. How the check is sent decides the label: ProxySQL
// Admin and MySQL Shell send it as their own first statement in the same
// login, so the shown text is exactly as sent; for MySQL and MariaDB the
// backend frames the check into the same packet as each statement
// (dbops.target_guarded_script), so the shown text is labelled as the
// reviewed statements for reading, not a script to paste.
// Cancel and Escape record a declined attempt in the Audit log. Confirm reads
// the saved connections again (useWritePolicy) and sends the reviewed plan
// with its token; the backend reads the target again and sends nothing when
// it changed.
//
// After Confirm the same sheet follows the send: the button reads
// "Sending…" and nothing can be clicked twice; the backend's progress
// (/api/live/action/progress, reviewed_writes.Progress) names the step and
// each statement's state (Waiting, Sent, Done, Failed, Not run, Not sent,
// Outcome unknown). A statement is Done only once the server answered. Then
// the sheet shows the result: what changed (read again after the write), for
// ClusterSet actions the SQL verification facts (a "done, not fully verified"
// outcome is a watch pill, never a green Done), the Audit log record, and for
// an unknown outcome what to check. Close leaves the
// result as a panel in the view, never a toast. A request without a reply is
// "Outcome unknown": nothing is sent again.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { api, ApiError, recordDeclined, type Connection } from "./api";
import { connectionKey, connectionLabel } from "./binding";
import { ConnectionTypeBand, TYPE_LABEL, useWritePolicy } from "./Connections";
import { ActionStatus, ConfirmSheet, Disclosure, Fields, NumberField, StatusPill, type Tone } from "./kit";
import { Commands } from "./OpsShared";
import { useOpsScope } from "./opsApi";
import { LiveShellOutput, ShellActionOutput, type ProgressOutputChunk } from "./ShellOutput";
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
  /** Automatically derived identity check sent first on the same session
   * (reviewed_writes.session_guard: SQL or MySQL Shell JavaScript). */
  session_guard?: string[];
  /** Execution transport when present (sql, udf, mysqlsh). */
  via?: string;
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
  session_guard?: string[];
  /** What changed: what, before, now (read again after the write). */
  effect?: [string, string, string][] | null;
  checks?: string[];
  /** MySQL Shell stdout returned by ClusterSet / Shell execution (including check_clusterset JSON). */
  shell_output?: string | null;
  /** True when the backend kept only the last 1 MiB of shell_output. */
  shell_output_truncated?: boolean;
  /** MySQL Shell stderr returned by ClusterSet / Shell execution. */
  shell_stderr?: string | null;
  /** True when the backend kept only the last 1 MiB of shell_stderr. */
  shell_stderr_truncated?: boolean;
  /** ClusterSet execution outcome classification and SQL verification details. */
  outcome_class?: string | null;
  outcome?: string | null;
  verification?: {
    /** Per-server expected/actual facts (clusterset_verify._Evidence.add). */
    facts?: { server: string; fact: string; expected: unknown; actual: unknown;
      status: "verified" | "missing" | "mismatch" }[];
    missing?: string[];
    mismatches?: string[];
    summary?: string;
  } | null;
};
export type { ProgressOutputChunk };
/** reviewed_writes.Progress as /api/live/action/progress returns it. */
export type ActionProgress = {
  schema_version: string;
  phase: "received" | "checking" | "sending" | "reading" | "finished" | "unknown";
  states: StatementState[];
  status: string | null;
  elapsed_s: number | null;
  waiting_s: number | null;
  timeout_s: number | null;
  /** Bounded, already-redacted Shell progress chunks from Progress.emit. */
  output?: ProgressOutputChunk[];
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
/** The outcome pill for a result. "done, not fully verified" (ClusterSet
 * execution: Shell's call succeeded but some SQL facts could not be read back)
 * is not a green Done. */
function resultTone(r: ReviewedResult): [Tone, string] {
  if (r.status === "done" && (r.outcome_class || r.outcome) === "done, not fully verified") {
    return ["watch", "Done, not fully verified"];
  }
  return RESULT_TONE[r.status] || RESULT_TONE.unknown;
}
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
type Run = { p: Pending; id: string; progress: ActionProgress | null; result: ReviewedResult | null; sentAt: number | null; startedAt: number };

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
export function useReviewedActions({ connection, operation, send, onSent, onNext, resetKey = "" }: {
  connection: Connection;
  operation: string;
  send: SendReviewed;
  onSent?: () => void;
  onNext?: (action: string) => void;
  /** A caller-scoped generation that closes an open review the same way a
   * connection switch does (Readyset: the report turned database-stale). */
  resetKey?: string;
}) {
  const key = connectionKey(connection);
  const policy = useWritePolicy();
  const captureScope = useOpsScope(connection);
  const [pending, setPending] = useState<Pending | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [left, setLeft] = useState(false);
  const leftRef = useRef(false);
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
  // A caller-scoped reset (resetKey) does the same without a key change, but
  // keeps an answered Outcome unknown panel: the Audit log row alone does
  // not show whether the change ran, so it stays until it is closed.
  const resetRef = useRef(resetKey);
  // The resetKey generation of this render, current before the effect below
  // runs: a review tags its plan request with it and drops an answer that
  // returns after a reset changed it.
  const genRef = useRef(resetKey);
  genRef.current = resetKey;
  useEffect(() => {
    const reset = resetRef.current !== resetKey;
    resetRef.current = resetKey;
    const p = pendingRef.current;
    if (p && (p.key !== key || reset) && !sending.current) { decline(p); setPending(null); }
    const r = runRef.current;
    if (r && (r.p.key !== key || reset) && r.result) setRun(null);
    if (reset) setResult((cur) => (cur && cur.value.status === "unknown" ? cur : null));
    setAsk(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, resetKey]);
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
    const gen = genRef.current;
    setError(null);
    setPlanning(what);
    try {
      const plan = await planned(c);
      // A reset while planning closed the context this plan was read for:
      // drop the answer. Nothing was ever pending, so no sheet opens and
      // there is nothing to decline.
      if (!live() || gen !== genRef.current) return;
      if (plan.policy?.action === "refuse") { setError({ key: connectionKey(c), text: plan.policy.text }); return; }
      setTyped("");
      setPending({ plan, connection: c, key: connectionKey(c), label: connectionLabel(c) });
    } catch (e) {
      if (live() && gen === genRef.current) setError({ key: connectionKey(c), text: (e as Error).message });
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
    leftRef.current = false; setLeft(false);
    setResult(null);
    setBusy(true);
    const id = progressId();
    // The same sheet follows the send (no second dialog): the review is over.
    setRun({ p, id, progress: null, result: null, sentAt: null, startedAt: Date.now() });
    setPending(null);
    let sent = false;
    let stopped = false;
    let lastPhase: ActionProgress["phase"] | null = null;
    const finish = (value: ReviewedResult, keepResult = true) => {
      // A send keeps its audit record, but its completed sheet must not move
      // to another connection (including a switch away and back).
      const wasLeft = leftRef.current;
      setRun((r) => (r && r.id === id ? (live() && !wasLeft ? { ...r, result: value } : null) : r));
      if (live() && (keepResult || wasLeft)) setResult({ key: p.key, label: p.label, title: p.plan.title, value });
    };
    async function poll() {
      while (!stopped) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
        if (stopped) return;
        try {
          const snap = await api<ActionProgress>("live/action/progress", { progress_id: id });
          if (stopped) return;
          if (snap.phase !== "unknown") lastPhase = snap.phase;
          setRun((r) => {
            if (!r || r.id !== id || r.result) return r;
            const output = Array.isArray(snap.output) ? snap.output : r.progress?.output;
            const progress = output !== undefined ? { ...snap, output } : snap;
            return {
              ...r,
              progress,
              sentAt: r.sentAt ?? (snap.phase === "sending" ? Date.now() - (snap.waiting_s || 0) * 1000 : null),
            };
          });
        } catch {
          // Progress only describes the send; the send's own answer decides.
        }
      }
    }
    void poll();
    try {
      let checked: Connection;
      try {
        checked = policy ? await policy.check(p.connection) : p.connection;
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
      leftRef.current = false; setLeft(false);
    }
  }

  const guard = pending?.plan.typed_guard || "";
  const shown = run ? run.p : pending;
  const closeRun = () => setRun(null);
  const sheet: ReactNode = <>
    {ask && <ValueSheet connection={connection} ask={ask} onCancel={() => setAsk(null)} onReview={(v) => { const go = ask.run; setAsk(null); go(v); }} />}
    {left && run && !run.result && <ActionStatus className="reviewed-action-status" state={{ phase: "pending",
      message: `Waiting for the reviewed change on ${run.p.label}. Its outcome is not known yet; nothing will be sent again.`, startedAt: run.startedAt }} />}
    {shown && !left && <ReviewSheet p={shown} run={run} busy={busy} typed={typed} onTyped={setTyped}
      confirmDisabled={!!run || shown.key !== key || (!!guard && typed !== guard) || !!policy?.blocked}
      blocked={run ? "" : policy?.blocked || (shown.key !== key ? "The connection changed. Review the change again." : "")}
      onCancel={() => {
        if (busy) return;
        if (run) { closeRun(); return; }
        if (pending) { decline(pending); setPending(null); setTyped(""); }
      }}
      onLeave={() => { leftRef.current = true; setLeft(true); }}
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

const VERIFICATION_TONE: Record<string, [Tone, string]> = {
  verified: ["ok", "Verified"],
  missing: ["unknown", "Not read"],
  mismatch: ["problem", "Mismatch"],
};

function factValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** The SQL facts read back after a ClusterSet change: each server's expected
 * and actual state, with what was verified, not read or contradictory.
 * Collapsible; open when anything is missing or mismatched. */
export function VerificationSection({ verification }: { verification: NonNullable<ReviewedResult["verification"]> }) {
  const facts = (Array.isArray(verification.facts) ? verification.facts : [])
    .filter((f) => !!f && typeof f === "object" && typeof f.fact === "string");
  const missing = Array.isArray(verification.missing) ? verification.missing : [];
  const mismatches = Array.isArray(verification.mismatches) ? verification.mismatches : [];
  if (!facts.length) return null;
  return (
    <Disclosure title="SQL verification" count={facts.length || undefined}
      open={missing.length > 0 || mismatches.length > 0} className="action-verification">
      {verification.summary && <p className="hint">{verification.summary}</p>}
      {facts.length > 0 && (
        <div className="shell-check-table-wrap">
          <table className="shell-check-clusters action-verification-facts">
            <thead>
              <tr><th scope="col">Server</th><th scope="col">Fact</th><th scope="col">Expected</th><th scope="col">Actual</th><th scope="col">Result</th></tr>
            </thead>
            <tbody>
              {facts.map((f, i) => {
                const [tone, word] = VERIFICATION_TONE[f.status] || ["unknown", f.status] as [Tone, string];
                return (
                  <tr key={i} data-status={f.status}>
                    <th scope="row">{f.server}</th>
                    <td>{f.fact}</td>
                    <td>{factValue(f.expected)}</td>
                    <td>{factValue(f.actual)}</td>
                    <td><StatusPill tone={tone} label={word} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Disclosure>
  );
}

function auditText(r: ReviewedResult): string {
  if (r.audit_id) return r.status === "unknown" ? "Recorded in the Audit log as Outcome unknown." : "Recorded in the Audit log.";
  return r.status === "refused" ? "Recorded in the Audit log as refused." : "The Audit log has the attempt.";
}

function ReviewSheet({ p, run, busy, typed, onTyped, confirmDisabled, blocked, onCancel, onConfirm, onLeave, onNext, onAudit }: {
  p: Pending; run: Run | null; busy: boolean; typed: string; onTyped: (v: string) => void; confirmDisabled: boolean; blocked: string;
  onCancel: () => void; onConfirm: () => void; onLeave: () => void; onNext?: () => void; onAudit: (id: string | null) => void;
}) {
  const plan = p.plan;
  const typedId = useId();
  const type = plan.policy?.connection_type || p.connection.type || "production";
  const readOnly = plan.policy?.read_only === true;
  const endpoint = `${p.connection.host || "127.0.0.1"}:${p.connection.port || 3306}`;
  const [now, setNow] = useState(() => Date.now());
  const [guardOpen, setGuardOpen] = useState(true);
  useEffect(() => { setGuardOpen(true); }, [plan]);
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
  const [tone, word] = r ? resultTone(r) : ["unknown", ""];
  const checks = r?.checks?.length ? r.checks : plan.checks || [];
  const sessionGuard = Array.isArray(plan.session_guard)
    ? plan.session_guard.filter((line): line is string => typeof line === "string" && line.trim().length > 0)
    : [];
  const isShell = plan.via === "mysqlsh";
  const fullScript = [...sessionGuard, ...plan.statements].join("\n");
  // Match reviewed_writes.execute: SQL guards share each statement's packet,
  // while ProxySQL Admin and MySQL Shell send the guard as a first statement.
  // The joined review text only reproduces the send in the latter two cases.
  const expectEngine = plan.expect && typeof plan.expect === "object"
    ? String((plan.expect as { engine?: unknown }).engine || "") : "";
  const packetGuard = sessionGuard.length > 0 && !isShell &&
    ["mysql", "percona", "percona_server", "pxc", "mariadb"].includes(expectEngine);
  const guardLines = `first ${sessionGuard.length === 1 ? "line" : `${sessionGuard.length.toLocaleString()} lines`} below`;
  const commandsLabel = packetGuard
    ? "Reviewed statements with the identity check first, for reading; the check travels in the same packet as each statement, so this is not a script to paste"
    : isShell
      ? "Identity check and MySQL Shell call, exactly as sent"
      : "Identity check sent first, before the statements, and statements to send, exactly as sent";
  return (
    <ConfirmSheet className="ops-confirm action-review"
      band={<ConnectionTypeBand connection={p.connection} label={p.label} type={type} readOnly={readOnly} />}
      eyebrow={plan.eyebrow} title={title}
      description={run ? undefined : plan.description}
      busy={busy} confirmTone={plan.policy?.tone || "danger"} confirmDisabled={confirmDisabled}
      busyLabel="Sending…" progress="Waiting for the server to run the statements" onLeave={onLeave}
      leaveText="Close keeps the request running. Its outcome appears in the view; nothing is sent again."
      confirmLabel={plan.confirm_label} onCancel={onCancel} onConfirm={onConfirm}
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
        {applying && run.progress?.output && run.progress.output.length > 0 && (
          <LiveShellOutput chunks={run.progress.output} />
        )}
        {applying && <p className="hint">Sent once, never again. If the reply takes longer, Close lets you follow the outcome in the view.</p>}
        {r?.effect && r.effect.length > 0 && <EffectTable rows={r.effect} />}
        {r?.verification && <VerificationSection verification={r.verification} />}
        {r && <ShellActionOutput result={r} />}
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
        {sessionGuard.length > 0 ? (
          <>
            {isShell && <>
              <p className="ops-plan-note">MySQL Shell call (runs after the identity check below):</p>
              <pre className="ops-pre" tabIndex={0} aria-label="MySQL Shell call">
                <code>{plan.statements.join("\n")}</code>
              </pre>
            </>}
            <Disclosure
              title="Identity check"
              open={guardOpen}
              onToggle={setGuardOpen}
              className="action-guard"
            >
              <p className={isShell ? "hint" : "hint ops-plan-note action-guard-note"}>
                {isShell
                  ? "Sent first on the same MySQL Shell session before the call above. Checks the server and AdminAPI identity and defines the checked object used by the call. Copy copies the complete script as sent."
                  : packetGuard
                    ? `Identity check (${guardLines}): the backend sends it in the same packet as each statement, so the text below is the reviewed statements for reading, not a script to paste.`
                    : `Identity check sent first (${guardLines}), before the ${plan.statements.length === 1 ? "statement" : "statements"}.`}
              </p>
              <Commands
                label={commandsLabel}
                text={fullScript}
              />
            </Disclosure>
          </>
        ) : (
          <Commands label="Statements to send, exactly as sent" text={plan.statements.join("\n")} />
        )}
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

function ValueSheet({ connection, ask, onCancel, onReview }: { connection: Connection; ask: Ask; onCancel: () => void; onReview: (value: number) => void }) {
  const [text, setText] = useState(ask.current === null ? "" : String(ask.current));
  const id = useId();
  const value = /^\d+$/.test(text.trim()) ? Number(text.trim()) : NaN;
  const valid = Number.isSafeInteger(value) && value >= ask.min && value <= ask.max && value !== ask.current;
  return (
    <ConfirmSheet className="ops-confirm action-value" band={<ConnectionTypeBand connection={connection} />}
      title={ask.title} confirmTone="primary" confirmLabel="Review the change…"
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
  const [tone, word] = resultTone(r);
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
      {r.verification && <VerificationSection verification={r.verification} />}
      <ShellActionOutput result={r} />
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
