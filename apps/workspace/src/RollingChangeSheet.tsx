// One reviewed, rolling variable change over the saved members of a
// connection group (myflames/topology_change.py; contracts in
// docs/schemas/topology-change-plan-1.0.json and topology-change-1.0.json).
// The sheet asks for the value, the mode and the members; Review reads every
// member and shows the exact statements in the plan's order; Apply sends the
// reviewed plan_id and expect, one member at a time, and polls
// /api/live/action/progress about once a second while the sheet is open.
// Stop prevents the next member only: a statement already sent is never
// interrupted. Apply never sends without the reviewed plan.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { recordDeclined, type Connection } from "./api";
import { openAuditLog } from "./ActionReview";
import { Checkbox, ConfirmSheet, StatusPill, writeFailure, type Tone } from "./kit";
import { Commands } from "./OpsShared";
import {
  applyTopologyChange, planTopologyChange, rollingChangeProgress, stopTopologyChange,
  type ParameterRow, type RollingChangeMode, type RollingChangePlan, type RollingChangeResult,
  type RollingProgress, type TopologyGroupReport, type TopologyMember,
} from "./topologyApi";

const MODES: { value: RollingChangeMode; label: string; hint: string }[] = [
  { value: "global", label: "This server now (SET GLOBAL)", hint: "Changes the running value until the server restarts." },
  { value: "persist", label: "Now and after restart (SET PERSIST)", hint: "Changes the running value and keeps it across restarts." },
  { value: "persist_only", label: "After restart only (SET PERSIST_ONLY)", hint: "Writes only the persisted setting; the running value changes at the next restart." },
];

const PLAN_STATUS: Record<string, [Tone, string]> = {
  planned: ["info", "Planned"],
  unchanged: ["ok", "Unchanged"],
  refused: ["problem", "Refused"],
};
const RESULT_STATUS: Record<string, [Tone, string]> = {
  applied: ["ok", "Applied"],
  unchanged: ["ok", "Unchanged"],
  refused: ["problem", "Refused"],
  not_sent: ["unknown", "Not sent"],
  unknown: ["watch", "Outcome unknown"],
};
const OVERALL_STATUS: Record<string, [Tone, string]> = {
  applied: ["ok", "Applied"],
  partial: ["problem", "Partially applied"],
  refused: ["unknown", "Refused"],
  stopped: ["watch", "Stopped"],
  stale: ["watch", "Stale review"],
};
const STOP_NOTE = "Stop requested. No later server will start; a statement already sent is not interrupted.";
const POLL_MS = 1000;

function newProgressId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** The most common current value among the compared members; ties keep the
 * first member's value. */
function commonValue(row: ParameterRow, ids: string[]): string {
  const counts = new Map<string, number>();
  for (const id of ids) {
    const value = row.values[id];
    if (value === undefined || value === null) continue;
    const key = String(value);
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  let best = "";
  let bestCount = 0;
  for (const [key, count] of counts) {
    if (count > bestCount) {
      best = key;
      bestCount = count;
    }
  }
  return best;
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** The scopes a mode reads and writes, as the plan's before and after name them. */
function modeScopes(mode: RollingChangeMode): [string, string][] {
  if (mode === "global") return [["global", "Global"]];
  if (mode === "persist") return [["global", "Global"], ["persisted", "Persisted"]];
  return [["persisted", "Persisted"]];
}

/** "effect" of a member plan as short lines (its shape is the backend's). */
function effectLines(effect: unknown): string[] {
  if (!effect) return [];
  if (typeof effect === "string") return [effect];
  if (Array.isArray(effect)) return effect.map(text);
  if (typeof effect === "object") return Object.entries(effect as Record<string, unknown>).map(([key, value]) => `${key}: ${text(value)}`);
  return [text(effect)];
}

export function RollingChangeSheet({ connection, report, row, onClose, onChanged }: {
  /** The Group connection in use (group_name and active member profile id). */
  connection: Connection;
  /** The Parameters read the row comes from. */
  report: TopologyGroupReport;
  row: ParameterRow;
  onClose: () => void;
  /** An apply answered, whatever its status: the tab offers Refresh. */
  onChanged: () => void;
}) {
  const group = connection.group_name || report.group_name;
  const compared = useMemo(() => report.parameters?.compared || [], [report]);
  // Members the change can target: compared servers only, proxies absent.
  const members = useMemo(
    () => compared
      .map((id) => report.members.find((m) => m.profile_id === id))
      .filter((m): m is TopologyMember => !!m && m.role !== "proxy" && m.role !== "cache"),
    [compared, report.members],
  );
  const allMaria = members.length > 0 && members.every((m) => m.engine === "mariadb");
  const someMaria = members.some((m) => m.engine === "mariadb");
  const modes = allMaria ? MODES.filter((m) => m.value === "global") : MODES;

  const [value, setValue] = useState(() => commonValue(row, members.map((m) => m.profile_id)));
  const [mode, setMode] = useState<RollingChangeMode>(allMaria ? "global" : "persist");
  const [ticked, setTicked] = useState<Record<string, boolean> | null>(null);
  const [plan, setPlan] = useState<RollingChangePlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState("");
  const [typed, setTyped] = useState("");
  const [applying, setApplying] = useState(false);
  const [progress, setProgress] = useState<RollingProgress | null>(null);
  const [stopNote, setStopNote] = useState("");
  const [stopping, setStopping] = useState(false);
  const [result, setResult] = useState<RollingChangeResult | null>(null);
  const [applyError, setApplyError] = useState("");
  const live = useRef(true);
  const progressId = useRef("");
  const typedId = useId();
  const valueId = useId();
  useEffect(() => () => {
    live.current = false;
  }, []);

  // A member whose value already equals the target starts unticked; editing
  // the value recomputes the defaults, a manual tick wins until then.
  const isTicked = (m: TopologyMember) =>
    ticked?.[m.profile_id] ?? String(row.values[m.profile_id] ?? "") !== value;
  const chosen = members.filter(isTicked);

  async function review() {
    if (planning || applying || !chosen.length) return;
    setPlanning(true);
    setPlanError("");
    try {
      const answer = await planTopologyChange(connection, row.name, value, mode, chosen.map((m) => m.profile_id));
      if (!live.current) return;
      setPlan(answer);
      setTyped("");
    } catch (error) {
      if (live.current) setPlanError((error as Error).message);
    } finally {
      if (live.current) setPlanning(false);
    }
  }

  async function apply(plan: RollingChangePlan) {
    if (applying) return;
    const id = newProgressId();
    progressId.current = id;
    setApplying(true);
    setApplyError("");
    setStopNote("");
    setProgress(null);
    let done = false;
    const poll = async () => {
      while (!done && live.current) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
        if (done || !live.current) return;
        try {
          const snap = await rollingChangeProgress(id);
          if (!done && live.current) setProgress(snap);
        } catch {
          // Progress only describes the send; the apply's own reply decides.
        }
      }
    };
    void poll();
    try {
      const answer = await applyTopologyChange(connection, plan, true,
        plan.confirmation.typed_name !== null ? typed : undefined, id);
      if (!live.current) return;
      setResult(answer);
      onChanged();
    } catch (error) {
      if (live.current) setApplyError(writeFailure(error, { audited: true }).message);
    } finally {
      done = true;
      if (live.current) setApplying(false);
    }
  }

  async function stop() {
    if (stopping || !applying || !progressId.current) return;
    setStopping(true);
    try {
      await stopTopologyChange(connection, progressId.current);
      if (live.current) setStopNote(STOP_NOTE);
    } catch (error) {
      if (live.current) setStopNote((error as Error).message);
    } finally {
      if (live.current) setStopping(false);
    }
  }

  function cancel() {
    if (planning || applying) return;
    if (plan && !result) {
      // Cancelling a review is a declined attempt in the Audit log.
      const statements = plan.members.map((m) => m.statement).filter((s): s is string => !!s);
      void recordDeclined("topology.variable", statements, connection,
        { group_name: plan.group_name, name: plan.name, mode: plan.mode });
    }
    onClose();
  }

  function reviewAgain() {
    setPlan(null);
    setResult(null);
    setApplyError("");
    setTyped("");
    setProgress(null);
    setStopNote("");
  }

  const refusedCount = plan ? plan.members.filter((m) => m.status === "refused").length : 0;
  const typedName = plan?.confirmation.typed_name ?? null;
  const confirmDisabled = result || applyError ? false
    : plan ? refusedCount > 0 || (typedName !== null && typed !== typedName)
      : chosen.length === 0;
  const busy = planning || applying;
  const title = plan ? `Change ${row.name} on ${plan.members.length} ${plan.members.length === 1 ? "member" : "members"}`
    : `Change ${row.name} on every member?`;

  return (
    <ConfirmSheet
      className="rolling-change-sheet"
      eyebrow={plan ? { global: "SET GLOBAL", persist: "SET PERSIST", persist_only: "SET PERSIST_ONLY" }[plan.mode] : "ROLLING CHANGE"}
      title={title}
      description={plan ? undefined
        : `One value for the selected members of ${group}, one server at a time, with one review before anything is sent.`}
      busy={busy}
      busyLabel={applying ? "Applying…" : "Reading the members…"}
      progress={progress?.message || "Applying the reviewed change, one member at a time"}
      confirmLabel={plan ? "Apply" : "Review"}
      confirmTone={plan ? "danger" : "primary"}
      confirmDisabled={confirmDisabled}
      onCancel={result || applyError ? onClose : cancel}
      onConfirm={() => {
        if (plan && !result && !applyError) void apply(plan);
        else if (!plan) void review();
      }}
      doneLabel={result || applyError ? "Close" : undefined}
      secondary={result?.status === "stale" ? { label: "Review again", onClick: reviewAgain }
        : plan && !result && !applyError ? { label: "Back", onClick: () => { setPlan(null); setTyped(""); } }
          : undefined}
    >
      {!plan && (
        <>
          <div className="rolling-field">
            <label htmlFor={valueId}>New value</label>
            <input id={valueId} autoComplete="off" spellCheck={false} value={value} disabled={busy}
              onChange={(e) => { setValue(e.target.value); setTicked(null); }} />
            <span className="hint">Prefilled with the most common current value.</span>
          </div>
          <fieldset className="rolling-modes" disabled={busy}>
            <legend>When it applies</legend>
            {modes.map((m) => (
              <label key={m.value}>
                <input type="radio" name="rolling-change-mode" value={m.value} checked={mode === m.value}
                  onChange={() => setMode(m.value)} />
                <span>
                  <strong>{m.label}</strong>
                  <span className="hint">{m.hint}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {someMaria && (
            <p className="hint">MariaDB members have no SET PERSIST: the change there is SET GLOBAL, and the review shows the option-file line that keeps the value after a restart.</p>
          )}
          <fieldset className="rolling-members" disabled={busy}>
            <legend>Members</legend>
            {members.map((m) => (
              <Checkbox key={m.profile_id} checked={isTicked(m)} disabled={busy}
                onChange={(next) => setTicked((current) => ({ ...current, [m.profile_id]: next }))}>
                {m.profile_name} ({m.host}:{m.port})
                <span className="hint"> now {text(row.values[m.profile_id])}</span>
              </Checkbox>
            ))}
          </fieldset>
          {chosen.length === 0 && <p className="hint">Every member already has this value, or none is chosen.</p>}
          {planning && <p className="hint" role="status">Reading the members and planning the change…</p>}
          {planError && <p className="rolling-problem" role="alert">{planError}</p>}
        </>
      )}
      {plan && !result && !applyError && (
        <>
          <p className="rolling-order-rule">{plan.order_rule}</p>
          <ol className="rolling-review-list" aria-label="Members in the order they are changed">
            {plan.members.map((m, index) => {
              const [tone, word] = PLAN_STATUS[m.status] || PLAN_STATUS.refused;
              return (
                <li key={m.profile_id} data-member={m.profile_id} data-status={m.status}>
                  <div className="rolling-review-head">
                    <span className="rolling-review-order" aria-hidden="true">{index + 1}.</span>
                    <strong>{m.profile_name}</strong>
                    <span className="rolling-review-address">{m.host}:{m.port}</span>
                    <StatusPill tone={tone} label={word} />
                  </div>
                  {m.statement && <Commands label={`Statement for ${m.profile_name}`} text={m.statement} />}
                  {m.status !== "refused" && (m.before || m.after) && (
                    <p className="rolling-before-after">
                      {modeScopes(plan.mode).map(([scope, label]) => (
                        <span key={scope}>
                          {label}: {text(m.before?.[scope])} → {text(m.after?.[scope])}
                        </span>
                      ))}
                    </p>
                  )}
                  {effectLines(m.effect).map((line, i) => <p key={i} className="hint">{line}</p>)}
                  {m.caveats.map((line, i) => <p key={i} className="rolling-warning">{line}</p>)}
                  {m.config_line && <Commands label={`Option file line for ${m.profile_name}`} text={m.config_line} />}
                  {m.reason && <p className="rolling-member-reason">{m.reason}</p>}
                </li>
              );
            })}
          </ol>
          {refusedCount > 0 && (
            <p className="rolling-problem" role="alert">Untick the refused servers and review again.</p>
          )}
          {typedName !== null && (
            <label className="rolling-typed" htmlFor={typedId}>
              <span>Type {typedName} to confirm</span>
              <input id={typedId} autoComplete="off" spellCheck={false} value={typed} disabled={busy}
                onChange={(e) => setTyped(e.target.value)} />
            </label>
          )}
          {applying && (
            <div className="rolling-applying">
              <p className="rolling-phase" role="status" aria-live="polite">
                <span className="spinner" aria-hidden="true" />
                {progress?.message || `Applying on ${plan.members[0]?.profile_name || "the first member"}…`}
              </p>
              <button type="button" className="button" disabled={stopping || !!stopNote} onClick={() => void stop()}>
                {stopping ? "Stopping…" : "Stop"}
              </button>
            </div>
          )}
          {stopNote && <p className="rolling-warning" role="status">{stopNote}</p>}
          {!applying && (
            <p className="hint">Before each member, myflames reads it again and sends nothing if it changed. A failure stops the change before the next member.</p>
          )}
        </>
      )}
      {(result || applyError) && (
        <div className="rolling-result">
          {applyError ? (
            <p className="rolling-problem" role="alert">{applyError}</p>
          ) : result && (
            <>
              <div className="rolling-outcome" role="status" data-status={result.status}>
                <StatusPill tone={(OVERALL_STATUS[result.status] || OVERALL_STATUS.stale)[0]}
                  label={(OVERALL_STATUS[result.status] || OVERALL_STATUS.stale)[1]} />
                <p>{result.message}</p>
              </div>
              <ol className="rolling-result-list" aria-label="Result per member">
                {result.members.map((m) => {
                  const [tone, word] = RESULT_STATUS[m.status] || RESULT_STATUS.unknown;
                  return (
                    <li key={m.profile_id} data-member={m.profile_id} data-status={m.status}>
                      <strong>{m.profile_name}</strong>
                      <span className="rolling-review-address">{m.host}:{m.port}</span>
                      <StatusPill tone={tone} label={word} />
                      {m.message && <p className="hint">{m.message}</p>}
                    </li>
                  );
                })}
              </ol>
              <p className="hint rolling-audit">
                One audit record per member.{" "}
                <button type="button" className="text-button" onClick={() => openAuditLog(result.audit_ids[0] || null)}>
                  Open the Audit log
                </button>
              </p>
            </>
          )}
        </div>
      )}
    </ConfirmSheet>
  );
}
