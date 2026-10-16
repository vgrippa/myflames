// Server > Configuration: the settings that matter most, grouped by area as
// one-line rows (System Settings): status, setting, value, and the engine
// default (config-1.0 from myflames/config_review.py). The why, the rule, the
// caveat and the SET / my.cnf lines open in the inspector. The review only
// reads. Each setting in the inspector has Change…, which opens a sheet
// (variables.py: variable-1.0, variable-plan-1.0, variable-change-1.0) with
// the value, when it applies (SET GLOBAL, SET PERSIST, SET PERSIST_ONLY, SET
// SESSION, RESET PERSIST, or the option file line on MariaDB), the exact
// statement, the privileges, the effect and the caveats. Nothing is sent
// until the person confirms; the server re-reads the variable first.
// Session statements open in the current Query editor tab without running.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type ConfigCheck, type ConfigReport, type ConfigSetting, type Connection } from "./api";
import { uptime } from "./Health";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { PrivilegeGate, PrivilegeConfirmLabel, privilegeResult, targetPrivilege, usePrivilege } from "./privileges";
import { useWritePolicy } from "./Connections";
import { CopySql, bytes, engineLabel } from "./PerfShared";
import {
  ConfirmSheet,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorSection,
  SearchField,
  Select,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  type InspectorContent,
  type Selection,
} from "./kit";
import "./config.css";

// ---- variables.py contract ----------------------------------------------------------

export type VariableMode = "global" | "persist" | "persist_only" | "session" | "reset_persist";
/** config-1.0 settings carry whether they can change at runtime (variables.static_dynamic). */
type Setting = ConfigSetting & { dynamic?: boolean | null };
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
type ChangeRequest = { name: string; mode: VariableMode | null; via: Via };
type ChangeNote = { name: string; mode: VariableMode; status: string; message: string; type: VariableInfo["type"]; result: VariableChange | null };

function shownValue(check: ConfigCheck) {
  if (check.value) return check.value;
  const first = check.settings[0];
  return first?.display ?? "—";
}

/** The main setting's default, in muted text after the value. */
function defaultText(check: ConfigCheck) {
  const s = check.settings[0];
  if (!s) return "";
  return s.default !== null && s.default !== s.display ? `default ${s.default}` : s.default !== null ? "default" : "";
}

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
function ValueField({ info, value, onChange, id, hintId, disabled }: { info: VariableInfo; value: string; onChange: (v: string) => void; id: string; hintId: string; disabled: boolean }) {
  if (info.type === "boolean")
    return <Select id={id} value={value === "ON" ? "ON" : "OFF"} options={[["ON", "ON"], ["OFF", "OFF"]]} onChange={onChange} disabled={disabled} describedBy={hintId} />;
  if (info.type === "enum" && info.values?.length)
    return <Select id={id} value={value} options={info.values.map((v) => [v, v] as [string, string])} onChange={onChange} disabled={disabled} describedBy={hintId} />;
  return (
    <input
      id={id}
      className="variable-input"
      value={value}
      disabled={disabled}
      spellCheck={false}
      autoComplete="off"
      inputMode={info.type === "integer" || info.type === "double" ? "decimal" : undefined}
      aria-describedby={hintId}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function privilegeText(p: NonNullable<VariablePlan["privileges"]>) {
  if (p.need === "no privilege" && p.allowed === true) return "No privilege needed.";
  const state = p.allowed === true ? `your grants include ${p.via}` : p.allowed === false ? "your grants do not seem to include it" : "Unverified; the server decides";
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
        setValue(startValue(v, m));
        setIsNull(v.nullable && (m === "session" ? v.session_value : v.global_value) === null);
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
  if (stale) lock = "The connection in use changed after this sheet opened. Close it and open Change again.";
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
      confirmLabel={mode && mode !== "session" ? <PrivilegeConfirmLabel gate={gate}>{confirmLabel}</PrivilegeConfirmLabel> : confirmLabel}
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
            {plan.privileges && (
              <div>
                <dt>Privileges</dt>
                <dd>{privilegeText(plan.privileges)}</dd>
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

export function ConfigView({
  connection,
  disabled,
  onBack,
  selection,
  report: bound,
  onReport,
  onOpenSql,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  report: Bound<ConfigReport> | null;
  onReport: (report: Bound<ConfigReport>) => void;
  onOpenSql?: (sql: string) => void;
}) {
  const [search, setSearch] = useState("");
  const [change, setChange] = useState<ChangeRequest | null>(null);
  const [note, setNote] = useState<ChangeNote | null>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const noteRef = useRef<HTMLDivElement>(null);
  const policy = useWritePolicy();
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;

  async function load(force = false) {
    await reads.run(
      "config",
      "Reading the configuration…",
      (c) => api<ConfigReport>("live/config", { connection: c }),
      onReport,
      { force },
    );
  }

  function openChange(name: string, mode: VariableMode | null, el: HTMLElement) {
    if (other || disabled) return;
    trigger.current = el;
    setNote(null);
    setChange({ name, mode, via: { connection, key: connectionKey(connection), label: connectionLabel(connection) } });
  }
  function closeChange() {
    setChange(null);
    window.setTimeout(() => trigger.current?.focus(), 0);
  }
  async function changed(result: ChangeNote) {
    const via = change?.via;
    setChange(null);
    setNote(result);
    window.setTimeout(() => noteRef.current?.focus(), 0);
    // Read again only while the connection in use is still the one changed.
    if (result.status === "changed" && via && reads.currentKey() === via.key) await load(true);
  }

  const s = report?.summary;
  const needle = search.trim().toLowerCase();
  const matches = (c: ConfigCheck) =>
    !needle ||
    c.label.toLowerCase().includes(needle) ||
    c.settings.some((x) => x.name.toLowerCase().includes(needle) || (x.alias || "").toLowerCase().includes(needle));
  const groups = report
    ? report.groups
        .map((g) => ({ ...g, checks: report.checks.filter((c) => c.group === g.key && matches(c)) }))
        .filter((g) => g.checks.length)
    : [];

  const actionsOff = disabled || !!loading || !!other;
  const offTitle = other ? `From ${other}: refresh to load this server` : policy?.blocked || (connection.read_only ? "This connection is read-only." : "");
  /** Change… for a setting (or Change at next restart… when it is read-only). */
  const changeActions = (x: Setting): ReactNode => {
    if (!report) return null;
    const mysql = report.engine === "mysql";
    const label = x.dynamic === false ? (mysql ? "Change at next restart…" : "Option file line…") : "Change…";
    // A MariaDB read-only variable only offers an option file line to copy:
    // nothing is sent, so no privilege applies and no "Unverified" chip.
    if (x.dynamic === false && !mysql)
      return (
        <span className="config-actions">
          <button
            type="button"
            className="button text-button"
            data-change={x.name}
            aria-label={`${label.replace("…", "")}: ${x.name}`}
            disabled={disabled || !!loading || !!other}
            title={other ? offTitle : undefined}
            onClick={(e) => openChange(x.name, null, e.currentTarget)}
          >
            {label}
          </button>
        </span>
      );
    return (
      <span className="config-actions">
        <PrivilegeGate action="set_global" gate={privilegeResult()}
          type="button"
          className="text-button"
          data-change={x.name}
          aria-label={`${label.replace("…", "")}: ${x.name}`}
          disabled={actionsOff}
          blocked={offTitle}
          onClick={(e) => openChange(x.name, x.dynamic === false && mysql ? "persist_only" : null, e.currentTarget)}
        >
          {label}
        </PrivilegeGate>
        {mysql && x.set_by === "SET PERSIST" && (
          <PrivilegeGate action="reset_persist" gate={privilegeResult()}
            type="button"
            className="text-button"
            data-reset={x.name}
            aria-label={`Reset persisted value: ${x.name}`}
            disabled={actionsOff}
            blocked={offTitle}
            onClick={(e) => openChange(x.name, "reset_persist", e.currentTarget)}
          >
            Reset persisted value…
          </PrivilegeGate>
        )}
      </span>
    );
  };

  let inspector: InspectorContent | null = null;
  const check = report?.checks.find((c) => c.key === selection.key);
  if (check) {
    const flagged = check.status === "watch" || check.status === "problem";
    inspector = {
      key: check.key,
      title: check.label,
      meta: (
        <>
          <StatusPill tone={check.status} />
          <span>{shownValue(check)}</span>
        </>
      ),
      body: (
        <>
          <InspectorSection title="What it is">
            <p>{check.explain}</p>
          </InspectorSection>
          <InspectorSection title={flagged ? "Why it is flagged" : "Why this status"}>
            <p className="config-why">{check.why}</p>
          </InspectorSection>
          {check.settings.length > 0 && (
            <InspectorSection title="Current values">
              <Fields
                label={`${check.label}: settings`}
                rows={(check.settings as Setting[]).map((x) => [
                  <>
                    <code>{x.name}</code>
                    {x.alias && <span className="hint"> (alias {x.alias})</span>}
                  </>,
                  <>
                    {x.display ?? "—"}
                    <span className="hint">
                      {x.default !== null && ` · default ${x.default}`}
                      {x.set_by && ` · set by ${x.set_by}`}
                    </span>
                    {changeActions(x)}
                  </>,
                ])}
              />
            </InspectorSection>
          )}
          {check.action && (check.action.text || check.action.sql || check.action.config) && (
            <InspectorSection title="What to do">
              {check.action.text && <p>{check.action.text}</p>}
              {check.action.sql && <CopySql sql={check.action.sql} label={`Suggested statement for ${check.label}`} />}
              {check.action.config && <CopySql sql={check.action.config} label={`my.cnf line for ${check.label}`} />}
            </InspectorSection>
          )}
          {check.caveat && <p className="hint perf-caveat">Caveat: {check.caveat}</p>}
          <p className="hint config-sources">From {check.sources.join(", ")}</p>
        </>
      ),
    };
  }

  return (
    <ViewLayout
      id="config"
      title="Configuration"
      className="config-panel"
      onBack={onBack}
      info={
        <>
          <p>
            The settings that matter most (buffer pool, redo log, durability, connections, caches, temporary tables,
            Performance Schema and the slow query log), each against the engine's default with a rule of thumb.
          </p>
          <p>
            Statuses are rules of thumb, not verdicts. Select a setting for why, the caveat, and the statement to copy. The review never changes a
            setting; Change in the inspector shows the exact statement and asks before sending it.
          </p>
        </>
      }
      controls={report && <SearchField label="Filter settings" value={search} onChange={setSearch} placeholder="Filter" />}
      onRefresh={() => void load()}
      refreshDisabled={disabled}
      loading={loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={<p>Select a setting to see why it has its status, its default, and what to do.</p>}
      status={<ViewStatus loading={reads.loading} error={error} />}
    >
      {note && <ChangeResultNote note={note} noteRef={noteRef} onDismiss={() => setNote(null)} />}
      {!report && !loading && !error && (
        <EmptyState
          icon="configuration"
          title="No configuration loaded"
          action={
            <button type="button" className="button primary" disabled={disabled} onClick={() => void load()}>
              Load configuration
            </button>
          }
        >
          Reads settings and counters since startup and compares them with the engine's defaults. It only reads.
        </EmptyState>
      )}
      {report && s && (
        <>
          <SummaryLine tone={s.status} notes={report.notes}>
            {s.problem} {s.problem === 1 ? "problem" : "problems"}, {s.watch} to watch, {s.ok} ok
          </SummaryLine>
          <FactStrip label="Server" facts={[["Server", engineLabel(report)], ["Uptime", uptime(report.uptime_s)]]} />
          {groups.length === 0 && <div className="table-empty">No setting matches “{search.trim()}”.</div>}
          {groups.map((g) => (
            <GroupedList key={g.key} id={`config-${g.key}`} title={g.label} count={g.checks.length}>
              {g.checks.map((c) => (
                <GroupedRow
                  key={c.key}
                  rowKey={c.key}
                  selection={selection}
                  tone={c.status}
                  label={c.label}
                  detail={c.settings.length ? c.settings.map((x) => x.name).join(", ") : undefined}
                  value={shownValue(c)}
                  secondary={defaultText(c) || undefined}
                  attrs={{ "data-check": c.key }}
                />
              ))}
            </GroupedList>
          ))}
        </>
      )}
      {change && <VariableSheet request={change} activeKey={reads.key} onClose={closeChange} onDone={(n) => void changed(n)}
        onOpenSql={onOpenSql ? sql => { setChange(null); onOpenSql(sql); } : undefined} />}
    </ViewLayout>
  );
}
