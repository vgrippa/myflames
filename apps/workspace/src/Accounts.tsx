// Users and roles keeps all drafts, reports and secrets out of session.ts.
// Only the selected view and column layout are eligible for restore.
import { useCallback, useEffect, useId, useRef, useState, type ButtonHTMLAttributes, type PropsWithChildren } from "react";
import type { Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import { useOpenRead } from "./viewOnScreen";
import { CopySql, engineLabel } from "./PerfShared";
import { VisiblePrivilegeGate } from "./Locks";
import { Checkbox, ConfirmSheet, CountBadge, DataTable, Disclosure, EmptyState, Fields, InspectorSection,
  SearchField, Segmented, Select, StatusDot, SummaryLine, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Selection } from "./kit";
import { PrivilegeGate, privilegeButtonProps, privilegeResult, usePrivilege, usePrivileges, type PrivilegeGateResult } from "./privileges";
import { accountKey, accountLabel, roleLabel, applyAccountGroups, declineAccountGroups, listAccounts, observedScopes, mergeGrantScopes, grantScopeKey, readScopeObjects, planAccountGroups, readAccount, previewAccountPlan, accountPlanProblem,
  type AccountChange, type AccountGroup, type AccountGroupResult, type AccountDetail, type AccountEdit, type AccountName, type AccountPlan, type AccountRow,
  type ScopeObjects, type AccountsReport, type GrantLevel, type GrantScope, type PrivilegeDefinition } from "./accountsApi";
import "./accounts.css";

function AccountGate({ action, gate: targetGate, blocked = "", write = true, children, onClick, ...props }: PropsWithChildren<{
  action?: string; gate?: PrivilegeGateResult; blocked?: string; write?: boolean;
} & ButtonHTMLAttributes<HTMLButtonElement>>) {
  const generic = usePrivilege(action);
  const gate = targetGate || generic;
  const policy = useWritePolicy();
  const context = usePrivileges();
  const descriptionId = useId();
  const reason = blocked || (write ? (context?.readOnly ? "This connection is read-only." : policy?.blocked || "") : "");
  if (!gate.available) return null;
  return <>
    <button
      {...props}
      className={`button privilege-action ${props.className || ""}`}
      {...privilegeButtonProps(gate, onClick, reason, [props["aria-describedby"], descriptionId].filter(Boolean).join(" "))}
      title={reason || gate.disabledReason || props.title || gate.reason}
      data-privilege-action={action}
      data-privilege-state={gate.state}
    >
      {children}
    </button>
    <span id={descriptionId} className="account-sr">{reason || gate.disabledReason || gate.reason}</span>
  </>;
}

type Via = { key: string; label: string; connection: Connection };
type Review = { via: Via; groups: AccountGroup[] };
type Form = { target: AccountName; operation: "create" | "password" | "rename" };
type Pending = { id: string; label: string; edit: AccountEdit };
type Prepare = (target: AccountName, edits: AccountEdit[], password?: string) => Promise<void>;
const SYSTEM_ACCOUNTS = new Set(["mysql.sys", "mysql.session", "mysql.infoschema", "mariadb.sys"]);
const LEVELS: [GrantLevel, string][] = [["global", "Global"], ["database", "Databases"], ["table", "Tables"],
  ["column", "Columns"], ["routine", "Routines"], ["proxy", "Proxy"]];
const OP_LABEL: Record<string, string> = { create: "Create account", drop: "Drop account", rename: "Rename account",
  lock: "Lock account", unlock: "Unlock account", expire: "Expire password", password: "Set password", grant: "Grant privileges", revoke: "Revoke privileges", roles: "Change roles", changes: "Apply changes" };
/** The review question, with the preposition each operation takes. */
function reviewTitle(operation: string, label: string) {
  switch (operation) {
    case "grant": return `Grant privileges to ${label}?`;
    case "revoke": return `Revoke privileges from ${label}?`;
    case "expire": return `Expire the password of ${label}?`;
    case "password": return `Set the password of ${label}?`;
    case "roles": return `Change the roles of ${label}?`;
    default: return OP_LABEL[operation] ? `${OP_LABEL[operation]} ${label}?` : `Apply changes to ${label}?`;
  }
}

/** Keep the backend role classification, with observed role edges as evidence.
 * A locked flag alone is not a role classification. */
function isRole(row: AccountRow, report: AccountsReport) {
  return row.role === true || report.role_grants.some(edge => accountKey(edge.role) === accountKey(row));
}
function yesNo(value: boolean | null | undefined) { return value == null ? "Unknown" : value ? "Yes" : "No"; }

export function AccountsView({ connection, disabled = false, onBack, selection, report: bound, onReport }: {
  connection: Connection; disabled?: boolean; onBack?: () => void; selection: Selection;
  report: Bound<AccountsReport> | null; onReport: (report: Bound<AccountsReport>) => void;
}) {
  const reads = useConnectionReads(connection);
  const details = useConnectionReads(connection);
  const policy = useWritePolicy();
  const privileges = usePrivileges();
  const [detail, setDetail] = useState<Bound<AccountDetail> | null>(null);
  const [search, setSearch] = useState("");
  const createUser = usePrivilege("create_user"), createRole = usePrivilege("create_role");
  const [form, setForm] = useState<Form | null>(null);
  const [review, setReview] = useState<Review | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<Bound<AccountGroupResult[]> | null>(null);
  const [editorRevision, setEditorRevision] = useState(0);
  const [pending, setPending] = useState<Pending[]>([]);
  const [nextSelection, setNextSelection] = useState<{ key: string; open?: boolean } | null>(null);
  const [generated, setGenerated] = useState<{ key: string; password: string } | null>(null);
  const operation = useRef(false);
  const mounted = useRef(true);
  const reviewRef = useRef<Review | null>(null);
  reviewRef.current = review;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const report = bound?.report;
  const engine = report?.engine || privileges?.report?.engine;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const staleReason = other ? "Refresh Users and roles for the connection in use." : "";
  const selected = report?.accounts.find(a => accountKey(a) === selection.key);
  const currentDetail = detail?.key === reads.key && accountKey(detail.report) === selection.key && !other ? detail.report : null;
  const blocked = staleReason || (disabled ? "Wait for the current capture to finish." : "") ||
    (connection.read_only ? "This connection is read-only." : "") || policy?.blocked || (!policy ? "Connection write policy is unavailable." : "");
  const createBlocked = blocked || (busy || review ? "Finish the current review first." : "");
  // Only a reason that stops New user or New role is shown under the toolbar.
  // An unverified answer leaves them enabled; its reason stays in the button's
  // description and the review dialog, where the server decides.
  const createReason = (gate: PrivilegeGateResult) => gate.available ? createBlocked || gate.disabledReason : "";
  const userReason = createReason(createUser), roleReason = createReason(createRole);
  const createNote = userReason && userReason === roleReason ? `New user and New role: ${userReason}` :
    [userReason && `New user: ${userReason}`, roleReason && `New role: ${roleReason}`].filter(Boolean).join(" ");

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; const r = reviewRef.current; if (r && !operation.current) void declineAccountGroups(r.via.connection, r.groups); };
  }, []);
  useEffect(() => { setForm(null); setGenerated(null); setError(""); }, [reads.key]);
  useEffect(() => { setPending([]); setNextSelection(null); }, [reads.key]);

  const load = useCallback(async () => {
    if (disabled || operation.current) return;
    await reads.run("accounts", "Reading users and roles…", listAccounts, onReport);
  }, [disabled, reads.run, onReport]);
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled && !review && !busy,
    busy: !!reads.loading || reads.running(), read: () => { void load(); } });

  function selectAccount(key: string, open?: boolean) {
    if (operation.current || review) return;
    if (key !== selection.key && pending.length) {
      setNextSelection({ key, open });
      return;
    }
    selection.select(key, open);
  }
  useEffect(() => {
    if (!selected || other || !bound) return;
    const target = { ...selected, role: isRole(selected, bound.report) };
    void details.run(`account-detail:${accountKey(target)}`, "Reading account grants…", (c, signal) => readAccount(c, target, signal), setDetail, { force: true });
  }, [selection.key, bound, other, details.run]);

  async function prepare(target: AccountName, edits: AccountEdit[], password?: string) {
    if (operation.current || blocked || review) return;
    const key = reads.key;
    operation.current = true; setBusy("Preparing review…"); setError("");
    try {
      const checked = await policy!.check();
      if (!mounted.current || reads.currentKey() !== key || connectionKey(checked) !== key) throw new Error("The connection changed. Review this account on the connection in use.");
      if (checked.read_only) throw new Error("This connection is read-only.");
      const via = { connection: { ...checked }, key, label: connectionLabel(checked) };
      const groups = await planAccountGroups(via.connection, target, edits, password);
      if (!mounted.current || reads.currentKey() !== key) { void declineAccountGroups(via.connection, groups); return; }
      if (groups.some(g => g.plan?.privileges?.state === "denied")) void privileges?.accountChanged();
      setForm(null); reviewRef.current = { via, groups }; setReview(reviewRef.current);
    } catch (e) {
      if (mounted.current && reads.currentKey() === key) setError((e as Error).message);
    } finally {
      password = undefined; operation.current = false;
      if (mounted.current) setBusy("");
    }
  }

  function cancelReview() {
    if (operation.current || !reviewRef.current) return;
    const r = reviewRef.current;
    reviewRef.current = null; setReview(null);
    void declineAccountGroups(r.via.connection, r.groups);
  }
  function removeGroup(index: number) {
    const r = reviewRef.current;
    if (!r || operation.current) return;
    const group = r.groups[index];
    if (!group) return;
    void declineAccountGroups(r.via.connection, [group]);
    setPending(old => old.filter(p => editGroupKey(p.edit) !== editGroupKey(group.edit)));
    const next = { ...r, groups: r.groups.filter((_, i) => i !== index) };
    reviewRef.current = next.groups.length ? next : null;
    setReview(reviewRef.current);
  }
  async function apply(typedNames: Record<string, string>, password?: string) {
    const r = reviewRef.current;
    if (!r || operation.current || blocked || reads.currentKey() !== r.via.key ||
        r.groups.some(g => g.error || accountPlanProblem(g.plan))) return;
    operation.current = true; setBusy("Applying account changes…"); setError("");
    try {
      const outcomes = await applyAccountGroups(r.via.connection, r.groups, typedNames, async () => {
        const checked = await policy!.check();
        if (!mounted.current || reads.currentKey() !== r.via.key || connectionKey(checked) !== r.via.key)
          throw new Error("The connection changed.");
        if (checked.read_only) throw new Error("This connection is read-only.");
      }, password);
      password = undefined;
      const generatedPassword = outcomes.find(o => o.result?.generated_password)?.result?.generated_password;
      const safe = outcomes.map(o => {
        if (!o.result) return o;
        const { generated_password, ...result } = o.result;
        return { ...o, result };
      });
      if (!mounted.current || reads.currentKey() !== r.via.key) return;
      setResult({ key: r.via.key, label: r.via.label, report: safe });
      setGenerated(generatedPassword ? { key: r.via.key, password: generatedPassword } : null);
      // Discard old checkbox state even after partial or unknown outcomes.
      // The result retains every requested group and its dispatch status.
      setPending([]); setDetail(null);
      await privileges?.accountChanged();
      await reads.run("accounts", "Reading account changes…", listAccounts, onReport, { force: true });
      if (mounted.current && reads.currentKey() === r.via.key) {
        const after = outcomes.map(o => o.result?.after).reverse().find(a => a?.schema_version === "account-1.0");
        const target = after?.exists ? after as AccountDetail : r.groups[0].target;
        await details.run(`account-detail:${accountKey(target)}`, "Reading account grants…",
          (c, signal) => readAccount(c, target, signal), setDetail, { force: true });
        if (reads.currentKey() === r.via.key) selectionRef.current.select(accountKey(target));
      }
    } catch (e) {
      if (mounted.current && reads.currentKey() === r.via.key) setError((e as Error).message);
    } finally {
      password = undefined;
      reviewRef.current = null;
      if (mounted.current) { setReview(null); setEditorRevision(n => n + 1); setBusy(""); }
      operation.current = false;
    }
  }

  const columns: Column<AccountRow>[] = [
    // The shared table measures names, capped at its normal text-column width.
    { key: "account", label: "Account", primary: true, sort: accountLabel, render: a => accountLabel({ ...a, role: report ? isRole(a, report) : a.role }) },
    { key: "type", label: "Type", sort: a => report && isRole(a, report) ? "Role" : "User", render: a => report && isRole(a, report) ? "Role" : "User" },
    { key: "locked", label: "Locked", sort: a => yesNo(a.locked), render: a => yesNo(a.locked) },
    { key: "expired", label: "Expired", sort: a => yesNo(a.expired), render: a => yesNo(a.expired) },
    // Before the long plugin names, so it stays in view beside the inspector.
    { key: "risk", label: "Risky grants", render: a => currentDetail && accountKey(a) === accountKey(currentDetail) ?
      currentDetail.dangerous?.length ? <span title={currentDetail.dangerous.join(", ")}><StatusDot tone="watch" label={`${currentDetail.dangerous.length} risky`} /></span> : "None read" : "Not checked" },
    { key: "plugin", label: "Authentication plugin", sort: a => a.plugin || "", render: a => a.plugin || "Unknown" },
  ];
  const metadataUnreadable = report?.complete === false;
  // The unreadable-list fallback supplies only the connected name and host;
  // it has no role/type or account attributes to judge.
  const shownColumns: Column<AccountRow>[] = metadataUnreadable ? [columns[0],
    { key: "details", label: "Details", render: () => "Not readable" }] : columns;
  const rows = (report?.accounts || []).filter(a => accountLabel(a).toLocaleLowerCase().includes(search.toLocaleLowerCase()));
  const roles = report ? report.accounts.filter(a => isRole(a, report)).length : 0;
  let inspector: InspectorContent | null = null;
  if (selected) inspector = { key: selection.key, title: accountLabel(selected), meta: currentDetail ? engineLabel({ engine: currentDetail.engine, server_version: currentDetail.version }) : undefined,
    actions: currentDetail ? <AccountActions detail={currentDetail} blocked={blocked || (busy || review ? "Finish the current review first." : "") || (pending.length ? "Review or discard pending changes first." : "")}
      onPrepare={prepare} onForm={setForm} /> : undefined,
    body: currentDetail ? <AccountInspector key={`${reads.key}:${selection.key}:${currentDetail.fingerprint}:${editorRevision}`} detail={currentDetail} connection={connection} report={report!}
      blocked={blocked || (busy || review ? "Finish the current review first." : "")} pending={pending} onPending={setPending} onPrepare={prepare} /> :
      details.loading || details.error ? <ViewStatus loading={details.loading} error={details.error} /> :
      <p className="account-note">{other ? "Refresh to inspect this account on the connection in use." : "Select an account to read its grants."}</p> };

  return <ViewLayout id="accounts" title="Users and roles" className="accounts-view" onBack={onBack} otherConnection={other}
    info="Review users, roles and grants. Pending changes run only after you review the server's exact statements and confirm."
    onRefresh={() => void load()} refreshDisabled={disabled || !!busy || !!reads.loading || !!review} loading={!!reads.loading}
    controls={<><SearchField value={search} onChange={setSearch} label="Find an account" />
      <AccountGate action="create_user" blocked={createBlocked}
        onClick={() => setForm({ operation: "create", target: { name: "", host: "%", role: false } })}>New user</AccountGate>
      <AccountGate action="create_role" blocked={createBlocked}
        onClick={() => setForm({ operation: "create", target: { name: "", host: engine === "mariadb" ? "" : "%", role: true } })}>New role</AccountGate></>}
    status={<>
      <ViewStatus loading={busy || reads.loading} error={error || reads.error} />
      {result && result.key === reads.key && <BatchResult results={result.report} onDismiss={() => setResult(null)} />}
      {generated && generated.key === reads.key && <GeneratedPassword password={generated.password} onDismiss={() => setGenerated(null)} />}
    </>} inspector={inspector}
    inspectorEmpty="Select an account to view its grants, account settings and roles.">
    {createNote && <p className="account-note account-create-note" role="note">{createNote}</p>}
    {!report ? <EmptyState icon="server" title="Users and roles" action={reads.error && <button className="button primary" disabled={disabled || !!reads.loading} onClick={() => void load()}>Retry</button>}>Read accounts from the connection in use.</EmptyState> : <>
      <SummaryLine tone={metadataUnreadable ? "unknown" : "info"} toneLabel={metadataUnreadable ? "Not judged" : "Accounts read"} notes={report.warnings}>
        {metadataUnreadable ? report.warnings[0] || "Account metadata could not be read." : `${(report.accounts.length - roles).toLocaleString()} ${report.accounts.length - roles === 1 ? "user" : "users"} · ${roles.toLocaleString()} ${roles === 1 ? "role" : "roles"}`}
      </SummaryLine>
      <DataTable id="accounts.list" label="Users and roles" columns={shownColumns} rows={rows} rowKey={accountKey} selection={{ key: selection.key, select: selectAccount }} empty="No matching accounts." />
    </>}
    {nextSelection && <ConfirmSheet className="account-selection-review" title="Keep pending account changes?"
      description={`There are pending changes for ${selected ? accountLabel(selected) : "the selected account"}. Review them before choosing another account, or discard them.`}
      confirmLabel="Review changes" confirmTone="primary" confirmDisabled={!currentDetail || !!blocked || !!review}
      busy={!!busy} onCancel={() => {
        const open = nextSelection.open;
        setNextSelection(null);
        if (open) selection.select(selection.key, true);
      }} onConfirm={() => {
        const open = nextSelection.open;
        setNextSelection(null);
        if (open) selection.select(selection.key, true);
        if (currentDetail) void prepare(currentDetail, compactChanges(pending.map(p => p.edit)));
      }} secondary={{ label: "Discard", onClick: () => {
        setPending([]); setNextSelection(null);
        selection.select(nextSelection.key, nextSelection.open);
      } }} />}
    {form && <AccountForm key={`${reads.key}:${form.operation}:${accountKey(form.target)}`} form={form} engine={engine}
      randomAvailable={engine === "mariadb" || engine === "mysql"} blocked={blocked} busy={!!busy} error={error}
      onCancel={() => setForm(null)} onPrepare={prepare} />}
    {review && <ReviewSheet review={review} busy={!!busy} blocked={reads.key !== review.via.key ? "The connection changed. Cancel and review again on the connection in use." : blocked}
      error={error} onCancel={cancelReview} onApply={apply} onRemove={removeGroup} />}
  </ViewLayout>;
}

function AccountActions({ detail, blocked, onPrepare, onForm }: {
  detail: AccountDetail; blocked: string; onPrepare: Prepare; onForm: (form: Form) => void;
}) {
  const privileges = usePrivileges();
  const reason = blocked || (detail.system || SYSTEM_ACCOUNTS.has(detail.name) ? "System accounts are read-only." : "");
  const own = privileges?.report?.account === accountLabel(detail);
  const keys = detail.role ? ["drop_role"] : [detail.attributes.locked ? "unlock_account" : "lock_account", "expire_password",
    own ? "set_own_password" : "set_password", "rename_user", "drop_user"];
  const unknown = keys.some(k => privilegeResult(privileges?.report?.actions[k]).state === "unknown");
  // No status inside the buttons: a reason that applies to every action is
  // said once below them, a denial under its own control, and unknown
  // answers get one quiet note for the group.
  const Gate = reason ? PrivilegeGate : VisiblePrivilegeGate;
  const publicDrop = detail.role && detail.engine === "mariadb" && detail.name === "PUBLIC" ? "PUBLIC cannot be dropped." : "";
  return <div className="account-actions-group">
    <div className="account-actions">
      {!detail.role && <>
        <Gate action={detail.attributes.locked ? "unlock_account" : "lock_account"} blocked={reason}
          onClick={() => void onPrepare(detail, [{ operation: detail.attributes.locked ? "unlock" : "lock" }])}>{detail.attributes.locked ? "Unlock" : "Lock"}</Gate>
        <Gate action="expire_password" blocked={reason} onClick={() => void onPrepare(detail, [{ operation: "expire" }])}>Expire password</Gate>
        <Gate action={own ? "set_own_password" : "set_password"} blocked={reason} onClick={() => onForm({ target: detail, operation: "password" })}>Set password</Gate>
        <Gate action="rename_user" blocked={reason} onClick={() => onForm({ target: detail, operation: "rename" })}>Rename</Gate>
      </>}
      <Gate action={detail.role ? "drop_role" : "drop_user"} blocked={reason || publicDrop}
        onClick={() => void onPrepare(detail, [{ operation: "drop" }])}>Drop {detail.role ? "role" : "user"}</Gate>
    </div>
    {reason ? <p className="account-note" role="note">{reason}</p> :
      unknown && <p className="account-note">Some privileges are unverified. The server checks each action during review.</p>}
  </div>;
}

function AccountInspector({ detail, connection, report, blocked, pending, onPending, onPrepare }: {
  detail: AccountDetail; connection: Connection; report: AccountsReport; blocked: string;
  pending: Pending[]; onPending: React.Dispatch<React.SetStateAction<Pending[]>>; onPrepare: Prepare;
}) {
  const [tab, setTab] = useState("grants");
  const [draft, setDraft] = useState<GrantDraft>({ level: "global", added: [], chosen: "", newScope: false });
  const reason = blocked || (SYSTEM_ACCOUNTS.has(detail.name) ? "System accounts are read-only." : "");
  const grant = usePrivilege("grant");
  const revoke = usePrivilege("revoke");
  const rolesGate = usePrivilege("grant_role");
  const revokeRoles = usePrivilege("revoke_role");
  const defaultsGate = usePrivilege("default_roles");
  const unverified = (tab === "grants" ? [grant, revoke] : tab === "roles" ? [rolesGate, revokeRoles, defaultsGate] : []).some(g => g.state === "unknown");
  function stage(id: string, label: string, edit: AccountEdit | null) {
    if (reason) return;
    onPending(old => [...old.filter(p => p.id !== id), ...(edit ? [{ id, label, edit }] : [])]);
  }
  const edits = compactChanges(pending.map(p => p.edit));
  return <div className="account-inspector">
    <Segmented label="Account details" value={tab} onChange={setTab} options={[{ value: "grants", label: "Grants" }, { value: "account", label: "Account" }, { value: "roles", label: "Roles" }]} />
    {reason && <p className="account-note" role="status">{reason}</p>}
    {!reason && unverified && <p className="account-note">Unverified privileges. The server checks the selected changes during review.</p>}
    {tab === "grants" && <GrantMatrix detail={detail} connection={connection} draft={draft} setDraft={setDraft} pending={pending} blocked={reason} grantReason={grant.disabledReason} revokeReason={revoke.disabledReason} onStage={stage} />}
    {tab === "account" && <>
      <InspectorSection title="Account"><Fields rows={[["Type", detail.role ? "Role" : "User"], ["Host", detail.host || "None"], ["Locked", yesNo(detail.attributes.locked)],
        ["Expired", yesNo(detail.attributes.expired)], ["Authentication plugin", detail.attributes.plugins?.join(", ") || detail.attributes.plugin || "Unknown"]]} /></InspectorSection>

    </>}
    {tab === "roles" && <RolesEditor detail={detail} report={report} pending={pending} blocked={reason} grantReason={rolesGate.disabledReason}
      revokeReason={revokeRoles.disabledReason} defaultReason={defaultsGate.disabledReason} onStage={stage} />}
    {!!pending.length && <InspectorSection title="Pending changes"><div className="account-pending" aria-live="polite">
      <CountBadge count={pending.length} label={`${pending.length} pending changes`} /><ul>{pending.map(p => <li key={p.id}>{p.label}</li>)}</ul>
      <button type="button" className="button primary" disabled={!!reason}
        onClick={() => void onPrepare(detail, edits)}>Review changes ({pending.length})</button>
      <button type="button" className="button" disabled={!!blocked} onClick={() => onPending([])}>Discard</button>
    </div></InspectorSection>}
    <Disclosure title="Current grant statements"><CopySql label="Current grants" sql={detail.grants.join(";\n")} /></Disclosure>
    {!!detail.warnings.length && <Disclosure title="Account notes"><ul>{detail.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></Disclosure>}
  </div>;
}

/** Group only edits representable by one backend request. Every group shares
 * a review sheet and gets a fresh fingerprint immediately before apply. */
function editGroupKey(edit: AccountEdit): string {
  const { privileges, roles, ...scope } = edit;
  if (edit.operation === "roles" && edit.mode !== "default") return JSON.stringify(scope);
  if (edit.operation === "grant" || edit.operation === "revoke")
    return JSON.stringify({ ...scope, grantOptionOnly: privileges?.includes("GRANT OPTION") === true });
  return JSON.stringify(edit);
}
export function compactChanges(edits: AccountEdit[]): AccountEdit[] {
  const out: AccountEdit[] = [];
  const grouped = new Map<string, AccountEdit>();
  for (const edit of edits) {
    const roleEdit = edit.operation === "roles" && edit.mode !== "default";
    if (edit.operation !== "grant" && edit.operation !== "revoke" && !roleEdit) { out.push(edit); continue; }
    const key = editGroupKey(edit);
    const existing = grouped.get(key);
    if (existing) {
      if (roleEdit) existing.roles = [...new Map([...(existing.roles || []), ...(edit.roles || [])].map(r => [accountKey(r), r])).values()];
      else existing.privileges = [...new Set([...(existing.privileges || []), ...(edit.privileges || [])])];
    } else {
      const copy = { ...edit, ...(roleEdit ? { roles: [...(edit.roles || [])] } : { privileges: [...(edit.privileges || [])] }) };
      grouped.set(key, copy); out.push(copy);
    }
  }
  return out;
}

type Stage = (id: string, label: string, edit: AccountEdit | null) => void;
/** The shared Checkbox uses native disabled. Account denials must remain
 * keyboard reachable, with the same visual anatomy and a linked reason. */
function AccountCheckbox({ checked, disabled, title, onChange, children }: {
  checked: boolean; disabled?: boolean; title?: string; onChange: (value: boolean) => void; children: React.ReactNode;
}) {
  const id = useId();
  return <label className="checkbox account-checkbox" title={title}>
    <input type="checkbox" checked={checked} aria-disabled={!!disabled} aria-describedby={title ? id : undefined}
      onClick={e => { if (disabled) e.preventDefault(); }} onChange={e => { if (!disabled) onChange(e.target.checked); }} />
    <span>{children}</span>{title && <span id={id} className="account-sr">{title}</span>}
  </label>;
}
function scopeParams(s: GrantScope): Partial<AccountEdit> {
  return { level: s.level, database: s.database, table: s.table, columns: s.columns, routine: s.routine, kind: s.kind, pattern: s.pattern, proxy: s.proxy };
}
/** Routine grant syntax belongs to the server version, not object discovery.
 * Packages and component libraries can still be entered by exact name. */
function routineKinds(engine: string, version: string): [string, string][] {
  const kinds: [string, string][] = [["PROCEDURE", "Procedure"], ["FUNCTION", "Function"]];
  if (engine === "mariadb") kinds.push(["PACKAGE", "Package"], ["PACKAGE BODY", "Package body"]);
  else {
    const [major = 0, minor = 0] = version.split(".").map(Number);
    if (major > 9 || (major === 9 && minor >= 2)) kinds.push(["LIBRARY", "Library"]);
  }
  return kinds;
}
type GrantDraft = { level: GrantLevel; added: GrantScope[]; chosen: string; newScope: boolean };
function GrantMatrix({ detail, connection, draft, setDraft, pending, blocked, grantReason, revokeReason, onStage }: {
  detail: AccountDetail; connection: Connection; draft: GrantDraft; setDraft: React.Dispatch<React.SetStateAction<GrantDraft>>;
  pending: Pending[]; blocked: string; grantReason: string; revokeReason: string; onStage: Stage;
}) {
  const { level, added, chosen, newScope } = draft;
  const update = (patch: Partial<GrantDraft>) => setDraft(old => ({ ...old, ...patch }));
  const all = mergeGrantScopes(observedScopes(detail), added);
  const scopes = all.filter(s => s.level === level);
  const scope = scopes.find(s => s.id === chosen) || scopes[0];
  const catalog = detail.privilege_catalog?.[level];
  const unsupportedKind = scope?.level === "routine" && !!scope.kind && !routineKinds(detail.engine, detail.version).some(([kind]) => kind === scope.kind);
  const scopeReason = unsupportedKind ? "This routine kind is shown for reference. This server version does not support editing it." : "";
  const names: PrivilegeDefinition[] = [...(catalog || [])];
  for (const cell of scope?.cells || []) if (!names.some(n => n.name === cell.privilege)) names.push({ name: cell.privilege });
  const incomplete = !detail.grant_scopes;
  const staticScope = !!scope && level !== "column" && level !== "proxy";
  const scopeOptionId = `grant-option:${scope?.id}`;
  const scopeOption = pending.find(p => p.id === scopeOptionId);
  const staticGrantable = level === "global" ? detail.model?.static_grant_option === true ||
    !!scope?.cells.some(c => c.grantable && !detail.model?.dynamic.includes(c.privilege)) : !!scope?.cells.some(c => c.grantable);
  const scopeOptionChecked = scopeOption ? scopeOption.edit.operation === "grant" : staticGrantable;
  const scopeOptionReason = blocked || scopeReason || (scopeOptionChecked ? revokeReason : grantReason);
  const addScopeReason = blocked || (!catalog?.length ? "The server must supply the privilege catalog for new scopes." : "");
  const addScopeReasonId = useId();
  return <InspectorSection title="Privileges">
    <Select label="Grant level" value={level} options={LEVELS} onChange={v => update({ level: v as GrantLevel, chosen: "", newScope: false })} />
    {!!scopes.length && <Select label="Grant scope" value={scope?.id || ""} options={scopes.map(s => [s.id, s.label])} onChange={chosen => update({ chosen })} />}
    {level !== "global" && <>
      <button type="button" className="button privilege-action" aria-disabled={!!addScopeReason}
        aria-describedby={addScopeReason ? addScopeReasonId : undefined} title={addScopeReason || undefined}
        aria-expanded={newScope} onClick={() => { if (!addScopeReason) update({ newScope: !newScope }); }}>Add scope</button>
      {addScopeReason && <p id={addScopeReasonId} className="account-note">{addScopeReason}</p>}
    </>}
    {newScope && <ScopeForm key={level} level={level} connection={connection} engine={detail.engine} version={detail.version} scopes={all} blocked={blocked}
      onCancel={() => update({ newScope: false })} onAdd={s => {
        const existing = all.find(o => grantScopeKey(o) === grantScopeKey(s));
        update({ added: existing ? added : [...added, s], chosen: existing?.id || s.id, newScope: false });
      }} />}
    {scopeReason && <p className="account-note">{scopeReason}</p>}
    {staticScope && <AccountCheckbox checked={scopeOptionChecked} disabled={!!scopeOptionReason}
      title={scopeOptionReason || "Applies to every static privilege at this scope. Dynamic privileges keep their own grant option."}
      onChange={value => onStage(scopeOptionId, `${value ? "Grant" : "Revoke"} GRANT OPTION on ${scope.label}`,
        value === staticGrantable ? null : { ...scopeParams(scope), operation: value ? "grant" : "revoke", privileges: ["GRANT OPTION"] })}>
      Grant option for static privileges on {scope.label}{scopeOption && <span className="account-cell-note">Pending {scopeOption.edit.operation}</span>}
    </AccountCheckbox>}
    {!scope ? <p className="account-note">Choose Add scope to grant privileges at this level.</p> : <div className="account-matrix-scroll"><table className="account-matrix" aria-label={`${LEVELS.find(l => l[0] === level)?.[1]} privileges`}>
      <thead><tr><th>Privilege</th><th>Granted here</th><th>Grant option</th></tr></thead>
      <tbody>{names.filter(p => p.name !== "GRANT OPTION").map(p => {
        const cell = scope.cells.find(c => c.privilege === p.name);
        const id = `privilege:${scope.id}:${p.name}`;
        const change = pending.find(c => c.id === id);
        const direct = cell?.direct === true;
        const checked = change ? change.edit.operation === "grant" : direct;
        const inherited = !direct && !!cell?.via;
        const disabledReason = blocked || scopeReason || (!checked && p.grantable_here === false ? "This privilege cannot be granted at this level." : "") || (inherited ? `Held ${cell?.via}. Edit that grant.` : "") || (checked ? revokeReason : grantReason);
        const individualOption = level === "proxy" || p.dynamic === true || detail.model?.dynamic.includes(p.name);
        const grantable = change ? change.edit.grant_option === true : cell?.grantable === true;
        const optionReason = blocked || scopeReason || (p.grantable_here === false ? "This privilege cannot be granted at this level." : "") || grantReason || (!checked || inherited ? "Select a direct privilege first." : "") ||
          (cell?.grantable ? "Revoke and grant this privilege in separate reviews to remove its grant option." : "");
        return <tr key={p.name} className={change ? "is-pending" : ""}>
          <th scope="row" title={p.description}><span className="account-privilege-name">{p.name}</span>{p.dangerous && <span className="account-risk">Risky</span>}
            {p.description && <span className="account-cell-note account-privilege-description">{p.description}</span>}
            {cell?.via && <span className="account-cell-note">{cell.via}</span>}{cell?.restricted && <span className="account-cell-note">Restricted</span>}
            {change && <span className="account-cell-note">Pending {change.edit.operation}</span>}</th>
          <td><AccountCheckbox checked={checked} disabled={!!disabledReason} title={disabledReason || p.description}
            onChange={value => onStage(id, `${value ? "Grant" : "Revoke"} ${p.name} on ${scope.label}`,
              value === direct ? null : { ...scopeParams(scope), operation: value ? "grant" : "revoke", privileges: [p.name] })}>
            <span className="account-sr">{p.name} on {scope.label}</span></AccountCheckbox></td>
          <td>{individualOption ? <AccountCheckbox checked={grantable} disabled={!!optionReason} title={optionReason || "Allows passing this privilege to another account."}
            onChange={value => onStage(id, `Grant ${p.name}${value ? " with grant option" : ""} on ${scope.label}`,
              direct && value === !!cell?.grantable ? null : { ...scopeParams(scope), operation: "grant", privileges: [p.name], grant_option: value })}>
            <span className="account-sr">Grant option for {p.name} on {scope.label}</span></AccountCheckbox> : level === "column" ? "At table scope" : "At this scope"}</td>
        </tr>;
      })}</tbody>
    </table></div>}
    {(!catalog || incomplete) && <p className="account-note">{!catalog ? "Only observed privileges are listed; the server did not supply a privilege catalog. " : ""}{incomplete ? "Inherited privileges and restrictions are not projected here. An empty cell means no direct grant was read." : ""}</p>}
  </InspectorSection>;
}

function ScopeForm({ level, connection, engine, version, scopes, blocked, onAdd, onCancel }: {
  level: GrantLevel; connection: Connection; engine: string; version: string; scopes: GrantScope[]; blocked: string;
  onAdd: (scope: GrantScope) => void; onCancel: () => void;
}) {
  const [database, setDatabase] = useState(""); const [object, setObject] = useState("");
  const [column, setColumn] = useState(""); const [pattern, setPattern] = useState(false);
  const [kind, setKind] = useState("PROCEDURE");
  const [manualDatabase, setManualDatabase] = useState(false), [manualObject, setManualObject] = useState(false);
  const [catalog, setCatalog] = useState<ScopeObjects | null>(null), [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [lookup, setLookup] = useState("");
  const key = connectionKey(connection);
  const objectLevel = level === "table" || level === "column" || level === "routine";
  const databases = [...new Map(scopes.filter(s => s.database && !s.pattern).map(s => [s.database!, s])).values()];
  useEffect(() => {
    setCatalog(null); setError(""); setObject(""); setManualObject(false);
    if (!objectLevel || !database || ((manualDatabase || !databases.length) && lookup !== database)) { setLoading(false); return; }
    const controller = new AbortController();
    setLoading(true);
    readScopeObjects(connection, database, controller.signal).then(result => {
      if (!controller.signal.aborted && result.database === database) setCatalog(result);
    }).catch(e => { if (!controller.signal.aborted) setError((e as Error).message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [key, database, objectLevel, manualDatabase, lookup]);
  const names = catalog ? (level === "routine" ? kind === "PROCEDURE" ? catalog.procedures : kind === "FUNCTION" ? catalog.functions : [] : catalog.tables) : [];
  const valid = level === "proxy" ? !!object : !!database && (level === "database" || !!object) && (level !== "column" || !!column);
  return <div className="account-form account-scope-form" aria-label="Add grant scope">
    {level === "proxy" ? <label>Proxied user<input value={database} onChange={e => setDatabase(e.target.value)} /></label> : <>
      {pattern || manualDatabase || !databases.length ? <label>{pattern ? "Database pattern" : "Database name"}<input autoFocus value={database} onChange={e => setDatabase(e.target.value)} /></label> :
        <Select label="Database" value={database} options={[["", "Choose a database"], ...databases.map(s => [s.database!, `${s.database}${s.system ? " (system)" : ""}`] as [string, string])]}
          onChange={setDatabase} />}
      <div className="account-scope-options">{level === "database" && <Checkbox checked={pattern} onChange={value => { setPattern(value); setDatabase(""); }}>Pattern (% and _ are wildcards)</Checkbox>}
        {!pattern && !!databases.length && <button className="button ghost" type="button" onClick={() => { setManualDatabase(!manualDatabase); setDatabase(""); }}>{manualDatabase ? "Choose from databases" : "Enter a database name"}</button>}
      </div>
    </>}
    {objectLevel && (manualDatabase || !databases.length) && database && <button className="button" onClick={() => setLookup(database)}>Read objects</button>}
    {level === "routine" && <Select label="Routine kind" value={kind} options={routineKinds(engine, version)}
      onChange={value => { setKind(value); setObject(""); }} />}
    {level === "routine" && !["PROCEDURE", "FUNCTION"].includes(kind) && <p className="account-note">Enter the exact name. {kind === "LIBRARY" ? "The server must provide the MLE component and library." : "The review includes a temporary Oracle SQL mode for package syntax, then restores the session mode."}</p>}
    {level === "proxy" && <p className="account-note">Leave the user empty for an anonymous account.</p>}
    {level === "proxy" && <label>Proxied host<input value={object} onChange={e => setObject(e.target.value)} /></label>}
    {objectLevel && <>
      {loading && <p className="account-note" role="status">Reading objects…</p>}
      {database && (manualObject || !!error || (!loading && !names.length) ?
        <label>{level === "routine" ? "Routine name" : "Table name"}<input value={object} onChange={e => setObject(e.target.value)} /></label> :
        <Select label={level === "routine" ? "Routine" : "Table"} value={object} options={[["", "Choose an object"], ...names.map(n => [n.name, n.name] as [string, string])]} onChange={setObject} />)}
      {database && !!names.length && <button className="button ghost" type="button" onClick={() => { setManualObject(!manualObject); setObject(""); }}>{manualObject ? "Choose from objects" : "Enter an exact name"}</button>}
      {error && <p className="account-note" role="alert">{error} Enter the exact name to continue.</p>}
      {catalog?.truncated && <p className="account-note">Showing the first 500 objects. Enter an exact name if yours is missing.</p>}
      {!!catalog?.warnings.length && <Disclosure title="Object list notes"><ul>{catalog.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></Disclosure>}
    </>}
    {level === "column" && <label>Column name<input value={column} onChange={e => setColumn(e.target.value)} /></label>}
    <div className="account-scope-actions"><button className="button primary" disabled={!valid || !!blocked} onClick={() => {
      const scope = { level, database: level !== "proxy" ? database : undefined, pattern: level === "database" ? pattern : undefined,
        table: level === "table" || level === "column" ? object : undefined, columns: level === "column" ? [column] : undefined,
        routine: level === "routine" ? object : undefined, kind: level === "routine" ? kind : undefined,
        proxy: level === "proxy" ? { name: database, host: object } : undefined };
      onAdd({ ...scope, id: grantScopeKey(scope), label: level === "proxy" ? `${database || "(anonymous)"}@${object}` : [database, object || (level === "database" ? "*" : ""), column].filter(Boolean).join("."), cells: [] });
    }}>Use scope</button><button className="button" onClick={onCancel}>Cancel</button></div>
  </div>;
}

function RolesEditor({ detail, report, pending, blocked, grantReason, revokeReason, defaultReason, onStage }: {
  detail: AccountDetail; report: AccountsReport; pending: Pending[]; blocked: string;
  grantReason: string; revokeReason: string; defaultReason: string; onStage: Stage;
}) {
  const roles = new Map<string, AccountName>();
  for (const a of [...report.accounts.filter(a => isRole(a, report)), ...detail.roles]) if (accountKey(a) !== accountKey(detail) && !(report.engine === "mariadb" && a.name.toUpperCase() === "PUBLIC")) roles.set(accountKey(a), { ...a, role: true });
  const defaults = pending.find(p => p.id === "default_roles")?.edit.roles || detail.default_roles;
  return <InspectorSection title="Roles">{!roles.size && <p className="account-note">No assignable roles were read.</p>}
    <div className="account-role-list">{[...roles.values()].map(role => {
      const key = accountKey(role); const existing = detail.roles.find(r => accountKey(r) === key);
      const change = pending.find(p => p.id === `role:${key}`);
      const granted = change ? change.edit.mode === "grant" : !!existing;
      const reason = blocked || (granted ? revokeReason : grantReason);
      return <div className={`account-role-row${change ? " is-pending" : ""}`} key={key}>
        <AccountCheckbox checked={granted} disabled={!!reason} title={reason} onChange={value => {
          onStage(`role:${key}`, `${value ? "Grant" : "Revoke"} role ${accountLabel(role)}`,
            value === !!existing ? null : { operation: "roles", mode: value ? "grant" : "revoke", roles: [role] });
          if (!value && defaults.some(r => accountKey(r) === key)) onStage("default_roles", "Set default roles", { operation: "roles", mode: "default", roles: defaults.filter(r => accountKey(r) !== key) });
        }}>{accountLabel(role)}</AccountCheckbox>
        <AccountCheckbox checked={change ? !!change.edit.admin_option : !!existing?.admin_option} disabled={!!(blocked || grantReason) || !granted || existing?.admin_option === true}
          title={existing?.admin_option ? "Removing admin option requires revoking and granting the role again." : blocked || grantReason}
          onChange={value => onStage(`role:${key}`, `Grant role ${accountLabel(role)}${value ? " with admin option" : ""}`,
            existing && value === !!existing.admin_option ? null : { operation: "roles", mode: "grant", roles: [role], admin_option: value })}>Admin option</AccountCheckbox>
        <AccountCheckbox checked={defaults.some(r => accountKey(r) === key)} disabled={!!(blocked || defaultReason) || !existing || !granted || detail.defaults_checked === false}
          title={blocked || defaultReason || (!existing ? "Grant the role before making it a default." : detail.defaults_checked === false ? "Default roles could not be read." : "")}
          onChange={value => {
            const next = value ? (report.engine === "mariadb" ? [role] : [...defaults.filter(r => accountKey(r) !== key), role]) : defaults.filter(r => accountKey(r) !== key);
            const unchanged = next.length === detail.default_roles.length && next.every(r => detail.default_roles.some(d => accountKey(r) === accountKey(d)));
            onStage("default_roles", "Set default roles", unchanged ? null : { operation: "roles", mode: "default", roles: next });
          }}>Default</AccountCheckbox>
      </div>;
    })}</div>
  </InspectorSection>;
}

function AccountForm({ form, engine, randomAvailable, blocked, busy, error, onCancel, onPrepare }: {
  form: Form; engine?: string; randomAvailable: boolean; blocked: string; busy: boolean; error: string; onCancel: () => void;
  onPrepare: Prepare;
}) {
  const [name, setName] = useState(form.target.name); const [host, setHost] = useState(form.target.host);
  const [random, setRandom] = useState(false); const [hasPassword, setHasPassword] = useState(false);
  const password = useRef<HTMLInputElement>(null);
  const needsPassword = !form.target.role && form.operation !== "rename";
  const valid = (form.operation === "password" || !!name) && (form.target.role && engine === "mariadb" || !!host) && (!needsPassword || random || hasPassword);
  function clear() { if (password.current) password.current.value = ""; setHasPassword(false); }
  return <ConfirmSheet eyebrow={form.target.role ? "Role" : "User"} title={form.operation === "create" ? `New ${form.target.role ? "role" : "user"}` : `${OP_LABEL[form.operation]} for ${accountLabel(form.target)}`}
    busy={busy} confirmLabel="Review statements" confirmTone="primary" confirmDisabled={!!blocked || !valid}
    onCancel={() => { clear(); onCancel(); }} onConfirm={() => {
      const secret = needsPassword && !random ? password.current?.value : undefined;
      clear();
      const target = form.operation === "create" ? { ...form.target, name, host: form.target.role && engine === "mariadb" ? "" : host } : form.target;
      void onPrepare(target, [{ operation: form.operation, ...(form.operation === "rename" ? { new_name: name, new_host: host } : {}), ...(random ? { random_password: true } : {}) }], secret);
    }}>
    <div className="account-form">{form.operation !== "password" && <>
      <label>Name<input value={name} autoComplete="off" onChange={e => setName(e.target.value)} disabled={busy} /></label>
      {!(form.target.role && engine === "mariadb") && <label>Host<input value={host} autoComplete="off" onChange={e => setHost(e.target.value)} disabled={busy} /></label>}
    </>}{needsPassword && <>
      {randomAvailable && <Checkbox checked={random} disabled={busy} onChange={v => { clear(); setRandom(v); }}>Generate password</Checkbox>}
      {!random && <label>New password<input ref={password} type="password" autoComplete="new-password" disabled={busy} onChange={e => setHasPassword(e.target.value.length > 0)} /></label>}
      {engine === "mariadb" && <p className="account-note" role="note">This protects the binary log only. The general and slow logs can contain the typed password.</p>}
    </>}{blocked && <p role="status">{blocked}</p>}{error && <p role="alert">{error}</p>}</div>
  </ConfirmSheet>;
}

function ReviewSheet({ review: { groups, via }, busy, blocked, error, onCancel, onApply, onRemove }: {
  review: Review; busy: boolean; blocked: string; error: string; onCancel: () => void;
  onApply: (typedNames: Record<string, string>, password?: string) => Promise<void>; onRemove: (index: number) => void;
}) {
  const [typed, setTyped] = useState<Record<string, string>>({}); const nameId = useId();
  const password = useRef<HTMLInputElement>(null);
  const [hasPassword, setHasPassword] = useState(false);
  const plans = groups.flatMap(g => g.plan ? [g.plan] : []);
  const needsPassword = plans.some(p => !p.role && !p.generates_password && (p.operation === "create" || p.operation === "password"));
  // One typed name per protected account, however many groups touch it.
  const guards = [...new Map(plans.filter(p => p.guards.typed_name).map(p => [accountKey(p), p.guards.typed_name])).entries()];
  function clearPassword() { if (password.current) password.current.value = ""; setHasPassword(false); }
  const invalid = groups.some(g => g.error || accountPlanProblem(g.plan));
  const statements = plans.reduce((n, p) => n + p.statements.length, 0);
  const title = groups.length === 1 && groups[0].plan ? reviewTitle(groups[0].plan.operation, accountLabel(groups[0].plan))
    : `Apply ${groups.length} changes to ${accountLabel(groups[0].target)}?`;
  return <ConfirmSheet className="account-review" eyebrow="Review changes" title={title}
    description={groups.length > 1 ? "Groups run in the order shown. Each is checked again before sending. The batch stops at the first refusal or failure; earlier changes remain applied." : plans[0]?.policy.text}
    busy={busy} confirmLabel={`Apply ${statements} ${statements === 1 ? "statement" : "statements"}`}
    confirmDisabled={!!blocked || invalid || guards.some(([key, name]) => typed[key] !== name) || needsPassword && !hasPassword}
    onCancel={() => { clearPassword(); onCancel(); }} onConfirm={() => {
      const secret = needsPassword ? password.current?.value : undefined;
      clearPassword(); void onApply(typed, secret);
    }}>
    <Fields rows={[["Connection", via.label], ["Type", via.connection.type === "development" ? "Development" : "Production"],
      ["Before sending", groups.length > 1 ? "Re-read account grants, attributes, roles and connection identity for each group." : "Re-read account grants, attributes, roles and connection identity."]]} />
    {/* The confirmations come first, before any long list of grants (a Drop lists every one). */}
    {guards.map(([key, name], i) => <label key={key} className="account-typed" htmlFor={`${nameId}-${i}`}>Type {name} to confirm
      <input id={`${nameId}-${i}`} autoComplete="off" value={typed[key] || ""} disabled={busy}
        onChange={e => setTyped(old => ({ ...old, [key]: e.target.value }))} /></label>)}
    {needsPassword && <label className="account-typed">Re-enter new password to apply
      <input ref={password} type="password" autoComplete="new-password" disabled={busy} onChange={e => setHasPassword(e.target.value.length > 0)} />
      <span className="account-note">The reviewed plan contains no password. This field is cleared when Apply sends it.</span>
    </label>}
    {(blocked || error) && <p role="alert">{blocked || error}</p>}
    {groups.map((group, i) => <section className="account-review-group" key={i} aria-label={`Change group ${i + 1}`}>
      {groups.length > 1 && <h3>{i + 1}. {OP_LABEL[group.edit.operation]}{group.edit.level ? ` (${group.edit.level})` : ""}</h3>}
      {group.plan ? <ReviewPlan plan={group.plan} single={groups.length === 1} /> : <Fields rows={[
        ["Account", accountLabel(group.target)],
        ["Scope", group.edit.proxy ? accountLabel(group.edit.proxy) :
          [group.edit.database, group.edit.table || group.edit.routine, group.edit.columns?.join(", ")].filter(Boolean).join(".") || group.edit.level || "Account"],
        ["Requested", group.edit.privileges?.join(", ") || group.edit.roles?.map(roleLabel).join(", ") || group.edit.special || OP_LABEL[group.edit.operation]],
      ]} />}
      {(group.error || accountPlanProblem(group.plan)) && <p role="alert">{group.error || accountPlanProblem(group.plan)}</p>}
      {groups.length > 1 && <button className="button" disabled={busy} onClick={() => onRemove(i)}>Remove group {i + 1}</button>}
    </section>)}
  </ConfirmSheet>;
}

function ReviewPlan({ plan, single }: { plan: AccountPlan; single: boolean }) {
  const preview = previewAccountPlan(plan);
  return <>
    {!single && <p className="account-note">{plan.policy.text}</p>}
    <Fields rows={[["Server", engineLabel({ engine: plan.engine, server_version: plan.version })], ["Privileges", privilegeResult(plan.privileges).reason]]} />
    <InspectorSection title="Before and after grants">
      <p className="account-note">Requested direct changes. Inherited privileges and the observed result are checked after Apply.</p>
      <div className="account-matrix-scroll"><table className="account-matrix" aria-label="Before and after grants">
        <thead><tr><th>Level / scope</th><th>Change</th><th>Before</th><th>After (requested)</th></tr></thead>
        <tbody>{preview.map((row, i) => <tr key={i}><th scope="row">{row.level} · {row.scope}</th><td>{row.item}</td><td>{row.before}</td><td>{row.after}</td></tr>)}</tbody>
      </table></div>
      {plan.before?.grants.length ? <Disclosure title="Before: current grant statements"><CopySql label="Before grants" sql={plan.before.grants.join(";\n")} /></Disclosure> : null}
    </InspectorSection>
    <InspectorSection title="Statements to apply"><CopySql label="Statements to apply" sql={plan.statements.join(";\n")} /></InspectorSection>
    {!!plan.warnings.length && <div className="account-warnings" role="note"><ul>{plan.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></div>}
    {plan.guards.self_lockout && <p className="account-warning">This change may remove your ability to manage accounts or reconnect.</p>}
    {plan.effects && <Disclosure title="Sessions and defined objects">
      <Fields rows={(plan.effects.sessions || []).map(s => [`Session ${s.id}`, `${s.host} · ${s.command}`])} />
      <Fields rows={(plan.effects.definers || []).map(d => [d.kind, `${d.database}.${d.name}`])} />
      {!plan.effects.sessions?.length && !plan.effects.definers?.length && <p className="account-note">No sessions or defined objects were visible to this account.</p>}
    </Disclosure>}
    {!!plan.guards.warnings?.length && <ul>{plan.guards.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
    {!!plan.effects?.warnings?.length && <ul>{plan.effects.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
  </>;
}

function BatchResult({ results, onDismiss }: { results: AccountGroupResult[]; onDismiss: () => void }) {
  const applied = results.filter(r => r.status === "applied").length;
  return <section className="account-result" aria-label="Account change result">
    <p role="status">{applied} {applied === 1 ? "group" : "groups"} applied, {results.filter(r => r.status === "failed").length} failed, {results.filter(r => r.status === "not_sent").length} not sent.</p>
    {results.map((r, i) => <section className="account-review-group" key={i} data-group-status={r.status}>
      <h3>{i + 1}. {OP_LABEL[r.group.edit.operation]}: {r.status === "not_sent" ? "Not sent" : r.status === "applied" ? "Applied" : "Failed"}</h3>
      {!r.result && <p role={r.status === "failed" ? "alert" : undefined}>{r.message}</p>}
      {r.result ? <ChangeResult result={r.result} /> : r.group.plan && <CopySql label={`Group ${i + 1} statements`} sql={r.group.plan.statements.join(";\n")} />}
    </section>)}
    <button className="button" onClick={onDismiss}>Dismiss result</button>
  </section>;
}

function describeValue(value: unknown): string {
  if (value == null) return "Not read";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.map(describeValue).join(", ") || "None";
  if (typeof value === "object") return Object.entries(value).map(([key, v]) => `${key.replace(/_/g, " ")}: ${describeValue(v)}`).join("; ");
  return String(value);
}

function ChangeResult({ result }: { result: AccountChange }) {
  return <section aria-label="Group result"><div role="status"><strong>{result.status === "applied" ? "Changes applied" : result.status}</strong><p>{result.message}</p></div>
    <ul>{result.statements.map(s => <li key={s.index}><strong>{s.status}</strong> <code>{s.statement}</code></li>)}</ul>
    <div className="account-diff">{result.diff.removed?.map((s, i) => <p key={`r${i}`}>Removed: <code>{s}</code></p>)}{result.diff.added?.map((s, i) => <p key={`a${i}`}>Added: <code>{s}</code></p>)}</div>
    <Disclosure title="Observed before and after">
      {Object.entries(result.diff.levels || {}).filter(([, d]) => d.added.length || d.removed.length).map(([level, diff]) => <InspectorSection key={level} title={level}>
        <Fields rows={[...diff.removed.map(v => ["Removed", describeValue(v)] as [string, string]), ...diff.added.map(v => ["Added", describeValue(v)] as [string, string])]} />
      </InspectorSection>)}
      <Fields rows={Object.keys({ ...result.diff.attributes_before, ...result.diff.attributes_after }).filter(k => describeValue(result.diff.attributes_before?.[k]) !== describeValue(result.diff.attributes_after?.[k]))
        .map(k => [k.replace(/_/g, " "), `${describeValue(result.diff.attributes_before?.[k])} → ${describeValue(result.diff.attributes_after?.[k])}`])} />
      <Fields rows={[["Default roles before", result.diff.defaults_before?.map(roleLabel).join(", ") || "None read"], ["Default roles after", result.diff.defaults_after?.map(roleLabel).join(", ") || "None read"]]} />
    </Disclosure>
    {!!result.warnings.length && <ul>{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
  </section>;
}
function GeneratedPassword({ password, onDismiss }: { password: string; onDismiss: () => void }) {
  const [copied, setCopied] = useState("");
  return <section className="account-result" aria-label="Generated password"><strong>Generated password</strong>
    <p>Shown once. Copy it before dismissing this result.</p><code>{password}</code><div className="account-actions">
      <button className="button" onClick={() => void navigator.clipboard.writeText(password).then(() => setCopied("Copied."), () => setCopied("Copy failed. Select and copy the password."))}>Copy password</button>
      <button className="button" onClick={onDismiss}>Dismiss password</button></div><span role="status">{copied}</span>
  </section>;
}
