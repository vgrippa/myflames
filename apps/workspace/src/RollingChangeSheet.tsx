// One reviewed, rolling variable change over the saved members of a
// connection group (myflames/topology_change.py; contracts in
// docs/schemas/topology-change-plan-1.0.json and topology-change-1.0.json).
// The sheet lists every group member (members that cannot be changed stay
// unticked and disabled with their reason), Review reads the chosen members
// and shows the exact statements in the plan's order, and Apply sends the
// reviewed plan_id and expect, one member at a time. The run itself lives in
// rollingRuns.ts: closing the sheet, switching the group or refreshing the
// Parameters read never loses it, and the Topology place banner reopens the
// sheet. Stop prevents the next member only: a statement already sent is
// never interrupted. Apply never sends without the reviewed plan.
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { recordDeclined, type Connection } from "./api";
import { openAuditLog } from "./ActionReview";
import { Checkbox, ConfirmSheet, StatusPill, type Tone } from "./kit";
import { Commands } from "./OpsShared";
import { clearRollingRun, runTerminal, startRollingRun, stopRollingRun, useRollingRun } from "./rollingRuns";
import {
  planTopologyChange,
  type ParameterRow, type RollingChangeMode, type RollingChangePlan, type RollingPlanMember,
  type TopologyGroupReport, type TopologyMember,
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
const STATEMENT_KIND: Record<RollingChangeMode, string> = {
  global: "SET GLOBAL",
  persist: "SET PERSIST",
  persist_only: "SET PERSIST_ONLY",
};
const STOP_NOTE = "Stop requested. No later server will start; a statement already sent is not interrupted.";
const LOST_NOTE = "The result reply was lost; this is the last state the server reported.";

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

/** The scopes a mode reads and writes; older plan replies name before and
 * after for every one of them. */
function modeScopes(mode: RollingChangeMode): string[] {
  if (mode === "global") return ["global"];
  if (mode === "persist") return ["global", "persisted"];
  return ["persisted"];
}

function scopeLabel(scope: string): string {
  if (scope === "global") return "Global";
  if (scope === "persisted") return "Persisted";
  return scope;
}

/** The scopes this member effectively changes: the keys of its `after` when
 * the plan names them (a MariaDB member in a persist plan holds only
 * `global`), else every scope of the plan's mode. */
function memberScopes(member: RollingPlanMember, mode: RollingChangeMode): string[] {
  if (member.after && typeof member.after === "object") {
    const keys = Object.keys(member.after);
    if (keys.length) return keys;
  }
  return modeScopes(mode);
}

/** The statement kind the member runs: its own effective mode when the plan
 * names it, else the plan's mode. */
function statementKind(member: RollingPlanMember, mode: RollingChangeMode): string {
  return STATEMENT_KIND[member.effective_mode || mode];
}

/** "effect" of a member plan as short lines. The current shape shows its
 * `text` only (never the raw object keys); older shapes still render. */
function effectLines(effect: unknown): string[] {
  if (!effect) return [];
  if (typeof effect === "string") return [effect];
  if (Array.isArray(effect)) return effect.map(text);
  if (typeof effect === "object") {
    const record = effect as Record<string, unknown>;
    if (typeof record.text === "string") return [record.text];
    return Object.values(record).map(text);
  }
  return [text(effect)];
}

type MemberRow = { member: TopologyMember; changeable: boolean; reason: string };

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
  const section = report.parameters;
  // Every group member is listed; a member is changeable when the Parameters
  // read compared it and it is a server (never a proxy or a cache).
  const rows = useMemo<MemberRow[]>(() => {
    const compared = new Set(section?.compared || []);
    const reasons = new Map((section?.not_compared || []).map((n) => [n.profile_id, n.reason]));
    return report.members.map((member) => {
      if (member.role === "proxy" || member.role === "cache") {
        return { member, changeable: false, reason: `A ${member.role} is not changed by a rolling variable change.` };
      }
      if (!compared.has(member.profile_id)) {
        return { member, changeable: false, reason: reasons.get(member.profile_id) || "Not compared in this read." };
      }
      return { member, changeable: true, reason: "" };
    });
  }, [report.members, section]);
  const members = useMemo(() => rows.filter((r) => r.changeable).map((r) => r.member), [rows]);
  const allMaria = members.length > 0 && members.every((m) => m.engine === "mariadb");
  const someMaria = members.some((m) => m.engine === "mariadb");
  const modes = allMaria ? MODES.filter((m) => m.value === "global") : MODES;

  const run = useRollingRun();
  // This sheet's run is the one started from its row; an unfinished run of
  // another row blocks Apply here until it ends.
  const ownRun = run && run.row.name === row.name ? run : null;
  const otherRun = run && !ownRun && !runTerminal(run) ? run : null;
  const [value, setValue] = useState(() => commonValue(row, members.map((m) => m.profile_id)));
  const [mode, setMode] = useState<RollingChangeMode>(allMaria ? "global" : "persist");
  const [ticked, setTicked] = useState<Record<string, boolean> | null>(null);
  const [reviewed, setReviewed] = useState<RollingChangePlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState("");
  const [typed, setTyped] = useState("");
  const [ackExcluded, setAckExcluded] = useState(false);
  const [stopping, setStopping] = useState(false);
  const live = useRef(true);
  const typedId = useId();
  const valueId = useId();
  useEffect(() => () => {
    live.current = false;
  }, []);

  const plan = reviewed || ownRun?.plan || null;
  const applying = !!ownRun && !runTerminal(ownRun);
  const result = ownRun?.result || null;
  const applyError = ownRun?.applyError || null;
  const lostDone = !!ownRun?.lostDone && !result && !applyError;
  const finished = !!(result || applyError || lostDone);
  const progress = ownRun?.progress || null;
  const onChangedRef = useRef(onChanged);
  onChangedRef.current = onChanged;
  useEffect(() => {
    if (result) onChangedRef.current();
  }, [result]);

  // Every comparable member starts ticked: the displayed value can be stale
  // and the persisted value can differ, so the fresh plan, not the last read,
  // marks members unchanged.
  const isTicked = (m: TopologyMember) => ticked?.[m.profile_id] ?? true;
  const chosen = members.filter(isTicked);
  // Group members the change will not touch: unticked or not changeable.
  const excluded = useMemo(() => {
    const inPlan = plan ? new Set(plan.members.map((m) => m.profile_id)) : null;
    return rows.filter((r) => (inPlan ? !inPlan.has(r.member.profile_id) : !(r.changeable && isTicked(r.member))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, plan, ticked]);
  const excludedNames = excluded.map((r) => r.member.profile_name).join(", ");

  async function review() {
    if (planning || applying || !chosen.length) return;
    setPlanning(true);
    setPlanError("");
    try {
      const answer = await planTopologyChange(connection, row.name, value, mode, chosen.map((m) => m.profile_id));
      if (!live.current) return;
      setReviewed(answer);
      setTyped("");
      setAckExcluded(false);
    } catch (error) {
      if (live.current) setPlanError((error as Error).message);
    } finally {
      if (live.current) setPlanning(false);
    }
  }

  function applyNow(reviewedPlan: RollingChangePlan) {
    if (applying || otherRun) return;
    startRollingRun(connection, report, row, reviewedPlan,
      reviewedPlan.confirmation.typed_name !== null ? typed : undefined);
  }

  async function stop() {
    if (stopping || !ownRun || runTerminal(ownRun)) return;
    setStopping(true);
    try {
      await stopRollingRun();
    } finally {
      if (live.current) setStopping(false);
    }
  }

  function closeSheet() {
    if (ownRun && runTerminal(ownRun)) clearRollingRun();
    onClose();
  }

  function cancel() {
    if (planning) return;
    // An unfinished run keeps going under the Topology place banner; Show
    // reopens this sheet. A reviewed plan that was never applied is a
    // declined attempt in the Audit log.
    if (applying) {
      onClose();
      return;
    }
    if (plan && !finished && !ownRun) {
      const statements = plan.members.map((m) => m.statement).filter((s): s is string => !!s);
      void recordDeclined("topology.variable", statements, connection,
        { group_name: plan.group_name, name: plan.name, mode: plan.mode });
    }
    closeSheet();
  }

  function reviewAgain() {
    if (ownRun) clearRollingRun();
    setReviewed(null);
    setTyped("");
    setAckExcluded(false);
  }

  const refusedCount = plan ? plan.members.filter((m) => m.status === "refused").length : 0;
  const typedName = plan?.confirmation.typed_name ?? null;
  const confirmDisabled = finished ? false
    : plan ? refusedCount > 0 || (typedName !== null && typed !== typedName)
        || (excluded.length > 0 && !ackExcluded) || !!otherRun
      : chosen.length === 0;
  const busy = planning || applying;
  const title = plan
    ? excluded.length
      ? `Change ${row.name} on ${plan.members.length} of ${report.members.length} members`
      : `Change ${row.name} on ${plan.members.length} ${plan.members.length === 1 ? "member" : "members"}`
    : `Change ${row.name} on every member?`;

  return (
    <ConfirmSheet
      className="rolling-change-sheet"
      eyebrow={plan ? STATEMENT_KIND[plan.mode] : "ROLLING CHANGE"}
      title={title}
      description={plan ? undefined
        : `One value for the selected members of ${group}, one server at a time, with one review before anything is sent.`}
      busy={busy}
      busyLabel={applying ? "Applying…" : "Reading the members…"}
      progress={progress?.message || "Applying the reviewed change, one member at a time"}
      confirmLabel={plan ? "Apply" : "Review"}
      confirmTone={plan ? "danger" : "primary"}
      confirmDisabled={confirmDisabled}
      onCancel={finished ? closeSheet : cancel}
      onConfirm={() => {
        if (plan && !finished) applyNow(plan);
        else if (!plan) void review();
      }}
      doneLabel={finished ? "Close" : undefined}
      secondary={result?.status === "stale" ? { label: "Review again", onClick: reviewAgain }
        : plan && !finished ? { label: "Back", onClick: () => { setReviewed(null); setTyped(""); } }
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
            {rows.map(({ member: m, changeable, reason }) => (
              <Checkbox key={m.profile_id} checked={changeable && isTicked(m)} disabled={busy || !changeable}
                onChange={(next) => setTicked((current) => ({ ...current, [m.profile_id]: next }))}>
                {m.profile_name} ({m.host}:{m.port})
                {changeable
                  ? <span className="hint"> now {text(row.values[m.profile_id])}</span>
                  : <span className="hint rolling-member-excluded"> {reason}</span>}
              </Checkbox>
            ))}
          </fieldset>
          {chosen.length === 0 && <p className="hint">None of the changeable members is chosen.</p>}
          {otherRun && (
            <p className="hint">A rolling change is already running: {otherRun.plan.name}. Its sheet opens from the Topology place banner.</p>
          )}
          {planning && <p className="hint" role="status">Reading the members and planning the change…</p>}
          {planError && <p className="rolling-problem" role="alert">{planError}</p>}
        </>
      )}
      {plan && !finished && (
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
                  {m.statement && (
                    <div className="rolling-statement">
                      <span className="rolling-statement-kind">{statementKind(m, plan.mode)}</span>
                      <Commands label={`Statement for ${m.profile_name}`} text={m.statement} />
                    </div>
                  )}
                  {m.status !== "refused" && (m.before || m.after) && (
                    <p className="rolling-before-after">
                      {memberScopes(m, plan.mode).map((scope) => (
                        <span key={scope}>
                          {scopeLabel(scope)}: {text(m.before?.[scope])} → {text(m.after?.[scope])}
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
          {excluded.length > 0 && (
            <div className="rolling-excluded">
              <p className="hint">Not changed: {excludedNames}.</p>
              <Checkbox checked={ackExcluded} disabled={busy} onChange={setAckExcluded}>
                I understand {excludedNames} will not be changed.
              </Checkbox>
            </div>
          )}
          {otherRun && (
            <p className="rolling-problem" role="alert">A rolling change is already running: {otherRun.plan.name}. Apply waits until it ends.</p>
          )}
          {applying && (
            <div className="rolling-applying">
              <p className="rolling-phase" role="status" aria-live="polite">
                <span className="spinner" aria-hidden="true" />
                {progress?.message || `Applying on ${plan.members[0]?.profile_name || "the first member"}…`}
              </p>
              <button type="button" className="button" disabled={stopping || !!ownRun?.stopAcked} onClick={() => void stop()}>
                {stopping ? "Stopping…" : "Stop"}
              </button>
            </div>
          )}
          {ownRun?.stopAcked && <p className="rolling-warning" role="status">{STOP_NOTE}</p>}
          {ownRun?.stopError && <p className="rolling-problem" role="alert">{ownRun.stopError}</p>}
          {!applying && (
            <p className="hint">Before each member, myflames reads it again and sends nothing if it changed. A failure stops the change before the next member.</p>
          )}
        </>
      )}
      {finished && (
        <div className="rolling-result">
          {applyError ? (
            <p className="rolling-problem" role="alert">{applyError}</p>
          ) : lostDone ? (
            <>
              <p className="rolling-warning" role="status">{LOST_NOTE}</p>
              {progress?.message && <p className="hint">{progress.message}</p>}
              <p className="hint rolling-audit">
                Each member's attempt is one audit record.{" "}
                <button type="button" className="text-button" onClick={() => openAuditLog(null)}>
                  Open the Audit log
                </button>
              </p>
            </>
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
                      <span className="rolling-review-address">{m.host}:{m.port}</span>{" "}
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
