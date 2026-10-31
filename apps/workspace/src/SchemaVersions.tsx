import { AlterPredictionWhy, notPredictedReason } from "./AlterPrediction";
// The Schema versions place (SQL workspace group, after Database diagram):
// versions of a database's schema saved on this computer, and comparisons of
// any two schemas: a saved version, or a database read now through the
// connection in use or a saved connection whose password is saved. Reading a
// database only reads (information_schema and SHOW CREATE). A comparison has
// a source (the schema to match) and a target (the schema that changes): it
// carries the sync script that would change the target to match the source,
// which the user copies, downloads or opens in the Query editor (that editor
// runs it, one statement at a time with its own confirmations). Python decides
// every difference and writes every sentence (schemaVersionsApi.ts); this file
// lays them out.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Connection, Profile } from "./api";
import { deletedConnectionLabel } from "./savedConnections";
import { connectionKey, connectionLabel } from "./binding";
import { Icon } from "./components";
import { ComparisonBadge, ComparisonSetup } from "./compareShared";
import {
  ActionButton, ActionStatus, Checkbox, ConfirmSheet, DataTable, Disclosure, EmptyState, ExportMenu, Fields, FormGrid, FormRow,
  InspectorContext, InspectorSection, Pill, Segmented, Select, SummaryLine, ToolbarButton, ToolbarSelect, ViewLayout, ViewStatus,
  copyToClipboard, storedInspectorWidth, useAction, useInspector, useWindowStatus,
  type Column, type InspectorContent, type InspectorShared, type Selection,
} from "./kit";
import { useSchemaCatalog } from "./PerfShared";
import { useViewOnScreen } from "./viewOnScreen";
import {
  compareSchemas, countsText, deleteVersion, exportVersion, getVersion, importVersion, lintVersion, listVersions, localTime, objectCount,
  productText, RISK_LABELS, saveVersion, selectSync, serverAddress, serverMatch, STATUS_LABELS, SYNC_ACTIONS, TYPE_LABELS, updateVersion, versionPhrase,
  versionTitle, type Change, type CompareEntry, type CompareReport, type CompareStatus, type DiffLine, type SideRequest,
  type LintReport, type Risks, type VersionItem, type VersionWithSnapshot,
} from "./schemaVersionsApi";
import { SchemaExportSheet, type ExportSource } from "./SchemaExport";
import "./schema-versions.css";

export type SchemaVersionsTab = "saved" | "compare";
const SHEET_WIDTH = 1100;
const SYSTEM = new Set(["mysql", "sys", "performance_schema", "information_schema"]);

/** One side as chosen on screen: a saved version, or a database read now
 * through the connection in use ("current") or a saved connection (its id). */
type SideChoice = { kind: "version"; id: string } | { kind: "live"; via: string; database: string };
const NO_SIDE: SideChoice = { kind: "version", id: "" };

function download(filename: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A saved connection as a request: its settings and id, never a password
 * (the server fills the saved one for that read only). */
function profileConnection(p: Profile): Connection {
  return { name: p.name, host: p.host, port: p.port, user: p.user, database: p.database || "", ssl_mode: p.ssl_mode,
    ssl_ca: p.ssl_ca, ...(p.ssh_host ? { ssh_host: p.ssh_host, ssh_user: p.ssh_user, ssh_port: p.ssh_port } : {}),
    profile_id: p.id, password: "" };
}

const STATUS_ORDER: CompareStatus[] = ["different", "only_in_source", "only_in_target", "not_compared", "identical"];
const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

export function SchemaVersionsPlace({ connection, profiles, onOpenSql }: {
  /** The connection in use (with its typed password when not saved). */
  connection: Connection;
  /** Saved connections: a comparison can read through one whose password is saved. */
  profiles: Profile[];
  /** Open SQL in a new Query editor tab (nothing runs). */
  onOpenSql: (sql: string) => void;
}) {
  const [tab, setTab] = useState<SchemaVersionsTab>("saved");
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
  const place = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(() => (typeof window !== "undefined" ? window.innerWidth : 1440));
  useEffect(() => {
    const el = place.current;
    const measure = () => {
      const w = el && el.clientWidth > 0 ? el.clientWidth : window.innerWidth;
      if (w > 0) setWidth(w);
    };
    measure();
    const observer = typeof ResizeObserver !== "undefined" && el ? new ResizeObserver(measure) : null;
    if (el && observer) observer.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);
  const sheet = width > 0 && width < SHEET_WIDTH;
  const inspector = useMemo<InspectorShared>(() => ({
    open: inspectorOpen && !!selectedKey, setOpen: setInspectorOpen,
    width: inspectorWidth, setWidth: setInspectorWidth, sheet,
  }), [inspectorOpen, selectedKey, inspectorWidth, sheet]);
  const selection: Selection = {
    key: selectedKey,
    select: (key: string, open?: boolean) => {
      setSelectedKey(key);
      if (open !== false) setInspectorOpen(true);
    },
  };
  useWindowStatus(`schema:${connectionKey(connection)}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
  });
  return (
    <InspectorContext.Provider value={inspector}>
      <div className="schema-versions-place" ref={place}>
        <SchemaVersionsView connection={connection} profiles={profiles} onOpenSql={onOpenSql} tab={tab}
          onTab={(next) => { setTab(next); setSelectedKey(""); setInspectorOpen(false); }}
          selection={selection} />
      </div>
    </InspectorContext.Provider>
  );
}

function SchemaVersionsView({ connection, profiles, onOpenSql, tab, onTab, selection }: {
  connection: Connection;
  profiles: Profile[];
  onOpenSql: (sql: string) => void;
  tab: SchemaVersionsTab;
  onTab: (tab: SchemaVersionsTab) => void;
  selection: Selection;
}) {
  const onScreen = useViewOnScreen();
  const sharedInspector = useInspector();
  // ---- saved versions ------------------------------------------------------
  const [versionsState, setVersionsState] = useState<VersionItem[] | null>(null);
  const versions = versionsState;
  // Every local change (save, import, rename, delete) counts: a list read
  // that started before one never replaces what it did.
  const changes = useRef(0);
  const setVersions = (update: (list: VersionItem[] | null) => VersionItem[]) => {
    changes.current += 1;
    setVersionsState(update);
  };
  const listing = useAction();
  const loadList = () => {
    const started = changes.current;
    return listing.run((signal) => listVersions(signal).then((list) => {
      if (changes.current === started) setVersionsState(list.items);
      return list.items;
    }), { pending: "Reading saved versions…", done: "" });
  };
  useEffect(() => {
    if (onScreen && versions === null && !listing.pending) void loadList();
  }, [onScreen]);
  // The databases of the connection in use: the Database choice of the list,
  // the Save sheet and Compare (nothing is typed while they can be read).
  const catalog = useSchemaCatalog(connection, onScreen && !!connection.host);
  const databases = (catalog.catalog?.schemas || []).filter((s) => !s.system && !SYSTEM.has(s.name)).map((s) => s.name);
  const [database, setDatabase] = useState("");
  const shown = useMemo(() => (versions || []).filter((v) => !database || v.database === database), [versions, database]);
  const databaseOptions = useMemo<[string, string][]>(() => {
    const counts = new Map<string, number>();
    for (const v of versions || []) counts.set(v.database, (counts.get(v.database) || 0) + 1);
    const names = [...new Set([...databases, ...counts.keys(), ...(database ? [database] : [])])]
      .sort((a, b) => a.localeCompare(b));
    return [["", "All databases"], ...names.map((name): [string, string] => {
      const n = counts.get(name) || 0;
      const where = databases.length && !databases.includes(name) ? ", not on this server" : "";
      return [name, `${name} · ${n ? plural(n, "version") : "none saved"}${where}`];
    })];
  }, [versions, databases.join("\0"), database]);
  /** The previous version of the same database (source) for "same as". */
  const previous = useMemo(() => {
    const out = new Map<string, VersionItem>();
    const bySource = new Map<string, VersionItem[]>();
    for (const v of versions || []) bySource.set(v.source_key, [...(bySource.get(v.source_key) || []), v]);
    for (const list of bySource.values()) {
      const ordered = [...list].sort((a, b) => a.number - b.number);
      ordered.forEach((v, i) => { if (i > 0) out.set(v.id, ordered[i - 1]); });
    }
    return out;
  }, [versions]);
  const selectedVersion = tab === "saved" ? (versions || []).find((v) => v.id === selection.key) || null : null;

  // The selected version's snapshot (its objects and definitions).
  const [detail, setDetail] = useState<VersionWithSnapshot | null>(null);
  const detailRead = useAction(selectedVersion?.id || "");
  useEffect(() => {
    if (!selectedVersion) { setDetail(null); return; }
    if (!onScreen || !sharedInspector?.open || detail?.id === selectedVersion.id) return;
    void detailRead.run((signal) => getVersion(selectedVersion.id, signal),
      { pending: "Reading the saved definitions…", done: "" }).then((result) => {
      if (result.ok) setDetail(result.value);
    });
    return detailRead.cancel;
  }, [selectedVersion?.id, onScreen, sharedInspector?.open]);

  // Save: read the chosen database and keep it as the next version.
  const [saveSheet, setSaveSheet] = useState<{ database: string; name: string; note: string } | null>(null);
  const saving = useAction();
  const [savedNote, setSavedNote] = useState("");
  function openSave() {
    saving.reset();
    const preferred = database || (connection.database && !SYSTEM.has(connection.database) ? connection.database : "");
    setSaveSheet({ database: preferred || databases[0] || "", name: "", note: "" });
  }
  async function confirmSave() {
    if (!saveSheet || !saveSheet.database.trim()) return;
    const target = { ...saveSheet, database: saveSheet.database.trim() };
    const result = await saving.run((signal) => saveVersion(connection, target.database, target.name.trim(), target.note, signal), {
      pending: `Reading ${target.database}…`,
      done: (item) => savedText(item, "Saved"),
    });
    if (result.ok) {
      setSaveSheet(null);
      setSavedNote(savedText(result.value, "Saved"));
      setVersions((list) => [result.value, ...(list || []).filter((v) => v.id !== result.value.id)]);
      if (database && database !== result.value.database) setDatabase(result.value.database);
      selection.select(result.value.id, true);
    }
  }

  // Import: a schema-snapshot-1.0 file becomes the next version of its database.
  const importing = useAction();
  const fileInput = useRef<HTMLInputElement>(null);
  async function importFile(file: File | undefined) {
    if (!file) return;
    const result = await importing.run(async (signal) => importVersion(await file.text(), signal), {
      pending: `Importing ${file.name}…`,
      done: (item) => savedText(item, "Imported as"),
    });
    if (result.ok) {
      setVersions((list) => [result.value, ...(list || []).filter((v) => v.id !== result.value.id)]);
      if (database && database !== result.value.database) setDatabase(result.value.database);
      selection.select(result.value.id, true);
    }
    if (fileInput.current) fileInput.current.value = "";
  }

  // Rename, delete, export.
  const [renameSheet, setRenameSheet] = useState<{ item: VersionItem; name: string; note: string } | null>(null);
  const renaming = useAction();
  const [deleteSheet, setDeleteSheet] = useState<VersionItem | null>(null);
  const deleting = useAction();
  const exporting = useAction();
  // Export as SQL: a saved version (its inspector) or a database read now
  // through the connection in use (the toolbar).
  const [exportSheet, setExportSheet] = useState<ExportSource | null>(null);
  function exportLive() {
    const preferred = database && databases.includes(database) ? database
      : connection.database && !SYSTEM.has(connection.database) ? connection.database : databases[0] || "";
    setExportSheet({ kind: "live", connection, database: preferred, databases });
  }
  async function confirmRename() {
    if (!renameSheet) return;
    const result = await renaming.run(() => updateVersion(renameSheet.item.id, renameSheet.name.trim(), renameSheet.note),
      { pending: "Saving…", done: "" });
    if (result.ok) {
      setVersions((list) => (list || []).map((v) => (v.id === result.value.id ? result.value : v)));
      setRenameSheet(null);
    }
  }
  async function confirmDelete() {
    if (!deleteSheet) return;
    const gone = deleteSheet;
    const result = await deleting.run(() => deleteVersion(gone.id), { pending: "Deleting…", done: "" });
    if (result.ok) {
      setVersions((list) => (list || []).filter((v) => v.id !== gone.id));
      setDeleteSheet(null);
      selection.select("", false);
      setSavedNote(`Deleted ${versionPhrase(gone)}.`);
    }
  }
  function exportItem(item: VersionItem) {
    void exporting.run((signal) => exportVersion(item.id, signal).then((file) => { download(file.filename, file.text, "application/json"); return file; }),
      { pending: "Exporting…", done: (file) => `Saved ${file.filename}.` });
  }

  // ---- compare ---------------------------------------------------------------
  // Source: the schema to match. Target: the schema the sync script changes,
  // by default the database of the connection in use (the only target the
  // Query editor can run the script on).
  const [source, setSource] = useState<SideChoice>(NO_SIDE);
  const [target, setTarget] = useState<SideChoice>({ kind: "live", via: "current", database: "" });
  const [ignoreDefiners, setIgnoreDefiners] = useState(false);
  const [showIdentical, setShowIdentical] = useState(false);
  const [resultTab, setResultTab] = useState<"differences" | "sync">("differences");
  const [report, setReport] = useState<CompareReport | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const [comparedSides, setComparedSides] = useState("");
  // The names of the sides the result on screen came from: the bar keeps them
  // after Swap or Change… until Compare runs again.
  const [comparedLabels, setComparedLabels] = useState<[string, string]>(["", ""]);
  const [chosenKeys, setChosenKeys] = useState<string[]>([]);
  const chosenKeysRef = useRef(chosenKeys);
  chosenKeysRef.current = chosenKeys;
  const comparing = useAction(connectionKey(connection));
  const syncing = useAction();
  const syncRevision = useRef(0);
  const currentReport = useRef(report);
  currentReport.current = report;
  const chosen = new Set(chosenKeys);
  // A failed, cancelled or superseded generation must not expose the last
  // script as though it represents the checkboxes currently on screen.
  const scriptCurrent = !!report && !comparing.pending && !syncing.pending
    && chosenKeys.length === report.sync.chosen_keys.length
    && report.sync.chosen_keys.every((key) => chosen.has(key));
  async function regenerateSync(keys: string[]) {
    if (!report || !onScreen || tab !== "compare" || comparing.pending) return;
    syncing.cancel();
    const revision = ++syncRevision.current;
    const id = report.comparison_id;
    chosenKeysRef.current = keys;
    setChosenKeys(keys);
    const result = await syncing.run((signal) => selectSync(id, keys, signal), {
      pending: "Updating the sync script…", done: "",
    });
    if (result.ok && revision === syncRevision.current && currentReport.current?.comparison_id === id
      && result.value.comparison_id === id) {
      setReport(result.value);
      setChosenKeys(result.value.sync.chosen_keys);
    }
  }
  function toggleEntry(key: string, include: boolean) {
    const next = new Set(chosenKeysRef.current);
    if (include) next.add(key); else next.delete(key);
    void regenerateSync((report?.objects || []).filter((entry) => next.has(entry.key)).map((entry) => entry.key));
  }
  const saved = profiles.filter((p) => p.has_saved_password);
  function sideConnection(via: string): Connection | null {
    if (via === "current") return connection.host ? connection : null;
    const profile = saved.find((p) => p.id === via);
    return profile ? profileConnection(profile) : null;
  }
  function sideChoiceLabel(side: SideChoice): string {
    if (side.kind === "version") {
      const version = versions?.find(v => v.id === side.id);
      return version ? `${version.database} · ${versionTitle(version)}` : "Choose a saved version";
    }
    const via = sideConnection(side.via);
    return `${side.database || "Choose a database"} · ${via ? connectionLabel(via) : "Connection unavailable"}`;
  }
  function sideRequest(side: SideChoice): SideRequest | string {
    if (side.kind === "version") return side.id ? { version: side.id } : "Choose a saved version.";
    const via = sideConnection(side.via);
    if (!via) return side.via === "current" ? "Choose a connection in the picker first." : "That saved connection is gone.";
    return side.database.trim() ? { live: { connection: via, database: side.database.trim() } } : "Choose a database.";
  }
  // A saved-version source starts at the newest version of the database the
  // target reads, from the same server when there is one (host, port and SSH
  // target: the same address through another SSH host is another server),
  // else the newest version, so nothing has to be picked when there is an
  // obvious choice. It waits for a live target's database (its picker fills
  // it in), and an automatic choice follows the target's database and server
  // until the user picks a version.
  const otherDatabase = target.kind === "live" ? target.database : "";
  const targetVia = target.kind === "live" ? sideConnection(target.via) : null;
  const targetServer = targetVia ? connectionKey(targetVia) : "";
  const autoSource = useRef("");
  /** A source the user chose (a picker, Compare with…, swap): never replaced. */
  function chooseSource(side: SideChoice) {
    autoSource.current = "";
    setSource(side);
  }
  useEffect(() => {
    if (tab !== "compare" || source.kind !== "version" || !versions?.length) return;
    if (source.id && source.id !== autoSource.current) return;
    if (target.kind === "live" && !target.database && connection.host) return;
    const newest = [...versions].sort((a, b) => b.saved_at.localeCompare(a.saved_at));
    const sameDatabase = newest.filter((v) => v.database === otherDatabase);
    const match = (targetVia && sameDatabase.find((v) => serverMatch(v.server, targetVia).same)) || sameDatabase[0] || newest[0];
    autoSource.current = match.id;
    if (match.id !== source.id) setSource({ kind: "version", id: match.id });
  }, [tab, versions, otherDatabase, target.kind, targetServer, connection.host, source.kind, source.kind === "version" ? source.id : ""]);
  const sourceRequest = sideRequest(source), targetRequest = sideRequest(target);
  const sidesProblem = typeof sourceRequest === "string" ? `Source: ${sourceRequest}` : typeof targetRequest === "string" ? `Target: ${targetRequest}` : "";
  async function runCompare(s: SideChoice = source, t: SideChoice = target) {
    if (comparing.pending || !onScreen) return;
    const left = sideRequest(s), right = sideRequest(t);
    if (typeof left === "string" || typeof right === "string") return;
    syncing.cancel();
    syncRevision.current += 1;
    const live = [s, t].some((side) => side.kind === "live");
    const result = await comparing.run((signal) => compareSchemas(left, right, ignoreDefiners, signal), {
      pending: live ? "Reading and comparing…" : "Comparing…",
      done: (r) => r.identical ? "No differences." : "",
    });
    if (result.ok) {
      setReport(result.value);
      setComparedSides(JSON.stringify([s, t]));
      setComparedLabels([sideChoiceLabel(s), sideChoiceLabel(t)]);
      setSetupOpen(false);
      setChosenKeys(result.value.sync.chosen_keys);
      setResultTab("differences");
      const first = result.value.objects.find((e) => e.status !== "identical");
      selection.select(first ? entryKey(first) : "", false);
    }
  }
  /** Compare a saved version with its database now: the connection in use, or
   * a saved connection, that reaches the same server (host, port and SSH
   * target; the same address through another SSH host, or with SSH on one
   * side only, is another server). */
  function nowVia(item: VersionItem): string | null {
    if (connection.host && serverMatch(item.server, connection).same) return "current";
    const match = saved.find((p) => serverMatch(item.server, p).same);
    return match ? match.id : null;
  }
  /** Why Compare with the database now is off for *item*. */
  function nowReason(item: VersionItem): string {
    return (connection.host && serverMatch(item.server, connection, "This version").ssh)
      || `No connection to ${serverAddress(item.server)} with a saved or typed password`;
  }
  /** The version is the source and the database now the target: what changed
   * since the version, and a script that would bring the database back to it. */
  function compareWithNow(item: VersionItem) {
    const via = nowVia(item);
    if (!via) return;
    const s: SideChoice = { kind: "version", id: item.id }, t: SideChoice = { kind: "live", via, database: item.database };
    chooseSource(s);
    setTarget(t);
    onTab("compare");
    void runCompare(s, t);
  }
  function compareFrom(item: VersionItem) {
    syncing.cancel();
    syncRevision.current += 1;
    chooseSource({ kind: "version", id: item.id });
    setTarget({ kind: "live", via: nowVia(item) || "current", database: item.database });
    setReport(null);
    onTab("compare");
  }
  const entries = (report?.objects || []).filter((e) => showIdentical || e.status !== "identical");
  const selectedEntry = tab === "compare" && resultTab === "differences"
    ? (report?.objects || []).find((e) => entryKey(e) === selection.key) || null : null;
  const openable = report ? scriptTarget(report, connection) : null;

  // ---- layout ------------------------------------------------------------------
  const tabs = (
    <Segmented<SchemaVersionsTab> label="Schema versions" value={tab} onChange={onTab} tabs="schema-versions"
      options={[{ value: "saved", label: "Saved versions", count: versions ? versions.length : null },
        { value: "compare", label: "Compare" }]} />
  );
  const savedControls = <>
    {tabs}
    <ToolbarSelect className="schema-database-filter" label="Database" value={database} options={databaseOptions}
      onChange={(value) => {
        setDatabase(value);
        // The selected version stays selected while the choice still lists it.
        if (selectedVersion && value && selectedVersion.database !== value) selection.select("", false);
      }} />
    {catalog.loading && <span className="muted schema-catalog-note" role="status">Reading databases…</span>}
    <span className="schema-versions-toolbar-gap" />
    <input ref={fileInput} type="file" accept=".json,application/json" hidden onChange={(e) => void importFile(e.target.files?.[0])} />
    <ActionButton icon="import" busy={importing.pending} busyLabel="Importing…" title="Import a schema version file (JSON)"
      onClick={() => fileInput.current?.click()}>Import…</ActionButton>
    <ActionButton data-action="schema-export-sql" icon="export" disabled={!connection.host} aria-haspopup="dialog"
      title={connection.host ? `Read a database on ${connectionLabel(connection)} and export a script that recreates it` : "Choose a connection first"}
      onClick={exportLive}>Export as SQL…</ActionButton>
    {(versions === null || shown.length > 0) && <ActionButton data-action="schema-save" primary icon="plus" disabled={!connection.host} aria-haspopup="dialog"
      title={connection.host ? `Save the schema of a database on ${connectionLabel(connection)}` : "Choose a connection first"}
      onClick={openSave}>Save version…</ActionButton>}
  </>;
  const compareControls = <>
    {tabs}
    <span className="schema-versions-toolbar-gap" />
    {report && <ExportMenu label="Export comparison" formats={[
      { key: "markdown", label: "Markdown (.md)", onSelect: () => { if (scriptCurrent) download(`schema-compare-${fileStem(report)}.md`, report.markdown || "", "text/markdown"); }, disabled: !scriptCurrent || !report.markdown },
      { key: "json", label: "JSON (.json)", onSelect: () => { if (scriptCurrent) download(`schema-compare-${fileStem(report)}.json`, JSON.stringify(report, null, 1) + "\n", "application/json"); }, disabled: !scriptCurrent },
      { key: "sql", label: "Sync script (.sql)", onSelect: () => { if (scriptCurrent) download(`schema-sync-${fileStem(report)}.sql`, report.sync.script, "application/sql"); }, disabled: !scriptCurrent },
    ]} />}
  </>;

  const inspector: InspectorContent | null = tab === "saved"
    ? selectedVersion && versionInspector(selectedVersion, detail, detailRead.phase === "failed" ? detailRead.message : "",
      previous.get(selectedVersion.id) || null, {
        now: nowVia(selectedVersion) ? () => compareWithNow(selectedVersion) : null,
        nowReason: nowReason(selectedVersion),
        compare: () => compareFrom(selectedVersion),
        exportFile: () => exportItem(selectedVersion),
        exportSql: () => setExportSheet({ kind: "version", item: selectedVersion }),
        rename: () => { renaming.reset(); setRenameSheet({ item: selectedVersion, name: selectedVersion.name, note: selectedVersion.note }); },
        remove: () => { deleting.reset(); setDeleteSheet(selectedVersion); },
      }, <VersionChecks key={`${selectedVersion.id}:${connectionKey(connection)}`} id={selectedVersion.id} connection={connection}
        enabled={onScreen && !!sharedInspector?.open} />)
    : selectedEntry && report && entryDetail(selectedEntry, report, chosen.has(selectedEntry.key));
  const emptyDatabase = !!database && !!versions && !shown.length;

  return (
    <InspectorContext.Provider value={sharedInspector}>
    <ViewLayout id="schema-versions" title="Schema versions" className={`schema-versions-view is-${tab}`}
      info={<p>Save the schema of a database on this computer and compare any two schemas: saved versions, or databases
        read now through the connection in use or a saved connection. A comparison has a source, the schema to match,
        and a target, the schema that would change: its sync script changes the target to match the source. Reading a
        database never changes it, and myflames never runs the script.</p>}
      controls={tab === "saved" ? savedControls : compareControls}
      onRefresh={tab === "saved" ? () => void loadList() : undefined}
      loading={tab === "saved" && listing.pending}
      inspector={inspector}
      inspectorEmpty={tab === "saved" ? <p>Select a version to see its objects and compare it.</p> : <p>Select an object to see how it differs.</p>}
      // A difference opens over the whole view (Back keeps the list, its sort
      // and scroll): two definitions side by side need the width.
      detailBackLabel={tab === "compare" ? "Back to differences" : ""}
      status={tab === "saved" ? <ViewStatus loading={listing.pending ? "Reading saved versions…" : ""}
        error={listing.phase === "failed" ? listing.message : ""} onRetry={() => void loadList()} /> : undefined}>
      <div id={`schema-versions-panel-${tab}`} role="tabpanel" aria-labelledby={`schema-versions-tab-${tab}`} className="schema-versions-panel">
        {tab === "saved" ? <>
          <ActionStatus className="schema-versions-line" state={saving.phase !== "idle" && !saveSheet ? saving : importing.phase !== "idle" ? importing
            : exporting.phase !== "idle" ? exporting : { phase: savedNote ? "done" : "idle", message: savedNote, startedAt: null }}
            onDismiss={() => { setSavedNote(""); saving.reset(); importing.reset(); exporting.reset(); }} />
          {versions && versions.length > 0 && !emptyDatabase && <SummaryLine>{summaryOfVersions(versions, shown.length, database)}</SummaryLine>}
          {versions && (!versions.length || emptyDatabase) ? (
            <EmptyState icon="branches" title={emptyDatabase ? `No saved versions of ${database}` : "No saved versions"}
              action={<ActionButton data-action="schema-save" primary icon="plus" disabled={!connection.host} aria-haspopup="dialog"
                onClick={openSave}>{emptyDatabase ? `Save version of ${database}…` : "Save version…"}</ActionButton>}>
              {emptyDatabase ? "Save its schema now to compare it later with the database, another version or another server."
                : "Save the schema of a database to compare it later with the database, another version or another server."}
            </EmptyState>
          ) : versions && (
            <DataTable<VersionItem> id="schema.versions" label="Saved schema versions" rows={shown} rowKey={(v) => v.id}
              selection={selection} defaultSort={{ key: "saved", dir: "desc" }} columns={versionColumns(previous)} />
          )}
        </> : <>
          <ComparisonSetup className="schema-compare-bar" runClassName="schema-compare-run"
            hasResult={!!report} expanded={setupOpen} onExpanded={setSetupOpen}
            source={report ? comparedLabels[0] : sideChoiceLabel(source)} target={report ? comparedLabels[1] : sideChoiceLabel(target)}
            context={report && <div className="schema-compare-sides" aria-label="Compared">
                <p><span>Source</span>{report.source.label}<time dateTime={report.source.taken_at}> · read {localTime(report.source.taken_at)}</time></p>
                <p><span>Target</span>{report.target.label}<time dateTime={report.target.taken_at}> · read {localTime(report.target.taken_at)}</time></p>
              </div>}
            contextText={report ? `Last compared: Source ${report.source.label} · read ${localTime(report.source.taken_at)}; Target ${report.target.label} · read ${localTime(report.target.taken_at)}` : undefined}
            onSwap={() => { chooseSource(target); setTarget(source); }} busy={comparing.pending}
            problem={sidesProblem} onCompare={() => void runCompare()}>
            <div className="schema-compare-fields">
              <SidePicker label="Source" hint="The schema to match" side={source} onChange={chooseSource} versions={versions || []}
                connection={connection} profiles={profiles} enabled={onScreen && (!report || setupOpen)} />
              {!report && <div className="schema-compare-direction">
                <span className="schema-compare-arrow" aria-hidden="true"><Icon name="chevron" size={16} /></span>
                <button type="button" className="icon-button schema-compare-swap" aria-label="Swap source and target"
                  title="Swap source and target" onClick={() => { chooseSource(target); setTarget(source); }}>
                  <Icon name="compare" />
                </button>
              </div>}
              <SidePicker label="Target" hint="The schema the sync script changes" side={target} onChange={setTarget}
                versions={versions || []} connection={connection} profiles={profiles} enabled={onScreen && (!report || setupOpen)} />
              <div className="schema-compare-run">
                <Checkbox checked={ignoreDefiners} onChange={setIgnoreDefiners}
                  title="Leave out differences that are only the DEFINER of a view, routine, trigger or event">Ignore definers</Checkbox>
                {!report && <ActionButton primary icon="compare" busy={comparing.pending} busyLabel="Comparing…"
                  disabled={!!sidesProblem} title={sidesProblem || "Compare the source with the target"} onClick={() => void runCompare()}>Compare</ActionButton>}
              </div>
            </div>
          </ComparisonSetup>
          {report && comparedSides !== JSON.stringify([source, target]) && <p className="schema-sides-changed" role="status">
            The chosen sides changed (next: Source {sideChoiceLabel(source)} → Target {sideChoiceLabel(target)}). Press Compare to update the differences.
          </p>}
          <ActionStatus className="schema-versions-line" state={comparing} onStop={comparing.cancel} stopLabel="Stop"
            onRetry={() => void runCompare()} />
          <ActionStatus className="schema-sync-status" state={syncing} onStop={syncing.cancel} stopLabel="Stop"
            onRetry={() => void regenerateSync(chosenKeys)} />
          {report && !scriptCurrent && !comparing.pending && !syncing.pending && syncing.phase !== "failed" &&
            <p className="schema-sync-stale" role="status">The sync script needs updating. <button type="button" className="text-button"
              onClick={() => void regenerateSync(chosenKeys)}>Try again</button></p>}
          {!report ? (
            <EmptyState icon="compare" title={sidesProblem ? "Choose a source and a target" : "Ready to compare"}>
              {sidesProblem ? "Each side can be a saved version or a database read now. The source is the schema to match; the target is the one that would change."
                : "Compare lists what differs and the sync script that would change the target to match the source."}
            </EmptyState>
          ) : <>
            <SummaryLine
              notes={report.notes.length ? report.notes : undefined} notesTitle="Notes about this comparison">
              {compareSummary(report)}
            </SummaryLine>
            <div className="schema-compare-tabs">
              <Segmented<"differences" | "sync"> label="Comparison" value={resultTab} onChange={setResultTab} tabs="schema-compare"
                options={[{ value: "differences", label: "Differences", count: report.objects.filter((e) => e.status !== "identical").length },
                  { value: "sync", label: "Sync script", count: report.sync.summary.statements }]} />
              {resultTab === "differences" && <Checkbox checked={showIdentical} onChange={setShowIdentical}>Show identical</Checkbox>}
            </div>
            <div id={`schema-compare-panel-${resultTab}`} role="tabpanel" aria-labelledby={`schema-compare-tab-${resultTab}`}>
              {resultTab === "differences" ? (
                <DataTable<CompareEntry> id="schema.differences" label="Differences" rows={entries} rowKey={entryKey}
                  selection={selection} columns={differenceColumns(report, chosen, toggleEntry,
                    (include) => void regenerateSync(include ? report.objects.map((e) => e.key) : []), comparing.pending)}
                  empty={<p className="schema-versions-empty-filter">{report.identical ? "The source and the target have the same objects and definitions." : "Every difference is hidden."}</p>} />
              ) : <SyncPanel report={report} openable={openable} onOpenSql={onOpenSql} current={scriptCurrent} />}
            </div>
          </>}
        </>}
      </div>
      {saveSheet && (
        <ConfirmSheet eyebrow="Save version" title="Save the schema of a database"
          description={<>myflames reads the definitions on {connectionLabel(connection)} (information_schema and SHOW CREATE; nothing
            on the server changes) and keeps them on this computer as the next version of that database.</>}
          confirmLabel="Save version" confirmTone="primary" busy={saving.pending} busyLabel="Saving…"
          progress={saving.pending ? saving.message : undefined}
          confirmDisabled={!saveSheet.database.trim()}
          returnFocus={() => document.querySelector<HTMLElement>('[data-action="schema-save"]')}
          onCancel={() => { saving.cancel(); setSaveSheet(null); }} onConfirm={() => void confirmSave()}>
          <FormGrid>
            <FormRow label="Database" htmlFor="schema-save-database"
              hint={catalog.loading ? "Reading the databases…" : catalog.error ? catalog.error : undefined}>
              {databases.length ? (
                <Select id="schema-save-database" label="Database" value={saveSheet.database}
                  options={[...(databases.includes(saveSheet.database) || !saveSheet.database ? [] : [[saveSheet.database, saveSheet.database] as [string, string]]),
                    ...databases.map((d) => [d, d] as [string, string])]}
                  onChange={(value) => setSaveSheet({ ...saveSheet, database: value })} />
              ) : (
                <input id="schema-save-database" value={saveSheet.database} maxLength={64} spellCheck={false} autoCapitalize="off"
                  onChange={(e) => setSaveSheet({ ...saveSheet, database: e.target.value })} />
              )}
            </FormRow>
            <FormRow label="Name" htmlFor="schema-save-name" hint="Optional, for example the release it belongs to.">
              <input id="schema-save-name" value={saveSheet.name} maxLength={200}
                onChange={(e) => setSaveSheet({ ...saveSheet, name: e.target.value.replace(/[\r\n]/g, " ") })} />
            </FormRow>
            <FormRow label="Note" htmlFor="schema-save-note">
              <textarea id="schema-save-note" rows={3} value={saveSheet.note} maxLength={10000}
                onChange={(e) => setSaveSheet({ ...saveSheet, note: e.target.value })} />
            </FormRow>
          </FormGrid>
          {saving.phase === "failed" && <p className="form-error" role="alert">{saving.message}</p>}
        </ConfirmSheet>
      )}
      {renameSheet && (
        <ConfirmSheet title={`Rename version ${renameSheet.item.number} of ${renameSheet.item.database}`}
          confirmLabel="Save" confirmTone="primary" busy={renaming.pending} busyLabel="Saving…"
          onCancel={() => setRenameSheet(null)} onConfirm={() => void confirmRename()}>
          <FormGrid>
            <FormRow label="Name" htmlFor="schema-rename-name">
              <input id="schema-rename-name" value={renameSheet.name} maxLength={200}
                onChange={(e) => setRenameSheet({ ...renameSheet, name: e.target.value.replace(/[\r\n]/g, " ") })} />
            </FormRow>
            <FormRow label="Note" htmlFor="schema-rename-note">
              <textarea id="schema-rename-note" rows={3} value={renameSheet.note} maxLength={10000}
                onChange={(e) => setRenameSheet({ ...renameSheet, note: e.target.value })} />
            </FormRow>
          </FormGrid>
          {renaming.phase === "failed" && <p className="form-error" role="alert">{renaming.message}</p>}
        </ConfirmSheet>
      )}
      {exportSheet && <SchemaExportSheet key={exportSheet.kind === "version" ? exportSheet.item.id : `live:${exportSheet.database}`}
        source={exportSheet} onOpenSql={onOpenSql} onClose={() => setExportSheet(null)} />}
      {deleteSheet && (
        <ConfirmSheet eyebrow="Delete version" title={`Delete ${versionPhrase(deleteSheet)}?`}
          description="It is removed from this computer. The database is not changed, and other versions keep their numbers."
          confirmLabel="Delete version" busy={deleting.pending} busyLabel="Deleting…"
          onCancel={() => setDeleteSheet(null)} onConfirm={() => void confirmDelete()}>
          {deleting.phase === "failed" && <p className="form-error" role="alert">{deleting.message}</p>}
        </ConfirmSheet>
      )}
    </ViewLayout>
    </InspectorContext.Provider>
  );
}

function savedText(item: VersionItem, verb: string): string {
  const base = `${verb} ${versionPhrase(item)} · ${countsText(item.counts)}.`;
  return item.same_as ? `${base} No changes since version ${item.same_as.number}.` : base;
}

function summaryOfVersions(all: VersionItem[], shown: number, database: string): string {
  if (database) return `${plural(shown, "saved version")} of ${database}`;
  const databases = new Set(all.map((v) => v.source_key)).size;
  return `${plural(all.length, "saved version")} of ${plural(databases, "database")}`;
}

function versionColumns(previous: Map<string, VersionItem>): Column<VersionItem>[] {
  return [
    { key: "database", label: "Database", primary: true, sort: (v) => v.database.toLowerCase(), render: (v) => v.database },
    { key: "version", label: "Version", num: true, sort: (v) => v.number, render: (v) => v.number.toLocaleString() },
    { key: "name", label: "Name", flex: true, minWidth: 120, sort: (v) => v.name.toLowerCase(),
      render: (v) => <>{v.name || <span className="muted">—</span>}{!v.complete && <span className="schema-version-flag"><Pill tone="warn"
        title="The account that read it may not see every object; the inspector says which kinds">May be incomplete</Pill></span>}</> },
    { key: "saved", label: "Saved", minWidth: 160, sort: (v) => v.saved_at, render: (v) => <time dateTime={v.saved_at}>{localTime(v.saved_at)}</time> },
    { key: "change", label: "Since previous", title: "Compared with the version before it of the same database",
      sort: (v) => { const p = previous.get(v.id); return !p ? "" : p.checksum === v.checksum ? "same" : "changed"; },
      render: (v) => { const p = previous.get(v.id); return !p ? <span className="muted">First</span> : p.checksum === v.checksum
        ? <span>Same as {p.number}</span> : <span>Changed</span>; } },
    { key: "objects", label: "Objects", num: true, title: "Tables, views, routines, triggers and events",
      sort: (v) => objectCount(v.counts), render: (v) => <span title={countsText(v.counts)}>{objectCount(v.counts).toLocaleString()}</span> },
    { key: "server", label: "Server", title: "The address it was read from, and the saved connection", sort: (v) => serverAddress(v.server),
      render: (v) => v.connection.profile_name ? `${v.connection.profile_name} · ${serverAddress(v.server)}` : serverAddress(v.server) },
    { key: "product", label: "Product", sort: (v) => productText(v.server), render: (v) => productText(v.server) },
  ];
}

function versionInspector(item: VersionItem, detail: VersionWithSnapshot | null, error: string, previous: VersionItem | null, act: {
  now: (() => void) | null; nowReason: string; compare: () => void; exportFile: () => void; exportSql: () => void;
  rename: () => void; remove: () => void;
}, checks: ReactNode): InspectorContent {
  const snapshot = detail && detail.id === item.id ? detail.snapshot : null;
  const incomplete = Object.entries(snapshot?.coverage || {}).filter(([, c]) => c.state !== "complete");
  return {
    key: `version:${item.id}`,
    title: `${item.database} · ${versionTitle(item)}`,
    meta: <span>{productText(item.server)} · {serverAddress(item.server)}</span>,
    actions: <div className="schema-inspector-actions">
      <ToolbarButton primary icon="compare" disabled={!act.now} onClick={() => act.now?.()}
        title={act.now ? "Read the database now and compare it with this version: what changed since, and a sync script back to it"
          : act.nowReason}>Compare with the database now</ToolbarButton>
      <ToolbarButton icon="compare" onClick={act.compare}>Compare with…</ToolbarButton>
      <ToolbarButton icon="export" onClick={act.exportFile} title="Save this version as a schema version file (JSON)">Export</ToolbarButton>
      <ToolbarButton icon="export" data-action="schema-export-sql" aria-haspopup="dialog" onClick={act.exportSql}
        title="A SQL script that recreates this version's schema on another server">Export as SQL…</ToolbarButton>
      <ToolbarButton icon="edit" onClick={act.rename}>Rename…</ToolbarButton>
      <ToolbarButton icon="trash" onClick={act.remove}>Delete…</ToolbarButton>
    </div>,
    body: <>
      <InspectorSection title="Version">
        <Fields rows={[
          ["Saved", <time dateTime={item.saved_at}>{localTime(item.saved_at)}</time>],
          item.connection.profile_name ? ["Connection", item.connection.profile_name] : null,
          ["Server", `${productText(item.server)} at ${serverAddress(item.server)}`],
          ["Objects", countsText(item.counts)],
          ["Since previous", previous ? (previous.checksum === item.checksum ? `Same as version ${previous.number}` : `Changed since version ${previous.number}`) : "First version"],
          ["Checksum", <code title={item.checksum}>{item.checksum.replace(/^sha256:/, "").slice(0, 16)}</code>],
          item.note ? ["Note", <span className="schema-note">{item.note}</span>] : null,
        ]} />
      </InspectorSection>
      {error && <p className="form-error" role="alert">{error}</p>}
      {incomplete.length > 0 && <InspectorSection title="May be incomplete">
        <ul className="schema-notes">{incomplete.map(([kind, c]) => <li key={kind}><strong>{kind}</strong>: {c.reason}</li>)}</ul>
      </InspectorSection>}
      {checks}
      {snapshot ? <ObjectBrowser key={item.id} objects={snapshot.objects} notes={snapshot.notes} />
        : !error && <p className="muted"><span className="spinner" aria-hidden="true" /> Reading the saved definitions…</p>}
    </>,
  };
}

/** Checks belong to the saved version. Opening its inspector alone does not
 * read live metadata; that starts only when Checks is expanded on screen. */
function VersionChecks({ id, connection, enabled }: { id: string; connection: Connection; enabled: boolean }) {
  const onScreen = useViewOnScreen();
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<LintReport | null>(null);
  const [stopped, setStopped] = useState(false);
  const reading = useAction(`${id}:${connectionKey(connection)}`);
  const attempted = useRef(false);
  async function load() {
    if (!open || !enabled || !onScreen) return;
    setStopped(false);
    attempted.current = true;
    const result = await reading.run((signal) => lintVersion(id, connection.host ? connection : undefined, signal), {
      pending: "Checking the saved version…", done: "",
    });
    if (result.ok) setReport(result.value);
    else if (result.reason === "cancelled") attempted.current = false;
  }
  useEffect(() => {
    if (!open || !enabled || !onScreen) {
      if (reading.pending) {
        attempted.current = false;
        reading.cancel();
      }
      return;
    }
    if (!report && !attempted.current) void load();
  }, [open, enabled, onScreen]);
  return <Disclosure title="Checks" count={report?.findings.length} open={open} onToggle={setOpen} className="schema-checks">
    <ActionStatus state={reading} onStop={() => { reading.cancel(); attempted.current = false; setStopped(true); }} stopLabel="Stop" onRetry={() => void load()} />
    {stopped && <p className="schema-sync-stale" role="status">Checks stopped. <button type="button" className="text-button"
      onClick={() => void load()}>Try again</button></p>}
    {report && <>
      {report.findings.length ? <ul className="schema-check-list">
        {report.findings.map((finding, i) => <li key={`${finding.rule}:${finding.object}:${finding.column}:${i}`} title={`${finding.text}\n${finding.detail}`}>
          <Pill tone="warn">Watch</Pill><span><code>{finding.object}{finding.column ? `.${finding.column}` : ""}</code>: {finding.text}</span>
        </li>)}
      </ul> : <p className="muted">No findings in the checks completed.</p>}
      {!!report.not_judged.length && <Disclosure title="Details" count={report.not_judged.length}>
        <ul className="schema-notes">{report.not_judged.map((finding, i) => <li key={`${finding.rule}:${i}`}>
          <Pill tone="neutral">Not judged</Pill>{finding.object && <><code>{finding.object}{finding.column ? `.${finding.column}` : ""}</code>: </>}{finding.reason}
        </li>)}</ul>
      </Disclosure>}
    </>}
  </Disclosure>;
}

function ObjectBrowser({ objects, notes }: { objects: VersionWithSnapshot["snapshot"]["objects"]; notes: string[] }) {
  const [open, setOpen] = useState("");
  const [type, setType] = useState("");
  const kinds = [...new Set(objects.filter((o) => o.type !== "database").map((o) => o.type))];
  const list = objects.filter((o) => o.type !== "database" && (!type || o.type === type));
  const chosen = objects.find((o) => `${o.type}:${o.name}` === open) || null;
  return <InspectorSection title="Objects">
    {kinds.length > 1 && <ToolbarSelect label="Show" value={type} onChange={setType}
      options={[["", "All objects"], ...kinds.map((k): [string, string] => [k, TYPE_LABELS[k]])]} />}
    <ul className="schema-object-list">
      {list.map((o) => {
        const id = `${o.type}:${o.name}`;
        return <li key={id}>
          <button type="button" aria-expanded={open === id} className={open === id ? "is-open" : undefined}
            onClick={() => setOpen(open === id ? "" : id)}>
            <span className="schema-object-chevron" aria-hidden="true"><Icon name="disclosure" size={12} /></span>
            <span className="schema-object-type">{TYPE_LABELS[o.type]}</span>
            <span className="schema-object-name">{o.name}</span>
            {o.ddl === null && <Pill tone="neutral" title={o.reason}>Not readable</Pill>}
          </button>
          {open === id && chosen && (chosen.ddl === null
            ? <p className="schema-object-reason">{chosen.reason}</p>
            : <CodeBlock text={chosen.ddl} label={`Definition of ${TYPE_LABELS[chosen.type].toLowerCase()} ${chosen.name}`} />)}
        </li>;
      })}
    </ul>
    {notes.length > 0 && <ul className="schema-notes">{notes.map((n) => <li key={n}>{n}</li>)}</ul>}
  </InspectorSection>;
}

function CodeBlock({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState("");
  return <div className="schema-code">
    <pre tabIndex={0} aria-label={label}><code>{text}</code></pre>
    <button type="button" className="text-button" onClick={() => {
      void copyToClipboard(text).then((ok) => setCopied(ok ? "Copied." : "Select the text to copy it."));
    }}>Copy</button>
    {copied && <span className="muted" role="status">{copied}</span>}
  </div>;
}

const entryKey = (e: CompareEntry) => e.key;

function RiskLabels({ risks }: { risks: Risks }) {
  return <span className="schema-risk-labels">
    {!!risks.destructive.length && <Pill tone="danger" title={risks.destructive.join("\n")}>Drops data</Pill>}
    {!!risks.data_dependent.length && <Pill tone="warn" title={risks.data_dependent.join("\n")}>May fail</Pill>}
    {risks.rebuild && <Pill tone="warn" title={risks.rebuild}>Rebuilds table</Pill>}
  </span>;
}

function RiskDetails({ risks }: { risks: Risks }) {
  return <ul className="schema-notes schema-risk-details">
    {risks.destructive.map((text) => <li key={text}><Pill tone="danger">Drops data</Pill>{text}</li>)}
    {risks.data_dependent.map((text) => <li key={text}><Pill tone="warn">May fail</Pill>{text}</li>)}
    {risks.rebuild && <li><Pill tone="warn">Rebuilds table</Pill>{risks.rebuild}</li>}
  </ul>;
}

function differenceColumns(report: CompareReport, chosen: Set<string>, toggle: (key: string, include: boolean) => void,
  all: (include: boolean) => void, busy: boolean): Column<CompareEntry>[] {
  const names = new Map(report.objects.map((e) => [e.key, e.name]));
  const needs = (e: CompareEntry) => (e.blocked_by || []).map((key) => names.get(key) || key).join(", ");
  return [
    { key: "include", name: "Include in sync script", minWidth: 190,
    label: <Checkbox checked={report.objects.length > 0 && report.objects.every((e) => chosen.has(e.key))}
      onChange={all} busy={busy} title="Include every object, including identical objects hidden from this list">Include in sync script</Checkbox>,
    render: (e) => <span onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
      <Checkbox checked={chosen.has(e.key)} onChange={(include) => toggle(e.key, include)} busy={busy}
        title={`Include ${TYPE_LABELS[e.type].toLowerCase()} ${e.name} in the sync script`}>
        <span className="visually-hidden">Include {e.name} in sync script</span></Checkbox></span> },
  { key: "status", label: "Status", sort: (e) => STATUS_ORDER.indexOf(e.status),
    render: (e) => <ComparisonBadge status={e.status} />, minWidth: 128 },
  { key: "type", label: "Type", sort: (e) => e.type, render: (e) => TYPE_LABELS[e.type] },
  { key: "name", label: "Name", primary: true, sort: (e) => e.name.toLowerCase(), render: (e) => e.name },
  { key: "sync", label: "Sync script", title: "What the sync script does to the target", minWidth: 150,
    sort: (e) => chosen.has(e.key) ? needs(e) ? "Not generated" : SYNC_ACTIONS[e.status] : "Excluded",
    render: (e) => <span className="muted">{chosen.has(e.key) ? needs(e) ? "Not generated" : SYNC_ACTIONS[e.status] : "Excluded"}</span> },
  { key: "risk", label: "Risk", minWidth: 160, render: (e) => <span className="schema-risk-labels">
    <RiskLabels risks={e.risks} />
    {needs(e) && <span className="schema-needs muted">Needs {needs(e)}</span>}
  </span> },
  { key: "what", label: "Details", flex: true,
    render: (e) => e.status === "different" ? (e.definer_only ? "Only the definer differs" : e.summary || "")
      : e.status === "not_compared" ? e.reason || "" : "" },
  ];
}

/** A difference over the whole view: what differs, then both definitions
 * side by side (source left, target right), or the one definition there is. */
function entryDetail(entry: CompareEntry, report: CompareReport, included: boolean): InspectorContent {
  const label = `${TYPE_LABELS[entry.type]} ${entry.name}`;
  const alters = report.sync.statements.filter((s) => s.algorithm && s.object?.type === entry.type
    && s.object.name === (entry.target_name || entry.name));
  const blocked = report.sync.skipped.filter((s) => s.type === entry.type && s.name === entry.name);
  const lead = included && entry.blocked_by?.length ? "Not generated in the sync script."
    : entry.status === "only_in_source"
    ? `Only in the source (${report.source.database}). ${included ? "The sync script creates it in the target." : "Excluded from the sync script."}`
    : entry.status === "only_in_target"
      ? `Only in the target (${report.target.database}). ${included ? "The sync script drops it from the target." : "Excluded from the sync script."}`
      : entry.status === "different"
        ? `In both, with differences. ${included ? `The sync script changes the target's ${TYPE_LABELS[entry.type].toLowerCase()} to match the source.` : "Excluded from the sync script."}`
        : entry.status === "not_compared" ? "Not compared: a definition could not be read." : "The same in the source and the target.";
  return {
    key: `entry:${entryKey(entry)}`,
    title: label,
    meta: <ComparisonBadge status={entry.status} />,
    body: <div className="schema-detail">
      <p className="schema-detail-lead">{lead}</p>
      {blocked.map((s, i) => <p key={i} className="schema-reason">{s.reason}</p>)}
      {entry.reason && <p className="schema-reason">{entry.reason}</p>}
      {(entry.risks.destructive.length > 0 || entry.risks.data_dependent.length > 0 || entry.risks.rebuild) && <section className="schema-detail-section" aria-label="Review before running">
        <h3>Review before running</h3><RiskDetails risks={entry.risks} />
      </section>}
      {alters.map((s, i) => <section key={i} className="schema-detail-section schema-alter-prediction" aria-label="ALTER prediction">
        {alters.length > 1 && <CodeBlock text={s.sql} label={`ALTER statement ${i + 1}`} />}
        <p className="alter-prediction" data-algorithm={s.algorithm!.predicted ? s.algorithm!.algorithm ?? "" : "none"}>
          Predicted: <strong>{s.algorithm!.predicted ? s.algorithm!.summary : "not predicted"}</strong>
          {!s.algorithm!.predicted && <>. {notPredictedReason(s.algorithm!.reason)}</>}
        </p>
        <AlterPredictionWhy prediction={s.algorithm!} />
      </section>)}
      {entry.changes && entry.changes.length > 0 && <section className="schema-detail-section" aria-label="What differs">
        <h3>What differs</h3>
        <ChangesTable changes={entry.changes} />
      </section>}
      {entry.status === "different" && entry.diff && entry.diff.length > 0 && <section className="schema-detail-section" aria-label="Definitions">
        <h3>Definitions</h3>
        <SplitDiff lines={entry.diff} target={entry.target_ddl || ""} source={entry.source_ddl || ""} label={`Definitions of ${label}`} />
      </section>}
      {entry.status === "only_in_source" && entry.source_ddl && <section className="schema-detail-section" aria-label="Definition in the source">
        <h3>Definition in the source</h3>
        <CodeBlock text={entry.source_ddl} label={`Definition of ${label} in the source`} />
      </section>}
      {entry.status === "only_in_target" && entry.target_ddl && <section className="schema-detail-section" aria-label="Definition in the target">
        <h3>Definition in the target</h3>
        <CodeBlock text={entry.target_ddl} label={`Definition of ${label} in the target`} />
      </section>}
      {entry.status === "not_compared" && (entry.source_ddl || entry.target_ddl) && <Disclosure title="The definition that could be read">
        <CodeBlock text={(entry.source_ddl || entry.target_ddl)!} label={`Definition of ${label}`} />
      </Disclosure>}
    </div>,
  };
}

/** What differs, one row per element: its name, then the source's and the
 * target's text. Elements without two texts (a moved column, a definition)
 * keep Python's sentence. */
function ChangesTable({ changes }: { changes: Change[] }) {
  return <table className="schema-changes-table">
    <thead><tr><th scope="col">Element</th><th scope="col">Source</th><th scope="col">Target</th></tr></thead>
    <tbody>{changes.map((c, i) => {
      const name = c.label || c.text.split(":")[0];
      const twoTexts = c.change !== "moved" && (c.source != null || c.target != null)
        && !`${c.source ?? ""}${c.target ?? ""}`.includes("\n");
      return <tr key={i} className={`is-${c.change}`}>
        <th scope="row"><span className="schema-change-name">{name}</span>
          <ComparisonBadge status={c.change} className="schema-change-kind" />
          <RiskLabels risks={c.risks} /></th>
        {twoTexts ? <>
          <td>{c.source != null ? <code>{c.source}</code> : <span className="muted">Not in source</span>}
            {c.collation && <span className="schema-change-collation">Character set and collation {c.collation.source.join(" / ")}</span>}</td>
          <td>{c.target != null ? <code>{c.target}</code> : <span className="muted">Not in target</span>}
            {c.collation && <span className="schema-change-collation">Character set and collation {c.collation.target.join(" / ")}</span>}</td>
        </> : <td colSpan={2}>{c.text}</td>}
      </tr>;
    })}</tbody>
  </table>;
}

type SplitRow =
  | { kind: "same" | "change" | "source" | "target"; source?: [number, string]; target?: [number, string] }
  | { kind: "gap"; count: number };

/** Side-by-side rows from the unified diff (lines_diff(target, source), three
 * lines of context): removed lines are the target's, added ones the source's,
 * paired in order; the lines both share outside the hunks fold into a gap. */
export function splitRows(lines: DiffLine[], targetLines: number): SplitRow[] {
  const rows: SplitRow[] = [];
  let t = 1, s = 1;
  let dels: [number, string][] = [], adds: [number, string][] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(dels.length, adds.length); i++) {
      const target = dels[i], source = adds[i];
      rows.push({ kind: target && source ? "change" : source ? "source" : "target", source, target });
    }
    dels = [];
    adds = [];
  };
  for (const [op, text] of lines) {
    if (op === "@") {
      flush();
      const m = /^@@ -(\d+),\d+ \+(\d+),\d+ @@/.exec(text);
      if (m) {
        const nt = Number(m[1]), ns = Number(m[2]);
        if (nt > t) rows.push({ kind: "gap", count: nt - t });
        t = nt;
        s = ns;
      }
      continue;
    }
    if (op === "-") { dels.push([t++, text]); continue; }
    if (op === "+") { adds.push([s++, text]); continue; }
    flush();
    rows.push({ kind: "same", source: [s++, text], target: [t++, text] });
  }
  flush();
  if (targetLines >= t) rows.push({ kind: "gap", count: targetLines - t + 1 });
  return rows;
}

/** *text* with the part that differs from *other* marked (common prefix and suffix kept plain). */
function marked(text: string, other: string): ReactNode {
  let start = 0;
  while (start < text.length && start < other.length && text[start] === other[start]) start++;
  let end = 0;
  while (end < text.length - start && end < other.length - start && text[text.length - 1 - end] === other[other.length - 1 - end]) end++;
  if (!start && !end) return text;
  return <>{text.slice(0, start)}<mark>{text.slice(start, text.length - end)}</mark>{text.slice(text.length - end)}</>;
}

function SplitDiff({ lines, source, target, label }: { lines: DiffLine[]; source: string; target: string; label: string }) {
  const rows = splitRows(lines, target ? target.split("\n").length : 0);
  const [copied, setCopied] = useState("");
  return <div className="schema-split-wrap">
    <table className="schema-split" aria-label={label}>
      <colgroup><col className="schema-split-num" /><col /><col className="schema-split-num" /><col /></colgroup>
      <thead><tr>
        <th scope="col" colSpan={2}>Source</th>
        <th scope="col" colSpan={2}>Target</th>
      </tr></thead>
      <tbody>{rows.map((row, i) => row.kind === "gap"
        ? <tr key={i} className="is-gap"><td colSpan={4}>{plural(row.count, "line")} the same on both sides</td></tr>
        : <tr key={i} className={`is-${row.kind}`}>
          <td className="schema-split-n">{row.source?.[0] ?? ""}</td>
          <td className={row.source && row.kind !== "same" ? "is-source" : row.source ? "" : "is-empty"}>
            {row.source ? (row.kind === "change" && row.target ? marked(row.source[1], row.target[1]) : row.source[1]) : ""}
          </td>
          <td className="schema-split-n">{row.target?.[0] ?? ""}</td>
          <td className={row.target && row.kind !== "same" ? "is-target" : row.target ? "" : "is-empty"}>
            {row.target ? (row.kind === "change" && row.source ? marked(row.target[1], row.source[1]) : row.target[1]) : ""}
          </td>
        </tr>)}</tbody>
    </table>
    <div className="schema-split-actions">
      <button type="button" className="text-button" onClick={() => void copyToClipboard(source).then((ok) => setCopied(ok ? "Copied the source's definition." : "Select the text to copy it."))}>Copy source</button>
      <button type="button" className="text-button" onClick={() => void copyToClipboard(target).then((ok) => setCopied(ok ? "Copied the target's definition." : "Select the text to copy it."))}>Copy target</button>
      {copied && <span className="muted" role="status">{copied}</span>}
    </div>
  </div>;
}

function compareSummary(report: CompareReport): string {
  const s = report.summary;
  if (report.identical) return `No differences: ${plural(s.identical, "object")} compared.`;
  const parts = (["different", "only_in_source", "only_in_target", "not_compared"] as CompareStatus[]).filter((k) => s[k])
    .map((k) => `${s[k].toLocaleString()} ${STATUS_LABELS[k].toLowerCase()}`);
  if (s.identical) parts.push(`${s.identical.toLocaleString()} identical`);
  return parts.join(" · ");
}

function fileStem(report: CompareReport): string {
  const clean = (text: string) => text.replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40);
  return `${clean(report.source.database)}-to-${clean(report.target.database)}`;
}

/** Whether the script may open in the Query editor, which runs it on the
 * connection in use: only when the target is a database read now through that
 * server. A saved version is a past state, not a database to run it on.
 * Otherwise the reason. */
function scriptTarget(report: CompareReport, connection: Connection): { ok: boolean; reason: string } {
  const side = report.target;
  if (side.kind !== "live") {
    return { ok: false, reason: `The target is ${side.kind === "file" ? "a file" : "a saved version"}, not a database. Copy or download the script to run it on a database with that schema, or compare with a database now as the target.` };
  }
  // The same server: host, port and SSH target. Through another SSH host (or
  // with SSH on one side only) the same address can be another server.
  const match = serverMatch(side.server, connection, "The target");
  if (match.same) return { ok: true, reason: "" };
  if (match.ssh) {
    const route = side.server.ssh_host ? serverAddress(side.server) : `${serverAddress(side.server)} without SSH`;
    return { ok: false, reason: `${match.ssh} Use a connection to ${route} to open the script in the Query editor.` };
  }
  return { ok: false, reason: `The script changes ${side.database} at ${serverAddress(side.server)}; use a connection to that server to open it in the Query editor.` };
}

function SyncPanel({ report, openable, onOpenSql, current }: {
  report: CompareReport; openable: { ok: boolean; reason: string } | null; onOpenSql: (sql: string) => void; current: boolean;
}) {
  const m = report.sync;
  const [copied, setCopied] = useState("");
  useEffect(() => setCopied(""), [m.script, current]);
  if (!m.summary.statements && !m.skipped.length && !m.omitted.length && current) {
    return <EmptyState icon="check" title={report.identical ? "Nothing to change" : "No statements selected"}>
      {report.identical ? "The target already matches the source." : "Choose objects in Differences to include them in the sync script."}
    </EmptyState>;
  }
  const risky = m.statements.filter((s) => s.risks.length || s.risk_details.destructive.length || s.risk_details.data_dependent.length || s.risk_details.rebuild);
  return <div className={`schema-migration${current ? "" : " is-stale"}`} aria-busy={!current}>
    <div className="schema-migration-head">
      <p>{plural(m.summary.statements, "statement")} that would change <strong>{report.target.database}</strong> (the target) to
        match <strong>{report.source.database}</strong> (the source)
        {m.summary.drops_data ? <> · <strong>{plural(m.summary.drops_data, "drops data", "drop data")}</strong></> : null}
        {m.summary.skipped ? <> · {m.summary.skipped.toLocaleString()} not generated</> : null}. myflames never runs them.</p>
      <div className="schema-migration-actions">
        <ToolbarButton icon="copy" disabled={!current} onClick={() => { if (current) void copyToClipboard(m.script).then((ok) => setCopied(ok ? "Copied." : "Select the text to copy it.")); }}>Copy script</ToolbarButton>
        <ToolbarButton icon="export" disabled={!current} onClick={() => { if (current) download(`schema-sync-${fileStem(report)}.sql`, m.script, "application/sql"); }}>Download .sql</ToolbarButton>
        <ToolbarButton primary icon="query" disabled={!current || !openable?.ok} title={!current ? "Wait for the sync script to update" : openable?.ok ? "Open the script in a new Query editor tab; nothing runs until you run it there" : openable?.reason}
          onClick={() => { if (current && openable?.ok) onOpenSql(m.script); }}>Open in Query editor</ToolbarButton>
        {copied && <span className="muted" role="status">{copied}</span>}
      </div>
      {openable && !openable.ok && <p className="muted schema-migration-why">{openable.reason}</p>}
    </div>
    {(m.warnings.length > 0 || m.skipped.length > 0 || risky.length > 0) && <Disclosure open title="Review before running" count={m.warnings.length + m.skipped.length + risky.length}>
      <ul className="schema-notes">
        {risky.map((s, i) => <li key={`r${i}`}>{s.risks.map((r) => <Pill key={r} tone={r === "drops_data" ? "danger" : "warn"}>{RISK_LABELS[r]}</Pill>)}
          <code>{firstLine(s.sql)}</code>{s.warnings.length ? ` ${s.warnings.join(" ")}` : ""}
          {(s.risk_details.destructive.length > 0 || s.risk_details.data_dependent.length > 0 || s.risk_details.rebuild) && <RiskDetails risks={s.risk_details} />}</li>)}
        {m.skipped.map((s) => <li key={`s${s.type}:${s.name}`}><Pill tone="neutral">Not generated</Pill>{TYPE_LABELS[s.type]} {s.name}: {s.reason}</li>)}
        {m.warnings.map((w) => <li key={w}>{w}</li>)}
      </ul>
    </Disclosure>}
    {m.omitted.length > 0 && <Disclosure title="Excluded from sync script" count={m.omitted.length}>
      <ul className="schema-notes">{m.omitted.map((item) => <li key={item.key}><code>{item.key}</code>: {item.reason}</li>)}</ul>
    </Disclosure>}
    <CodeBlockPlain text={m.script} label="Sync script" />
  </div>;
}

function firstLine(sql: string): string {
  const line = sql.split("\n")[0];
  return line.length > 90 ? `${line.slice(0, 89)}…` : line;
}

/** The script with comment lines dimmed (presentation only). */
function CodeBlockPlain({ text, label }: { text: string; label: string }) {
  return <pre className="schema-script" tabIndex={0} aria-label={label}>
    {text.split("\n").map((line, i) => <span key={i} className={line.startsWith("--") ? "sql-comment" : undefined}>{line}{"\n"}</span>)}
  </pre>;
}

function SidePicker({ label, hint, side, onChange, versions, connection, profiles, enabled }: {
  label: string; hint: string; side: SideChoice; onChange: (side: SideChoice) => void; versions: VersionItem[];
  connection: Connection; profiles: Profile[]; enabled: boolean;
}) {
  const id = `schema-side-${label.toLowerCase()}`;
  const via = side.kind === "live" ? side.via : "";
  const profile = profiles.find((p) => p.id === via && p.has_saved_password);
  const viaConnection = via === "current" ? connection : profile ? profileConnection(profile) : null;
  const catalog = useSchemaCatalog(viaConnection || connection, enabled && side.kind === "live" && !!viaConnection?.host);
  const databases = (catalog.catalog?.schemas || []).filter((s) => !s.system && !SYSTEM.has(s.name)).map((s) => s.name);
  const versionOptions: [string, string][] = [["", versions.length ? "Choose a saved version" : "No saved versions"],
    ...versions.map((v) => [v.id, `${v.database} · ${versionTitle(v)} · ${localTime(v.saved_at)}`] as [string, string])];
  // A chosen saved connection that was deleted, or whose password was
  // forgotten, stays chosen and says so (savedConnections.ts).
  // So does one that became the connection in use (it is no longer listed apart).
  const unlisted = via && via !== "current" && !profile ? profiles.find((p) => p.id === via) : undefined;
  const listed: [string, string][] = [
    ["current", connection.host ? `In use: ${connectionLabel(connection)}` : "In use: none chosen"],
    ...profiles.filter((p) => p.has_saved_password && !(p.id === connection.profile_id))
      .map((p) => [p.id, connectionLabel(p)] as [string, string]),
  ];
  const connectionOptions: [string, string][] = via && via !== "current" && !listed.some(([v]) => v === via)
    ? [...listed, [via, profile ? connectionLabel(profile)
      : unlisted ? `${connectionLabel(unlisted)} · no saved password` : deletedConnectionLabel(via)]] : listed;
  // A database read now starts with the connection's default database, else
  // the first one on the server, so nothing has to be typed.
  useEffect(() => {
    if (side.kind === "live" && !side.database && databases.length) {
      const preferred = viaConnection?.database && databases.includes(viaConnection.database) ? viaConnection.database : databases[0];
      onChange({ ...side, database: preferred });
    }
  }, [side.kind, via, databases.join("\0")]);
  return <fieldset className="schema-side" aria-labelledby={`${id}-label`}>
    <legend id={`${id}-label`} title={hint}>{label}</legend>
    <Segmented<"version" | "live"> label={`${label}: what to read`} value={side.kind}
      onChange={(kind) => onChange(kind === "version" ? { kind, id: versions[0]?.id || "" } : { kind, via: "current", database: connection.database || "" })}
      options={[{ value: "version", label: "Saved version" }, { value: "live", label: "Database now" }]} />
    {side.kind === "version" ? (
      <Select id={`${id}-version`} label={`${label} saved version`} value={side.id} options={versionOptions}
        onChange={(value) => onChange({ kind: "version", id: value })} />
    ) : <div className="schema-side-live">
      <Select id={`${id}-connection`} label={`${label} connection`} value={side.via} options={connectionOptions}
        onChange={(value) => onChange({ kind: "live", via: value, database: "" })} />
      {databases.length ? (
        <Select id={`${id}-database`} label={`${label} database`} value={side.database}
          options={[["", "Choose a database"] as [string, string], ...(side.database && !databases.includes(side.database) ? [[side.database, side.database] as [string, string]] : []),
            ...databases.map((d) => [d, d] as [string, string])]}
          onChange={(value) => onChange({ ...side, database: value })} />
      ) : (
        <input id={`${id}-database`} aria-label={`${label} database`} placeholder={catalog.loading ? "Reading databases…" : "Database"}
          value={side.database} maxLength={64} spellCheck={false} autoCapitalize="off"
          onChange={(e) => onChange({ ...side, database: e.target.value })} />
      )}
    </div>}
    {side.kind === "live" && catalog.error && <p className="hint">{catalog.error}</p>}
  </fieldset>;
}
