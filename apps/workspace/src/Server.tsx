// The Server, Workload and Tuning places: live views of the connection in use.
// Server uses a grouped sub-navigation (Overview, Contention, Schema, Setup,
// Security) and Workload uses Queries and Resources, with a status dot or count
// badge per view from data already loaded; Tuning renders full width without a
// list of views. Each view has a toolbar, one summary line, a table or grouped
// list, and an inspector for the selected row.
//
// Reports stay in memory while the workspace is open (switching views keeps
// them) and are never saved. The selected view, the controls, and whether the
// inspector is open are restored with the workspace (session.ts).
//
// Refresh all (place toolbar, ⌥⌘R) reads every Server section, including
// unopened views. Full analysis runs separately from the per-view reads so
// its own worker pool cannot multiply the concurrency limit. At most
// REFRESH_CONCURRENCY view reads run at once, the
// sampling views first, one run at a time, each view's read shared with its
// own Refresh (binding.ts sharedRead) and dropped when the connection changes
// or a newer read of the view started. Stop or Escape ends it; a view that
// failed keeps its previous report and is marked in the list of views.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  AnalysisFinding,
  AnalysisLink,
  AnalysisReport,
  AnalysisSource,
  Capture,
  ConfigReport,
  Connection,
  ConnectionPlan,
  ErrorLogReport,
  HealthReport,
  IndexesReport,
  InnodbReport,
  InstrumentsReport,
  IoReport,
  LocksReport,
  Plan,
  SessionsReport,
  TablesReport,
  TopOrder,
  TopReport,
  TopStatement,
  TopTable,
  WaitsReport,
} from "./api";
import { api } from "./api";
import { validateDiagram, rebuildDiagramLayout, type DiagramDocument, type SchemaDiagram } from "./schemaDiagramModel";
import type { AccountsReport } from "./accountsApi";
import {
  ColumnStoreContext,
  ViewActivityContext,
  ViewBackLabelContext,
  ViewSwitcherContext,
  type ViewSwitcherShared,
  useWindowStatus,
  NavMarker,
  countState,
  type BadgeTone,
  InfoPopover,
  InspectorContext,
  Popover,
  closePopovers,
  SourceList,
  StatusPill,
  TONE_TEXT,
  ToolbarButton,
  storeInspectorWidth,
  storedInspectorWidth,
  worstTone,
  type ColumnPrefs,
  type ColumnStore,
  type InspectorShared,
  type Selection,
  type SourceItem,
  type Tone,
} from "./kit";
import { Icon } from "./components";
import { serverKind, type ServerKind, type SlowState } from "./session";
import {
  connectionKey,
  connectionLabel,
  lastViewRead,
  latestGeneration,
  nextGeneration,
  readRunning,
  sharedRead,
  useConnectionReads,
  type Bound,
} from "./binding";
import { HealthView, emptyHealth, rememberHealth, type HealthMemory } from "./Health";
import { ErrorLogView, ERROR_LOG_READ, errorLogBody } from "./ErrorLog";
import { analysisSource as reportSource } from "./Report";
import {
  TopStatementsView,
  clearStatementsBehind,
  topRead,
  useTopStatements,
  type DigestAction,
  type LabSettings,
} from "./SlowQueries";
import { LocksView } from "./Locks";
import type { TableFocus } from "./Tables";
import { InstrumentsLinkContext, InstrumentsView } from "./Instruments";
import { MaintenanceView, emptyMaintenance, type MaintenanceMemory, type MaintenanceSection } from "./Maintenance";
import { ReplicationLoopBanner } from "./Replication";
import {
  jobActive,
  loopActive,
  readSkipLoops,
  stopSkipLoop,
  type LifecycleTools,
  type ReplicationReport,
  type SkipLoop,
} from "./opsApi";
import { ViewOnScreenContext } from "./viewOnScreen";
import { ALL_SERVER_VIEWS, SERVER_GROUPS, SERVER_VIEWS, WORKLOAD_GROUPS, WORKLOAD_KINDS, WORKLOAD_VIEWS } from "./serverViews";
import { lazyComponent, preloadWhenIdle } from "./lazy";
import "./server.css";

// Views that Server uses only as components load their chunk on first open
// (lazy.tsx); the rest share helpers with Server and come with it. Once the
// place is open, the others load in idle time, so switching views does not
// wait.
const InnodbView = lazyComponent(() => import("./InnoDB").then((m) => m.InnodbView));
const TuningView = lazyComponent(() => import("./Tuning").then((m) => m.TuningView));
const SessionsView = lazyComponent(() => import("./Sessions").then((m) => m.SessionsView));
const WaitsView = lazyComponent(() => import("./Waits").then((m) => m.WaitsView));
const IoView = lazyComponent(() => import("./IoStats").then((m) => m.IoView));
const IndexesView = lazyComponent(() => import("./Indexes").then((m) => m.IndexesView));
const TablesView = lazyComponent(() => import("./Tables").then((m) => m.TablesView));
const ConfigView = lazyComponent(() => import("./Config").then((m) => m.ConfigView));
const SchemaDiagramView = lazyComponent(() => import("./SchemaDiagram").then((m) => m.SchemaDiagramView));
const AccountsView = lazyComponent(() => import("./Accounts").then((m) => m.AccountsView));
const LAZY_VIEWS = [InnodbView, TuningView, SessionsView, WaitsView, IoView, IndexesView, TablesView, ConfigView, SchemaDiagramView, AccountsView];
/** Focuses the element with *id* after this task, or once it appears: a view
 * whose chunk is still loading mounts its heading a moment later (up to 2 s). */
function focusWhenMounted(id: string) {
  let tries = 0;
  const attempt = () => {
    const element = document.getElementById(id);
    if (element) element.focus();
    else if (tries++ < 40) window.setTimeout(attempt, 50);
  };
  window.setTimeout(attempt);
}

/** Names of what Refresh all reads that is not a view in a list: the Full
 * analysis lives in the Tuning place. */
const READ_LABELS: Partial<Record<ServerKind, string>> = { report: "Full analysis", tuning: "Tuning", replication: "Replication" };

/** Below this width the sub-navigation becomes a screen of its own. */
const COMPACT_WIDTH = 720;
/** Below this width the inspector is a bottom sheet instead of a pane. */
const SHEET_WIDTH = 1040;
/** Refresh all: view reads in flight at once (each one is a client process
 * and a server connection; the server may cap max_user_connections). */
export const REFRESH_CONCURRENCY = 3;
/** Views that sample over a window start first, so the total is the longest window. */
const WINDOWED: ServerKind[] = ["health", "waits", "io"];

type RefreshState = "queued" | "loading" | "ok" | "failed" | "stopped" | "replaced";
type RefreshItem = {
  view: ServerKind;
  label: string;
  state: RefreshState;
  error?: string;
  at?: string;
  /** The view's report when this read failed, stopped or was replaced: a newer
   * report (its own Refresh) means the view is no longer stale. */
  report?: unknown;
  /** A replaced read's answer: when the view shows it anyway (a joined read), it counts as refreshed. */
  result?: unknown;
};
/** One Refresh all: the connection it ran on and each view's outcome. */
type RefreshRun = {
  id: number;
  key: string;
  label: string;
  state: "running" | "done" | "stopped";
  reason?: string;
  started: string;
  views: RefreshItem[];
};
/** One view's read for Refresh all: the same parameters and request as its own Refresh. */
type RefreshTask = {
  view: ServerKind;
  label: string;
  params: string;
  request: (c: Connection, signal: AbortSignal) => Promise<unknown>;
  accept: (b: Bound<never>) => void | boolean;
};
const REFRESH_TEXT: Record<RefreshState, [Tone, string]> = {
  queued: ["info", "Waiting"],
  loading: ["info", "Reading"],
  ok: ["ok", "Refreshed"],
  failed: ["problem", "Failed"],
  stopped: ["unknown", "Not refreshed"],
  replaced: ["info", "Replaced"],
};
function ago(iso: string | null | undefined, now: number) {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  const age = seconds < 60 ? `${seconds} s ago` : seconds < 3600 ? `${Math.round(seconds / 60)} min ago` : `${Math.round(seconds / 3600)} h ago`;
  return `${new Date(then).toLocaleTimeString()}, ${age}`;
}

/** "schema.table" from a Full analysis link (the first dot splits). */
function tableFromText(text: string | undefined): TopTable | null {
  if (!text) return null;
  const at = text.indexOf(".");
  return at > 0 ? { schema: text.slice(0, at), table: text.slice(at + 1) } : null;
}

function toneOf(status: string | null | undefined): Tone {
  return status === "ok" || status === "watch" || status === "problem" || status === "info" || status === "unknown"
    ? status
    : "unknown";
}
// A sub-navigation marker (NavMarker): a dot for state, a neutral pill for a
// count; the item's hidden text (", Watch", ", 5 blocked") names it once.
function dot(tone: Tone, text: string): Marker {
  return { badge: <NavMarker tone={tone} />, badgeText: text, tone };
}
// A count draws a dot and the count only (a list row is narrow); the state
// word moves into the tooltip and the hidden text ("2 flagged, Watch").
function count(n: number, tone: BadgeTone, text: string): Marker {
  const state = countState(tone);
  const word = state ? TONE_TEXT[state] : "";
  return { badge: <NavMarker compact tone={state} count={n} countTone={tone} />, badgeText: word && !text.includes(word) ? `${text}, ${word}` : text, tone: state };
}
/** A list item's marker and the state it stands for (the floating title's
 * marker sums up the other views from it). */
type Marker = Pick<SourceItem, "badge" | "badgeText"> & { tone?: Tone };
/** With the list of views floated, the title says when another view needs
 * attention: the worst state among the others, and how many share it. */
function othersMarker(tones: (Tone | undefined)[]): ViewSwitcherShared["others"] {
  const problems = tones.filter((t) => t === "problem").length;
  const watches = tones.filter((t) => t === "watch").length;
  const views = (n: number) => `${n} other ${n === 1 ? "view" : "views"}`;
  if (problems) return { tone: "problem", text: `${views(problems)} ${problems === 1 ? "has" : "have"} a problem${watches ? `, ${watches} to watch` : ""}` };
  if (watches) return { tone: "watch", text: `${views(watches)} to watch` };
  return null;
}

export function Server({
  place = "server",
  onScreen = true,
  onNavigatePlace,
  picker,
  connection,
  disabled,
  settings,
  initial,
  onStateChange,
  onDigest,
  onShowPlan,
  onAddPlan,
  onOpenPlan,
  onBatchChange,
  onOpenLesson,
  onOpenSql,
  link,
  instrumentsLink,
  navigation,
  navCollapsed,
  onToggleNavCollapsed: toggleNavCollapsed,
  replicationLoops: propReplicationLoops,
  onReplicationLoops: propOnReplicationLoops,
  replication: propReplication,
  onReplication: propOnReplication,
}: {
  /** The place on screen (or shown last, while another place is): Server and
   * Workload list their views; Tuning shows its one view, full width. */
  place?: "server" | "workload" | "tuning";
  /** The place is on screen (not hidden behind another place). Views read on
   * open only while it is (viewOnScreen.ts). */
  onScreen?: boolean;
  onNavigatePlace?: (place: "server" | "workload" | "tuning" | "replication", view: ServerKind) => void;
  /** Command palette navigation: changes the visible view without reading it. */
  navigation?: { view: ServerKind; n: number };
  /** The list of views floats (opened from the view's title) instead of
   * docking beside the view; the shell owns this preference (main.tsx). */
  navCollapsed: boolean;
  onToggleNavCollapsed: () => void;
  picker: React.ReactNode;
  connection: Connection;
  /** The Query editor is capturing: reads and row actions wait. */
  disabled: boolean;
  settings: LabSettings;
  initial?: SlowState;
  onStateChange: (state: SlowState) => void;
  /** Open a Top statements row in the Query editor (and run it), only while
   * the connection in use is still *expect* (connectionKey). */
  onDigest: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  onShowPlan: (capture: Capture, name: string) => Promise<unknown>;
  onAddPlan: (capture: Capture, name: string, cancelled?: () => boolean) => Promise<Plan | undefined>;
  onOpenPlan: (id: string) => void;
  onBatchChange: (running: boolean) => void;
  /** Open a Learn lesson linked from Health. */
  onOpenLesson?: (lesson: string) => void;
  /** Put a statement (Parameters' SET SESSION) in the Query editor's current tab, without running it. */
  onOpenSql?: (sql: string) => void;
  /** A link from the Query editor's schema browser: open Indexes or Tables
   * on one table (n changes per click; from is the key it was read with). */
  link?: { view: "indexes" | "tables" | "health"; schema: string; table: string; from: string; n: number } | null;
  /** A link from the Replication place's "instrument is off" note: open
   * Instruments at that row (n changes per click). */
  instrumentsLink?: { row: string; n: number } | null;
  replicationLoops?: Bound<SkipLoop[]> | null;
  onReplicationLoops?: (loops: Bound<SkipLoop[]>) => void;
  /** The Replication place's report, owned by the shell: Refresh all reads
   * replication once and the place shows that answer. */
  replication?: Bound<ReplicationReport> | null;
  onReplication?: (report: Bound<ReplicationReport>) => void;
}) {
  // Tuning (with its Full analysis tab) is a place of its own, never a view
  // chosen inside Server: an earlier session that saved either as the Server
  // view opens Health there.
  const [serverSelectedKind, setServerSelectedKind] = useState<ServerKind>(() => {
    return initial?.kind && !WORKLOAD_KINDS.includes(initial.kind) && !["tuning", "report", "replication"].includes(initial.kind) ? initial.kind : "health";
  });
  const [workloadSelectedKind, setWorkloadSelectedKind] = useState<ServerKind>(() => {
    return initial?.kind && WORKLOAD_KINDS.includes(initial.kind) ? initial.kind : "top";
  });
  useEffect(() => preloadWhenIdle(LAZY_VIEWS), []);
  const tuningPlace = place === "tuning";
  const kind: ServerKind = place === "workload" ? workloadSelectedKind : tuningPlace ? "tuning" : serverSelectedKind;
  const kindRef = useRef(kind);
  kindRef.current = kind;
  // The Tuning place saves the Server view under it, so Server opens where it was.
  const savedKind = tuningPlace ? serverSelectedKind : kind;
  const [order, setOrder] = useState(initial?.order || "total");
  const [minSeconds, setMinSeconds] = useState(initial?.min_seconds || 0);
  const [healthInterval, setHealthInterval] = useState(initial?.health_interval || 5);
  // The schema in focus for I/O and Top statements, and the Waits and I/O
  // windows, are restored with the workspace; reports are not.
  const [schema, setSchema] = useState(initial?.schema ?? "");
  const [waitsInterval, setWaitsInterval] = useState(initial?.waits_interval || 10);
  const [ioInterval, setIoInterval] = useState(initial?.io_interval || 10);
  const [indexesSchemas, setIndexesSchemas] = useState<string[]>(initial?.indexes_schemas || []);
  const [tablesSchemas, setTablesSchemas] = useState<string[]>(initial?.tables_schemas || []);
  const [sessionsScope, setSessionsScope] = useState<"active" | "all" | "locked">(initial?.sessions_scope || "active");
  /** Maintenance's open task section, restored with the workspace. */
  const [maintenanceSection, setMaintenanceSection] = useState<MaintenanceSection>(initial?.maintenance_section || "tools");
  const activeKey = connectionKey(connection);
  const [localReplicationLoops, setLocalReplicationLoops] = useState<Bound<SkipLoop[]> | null>(null);
  const effectiveLoops = propReplicationLoops !== undefined ? propReplicationLoops : localReplicationLoops;
  const loopsHere = effectiveLoops?.key === activeKey ? effectiveLoops.report : [];
  const loopActiveNow = loopsHere.some(loopActive);
  const [inspectorOpen, setInspectorOpen] = useState(initial?.inspector === true);
  /** A Full analysis ran (restored; the report itself never is). */
  const [reportRun, setReportRun] = useState(initial?.report_run === true);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
  /** The floating list of views is open (under the view's title, kit.tsx ViewSwitcher). */
  // Held as the view it was opened on: navigation from elsewhere (the command
  // palette, a link) never carries an open list onto another view.
  const [floatingMenuFor, setFloatingMenuFor] = useState<ServerKind | null>(null);
  const setFloatingMenuOpen = useCallback((open: boolean) => setFloatingMenuFor(open ? kindRef.current : null), []);
  /** A view chosen from the floating list: its title button takes focus once it is on screen. */
  const switcherFocus = useRef(false);
  const takeSwitcherFocus = useCallback(() => {
    const take = switcherFocus.current;
    switcherFocus.current = false;
    return take;
  }, []);
  // Column widths and hidden columns of every Server table, by table id,
  // restored with the workspace (session.ts validates them).
  const [columns, setColumns] = useState<Record<string, ColumnPrefs>>(initial?.columns_v1 || {});
  const columnStore: ColumnStore = useMemo(
    () => ({
      get: (table) => columns[table],
      set: (table, prefs) =>
        setColumns((all) => {
          const next = { ...all };
          if (prefs) next[table] = prefs;
          else delete next[table];
          return next;
        }),
    }),
    [columns],
  );
  const reportRef = useRef(onStateChange);
  reportRef.current = onStateChange;
  const latestConnection = useRef(connection);
  latestConnection.current = connection;
  const disabledRef = useRef(disabled);
  disabledRef.current = disabled;
  useEffect(() => {
    reportRef.current?.({
      kind: savedKind,
      order,
      min_seconds: minSeconds,
      health_interval: healthInterval,
      schema,
      waits_interval: waitsInterval,
      io_interval: ioInterval,
      ...(indexesSchemas.length ? { indexes_schemas: indexesSchemas } : {}),
      ...(tablesSchemas.length ? { tables_schemas: tablesSchemas } : {}),
      inspector: inspectorOpen,
      sessions_scope: sessionsScope,
      maintenance_section: maintenanceSection,
      ...(loopActiveNow ? { replication_loop_active: true } : {}),
      ...(reportRun ? { report_run: true } : {}),
      ...(Object.keys(columns).length ? { columns_v1: columns } : {}),
    });
  }, [savedKind, order, minSeconds, healthInterval, schema, waitsInterval, ioInterval, indexesSchemas, tablesSchemas, inspectorOpen, sessionsScope, maintenanceSection, loopActiveNow, reportRun, columns]);

  // Reports, kept while the place is mounted (never saved). Each one carries
  // the key of the connection it was read from (binding.ts): a report from
  // another connection stays on screen, greyed, with its actions off.
  const [health, setHealth] = useState<HealthMemory>(emptyHealth);
  const [innodb, setInnodb] = useState<Bound<InnodbReport> | null>(null);
  const [errorlog, setErrorlog] = useState<Bound<ErrorLogReport> | null>(null);
  const [sessions, setSessions] = useState<Bound<SessionsReport> | null>(null);
  const [waits, setWaits] = useState<Bound<WaitsReport> | null>(null);
  const [io, setIo] = useState<Bound<IoReport> | null>(null);
  const [locks, setLocks] = useState<Bound<LocksReport> | null>(null);
  const [indexes, setIndexes] = useState<Bound<IndexesReport> | null>(null);
  const [tables, setTables] = useState<Bound<TablesReport> | null>(null);
  const [diagram, setDiagram] = useState<DiagramDocument | null>(null);
  const latestDiagram = useRef(diagram);
  latestDiagram.current = diagram;
  // Keep an unbuilt diagram's explicit database selection available to the
  // global refresh, across view switches, but never across connections.
  const [diagramSelection, setDiagramSelection] = useState({ key: activeKey, defaultDatabase: connection.database || "", database: connection.database || "" });
  const diagramDatabase = diagramSelection.key === activeKey && diagramSelection.defaultDatabase === (connection.database || "")
    ? diagramSelection.database : connection.database || "";
  const chooseDiagramDatabase = useCallback((key: string, database: string) => {
    if (key !== connectionKey(latestConnection.current)) return;
    setDiagramSelection({ key, defaultDatabase: latestConnection.current.database || "", database });
  }, []);
  const [config, setConfig] = useState<Bound<ConfigReport> | null>(null);
  const [accounts, setAccounts] = useState<Bound<AccountsReport> | null>(null);
  const [instruments, setInstruments] = useState<Bound<InstrumentsReport> | null>(null);
  const [localReplication, setLocalReplication] = useState<Bound<ReplicationReport> | null>(null);
  const replication = propReplication !== undefined ? propReplication : localReplication;
  const setReplication = useCallback(
    (report: Bound<ReplicationReport>) => {
      setLocalReplication(report);
      propOnReplication?.(report);
    },
    [propOnReplication],
  );
  const setReplicationLoops = useCallback(
    (loops: Bound<SkipLoop[]>) => {
      setLocalReplicationLoops(loops);
      propOnReplicationLoops?.(loops);
    },
    [propOnReplicationLoops],
  );
  /** Maintenance memory is ephemeral: reports, plans, jobs and donor secrets
   * never reach the session (session.ts keeps the section and the loop flag). */
  const [maintenance, setMaintenance] = useState<MaintenanceMemory>(emptyMaintenance);
  const [analysis, setAnalysis] = useState<Bound<AnalysisReport> | null>(null);
  const [analysisSource, setAnalysisSource] = useState<AnalysisSource | null>(null);
  // Full analysis reads live here, not in its view, so a run survives switching views.
  const analysisReads = useConnectionReads(connection);
  const topStatements = useTopStatements({ connection, settings, order, onAddPlan, onBatchChange });
  const [activity, setActivity] = useState<{ key: string; views: Record<string, boolean> }>({ key: activeKey, views: {} });
  const reportActivity = useCallback((view: string, loading: boolean) => {
    setActivity((old) => {
      if (old.key !== activeKey) return loading ? { key: activeKey, views: { [view]: true } } : old;
      if (!!old.views[view] === loading) return old;
      return { key: activeKey, views: { ...old.views, [view]: loading } };
    });
  }, [activeKey]);
  // Timestamp only accepted report objects. Failed reads retain the previous
  // timestamp, and switching connections never relabels another server's data.
  const received = useRef(new WeakMap<object, number>());
  const [lastRead, setLastRead] = useState<{ key: string; at?: number; engineVersion?: string }>({ key: activeKey });
  useEffect(() => {
    const reports = [health.report ? { key: health.key, report: health.report } : null,
      innodb, errorlog, sessions, waits, io, locks, indexes, tables, config, instruments, replication,
      maintenance.tools, maintenance.upgrade, analysis, topStatements.top];
    let at: number | undefined;
    let engineVersion: string | undefined;
    let versionAt = -1;
    for (const bound of reports) {
      if (!bound || bound.key !== activeKey) continue;
      let seen = received.current.get(bound.report);
      if (seen === undefined) {
        seen = bound === topStatements.top ? topStatements.refreshedAt ?? Date.now() : Date.now();
        received.current.set(bound.report, seen);
      }
      at = Math.max(at ?? seen, seen);
      const report = bound.report;
      if ('server_version' in report && report.server_version && seen >= versionAt) {
        const engine = 'engine' in report ? report.engine : null;
        engineVersion = `${engine === "mariadb" ? "MariaDB" : engine === "mysql" ? "MySQL" : "Server"} ${report.server_version}`;
        versionAt = seen;
      }
    }
    setLastRead((old) => old.key === activeKey && old.at === at && old.engineVersion === engineVersion
      ? old : { key: activeKey, at, engineVersion });
  }, [activeKey, health.report, health.key, innodb, errorlog, sessions, waits, io, locks, indexes, tables,
    config, instruments, replication, maintenance.tools, maintenance.upgrade, analysis, topStatements.top, topStatements.refreshedAt]);

  // Links between views.
  const [instrumentsTarget, setInstrumentsTarget] = useState<{ row: string; n: number } | null>(null);
  const [indexesRequest, setIndexesRequest] = useState(0);
  const [tablesRequest, setTablesRequest] = useState(0);
  const [indexesFocus, setIndexesFocus] = useState<TableFocus>(null);
  const [tablesFocus, setTablesFocus] = useState<TableFocus>(null);
  // The connection each view's report was read from ("" before the first read).
  const reportKeys: Record<ServerKind, string> = {
    health: health.report ? health.key : "",
    innodb: innodb?.key || "",
    errorlog: errorlog?.key || "",
    tuning: analysis?.key || (health.report ? health.key : ""),
    report: analysis?.key || "",
    top: topStatements.top?.key || "",
    sessions: sessions?.key || "",
    replication: replication?.key || "",
    waits: waits?.key || "",
    io: io?.key || "",
    locks: locks?.key || "",
    indexes: indexes?.key || "",
    tables: tables?.key || "",
    diagram: diagram?.source.key || (diagram ? "import" : ""),
    config: config?.key || "",
    instruments: instruments?.key || "",
    accounts: accounts?.key || "",
    maintenance:
      (maintenanceSection === "upgrade" ? maintenance.upgrade?.key : maintenance.tools?.key) || "",
  };
  // The selected row of each view (kept while switching views), bound to the
  // report it was chosen in: once the view holds a report from another
  // connection, the selection no longer applies.
  const [selected, setSelected] = useState<Partial<Record<ServerKind, { key: string; report: string }>>>({});
  const selection = (view: ServerKind): Selection => {
    const chosen = selected[view];
    const report = reportKeys[view];
    return {
      key: chosen && chosen.report === report ? chosen.key : "",
      select: (key, open) => {
        setSelected((s) => (s[view]?.key === key && s[view]?.report === report ? s : { ...s, [view]: { key, report } }));
        if (open) setInspectorOpen(true);
      },
    };
  };

  // Layout: three panes when wide, a sheet inspector below SHEET_WIDTH, and a
  // pushed list (the views list first, then the view) below COMPACT_WIDTH.
  const placeRef = useRef<HTMLElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = placeRef.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const compact = width > 0 && width < COMPACT_WIDTH;
  // The list floats in a wide window only; the narrow layout keeps its own
  // pushed list (Back), and Tuning has no list of views.
  const floating = !tuningPlace && navCollapsed && !compact;
  const floatingNavOpen = floating && onScreen && floatingMenuFor === kind;
  useEffect(() => {
    if (!floating || !onScreen || floatingMenuFor !== kind) setFloatingMenuFor(null);
  }, [floating, onScreen, kind]);
  const sheet = width > 0 && width < SHEET_WIDTH;
  const [pushed, setPushed] = useState(false);
  const showList = compact && !pushed;
  // A wide window always shows a view beside the list: when it narrows to the
  // pushed layout, that view stays on screen (Back shows the list).
  const wide = width > 0 && !compact;
  useEffect(() => {
    if (wide) setPushed(true);
  }, [wide]);

  const inspector: InspectorShared = useMemo(
    () => ({
      open: inspectorOpen,
      setOpen: setInspectorOpen,
      width: inspectorWidth,
      setWidth: (w: number) => {
        setInspectorWidth(w);
        storeInspectorWidth(w);
      },
      sheet,
    }),
    [inspectorOpen, inspectorWidth, sheet],
  );
  // ⌥⌘I (Alt+Ctrl+I elsewhere) shows or hides the inspector while Server is on screen.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.altKey && (e.metaKey || e.ctrlKey) && e.code === "KeyI")) return;
      if (!placeRef.current || placeRef.current.offsetParent === null) return;
      e.preventDefault();
      setInspectorOpen((o) => !o);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /** Choose a view from the list (narrow layouts push it on screen). Tuning
   * has one home, its own place: a route to it goes there. */
  function choose(view: ServerKind) {
    if (view === "replication") {
      onNavigatePlace?.("replication", "replication");
      return;
    }
    if (view === "tuning" || view === "report") {
      // The Full analysis is the Tuning place's second tab.
      if (view === "report") setFullAnalysisRequest((n) => n + 1);
      if (!tuningPlace) onNavigatePlace?.("tuning", "tuning");
      return;
    }
    if (WORKLOAD_KINDS.includes(view)) {
      setWorkloadSelectedKind(view);
      if (place !== "workload") onNavigatePlace?.("workload", view);
    } else {
      setServerSelectedKind(view);
      if (place !== "server") onNavigatePlace?.("server", view);
    }
    keepMounted(view);
    // Once a view is chosen it stays on screen if the window narrows.
    setPushed(true);
    if (compact) focusWhenMounted(`${view}-title`);
  }
  const keepInspectorOnNavigation = useRef(false);
  useEffect(() => {
    if (!navigation) return;
    // Views effects run first: a link that revealed a row already opened its inspector.
    if (keepInspectorOnNavigation.current) keepInspectorOnNavigation.current = false;
    else setInspectorOpen(false);
    choose(navigation.view);
  }, [navigation?.n]);
  // A link to the Full analysis opens the Tuning place on that tab; each new
  // request (n) switches to it again.
  const [fullAnalysisRequest, setFullAnalysisRequest] = useState(0);
  const tuningTab = useMemo(
    () => (fullAnalysisRequest ? { tab: "analysis" as const, n: fullAnalysisRequest } : null),
    [fullAnalysisRequest],
  );
  /** These views run operations (reviewed SQL, a skip loop, a dump) that
   * must survive a view switch, so each is mounted on first visit and kept
   * mounted, hidden, after that. */
  // Replication is a place of its own (ReplicationPlace, mounted by the
  // shell, which also mounts it at once for a restored skip loop so its
  // status read finds a loop still running in this myflames, or says it
  // stopped when myflames restarted; it never starts one): Server keeps no
  // hidden copy of it. Its report and loops are shared through the
  // shell, so Refresh all and the loop banner below still cover it.
  const [keptViews, setKeptViews] = useState<ServerKind[]>(() => {
    const list: ServerKind[] = [];
    if (initial?.kind && ["maintenance", "tables", "instruments"].includes(initial.kind)) list.push(initial.kind);
    return list;
  });
  function keepMounted(view: ServerKind) {
    if (["maintenance", "tables", "instruments"].includes(view))
      setKeptViews((list) => (list.includes(view) ? list : [...list, view]));
  }
  /** A link from a report may read with that report's schema or table only
   * while the report is from the connection in use (*from* is its key). */
  function linkFromHere(from: string) {
    return from === connectionKey(latestConnection.current);
  }
  /** Switch views from a link and move focus to it. A link that carries a
   * schema reads with it, so it needs the key of the report it came from. */
  function openView(view: ServerKind | string, linkSchema?: string, from?: string) {
    if (linkSchema !== undefined && from !== undefined && !linkFromHere(from)) return;
    const target = serverKind(view);
    choose(target);
    keepMounted(target);
    setPushed(true);
    if (target === "top" && !disabledRef.current && (!topStatements.top || linkSchema !== undefined)) {
      void topStatements.load(linkSchema !== undefined ? linkSchema : schema);
    }
    if (linkSchema !== undefined) {
      setSchema(linkSchema);
      if (target === "indexes") {
        setIndexesSchemas(linkSchema ? [linkSchema] : []);
        setIndexesFocus(null);
        setIndexesRequest((n) => n + 1);
      }
    }
    window.setTimeout(() => {
      const nav = document.getElementById(`server-nav-${target}`);
      if (nav && nav.offsetParent !== null) nav.focus({ preventScroll: true });
      else focusWhenMounted(`${target}-title`);
    });
  }
  const returningToViews = useRef(false);
  useEffect(() => {
    if (showList && returningToViews.current) {
      returningToViews.current = false;
      document.getElementById(`server-nav-${kind}`)?.focus();
    }
  }, [showList, kind]);
  function back() {
    returningToViews.current = true;
    setPushed(false);
  }
  /** Open Instruments at one row (from another view's "instrument is off" note). */
  function openInstruments(row: string) {
    setInstrumentsTarget((t) => ({ row, n: (t?.n || 0) + 1 }));
    // The destination detail owns focus. A delayed navigation focus would
    // steal it after Instruments has revealed the requested setting.
    setServerSelectedKind("instruments");
    if (place !== "server") {
      // The place switch arrives as a navigation, which closes the inspector; the row
      // revealed by this link must stay open.
      keepInspectorOnNavigation.current = true;
      onNavigatePlace?.("server", "instruments");
    }
    keepMounted("instruments");
    setPushed(true);
  }
  /** Indexes and Tables link to each other for one table, from a report of
   * the connection in use (*from*) only. */
  function openTable(view: "indexes" | "tables", linkSchema: string, table: string, from: string) {
    if (!linkFromHere(from)) return;
    if (view === "indexes") {
      setIndexesSchemas([linkSchema]);
      setIndexesFocus({ schema: linkSchema, table });
      setIndexesRequest((n) => n + 1);
    } else {
      setTablesSchemas([linkSchema]);
      setTablesFocus({ schema: linkSchema, table });
      setTablesRequest((n) => n + 1);
    }
    openView(view);
  }
  // The Replication place's "How to turn it on" link: Instruments at that row.
  const instrumentsLinkN = instrumentsLink?.n;
  useEffect(() => {
    if (instrumentsLink) openInstruments(instrumentsLink.row);
    // Once per click (n).
  }, [instrumentsLinkN]);
  // The schema browser's Open in Indexes / Open in Tables, for the connection in use only.
  const linkN = link?.n;
  useEffect(() => {
    // Home's Server Health opens Health; the schema browser opens a table.
    if (link) { if (link.view === "health") openView("health"); else openTable(link.view, link.schema, link.table, link.from); }
    // Once per click (n).
  }, [linkN]);
  /** Open the plan of a connection's running statement (Sessions and Locks), on
   * the connection the report was read from. */
  async function explainConnection(id: number, from: Connection) {
    if (connectionKey(from) !== connectionKey(latestConnection.current))
      throw new Error("The connection in use changed. Nothing was read; refresh to load this server.");
    const plan = await api<ConnectionPlan>("live/explain-connection", { connection: from, connection_id: id });
    const label = plan.mode === "explain" ? "estimated plan" : "partial analyze";
    await onShowPlan(plan.capture, `Connection ${id} · ${label}`);
  }
  /** Top statements behind a number (Health, Indexes, the Full analysis): an
   * attribution order and, for a table, the "Table: schema.table" filter. The
   * schema filter is cleared, since a table can be named from any default schema. */
  function openTop(nextOrder: TopOrder, table: TopTable | null, from: string) {
    // It reads Top statements at once: not while the Query editor is capturing.
    if (!linkFromHere(from) || disabledRef.current) return;
    setOrder(nextOrder);
    setSchema("");
    openView("top");
    void topStatements.load("", { order: nextOrder, table });
  }
  /** A Full analysis link: the view it names, with its row selected when that
   * view holds a report for the connection in use. */
  function openLink(link: AnalysisLink, finding: AnalysisFinding, from: string) {
    if (link.view === "top") {
      openTop((link.order as TopOrder) || "total", tableFromText(link.table), from);
      return;
    }
    const target = serverKind(link.view);
    let row = "";
    if (link.key && target === "config") row = config?.report.checks.find(c => c.key === link.key)?.settings[0]?.name || "";
    else if (link.key && target === "innodb") row = `section:${link.key}`;
    else if (link.key && target === "health" && finding.source?.view === "health")
      row = `${finding.source.kind === "diagnostic" ? "diag" : "metric"}:${link.key}`;
    const report = reportKeys[target];
    if (row && report && report === connectionKey(latestConnection.current)) {
      setSelected((s) => ({ ...s, [target]: { key: row, report } }));
      setInspectorOpen(true);
      // From the Tuning place (the Full analysis) the view is in another place: that
      // navigation closes the inspector, but this link's revealed row must stay open.
      const crosses = WORKLOAD_KINDS.includes(target) ? place !== "workload" : place !== "server";
      if (crosses) keepInspectorOnNavigation.current = true;
    }
    openView(target);
  }

  // ---- Refresh all ----------------------------------------------------------
  const [refresh, setRefresh] = useState<RefreshRun | null>(null);
  const refreshControl = useRef<{ id: number; controller: AbortController } | null>(null);
  const refreshSeq = useRef(0);
  const [clock, setClock] = useState(0);
  /** The report object each view shows now (identity, never compared by value). */
  const reportOf = (view: ServerKind): unknown =>
    ({
      health: health.report,
      innodb: innodb?.report,
      errorlog: errorlog?.report,
      tuning: analysis?.report,
      report: analysis?.report,
      top: topStatements.top?.report,
      sessions: sessions?.report,
      replication: replication?.report,
      waits: waits?.report,
      io: io?.report,
      locks: locks?.report,
      indexes: indexes?.report,
      tables: tables?.report,
      diagram: diagram?.diagram,
      config: config?.report,
      instruments: instruments?.report,
      accounts: accounts?.report,
      maintenance: maintenanceSection === "upgrade" ? maintenance.upgrade?.report : maintenance.tools?.report,
    })[view] ?? null;
  const reportOfRef = useRef(reportOf);
  reportOfRef.current = reportOf;
  /** Read every section with its current settings, whether or not it has
   * been opened. Imported diagrams remain local documents and are never replaced. */
  function refreshTasks(): RefreshTask[] {
    const key = connectionKey(latestConnection.current);
    const live = <T,>(b: Bound<T> | null) => (b && b.key === key ? b.report : null);
    const tasks: RefreshTask[] = [];
    const add = (view: ServerKind, params: string, path: string, body: (c: Connection) => object, accept: (b: Bound<never>) => void | boolean) => {
      // The view's own read still on its way is joined, never replaced.
      const own = lastViewRead(view, key);
      const joined = own && readRunning(key, own.params) ? own : null;
      tasks.push({
        view,
        label: ALL_SERVER_VIEWS.find((v) => v.id === view)?.label || READ_LABELS[view] || view,
        params: joined ? joined.params : params,
        request: joined ? joined.request : (c, signal) => api(path, body(c), signal),
        accept,
      });
    };
    // Each read matches the view's own Refresh (its load() in the view's file).
    // Settings a view keeps to itself (Locks' Waits only, Tables' Fresh
    // numbers) come from the last read that view asked for.
    const lastFlag = (view: ServerKind, fallback: boolean) => {
      const own = lastViewRead(view, key);
      return own ? own.params.endsWith(":true") : fallback;
    };
    add("health", `health:${healthInterval}`, "live/health", (c) => ({ connection: c, interval: healthInterval }), (b) =>
        setHealth((previous) => rememberHealth(previous, b as Bound<HealthReport>)),
      );
    add("waits", `waits:${waitsInterval}`, "live/waits", (c) => ({ connection: c, interval: waitsInterval }), (b) => setWaits(b as Bound<WaitsReport>));
    {
      const target = schema.trim();
      add("io", `io:${ioInterval}:${target}`, "live/io", (c) => (target ? { connection: c, interval: ioInterval, schema: target } : { connection: c, interval: ioInterval }), (b) =>
        setIo(b as Bound<IoReport>),
      );
    }
    // InnoDB's Refresh reads without the raw text (Show raw output asks for it).
    add("innodb", "innodb:false", "live/innodb", (c) => ({ connection: c, raw: false }), (b) => setInnodb(b as Bound<InnodbReport>));
    add("errorlog", ERROR_LOG_READ, "live/error-log", errorLogBody, (b) => setErrorlog(b as Bound<ErrorLogReport>));
    {
      const read = topRead(order, schema, topStatements.table);
      const target = schema.trim();
      add("top", read.params, "live/top", read.body, (b) => topStatements.accept(b as Bound<TopReport>, target));
    }
    {
      // The same parameters and key as the view's own read (Sessions.tsx
      // load()): the source it last asked for, SHOW FULL PROCESSLIST by
      // default, so a running read is joined and the report matches its picker.
      const activeOnly = sessionsScope === "active";
      const lockedOnly = sessionsScope === "locked";
      const lastSource = lastViewRead("sessions", key)?.params.split(":")[3];
      const source = lastSource === "auto" ? "auto" : "show_processlist";
      add("sessions", `sessions:${activeOnly}:${minSeconds}:${source}:${lockedOnly}`, "live/sessions", (c) => ({ connection: c, active_only: activeOnly, min_seconds: minSeconds, source_mode: source, locked_only: lockedOnly }), (b) =>
        setSessions(b as Bound<SessionsReport>),
      );
    }
    const locksReport = live(locks);
    {
      const waitsOnly = lastFlag("locks", locksReport?.scope === "waits");
      add("locks", `locks:${waitsOnly}`, "live/locks", (c) => ({ connection: c, limit: 500, waits_only: waitsOnly }), (b) => setLocks(b as Bound<LocksReport>));
    }
    {
      const scope = `indexes:${JSON.stringify(indexesSchemas)}`;
      const allSchemas = lastViewRead("indexes", key)?.params === `${scope}:all`;
      add("indexes", `${scope}${allSchemas ? ":all" : ""}`, "live/indexes", (c) => ({ connection: c, ...(indexesSchemas.length ? { schemas: indexesSchemas } : {}), ...(allSchemas ? { all_schemas: true } : {}) }), (b) =>
        setIndexes(b as Bound<IndexesReport>),
      );
    }
    const tablesReport = live(tables);
    {
      const own = lastViewRead("tables", key);
      const scope = `tables:${JSON.stringify(tablesSchemas)}:`;
      // Consent belongs to the last explicitly requested scope, never to a
      // newly chosen database list. Keep Fresh numbers ahead of the :all suffix.
      const sameScope = own?.params.startsWith(scope);
      const allSchemas = !!sameScope && own!.params.endsWith(":all");
      const fresh = (sameScope ? own!.params.slice(scope.length).split(":")[0] === "true" : !!tablesReport?.statistics?.fresh_requested) && tablesReport?.engine !== "mariadb";
      add("tables", `${scope}${fresh}${allSchemas ? ":all" : ""}`, "live/tables", (c) => ({ connection: c, ...(tablesSchemas.length ? { schemas: tablesSchemas } : {}), fresh, ...(allSchemas ? { all_schemas: true } : {}) }), (b) =>
        setTables(b as Bound<TablesReport>),
      );
    }
    // A changed database selection requires the diagram's own replacement
    // review. Refresh only the existing document's source, or the explicit
    // picker/default database when no document has been built yet.
    const database = diagram?.source.kind === "server" && diagram.source.key === key
      ? diagram.source.database : !diagram ? diagramDatabase.trim() : "";
    if (database) {
      add("diagram", `diagram:${JSON.stringify(database)}`, "schema-diagram/read", c => ({ connection: c, database }), b => {
        const report = validateDiagram((b as Bound<SchemaDiagram>).report);
        const sameSource = (doc: DiagramDocument | null) => !doc ||
          (doc.source.kind === "server" && doc.source.key === key && doc.source.database === database);
        // Layout edits do not invalidate fresh metadata. Use the latest
        // positions, but never overwrite an import or another database.
        if (!sameSource(latestDiagram.current)) return false;
        setDiagram(old => sameSource(old) ? {
          diagram: rebuildDiagramLayout(report, old?.diagram),
          source: { kind: "server", key, label: b.label, database },
        } : old);
      });
    }
    add("accounts", "accounts", "accounts/list", (c) => ({ connection: c }), (b) => setAccounts(b as Bound<AccountsReport>));
    add("config", "config", "live/config", (c) => ({ connection: c }), (b) => setConfig(b as Bound<ConfigReport>));
    add("instruments", "instruments", "live/instruments", (c) => ({ connection: c }), (b) => setInstruments(b as Bound<InstrumentsReport>));
    // Replication status and detected maintenance tools refresh like the other
    // views; loop starts, preflights, jobs and the upgrade check never run in
    // Refresh all.
    add("replication", "replication", "live/replication", (c) => ({ connection: c }), (b) => setReplication(b as Bound<ReplicationReport>));
    add("maintenance", "maintenance-tools", "lifecycle/tools", (c) => ({ connection: c }), (b) =>
        setMaintenance((m) => ({ ...m, tools: b as Bound<LifecycleTools> })),
      );
    add("report", "report", "live/report", c => ({ connection: c }), b => {
      setAnalysis(b as Bound<AnalysisReport>);
      setAnalysisSource(reportSource(latestConnection.current));
      setReportRun(true);
    });
    const order_ = (v: ServerKind) => (WINDOWED.includes(v) ? 0 : 1);
    // Existing individual reads occupy worker slots too. Join them before
    // starting new requests, otherwise a held Locks read plus three sampling
    // reads would exceed the global budget.
    return tasks.sort((a, b) => Number(readRunning(key, b.params)) - Number(readRunning(key, a.params)) || order_(a.view) - order_(b.view));
  }
  /** Refresh every Server section of the connection in use; a second call while one runs does nothing. */
  async function refreshAll() {
    if (refreshControl.current || disabled) return;
    const c = latestConnection.current;
    const key = connectionKey(c);
    const label = connectionLabel(c);
    const tasks = refreshTasks();
    if (!tasks.length) return;
    const id = ++refreshSeq.current;
    const controller = new AbortController();
    refreshControl.current = { id, controller };
    setRefresh({
      id,
      key,
      label,
      state: "running",
      started: new Date().toISOString(),
      views: tasks.map((t) => ({ view: t.view, label: t.label, state: "queued" })),
    });
    const update = (view: ServerKind, patch: Partial<RefreshItem>) =>
      setRefresh((r) => (r && r.id === id ? { ...r, views: r.views.map((v) => (v.view === view ? { ...v, ...patch } : v)) } : r));
    const queue = tasks.filter(task => task.view !== "report");
    const reportTask = tasks.find(task => task.view === "report");
    const worker = async (queue: RefreshTask[]) => {
      for (let task = queue.shift(); task && !controller.signal.aborted; task = queue.shift()) {
        update(task.view, { state: "loading" });
        // A newer read of this view (its own Refresh) wins over this one.
        const generation = nextGeneration(task.view);
        const current = task;
        try {
          const result = await sharedRead(key, current.params, (signal) => current.request(c, signal), { signal: controller.signal });
          if (controller.signal.aborted) throw new DOMException("Stopped", "AbortError");
          // Refreshed only when shown: a newer read of the view (or another
          // connection) replaced this one, so its answer is dropped.
          if (connectionKey(latestConnection.current) === key && latestGeneration(current.view) === generation) {
            const accepted = current.accept({ key, label, report: result as never });
            update(current.view, accepted === false
              ? { state: "replaced", result, report: reportOfRef.current(current.view) }
              : { state: "ok", at: new Date().toISOString(), error: undefined });
          } else update(current.view, { state: "replaced", result, report: reportOfRef.current(current.view) });
        } catch (e) {
          const stopped = controller.signal.aborted || (e as Error).name === "AbortError";
          const report = reportOfRef.current(current.view);
          update(current.view, stopped ? { state: "stopped", report } : { state: "failed", error: (e as Error).message, report });
        }
      }
    };
    // The report endpoint has its own bounded worker pool. Finish an existing
    // analysis before starting view reads; otherwise run it after they finish.
    // A separate phase prevents three HTTP requests from hiding many SQL reads.
    const joinedReport = reportTask && readRunning(key, reportTask.params);
    if (joinedReport) await worker([reportTask]);
    await Promise.all(Array.from({ length: Math.min(REFRESH_CONCURRENCY, queue.length) }, () => worker(queue)));
    if (reportTask && !joinedReport) await worker([reportTask]);
    setRefresh((r) =>
      r && r.id === id
        ? {
            ...r,
            state: controller.signal.aborted ? "stopped" : "done",
            views: r.views.map((v) =>
              v.state === "queued" || v.state === "loading" ? { ...v, state: "stopped", report: reportOfRef.current(v.view) } : v,
            ),
          }
        : r,
    );
    if (refreshControl.current?.id === id) refreshControl.current = null;
  }
  /** Stop Refresh all: nothing more starts, reads on their way are dropped (and
   * aborted unless a view's own Refresh waits for the same read). */
  function stopRefresh(reason = "Stopped.") {
    const run = refreshControl.current;
    if (!run) return;
    refreshControl.current = null;
    run.controller.abort();
    setRefresh((r) => (r && r.id === run.id ? { ...r, reason } : r));
  }
  const refreshRef = useRef({ refreshAll, stopRefresh });
  refreshRef.current = { refreshAll, stopRefresh };
  // Refresh all belongs to the Server and Workload toolbars; Tuning has none.
  const tuningRef = useRef(tuningPlace);
  tuningRef.current = tuningPlace;
  // Switching the connection, or leaving the workspace, stops it.
  useEffect(() => {
    if (refreshControl.current && refresh && refresh.key !== activeKey) stopRefresh("The connection in use changed.");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);
  useEffect(
    () => () => {
      refreshControl.current?.controller.abort();
      clearStatementsBehind();
    },
    [],
  );
  // ⌥⌘R (Alt+Ctrl+R elsewhere) runs Refresh all while Server is on screen;
  // Escape stops a run unless something else used it (a popover, the inspector).
  const running = refresh?.state === "running" && !!refreshControl.current;
  useWindowStatus(`${place || "server"}:${activeKey}`, {
    connectionState: connectionLabel(connection),
    engineVersion: lastRead.key === activeKey ? lastRead.engineVersion : undefined,
    refreshedAt: lastRead.key === activeKey ? lastRead.at : undefined,
    refreshing: (running && refresh?.key === activeKey) || !!topStatements.loading || !!analysisReads.loading ||
      (activity.key === activeKey && Object.values(activity.views).some(Boolean)),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!placeRef.current || placeRef.current.offsetParent === null || tuningRef.current) return;
      if (e.altKey && (e.metaKey || e.ctrlKey) && e.code === "KeyR") {
        e.preventDefault();
        void refreshRef.current.refreshAll();
        return;
      }
      if (e.key !== "Escape" || e.defaultPrevented || !refreshControl.current) return;
      const target = e.target as HTMLElement | null;
      if (target?.closest?.("dialog, [role=dialog], input, select, textarea")) return;
      e.preventDefault();
      refreshRef.current.stopRefresh();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // The ages in the Refresh all list move on while it is open.
  useEffect(() => {
    if (!refresh) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  // Badges from data already loaded for the connection in use (nothing is
  // read for a badge, and a report from another connection has none).
  const current = <T,>(b: Bound<T> | null) => (b && b.key === activeKey ? b.report : null);
  const innodbNow = current(innodb);
  const errorlogNow = current(errorlog);
  const sessionsNow = current(sessions);
  const locksNow = current(locks);
  const indexesNow = current(indexes);
  const tablesNow = current(tables);
  const configNow = current(config);
  const instrumentsNow = current(instruments);
  const topNow = current(topStatements.top);
  const badges: Partial<Record<ServerKind, Marker>> = {};
  if (health.report && health.key === activeKey) {
    const tone = toneOf(health.report.summary.status);
    badges.health = dot(tone, TONE_TEXT[tone]);
  }
  if (innodbNow) {
    const tone = innodbNow.available ? toneOf(innodbNow.summary?.status || innodbNow.status) : "unknown";
    badges.innodb = dot(tone, innodbNow.available ? TONE_TEXT[tone] : "not available");
  }
  if (errorlogNow) {
    const n = errorlogNow.counts_current_run.error;
    badges.errorlog = errorlogNow.available
      ? count(n, n > 0 ? "warn" : "ok", `${n} ${n === 1 ? "error" : "errors"} this run, ${n > 0 ? "Watch" : "OK"}`)
      : dot("unknown", "not available");
  }
  if (topNow) {
    const flagged = topNow.statements.filter((s) => s.flags.length).length;
    if (flagged) badges.top = count(flagged, "warn", `${flagged} flagged`);
  }
  if (sessionsNow && sessionsNow.summary.active > 0) {
    const n = sessionsNow.summary.active;
    const tone = toneOf(sessionsNow.summary.status);
    badges.sessions =
      tone === "problem" || tone === "watch"
        ? count(n, tone === "problem" ? "danger" : "warn", `${n} active, ${TONE_TEXT[tone]}`)
        : count(n, "neutral", `${n} active`);
  }
  if (locksNow) {
    const blocked = locksNow.summary.blocked_sessions + (locksNow.ddl_waits?.length || 0);
    badges.locks = blocked
      ? count(blocked, "danger", `${blocked} blocked`)
      : dot("ok", "nothing waiting");
  }
  if (indexesNow) {
    const n = indexesNow.summary.unused_candidates + indexesNow.summary.redundant;
    badges.indexes = n ? count(n, "warn", `${n} to review`) : dot("ok", "nothing to review");
  }
  if (tablesNow) {
    const tone = toneOf(tablesNow.summary.status);
    badges.tables = dot(tone, TONE_TEXT[tone]);
  }
  if (configNow) {
    const tone = toneOf(configNow.summary.status);
    badges.config = dot(tone, TONE_TEXT[tone]);
  }
  if (instrumentsNow) {
    const off = !instrumentsNow.status.enabled;
    const missing = instrumentsNow.features.filter((f) => f.status === "off").length;
    badges.instruments = off
      ? dot("off", "Performance Schema off")
      : missing
        ? dot("watch", `${missing} views missing instruments`)
        : dot("ok", "ready");
  }
  // Replication: only a report of the connection in use counts; Group
  // Replication channels stay Not judged, never a badge tone.
  const replicationNow = current(replication);
  if (replicationNow) {
    if (!replicationNow.replica) badges.replication = dot("unknown", "no replica channels");
    else if (replicationNow.summary.failed > 0)
      badges.replication = count(replicationNow.summary.failed, "danger", `${replicationNow.summary.failed} failed`);
    else {
      const judged = replicationNow.channels.filter((c) => c.judged);
      const worst = worstTone(judged.map((c) => toneOf(c.status)));
      badges.replication = worst === "ok" ? dot("ok", "replicating") : dot(worst, TONE_TEXT[worst]);
    }
  }
  // Maintenance: jobs of the connection in use only, from what is already
  // loaded; nothing is polled or read for a badge.
  const maintenanceJobsNow = maintenance.jobs.filter((b) => b.key === activeKey);
  const maintenanceActive = maintenanceJobsNow.filter((b) => jobActive(b.report));
  const maintenanceFailed = maintenanceJobsNow.filter(
    (b) =>
      !jobActive(b.report) &&
      (b.report.state === "failed" || b.report.state === "refused" || b.report.result?.status === "verification_failed"),
  );
  if (maintenanceActive.length)
    badges.maintenance = count(maintenanceActive.length, "warn", `${maintenanceActive.length} running`);
  else if (maintenanceFailed.length)
    badges.maintenance = count(maintenanceFailed.length, "danger", `${maintenanceFailed.length} failed`);
  const accountsNow = current(accounts);
  if (accountsNow) badges.accounts = count(accountsNow.accounts.length, "neutral", `${accountsNow.accounts.length} accounts read${accountsNow.complete ? "" : " (partial list)"}`);
  const analysisNow = current(analysis);
  if (analysisNow) {
    const tone = toneOf(analysisNow.summary.status);
    badges.report = dot(tone, TONE_TEXT[tone]);
    badges.tuning = analysisNow.summary.problem > 0
      ? count(analysisNow.summary.problem, "danger", `${analysisNow.summary.problem} critical tuning issues`)
      : analysisNow.summary.watch > 0
      ? count(analysisNow.summary.watch, "warn", `${analysisNow.summary.watch} tuning warnings`)
      : dot("ok", "Tuning optimal");
  } else if (health.report && health.key === activeKey) {
    const tone = toneOf(health.report.summary.status);
    badges.tuning = dot(tone, TONE_TEXT[tone]);
  }
  // Refresh all on this connection: a spinner while a view waits or reads, and
  // a mark on a view it could not refresh (its report is the previous one).
  const refreshHere = refresh && refresh.key === activeKey ? refresh : null;
  const refreshOf = (view: ServerKind) => refreshHere?.views.find((v) => v.view === view);
  /** A replaced read whose answer the view shows anyway (a joined read) did refresh it. */
  const outcome = (v: RefreshItem): RefreshState =>
    v.state === "replaced" && v.result !== undefined && reportOf(v.view) === v.result ? "ok" : v.state;
  /** The view still shows the report it had when Refresh all could not refresh it
   * (a newer report from its own Refresh clears the mark; the run's list keeps the outcome). */
  const staleNow = (v: RefreshItem | undefined) =>
    !!v && (v.state === "failed" || v.state === "stopped" || outcome(v) === "replaced") && reportOf(v.view) === v.report;
  const marked = (view: ServerKind): Pick<SourceItem, "badge" | "badgeText"> | undefined => {
    const item = refreshOf(view);
    const own = badges[view];
    if (!item) return own;
    if (item.state === "queued" || item.state === "loading")
      return { badge: <span className="spinner source-refreshing" aria-hidden="true" />, badgeText: "refreshing" };
    if (item && staleNow(item)) {
      const why = item.state === "failed" ? `not refreshed: ${item.error}` : "not refreshed";
      const state = item.state === "failed" ? "failed" : "stopped";
      return {
        badge: (
          <>
            {own?.badge}
            <span className="source-stale" title={why} data-refresh={state}>
              <Icon name="warning" size={12} />
            </span>
          </>
        ),
        badgeText: [own?.badgeText, why].filter(Boolean).join(", "),
      };
    }
    return own;
  };
  const activeViews = place === "workload" ? WORKLOAD_VIEWS : SERVER_VIEWS;
  const activeGroups = place === "workload" ? WORKLOAD_GROUPS : SERVER_GROUPS;
  const groups = activeGroups.map((label) => ({
    label,
    items: activeViews.filter((v) => v.group === label).map((v) => {
      const { badge, badgeText } = marked(v.id) || {};
      return { id: v.id, label: v.label, icon: v.icon, badge, badgeText };
    }),
  }));
  const refreshableHere = refreshTasks();
  const refreshDone = refreshHere ? refreshHere.views.filter((v) => outcome(v) === "ok").length : 0;
  const refreshFailed = refreshHere ? refreshHere.views.filter((v) => v.state === "failed") : [];
  const refreshText = !refreshHere
    ? ""
    : running
      ? `Refreshing ${Math.min(refreshDone + 1, refreshHere.views.length)} of ${refreshHere.views.length}…`
      : `${refreshDone} of ${refreshHere.views.length} ${refreshHere.views.length === 1 ? "view" : "views"} refreshed` +
        (refreshFailed.length ? `; ${refreshFailed.map((v) => v.label).join(", ")} failed` : "") +
        (refreshHere.state === "stopped" && refreshDone + refreshFailed.length < refreshHere.views.length ? "; stopped" : "");
  const viewFailure = staleNow(refreshOf(kind)) ? refreshOf(kind) : undefined;
  const listLabel = place === "workload" ? "Workload views" : "Server views";
  /** The floating list of views, opened from the view's title (ViewLayout
   * draws it through ViewSwitcherContext, for the view on screen only). */
  const switcherFor = (view: ServerKind): ViewSwitcherShared | null => floating ? {
    label: listLabel,
    open: floatingNavOpen,
    setOpen: setFloatingMenuOpen,
    active: view === kind,
    others: othersMarker(activeViews.filter((v) => v.id !== kind).map((v) => badges[v.id]?.tone)),
    takeFocus: takeSwitcherFocus,
    list: (
      <>
        <div className="server-floating-header">
          <span>{listLabel}</span>
          <button
            type="button"
            className="icon-button"
            title="Dock the list of views"
            aria-label="Dock the list of views"
            onClick={() => {
              setFloatingMenuOpen(false);
              toggleNavCollapsed();
            }}
          >
            <Icon name="browser" size={13} />
          </button>
        </div>
        <SourceList
          label={listLabel}
          groups={groups}
          current={kind}
          onSelect={(id) => {
            if (id !== kind) switcherFocus.current = true;
            setFloatingMenuOpen(false);
            choose(id as ServerKind);
          }}
          idPrefix="server-nav-"
          selectOnFocus={false}
        />
      </>
    ),
  } : null;

  // Performance Schema on or off, from any report already loaded for this connection.
  const known = [instrumentsNow, current(waits), current(io), indexesNow, configNow, sessionsNow, innodbNow, errorlogNow].find(
    (r): r is NonNullable<typeof r> => !!r && typeof r.performance_schema === "boolean",
  );
  const performanceSchema = instrumentsNow ? instrumentsNow.status.enabled : known ? known.performance_schema : null;
  // The Tuning place has no list of views to go back to.
  const onBack = compact && !tuningPlace ? back : undefined;
  const analysisPhase = running && refreshHere?.views.some(v => v.view === "report" && v.state === "loading");
  const common = { connection, disabled: disabled || !!analysisPhase, onBack };
  // A persistent status area: an active skip loop or a running maintenance
  // job stays reachable from any view, so Stop and Cancel are never lost
  // while the work runs. Loops listed here belong to the connection in use;
  // stopping one is cancellation of an authorized action, so it stays
  // enabled during capture, policy loading and on a read-only connection.
  const [loopStopping, setLoopStopping] = useState("");
  const loopStoppingRef = useRef(new Set<string>());
  async function stopLoopNow(loop: SkipLoop) {
    const id = loop.job_id;
    const c = latestConnection.current;
    if (!id || loopStoppingRef.current.has(id) || connectionKey(c) !== activeKey) return;
    loopStoppingRef.current.add(id);
    setLoopStopping(id);
    try {
      await stopSkipLoop(c, id);
      // The read rebinds loops to the connection in use; after a switch the
      // view's own poll reconciles when you switch back.
      const answer = await readSkipLoops(c);
      if (connectionKey(latestConnection.current) === activeKey)
        setReplicationLoops({ key: activeKey, label: connectionLabel(c), report: answer.loops });
    } catch {
      // The view shows loop status errors; the persistent banner stays quiet.
    } finally {
      loopStoppingRef.current.delete(id);
      setLoopStopping((pending) => (pending === id ? "" : pending));
    }
  }
  const MAINTENANCE_JOB_LABEL: Record<string, string> = { dump: "Dump", restore: "Restore", clone: "Clone" };
  const jobsHere = maintenanceJobsNow.filter((b) => jobActive(b.report));
  const view = (() => {
    switch (kind) {
      case "health":
        return (
          <HealthView
            {...common}
            selection={selection("health")}
            interval={healthInterval}
            onIntervalChange={setHealthInterval}
            memory={health}
            onMemory={setHealth}
            onOpenView={openView}
            onOpenLesson={onOpenLesson}
            performanceSchema={performanceSchema}
            settings={settings}
            onDigest={onDigest}
            onOpenTop={openTop}
          />
        );
      case "innodb":
        return <InnodbView {...common} selection={selection("innodb")} report={innodb} onReport={setInnodb} onOpenView={openView} />;
      case "errorlog":
        return <ErrorLogView {...common} selection={selection("errorlog")} report={errorlog} onReport={setErrorlog} />;
      case "tuning":
        return (
          <TuningView
            {...common}
            selection={selection("tuning")}
            // Bound to the connection it was read from: after a switch the Full
            // analysis and the scorecard name that server (other-connection note,
            // actions off) and never show it as this one's (tuningInputs).
            report={analysis}
            health={health}
            config={config}
            tables={tables}
            onOpenTop={openTop}
            tabRequest={tuningTab}
            runBlocked={running}
            source={analysisSource}
            reads={analysisReads}
            wasRun={reportRun}
            settings={settings}
            onDigest={onDigest}
            onOpenLink={openLink}
            onOpenLesson={onOpenLesson}
            onRunReport={async () => {
              if (disabled || running) return;
              let from: Connection | null = null;
              await analysisReads.run(
                "report",
                "Running the full analysis…",
                (c, signal) => {
                  from = c;
                  return api<AnalysisReport>("live/report", { connection: c }, signal);
                },
                (b) => {
                  setAnalysis(b);
                  setAnalysisSource(reportSource(from || connection));
                  setReportRun(true);
                },
              );
            }}
            onOpenSql={onOpenSql}
            onOpenView={openView}
          />
        );
      case "top":
        return (
          <TopStatementsView
            {...common}
            selection={selection("top")}
            state={topStatements}
            order={order}
            onOrderChange={setOrder}
            schema={schema}
            onSchemaChange={setSchema}
            settings={settings}
            onDigest={onDigest}
            onOpenPlan={onOpenPlan}
          />
        );
      case "sessions":
        return (
          <SessionsView
            {...common}
            selection={selection("sessions")}
            report={sessions}
            onReport={setSessions}
            scope={sessionsScope}
            onScopeChange={setSessionsScope}
            minSeconds={minSeconds}
            onMinSecondsChange={setMinSeconds}
            onShowPlan={explainConnection}
            onOpenView={openView}
            onOpenSql={onOpenSql}
          />
        );
      case "waits":
        return (
          <WaitsView
            {...common}
            selection={selection("waits")}
            interval={waitsInterval}
            onIntervalChange={setWaitsInterval}
            report={waits}
            onReport={setWaits}
            onOpenView={openView}
          />
        );
      case "io":
        return (
          <IoView
            {...common}
            selection={selection("io")}
            interval={ioInterval}
            onIntervalChange={setIoInterval}
            schema={schema}
            onSchemaChange={setSchema}
            report={io}
            onReport={setIo}
            onOpenIndexes={(name, from) => openView("indexes", name, from)}
            onOpenTop={(name, from) => openView("top", name, from)}
          />
        );
      case "locks":
        return (
          <LocksView
            {...common}
            selection={selection("locks")}
            report={locks}
            onReport={setLocks}
            onShowPlan={explainConnection}
          />
        );
      case "indexes":
        return (
          <IndexesView
            {...common}
            selection={selection("indexes")}
            schemas={indexesSchemas}
            onSchemasChange={setIndexesSchemas}
            report={indexes}
            onReport={setIndexes}
            loadRequest={indexesRequest}
            onLoadRequestDone={() => setIndexesRequest(0)}
            focus={indexesFocus}
            onFocusChange={setIndexesFocus}
            onOpenTables={(name, table, from) => openTable("tables", name, table, from)}
            settings={settings}
            onDigest={onDigest}
            onOpenTop={openTop}
            onOpenSql={onOpenSql}
          />
        );
      case "diagram":
        return <SchemaDiagramView {...common} selection={selection("diagram")} document={diagram} onDocument={setDiagram} selectedDatabase={diagramDatabase} onDatabaseChange={chooseDiagramDatabase} />;
      case "accounts":
        return <AccountsView {...common} selection={selection("accounts")} report={accounts} onReport={setAccounts} />;
      case "config":
        return <ConfigView {...common} selection={selection("config")} report={config} onReport={setConfig} onOpenSql={onOpenSql} />;
      // Operations views mount once and stay mounted, hidden, in
      // .server-keep below: an open preflight or a running job survives
      // switching views and keeps its polls and control paths. Replication
      // is its own place (choose() routes there).
      case "replication":
      case "maintenance":
      case "tables":
      case "instruments":
        return null;
    }
  })();

  return (
    <InstrumentsLinkContext.Provider value={openInstruments}>
      <InspectorContext.Provider value={inspector}>
        <ColumnStoreContext.Provider value={columnStore}>
        <ViewActivityContext.Provider value={reportActivity}>
        <ViewBackLabelContext.Provider value={place === "workload" ? "Workload" : "Server"}>
        <section
          className={`server-place${tuningPlace ? " is-tuning-place" : navCollapsed && !compact ? " is-nav-collapsed" : ""}`}
          aria-labelledby={tuningPlace ? "tuning-place-title" : "server-title"}
          data-place-view={tuningPlace ? "tuning" : undefined}
          ref={placeRef}
        >
          {tuningPlace ? (
            // The top bar names the place; its view toolbar is the header.
            <h1 id="tuning-place-title" className="visually-hidden">Tuning</h1>
          ) : (
          <div className={`place-toolbar has-refresh-all${compact ? " is-compact" : ""}`}>
            <div className="place-title">
              <h1 id="server-title">{place === "workload" ? "Workload" : "Server"}</h1>
              <InfoPopover label={place === "workload" ? "About Workload" : "About Server"}>
                {place === "workload" ? (
                  <>
                    <p>
                      Live views of the connection in use for query workload and resource consumption:
                      Top statements, Sessions, Waits, and I/O. Each view reads the server only when you
                      click Load or Refresh, and its numbers stay in memory until you close the workspace.
                    </p>
                    <p>
                      They only read, except Kill in Sessions (with explicit confirmation).
                    </p>
                    <p>
                      Refresh all (⌥⌘R) reads every Workload section for this connection.
                    </p>
                  </>
                ) : (
                  <>
                    <p>
                      Live views of the connection in use, grouped by job. Each view reads the server only when you
                      click Load or Refresh, and its numbers stay in memory until you close the workspace.
                    </p>
                    <p>
                      They only read. myflames changes the server only when you choose an action and confirm it: Kill in
                      Locks, Change in Parameters, a skip in Replication, or a dump, restore or clone in
                      Maintenance, or changes in Users and roles (your account needs the privilege).
                    </p>
                    <p>Refresh all (⌥⌘R) reads every Server section, including unopened views, three at a time. Full analysis runs afterwards. Database diagram needs a chosen database; imported diagrams stay unchanged. Maintenance refreshes tool detection only.</p>
                  </>
                )}
              </InfoPopover>
              {/* One control for the list of views: dock or float it in a
                  wide window; in a narrow one, where the list is a screen of
                  its own, a Views menu switches views without going back. */}
              {compact && (
                <Popover
                  label={place === "workload" ? "Workload views" : "Server views"}
                  menu
                  buttonClassName="icon-button server-views-menu"
                  buttonTitle={`Switch ${place === "workload" ? "Workload" : "Server"} view`}
                  trigger={<Icon name="menu" size={14} />}
                >
                  <div className="topbar-menu-dropdown views-dropdown">
                    {groups.map((group) => (
                      <div key={group.label} className="topbar-menu-group" role="group" aria-label={group.label}>
                        <div className="topbar-menu-section-title" aria-hidden="true">{group.label}</div>
                        {group.items.map((v) => (
                          <button
                            key={v.id}
                            type="button"
                            role="menuitem"
                            className={`topbar-menu-item${kind === v.id ? " is-active" : ""}`}
                            aria-current={kind === v.id ? "page" : undefined}
                            onClick={() => {
                              closePopovers();
                              choose(v.id as ServerKind);
                            }}
                          >
                            <Icon name={v.icon} size={14} />
                            <span>{v.label}</span>
                          </button>
                        ))}
                      </div>
                    ))}
                  </div>
                </Popover>
              )}
              {!compact && (
                <button
                  type="button"
                  className="icon-button server-sidebar-toggle"
                  aria-label={navCollapsed ? "Dock the list of views" : "Float the list of views"}
                  title={navCollapsed ? "Dock the list of views" : "Float the list of views"}
                  onClick={toggleNavCollapsed}
                >
                  <Icon name={navCollapsed ? "browser" : "split-pane"} size={14} />
                </button>
              )}
            </div>
            {picker}
            <div className="refresh-all" role="group" aria-label="Refresh all">
              {running ? (
                <ToolbarButton className="refresh-all-stop" onClick={() => stopRefresh()} title="Stop refreshing (Escape)">
                  <span className="spinner" aria-hidden="true" />
                  Stop
                </ToolbarButton>
              ) : (
                <ToolbarButton
                  className="refresh-all-button"
                  data-action="refresh-all"
                  disabled={disabled}
                  title={`Refresh all ${refreshableHere.length} Server sections for this connection (⌥⌘R)`}
                  onClick={() => void refreshAll()}
                >
                  Refresh all
                </ToolbarButton>
              )}
              <span className="visually-hidden" role="status">{refreshHere ? refreshText : ""}</span>
              {refreshHere && (
                <Popover
                  label="Refresh all: each view"
                  wide
                  className="refresh-all-anchor"
                  buttonClassName={`text-button refresh-all-status${refreshFailed.length ? " has-failures" : ""}`}
                  trigger={<span>{refreshText}</span>}
                >
                  <h3 className="popover-title">Refresh all on {refreshHere.label}</h3>
                  <ul className="refresh-all-list">
                    {refreshHere.views.map((v) => {
                      const state = outcome(v);
                      const [tone, word] = REFRESH_TEXT[state];
                      const since = state !== "ok" && v.report !== undefined && reportOf(v.view) !== v.report;
                      const retained = v.report ? "The previous report stays." : "No report has been loaded yet.";
                      return (
                        <li key={v.view} data-view={v.view} data-state={state}>
                          <StatusPill tone={tone} label={word} />
                          <span className="refresh-all-view">{v.label}</span>
                          <span className="hint">
                            {state === "ok"
                              ? ago(v.at, clock || Date.now())
                              : since
                                ? `${state === "failed" ? `${v.error} ` : ""}Refreshed since by its own Refresh.`
                                : state === "failed"
                                  ? `${v.error} ${retained}`
                                  : state === "stopped"
                                    ? retained
                                    : state === "replaced"
                                      ? `A newer read of this view was asked for, so this answer was not shown. ${retained}`
                                      : ""}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                  {refreshHere.reason && <p className="hint">{refreshHere.reason}</p>}
                </Popover>
              )}
            </div>
          </div>
          )}
          <div className={`server-layout${compact ? " is-compact" : ""}${showList && !tuningPlace ? " show-list" : ""}${navCollapsed && !compact && !tuningPlace ? " is-nav-collapsed" : ""}`}>
            {!tuningPlace && !floating && (
              <div className="server-source-wrap">
                <SourceList
                  label={listLabel}
                  groups={groups}
                  current={kind}
                  onSelect={(id) => choose(id as ServerKind)}
                  idPrefix="server-nav-"
                  pushed={compact}
                  selectOnFocus={!compact}
                />
              </div>
            )}
            <div className="server-content">
              {viewFailure && (
                <p className="refresh-note" role="note" data-view={kind}>
                  <Icon name="warning" size={14} />
                  {viewFailure.state === "failed"
                    ? `Refresh all could not refresh ${viewFailure.label}: ${viewFailure.error} ${viewFailure.report ? "This is the previous report." : "No report has been loaded yet."}`
                    : viewFailure.state === "replaced"
                      ? `Refresh all did not refresh ${viewFailure.label}: a newer read replaced it. ${viewFailure.report ? "This is the previous report." : "No report has been loaded yet."}`
                      : `Refresh all stopped before ${viewFailure.label}. ${viewFailure.report ? "This is the previous report." : "No report has been loaded yet."}`}
                </p>
              )}
              {loopsHere.some((l) => loopActive(l)) && (
                <ReplicationLoopBanner loops={loopsHere.filter((l) => loopActive(l))} stopping={loopStopping} onStop={(l) => void stopLoopNow(l)} />
              )}
              {kind !== "maintenance" && jobsHere.length > 0 && (
                <div className="ops-loop" role="status">
                  <span>
                    {jobsHere.map((b) => `${MAINTENANCE_JOB_LABEL[b.report.operation] || b.report.operation} on ${b.label}`).join("; ")}
                    {" "}is running.
                  </span>
                  <ToolbarButton onClick={() => choose("maintenance")}>Open Maintenance</ToolbarButton>
                </div>
              )}
              <ViewSwitcherContext.Provider value={switcherFor(kind)}>
                <ViewOnScreenContext.Provider value={onScreen && !(compact && showList && !tuningPlace)}>{view}</ViewOnScreenContext.Provider>
              </ViewSwitcherContext.Provider>
              {keptViews.includes("tables") && (
                <div className="server-keep" hidden={kind !== "tables"}>
                  <ViewSwitcherContext.Provider value={switcherFor("tables")}>
                  <ViewOnScreenContext.Provider value={onScreen && kind === "tables" && !(compact && showList)}>
                  <InspectorContext.Provider value={{...inspector, open: inspector.open && kind === "tables"}}>
                    <TablesView
                      {...common}
                      selection={selection("tables")}
                      schemas={tablesSchemas}
                      onSchemasChange={setTablesSchemas}
                      report={tables}
                      onReport={setTables}
                      loadRequest={tablesRequest}
                      onLoadRequestDone={() => setTablesRequest(0)}
                      focus={tablesFocus}
                      onFocusChange={setTablesFocus}
                      onOpenIndexes={(name, table, from) => openTable("indexes", name, table, from)}
                      onOpenSql={onOpenSql}
                    />
                  </InspectorContext.Provider>
                  </ViewOnScreenContext.Provider>
                  </ViewSwitcherContext.Provider>
                </div>
              )}
              {keptViews.includes("instruments") && (
                <div className="server-keep" hidden={kind !== "instruments"}>
                  <ViewSwitcherContext.Provider value={switcherFor("instruments")}>
                  <ViewOnScreenContext.Provider value={onScreen && kind === "instruments" && !(compact && showList)}>
                  <InspectorContext.Provider value={{...inspector, open: inspector.open && kind === "instruments"}}>
                    <InstrumentsView
                      {...common}
                      selection={selection("instruments")}
                      report={instruments}
                      onReport={setInstruments}
                      target={instrumentsTarget}
                      onTargetDone={() => setInstrumentsTarget(null)}
                      onOpenView={openView}
                      onOpenSql={onOpenSql}
                    />
                  </InspectorContext.Provider>
                  </ViewOnScreenContext.Provider>
                  </ViewSwitcherContext.Provider>
                </div>
              )}
              {keptViews.includes("maintenance") && (
                <div className="server-keep" hidden={kind !== "maintenance"}>
                  <ViewSwitcherContext.Provider value={switcherFor("maintenance")}>
                  <ViewOnScreenContext.Provider value={onScreen && kind === "maintenance" && !(compact && showList)}>
                  <MaintenanceView
                    {...common}
                    selection={selection("maintenance")}
                    section={maintenanceSection}
                    onSection={setMaintenanceSection}
                    memory={maintenance}
                    onMemory={setMaintenance}
                  />
                  </ViewOnScreenContext.Provider>
                  </ViewSwitcherContext.Provider>
                </div>
              )}
            </div>
          </div>
        </section>
        </ViewBackLabelContext.Provider>
        </ViewActivityContext.Provider>
        </ColumnStoreContext.Provider>
      </InspectorContext.Provider>
    </InstrumentsLinkContext.Provider>
  );
}
