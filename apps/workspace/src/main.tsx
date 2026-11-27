// First: the stylesheets, in cascade order (styles.ts).
import "./styles";
import { setUnsavedWorkCheck } from "./unsavedWork";
import { PrivilegesProvider, PrivilegeWhyHost, preloadWhyDisabled } from "./privileges";
import { ConnectionInUseProvider } from "./connectionInUse";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode, type SyntheticEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  api,
  authenticate,
  download,
  exportedAnalysis,
  openConnectionGroup,
  searchObjects,
  type Analysis,
  type Bundle,
  type Capture,
  type CatalogObject,
  type Comparison,
  type GroupConnection,
  type GroupSaveResult,
  type Connection,
  type Profile,
  type Plan,
  type PlanReportRequest,
  type TopStatement,
  type View,
} from "./api";
import { Icon, ImportDialog, type IconName } from "./components";
import { currentUiPreferences, loadUiPreferencesWithin, setUiPreference, useUiPreferences } from "./uiPreferences";
import { THEME_CHOICES, applyTheme } from "./themes";
import "./fonts";
import type { WorkspaceCommand } from "./CommandPalette";
import type { EditorState, QueryLabHandle, QueryNavigation } from "./QueryLab";
import { Home } from "./Home";
import { SERVER_VIEWS, WORKLOAD_VIEWS, WORKLOAD_KINDS, ALL_SERVER_VIEWS } from "./serverViews";
import { lazyComponent, preloadWhenIdle } from "./lazy";
import { connectionKey, connectionLabel, endpointKey, keyProfileId, type Bound } from "./binding";
import { refreshSavedConnections, useSavedConnections } from "./savedConnections";
import { useSchemaCatalog } from "./PerfShared";
import { LOOP_POLL_MS, loopActive, NOT_A_REPLICA, readSkipLoops, replicationNav, stopSkipLoop, type ReplicationReport, type SkipLoop } from "./opsApi";
import { clusterNav, NOT_A_CLUSTER, type ClusterFailure, type ClusterReport } from "./clusterApi";
import { NOT_A_PROXYSQL, proxySqlNav, type ProxySqlFailure, type ProxySqlReport } from "./proxysqlApi";
import { NOT_A_READYSET, readysetNav, type ReadysetFailure, type ReadysetReport } from "./readysetApi";
import { NavMarker, WindowStatusBar, WindowStatusProvider } from "./kit";
import { MAC, shortcut } from "./platform";
import { ViewOnScreenContext } from "./viewOnScreen";
import { PLACES, Sidebar, SidebarToggle, type Place } from "./Sidebar";
import {
  AccountPrivilegesSheet,
  ConnectionPicker,
  ConnectionTypeStrip,
  Connections,
  SafetyStatus,
  WritePolicyContext,
  connectionFields,
  connectionSafety,
  connectionTarget,
  requestConnection,
  recordConnectionUse,
  type ActiveConnection,
  type Safety,
} from "./Connections";
import { groupConnectionLabel } from "./binding";
import { unavailableMemberConnection } from "./queryConnection";
const GroupEditorSheet = lazyComponent(() => import("./GroupEditorSheet").then((m) => m.GroupEditorSheet));
import type { DigestAction } from "./SlowQueries";
import type { SettingsSection } from "./SettingsPage";
import { ActionButton, ActionStatus, useAction, ColumnStoreContext, Popover, useColumnStore, useFocusScope, type ColumnPrefs } from "./kit";
import {
  defaultLab,
  loadSession,
  loopMemberIds,
  normalizeLab,
  sameLab,
  serializePlans,
  serializeSession,
  sessionEdits,
  SessionSaver,
  text,
  type LabState,
  type SessionPlan,
  type SaveStatus,
  type ServerKind,
  type SessionConnection,
  type SlowState,
} from "./session";
import { engineName, metrics, views } from "./planDisplay";
import logo from "../../../docs/brand/myflames-app-logo.png";
// Places and views that are not on the first screen load their chunk when
// they first mount (lazy.tsx); Home, the sidebar and the shell stay here.
// The Query editor mounts at startup, hidden; its chunk loads beside the
// first render instead of delaying it.
const QueryLab = lazyComponent(() => import("./QueryLab").then((m) => m.QueryLab));
// Start loading it now, alongside sign-in and the session read, so it is
// usually ready when the first screen renders.
void QueryLab.preload().catch(() => {});
const Server = lazyComponent(() => import("./Server").then((m) => m.Server));
const ReplicationPlace = lazyComponent(() => import("./ReplicationPlace").then((m) => m.ReplicationPlace));
const ClusterPlace = lazyComponent(() => import("./ClusterPlace").then((m) => m.ClusterPlace));
const TopologyPlace = lazyComponent(() => import("./TopologyPlace").then((m) => m.TopologyPlace));
import type { MemberPlace, OpenMemberInfo } from "./TopologyPlace";
const ProxySqlPlace = lazyComponent(() => import("./ProxySqlPlace").then((m) => m.ProxySqlPlace));
const ReadysetPlace = lazyComponent(() => import("./ReadysetPlace").then((m) => m.ReadysetPlace));
const FilesPlace = lazyComponent(() => import("./FilesPlace").then((m) => m.FilesPlace));
// Query digest (in the style of pt-query-digest) is a place of its own in the
// SQL workspace group, after the Query editor; its row actions open the editor.
const QueryDigestView = lazyComponent(() => import("./QueryDigest").then((m) => m.QueryDigestView));
// Schema versions: saved schemas of a database and comparisons, after Database diagram.
const SchemaVersionsPlace = lazyComponent(() => import("./SchemaVersions").then((m) => m.SchemaVersionsPlace));
const Library = lazyComponent(() => import("./Library").then((m) => m.Library));
const AuditLog = lazyComponent(() => import("./Audit").then((m) => m.AuditLog));
// Settings: a page in the main area (the sidebar gear, ⌘, / Ctrl+,, the
// command palette), not a place in the sidebar's navigation.
const SettingsPage = lazyComponent(() => import("./SettingsPage").then((m) => m.SettingsPage));
const Teach = lazyComponent(() => import("./Teach").then((m) => m.Teach));
// The command palette renders only while open (⌘K / Ctrl+K or the toolbar
// button), so its chunk loads on first use.
const CommandPalette = lazyComponent(() => import("./CommandPalette").then((m) => m.CommandPalette));
// Pointing at or focusing the toolbar opener starts the chunk on its way, so
// a click rarely waits (the UpdateBadge pattern); the ⌘K path is covered by
// the idle preload below. A failed warm-up is quiet: the open itself shows
// the lazy fallback, then LoadFailed if the chunk cannot be read.
const warmCommandPalette = () => { void CommandPalette.preload().catch(() => {}); };
// Start the screen and its chart chunks together, but only the screen gates
// the place. A stale chart must fail inside its own boundary, not hide the
// empty state or prevent other Plans controls from opening.
const PlanScreen = lazyComponent(() => {
  const screen = import("./PlanScreen");
  void import("./PlanChart").catch(() => {});
  void import("./ContextPanel").catch(() => {});
  return screen.then((m) => m.PlanScreen);
});
type PlanExportSnapshot =
  | ({ kind: "plan"; name: string } & PlanReportRequest)
  | { kind: "compare"; before: string; after: string; hide_literals: boolean; name: string };
// The screen in the saved session. Stored values keep the names earlier
// versions wrote (inspect, compare, query, library, teach), so a session opens
// in either direction; home and server are new. Each maps to one place, except
// audit and settings: the Audit log and Settings are local to this computer
// and open from the gear, so no place is "you are here" while one is on screen.
type Mode = "home" | "inspect" | "compare" | "query" | "digest" | "diagram" | "schema" | "server" | "workload" | "topology" | "replication" | "cluster" | "proxysql" | "readyset" | "tuning" | "files" | "library" | "teach" | "audit" | "settings";
const modes: Mode[] = ["home", "inspect", "compare", "query", "digest", "diagram", "schema", "server", "workload", "topology", "replication", "cluster", "proxysql", "readyset", "tuning", "files", "library", "teach", "audit", "settings"];
function placeOf(mode: Mode): Place | null {
  switch (mode) {
    case "audit":
    case "settings":
      return null;
    case "inspect":
    case "compare":
      return "plans";
    case "library":
      return "investigations";
    case "teach":
      return "learn";
    default:
      return mode;
  }
}
const PLACE_MODE: Record<Exclude<Place, "plans">, Mode> = {
  home: "home",
  query: "query",
  digest: "digest",
  diagram: "diagram",
  schema: "schema",
  server: "server",
  workload: "workload",
  topology: "topology",
  replication: "replication",
  cluster: "cluster",
  proxysql: "proxysql",
  readyset: "readyset",
  tuning: "tuning",
  files: "files",
  investigations: "library",
  learn: "teach",
};
const isRecord = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
/** After a topology card opened a place, focus lands on the place's heading
 * (the chunk can still be loading, so retry briefly); the page never scrolls. */
function focusPlaceHeadingSoon() {
  let attempts = 0;
  const tick = () => {
    const main = document.getElementById("main");
    const target = main
      ? Array.from(main.querySelectorAll<HTMLElement>('h2[id$="-title"]')).find((el) => !el.closest("[hidden]"))
        || main.querySelector<HTMLElement>(
          ':scope > div:not([hidden]) button, :scope > div:not([hidden]) input, :scope > div:not([hidden]) select, :scope > div:not([hidden]) [tabindex]:not([tabindex="-1"])')
      : null;
    if (target) { target.focus({ preventScroll: true }); return; }
    if (++attempts < 80) window.setTimeout(tick, 25);
  };
  window.setTimeout(tick, 0);
}
/** Back to Topology returns focus to the card that was activated: the shell
 * hands the card's key to the Topology place (topologyFocusCard), which
 * focuses it in a layout effect once its map shows the card on screen. */
/** One stop in the window's navigation history (Back and Forward, the cross-link
 * chip, the palette's Recent views). In-memory only. */
type NavEntry = {
  mode: Mode;
  /** Document/object identities, never SQL or results. */
  selection: { query?: QueryNavigation; plan?: string; compare?: [string, string] };
  /** Explicit cross-links are separate stops even within the same place. */
  stop: number;
  /** The Server or Workload view on the stop (null elsewhere). */
  view: ServerKind | null;
  /** connectionKey of the connection the stop belonged to ("" without one). */
  connection: string;
  connectionName: string;
  label: string;
  icon: IconName;
};
/** The name and glyph of one history stop: the place, and for Server and
 * Workload the view on it. */
function navEntryLabel(mode: Mode, view: ServerKind | null): { label: string; icon: IconName } {
  if ((mode === "server" || mode === "workload") && view) {
    const found = ALL_SERVER_VIEWS.find((v) => v.id === view);
    const placeLabel = mode === "server" ? "Server" : "Workload";
    if (found) return { label: `${placeLabel} · ${found.label}`, icon: found.icon };
    return { label: placeLabel, icon: mode === "server" ? "server" : "statements" };
  }
  if (mode === "compare") return { label: "Plans · Compare", icon: "compare" };
  if (mode === "settings") return { label: "Settings", icon: "settings" };
  const place = placeOf(mode);
  const found = place ? PLACES.find((p) => p.key === place) : null;
  return { label: found?.label || "Audit log", icon: found?.icon || "audit" };
}
/** Object kinds in the palette's search results: one glyph and one noun each. */
const OBJECT_ICONS: Record<string, IconName> = {
  table: "table", view: "view", "system view": "view", sequence: "table",
  procedure: "query", function: "query", trigger: "run", event: "history", schema: "database",
};
const OBJECT_GROUP_LABELS: Record<string, string> = {
  table: "Table", view: "View", "system view": "System view", sequence: "Sequence",
  procedure: "Procedure", function: "Function", trigger: "Trigger", event: "Event",
};
/** The analysis request limit (ui.py MAX_BODY), less room for framing. */
const ANALYZE_BUDGET = 12 * 1024 * 1024 - 64 * 1024;
const utf8 = new TextEncoder();
/** The editor text that unload protection compares (tab ids, names, SQL, parameters). */
const tabsText = (tabs: LabState["tabs"]) =>
  JSON.stringify(tabs.map((t) => [t.id, t.name, t.sql, t.parameters]));
/**
 * What restoring a captured plan sends to /api/analyze: the plan text once
 * (capture.raw is the same plan), and no run plans or optimizer trace (the
 * analysis does not read them). They are put back on the result, so the
 * restored plan keeps all of its evidence.
 */
function slimCapture(raw: string, capture: Capture) {
  return {
    ...capture,
    raw: capture.raw === raw ? "" : capture.raw,
    runs: Array.isArray(capture.runs)
      ? capture.runs.map((r) => (isRecord(r) ? { ...r, plan: "" } : r))
      : capture.runs,
    trace: null,
  };
}
function restoreCapture(original: Capture, analyzed: Capture): Capture {
  const runs = Array.isArray(original.runs) ? original.runs : [];
  const trace = original.trace;
  const traceOk =
    isRecord(trace) &&
    "data" in trace &&
    typeof trace.missing_bytes === "number" &&
    typeof trace.insufficient_privileges === "number";
  return {
    ...analyzed,
    raw: typeof original.raw === "string" ? original.raw : analyzed.raw,
    runs: analyzed.runs.map((r, i) => ({
      ...r,
      plan: typeof runs[i]?.plan === "string" ? runs[i].plan : r.plan,
    })),
    trace: traceOk ? trace : null,
  };
}
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Could not complete the operation.";
/** Group type follows every current saved member; read-only belongs to the
 * member being used. A failed list read or changed endpoint stays conservative
 * through connectionSafety, just like a plain saved connection. */
function currentGroupSafety(active: ActiveConnection, group: GroupConnection, profiles: Profile[] | null): Safety {
  const safety = connectionSafety(active, profiles);
  const members = profiles?.filter((p) => (p.group || "").trim() === group.group_name);
  return {
    ...safety,
    type: safety.endpointMatches && members?.length && members.every((p) => p.type === "development")
      ? "development" : "production",
  };
}
function App() {
  // Width mode and expansion change together. Separate media listeners in
  // the shell and Sidebar could briefly turn the desktop sidebar into an
  // open modal drawer on resize, stealing focus from a chart or inspector.
  const [sidebar, setSidebar] = useState(() => ({
    open: window.innerWidth > 900,
    narrow: window.innerWidth <= 900,
  }));
  const sidebarOpen = sidebar.open;
  const setSidebarOpen = (open: boolean) => setSidebar((current) => ({ ...current, open }));
  useEffect(() => {
    const narrow = window.matchMedia("(max-width: 900px)");
    const changed = () => setSidebar({ open: !narrow.matches, narrow: narrow.matches });
    narrow.addEventListener("change", changed);
    return () => narrow.removeEventListener("change", changed);
  }, []);
  const chrome = useRef<HTMLDivElement>(null);
  const [placeActionsWidth, setPlaceActionsWidth] = useState(0);
  const [replicationLoops, setReplicationLoops] = useState<Record<string, Bound<SkipLoop[]>>>({});
  const [loopRevision, setLoopRevision] = useState(0);
  const [restoredMembers, setRestoredMembers] = useState<{
    reset: number;
    activeKeys: string[];
    stoppedKeys: string[];
    outsideGroup: { id: string; name: string; group: string }[];
    anyFailed: boolean;
  } | null>(null);
  const restoredMembersPolled = useRef(-1);
  const unreachedRechecked = useRef(new Set<string>());
  const [otherLoopErrors, setOtherLoopErrors] = useState<Record<string, string>>({});
  const [pollerStaleEndpoints, setPollerStaleEndpoints] = useState<string[]>([]);
  const publishReplicationLoops = useCallback((next: Bound<SkipLoop[]>) => {
    setReplicationLoops((current) => {
      const endpoint = endpointKey(next.key);
      const updated: Record<string, Bound<SkipLoop[]>> = {};
      for (const [k, v] of Object.entries(current)) {
        // Replace any older key for the same endpoint (e.g. a safety/type edit
        // on the same saved connection) while keeping every other member's slot.
        if (k !== next.key && endpointKey(k) === endpoint) continue;
        updated[k] = v;
      }
      updated[next.key] = next;
      return updated;
    });
    setLoopRevision((n) => n + 1);
  }, []);
  const [plans, setPlans] = useState<(Plan & { origin?: "sample" })[]>([]);
  const plansInUse = useRef(plans);
  plansInUse.current = plans;
  const [activeId, setActiveId] = useState("");
  const active = plans.find((p) => p.id === activeId);
  const [mode, setMode] = useState<Mode>("home");
  /** The investigation notebook sheet, opened by Save investigation. */
  const [notebookOpen, setNotebookOpen] = useState(false);
  const [savedRevision, setSavedRevision] = useState(0);
  const [selectedInvestigationId, setSelectedInvestigationId] = useState("");
  // Plans remembers whether it was exploring or comparing.
  const [plansView, setPlansView] = useState<"inspect" | "compare">("inspect");
  useEffect(() => {
    if (mode === "inspect" || mode === "compare") setPlansView(mode);
    setNotebookOpen(false);
  }, [mode]);
  const [view, setView] = useState<View>("workbench");
  const [metric, setMetric] = useState("self_time");
  const [selectedId, setSelectedId] = useState("");
  const [statsIds, setStatsIds] = useState<string[]>([]);
  const [hideLiterals, setHideLiterals] = useState(true);
  const [metricsTarget, setMetricsTarget] = useState<HTMLDivElement | null>(null);
  const [focusId, setFocusId] = useState("");
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [working, setBusy] = useState(false);
  const sampleAction = useAction();
  const saveAction = useAction();
  const busy = working || saveAction.pending;
  const exportAction = useAction();
  const exporting = exportAction.pending;
  const exportRetry = useRef<(() => void) | null>(null);
  const [search, setSearch] = useState("");
  // The plan header's Misestimates chip: which operator the chart should
  // bring into view (n counts presses) and the announcement of the last one.
  const [reveal, setReveal] = useState({ id: "", n: 0 });
  const [misestimateNote, setMisestimateNote] = useState<ReactNode>("");
  const [misestimateNotShown, setMisestimateNotShown] = useState(false);
  const [beforeId, setBeforeId] = useState("");
  const [afterId, setAfterId] = useState("");
  const before = plans.find((p) => p.id === beforeId),
    after = plans.find((p) => p.id === afterId);
  // The comparison and the before/after plan ids it was computed for: a
  // result is shown or exported only for the pair on screen.
  const [compared, setCompared] = useState<{
    before: string;
    after: string;
    result: Comparison;
  } | null>(null);
  const comparison =
    compared && compared.before === beforeId && compared.after === afterId
      ? compared.result
      : null;
  const [pair, setPair] = useState<[string, string]>(["", ""]);
  const [name, setName] = useState("Untitled investigation");
  const [notes, setNotes] = useState("");
  const [tags, setTags] = useState("");
  const [savedId, setSavedId] = useState<string | undefined>();
  const [baseline, setBaseline] = useState("");
  const [dirty, setDirty] = useState(false);
  // Reload protection is separate from investigation edits. Set immediately
  // on a content change, before React or the debounced saver can catch up.
  const unsavedWorkspaceContent = useRef(false);
  // Plans of the saved workspace that could not be reopened: kept in the
  // autosaved snapshot (so they are not lost) until downloaded or removed.
  const [held, setHeld] = useState<SessionPlan[]>([]);
  // The query tabs as last restored or reset: with autosave off, edits since
  // then are unsaved work.
  const tabsBaseline = useRef(tabsText(defaultLab().tabs));
  const [lesson, setLesson] = useState("");
  const [returnToPlan, setReturnToPlan] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  const importLock = useRef(false);
  // Session restore (see session.ts). Nothing renders in <main> until the
  // stored session has been read, so the Query editor, Server, and Learn mount restored.
  const [ready, setReady] = useState(false);
  const [resetKey, setResetKey] = useState(0);
  // One Replication report for the Replication place and Server's Refresh all
  // (a reset, such as Start fresh, drops it like it drops Server's own state).
  const [replicationShared, setReplicationShared] = useState<{ reset: number; report: Bound<ReplicationReport> | null }>({ reset: 0, report: null });
  const resetKeyRef = useRef(resetKey);
  resetKeyRef.current = resetKey;
  const replicationReport = replicationShared.reset === resetKey ? replicationShared.report : null;
  const shareReplication = useCallback(
    (report: Bound<ReplicationReport>) => setReplicationShared({ reset: resetKeyRef.current, report }),
    [],
  );
  // The Cluster place's report, shared the same way: Server's Refresh all
  // reads it too, and the sidebar names its state from it.
  // A failed read (the place's Refresh or Refresh all) is kept with the
  // report it failed over, so that report shows as the last one, greyed,
  // until a read succeeds through either path.
  const [clusterShared, setClusterShared] = useState<{ reset: number; report: Bound<ClusterReport> | null; failure?: ClusterFailure }>({ reset: 0, report: null });
  const clusterReport = clusterShared.reset === resetKey ? clusterShared.report : null;
  const clusterFailure = clusterShared.reset === resetKey ? clusterShared.failure || null : null;
  const shareCluster = useCallback(
    (report: Bound<ClusterReport>) => setClusterShared({ reset: resetKeyRef.current, report }),
    [],
  );
  const failCluster = useCallback(
    (key: string, message: string) => setClusterShared((s) => {
      const report = s.reset === resetKeyRef.current && s.report?.key === key ? s.report : null;
      return { reset: resetKeyRef.current, report, failure: { key, message, over: report } };
    }),
    [],
  );
  const [proxyShared, setProxyShared] = useState<{ reset: number; report: Bound<ProxySqlReport> | null; failure?: ProxySqlFailure }>({ reset: 0, report: null });
  const proxyReport = proxyShared.reset === resetKey ? proxyShared.report : null;
  const proxyFailure = proxyShared.reset === resetKey ? proxyShared.failure || null : null;
  const shareProxySql = useCallback(
    (report: Bound<ProxySqlReport>) => setProxyShared({ reset: resetKeyRef.current, report }),
    [],
  );
  const failProxySql = useCallback(
    (key: string, message: string) => setProxyShared((s) => {
      const report = s.reset === resetKeyRef.current ? s.report : null;
      return { reset: resetKeyRef.current, report, failure: { key, message, over: report } };
    }),
    [],
  );
  const [readysetShared, setReadysetShared] = useState<{ reset: number; report: Bound<ReadysetReport> | null; failure?: ReadysetFailure }>({ reset: 0, report: null });
  const readysetReport = readysetShared.reset === resetKey ? readysetShared.report : null;
  const readysetFailure = readysetShared.reset === resetKey ? readysetShared.failure || null : null;
  const shareReadyset = useCallback(
    (report: Bound<ReadysetReport>) => setReadysetShared({ reset: resetKeyRef.current, report }),
    [],
  );
  const failReadyset = useCallback(
    (key: string, message: string) => setReadysetShared((s) => {
      const report = s.reset === resetKeyRef.current ? s.report : null;
      return { reset: resetKeyRef.current, report, failure: { key, message, over: report } };
    }),
    [],
  );
  const [labInitial, setLabInitial] = useState<LabState | undefined>();
  const [labState, setLabState] = useState<LabState>(defaultLab);
  const [teachLesson, setTeachLesson] = useState("");
  const [saveStatus, setSaveStatus] = useState<SaveStatus>({ kind: "loading" });
  const [restoreEnabled, setRestoreEnabled] = useState(true);
  const [skipRestore, setSkipRestore] = useState(false);
  const [restored, setRestored] = useState<{
    text: string;
    fresh: boolean;
  } | null>(null);
  const [confirmFresh, setConfirmFresh] = useState(false);
  const pendingView = useRef<{
    planId: string;
    selected: string;
    focus: string;
    collapsed: string[];
  } | null>(null);
  const conflictRef = useRef<() => void>(() => {});
  const saverRef = useRef<SessionSaver | null>(null);
  if (!saverRef.current)
    saverRef.current = new SessionSaver(
      (next) =>
        setSaveStatus((prev) =>
          prev.kind === next.kind &&
          ("at" in prev ? prev.at : "") === ("at" in next ? next.at : "") &&
          ("message" in prev ? prev.message : "") === ("message" in next ? next.message : "")
            ? prev
            : next,
        ),
      () => conflictRef.current(),
    );
  const saver = saverRef.current;
  /** Merge part of the restorable lab state (editor, Server tabs, or the
   * connection in use); unchanged state keeps its identity. */
  const patchLab = useCallback((patch: Partial<LabState>) => {
    setLabState((current) => {
      const next = normalizeLab({ ...current, ...patch });
      return sameLab(current, next) ? current : next;
    });
  }, []);
  const reportEditor = useCallback(
    (editor: EditorState) => patchLab(editor),
    [patchLab],
  );
  const reportSlow = useCallback(
    (slow: SlowState) => setLabState((current) => {
      const members = current.slow?.replication_loop_active
        ? loopMemberIds(current.slow.replication_loop_members, current.slow.replication_loop_member)
        : [];
      const merged: SlowState = {
        ...slow,
        ...(current.slow?.replication_loop_active ? { replication_loop_active: true } : {}),
        ...(members.length
          ? { replication_loop_members: members, replication_loop_member: members[0] }
          : {}),
      };
      const next = normalizeLab({ ...current, slow: merged });
      return sameLab(current, next) ? current : next;
    }),
    [],
  );
  const syncLoopSession = useCallback((active: boolean, members: string[] = []) => {
    setLabState((current) => {
      const slow: SlowState = { ...(current.slow || { kind: "health" }) };
      if (active) {
        const ids = [...new Set(members.map((id) => id.slice(0, 200)).filter(Boolean))].slice(0, 64);
        slow.replication_loop_active = true;
        if (ids.length) {
          slow.replication_loop_members = ids;
          slow.replication_loop_member = ids[0];
        } else {
          delete slow.replication_loop_members;
          delete slow.replication_loop_member;
        }
      } else {
        delete slow.replication_loop_active;
        delete slow.replication_loop_members;
        delete slow.replication_loop_member;
      }
      const next = normalizeLab({ ...current, slow });
      return sameLab(current, next) ? current : next;
    });
  }, []);
  // Column widths and hidden columns of the workspace's tables by table id
  // (Server's views and the Cluster place), one record for both so each
  // table keeps its own layout and it is saved with the workspace
  // (slow.columns_v1, validated by session.ts).
  const [columnLayouts, setColumnLayouts] = useState<Record<string, ColumnPrefs>>({});
  const columnStore = useColumnStore(columnLayouts, setColumnLayouts);
  useEffect(() => {
    setLabState((current) => {
      const slow = { ...current.slow };
      if (Object.keys(columnLayouts).length) slow.columns_v1 = columnLayouts;
      else delete slow.columns_v1;
      const next = normalizeLab({ ...current, slow });
      return sameLab(current, next) ? current : next;
    });
  }, [columnLayouts]);
  // Saved connections and the connection in use. Its non-secret settings
  // live in the lab state (and so in the saved session); the password is
  // typed for this page only. The list is the window's one shared list
  // (savedConnections.ts): every picker reads it, and it is read again after
  // each change made here and when the window comes back.
  const savedList = useSavedConnections(false);
  const profiles = savedList.items;
  const profilesRef = useRef(profiles);
  profilesRef.current = profiles;
  // The type of a saved connection is known once the list has loaded, and
  // not known again while the latest read of it failed.
  const profilesLoaded = savedList.loaded;
  const profilesFailed = savedList.error;
  const profilesStatusRef = useRef({ loaded: profilesLoaded, failed: profilesFailed });
  profilesStatusRef.current = { loaded: profilesLoaded, failed: profilesFailed };
  const secretStore = savedList.secretStore;
  const [activePassword, setActivePassword] = useState("");
  const [connectionNote, setConnectionNote] = useState("");
  const [connectRevision, setConnectRevision] = useState(0);
  const [passwordRevision, setPasswordRevision] = useState(0);
  const [labBusy, setLabBusy] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const labRef = useRef<QueryLabHandle | null>(null);
  // Calls to the Query editor made before its chunk has loaded (lazy.tsx)
  // wait for it instead of being dropped.
  const labWaiting = useRef<((lab: QueryLabHandle) => void)[]>([]);
  const withLab = useCallback((call: (lab: QueryLabHandle) => void) => {
    if (labRef.current) call(labRef.current);
    else labWaiting.current.push(call);
  }, []);
  const setLab = useCallback((lab: QueryLabHandle | null) => {
    labRef.current = lab;
    if (lab) for (const call of labWaiting.current.splice(0)) call(lab);
  }, []);
  const labStateRef = useRef(labState);
  labStateRef.current = labState;
  // One read at a time wins (savedConnections.ts); callers await the latest
  // read, so a write-policy check or the restored-connection check below
  // never acts on stale connection settings.
  const refreshProfiles = refreshSavedConnections;
  // The saved connection in use may be gone: the one a restored session names
  // (checked after the first read), or one deleted later in another window or
  // with the CLI (seen when the list is read again). Its settings stay as an
  // unsaved connection, and the picker says what happened. A deletion made
  // here, in Connections, clears the connection itself (onActiveDeleted).
  const restoreChecked = useRef(false);
  useEffect(() => {
    if (!ready) return;
    restoreChecked.current = false;
    refreshProfiles()
      .then((items) => {
        restoreChecked.current = true;
        // A restored connection may have been deleted since the last session.
        const id = labStateRef.current.profile_id;
        if (id && !items.some((p) => p.id === id)) {
          patchLab({ profile_id: "" });
          setConnectionNote(
            "The connection from your last session no longer exists. Its settings are kept as an unsaved connection.",
          );
        }
      })
      .catch((e) => {
        // A later read that succeeds checks the connection in use instead.
        restoreChecked.current = true;
        setError(message(e));
      });
  }, [ready, resetKey, refreshProfiles, patchLab]);
  useEffect(() => {
    if (!ready || !restoreChecked.current || !profilesLoaded || profilesFailed) return;
    const id = labStateRef.current.profile_id;
    if (!id || profiles.some((p) => p.id === id)) return;
    const name = labStateRef.current.connection.name;
    patchLab({ profile_id: "" });
    setConnectionNote(
      `${name ? `The saved connection “${name}”` : "The saved connection in use"} was deleted. Its settings are kept as an unsaved connection.`,
    );
  }, [ready, profiles, profilesLoaded, profilesFailed, patchLab]);
  // A Group connection (a connection group opened as one connection, POST
  // /api/connect): its members, their probed state, and the
  // active member. In-memory only; a reload restores the active member as a
  // plain connection.
  const [groupConnection, setGroupConnection] = useState<GroupConnection | null>(null);
  // The group open in the group editor sheet (picker, status bar).
  const [groupEditor, setGroupEditor] = useState<string | null>(null);
  // The group editor in create mode, with these saved connections ticked
  // (Topology's New group...).
  const [groupCreate, setGroupCreate] = useState<string[] | null>(null);
  const [groupRevision, setGroupRevision] = useState(0);
  // Any explicit connection choice invalidates an older group probe.
  const connectionChoice = useRef(0);
  function applyConnection(id: string, fields: SessionConnection, password: string, group: GroupConnection | null = null) {
    connectionChoice.current += 1;
    if (!group) {
      setMemberByPlace({});
      setMemberNote("");
    }
    recordConnectionUse(id);
    patchLab({ profile_id: id, connection: fields });
    setActivePassword(password);
    setGroupConnection(group);
    // Use connection is an explicit credential choice, including when Home
    // supplies a new password for the same endpoint. Reconnect is not.
    setPasswordRevision(value => value + 1);
    setConnectionNote("");
  }
  function switchConnection(id: string) {
    const p = profiles.find((item) => item.id === id);
    if (p) applyConnection(p.id, connectionFields(p), "");
  }
  /** Open a connection group as one Group connection (an explicit open or
   * Refresh probes the other members). Starts on the member the backend
   * picks (the primary or source) unless *activeMember* names one. Resolves
   * true only when this group became the connection in use. */
  async function openGroup(name: string, options: { activeMember?: string; probe?: boolean } = {}): Promise<boolean> {
    const choice = ++connectionChoice.current;
    const probe = options.probe !== false;
    setConnectionNote("");
    try {
      const result = await openConnectionGroup(name, { activeMember: options.activeMember, probe });
      if (choice !== connectionChoice.current) return false;
      const gc = result.group_connection;
      if (groupActiveRef.current?.group_name !== gc.group_name) {
        setMemberByPlace({});
        setMemberNote("");
      }
      const member = gc.members.find((m) => m.profile_id === gc.active_member_profile_id);
      const profile = profilesRef.current.find((p) => p.id === gc.active_member_profile_id);
      const fields = profile
        ? connectionFields(profile)
        : connectionFields(member ? { name: member.profile_name, host: member.host, port: member.port, user: "", type: member.safety.type, read_only: member.safety.read_only } : {});
      applyConnection(gc.active_member_profile_id, fields, "", gc);
      setConnectionNote(
        `${groupConnectionLabel(gc.group_name, gc.members.length)}: ${member?.profile_name || gc.active_member_profile_id} is the active member.`,
      );
      return true;
    } catch (e) {
      if (choice !== connectionChoice.current) return false;
      setConnectionNote(`Could not open group ${name}: ${(e as Error).message}`);
      return false;
    }
  }
  /** After a group edit: refresh the saved connections and move an active
   * Group connection (rename, or the active member leaving the group). The
   * same re-open applies when the saved group is another one (a create has no
   * previousName) that took a member of the active Group connection: that
   * group lost the member and shows it again without probing. */
  async function groupSaved(result: GroupSaveResult, previousName: string) {
    const current = groupActiveRef.current;
    const movedOut = !!current && current.group_name !== result.name &&
      result.profile_ids.some((id) => current.members.some((m) => m.profile_id === id));
    const affected = !!current && (current.group_name === previousName || movedOut);
    const choice = affected ? ++connectionChoice.current : connectionChoice.current;
    const fresh = await refreshProfiles().catch(() => null);
    if (!current || !affected || choice !== connectionChoice.current) return;
    // The group to open again: the edited one (its name after a rename), or
    // the active one another group's save took a member from.
    const name = current.group_name === previousName ? result.name : current.group_name;
    try {
      const stillMember = name === result.name
        ? result.profile_ids.includes(labStateRef.current.profile_id)
        : (fresh || profilesRef.current).some((p) => p.id === labStateRef.current.profile_id && (p.group || "").trim() === name);
      const reopened = await openConnectionGroup(name, {
        activeMember: stillMember ? labStateRef.current.profile_id : undefined,
        probe: false,
      });
      const gc = reopened.group_connection;
      if (choice !== connectionChoice.current) return;
      const member = gc.members.find((m) => m.profile_id === gc.active_member_profile_id);
      const profile = (fresh || profilesRef.current).find((p) => p.id === gc.active_member_profile_id);
      const fields = profile
        ? connectionFields(profile)
        : connectionFields(member ? { name: member.profile_name, host: member.host, port: member.port, user: "", type: member.safety.type, read_only: member.safety.read_only } : {});
      // Editing the group does not discard the active member's session
      // password, but only while the backend kept the same member active: a
      // save that moves the Group connection clears it with the member.
      const password = stillMember && gc.active_member_profile_id === labStateRef.current.profile_id
        ? activeRef.current.password
        : "";
      // A rename keeps the per-place choices; removal is handled below.
      applyConnection(gc.active_member_profile_id, fields, password, gc);
      setGroupRevision((revision) => revision + 1);
      if (!stillMember) {
        setConnectionNote(`The active member left the group. The Group connection moved to ${member?.profile_name || gc.active_member_profile_id}.`);
      }
    } catch (e) {
      if (choice !== connectionChoice.current) return;
      // The group is gone or has no member left: the connection in use stays
      // as the plain member connection.
      setGroupConnection(null);
      setConnectionNote(`Group ${name} could not be opened again: ${(e as Error).message}`);
    }
  }
  /** After a group delete: its saved connections stay; an active Group
   * connection becomes the plain member connection. */
  async function groupDeleted(name: string) {
    const choice = groupActiveRef.current?.group_name === name ? ++connectionChoice.current : connectionChoice.current;
    await refreshProfiles().catch(() => {});
    if (groupActiveRef.current?.group_name !== name || choice !== connectionChoice.current) return;
    setGroupConnection(null);
    setMemberByPlace({});
    setMemberNote("");
    setConnectionNote(`Group ${name} was deleted. Its saved connections stay; the active member stays in use as a plain connection.`);
  }
  // The member each single-server place works on, by place ("server",
  // "query", "readyset", "proxysql", "cluster", "replication"): chosen in the
  // place's Server: control, an explicit open of that member's saved
  // connection, or a Topology map card. Absent entries mean the primary or
  // source (the active member the backend picked). Never saved; a reload
  // starts there.
  const [memberByPlace, setMemberByPlace] = useState<Record<string, string>>({});
  // A group edit removed a member a place was on: its views moved to the
  // primary and this note says so until the next navigation or explicit
  // choice (go() and chooseMember clear it). The "Using <member>" note is not
  // stored: it is derived per place from the request the place reads
  // (placeMemberNote below), so it can never name another member, and the two
  // notes show together when both apply.
  const [memberNote, setMemberNote] = useState("");
  /** The member's saved connection as a request connection (its profile id,
   * its own endpoint, the group's strictest safety), with the group name so
   * the backend re-checks membership at every login. Null for a non-member. */
  function memberRequest(id: string): Connection | null {
    const group = groupActiveRef.current;
    const member = group?.members.find((m) => m.profile_id === id);
    if (!group || !member) return null;
    // Keep this member's typed password and exact policy when it is active.
    if (id === activeRef.current.id) return requestRef.current;
    const profile = profilesRef.current.find((p) => p.id === id);
    const fields = profile
      ? connectionFields(profile)
      : connectionFields({ name: member.profile_name, host: member.host, port: member.port, user: "", type: member.safety.type, read_only: member.safety.read_only });
    const active = { id, fields, password: "" };
    const c = requestConnection(active, currentGroupSafety(active, group,
      profilesStatusRef.current.loaded && !profilesStatusRef.current.failed ? profilesRef.current : null));
    c.group_name = group.group_name;
    c.name = `${groupConnectionLabel(group.group_name, group.members.length)} · ${profile?.name || member.profile_name}`;
    return c;
  }
  /** Rebuild a connection for *id* when it is a member of the active group, or
   * (after View > Reload, before the group is opened again) a saved connection
   * in the same saved group as the active connection. Null outside that group. */
  const loopMemberRequest = useCallback((id: string): Connection | null => {
    if (!id) return null;
    if (groupActiveRef.current) return memberRequest(id);
    if (id === activeRef.current.id) return requestRef.current;
    if (!profilesStatusRef.current.loaded || profilesStatusRef.current.failed) return null;
    const list = profilesRef.current;
    const currentProfile = list.find((p) => p.id === activeRef.current.id);
    const groupName = (currentProfile?.group || "").trim();
    if (!groupName) return null;
    const targetProfile = list.find((p) => p.id === id);
    if (!targetProfile || (targetProfile.group || "").trim() !== groupName) return null;
    const members = list.filter((p) => (p.group || "").trim() === groupName);
    const fields = connectionFields(targetProfile);
    const active = { id: targetProfile.id, fields, password: "" };
    const baseSafety = connectionSafety(active, list);
    const c = requestConnection(active, {
      ...baseSafety,
      type: baseSafety.endpointMatches && members.length && members.every((p) => p.type === "development")
        ? "development" : "production",
    });
    c.group_name = groupName;
    c.name = `${groupConnectionLabel(groupName, members.length)} · ${targetProfile.name}`;
    return c;
  }, []);
  function selectOperator(id: string) {
    setStatsIds([]);
    const ancestors = new Set<string>();
    let node = active?.operators.find((op) => op.node_id === id);
    while (node) {
      ancestors.add(node.node_id);
      node = active?.operators.find((op) => op.node_id === node?.parent_id);
    }
    if (focusId && !ancestors.has(focusId)) setFocusId("");
    setCollapsed((ids) => {
      const next = ids.filter((key) => key === id || !ancestors.has(key));
      return next.length === ids.length ? ids : next;
    });
    setSelectedId(id);
  }
  useEffect(() => {
    // A restored selection applies once its plan is active; any other plan
    // change starts with a clean selection.
    const restoredView = pendingView.current;
    pendingView.current = null;
    const keep = restoredView?.planId === activeId;
    setStatsIds([]);
    setSelectedId(keep ? restoredView.selected : "");
    setFocusId(keep ? restoredView.focus : "");
    setCollapsed(keep ? restoredView.collapsed : []);
    setSearch("");
  }, [activeId]);
  useEffect(() => {
    if (mode !== "compare" || !before || !after || before.id === after.id) {
      setCompared(null);
      return;
    }
    const controller = new AbortController();
    const ids = { before: before.id, after: after.id };
    // The previous pair's result is not shown while this one is computed or
    // when it fails.
    setCompared(null);
    setLoading(true);
    setError("");
    setPair(["", ""]);
    // Both charts are drawn in the page from each plan's chart data.
    api<Comparison>(
      "compare",
      { before: before.raw, after: after.raw },
      controller.signal,
    )
      .then((diff) => {
        if (!controller.signal.aborted) setCompared({ ...ids, result: diff });
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [mode, before, after]);
  // A chart selection becomes the page selection (inspect) or the matched
  // before/after pair (compare); both charts then follow the pair.
  function chartSelect(id: string) {
    setStatsIds([]);
    if (active?.operators.some((n) => n.node_id === id)) {
      setSelectedId(id);
      setSearch("");
    }
  }
  function compareSelect(side: "before_node_id" | "after_node_id") {
    return (id: string) => {
      const match = comparison?.analysis.deltas.find((d) => d[side] === id);
      if (match) setPair([match.before_node_id || "", match.after_node_id || ""]);
    };
  }
  const unsaved = useRef({ dirty, tabs: labState.tabs });
  unsaved.current = { dirty, tabs: labState.tabs };
  // The editor's tabs as of the latest keystroke (QueryLab hands them over
  // in the same task as the edit); labState follows a task later.
  const editorTabs = useRef<LabState["tabs"] | null>(null);
  const tabsNow = useCallback((tabs: LabState["tabs"]) => {
    editorTabs.current = tabs;
  }, []);
  useEffect(() => {
    // With autosave working, the workspace survives a reload or restart, so
    // warn about imported/captured/closed plans until their save is confirmed,
    // and, with autosave off, investigation changes or edited query tabs.
    // Also warn when a save failed, or
    // when changes are still unconfirmed and too large for the request a
    // closing page may still send (keepalive).
    const atRisk = () => {
      const { dirty: changed, tabs } = unsaved.current;
      // Serialized only here, when the page is about to unload.
      const edited = () => tabsText(editorTabs.current || tabs) !== tabsBaseline.current;
      const offAndEdited = !saver.active && (changed || edited());
      return unsavedWorkspaceContent.current || offAndEdited || saver.failed || saver.atRisk();
    };
    const guard = (event: BeforeUnloadEvent) => {
      if (atRisk()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", guard);
    // Restart to update asks with the same rule before the desktop app closes the window.
    setUnsavedWorkCheck(atRisk);
    return () => {
      window.removeEventListener("beforeunload", guard);
      setUnsavedWorkCheck(null);
    };
  }, [saver]);
  // The restorable snapshot. Plans are serialized only when they change;
  // plans that could not be reopened are kept as they were stored.
  const plansJson = useMemo(
    () => serializePlans([...plans, ...held]),
    [plans, held],
  );
  const snapshot = useMemo(
    () =>
      serializeSession(
        {
          mode,
          view,
          metric,
          active_plan_id: activeId,
          selected_id: selectedId,
          focus_id: focusId,
          collapsed_ids: collapsed,
          before_id: beforeId,
          after_id: afterId,
          investigation: {
            id: savedId || "",
            name,
            notes,
            tags,
            baseline_id: baseline,
            dirty,
          },
          teach: { lesson: teachLesson, return_to_plan: returnToPlan },
          query_lab: labState,
        },
        plansJson,
      ),
    [
      mode,
      view,
      metric,
      activeId,
      selectedId,
      focusId,
      collapsed,
      beforeId,
      afterId,
      savedId,
      name,
      notes,
      tags,
      baseline,
      dirty,
      teachLesson,
      returnToPlan,
      labState,
      plansJson,
    ],
  );
  // View/selection changes still restore, but are not user-authored content.
  // Keep this signature separate so navigation never flashes a saving warning.
  const edits = useMemo(() => sessionEdits({
    investigation: { id: savedId || "", name, notes, tags, baseline_id: baseline, dirty },
    query_lab: labState,
  }, plansJson), [plansJson, savedId, name, notes, tags, baseline, dirty, labState.tabs]);
  // QueryLab publishes in a layout effect. Finish handing its snapshot to
  // the saver in that same commit, before beforeunload/pagehide can run.
  useLayoutEffect(() => {
    if (ready) saver.update(snapshot, edits);
  }, [snapshot, edits, ready, saver]);
  useEffect(() => {
    // The status can say "saved" while a silent save is still pending. Only
    // a confirmed copy of this snapshot makes its workspace content safe.
    if (ready && saver.active && saver.isSaved(snapshot))
      unsavedWorkspaceContent.current = false;
  }, [snapshot, saveStatus, ready, saver]);
  useEffect(() => {
    // Save right away when the page is hidden or closed: keepalive lets a
    // small request finish after the page is gone.
    const hidden = () => {
      if (document.visibilityState === "hidden") void saver.flush(true);
    };
    const leave = () => void saver.flush(true);
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", leave);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", leave);
    };
  }, [saver]);
  useEffect(() => {
    loadSession()
      .then(async (session) => {
        saver.revision = session.revision;
        setRestoreEnabled(session.restore);
        setSkipRestore(session.skip_restore);
        if (session.skip_restore) {
          saver.disable("Autosave is off for this launch");
          return;
        }
        if (!session.restore) {
          saver.disable("Autosave is off");
          return;
        }
        const baseline = session.state
          ? await applySession(session.state, session.saved_at, "startup")
          : blankSnapshot();
        saver.enable(baseline);
      })
      .catch((e) => {
        // Without the stored revision, saving could overwrite newer work.
        saver.disable("");
        setSaveStatus({
          kind: "error",
          reason: `could not read the saved workspace (${message(e)})`,
        });
        saver.failed = true;
      })
      .finally(() => setReady(true));
  }, []);
  const analyze = async (raw: string, capture?: Capture, signal?: AbortSignal) =>
    api<
      Pick<Plan, "analysis" | "operators" | "query" | "teach_hooks" | "capture">
    >("analyze", { plan: raw, capture }, signal);
  /**
   * Analyze and add a plan. `open: false` adds it without leaving the
   * current screen (Explain top N). `wait: true` queues behind an import in
   * progress instead of ignoring the request (captures must not be lost).
   */
  async function importPlan(
    raw: string,
    planName: string,
    capture?: Capture,
    options: { open?: boolean; wait?: boolean; cancelled?: () => boolean; origin?: "sample"; signal?: AbortSignal } = {},
  ): Promise<Plan | undefined> {
    if (importLock.current) {
      if (!options.wait) return undefined;
      while (importLock.current)
        await new Promise((resolve) => window.setTimeout(resolve, 50));
    }
    // The caller gave up (Explain top N cancelled): add nothing.
    if (options.cancelled?.()) return undefined;
    const open = options.open !== false;
    importLock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await analyze(raw, capture, options.signal);
      if (options.signal?.aborted || options.cancelled?.()) return undefined;
      const plan: Plan & { origin?: "sample" } = {
        ...result,
        raw,
        name: planName,
        id: crypto.randomUUID(),
        origin: options.origin,
        capture: result.capture || undefined,
      };
      const hasOtherPlan = plansInUse.current.length > 0;
      setPlans((ps) => [...ps, plan]);
      setBeforeId((id) => id || plan.id);
      // A queued import may have started before another plan finished opening.
      setAfterId(hasOtherPlan ? plan.id : "");
      setBaseline((id) => id || plan.id);
      if (open) {
        setActiveId(plan.id);
        setMode("inspect");
        setImportOpen(false);
      } else setActiveId((id) => id || plan.id);
      // Opening a read-only plan is workspace content, not an investigation edit.
      if (options.origin !== "sample") unsavedWorkspaceContent.current = true;
      return plan;
    } catch (e) {
      if (options.origin !== "sample" && !options.signal?.aborted) setError(message(e));
      throw e;
    } finally {
      setBusy(false);
      importLock.current = false;
    }
  }
  const capture = useCallback(
    (result: Capture, planName: string, options?: { open?: boolean; cancelled?: () => boolean }) =>
      importPlan(result.raw, planName, result, { ...options, wait: true }),
    [],
  );
  const openPlan = useCallback((id: string) => {
    setActiveId(id);
    setMode("inspect");
  }, []);
  async function openBundle(bundle: Bundle) {
    if (
      bundle.schema_version !== "investigation-1.0" ||
      !Array.isArray(bundle.plans) ||
      bundle.plans.length > 30
    )
      throw new Error("Choose a myflames investigation bundle.");
    const parsed = await Promise.all(
      bundle.plans.map(async (p) => ({
        ...p,
        ...(await analyze(p.raw, p.capture)),
      })),
    );
    // Opening another saved investigation keeps unsaved work available as a bundle.
    if (dirty && plans.length) backupUnsaved();
    setPlans(parsed);
    setActiveId(parsed[0]?.id || "");
    setBaseline(bundle.baseline_id);
    const beforePlanId = bundle.baseline_id || parsed[0]?.id || "";
    setBeforeId(beforePlanId);
    setAfterId(parsed.find((p) => p.id !== beforePlanId)?.id || "");
    setName(bundle.name);
    setNotes(bundle.notes);
    setTags(bundle.tags.join(", "));
    setSavedId(bundle.id);
    setDirty(false);
    unsavedWorkspaceContent.current = false;
    setMode("inspect");
    setImportOpen(false);
  }
  async function importFile(f?: File) {
    if (!f) return;
    setError("");
    try {
      if (f.size > 10 * 1024 * 1024)
        throw new Error("Choose a file smaller than 10 MiB.");
      const raw = await f.text();
      let obj;
      try {
        obj = JSON.parse(raw);
      } catch {
        /* parser diagnoses JSON */
      }
      if (obj?.schema_version === "investigation-1.0") await openBundle(obj);
      else if (obj?.schema_version === "capture-1.0")
        await importPlan(obj.raw, f.name, obj);
      else await importPlan(raw, f.name);
    } catch (e) {
      setError(message(e));
    }
  }
  function makeBundle(): Bundle {
    return {
      schema_version: "investigation-1.0",
      name,
      notes,
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      baseline_id: baseline,
      plans: plans.map((p) => ({
        id: p.id,
        name: p.name,
        raw: p.raw,
        capture: p.capture,
        notes: p.notes,
      })),
    };
  }
  const currentInvestigation = useRef(() => ({ id: savedId, value: makeBundle() }));
  currentInvestigation.current = () => ({ id: savedId, value: makeBundle() });
  function backupUnsaved() {
    download(
      JSON.stringify(makeBundle(), null, 2),
      "unsaved-investigation.json",
      "application/json",
    );
  }
  /** The snapshot of an empty workspace, as App serializes it after a reset. */
  function blankSnapshot() {
    return serializeSession(
      {
        mode: "home",
        view: "workbench",
        metric: "self_time",
        active_plan_id: "",
        selected_id: "",
        focus_id: "",
        collapsed_ids: [],
        before_id: "",
        after_id: "",
        investigation: {
          id: "",
          name: "Untitled investigation",
          notes: "",
          tags: "",
          baseline_id: "",
          dirty: false,
        },
        teach: { lesson: "", return_to_plan: false },
        query_lab: defaultLab(),
      },
      "[]",
    );
  }
  function resetWorkspace() {
    pendingView.current = null;
    setPlans([]);
    setActiveId("");
    setMode("home");
    setPlansView("inspect");
    setView("workbench");
    setMetric("self_time");
    setSelectedId("");
    setFocusId("");
    setCollapsed([]);
    setSearch("");
    setBeforeId("");
    setAfterId("");
    setCompared(null);
    setName("Untitled investigation");
    setNotes("");
    setTags("");
    setSavedId(undefined);
    setBaseline("");
    setDirty(false);
    unsavedWorkspaceContent.current = false;
    setHeld([]);
    tabsBaseline.current = tabsText(defaultLab().tabs);
    setLesson("");
    setReturnToPlan(false);
    setLabInitial(undefined);
    setLabState(defaultLab());
    setReplicationLoops({});
    setLoopRevision(0);
    setRestoredMembers(null);
    restoredMembersPolled.current = -1;
    unreachedRechecked.current.clear();
    setOtherLoopErrors({});
    setPollerStaleEndpoints([]);
    setColumnLayouts({});
    setActivePassword("");
    setPasswordRevision(value => value + 1);
    setConnectionNote("");
    setTeachLesson("");
    setResetKey((k) => k + 1);
    // Server mounts again on its next visit: it must not replay an old link.
    setServerNavigation(undefined);
    setServerLink(null);
    setInstrumentsLink(null);
    navHistory.current = { entries: [], index: -1 };
    traversing.current = null;
    observedNav.current = "";
    pendingObjectLink.current = null;
    pendingServerView.current = null;
    setLinkBack(null);
    setNavRevision((v) => v + 1);
    setError("");
    setNotice("");
  }
  /**
   * Reopen a stored snapshot. Anything stale is left out with a note: plans
   * that no longer parse, unknown screens or views, operators that are gone.
   * Returns the serialized snapshot App will hold once the state settles.
   */
  async function applySession(
    raw: unknown,
    _savedAt: string,
    reason: "startup" | "conflict",
  ) {
    const state = isRecord(raw) ? raw : {};
    const notes: string[] = [];
    const ids = new Set<string>();
    const entries = (Array.isArray(state.plans) ? state.plans : [])
      .filter((p): p is Record<string, unknown> => isRecord(p))
      .filter((p) => {
        const ok =
          typeof p.id === "string" &&
          !!p.id &&
          !ids.has(p.id) &&
          typeof p.raw === "string";
        if (ok) ids.add(p.id as string);
        return ok;
      });
    let tooLarge = 0;
    const results = await Promise.allSettled(
      entries.map(async (p) => {
        const raw = p.raw as string;
        const capture = isRecord(p.capture)
          ? (p.capture as unknown as Capture)
          : undefined;
        const sent = capture ? slimCapture(raw, capture) : undefined;
        const size = utf8.encode(JSON.stringify({ plan: raw, capture: sent })).length;
        if (size > ANALYZE_BUDGET) {
          tooLarge++;
          throw new Error("This plan is larger than an analysis request allows.");
        }
        const result = await analyze(raw, sent as Capture | undefined);
        return {
          ...result,
          capture:
            capture && result.capture
              ? restoreCapture(capture, result.capture)
              : result.capture,
        };
      }),
    );
    const restoredPlans: (Plan & { origin?: "sample" })[] = [];
    const kept: SessionPlan[] = [];
    results.forEach((result, i) => {
      const entry = entries[i];
      if (result.status !== "fulfilled") {
        // Kept as stored, so autosave does not discard it.
        kept.push({
          id: entry.id as string,
          name: text(entry.name, "Plan", 200) || "Plan",
          raw: entry.raw as string,
          capture: isRecord(entry.capture)
            ? (entry.capture as unknown as Capture)
            : undefined,
          notes: text(entry.notes, "", 10000) || undefined,
          origin: entry.origin === "sample" ? "sample" : undefined,
        });
        return;
      }
      restoredPlans.push({
        ...result.value,
        id: entry.id as string,
        name: text(entry.name, "Plan", 200) || "Plan",
        raw: entry.raw as string,
        capture: result.value.capture || undefined,
        notes: text(entry.notes, "", 10000) || undefined,
        origin: entry.origin === "sample" ? "sample" : undefined,
      });
    });
    const dropped =
      (Array.isArray(state.plans) ? state.plans.length : 0) -
      restoredPlans.length -
      kept.length;
    if (dropped > 0)
      notes.push(
        dropped === 1
          ? "One unreadable plan entry was left out."
          : `${dropped} unreadable plan entries were left out.`,
      );
    if (tooLarge > 0)
      notes.push(
        tooLarge === 1
          ? "One plan is larger than the 12 MiB analysis request allows."
          : `${tooLarge} plans are larger than the 12 MiB analysis request allows.`,
      );
    const has = (id: unknown) =>
      typeof id === "string" && restoredPlans.some((p) => p.id === id);
    const activePlan =
      restoredPlans.find((p) => p.id === state.active_plan_id) ||
      restoredPlans[0];
    const activePlanId = activePlan?.id || "";
    let nextMode: Mode = modes.includes(state.mode as Mode)
      ? (state.mode as Mode)
      : restoredPlans.length
        ? "inspect"
        : "home";
    if (state.mode !== undefined && !modes.includes(state.mode as Mode))
      notes.push(
        restoredPlans.length
          ? "The last screen is not available, so Plans is open."
          : "The last screen is not available, so Home is open.",
      );
    // Plans with nothing open was the welcome screen, which is now Home.
    if ((nextMode === "inspect" || nextMode === "compare") && !restoredPlans.length)
      nextMode = "home";
    // Database diagram was a Server view: a session saved on it opens its place.
    if (nextMode === "server" && isRecord(state.query_lab) && isRecord(state.query_lab.slow) && state.query_lab.slow.kind === "diagram")
      nextMode = "diagram";
    const nextView = views.some(([id]) => id === state.view)
      ? (state.view as View)
      : "workbench";
    if (state.view !== undefined && nextView !== state.view)
      notes.push("The last chart view is not available, so Visual Explain is shown.");
    const nextMetric = metrics.some(([id]) => id === state.metric)
      ? (state.metric as string)
      : "self_time";
    const opIds = new Set(activePlan?.operators.map((o) => o.node_id) || []);
    const pick = (id: unknown) =>
      typeof id === "string" && opIds.has(id) ? id : "";
    const selected = pick(state.selected_id);
    const focus = pick(state.focus_id);
    const collapsedIds = (
      Array.isArray(state.collapsed_ids) ? state.collapsed_ids : []
    ).filter((id): id is string => typeof id === "string" && opIds.has(id));
    if (
      (state.selected_id && !selected) ||
      (state.focus_id && !focus)
    )
      notes.push("The selected operator is no longer in the plan.");
    const inv = isRecord(state.investigation) ? state.investigation : {};
    const beforeValue = has(state.before_id) ? (state.before_id as string) : "";
    const afterValue = restoredPlans.length > 1 && has(state.after_id) ? (state.after_id as string) : "";
    const baselineValue = has(inv.baseline_id)
      ? (inv.baseline_id as string)
      : restoredPlans[0]?.id || "";
    const teach = isRecord(state.teach) ? state.teach : {};
    const returning = teach.return_to_plan === true && !!activePlanId;
    const lessonKey = nextMode === "teach" ? text(teach.lesson, "", 200) : "";
    const lab = normalizeLab(state.query_lab);
    const investigation = {
      id: text(inv.id, "", 200),
      name: text(inv.name, "Untitled investigation", 200),
      notes: text(inv.notes, "", 100000),
      tags: text(inv.tags, "", 2400),
      baseline_id: baselineValue,
      dirty: inv.dirty === true,
    };
    pendingView.current = {
      planId: activePlanId,
      selected,
      focus,
      collapsed: collapsedIds,
    };
    setPlans(restoredPlans);
    setHeld(kept);
    tabsBaseline.current = tabsText(lab.tabs);
    setActiveId(activePlanId);
    setSelectedId(selected);
    setFocusId(focus);
    setCollapsed(collapsedIds);
    setSearch("");
    setMode(nextMode);
    setView(nextView);
    setMetric(nextMetric);
    setBeforeId(beforeValue);
    setAfterId(afterValue);
    setName(investigation.name);
    setNotes(investigation.notes);
    setTags(investigation.tags);
    setSavedId(investigation.id || undefined);
    setBaseline(baselineValue);
    setDirty(investigation.dirty);
    unsavedWorkspaceContent.current = false;
    setLesson(lessonKey);
    setReturnToPlan(returning);
    setTeachLesson(lessonKey);
    setLabInitial(lab);
    setLabState(lab);
    setReplicationLoops({});
    setLoopRevision(0);
    setRestoredMembers(null);
    restoredMembersPolled.current = -1;
    unreachedRechecked.current.clear();
    setOtherLoopErrors({});
    setPollerStaleEndpoints([]);
    setColumnLayouts(lab.slow?.columns_v1 || {});
    setActivePassword("");
    setPasswordRevision(value => value + 1);
    setConnectionNote("");
    setResetKey((k) => k + 1);
    setServerNavigation(undefined);
    setServerLink(null);
    setInstrumentsLink(null);
    navHistory.current = { entries: [], index: -1 };
    traversing.current = null;
    observedNav.current = "";
    pendingObjectLink.current = null;
    pendingServerView.current = null;
    setLinkBack(null);
    setNavRevision((v) => v + 1);
    const snapshotText = serializeSession(
      {
        mode: nextMode,
        view: nextView,
        metric: nextMetric,
        active_plan_id: activePlanId,
        selected_id: selected,
        focus_id: focus,
        collapsed_ids: collapsedIds,
        before_id: beforeValue,
        after_id: afterValue,
        investigation,
        teach: { lesson: lessonKey, return_to_plan: returning },
        query_lab: lab,
      },
      serializePlans([...restoredPlans, ...kept]),
    );
    if (reason === "conflict")
      setRestored({
        text: [
          "Your workspace changed in another browser tab. Showing its latest version.",
          ...notes,
        ].join(" "),
        fresh: false,
      });
    else if (notes.length)
      setRestored({ text: notes.join(" "), fresh: false });
    else setRestored(null);
    return snapshotText;
  }
  async function startFresh(backup: boolean) {
    setConfirmFresh(false);
    if (backup) backupUnsaved();
    saver.pause();
    try {
      const r = await api<{ revision: number }>("session/clear", {
        client_id: saver.clientId,
        seq: saver.nextSeq(),
      });
      saver.revision = r.revision;
    } catch (e) {
      setError(message(e));
      if (restoreEnabled && !skipRestore) saver.enable("");
      return;
    }
    resetWorkspace();
    setRestored(null);
    setNotice("Started a fresh workspace.");
    if (restoreEnabled && !skipRestore) saver.enable(blankSnapshot());
  }
  conflictRef.current = () => {
    loadSession()
      .then(async (session) => {
        saver.revision = session.revision;
        setRestoreEnabled(session.restore);
        if (!session.restore) {
          saver.disable("Autosave is off");
          return;
        }
        if (session.state)
          saver.enable(
            await applySession(session.state, session.saved_at, "conflict"),
          );
        else {
          resetWorkspace();
          setRestored({
            text: "Your workspace was cleared in another browser tab.",
            fresh: false,
          });
          saver.enable(blankSnapshot());
        }
      })
      .catch((e) => {
        saver.failed = true;
        setSaveStatus({ kind: "error", reason: message(e) });
      });
  };
  async function toggleRestore(on: boolean) {
    try {
      const r = await api<{ revision: number; restore: boolean }>(
        "session/settings",
        { restore: on, client_id: saver.clientId, seq: saver.nextSeq() },
      );
      saver.revision = r.revision;
      setRestoreEnabled(r.restore);
      if (!r.restore) {
        setRestored(null);
        saver.disable("Autosave is off");
      } else if (skipRestore) saver.disable("Autosave is off for this launch");
      // Nothing is stored yet: save the current workspace now.
      else saver.enable("");
    } catch (e) {
      setError(message(e));
    }
  }
  async function save(): Promise<boolean> {
    // The hook's synchronous guard covers a second click before React renders.
    if (busy || saveAction.pending) return false;
    setError("");
    const snapshot = { id: savedId, value: makeBundle() };
    const result = await saveAction.run(() => api<Bundle>("investigations/save", snapshot),
      { pending: "Saving the investigation…", done: "Investigation saved on this computer.", write: true });
    if (!result.ok) return false;
    setSavedRevision((n) => n + 1);
    // Close leaves the request running. A reply for an older notebook must
    // not mark changes made since then (or another investigation) as saved.
    if (JSON.stringify(currentInvestigation.current()) !== JSON.stringify(snapshot)) {
      saveAction.show("done", `Saved “${snapshot.value.name}” on this computer. Later workspace changes remain unsaved.`);
      return false;
    }
    setSavedId(result.value.id);
    setDirty(false);
    setNotice("Investigation saved on this computer.");
    return true;
  }
  function removePlan(id: string) {
    const removed = plans.find((p) => p.id === id);
    if (removed && (removed.origin !== "sample" || savedId || dirty))
      unsavedWorkspaceContent.current = true;
    const rest = plans.filter((p) => p.id !== id);
    setPlans(rest);
    if (id === activeId) setActiveId(rest[0]?.id || "");
    if (id === beforeId) setBeforeId("");
    if (id === afterId) setAfterId("");
    if (id === baseline) setBaseline(rest[0]?.id || "");
    if (savedId || dirty) setDirty(true);
  }
  function exportHtml() {
    if (exporting || (mode === "compare" ? !comparison || loading : !active)) return;
    // A retry keeps the original plans, view and privacy choice.
    const snapshot: PlanExportSnapshot = mode === "compare"
      ? { kind: "compare" as const, before: before!.raw, after: after!.raw, hide_literals: hideLiterals, name: "myflames-comparison.html" }
      : { kind: "plan" as const, plan: active!.raw, query: active!.query, view: view === "stats" ? "workbench" : view, hide_literals: hideLiterals, name: `${active!.name.replace(/\.json$/i, "")}-report.html` };
    const run = () => { void exportAction.run(async (signal) => {
      const result = snapshot.kind === "compare"
        ? await api<{ html: string }>("compare", snapshot, signal)
        : await api<{ html: string }>("report", snapshot, signal);
      if (!signal.aborted) download(result.html, snapshot.name, "text/html");
    }, { pending: "Building the HTML report…", done: `Saved ${snapshot.name}.` }); };
    exportRetry.current = run;
    run();
  }
  function exportJson() {
    if (exporting || (mode === "compare" ? !comparison || loading : !active)) return;
    const snapshot: PlanExportSnapshot = mode === "compare"
      ? { kind: "compare" as const, before: before!.raw, after: after!.raw, hide_literals: hideLiterals, name: "comparison.json" }
      : { kind: "plan" as const, plan: active!.raw, query: active!.query, analysis: exportedAnalysis(active!.analysis), hide_literals: hideLiterals, format: "json", name: `${active!.name.replace(/\.json$/i, "")}-analysis.json` };
    const run = () => { void exportAction.run(async (signal) => {
      const result = snapshot.kind === "compare"
        ? await api<{ analysis: Analysis }>("compare", snapshot, signal)
        : await api<{ analysis: Analysis }>("report", snapshot, signal);
      if (!signal.aborted) download(JSON.stringify(result.analysis, null, 2), snapshot.name, "application/json");
    }, { pending: "Exporting JSON…", done: `Saved ${snapshot.name}.` }); };
    exportRetry.current = run;
    run();
  }
  // The header's Plan summary and SQL panels close like popovers: a click
  // elsewhere, the other panel, or Escape (focus returns to its summary).
  const planHeaderTools = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const openPanels = () => Array.from(planHeaderTools.current?.querySelectorAll<HTMLDetailsElement>("details[open]") || []);
    const down = (e: MouseEvent) => {
      for (const d of openPanels()) if (!d.contains(e.target as Node)) d.open = false;
    };
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const open = openPanels();
      if (!open.length || document.querySelector("dialog[open]")) return;
      const focused = open.find((d) => d.contains(document.activeElement));
      for (const d of open) d.open = false;
      focused?.querySelector("summary")?.focus();
    };
    document.addEventListener("mousedown", down);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", down);
      document.removeEventListener("keydown", key);
    };
  }, []);
  /** One header panel at a time, however it is opened: a summary's click
   * (Enter and Space on a summary click it too) closes the other panels
   * before its own opens. The toggle event comes later, out of order. */
  const exclusivePanel = (e: SyntheticEvent<HTMLElement>) => {
    const opening = e.currentTarget.parentElement;
    if (!(opening instanceof HTMLDetailsElement) || opening.open) return;
    planHeaderTools.current?.querySelectorAll<HTMLDetailsElement>("details[open]").forEach((d) => {
      if (d !== opening) d.open = false;
    });
  };
  useEffect(() => {
    setMisestimateNote("");
    setMisestimateNotShown(false);
  }, [activeId, view]);
  useEffect(() => {
    if (selectedId === reveal.id) return;
    setMisestimateNote("");
    setMisestimateNotShown(false);
  }, [selectedId, reveal.id]);
  const place = placeOf(mode);
  const placeLabel = place ? PLACES.find((item) => item.key === place)!.label : mode === "settings" ? "Settings" : "Audit log";
  const activeConnection: ActiveConnection = {
    id: labState.profile_id,
    fields: labState.connection,
    password: activePassword,
  };
  // A Group connection stays active while its active member is the connection
  // in use. Safety follows the current saved profiles, so edits in another
  // window take effect without reopening or probing the group.
  const groupActive = groupConnection &&
    groupConnection.members.some((m) => m.profile_id === labState.profile_id)
    ? groupConnection
    : null;
  const groupActiveRef = useRef(groupActive);
  groupActiveRef.current = groupActive;
  // A group edit can remove a member a place was on: that place moves to the
  // primary or source (the active member the backend picked) and says so.
  useEffect(() => {
    if (!groupActive) {
      setMemberByPlace((prev) => (Object.keys(prev).length ? {} : prev));
      setMemberNote("");
      return;
    }
    setMemberByPlace((prev) => {
      const gone = Object.entries(prev).filter(([, id]) => !groupActive.members.some((m) => m.profile_id === id));
      if (!gone.length) return prev;
      const next = { ...prev };
      for (const [place] of gone) delete next[place];
      const target = groupActive.members.find((m) => m.profile_id === groupActive.active_member_profile_id);
      const names = gone.map(([, id]) => profiles.find((p) => p.id === id)?.name || id).join(", ");
      setMemberNote(`${names} ${gone.length === 1 ? "is" : "are"} not in this group any more; its views moved to ${target?.profile_name || groupActive.active_member_profile_id}.`);
      return next;
    });
  }, [groupActive, profiles]);
  // Production, Development or Read-only, as the server will judge the
  // connection in use; part of its identity (binding.ts connectionKey).
  const safety: Safety = groupActive
    ? currentGroupSafety(activeConnection, groupActive, profilesLoaded && !profilesFailed ? profiles : null)
    : connectionSafety(activeConnection, profilesLoaded && !profilesFailed ? profiles : null);
  const activeRef = useRef(activeConnection);
  activeRef.current = activeConnection;
  // Write actions (Kill) stay off while the policy is unknown, and check it
  // again right before sending (see WritePolicy in Connections.tsx).
  const policyBlocked = safety.known
    ? ""
    : profilesFailed
      ? `The settings of this connection could not be read (${profilesFailed}), so changes are off until they load.`
      : "The settings of this connection are still loading.";
  const checkPolicy = useCallback(async (target?: Connection) => {
    const items = await refreshProfiles();
    const active = activeRef.current;
    const asked = target?.group_name ? {
      id: target.profile_id || "", fields: connectionFields(target), password: target.password || "",
    } : active;
    const freshSafety = connectionSafety(asked, items);
    const checked = requestConnection(asked, freshSafety);
    const group = groupActiveRef.current;
    if (target?.group_name && target.group_name !== group?.group_name) {
      throw new Error("The Group connection changed. Open it again and review the change.");
    }
    if (group) {
      const members = items.filter((p) => (p.group || "").trim() === group.group_name);
      if (!freshSafety.endpointMatches || !members.some((p) => p.id === asked.id)) {
        throw new Error("The saved connection changed or left this group. Open the Group connection again.");
      }
      // Confirmation must preserve the Group connection's identity and use
      // its freshly read strictest type. Read-only remains this member's flag.
      // The name carries the member (target?.name already does; the active
      // member's fallback adds it), so the review sheet names the server.
      const activeName = members.find((p) => p.id === asked.id)?.name;
      checked.group_name = group.group_name;
      checked.name = target?.name || `${groupConnectionLabel(group.group_name, members.length)}${activeName ? ` · ${activeName}` : ""}`;
      checked.type = members.some((p) => p.type !== "development") ? "production" : "development";
    }
    return checked;
  }, [refreshProfiles]);
  const writePolicy = useMemo(() => ({ blocked: policyBlocked, check: checkPolicy }), [policyBlocked, checkPolicy]);
  const request = useMemo(
    () => {
      const c = requestConnection(
        {
          id: labState.profile_id,
          fields: labState.connection,
          password: activePassword,
        },
        { type: safety.type, read_only: safety.read_only, endpointMatches: safety.endpointMatches, known: safety.known },
      );
      // A Group connection carries its group name: the backend re-checks the
      // membership on every request, and the key reloads the views on a change.
      // Its display name is the group's with the active member's name
      // ("Group connection mf-ic · 3 servers · db-1"), so a review sheet names
      // the server the reviewed statement runs on, not only the group.
      if (groupActive) {
        c.group_name = groupActive.group_name;
        const activeName = profiles.find((p) => p.id === labState.profile_id)?.name
          || groupActive.members.find((m) => m.profile_id === labState.profile_id)?.profile_name || "";
        c.name = groupConnectionLabel(groupActive.group_name, groupActive.members.length) + (activeName ? ` · ${activeName}` : "");
      }
      return c;
    },
    [labState.profile_id, labState.connection, activePassword, safety.type, safety.read_only, safety.endpointMatches, safety.known, groupActive, profiles],
  );
  const requestRef = useRef(request);
  requestRef.current = request;
  // The member each single-server place works on: its own choice while it is
  // a member, else the primary or source the backend made active. A view's
  // requests carry that member's profile id and the group name; the
  // connection's safety stays the group's strictest (memberRequest).
  const memberOf = (place: string) => {
    if (!groupActive) return "";
    const chosen = memberByPlace[place];
    return chosen && groupActive.members.some((m) => m.profile_id === chosen)
      ? chosen
      : groupActive.active_member_profile_id;
  };
  const serverMemberId = memberOf("server");
  const queryMemberId = memberOf("query");
  const serverRequest = useMemo(
    () => (groupActive && serverMemberId ? memberRequest(serverMemberId) || request : request),
    [groupActive, serverMemberId, request, profiles],
  );
  // Readyset and ProxySQL route their reads to their chosen member too: the
  // Topology map's card opens the place on the member the card names.
  const readysetMemberId = memberOf("readyset");
  const proxyMemberId = memberOf("proxysql");
  const readysetRequest = useMemo(
    () => (groupActive && readysetMemberId ? memberRequest(readysetMemberId) || request : request),
    [groupActive, readysetMemberId, request, profiles],
  );
  const proxyRequest = useMemo(
    () => (groupActive && proxyMemberId ? memberRequest(proxyMemberId) || request : request),
    [groupActive, proxyMemberId, request, profiles],
  );
  // Cluster and Replication route the same way: a card or the inspector opens
  // the place on the clicked member.
  const clusterMemberId = memberOf("cluster");
  const replicationMemberId = memberOf("replication");
  const clusterRequest = useMemo(
    () => (groupActive && clusterMemberId ? memberRequest(clusterMemberId) || request : request),
    [groupActive, clusterMemberId, request, profiles],
  );
  const replicationRequest = useMemo(
    () => (groupActive && replicationMemberId ? memberRequest(replicationMemberId) || request : request),
    [groupActive, replicationMemberId, request, profiles],
  );
  const serverRequestRef = useRef(serverRequest);
  serverRequestRef.current = serverRequest;
  const serverWritePolicy = useMemo(() => ({
    blocked: policyBlocked,
    check: async () => {
      const checked = await checkPolicy(serverRequestRef.current);
      if (connectionKey(checked) !== connectionKey(serverRequestRef.current)) {
        throw new Error("The server or its write policy changed. Review the change again.");
      }
      return checked;
    },
  }), [policyBlocked, checkPolicy]);
  const queryTab = labState.tabs.find((t) => t.id === labState.tab_id) || labState.tabs[0];
  const queryRequest = queryTab?.member ? memberRequest(queryTab.member) : request;
  // An orphaned member tab has no endpoint. Keep the same disconnected key
  // as QueryLab, rather than borrowing the window's status or grants.
  const placeRequest = mode === "query" ? queryRequest || unavailableMemberConnection(queryTab.member!, queryTab.member_name)
    : ["server", "workload", "tuning", "diagram"].includes(mode) ? serverRequest
    : mode === "readyset" ? readysetRequest
    : mode === "proxysql" ? proxyRequest
    : mode === "cluster" ? clusterRequest
    : mode === "replication" ? replicationRequest
    : request;
  const queryRequestRef = useRef(queryRequest);
  queryRequestRef.current = queryRequest;
  // The "Using <member> from group <group>." note is derived per place from
  // the request the place on screen actually reads: it can never name another
  // member, and navigating to a place that reads the active member (or the
  // connection in use) shows no stale note. The removal notice (memberNote)
  // is separate and takes precedence until the next navigation or choice.
  const memberNameOf = (id: string) =>
    profiles.find((p) => p.id === id)?.name
    || groupActive?.members.find((m) => m.profile_id === id)?.profile_name
    || id;
  const usingNote = (req: Connection) =>
    groupActive && req.profile_id && req.profile_id !== labState.profile_id
      ? `Using ${memberNameOf(req.profile_id)} from group ${groupActive.group_name}.`
      : "";
  const serverMemberNote = usingNote(serverRequest);
  const placeMemberNote = mode === "readyset" || mode === "proxysql" || mode === "cluster" || mode === "replication"
    ? usingNote(placeRequest)
    : "";
  const placeConnected = ready && safety.known && !!placeRequest.host;
  const placeConnectionName = groupActive && placeRequest.profile_id
    ? profiles.find((p) => p.id === placeRequest.profile_id)?.name
      || groupActive.members.find((m) => m.profile_id === placeRequest.profile_id)?.profile_name
      || placeRequest.name || connectionTarget(placeRequest)
    : placeRequest.name || connectionTarget(placeRequest);
  const chooseMember = useCallback((place: string, id: string) => {
    setMemberNote("");
    setMemberByPlace((current) => (current[place] === id ? current : { ...current, [place]: id }));
  }, []);
  // A member card's click opens that member's place in the click's frame:
  // Server, Readyset, ProxySQL, Cluster and Replication route their reads to
  // the chosen member and the per-place note (derived from placeRequest) says
  // whose connection the place uses; the Back chip returns to the map with
  // its tab, scroll and selection, and focus returns to the card.
  const topologyCardFocus = useRef("");
  // The card a return to Topology focuses: set by applyNavEntry, consumed and
  // cleared by the Topology place once its map shows the card (no timer).
  const [topologyFocusCard, setTopologyFocusCard] = useState("");
  const clearTopologyFocusCard = useCallback(() => setTopologyFocusCard(""), []);
  const openTopologyMember = useCallback((member: OpenMemberInfo, place: MemberPlace) => {
    const back = linkOrigin();
    chooseMember(place, member.profile_id);
    go(place);
    finishLink(back);
    topologyCardFocus.current = member.profile_id;
    focusPlaceHeadingSoon();
  }, [chooseMember]);
  const [serverNavigation, setServerNavigation] = useState<{ view: ServerKind; n: number } | undefined>(undefined);
  // Navigation history (Back/Forward, the cross-link chip, the palette's
  // Recent views): one entry per settled place or view, in-memory only.
  const navHistory = useRef<{ entries: NavEntry[]; index: number }>({ entries: [], index: -1 });
  const [navRevision, setNavRevision] = useState(0);
  // A Server/Workload view the shell asked for but Server has not reported
  // yet (slow.kind lags one render): the transition is not a history stop.
  const pendingServerView = useRef<ServerKind | null>(null);
  // The cross-link chip in the top bar: "← <previous view>", back on click.
  const [linkBack, setLinkBack] = useState<NavEntry | null>(null);
  const [navStop, setNavStop] = useState(0);
  const nextNavStop = useRef(0);
  const traversing = useRef<{ entry: NavEntry; waiting: boolean; connection: string } | null>(null);
  // Actual UI state last observed, independent of an entry's historical
  // connection. Traversal never changes the connection in use.
  const observedNav = useRef("");
  const pendingObjectLink = useRef<{ connection: string } | null>(null);
  const lastServerView = useRef<ServerKind>("health");
  const lastWorkloadView = useRef<ServerKind>("top");
  if (WORKLOAD_KINDS.includes(labState.slow.kind)) lastWorkloadView.current = labState.slow.kind;
  else if (!["tuning", "report", "replication", "cluster", "diagram"].includes(labState.slow.kind))
    lastServerView.current = labState.slow.kind;
  const serverViewNow: ServerKind | null = mode === "server"
    ? (WORKLOAD_KINDS.includes(labState.slow.kind) ? lastServerView.current : labState.slow.kind)
    : mode === "workload"
      ? (WORKLOAD_KINDS.includes(labState.slow.kind) ? labState.slow.kind : lastWorkloadView.current)
      : null;
  const navKey = connectionKey(request);
  const navStateRef = useRef({ key: "", name: "", label: "" });
  navStateRef.current = { key: navKey, name: connectionLabel(request), label: navEntryLabel(mode, serverViewNow).label };
  function navSelection(): NavEntry["selection"] {
    if (mode === "query") return { query: labRef.current?.navigationSelection() || { tabId: labState.tab_id, reveal: null } };
    if (mode === "inspect") return { plan: activeId };
    if (mode === "compare") return { compare: [beforeId, afterId] };
    return {};
  }
  useEffect(() => {
    if (!ready) return;
    if (pendingObjectLink.current) {
      if (pendingObjectLink.current.connection === navKey) return;
      pendingObjectLink.current = null;
    }
    if (mode !== "server" && mode !== "workload") pendingServerView.current = null;
    else if (pendingServerView.current) {
      if (serverViewNow === pendingServerView.current) pendingServerView.current = null;
      else return;
    }
    const selection = navSelection();
    const signature = JSON.stringify([mode, serverViewNow, navKey, selection, navStop]);
    if (traversing.current && traversing.current.connection !== navKey) traversing.current = null;
    const traversal = traversing.current;
    if (traversal) {
      // Child state can settle after the shell. Hold the cursor until the
      // destination is restored, including a same-place query tab change.
      if (traversal.waiting || mode !== traversal.entry.mode || serverViewNow !== traversal.entry.view ||
          navStop !== traversal.entry.stop || JSON.stringify(selection) !== JSON.stringify(traversal.entry.selection)) return;
      traversing.current = null;
      observedNav.current = signature;
      return;
    }
    if (observedNav.current === signature) return;
    observedNav.current = signature;
    const h = navHistory.current;
    const { label, icon } = navEntryLabel(mode, serverViewNow);
    h.entries = h.entries.slice(0, h.index + 1);
    h.entries.push({ mode, view: serverViewNow, selection, stop: navStop, connection: navKey,
      connectionName: connectionLabel(connectedRequest.current), label, icon });
    if (h.entries.length > 100) h.entries.shift();
    h.index = h.entries.length - 1;
    setNavRevision((v) => v + 1);
  }, [ready, mode, serverViewNow, navKey, labState.tab_id, activeId, beforeId, afterId, navStop, navRevision]);
  /** Capture the origin itself, not a relative Back step that can change
   * after another tab or cross-link is opened. */
  function linkOrigin() {
    traversing.current = null;
    return navHistory.current.entries[navHistory.current.index] || null;
  }
  function finishLink(origin: NavEntry | null) {
    setNavStop(++nextNavStop.current);
    setLinkBack(origin);
  }
  function applyNavEntry(entry: NavEntry, index: number) {
    pendingObjectLink.current = null;
    const h = navHistory.current;
    // A retained chip can outlive the 100-entry cap. Restore its captured
    // origin without treating an unrelated predecessor as the destination.
    if (index < 0) {
      h.entries = [...h.entries.slice(0, h.index + 1), entry].slice(-100);
      index = h.entries.length - 1;
    }
    h.index = index;
    const target = { ...entry, selection: { ...entry.selection } };
    const traversal = { entry: target, waiting: !!target.selection.query, connection: navStateRef.current.key };
    traversing.current = traversal;
    setNavStop(entry.stop);
    setNavRevision((v) => v + 1);
    setLinkBack(null);
    if (entry.mode === "inspect" || entry.mode === "compare") setMode(entry.mode);
    else if (entry.mode === "audit") { setAuditFocus(""); setMode("audit"); }
    else if (entry.mode === "settings") setMode("settings");
    else {
      const place = placeOf(entry.mode);
      if (place) go(place, true);
    }
    if (target.selection.query) withLab((lab) => {
      if (traversing.current !== traversal) return;
      target.selection.query = lab.restoreNavigation(target.selection.query!);
      traversal.waiting = false;
      setNavRevision((v) => v + 1);
    });
    if (target.selection.plan !== undefined) {
      target.selection.plan = plans.some((p) => p.id === target.selection.plan) ? target.selection.plan : activeId;
      setActiveId(target.selection.plan);
    }
    if (target.selection.compare) {
      target.selection.compare = target.selection.compare.map((id) => plans.some((p) => p.id === id) ? id : "") as [string, string];
      setBeforeId(target.selection.compare[0]);
      setAfterId(target.selection.compare[1]);
    }
    if ((entry.mode === "server" || entry.mode === "workload") && entry.view) {
      pendingServerView.current = entry.view;
      setServerNavigation((n) => ({ view: entry.view as ServerKind, n: (n?.n || 0) + 1 }));
    }
    const now = navStateRef.current;
    if (entry.connection && entry.connection !== now.key)
      setNotice(`${entry.label} was open on ${entry.connectionName}; this window is on ${now.name}. Nothing was switched: views read the connection in use.`);
    // Back to Topology after a card opened a member's place: the Topology
    // place focuses the card itself once its map shows it (the map stays
    // mounted, but its render after the traversal is not timed).
    if (entry.mode === "topology" && topologyCardFocus.current) {
      const cardId = topologyCardFocus.current;
      topologyCardFocus.current = "";
      setTopologyFocusCard(cardId);
    }
  }
  function navMove(dir: -1 | 1) {
    const h = navHistory.current;
    const target = h.entries[h.index + dir];
    if (!target) return;
    applyNavEntry(target, h.index + dir);
  }
  const applyNavEntryRef = useRef(applyNavEntry);
  applyNavEntryRef.current = applyNavEntry;
  const navMoveRef = useRef(navMove);
  navMoveRef.current = navMove;
  // Back/Forward through the window's views: ⌘[ and ⌘]; Alt+Left/Right on
  // Windows and Linux. An open dialog (the palette, a sheet) keeps its keys.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      let dir: -1 | 0 | 1 = 0;
      if ((e.metaKey || e.ctrlKey) && e.key === "[") dir = -1;
      else if ((e.metaKey || e.ctrlKey) && e.key === "]") dir = 1;
      else if (!MAC && e.altKey && e.key === "ArrowLeft") dir = -1;
      else if (!MAC && e.altKey && e.key === "ArrowRight") dir = 1;
      if (!dir || document.querySelector("dialog[open]")) return;
      e.preventDefault();
      navMoveRef.current(dir);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // The Cluster item names the state of the member the Cluster place reads
  // (clusterRequest) once the place or Refresh all has read it: muted when it
  // is not part of a cluster (still focusable; the place explains), otherwise
  // the cluster's status. Before the first read it looks like any other place.
  // A ProxySQL or Readyset report proves the product of the one member it was
  // read on: it mutes (or hides a stale report of) only a place whose own
  // request is that member. A card that opened the ProxySQL or Readyset place
  // on one member never greys the Cluster, Replication or the other place
  // reading a different member, and never replaces its report.
  const proxyHere = (req: Connection) => proxyReport?.key === connectionKey(req) && proxyReport.report.is_proxysql;
  const readysetHere = (req: Connection) => readysetReport?.key === connectionKey(req) && readysetReport.report.is_readyset;
  const shownClusterReport = (proxyHere(clusterRequest) || readysetHere(clusterRequest)) && clusterReport?.key !== connectionKey(clusterRequest) ? null : clusterReport;
  const clusterState = clusterNav(shownClusterReport, connectionKey(clusterRequest));
  const clusterMuted = clusterState.state === "none" || proxyHere(clusterRequest) || readysetHere(clusterRequest);
  const proxyNav = proxySqlNav(proxyReport, connectionKey(proxyRequest));
  const proxyMuted = proxyNav === "none" || readysetHere(proxyRequest);
  const proxyNow = proxyNav === "proxysql" && proxyReport && proxyReport.key === connectionKey(proxyRequest) ? proxyReport.report : null;
  const proxyIssues = proxyNow && (proxyNow.status === "problem" || proxyNow.status === "watch") ? proxyNow.status_why.length : 0;
  const proxyBadge = !proxyNow ? null
    : proxyIssues > 0
      ? <NavMarker compact tone={proxyNow.status} count={proxyIssues} countTone={proxyNow.status === "problem" ? "danger" : "warn"} text={`ProxySQL ${proxyNow.status === "problem" ? "problem" : "to watch"}, ${proxyIssues} ${proxyIssues === 1 ? "issue" : "issues"}`} />
      : <NavMarker compact tone={proxyNow.status === "unknown" ? "unknown" : proxyNow.status} text={proxyNow.status === "ok" ? "ProxySQL OK" : proxyNow.status === "unknown" ? "ProxySQL not judged" : `ProxySQL ${proxyNow.status === "problem" ? "problem" : "to watch"}`} />;
  // A report read with another default database is stale like an unread one:
  // the sidebar names Readyset's state only from a report of this database,
  // read on the member the Readyset place reads (readysetRequest).
  const readysetDbStale =
    !!readysetReport &&
    readysetReport.key === connectionKey(readysetRequest) &&
    readysetReport.report.is_readyset &&
    (readysetReport.report.session_database || null) !== (readysetRequest.database || null);
  const readysetNavState = readysetDbStale ? "unread" : readysetNav(readysetReport, connectionKey(readysetRequest));
  const readysetMuted = readysetNavState === "none" || proxyHere(readysetRequest);
  const readysetNow = (readysetNavState === "ok" || readysetNavState === "watch" || readysetNavState === "problem" || readysetNavState === "unknown") && readysetReport && readysetReport.key === connectionKey(readysetRequest) ? readysetReport.report : null;
  const readysetIssues = readysetNow && (readysetNow.status === "problem" || readysetNow.status === "watch") ? readysetNow.status_why.length : 0;
  const readysetBadge = !readysetNow ? null
    : readysetIssues > 0
      ? <NavMarker compact tone={readysetNow.status} count={readysetIssues} countTone={readysetNow.status === "problem" ? "danger" : "warn"} text={`Readyset ${readysetNow.status === "problem" ? "problem" : "to watch"}, ${readysetIssues} ${readysetIssues === 1 ? "issue" : "issues"}`} />
      : <NavMarker compact tone={readysetNow.status === "unknown" ? "unknown" : readysetNow.status} text={readysetNow.status === "ok" ? "Readyset OK" : readysetNow.status === "unknown" ? "Readyset not judged" : `Readyset ${readysetNow.status === "problem" ? "problem" : "to watch"}`} />;
  const clusterBadge = clusterState.state !== "cluster" ? null
    : clusterState.status === "problem" && clusterState.problems > 0
      ? <NavMarker compact tone="problem" count={clusterState.problems} countTone="danger" text={clusterState.text} />
      // Not judged (an account that cannot read): no marker word of its own; the place says why.
      : <NavMarker compact tone={clusterState.status === "unknown" ? undefined : clusterState.status} text={clusterState.text} />;
  // Replication names the state of the member the Replication place reads
  // (replicationRequest) the same way, from the one report the place and
  // Refresh all share: muted when it is not a replica (a neutral state; Group
  // Replication's own channels do not count), Unknown only when the status
  // could not be read.
  const replicationState = replicationNav(replicationReport, connectionKey(replicationRequest));
  const replicationMuted = replicationState.state === "none" || readysetHere(replicationRequest);
  const replicationBadge = replicationState.state !== "replica" ? null
    : replicationState.failed > 0
      ? <NavMarker compact tone="problem" count={replicationState.failed} countTone="danger" text={replicationState.text} />
      : <NavMarker compact tone={replicationState.tone} text={replicationState.text} />;
  // The places that send to the connection in use (the footer names its
  // write safety there, and only there).
  const connectionPlace = mode === "query" || mode === "digest" || mode === "diagram" || mode === "schema" || mode === "server" || mode === "workload" || mode === "topology" || mode === "replication" || mode === "cluster" || mode === "proxysql" || mode === "readyset" || mode === "tuning";
  // Server renders Server, Workload, Tuning and Database diagram. While
  // another place is on screen it keeps the one shown last, so leaving it
  // never swaps (and mounts) a different view in the hidden place.
  const serverPlaceRef = useRef<"server" | "workload" | "tuning" | "diagram">(mode === "workload" || mode === "tuning" || mode === "diagram" ? mode : "server");
  if (mode === "server" || mode === "workload" || mode === "tuning" || mode === "diagram") serverPlaceRef.current = mode;
  const serverPlace = serverPlaceRef.current;
  const serverOnScreen = ready && mode === serverPlace;
  // Server and Replication mount on their first visit and stay mounted,
  // hidden, after it, so nothing reads a server before its place is opened
  // (views read on open only while on screen, viewOnScreen.ts). A restored
  // skip loop mounts both at once so its status is found again (that status
  // reads no database). Start fresh and reopening a snapshot start over.
  const visited = useRef({ key: resetKey, server: false, replication: false, topology: false, cluster: false, proxysql: false, readyset: false, files: false, digest: false, schema: false, teach: false });
  if (visited.current.key !== resetKey) visited.current = { key: resetKey, server: false, replication: false, topology: false, cluster: false, proxysql: false, readyset: false, files: false, digest: false, schema: false, teach: false };
  if (serverOnScreen) visited.current.server = true;
  if (ready && mode === "replication") visited.current.replication = true;
  if (ready && mode === "topology") visited.current.topology = true;
  if (ready && mode === "cluster") visited.current.cluster = true;
  if (ready && mode === "proxysql") visited.current.proxysql = true;
  if (ready && mode === "readyset") visited.current.readyset = true;
  // Files stays mounted after its first visit too, so a running transfer
  // and the folders on screen survive a trip to another place.
  if (ready && mode === "files") visited.current.files = true;
  if (ready && mode === "digest") visited.current.digest = true;
  if (ready && mode === "schema") visited.current.schema = true;
  if (ready && mode === "teach") visited.current.teach = true;
  const mountTeach = visited.current.teach;
  const mountDigest = visited.current.digest;
  const mountSchema = visited.current.schema;
  const mountTopology = visited.current.topology;
  const mountCluster = visited.current.cluster;
  const mountProxySql = visited.current.proxysql;
  const mountReadyset = visited.current.readyset;
  const mountFiles = visited.current.files;
  const initialProfileId = labInitial?.profile_id || "";
  const restoredLoop = labInitial?.slow?.replication_loop_active === true;
  const restoredLoopMembers = useMemo(
    () => (restoredLoop ? loopMemberIds(labInitial?.slow?.replication_loop_members, labInitial?.slow?.replication_loop_member) : []),
    [restoredLoop, labInitial],
  );
  // Keep the active member's own restore poll when no member ids were stored
  // (an unsaved connection) or when the active member's own id is listed.
  const restoredLoopForCurrent =
    restoredLoop && (restoredLoopMembers.length === 0 || (!!initialProfileId && restoredLoopMembers.includes(initialProfileId)));
  const restoredOtherMembers = useMemo(
    () => restoredLoopMembers.filter((id) => id !== initialProfileId),
    [restoredLoopMembers, initialProfileId],
  );
  const restoreMemberPending =
    restoredOtherMembers.length > 0 && !profilesFailed && restoredMembers?.reset !== resetKey;
  const activeRestoredLoopKeys = useMemo(
    () =>
      restoredMembers &&
      restoredMembers.reset === resetKey &&
      !groupActive &&
      labState.profile_id === initialProfileId
        ? restoredMembers.activeKeys
        : [],
    [restoredMembers, resetKey, groupActive, labState.profile_id, initialProfileId],
  );
  const stoppedRestoredLoopKeys = useMemo(
    () => (restoredMembers && restoredMembers.reset === resetKey ? restoredMembers.stoppedKeys : []),
    [restoredMembers, resetKey],
  );
  // Active skip loops belonging to the connection in use, any member of the
  // active group, or restored same-group members after View > Reload.
  const requestKey = connectionKey(request);
  const replicationKey = connectionKey(replicationRequest);
  const shellActiveLoops = useMemo(() => {
    const memberIds = groupActive ? new Set(groupActive.members.map((m) => m.profile_id)) : null;
    const reqEp = endpointKey(requestKey);
    const replEp = endpointKey(replicationKey);
    const activeEps = new Set(activeRestoredLoopKeys.map(endpointKey));
    const list = Object.values(replicationLoops).filter((bound) => {
      if (!bound.report.some(loopActive)) return false;
      const ep = endpointKey(bound.key);
      if (ep === reqEp || ep === replEp) return true;
      if (activeEps.has(ep)) return true;
      const pid = keyProfileId(bound.key);
      return !!memberIds && !!pid && memberIds.has(pid);
    });
    const order = groupActive
      ? new Map(groupActive.members.map((m, i) => [m.profile_id, i]))
      : null;
    list.sort((a, b) => {
      if (endpointKey(a.key) === reqEp) return -1;
      if (endpointKey(b.key) === reqEp) return 1;
      if (order) {
        const ai = order.get(keyProfileId(a.key)) ?? 9999;
        const bi = order.get(keyProfileId(b.key)) ?? 9999;
        if (ai !== bi) return ai - bi;
      }
      return a.key.localeCompare(b.key);
    });
    return list;
  }, [replicationLoops, requestKey, replicationKey, activeRestoredLoopKeys, groupActive]);
  // Saved loop members that are outside the active connection's group (or no
  // longer in any group): restored ids not yet polled, plus in-window active
  // loops whose member left scope (e.g. removed from the group by a refresh).
  // Drop an id only when its saved connection was deleted (leaving a group does
  // not stop a running loop on the backend).
  const unreachedLoopMembers = useMemo(() => {
    const out: { id: string; name: string; group: string }[] = [];
    const seen = new Set<string>();
    const profilesKnown = profilesLoaded && !profilesFailed;
    if (restoredMembers && restoredMembers.reset === resetKey) {
      for (const m of restoredMembers.outsideGroup) {
        if (seen.has(m.id)) continue;
        if (Object.values(replicationLoops).some((b) => keyProfileId(b.key) === m.id)) continue;
        const p = profiles.find((item) => item.id === m.id);
        if (profilesKnown && !p) continue;
        seen.add(m.id);
        out.push({
          id: m.id,
          name: p?.name || m.name || m.id,
          group: p ? (p.group || "").trim() : m.group,
        });
      }
    }
    const inScopeKeys = new Set(shellActiveLoops.map((b) => b.key));
    for (const bound of Object.values(replicationLoops)) {
      if (!bound.report.some(loopActive) || inScopeKeys.has(bound.key)) continue;
      const pid = keyProfileId(bound.key);
      if (!pid || seen.has(pid)) continue;
      const p = profiles.find((item) => item.id === pid);
      if (profilesKnown && !p) continue;
      seen.add(pid);
      out.push({
        id: pid,
        name: p?.name || bound.label || pid,
        group: p ? (p.group || "").trim() : "",
      });
    }
    return out;
  }, [restoredMembers, resetKey, replicationLoops, shellActiveLoops, profiles, profilesLoaded, profilesFailed]);
  const restoredOutsideNote = useMemo(() => {
    if (!unreachedLoopMembers.length) return "";
    if (unreachedLoopMembers.length === 1) {
      const m = unreachedLoopMembers[0];
      if (!m.group) {
        return `A skip loop was running on ${m.name}, which is no longer in a connection group. Open ${m.name} to check or stop it.`;
      }
      return `A skip loop was running on ${m.name} (group ${m.group}), which is outside the active connection's group. Switch to that connection or open its group to check or stop it.`;
    }
    if (unreachedLoopMembers.every((m) => !m.group)) {
      const names = unreachedLoopMembers.map((m) => m.name);
      return `Skip loops were running on ${names.join(", ")}, which are no longer in a connection group. Open those connections to check or stop them.`;
    }
    const names = unreachedLoopMembers.map((m) => (m.group ? `${m.name} (group ${m.group})` : `${m.name} (no group)`));
    return `Skip loops were running on ${names.join(", ")}, which are outside the active connection's group. Switch to those connections or open their group to check or stop them.`;
  }, [unreachedLoopMembers]);
  const mountServer = visited.current.server || restoredLoop;
  const mountReplication = visited.current.replication || restoredLoop;
  // When a restored workspace had skip loops on other members, poll each
  // same-group member's loop registry once saved connections load (no database
  // login). Members outside the active connection's group (or with no group)
  // get no read: drop an id only when its saved connection was deleted; keep
  // the flag and warn naming it otherwise.
  useEffect(() => {
    if (!ready || restoredOtherMembers.length === 0 || !profilesLoaded || profilesFailed) return;
    if (restoredMembersPolled.current === resetKey) return;
    restoredMembersPolled.current = resetKey;
    const list = profilesRef.current;
    const toPoll: { id: string; c: Connection; key: string; label: string }[] = [];
    const outsideGroup: { id: string; name: string; group: string }[] = [];
    for (const id of restoredOtherMembers) {
      const c = loopMemberRequest(id);
      if (c) {
        toPoll.push({ id, c, key: connectionKey(c), label: connectionLabel(c) });
        continue;
      }
      const target = list.find((p) => p.id === id);
      if (target) {
        outsideGroup.push({ id, name: target.name || id, group: (target.group || "").trim() });
      }
    }
    if (toPoll.length === 0) {
      setRestoredMembers({ reset: resetKey, activeKeys: [], stoppedKeys: [], outsideGroup, anyFailed: false });
      if (!restoredLoopForCurrent) {
        if (outsideGroup.length === 0) syncLoopSession(false, []);
        else syncLoopSession(true, outsideGroup.map((m) => m.id));
      }
      return;
    }
    const controller = new AbortController();
    let done = false;
    void Promise.allSettled(
      toPoll.map(async (item) => {
        const answer = await readSkipLoops(item.c, controller.signal);
        return { item, loops: answer.loops };
      }),
    ).then((results) => {
      if (controller.signal.aborted) return;
      done = true;
      const activeKeys: string[] = [];
      const stoppedKeys: string[] = [];
      let anyFailed = false;
      for (let i = 0; i < results.length; i++) {
        const r = results[i];
        const item = toPoll[i];
        if (r.status === "fulfilled") {
          publishReplicationLoops({ key: item.key, label: item.label, report: r.value.loops });
          if (r.value.loops.some(loopActive)) activeKeys.push(item.key);
          else if (r.value.loops.length === 0) stoppedKeys.push(item.key);
        } else {
          anyFailed = true;
        }
      }
      setRestoredMembers({ reset: resetKey, activeKeys, stoppedKeys, outsideGroup, anyFailed });
      if (!anyFailed && activeKeys.length === 0 && outsideGroup.length === 0 && !restoredLoopForCurrent) {
        syncLoopSession(false, []);
      }
    });
    return () => {
      if (!done) restoredMembersPolled.current = -1;
      controller.abort();
    };
  }, [ready, resetKey, restoredOtherMembers, restoredLoopForCurrent, profilesLoaded, profilesFailed, loopMemberRequest, publishReplicationLoops, syncLoopSession]);
  // If the user later switches to an unreached restored loop's connection or
  // opens its group, poll its registry (no database login) and clear the note.
  useEffect(() => {
    if (!ready || !unreachedLoopMembers.length || !profilesLoaded || profilesFailed) return;
    const reachable = unreachedLoopMembers
      .filter((m) => !unreachedRechecked.current.has(m.id))
      .map((m) => ({ m, c: loopMemberRequest(m.id) }))
      .filter((item): item is { m: { id: string; name: string; group: string }; c: Connection } => !!item.c);
    if (!reachable.length) return;
    const controller = new AbortController();
    const finished = new Set<string>();
    for (const { m } of reachable) unreachedRechecked.current.add(m.id);
    void Promise.allSettled(
      reachable.map(async ({ m, c }) => {
        const answer = await readSkipLoops(c, controller.signal);
        if (controller.signal.aborted) return;
        finished.add(m.id);
        const targetKey = connectionKey(c);
        publishReplicationLoops({ key: targetKey, label: connectionLabel(c), report: answer.loops });
        if (answer.loops.length === 0) {
          setRestoredMembers((prev) =>
            prev && prev.reset === resetKey
              ? { ...prev, stoppedKeys: [...new Set([...prev.stoppedKeys, targetKey])] }
              : prev,
          );
        }
      }),
    );
    return () => {
      for (const { m } of reachable) {
        if (!finished.has(m.id)) unreachedRechecked.current.delete(m.id);
      }
      controller.abort();
    };
  }, [ready, resetKey, unreachedLoopMembers, profilesLoaded, profilesFailed, groupActive, requestKey, loopMemberRequest, publishReplicationLoops]);
  const restoreCurrentPending =
    restoredLoopForCurrent &&
    labState.profile_id === initialProfileId &&
    !Object.values(replicationLoops).some((b) => endpointKey(b.key) === endpointKey(requestKey));
  useEffect(() => {
    const restoredReady = restoredMembers?.reset === resetKey && !restoredMembers.anyFailed;
    if ((loopRevision === 0 && !restoredReady) || restoreMemberPending || restoreCurrentPending) return;
    const activeIds = shellActiveLoops.map((b) => keyProfileId(b.key)).filter(Boolean);
    const unreachedIds = unreachedLoopMembers.map((m) => m.id);
    const allIds = [...new Set([...activeIds, ...unreachedIds])];
    const hasActive = shellActiveLoops.length > 0 || unreachedIds.length > 0;
    syncLoopSession(hasActive, allIds);
  }, [loopRevision, restoredMembers, resetKey, restoreMemberPending, restoreCurrentPending, shellActiveLoops, unreachedLoopMembers, syncLoopSession]);
  const resolveBoundLoopConnection = useCallback((boundKey: string): { connection: Connection | null; reason: string } => {
    const wanted = endpointKey(boundKey);
    if (endpointKey(connectionKey(requestRef.current)) === wanted) {
      return { connection: requestRef.current, reason: "" };
    }
    const pid = keyProfileId(boundKey);
    if (!pid) {
      return {
        connection: null,
        reason: "Could not send Stop: the active connection changed since this skip loop started.",
      };
    }
    const rebuilt = loopMemberRequest(pid);
    if (rebuilt) {
      if (endpointKey(connectionKey(rebuilt)) === wanted) {
        return { connection: rebuilt, reason: "" };
      }
      return {
        connection: null,
        reason: "Could not send Stop: this saved connection's address, user, TLS or SSH settings changed since the skip loop started.",
      };
    }
    const known = profilesRef.current.find((p) => p.id === pid);
    return {
      connection: null,
      reason: known
        ? `Could not send Stop: ${known.name || pid} is no longer in the active connection group.`
        : "Could not send Stop: this saved connection was deleted.",
    };
  }, [loopMemberRequest]);
  // Refresh active skip loops running on other members (ReplicationView polls
  // only its own key) through the same endpoint-key Stop resolver. Registry
  // only, no database login; re-arms when a target cannot be resolved and marks
  // unresolved or failed rows "Status not refreshed" until a poll succeeds.
  const otherActiveLoopKeys = useMemo(
    () => shellActiveLoops
      .filter((b) => endpointKey(b.key) !== endpointKey(replicationKey))
      .map((b) => b.key)
      .sort()
      .join("\n"),
    [shellActiveLoops, replicationKey],
  );
  useEffect(() => {
    if (!otherActiveLoopKeys) {
      setPollerStaleEndpoints((prev) => (prev.length ? [] : prev));
      return;
    }
    const keys = otherActiveLoopKeys.split("\n");
    const activeEps = new Set(keys.map(endpointKey));
    setPollerStaleEndpoints((prev) => {
      const kept = prev.filter((ep) => activeEps.has(ep));
      return kept.length === prev.length ? prev : kept;
    });
    const controller = new AbortController();
    let timer: number | undefined;
    const updateStale = (staleSet: Set<string>) => {
      const next = [...staleSet].sort();
      setPollerStaleEndpoints((prev) =>
        prev.length === next.length && prev.every((v, i) => v === next[i]) ? prev : next,
      );
    };
    async function poll() {
      const resolved = keys.map((boundKey) => ({
        boundKey,
        ep: endpointKey(boundKey),
        c: resolveBoundLoopConnection(boundKey).connection,
      }));
      const targets = resolved.filter((item): item is { boundKey: string; ep: string; c: Connection } => !!item.c);
      const stale = new Set(resolved.filter((item) => !item.c).map((item) => item.ep));
      if (targets.length) {
        const answers = await Promise.allSettled(
          targets.map(async (t) => ({ t, answer: await readSkipLoops(t.c, controller.signal) })),
        );
        if (controller.signal.aborted) return;
        for (let i = 0; i < answers.length; i++) {
          const r = answers[i];
          if (r.status === "fulfilled") {
            publishReplicationLoops({
              key: connectionKey(r.value.t.c),
              label: connectionLabel(r.value.t.c),
              report: r.value.answer.loops,
            });
          } else {
            stale.add(targets[i].ep);
          }
        }
      }
      if (!controller.signal.aborted) {
        updateStale(stale);
        timer = window.setTimeout(() => { void poll(); }, LOOP_POLL_MS);
      }
    }
    timer = window.setTimeout(() => { void poll(); }, LOOP_POLL_MS);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [otherActiveLoopKeys, resolveBoundLoopConnection, publishReplicationLoops]);
  const staleLoopEndpoints = useMemo(() => {
    const set = new Set(pollerStaleEndpoints);
    if (otherActiveLoopKeys) {
      for (const boundKey of otherActiveLoopKeys.split("\n")) {
        if (!resolveBoundLoopConnection(boundKey).connection) {
          set.add(endpointKey(boundKey));
        }
      }
    }
    return [...set];
  }, [pollerStaleEndpoints, otherActiveLoopKeys, profiles, profilesLoaded, profilesFailed, groupActive, requestKey, resolveBoundLoopConnection]);
  const [otherLoopStopping, setOtherLoopStopping] = useState<{ key: string; id: string } | null>(null);
  const otherLoopStoppingRef = useRef(new Set<string>());
  const stopOtherLoop = useCallback(async (bound: Bound<SkipLoop[]>, loop: SkipLoop) => {
    const id = loop.job_id;
    const stopKey = id ? `${bound.key}:${id}` : "";
    if (!id || otherLoopStoppingRef.current.has(stopKey)) return;
    const { connection: c, reason } = resolveBoundLoopConnection(bound.key);
    if (!c) {
      setOtherLoopErrors((prev) => ({ ...prev, [stopKey]: reason }));
      return;
    }
    setOtherLoopErrors((prev) => {
      if (!(stopKey in prev)) return prev;
      const next = { ...prev };
      delete next[stopKey];
      return next;
    });
    otherLoopStoppingRef.current.add(stopKey);
    setOtherLoopStopping({ key: bound.key, id });
    try {
      await stopSkipLoop(c, id);
      const answer = await readSkipLoops(c);
      publishReplicationLoops({ key: connectionKey(c), label: connectionLabel(c), report: answer.loops });
    } catch {
      // Banner stays quiet; the member's own view shows status errors.
    } finally {
      otherLoopStoppingRef.current.delete(stopKey);
      setOtherLoopStopping((pending) => (pending?.key === bound.key && pending.id === id ? null : pending));
    }
  }, [resolveBoundLoopConnection, publishReplicationLoops]);
  const otherReplicationLoops = useMemo(
    () => shellActiveLoops
      .filter((bound) => endpointKey(bound.key) !== endpointKey(replicationKey))
      .map((bound) => {
        const pid = keyProfileId(bound.key);
        const member = pid ? groupActive?.members.find((m) => m.profile_id === pid) : undefined;
        const profile = !member && pid ? profiles.find((p) => p.id === pid) : undefined;
        const server = member
          ? `${member.profile_name} · ${member.host}:${member.port}${member.role ? ` · ${member.role.toUpperCase()}` : ""}`
          : profile
            ? `${profile.name} · ${profile.host}:${profile.port}`
            : bound.label;
        return { bound, server, stale: staleLoopEndpoints.includes(endpointKey(bound.key)) };
      }),
    [shellActiveLoops, replicationKey, groupActive, profiles, staleLoopEndpoints],
  );
  // Once the first screen is up, load the code of the places opened most
  // (Server, Workload and Tuning, Replication, the Plans chart) in idle time,
  // so their first visit does not wait. Nothing mounts or reads a server.
  useEffect(() => {
    if (!ready) return;
    // A session restored on Plans or Compare gets its chunks now (all three
    // together), not when the idle queue reaches them.
    if (mode === "inspect" || mode === "compare") void PlanScreen.preload().catch(() => {});
    if (ready) preloadWhenIdle([CommandPalette, Server, ReplicationPlace, TopologyPlace, ClusterPlace, ProxySqlPlace, ReadysetPlace, FilesPlace, QueryDigestView, SchemaVersionsPlace, { preload: preloadWhyDisabled }, { preload: () => PlanScreen.preload().then(() => import("./PlanScreen")).then((m) => m.preloadPlanViews()) }]);
  }, [ready]);
  // Replication's and Query digest's "How to turn it on" open Server > Instruments at that row.
  const [instrumentsLink, setInstrumentsLink] = useState<{ row: string; n: number } | null>(null);
  const openInstrumentsRow = useCallback((row: string) => {
    const back = linkOrigin();
    setInstrumentsLink((l) => ({ row, n: (l?.n || 0) + 1 }));
    setMode("server");
    pendingServerView.current = "instruments";
    finishLink(back);
    setError("");
    setNotice("");
  }, []);

  function go(next: Place, restoring = false) {
    if (!restoring) traversing.current = null;
    pendingObjectLink.current = null;
    if (next === "learn") {
      setLesson("");
      setReturnToPlan(false);
    }
    setMode(next === "plans" ? plansView : PLACE_MODE[next]);
    setLinkBack(null);
    setError("");
    setNotice("");
    setRestored(null);
    setConfirmFresh(false);
    // The removed-member notice is consumed when the place that rendered it
    // (Server's view header, or the topbar of Readyset, ProxySQL, Cluster and
    // Replication) is left. Places without a note never showed it, so leaving
    // them keeps it until it is seen once.
    if (mode === "server" || mode === "workload" || mode === "tuning" || mode === "diagram" ||
        mode === "readyset" || mode === "proxysql" || mode === "cluster" || mode === "replication") setMemberNote("");
  }
  /** The Audit log (Settings): a screen of its own, not a place. */
  function openAuditLog() {
    setMode("audit");
    setAuditFocus("");
    setError("");
    setNotice("");
    setRestored(null);
    setConfirmFresh(false);
  }
  /** Settings: a page of its own, not a place in the sidebar's navigation.
   * The gear, Command-comma or Ctrl-comma, and the palette open it; Back and Esc return
   * to where the user was. */
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("appearance");
  const [settingsFocusRequest, setSettingsFocusRequest] = useState(0);
  function openSettings(section: SettingsSection = "appearance") {
    traversing.current = null;
    pendingObjectLink.current = null;
    setSettingsSection(section);
    setSettingsFocusRequest((request) => request + 1);
    setMode("settings");
    setLinkBack(null);
    setError("");
    setNotice("");
    setRestored(null);
    setConfirmFresh(false);
  }
  const openSettingsRef = useRef(openSettings);
  openSettingsRef.current = openSettings;
  // Esc on the Settings page is Back: the previous history stop, or Home when
  // the page was the first one. A reload that restored the Settings page can
  // leave several Settings entries in a row; step past them.
  const settingsBack = useCallback(() => {
    const h = navHistory.current;
    let index = h.index - 1;
    while (index >= 0 && h.entries[index].mode === "settings") index--;
    if (index >= 0) applyNavEntryRef.current(h.entries[index], index);
    else go("home");
  }, []);
  // A reviewed change's result opens the Audit log at its record
  // (ActionReview.tsx openAuditLog sends "myflames-open-audit" with the id).
  const [auditFocus, setAuditFocus] = useState("");
  const openAuditRef = useRef(openAuditLog);
  openAuditRef.current = openAuditLog;
  useEffect(() => {
    const open = (e: Event) => {
      const back = linkOrigin();
      openAuditRef.current();
      setAuditFocus(String((e as CustomEvent).detail || ""));
      finishLink(back);
    };
    window.addEventListener("myflames-open-audit", open);
    return () => window.removeEventListener("myflames-open-audit", open);
  }, []);
  // The write safety of the connection in use, first in the window footer of
  // the places that send to it (Query editor, Server, Workload, Replication, Cluster,
  // Tuning). Unknown (not loaded, or the read failed) is Production assumed.
  // A Group connection names the group and offers the group editor beside it.
  const safetyStatus = ready && connectionPlace && !!placeRequest.host ? (
    <>
      <SafetyStatus
        safety={{ ...safety, type: placeRequest.type === "development" ? "development" : "production", read_only: placeRequest.read_only === true }}
        name={groupActive ? groupConnectionLabel(groupActive.group_name, groupActive.members.length) : labState.connection.name || connectionTarget(labState.connection)}
        unknown={profilesFailed || "loading"}
        onRetry={() => void refreshProfiles().catch(() => {})}
      />
      {groupActive && (
        <button
          type="button"
          className="button small statusbar-group-edit"
          data-action="edit-group"
          onClick={() => setGroupEditor(groupActive.group_name)}
        >
          Edit group…
        </button>
      )}
    </>
  ) : null;
  function manageConnections() {
    go("home");
    window.setTimeout(() => {
      const table = document.getElementById("connections-table");
      table?.scrollIntoView({ block: "nearest" });
      table?.focus({ preventScroll: true });
    });
  }
  // New connection… (picker and palette): Home's Connections opens its Add
  // connection form once for each request, then clears it.
  const [addConnection, setAddConnection] = useState(0);
  function newConnection() {
    go("home");
    setAddConnection((n) => n + 1);
  }
  // The Query editor's schema browser opens Indexes or Tables on one table.
  const [serverLink, setServerLink] = useState<{
    view: "indexes" | "tables" | "health";
    schema: string;
    table: string;
    from: string;
    n: number;
  } | null>(null);
  const openServerTable = useCallback((view: "indexes" | "tables", schema: string, table: string, from: string) => {
    const back = linkOrigin();
    const group = groupActiveRef.current;
    const member = group?.members.find((m) => {
      const via = memberRequest(m.profile_id);
      return via && connectionKey(via) === from;
    });
    if (member) chooseMember("server", member.profile_id);
    setServerLink((l) => ({ view, schema, table, from, n: (l?.n || 0) + 1 }));
    setMode("server");
    pendingServerView.current = view;
    finishLink(back);
    setError("");
    setNotice("");
  }, [profiles, chooseMember]);
  const openDigest = useCallback(
    (row: TopStatement, rank: number, action: DigestAction, expect?: string) => {
      const back = linkOrigin();
      withLab((lab) => lab.openDigest(row, rank, action, expect));
      setMode("query");
      finishLink(back);
      setError("");
      setNotice("");
    },
    [],
  );
  const showPlan = useCallback(
    (c: Capture, planName: string) => capture(c, planName),
    [capture],
  );
  const addPlan = useCallback(
    (c: Capture, planName: string, cancelled?: () => boolean) =>
      capture(c, planName, { open: false, cancelled }),
    [capture],
  );
  const openImport = () => {
    setNotebookOpen(false);
    setError("");
    setImportOpen(true);
  };
  const openNotebook = () => {
    if (!ready || !plans.length || document.querySelector("dialog[open]")) return;
    setError("");
    saveAction.reset();
    setNotebookOpen(true);
  };
  const openSample = () => {
    if (busy) return;
    // The sample plan text loads on demand: the first screen never shows it.
    void sampleAction.run((signal) => import("./example.json").then(({ default: example }) =>
      importPlan(JSON.stringify(example), "sample-plan.json", undefined, { origin: "sample", signal })),
      { pending: "Analyzing the sample plan…" });
  };
  // Account and privileges of the connection in use (picker and palette).
  const [accountOpen, setAccountOpen] = useState(false);
  const picker = (
    <ConnectionPicker
      profiles={profiles}
      active={activeConnection}
      safety={safety}
      onSwitch={switchConnection}
      onPassword={setActivePassword}
      onPasswordCommit={() => setPasswordRevision(value => value + 1)}
      onManage={manageConnections}
      onAdd={newConnection}
      onAccount={() => setAccountOpen(true)}
      onOpenGroup={(name) => void openGroup(name, { probe: true })}
      onEditGroup={(name) => setGroupEditor(name)}
      activeGroup={groupActive ? { name: groupActive.group_name, count: groupActive.members.length } : null}
      note={[connectionNote, mode === "query" && !queryRequest
        ? "This tab has no open connection. Account and privileges are unavailable."
        : groupActive ? `Account and privileges shown for ${placeConnectionName}.` : ""].filter(Boolean).join(" ")}
    />
  );
  // Server owns its Refresh all state. Measure its existing action strip so
  // the shell reserves exactly that space in the shared toolbar, including
  // the progress menu. This adapter leaves Server's operation lifecycle intact.
  useLayoutEffect(() => {
    const root = chrome.current;
    if (!root || (mode !== "server" && mode !== "workload")) { setPlaceActionsWidth(0); return; }
    let observer: ResizeObserver | null = null;
    const attach = () => {
      const actions = root.querySelector<HTMLElement>(".server-place > .place-toolbar");
      if (!actions) return false;
      const measure = () => setPlaceActionsWidth(actions.getBoundingClientRect().width);
      measure();
      observer = new ResizeObserver(measure);
      observer.observe(actions);
      return true;
    };
    if (attach()) return () => observer?.disconnect();
    setPlaceActionsWidth(0);
    // On its first visit the Server place mounts once its chunk has loaded
    // (lazy.tsx): measure the strip when it appears.
    const appear = new MutationObserver(() => { if (attach()) appear.disconnect(); });
    appear.observe(root, { childList: true, subtree: true });
    return () => { appear.disconnect(); observer?.disconnect(); };
  }, [ready, mode, resetKey]);
  const statusScope = mode === "query" ? `query:${connectionKey(placeRequest)}:${labState.tab_id}`
    : (mode === "digest" || mode === "diagram" || mode === "schema" || mode === "server" || mode === "workload" || mode === "topology" || mode === "replication" || mode === "cluster" || mode === "proxysql" || mode === "readyset" || mode === "tuning") ? `${mode}:${connectionKey(placeRequest)}` : mode === "compare" ? `compare:${beforeId}:${afterId}` : (mode === "inspect" ? `plans:${activeId}` : `place:${mode}`);
  const plansStatus = mode === "inspect" || mode === "compare";
  const connectedRequest = useRef(request);
  connectedRequest.current = request;

  // The Server and Workload list of views floats (opened from the view's
  // title) unless the person docked it: only a stored "false" docks it. This
  // is the one copy of the preference; Server reads it from here.
  const uiPreferences = useUiPreferences();
  const serverNavCollapsed = uiPreferences.server_nav_collapsed;
  const toggleServerNavCollapsed = useCallback(() => {
    void setUiPreference("server_nav_collapsed", !currentUiPreferences().server_nav_collapsed).catch(() => {});
  }, []);
  function chooseServerView(viewId: ServerKind) {
    traversing.current = null;
    // Database diagram is a place of its own now: an old route to it opens it.
    if (viewId === "diagram") {
      go("diagram");
      return;
    }
    if (WORKLOAD_KINDS.includes(viewId)) {
      if (mode !== "workload") go("workload");
    } else {
      if (mode !== "server") go("server");
    }
    pendingServerView.current = viewId;
    setServerNavigation((n) => ({ view: viewId, n: (n?.n || 0) + 1 }));
  }

  const [commandPaletteOpen, setCommandPaletteOpen] = useState(false);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCommandPaletteOpen((open) => !open);
      } else if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        // ⌘, / Ctrl+, opens Settings; an open dialog keeps its keys.
        if (document.querySelector("dialog[open]")) return;
        e.preventDefault();
        openSettingsRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  // ⌘K: recent views lead the empty palette; from two characters the cached
  // schema catalog answers first, then one bounded server search adds objects.
  const [paletteQuery, setPaletteQuery] = useState("");
  useEffect(() => {
    if (!commandPaletteOpen) setPaletteQuery("");
  }, [commandPaletteOpen]);
  const paletteConnected = !!(request.host || request.profile_id);
  const objectCatalog = useSchemaCatalog(request, commandPaletteOpen && paletteConnected && paletteQuery.trim().length >= 2);
  const [objectSearch, setObjectSearch] = useState<{ key: string; text: string; searching: boolean; error: string; matches: CatalogObject[]; truncated: boolean }>(
    { key: "", text: "", searching: false, error: "", matches: [], truncated: false });
  const paletteSearchRef = useRef({ key: navKey, text: "", open: false });
  paletteSearchRef.current = { key: navKey, text: paletteQuery.trim(), open: commandPaletteOpen };
  useEffect(() => {
    const text = paletteQuery.trim();
    if (!commandPaletteOpen || !paletteConnected || text.length < 2) {
      setObjectSearch({ key: navKey, text: "", searching: false, error: "", matches: [], truncated: false });
      return;
    }
    const controller = new AbortController();
    const from = { ...connectedRequest.current };
    const current = () => !controller.signal.aborted && paletteSearchRef.current.open &&
      paletteSearchRef.current.key === navKey && paletteSearchRef.current.text === text;
    setObjectSearch({ key: navKey, text, searching: true, error: "", matches: [], truncated: false });
    const timer = window.setTimeout(() => {
      if (!current()) return;
      searchObjects(from, text, controller.signal).then(
        (result) => { if (current()) setObjectSearch({ key: navKey, text, searching: false, error: "", matches: result.objects, truncated: result.truncated }); },
        (e: unknown) => { if (current()) setObjectSearch({ key: navKey, text, searching: false, error: message(e), matches: [], truncated: false }); },
      );
    }, 250);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [paletteQuery, commandPaletteOpen, paletteConnected, navKey]);
  const objectSearchHere = objectSearch.key === navKey && objectSearch.text === paletteQuery.trim();
  /** A palette object: a table or view is revealed in the Query editor's
   * schema browser; a routine, trigger or event opens its SHOW CREATE in a
   * new tab (the schema browser's Show CREATE path), selected, not run. */
  function openObject(o: CatalogObject | { kind: "schema"; schema: string; name: string }, from: string) {
    // The palette defers selection until after it closes; the lazy editor
    // can defer it again. Check both boundaries against the active identity.
    if (navStateRef.current.key !== from) return;
    const back = linkOrigin();
    go("query");
    const pending = { connection: from };
    pendingObjectLink.current = pending;
    withLab((lab) => {
      if (pendingObjectLink.current !== pending) return;
      pendingObjectLink.current = null;
      if (navStateRef.current.key !== from) {
        setNavRevision((v) => v + 1);
        return;
      }
      const kind = o.kind;
      if (kind === "procedure" || kind === "function" || kind === "trigger" || kind === "event")
        // Open the tab in the same update as finishLink: deferring this until
        // after history records the destination loses a same-place Back stop.
        lab.openShowCreate(kind, o.schema, o.name);
      else lab.revealInSchemaBrowser(o.schema, kind === "schema" ? null : o.name);
      finishLink(back);
    });
  }
  const recentCommands = useMemo((): WorkspaceCommand[] => {
    const h = navHistory.current;
    const seen = new Set<string>();
    const current = h.entries[h.index];
    if (current) seen.add(JSON.stringify([current.mode, current.view, current.connection, current.selection]));
    const out: WorkspaceCommand[] = [];
    for (let i = h.entries.length - 1; i >= 0 && out.length < 5; i--) {
      const entry = h.entries[i];
      const key = JSON.stringify([entry.mode, entry.view, entry.connection, entry.selection]);
      if (seen.has(key)) continue;
      seen.add(key);
      const at = i;
      out.push({
        id: `recent-${at}`, label: entry.label, group: "Recent", icon: entry.icon,
        run: () => applyNavEntryRef.current(entry, navHistory.current.entries.indexOf(entry)),
      });
    }
    return out;
    // Rebuilt when the history moves (navRevision), not per render.
  }, [navRevision]);
  const objectCommands = useMemo((): WorkspaceCommand[] => {
    const text = paletteQuery.trim().toLocaleLowerCase();
    if (text.length < 2 || !paletteConnected) return [];
    const out: WorkspaceCommand[] = [];
    const seen = new Set<string>();
    // Cached catalog first: the schemas already read for this connection.
    for (const s of objectCatalog.catalog?.schemas || []) {
      if (!s.name.toLocaleLowerCase().includes(text)) continue;
      seen.add(`schema ${s.name}`);
      out.push({
        id: `object-schema-${s.name}`, label: s.name, group: "Database", icon: OBJECT_ICONS.schema,
        run: () => openObject({ kind: "schema", schema: s.name, name: s.name }, navKey),
      });
    }
    for (const o of objectSearchHere ? objectSearch.matches : []) {
      const key = `${o.kind} ${o.schema} ${o.name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        id: `object-${key}`, label: `${o.schema}.${o.name}`, group: OBJECT_GROUP_LABELS[o.kind] || "Object",
        icon: OBJECT_ICONS[o.kind] || "table",
        run: () => openObject(o, objectSearch.key),
      });
    }
    return out;
  }, [paletteQuery, objectSearch, objectSearchHere, objectCatalog.catalog, paletteConnected, navKey]);

  const workspaceCommands: WorkspaceCommand[] = [
    // Places in the sidebar's order, then what belongs to one of them.
    { id: "place-home", label: "Home", group: "Places", icon: "home", run: () => go("home") },
    { id: "place-server", label: "Server", group: "Places", icon: "server", keywords: "database status", run: () => go("server") },
    { id: "place-workload", label: "Workload", group: "Places", icon: "statements", keywords: "queries top processes waits io", run: () => go("workload") },
    { id: "place-topology", label: "Topology", group: "Places", icon: "topology", keywords: "connection group members parameters accounts plugins map servers fleet drift", run: () => go("topology") },
    { id: "place-replication", label: "Replication", group: "Places", icon: "replication", keywords: "slave replica topology channels lag gtid", run: () => go("replication") },
    { id: "place-cluster", label: "Cluster", group: "Places", icon: "cluster", keywords: "galera percona xtradb pxc innodb cluster group replication wsrep members quorum router", run: () => go("cluster") },
    { id: "place-proxysql", label: "ProxySQL", group: "Places", icon: "proxy", keywords: "proxysql proxy hostgroups mysql_servers query rules digest connection pool admin", run: () => go("proxysql") },
    { id: "place-readyset", label: "Readyset", group: "Places", icon: "readyset", keywords: "readyset cache shallow deep querypilot resnapshot cdc replication", run: () => go("readyset") },
    { id: "place-tuning", label: "Tuning", group: "Places", icon: "tuning", keywords: "mysql tuning mariadb tuning advisor health score optimization", run: () => go("tuning") },
    { id: "place-files", label: "File transfer", group: "Places", icon: "files", keywords: "files sftp ssh transfer upload download scp filezilla logs dump", run: () => go("files") },
    { id: "place-query", label: "Query editor", group: "Places", icon: "query", keywords: "sql run query editor", run: () => go("query") },
    { id: "place-digest", label: "Query digest", group: "Places", icon: "analysis", keywords: "pt-query-digest digest profile statements workload slow queries", run: () => go("digest") },
    { id: "place-diagram", label: "Database diagram", group: "Places", icon: "diagram", keywords: "schema diagram er erd mermaid tables relationships foreign keys", run: () => go("diagram") },
    { id: "place-schema", label: "Schema versions", group: "Places", icon: "branches", keywords: "schema version versions snapshot compare diff migration drift ddl alter", run: () => go("schema") },
    { id: "place-plans", label: "Plans", group: "Places", icon: "plan", keywords: "explain analyze flame graph visual explain", run: () => go("plans") },
    { id: "place-investigations", label: "Investigations", group: "Places", icon: "investigation", keywords: "saved library notes", run: () => go("investigations") },
    { id: "place-learn", label: "Learn", group: "Places", icon: "learn", keywords: "lessons teach algorithms", run: () => go("learn") },
    // The Full analysis is the Tuning place's second tab (Server.tsx choose).
    { id: "tuning-report", label: "Full analysis", group: "Tuning", icon: "analysis", keywords: "full analysis report findings tuner", run: () => { go("tuning"); setServerNavigation((n) => ({ view: "report", n: (n?.n || 0) + 1 })); } },
    // Compare is a mode of Plans (vocabulary), not a place.
    { id: "plans-compare", label: "Compare", group: "Plans", icon: "compare", keywords: "diff compare plans before after", run: () => setMode("compare") },
    ...SERVER_VIEWS.map((v) => ({
      id: `server-${v.id}`,
      label: v.label,
      group: `Server · ${v.group}`,
      icon: v.icon,
      keywords: `${v.label} ${v.group} server`,
      run: () => { go("server"); chooseServerView(v.id); },
    })),
    ...WORKLOAD_VIEWS.map((v) => ({
      id: `workload-${v.id}`,
      label: v.label,
      group: `Workload · ${v.group}`,
      icon: v.icon,
      keywords: `${v.label} ${v.group} workload`,
      run: () => { go("workload"); chooseServerView(v.id); },
    })),
    ...THEME_CHOICES.map(theme => ({
      id: `theme-${theme.id}`, label: `Theme: ${theme.name}`, group: "Appearance",
      icon: "settings" as const,
      run: () => applyTheme(theme.id),
    })),
    { id: "action-import", label: "Import plan", group: "Actions", icon: "import", keywords: "open json explain analyze", run: openImport },
    { id: "action-audit", label: "Audit log", group: "Actions", icon: "audit", keywords: "writes changes kill declined refused log", run: openAuditLog },
    { id: "action-account", label: "Account and privileges", group: "Actions", icon: "key", keywords: "privileges grants roles user account connected as permissions show grants", run: () => setAccountOpen(true) },
    { id: "action-new-connection", label: "New connection", group: "Actions", icon: "plus", keywords: "add connection server host profile new", run: newConnection },
    // Settings is a page (the gear, ⌘,): the palette's one Settings action
    // opens it, at the Query editor section when that is what was asked for.
    { id: "action-settings", label: "Settings", group: "Actions", icon: "settings", keywords: "settings preferences appearance theme dark light query editor startup restore updates news client tools about zoom", run: () => openSettings(/editor/.test(paletteQuery.toLowerCase()) ? "editor" : "appearance") },
    // Query editor actions on the current tab (QueryLabHandle).
    { id: "query-rename-tab", label: "Rename query tab", group: "Query editor", icon: "edit", keywords: "rename tab name title query editor", run: () => { go("query"); withLab((lab) => lab.renameTab()); } },
    { id: "query-format-sql", label: "Format SQL", group: "Query editor", icon: "format", keywords: "format indent beautify pretty sql query editor", run: () => { go("query"); withLab((lab) => lab.formatSql()); } },
    { id: "query-find", label: "Find", group: "Query editor", icon: "search", keywords: "find search text sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("find")); } },
    { id: "query-replace", label: "Find and replace", group: "Query editor", icon: "replace", keywords: "find replace search text sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("replace")); } },
    { id: "query-go-to-line", label: "Go to line", group: "Query editor", icon: "query", keywords: "go to line jump sql query editor", run: () => { go("query"); withLab((lab) => lab.runEditorCommand("goToLine")); } },
    { id: "query-save", label: "Save query…", group: "Query editor", icon: "plus", keywords: "save saved query favorite bookmark snippet folder keep query editor", run: () => { go("query"); withLab((lab) => lab.saveQuery()); } },
  ];

  const anotherDialogOpen = importOpen || confirmFresh || accountOpen || commandPaletteOpen;
  const notebookVisible = notebookOpen && !anotherDialogOpen && ready && plans.length > 0 &&
    (mode === "inspect" || mode === "compare" || mode === "library");
  useEffect(() => {
    if (anotherDialogOpen) setNotebookOpen(false);
  }, [anotherDialogOpen]);

  return (
    <ConnectionInUseProvider connection={placeRequest} connected={placeConnected}>
    <PrivilegesProvider connection={placeRequest} connectRevision={connectRevision + resetKey} passwordRevision={passwordRevision} connected={placeConnected}>
    <PrivilegeWhyHost resetKey={`${mode}:${serverViewNow || ""}:${mode === "query" ? labState.tab_id : ""}:${connectionKey(placeRequest)}:${connectRevision}:${resetKey}:${passwordRevision}`}>
    <div className={`app${sidebarOpen ? "" : " sidebar-collapsed"}`}>
      <a href="#main" className="skip">
        Skip to content
      </a>
      <Sidebar
        logo={logo}
        expanded={sidebarOpen}
        narrow={sidebar.narrow}
        onExpandedChange={setSidebarOpen}
        place={ready ? place : null}
        plans={plans}
        activeId={mode === "inspect" ? activeId : ""}
        baselineId={baseline}
        saveStatus={saveStatus}
        onGo={go}
        onOpenPlan={openPlan}
        onRemovePlan={removePlan}
        onOpenSettings={() => openSettings()}
        onOpenUpdates={() => openSettings("updates")}
        settingsOpen={ready && mode === "settings"}
        placeBadges={{ replication: replicationBadge, cluster: clusterBadge, proxysql: proxyBadge, readyset: readysetBadge }}
        placeMuted={clusterMuted || proxyMuted || readysetMuted || replicationMuted ? {
          ...(replicationMuted ? { replication: NOT_A_REPLICA } : {}),
          ...(clusterMuted ? { cluster: NOT_A_CLUSTER } : {}),
          ...(proxyMuted ? { proxysql: NOT_A_PROXYSQL } : {}),
          ...(readysetMuted ? { readyset: NOT_A_READYSET } : {}),
        } : undefined}
      />
      <div ref={chrome} data-place={place || (mode === "settings" ? "settings" : "audit")} className="workspace" style={{ "--place-actions-width": `${placeActionsWidth}px` } as CSSProperties}>
        <div className="topbar-stack">
        {ready && connectionPlace && <ConnectionTypeStrip safety={{ ...safety, type: placeRequest.type === "development" ? "development" : "production", read_only: placeRequest.read_only === true }} />}
        <header className="topbar window-toolbar" aria-label={`${placeLabel} toolbar`}>
          <SidebarToggle expanded={sidebarOpen} narrow={sidebar.narrow}
            onToggle={() => setSidebar((current) => ({ ...current, open: !current.open }))} />
          <div className="topbar-title">
            {/* The place is named once. Server and Workload name the place
                here and the view in its header (switch views from their own
                list, or the Views menu in a narrow window); Plans names its
                document in the plan header. Replication and Tuning are one
                view whose header already says the place name, so it stays
                here for assistive technology only. */}
            <strong className={mode === "replication" || mode === "topology" || mode === "cluster" || mode === "proxysql" || mode === "readyset" || mode === "tuning" || mode === "digest" || mode === "diagram" || mode === "schema" ? "visually-hidden" : undefined}>{placeLabel}</strong>
          </div>
          {linkBack && (
            <button type="button" className="nav-back-chip" aria-label={`Back to ${linkBack.label}`}
              title={`Back to ${linkBack.label} (${shortcut("[")})`} onClick={() => applyNavEntry(linkBack, navHistory.current.entries.indexOf(linkBack))}>
              <Icon name="back" size={12} />
              <span>{linkBack.label}</span>
            </button>
          )}
          {/* The Server place shows the same note inside its view header. The
              "Using" note is the current place's own (derived from
              placeRequest); the removed-member notice shows beside it, never
              instead of it, and clears on the next navigation. */}
          {(memberNote || placeMemberNote) && (mode === "readyset" || mode === "proxysql" || mode === "cluster" || mode === "replication") && (
            <span className="group-server-note nav-member-note" role="status" title={[memberNote, placeMemberNote].filter(Boolean).join(" ")}>{[memberNote, placeMemberNote].filter(Boolean).join(" ")}</span>
          )}
          {ready && mode === "library" && (
            <Popover label="About Investigations" className="topbar-info">
              <p>Saved collections of plans, notes, and tags on this computer.</p>
            </Popover>
          )}
          {ready && (mode === "inspect" || mode === "compare") && (
            <div className={`plans-bar${mode === "inspect" && active ? " has-active-plan" : ""}`}>
              <div className="segmented" role="group" aria-label="Plans view">
                <button
                  aria-pressed={mode === "inspect"}
                  onClick={() => setMode("inspect")}
                >
                  Explore
                </button>
                <button
                  aria-pressed={mode === "compare"}
                  title="Compare two open plans side by side"
                  onClick={() => setMode("compare")}
                >
                  Compare
                </button>
              </div>
              <button className={`button small plans-import${plans.length ? "" : " primary"}`} onClick={openImport} title="Import plan">
                <Icon name="import" size={14} />
                <span className="topbar-label">Import plan</span>
              </button>
            </div>
          )}
          {ready && connectionPlace && picker}
          {/* Server and Workload draw their place actions (Refresh all, ⓘ,
              the list toggle) over this slot, right after the connection
              picker; the slot keeps their width in the bar. */}
          {ready && (mode === "server" || mode === "workload") && <span className="place-actions-slot" aria-hidden="true" />}
          <div className="topbar-actions">
            {/* ui-architecture: actions stay with the object they act on;
                workspace autosave and investigation edits have distinct names. */}
            {plans.length > 0 && (mode === "inspect" || mode === "compare") && (
              <>
                {(dirty || savedId) && <span className={`save-state${dirty ? " unsaved" : ""}`}>
                  {dirty ? "Unsaved changes" : "Investigation saved"}
                </span>}
                <button className="button small window-save" disabled={busy || saveAction.pending} onClick={openNotebook} aria-label="Save investigation" title="Save investigation: name, notes and tags" aria-haspopup="dialog" aria-expanded={notebookVisible}>
                  <Icon name="investigation" /><span>Save investigation</span>
                </button>
              </>
            )}
            {ready && mode === "home" && (
              // Home's one Import plan, in the top bar with or without recent
              // work; Add connection is Home's primary action.
              <button
                className="button small"
                aria-label="Import plan"
                title="Import plan"
                onClick={openImport}
              >
                <Icon name="import" size={14} />
                <span className="topbar-label">Import plan</span>
              </button>
            )}
            <button
              type="button"
              className="button small ghost command-trigger"
              aria-label={`Search or jump to (${MAC ? "Cmd+K" : "Ctrl+K"})`}
              title={`Search or jump to (${MAC ? "Cmd+K" : "Ctrl+K"})`}
              onClick={() => setCommandPaletteOpen(true)}
              onPointerEnter={warmCommandPalette} onFocus={warmCommandPalette}
            >
              <Icon name="search" size={13} />
              <span>Search…</span>
              <kbd>{shortcut("K")}</kbd>
            </button>
          </div>
        </header>
        </div>
        <main
          id="main"
          style={mode === "inspect" && !active ? { minHeight: "calc(100dvh - var(--topbar-height) - var(--statusbar-h))" } : undefined}
          onDragOver={(e) => {
            if (ready && mode === "inspect" && !active) e.preventDefault();
          }}
          onDrop={(e) => {
            if (ready && mode === "inspect" && !active) {
              e.preventDefault();
              void importFile(e.dataTransfer.files[0]);
            }
          }}
          className={
            mode === "inspect" ||
            mode === "compare" ||
            mode === "server" ||
            mode === "workload" ||
            mode === "replication" ||
            mode === "cluster" ||
            mode === "proxysql" ||
            mode === "readyset" ||
            mode === "tuning" ||
            mode === "files" ||
            mode === "query" ||
            mode === "digest" ||
            mode === "diagram" ||
            mode === "schema" ||
            mode === "audit" ||
            mode === "settings" ||
            mode === "home" ||
            mode === "teach"
              ? "is-flush"
              : undefined
          }
        >
          {error && !importOpen && !notebookVisible && (
            <div role="alert" className="error">
              {error}
              <button
                className="icon-button"
                aria-label="Dismiss error"
                onClick={() => setError("")}
              >
                <Icon name="close" />
              </button>
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              {notice}
              <button className="text-button" onClick={() => setNotice("")}>
                Dismiss
              </button>
            </div>
          )}
          {restored && (
            <div className="notice session-notice" role="status">
              <span>{restored.text}</span>
              <button className="text-button" onClick={() => setRestored(null)}>Dismiss</button>
            </div>
          )}
          {held.length > 0 && (
            <div className="notice held-notice" role="status">
              <span>
                {held.length === 1
                  ? "One plan from your saved workspace could not be reopened. It stays in the saved workspace."
                  : `${held.length} plans from your saved workspace could not be reopened. They stay in the saved workspace.`}
              </span>
              <span className="button-row">
                <button
                  className="text-button"
                  onClick={() =>
                    download(
                      JSON.stringify(
                        {
                          schema_version: "investigation-1.0",
                          name: "Plans that could not be reopened",
                          notes: "",
                          tags: [],
                          baseline_id: "",
                          plans: held,
                        },
                        null,
                        2,
                      ),
                      "unopened-plans.json",
                      "application/json",
                    )
                  }
                >
                  Download {held.length === 1 ? "it" : "them"}
                </button>
                <button className="text-button" onClick={() => setHeld([])}>
                  Remove {held.length === 1 ? "it" : "them"}
                </button>
              </span>
            </div>
          )}
          {!ready && (
            <div className="preview-state" role="status">
              <span className="spinner" />
              Opening your workspace…
            </div>
          )}
          {ready && mode === "home" && (
            <Home
              logo={logo}
              busy={busy}
              plans={plans}
              connections={
                <Connections
                  profiles={profiles}
                  secretStore={secretStore}
                  refresh={refreshProfiles}
                  active={activeConnection}
                  busy={labBusy || batchRunning}
                  onUse={applyConnection}
                  loading={!profilesLoaded && !profilesFailed}
                  loadError={profilesFailed}
                  onActiveDeleted={() => applyConnection("", { ...defaultLab().connection, name: "", host: "", user: "", database: "", ssl_ca: "" }, "")}
                  onGroupSaved={groupSaved}
                  onGroupDeleted={groupDeleted}
                  onOpenGroup={(name) => {
                    // Topology reads the group in use: navigate only once the
                    // requested group is it, so the place's first read never
                    // lands on the previous group.
                    void openGroup(name, { probe: true }).then((opened) => { if (opened) go("topology"); });
                  }}
                  activeGroup={groupActive ? { name: groupActive.group_name, count: groupActive.members.length } : null}
                  addRequest={addConnection}
                  onAddHandled={() => setAddConnection(0)}
                />
              }
              onDropFile={importFile}
              onSample={openSample}
              onOpenPlan={openPlan}
              onOpenBundle={openBundle}
              onInvestigations={() => go("investigations")}
              onQuery={() => go("query")}
              onServer={() => go("server")}
              onDiagram={() => go("diagram")}
              queryTabs={labState.tabs}
              onOpenQuery={(id) => {
                withLab((lab) => lab.openTab(id));
                go("query");
              }}
            />
          )}
          {/* Its own key: Teach, a sibling, is keyed by resetKey too, and two
              equal keys among siblings leave stale QueryLab/Server copies. */}
          {ready && (
            <div hidden={mode !== "query"}>
              <WritePolicyContext.Provider value={writePolicy}>
              <QueryLab
                ref={setLab}
                key={resetKey}
                initial={labInitial}
                connection={request}
                picker={null}
                group={groupActive}
                groupMember={queryMemberId}
                onGroupMember={(id) => chooseMember("query", id)}
                memberConnection={memberRequest}
                batchRunning={batchRunning}
                onStateChange={reportEditor}
                onTabsNow={tabsNow}
                onBusyChange={setLabBusy}
                onConnected={(via) => { if (queryRequestRef.current && connectionKey(via) === connectionKey(queryRequestRef.current)) setConnectRevision(n => n + 1); }}
                onCapture={capture}
                onCompareCaptures={async (before, after) => {
                  const left = await capture(before.capture, before.name, { open: false });
                  const right = await capture(after.capture, after.name, { open: false });
                  if (left && right) { setBeforeId(left.id); setAfterId(right.id); setMode("compare"); }
                }}
                onOpenTable={openServerTable}
                onOpenLocks={() => {
                  if (groupActive && queryRequest?.profile_id) chooseMember("server", queryRequest.profile_id);
                  chooseServerView("locks");
                }}
                onScreen={mode === "query"}
                onOpenInstruments={(row) => {
                  if (groupActive && queryRequest?.profile_id) chooseMember("server", queryRequest.profile_id);
                  openInstrumentsRow(row);
                }}
              />
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountDigest && (
            // Mounted on its first visit and kept, hidden, after it: it reads
            // only while on screen (viewOnScreen.ts), and its rows open the
            // Query editor, which runs them.
            <div className="digest-place" hidden={mode !== "digest"}>
              <ViewOnScreenContext.Provider value={mode === "digest"}>
                {/* Keyed like Server: Start fresh or another tab's newer
                    workspace starts it over even when that workspace is on
                    Query digest too (the wrapper is a sibling of Teach). */}
                <QueryDigestView
                  key={resetKey}
                  connection={request}
                  onOpenInEditor={(row, rank, from) => openDigest(row, rank, "open", from)}
                  onExplain={(row, rank, from) => openDigest(row, rank, "explain", from)}
                  onAnalyze={(row, rank, from) => openDigest(row, rank, "analyze", from)}
                  onOpenSql={(sql, options) => { setMode("query"); withLab(lab => lab.openSql(sql, { ...options, from: connectionKey(request) })); }}
                  settings={{ timeout: labState.timeout, repeat: labState.repeat, server: `${request.host}:${request.port}` }}
                  disabled={labBusy || batchRunning}
                  onOpenInstruments={openInstrumentsRow}
                  onOpenIndexes={(schema, table, from) => openServerTable("indexes", schema, table, from)}
                />
              </ViewOnScreenContext.Provider>
            </div>
          )}
          {ready && mountSchema && (
            // Mounted on its first visit and kept, hidden, after it. It reads
            // the saved versions on open; a database is read only when asked
            // (Save version, Compare). Its sync script opens in the Query
            // editor, which runs it.
            <div className="schema-place" hidden={mode !== "schema"}>
              <ViewOnScreenContext.Provider value={mode === "schema"}>
              <ColumnStoreContext.Provider value={columnStore}>
                <SchemaVersionsPlace key={resetKey} connection={request} profiles={profiles}
                  onOpenSql={(sql) => {
                    const back = linkOrigin();
                    withLab((lab) => lab.openSql(sql, { from: connectionKey(request) }));
                    go("query");
                    finishLink(back);
                  }} />
              </ColumnStoreContext.Provider>
              </ViewOnScreenContext.Provider>
            </div>
          )}
          {ready && mountServer && (
            <div hidden={!serverOnScreen}>
              <WritePolicyContext.Provider value={serverWritePolicy}>
              <ViewOnScreenContext.Provider value={serverOnScreen}>
              <Server
                key={resetKey}
                place={serverPlace}
                onScreen={serverOnScreen}
                onNavigatePlace={(targetPlace, targetView) => {
                  const back = linkOrigin();
                  go(targetPlace);
                  // Tuning, Database diagram, Replication and Cluster show their one view; Server and Workload choose theirs.
                  if (targetPlace !== "tuning" && targetPlace !== "diagram" && targetPlace !== "replication" && targetPlace !== "cluster") {
                    pendingServerView.current = targetView;
                    setServerNavigation((n) => ({ view: targetView, n: (n?.n || 0) + 1 }));
                  }
                  finishLink(back);
                }}
                navCollapsed={serverNavCollapsed}
                onToggleNavCollapsed={toggleServerNavCollapsed}
                picker={null}
                connection={serverRequest}
                placeConnections={{ replication: replicationRequest, cluster: clusterRequest }}
                groupServer={groupActive ? {
                  group: groupActive,
                  member: serverMemberId,
                  note: [memberNote, serverMemberNote].filter(Boolean).join(" "),
                  onChoose: (id: string) => chooseMember("server", id),
                } : null}
                disabled={labBusy}
                navigation={serverNavigation}
                settings={{
                  timeout: labState.timeout,
                  repeat: labState.repeat,
                  server: `${serverRequest.host}:${serverRequest.port}`,
                }}
                initial={labInitial?.slow}
                onStateChange={reportSlow}
                columns={columnLayouts}
                onColumns={setColumnLayouts}
                profiles={profiles}
                replicationLoops={replicationLoops}
                onReplicationLoops={publishReplicationLoops}
                restoredLoopKeys={activeRestoredLoopKeys}
                staleLoopEndpoints={staleLoopEndpoints}
                restoredOutsideNote={restoredOutsideNote}
                resolveMemberConnection={loopMemberRequest}
                replication={replicationReport}
                onReplication={shareReplication}
                cluster={clusterReport}
                onCluster={shareCluster}
                onClusterFailed={failCluster}
                onDigest={openDigest}
                onShowPlan={showPlan}
                onAddPlan={addPlan}
                onOpenPlan={openPlan}
                onBatchChange={setBatchRunning}
                link={serverLink}
                instrumentsLink={instrumentsLink}
                onOpenLesson={(key) => {
                  go("learn");
                  setLesson(key);
                }}
                onOpenSql={(sql, options) => {
                  const back = linkOrigin();
                  withLab((lab) => lab.openSql(sql, { ...options, from: connectionKey(serverRequest) }));
                  go("query");
                  finishLink(back);
                }}
                onOpenInvestigation={(id) => {
                  const back = linkOrigin();
                  setSavedRevision((n) => n + 1);
                  if (id) setSelectedInvestigationId(id);
                  go("investigations");
                  finishLink(back);
                }}
              />
              </ViewOnScreenContext.Provider>
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountReplication && (
            <div hidden={mode !== "replication"}>
              <WritePolicyContext.Provider value={writePolicy}>
              <ViewOnScreenContext.Provider value={mode === "replication"}>
                <ReplicationPlace
                  key={resetKey}
                  connection={replicationRequest}
                  restoredLoop={restoredLoopForCurrent}
                  restoredStoppedKeys={stoppedRestoredLoopKeys}
                  restoredOutsideNote={restoredOutsideNote}
                  disabled={labBusy}
                  clusterKind={clusterReport?.key === connectionKey(replicationRequest) && clusterReport.report.available && clusterReport.report.kind !== "none" ? clusterReport.report.kind : null}
                  onOpenCluster={() => go("cluster")}
                  onOpenReadyset={() => go("readyset")}
                  onOpenProxySql={() => go("proxysql")}
                  onSwitchConnection={switchConnection}
                  onOpenInstruments={openInstrumentsRow}
                  loops={replicationLoops}
                  onLoops={publishReplicationLoops}
                  otherLoops={otherReplicationLoops}
                  otherLoopStopping={otherLoopStopping}
                  otherLoopErrors={otherLoopErrors}
                  onStopOtherLoop={(bound, loop) => { void stopOtherLoop(bound, loop); }}
                  report={replicationReport}
                  onReport={shareReplication}
                />
              </ViewOnScreenContext.Provider>
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountTopology && (
            // The Topology place reads only on a Group connection, and only
            // when a tab first comes on screen or its Refresh is pressed.
            <div hidden={mode !== "topology"}>
              <ViewOnScreenContext.Provider value={mode === "topology"}>
              <ColumnStoreContext.Provider value={columnStore}>
                <TopologyPlace connection={request} group={groupActive} profiles={profiles} disabled={labBusy}
                  focusCard={topologyFocusCard} onCardFocused={clearTopologyFocusCard}
                  onOpenGroup={(name) => void openGroup(name, { probe: true })}
                  onNewGroup={(members) => setGroupCreate(members)}
                  onUseInServer={(id) => chooseMember("server", id)}
                  onOpenCluster={() => go("cluster")} onOpenReplication={() => go("replication")}
                  onOpenAccounts={(id) => { chooseMember("server", id); chooseServerView("accounts"); }}
                  onOpenMember={openTopologyMember} />
              </ColumnStoreContext.Provider>
              </ViewOnScreenContext.Provider>
            </div>
          )}
          {ready && mountCluster && (
            <div hidden={mode !== "cluster"}>
              <WritePolicyContext.Provider value={writePolicy}>
              <ViewOnScreenContext.Provider value={mode === "cluster"}>
              <ColumnStoreContext.Provider value={columnStore}>
                <ClusterPlace connection={clusterRequest} disabled={labBusy} report={shownClusterReport} onReport={shareCluster}
                  failure={clusterFailure} onFailure={failCluster}
                  onOpenProxySql={() => go("proxysql")} onOpenReadyset={() => go("readyset")}
                  profiles={profiles} onSwitchConnection={switchConnection}
                  onEditGroup={setGroupEditor} groupRevision={groupRevision} />
              </ColumnStoreContext.Provider>
              </ViewOnScreenContext.Provider>
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountProxySql && (
            <div hidden={mode !== "proxysql"}>
              <WritePolicyContext.Provider value={writePolicy}>
              <ViewOnScreenContext.Provider value={mode === "proxysql"}>
              <ColumnStoreContext.Provider value={columnStore}>
                <ProxySqlPlace connection={proxyRequest} disabled={labBusy} report={proxyReport} onReport={shareProxySql}
                  failure={proxyFailure} onFailure={failProxySql}
                  onSwitchConnection={switchConnection} onOpenReadyset={() => go("readyset")}
                  onOpenCluster={() => go("cluster")} onOpenReplication={() => go("replication")} />
              </ColumnStoreContext.Provider>
              </ViewOnScreenContext.Provider>
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountReadyset && (
            <div hidden={mode !== "readyset"}>
              <WritePolicyContext.Provider value={writePolicy}>
              <ViewOnScreenContext.Provider value={mode === "readyset"}>
              <ColumnStoreContext.Provider value={columnStore}>
                <ReadysetPlace connection={readysetRequest} disabled={labBusy} report={readysetReport} onReport={shareReadyset}
                  failure={readysetFailure} onFailure={failReadyset}
                  onOpenProxySql={() => go("proxysql")} onOpenCluster={() => go("cluster")}
                  onOpenReplication={() => go("replication")} onOpenServer={() => go("server")}
                  onSwitchConnection={switchConnection} />
              </ColumnStoreContext.Provider>
              </ViewOnScreenContext.Provider>
              </WritePolicyContext.Provider>
            </div>
          )}
          {ready && mountFiles && (
            <div hidden={mode !== "files"}>
              <FilesPlace profiles={profiles} />
            </div>
          )}
          {ready && mode === "library" && <Library onOpen={openBundle} openPlanCount={plans.length}
            onSaveInvestigation={openNotebook} savedRevision={savedRevision} initialSelectedId={selectedInvestigationId} />}
          {ready && mode === "audit" && <AuditLog key={auditFocus} profiles={profiles} focusId={auditFocus} />}
          {ready && mode === "settings" && (
            <SettingsPage
              logo={logo}
              section={settingsSection}
              focusRequest={settingsFocusRequest}
              onSection={setSettingsSection}
              onBack={settingsBack}
              ready={ready}
              restoreEnabled={restoreEnabled}
              skipRestore={skipRestore}
              onToggleRestore={toggleRestore}
              onStartFresh={() => setConfirmFresh(true)}
              onAuditLog={openAuditLog}
            />
          )}
          {mountTeach && (
            // Mounted on its first visit and kept, hidden, after it, like the
            // other lazy places: its chunk's loading or failure text shows
            // only while Learn is the place on screen.
            <div hidden={mode !== "teach"}>
              <Teach
                key={resetKey}
                active={ready && mode === "teach"}
                onLessonChange={setTeachLesson}
                initialLesson={lesson}
                onReturn={
                  returnToPlan
                    ? () => {
                        setMode("inspect");
                        setReturnToPlan(false);
                      }
                    : undefined
                }
              />
            </div>
          )}
          {(mode === "inspect" || mode === "compare") && (
            <PlanScreen
              {...{
                  ready, mode, active, openPlan, plans, openImport, setMode, exportJson,
                  hideLiterals, setHideLiterals, exporting, exportHtml, sampleAction,
                  busy, openSample, exportAction, exportRetry,
                  planHeaderTools, exclusivePanel, setNotice, setError, misestimateNotShown,
                  misestimateNote, view, metric, focusId, collapsed, selectedId, chartSelect, statsIds,
                  setFocusId, setCollapsed, setStatsIds, metricsTarget, reveal, setMisestimateNotShown, setMisestimateNote,
                  setView, setMetric, setLesson, setReturnToPlan, setMetricsTarget,
                  search, setSearch, selectOperator, setBeforeId,
                  beforeId, afterId, setAfterId, comparison, loading,
                  before, after, pair, compareSelect, setPair, setSelectedId, setReveal,
              }}
            />
          )}
          {!notebookVisible && <ActionStatus state={saveAction} />}
        </main>
        {ready && <WindowStatusBar scope={statusScope} lead={safetyStatus}
          fallback={plansStatus ? {
            engineVersion: mode === "inspect" && active?.capture?.server_version ? `${engineName(active.capture.engine)} ${active.capture.server_version}` : undefined,
            connectionState: mode === "compare" ? "Compared plans" : active?.capture ? "Captured plan" : active?.origin === "sample" ? "Sample plan" : active ? "Imported plan" : "No plan selected",
          } : connectionPlace ? { connectionState: placeRequest.host ? connectionLabel(placeRequest) : "No connection" } : {}} />}
      </div>
      <input
        ref={file}
        type="file"
        accept=".json,application/json"
        hidden
        onChange={(e) => {
          importFile(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      {notebookVisible && (
        <NotebookSheet
          open={notebookOpen}
          busy={saveAction.pending}
          error={notebookOpen ? error : ""}
          action={saveAction}
          onClose={() => setNotebookOpen(false)}
          onSave={async () => { if (await save()) setNotebookOpen(false); }}
          onExport={() =>
            download(
              JSON.stringify(makeBundle(), null, 2),
              `${name.replace(/[^a-z0-9_-]/gi, "-") || "investigation"}.json`,
              "application/json",
            )
          }
        >
            <section className="investigation-notes" aria-label="Investigation notebook">
              <div className="notebook-grid">
                <label className="field">
                  Investigation name
                  <input
                    value={name}
                    onChange={(e) => {
                      setName(e.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                {active && (
                  <label className="field">
                    Plan name
                    <input
                      value={active.name}
                      onChange={(e) => {
                        setPlans((ps) =>
                          ps.map((p) =>
                            p.id === activeId
                              ? { ...p, name: e.target.value }
                              : p,
                          ),
                        );
                        setDirty(true);
                      }}
                    />
                  </label>
                )}
                <label className="field">
                  Baseline
                  <select
                    value={baseline}
                    onChange={(e) => {
                      setBaseline(e.target.value);
                      setBeforeId(e.target.value);
                      setDirty(true);
                    }}
                  >
                    {plans.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  Tags
                  <input
                    placeholder="payments, index-review"
                    value={tags}
                    onChange={(e) => {
                      setTags(e.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
              </div>
              <label className="field">
                Notes and conclusions
                <textarea
                  placeholder="What changed? What did the evidence show?"
                  value={notes}
                  onChange={(e) => {
                    setNotes(e.target.value);
                    setDirty(true);
                  }}
                />
              </label>
              <p className="hint">
                Save keeps the open plans, these notes and tags on this
                computer. Credentials are excluded. Edits here are kept in the
                workspace even if you close this sheet without saving.
              </p>
            </section>
        </NotebookSheet>
      )}
      {importOpen && (
        <ImportDialog
          busy={busy}
          error={error}
          onClose={() => {
            setImportOpen(false);
            setError("");
          }}
          onFile={(f) => {
            if (f) importFile(f);
            else file.current?.click();
          }}
          onPaste={(raw, n) => {
            importPlan(raw, n).catch(() => {});
          }}
        />
      )}
      {confirmFresh && <FreshWorkspaceSheet
        backupAvailable={!!plans.length && (!savedId || dirty)}
        onClose={() => setConfirmFresh(false)}
        onStart={(backup) => { void startFresh(backup); }}
      />}
      {accountOpen && (
        <AccountPrivilegesSheet
          name={placeConnectionName}
          hasConnection={!!placeRequest.host}
          onClose={() => setAccountOpen(false)}
        />
      )}
      {groupEditor && (
        <GroupEditorSheet
          group={groupEditor}
          profiles={profiles}
          onSaved={groupSaved}
          onDeleted={groupDeleted}
          onClose={() => setGroupEditor(null)}
        />
      )}
      {groupCreate && (
        <GroupEditorSheet
          group={null}
          initialMembers={groupCreate}
          profiles={profiles}
          onSaved={groupSaved}
          onDeleted={groupDeleted}
          onClose={() => setGroupCreate(null)}
        />
      )}
      {commandPaletteOpen && (
        <CommandPalette
          commands={workspaceCommands}
          recent={recentCommands}
          objects={objectCommands}
          objectStatus={objectSearchHere ? objectSearch : { searching: paletteConnected && paletteQuery.trim().length >= 2, error: "", truncated: false }}
          onQueryChange={setPaletteQuery}
          onClose={() => setCommandPaletteOpen(false)}
        />
      )}
    </div>
    </PrivilegeWhyHost>
    </PrivilegesProvider>
    </ConnectionInUseProvider>
  );
}
const root = createRoot(document.getElementById("root")!);
let launchGeneration = 0;
function openWorkspace() {
  const generation = ++launchGeneration;
  authenticate().then(async () => {
    // Reserve the stored width if it arrives promptly; late preferences still
    // update the mounted workspace. The server painted the theme in index.html.
    await loadUiPreferencesWithin().catch(() => {});
    if (generation === launchGeneration) root.render(<WindowStatusProvider><App /></WindowStatusProvider>);
  }).catch(error => {
    if (generation === launchGeneration) root.render(<main className="launch-refusal" role="alert"><div className="launch-brand"><img src="/assets/myflames-logo.png" alt="" width="48" height="48" /><strong>myflames</strong></div><h1>Open your workspace</h1><p>{error instanceof Error ? error.message : "Open the launch URL printed by myflames ui."}</p></main>);
  });
}
window.addEventListener("hashchange", openWorkspace);
openWorkspace();

/** Save investigation: the notebook (name, plan name, baseline, tags and
 * notes) in a sheet over the plan, saved with Save. Its fields edit the
 * workspace directly, so Close keeps them as unsaved changes. */
function NotebookSheet({ open, busy, error, action, onClose, onSave, onExport, children }: {
  open: boolean; busy: boolean; error: string; action: ReturnType<typeof useAction>; onClose: () => void; onSave: () => void; onExport: () => void; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useFocusScope(dialog, { open, onClose });
  return (
    <dialog
      ref={dialog}
      className="notebook-sheet"
      tabIndex={-1}
      aria-labelledby="notebook-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id="notebook-title">Save investigation</h2>
      </div>
      {error && <div className="error" role="alert">{error}</div>}
      <fieldset disabled={busy}>{children}</fieldset>
      <ActionStatus state={action} slowText="No reply yet. Close keeps the save running; its outcome appears in the window." />
      <div className="dialog-actions sheet-actions">
        <button type="button" className="text-button notebook-export" onClick={onExport}>Export investigation bundle</button>
        <button type="button" className="button" onClick={onClose}>Close</button>
        <ActionButton primary busy={busy} busyLabel="Saving…" onClick={onSave}>Save</ActionButton>
      </div>
    </dialog>
  );
}
/** A deliberate workspace action, available even when nothing was restored. */
function FreshWorkspaceSheet({ backupAvailable, onClose, onStart }: {
  backupAvailable: boolean; onClose: () => void; onStart: (backup: boolean) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useFocusScope(dialog, { open: true, onClose });
  return <dialog ref={dialog} className="fresh-workspace-sheet notebook-sheet" tabIndex={-1}
    aria-labelledby="fresh-workspace-title" onCancel={(event) => { event.preventDefault(); onClose(); }}>
    <div className="dialog-heading"><h2 id="fresh-workspace-title">Start fresh</h2></div>
    <p>Close the open plans and query tabs and clear this workspace. Saved investigations and connections remain available.</p>
    {backupAvailable && <p>Download a backup of your unsaved plans before starting fresh? Query tab text is not included in this backup.</p>}
    <div className="dialog-actions sheet-actions">
      <button type="button" className="button" onClick={onClose}>Cancel</button>
      <button type="button" className="button" onClick={() => onStart(false)}>{backupAvailable ? "Start fresh without a backup" : "Start fresh"}</button>
      {backupAvailable && <button type="button" className="button primary" onClick={() => onStart(true)}>Download backup and start fresh</button>}
    </div>
  </dialog>;
}
