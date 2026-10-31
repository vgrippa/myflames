// Parameters and Users and roles share the compare anatomy of Schema versions:
// the Source → Target bar with Swap, the Differences table with status words,
// and a full-width detail whose Back keeps the list. Only Compare and Export
// read servers, and only when pressed; choices and snapshots are local data.
// The saved connections are the window's shared list (savedConnections.ts):
// one created, renamed or deleted on Home shows here at once.
import { useContext, useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { ConfigReport, Connection } from "./api";
import { connectionKey, connectionLabel, type Bound } from "./binding";
import { Icon } from "./components";
import { ComparisonBadge, ComparisonSetup } from "./compareShared";
import { ViewOnScreenContext, useOpenRead, useViewOnScreen } from "./viewOnScreen";
import { ActionButton, ActionStatus, Checkbox, ConfirmSheet, DataTable, Disclosure, EmptyState, FormGrid, FormRow,
  Pill, SearchField, Segmented, Select, SummaryLine, ToolbarSelect, ViewLayout, ViewSwitcherContext, copyToClipboard, useAction, useInspector,
  type Column, type InspectorContent, type ViewSwitcherShared } from "./kit";
import { localTime } from "./schemaVersionsApi";
import { deletedConnectionLabel, refreshSavedConnections, savedConnectionDeleted, seenConnectionName, useSavedConnections } from "./savedConnections";
import { ParameterVersions, type HistoryRequest } from "./ParameterVersions";
import type { VersionEngine } from "./paramVersionsApi";
import { COMPARE_ORDER, accountDifferenceText, address, compareAccounts, compareParameters, comparisonSummary, deleteSnapshot,
  displayValue, exportAccountName, exportGrants, listSnapshots, onlyInSourceForExport, recordText, saveSnapshot, tlsText,
  type AccountDifference, type AccountValue, type CompareRole, type CompareSide, type CompareStatus, type Comparison,
  type GrantsExport, type ParameterDifference, type SideMetadata, type Snapshot } from "./compareApi";

type Kind = "parameters" | "accounts";
type Difference = ParameterDifference | AccountDifference;
type Pair = { source: CompareSide; target: CompareSide };
/** `sides`: the names of the sides the report came from, kept in the bar after
 * Swap or Change… until Compare runs again. */
type Result = { report: Comparison<Difference>; key: string; label: string; pair: Pair; choices: string; sides: [string, string] };
const rowKey = (row: Difference) => "account" in row ? JSON.stringify([row.name, row.host]) : row.name;
const snapshotLabel = (s: Snapshot) => `#${s.number}${s.label ? ` · ${s.label}` : ""} · ${address(s.server)} · ${localTime(s.captured_at)}`;
const LABELS: Record<Kind, string> = { parameters: "Parameters", accounts: "Users and roles" };
const yesNo = (value: boolean | null | undefined) => value == null ? "Unknown" : value ? "Yes" : "No";
const plural = (n: number, one: string) => `${n.toLocaleString()} ${n === 1 ? one : `${one}s`}`;
/** A raw parameter value as read; an empty string is marked, never left blank. */
const rawValue = (value: unknown) => value === "" ? <span className="muted">Empty</span> : displayValue(value);

/** What one side of a finished comparison was: a snapshot, a saved
 * connection or an address, never a guess when it could not be read. */
function sideLabel(side: SideMetadata) {
  if (side.kind === "unavailable") return "Not read";
  if (side.kind === "snapshot") return `Snapshot #${side.number ?? "?"}${side.label ? ` (“${side.label}”)` : ""} of ${address(side.server)}`;
  const name = side.server?.profile_name;
  return `${side.kind === "file" ? "File · " : ""}${name ? `${name} · ` : ""}${address(side.server)}`;
}
function sideTime(side: SideMetadata) {
  if (!side.captured_at) return null;
  return <time dateTime={side.captured_at}> · {side.kind === "live" ? "read" : "captured"} {localTime(side.captured_at)}</time>;
}

function ComparePill({ status, text }: { status: CompareStatus; text: string }) {
  return <ComparisonBadge status={status}>{text}</ComparisonBadge>;
}

/** Source, the direction (an arrow over the swap button), Target, then
 * Compare: Schema versions' bar, with one choice per side. A side's read
 * error is shown once, inside that side. */
export function CompareSidesBar({ options, source, target, onSource, onTarget, onSwap, sourceError, targetError, busy, problem, disabled, onCompare, hasResult = false, missingLabel }: {
  options: [string, string][]; source: string; target: string; onSource: (v: string) => void; onTarget: (v: string) => void;
  /** How a chosen side that is not offered any more is named ("Alpha (deleted)"). */
  missingLabel?: (value: string) => string;
  hasResult?: boolean; onSwap: () => void; sourceError?: string; targetError?: string; busy: boolean; problem: string; disabled: boolean; onCompare: () => void;
}) {
  return <div className="server-compare-fields">
    <SidePicker label="Source" value={source} onChange={onSource} options={options} error={sourceError} missingLabel={missingLabel} />
    {!hasResult && <div className="server-compare-direction">
      <span className="server-compare-arrow" aria-hidden="true"><Icon name="chevron" size={16} /></span>
      <button type="button" className="icon-button server-compare-swap" aria-label="Swap source and target" title="Swap source and target" onClick={onSwap}>
        <Icon name="compare" />
      </button>
    </div>}
    <SidePicker label="Target" value={target} onChange={onTarget} options={options} error={targetError} missingLabel={missingLabel} />
    {!hasResult && <div className="server-compare-run">
      <ActionButton primary icon="compare" busy={busy} busyLabel="Comparing…" disabled={disabled || !!problem}
        title={problem || "Compare the source with the target"} onClick={onCompare}>Compare</ActionButton>
    </div>}
  </div>;
}
function SidePicker({ label, value, onChange, options, error, missingLabel }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][]; error?: string; missingLabel?: (value: string) => string;
}) {
  const id = useId();
  // A chosen side that is gone stays chosen and says so, so the choice is never silently another one.
  const choices = options.some(([v]) => v === value) ? options : [...options, [value, missingLabel?.(value) || "No longer available"] as [string, string]];
  return <fieldset className="server-compare-side">
    <legend>{label}</legend>
    <Select label={label} value={value} onChange={onChange} options={choices} describedBy={error ? `${id}-error` : undefined} />
    {error && <p className="server-compare-error" id={`${id}-error`} role="alert"><Icon name="warning" size={12} />{error}</p>}
  </fieldset>;
}

/** What the list tab can open in another tab: Parameters' Version history. */
export type ListLinks = { openVersionHistory?: (name: string, engine: VersionEngine | null) => void };
type HostTab = "list" | "compare" | "versions";

/** Keeps each existing list mounted, including drafts and late write outcomes.
 * The hidden tab receives false visibility so its useOpenRead cannot run.
 * Parameters adds Versions (the release catalog; it reads no server), mounted
 * on its first visit like Compare. */
export function ServerCompareHost({ kind, connection, disabled = false, onBack, renderList, versions }: {
  kind: Kind; connection: Connection; disabled?: boolean; onBack?: () => void; renderList: (controls: ReactNode, links: ListLinks) => ReactNode;
  /** Parameters only: the Parameters report and the list's selection, for Versions. */
  versions?: { report: Bound<ConfigReport> | null; selected: string };
}) {
  const [tab, setTab] = useState<HostTab>("list");
  const [visited, setVisited] = useState(false);
  const [versionsVisited, setVersionsVisited] = useState(false);
  const [historyRequest, setHistoryRequest] = useState<HistoryRequest | null>(null);
  const hasVersions = kind === "parameters" && !!versions;
  const onScreen = useViewOnScreen();
  const inspector = useInspector();
  const [saveSheet, setSaveSheet] = useState(false);
  const [label, setLabel] = useState("");
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  // A save is a request the user made: it is never dropped when the view is
  // hidden, and a slow one can be left (Close) while its outcome follows here.
  const saving = useAction();
  const saveVia = useRef<Connection | null>(null);
  // Both tabs keep a view title; only the tab on screen may open the floating
  // list of views (two open lists would close each other at once).
  const switcher = useContext(ViewSwitcherContext);
  const switcherFor = (which: HostTab): ViewSwitcherShared | null => switcher && { ...switcher, active: switcher.active && tab === which };
  const viewSwitch = <Segmented label={`${LABELS[kind]} view`} tabs={`${kind}-views`} value={tab} onChange={next => {
    inspector?.setOpen(false); setTab(next); if (next === "compare") setVisited(true); if (next === "versions") setVersionsVisited(true);
    window.requestAnimationFrame(() => document.getElementById(`${kind}-views-tab-${next}`)?.focus());
  }} options={[{ value: "list", label: LABELS[kind] }, { value: "compare", label: "Compare" }, ...(hasVersions ? [{ value: "versions" as const, label: "Versions" }] : [])]} />;
  // Version history in the Parameters inspector: Versions > Parameter history.
  const links: ListLinks = hasVersions ? { openVersionHistory: (name, engine) => {
    inspector?.setOpen(false); setTab("versions"); setVersionsVisited(true);
    setHistoryRequest(old => ({ name, engine, n: (old?.n || 0) + 1 }));
  } } : {};
  const controls = <>{viewSwitch}
    {kind === "parameters" && <button type="button" className="button" data-action="save-parameter-snapshot" aria-haspopup="dialog"
      disabled={disabled || saving.pending} title={`Save the parameters of ${connectionLabel(connection)} on this computer`}
      onClick={() => { saveVia.current = { ...connection }; saving.reset(); setLabel(""); setSaveSheet(true); }}>Save snapshot…</button>}</>;
  async function save() {
    const via = saveVia.current;
    if (!via) return;
    const result = await saving.run(signal => saveSnapshot(via, label.trim(), signal), {
      pending: `Reading parameters on ${connectionLabel(via)}…`, done: s => `Saved snapshot #${s.number} of ${address(s.server)}.`,
    });
    if (result.ok) {
      setSnapshots(old => [result.value, ...old.filter(s => s.id !== result.value.id)]);
      setSaveSheet(false);
    }
  }
  const saveFeedback = <ActionStatus className="server-compare-save-status" state={saveSheet ? { phase: "idle", message: "", startedAt: null } : saving} onDismiss={saving.reset} />;
  return <div className="server-compare-host" data-compare-host={kind}>
    <ViewSwitcherContext.Provider value={switcherFor("list")}><ViewOnScreenContext.Provider value={onScreen && tab === "list"}><div className="server-compare-tab-panel" id={`${kind}-views-panel-list`} role="tabpanel" aria-labelledby={`${kind}-views-tab-list`} hidden={tab !== "list"}>
      {renderList(tab === "list" ? <>{controls}{saveFeedback}</> : null, links)}
    </div></ViewOnScreenContext.Provider></ViewSwitcherContext.Provider>
    <ViewSwitcherContext.Provider value={switcherFor("compare")}><ViewOnScreenContext.Provider value={onScreen && tab === "compare"}><div className="server-compare-tab-panel" id={`${kind}-views-panel-compare`} role="tabpanel" aria-labelledby={`${kind}-views-tab-compare`} hidden={tab !== "compare"}>
      {visited && <ServerCompare kind={kind} connection={connection} disabled={disabled} controls={tab === "compare" ? <>{controls}{saveFeedback}</> : null} onBack={onBack}
        snapshots={snapshots} onSnapshots={setSnapshots} />}
    </div></ViewOnScreenContext.Provider></ViewSwitcherContext.Provider>
    {hasVersions && <ViewSwitcherContext.Provider value={switcherFor("versions")}><ViewOnScreenContext.Provider value={onScreen && tab === "versions"}><div className="server-compare-tab-panel" id={`${kind}-views-panel-versions`} role="tabpanel" aria-labelledby={`${kind}-views-tab-versions`} hidden={tab !== "versions"}>
      {/* Versions reads only the catalog shipped with myflames: no Save snapshot here. */}
      {versionsVisited && <ParameterVersions connection={connection} controls={tab === "versions" ? viewSwitch : null} onBack={onBack}
        parameters={versions!.report} selectedName={versions!.selected} request={historyRequest} />}
    </div></ViewOnScreenContext.Provider></ViewSwitcherContext.Provider>}
    {saveSheet && <ConfirmSheet eyebrow="Save snapshot" title="Save a parameter snapshot"
      description={<>myflames reads the global parameters of {connectionLabel(saveVia.current || connection)} (nothing on the server changes) and keeps them on this computer to compare later.</>}
      confirmLabel="Save snapshot" confirmTone="primary" busy={saving.pending} busyLabel="Saving…" progress={saving.pending ? saving.message : undefined}
      onLeave={() => setSaveSheet(false)} leaveText="The snapshot is still being read. Close does not stop it; the outcome shows in the toolbar."
      returnFocus={() => document.querySelector<HTMLElement>(`[data-compare-host="${kind}"] [data-action="save-parameter-snapshot"]`)}
      onCancel={() => setSaveSheet(false)} onConfirm={() => void save()}>
      <FormGrid>
        <FormRow label="Label" htmlFor={`${kind}-snapshot-label`} hint="Optional, for example the change it comes before.">
          <input id={`${kind}-snapshot-label`} aria-label="Snapshot label" value={label} maxLength={200} disabled={saving.pending}
            onChange={e => setLabel(e.target.value.replace(/[\r\n]/g, " "))} />
        </FormRow>
      </FormGrid>
      {saving.phase === "failed" && <p className="form-error" role="alert">{saving.message}</p>}
    </ConfirmSheet>}
  </div>;
}

function ServerCompare({ kind, connection, disabled, controls, onBack, snapshots, onSnapshots }: {
  kind: Kind; connection: Connection; disabled: boolean; controls: ReactNode; onBack?: () => void; snapshots: Snapshot[]; onSnapshots: (v: Snapshot[] | ((old: Snapshot[]) => Snapshot[])) => void;
}) {
  const onScreen = useViewOnScreen();
  const inspector = useInspector();
  const key = connectionKey(connection);
  const current = useRef(key); current.current = key;
  const saved = useSavedConnections();
  const profiles = saved.items;
  const [catalogRead, setCatalogRead] = useState(false);
  const catalog = useAction();
  const [source, setSourceState] = useState("current"), [target, setTargetState] = useState("current");
  // Until the user picks a side, Target follows the most useful choice the
  // local lists offer (pre-filled, never typed); a pick or a swap ends that.
  const picked = useRef(false);
  const setSource = (v: string) => { picked.current = true; setSourceState(v); };
  const setTarget = (v: string) => { picked.current = true; setTargetState(v); };
  const choiceKey = JSON.stringify([source, target]);
  // A switch of the connection in use drops a comparison on its way; a change
  // of the chosen sides does not (the result then says the sides changed).
  const comparing = useAction(key);
  const exporting = useAction(key);
  const copying = useAction();
  const [result, setResult] = useState<Result | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [showIdentical, setShowIdentical] = useState(false), [showIdentity, setShowIdentity] = useState(false);
  const [selected, setSelected] = useState("");
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [exportSide, setExportSide] = useState<"source" | "target">("source");
  // What "All accounts only in source" chose, so the click always says what it did.
  const [choiceNote, setChoiceNote] = useState("");
  const [sql, setSql] = useState<GrantsExport | null>(null);
  const [manage, setManage] = useState(false), [deleting, setDeleting] = useState<Snapshot | null>(null);
  const deletion = useAction();
  // Bumped by every local snapshot change: a list read that started before
  // a save or a delete must not put the old list back.
  const snapshotChanges = useRef(0);
  const lastSnapshots = useRef(snapshots);
  if (lastSnapshots.current !== snapshots) { lastSnapshots.current = snapshots; snapshotChanges.current += 1; }
  async function loadCatalog(asked = false) {
    const revision = ++snapshotChanges.current;
    const loaded = await catalog.run(async signal => {
      // Local workspace data only (saved connections, snapshots): no server is read.
      // The saved connections are read into the window's shared list, so every
      // other list of them shows the same answer.
      const [items, s] = await Promise.all([refreshSavedConnections(), kind === "parameters" ? listSnapshots(signal) : Promise.resolve({ items: [] as Snapshot[] })]);
      return { profiles: items, snapshots: s.items };
    }, { pending: "Reading saved connections and snapshots…", done: v => asked ? `Choices updated: ${plural(v.profiles.length, "saved connection")}${kind === "parameters" ? `, ${plural(v.snapshots.length, "snapshot")}` : ""}.` : "" });
    if (loaded.ok) {
      if (revision === snapshotChanges.current && kind === "parameters") onSnapshots(loaded.value.snapshots);
      setCatalogRead(true);
    }
  }
  useOpenRead({ key: `compare-catalog-${kind}`, reportKey: catalogRead ? `compare-catalog-${kind}` : null, ready: true,
    busy: catalog.pending, read: () => { void loadCatalog(); } });
  const options: [string, string][] = [["current", `This connection · ${connectionLabel(connection)}`],
    ...profiles.filter(p => !connection.profile_id || p.id !== connection.profile_id)
      .map((p): [string, string] => [`profile:${p.id}`, `${connectionLabel(p)}${p.has_saved_password ? "" : " · no saved password"}`])];
  if (kind === "parameters") {
    const servers = new Map<string, Snapshot>();
    for (const s of snapshots) if (!servers.has(s.source_key)) servers.set(s.source_key, s);
    for (const [skey, s] of servers) options.push([`latest:${skey}`, `Latest snapshot of ${address(s.server)}`]);
    for (const s of snapshots) options.push([`snapshot:${s.id}`, `Snapshot ${snapshotLabel(s)}`]);
  }
  const known = (v: string) => options.some(([o]) => o === v);
  /** A chosen saved connection that was deleted keeps its name; a deleted
   * snapshot (or a choice not offered any more) is No longer available. */
  const deletedProfile = (v: string) => v.startsWith("profile:") && savedConnectionDeleted(saved, v.slice(8));
  const missingLabel = (v: string) => deletedProfile(v) ? deletedConnectionLabel(v.slice(8)) : v.startsWith("profile:") && !saved.loaded ? "Saved connections not read yet" : "No longer available";
  const optionLabel = (v: string) => options.find(([o]) => o === v)?.[1] || missingLabel(v);
  const goneText = (side: "Source" | "Target", v: string) => {
    if (!deletedProfile(v)) return `${side}: that choice is no longer available.`;
    const name = seenConnectionName(v.slice(8));
    return `${side}: ${name ? `the saved connection “${name}”` : "the chosen saved connection"} was deleted. Choose another ${side.toLowerCase()}.`;
  };
  // Parameters: the latest snapshot of this server (what changed since), else
  // another saved connection, else any snapshot. Accounts: another saved connection.
  const own = snapshots.find(s => s.server.host === connection.host && Number(s.server.port) === Number(connection.port));
  const suggested = (own && `latest:${own.source_key}`) || options.find(([v]) => v.startsWith("profile:"))?.[0] || options.find(([v]) => v.startsWith("latest:"))?.[0] || "";
  // The saved connections arrive before the snapshots: Target keeps following
  // the suggestion (a snapshot of this server wins) until the user picks.
  useEffect(() => {
    if (!picked.current && source === "current" && suggested && target !== suggested) setTargetState(suggested);
  }, [suggested]);
  const problem = !known(source) ? goneText("Source", source) : !known(target) ? goneText("Target", target)
    : source === target ? "Choose two different sides." : "";
  async function side(choice: string, signal: AbortSignal): Promise<CompareSide> {
    if (choice === "current") return { live: { connection: { ...connection } } };
    if (choice.startsWith("profile:")) return { profile: choice.slice(8) };
    if (choice.startsWith("snapshot:")) return { snapshot: choice.slice(9) };
    if (choice.startsWith("latest:")) {
      // Resolved on this computer when Compare is pressed, scoped to the named
      // server whatever the other side is, newest first as Python orders them.
      const list = await listSnapshots(signal, choice.slice(7));
      const latest = [...list.items].sort((a, b) => b.saved_at.localeCompare(a.saved_at) || b.number - a.number)[0];
      if (!latest) throw new Error("No snapshot remains for this server. Choose another side.");
      return { snapshot: latest.id };
    }
    throw new Error("Choose a source and a target.");
  }
  async function runCompare() {
    if (!onScreen || disabled || problem || comparing.pending) return;
    const startedKey = key, choices = choiceKey, label = connectionLabel(connection), from = source, to = target;
    const sides: [string, string] = [optionLabel(from), optionLabel(to)];
    const read = await comparing.run(async signal => {
      const pair = { source: await side(from, signal), target: await side(to, signal) };
      const report: Comparison<Difference> = kind === "parameters" ? await compareParameters(pair.source, pair.target, signal) : await compareAccounts(pair.source, pair.target, signal);
      return { report, pair };
    }, { pending: "Reading and comparing…", done: r => r.report.identical ? "No differences." : "" });
    if (read.ok && current.current === startedKey) {
      setResult({ ...read.value, key: startedKey, label, choices, sides });
      setSetupOpen(false);
      setChosen(new Set()); setChoiceNote(""); setSelected(""); setSql(null); exporting.cancel(); copying.reset(); inspector?.setOpen(false);
    }
  }
  const other = result && result.key !== key ? result.label : null;
  const sidesChanged = !!result && result.choices !== choiceKey;
  const report = result?.report;
  const stale = !!other || sidesChanged;
  const items = report?.items || [];
  const filtered = items.filter(row => (showIdentical || row.status !== "identical") &&
    (kind !== "parameters" || showIdentity || !(row as ParameterDifference).identity));
  const hiddenIdentical = showIdentical ? 0 : items.filter(r => r.status === "identical").length;
  const hiddenIdentity = kind === "parameters" && !showIdentity ? items.filter(r => (r as ParameterDifference).identity && (showIdentical || r.status !== "identical")).length : 0;
  const needle = search.trim().toLowerCase();
  const rows = !needle ? filtered : filtered.filter(row => ("account" in row ? [row.account, row.status_text, accountDifferenceText(row)]
    : [row.name, row.status_text, row.group, displayValue(row.source?.value), displayValue(row.target?.value)]).some(v => v?.toLowerCase().includes(needle)));
  const selectedRow = items.find(row => rowKey(row) === selected);
  const detail: InspectorContent | null = selectedRow ? { key: rowKey(selectedRow), title: "account" in selectedRow ? selectedRow.account : selectedRow.name,
    meta: <><ComparePill status={selectedRow.status} text={selectedRow.status_text} />
      {"account" in selectedRow ? selectedRow.system && <Pill title="A built-in account the server creates">System account</Pill>
        : selectedRow.identity && <Pill title="Usually differs from server to server (server_id, host names, paths)">Server-specific</Pill>}</>,
    body: "account" in selectedRow ? <AccountCompareDetail row={selectedRow} /> : <ParameterCompareDetail row={selectedRow} /> } : null;
  function toggle(row: AccountDifference, include: boolean) {
    setChoiceNote("");
    setChosen(old => { const next = new Set(old); if (include) next.add(rowKey(row)); else next.delete(rowKey(row)); return next; });
  }
  const valueCell = (r: Difference, which: "source" | "target") => {
    const v = (r as ParameterDifference)[which];
    return v ? rawValue(v.value) : <span className="muted">{r.status === "not_compared" ? "Not read" : "Not present"}</span>;
  };
  const columns: Column<Difference>[] = kind === "parameters" ? [
    { key: "name", label: "Name", primary: true, sort: r => r.name, render: r => <span className="server-compare-name"><code>{r.name}</code>
      {(r as ParameterDifference).identity && <Pill title="Usually differs from server to server (server_id, host names, paths)">Server-specific</Pill>}</span> },
    { key: "status", label: "Status", minWidth: 128, sort: r => COMPARE_ORDER.indexOf(r.status), render: r => <ComparePill status={r.status} text={r.status_text} /> },
    { key: "source", label: "Source value", sort: r => displayValue((r as ParameterDifference).source?.value), render: r => valueCell(r, "source") },
    { key: "target", label: "Target value", sort: r => displayValue((r as ParameterDifference).target?.value), render: r => valueCell(r, "target") },
    { key: "group", label: "Group", flex: true, sort: r => (r as ParameterDifference).group || "", render: r => (r as ParameterDifference).group || "" },
  ] : [
    { key: "selected", name: "Choose for export", label: "Export", width: 72, render: r => {
      const row = r as AccountDifference, present = !!row[exportSide];
      // The checkbox chooses; it never opens the detail (click or Space).
      return <span onClick={e => e.stopPropagation()} onKeyDown={e => e.stopPropagation()}><Checkbox checked={chosen.has(rowKey(r))}
        disabled={stale || comparing.pending || !present} title={present ? `Export ${row.account} from the ${exportSide}` : `Not in the ${exportSide}`}
        onChange={v => toggle(row, v)}><span className="visually-hidden">Export {row.account}</span></Checkbox></span>;
    } },
    { key: "account", label: "Account", primary: true, sort: r => (r as AccountDifference).account, render: r => <span className="server-compare-name"><code>{(r as AccountDifference).account}</code>
      {(r as AccountDifference).system && <Pill title="A built-in account the server creates">System</Pill>}</span> },
    { key: "status", label: "Status", minWidth: 128, sort: r => COMPARE_ORDER.indexOf(r.status), render: r => <ComparePill status={r.status} text={r.status_text} /> },
    { key: "differs", label: "What differs", flex: true, render: r => accountDifferenceText(r as AccountDifference) },
  ];
  async function prepareExport() {
    if (!result || stale || !onScreen || !chosen.size) return;
    const bound = result, from = exportSide, chosenAccounts = (result.report.items as AccountDifference[]).filter(row => chosen.has(rowKey(row))).map(exportAccountName);
    const read = await exporting.run(signal => exportGrants(bound.pair[from], chosenAccounts, signal), {
      pending: `Reading grants on the ${from} for the SQL export…`, done: `SQL export ready: ${plural(chosenAccounts.length, "account")} from the ${from}.`,
    });
    if (read.ok && current.current === bound.key) { setSql(read.value); copying.reset(); }
  }
  async function removeSnapshot() {
    if (!deleting) return;
    const item = deleting;
    const removed = await deletion.run(signal => deleteSnapshot(item.id, signal), { pending: "Deleting snapshot…", done: `Deleted snapshot #${item.number} of ${address(item.server)}.` });
    if (removed.ok) {
      onSnapshots(old => old.filter(s => s.id !== item.id)); setDeleting(null);
      if (source === `snapshot:${item.id}`) setSource("current");
      if (target === `snapshot:${item.id}`) setTarget("current");
    }
  }
  const sideNotes = report ? report.notes.filter(note => ![report.source.error, report.target.error].some(error => error && note.endsWith(error))) : [];
  const hidden = [hiddenIdentical && `${hiddenIdentical.toLocaleString()} identical`, hiddenIdentity && `${hiddenIdentity.toLocaleString()} server-specific`].filter(Boolean).join(", ");
  return <ViewLayout id={`${kind}-compare`} title={LABELS[kind]} className="server-compare-view" controls={controls} onBack={onBack}
    inspector={detail} inspectorEmpty={<p>Select a difference to see both sides.</p>} detailBackLabel="Back to differences" otherConnection={other}
    otherAction={{ label: "Compare", onClick: () => void runCompare(), disabled: disabled || !!problem || comparing.pending }}
    info={<p>{kind === "parameters"
      ? "Compare the global parameters of two servers, or of a server and a parameter snapshot saved on this computer. The source is the side to match; the target is the other side."
      : "Compare the users and roles of two servers: their grants, roles, default roles and account settings. Passwords are never read or shown."} Compare only reads; nothing is sent to a server to change it.</p>}>
    <div className="server-compare-content">
      <ComparisonSetup className="server-compare-bar" runClassName="server-compare-run" hasResult={!!report}
        expanded={setupOpen} onExpanded={setSetupOpen}
        source={result && report ? result.sides[0] : optionLabel(source)}
        target={result && report ? result.sides[1] : optionLabel(target)}
        context={report && <div className="server-compare-sides" aria-label="Compared servers">
          <p><span>Source</span>{sideLabel(report.source)}{sideTime(report.source)}</p>
          <p><span>Target</span>{sideLabel(report.target)}{sideTime(report.target)}</p>
        </div>}
        contextText={report ? `Last compared: Source ${sideLabel(report.source)}${report.source.captured_at ? ` · ${report.source.kind === "live" ? "read" : "captured"} ${localTime(report.source.captured_at)}` : ""}; Target ${sideLabel(report.target)}${report.target.captured_at ? ` · ${report.target.kind === "live" ? "read" : "captured"} ${localTime(report.target.captured_at)}` : ""}` : undefined}
        onSwap={() => { setSource(target); setTarget(source); }} busy={comparing.pending} problem={problem} disabled={disabled} onCompare={() => void runCompare()}>
        <CompareSidesBar hasResult={!!report} options={options} missingLabel={missingLabel} source={source} target={target} onSource={setSource} onTarget={setTarget} onSwap={() => { setSource(target); setTarget(source); }}
          sourceError={!stale ? report?.source.error : undefined} targetError={!stale ? report?.target.error : undefined} busy={comparing.pending}
          problem={problem} disabled={disabled} onCompare={() => void runCompare()} />
      </ComparisonSetup>
      {report && !setupOpen && !stale && [report.source.error, report.target.error].filter(Boolean).map((error, i) =>
        <p className="server-compare-error" key={i} role="alert">{i === 0 && report.source.error ? "Source" : "Target"}: {error}</p>)}
      <ActionStatus state={comparing} onStop={() => { comparing.cancel(); comparing.show("done", result ? "Comparison stopped. The differences below are from the last comparison." : "Comparison stopped. Nothing was compared."); }} slowAfter={0}
        onRetry={() => void runCompare()} onDismiss={comparing.reset} />
      <div className="server-compare-tools" hidden={!!report && !setupOpen}>
        {kind === "parameters" && <button type="button" className="text-button" aria-expanded={manage} aria-controls={`${kind}-manage-snapshots`}
          onClick={() => setManage(v => !v)}>Manage snapshots ({snapshots.length.toLocaleString()})</button>}
        {/* Never disabled while it reads (focus would fall to the page); a second click joins the read. */}
        <button type="button" className="text-button" aria-busy={catalog.pending || undefined} onClick={() => void loadCatalog(true)}>Refresh choices</button>
      </div>
      <ActionStatus state={catalog} onRetry={() => void loadCatalog(true)} onDismiss={catalog.reset} />
      {!deleting && <ActionStatus state={deletion} onDismiss={deletion.reset} />}
      {kind === "parameters" && manage && (!report || setupOpen) && <section className="server-compare-manage" id={`${kind}-manage-snapshots`} aria-label="Manage parameter snapshots">
        {snapshots.length ? <ul>{snapshots.map(s => <li key={s.id}>
          <span>{snapshotLabel(s)}</span>
          <button type="button" className="button small" aria-label={`Delete snapshot #${s.number} from ${address(s.server)}`} onClick={() => { deletion.reset(); setDeleting(s); }}>Delete…</button>
        </li>)}</ul> : <p className="server-compare-note">No parameter snapshots saved. Save snapshot… keeps the parameters of the connection in use.</p>}
      </section>}
    </div>
    {!report ? <EmptyState icon="compare" title={problem ? "Choose a source and a target" : "Ready to compare"}>
      {problem ? `${problem} ` : ""}{kind === "parameters" ? "Each side can be this connection, a saved connection or a parameter snapshot." : "Each side can be this connection or a saved connection."}
    </EmptyState> : <div className="server-compare-result">
      {sidesChanged && !other && <p className="server-compare-note" role="status">The chosen sides changed (next: Source {optionLabel(source)} → Target {optionLabel(target)}). Press Compare to update the differences.</p>}
      <SummaryLine
        notes={sideNotes.length ? sideNotes : undefined} notesTitle="Notes about this comparison">{comparisonSummary(report.counts)}</SummaryLine>
      <div className="server-compare-filters">
        <h3>Differences</h3>
        <SearchField label="Filter differences" value={search} onChange={setSearch} placeholder="Filter differences" />
        <Checkbox checked={showIdentical} onChange={setShowIdentical}>Show identical</Checkbox>
        {kind === "parameters" && <Checkbox checked={showIdentity} onChange={setShowIdentity}
          title="server_id, host names, paths and other settings that usually differ between servers">Show server-specific settings</Checkbox>}
        {hidden && <span className="server-compare-hidden">Hidden: {hidden}</span>}
      </div>
      <DataTable label="Differences" columns={columns} rows={rows} rowKey={rowKey} defaultSort={{ key: "status", dir: "asc" }} selection={{ key: selected, select: (v, open) => { setSelected(v); if (open !== false) inspector?.setOpen(true); } }}
        empty={<p className="server-compare-empty">{report.identical ? "The source and the target are the same." : items.length ? "No differences match these filters." : "Nothing to compare."}</p>} />
      {kind === "accounts" && <section className="server-compare-export" aria-labelledby={`${kind}-export-title`}>
        <h3 id={`${kind}-export-title`}>Export grants as SQL</h3>
        <div className="server-compare-export-tools">
          <ToolbarSelect label="From" value={exportSide} options={[["source", "Source"], ["target", "Target"]]}
            onChange={v => { setExportSide(v === "target" ? "target" : "source"); setChosen(new Set()); setChoiceNote(""); }} disabled={stale} />
          <button type="button" className="button" disabled={stale || comparing.pending} title="Choose every account only the source has. System accounts are left out."
            onClick={() => {
              const accounts = report.items as AccountDifference[], picked = onlyInSourceForExport(accounts);
              const left = accounts.filter(r => r.status === "only_in_source" && r.system).length;
              setExportSide("source"); setChosen(new Set(picked.map(rowKey)));
              setChoiceNote(picked.length ? `Chose ${plural(picked.length, "account")} only in the source${left ? `; ${plural(left, "system account")} left out` : ""}.`
                : `No account is only in the source${left ? ` apart from ${plural(left, "system account")}, which are left out` : ""}.`);
            }}>All accounts only in source</button>
          <ActionButton busy={exporting.pending} busyLabel="Exporting…" disabled={stale || !chosen.size || comparing.pending}
            title={stale ? "Compare again to export from these sides." : !chosen.size ? "Choose accounts in the Export column." : undefined}
            onClick={() => void prepareExport()}>Export grants as SQL ({chosen.size.toLocaleString()})</ActionButton>
        </div>
        {choiceNote && <p className="server-compare-choice" role="status">{choiceNote}</p>}
        <p className="server-compare-note">Passwords are not included. Export reads the chosen side again, and myflames never runs the SQL.</p>
        <ActionStatus state={exporting} onStop={exporting.cancel} onRetry={() => void prepareExport()} />
        {sql && <div className="server-compare-sql-block">
          <pre className="server-compare-sql" tabIndex={0} aria-label="SQL export">{sql.text}</pre>
          <div className="server-compare-export-tools">
            <ActionButton icon="copy" busy={copying.pending} busyLabel="Copying…" onClick={() => void copying.run(async () => { if (!await copyToClipboard(sql.text)) throw new Error("Could not copy. Select the SQL text and copy it."); }, { pending: "Copying SQL…", done: "Copied the SQL." })}>Copy</ActionButton>
            <button type="button" className="button" onClick={() => { downloadSql(sql); copying.show("done", `Downloaded ${sql.filename}.`); }}>Download .sql</button>
            <ActionStatus state={copying} onDismiss={copying.reset} />
          </div>
        </div>}
      </section>}
    </div>}
    {deleting && <ConfirmSheet eyebrow="Delete snapshot" title={`Delete snapshot #${deleting.number}?`}
      description={`${snapshotLabel(deleting)}. It is removed from this computer; the server is not changed.`}
      confirmLabel="Delete snapshot" busy={deletion.pending} busyLabel="Deleting…"
      returnFocus={() => document.querySelector<HTMLElement>(`[aria-controls="${kind}-manage-snapshots"]`)}
      onConfirm={() => void removeSnapshot()} onCancel={() => setDeleting(null)}>
      {deletion.phase === "failed" && <p className="form-error" role="alert">{deletion.message}</p>}
    </ConfirmSheet>}
  </ViewLayout>;
}

/** Setting | Source | Target, one row per fact (Schema versions' "What
 * differs" table). A row that differs says so in words, not color alone. */
function SideTable({ heading, label, rows }: { heading: string; label: string; rows: { label: string; source: ReactNode; target: ReactNode; differs?: boolean }[] }) {
  return <table className="server-compare-table" aria-label={label}>
    <thead><tr><th scope="col">{heading}</th><th scope="col">Source</th><th scope="col">Target</th></tr></thead>
    <tbody>{rows.map(r => <tr key={r.label} className={r.differs ? "is-different" : undefined}>
      <th scope="row">{r.label}{r.differs && <ComparisonBadge status="different" className="server-compare-kind" />}</th>
      <td>{r.source}</td><td>{r.target}</td>
    </tr>)}</tbody>
  </table>;
}

const PARAMETER_LEAD: Record<CompareStatus, string> = {
  different: "The source and the target have different values.", only_in_source: "Only the source has this parameter.",
  only_in_target: "Only the target has this parameter.", identical: "The source and the target have the same value.", not_compared: "Not compared.",
};
function ParameterCompareDetail({ row }: { row: ParameterDifference }) {
  const missing = <span className="muted">{row.status === "not_compared" ? "Not read" : "Not present"}</span>;
  const cell = (side: "source" | "target", text: (v: NonNullable<ParameterDifference["source"]>) => ReactNode) => row[side] ? text(row[side]!) : missing;
  const at = (side: "source" | "target") => row[side] ? yesNo(row[`${side}_at_default`]) : missing;
  return <div className="server-compare-detail">
    <p className="server-compare-lead">{PARAMETER_LEAD[row.status]}{row.identity ? " This setting usually differs from server to server." : ""}</p>
    {row.reason && <p className="server-compare-reason">{row.reason}</p>}
    <SideTable heading="Setting" label={`${row.name} in the source and the target`} rows={[
      { label: "Raw value", source: cell("source", v => <code>{rawValue(v.value)}</code>), target: cell("target", v => <code>{rawValue(v.value)}</code>), differs: row.status === "different" },
      { label: "Default", source: cell("source", v => <code>{rawValue(v.default)}</code>), target: cell("target", v => <code>{rawValue(v.default)}</code>) },
      { label: "At default", source: at("source"), target: at("target") },
      { label: "Value source", source: cell("source", v => v.source_label || v.source || "Unknown"), target: cell("target", v => v.source_label || v.source || "Unknown") },
      ...(row.source?.type || row.target?.type ? [{ label: "Type", source: cell("source", v => v.type || "Unknown"), target: cell("target", v => v.type || "Unknown") }] : []),
      { label: "Group", source: row.group || "None", target: row.group || "None" },
    ]} />
  </div>;
}

const roleText = (role: CompareRole) => `${role.name}${role.host ? `@${role.host}` : ""}${role.admin_option ? " (admin option)" : ""}`;
const ACCOUNT_LEAD: Record<CompareStatus, string> = {
  different: "In both, with differences.", only_in_source: "Only the source has this account.", only_in_target: "Only the target has this account.",
  identical: "The same in the source and the target.", not_compared: "Not compared.",
};
function AccountCompareDetail({ row }: { row: AccountDifference }) {
  const changes = row.differences;
  // Differences contain original source lines, already matched by Python.
  // Subtract those lines to show the source spelling of the shared grants.
  const sourceOnly = changes?.grants.only_in_source || (row.status === "only_in_source" ? row.source?.grants || [] : []);
  const targetOnly = changes?.grants.only_in_target || (row.status === "only_in_target" ? row.target?.grants || [] : []);
  const commonKnown = !!(changes && row.source && row.target && !row.source.error && !row.target.error);
  const both = commonKnown ? row.source!.grants.filter(g => !sourceOnly.includes(g)) : [];
  const unread = !changes && row.status === "not_compared";
  const differs = new Set(changes?.attributes.map(a => a.name) || []);
  const listDiffers = (d?: { only_in_source: unknown[]; only_in_target: unknown[] }) => !!d && (d.only_in_source.length + d.only_in_target.length > 0);
  const missing = <span className="muted">{row.status === "not_compared" ? "Not read" : "Not present"}</span>;
  const fact = (text: (v: AccountValue) => ReactNode) => (side: "source" | "target") => row[side] ? text(row[side]!) : missing;
  const attribute = (text: (a: AccountValue["attributes"]) => string) => fact(v => v.attributes_checked === false ? "Unknown" : <span className="server-compare-raw">{text(v.attributes)}</span>);
  const facts: [string, (side: "source" | "target") => ReactNode, boolean][] = [
    ["Type", fact(v => v.role ? "Role" : "User"), differs.has("role")],
    ["Roles", fact(v => v.roles.map(roleText).join(", ") || "None"), listDiffers(changes?.roles)],
    ["Default roles", fact(v => v.defaults_checked === false ? "Unknown" : v.default_roles.map(roleText).join(", ") || "None"), listDiffers(changes?.default_roles)],
    ["Plugin", fact(v => v.attributes_checked === false ? "Unknown" : v.attributes.plugins?.join(", ") || "Unknown"), differs.has("plugins")],
    ["Locked", fact(v => yesNo(v.attributes.locked)), differs.has("locked")],
    ["Password expired", fact(v => yesNo(v.attributes.expired)), differs.has("expired")],
    ["TLS", fact(v => v.tls_checked === false ? "Unknown" : <span className="server-compare-raw">{tlsText(v.attributes.tls)}</span>), differs.has("tls")],
    ["Limits", attribute(a => recordText(a.limits)), differs.has("limits")],
    ["Password policy", attribute(a => recordText(a.password_options)), differs.has("password_options")],
  ];
  return <div className="server-compare-detail">
    <p className="server-compare-lead">{ACCOUNT_LEAD[row.status]}{row.status === "different" ? ` ${accountDifferenceText(row)}.` : ""}</p>
    {row.reason && <p className="server-compare-reason">{row.reason}</p>}
    {(["source", "target"] as const).map(side => row[side]?.error && <p key={side} className="server-compare-reason">{side === "source" ? "Source" : "Target"}: {row[side]!.error}</p>)}
    <section className="server-compare-section" aria-label="Grants">
      <h3>Grants</h3>
      {unread ? <>
        <GrantLines title="Read on the source" lines={row.source?.grants || []} unknown={!row.source || !!row.source.error} open />
        <GrantLines title="Read on the target" lines={row.target?.grants || []} unknown={!row.target || !!row.target.error} open />
      </> : <>
        <GrantLines title="Only in source" lines={sourceOnly} open={sourceOnly.length > 0} />
        <GrantLines title="Only in target" lines={targetOnly} open={targetOnly.length > 0} />
        <GrantLines title="In both" lines={both} unknown={!commonKnown && row.status === "not_compared"} />
      </>}
    </section>
    <section className="server-compare-section" aria-label="Account settings">
      <h3>Account settings</h3>
      <SideTable heading="Setting" label={`${row.account} in the source and the target`}
        rows={facts.map(([label, value, different]) => ({ label, source: value("source"), target: value("target"), differs: different }))} />
    </section>
  </div>;
}
function GrantLines({ title, lines, unknown = false, open = false }: { title: string; lines: string[]; unknown?: boolean; open?: boolean }) {
  return <Disclosure className="server-compare-grants" title={title} count={unknown ? undefined : lines.length} open={open}>
    {unknown ? <p className="server-compare-reason">Unknown: this side could not be read.</p>
      : lines.length ? <pre className="server-compare-code">{lines.join("\n")}</pre> : <p className="server-compare-reason">None</p>}
  </Disclosure>;
}
function downloadSql(sql: GrantsExport) {
  const url = URL.createObjectURL(new Blob([sql.text], { type: "application/sql;charset=utf-8" }));
  const a = document.createElement("a"); a.href = url; a.download = sql.filename; document.body.appendChild(a); a.click(); a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
