// The HTTP boundary for accounts_api.py. Python builds SQL and rechecks
// each reviewed operation. Plan handles expire after 30 minutes; no secrets
// are retained by the endpoint between plan and apply.
import { api, recordDeclined, type Connection } from "./api";

export type PrivilegeState = "allowed" | "denied" | "unknown";
export type ActionPrivilege = {
  state: PrivilegeState;
  allowed?: boolean | null;
  // privileges.can returns alternatives; the endpoint may flatten them.
  missing: string[] | string[][];
  reason: string;
  need?: string;
  note?: string;
  available?: boolean;
  grey_out?: boolean;
};
export type AccountName = { name: string; host: string; role?: boolean };
export type RoleGrant = AccountName & { admin_option?: boolean; default?: boolean; active?: boolean };
export type GrantLevel = "global" | "database" | "table" | "column" | "routine" | "proxy";
export type GrantEntry = {
  privs: string[]; grantable: boolean; source?: AccountName;
  database?: string; pattern?: string; wildcard?: boolean; table?: string;
  columns?: string[]; name?: string; kind?: string; account?: AccountName;
};
export type GrantModel = {
  global: string[]; dynamic: string[]; global_grantable: string[];
  static_grant_option?: boolean;
  schemas: GrantEntry[]; tables: GrantEntry[]; columns: GrantEntry[];
  routines: GrantEntry[]; proxies: GrantEntry[]; restrictions: GrantEntry[];
  roles: RoleGrant[]; unparsed: string[];
};
export type PrivilegesReport = {
  schema_version: "privileges-1.0"; account: string; engine?: string; version?: string;
  current_roles?: RoleGrant[]; read_at?: string; actions: Record<string, ActionPrivilege>;
  model?: GrantModel;
};
export type AccountRow = AccountName & {
  plugin?: string | null; locked?: boolean | null; expired?: boolean | null;
  connected?: boolean; system?: boolean; public?: boolean; no_password?: boolean;
};
export type AccountsReport = {
  schema_version: "accounts-1.0"; engine: "mysql" | "mariadb"; version: string;
  accounts: AccountRow[]; complete: boolean; warnings: string[];
  role_grants: { role: AccountName; grantee: AccountName; admin_option: boolean }[];
  default_roles: { account: AccountName; role: AccountName }[];
};
export type PrivilegeDefinition = { name: string; description?: string; group?: string; dangerous?: boolean; dynamic?: boolean; grantable_here?: boolean };
export type AccountDetail = AccountName & {
  schema_version: "account-1.0"; engine: "mysql" | "mariadb"; version: string;
  exists: boolean; fingerprint: string; grants: string[]; model?: GrantModel;
  effective_model?: GrantModel; dangerous?: string[]; warnings: string[];
  system?: boolean; role_grants?: string[]; attributes_checked?: boolean;
  attributes: { locked?: boolean; expired?: boolean; plugins?: string[]; plugin?: string; [key: string]: unknown };
  roles: RoleGrant[]; default_roles: AccountName[]; defaults_checked?: boolean;
  // Server-supplied, version-specific catalog. Never infer an absent grant
  // or wildcard inheritance in JavaScript. Cells supplied here own that fact.
  privilege_catalog?: Partial<Record<GrantLevel, PrivilegeDefinition[]>>;
  privilege_catalog_meta?: { engine: string; version: string; source: "server" | "static" };
  grant_scopes?: GrantScope[];
};
export type GrantScope = {
  id: string; label: string; level: GrantLevel;
  database?: string; table?: string; columns?: string[]; routine?: string;
  kind?: string; pattern?: boolean; proxy?: AccountName; system?: boolean;
  cells: { privilege: string; direct: boolean; grantable: boolean; via?: string; restricted?: boolean }[];
};
export type AccountOperation = "create" | "drop" | "rename" | "lock" | "unlock" | "expire" | "password" | "grant" | "revoke" | "roles";
export type AccountEdit = {
  operation: AccountOperation;
  new_name?: string; new_host?: string;
  level?: GrantLevel; database?: string; table?: string; columns?: string[];
  routine?: string; kind?: string; pattern?: boolean; proxy?: AccountName;
  privileges?: string[]; grant_option?: boolean;
  mode?: "grant" | "revoke" | "default"; roles?: AccountName[]; admin_option?: boolean;
  special?: "ALL" | "NONE"; random_password?: boolean;
};
export type AccountPlan = AccountName & {
  schema_version: "account-plan-1.0"; plan_id: string; change_set_id: string;
  operation: AccountOperation; account: string; engine: string; version: string;
  request: AccountEdit & { user: string; host: string; role: boolean };
  generates_password: boolean;
  expect: string; status: "ready" | "blocked" | "refused"; statements: string[];
  warnings: string[]; guards: { blocked: string[]; typed_name: string; self_lockout?: boolean; warnings?: string[] };
  privileges?: ActionPrivilege; before?: AccountDetail;
  policy: { action: "allow" | "confirm" | "refuse"; text: string; title?: string };
  effects?: { sessions?: { id: string; host: string; command: string }[];
    definers?: { kind: string; database: string; name: string }[]; warnings?: string[] };
};
export type AccountChange = {
  schema_version: "account-change-1.0"; change_set_id: string; status: string; message: string;
  statements: { index: number; statement: string; status: string }[];
  before?: AccountDetail; after?: AccountDetail | Record<string, never>;
  diff: { added?: string[]; removed?: string[]; levels?: Record<string, { added: unknown[]; removed: unknown[] }>; attributes_before?: Record<string, unknown>; attributes_after?: Record<string, unknown>; defaults_before?: AccountName[]; defaults_after?: AccountName[] };
  warnings: string[]; audit_ids: string[]; generated_password?: string;
};

export const readPrivileges = (connection: Connection, signal?: AbortSignal) => api<PrivilegesReport>("live/privileges", { connection }, signal);
export const listAccounts = (connection: Connection, signal?: AbortSignal) => api<AccountsReport>("accounts/list", { connection }, signal);
export const readAccount = (connection: Connection, account: AccountName, signal?: AbortSignal) =>
  api<AccountDetail>("accounts/read", { connection, user: account.name, host: account.host, role: !!account.role }, signal);

export type ScopeObjects = {
  database: string; tables: { name: string }[]; procedures: { name: string }[]; functions: { name: string }[];
  truncated: boolean; warnings: string[];
};
export const readScopeObjects = (connection: Connection, database: string, signal?: AbortSignal) =>
  api<ScopeObjects>("accounts/scope-objects", { connection, database }, signal);

/** Scope identity uses fields, never server labels or opaque IDs. */
export function grantScopeKey(scope: Partial<GrantScope>): string {
  return JSON.stringify([scope.level, scope.database || "", scope.table || "", [...(scope.columns || [])].sort(),
    scope.routine || "", scope.kind || "", !!scope.pattern, scope.proxy ? accountKey(scope.proxy) : ""]);
}
export function mergeGrantScopes(observed: GrantScope[], added: GrantScope[]): GrantScope[] {
  const scopes = new Map<string, GrantScope>();
  for (const scope of [...observed, ...added]) if (!scopes.has(grantScopeKey(scope))) scopes.set(grantScopeKey(scope), scope);
  return [...scopes.values()];
}

/** Each endpoint accepts one operation; the review orchestrates groups. */
export async function planAccount(connection: Connection, target: AccountName, edits: AccountEdit[], password?: string, signal?: AbortSignal) {
  if (!edits.length) throw new Error("No pending changes.");
  if (edits.length !== 1) throw new Error("Review one operation at a time.");
  const request = edits[0];
  const plan = await api<AccountPlan>("accounts/plan", {
    connection, user: target.name, host: target.host, role: !!target.role, ...request,
    ...(password !== undefined ? { password } : {}),
  }, signal);
  // Refuse an incomplete review instead of accepting serialized executable SQL.
  if (!plan.plan_id || !plan.expect || !Array.isArray(plan.statements) || !plan.statements.length ||
      !plan.policy?.text || !plan.guards || !Array.isArray(plan.guards.blocked) || typeof plan.guards.typed_name !== "string" ||
      !plan.request || !plan.before)
    throw new Error("The server did not return a complete reviewable account plan. Nothing was sent.");
  return plan;
}
export const applyAccount = (connection: Connection, plan: AccountPlan, typedName: string, password?: string) =>
  api<AccountChange>("accounts/apply", { connection, plan_id: plan.plan_id, expect: plan.expect, confirmed: true, typed_name: typedName,
    ...(password !== undefined ? { password } : {}) });
export const declineAccount = (connection: Connection, plan: AccountPlan) =>
  recordDeclined(`accounts.${plan.operation}`, plan.statements, connection, {
    plan_id: plan.plan_id, change_set_id: plan.change_set_id, name: plan.name, host: plan.host, expect: plan.expect,
  });
export const accountKey = (a: AccountName) => JSON.stringify([a.name, a.host]);
export const accountLabel = (a: AccountName) => a.role && !a.host ? a.name : `${a.name}@${a.host}`;
export const roleLabel = (a: AccountName) => accountLabel({ ...a, role: true });

export type AccountGroup = { target: AccountName; edit: AccountEdit; plan?: AccountPlan; error?: string };
export type AccountGroupResult = { group: AccountGroup; status: "applied" | "failed" | "not_sent"; message: string; result?: AccountChange };
export function accountPlanProblem(plan?: AccountPlan): string {
  if (!plan) return "No reviewed plan is available.";
  return plan.guards.blocked.join(" ") || (plan.policy.action === "refuse" ? plan.policy.text : "") ||
    (plan.privileges?.state === "denied" ? plan.privileges.reason : "") ||
    (plan.status !== "ready" ? "This plan cannot be applied." : "");
}

/** Read every group in order, retaining individual errors for review. */
export async function planAccountGroups(connection: Connection, target: AccountName, edits: AccountEdit[], password?: string) {
  const groups: AccountGroup[] = [];
  for (const edit of edits) {
    try { groups.push({ target, edit, plan: await planAccount(connection, target, [edit], password) }); }
    catch (e) { groups.push({ target, edit, error: (e as Error).message }); }
  }
  return groups;
}
export async function declineAccountGroups(connection: Connection, groups: AccountGroup[]) {
  for (const group of groups) if (group.plan) await declineAccount(connection, group.plan);
}

/** One confirmation, sequential one-shot plans. A failed/unknown write is
 * never retried and later groups are never dispatched. Replanning may update
 * the fingerprint after our preceding write, but cannot expand consent. */
export async function applyAccountGroups(connection: Connection, groups: AccountGroup[], typed: Record<string, string>,
  beforeEach: () => Promise<void>, password?: string): Promise<AccountGroupResult[]> {
  const results: AccountGroupResult[] = groups.map(group => ({ group, status: "not_sent", message: "Not sent." }));
  if (groups.some(g => g.error || accountPlanProblem(g.plan))) return results;
  for (let i = 0; i < groups.length; i++) {
    const group = groups[i], reviewed = group.plan!;
    let sent = false;
    try {
      await beforeEach();
      const fresh = await planAccount(connection, group.target, [group.edit], password);
      const problem = accountPlanProblem(fresh);
      if (problem) throw new Error(problem);
      const warnings = (p: AccountPlan) => [...p.warnings, ...(p.guards.warnings || []), ...(p.effects?.warnings || [])];
      if (JSON.stringify(fresh.statements) !== JSON.stringify(reviewed.statements) ||
          fresh.engine !== reviewed.engine || fresh.version !== reviewed.version ||
          warnings(fresh).some(w => !warnings(reviewed).includes(w)) ||
          (fresh.guards.self_lockout && !reviewed.guards.self_lockout) ||
          (fresh.guards.typed_name && fresh.guards.typed_name !== reviewed.guards.typed_name))
        throw new Error("The statements or safety checks changed. Review this group again. Nothing was sent for it.");
      const name = typed[accountKey(group.target)] || "";
      if (fresh.guards.typed_name && name !== fresh.guards.typed_name)
        throw new Error("Type the protected account name before applying this group.");
      // A connection switch while planning must also stop the next write.
      await beforeEach();
      sent = true;
      const result = await applyAccount(connection, fresh, name, password);
      results[i] = { group, status: result.status === "applied" ? "applied" : "failed", message: result.message, result };
      if (result.status !== "applied") break;
    } catch (e) {
      results[i] = { group, status: "failed", message: sent ?
        `The apply response was lost or failed. This group may have run. Read the account and Audit log before reviewing again. ${(e as Error).message}` :
        `Nothing was sent for this group. ${(e as Error).message}` };
      break;
    }
  }
  return results;
}

/** Only direct, already observed grants are reconstructed as editable
 * scopes. Inherited grants and partial revokes need server-projected cells. */
export function observedScopes(detail: AccountDetail): GrantScope[] {
  if (detail.grant_scopes) return detail.grant_scopes;
  const m = detail.model;
  if (!m) return [];
  const scopes: GrantScope[] = [{ id: "global", label: "*.*", level: "global", cells: [...m.global, ...m.dynamic]
    .filter(p => p !== "USAGE").map(privilege => ({ privilege, direct: true, grantable: m.global_grantable.includes(privilege) })) }];
  const buckets = [["database", m.schemas], ["table", m.tables], ["column", m.columns], ["routine", m.routines], ["proxy", m.proxies]] as const;
  for (const [level, entries] of buckets) for (const entry of entries) {
    const scope = { level, database: entry.database, table: entry.table, columns: entry.columns,
      routine: entry.name, kind: entry.kind, pattern: entry.wildcard, proxy: entry.account };
    // Preserve raw schema patterns from SHOW GRANTS. A literal escaped name
    // cannot safely be converted to a pattern in the browser.
    if (level === "database") scope.database = entry.wildcard ? entry.pattern : entry.database;
    const id = JSON.stringify(scope);
    let row = scopes.find(s => s.id === id);
    if (!row) {
      row = { ...scope, id, label: level === "proxy" && entry.account ? accountLabel(entry.account) :
        [scope.database, scope.table || scope.routine || (level === "database" ? "*" : ""), scope.columns?.join(", ")].filter(Boolean).join("."), cells: [] };
      scopes.push(row);
    }
    for (const privilege of entry.privs) if (!row.cells.some(c => c.privilege === privilege))
      row.cells.push({ privilege, direct: true, grantable: entry.grantable });
  }
  return scopes;
}

export type GrantPreviewRow = { level: string; scope: string; item: string; before: string; after: string };
/** A preview of the requested direct changes, not a simulated SHOW GRANTS or
 * an effective privilege calculation. Python supplies both the read and the
 * validated request; inherited grants and server normalization stay with it. */
export function previewAccountPlan(plan: AccountPlan): GrantPreviewRow[] {
  const edit = plan.request;
  const before = plan.before;
  if (!before) return [];
  const rows: GrantPreviewRow[] = [];
  const add = (level: string, scope: string, item: string, old: string, next: string) => rows.push({ level, scope, item, before: old, after: next });
  if (edit.operation === "grant" || edit.operation === "revoke") {
    const level = edit.level || "global";
    const scopes = observedScopes(before).filter(s => s.level === level &&
      (level === "global" || level === "proxy" ? level !== "proxy" || !!s.proxy && !!edit.proxy && accountKey(s.proxy) === accountKey(edit.proxy) :
        s.database === edit.database && (level !== "database" || !!s.pattern === !!edit.pattern) &&
        (!edit.table || s.table === edit.table) && (!edit.routine || s.routine === edit.routine && s.kind === edit.kind) &&
        (!edit.columns || edit.columns.every(c => s.columns?.includes(c)))));
    const scope = scopes[0]?.label || (level === "global" ? "*.*" : edit.proxy ? accountLabel(edit.proxy) :
      [edit.database, edit.table || edit.routine, edit.columns?.join(", ")].filter(Boolean).join("."));
    const cells = scopes.flatMap(s => s.cells);
    for (const name of edit.privileges || []) {
      const cell = cells.find(c => c.privilege === name && c.direct);
      const viaAll = cells.some(c => c.direct && (c.privilege === "ALL" || c.privilege === "ALL PRIVILEGES"));
      const option = name === "GRANT OPTION";
      const old = option ? (level === "global" ? !!before.model?.static_grant_option : cells.some(c => c.grantable)) : !!cell;
      add(level, scope, name, old ? option ? "Granted at scope" : `Granted${cell?.grantable ? "; grant option" : ""}` : viaAll ? "Included in ALL" : "No direct grant read",
        edit.operation === "revoke" ? option ? "Scope grant option removed" : "Direct privilege removed" :
          option ? "Granted at scope" : `Granted${edit.grant_option || cell?.grantable ? "; grant option" : ""}`);
    }
  } else if (edit.operation === "roles") {
    if (edit.mode === "default") add("roles", accountLabel(plan), "Default roles", before.default_roles.map(roleLabel).join(", ") || "None",
      edit.special || edit.roles?.map(roleLabel).join(", ") || "None");
    else for (const role of edit.roles || []) {
      const old = before.roles.find(r => accountKey(r) === accountKey(role));
      add("roles", accountLabel(plan), roleLabel(role), old ? `Granted${old.admin_option ? "; admin option" : ""}` : "Not granted directly",
        edit.mode === "revoke" ? "Direct role grant removed" : `Granted${edit.admin_option || old?.admin_option ? "; admin option" : ""}`);
    }
  } else if (edit.operation === "drop") {
    for (const scope of observedScopes(before)) for (const cell of scope.cells.filter(c => c.direct))
      add(scope.level, scope.label, cell.privilege, `Granted${cell.grantable ? "; grant option" : ""}`, "Account removed");
    for (const role of before.roles) add("roles", accountLabel(plan), roleLabel(role), "Granted", "Account removed");
    add("account", accountLabel(plan), "Exists", "Yes", "No");
  } else if (edit.operation === "rename") add("account", accountLabel(plan), "Name", accountLabel(before),
    accountLabel({ name: edit.new_name || before.name, host: edit.new_host ?? before.host }));
  else if (edit.operation === "create") add("account", accountLabel(plan), "Exists", "No", "Yes");
  else if (edit.operation === "password") add("account", accountLabel(plan), "Password", "Not read", plan.generates_password ? "Generated; shown once after Apply" : "Replaced; redacted");
  else {
    const key = edit.operation === "expire" ? "expired" : "locked";
    add("account", accountLabel(plan), key === "expired" ? "Password expired" : "Locked",
      before.attributes[key] == null ? "Unknown" : before.attributes[key] ? "Yes" : "No", edit.operation === "unlock" ? "No" : "Yes");
  }
  return rows;
}
