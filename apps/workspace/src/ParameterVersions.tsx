// Server > Parameters > Versions: how parameters differ between two MySQL or
// MariaDB releases (Compare versions) and every release where one parameter
// changed or was deprecated (Parameter history). The answers come from the
// catalog shipped with myflames (param_versions.py); nothing here reads a
// server. Python writes every sentence; this view lays them out.
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import type { ConfigReport, Connection } from "./api";
import { connectionKey, type Bound } from "./binding";
import { Icon } from "./components";
import { ComparisonBadge } from "./compareShared";
import type { CompareStatus } from "./compareApi";
import { useViewOnScreen } from "./viewOnScreen";
import { ActionStatus, Checkbox, DataTable, EmptyState, Fields, SearchField, Segmented, Select, SummaryLine, ViewLayout,
  useAction, useInspector, type Column, type InspectorContent } from "./kit";
import { CHANGE_KINDS, ENGINE_NAMES, KIND_FILTERS, KIND_LABELS, compareReleases, compareVersions, defaultPair, factRows, kindCounts,
  kindValue, matchesKind, parameterHistory, seriesGroups, serverRelease, shownKind, versionCatalog,
  type CatalogEngine, type ChangeKind, type FactRow, type HistoryEvent, type KindFilter, type ParameterHistory, type Shown,
  type VersionChange, type VersionComparison, type VersionEngine } from "./paramVersionsApi";
import "./parameter-versions.css";

/** "Version history" in the Parameters inspector: open Parameter history for
 * this name and the engine of the report it came from. */
export type HistoryRequest = { name: string; engine: VersionEngine | null; n: number };
type Mode = "compare" | "history";
/** Below this pane width From and To share one column (Parameter, Change,
 * two values and Since need about this much without scrolling). */
const NARROW_PANE = 860;

/** Added and Removed name the side that has the parameter; every other kind
 * is a difference. Relationship tones only (compareShared), never health. */
const BADGE: Record<ChangeKind, CompareStatus> = {
  added: "only_in_target", removed: "only_in_source", deprecated: "different", undeprecated: "different", default: "different",
  dynamic: "different", scope: "different", type: "different", values: "different", range: "different",
};
function KindBadges({ kinds, all = false }: { kinds: ChangeKind[]; all?: boolean }) {
  // At most three pills per row; the rest are named on hover.
  const shown = all || kinds.length <= 3 ? kinds : kinds.slice(0, 2);
  const rest = kinds.slice(shown.length);
  return <span className="pv-kinds">
    {shown.map(k => <ComparisonBadge key={k} status={BADGE[k]}>{KIND_LABELS[k]}</ComparisonBadge>)}
    {rest.length > 0 && <span className="pv-more" title={rest.map(k => KIND_LABELS[k]).join(", ")}>+{rest.length}<span className="visually-hidden">: {rest.map(k => KIND_LABELS[k]).join(", ")}</span></span>}
  </span>;
}
/** Text that may wrap after a comma or a space, never inside a digit group. */
function Breakable({ text }: { text: string }) {
  const pieces = text.split(/(?<=[, ])/);
  return pieces.length > 1 ? <>{pieces.map((piece, i) => <span key={i}>{piece}{i < pieces.length - 1 && <wbr />}</span>)}</> : <>{text}</>;
}
/** A value as Python sent it, grouped and explained on hover (paramVersionsApi).
 * *wrap*: in the facts and history tables long values wrap at commas; table
 * rows stay one line and truncate. */
function Value({ value, wrap = false }: { value: Shown; wrap?: boolean }) {
  const text = !wrap ? value.text : value.parts ? <>{value.parts.map((part, i) => <span key={i}>{i > 0 && " "}<span className="pv-part"><Breakable text={part} /></span></span>)}</>
    : <Breakable text={value.text} />;
  return <span className={`pv-value${value.muted ? " muted" : ""}`} title={value.title}>{value.code ? <code>{text}</code> : text}</span>;
}
/** A version as a sortable key: 8.0.9 before 8.0.10. */
const versionKey = (v: string | undefined) => (v || "").split(".").map(n => n.padStart(5, "0")).join(".");
const sentence = (text: string) => /[.!?]$/.test(text.trim()) ? text.trim() : `${text.trim()}.`;

/** One release choice: optgroups by series, newest first; never typed. */
function ReleaseSelect({ label, value, catalog, onChange }: { label: string; value: string; catalog: CatalogEngine; onChange: (v: string) => void }) {
  const groups = useMemo(() => seriesGroups(catalog.versions), [catalog]);
  const known = catalog.versions.some(r => r.version === value);
  const tag = (maturity: string) => /^lts$/i.test(maturity) ? "LTS" : /^innovation$/i.test(maturity) ? "Innovation" : /^ga$/i.test(maturity) ? "GA" : "";
  return <label className="toolbar-field pv-field">
    <span>{label}</span>
    <select className="select" aria-label={label} value={value} onChange={e => onChange(e.target.value)}>
      {!known && <option value={value}>{value || "Choose a release"}</option>}
      {groups.map(g => <optgroup key={g.series} label={`${catalog.product} ${g.series}${tag(g.releases[0].maturity) ? ` (${tag(g.releases[0].maturity)})` : ""}`}>
        {g.releases.map(r => <option key={r.version} value={r.version}>{r.version}</option>)}
      </optgroup>)}
    </select>
  </label>;
}

/** The catalog's names in a list under a text field: arrows and Enter pick a
 * name; Enter on typed text asks for it as written (the catalog then offers
 * close names). Escape closes the list first. */
function ParameterCombobox({ names, value, onChoose, inputRef }: {
  names: string[]; value: string; onChoose: (name: string) => void; inputRef: RefObject<HTMLInputElement>;
}) {
  const [query, setQuery] = useState(value);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const id = useId();
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => { setQuery(value); }, [value]);
  const needle = query.trim().toLowerCase();
  const matches = useMemo(() => {
    const all = needle ? names.filter(n => n.toLowerCase().includes(needle)) : names;
    return [...all].sort((a, b) => Number(!a.toLowerCase().startsWith(needle)) - Number(!b.toLowerCase().startsWith(needle)) || a.localeCompare(b));
  }, [names, needle]);
  const shown = matches.slice(0, 100);
  useEffect(() => { if (active >= 0) document.getElementById(`${id}-option-${active}`)?.scrollIntoView({ block: "nearest" }); }, [active, id]);
  function choose(name: string) {
    const exact = names.find(n => n.toLowerCase() === name.trim().toLowerCase());
    const chosen = exact || name.trim();
    if (!chosen) return;
    setQuery(chosen); setOpen(false); setActive(-1); onChoose(chosen);
  }
  return <div className="pv-combobox toolbar-field pv-field">
    <label htmlFor={`${id}-input`}>Parameter</label>
    <span className="pv-combobox-anchor">
      <span className="toolbar-search pv-combobox-input">
        <Icon name="search" size={14} />
        <input ref={inputRef} id={`${id}-input`} type="text" role="combobox" aria-expanded={open && shown.length > 0} aria-controls={`${id}-list`}
          aria-autocomplete="list" aria-activedescendant={open && active >= 0 && active < shown.length ? `${id}-option-${active}` : undefined}
          value={query} placeholder="Type a parameter name" spellCheck={false} autoCapitalize="off" autoComplete="off" maxLength={64}
          onChange={e => { setQuery(e.target.value); setOpen(true); setActive(-1); }}
          onFocus={() => setOpen(true)} onClick={() => setOpen(true)}
          onBlur={e => { if (!list.current?.contains(e.relatedTarget as Node | null)) setOpen(false); }}
          onKeyDown={e => {
            if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive(a => Math.min(shown.length - 1, a + 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setActive(a => Math.max(0, a - 1)); }
            else if (e.key === "Enter") { e.preventDefault(); choose(open && active >= 0 && shown[active] ? shown[active] : query); }
            else if (e.key === "Escape" && open) { e.preventDefault(); e.stopPropagation(); setOpen(false); setActive(-1); }
            else if (e.key === "Tab") setOpen(false);
          }} />
      </span>
      <div ref={list} className="pv-combobox-list" id={`${id}-list`} role="listbox" aria-label="Parameters in the catalog" hidden={!open || !shown.length}>
        {shown.map((name, i) => <div key={name} id={`${id}-option-${i}`} role="option" aria-selected={i === active} className="pv-option"
          onMouseDown={e => e.preventDefault()} onMouseEnter={() => setActive(i)} onClick={() => choose(name)}><code>{name}</code></div>)}
        {matches.length > shown.length && <p className="pv-combobox-more">{shown.length.toLocaleString()} of {matches.length.toLocaleString()} names. Type to narrow the list.</p>}
      </div>
    </span>
  </div>;
}

/** Setting | From | To, one row per fact; a row that differs says so in words. */
function FactsTable({ label, from, to, rows }: { label: string; from: string; to: string; rows: FactRow[] }) {
  return <table className="server-compare-table pv-facts" aria-label={label}>
    <thead><tr><th scope="col">Setting</th><th scope="col">{from}</th><th scope="col">{to}</th></tr></thead>
    <tbody>{rows.map(r => <tr key={r.key} className={r.differs ? "is-different" : undefined}>
      <th scope="row">{r.label}{r.differs && <ComparisonBadge status="different" className="server-compare-kind" />}</th>
      <td><Value value={r.a} wrap /></td><td><Value value={r.b} wrap /></td>
    </tr>)}</tbody>
  </table>;
}

/** Parameter history's changes, grouped by series, oldest first. */
function HistoryTable({ history }: { history: ParameterHistory }) {
  const events = [...history.events].sort((a, b) => compareReleases(a.version, b.version));
  const groups: { series: string; events: HistoryEvent[] }[] = [];
  for (const e of events) {
    const last = groups[groups.length - 1];
    if (last && last.series === e.series) last.events.push(e); else groups.push({ series: e.series, events: [e] });
  }
  if (!events.length) return <p className="pv-empty">No changes across the cataloged releases.</p>;
  return <div className="pv-history-frame" tabIndex={0} role="region" aria-label={`Changes to ${history.name}`}>
    <table className="pv-history-table">
      <thead><tr><th scope="col">Version</th><th scope="col">Change</th><th scope="col">Before</th><th scope="col">After</th></tr></thead>
      {groups.map(g => <tbody key={g.series} data-series={g.series}>
        <tr className="pv-series-row"><th scope="rowgroup" colSpan={4}>{history.product} {g.series}</th></tr>
        {g.events.map(e => {
          const kind = shownKind(e.kinds);
          return <tr key={e.version} data-version={e.version}>
            <td className="pv-version" title={e.previous ? `Compared with ${e.previous}` : "The first cataloged release"}><code>{e.version}</code></td>
            <td className="pv-change"><KindBadges kinds={e.kinds} all />{e.text.length > 0 && <span className="pv-text">{e.text.map(sentence).join(" ")}</span>}</td>
            <td><Value value={kindValue(kind, e.before)} wrap /></td>
            <td><Value value={kindValue(kind, e.after)} wrap /></td>
          </tr>;
        })}
      </tbody>)}
    </table>
  </div>;
}

export function ParameterVersions({ connection, controls, onBack, parameters, selectedName, request }: {
  connection: Connection; controls: ReactNode; onBack?: () => void;
  /** The Parameters report: the engine, release and set parameters of the connection in use. */
  parameters: Bound<ConfigReport> | null;
  /** The parameter selected in the Parameters list (prefills Parameter history). */
  selectedName: string;
  request: HistoryRequest | null;
}) {
  const onScreen = useViewOnScreen();
  const inspector = useInspector();
  const activeKey = connectionKey(connection);
  // Only the report of the connection in use says what that server is.
  const own = parameters && parameters.key === activeKey ? parameters.report : null;
  const ownEngine: VersionEngine | null = own ? (own.engine === "mariadb" ? "mariadb" : "mysql") : null;
  const percona = !!own && (own.flavor === "percona" || /percona/i.test(own.server_product || ""));
  const serverName = connection.name || `${connection.host}:${connection.port}`;

  const [mode, setMode] = useState<Mode>("compare");
  const combo = useRef<HTMLInputElement>(null);
  // The pane's width (the side inspector narrows it); a hidden pane keeps the last one.
  const pane = useRef<HTMLDivElement>(null);
  const [paneWidth, setPaneWidth] = useState(0);
  useLayoutEffect(() => {
    const el = pane.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(entries => { const w = Math.round(entries[0]?.contentRect.width || 0); if (w > 0) setPaneWidth(w); });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // The catalog index: read once, when Versions is first on screen.
  const catalogRead = useAction();
  const [catalog, setCatalog] = useState<{ engines: CatalogEngine[] } | null>(null);
  const catalogTried = useRef(false);
  async function loadCatalog() {
    catalogTried.current = true;
    const read = await catalogRead.run(signal => versionCatalog(signal), { pending: "Reading the version catalog…", done: "" });
    if (read.ok) setCatalog(read.value);
  }
  useEffect(() => { if (onScreen && !catalogTried.current) void loadCatalog(); }, [onScreen]);

  // Engine: the connection's, until the user picks one.
  const [engine, setEngineState] = useState<VersionEngine>(ownEngine || "mysql");
  const enginePicked = useRef(false);
  useEffect(() => { if (!enginePicked.current && ownEngine) setEngineState(ownEngine); }, [ownEngine]);
  const cat = catalog?.engines.find(e => e.engine === engine) || null;
  const serverVersion = own && ownEngine === engine ? own.server_version : null;

  // From and To: the server's release and the newest one, until the user picks.
  const [pair, setPairState] = useState<{ from: string; to: string } | null>(null);
  const pairPicked = useRef(false);
  const defaults = useMemo(() => cat && cat.versions.length ? defaultPair(cat, serverVersion) : null, [cat, serverVersion]);
  useEffect(() => { if (!pairPicked.current && defaults) setPairState(defaults); }, [defaults]);
  const setPair = (next: { from: string; to: string }) => { pairPicked.current = true; setPairState(next); };
  function setEngine(next: string) {
    const e: VersionEngine = next === "mariadb" ? "mariadb" : "mysql";
    if (e === engine) return;
    enginePicked.current = true; pairPicked.current = false; setPairState(null); setEngineState(e); setSelected("");
  }

  // Compare versions: reads whenever the choice changes (local data).
  const choice = cat && pair && pair.from && pair.to ? JSON.stringify([engine, pair.from, pair.to]) : "";
  const comparing = useAction(choice);
  const [result, setResult] = useState<{ choice: string; report: VersionComparison } | null>(null);
  async function runCompare(which: string) {
    if (!which) return;
    const [e, from, to] = JSON.parse(which) as [VersionEngine, string, string];
    const read = await comparing.run(signal => compareVersions(e, from, to, signal),
      { pending: `Comparing ${ENGINE_NAMES[e]} ${from} with ${to}…`, done: "" });
    if (read.ok) setResult({ choice: which, report: read.value });
  }
  useEffect(() => {
    if (onScreen && mode === "compare" && choice && result?.choice !== choice) void runCompare(choice);
  }, [onScreen, mode, choice]);

  const [kind, setKind] = useState<KindFilter>("all");
  const [search, setSearch] = useState("");
  const [onlySet, setOnlySet] = useState(false);
  const [selected, setSelected] = useState("");
  // Names set on the connection in use (modified), when its engine is the one compared.
  const setNames = useMemo(() => own?.parameters && ownEngine === engine
    ? new Set(own.parameters.items.filter(p => p.modified === true).map(p => p.name.toLowerCase())) : null, [own, ownEngine, engine]);
  // A greyed result is an earlier choice's; Stop keeps it with Try again.
  const [compareStopped, setCompareStopped] = useState("");
  const report = result?.report || null;
  const stale = !!result && result.choice !== choice;
  const needle = search.trim().toLowerCase();
  const base = useMemo(() => (report?.changes || []).filter(c => (!needle || c.name.toLowerCase().includes(needle)) &&
    (!onlySet || !setNames || setNames.has(c.name.toLowerCase()))), [report, needle, onlySet, setNames]);
  const counts = useMemo(() => kindCounts(base), [base]);
  const rows = useMemo(() => base.filter(c => matchesKind(c, kind)), [base, kind]);
  const setCount = setNames && report ? report.changes.filter(c => setNames.has(c.name.toLowerCase())).length : 0;

  // Parameter history.
  const [historyName, setHistoryName] = useState("");
  const historyChoice = historyName ? JSON.stringify([engine, historyName]) : "";
  const reading = useAction(historyChoice);
  const [history, setHistory] = useState<{ choice: string; report: ParameterHistory } | null>(null);
  async function runHistory(which: string) {
    if (!which) return;
    const [e, name] = JSON.parse(which) as [VersionEngine, string];
    const read = await reading.run(signal => parameterHistory(e, name, signal), { pending: `Reading the history of ${name}…`, done: "" });
    if (read.ok) setHistory({ choice: which, report: read.value });
  }
  useEffect(() => {
    if (onScreen && mode === "history" && historyChoice && history?.choice !== historyChoice) void runHistory(historyChoice);
  }, [onScreen, mode, historyChoice]);
  const hist = history?.report || null;
  // Python repeats the build-only sentence in notes; it shows once, under the summary.
  const histNotes = hist ? hist.notes.filter(n => n !== hist.build_only) : [];
  const [firstCataloged, newestCataloged] = useMemo(() => {
    const sorted = cat ? [...cat.versions].sort((a, b) => compareReleases(a.version, b.version)) : [];
    return [sorted[0]?.version || "", sorted[sorted.length - 1]?.version || ""];
  }, [cat]);
  const histStale = !!history && history.choice !== historyChoice;
  const [historyStopped, setHistoryStopped] = useState("");

  const focusCombo = () => window.requestAnimationFrame(() => combo.current?.focus());
  function chooseMode(next: Mode) {
    if (next === mode) return;
    if (next === "history") {
      inspector?.setOpen(false);
      // Prefill from the Parameters list's selection when the catalog knows it.
      if (!historyName && selectedName && cat?.names.includes(selectedName)) setHistoryName(selectedName);
    }
    setMode(next);
  }
  function showHistory(name: string) {
    inspector?.setOpen(false);
    setHistoryName(name); setMode("history"); focusCombo();
  }
  // Version history from the Parameters inspector.
  const handled = useRef(0);
  useEffect(() => {
    if (!request || request.n === handled.current) return;
    handled.current = request.n;
    if (request.engine && request.engine !== engine) {
      enginePicked.current = request.engine !== ownEngine; pairPicked.current = false; setPairState(null); setEngineState(request.engine);
    }
    showHistory(request.name);
  }, [request]);

  const modeSwitch = <Segmented label="Versions" className="pv-mode" value={mode} onChange={chooseMode}
    options={[{ value: "compare", label: "Compare versions" }, { value: "history", label: "Parameter history" }]} />;
  const engineField = <label className="toolbar-field pv-field"><span>Engine</span>
    <Select label="Engine" value={engine} onChange={setEngine}
      options={(catalog?.engines.length ? catalog.engines : [{ engine: "mysql", product: "MySQL" }, { engine: "mariadb", product: "MariaDB" }] as Pick<CatalogEngine, "engine" | "product">[])
        .map(e => [e.engine, e.product] as [string, string])} /></label>;

  // One line each: Percona uses the MySQL catalog; a server release the catalog lacks.
  const notes: ReactNode[] = [];
  if (percona && engine === "mysql") notes.push("Percona Server uses the MySQL catalog. Variables Percona Server adds are not cataloged.");
  if (cat && serverVersion) {
    const at = serverRelease(cat, serverVersion);
    const product = own?.server_product || cat.product;
    if (at.state === "newer") notes.push(`${product} ${at.version} on ${serverName} is newer than the catalog, whose newest release is ${at.newest}.`);
    // A skipped or missing release only matters for the From choice.
    else if (mode === "compare" && at.state === "skipped") notes.push(`${product} ${at.version} on ${serverName} is not in the catalog: ${sentence(at.reason)}${at.nearest ? ` From shows ${at.nearest}, the nearest earlier release.` : ""}`);
    else if (mode === "compare" && at.state === "missing") notes.push(`${product} ${at.version} on ${serverName} is not in the catalog.${at.nearest ? ` From shows ${at.nearest}, the nearest earlier release.` : ""}`);
  }
  const noteLines = notes.length > 0 && <div className="pv-notes">{notes.map((n, i) => <p key={i} role="note" title={typeof n === "string" ? n : undefined}>{n}</p>)}</div>;

  const valueCell = (c: VersionChange, side: "from" | "to") => <Value value={kindValue(shownKind(c.kinds), c[side])} />;
  // In a narrow pane (a narrow window, or the side inspector open) From and To
  // share one column, so both values stay in view without scrolling the table.
  const narrow = (inspector?.sheet ?? false) || (paneWidth > 0 && paneWidth < NARROW_PANE);
  const columns: Column<VersionChange>[] = report ? [
    { key: "name", label: "Parameter", primary: true, flex: !narrow, width: narrow ? 200 : undefined, minWidth: 140, sort: c => c.name, render: c => <code className="pv-name">{c.name}</code> },
    { key: "change", label: "Change", minWidth: 120, sort: c => CHANGE_KINDS.indexOf(c.kinds[0]), render: c => <KindBadges kinds={c.kinds} /> },
    ...(narrow ? [
      { key: "values", name: "From and To", label: `${report.from} → ${report.to}`, title: `In ${report.product} ${report.from}, then in ${report.to}`, flex: true, minWidth: 160,
        sort: (c: VersionChange) => kindValue(shownKind(c.kinds), c.to).text,
        render: (c: VersionChange) => <span className="pv-pair">{valueCell(c, "from")}<span className="pv-arrow" aria-hidden="true">→</span><span className="visually-hidden"> to </span>{valueCell(c, "to")}</span> },
    ] : [
      { key: "from", name: "From", label: report.from, title: `In ${report.product} ${report.from}`, sort: (c: VersionChange) => kindValue(shownKind(c.kinds), c.from).text, render: (c: VersionChange) => valueCell(c, "from") },
      { key: "to", name: "To", label: report.to, title: `In ${report.product} ${report.to}`, sort: (c: VersionChange) => kindValue(shownKind(c.kinds), c.to).text, render: (c: VersionChange) => valueCell(c, "to") },
      { key: "since", label: "Since", title: "The release on the To line from which its value holds; — when the From line made the difference (its details name the release)", sort: (c: VersionChange) => versionKey(c.since[shownKind(c.kinds)] || undefined),
        render: (c: VersionChange) => { const v = c.since[shownKind(c.kinds)]; return v ? <span title={c.kinds.filter(k => c.since[k]).map(k => `${KIND_LABELS[k]} since ${c.since[k]}`).join(" · ")}>{v}</span>
          : <span className="muted" title={`${report?.from ?? "From"} has a change ${report?.to ?? "To"} does not have, or both lines changed it. The details name the release.`}>—</span>; } },
    ]),
  ] : [];

  const row = report && !stale ? report.changes.find(c => c.name === selected) : undefined;
  const detail: InspectorContent | null = mode === "compare" && row && report ? {
    key: row.name, title: row.name, meta: <KindBadges kinds={row.kinds} all />,
    actions: <div className="pv-actions"><button type="button" className="button" data-action="show-full-history" onClick={() => showHistory(row.name)}>
      <Icon name="history" />Show full history</button></div>,
    body: <div className="pv-detail">
      {row.text.length > 0 && <ul className="pv-sentences">{row.text.map((t, i) => <li key={i}>{sentence(t)}</li>)}</ul>}
      {row.replacement && <p className="pv-replacement">Replacement <code>{row.replacement}</code>
        <button type="button" className="text-button" onClick={() => showHistory(row.replacement!)}>Its history</button></p>}
      <FactsTable label={`${row.name} in ${report.from} and ${report.to}`} from={report.from} to={report.to} rows={factRows(row.from, row.to)} />
    </div>,
  } : null;

  const generated = cat?.generated && typeof cat.generated.on === "string" ? cat.generated.on : "";
  const catalogMissing = !!catalog && (!cat?.available || !cat.versions.length);
  // A side inspector (a sheet when narrow) keeps the list beside the selected change.
  return <ViewLayout id="parameters-versions" title="Parameters" className="pv-view" controls={controls} onBack={onBack} detailBackLabel=""
    inspector={detail} inspectorEmpty={<p>{mode === "compare" ? "Select a change to see both releases." : "Select a change in Compare versions to see it here."}</p>}
    info={<><p>How parameters differ between MySQL or MariaDB releases, and every release where one parameter changed or was deprecated.</p>
      <p>Each release was started in a lab and read from the running server; the catalog ships with myflames{generated ? ` (built ${generated})` : ""}. Nothing here reads your server.</p></>}>
    <div className="pv-panel" data-mode={mode} ref={pane}>
      <div className="pv-setup" role="group" aria-label={mode === "compare" ? "Releases to compare" : "Parameter to trace"}>
        {modeSwitch}
        {engineField}
        {mode === "compare" && cat && cat.versions.length > 0 && pair && <span className="pv-releases">
          <ReleaseSelect label="From" value={pair.from} catalog={cat} onChange={v => setPair({ ...pair, from: v })} />
          <button type="button" className="button pv-swap" aria-label="Swap From and To" title="Swap From and To"
            onClick={() => setPair({ from: pair.to, to: pair.from })}><Icon name="compare" />Swap</button>
          <ReleaseSelect label="To" value={pair.to} catalog={cat} onChange={v => setPair({ ...pair, to: v })} />
        </span>}
        {mode === "history" && <ParameterCombobox names={cat?.names || []} value={historyName} inputRef={combo}
          onChoose={setHistoryName} />}
      </div>
      <ActionStatus state={catalogRead} onRetry={() => void loadCatalog()} />
      {noteLines}
      {mode === "compare" ? <>
        <ActionStatus state={comparing} onRetry={() => void runCompare(choice)} onStop={() => { comparing.cancel(); setCompareStopped(choice); }} />
        {choice && compareStopped === choice && comparing.phase === "idle" && <StoppedLine
          what={report ? "The table shows the earlier choice." : ""} onRetry={() => { setCompareStopped(""); void runCompare(choice); }} />}
        {catalogMissing && <EmptyState icon="configuration" title={`No ${ENGINE_NAMES[engine]} releases in the catalog`}>{cat?.notes[0] || "This copy of myflames has no catalog for this engine."}</EmptyState>}
        {report && !catalogMissing && <div className={`pv-result${stale ? " is-stale" : ""}`} aria-busy={comparing.phase === "pending" || undefined}>
          <SummaryLine notes={report.notes.length ? report.notes : undefined} notesTitle="Notes about this comparison">{report.summary}</SummaryLine>
          <div className="pv-filters">
            <Segmented label="Show changes" value={kind} onChange={setKind} className="pv-kind-filter"
              options={KIND_FILTERS.map(([value, label]) => ({ value, label, count: counts[value] }))} />
            <SearchField label="Filter parameters" value={search} onChange={setSearch} placeholder="Filter parameters" />
            {setNames && <Checkbox checked={onlySet} onChange={setOnlySet}
              title={`Parameters whose value on ${serverName} differs from the default (from its Parameters list)`}>Only parameters set on {serverName} ({setCount.toLocaleString()})</Checkbox>}
          </div>
          <DataTable id="parameters.versions" label={`Parameter changes from ${report.from} to ${report.to}`} columns={columns} rows={rows}
            rowKey={c => c.name} selection={{ key: selected, select: (k, open) => { setSelected(k); if (open !== false) inspector?.setOpen(true); } }}
            compact={narrow}
            empty={<p className="pv-empty">{report.changes.length ? "No changes match these filters." : report.direction === "same" ? "From and To are the same release." : "No parameter changed between these releases."}</p>} />
        </div>}
      </> : <>
        <ActionStatus state={reading} onRetry={() => void runHistory(historyChoice)} onStop={() => { reading.cancel(); setHistoryStopped(historyChoice); }} />
        {historyChoice && historyStopped === historyChoice && reading.phase === "idle" && <StoppedLine
          what={hist ? "The history shown is for the earlier choice." : ""} onRetry={() => { setHistoryStopped(""); void runHistory(historyChoice); }} />}
        {!historyName ? <EmptyState icon="history" title="Choose a parameter">See every release where it changed or was deprecated, including minor releases.</EmptyState>
          : hist && !catalogMissing && <div className={`pv-result${histStale ? " is-stale" : ""}`} aria-busy={reading.phase === "pending" || undefined}>
            <SummaryLine notes={histNotes.length ? histNotes : undefined} notesTitle="Notes about this history">{hist.summary}</SummaryLine>
            {hist.build_only && <p className="pv-build-only" role="note">{hist.build_only}</p>}
            {!hist.found ? <div className="pv-suggestions" role="group" aria-label="Close names">
              {hist.suggestions.length ? <><span>Close names</span>{hist.suggestions.map(s => <button key={s} type="button" className="button small"
                onClick={() => { setHistoryName(s); focusCombo(); }}><code>{s}</code></button>)}</>
                : <span>No close names in the {hist.product} catalog.</span>}
            </div> : <div className="pv-history">
              {hist.current && <section className="pv-latest" aria-labelledby="pv-latest-title">
                <h3 id="pv-latest-title">In {hist.latest}{hist.latest === newestCataloged ? ", the newest release" : ", its last release"}</h3>
                <Fields label={`${hist.name} in ${hist.latest}`} rows={[
                  ["First release", hist.first ? <span>{hist.first}{hist.first === firstCataloged && <span className="muted"> (the first cataloged)</span>}</span> : "Unknown"],
                  ...factRows(hist.current, hist.current).map((r): [ReactNode, ReactNode] => [r.label, <Value value={r.a} wrap />]),
                  hist.aliases.length > 0 && ["Also known as", <span>{hist.aliases.map((a, i) => <span key={a}>{i > 0 && ", "}<code>{a}</code></span>)}</span>],
                ]} />
                {hist.default_rule && <p className="pv-default-rule">How the server sets the default: {hist.default_rule}</p>}
              </section>}
              <section className="pv-changes" aria-labelledby="pv-changes-title">
                <h3 id="pv-changes-title">Changes</h3>
                <HistoryTable history={hist} />
              </section>
            </div>}
          </div>}
      </>}
    </div>
  </ViewLayout>;
}

/** After Stop: the read ended without an answer; Try again reads the current choice. */
function StoppedLine({ what, onRetry }: { what: string; onRetry: () => void }) {
  return <div className="action-status" data-phase="stopped">
    <span className="action-status-text" role="status" aria-live="polite">
      <span className="action-status-message">Stopped.{what ? ` ${what}` : ""}</span>
    </span>
    <button type="button" className="text-button" data-action="retry-versions" onClick={onRetry}>Try again</button>
  </div>;
}
