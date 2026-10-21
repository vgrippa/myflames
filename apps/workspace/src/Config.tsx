// Server > Parameters: a searchable catalog with local multi-value drafts,
// preview, and one exact-SQL batch review. Plans are bounded to three reads;
// writes run sequentially and stop at the first unconfirmed outcome. Reports,
// drafts and reviews belong to one connection. The single-variable sheet keeps
// all modes, including the Query editor handoff for Session SQL.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type ConfigCheck, type ConfigReport, type ConfigParameter, type Connection } from "./api";
import { uptime } from "./Health";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useViewOnScreen } from "./viewOnScreen";
import { PrivilegeConfirmLabel, privilegeResult, targetPrivilege, usePrivilege } from "./privileges";
import { useWritePolicy } from "./Connections";
import { CopySql, bytes, engineLabel } from "./PerfShared";
import {
  ConfirmSheet,
  copyToClipboard,
  EmptyState,
  FactStrip,
  Disclosure,
  DataTable,
  Fields,
  StatusDot,
  ToolbarSelect,
  type Column,
  InspectorSection,
  Pill,
  SearchField,
  Select,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  useInspector,
  type InspectorContent,
  type Selection,
} from "./kit";
import "./config.css";

// ---- variables.py contract ----------------------------------------------------------

export type VariableMode = "global" | "persist" | "persist_only" | "session" | "reset_persist";
const MODE_ACTION: Record<VariableMode, string> = { global: "set_global", persist: "set_persist", persist_only: "set_persist_only", session: "set_session_restricted", reset_persist: "reset_persist" };
type ModeInfo = { available: boolean; reason: string | null; recommended: boolean };
/** variable-1.0: one variable as the server reports it. */
export type VariableInfo = {
  schema_version: "variable-1.0";
  engine: "mysql" | "mariadb";
  server_version: string | null;
  name: string;
  exists: boolean;
  scope: "global" | "both" | "session" | null;
  dynamic: boolean | null;
  nullable: boolean;
  read_at: string;
  type: "bytes" | "integer" | "double" | "boolean" | "enum" | "set" | "flagset" | "string" | null;
  values: string[] | null;
  min: number | null;
  max: number | null;
  block_size: number | null;
  global_value: string | null;
  session_value: string | null;
  default: string | null;
  set_by: string | null;
  source: string | null;
  persisted: { supported: boolean; value: string | null; present: boolean | null; load: boolean | null };
  description: string | null;
  notes: string[];
  modes: Record<VariableMode, ModeInfo>;
};
type PersistedState = { state: "present" | "absent" | "unreadable" | "unsupported"; present: boolean | null; value: string | null };
type VariableValues = { global: string | null; session: string | null; persisted: string | null; persisted_state?: PersistedState };
/** variable-plan-1.0: the checked change, its exact statement and what it does. */
export type VariablePlan = {
  schema_version: "variable-plan-1.0";
  engine: "mysql" | "mariadb";
  server_version: string | null;
  name: string;
  mode: VariableMode;
  requested: string | null;
  value: string | null;
  display: string | null;
  status: "ready" | "refused";
  problems: { code: string; message: string }[];
  statement: string | null;
  config_line: string | null;
  before: VariableValues;
  variable: VariableInfo;
  undo: string[];
  effect: { now: boolean; after_restart: boolean; needs_restart: boolean; text: string } | null;
  caveats: string[];
  privileges: { need: string; note: string; checked: boolean; allowed: boolean | null; via: string | null } | null;
  expect: Record<string, unknown> | null;
  alternative: { mode: VariableMode | null; config_line: string | null; text: string } | null;
  policy?: { action: "allow" | "confirm" | "refuse"; tone: "danger" | "primary"; title: string; text: string };
};
/** variable-change-1.0: what was sent and the value read back. */
export type VariableChange = {
  schema_version: "variable-change-1.0";
  name: string;
  mode: VariableMode;
  status: string;
  message: string;
  statement: string | null;
  adjusted: boolean;
  before: VariableValues | null;
  after: (VariableValues & { resize_status?: string | null }) | null;
};

const MODE_ORDER: VariableMode[] = ["global", "persist", "persist_only", "session", "reset_persist"];
const MODES: Record<VariableMode, { label: string; hint: string; verb: string; confirm: string }> = {
  global: { label: "Now only", hint: "SET GLOBAL: until the server restarts.", verb: "SET GLOBAL", confirm: "Set globally" },
  persist: {
    label: "Now and after restart",
    hint: "SET PERSIST: also writes mysqld-auto.cnf.",
    verb: "SET PERSIST",
    confirm: "Set and persist",
  },
  persist_only: {
    label: "After restart only",
    hint: "SET PERSIST_ONLY: the running value stays until a restart.",
    verb: "SET PERSIST_ONLY",
    confirm: "Persist for next restart",
  },
  session: {
    label: "Session",
    hint: "Run in the Query editor to change only that tab’s session.",
    verb: "SET SESSION",
    confirm: "Open in Query editor",
  },
  reset_persist: {
    label: "Remove the persisted value",
    hint: "RESET PERSIST: the running value stays.",
    verb: "RESET PERSIST",
    confirm: "Remove persisted value",
  },
};

/** Hand off the server-generated SQL verbatim, with its statement terminator.
 * This action never calls the apply endpoint. The editor owns execution. */
export function openSessionSql(plan: VariablePlan, onOpenSql?: (sql: string) => void): boolean {
  if (plan.mode !== "session" || plan.status !== "ready" || !plan.statement || !onOpenSql) return false;
  onOpenSql(plan.statement.endsWith(";") ? plan.statement : `${plan.statement};`);
  return true;
}

/** The connection a Change sheet was opened for (Locks' killVia pattern). */
type Via = { connection: Connection; key: string; label: string };
type ChangeRequest = { name: string; mode: VariableMode | null; via: Via; draft?: string };
type ChangeNote = { name: string; mode: VariableMode; status: string; message: string; type: VariableInfo["type"]; result: VariableChange | null };

/** A value as the server shows it; sizes also in KiB, MiB, GiB. */
function valueText(type: VariableInfo["type"], value: string | null | undefined) {
  if (value === undefined) return "not read";
  if (value === null) return "SQL NULL";
  if (value === "") return "(empty)";
  if (value === "NULL") return "'NULL' (text)";
  const n = Number(value);
  if (type === "bytes" && /^\d+$/.test(value) && n >= 1024) return `${bytes(n)} (${n.toLocaleString()})`;
  return value;
}

function persistedText(type: VariableInfo["type"], state: PersistedState | undefined, value: string | null) {
  if (!state) return value === null ? "not read" : valueText(type, value);
  if (state.state === "absent") return "no value";
  if (state.state === "unreadable") return "not readable";
  if (state.state === "unsupported") return "not supported";
  return valueText(type, state.value);
}

function firstMode(info: VariableInfo, wanted: VariableMode | null): VariableMode | null {
  if (wanted && info.modes[wanted]?.available) return wanted;
  const recommended = MODE_ORDER.find((m) => info.modes[m]?.available && info.modes[m]?.recommended);
  return recommended || MODE_ORDER.find((m) => m !== "reset_persist" && info.modes[m]?.available) || null;
}

function startValue(info: VariableInfo, mode: VariableMode | null) {
  const raw = (mode === "session" ? info.session_value : info.global_value) ?? info.session_value ?? "";
  if (info.type === "boolean") return ["1", "ON", "TRUE", "YES"].includes(raw.toUpperCase()) ? "ON" : "OFF";
  if (info.type === "enum" && info.values) return info.values.find((v) => v.toLowerCase() === raw.toLowerCase()) || info.values[0] || raw;
  return raw;
}

function rangeHint(info: VariableInfo) {
  const fmt = (n: number | null) => (n === null ? "?" : info.type === "bytes" ? bytes(n) : n.toLocaleString());
  const range = info.min !== null || info.max !== null ? ` from ${fmt(info.min)} to ${fmt(info.max)}` : "";
  switch (info.type) {
    case "bytes":
      return `Bytes${range}; K, M, G and T are converted. DEFAULT restores the server's default.`;
    case "integer":
      return `A whole number${range}. DEFAULT restores the server's default.`;
    case "double":
      return `A number${range}.`;
    case "set":
      return info.values ? `A comma list of: ${info.values.join(", ")}.` : "A comma list.";
    case "flagset":
      return "A comma list of flag=on or flag=off; flags not named keep their value.";
    case "boolean":
    case "enum":
      return "";
    default:
      return "The server checks the value.";
  }
}

/** The value control for the variable's type. */
function ValueField({ info, value, onChange, id, hintId, disabled, label }: { info: Pick<VariableInfo, "type" | "values">; value: string; onChange: (v: string) => void; id: string; hintId: string; disabled: boolean; label?: string }) {
  if (info.type === "boolean")
    return <button id={id} type="button" role="switch" aria-checked={value === "ON"} aria-label={label} aria-describedby={hintId}
      className="config-switch" disabled={disabled} onClick={() => onChange(value === "ON" ? "OFF" : "ON")}>
      <span className="config-switch-track" aria-hidden="true"><span /></span><span>{value === "ON" ? "On" : "Off"}</span>
    </button>;
  if (info.type === "enum" && info.values?.length)
    return <Select id={id} label={label} value={value} options={info.values.map((v) => [v, v] as [string, string])} onChange={onChange} disabled={disabled} describedBy={hintId} />;
  return (
    <input
      id={id}
      className="variable-input"
      value={value}
      disabled={disabled}
      spellCheck={false}
      autoComplete="off"
      inputMode={info.type === "integer" || info.type === "double" || info.type === "bytes" ? "decimal" : undefined}
      aria-describedby={hintId}
      aria-label={label}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function privilegeText(p: NonNullable<VariablePlan["privileges"]>) {
  if (p.need === "no privilege" && p.allowed === true) return "No privilege needed.";
  const allowed = p.checked === false && p.allowed !== true ? null : p.allowed;
  const state = allowed === true ? (p.via ? `your grants include ${p.via}` : "permission confirmed") : allowed === false ? "your grants do not seem to include it" : "permission could not be verified; the server decides";
  return `${p.need} (${state}).${p.note ? ` ${p.note}` : ""}`;
}

/** The Change sheet: the value, when it applies, and the exact statement,
 * checked by the server (plan) as the inputs settle. Cancel has focus;
 * Cancel and Escape send nothing and are recorded as declined. */
function VariableSheet({
  request,
  activeKey,
  onClose,
  onDone,
  onOpenSql,
}: {
  request: ChangeRequest;
  activeKey: string;
  onClose: () => void;
  onDone: (note: ChangeNote) => void;
  onOpenSql?: (sql: string) => void;
}) {
  const policy = useWritePolicy();
  const [info, setInfo] = useState<VariableInfo | null>(null);
  const [readError, setReadError] = useState("");
  const [mode, setMode] = useState<VariableMode | null>(null);
  const [value, setValue] = useState("");
  const [isNull, setIsNull] = useState(false);
  const [plan, setPlan] = useState<VariablePlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState("");
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);
  const cancelled = useRef(false);
  const name = request.name;
  const { via } = request;

  useEffect(() => {
    const controller = new AbortController();
    api<VariableInfo>("live/variable", { connection: via.connection, name }, controller.signal)
      .then((v) => {
        if (controller.signal.aborted) return;
        const m = firstMode(v, request.mode);
        setInfo(v);
        setMode(m);
        setValue(request.draft ?? startValue(v, m));
        setIsNull(request.draft === undefined && v.nullable && (m === "session" ? v.session_value : v.global_value) === null);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setReadError((e as Error).message);
      });
    return () => controller.abort();
  }, [request, via, name]);

  // Plan the change once the inputs settle; only the latest answer counts.
  // Without an available mode (a read-only MariaDB variable) the plan of
  // SET GLOBAL still says why and gives the option file line.
  const planMode: VariableMode | null = mode ?? (info?.exists ? "global" : null);
  const requestedValue = (planMode ?? request.mode) === "reset_persist" || isNull ? null : value;
  const loaded = !!info;
  useEffect(() => {
    if (!loaded || !planMode) return;
    const id = ++seq.current;
    const controller = new AbortController();
    setPlanning(true);
    setPlanError("");
    const timer = window.setTimeout(() => {
      api<VariablePlan>("live/variable/plan", {
        connection: via.connection,
        name,
        value: requestedValue,
        mode: planMode,
      }, controller.signal)
        .then((p) => {
          if (controller.signal.aborted || seq.current !== id) return;
          setPlan(p);
          setInfo(p.variable);
          setPlanError("");
        })
        .catch((e) => {
          if (controller.signal.aborted || seq.current !== id) return;
          setPlan(null);
          setPlanError((e as Error).message);
        })
        .finally(() => {
          if (!controller.signal.aborted && seq.current === id) setPlanning(false);
        });
    }, 350);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [loaded, planMode, requestedValue, via, name]);

  const current = !!plan && plan.mode === planMode && plan.requested === requestedValue && !planning;
  const generic = usePrivilege(MODE_ACTION[planMode || "global"]);
  // Neither the variable nor an ordinary SET SESSION can be judged by a
  // generic restricted-variable answer. Wait for this mode's exact hint.
  const gate = current && plan?.privileges ? targetPrivilege(generic, plan.privileges) : privilegeResult();
  const ready = current && !!mode && plan?.status === "ready";
  const stale = activeKey !== via.key;
  const refused = current && plan?.policy?.action === "refuse";
  const blocked = policy?.blocked || (via.connection.read_only ? "This connection is read-only." : "");
  const tone = !mode ? "primary" : (current && plan?.policy?.tone) || (mode === "session" ? "primary" : "danger");
  const verb = mode ? MODES[mode].verb : info && info.engine === "mariadb" ? "OPTION FILE" : "SET";
  const valueId = `variable-value-${name}`;
  const hintId = `variable-hint-${name}`;

  function cancel() {
    if (cancelled.current) return;
    cancelled.current = true;
    // Cancelling before a plan arrives is still an attempt. A previous plan
    // belongs to different inputs and must never become the audit evidence.
    const intentMode = planMode ?? request.mode ?? "global";
    if (intentMode === "session") { onClose(); return; }
    const reviewed = current ? plan : null;
    void api<VariableChange>("live/variable/apply", {
      connection: via.connection, name, mode: intentMode, value: requestedValue,
      declined: true, expect: reviewed?.expect ?? null, reviewed_statement: reviewed?.statement ?? null,
    }).catch(() => null);
    onClose();
  }

  async function confirm() {
    if (mode === "session") {
      if (ready && plan && !stale && !busy) openSessionSql(plan, onOpenSql);
      return;
    }
    if (!ready || !plan || !mode || stale || refused || busy || blocked || gate.disabledReason) return;
    setBusy(true);
    const base = { name, mode, type: info?.type ?? null, result: null };
    if (policy) {
      // The type and read-only flag as they are now: the sheet was opened under the old ones.
      try {
        const active = await policy.check();
        if (connectionKey(active) !== via.key) {
          onDone({ ...base, status: "refused", message: "The connection's settings changed after this sheet opened, so nothing was sent. Check them and try again." });
          return;
        }
      } catch (e) {
        onDone({ ...base, status: "error", message: `Could not read the connection's settings, so nothing was sent: ${(e as Error).message}` });
        return;
      }
    }
    try {
      const result = await api<VariableChange>("live/variable/apply", {
        connection: via.connection,
        name,
        mode,
        value: requestedValue,
        expect: plan.expect,
        confirmed: true,
      });
      onDone({ ...base, status: result.status, message: result.message, result });
    } catch (e) {
      onDone({ ...base, status: "error", message: (e as Error).message });
    }
  }

  const modes = info ? MODE_ORDER.filter((m) => info.modes[m]?.available) : [];
  const confirmLabel = busy ? "Sending…" : mode ? MODES[mode].confirm : "No change at runtime";
  let lock = "";
  if (stale) lock = "The connection in use changed after this sheet opened. Close it and open Review change again.";
  else if (mode === "session") lock = !onOpenSql ? "Query editor is unavailable. Copy the statement to your query tab." : "";
  else if (blocked) lock = blocked;
  else if (refused) lock = plan?.policy?.text || "This connection is read-only in myflames.";

  return (
    <ConfirmSheet
      className="variable-sheet"
      eyebrow={verb}
      title={mode === "reset_persist" ? `Remove ${name} from mysqld-auto.cnf?` : info && !mode && info.exists ? `Change ${name} at the next restart?` : `Change ${name}?`}
      description={
        mode === "session" ? "Open this statement in the current Query editor tab without running it." : info && !mode && info.exists
          ? "This setting changes only when the server starts, so myflames has nothing to send."
          : "Check the statement before you send it. Right before sending, myflames reads the variable again and stops if it changed."
      }
      busy={busy}
      // Nothing to send (no mode): no privilege applies, so no gate on the label.
      confirmLabel={mode && mode !== "session" ? <PrivilegeConfirmLabel gate={gate} showStatus={false}>{confirmLabel}</PrivilegeConfirmLabel> : confirmLabel}
      confirmTone={mode === "session" ? "primary" : tone}
      confirmDisabled={!ready || stale || (mode === "session" ? !onOpenSql : refused || !!blocked)}
      onCancel={cancel}
      onConfirm={() => void confirm()}
    >
      {!info && !readError && (
        <p className="hint" role="status">
          Reading {name}…
        </p>
      )}
      {readError && (
        <p className="variable-problems" role="alert">
          {readError}
        </p>
      )}
      {info && (
        <>
          <dl className="variable-facts">
            <div>
              <dt>Server</dt>
              <dd>
                {via.label} · {engineLabel(info)}
              </dd>
            </div>
            {current && plan && (
              <>
                <div>
                  <dt>{planMode === "session" ? "Planning session value" : "Global before"}</dt>
                  <dd>{valueText(info.type, planMode === "session" ? plan.before.session : plan.before.global)}</dd>
                </div>
                {planMode !== "session" && (info.set_by || info.source) && (
                  <div><dt>Global source</dt><dd>{info.set_by || info.source}{info.set_by && info.source ? ` (${info.source})` : ""}</dd></div>
                )}
                {info.persisted.supported && planMode !== "session" && (
                  <div>
                    <dt>mysqld-auto.cnf</dt>
                    <dd>{persistedText(info.type, plan.before.persisted_state, plan.before.persisted)}</dd>
                  </div>
                )}
                <div><dt>Read at</dt><dd>{info.read_at ? <time dateTime={info.read_at}>{info.read_at}</time> : "not recorded"}</dd></div>
              </>
            )}
          </dl>
          {modes.length > 0 ? (
            <fieldset className="variable-modes" disabled={busy}>
              <legend>When it applies</legend>
              {modes.map((m) => (
                <label key={m}>
                  <input type="radio" name={`variable-mode-${name}`} value={m} checked={mode === m} onChange={() => setMode(m)} />
                  <span>
                    <strong>{MODES[m].label}</strong>
                    <span className="hint">{MODES[m].hint}</span>
                  </span>
                </label>
              ))}
            </fieldset>
          ) : (
            info.exists && (
              <p className="variable-note" role="note">
                {info.modes.global.reason} It changes only at a restart.
              </p>
            )
          )}
          {request.mode === "reset_persist" && !info.modes.reset_persist?.available && (
            <p className="variable-note" role="note">
              {info.modes.reset_persist?.reason} The value was loaded from it at startup; after a RESET PERSIST the
              source stays until the next restart.
            </p>
          )}
          {info.engine === "mariadb" && modes.length > 0 && (
            <p className="hint variable-note">MariaDB has no SET PERSIST: to keep a value after a restart, copy the option file line below.</p>
          )}
          {info.exists && mode !== "reset_persist" && (
            <div className="variable-field">
              <label htmlFor={valueId}>New value</label>
              <ValueField info={info} value={value} onChange={setValue} id={valueId} hintId={hintId} disabled={busy || isNull} />
              {info.nullable && (
                <label><input type="checkbox" checked={isNull} disabled={busy} onChange={(e) => setIsNull(e.target.checked)} /> Set SQL NULL</label>
              )}
              <span id={hintId} className="hint">
                {rangeHint(info)}
                {current && plan?.display && plan.display !== value ? ` Sends ${plan.display}.` : ""}
              </span>
            </div>
          )}
          {planning && (
            <p className="hint" role="status">
              Checking with the server…
            </p>
          )}
          {planError && (
            <p className="variable-problems" role="alert">
              {planError}
            </p>
          )}
          {current && plan && <PlanDetails plan={plan} onUseMode={(m) => setMode(m)} canUse={(m) => modes.includes(m)} />}
          {lock && (
            <p className="variable-problems" role="alert">
              {lock}
            </p>
          )}
          {current && mode && mode !== "session" && plan?.policy && !refused && <p className="hint variable-policy">{plan.policy.text}</p>}
        </>
      )}
    </ConfirmSheet>
  );
}

function PlanDetails({ plan, onUseMode, canUse }: { plan: VariablePlan; onUseMode: (m: VariableMode) => void; canUse: (m: VariableMode) => boolean }) {
  const alt = plan.alternative;
  // Without a mode that can change it now, the sheet already says why; the alternative says what to do.
  const problems = plan.problems.filter((p) => !(alt && p.code === "mode_unavailable" && !canUse(plan.mode)));
  return (
    <div className="variable-plan">
      {problems.length > 0 && (
        <ul className="variable-problems" role="alert">
          {problems.map((p) => (
            <li key={p.code + p.message}>{p.message}</li>
          ))}
        </ul>
      )}
      {alt && (
        <div className="variable-alternative">
          <p>{alt.text}</p>
          {alt.mode && canUse(alt.mode) && (
            <button type="button" className="text-button" onClick={() => onUseMode(alt.mode as VariableMode)}>
              Use {MODES[alt.mode].label}
            </button>
          )}
          {alt.config_line && <CopySql sql={alt.config_line} label={`Option file line for ${plan.name}`} />}
        </div>
      )}
      {(plan.privileges || plan.statement) && (
        <dl className="variable-facts variable-permissions">
          <div>
            <dt>Permissions</dt>
            <dd>{plan.privileges ? privilegeText(plan.privileges) : "Permission could not be verified. The server decides whether this change is allowed."}</dd>
          </div>
        </dl>
      )}
      {plan.status === "ready" && plan.statement && (
        <>
          <span className="variable-label">Statement</span>
          <pre className="variable-statement" tabIndex={0} aria-label="The exact statement">
            <code>{plan.statement};</code>
          </pre>
          <dl className="variable-facts">
            {plan.effect && (
              <div>
                <dt>Effect</dt>
                <dd>{plan.mode === "session" ? "Run this statement in the Query editor to affect that tab’s session." : plan.effect.text}</dd>
              </div>
            )}
            {plan.mode !== "session" && plan.undo.length > 0 && (
              <div>
                <dt>Undo</dt>
                <dd>
                  <code>{plan.undo.join(" ")}</code>
                </dd>
              </div>
            )}
          </dl>
          {plan.caveats.length > 0 && (
            <ul className="variable-caveats">
              {plan.caveats.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          )}
          {plan.engine === "mariadb" && plan.config_line && plan.mode !== "session" && (
            <CopySql sql={plan.config_line} label={`Option file line for ${plan.name}`} />
          )}
        </>
      )}
    </div>
  );
}

const HEADLINE: Record<string, string> = {
  changed: "Changed.",
  applied_unverified: "Applied; verification incomplete.",
  unchanged: "Not applied.",
  denied: "The server refused.",
  refused_by_server: "The server refused.",
  interrupted: "Interrupted.",
  failed: "Could not change it.",
  error: "Could not change it.",
};

/** The answer to a change, announced and focused, with before and after. */
function ChangeResultNote({ note, onDismiss, noteRef }: { note: ChangeNote; onDismiss: () => void; noteRef: React.RefObject<HTMLDivElement> }) {
  const r = note.result;
  const rows: [string, string, string][] = [];
  if (r && r.before && r.after) {
    if (note.mode === "global" || note.mode === "persist") rows.push(["Global", valueText(note.type, r.before.global), valueText(note.type, r.after.global)]);
    if (note.mode === "session") rows.push(["Session", valueText(note.type, r.before.session), valueText(note.type, r.after.session)]);
    if (note.mode !== "global" && note.mode !== "session") rows.push(["mysqld-auto.cnf", persistedText(note.type, r.before.persisted_state, r.before.persisted), persistedText(note.type, r.after.persisted_state, r.after.persisted)]);
  }
  const tone = note.status === "changed" ? "ok" : note.status === "denied" || note.status === "error" || note.status === "failed" ? "danger" : "warn";
  // The rows already show before and after; the message adds only what they cannot (a rounding, a session, a reset).
  const plain = note.status === "changed" && !r?.adjusted && rows.length > 0 && ["global", "persist", "persist_only"].includes(note.mode);
  return (
    <div ref={noteRef} tabIndex={-1} className={`variable-result is-${tone}`} role={note.status === "changed" ? "status" : "alert"} data-status={note.status}>
      <p>
        <strong>{HEADLINE[note.status] || "Nothing was sent."}</strong> {plain ? `${note.name}, ${MODES[note.mode].label.toLowerCase()}.` : note.message}
      </p>
      {rows.length > 0 && (
        <dl className="variable-facts">
          {rows.map(([label, before, after]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd>
                {before} → {after}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {r?.after?.resize_status && <p className="hint">Resize: {r.after.resize_status}</p>}
      <button type="button" className="text-button" onClick={onDismiss}>
        Dismiss
      </button>
    </div>
  );
}

/** Older reports contain only checked settings. Unknown catalog fields stay
 * unknown; the single-variable sheet can still read and review their modes. */
function parametersOf(report: ConfigReport): ConfigParameter[] {
  if (report.parameters) return report.parameters.items;
  const rows = new Map<string, ConfigParameter>();
  for (const check of report.checks) for (const setting of check.settings) {
    const previous = rows.get(setting.name);
    if (previous) {
      if (!previous.checks.includes(check.key)) previous.checks.push(check.key);
      if (!previous.description && check.settings[0]?.name === setting.name) previous.description = check.explain;
      continue;
    }
    const dynamic = setting.dynamic;
    rows.set(setting.name, {
      name: setting.name, value: setting.value, truncated: false, scope: null,
      apply_type: dynamic === true ? "dynamic" : dynamic === false ? "static" : null,
      // A static legacy row does not establish whether MySQL permits persistence.
      modifiable: dynamic === true ? "now" : dynamic === false && report.engine === "mariadb" ? "option_file" : null,
      type: null, values: null, min: null, max: null, block_size: null,
      default: setting.default, source: setting.set_by, source_label: setting.set_by,
      source_path: null, set_time: null, set_user: null, set_host: null,
      persisted: report.engine === "mysql" && setting.set_by === "SET PERSIST" ? true : null,
      modified: setting.default !== null && setting.display !== null ? setting.default !== setting.display : null,
      // The check describes its first setting; oneLine falls back the same way.
      description: check.settings[0]?.name === setting.name ? check.explain : null, checks: [check.key],
    });
  }
  return [...rows.values()];
}

function allowedValues(p: ConfigParameter) {
  if (p.type === "boolean") return "OFF, ON";
  if (p.type === "enum" || p.type === "set" || p.type === "flagset") return p.values?.join(", ") || "";
  if (p.type === "string") return "Any text";
  if (["bytes", "integer", "double"].includes(p.type || "") && (p.min !== null || p.max !== null)) {
    const fmt = (n: number | null) => n === null ? "?" : p.type === "bytes" ? bytes(n) : n.toLocaleString();
    return `${fmt(p.min)} to ${fmt(p.max)}`;
  }
  return "";
}
function applyType(p: ConfigParameter) { return p.apply_type === "dynamic" ? "Dynamic" : p.apply_type === "static" ? "Static" : "Unknown"; }
function sourceDetails(p: ConfigParameter) {
  return [p.source_path, p.set_user || p.set_host ? `${p.set_user || "?"}@${p.set_host || "?"}${p.set_time ? ` at ${p.set_time}` : ""}` : p.set_time].filter(Boolean).join(" · ");
}
function unmodifiable(p: ConfigParameter, engine: string | null) {
  if (p.truncated) return "The current value was truncated. Use Change to read the full value before reviewing a change.";
  if (p.modifiable === "none") return "Set by the server; no option or statement changes it.";
  if (p.modifiable === "option_file" || (engine === "mariadb" && p.apply_type === "static")) return "Option file only; restart the server after changing the option file.";
  if (p.modifiable === "now" || (engine === "mysql" && p.modifiable === "restart")) return "";
  return "myflames cannot tell whether it can change while running.";
}
function draftValue(p: ConfigParameter) {
  if (p.type === "boolean") return ["1", "ON", "TRUE", "YES"].includes((p.value || "").toUpperCase()) ? "ON" : "OFF";
  if (p.type === "enum") return p.values?.find(v => v.toLowerCase() === (p.value || "").toLowerCase()) || p.value || "";
  return p.value ?? "";
}
/** SET GLOBAL and SET PERSIST change the global value; connected sessions keep their copy. */
function whenApplies(p: ConfigParameter) {
  if (p.modifiable === "restart") return "Applies after the next restart";
  return p.scope === "both" ? "Applies now to new sessions; existing sessions keep their value" : "Applies now";
}

type StagedParameter = { parameter: ConfigParameter; value: string };
type BatchRequest = { rows: StagedParameter[]; via: Via; engine: string | null };
type BatchResult = { name: string; status: string; message: string; result?: VariableChange; type?: VariableInfo["type"] };
type BatchPlans = { signature: string; plans: Record<string, VariablePlan>; errors: Record<string, string> };
const BATCH_LIMIT = 25;

type ParameterIntent = { name: string; value: string; mode: VariableMode };

/** Bound concurrent preflight reads without starting queued work after abort. */
async function planParameterBatch(intents: ParameterIntent[], signal: AbortSignal,
  read: (intent: ParameterIntent) => Promise<VariablePlan>,
  accept: (name: string, plan: VariablePlan) => void, reject: (name: string, message: string) => void,
) {
  let next = 0;
  async function worker() {
    while (!signal.aborted && next < intents.length) {
      const intent = intents[next++];
      try {
        const plan = await read(intent);
        if (!signal.aborted) accept(intent.name, plan);
      } catch (e) {
        if (!signal.aborted) reject(intent.name, (e as Error).message);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, intents.length) }, () => worker()));
}

/** Policy is re-read once per confirmed batch; the server re-verifies each
 * plan's expect immediately before its individual statement. Never retry. */
async function applyParameterBatch(rows: StagedParameter[], intents: ParameterIntent[], via: Via,
  plans: Record<string, VariablePlan>, check: (() => Promise<Connection>) | undefined,
  currentKey: () => string, apply: (intent: ParameterIntent, plan: VariablePlan) => Promise<VariableChange>,
): Promise<BatchResult[]> {
  const results: BatchResult[] = rows.map(r => ({ name: r.parameter.name, status: "not_sent", message: "Not sent because an earlier change stopped the batch." }));
  try {
    if (check && connectionKey(await check()) !== via.key) throw new Error("The connection's settings changed after this sheet opened.");
    if (currentKey() !== via.key) throw new Error("The connection in use changed.");
  } catch (e) {
    results.forEach(r => { r.message = `Could not confirm the connection's settings; nothing was sent. ${(e as Error).message}`; });
    return results;
  }
  for (let i = 0; i < intents.length; i++) {
    const intent = intents[i];
    if (currentKey() !== via.key) {
      results[i].message = "The connection in use changed; this change was not sent.";
      break;
    }
    try {
      const result = await apply(intent, plans[intent.name]);
      results[i] = { name: intent.name, status: result.status, message: result.message, result, type: rows[i].parameter.type };
      if (result.status !== "changed") break;
    } catch (e) {
      // The statement may have run before the reply was lost: never call it unchanged.
      results[i] = { name: intent.name, status: "unknown", message: `The reply did not arrive, so the outcome is unknown. Check the value before trying again. ${(e as Error).message}` };
      break;
    }
  }
  return results;
}

/** One review is a bounded set of server-generated plans. Changing the scope
 * invalidates all earlier plans immediately, including their audit evidence. */
function ParameterBatchSheet({ request, activeKey, onClose, onRemove, onDone }: {
  request: BatchRequest; activeKey: string; onClose: () => void;
  onRemove: (name: string) => void; onDone: (results: BatchResult[]) => void;
}) {
  const policy = useWritePolicy();
  const [rows, setRows] = useState(request.rows);
  const [persist, setPersist] = useState(true);
  const [answers, setAnswers] = useState<BatchPlans>({ signature: "", plans: {}, errors: {} });
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const cancelled = useRef(false);
  const latestKey = useRef(activeKey);
  latestKey.current = activeKey;
  const modeOf = (p: ConfigParameter): VariableMode => p.modifiable === "restart" ? "persist_only" : request.engine === "mysql" && persist ? "persist" : "global";
  const intents = rows.map(r => ({ name: r.parameter.name, value: r.value, mode: modeOf(r.parameter) }));
  const signature = JSON.stringify(intents);
  const current = answers.signature === signature;
  const plans = current ? answers.plans : {};
  const errors = current ? answers.errors : {};

  useEffect(() => {
    if (rows.length > BATCH_LIMIT) return;
    const controller = new AbortController();
    setAnswers({ signature, plans: {}, errors: {} });
    // Each response belongs to this exact set of values, modes and connection.
    void planParameterBatch(intents, controller.signal,
      intent => api<VariablePlan>("live/variable/plan", { connection: request.via.connection, ...intent }, controller.signal),
      (name, plan) => setAnswers(a => a.signature !== signature ? a : { ...a, plans: { ...a.plans, [name]: plan } }),
      (name, message) => setAnswers(a => a.signature !== signature ? a : { ...a, errors: { ...a.errors, [name]: message } }),
    );
    return () => controller.abort();
    // signature includes every row, value and mode; the review's via is immutable.
  }, [signature, request]);

  const gateFor = (plan: VariablePlan) => targetPrivilege(privilegeResult(), plan.privileges);
  const gates = Object.values(plans).map(gateFor);
  const gate = gates.find(g => !!g.disabledReason) || gates.find(g => g.state === "unknown") || gates[0] || privilegeResult();
  const ready = rows.length > 0 && rows.length <= BATCH_LIMIT && rows.every(({ parameter: p, value }) => {
    const plan = plans[p.name];
    return plan?.status === "ready" && !!plan.statement && !!plan.expect && plan.name === p.name && plan.requested === value && plan.mode === modeOf(p) && plan.policy?.action !== "refuse";
  });
  const blocked = policy?.blocked || (request.via.connection.read_only ? "This connection is read-only." : "");
  const stale = activeKey !== request.via.key;
  const policyPlan = Object.values(plans).find(p => p.policy?.action === "refuse") || Object.values(plans).find(p => p.policy);

  function cancel() {
    if (sending.current || cancelled.current) return;
    cancelled.current = true;
    for (const intent of intents) {
      const reviewed = plans[intent.name];
      void api<VariableChange>("live/variable/apply", {
        connection: request.via.connection, ...intent, declined: true,
        expect: reviewed?.expect ?? null, reviewed_statement: reviewed?.statement ?? null,
      }).catch(() => null);
    }
    onClose();
  }
  function remove(name: string) {
    if (sending.current) return;
    setRows(r => r.filter(x => x.parameter.name !== name));
    onRemove(name);
  }
  async function confirm() {
    if (!ready || sending.current || stale || blocked || gate.disabledReason) return;
    sending.current = true; setBusy(true);
    const results = await applyParameterBatch(rows, intents, request.via, plans, policy ? () => policy.check() : undefined,
      () => latestKey.current,
      (intent, plan) => api<VariableChange>("live/variable/apply", {
        connection: request.via.connection, ...intent, expect: plan.expect, confirmed: true,
      }),
    );
    onDone(results);
  }
  return <ConfirmSheet className="parameter-batch-sheet" eyebrow="PARAMETERS" title={`Apply ${rows.length} parameter ${rows.length === 1 ? "change" : "changes"}?`}
    description="Review every statement for this server. Each value is checked again before sending. The batch stops at the first change that is not confirmed."
    busy={busy} confirmLabel={<PrivilegeConfirmLabel gate={gate} showStatus={false}>{busy ? "Applying…" : `Apply ${rows.length} changes`}</PrivilegeConfirmLabel>}
    confirmDisabled={!ready || stale || !!blocked} confirmTone={policyPlan?.policy?.tone || "danger"} onCancel={cancel} onConfirm={() => void confirm()}>
    <Fields rows={[["Server", request.via.label]]} />
    {request.engine === "mysql" && rows.some(r => r.parameter.modifiable === "now") && <label className="parameter-persist">
      <input type="checkbox" checked={persist} disabled={busy} onChange={e => setPersist(e.target.checked)} /> Keep after restart
    </label>}
    {rows.length > BATCH_LIMIT && <p className="variable-problems" role="alert">Review at most {BATCH_LIMIT} changes at once. Remove changes to continue.</p>}
    {rows.map(({ parameter: p, value }) => {
      const plan = plans[p.name];
      return <section className="parameter-review-row" key={p.name} data-parameter-review={p.name}>
        <div className="parameter-review-heading"><strong><code>{p.name}</code></strong><button type="button" className="text-button" disabled={busy} aria-label={`Remove ${p.name}`} onClick={() => remove(p.name)}>Remove</button></div>
        <Fields rows={[["Current value", valueText(p.type, plan ? plan.before.global : p.value)], ["New value", valueText(p.type, value)], ["When it applies", whenApplies(p)]]} />
        {!plan && !errors[p.name] && rows.length <= BATCH_LIMIT && <p className="hint" role="status">Checking with the server…</p>}
        {errors[p.name] && <p className="variable-problems" role="alert">{errors[p.name]}</p>}
        {plan && <>
          {plan.statement && <pre className="variable-statement" tabIndex={0} aria-label={`Exact statement for ${p.name}`}><code>{plan.statement}{plan.statement.endsWith(";") ? "" : ";"}</code></pre>}
          {plan.problems.length > 0 && <ul className="variable-problems" role="alert">{plan.problems.map(problem => <li key={problem.code + problem.message}>{problem.message}</li>)}</ul>}
          <p className="hint variable-permissions">{plan.privileges ? privilegeText(plan.privileges) : "Permission could not be verified. The server decides."}</p>
          {plan.effect && <p className="hint">{plan.effect.text}</p>}
          {plan.undo.length > 0 && <Disclosure title="Undo and caveats"><CopySql sql={plan.undo.join("\n")} label={`Undo ${p.name}`} />{plan.caveats.map(c => <p className="hint" key={c}>{c}</p>)}</Disclosure>}
          {plan.engine === "mariadb" && <><p className="hint">MariaDB has no SET PERSIST. Copy this line to the option file to keep the value after restart.</p>{plan.config_line && <CopySql sql={plan.config_line} label={`Option file line for ${p.name}`} />}</>}
        </>}
      </section>;
    })}
    {policyPlan?.policy && <p className="hint variable-policy">{policyPlan.policy.text}</p>}
    {(stale || blocked) && <p className="variable-problems" role="alert">{stale ? "The connection in use changed. Close this review; nothing will be sent." : blocked}</p>}
  </ConfirmSheet>;
}

const SEVERITY: Record<string, number> = { problem: 0, watch: 1 };
/** The most severe finding first: problem, then watch. */
function findings(checks: ConfigCheck[]) {
  return checks.filter(c => c.status in SEVERITY).sort((a, b) => SEVERITY[a.status] - SEVERITY[b.status]);
}
/** Only the official manuals the desktop app opens (apps/desktop/backend.cjs). */
const DOC_HOSTS = new Set(["dev.mysql.com", "mariadb.com", "docs.percona.com"]);
export function documentationLink(doc: ConfigParameter["documentation"]): { url: string; source: string } | null {
  if (!doc || typeof doc.url !== "string") return null;
  try {
    const u = new URL(doc.url);
    return u.protocol === "https:" && !u.username && !u.password && !u.port && DOC_HOSTS.has(u.hostname)
      ? { url: u.href, source: doc.source || "Documentation" } : null;
  } catch { return null; }
}
/** One plain sentence: the server's own description, else the opening
 * sentence of a check that is about this parameter first. */
export function oneLine(p: ConfigParameter, checks: ConfigCheck[]) {
  const text = p.description || checks.find(c => c.settings[0]?.name === p.name)?.explain || "";
  const first = text.trim().match(/^.*?[.!?](?=\s+[A-Z(]|$)/s)?.[0] || text.trim();
  return first.length > 240 ? `${first.slice(0, 239).trimEnd()}…` : first;
}
/** When a change takes effect, in words (replaces Apply type and Scope). */
function changeTiming(p: ConfigParameter, engine: string | null) {
  if (p.modifiable === "none") return "Set by the server";
  if (p.apply_type === "dynamic") return p.scope === "both" ? "Now, for new sessions" : "Now";
  if (p.modifiable === "restart") return "After a restart";
  if (p.modifiable === "option_file" || (engine === "mariadb" && p.apply_type === "static")) return "Option file, then a restart";
  return p.apply_type === "static" ? "After a restart" : "Unknown";
}
function sourceText(p: ConfigParameter) {
  const details = sourceDetails(p);
  return [p.source_label || p.source || "Unknown", details].filter(Boolean).join(" · ");
}

/** The single recommendation for a parameter: the most severe finding, its
 * reason, the suggested change and one caveat line. */
function ParameterRecommendation({ name, flagged }: { name: string; flagged: ConfigCheck[] }) {
  const [check, ...others] = flagged;
  return <InspectorSection title="Recommendation">
    <div className="parameter-finding" data-check={check.key}>
      <p className="parameter-finding-head"><StatusPill tone={check.status} /><strong>{check.label}</strong></p>
      <p>{check.why}</p>
      {check.action?.text && <p>{check.action.text}</p>}
      {check.action?.sql && <CopySql sql={check.action.sql} label={`Suggested statement for ${name}`} />}
      {check.action?.config && <CopySql sql={check.action.config} label={`Option file line for ${name}`} />}
      {check.caveat && <p className="hint">{check.caveat}</p>}
      {others.length > 0 && <p className="hint">Also flagged: {others.map(c => c.label).join(", ")}.</p>}
    </div>
  </InspectorSection>;
}

export function ConfigView({ connection, disabled, onBack, selection, report: bound, onReport, onOpenSql }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  report: Bound<ConfigReport> | null; onReport: (report: Bound<ConfigReport>) => void; onOpenSql?: (sql: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [editing, setEditing] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState(false);
  const [batch, setBatch] = useState<BatchRequest | null>(null);
  const [batchResults, setBatchResults] = useState<BatchResult[]>([]);
  const [change, setChange] = useState<ChangeRequest | null>(null);
  const [note, setNote] = useState<ChangeNote | null>(null);
  const [focusName, setFocusName] = useState("");
  const trigger = useRef<HTMLElement | null>(null);
  const noteRef = useRef<HTMLDivElement>(null);
  const resultsRef = useRef<HTMLDivElement>(null);
  const attempted = useRef("");
  const policy = useWritePolicy();
  const sharedInspector = useInspector();
  const reads = useConnectionReads(connection);
  const onScreen = useViewOnScreen();
  const loading = !!reads.loading;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const parameters = useMemo(() => report ? parametersOf(report) : [], [report]);
  const checkMap = useMemo(() => new Map(report?.checks.map(c => [c.key, c]) || []), [report]);
  const recommendation = (p: ConfigParameter) => p.checks.map(k => checkMap.get(k)).find(c => c?.status === "problem") || p.checks.map(k => checkMap.get(k)).find(c => c?.status === "watch");
  const actionsOff = disabled || loading || !!other;
  const offTitle = other ? `From ${other}: refresh to load this server` : policy?.blocked || (connection.read_only ? "This connection is read-only." : disabled ? "Select an available connection." : "");
  const editOff = actionsOff || !!offTitle || !report;

  async function load(force = false) {
    if (disabled) return;
    await reads.run("config", "Reading parameters…", (c, signal) => api<ConfigReport>("live/config", { connection: c }, signal), onReport, { force });
  }
  useEffect(() => {
    // One attempt on open or connection change, including a failed read. An
    // error never causes a retry loop; Refresh remains the explicit retry.
    if (disabled || !onScreen || bound?.key === reads.key || attempted.current === reads.key) return;
    attempted.current = reads.key;
    void load();
  }, [reads.key, disabled, onScreen, bound?.key]);
  useEffect(() => {
    setDrafts({}); setEditing(false); setPreview(false); setFilter("all"); setFocusName("");
    setNote(null); setBatchResults([]);
  }, [reads.key]);
  // Legacy links carry a check key; row selection always uses the parameter name.
  useEffect(() => {
    if (!selection.key || parameters.some(p => p.name === selection.key)) return;
    const name = checkMap.get(selection.key)?.settings[0]?.name;
    if (name && parameters.some(p => p.name === name)) selection.select(name, true);
  }, [selection.key, parameters, checkMap]);

  const pending = parameters.filter(p => Object.prototype.hasOwnProperty.call(drafts, p.name)).map(parameter => ({ parameter, value: drafts[parameter.name] }));
  const needle = search.trim().toLowerCase();
  const rows = parameters.filter(p => (!needle || [p.name, p.value || "", p.description || ""].some(v => v.toLowerCase().includes(needle))) &&
    (filter === "all" || filter === "modified" && p.modified === true || filter === "dynamic" && p.apply_type === "dynamic" || filter === "static" && p.apply_type === "static" || filter === "recommendations" && !!recommendation(p) || filter === "pending" && Object.prototype.hasOwnProperty.call(drafts, p.name)));
  useEffect(() => {
    if (!focusName || !editing) return;
    // Filtering to the requested row also brings a windowed row into the DOM.
    const frame = window.requestAnimationFrame(() => {
      const el = document.getElementById(`parameter-value-${focusName}`);
      if (el) { el.focus(); setFocusName(""); }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [focusName, editing, rows.length]);

  function discard(name: string) { setDrafts(d => { const next = { ...d }; delete next[name]; return next; }); }
  function stage(p: ConfigParameter, value: string) {
    if (editOff || unmodifiable(p, report?.engine || null)) return;
    setDrafts(d => { const next = { ...d }; if (value === draftValue(p)) delete next[p.name]; else next[p.name] = value; return next; });
  }
  function cancelEditing() { setDrafts({}); setEditing(false); setPreview(false); if (filter === "pending") setFilter("all"); }
  function editParameter(p: ConfigParameter) {
    if (editOff) return;
    setEditing(true); setSearch(p.name); setFilter("all"); setFocusName(p.name);
    selection.select(p.name, false); sharedInspector?.setOpen(false);
  }
  function openChange(name: string, mode: VariableMode | null, el: HTMLElement) {
    if (other || disabled) return;
    trigger.current = el; setNote(null);
    setChange({ name, mode, via: { connection, key: reads.key, label: connectionLabel(connection) } });
  }
  function closeChange() { setChange(null); window.setTimeout(() => trigger.current?.focus(), 0); }
  async function changed(result: ChangeNote) {
    const via = change?.via; setChange(null); setNote(result);
    window.setTimeout(() => noteRef.current?.focus(), 0);
    if (result.status === "changed" && via && reads.currentKey() === via.key) { discard(result.name); await load(true); }
  }
  async function batchDone(results: BatchResult[]) {
    const via = batch?.via; setBatch(null);
    if (!via || reads.currentKey() !== via.key) return;
    setBatchResults(results);
    const changedNames = new Set(results.filter(r => r.status === "changed").map(r => r.name));
    setDrafts(d => { const next = { ...d }; for (const name of changedNames) delete next[name]; return next; });
    const remaining = pending.some(r => !changedNames.has(r.parameter.name));
    setEditing(remaining); setPreview(remaining);
    if (!remaining && filter === "pending") setFilter("all");
    window.setTimeout(() => resultsRef.current?.focus(), 0);
    await load(true);
  }
  const columns: Column<ConfigParameter>[] = [
    { key: "name", label: "Name", primary: true, width: 260, sort: p => p.name, render: p => <span className="parameter-name">{recommendation(p) && <StatusDot tone={recommendation(p)!.status} />}<code>{p.name}</code>{p.vendor === "percona" && <Pill title="A variable Percona Server adds to MySQL">Percona</Pill>}</span> },
    { key: "value", label: "Value", width: editing ? 320 : 180, minWidth: editing ? 300 : undefined, sort: p => p.value, render: p => {
      const staged = Object.prototype.hasOwnProperty.call(drafts, p.name);
      const reason = unmodifiable(p, report?.engine || null);
      if (!editing || reason) return <span title={reason || (p.truncated ? "Value truncated" : valueText(p.type, p.value))}>{valueText(p.type, p.value)}{p.truncated ? "…" : ""}</span>;
      const id = `parameter-value-${p.name}`;
      return <span className="parameter-cell-editor" onKeyDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()}>
        {staged && drafts[p.name] === "DEFAULT" ? <code>DEFAULT</code> : <ValueField info={p} label={p.name} value={staged ? drafts[p.name] : draftValue(p)} onChange={v => stage(p, v)} id={id} hintId={`${id}-hint`} disabled={editOff} />}
        <span className="account-sr" id={`${id}-hint`}>{p.name}. {allowedValues(p)}</span>
        {staged && <><span className="hint parameter-current" title={`Current value: ${valueText(p.type, p.value)}`}>{valueText(p.type, p.value)}</span><button type="button" className="text-button" aria-label={`Discard ${p.name}`} onClick={() => discard(p.name)}>Discard</button></>}
      </span>;
    } },
    { key: "allowed", label: "Allowed values", width: 190, render: p => <span title={allowedValues(p)}>{allowedValues(p)}</span> },
    { key: "apply_type", label: "Apply type", sort: p => applyType(p), render: p => applyType(p) },
    { key: "source", label: "Source", sort: p => p.source_label, render: p => <span title={sourceDetails(p)}>{p.source_label || ""}</span> },
    { key: "type", label: "Data type", sort: p => p.type, render: p => p.type || "" },
    { key: "description", label: "Description", flex: true, width: 320, render: p => <span title={p.description || ""}>{p.description || ""}</span> },
  ];
  let inspector: InspectorContent | null = null;
  const selected = parameters.find(p => p.name === selection.key);
  if (selected && report) {
    const p = selected;
    const reason = unmodifiable(p, report.engine);
    const resettable = report.engine === "mysql" && p.apply_type === "dynamic" && ["bytes", "integer", "double", "boolean"].includes(p.type || "");
    const checks = p.checks.map(k => checkMap.get(k)).filter((c): c is ConfigCheck => !!c);
    const flagged = findings(checks);
    const doc = documentationLink(p.documentation);
    const line = oneLine(p, checks);
    inspector = { key: p.name, title: p.name,
      meta: <><span>{valueText(p.type, p.value)}</span>{flagged[0] && <StatusPill tone={flagged[0].status} />}</>,
      actions: <div className="config-actions">
        <button type="button" className="button primary" disabled={editOff || !!reason} title={offTitle || reason || undefined} onClick={() => editParameter(p)}>Edit value</button>
        {resettable && <button type="button" className="button" disabled={editOff || !!reason} title={offTitle || reason || undefined} onClick={() => { setEditing(true); stage(p, "DEFAULT"); sharedInspector?.setOpen(false); setPreview(true); }}>Reset to default</button>}
        <button type="button" className="button" data-change={p.name} disabled={actionsOff || !!offTitle || p.modifiable === "none"}
          title={offTitle || (p.modifiable === "none" ? reason : undefined) || undefined} onClick={e => openChange(p.name, null, e.currentTarget)}>Change…</button>
        {p.persisted === true && <button type="button" className="button" data-reset={p.name} disabled={actionsOff || !!offTitle} title={offTitle || undefined} onClick={e => openChange(p.name, "reset_persist", e.currentTarget)}>Reset persisted value…</button>}
      </div>,
      body: <>
        <div className="parameter-about">
          {line ? <p className="parameter-summary">{line}</p> : !doc && <p className="hint">The server gives no description for this parameter.</p>}
          {doc && <p className="parameter-docs"><a href={doc.url} target="_blank" rel="noreferrer" data-doc-link={p.name}>Documentation<span className="visually-hidden"> for {p.name}, opens in your browser</span></a><span className="hint">{doc.source}</span></p>}
        </div>
        <InspectorSection title="Current value">
          <Fields label={`${p.name} facts`} rows={[
            ["Value", valueText(p.type, p.value)],
            ["Default", p.default === null ? "Unknown" : valueText(p.type, p.default)],
            ["Source", sourceText(p)],
            ["Changes", changeTiming(p, report.engine)],
            ["Allowed", allowedValues(p) || "Unknown"],
            report.engine === "mysql" ? ["Persisted", p.persisted === null ? "Unknown" : p.persisted ? "Yes" : "No"] : null,
            checks.length > 0 && !flagged.length ? ["Checked", <span className="parameter-checked">{checks.map(c => <span key={c.key}><StatusPill tone={c.status} /> {c.label}</span>)}</span>] : null,
          ]} />
        </InspectorSection>
        {flagged.length > 0 && <ParameterRecommendation name={p.name} flagged={flagged} />}
      </> };
  }
  const s = report?.summary;
  const notes = [...(report?.notes || []), ...(report?.parameters?.notes || [])];
  if (report?.parameters && !report.parameters.complete) notes.push("The parameter catalog is incomplete. Some parameters or metadata could not be read.");
  return <ViewLayout id="config" title="Parameters" className="config-panel" onBack={onBack}
    info={<><p>Every server parameter, with its allowed values, source and when it applies.</p><p>Edit stages local drafts. Preview changes, then review every exact statement before applying. Recommendations are rules of thumb; select a parameter for its recommendation and a link to the official documentation.</p></>}
    controls={report && <>
      <SearchField label="Filter parameters" value={search} onChange={setSearch} placeholder="Filter parameters" />
      <ToolbarSelect label="Show" value={filter} options={[["all", "All"], ["modified", "Modified"], ["dynamic", "Dynamic"], ["static", "Static"], ["recommendations", "Recommendations"], ...(editing ? [["pending", "Pending"] as [string, string]] : [])]} onChange={setFilter} />
      {!editing ? <button type="button" className="button primary" data-action="edit-parameters" disabled={editOff} title={offTitle || (loading ? "Reading parameters…" : undefined)} onClick={() => setEditing(true)}>Edit</button> : <>
        <button type="button" className="button" onClick={cancelEditing}>Cancel</button>
        <button type="button" className="button" aria-expanded={preview} onClick={() => setPreview(v => !v)}>Preview changes ({pending.length})</button>
        <button type="button" className="button primary" disabled={editOff || pending.length === 0 || pending.length > BATCH_LIMIT} title={pending.length > BATCH_LIMIT ? `Review at most ${BATCH_LIMIT} changes at once.` : offTitle || undefined}
          onClick={() => { if (!editOff && pending.length && pending.length <= BATCH_LIMIT) setBatch({ rows: pending, via: { connection, key: reads.key, label: connectionLabel(connection) }, engine: report.engine }); }}>Review and apply ({pending.length})</button>
      </>}
    </>}
    onRefresh={() => void load()} refreshDisabled={disabled} loading={loading} otherConnection={other}
    inspector={inspector} inspectorEmpty={<p>Select a parameter to inspect its source, allowed values and recommendations.</p>}
    status={<><ViewStatus loading={reads.loading} error={reads.error} />
      {note && <ChangeResultNote note={note} noteRef={noteRef} onDismiss={() => setNote(null)} />}
      {batchResults.length > 0 && <div ref={resultsRef} tabIndex={-1} className="parameter-results variable-result" role="status">
        <strong>Parameter changes</strong><ul>{batchResults.map(r => <li key={r.name} data-result={r.name} data-status={r.status}><code>{r.name}</code>: {r.status === "changed" ? "Changed" : r.status === "not_sent" ? "Not sent" : r.status === "applied_unverified" ? "Applied; verification incomplete" : r.status === "unknown" ? "Outcome unknown" : "Not changed"}. {r.message}
          {r.result?.before && r.result.after && <span className="hint parameter-result-values">Global: {valueText(r.type ?? null, r.result.before.global)} → {valueText(r.type ?? null, r.result.after.global)}
            {r.result.mode !== "global" && <> · mysqld-auto.cnf: {persistedText(r.type ?? null, r.result.before.persisted_state, r.result.before.persisted)} → {persistedText(r.type ?? null, r.result.after.persisted_state, r.result.after.persisted)}</>}
          </span>}
        </li>)}</ul>
        <button type="button" className="text-button" onClick={() => setBatchResults([])}>Dismiss</button>
      </div>}
    </>}
  >
    {!report && !loading && (disabled || !!reads.error) && <EmptyState icon="configuration" title="No parameters loaded" action={<button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>Load parameters</button>}>Select an available connection and refresh to read its parameters.</EmptyState>}
    {report && s && <>
      <SummaryLine tone={s.status} notes={[...new Set(notes)]}>{parameters.length} parameters, {parameters.filter(p => p.modified === true).length} modified · {s.problem} {s.problem === 1 ? "problem" : "problems"}, {s.watch} to watch</SummaryLine>
      <FactStrip label="Server" facts={[["Server", engineLabel(report)], ["Uptime", uptime(report.uptime_s)]]} />
      {pending.length > BATCH_LIMIT && <p role="alert" className="variable-problems">Review at most {BATCH_LIMIT} changes at once. Discard changes in Preview to continue.</p>}
      {preview && editing && <section className="parameter-preview" aria-label="Preview changes"><h3>Preview changes ({pending.length})</h3>
        <DataTable label="Pending parameter changes" rows={pending} rowKey={r => r.parameter.name} columns={[
          { key: "name", label: "Name", primary: true, render: r => <code>{r.parameter.name}</code> },
          { key: "current", label: "Current value", render: r => valueText(r.parameter.type, r.parameter.value) },
          { key: "new", label: "New value", render: r => valueText(r.parameter.type, r.value) },
          { key: "apply", label: "Apply type", render: r => applyType(r.parameter) },
          { key: "when", label: "When it applies", flex: true, render: r => whenApplies(r.parameter) },
          { key: "remove", label: "Remove", render: r => <button type="button" className="text-button" aria-label={`Remove ${r.parameter.name} from preview`} onClick={() => discard(r.parameter.name)}>Remove</button> },
        ]} empty="No pending changes." />
      </section>}
      <DataTable id="parameters.all" label="Parameters" columns={columns} rows={rows} rowKey={p => p.name} selection={selection} detailColumns={["type", "description"]}
        rowClass={p => Object.prototype.hasOwnProperty.call(drafts, p.name) ? "parameter-staged" : ""} empty="No parameters match this filter."
        rowContextMenu={p => [
          ...(p.modifiable !== "none" ? [{
            label: "Edit parameter value…",
            icon: "edit",
            onClick: () => openChange(p.name, p.modifiable === "now" ? "global" : p.modifiable === "restart" ? "persist_only" : null, document.body),
          }] : []),
          {
            label: "Inspect parameter",
            icon: "info",
            onClick: () => selection.select(p.name, true),
          },
          ...(documentationLink(p.documentation) ? [{
            label: "Open documentation",
            icon: "browser" as const,
            onClick: () => { window.open(documentationLink(p.documentation)!.url, "_blank", "noopener,noreferrer"); },
          }] : []),
          { divider: true as const },
          {
            label: `Copy variable name (${p.name})`,
            icon: "copy",
            onClick: () => void copyToClipboard(p.name),
          },
          {
            label: `Copy current value (${valueText(p.type, p.value)})`,
            icon: "copy",
            onClick: () => void copyToClipboard(valueText(p.type, p.value)),
          },
          ...(onOpenSql ? [{
            label: "Query in Editor (SHOW VARIABLES)",
            icon: "query",
            onClick: () => onOpenSql(`SHOW VARIABLES LIKE '${p.name}';`),
          }] : []),
        ]} />
    </>}
    {change && <VariableSheet request={change} activeKey={reads.key} onClose={closeChange} onDone={n => void changed(n)} onOpenSql={onOpenSql ? sql => { setChange(null); onOpenSql(sql); } : undefined} />}
    {batch && <ParameterBatchSheet request={batch} activeKey={reads.key} onClose={() => setBatch(null)} onRemove={name => { if (batch.via.key === reads.currentKey()) discard(name); }} onDone={results => void batchDone(results)} />}
  </ViewLayout>;
}
