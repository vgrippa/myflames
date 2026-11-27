// A .ts module deliberately uses createElement so consumers need no JSX
// wrapper. Server answers own privilege semantics; no grants are parsed here.
import { Fragment, Suspense, createContext, createElement, lazy, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type PropsWithChildren } from "react";
import { api, type Connection } from "./api";
import { connectionKey, sharedRead } from "./binding";
import { useWritePolicy } from "./Connections";
import type { ActionPrivilege, PrivilegesReport, PrivilegeState } from "./accountsApi";
import { closeWhyDisabled, onWhyDemand, openWhyDisabled } from "./whyDisabledBus";

// The same read as accountsApi.readPrivileges, spelled out here: gating
// write actions is shell code, so this one endpoint stays in the entry chunk
// while the accounts API loads with the Accounts view.
const readPrivileges = (connection: Connection, signal?: AbortSignal) =>
  api<PrivilegesReport>("live/privileges", { connection }, signal);

export type PrivilegeGateResult = {
  state: PrivilegeState;
  missing: string[];
  reason: string;
  disabledReason: string;
  available: boolean;
  /** Backend-supplied advisory GRANT statement, or null when unavailable/inapplicable. */
  grantSql?: string | null;
};
type PrivilegesContextValue = {
  key: string; readOnly: boolean; report: PrivilegesReport | null; loading: boolean; error: string;
  refresh: (fresh?: boolean) => Promise<void>; accountChanged: () => Promise<void>;
};
const PrivilegesContext = createContext<PrivilegesContextValue | null>(null);

export function PrivilegesProvider({ connection, connected = true, connectRevision = 0, passwordRevision = 0, children }: PropsWithChildren<{
  connection: Connection; connected?: boolean;
  /** Increment after a successful reconnect to the same identity. */
  connectRevision?: number;
  /** Increment only when the session password is committed, never while typing. */
  passwordRevision?: number;
}>) {
  const key = connectionKey(connection);
  // A late reconnect acknowledges the credentials it sent, not the picker's
  // current draft. Only an explicit password commit (or a new identity) may
  // adopt that value; connectRevision still refreshes status in the effect.
  const committed = useRef({ key, passwordRevision, password: connection.password });
  if (committed.current.key !== key || committed.current.passwordRevision !== passwordRevision)
    committed.current = { key, passwordRevision, password: connection.password };
  const passwordCommitted = committed.current.password === connection.password;
  const latest = useRef({ connection, key, connected, passwordCommitted });
  latest.current = { connection, key, connected, passwordCommitted };
  const [value, setValue] = useState<{ key: string; report: PrivilegesReport | null; at: number; error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const pending = useRef<{ key: string; controller: AbortController; promise: Promise<void> } | null>(null);
  const generation = useRef(0);
  const at = useRef(0);

  const refresh = useCallback((fresh = false): Promise<void> => {
    const c = latest.current;
    // Focus and incidental refreshes must not authenticate with a draft either.
    if (!c.connected || !c.passwordCommitted) return Promise.resolve();
    if (!fresh && pending.current?.key === c.key) return pending.current.promise;
    pending.current?.controller.abort();
    const controller = new AbortController();
    const seq = ++generation.current;
    setLoading(true);
    const promise = sharedRead(c.key, "privileges", signal => readPrivileges(c.connection, signal), { signal: controller.signal, fresh })
      .then(report => {
        if (seq !== generation.current || latest.current.key !== c.key || controller.signal.aborted) return;
        at.current = Date.now();
        setValue({ key: c.key, report, at: at.current, error: "" });
      }).catch((e: Error) => {
        if (seq !== generation.current || latest.current.key !== c.key || controller.signal.aborted) return;
        // A failed refresh invalidates earlier denials as well as allowances.
        setValue({ key: c.key, report: null, at: 0, error: e.message });
      }).finally(() => {
        if (seq === generation.current) { pending.current = null; setLoading(false); }
      });
    pending.current = { key: c.key, controller, promise };
    return promise;
  }, []);

  const accountChanged = useCallback(async () => {
    // A pre-change read must never answer a post-change refresh.
    ++generation.current;
    pending.current?.controller.abort();
    pending.current = null;
    setValue(null);
    at.current = 0;
    await refresh(true);
  }, [refresh]);

  useEffect(() => {
    setValue(null); setLoading(false); at.current = 0;
    void refresh(true);
    return () => { ++generation.current; pending.current?.controller.abort(); pending.current = null; };
  }, [key, connected, connectRevision, passwordRevision, refresh]);
  useEffect(() => {
    const focus = () => {
      if (document.activeElement?.matches(".picker-password")) return;
      if (Date.now() - at.current > 60_000) void refresh();
    };
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, [refresh]);

  const current = connected && value?.key === key ? value : null;
  return createElement(PrivilegesContext.Provider, { value: {
    key, readOnly: connection.read_only === true, report: current?.report || null, loading, error: current?.error || "", refresh, accountChanged,
  } }, children);
}

/** What each action key means, for lists shown to people (never the raw key). */
export const ACTION_LABELS: Record<string, string> = {
  privileges: "Read your privileges",
  health: "Health",
  config: "Parameters",
  config_sources: "Where settings come from",
  top_statements: "Top statements",
  sessions_own: "Your sessions",
  sessions_all: "Every session",
  explain_connection: "Show plan of a session",
  locks_transactions: "Locks: transactions and row locks",
  locks_metadata: "Locks: metadata locks",
  innodb_status: "InnoDB status",
  error_log: "Error log",
  waits: "Waits",
  io: "I/O",
  instruments: "Instruments",
  indexes_usage: "Index usage",
  table_lists: "Table lists",
  tablespaces: "Tablespaces",
  database_picker: "Database list",
  replication_status: "Replication status",
  binlog_status: "Binary log status",
  relay_events: "Relay log events",
  routine_body: "Routine bodies",
  view_definition: "View definitions",
  accounts_list: "List users and roles",
  grants_other: "Other accounts' grants",
  explain: "Explain",
  run_analysis: "Run analysis",
  kill: "Kill another account's session",
  kill_own: "Kill your own sessions",
  set_global: "SET GLOBAL",
  set_persist: "SET PERSIST",
  set_persist_only: "SET PERSIST_ONLY",
  reset_persist: "RESET PERSIST",
  set_session_restricted: "SET SESSION of restricted variables",
  replica_start_stop: "Start or stop replication",
  skip_gtid: "Skip a transaction (GTID)",
  skip_counter: "Skip a transaction (skip counter)",
  clone_donor: "Clone from this server",
  clone_recipient: "Clone into this server",
  dump_shell: "Dump with MySQL Shell",
  dump_mysqldump: "Dump with mysqldump",
  dump_mariadb: "Dump with mariadb-dump",
  restore: "Restore a dump",
  insert: "INSERT",
  update: "UPDATE",
  delete: "DELETE",
  create_table: "CREATE TABLE",
  alter_table: "ALTER TABLE",
  create_index: "CREATE INDEX",
  create_view: "CREATE VIEW",
  drop_table: "DROP TABLE",
  truncate_table: "TRUNCATE TABLE",
  create_temporary_table: "CREATE TEMPORARY TABLE",
  analyze_table: "ANALYZE TABLE",
  optimize_table: "OPTIMIZE TABLE",
  flush: "FLUSH",
  backup_lock: "LOCK INSTANCE FOR BACKUP",
  instruments_presets: "Apply instrument presets",
  read_only_bypass: "Write while the server is read-only",
  create_user: "New user",
  create_role: "New role",
  drop_user: "Drop user",
  drop_role: "Drop role",
  rename_user: "Rename",
  lock_account: "Lock",
  unlock_account: "Unlock",
  expire_password: "Expire password",
  account_options: "Account options",
  set_password: "Set another account's password",
  set_own_password: "Set your own password",
  grant: "Grant privileges",
  revoke: "Revoke privileges",
  grant_role: "Grant roles",
  revoke_role: "Revoke roles",
  default_roles: "Default roles"
};
export const actionLabel = (key: string) => ACTION_LABELS[key] || key.replace(/_/g, " ");

export function usePrivileges() { return useContext(PrivilegesContext); }

/** The user name a connection logs in as: the one typed, else (no user name:
 * the client logs in as the OS user) the one the privileges read names,
 * USER() first, then CURRENT_USER. Used to recognize the account's own sessions. */
export function useLoginUser(typed?: string): string {
  const report = usePrivileges()?.report as { user?: string; account?: string } | null | undefined;
  return typed || (report?.user || report?.account || "").replace(/@[^@]*$/, "").replace(/^'|'$/g, "");
}

/** Pure projection, also useful for the target-specific hint in a plan.
 * grey_out=false is authoritative (read actions keep working partially). */
export function privilegeResult(answer?: ActionPrivilege, account = ""): PrivilegeGateResult {
  const missing = [...new Set((answer?.missing || []).flat())];
  const state = answer?.state || "unknown";
  const available = answer?.available !== false;
  const grantSql = state === "denied" && typeof answer?.grant_sql === "string" && answer.grant_sql.length > 0
    ? answer.grant_sql
    : null;
  const reason = state === "unknown" ? `Unverified. ${answer?.reason || "SHOW GRANTS cannot tell; the server decides."}` :
    // The server's reason usually says both already ("Needs X; app@% has
    // none of them."): add what it does not say, never repeat it.
    state === "denied" ? [answer?.reason,
      missing.length && !/\bNeeds\b/.test(answer?.reason || "") ? `Needs ${answer?.need || missing.join(" or ")}.` : "",
      account && !(answer?.reason || "").includes(account) ? `Connected as ${account}.` : ""].filter(Boolean).join(" ") : answer?.reason || "Required privileges are present.";
  return { state, missing, reason, available, grantSql,
    disabledReason: !available ? answer?.note || "Unavailable on this server." : state === "denied" && answer?.grey_out !== false ? reason : "" };
}
export function usePrivilege(actionKey?: string): PrivilegeGateResult {
  const context = usePrivileges();
  return privilegeResult(actionKey ? context?.report?.actions[actionKey] : undefined, context?.report?.account);
}

export type EditorPrivilegePreview = {
  status: "ready" | "invalid";
  statements: { index: number; kind: string; action_keys: string[]; state: PrivilegeState; missing: string[]; reason: string; grant_sql?: string | null }[];
  aggregate: { state: PrivilegeState; missing: string[]; reason: string; warning: string; grant_sql?: string | null };
  policy?: { action: string; text: string };
};

/** The server aggregates the exact statements Run sends. Never derive a
 * global Run privilege, or turn a mixed batch into a blanket denial. */
export function editorPrivilegeGate(preview?: EditorPrivilegePreview): PrivilegeGateResult {
  const gate = privilegeResult(preview?.aggregate);
  if (preview?.policy?.action === "refuse")
    return { ...gate, disabledReason: preview.policy.text, reason: preview.policy.text, grantSql: null };
  return gate;
}

/** Older preflights return a tri-state hint rather than an action envelope.
 * A target answer, including unknown, always replaces the generic answer. */
export function targetPrivilege(generic: PrivilegeGateResult, hint?: {
  allowed: boolean | null;
  need?: string;
  note?: string;
  checked?: boolean;
  grant_sql?: string | null;
} | null): PrivilegeGateResult {
  if (!hint) return generic;
  const allowed = hint.checked === false && hint.allowed !== true ? null : hint.allowed;
  const hintNeed = (hint.need || "").trim();
  const genericNeed = generic.missing.join(" or ");
  const genericPrimary = generic.missing[0] || "";
  const need = hintNeed || genericNeed;
  // Prefer an explicit backend grant_sql on the hint; otherwise reuse the
  // backend grant_sql from the generic denial only when it grants exactly
  // the hint's need (generic.grantSql always grants generic.missing[0]).
  const grant_sql = allowed === false
    ? hint.grant_sql !== undefined
      ? hint.grant_sql
      : generic.state === "denied" && generic.grantSql && hintNeed &&
          (hintNeed === genericNeed || hintNeed === genericPrimary)
        ? generic.grantSql
        : null
    : null;
  return privilegeResult({ state: allowed === true ? "allowed" : allowed === false ? "denied" : "unknown",
    missing: allowed === false && need ? [need] : [], need,
    reason: hint.note || (allowed === false && !need ? "The server report denies this action." : ""), grey_out: true, grant_sql });
}

const LazyWhyDisabledHost = lazy(() => import("./WhyDisabled").then((m) => ({ default: m.WhyDisabledHost })));

/** App-level WhyDisabled host so privilege-disabled buttons show their reason
 * and backend-supplied advisory GRANT on click. Loads WhyDisabled on first use
 * so the entry chunk stays small. */
export function PrivilegeWhyHost({ resetKey, children }: PropsWithChildren<{ resetKey?: string }>) {
  const [needed, setNeeded] = useState(false);
  const prevReset = useRef(resetKey);
  useEffect(() => onWhyDemand(() => setNeeded(true)), []);
  useEffect(() => {
    if (prevReset.current !== resetKey) {
      prevReset.current = resetKey;
      closeWhyDisabled();
    }
  }, [resetKey]);
  return createElement(Fragment, null, children,
    needed ? createElement(Suspense, { fallback: null }, createElement(LazyWhyDisabledHost, { resetKey })) : null);
}

const CONFIRM_MANAGED_ATTRS = [
  "aria-disabled",
  "aria-describedby",
  "title",
  "data-privilege-state",
  "data-why-reason",
  "data-why-grant",
] as const;
type ConfirmManagedAttr = (typeof CONFIRM_MANAGED_ATTRS)[number];
type SavedConfirmButton = {
  button: HTMLButtonElement;
  hadClass: boolean;
  prev: Partial<Record<ConfirmManagedAttr, string | null>>;
  applied: Partial<Record<ConfirmManagedAttr, string | null>>;
};

function restoreConfirmAttr(saved: SavedConfirmButton, attr: ConfirmManagedAttr) {
  const expected = saved.applied[attr] ?? null;
  if (expected !== null && saved.button.getAttribute(attr) === expected) {
    const prior = saved.prev[attr] ?? null;
    if (prior === null) saved.button.removeAttribute(attr);
    else saved.button.setAttribute(attr, prior);
  }
  saved.applied[attr] = null;
}

function restoreConfirmButton(saved: SavedConfirmButton) {
  for (const attr of CONFIRM_MANAGED_ATTRS) restoreConfirmAttr(saved, attr);
  if (!saved.hadClass) saved.button.classList.remove("privilege-action");
}

/** ConfirmSheet currently accepts a label but no button attributes. Attach
 * accessibility metadata and the disabled-click message through that label,
 * without changing its native busy/form guards. The caller must also guard
 * its onConfirm handler. */
export function PrivilegeConfirmLabel({ gate, children, showStatus = true }: PropsWithChildren<{
  gate: PrivilegeGateResult;
  /** Omit the badge when the dialog already displays permission details. */
  showStatus?: boolean;
}>) {
  const label = useRef<HTMLSpanElement>(null);
  const gateRef = useRef(gate);
  gateRef.current = gate;
  const savedRef = useRef<SavedConfirmButton | null>(null);
  const id = useId();
  useLayoutEffect(() => {
    const button = label.current?.closest("button") as HTMLButtonElement | null;
    if (savedRef.current && savedRef.current.button !== button) {
      restoreConfirmButton(savedRef.current);
      savedRef.current = null;
    }
    if (!button) return;
    if (!savedRef.current) {
      const prev: SavedConfirmButton["prev"] = {};
      for (const attr of CONFIRM_MANAGED_ATTRS) prev[attr] = button.getAttribute(attr);
      savedRef.current = { button, hadClass: button.classList.contains("privilege-action"), prev, applied: {} };
    }
    const saved = savedRef.current;
    const reason = gate.disabledReason;
    const privilegeDenial = gate.state === "denied" && !!reason && reason === gate.reason;
    const desired: Record<ConfirmManagedAttr, string | null> = {
      "aria-disabled": String(!!reason || button.disabled || button.getAttribute("aria-busy") === "true"),
      "aria-describedby": id,
      title: reason || gate.reason || null,
      "data-privilege-state": gate.state,
      "data-why-reason": reason || null,
      "data-why-grant": privilegeDenial && gate.grantSql ? gate.grantSql : null,
    };
    button.classList.add("privilege-action");
    for (const attr of CONFIRM_MANAGED_ATTRS) {
      const next = desired[attr];
      if (next === null) restoreConfirmAttr(saved, attr);
      else {
        if (button.getAttribute(attr) !== next) button.setAttribute(attr, next);
        saved.applied[attr] = next;
      }
    }
  });
  useLayoutEffect(() => () => {
    if (savedRef.current) {
      restoreConfirmButton(savedRef.current);
      savedRef.current = null;
    }
  }, []);
  useEffect(() => {
    const button = label.current?.closest("button");
    if (!button) return;
    const onClick = (event: MouseEvent) => {
      const current = gateRef.current;
      if (!current.disabledReason || button.disabled || button.getAttribute("aria-busy") === "true") return;
      event.preventDefault();
      event.stopPropagation();
      const privilegeDenial = current.state === "denied" && current.disabledReason === current.reason;
      openWhyDisabled({
        anchor: button,
        reason: current.disabledReason,
        grantSql: privilegeDenial ? current.grantSql : null,
      });
    };
    button.addEventListener("click", onClick);
    return () => button.removeEventListener("click", onClick);
  }, []);
  return createElement("span", { ref: label }, children,
    showStatus && gate.state === "unknown" ? createElement("span", { className: "privilege-unverified" }, "Unverified") : null,
    // The reason describes the button (aria-describedby) without becoming
    // part of its name: aria-hidden keeps it out of the label, not the description.
    createElement("span", { id, className: "account-sr", "aria-hidden": "true" }, gate.disabledReason || gate.reason));
}

/** Focusable disabled button: keyboard and pointer activation send nothing and
 * stop propagation, while opening the app-level WhyDisabled message anchored
 * to the button. Spread these props LAST. The server's write preflight remains
 * the enforcement boundary. */
export function privilegeButtonProps(gate: PrivilegeGateResult, onClick: ButtonHTMLAttributes<HTMLButtonElement>["onClick"], blocked = "", describedBy?: string): ButtonHTMLAttributes<HTMLButtonElement> & { "data-why-reason"?: string; "data-why-grant"?: string } {
  const reason = blocked || gate.disabledReason;
  const privilegeDenial = !blocked && gate.state === "denied" && !!gate.disabledReason && gate.disabledReason === gate.reason;
  const grantSql = privilegeDenial && gate.grantSql ? gate.grantSql : undefined;
  const handler: NonNullable<ButtonHTMLAttributes<HTMLButtonElement>["onClick"]> & { whyDisabled?: boolean } = e => {
    if (reason) {
      e.preventDefault();
      e.stopPropagation();
      if (typeof HTMLElement !== "undefined" && e.currentTarget instanceof HTMLElement &&
          e.currentTarget.getAttribute("aria-busy") !== "true") {
        openWhyDisabled({
          anchor: e.currentTarget,
          reason,
          grantSql: privilegeDenial ? gate.grantSql : null,
        });
      }
    } else onClick?.(e);
  };
  if (reason) handler.whyDisabled = true;
  // Only a blocking reason replaces the caller's title (its shortcut hint or
  // why a row cannot run); otherwise the reason stays in aria-describedby.
  return { type: "button", "aria-disabled": !!reason, ...(reason ? { title: reason } : {}),
    "aria-describedby": describedBy,
    "data-why-reason": reason || undefined,
    "data-why-grant": grantSql,
    onClick: handler };
}

/** A tiny button helper; use usePrivilege for existing controls. Unknown is
 * visibly annotated and enabled. Explicit write policy always wins. */
export function PrivilegeGate({ action, gate: targetGate, blocked = "", write = true, children, onClick, ...props }: PropsWithChildren<{
  action?: string; gate?: PrivilegeGateResult; blocked?: string; write?: boolean;
} & ButtonHTMLAttributes<HTMLButtonElement>>) {
  const generic = usePrivilege(action);
  const gate = targetGate || generic;
  const policy = useWritePolicy();
  const context = usePrivileges();
  const descriptionId = useId();
  const reason = blocked || (write ? (context?.readOnly ? "This connection is read-only." : policy?.blocked || "") : "");
  if (!gate.available) return null;
  return createElement(Fragment, null, createElement("button", { ...props, className: `button privilege-action ${props.className || ""}`, ...privilegeButtonProps(gate, onClick, reason, [props["aria-describedby"], descriptionId].filter(Boolean).join(" ")),
    title: reason || gate.disabledReason || props.title || gate.reason,
    "data-privilege-action": action, "data-privilege-state": gate.state }, children),
    createElement("span", { id: descriptionId, className: "account-sr" }, reason || gate.disabledReason || gate.reason));
}

/** Server denials, not network failures or a missing object. Used only to
 * invalidate a cached answer; the backend still decides the action. */
export function isPrivilegeDenial(value: unknown): boolean {
  const reply = value && typeof value === "object" ? value as { code?: unknown; status?: unknown; message?: unknown; error?: unknown } : null;
  if (reply?.status === "denied") return true;
  const codes = [1044, 1045, 1095, 1142, 1143, 1227, 1290, 1370, 1792, 3630];
  if (codes.includes(Number(reply?.code))) return true;
  const message = typeof value === "string" ? value : String(reply?.message || reply?.error || "");
  return /\b(?:access denied|command denied|you need .*privilege)\b/i.test(message) ||
    codes.some(code => new RegExp(`\\bERROR ${code}\\b`, "i").test(message));
}
