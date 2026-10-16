// A .ts module deliberately uses createElement so consumers need no JSX
// wrapper. Server answers own privilege semantics; no grants are parsed here.
import { Fragment, createContext, createElement, useCallback, useContext, useEffect, useId, useLayoutEffect, useRef, useState, type ButtonHTMLAttributes, type PropsWithChildren } from "react";
import type { Connection } from "./api";
import { connectionKey, sharedRead } from "./binding";
import { useWritePolicy } from "./Connections";
import { readPrivileges, type ActionPrivilege, type PrivilegesReport, type PrivilegeState } from "./accountsApi";

export type PrivilegeGateResult = { state: PrivilegeState; missing: string[]; reason: string; disabledReason: string; available: boolean };
type PrivilegesContextValue = {
  key: string; readOnly: boolean; report: PrivilegesReport | null; loading: boolean; error: string;
  refresh: (fresh?: boolean) => Promise<void>; accountChanged: () => Promise<void>;
};
const PrivilegesContext = createContext<PrivilegesContextValue | null>(null);

export function PrivilegesProvider({ connection, connected = true, connectRevision = 0, children }: PropsWithChildren<{
  connection: Connection; connected?: boolean;
  /** Increment after a successful reconnect to the same identity. */
  connectRevision?: number;
}>) {
  const key = connectionKey(connection);
  const latest = useRef({ connection, key, connected });
  latest.current = { connection, key, connected };
  const [value, setValue] = useState<{ key: string; report: PrivilegesReport | null; at: number; error: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const pending = useRef<{ key: string; controller: AbortController; promise: Promise<void> } | null>(null);
  const generation = useRef(0);
  const at = useRef(0);

  const refresh = useCallback((fresh = false): Promise<void> => {
    const c = latest.current;
    if (!c.connected) return Promise.resolve();
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
  }, [key, connected, connectRevision, connection.password, refresh]);
  useEffect(() => {
    const focus = () => { if (Date.now() - at.current > 60_000) void refresh(); };
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
  config: "Configuration",
  config_sources: "Where settings come from",
  top_statements: "Top statements",
  sessions_own: "Your sessions",
  sessions_all: "Every session",
  explain_connection: "Show plan of a session",
  locks_transactions: "Locks: transactions and row locks",
  locks_metadata: "Locks: metadata locks",
  innodb_status: "InnoDB status",
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
  const reason = state === "unknown" ? `Unverified. ${answer?.reason || "SHOW GRANTS cannot tell; the server decides."}` :
    // The server's reason usually says both already ("Needs X; app@% has
    // none of them."): add what it does not say, never repeat it.
    state === "denied" ? [answer?.reason,
      missing.length && !/\bNeeds\b/.test(answer?.reason || "") ? `Needs ${answer?.need || missing.join(" or ")}.` : "",
      account && !(answer?.reason || "").includes(account) ? `Connected as ${account}.` : ""].filter(Boolean).join(" ") : answer?.reason || "Required privileges are present.";
  return { state, missing, reason, available,
    disabledReason: !available ? answer?.note || "Unavailable on this server." : state === "denied" && answer?.grey_out !== false ? reason : "" };
}
export function usePrivilege(actionKey?: string): PrivilegeGateResult {
  const context = usePrivileges();
  return privilegeResult(actionKey ? context?.report?.actions[actionKey] : undefined, context?.report?.account);
}

export type EditorPrivilegePreview = {
  status: "ready" | "invalid";
  statements: { index: number; kind: string; action_keys: string[]; state: PrivilegeState; missing: string[]; reason: string }[];
  aggregate: { state: PrivilegeState; missing: string[]; reason: string; warning: string };
  policy?: { action: string; text: string };
};

/** The server aggregates the exact statements Run sends. Never derive a
 * global Run privilege, or turn a mixed batch into a blanket denial. */
export function editorPrivilegeGate(preview?: EditorPrivilegePreview): PrivilegeGateResult {
  const gate = privilegeResult(preview?.aggregate);
  if (preview?.policy?.action === "refuse")
    return { ...gate, disabledReason: preview.policy.text, reason: preview.policy.text };
  return gate;
}

/** Older preflights return a tri-state hint rather than an action envelope.
 * A target answer, including unknown, always replaces the generic answer. */
export function targetPrivilege(generic: PrivilegeGateResult, hint?: { allowed: boolean | null; need?: string; note?: string; checked?: boolean } | null): PrivilegeGateResult {
  if (!hint) return generic;
  const allowed = hint.checked === false && hint.allowed !== true ? null : hint.allowed;
  const need = hint.need || generic.missing.join(" or ");
  return privilegeResult({ state: allowed === true ? "allowed" : allowed === false ? "denied" : "unknown",
    missing: allowed === false && need ? [need] : [], need,
    reason: hint.note || (allowed === false && !need ? "The server report denies this action." : ""), grey_out: true });
}

/** ConfirmSheet currently accepts a label but no button attributes. Attach
 * accessibility metadata through that label, without changing its native
 * busy/form guards. The caller must also guard its onConfirm handler. */
export function PrivilegeConfirmLabel({ gate, children }: PropsWithChildren<{ gate: PrivilegeGateResult }>) {
  const label = useRef<HTMLSpanElement>(null);
  const id = useId();
  useLayoutEffect(() => {
    const button = label.current?.closest("button");
    if (!button) return;
    button.classList.add("privilege-action");
    button.setAttribute("aria-disabled", String(!!gate.disabledReason || button.disabled));
    button.setAttribute("aria-describedby", id);
    button.title = gate.disabledReason || gate.reason;
    button.dataset.privilegeState = gate.state;
  });
  return createElement("span", { ref: label }, children,
    gate.state === "unknown" ? createElement("span", { className: "privilege-unverified" }, "Unverified") : null,
    // The reason describes the button (aria-describedby) without becoming
    // part of its name: aria-hidden keeps it out of the label, not the description.
    createElement("span", { id, className: "account-sr", "aria-hidden": "true" }, gate.disabledReason || gate.reason));
}

/** Focusable disabled button: keyboard and pointer activation are both
 * suppressed, while the reason remains reachable. Spread these props LAST.
 * The server's write preflight remains the enforcement boundary. */
export function privilegeButtonProps(gate: PrivilegeGateResult, onClick: ButtonHTMLAttributes<HTMLButtonElement>["onClick"], blocked = "", describedBy?: string): ButtonHTMLAttributes<HTMLButtonElement> {
  const reason = blocked || gate.disabledReason;
  // Only a blocking reason replaces the caller's title (its shortcut hint or
  // why a row cannot run); otherwise the reason stays in aria-describedby.
  return { type: "button", "aria-disabled": !!reason, ...(reason ? { title: reason } : {}),
    "aria-describedby": describedBy,
    onClick: e => { if (reason) { e.preventDefault(); e.stopPropagation(); } else onClick?.(e); } };
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
    "data-privilege-action": action, "data-privilege-state": gate.state }, children,
    gate.state === "unknown" && !reason ? createElement("span", { className: "privilege-unverified" }, "Unverified") : null),
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
