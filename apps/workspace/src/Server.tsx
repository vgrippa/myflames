// The Server place: live views of the connection in use, read-only except the
// confirmed Kill in Locks and Sessions. A grouped sub-navigation (Overview,
// Workload, Contention, Schema, Setup, Security) with a status dot or count badge per
// view from data already loaded; each view is a toolbar, one summary line, a
// table or grouped list, and an inspector for the selected row.
//
// Reports stay in memory while the workspace is open (switching views keeps
// them) and are never saved. The selected view, the controls, and whether the
// inspector is open are restored with the workspace (session.ts).
//
// Refresh all (place toolbar, ⌥⌘R) reloads every view that already holds a
// report for the connection in use, never one the user has not opened and
// never the Full analysis: at most REFRESH_CONCURRENCY reads at once, the
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
import { AccountsView } from "./Accounts";
import { SchemaDiagramView } from "./SchemaDiagram";
import { validateDiagram, withDiagramLayout, type DiagramDocument, type SchemaDiagram } from "./schemaDiagramModel";
import type { AccountsReport } from "./accountsApi";
import {
  ColumnStoreContext,
  ViewActivityContext,
  useWindowStatus,
  CountBadge,
  InfoPopover,
  InspectorContext,
  Popover,
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
import { Icon, type IconName } from "./components";
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
import { InnodbView } from "./InnoDB";
import { ReportView } from "./Report";
import {
  TopStatementsView,
  clearStatementsBehind,
  topRead,
  useTopStatements,
  type DigestAction,
  type LabSettings,
} from "./SlowQueries";
import { SessionsView } from "./Sessions";
import { WaitsView } from "./Waits";
import { IoView } from "./IoStats";
import { LocksView } from "./Locks";
import { IndexesView } from "./Indexes";
import { TablesView, type TableFocus } from "./Tables";
import { ConfigView } from "./Config";
import { InstrumentsLinkContext, InstrumentsView } from "./Instruments";
import { MaintenanceView, emptyMaintenance, type MaintenanceMemory, type MaintenanceSection } from "./Maintenance";
import { ReplicationLoopBanner, ReplicationView } from "./Replication";
import {
  jobActive,
  loopActive,
  readSkipLoops,
  stopSkipLoop,
  type LifecycleTools,
  type ReplicationReport,
  type SkipLoop,
} from "./opsApi";
import "./server.css";

/** The views, grouped by job, in sub-navigation order. */
export const SERVER_VIEWS: { id: ServerKind; label: string; icon: IconName; group: string }[] = [
  { id: "health", label: "Health", icon: "health", group: "Overview" },
  { id: "innodb", label: "InnoDB", icon: "engine", group: "Overview" },
  { id: "report", label: "Full analysis", icon: "analysis", group: "Overview" },
  { id: "top", label: "Top statements", icon: "statements", group: "Workload" },
  { id: "sessions", label: "Sessions", icon: "sessions", group: "Workload" },
  { id: "replication", label: "Replication", icon: "replication", group: "Workload" },
  { id: "waits", label: "Waits", icon: "waits", group: "Workload" },
  { id: "io", label: "I/O", icon: "io", group: "Workload" },
  { id: "locks", label: "Locks", icon: "lock", group: "Contention" },
  { id: "indexes", label: "Indexes", icon: "index", group: "Schema" },
  { id: "tables", label: "Tables", icon: "table", group: "Schema" },
  { id: "diagram", label: "Database diagram", icon: "diagram", group: "Schema" },
  { id: "config", label: "Configuration", icon: "configuration", group: "Setup" },
  { id: "instruments", label: "Instruments", icon: "instruments", group: "Setup" },
  { id: "maintenance", label: "Maintenance", icon: "maintenance", group: "Setup" },
  { id: "accounts", label: "Users and roles", icon: "key", group: "Security" },
];
const GROUPS = ["Overview", "Workload", "Contention", "Schema", "Setup", "Security"];
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
  accept: (b: Bound<never>) => void;
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
// A sub-navigation badge is a bare dot: the item's hidden text (", Watch")
// names the status once. StatusDot now draws its word too, which repeated it.
function dot(tone: Tone, text: string) {
  return { badge: <span className={`status-dot is-${tone}`} aria-hidden="true" />, badgeText: text };
}
function count(n: number, tone: "warn" | "danger" | "neutral", text: string) {
  return { badge: <CountBadge count={n} tone={tone} />, badgeText: text };
}

export function Server({
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
}: {
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
  /** Put a statement (Configuration's SET SESSION) in the Query editor's current tab, without running it. */
  onOpenSql?: (sql: string) => void;
  /** A link from the Query editor's schema browser: open Indexes or Tables
   * on one table (n changes per click; from is the key it was read with). */
  link?: { view: "indexes" | "tables" | "health"; schema: string; table: string; from: string; n: number } | null;
}) {
  const [kind, setKind] = useState<ServerKind>(serverKind(initial?.kind));
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
  const [sessionsScope, setSessionsScope] = useState<"active" | "all">(initial?.sessions_scope || "active");
  /** Maintenance's open task section, restored with the workspace. */
  const [maintenanceSection, setMaintenanceSection] = useState<MaintenanceSection>(initial?.maintenance_section || "tools");
  /** A skip loop was active when the workspace was saved: the only loop state
   * a restore keeps, so Replication can say nothing resumed. Captured once,
   * independently of the flag polls write afterwards. */
  const [restoredReplicationLoop] = useState(initial?.replication_loop_active === true);
  const [loopActiveNow, setLoopActiveNow] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(initial?.inspector === true);
  /** A Full analysis ran (restored; the report itself never is). */
  const [reportRun, setReportRun] = useState(initial?.report_run === true);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
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
      kind,
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
  }, [kind, order, minSeconds, healthInterval, schema, waitsInterval, ioInterval, indexesSchemas, tablesSchemas, inspectorOpen, sessionsScope, maintenanceSection, loopActiveNow, reportRun, columns]);

  // Reports, kept while the place is mounted (never saved). Each one carries
  // the key of the connection it was read from (binding.ts): a report from
  // another connection stays on screen, greyed, with its actions off.
  const activeKey = connectionKey(connection);
  const [health, setHealth] = useState<HealthMemory>(emptyHealth);
  const [innodb, setInnodb] = useState<Bound<InnodbReport> | null>(null);
  const [sessions, setSessions] = useState<Bound<SessionsReport> | null>(null);
  const [waits, setWaits] = useState<Bound<WaitsReport> | null>(null);
  const [io, setIo] = useState<Bound<IoReport> | null>(null);
  const [locks, setLocks] = useState<Bound<LocksReport> | null>(null);
  const [indexes, setIndexes] = useState<Bound<IndexesReport> | null>(null);
  const [tables, setTables] = useState<Bound<TablesReport> | null>(null);
  const [diagram, setDiagram] = useState<DiagramDocument | null>(null);
  const [config, setConfig] = useState<Bound<ConfigReport> | null>(null);
  const [accounts, setAccounts] = useState<Bound<AccountsReport> | null>(null);
  const [instruments, setInstruments] = useState<Bound<InstrumentsReport> | null>(null);
  const [replication, setReplication] = useState<Bound<ReplicationReport> | null>(null);
  const [replicationLoops, setReplicationLoops] = useState<Bound<SkipLoop[]> | null>(null);
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
      innodb, sessions, waits, io, locks, indexes, tables, config, instruments, replication,
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
  }, [activeKey, health.report, health.key, innodb, sessions, waits, io, locks, indexes, tables,
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
  const place = useRef<HTMLElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = place.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const compact = width > 0 && width < COMPACT_WIDTH;
  const sheet = width > 0 && width < SHEET_WIDTH;
  const [pushed, setPushed] = useState(false);
  const showList = compact && !pushed;

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
      if (!place.current || place.current.offsetParent === null) return;
      e.preventDefault();
      setInspectorOpen((o) => !o);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /** Choose a view from the list (narrow layouts push it on screen). */
  function choose(view: ServerKind) {
    setKind(view);
    keepMounted(view);
    // Once a view is chosen it stays on screen if the window narrows.
    setPushed(true);
    if (compact) window.setTimeout(() => document.getElementById(`${view}-title`)?.focus());
  }
  /** Replication and Maintenance run operations (a skip loop, a dump) that
   * must survive a view switch, so each is mounted on first visit and kept
   * mounted, hidden, after that. */
  // A restored workspace that had a skip loop running mounts Replication at
  // once (hidden), so its status read finds a loop still running in this
  // myflames, or says it stopped when myflames restarted. It never starts one.
  const [keptViews, setKeptViews] = useState<ServerKind[]>(() => {
    const kept: ServerKind[] = initial?.kind === "replication" || initial?.kind === "maintenance" ? [initial.kind] : [];
    if (initial?.replication_loop_active === true && !kept.includes("replication")) kept.push("replication");
    return kept;
  });
  function keepMounted(view: ServerKind) {
    if (view === "replication" || view === "maintenance")
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
    setKind(target);
    keepMounted(target);
    setPushed(true);
    if (linkSchema !== undefined) {
      setSchema(linkSchema);
      if (target === "top" && !disabledRef.current) void topStatements.load(linkSchema);
      if (target === "indexes") {
        setIndexesSchemas(linkSchema ? [linkSchema] : []);
        setIndexesFocus(null);
        setIndexesRequest((n) => n + 1);
      }
    }
    window.setTimeout(() => {
      const nav = document.getElementById(`server-nav-${target}`);
      if (nav && nav.offsetParent !== null) nav.focus({ preventScroll: true });
      else document.getElementById(`${target}-title`)?.focus();
    });
  }
  function back() {
    setPushed(false);
    window.setTimeout(() => document.getElementById(`server-nav-${kind}`)?.focus());
  }
  /** Open Instruments at one row (from another view's "instrument is off" note). */
  function openInstruments(row: string) {
    setInstrumentsTarget((t) => ({ row, n: (t?.n || 0) + 1 }));
    openView("instruments");
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
    if (link.key && target === "config") row = link.key;
    else if (link.key && target === "innodb") row = `section:${link.key}`;
    else if (link.key && target === "health" && finding.source?.view === "health")
      row = `${finding.source.kind === "diagnostic" ? "diag" : "metric"}:${link.key}`;
    const report = reportKeys[target];
    if (row && report && report === connectionKey(latestConnection.current)) {
      setSelected((s) => ({ ...s, [target]: { key: row, report } }));
      setInspectorOpen(true);
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
  /** The reads Refresh all would make now: one per view with a report for the
   * connection in use, with that view's own parameters (windowed views first). */
  function refreshTasks(): RefreshTask[] {
    const key = connectionKey(latestConnection.current);
    const live = <T,>(b: Bound<T> | null) => (b && b.key === key ? b.report : null);
    const tasks: RefreshTask[] = [];
    const add = (view: ServerKind, params: string, path: string, body: (c: Connection) => object, accept: (b: Bound<never>) => void) => {
      // The view's own read still on its way is joined, never replaced.
      const own = lastViewRead(view, key);
      const joined = own && readRunning(key, own.params) ? own : null;
      tasks.push({
        view,
        label: SERVER_VIEWS.find((v) => v.id === view)?.label || view,
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
    if (health.report && health.key === key)
      add("health", `health:${healthInterval}`, "live/health", (c) => ({ connection: c, interval: healthInterval }), (b) =>
        setHealth((previous) => rememberHealth(previous, b as Bound<HealthReport>)),
      );
    if (live(waits))
      add("waits", `waits:${waitsInterval}`, "live/waits", (c) => ({ connection: c, interval: waitsInterval }), (b) => setWaits(b as Bound<WaitsReport>));
    if (live(io)) {
      const target = schema.trim();
      add("io", `io:${ioInterval}:${target}`, "live/io", (c) => (target ? { connection: c, interval: ioInterval, schema: target } : { connection: c, interval: ioInterval }), (b) =>
        setIo(b as Bound<IoReport>),
      );
    }
    // InnoDB's Refresh reads without the raw text (Show raw output asks for it).
    if (live(innodb)) add("innodb", "innodb:false", "live/innodb", (c) => ({ connection: c, raw: false }), (b) => setInnodb(b as Bound<InnodbReport>));
    if (live(topStatements.top)) {
      const read = topRead(order, schema, topStatements.table);
      const target = schema.trim();
      add("top", read.params, "live/top", read.body, (b) => topStatements.accept(b as Bound<TopReport>, target));
    }
    if (live(sessions)) {
      const activeOnly = sessionsScope === "active";
      add("sessions", `sessions:${activeOnly}:${minSeconds}`, "live/sessions", (c) => ({ connection: c, active_only: activeOnly, min_seconds: minSeconds }), (b) =>
        setSessions(b as Bound<SessionsReport>),
      );
    }
    const locksReport = live(locks);
    if (locksReport) {
      const waitsOnly = lastFlag("locks", locksReport.scope === "waits");
      add("locks", `locks:${waitsOnly}`, "live/locks", (c) => ({ connection: c, limit: 500, waits_only: waitsOnly }), (b) => setLocks(b as Bound<LocksReport>));
    }
    if (live(indexes))
      add("indexes", `indexes:${JSON.stringify(indexesSchemas)}`, "live/indexes", (c) => ({ connection: c, ...(indexesSchemas.length ? { schemas: indexesSchemas } : {}) }), (b) =>
        setIndexes(b as Bound<IndexesReport>),
      );
    const tablesReport = live(tables);
    if (tablesReport) {
      const own = lastViewRead("tables", key);
      const scope = `tables:${JSON.stringify(tablesSchemas)}:`;
      // Consent belongs to the last explicitly requested scope, never to a
      // newly chosen database list. Keep Fresh numbers ahead of the :all suffix.
      const sameScope = own?.params.startsWith(scope);
      const allSchemas = !!sameScope && own!.params.endsWith(":all");
      const fresh = (sameScope ? own!.params.slice(scope.length).split(":")[0] === "true" : !!tablesReport.statistics?.fresh_requested) && tablesReport.engine !== "mariadb";
      add("tables", `${scope}${fresh}${allSchemas ? ":all" : ""}`, "live/tables", (c) => ({ connection: c, ...(tablesSchemas.length ? { schemas: tablesSchemas } : {}), fresh, ...(allSchemas ? { all_schemas: true } : {}) }), (b) =>
        setTables(b as Bound<TablesReport>),
      );
    }
    if (diagram?.source.kind === "server" && diagram.source.key === key && diagram.source.database) {
      const database = diagram.source.database;
      add("diagram", `diagram:${JSON.stringify(database)}`, "schema-diagram/read", c => ({ connection: c, database }), b => {
        const report = validateDiagram((b as Bound<SchemaDiagram>).report);
        setDiagram(old => old?.source.kind === "server" && old.source.key === key && old.source.database === database ?
          { ...old, diagram: withDiagramLayout(report, old.diagram.layout?.positions) } : old);
      });
    }
    if (live(accounts)) add("accounts", "accounts", "accounts/list", (c) => ({ connection: c }), (b) => setAccounts(b as Bound<AccountsReport>));
    if (live(config)) add("config", "config", "live/config", (c) => ({ connection: c }), (b) => setConfig(b as Bound<ConfigReport>));
    if (live(instruments))
      add("instruments", "instruments", "live/instruments", (c) => ({ connection: c }), (b) => setInstruments(b as Bound<InstrumentsReport>));
    // Replication status and detected maintenance tools refresh like the other
    // views; loop starts, preflights, jobs and the upgrade check never run in
    // Refresh all.
    if (live(replication))
      add("replication", "replication", "live/replication", (c) => ({ connection: c }), (b) => setReplication(b as Bound<ReplicationReport>));
    if (live(maintenance.tools))
      add("maintenance", "maintenance-tools", "lifecycle/tools", (c) => ({ connection: c }), (b) =>
        setMaintenance((m) => ({ ...m, tools: b as Bound<LifecycleTools> })),
      );
    const order_ = (v: ServerKind) => (WINDOWED.includes(v) ? 0 : 1);
    return tasks.sort((a, b) => order_(a.view) - order_(b.view));
  }
  /** Refresh every loaded view of the connection in use; a second call while one runs does nothing. */
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
    const queue = [...tasks];
    const worker = async () => {
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
            current.accept({ key, label, report: result as never });
            update(current.view, { state: "ok", at: new Date().toISOString(), error: undefined });
          } else update(current.view, { state: "replaced", result, report: reportOfRef.current(current.view) });
        } catch (e) {
          const stopped = controller.signal.aborted || (e as Error).name === "AbortError";
          const report = reportOfRef.current(current.view);
          update(current.view, stopped ? { state: "stopped", report } : { state: "failed", error: (e as Error).message, report });
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(REFRESH_CONCURRENCY, tasks.length) }, worker));
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
  useWindowStatus(`server:${activeKey}`, {
    connectionState: connectionLabel(connection),
    engineVersion: lastRead.key === activeKey ? lastRead.engineVersion : undefined,
    refreshedAt: lastRead.key === activeKey ? lastRead.at : undefined,
    refreshing: (running && refresh?.key === activeKey) || !!topStatements.loading || !!analysisReads.loading ||
      (activity.key === activeKey && Object.values(activity.views).some(Boolean)),
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!place.current || place.current.offsetParent === null) return;
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
  const sessionsNow = current(sessions);
  const locksNow = current(locks);
  const indexesNow = current(indexes);
  const tablesNow = current(tables);
  const configNow = current(config);
  const instrumentsNow = current(instruments);
  const topNow = current(topStatements.top);
  const badges: Partial<Record<ServerKind, Pick<SourceItem, "badge" | "badgeText">>> = {};
  if (health.report && health.key === activeKey) {
    const tone = toneOf(health.report.summary.status);
    badges.health = dot(tone, TONE_TEXT[tone]);
  }
  if (innodbNow) {
    const tone = innodbNow.available ? toneOf(innodbNow.summary?.status || innodbNow.status) : "unknown";
    badges.innodb = dot(tone, innodbNow.available ? TONE_TEXT[tone] : "not available");
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
  if (diagram && (diagram.source.kind === "import" || diagram.source.key === activeKey))
    badges.diagram = count(diagram.diagram.tables.length, "neutral", `${diagram.diagram.tables.length} tables`);
  const accountsNow = current(accounts);
  if (accountsNow) badges.accounts = count(accountsNow.accounts.length, "neutral", `${accountsNow.accounts.length} accounts read${accountsNow.complete ? "" : " (partial list)"}`);
  const analysisNow = current(analysis);
  if (analysisNow) {
    const tone = toneOf(analysisNow.summary.status);
    badges.report = dot(tone, TONE_TEXT[tone]);
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
  const groups = GROUPS.map((label) => ({
    label,
    items: SERVER_VIEWS.filter((v) => v.group === label).map((v) => ({ id: v.id, label: v.label, icon: v.icon, ...marked(v.id) })),
  }));
  const loadedHere = refreshTasks();
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

  // Performance Schema on or off, from any report already loaded for this connection.
  const known = [instrumentsNow, current(waits), current(io), indexesNow, configNow, sessionsNow, innodbNow].find(
    (r): r is NonNullable<typeof r> => !!r && typeof r.performance_schema === "boolean",
  );
  const performanceSchema = instrumentsNow ? instrumentsNow.status.enabled : known ? known.performance_schema : null;
  const onBack = compact ? back : undefined;
  const common = { connection, disabled, onBack };
  // A persistent status area: an active skip loop or a running maintenance
  // job stays reachable from any view, so Stop and Cancel are never lost
  // while the work runs. Loops listed here belong to the connection in use;
  // stopping one is cancellation of an authorized action, so it stays
  // enabled during capture, policy loading and on a read-only connection.
  const loopsHere = replicationLoops?.key === activeKey ? replicationLoops.report : [];
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
      case "report":
        return (
          <ReportView
            {...common}
            selection={selection("report")}
            report={analysis}
            source={analysisSource}
            reads={analysisReads}
            onReport={(b, source) => {
              setAnalysis(b);
              setAnalysisSource(source);
              setReportRun(true);
            }}
            wasRun={reportRun}
            settings={settings}
            onDigest={onDigest}
            onOpenLink={openLink}
            onOpenLesson={onOpenLesson}
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
          />
        );
      case "tables":
        return (
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
          />
        );
      case "diagram":
        return <SchemaDiagramView {...common} selection={selection("diagram")} document={diagram} onDocument={setDiagram} />;
      case "accounts":
        return <AccountsView {...common} selection={selection("accounts")} report={accounts} onReport={setAccounts} />;
      case "config":
        return <ConfigView {...common} selection={selection("config")} report={config} onReport={setConfig} onOpenSql={onOpenSql} />;
      case "instruments":
        return (
          <InstrumentsView
            {...common}
            selection={selection("instruments")}
            report={instruments}
            onReport={setInstruments}
            target={instrumentsTarget}
            onTargetDone={() => setInstrumentsTarget(null)}
            onOpenView={openView}
          />
        );
      // Replication and Maintenance mount once and stay mounted, hidden, in
      // .server-keep below: an open preflight, a running job or a skip loop
      // survives switching views and keeps its polls and control paths.
      case "replication":
      case "maintenance":
        return null;
    }
  })();

  return (
    <InstrumentsLinkContext.Provider value={openInstruments}>
      <InspectorContext.Provider value={inspector}>
        <ColumnStoreContext.Provider value={columnStore}>
        <ViewActivityContext.Provider value={reportActivity}>
        <section className="server-place" aria-labelledby="server-title" ref={place}>
          <div className={`place-toolbar has-refresh-all${compact ? " is-compact" : ""}`}>
            <div className="place-title">
              <h1 id="server-title">Server</h1>
              <InfoPopover label="About Server">
                <p>
                  Live views of the connection in use, grouped by job. Each view reads the server only when you
                  click Load or Refresh, and its numbers stay in memory until you close the workspace.
                </p>
                <p>
                  They only read. myflames changes the server only when you choose an action and confirm it: Kill in
                  Locks or Sessions, Change in Configuration, a skip in Replication, or a dump, restore or clone in
                  Maintenance, or changes in Users and roles (your account needs the privilege).
                </p>
                <p>Refresh all (⌥⌘R) reloads every view you already loaded for this connection, three at a time.</p>
              </InfoPopover>
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
                  icon="refresh"
                  className="refresh-all-button"
                  data-action="refresh-all"
                  disabled={disabled || !loadedHere.length}
                  title={
                    loadedHere.length
                      ? `Refresh the ${loadedHere.length === 1 ? "view" : `${loadedHere.length} views`} loaded for this connection (⌥⌘R)`
                      : "Load a view first: Refresh all reloads only views that hold a report for this connection"
                  }
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
                                  ? `${v.error} The previous report stays.`
                                  : state === "stopped"
                                    ? "The previous report stays."
                                    : state === "replaced"
                                      ? "A newer read of this view was asked for, so this answer was not shown. The previous report stays."
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
          <div className={`server-layout${compact ? " is-compact" : ""}${showList ? " show-list" : ""}`}>
            <SourceList
              label="Server views"
              groups={groups}
              current={kind}
              onSelect={(id) => choose(id as ServerKind)}
              idPrefix="server-nav-"
              pushed={compact}
            />
            <div className="server-content">
              {viewFailure && (
                <p className="refresh-note" role="note" data-view={kind}>
                  <Icon name="warning" size={14} />
                  {viewFailure.state === "failed"
                    ? `Refresh all could not refresh ${viewFailure.label}: ${viewFailure.error} This is the previous report.`
                    : viewFailure.state === "replaced"
                      ? `Refresh all did not refresh ${viewFailure.label}: a newer read replaced it. This is the previous report.`
                      : `Refresh all stopped before ${viewFailure.label}. This is the previous report.`}
                </p>
              )}
              {kind !== "replication" && loopsHere.some((l) => loopActive(l)) && (
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
              {view}
              {keptViews.includes("replication") && (
                <div className="server-keep" hidden={kind !== "replication"}>
                  <ReplicationView
                    {...common}
                    selection={selection("replication")}
                    report={replication}
                    onReport={setReplication}
                    loops={replicationLoops}
                    onLoops={setReplicationLoops}
                    restoredLoop={restoredReplicationLoop}
                    onLoopActivity={setLoopActiveNow}
                  />
                </div>
              )}
              {keptViews.includes("maintenance") && (
                <div className="server-keep" hidden={kind !== "maintenance"}>
                  <MaintenanceView
                    {...common}
                    selection={selection("maintenance")}
                    section={maintenanceSection}
                    onSection={setMaintenanceSection}
                    memory={maintenance}
                    onMemory={setMaintenance}
                  />
                </div>
              )}
            </div>
          </div>
        </section>
        </ViewActivityContext.Provider>
        </ColumnStoreContext.Provider>
      </InspectorContext.Provider>
    </InstrumentsLinkContext.Provider>
  );
}
