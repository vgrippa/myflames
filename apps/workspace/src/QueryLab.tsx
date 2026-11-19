import { setUiPreference, useUiPreferences, type ExplainFormat } from "./uiPreferences";
import { captureThreadKey, indexWasApplied, measurementMatches, type SqlOpenOptions, type SuggestedIndexOrigin } from "./statementThread";
import { DigestBefore, MeasuredStatement } from "./MeasureAgain";
import { bytes, invalidateSchemaCatalog, useSchemaCatalog } from "./PerfShared";
import { AlterPredictionWhy, notPredictedReason, type AlterPrediction } from "./AlterPrediction";
import { engineName } from "./OpsShared";
import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type CSSProperties,
} from "react";
import {
  api,
  ddlProgress,
  type DdlProgress,
  ApiError,
  clientMissingCode,
  isClientAuthPlugin,
  type Capture,
  type CatalogTable,
  type Connection,
  type EditorClose,
  type EditorConfirmation,
  type EditorFileOpenReply,
  type EditorFileSaveReply,
  type EditorReply,
  type EditorRows,
  type EditorRun,
  type EditorSession,
  type EditorStop,
  type EditorUseReply,
  type GroupConnection,
  type Plan,
  type SavedQuery,
  type TopStatement,
} from "./api";
import type { DigestAction } from "./SlowQueries";
import { durationText } from "./duration";
import { delimiterAt, describeTarget, insertDefaults, runTarget, selectColumnReferences, sqlRun, type RunTarget } from "./sqlSelection";
import {
  digestName,
  digestQuery,
  fillNotice,
  shortDigest,
  skeleton,
} from "./placeholders";
import {
  DEFAULT_ROW_LIMIT,
  MAX_ROW_LIMIT,
  defaultEditorLayout,
  normalizeEditorLayout,
  normalizeLab,
  normalizeSource,
  type EditorLayout,
  type LabState,
  type QueryTab,
} from "./session";
import { connectionKey, connectionLabel } from "./binding";
import { unavailableMemberConnection } from "./queryConnection";
import { GroupServerPicker } from "./GroupServerPicker";
import { Icon } from "./components";
import { ActionButton, ActionStatus, IDLE_ACTION, writeFailure, type ActionState, Checkbox, ConfirmSheet, EmptyState, IconButton, InfoPopover, Notice, NumberField, Popover, Segmented, Select, StatusPill, useFocusScope, useWindowStatus, type Tone, ContextMenu } from "./kit";
import { ConnectionTypeBand, useWritePolicy, TYPE_LABEL } from "./Connections";
import { ClientMissingNotice } from "./ClientMissing";
import { SqlEditor, type SqlEditorHandle } from "./editor/SqlEditor";
import { normalizeEol, normalizedOffset } from "./editor/types";
import { EDITOR_ACTIONS, EDITOR_SHORTCUTS, editorActionShortcuts } from "./editor/keymap";
import type { Range } from "./editor/types";
import { createsTemporaryTable } from "./editor/problems";
import { EDITOR_SETTING_LABELS, useEditorSettings } from "./editorSettings";
import { ResultsPane, rowResults, duration as resultDuration } from "./ResultsPane";
import { SchemaBrowser, type SchemaReveal } from "./SchemaBrowser";
import { HistoryPanel, type SidePanelTab } from "./HistoryPanel";
import { SaveQuerySheet } from "./SavedQueries";
import { FillParametersSheet } from "./FillParameters";
import { fillFields } from "./fillValues";
import { SafeUpdatesInfo } from "./SafeUpdatesInfo";
import { QueryTabMenu, MAX_TAB_NAME, clipName, copyName } from "./QueryTabMenu";
import { editorPrivilegeGate, isPrivilegeDenial, privilegeButtonProps, usePrivilege, usePrivileges, type EditorPrivilegePreview } from "./privileges";
import { lazyComponent } from "./lazy";
import { MAC } from "./platform";
import "./editor.css";
import "./editor-insights.css";
import { StatementInsight } from "./EditorInsights";
import { DdlMeter, DdlUnavailable, ddlLeft } from "./DdlProgress";
import { followDdlProgress } from "./ddlProgressPoll";
import { useImportCsv } from "./importCsvApi";

// Opened on request (Table data, the Query Digest mode): their chunks load
// the first time they open (lazy.tsx).
const TableData = lazyComponent(() => import("./TableData").then((m) => m.TableData));

/** Visible shortcut text: "⌘Return" on macOS, "Ctrl+Enter" elsewhere. */
/** Format SQL: ⇧⌥F on macOS, Shift+Alt+F elsewhere (VS Code's binding). */
const FORMAT_KEYS = MAC ? "⇧⌥F" : "Shift+Alt+F";
function keys(shift: boolean, key: "Enter" | "E" = "Enter") {
  if (key === "E") return MAC ? `${shift ? "⇧" : ""}⌘E` : `Ctrl+${shift ? "Shift+" : ""}E`;
  return MAC ? `${shift ? "⇧" : ""}⌘Return` : `Ctrl+${shift ? "Shift+" : ""}Enter`;
}
/** A CodeMirror key name ("Shift-Mod-k") as the platform's visible text. */
function editorKeyText(name: string): string {
  const parts = name.split("-");
  let key = parts.pop() || "";
  const names: Record<string, string> = {
    ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→",
    Enter: MAC ? "Return" : "Enter", Escape: "Esc", Space: "Space", Tab: "Tab",
  };
  key = names[key] ?? (key.length === 1 ? key.toUpperCase() : key);
  const has = (mod: string) => parts.some((p) => p.toLowerCase() === mod);
  const order = MAC ? ["ctrl", "alt", "shift", "mod"] : ["ctrl", "mod", "alt", "shift"];
  const mods = order.filter(has).map((mod) =>
    mod === "mod" ? (MAC ? "⌘" : "Ctrl+") : mod === "ctrl" ? (MAC ? "⌃" : "Ctrl+") : mod === "alt" ? (MAC ? "⌥" : "Alt+") : MAC ? "⇧" : "Shift+");
  return `${mods.join("")}${key}`;
}
/** ARIA uses named modifiers and '+' separators, rather than display glyphs. */
function editorAriaKeys(name: string): string {
  return name.split("-").map(key => key === "Mod" ? (MAC ? "Meta" : "Control")
    : key === "Ctrl" ? "Control" : key.length === 1 ? key.toUpperCase() : key).join("+");
}
const FIND_KEYS = editorActionShortcuts("find").map(editorKeyText).join(", ");
const REPLACE_KEYS = editorActionShortcuts("replace").map(editorKeyText).join(", ");
/** How long the statement that runs stays highlighted in the editor. */
const FLASH_MS = 1600;
/** Below this width the schema browser and history open as sheets. */
const NARROW = 720;
/** The results keep their tabs, result bar, column header and three rows:
 * in a short window (the 720x520 minimum) the editor gives way to them. */
const RESULTS_KEEP = 208;
/** RESULTS_KEEP budgets the result toolbar at one line; when it wraps, the
 * extra lines come out of the editor's room too (measureEditorRoom). */
const RESULT_BAR_LINE = 40;
/** The editor itself keeps three lines. */
const EDITOR_FLOOR = 72;
/** Opening the Database picker re-reads the database list only when the
 * cached read is older than this (never on every focus or keystroke). */
const CATALOG_STALE_MS = 10_000;
type Tab = QueryTab;
/** Options for adding a captured plan: `open: false` keeps the current screen. */
export type CaptureOptions = { open?: boolean };
/** The editor part of the restorable lab state (the connection and the
 * Server tabs are reported by their own owners). */
export type EditorState = Pick<
  LabState,
  "tabs" | "tab_id" | "timeout" | "repeat" | "trace" | "conditions" | "editor_layout"
>;
/** In-memory navigation only: SQL, credentials and results stay in the editor. */
export type QueryNavigation = { tabId: string; reveal: Omit<SchemaReveal, "n"> | null };

/** What the Server place asks of the editor. */
export type QueryLabHandle = {
  navigationSelection: () => QueryNavigation;
  restoreNavigation: (selection: QueryNavigation) => QueryNavigation;
  /** Open a Top statements row in a new tab. Explain runs in that tab;
   * Run analysis captures measured timings. With *expect* (a
   * connectionKey), nothing opens or runs unless that is still the
   * connection in use. */
  openDigest: (row: TopStatement, rank: number, action: DigestAction, expect?: string) => void;
  /** Add an empty query tab and put the cursor in it (Home's New query tab). */
  newTab: () => void;
  /** Make an existing tab the current one (Home's Recent work); unknown ids do nothing. */
  openTab: (id: string) => void;
  /** Open a statement in a new tab (or append to the current tab when
   * *options.append* is set, as for Parameters' SET SESSION), selected and not run. */
  openSql: (sql: string, options?: SqlOpenOptions) => void;
  /** Request closing a query tab, verifying uncommitted transactions first. */
  closeTab: (id: string) => void;
  /** Start renaming the current tab (the palette's Rename query tab). */
  renameTab: () => void;
  /** Format the selection, or the whole tab (the palette's Format SQL). */
  formatSql: () => void;
  /** Show the editor and dispatch one of its keymap commands (the palette). */
  runEditorCommand: (id: string) => void;
  /** Save the current tab's SQL as a saved query (the palette's Save query…). */
  saveQuery: () => void;
  /** Reveal an object in the schema browser (the palette's object search):
   * opens the pane, expands the schema and focuses the row. */
  revealInSchemaBrowser: (schema: string, table: string | null) => void;
};
function parseParameters(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}
/**
 * The digest parameters (`p1`, …) still referenced in *sql* that have no
 * value yet. A null left in place would compare as NULL and match nothing.
 */
function unfilled(tab: Tab, sql: string, parameters: unknown) {
  const values =
    parameters && typeof parameters === "object" && !Array.isArray(parameters)
      ? (parameters as Record<string, unknown>)
      : {};
  return (tab.fill || []).filter(
    (name) =>
      new RegExp(`(^|[^:]):${name}(?![A-Za-z0-9_])`).test(sql) &&
      (values[name] === null || values[name] === undefined),
  );
}
function listNames(names: string[]) {
  return names.length === 1
    ? names[0]
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}
type Job = {
  id: string;
  state: string;
  progress: number;
  repeat: number;
  error: string;
  result: Capture | null;
};

// ---- Running SQL in the tab's session (editor_session.py) --------------------

/** What the editor knows about one tab's session and its last run. Kept in
 * page memory only: results and the connection are never saved. */
type CapturedStatement = { capture: Capture; name: string };
type Runtime = {
  /** Index-fix thread metadata stays in this page, never in saved tabs. */
  indexOrigin?: SuggestedIndexOrigin;
  indexDdl?: string;
  measureBefore?: SuggestedIndexOrigin;
  measureSql?: string;
  session: EditorSession | null;
  /** Temporary tables are absent from the server catalog; remembered only for this session. */
  sessionTempTables: boolean;
  /** connectionKey of the connection the session was opened with. */
  sessionKey: string;
  /** That connection (with the typed password, page memory only), for Stop, Commit and Roll back. */
  sessionConnection: Connection | null;
  /** The connection of the request on its way (before any reply names the session). */
  execConnection: Connection | null;
  /** Where the results on screen came from: the connection key and the
   * session (its connection id); Load more needs both to still be in use. */
  runKey: string;
  runSessionId: number | null;
  runConnectionName: string;
  /** Retained with the result, even if the tab later opens another session. */
  runConnection: Connection | null;
  runBackslash: boolean;
  /** Requests started for the tab; an answer to an older probe is dropped. */
  gen: number;
  /** The last status poll failed: the run may still be going, retrying. */
  pollTrouble: boolean;
  run: EditorRun | null;
  /** The editor text when the run started (positions are stale after an edit). */
  runSql: string;
  explain: boolean;
  /** The Parameters an Explain bound, reused by its Load more and Explore. */
  explainParameters?: Record<string, unknown>;
  explaining: boolean;
  analysis: (CapturedStatement & { previous?: CapturedStatement; measured?: SuggestedIndexOrigin }) | null;
  running: boolean;
  stopping: boolean;
  /** The per-tab control owning a request, including session opens and closes. */
  operation: "" | "run" | "commit" | "rollback" | "open" | "close";
  sessionFeedback: ActionState;
  stopFeedback: ActionState;
  /** One Stop per run, including a reply lost after KILL QUERY was sent. */
  stopGen: number | null;
  note: string;
  noteError: boolean;
  /** Result columns marked binary for SQL export, per rows result. */
  binary: Record<number, number[]>;
  /** Results reloaded with Load more, with their note. */
  reloaded: Record<number, string>;
  /** Safe updates wanted before the session opens (null: the connection type's default). */
  safe: boolean | null;
  /** The server answered whether this tab already has a session (after a reload). */
  probed: boolean;
  /** The file the tab was opened from or saved to: the backend-issued token,
   * the file's base name and the SQL as last saved (the dirty dot compares). */
  file: { token: string; name: string; savedSql: string } | null;
  /** A file dialog or write for this tab is on its way. */
  fileBusy: boolean;
};
const blankRuntime = (): Runtime => ({
  session: null,
  sessionTempTables: false,
  sessionKey: "",
  sessionConnection: null,
  execConnection: null,
  runKey: "",
  runSessionId: null,
  runConnectionName: "",
  runConnection: null,
  runBackslash: true,
  gen: 0,
  pollTrouble: false,
  run: null,
  runSql: "",
  explain: false,
  explaining: false,
  analysis: null,
  running: false,
  stopping: false,
  operation: "",
  sessionFeedback: IDLE_ACTION,
  stopFeedback: IDLE_ACTION,
  stopGen: null,
  note: "",
  noteError: false,
  binary: {},
  reloaded: {},
  safe: null,
  probed: false,
  file: null,
  fileBusy: false,
});
type SendMeta = {
  runSql: string;
  explain?: boolean;
  explainParameters?: Record<string, unknown>;
  /** Load more: the reloaded result replaces this one in the current run. */
  merge?: { statement: number; result: number; limit: number };
  /** Commit or Roll back: the run is an action; the tab's results stay. */
  action?: "commit" | "rollback";
  /** Called after a successful action (close the tab after Commit). */
  after?: () => void;
  /** This request carried a confirmation (a new needs_confirmation means re-ask). */
  confirmed?: { issued: number; ttl: number };
};
// These additive fields belong to the editor confirmation, not a stored result.
type DdlConfirmation = EditorConfirmation & {
  typed_target?: string | null;
  alter_preflight?: {
    schema: string | null; table: string; table_engine?: string | null;
    engine?: string; version?: string; approx_bytes: number | null;
    metadata_known?: boolean; transactions_known?: boolean; truncated?: boolean;
    metadata_locks: { connection_id: number | null; lock_type: string; status: string }[];
    transactions: { connection_id: number | null; age_s: number | null; query?: string | null }[];
    unattributed_transactions?: { connection_id: number | null; age_s: number | null }[];
    sessions?: { connection_id: number | null; time_s?: number | null; query?: string | null }[];
    suggestion?: string | null;
    prediction?: AlterPrediction | null;
  } | null;
};
type Pending = {
  tabId: string;
  path: string;
  payload: Record<string, unknown>;
  meta: SendMeta;
  confirmation: DdlConfirmation;
  typedTarget: string;
  issued: number;
  /** connectionKey of the request: the write policy is read again before sending. */
  key: string;
  /** Why the sheet asks again, when it does. */
  again?: string;
};
const ENDED: Record<string, string> = {
  closed: "the tab closed it",
  idle: "it was idle too long",
  connection_lost: "the connection to the server was lost",
  client_exited: "the MySQL client exited",
  unresponsive: "a statement did not stop after KILL QUERY",
  stopped: "Stop ended it",
  protocol: "myflames could not read the client's output",
  reconnected: "the client reconnected on its own",
  server_exit: "the workspace server stopped",
  start_failed: "it could not start",
};
/** A session whose connection matches *current*, as far as its status tells. */
function sessionKeyFor(s: EditorSession, current: Connection) {
  const c = s.connection;
  const same =
    c.host === current.host &&
    Number(c.port) === Number(current.port) &&
    (c.user || "") === (current.user || "") &&
    (c.profile_id || "") === (current.profile_id || "") &&
    (c.type || "production") === (current.type || "production") &&
    !!c.read_only === !!current.read_only;
  return same ? connectionKey(current) : `session:${c.profile_id || ""}:${c.host}:${c.port}:${c.user}`;
}
function sessionName(s: EditorSession) {
  const c = s.connection;
  return c.name || `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}`;
}
function connectionShortName(c: Connection) {
  return c.name || `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}`;
}
/** CREATE or DROP of a database or schema (the verb the backend names it). */
const DATABASE_LIST_DDL = /^(?:CREATE|DROP)\s+(?:DATABASE|SCHEMA)$/i;
/** True when a finished run may have changed the connection's database list:
 * a CREATE/DROP DATABASE or SCHEMA, or a statement the editor cannot classify
 * as harmless (no verb, or a bare CREATE/DROP whose text names a database). */
function runMayChangeDatabases(run: EditorRun): boolean {
  return run.statements.some((s) => {
    const verb = (s.verb || "").trim();
    if (!verb) return true;
    if (DATABASE_LIST_DDL.test(verb)) return true;
    if (/^(?:CREATE|DROP)$/i.test(verb)) return /^\s*(?:CREATE|DROP)\s+(?:DATABASE|SCHEMA)\b/i.test(s.text);
    return false;
  });
}
/** The word at *offset*, for the error underline (at least one character). */
function wordAt(sql: string, offset: number): Range {
  let start = Math.max(0, Math.min(offset, sql.length));
  if (start === sql.length && start > 0) start--;
  let end = start;
  while (end < sql.length && /[\w$`'".]/.test(sql[end])) end++;
  return { start, end: Math.max(end, start + 1) };
}
function useNarrow() {
  const query = `(max-width: ${NARROW}px)`;
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const change = () => setNarrow(list.matches);
    list.addEventListener("change", change);
    return () => list.removeEventListener("change", change);
  }, [query]);
  return narrow;
}

/** TODO: switch to kit's SplitButton when its controlled popover is available.
 * Keep fields mounted: digest actions open and focus Parameters in the same task.
 * This shares the kit popover dismissal event so only one menu stays open. */
function EditorSplitButton({ action, label, open, onOpen, children }: {
  action: ReactNode;
  label: string;
  open: boolean;
  onOpen: (open: boolean) => void;
  children: ReactNode;
}) {
  const id = useId();
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<CSSProperties>({ visibility: "hidden" });
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const b = button.current?.getBoundingClientRect();
      if (!b) return;
      const width = Math.min(320, window.innerWidth - 16);
      const maxHeight = Math.max(0, window.innerHeight - 16);
      const height = Math.min(panel.current?.offsetHeight || 0, maxHeight);
      // A wrapped toolbar has rows below the button: open under the whole
      // toolbar, never over the next row's controls.
      const toolbar = button.current?.closest(".editor-toolbar")?.getBoundingClientRect();
      const anchor = toolbar && toolbar.bottom > b.bottom + 1 ? toolbar.bottom : b.bottom;
      const below = anchor + 4;
      const top = below + height <= window.innerHeight - 8
        ? below : Math.max(8, b.top - height - 4);
      setPosition({ width, maxHeight, left: Math.max(8, Math.min(b.left, window.innerWidth - width - 8)), top });
    };
    place();
    const observer = new ResizeObserver(place);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent("myflames-popover", { detail: id }));
    const other = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail === id) return;
      // A popover opened inside this panel (the ⓘ next to Safe updates)
      // keeps the panel open; any other popover closes it.
      const opened = typeof detail === "string" && detail ? document.getElementById(detail) : null;
      if (opened && panel.current?.contains(opened)) return;
      onOpen(false);
    };
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!button.current?.contains(target) && !panel.current?.contains(target)) onOpen(false);
    };
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      // Escape closes a popover open inside this panel first (its own focus
      // scope returns focus to its ⓘ); the next Escape closes the panel.
      if (panel.current?.querySelector(".popover")) return;
      e.preventDefault();
      e.stopPropagation();
      onOpen(false);
      button.current?.focus();
    };
    window.addEventListener("myflames-popover", other);
    document.addEventListener("mousedown", outside);
    document.addEventListener("keydown", key, true);
    return () => {
      window.removeEventListener("myflames-popover", other);
      document.removeEventListener("mousedown", outside);
      document.removeEventListener("keydown", key, true);
    };
  }, [open, id, onOpen]);
  return (
    <span className="editor-split-button">
      {action}
      <button ref={button} type="button" className="button editor-options-trigger"
        aria-label={label} title={label} aria-haspopup="dialog" aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => {
          onOpen(!open);
          if (!open) window.setTimeout(() => panel.current?.querySelector<HTMLElement>("input, textarea, summary, button")?.focus());
        }}>
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
      </button>
      {/* Mounted while closed (digest actions fill Parameters), but only an
          open panel carries .popover: elsewhere a .popover is an open one. */}
      <div ref={panel} id={id} className={`editor-options-popover${open ? " popover" : ""}`} role="dialog" aria-label={label} hidden={!open} style={position}>
        {children}
      </div>
    </span>
  );
}

export const QueryLab = forwardRef<
  QueryLabHandle,
  {
    /** Adds a captured plan; by default Plans opens on it. */
    onCapture: (
      capture: Capture,
      name: string,
      options?: CaptureOptions,
    ) => Promise<Plan | undefined>;
    /** The connection in use: settings, typed password, and saved connection id. */
    connection: Connection;
    /** The connection picker shown in the header. */
    picker: ReactNode;
    /** The active Group connection (a connection group opened as one
     * connection), or null. The editor shows the Server: control; a new tab
     * binds the member it names and keeps it, a removed member's tab refuses
     * to run until the user picks a member. */
    group?: GroupConnection | null;
    /** The member new tabs bind to (the Server: control's value). */
    groupMember?: string;
    onGroupMember?: (profileId: string) => void;
    /** One member's request connection (its profile id and the group name),
     * or null for a non-member. */
    memberConnection?: (profileId: string) => Connection | null;
    /** Explain top N runs on Server: editor runs wait meanwhile. */
    batchRunning: boolean;
    /** Restored session state; the editor starts from defaults without it. */
    initial?: LabState;
    /** Reports the restorable editor state after every change (deferred:
     * App normalizes and serializes it for the session). */
    onStateChange?: (state: EditorState) => void;
    /** The tabs as they are now, in the same task as the edit: a cheap call
     * (App keeps the array in a ref for its unload check, nothing else). */
    onTabsNow?: (tabs: Tab[]) => void;
    /** Reports whether a capture started here is running. */
    onBusyChange?: (busy: boolean) => void;
    onConnected?: (connection: Connection) => void;
    /** Automatic DDL reads and the schema catalog read stop while the kept
     * editor is off screen. */
    onScreen?: boolean;
    onOpenInstruments?: (row: string) => void;
    /** Open Indexes or Tables on one table (the schema browser's menu); *from*
     * is the key of the connection the name was read from. */
    onOpenTable?: (view: "indexes" | "tables", schema: string, table: string, from: string) => void;
    onOpenLocks?: () => void;
    onCompareCaptures?: (before: CapturedStatement, after: CapturedStatement) => Promise<void>;
  }
>(function QueryLab(
  {
    onCapture,
    connection,
    picker,
    group = null,
    groupMember = "",
    onGroupMember,
    memberConnection,
    batchRunning,
    initial,
    onStateChange,
    onTabsNow,
    onBusyChange,
    onConnected,
    onScreen = true,
    onOpenInstruments,
    onOpenTable,
    onOpenLocks,
    onCompareCaptures,
  },
  ref,
) {
  // Read once on mount: App remounts the editor (new key) to restore again.
  const [start] = useState(() => normalizeLab(initial));
  const [tabs, setTabs] = useState<Tab[]>(start.tabs);
  const [tabId, setTabId] = useState(start.tab_id);
  const tab = tabs.find((t) => t.id === tabId) || tabs[0];
  // The tab on screen, for work that finishes after an await (Format SQL).
  const tabRef = useRef(tab.id);
  tabRef.current = tab.id;
  // The tab list, for file saves that finish after an await.
  const tabsRef = useRef(tabs);
  tabsRef.current = tabs;
  const [runtime, setRuntime] = useState<Record<string, Runtime>>({});
  // The latest runtime, updated in the same task as every change (a
  // continuation such as Commit and close reads it before React renders).
  const runtimeRef = useRef(runtime);
  const tabConnection = connectionFor(tab);
  // Async continuations resolve the same tab using the latest group and settings.
  const resolveConnection = useRef(connectionFor);
  resolveConnection.current = connectionFor;
  function currentTabConnection(id: string): Connection {
    const current = tabsRef.current.find((t) => t.id === id);
    return current ? resolveConnection.current(current) : connectionRef.current;
  }
  // The connection's schema list pre-fills the default-database picker (one
  // cheap catalog read per connection, cached; PerfShared.useSchemaCatalog),
  // read only while the editor is on screen: a hidden editor never reads.
  const { catalog: schemaCatalog, reloadStale: reloadStaleSchemaCatalog } = useSchemaCatalog(tabConnection, onScreen && !groupMemberProblem(tab));
  // Back on screen, a list invalidated or replaced while the editor was
  // hidden is read or attached by useSchemaCatalog itself; an unchanged list
  // stays (each read counts tables), and opening the picker re-reads one
  // older than CATALOG_STALE_MS.
  const [timeout, setTimeoutSeconds] = useState(start.timeout);
  const [repeat, setRepeat] = useState(start.repeat);
  const [trace, setTrace] = useState(start.trace);
  const [conditions, setConditions] = useState(start.conditions);
  const [layout, setLayout] = useState<EditorLayout>(() => normalizeEditorLayout(start.editor_layout ?? defaultEditorLayout()));
  // The room for the editor and the results under it: from the editor's top
  // to the bottom of the tab panel, less the splitter, the insight lines and
  // the operation feedback line under the results. Measured after every
  // render (a banner, a note or a wrapped toolbar moves the editor) and on
  // resize.
  const tabPanel = useRef<HTMLDivElement>(null);
  const [editorRoom, setEditorRoom] = useState(0);
  // The last room measured: an unchanged room is not set again. A no-op
  // update queued from this layout effect on every commit kept re-rendering
  // the editor while a lower-priority update (the caret from selectionchange)
  // waited, until React stopped with "Maximum update depth exceeded" (seen
  // when Format SQL replaced the text).
  const measuredRoom = useRef(0);
  // Set while the panel's ResizeObserver lives, so a measure can also watch
  // chrome whose height changes without the panel resizing (the insight
  // lines, a result toolbar wrapping onto more lines).
  const roomObserver = useRef<ResizeObserver | null>(null);
  const measureEditorRoom = useCallback(() => {
    const panel = tabPanel.current;
    const wrap = panel?.querySelector<HTMLElement>(".sql-editor-wrap");
    // A hidden panel (another place, Query Digest, table data) keeps the last room.
    if (!panel || !wrap || !panel.offsetHeight) return;
    const splitter = panel.querySelector<HTMLElement>(".editor-splitter");
    const feedback = panel.querySelector<HTMLElement>(".editor-feedback");
    const ddlFeedback = panel.querySelector<HTMLElement>(".ddl-editor");
    let taken = (splitter?.offsetHeight || 0) + (feedback?.offsetHeight || 0) + (ddlFeedback?.offsetHeight || 0);
    // The insight lines sit between the splitter and the results.
    for (const el of panel.querySelectorAll<HTMLElement>(".editor-insights")) {
      taken += el.offsetHeight;
      roomObserver.current?.observe(el);
    }
    const resultBar = panel.querySelector<HTMLElement>(".result-toolbar");
    if (resultBar) {
      roomObserver.current?.observe(resultBar);
      taken += Math.max(0, resultBar.offsetHeight - RESULT_BAR_LINE);
    }
    const room = Math.floor(panel.getBoundingClientRect().bottom - wrap.getBoundingClientRect().top - taken);
    if (room === measuredRoom.current) return;
    measuredRoom.current = room;
    setEditorRoom(room);
  }, []);
  useEffect(() => {
    const panel = tabPanel.current;
    if (!panel) return;
    const observer = new ResizeObserver(measureEditorRoom);
    roomObserver.current = observer;
    observer.observe(panel);
    return () => {
      observer.disconnect();
      roomObserver.current = null;
    };
  }, [measureEditorRoom]);
  /** The tallest editor that leaves the results their rows; the editor shows
   * the height the splitter set, up to this. */
  const editorCap = editorRoom > 0 ? Math.max(EDITOR_FLOOR, editorRoom - RESULTS_KEEP) : 2000;
  const editorHeight = Math.min(layout.height, editorCap);
  const privileges = usePrivileges();
  const editorSettings = useEditorSettings();
  const docIds = useMemo(() => tabs.map((t) => t.id), [tabs]);
  const privilegeRef = useRef(privileges);
  privilegeRef.current = privileges;
  const explainGate = usePrivilege("explain");
  const analysisGate = usePrivilege("run_analysis");
  const privilegeId = useId();
  function denied(value: unknown, via: Connection | null) {
    const current = privilegeRef.current;
    if (via && current?.key === connectionKey(via) && isPrivilegeDenial(value)) void current.accountChanged();
  }
  const [status, setStatus] = useState("");
  const [analysisWarning, setAnalysisWarning] = useState<{ tab: string; text: string } | null>(null);
  const [error, setError] = useState("");
  // A Run or Explain refused for a missing database client keeps the failed
  // request here, so the notice's Try again sends that same request again.
  /** A Run or Explain refused for a missing client, with the connection it was for (Try again resends only to it). */
  const [runClientMissing, setRunClientMissing] = useState<{ tabId: string; connection: string; text: string; retry: () => void } | null>(null);
  // When a fresh-connection capture fails because the query names a table
  // the server does not have (a TEMPORARY table lives only in the tab's
  // session), the error notice offers one retry inside that session.
  const [sessionFallback, setSessionFallback] = useState<{
    tabId: string;
    label: string;
    sql: string;
    parameters: unknown;
    mode: string;
  } | null>(null);
  // A staged table draft remains bound to the connection it was opened from.
  const [tableData, setTableData] = useState<{ connection: Connection; schema: string; table: string } | null>(null);
  // Import CSV from the Schemas panel: one import at a time, bound to the
  // connection it was opened on; its line stays under the toolbar.
  const importCsv = useImportCsv(tabConnection);
  const [job, setJob] = useState<Job | null>(null);
  // The connection the capture was started on: the server answers its status
  // and Cancel only for that connection.
  const jobConnection = useRef<Connection | null>(null);
  const [starting, setStarting] = useState(false);
  const [captureOwner, setCaptureOwner] = useState<{ tabId: string; mode: string } | null>(null);
  // The capture being started (live/start not answered yet), and whether
  // Cancel was pressed meanwhile; jobs the user cancelled are never imported.
  const pendingStart = useRef<{ cancelled: boolean } | null>(null);
  const cancelledJobs = useRef(new Set<string>());
  const jobBusy =
    starting || (!!job && (job.state === "queued" || job.state === "running"));
  const busy = jobBusy || batchRunning;
  const busyRef = useRef(onBusyChange);
  busyRef.current = onBusyChange;
  useEffect(() => {
    busyRef.current?.(jobBusy);
  }, [jobBusy]);
  const captureName = useRef("");
  const tabList = useRef<HTMLDivElement>(null);
  const editor = useRef<SqlEditorHandle>(null);
  const [editingMenu, setEditingMenu] = useState<{ x: number; y: number; opener: HTMLButtonElement } | null>(null);
  useEffect(() => { setEditingMenu(null); }, [tab.id, onScreen, tableData]);
  const parametersField = useRef<HTMLTextAreaElement>(null);
  const [parametersOpen, setParametersOpen] = useState(false);
  /** The tab whose parameter form is open (a digest that needs values). */
  const [fillFormTab, setFillFormTab] = useState<string | null>(null);
  const { explain_format: explainFormat } = useUiPreferences();
  const [analysisOptionsOpen, setAnalysisOptionsOpen] = useState(false);
  const [measurePending, setMeasurePending] = useState<string | null>(null);
  const capturedStatements = useRef(new Map<string, CapturedStatement>());
  const [analysisReview, setAnalysisReview] = useState<{
    tab: Tab; sql: string; parameters: unknown; label: string; binding: string;
  } | null>(null);
  const [openingPlan, setOpeningPlan] = useState(false);
  const openingPlanRef = useRef(false);
  const [runOptionsOpen, setRunOptionsOpen] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; name: string; invalid?: boolean } | null>(null);
  /** End a rename. Enter saves the trimmed name; an empty one keeps the field
   * open and says why. Escape cancels. Leaving the field saves a name and
   * keeps the old one when the field is empty. */
  function finishRename(save: boolean, restoreFocus = true) {
    if (!renaming) return;
    const name = clipName(renaming.name.trim()).trimEnd();
    const old = tabs.find((t) => t.id === renaming.id)?.name ?? "";
    if (save && !name && restoreFocus) {
      setRenaming({ ...renaming, invalid: true });
      note(tab.id, `Type a name for the tab, or press Escape to keep “${old}”.`, true);
      return;
    }
    if (save && name && name !== old) changeTab({ name }, renaming.id);
    if (save && !name) note(tab.id, `Kept the name “${old}”: a tab name cannot be empty.`);
    else if (renaming.invalid) note(tab.id, "");
    const id = renaming.id;
    setRenaming(null);
    if (restoreFocus) window.setTimeout(() => focusTab(id));
  }
  /** Put focus on a tab's button (after a rename, a duplicate or a close). */
  function focusTab(id: string) {
    tabList.current?.querySelector<HTMLElement>(`[role=tab][data-tab-id="${CSS.escape(id)}"]`)?.focus();
  }
  // ---- The tab menu: Rename…, Duplicate, Close, Close others ---------------
  const [tabMenu, setTabMenu] = useState<{ id: string; x: number; y: number; opener: HTMLElement } | null>(null);
  const closeTabMenu = useCallback(() => setTabMenu(null), []);
  function startRename(id: string) {
    const t = tabs.find((x) => x.id === id);
    if (!t) return;
    setRenaming({ id, name: t.name });
  }
  /** A copy of a tab right after it, with its SQL, parameters and row limit;
   * it opens its own session when it runs. */
  function duplicateTab(id: string) {
    const source = tabs.find((t) => t.id === id);
    if (!source) return;
    const copy: Tab = { ...source, id: crypto.randomUUID(), name: copyName(source.name, tabs.map((t) => t.name)) };
    delete copy.result_tab;
    setTabs((ts) => {
      const at = ts.findIndex((t) => t.id === id);
      const next = [...ts];
      next.splice(at < 0 ? next.length : at + 1, 0, copy);
      return next;
    });
    setTabId(copy.id);
    note(copy.id, `Duplicated “${source.name}” as “${copy.name}”. The copy runs in its own session.`);
    window.setTimeout(() => focusTab(copy.id));
  }
  /** Close every tab but *keep*, one at a time. A tab with a running
   * statement stays; one with a transaction that is open, or may be, stops
   * here and asks how to close it. */
  async function closeOthers(keep: string) {
    setTabId(keep);
    const others = tabs.filter((t) => t.id !== keep);
    let closed = 0;
    const running: string[] = [];
    let asking = "";
    for (const t of others) {
      if (runtimeRef.current[t.id]?.running) {
        running.push(t.name);
        continue;
      }
      const result = await requestClose(t.id);
      if (result === "closed") closed++;
      else {
        if (result === "asks") asking = t.name;
        break;
      }
    }
    // Closing the tab that was current picks a neighbour; the kept tab stays current.
    setTabId(keep);
    const parts = [`Closed ${closed === 1 ? "1 other tab" : `${closed} other tabs`}`];
    if (running.length) parts.push(`${running.join(", ")} kept: a statement is running`);
    if (asking) parts.push(`${asking} has a transaction open: choose how to close it`);
    note(keep, `${parts.join("; ")}.`);
    if (!asking) window.setTimeout(() => focusTab(keep));
  }
  const showAnalysisOptions = analysisOptionsOpen || parametersOpen;
  const toggleAnalysisOptions = useCallback((open: boolean) => {
    setAnalysisOptionsOpen(open);
    if (!open) setParametersOpen(false);
  }, []);
  // Editor selection, for the run scope (selection / statement at cursor).
  const [cursor, setCursor] = useState({ start: 0, end: 0 });
  // How many selection ranges the editor holds: only the main one runs.
  const [cursorCount, setCursorCount] = useState(1);
  const [flash, setFlash] = useState<{
    tabId: string;
    sql: string;
    start: number;
    end: number;
  } | null>(null);
  const flashTimer = useRef(0);
  const [lastRun, setLastRun] = useState<{
    tabId: string;
    label: string;
    sql: string;
  } | null>(null);
  // Everything a session retry needs, in a ref the poll effect can read.
  const lastRunInfo = useRef<{
    tabId: string;
    label: string;
    sql: string;
    parameters: unknown;
    mode: string;
    inSession: boolean;
  } | null>(null);
  const rt = runtime[tab.id] || blankRuntime();
  const sessionServer = rt.sessionKey === connectionKey(tabConnection) && !rt.session?.ended ? rt.session : null;
  const engine = sessionServer?.engine || privileges?.report?.engine;
  const version = sessionServer?.server_version || privileges?.report?.version || "";
  const versionParts = /^(\d+)\.(\d+)\.(\d+)/.exec(version);
  const treeAvailable = engine === "mysql" && !!versionParts &&
    (Number(versionParts[1]) > 8 || (Number(versionParts[1]) === 8 &&
      (Number(versionParts[2]) > 0 || Number(versionParts[3]) >= 16)));
  const effectiveExplainFormat = explainFormat === "tree" && !treeAvailable ? "table" : explainFormat;
  const analysisBinding = JSON.stringify([tab.id, tab.sql, tab.parameters, connectionKey(tabConnection), timeout, repeat, trace, conditions]);
  useEffect(() => { setAnalysisReview(null); }, [analysisBinding]);
  useEffect(() => {
    if (!measurePending) return;
    setMeasurePending(null);
    if (tab.id !== measurePending || !rt.measureBefore || bindingProblem(tab)) return;
    setAnalysisReview({ tab: { ...tab }, sql: tab.sql, parameters: {}, label: "the source statement", binding: analysisBinding });
  }, [measurePending, analysisBinding]);
  const [ddl, setDdl] = useState<{ owner: string; progress: DdlProgress } | null>(null);
  const ddlOwner = JSON.stringify([tab.id, rt.gen, rt.run?.id, rt.sessionKey]);
  const ddlEligible = rt.running && rt.run?.state === "running" && !rt.stopping && rt.stopGen !== rt.gen &&
    !tableData && !!rt.run.statements.some(s =>
      // Only statements that rebuild or copy a table report stage progress
      // (ALTER USER or ALTER VIEW would only show "turn on tracking").
      /^(ALTER TABLE|OPTIMIZE TABLE|OPTIMIZE)\b/i.test(s.verb || "") ||
      (/^CREATE INDEX$/i.test(s.verb || "") || /^\s*CREATE\s+(?:UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)?INDEX\b/i.test(s.text)));
  const ddlId = rt.session?.connection_id;
  const ddlConnection = rt.sessionConnection;
  useEffect(() => {
    if (!onScreen || !ddlEligible || !ddlId || !ddlConnection) return;
    return followDdlProgress(
      signal => ddlProgress(ddlConnection, [ddlId], signal),
      progress => setDdl({ owner: ddlOwner, progress }),
    );
  }, [onScreen, ddlEligible, ddlId, ddlConnection, ddlOwner]);
  const ddlShown = ddlEligible && onScreen && ddl?.owner === ddlOwner ? ddl.progress : null;
  const ddlItem = ddlShown?.items.find(item => item.id === ddlId);
  useLayoutEffect(measureEditorRoom, [
    measureEditorRoom,
    tabId,
    status,
    error,
    analysisWarning,
    layout.height,
    Boolean(rt.run),
    rt.running,
    Boolean(rt.note),
    ddlEligible,
    ddlShown,
  ]);
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingBusy, setPendingBusy] = useState(false);
  const [closing, setClosing] = useState<{ tabId: string; known: boolean; keepTab: boolean; discardFile: boolean } | null>(null);
  const [closingBusy, setClosingBusy] = useState(false);
  /** A file-backed tab with unsaved edits asks Save / Don't save / Cancel. */
  const [closingFile, setClosingFile] = useState<{ tabId: string; keepTab: boolean } | null>(null);
  const [historyTick, setHistoryTick] = useState(0);
  /** Bumped after Save query…, so an open Saved tab reads again. */
  const [savedTick, setSavedTick] = useState(0);
  /** The side panel's tab: History or Saved (page memory only). */
  const [sideTab, setSideTab] = useState<SidePanelTab>("history");
  /** The tab whose SQL Save query… is saving. */
  const [saveFor, setSaveFor] = useState<string | null>(null);
  const narrow = useNarrow();
  // Below NARROW px the panes are sheets, closed until asked for in this page.
  const [sheet, setSheet] = useState<"" | "browser" | "history">("");
  const browserSheet = useRef<HTMLDivElement>(null);
  const historySheet = useRef<HTMLDivElement>(null);
  const sheetOpener = useRef<HTMLElement | null>(null);
  useFocusScope(browserSheet, { open: narrow && onScreen && !tableData && sheet === "browser", onClose: () => setSheet(""), returnFocus: () => sheetOpener.current });
  useFocusScope(historySheet, { open: narrow && onScreen && !tableData && sheet === "history", onClose: () => setSheet(""), returnFocus: () => sheetOpener.current });
  const showBrowser = !groupMemberProblem(tab) && (narrow ? sheet === "browser" : layout.browser);
  const showHistory = narrow ? sheet === "history" : layout.history;
  // The palette's object search reveals an object here (opens the pane, then
  // the browser expands the schema and focuses the row).
  const [browserReveal, setBrowserReveal] = useState<SchemaReveal | null>(null);
  const browserRevealSerial = useRef(0);
  const polls = useRef(new Map<string, AbortController>());
  const policy = useWritePolicy();
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  useEffect(
    () => () => {
      for (const c of polls.current.values()) c.abort();
    },
    [],
  );
  function patchRuntime(id: string, patch: Partial<Runtime> | ((r: Runtime) => Partial<Runtime>)) {
    const all = runtimeRef.current;
    const current = all[id] || blankRuntime();
    const next = typeof patch === "function" ? patch(current) : patch;
    const updated = { ...current, ...next };
    // Every session reply (poll, Stop, Close or replacement) passes here.
    if (!updated.session || updated.session.ended ||
        updated.session.connection_id !== current.session?.connection_id ||
        updated.session.started_at !== current.session?.started_at || next.run?.state === "session_ended") {
      updated.sessionTempTables = false;
    }
    if (updated.session && !updated.session.ended && next.run && next.run.state !== "session_ended" &&
        next.run.statements.some(s => s.status === "ok" && createsTemporaryTable(s.text))) {
      updated.sessionTempTables = true;
    }
    runtimeRef.current = { ...all, [id]: updated };
    setRuntime(runtimeRef.current);
  }
  function note(id: string, text: string, isError = false) {
    patchRuntime(id, { note: text, noteError: isError });
  }

  useImperativeHandle(ref, () => ({
    navigationSelection: () => ({ tabId, reveal: browserReveal?.connection === connectionKey(tabConnection)
      ? { connection: browserReveal.connection, schema: browserReveal.schema, table: browserReveal.table } : null }),
    restoreNavigation: (selection: QueryNavigation) => {
      const id = tabs.some((t) => t.id === selection.tabId) ? selection.tabId : tabId;
      const reveal = selection.reveal?.connection === connectionKey(currentTabConnection(id)) ? selection.reveal : null;
      setTabId(id);
      setBrowserReveal(reveal ? { ...reveal, n: ++browserRevealSerial.current } : null);
      if (reveal) {
        if (narrow) setSheet("browser");
        else setLayout((l) => (l.browser ? l : { ...l, browser: true }));
      }
      return { tabId: id, reveal };
    },
    openDigest,
    newTab: () => addTab(true),
    openTab: (id: string) => {
      if (tabs.some((t) => t.id === id)) setTabId(id);
    },
    openSql: (sql: string, options?: SqlOpenOptions) =>
      options?.append && !options.indexOrigin && (!options.from || options.from === connectionKey(tabConnection))
        ? appendStatement(sql) : openStatement(sql, options?.indexOrigin, options?.from),
    closeTab: (id: string) => {
      void requestClose(id);
    },
    renameTab: () => startRename(tab.id),
    // The palette runs this as the Query editor comes on screen: the text
    // area must be visible to take focus and keep the browser's undo.
    // Hidden behind table data, nothing changes.
    formatSql: () => {
      setEditingMenu(null);
      whenShown(() => void formatSql());
    },
    runEditorCommand: (id: string) => {
      setEditingMenu(null);
      whenShown(() => editor.current?.runCommand(id));
    },
    saveQuery: () => startSaveQuery(tab.id),
    revealInSchemaBrowser: (schema: string, table: string | null) => {
      if (narrow) setSheet("browser");
      else setLayout((l) => (l.browser ? l : { ...l, browser: true }));
      setBrowserReveal({ connection: connectionKey(tabConnection), schema, table, n: ++browserRevealSerial.current });
    },
  }));
  /** Save query…: the sheet asks for a name (the tab's), a folder and a note. */
  function startSaveQuery(id: string) {
    const t = tabs.find((x) => x.id === id);
    if (!t) return;
    if (!t.sql.trim()) {
      note(id, "There is no SQL in this tab to save.", true);
      return;
    }
    setSaveFor(id);
  }
  function openingTarget(expect?: string) {
    if (expect !== undefined) {
      const member = group?.members.find((m) => {
        const via = memberConnection?.(m.profile_id);
        return via && connectionKey(via) === expect;
      });
      if (!member) return expect === connectionKey(connection) ? { connection, binding: {} } : null;
      const via = memberConnection?.(member.profile_id);
      return via ? { connection: via, binding: { member: member.profile_id, member_name: member.profile_name } } : null;
    }
    const via = group && groupMember ? memberConnection?.(groupMember) : null;
    // The Server: control's member is gone (a group change between render and
    // click): never fall back to the window connection; the caller says the
    // connection changed and opens nothing.
    if (group && groupMember && !via) return null;
    return { connection: via || connection, binding: via ? { member: groupMember, member_name: memberNameOf(groupMember) } : {} };
  }
  /** The error a null openingTarget shows: the target vanished mid-choice. */
  function openingChanged() {
    setStatus("");
    setError("The connection in use changed, so nothing was opened. Choose it again.");
  }
  /** Open a saved query in a new tab, its SQL as saved; nothing runs. */
  function openSaved(q: SavedQuery) {
    const id = crypto.randomUUID();
    const opened = openingTarget();
    if (!opened) {
      openingChanged();
      return;
    }
    setTabs((ts) => [...ts, { id, name: clipName(q.name).trimEnd() || `Query ${ts.length + 1}`, sql: q.sql, parameters: "{}", ...opened.binding }]);
    setTabId(id);
    const where = q.database && q.database !== opened.connection.database ? ` It was saved for the database ${q.database}.` : "";
    note(id, `Opened the saved query “${q.name}” in a new tab. Nothing ran.${where}`);
    if (narrow) setSheet("");
    window.requestAnimationFrame(() => editor.current?.focus());
  }
  /** Run *action* once the editor is on screen; never on a hidden editor. */
  function whenShown(action: () => void, frames = 30) {
    if (tabPanel.current?.offsetParent && editor.current?.isShown()) action();
    else if (frames > 0) window.requestAnimationFrame(() => whenShown(action, frames - 1));
  }
  /** A new tab after the others; *focus* puts the cursor in the editor. A
   * Group connection binds it to the member the Server: control names. */
  function addTab(focus = false) {
    const id = crypto.randomUUID();
    setTabs((ts) => [
      ...ts,
      {
        id,
        name: `Query ${ts.length + 1}`,
        sql: "SELECT 1",
        parameters: "{}",
        ...(group && groupMember ? { member: groupMember, member_name: memberNameOf(groupMember) || undefined } : {}),
      },
    ]);
    setTabId(id);
    if (focus) window.setTimeout(() => editor.current?.focus());
  }
  /**
   * Open a Top statements row in a new tab and run the requested action.
   * A statement that only has normalized
   * text opens with a parameters skeleton and waits for values.
   */
  function openDigest(row: TopStatement, rank: number, action: DigestAction, expect?: string, measureBefore?: SuggestedIndexOrigin) {
    const opened = openingTarget(expect);
    if (!opened) {
      setStatus("");
      setError("The connection in use changed after you chose this statement, so nothing ran. Refresh Top statements and choose it again.");
      return;
    }
    // A statement capture refuses (not a SELECT) opens as recorded: there is
    // nothing to fill in for a run that cannot happen.
    const refused = action === "explain" ? "" : row.capture_problem || "";
    const q = digestQuery(row);
    const source = normalizeSource({
      kind: "digest",
      digest: row.digest,
      schema: row.schema,
    });
    const next: Tab = {
      id: crypto.randomUUID(),
      name: digestName(rank, row),
      sql: refused ? q.text.trim() : q.sql,
      parameters: refused ? "{}" : skeleton(q.names),
      ...opened.binding,
    };
    if (source) {
      next.source = source;
      // The tab stays bound to the server the statement came from, also for
      // a run deferred until its parameters are filled in, and after restore.
      next.target = { key: connectionKey(opened.connection), label: connectionLabel(opened.connection) };
    }
    if (q.names.length && !refused) {
      next.fill = q.names;
      next.fill_note = fillNotice(q);
      if (q.lists.length) next.fill_lists = q.lists;
    }
    if (measureBefore) {
      patchRuntime(next.id, { measureBefore, measureSql: next.sql });
      next.target = { ...measureBefore.target };
    }
    setTabs((ts) => [...ts, next]);
    setTabId(next.id);
    setError("");
    setStatus("");
    if (refused) {
      setStatus(`Loaded the statement into a new tab. Run analysis cannot run it: ${refused}. Explain can ask the server for its plan.`);
      window.setTimeout(() => editor.current?.focus());
      return;
    }
    if (row.analysis_warning && action !== "explain") {
      setAnalysisWarning({ tab: next.id, text: row.analysis_warning });
      if (q.names.length) setFillFormTab(next.id);
      else window.setTimeout(() => editor.current?.focus());
      return;
    }
    if (q.names.length) {
      // Nothing runs until the values are filled in: the form asks for them.
      setFillFormTab(next.id);
      return;
    }
    if (action === "open") {
      setStatus(
        q.fromSample
          ? "Loaded the sample statement into a new tab. Review it, then run Explain."
          : "Loaded the normalized statement into a new tab. Review it, then run Explain.",
      );
      window.setTimeout(() => {
        editor.current?.focus();
        editor.current?.revealInPage();
      });
      return;
    }
    window.setTimeout(() => editor.current?.revealInPage());
    if (action === "explain") runSql(false, q.sql, 0, true, next);
    else if (measureBefore) setMeasurePending(next.id);
    else void startCapture(next, q.sql, {}, action, `Top statements #${rank}`);
  }
  /** Later: close the form and edit the Parameters JSON by hand, as before it. */
  function editParametersByHand() {
    setFillFormTab(null);
    setParametersOpen(true);
    window.setTimeout(() => {
      parametersField.current?.focus();
      parametersField.current?.scrollIntoView({ block: "nearest" });
    });
  }
  /** Use values: the form's values go into the tab's Parameters JSON. Names
   * given a value (NULL included) no longer need one; nothing runs. */
  function applyFillValues(id: string, parameters: string, filled: string[], remaining: string[]) {
    setFillFormTab(null);
    const t = tabs.find((x) => x.id === id);
    const fill = (t?.fill || []).filter((n) => remaining.includes(n));
    const lists = (t?.fill_lists || []).filter((n) => fill.includes(n));
    changeTab({
      parameters,
      fill: fill.length ? fill : undefined,
      fill_lists: lists.length ? lists : undefined,
      // The first notice names every parameter: once some have values the
      // generic one names those still missing.
      ...(filled.length ? { fill_note: undefined } : {}),
    }, id);
    const still = remaining.length
      ? `${listNames(remaining)} still ${remaining.length === 1 ? "needs a value" : "need values"}`
      : "";
    note(id, filled.length
      ? `Values for ${listNames(filled)} are in Parameters${still ? `; ${still}` : ""}. Nothing ran. Explain and Run analysis use these values.`
      : `No values were given; ${still}.`);
  }
  const received = useRef("");
  const reportRef = useRef(onStateChange);
  reportRef.current = onStateChange;
  const tabsNowRef = useRef(onTabsNow);
  tabsNowRef.current = onTabsNow;
  // App's beforeunload check (unsaved query tabs when autosave is off) must
  // see an edit as soon as it is made: a layout effect hands it the tabs in
  // the same task as the keystroke. The full report below also runs before
  // paint so autosave sees the edit before reload or quit handling.
  useLayoutEffect(() => {
    tabsNowRef.current?.(tabs);
  }, [tabs]);
  // The pane layout is reported once it differs from the defaults (or was
  // restored), so opening the workspace alone never changes the session.
  const layoutChanged = useRef(!!start.editor_layout);
  if (!layoutChanged.current && JSON.stringify(layout) !== JSON.stringify(defaultEditorLayout())) layoutChanged.current = true;
  const reportedLayout = layoutChanged.current ? layout : undefined;
  useLayoutEffect(() => {
    reportRef.current?.({
      tabs,
      tab_id: tabId,
      timeout,
      repeat,
      trace,
      conditions,
      editor_layout: reportedLayout,
    });
  }, [tabs, tabId, timeout, repeat, trace, conditions, reportedLayout]);
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.state)) return;
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      try {
        const next = await api<Job>(
          "live/status",
          { id: job.id, connection: jobConnection.current },
          controller.signal,
        );
        setJob(next);
        if (
          next.state === "complete" &&
          next.result &&
          received.current !== next.id &&
          cancelledJobs.current.has(next.id)
        ) {
          // Finished before the cancel reached it: the result is dropped.
          received.current = next.id;
          setStatus("Capture cancelled. Nothing was added.");
        } else if (
          next.state === "complete" &&
          next.result &&
          received.current !== next.id
        ) {
          received.current = next.id;
          const info = lastRunInfo.current;
          if (info?.mode === "analyze") {
            const previous = info.inSession ? undefined : capturedStatements.current.get(captureThreadKey(jobConnection.current!, next.result));
            const origin = runtimeRef.current[info.tabId]?.measureBefore;
            const measured = origin && origin.target.key === connectionKey(jobConnection.current!) &&
              measurementMatches(runtimeRef.current[info.tabId]?.measureSql, info.sql, info.inSession) ? origin : undefined;
            const analysis = { capture: next.result, name: captureName.current, previous, measured };
            patchRuntime(info.tabId, { analysis });
            if (!info.inSession) {
              const key = captureThreadKey(jobConnection.current!, next.result);
              capturedStatements.current.delete(key);
              if (capturedStatements.current.size >= 50) capturedStatements.current.delete(capturedStatements.current.keys().next().value!);
              capturedStatements.current.set(key, { capture: next.result, name: captureName.current });
            }
            changeTab({ result_tab: "plan" }, info.tabId);
            setStatus("Analysis complete. The statement ran. Choose Explore to open the visual plan.");
          } else {
            setStatus("Visual plan ready.");
            if (!info?.inSession) {
              const key = captureThreadKey(jobConnection.current!, next.result);
              if (capturedStatements.current.size >= 50) capturedStatements.current.delete(capturedStatements.current.keys().next().value!);
              capturedStatements.current.set(key, { capture: next.result, name: captureName.current });
            }
            await onCapture(next.result, captureName.current);
          }
        }
        if (next.error) { offerSessionFallback(next.error); setError(next.error); denied(next.error, jobConnection.current); }
      } catch (e) {
        if (!controller.signal.aborted) {
          setError((e as Error).message);
          setJob({ ...job, state: "disconnected" });
        }
      }
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [job, onCapture]);
  function changeTab(changes: Partial<Tab>, id = tabId) {
    setTabs((ts) => ts.map((t) => (t.id === id ? { ...t, ...changes } : t)));
  }
  // The caret and selection come only from the editor's onSelectionChange and
  // onChange; a tab switch restores each document's own state and reports its
  // selection, and the saved caret seeds a fresh document (initialSelection).
  // The caret is saved with the tab a moment after it stops moving, once the
  // user has worked in the editor (onFocus marks it; opening the page saves
  // nothing).
  const touched = useRef(false);
  useEffect(() => () => window.clearTimeout(flashTimer.current), []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      if (!touched.current) return;
      setTabs((ts) => {
        const t = ts.find((x) => x.id === tabId);
        if (!t) return ts;
        const same = t.selection ? t.selection.start === cursor.start && t.selection.end === cursor.end : !cursor.start && !cursor.end;
        if (same) return ts;
        const selection = cursor.start || cursor.end ? { start: cursor.start, end: cursor.end } : undefined;
        return ts.map((x) => (x.id === tabId ? { ...x, selection } : x));
      });
    }, 600);
    return () => window.clearTimeout(timer);
  }, [cursor.start, cursor.end, tabId]);
  // After a reload the server may still hold this tab's session (and an open
  // transaction): ask once per tab and page.
  useEffect(() => {
    const id = tab.id;
    const at = runtimeRef.current[id];
    if (at?.probed || groupMemberProblem(tab)) return;
    // Marked only once answered: a probe cut short by a tab switch or a
    // failure is asked again the next time the tab is shown.
    const gen = at?.gen ?? 0;
    const controller = new AbortController();
    api<EditorReply>("editor/status", { tab_id: id, wait: 0, connection: tabConnection }, controller.signal).then(
      (reply) => {
        const now = runtimeRef.current[id] || blankRuntime();
        // A run started meanwhile knows better than this answer.
        if (now.gen !== gen || now.running) return;
        patchRuntime(id, { probed: true });
        if (!reply.session) return;
        const session = reply.session;
        const key = sessionKeyFor(session, tabConnection);
        // Bound to the connection in use only when the session says it is that one.
        const verified = key === connectionKey(tabConnection) ? tabConnection : null;
        patchRuntime(id, { session, sessionKey: key, sessionConnection: verified });
        if (reply.run && reply.run.state === "running" && reply.run.kind === "run") {
          patchRuntime(id, { running: true, run: reply.run, gen: gen + 1 });
          void poll(id, reply.run.id, { runSql: "" }, gen + 1);
        }
      },
      () => {},
    );
    return () => controller.abort();
    // Once per tab until answered; the runtime ref holds the rest.
  }, [tab.id, connectionKey(tabConnection), !!groupMemberProblem(tab)]);
  // QueryLab stays mounted in other places. Read only for the visible tab,
  // never while its user SQL or transaction action owns the session pipe.
  useEffect(() => {
    if (!onScreen || tableData || groupMemberProblem(tab) || rt.session?.in_transaction !== true || rt.session.ended ||
        rt.running || rt.operation || rt.stopping || rt.sessionKey !== connectionKey(tabConnection)) return;
    const id = tab.id, gen = rt.gen, sessionId = rt.session.connection_id, key = rt.sessionKey;
    let stopped = false, timer = 0, controller: AbortController | null = null;
    const schedule = () => { window.clearTimeout(timer); if (!stopped && !document.hidden) timer = window.setTimeout(read, 10000); };
    async function read() {
      if (stopped || document.hidden) return;
      const current = runtimeRef.current[id];
      if (!current || current.gen !== gen || current.running || current.operation || current.stopping) return;
      controller = new AbortController();
      try {
        const reply = await api<EditorReply>("editor/status", {
          tab_id: id, connection: current.sessionConnection || currentTabConnection(id), refresh_insights: true, wait: 0,
        }, controller.signal);
        const latest = runtimeRef.current[id];
        if (!stopped && !controller.signal.aborted && latest?.gen === gen && !latest.running && !latest.operation &&
            latest.sessionKey === key && reply.session?.connection_id === sessionId) {
          patchRuntime(id, { session: reply.session });
        }
      } catch {
        // Keep the observed age, but never present old counters as a fresh read.
        const latest = runtimeRef.current[id];
        if (!stopped && !controller.signal.aborted && latest?.gen === gen && !latest.running && !latest.operation && latest.session?.transaction_meter) {
          patchRuntime(id, { session: { ...latest.session, transaction_meter: {
            ...latest.session.transaction_meter, rows_modified: null, rows_locked: null, lock_structs: null,
            blocking: null, note: "Transaction details could not be refreshed; open Server > Locks to check.",
          } } });
        }
      } finally { controller = null; schedule(); }
    }
    const visibility = () => { window.clearTimeout(timer); controller?.abort(); if (!document.hidden) schedule(); };
    document.addEventListener("visibilitychange", visibility);
    schedule();
    return () => { stopped = true; window.clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visibility); };
  }, [tab.id, onScreen, tableData, rt.session?.connection_id, rt.session?.in_transaction, rt.session?.ended,
      rt.running, rt.operation, rt.stopping, rt.gen, rt.sessionKey, connectionKey(tabConnection)]);
  const target = useMemo(
    () => runTarget(tab.sql, cursor.start, cursor.end),
    [tab.sql, cursor.start, cursor.end],
  );
  // The session's reading of backslashes, once known (the tint only: the server decides what runs).
  const backslash = runtime[tab.id]?.session?.backslash_escapes ?? true;
  const runPart = useMemo(
    () => sqlRun(tab.sql, cursor.start, cursor.end, false, backslash),
    [tab.sql, cursor.start, cursor.end, backslash],
  );
  // Preview uses the same request scope as Run. Replies are bound to the
  // text, tab, connection, session generation and privileges read; editing
  // or switching any of them immediately drops an older denial.
  const sqlVersionRef = useRef<{ sql: string; ver: number }>({ sql: "", ver: 0 });
  function previewKey(payload: Record<string, unknown>) {
    const via = payload.connection as Connection;
    const s = String(payload.sql || "");
    if (sqlVersionRef.current.sql !== s) {
      sqlVersionRef.current = { sql: s, ver: sqlVersionRef.current.ver + 1 };
    }
    return JSON.stringify([payload.tab_id, connectionKey(via), via.database || "", sqlVersionRef.current.ver,
      payload.selection || null, payload.cursor ?? null]);
  }
  const previewBase = { tab_id: tab.id, connection: connectionFor(tab), sql: tab.sql };
  const previewScoped = { ...previewBase, ...(runPart.selection
    ? { selection: runPart.selection } : { cursor: runPart.cursor ?? cursor.end }) };
  const scopedKey = previewKey(previewScoped);
  const allKey = previewKey(previewBase);
  const previewEpoch = JSON.stringify([rt.gen, rt.running, rt.session?.connection_id,
    rt.session?.sql_mode, rt.session?.ended, privileges?.loading, privileges?.error]);
  const previewGeneration = useMemo(() => Symbol(),
    [scopedKey, allKey, previewEpoch, privileges?.report, tabConnection.password]);
  const [previews, setPreviews] = useState<{
    generation: symbol;
    values: Record<string, EditorPrivilegePreview>;
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    if (!tab.sql.trim() || rt.running || groupMemberProblem(tab)) return () => controller.abort();
    const timer = window.setTimeout(() => {
      void Promise.all([previewScoped, previewBase].map(async payload => {
        try {
          const reply = await api<EditorPrivilegePreview>("editor/preview", payload, controller.signal);
          return [previewKey(payload), reply] as const;
        } catch (e) {
          const reply: EditorPrivilegePreview = { status: "invalid", statements: [], aggregate: {
            state: "unknown", missing: [], warning: "", reason: `Could not preview privileges: ${(e as Error).message}`,
          } };
          return [previewKey(payload), reply] as const;
        }
      })).then(values => {
        if (!controller.signal.aborted) setPreviews({ generation: previewGeneration, values: Object.fromEntries(values) });
      });
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [previewGeneration]);
  function currentPreview(key: string) {
    return previews?.generation === previewGeneration ? previews.values[key] : undefined;
  }
  const runPreview = currentPreview(scopedKey);
  const allPreview = currentPreview(allKey);
  const runGate = editorPrivilegeGate(runPreview);
  const allGate = editorPrivilegeGate(allPreview);
  const previewWarning = runPreview?.aggregate.warning || allPreview?.aggregate.warning;
  // One explanation for the run group, shown only when something is denied
  // (or a mixed batch has denied statements). Unverified privileges stay
  // quiet: the server decides, and each button keeps its reason for
  // assistive technology. Keep mixed batches runnable. A Run or Run all
  // refusal the error banner already shows (for example "Run DDL statements
  // one at a time") is not repeated here; the analysis gate's reason stays
  // (its denial has no banner of its own wording to repeat).
  const refusedByRunGate = (reason: string) => reason === error && [runGate, allGate].some(gate => gate.disabledReason === reason);
  const privilegeReasons = [...new Set(
    [runGate, allGate, analysisGate].map(gate => gate.disabledReason)
      .filter(reason => reason && !refusedByRunGate(reason) && !previewWarning?.includes(reason)),
  )];
  const privilegeNotice = [previewWarning && previewWarning !== error ? previewWarning : "", ...privilegeReasons].filter(Boolean).join(" ");
  const privilegeNoticeId = privilegeNotice ? `${privilegeId}-notice` : undefined;
  const scopeLabel = cursorCount > 1
    ? cursor.start === cursor.end
      ? "Runs the statement at the main cursor"
      : "Runs the main selection"
    : describeTarget(tab.sql, target);
  const selecting = target.scope === "selection";
  const lit =
    flash && flash.tabId === tab.id && flash.sql === tab.sql ? flash : null;
  function sentLabel(t: RunTarget) {
    if (t.scope === "selection")
      return `the selection (${t.lines} ${t.lines === 1 ? "line" : "lines"})`;
    if (t.scope === "statement")
      return `statement ${t.index} of ${t.total}, at the cursor`;
    return "the editor's query";
  }
  /** Button tooltip wording for what runs. */
  const scopePhrase =
    target.scope === "selection"
      ? "only the selected text"
      : target.scope === "statement"
        ? "the statement at the cursor"
        : "the query";
  /** Why tab *t* may not run on the connection in use: a Top statements tab
   * runs only on the server it came from until it is moved explicitly. */
  function bindingProblem(t: Tab) {
    const tabConnection = connectionFor(t);
    if (!t.source) return "";
    if (!t.target)
      return `This statement came from Top statements, and the workspace did not record which server it was read from. Choose Use this connection to run it on ${connectionLabel(tabConnection)}.`;
    if (t.target.key !== connectionKey(tabConnection))
      return `This statement came from Top statements on ${t.target.label || "another connection"}, not the connection in use. Switch back, or choose Use this connection to run it on ${connectionLabel(tabConnection)}.`;
    return "";
  }
  /** Move a Top statements tab to the connection in use (an explicit choice). */
  function bindToConnection() {
    changeTab({ target: { key: connectionKey(tabConnection), label: connectionLabel(tabConnection) } });
    setError("");
    setStatus(`This tab now runs on ${connectionLabel(tabConnection)}.`);
  }
  /** The member a tab is bound to, as the group names it now (its profile
   * name at bind time when the group no longer has it). */
  function memberNameOf(id: string): string {
    return group?.members.find((m) => m.profile_id === id)?.profile_name || "";
  }
  /** Why tab *t* may not run: its member left the group (a group edit while
   * connected). It runs again once the user picks a member for it. */
  function groupMemberProblem(t: Tab) {
    if (!t.member) return "";
    if (!group) return `This tab ran on ${t.member_name || t.member}, which is not open now.`;
    if (group.members.some((m) => m.profile_id === t.member)) return "";
    return `${t.member_name || "This tab's server"} is not in this group any more. Pick a server for this tab to run it.`;
  }
  /** Move a tab whose member left the group onto another member (an explicit
   * choice, like Use this connection for a Top statements tab). */
  function rebindMember(id: string, memberId: string) {
    if (!group?.members.some((m) => m.profile_id === memberId)) return;
    const via = memberConnection?.(memberId);
    if (!via) return;
    const t = tabsRef.current.find((item) => item.id === id);
    changeTab({ member: memberId, member_name: memberNameOf(memberId),
      ...(t?.source ? { target: { key: connectionKey(via), label: connectionLabel(via) } } : {}),
    }, id);
    setError("");
  }
  /** The group member a member-bound tab runs on, when that member is
   * available; null otherwise. */
  function boundMember(t: Tab): Connection | null {
    return t.member && group && memberConnection ? memberConnection(t.member) : null;
  }
  /** The connection a run of *t* uses: a tab bound to a group member runs on
   * that member (the session request carries its profile id and the group
   * name); a Top statements tab runs in the schema it was recorded in when
   * the connection names none. */
  function connectionFor(t: Tab): Connection {
    const bound = boundMember(t);
    // Keep an unavailable binding disconnected even if a future caller misses
    // its UI guard. Port 0 is refused before any login, as in a cleared picker.
    if (t.member && !bound) return unavailableMemberConnection(t.member, t.member_name);
    const base = bound || connection;
    const measurement = runtimeRef.current[t.id]?.measureBefore;
    // A measurement runs in its statement's schema; otherwise the tab's chosen
    // default database wins over the connection's and the source statement's.
    const database = measurement ? measurement.statement.schema ?? "" : t.database ?? (base.database || t.source?.schema || "");
    return database === base.database ? base : { ...base, database };
  }
  /** The schema a run of tab *t* uses, and why, as the Database picker shows
   * it: one rule for the schema note, Run, Explain and Run analysis. A
   * measurement runs in the schema its statement was recorded in. An open
   * session of this tab's connection (the statusSession check) runs in its
   * current schema, null or "" meaning no default schema. Otherwise the tab's
   * Database picker choice wins over the connection's default schema and the
   * schema the statement was recorded in. */
  function runSchema(t: Tab, rt: Runtime): { schema: string; why: string } {
    if (rt.measureBefore) return { schema: rt.measureBefore.statement.schema ?? "", why: "the schema it was recorded in" };
    const session = rt.session;
    if (session && !session.ended && rt.sessionKey === connectionKey(connectionFor(t)))
      return { schema: session.database_name || "", why: "the open session's current schema" };
    if (t.database != null) return { schema: t.database, why: "picked in this tab" };
    const bound = boundMember(t);
    const baseDatabase = t.member && !bound ? "" : (bound || connection).database || "";
    if (baseDatabase) return { schema: baseDatabase, why: "the connection's schema" };
    return { schema: t.source?.schema || "", why: "the schema it was recorded in" };
  }
  /** Start one capture of *sql* for tab *t* with the lab's settings. */
  async function startCapture(
    t: Tab,
    sql: string,
    parameters: unknown,
    mode: string,
    label: string,
    inSession = false,
  ) {
    // Run analysis and the explicit Explore action share the capture guards.
    if (busy || pendingStart.current || runtimeRef.current[t.id]?.running || runtimeRef.current[t.id]?.operation) return;
    const gate = mode === "explain" ? explainGate : analysisGate;
    const problem = groupMemberProblem(t) || bindingProblem(t) || sessionProblem(t.id) || gate.disabledReason;
    if (problem) {
      setStatus("");
      setError(problem);
      return;
    }
    const pending = { cancelled: false };
    pendingStart.current = pending;
    setCaptureOwner({ tabId: t.id, mode });
    setStarting(true);
    if (mode === "analyze") changeTab({ result_tab: "plan" }, t.id);
    try {
      const expectedConnection = connectionKey(connectionFor(t));
      const prepared = mode === "analyze"
        ? await api<{ sql: string; converted: boolean; warning: string; problem: string }>("analysis/prepare-query", { query: sql })
        : { sql, converted: false, warning: "", problem: "" };
      if (pending.cancelled) return;
      if (expectedConnection !== connectionKey(currentTabConnection(t.id)))
        throw new Error("The connection changed. Review the query and try again.");
      if (prepared.problem) throw new Error(prepared.problem);
      if (prepared.converted) {
        // Preserve the original editor text and make the safe projection
        // reviewable in its own tab. A second explicit action starts capture.
        const projected: Tab = { ...t, id: crypto.randomUUID(), name: `${t.name} SELECT`,
          sql: prepared.sql, parameters: JSON.stringify(parameters || {}, null, 2) };
        setTabs(ts => [...ts, projected]);
        setTabId(projected.id);
        setAnalysisWarning({ tab: projected.id, text: prepared.warning });
        setStatus("Review the converted SELECT, then choose Run analysis.");
        return;
      }
      sql = prepared.sql;
      captureName.current = t.name;
      setLastRun({ tabId: t.id, label, sql });
      lastRunInfo.current = { tabId: t.id, label, sql, parameters, mode, inSession };
      setSessionFallback(null);
      const via = connectionFor(t);
      jobConnection.current = via;
      // An open session of this tab runs Run, Explain and Run analysis in its
      // current schema (a USE, or a dropped database), which connectionFor
      // cannot see: the capture connection opens in the schema runSchema
      // names, as the note and the Database picker show it. connectionFor
      // itself is unchanged: editor/open and the session keys keep its rule.
      const now = runtimeRef.current[t.id];
      const sessionOpen = !!now && !!now.session && !now.session.ended && now.sessionKey === connectionKey(connectionFor(t));
      const captureConnection = sessionOpen && now ? { ...via, database: runSchema(t, now).schema } : via;
      const result = await api<{ id: string }>("live/start", {
        connection: captureConnection,
        query: sql,
        parameters,
        mode,
        timeout,
        // In-session runs keep the tab's temporary tables; the backend
        // refuses repeat/trace there.
        repeat: inSession || runtimeRef.current[t.id]?.measureBefore ? 1 : repeat,
        trace: inSession ? false : trace,
        conditions,
        ...(inSession ? { session_tab: t.id } : {}),
        ...(t.source ? { source: t.source } : {}),
      });
      if (pending.cancelled) {
        // Cancel was pressed while the start was on its way: stop the job
        // right away; it never becomes the running capture.
        cancelledJobs.current.add(result.id);
        setJob({ id: result.id, state: "cancelled", progress: 0, repeat: 1, error: "", result: null });
        try {
          await api("live/cancel", { id: result.id, connection: via });
          setStatus("Capture cancelled before it started. Nothing was added.");
        } catch (e) {
          setError((e as Error).message);
        }
        return;
      }
      setJob({
        id: result.id,
        state: "queued",
        progress: 0,
        repeat: mode === "explain" || runtimeRef.current[t.id]?.measureBefore ? 1 : repeat,
        error: "",
        result: null,
      });
    } catch (e) {
      offerSessionFallback((e as Error).message);
      denied(e, jobConnection.current);
      setError((e as Error).message);
    } finally {
      if (pendingStart.current === pending) pendingStart.current = null;
      setStarting(false);
    }
  }
  /** ERROR 1146 from a fresh capture connection usually means the query
   * names a TEMPORARY table that lives only in the tab's session: offer one
   * retry inside that session. Never after an in-session run already failed. */
  function offerSessionFallback(message: string) {
    const run = lastRunInfo.current;
    if (!run || run.inSession) return;
    if (!/1146|doesn't exist|does not exist/i.test(message)) return;
    const now = runtimeRef.current[run.tabId];
    if (!now?.session || now.session.ended) return;
    setSessionFallback({ tabId: run.tabId, label: run.label, sql: run.sql, parameters: run.parameters, mode: run.mode });
  }
  /** Cancel capture: the one starting, or the one running. */
  async function cancelCapture() {
    const pending = pendingStart.current;
    if (pending) {
      pending.cancelled = true;
      setStatus("Cancelling the capture…");
      return;
    }
    if (!job || (job.state !== "queued" && job.state !== "running")) return;
    cancelledJobs.current.add(job.id);
    try {
      await api("live/cancel", { id: job.id, connection: jobConnection.current });
    } catch (e) {
      setError((e as Error).message);
    }
  }
  /** Run analysis executes one SELECT after explicit review. */
  async function capture(mode: string) {
    if (mode === "explain") { runSql(false, undefined, undefined, true); return; }
    if (busy || rt.running || rt.operation || rt.stopping) return;
    const problem = groupMemberProblem(tab) || bindingProblem(tab) || sessionProblem(tab.id) || analysisGate.disabledReason;
    if (problem) { setError(problem); return; }
    setError("");
    setStatus("");
    // Read the selection and text now: a click on a button keeps the editor's
    // selection, and a shortcut runs before React has seen the latest state.
    const el = editor.current;
    const sel = el?.getSelection();
    const chosen = el && sel
      ? runTarget(el.getValue(), sel.start, sel.end)
      : target;
    if (chosen.problem) {
      setError(chosen.problem);
      return;
    }
    let parameters: unknown;
    try {
      parameters = JSON.parse(tab.parameters);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    const missing = unfilled(tab, chosen.text, parameters);
    if (missing.length) {
      setError(
        `Fill in ${listNames(missing)} under Parameters before running. ${missing.length === 1 ? "It stands" : "They stand"} for literal values the digest left out.`,
      );
      setParametersOpen(true);
      return;
    }
    if (chosen.scope === "statement") flashRange(chosen.start, chosen.end);
    setAnalysisReview({ tab: { ...tab }, sql: chosen.text, parameters, label: sentLabel(chosen), binding: analysisBinding });
  }
  function flashRange(startAt: number, endAt: number, sql = tab.sql) {
    window.clearTimeout(flashTimer.current);
    setFlash({ tabId: tab.id, sql, start: startAt, end: endAt });
    flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
  }

  // ---- Run SQL in the tab's session -------------------------------------------

  /** The session of the tab is on another connection than the one it runs on
   * now (the connection in use, or its bound group member). */
  const mismatch = !!rt.session && !rt.session.ended && !!rt.sessionKey && rt.sessionKey !== connectionKey(tabConnection);
  const mismatchReason = mismatch && rt.session
    ? `This tab is still connected to ${sessionName(rt.session)}. Close its session to run on ${connectionShortName(tabConnection)}.`
    : "";
  /** Why tab *id* may not run on the connection in use now: its session is
   * on another connection. Every execution path checks it (Run, Run all,
   * Explain, Run analysis, their shortcuts, Load more). */
  function sessionProblem(id: string) {
    const t = tabsRef.current.find((x) => x.id === id);
    if (t && groupMemberProblem(t)) return groupMemberProblem(t);
    const now = runtimeRef.current[id] || blankRuntime();
    const expected = t ? connectionFor(t) : connectionRef.current;
    if (now.session && !now.session.ended && now.sessionKey && now.sessionKey !== connectionKey(expected))
      return `This tab is still connected to ${sessionName(now.session)}. Close its session to run on ${connectionShortName(expected)}.`;
    return "";
  }
  /** The connection a tab's session operations use (Stop, Commit, Roll
   * back): the one the session or the request on its way was sent with,
   * never the picker's current choice. */
  function sessionConnectionOf(id: string) {
    const now = runtimeRef.current[id] || blankRuntime();
    return now.execConnection || now.sessionConnection;
  }
  function noSessionConnection(id: string) {
    const now = runtimeRef.current[id];
    return now?.session
      ? `This tab's session is on ${sessionName(now.session)}. Switch to that connection to stop, commit or roll it back, or close the session.`
      : "This tab has no session.";
  }
  /**
   * Run (the selection, else the statement at the cursor) or Run all (every
   * statement). *text* and *caret* replace the editor's when a statement was
   * just added (schema browser) and the field has not re-rendered yet. The
   * server decides what runs: it gets the whole text with the selection, or
   * only the caret, and splits with the session's own rules.
   */
  function runSql(all: boolean, text?: string, caret?: number, explain = false, runTab = tab) {
    setSessionFallback(null);
    setRunClientMissing(null);
    setError("");
    setStatus("");
    const t = runTab;
    // The text and the main selection as they are now, not the React state a
    // render ago; *text* and *caret* override both after a scripted edit.
    const el = editor.current;
    const sql = text ?? el?.getValue() ?? t.sql;
    const sel = caret === undefined ? el?.getSelection() : undefined;
    const lo = caret ?? sel?.start ?? cursor.start;
    const hi = caret ?? sel?.end ?? cursor.end;
    const now = runtimeRef.current[t.id] || blankRuntime();
    const part = sqlRun(sql, lo, hi, all, now.session?.backslash_escapes ?? true);
    if (part.problem) {
      setError(part.problem);
      return;
    }
    const problem = groupMemberProblem(t) || bindingProblem(t) || sessionProblem(t.id);
    if (problem) {
      setError(problem);
      return;
    }
    if (now.running || now.operation || now.stopping || busy || pendingStart.current) return;
    if (now.session?.ended) {
      setError("This tab's session ended. Choose Reconnect to start a new session.");
      return;
    }
    // Explain fills :name placeholders from Parameters (a digest from Top
    // statements); the server binds them with Run analysis's literals.
    let explainParameters: Record<string, unknown> | undefined;
    if (explain && /(^|[^:]):[A-Za-z_]/.test(sql.slice(part.start, part.end))) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(t.parameters || "{}");
      } catch (e) {
        setError(`Parameters: ${(e as Error).message}`);
        setParametersOpen(true);
        return;
      }
      const missing = unfilled(t, sql.slice(part.start, part.end), parsed);
      if (missing.length) {
        setError(`Fill in ${listNames(missing)} under Parameters before Explain. ${missing.length === 1 ? "It stands" : "They stand"} for literal values the digest left out.`);
        setParametersOpen(true);
        return;
      }
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed) && Object.keys(parsed).length)
        explainParameters = parsed as Record<string, unknown>;
    }
    if (part.scope === "statement" && part.total > 1) flashRange(part.start, part.end, sql);
    const payload: Record<string, unknown> = {
      tab_id: t.id,
      connection: connectionFor(t),
      sql,
      ...(part.selection ? { selection: part.selection } : {}),
      ...(part.cursor !== undefined ? { cursor: part.cursor } : {}),
      limit: t.limit ?? DEFAULT_ROW_LIMIT,
      timeout: layout.run_timeout || 0,
      on_error: t.on_error ?? "stop",
      ...(explain ? { explain_format: effectiveExplainFormat } : {}),
      ...(explainParameters ? { parameters: explainParameters } : {}),
      wait: 2,
      ...(now.safe === true ? { safe_updates: true } : {}),
    };
    const gate = editorPrivilegeGate(currentPreview(previewKey(payload)));
    if (!explain && gate.disabledReason) {
      setError(gate.disabledReason);
      return;
    }
    void send(t.id, "editor/run", payload, { runSql: sql, explain, explainParameters });
  }
  async function send(id: string, path: string, payload: Record<string, unknown>, meta: SendMeta) {
    const now = runtimeRef.current[id];
    if (now?.running || now?.operation || now?.stopping) return;
    const isRun = path !== "editor/open";
    const operation = meta.action || (isRun ? "run" : "open");
    const pendingText = meta.action === "commit" ? "Committing…" : meta.action === "rollback" ? "Rolling back…"
      : payload.safe_updates !== undefined && now?.session && !now.session.ended ? "Updating session settings…" : "Reconnecting…";
    const gen = (runtimeRef.current[id]?.gen ?? 0) + 1;
    // The connection of this request, before any reply: Stop uses it even
    // when the picker changes while the request is on its way.
    patchRuntime(id, { running: isRun, operation, note: "", noteError: false, gen, execConnection: payload.connection as Connection,
      ...(!meta.action && !meta.merge && isRun ? { explaining: !!meta.explain } : {}),
      stopFeedback: IDLE_ACTION, stopGen: null,
      sessionFeedback: operation === "run" ? IDLE_ACTION : { phase: "pending", message: pendingText, startedAt: Date.now(), endedAt: null } });
    let reply: EditorReply;
    try {
      reply = await api<EditorReply>(path, payload);
    } catch (e) {
      patchRuntime(id, { running: false, explaining: false, operation: "", execConnection: null });
      const err = e as ApiError;
      denied(err, payload.connection as Connection);
      if (meta.action) {
        const failure = writeFailure(e, { audited: true });
        patchRuntime(id, { sessionFeedback: { ...failure, message: failure.phase === "unknown" ? `Outcome unknown. ${failure.message}` : failure.message,
          startedAt: null, endedAt: Date.now() } });
      } else {
        if (!isRun) patchRuntime(id, { sessionFeedback: { phase: "failed", message: err.message, code: clientMissingCode(e) || undefined, startedAt: null, endedAt: Date.now() } });
        else if (clientMissingCode(e)) setRunClientMissing({ tabId: id, connection: connectionKey(payload.connection as Connection),
          text: isClientAuthPlugin(e) ? err.message : "",
          retry: () => void send(id, path, payload, meta) });
        else setError(err.message);
      }
      return;
    }
    handle(id, path, payload, meta, reply, gen);
  }
  function adoptSession(id: string, session: EditorSession | null | undefined, payload: Record<string, unknown>) {
    if (session === undefined) return;
    patchRuntime(id, (r) => {
      if (!session) return { session: null };
      const opened = !r.session || r.session.ended || r.session.connection_id !== session.connection_id;
      // The session this tab had is gone without an end the editor saw (the
      // workspace server restarted): say that its settings and any open
      // transaction did not carry over to the new one.
      const lost = !!r.session && !r.session.ended && r.session.connection_id !== session.connection_id;
      const notice = lost
        ? {
            note: `This tab's earlier session (connection ${r.session!.connection_id ?? "?"}) was gone, so this run opened a new one. Its session settings and any open transaction did not carry over.`,
            noteError: true,
          }
        : {};
      if (opened && !session.ended && payload.connection)
        return { session, sessionKey: connectionKey(payload.connection as Connection), sessionConnection: payload.connection as Connection, ...notice };
      return { session, ...notice };
    });
  }
  function handle(id: string, path: string, payload: Record<string, unknown>, meta: SendMeta, reply: EditorReply, gen: number) {
    const previousSession = runtimeRef.current[id]?.session;
    adoptSession(id, reply.session, payload);
    denied(reply, payload.connection as Connection);
    const done = { running: false, explaining: false, operation: "" as const, execConnection: null, sessionFeedback: IDLE_ACTION };
    if (reply.status === "needs_confirmation" && reply.confirmation) {
      patchRuntime(id, done);
      let again = "";
      if (meta.confirmed)
        again =
          Date.now() - meta.confirmed.issued >= meta.confirmed.ttl * 1000
            ? "The confirmation expired after 5 minutes, so nothing was sent. Review the statements again."
            : "Something changed since you reviewed this (the session or the statements), so nothing was sent. Review it again.";
      // The request as first asked: a confirm or a decline adds its own fields.
      const base = { ...payload };
      delete base.confirmed;
      delete base.confirm_token;
      delete base.decline;
      delete base.typed_target;
      setPending({
        tabId: id,
        path,
        payload: base,
        meta: { ...meta, confirmed: undefined },
        confirmation: reply.confirmation,
        typedTarget: "",
        issued: Date.now(),
        key: connectionKey(payload.connection as Connection),
        again,
      });
      return;
    }
    if (reply.status === "refused" || reply.status === "invalid") {
      patchRuntime(id, done);
      setError(reply.message || "Nothing was sent.");
      return;
    }
    if (reply.status === "declined") {
      patchRuntime(id, done);
      note(id, "Cancelled. Nothing was sent.");
      return;
    }
    const run = reply.run;
    if (!run || path === "editor/open") {
      patchRuntime(id, done);
      if (path === "editor/open" && reply.session && !reply.session.ended) {
        if (!previousSession || previousSession.ended || previousSession.connection_id !== reply.session.connection_id)
          onConnected?.(payload.connection as Connection);
        note(id, `Session open (connection ${reply.session.connection_id ?? "?"}); safe updates ${reply.session.safe_updates ? "on" : "off"}.`);
      }
      return;
    }
    if (run.state === "running") {
      if (!meta.merge && !meta.action) {
        patchRuntime(id, { run, explain: !!meta.explain, explainParameters: meta.explainParameters, runSql: meta.runSql, binary: {}, reloaded: {}, ...bound(id, reply.session) });
        changeTab({ result_tab: "messages" }, id);
      }
      void poll(id, run.id, meta, gen);
      return;
    }
    finish(id, run, meta, reply.session);
  }
  /** Where results come from: the connection key and the session id. */
  function bound(id: string, session?: EditorSession | null) {
    const now = runtimeRef.current[id] || blankRuntime();
    const s = session ?? now.session;
    return {
      runKey: now.sessionKey,
      runSessionId: s?.connection_id ?? null,
      runConnectionName: s ? sessionName(s) : now.sessionConnection ? connectionShortName(now.sessionConnection) : "",
      runConnection: now.sessionConnection,
      runBackslash: s?.backslash_escapes !== false,
    };
  }
  async function poll(id: string, runId: string, meta: SendMeta, gen: number) {
    polls.current.get(id)?.abort();
    const controller = new AbortController();
    polls.current.set(id, controller);
    let failures = 0;
    for (;;) {
      // A newer request of this tab owns the polling now.
      if ((runtimeRef.current[id]?.gen ?? 0) !== gen) return;
      let reply: EditorReply;
      try {
        reply = await api<EditorReply>(
          "editor/status",
          { tab_id: id, run_id: runId, wait: 5, connection: sessionConnectionOf(id) || currentTabConnection(id) },
          controller.signal,
        );
      } catch (e) {
        if (controller.signal.aborted) return;
        // The statement may still be running: keep Stop, say so, and ask again.
        failures++;
        patchRuntime(id, {
          pollTrouble: true,
          note: `Could not read the run's state (${(e as Error).message}). It may still be running; Stop still works. Trying again.`,
          noteError: true,
        });
        await new Promise((r) => window.setTimeout(r, Math.min(5000, 500 * failures)));
        if (controller.signal.aborted) return;
        continue;
      }
      if (controller.signal.aborted) return;
      if (failures) {
        failures = 0;
        patchRuntime(id, { pollTrouble: false, note: "", noteError: false });
      }
      if (reply.session !== undefined) patchRuntime(id, { session: reply.session ?? null });
      const run = reply.run;
      if (!run || run.id !== runId) {
        patchRuntime(id, { running: false, operation: "", execConnection: null, sessionFeedback: IDLE_ACTION });
        return;
      }
      if (run.state !== "running") {
        polls.current.delete(id);
        finish(id, run, meta, reply.session);
        return;
      }
      if (!meta.merge && !meta.action) patchRuntime(id, { run });
    }
  }
  function finish(id: string, run: EditorRun, meta: SendMeta, session?: EditorSession | null) {
    if (run.error) denied(run.error, sessionConnectionOf(id));
    // A run that created or dropped a database (or one the editor cannot
    // classify) makes the database list of the connection it ran on stale,
    // whichever connection is on screen now: views on screen read it again,
    // a hidden editor when it is shown (this callback may be from an earlier
    // render, so it never decides visibility itself).
    if (!meta.action && runMayChangeDatabases(run)) invalidateSchemaCatalog(sessionConnectionOf(id) || currentTabConnection(id));
    // A completed statement does not acknowledge a Stop still on its way.
    patchRuntime(id, { running: false, explaining: false, operation: "", execConnection: null, pollTrouble: false });
    setHistoryTick((n) => n + 1);
    if (meta.action) {
      const word = meta.action === "commit" ? "Committed" : "Rolled back";
      if (run.state === "complete") {
        patchRuntime(id, { sessionFeedback: IDLE_ACTION });
        note(id, `${word}.`);
        meta.after?.();
      } else {
        const err = run.error || run.results.find((r) => r.type === "error");
        const unknown = run.state === "session_ended";
        patchRuntime(id, { sessionFeedback: { phase: unknown ? "unknown" : "failed",
          message: `${unknown ? "Outcome unknown" : `${meta.action === "commit" ? "Commit" : "Roll back"} failed`}: ${(err && "message" in err && err.message) || run.message || run.state}${unknown ? ". It is not sent again. Check the Audit log." : ""}`,
          startedAt: null, endedAt: Date.now() } });
      }
      return;
    }
    if (meta.merge) {
      const merge = meta.merge;
      patchRuntime(id, (r) => {
        if (!r.run) return {};
        const fresh = run.results.map((x) => ({ ...x, statement: merge.statement }));
        const results = [...r.run.results.filter((x) => x.statement < merge.statement), ...fresh, ...r.run.results.filter((x) => x.statement > merge.statement)];
        const statements = r.run.statements.map((s) =>
          s.index === merge.statement ? { ...s, status: run.statements[0]?.status ?? s.status, duration_ms: run.statements[0]?.duration_ms ?? s.duration_ms, server_stats: run.statements[0]?.server_stats } : s,
        );
        const at = new Date().toLocaleTimeString();
        return {
          run: { ...r.run, results, statements },
          reloaded: {
            ...r.reloaded,
            [merge.result]:
              run.state === "complete"
                ? `Ran again at ${at} with up to ${merge.limit.toLocaleString()} rows; rows may differ from the first run.`
                : `Ran again at ${at}: ${run.error?.message || run.message || run.state}.`,
          },
        };
      });
      return;
    }
    patchRuntime(id, { run, explain: !!meta.explain, explainParameters: meta.explainParameters, runSql: meta.runSql, binary: {}, reloaded: {}, ...bound(id, session) });
    const rows = rowResults(run);
    changeTab({ result_tab: run.error || !rows.length || run.state === "session_ended" ? "messages" : "result:0" }, id);
    if (run.state === "stopped") note(id, run.stopped?.message || "Stopped.");
  }
  /** SQL export alone asks for declared types, using the result's source
   * connection and statement, never the editable INSERT target or picker. */
  async function columnTypesFor(result: EditorRows): Promise<(string | null)[]> {
    const original = result.columns.map(c => c.type);
    const source = rt.run?.statements[result.statement];
    const backslash = rt.runBackslash;
    if (!source || !rt.runConnection || original.every(Boolean)) return original;
    const references = selectColumnReferences(source.text, result.columns.map(c => c.name), backslash);
    const target = insertDefaults(rt.run, result, backslash);
    if (!references?.some(Boolean) || !target.schema || !target.table) return original;
    try {
      const catalog = await api<CatalogTable>("live/catalog", {
        connection: rt.runConnection, schema: target.schema, table: target.table,
      });
      if (catalog.schema !== target.schema || catalog.table !== target.table) return original;
      const declared = new Map(catalog.columns.map(c => [c.name.toLowerCase(), c.type]));
      return original.map((type, i) => type || (references[i] ? declared.get(references[i]!.toLowerCase()) : null) || null);
    } catch {
      // Missing or unreadable catalog metadata never changes literal quoting.
      return original;
    }
  }
  async function stop(id = tab.id) {
    const now = runtimeRef.current[id];
    if (!now?.running || now.stopping || now.stopGen === now.gen) return;
    const via = sessionConnectionOf(id);
    if (!via) {
      setError(noSessionConnection(id));
      return;
    }
    patchRuntime(id, { stopping: true, stopGen: now.gen,
      stopFeedback: { phase: "pending", message: "Sending KILL QUERY…", startedAt: Date.now(), endedAt: null } });
    try {
      const reply = await api<EditorStop>("editor/stop", { tab_id: id, connection: via });
      patchRuntime(id, { stopFeedback: { phase: "done", message: reply.message, startedAt: null, endedAt: Date.now() } });
      if (!reply.stopped && !reply.stop_requested) patchRuntime(id, { stopGen: null });
      if (reply.session !== undefined && runtimeRef.current[id]?.running) patchRuntime(id, { session: reply.session });
    } catch (e) {
      const failure = writeFailure(e, { audited: true });
      patchRuntime(id, { stopFeedback: { ...failure, message: failure.phase === "unknown" ? `Outcome unknown. ${failure.message}` : failure.message,
        startedAt: null, endedAt: Date.now() } });
    } finally {
      patchRuntime(id, { stopping: false });
    }
  }
  function commit(id = tab.id, after?: () => void) {
    const via = sessionConnectionOf(id);
    if (!via) return setError(noSessionConnection(id));
    void send(id, "editor/commit", { tab_id: id, connection: via, wait: 10 }, { runSql: "", action: "commit", after });
  }
  function rollback(id = tab.id) {
    const via = sessionConnectionOf(id);
    if (!via) return setError(noSessionConnection(id));
    void send(id, "editor/rollback", { tab_id: id, connection: via, wait: 10 }, { runSql: "", action: "rollback" });
  }
  /** Start a new session after one ended (never done without asking). */
  function reconnect(id = tab.id) {
    const t = tabsRef.current.find((x) => x.id === id);
    if (!t) return;
    const problem = groupMemberProblem(t);
    if (problem) return setError(problem);
    const now = runtimeRef.current[id] || blankRuntime();
    if (now.running || now.operation || now.stopping) return;
    setRunClientMissing(null);
    setError("");
    // Keep the ended-session banner (and its focused button) until the reply.
    void send(id, "editor/open", { tab_id: id, connection: connectionFor(t), ...(now.safe === true ? { safe_updates: true } : {}) }, { runSql: "" });
  }
  function setSafeUpdates(on: boolean) {
    const id = tab.id;
    const now = runtimeRef.current[id] || blankRuntime();
    if (on && !(now.session && !now.session.ended)) {
      patchRuntime(id, { safe: true });
      note(id, "Safe updates will be on when the first run opens this tab's session.");
      return;
    }
    if (!on) patchRuntime(id, { safe: false });
    const problem = sessionProblem(id);
    if (problem) {
      setError(problem);
      return;
    }
    void send(id, "editor/open", { tab_id: id, connection: connectionFor(tab), safe_updates: on }, { runSql: "" });
  }

  // ---- SQL files and the default database ------------------------------------

  /** The tab's SQL as a file on disk: ⌘O opens, ⌘S saves back, ⇧⌘S asks where. */
  async function openSqlFile() {
    if (rt.fileBusy) return;
    patchRuntime(tab.id, { fileBusy: true });
    setError("");
    const opened = openingTarget();
    if (!opened) {
      patchRuntime(tab.id, { fileBusy: false });
      openingChanged();
      return;
    }
    try {
      const reply = await api<EditorFileOpenReply>("editor/file/open", {});
      if (reply.cancelled) return;
      const id = crypto.randomUUID();
      const base = reply.name.replace(/\.(sql|txt)$/i, "");
      const name = clipName(base || reply.name, MAX_TAB_NAME) || reply.name;
      setTabs((ts) => [...ts, { id, name, sql: reply.sql, parameters: "{}", ...opened.binding }]);
      setTabId(id);
      patchRuntime(id, { file: { token: reply.token, name: reply.name, savedSql: reply.sql } });
      note(id, reply.warning ? `Opened ${reply.name}. ${reply.warning}` : `Opened ${reply.name}.`);
      window.setTimeout(() => focusTab(id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      patchRuntime(tab.id, { fileBusy: false });
    }
  }
  /** Save *id* back to its file, or ask where (Save as, or no file yet).
   * Returns false when the user cancelled or the write failed. */
  async function saveTab(id: string, saveAs = false): Promise<boolean> {
    const t = tabsRef.current.find((x) => x.id === id);
    const now = runtimeRef.current[id];
    if (!t || now?.fileBusy) return false;
    patchRuntime(id, { fileBusy: true });
    try {
      const save = (askWhere: boolean) => api<EditorFileSaveReply>("editor/file/save", {
        sql: t.sql,
        ...(askWhere || !now?.file ? { save_as: true, name: now?.file?.name || `${t.name}.sql` } : { token: now.file.token }),
      });
      let reply: EditorFileSaveReply;
      try {
        reply = await save(saveAs);
      } catch (e) {
        // Keep ownership of this operation while recovering an expired token.
        if ((e as ApiError).code !== "unknown_token" || saveAs || !now?.file) throw e;
        note(id, "The file binding expired. Choose where to save this SQL file.");
        reply = await save(true);
      }
      if (reply.cancelled) {
        note(id, "Save cancelled. The SQL file was not saved.");
        return false;
      }
      patchRuntime(id, { file: { token: reply.token, name: reply.name, savedSql: t.sql } });
      note(id, `Saved to ${reply.path}.`);
      return true;
    } catch (e) {
      const err = e as ApiError;
      note(id, err.message, true);
      return false;
    } finally {
      patchRuntime(id, { fileBusy: false });
    }
  }
  /** The default database of the tab's session (USE), remembered with the tab
   * and applied when a new session opens. */
  async function chooseDatabase(name: string) {
    const id = tab.id;
    const problem = sessionProblem(id);
    if (problem) {
      note(id, problem, true);
      return;
    }
    const previous = tab.database;
    changeTab({ database: name }, id);
    const now = runtimeRef.current[id];
    const session = now?.session;
    if (!session || session.ended) {
      note(id, name ? `${name} is the default database when this tab's session opens.` : "A new session starts with no default database.");
      return;
    }
    if (!name) {
      // There is no un-USE: a live session keeps its database until it closes.
      note(id, "The open session keeps its database; a new session starts with none. Close the session to clear it.");
      return;
    }
    if (session.database_name === name) return;
    patchRuntime(id, { fileBusy: true });
    try {
      const via = sessionConnectionOf(id) || connectionFor(tab);
      const reply = await api<EditorUseReply>("editor/use", { tab_id: id, connection: via, database: name });
      if (reply.session) adoptSession(id, reply.session, { connection: via });
      note(id, reply.session ? `Default database: ${reply.database ?? name}.` : `${name} is the default database when this tab's session opens.`);
    } catch (e) {
      changeTab({ database: previous }, id);
      note(id, (e as Error).message, true);
    } finally {
      patchRuntime(id, { fileBusy: false });
    }
  }
  /** Load more is only for results of the session in use, on the
   * connection in use: never another server's, or a session since closed. */
  function canReload(id: string) {
    const now = runtimeRef.current[id] || blankRuntime();
    return (
      !!now.run &&
      !now.running &&
      !!now.session &&
      !now.session.ended &&
      now.session.connection_id === now.runSessionId &&
      !!now.runKey &&
      now.runKey === now.sessionKey &&
      now.runKey === connectionKey(currentTabConnection(id)) &&
      !groupMemberProblem(tabsRef.current.find((t) => t.id === id) || tab)
    );
  }
  /** Load more: run one read again with a larger row limit, in the same
   * session: the run's text with that statement as the selection. */
  function loadMore(result: EditorRows) {
    const now = runtimeRef.current[tab.id];
    const run = now?.run;
    if (!run || !canReload(tab.id)) return;
    const statement = run.statements[result.statement];
    if (!statement) return;
    const limit = Math.min(MAX_ROW_LIMIT, Math.max(result.limit * 10, result.limit + DEFAULT_ROW_LIMIT));
    const index = rowResults(run).indexOf(result);
    void send(
      tab.id,
      "editor/run",
      {
        tab_id: tab.id,
        connection: connectionFor(tab),
        sql: now.runSql,
        selection: { start: statement.start, end: statement.end },
        ...(now.explain ? { explain_format: statement.sent?.startsWith("EXPLAIN FORMAT=TREE ") ? "tree"
          : statement.sent?.startsWith("EXPLAIN FORMAT=JSON ") ? "json" : "table" } : {}),
        ...(now.explain && now.explainParameters ? { parameters: now.explainParameters } : {}),
        limit,
        timeout: layout.run_timeout || 0,
        wait: 2,
      },
      { runSql: now.runSql, explain: now.explain, explainParameters: now.explainParameters, merge: { statement: result.statement, result: index, limit } },
    );
  }
  async function confirmPending() {
    const p = pending;
    if (!p || (p.confirmation.typed_target != null && p.typedTarget !== p.confirmation.typed_target)) return;
    setPendingBusy(true);
    try {
      if (policy) {
        // The type or read-only flag may have changed in another window or
        // the CLI: read them again, and send nothing under another policy.
        let fresh: Connection;
        try {
          fresh = await policy.check(currentTabConnection(p.tabId));
        } catch (e) {
          setPending(null);
          setError(`Could not read this connection's settings (${(e as Error).message}), so nothing was sent.`);
          return;
        }
        if (connectionKey(fresh) !== p.key || connectionKey(currentTabConnection(p.tabId)) !== p.key) {
          setPending(null);
          void send(p.tabId, p.path, { ...p.payload, decline: true }, p.meta);
          setError("This connection's type or read-only setting changed after you reviewed the statements, so nothing was sent. Run again to review them under the new setting.");
          return;
        }
      }
      setPending(null);
      void send(
        p.tabId,
        p.path,
        { ...p.payload, confirmed: true, confirm_token: p.confirmation.token, typed_target: p.typedTarget },
        { ...p.meta, confirmed: { issued: p.issued, ttl: p.confirmation.expires_in } },
      );
    } finally {
      setPendingBusy(false);
    }
  }
  function declinePending() {
    const p = pending;
    if (!p) return;
    setPending(null);
    // The server records the decline (audit) and sends nothing.
    void send(p.tabId, p.path, { ...p.payload, decline: true }, p.meta);
  }
  function removeTab(id: string) {
    polls.current.get(id)?.abort();
    polls.current.delete(id);
    const rest = { ...runtimeRef.current };
    delete rest[id];
    runtimeRef.current = rest;
    setRuntime(rest);
    // The replacement tab keeps the member binding only while it still names
    // one; a mid-change group leaves it unbound rather than on another member.
    const opened = openingTarget();
    setTabs((ts) => {
      const index = ts.findIndex((t) => t.id === id);
      const remainingTabs = ts.filter((t) => t.id !== id);
      if (remainingTabs.length === 0) {
        const freshId = crypto.randomUUID();
        setTabId(freshId);
        return [{
          id: freshId,
          name: "Query 1",
          sql: "",
          parameters: "{}",
          ...(opened?.binding || {}),
        }];
      }
      if (id === tabId && remainingTabs.length) setTabId(remainingTabs[Math.max(0, index - 1)].id);
      return remainingTabs;
    });
  }
  /** Close a tab (or only its session): a transaction that is open, or may
   * be, asks Commit, Roll back or Cancel first. */
  async function requestClose(id: string, keepTab = false, discardFile = false): Promise<"closed" | "asks" | "running" | "error"> {
    const now = runtimeRef.current[id];
    if (now?.running) {
      setError("A statement is running in this tab. Stop it before closing.");
      return "running";
    }
    if (now?.operation || now?.stopping) return "running";
    // A file-backed tab with unsaved edits asks before anything closes.
    const t = tabsRef.current.find((x) => x.id === id);
    if (!discardFile && !keepTab && now?.file && t && t.sql !== now.file.savedSql) {
      setClosingFile({ tabId: id, keepTab });
      return "asks";
    }
    if (t && groupMemberProblem(t) && !sessionConnectionOf(id)) {
      if (!keepTab) { removeTab(id); return "closed"; }
      note(id, groupMemberProblem(t), true);
      return "error";
    }
    patchRuntime(id, { operation: "close", note: "", noteError: false, sessionFeedback: { phase: "pending", message: "Closing the session…", startedAt: Date.now(), endedAt: null } });
    try {
      const reply = await api<EditorClose>("editor/close", { tab_id: id, connection: sessionConnectionOf(id) || currentTabConnection(id) });
      if (reply.closed) {
        if (keepTab) {
          patchRuntime(id, { session: null, sessionKey: "", sessionConnection: null });
          patchRuntime(id, { sessionFeedback: IDLE_ACTION });
          note(id, "Session closed. The next run opens a new one on the connection in use.");
        } else removeTab(id);
        return "closed";
      }
      if (reply.session) adoptSession(id, reply.session, {});
      patchRuntime(id, { sessionFeedback: IDLE_ACTION });
      setClosing({ tabId: id, known: reply.reason === "transaction_open", keepTab, discardFile });
      return "asks";
    } catch (e) {
      const failure = writeFailure(e);
      patchRuntime(id, { sessionFeedback: { ...failure, message: failure.phase === "unknown" ? `Outcome unknown. ${failure.message}` : failure.message,
        startedAt: null, endedAt: Date.now() } });
      return "error";
    } finally {
      // A closed tab has already removed its runtime; do not recreate it.
      if (runtimeRef.current[id]) patchRuntime(id, { operation: "" });
    }
  }
  async function closeDiscarding() {
    const c = closing;
    if (!c) return;
    setClosingBusy(true);
    try {
      const reply = await api<EditorClose>("editor/close", {
        tab_id: c.tabId,
        discard: true,
        connection: sessionConnectionOf(c.tabId) || currentTabConnection(c.tabId),
      });
      if (reply.closed) {
        setClosing(null);
        if (c.keepTab) {
          patchRuntime(c.tabId, { session: null, sessionKey: "", sessionConnection: null });
          note(c.tabId, "Rolled back and closed the session.");
        } else removeTab(c.tabId);
      }
    } catch (e) {
      setError((e as Error).message);
      setClosing(null);
    } finally {
      setClosingBusy(false);
    }
  }
  function commitAndClose() {
    const c = closing;
    if (!c) return;
    setClosing(null);
    commit(c.tabId, () => void requestClose(c.tabId, c.keepTab, c.discardFile));
  }

  // ---- Editing helpers (schema browser, history, error positions) ----------

  /** Insert *text* at every cursor, as one undo step. */
  function insertAtCursor(text: string) {
    const el = editor.current;
    if (!el) return;
    el.focus();
    el.insertAtCursor(text);
    if (narrow) setSheet("");
  }
  /** Open *statement* in a new tab, selected, without running it. */
  function openStatement(statement: string, indexOrigin?: SuggestedIndexOrigin, from?: string) {
    const opened = openingTarget(indexOrigin?.target.key || from);
    if (!opened) {
      setError("The connection changed. Refresh the statements before opening the suggested index.");
      return;
    }
    const body = statement.trim().replace(/;$/, "");
    const sql = body ? `${body};` : "";
    const id = crypto.randomUUID();
    if (indexOrigin) patchRuntime(id, { indexOrigin, indexDdl: sql });
    setTabs((ts) => [
      ...ts,
      {
        id,
        name: `Query ${ts.length + 1}`,
        sql,
        parameters: "{}",
        ...(indexOrigin ? { source: normalizeSource({ kind: "digest", digest: indexOrigin.statement.digest, schema: indexOrigin.statement.schema }) || undefined,
          target: { ...indexOrigin.target } } : {}),
        ...opened.binding,
      },
    ]);
    setTabId(id);
    window.requestAnimationFrame(() => {
      editor.current?.setSelection(0, normalizeEol(body).length, { focus: true });
    });
  }
  /** Add *statement* at the end of the tab, selected, without running it. */
  function appendStatement(statement: string) {
    const el = editor.current;
    const sql = el?.getValue() ?? normalizeEol(tab.sql);
    const d = sql.trim() ? delimiterAt(sql, sql.length, runtimeRef.current[tab.id]?.session?.backslash_escapes ?? true) : ";";
    const body = normalizeEol(statement.trim().replace(/;$/, ""));
    const trimmed = sql.replace(/\s+$/, "");
    const lead = !trimmed ? "" : !trimmed.endsWith(d) ? `${d}\n\n` : sql.endsWith("\n\n") ? "" : sql.endsWith("\n") ? "\n" : "\n\n";
    const at = sql.length + lead.length;
    // One replacement, one undo step; onChange carries the text to the tab.
    if (!el || !el.replaceRange(sql.length, sql.length, lead + body + d, { select: { start: at, end: at + body.length }, focus: true }))
      changeTab({ sql: sql + lead + body + d });
  }
  /** Add *statement* at the end of the tab and run it. */
  function appendAndRun(statement: string) {
    const el = editor.current;
    const sql = el?.getValue() ?? normalizeEol(tab.sql);
    const d = sql.trim() ? delimiterAt(sql, sql.length, runtimeRef.current[tab.id]?.session?.backslash_escapes ?? true) : ";";
    const trimmed = sql.replace(/\s+$/, "");
    const lead = !trimmed ? "" : !trimmed.endsWith(d) ? `${d}\n\n` : sql.endsWith("\n\n") ? "" : sql.endsWith("\n") ? "\n" : "\n\n";
    const addition = lead + normalizeEol(statement) + d;
    const at = sql.length + lead.length;
    if (!el || !el.replaceRange(sql.length, sql.length, addition, { select: { start: at, end: at }, focus: true }))
      changeTab({ sql: sql + addition });
    if (narrow) setSheet("");
    runSql(false, sql + addition, at);
  }
  function jump(offset: number) {
    // Server positions belong to the submitted SQL, which may still have CRLF.
    const at = normalizedOffset(rt.runSql || tab.sql, offset);
    editor.current?.setSelection(at, at, { focus: true, scroll: "center" });
  }

  // ---- Format SQL ------------------------------------------------------------

  const [formatting, setFormatting] = useState(false);
  const formatBusy = useRef(false);
  /**
   * Format the selection, or the whole tab when nothing is selected, as one
   * undo step (sqlFormat.ts, loaded on first use). Nothing runs on the server.
   * The selection stays on the formatted text; without one the caret keeps
   * its place among the statement's characters (where it is when the
   * formatter is ready: the first use waits for its chunk).
   */
  async function formatSql() {
    const el = editor.current;
    if (!el || formatBusy.current) return;
    const id = tab.id;
    if (el.getSelections().length > 1) {
      note(id, "Format SQL works with one cursor or selection; several are set. Keep one and try again.", true);
      return;
    }
    const sql = el.getValue();
    const first = el.getSelection();
    const whole = first.start === first.end;
    const from = whole ? 0 : first.start;
    const to = whole ? sql.length : first.end;
    formatBusy.current = true;
    setFormatting(true);
    note(id, whole ? "Formatting the tab…" : "Formatting the selection…");
    try {
      const { formatEditorText, describeKept, mapOffset } = await import("./sqlFormat");
      if (el.getValue() !== sql || tabRef.current !== id) {
        note(id, "The text changed while Format SQL was starting, so nothing was changed. Try again.", true);
        return;
      }
      // Hidden meanwhile (Query digest, table data, another place): a hidden
      // editor cannot take the edit as one undo step, so nothing changes.
      if (!el.isShown()) {
        note(id, "The editor was hidden while Format SQL was starting, so nothing was changed. Try again.", true);
        return;
      }
      // A selection made, changed or cleared meanwhile changes what to
      // format: nothing changes. A caret moved without selecting is kept.
      const caret = el.getSelection().start;
      const end = el.getSelection().end;
      if (el.getSelections().length > 1 || (caret === end) !== whole || (!whole && (caret !== from || end !== to))) {
        note(id, "The selection changed while Format SQL was starting, so nothing was changed. Try again.", true);
        return;
      }
      const session = runtimeRef.current[id]?.session;
      // The selection, or the whole text; the result is checked against the
      // whole text (sqlFormat.formatEditorText).
      const outcome = formatEditorText(sql, whole ? 0 : from, whole ? 0 : to, {
        dialect: session?.engine === "mariadb" ? "mariadb" : "mysql",
        backslash: session?.backslash_escapes ?? true,
      });
      if (outcome.status !== "formatted") {
        note(id, outcome.message, outcome.status === "refused");
        return;
      }
      // One replacement, one undo step: the caret keeps its place among the
      // statement's characters, or the formatted text stays selected.
      const at = whole ? mapOffset(sql, outcome.text, caret) : from;
      el.replaceRange(from, to, outcome.text, {
        select: { start: at, end: whole ? at : from + outcome.text.length },
        focus: true,
      });
      const count = outcome.formatted === 1 ? "1 statement" : `${outcome.formatted} statements`;
      note(id, `${whole ? `Formatted ${count}` : "Formatted the selection"}${outcome.kept.length ? `; ${describeKept(outcome.kept)}` : ""}. ${MAC ? "⌘Z" : "Ctrl+Z"} undoes it.`);
    } catch (e) {
      note(id, `Format SQL could not start: ${(e as Error).message}`, true);
    } finally {
      formatBusy.current = false;
      setFormatting(false);
    }
  }

  // ---- Layout: splitter and panes -------------------------------------------

  /** The splitter sets the editor height. A drag past the cap stores the cap
   * (a taller window then opens no larger than what was seen); a taller
   * height stored in a taller window is kept. */
  function setHeight(height: number) {
    const next = Math.round(Math.max(120, Math.min(2000, height)));
    setLayout((l) => {
      const value = next < editorCap ? next : Math.max(l.height, editorCap);
      return value === l.height ? l : { ...l, height: value };
    });
  }
  function togglePane(pane: "browser" | "history", opener?: HTMLElement) {
    if (narrow) {
      if (sheet !== pane) sheetOpener.current = opener || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
      setSheet(s => s === pane ? "" : pane);
    } else setLayout((l) => ({ ...l, [pane]: !l[pane] }));
  }

  const pendingFill = unfilled(tab, tab.sql, parseParameters(tab.parameters));
  const unbound = bindingProblem(tab);
  const session = rt.session;
  // The schema a run of this tab uses, and why: runSchema is the one rule the
  // note, Run, Explain and Run analysis share, matching the Database picker.
  const tabRunSchema = runSchema(tab, rt);
  const tabSchemaNote = !tab.source
    ? ""
    : tabRunSchema.schema
      ? ` · runs in ${tabRunSchema.schema}, ${tabRunSchema.why}`
      : " · runs without a default schema";
  const running = rt.running;
  const runBusy = running && !rt.explaining && (!rt.operation || rt.operation === "run");
  const sessionBusy = !!rt.operation || rt.stopping;
  const transactionUnknown = rt.sessionFeedback.phase === "unknown";
  const stale = !!rt.run && rt.runSql !== tab.sql;
  const errorPosition = rt.run?.error?.position;
  const errorMark = !stale && errorPosition ? wordAt(tab.sql, errorPosition.offset) : null;
  const statementMark = runPart.scope === "statement" && runPart.total > 1 && !runPart.problem ? { start: runPart.start, end: runPart.end } : null;
  const safeChecked = session && !session.ended && session.safe_updates !== null ? session.safe_updates : rt.safe ?? tabConnection.type !== "development";
  const txOpen = !!session && !session.ended && session.in_transaction !== false;
  let tx: [Tone, string, string] = ["off", "No session", "The first run opens this tab's session: one server connection kept until the tab closes."];
  if (session?.ended) tx = ["problem", "Session ended", session.message || ""];
  else if (session && session.in_transaction === true) tx = ["watch", "Transaction open", "Commit or roll it back; closing the tab asks first."];
  else if (session && session.in_transaction === null)
    tx = ["unknown", "Transaction unknown", session.transaction_note || "This server cannot tell whether a transaction is open."];
  else if (session) tx = ["ok", session.autocommit === false ? "Manual commit" : "Auto-commit", session.autocommit === false ? "autocommit is off: changes wait for Commit" : "Each statement commits on its own."];
  const meter = session?.transaction_meter;
  const transactionLabel = session?.in_transaction === true
    ? ["Transaction open", meter ? durationText(meter.age_seconds) : "",
       meter?.rows_modified != null ? `${BigInt(meter.rows_modified).toLocaleString()} rows changed` : "",
       meter?.blocking ? `blocking ${meter.blocking}${meter.blocking_truncated ? "+" : ""}` : ""].filter(Boolean).join(" · ")
    : tx[1];
  const runLabel = rt.run && !running ? rt.run : null;
  const resultIndex = tab.result_tab?.startsWith("result:") ? Number(tab.result_tab.slice(7)) : -1;
  const insightStatements = (rt.run?.statements || []).filter(s => resultIndex >= 0
    ? s.index === rowResults(rt.run)[resultIndex]?.statement
    : !["pending", "running", "not_run"].includes(s.status));
  // A tab may retain a session/results from another connection. Publish only
  // evidence belonging to the connection named by the shell's active scope.
  const statusKey = connectionKey(tabConnection);
  const statusSession = rt.sessionKey === statusKey ? session : null;
  const statusRun = rt.runKey === statusKey ? runLabel : null;
  // Edited since the last save (a tab opened from or saved to a file).
  const fileDirty = !!rt.file && rt.file.savedSql !== tab.sql;
  // The default database the picker shows: the live session's DATABASE(),
  // else the tab's choice (or the connection's) for the next session.
  const databaseShown = session && !session.ended ? session.database_name ?? "" : connectionFor(tab).database || "";
  const databaseNames = (schemaCatalog?.schemas || []).map((s) => s.name);
  const databaseOptions = databaseShown && !databaseNames.includes(databaseShown)
    ? [databaseShown, ...databaseNames]
    : databaseNames;
  const sessionText = statusSession
    ? statusSession.ended ? "Session ended"
      : `${running ? "Running" : statusSession.state === "starting" ? "Session starting" : "Session open"}${statusSession.connection_id !== null ? ` · connection ${statusSession.connection_id}` : ""}${statusSession.read_only_transactions ? " · read-only" : ""}`
    : rt.probed && !session ? "No session" : undefined;
  useWindowStatus(`query:${statusKey}:${tab.id}`, {
    connectionState: tabConnection.host ? connectionLabel(tabConnection) : "No connection",
    engineVersion: statusSession?.server_version ? `${statusSession.server_product || (statusSession.engine === "mariadb" ? "MariaDB" : statusSession.engine === "mysql" ? "MySQL" : "Server")} ${statusSession.server_version}` : undefined,
    sessionState: sessionText ? `${sessionText}${fileDirty ? " · Edited" : ""}` : fileDirty ? "Edited" : undefined,
    // The transaction state is in the editor toolbar, and the rows and time
    // of the run in its result tabs: the footer does not repeat them.
    lastRunAt: statusRun?.finished_at ? Date.parse(statusRun.finished_at) : undefined,
  });
  async function openAnalysis() {
    if (!rt.analysis || openingPlanRef.current) return;
    openingPlanRef.current = true;
    setOpeningPlan(true);
    try { await onCapture(rt.analysis.capture, rt.analysis.name); }
    catch (e) { setError((e as Error).message); }
    finally { openingPlanRef.current = false; setOpeningPlan(false); }
  }
  function exploreExplain(result: EditorRows) {
    if (!canReload(tab.id) || busy || !rt.explain) return;
    const statement = rt.run?.statements[result.statement];
    if (!statement) return;
    // The server kept the original statement alongside the prefixed SQL.
    // Capture's existing SELECT guards still apply to this optional view, and
    // its :name placeholders take the Parameters the Explain bound, not
    // values edited since.
    void startCapture(tab, statement.text, rt.explainParameters || {}, "explain", "the Explain result", true);
  }
  const indexApplied = !!rt.indexOrigin && !!rt.indexDdl && !rt.explain && indexWasApplied(rt.run, rt.indexDdl);
  const canMeasure = indexApplied && rt.indexOrigin?.target.key === connectionKey(tabConnection) && rt.runKey === connectionKey(tabConnection);
  function measureAgain() {
    if (!canMeasure || busy || rt.running || rt.operation || !rt.indexOrigin) return;
    openDigest(rt.indexOrigin.statement, 1, "analyze", rt.indexOrigin.target.key, rt.indexOrigin);
  }
  async function compareMeasurement() {
    if (!rt.analysis?.previous || !rt.analysis.measured || !onCompareCaptures || openingPlanRef.current) return;
    openingPlanRef.current = true; setOpeningPlan(true);
    try { await onCompareCaptures(rt.analysis.previous, rt.analysis); }
    catch (e) { setError((e as Error).message); }
    finally { openingPlanRef.current = false; setOpeningPlan(false); }
  }
  const analysisBusy = jobBusy && captureOwner?.mode === "analyze";
  const planPane = rt.analysis || (analysisBusy && lastRun?.tabId === tab.id) ? (
    <div className="plan-pane">
      {analysisBusy && lastRun?.tabId === tab.id ? (
        <p role="status" className="capture-status"><span className="spinner" /> Analyzing {job?.progress || 0} / {job?.repeat || repeat} runs…</p>
      ) : rt.analysis && <>
        {rt.analysis.measured && <MeasuredStatement before={rt.analysis.measured.before} capture={rt.analysis.capture}
          onOpenInstruments={onOpenInstruments ? () => onOpenInstruments("consumer:events_statements_history") : undefined} />}
        {rt.analysis.measured && rt.analysis.previous && onCompareCaptures && <ActionButton small data-action="compare-measurement"
          busy={openingPlan} busyLabel="Opening…" onClick={() => void compareMeasurement()}>Compare plans</ActionButton>}
        <p>Measured: the statement ran {rt.analysis.capture.measurements.count} time(s).
          {rt.analysis.capture.measurements.median_ms != null && ` Median: ${resultDuration(rt.analysis.capture.measurements.median_ms)}.`}</p>
        <ActionButton small busy={openingPlan} busyLabel="Opening…" onClick={() => void openAnalysis()}>Explore</ActionButton>
        <details className="sent-sql"><summary>SQL analyzed</summary><pre>{rt.analysis.capture.query}</pre></details>
      </>}
    </div>
  ) : null;
  const ctrl = (shift: boolean, key: "Enter" | "E") =>
    key === "E" ? (MAC ? `Meta+${shift ? "Shift+" : ""}E` : `Control+${shift ? "Shift+" : ""}E`) : `Control+${shift ? "Shift+" : ""}Enter Meta+${shift ? "Shift+" : ""}Enter`;
  return (
    <section className="query-lab" aria-labelledby="query-editor-title">
      <h1 id="query-editor-title" className="visually-hidden">Query editor</h1>
      {tableData && <TableData connection={tableData.connection} currentConnectionKey={connectionKey(tabConnection)}
        schema={tableData.schema} table={tableData.table} onClose={() => {
          setTableData(null);
          window.requestAnimationFrame(() => editor.current?.focus());
        }} /> }
      <div hidden={!!tableData} className={`editor-workbench${showBrowser ? " has-browser" : ""}${showHistory ? " has-history" : ""}${narrow ? " is-narrow" : ""}`}>
        {/* Kept mounted while hidden, so what was read stays when it opens again. */}
        <div ref={browserSheet} className="schema-browser-slot" hidden={!showBrowser}
          role={narrow && showBrowser ? "dialog" : undefined} aria-label="Schemas" tabIndex={-1}>
          <SchemaBrowser
            connection={tabConnection}
            visible={onScreen && showBrowser && !tableData && !groupMemberProblem(tab)}
            width={narrow ? 0 : layout.browser_width}
            onWidth={(w) => setLayout((l) => ({ ...l, browser_width: w }))}
            onClose={() => togglePane("browser")}
            onInsert={insertAtCursor}
            onQuery={appendAndRun}
            onOpenTable={onOpenTable}
            reveal={browserReveal}
            onOpenData={tableData ? undefined : (schema, table, from) => {
              if (groupMemberProblem(tab) || from !== connectionKey(tabConnection)) {
                setError("The connection changed. Refresh the schema browser before opening table data.");
                return;
              }
              // A second click cannot replace a draft before the grid's own
              // discard confirmation has called onClose.
              setTableData(current => current || { connection: { ...tabConnection }, schema, table });
            }}
            onImportCsv={(target, from) => {
              if (groupMemberProblem(tab) || from !== connectionKey(tabConnection)) {
                setError("The connection changed. Reload the schemas before importing.");
                return;
              }
              importCsv.start(target);
            }}
          />
        </div>
        <div className="editor-panel card">
          <div className="query-tabs">
                <div
                  className="query-tablist"
                  role="tablist"
                  aria-label="Query tabs"
                  ref={tabList}
                >
                  {tabs.map((t, i) => (
                    <div className="query-tab" key={t.id}>
                      {renaming?.id === t.id && (
                        <input className="query-tab-name" aria-label="Query name" value={renaming.name}
                          maxLength={MAX_TAB_NAME} spellCheck={false} autoComplete="off"
                          aria-invalid={renaming.invalid || undefined}
                          title={`Up to ${MAX_TAB_NAME} characters. Enter saves, Escape cancels.`}
                          autoFocus onFocus={(e) => e.currentTarget.select()}
                          onChange={(e) => setRenaming({ id: t.id, name: e.target.value, invalid: renaming.invalid && !e.target.value.trim() })}
                          onBlur={() => finishRename(true, false)}
                          onKeyDown={(e) => {
                            if (e.nativeEvent.isComposing) return;
                            if (e.key === "Enter" || e.key === "Escape") {
                              e.preventDefault();
                              e.stopPropagation();
                              finishRename(e.key === "Enter");
                            }
                          }} />
                      )}
                      <button
                        role="tab"
                        id={`query-tab-${i}`}
                        aria-selected={t.id === tab.id}
                        aria-controls="query-tab-panel"
                        tabIndex={t.id === tab.id ? 0 : -1}
                        hidden={renaming?.id === t.id}
                        data-tab-id={t.id}
                        aria-haspopup="menu"
                        title={`${t.name}\nDouble-click or F2 to rename; right-click for more`}
                        onDoubleClick={() => setRenaming({ id: t.id, name: t.name })}
                        onClick={() => setTabId(t.id)}
                        onContextMenu={(e) => {
                          e.preventDefault();
                          setTabMenu({ id: t.id, x: e.clientX, y: e.clientY, opener: e.currentTarget });
                        }}
                        // Middle-click closes, like browser and editor tabs.
                        onMouseDown={(e) => { if (e.button === 1) e.preventDefault(); }}
                        onAuxClick={(e) => {
                          if (e.button !== 1 || tabs.length < 2) return;
                          e.preventDefault();
                          void requestClose(t.id);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "F2") {
                            e.preventDefault();
                            setRenaming({ id: t.id, name: t.name });
                            return;
                          }
                          // The tab's menu from the keyboard, under the tab.
                          if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
                            e.preventDefault();
                            const box = e.currentTarget.getBoundingClientRect();
                            setTabMenu({ id: t.id, x: box.left, y: box.bottom + 2, opener: e.currentTarget });
                            return;
                          }
                          // Roving focus: arrows move between tabs (and open them), Home and End jump to the ends.
                          const next =
                            e.key === "ArrowRight"
                              ? (i + 1) % tabs.length
                              : e.key === "ArrowLeft"
                                ? (i - 1 + tabs.length) % tabs.length
                                : e.key === "Home"
                                  ? 0
                                  : e.key === "End"
                                    ? tabs.length - 1
                                    : -1;
                          if (next < 0) return;
                          e.preventDefault();
                          setTabId(tabs[next].id);
                          tabList.current
                            ?.querySelector<HTMLElement>(`#query-tab-${next}`)
                            ?.focus();
                        }}
                      >
                        <span className="query-tab-label">
                          {t.name}
                          {t.member && (
                            <span className={`query-tab-member${groupMemberProblem(t) ? " is-gone" : ""}`}>
                              {" · "}{memberNameOf(t.member) || t.member_name || t.member}
                            </span>
                          )}
                        </span>
                        {runtime[t.id]?.file && runtime[t.id]!.file!.savedSql !== t.sql && (
                          <span aria-hidden="true" title="Edited since the last save">●</span>
                        )}
                        {runtime[t.id]?.session?.in_transaction === true && !runtime[t.id]?.session?.ended && (
                          <span className="tab-transaction" aria-label="Transaction open" title="Transaction open">TX</span>
                        )}
                      </button>
                      {renaming?.id !== t.id && (
                        <button type="button" className="query-tab-close" aria-label={`Close ${t.name}`} title="Close tab"
                          tabIndex={t.id === tab.id ? 0 : -1} onClick={() => void requestClose(t.id)}>
                          <Icon name="close" size={12} />
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                <button
                  className="query-tab-add"
                  aria-label="New query tab"
                  title="New query tab"
                  onClick={() => addTab()}
                >
                  +
                </button>
                {tabMenu && tabs.some((t) => t.id === tabMenu.id) && (
                  <QueryTabMenu
                    x={tabMenu.x}
                    y={tabMenu.y}
                    opener={tabMenu.opener}
                    name={tabs.find((t) => t.id === tabMenu.id)?.name || ""}
                    single={tabs.length < 2}
                    onDismiss={closeTabMenu}
                    onRename={() => {
                      setTabMenu(null);
                      startRename(tabMenu.id);
                    }}
                    onDuplicate={() => {
                      setTabMenu(null);
                      duplicateTab(tabMenu.id);
                    }}
                    onSave={() => {
                      setTabMenu(null);
                      startSaveQuery(tabMenu.id);
                    }}
                    onOpenFile={() => {
                      setTabMenu(null);
                      void openSqlFile();
                    }}
                    onClose={() => {
                      const id = tabMenu.id;
                      setTabMenu(null);
                      void requestClose(id).then((result) => {
                        if (result === "closed") window.setTimeout(() => tabList.current?.querySelector<HTMLElement>("[role=tab][aria-selected=true]")?.focus());
                      });
                    }}
                    onCloseOthers={() => {
                      setTabMenu(null);
                      void closeOthers(tabMenu.id);
                    }}
                  />
                )}
            <div className="editor-connection">
              {picker}
              {group && onGroupMember && (
                <GroupServerPicker group={group} value={groupMember} narrow={narrow} onChoose={onGroupMember} />
              )}
            </div>
          </div>
          <div
            ref={tabPanel}
            role="tabpanel" tabIndex={0}
            id="query-tab-panel"
            className="query-tab-panel"
            aria-labelledby={`query-tab-${Math.max(0, tabs.indexOf(tab))}`}
            onKeyDown={(e) => {
              // Escape that ends an input method's composition is not Stop,
              // and neither is one the editor consumed (closing suggestions
              // or collapsing extra cursors).
              if (e.key === "Escape" && !e.defaultPrevented && !e.nativeEvent.isComposing && e.keyCode !== 229 && rt.running && !pending && !closing) {
                e.preventDefault();
                void stop();
              }
              // Format SQL: Shift+Option+F (the key, whatever character Option makes).
              if (e.code === "KeyF" && e.shiftKey && e.altKey && !e.metaKey && !e.ctrlKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void formatSql();
              }
              // Open SQL file (⌘O) and Save / Save as (⌘S, ⇧⌘S).
              const fileMod = (MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) && !e.altKey && !e.nativeEvent.isComposing;
              if (fileMod && e.key.toLowerCase() === "o") {
                e.preventDefault();
                void openSqlFile();
              }
              if (fileMod && e.key.toLowerCase() === "s") {
                e.preventDefault();
                void saveTab(tab.id, e.shiftKey);
              }
            }}
          >
            {groupMemberProblem(tab) && (
              <p className="group-member-note" role="note">
                <Icon name="warning" size={14} />
                <span>{groupMemberProblem(tab)}</span>
                {group ? <Select
                  label="Server for this tab"
                  className="group-member-rebind"
                  value=""
                  options={[["", "Pick a server…"] as [string, string], ...group.members.map((m): [string, string] => [m.profile_id, m.profile_name])]}
                  onChange={(id: string) => { if (id) rebindMember(tab.id, id); }}
                /> : <button type="button" className="text-button" onClick={() => {
                  changeTab({ member: undefined, member_name: undefined,
                    ...(tab.source ? { target: { key: connectionKey(connection), label: connectionLabel(connection) } } : {}) });
                  setError("");
                }}>Use this connection</button>}
              </p>
            )}
            <div className="editor-toolbar" role="toolbar" aria-label="Run and settings">
              <div className="toolbar-group run-actions">
                <EditorSplitButton
                  label="Run options"
                  open={runOptionsOpen}
                  onOpen={setRunOptionsOpen}
                  action={
                    <ActionButton
                      icon="run"
                      primary={!runGate.disabledReason && !mismatch}
                      className="privilege-action"
                      data-action="run"
                      busy={runBusy} busyLabel="Running…"
                  disabled={!runBusy && (busy || running || sessionBusy || mismatch)}
                      data-privilege-state={runGate.state}
                      aria-keyshortcuts={ctrl(false, "Enter")}
                      {...privilegeButtonProps(runGate, () => runSql(false), mismatchReason, privilegeNoticeId || `${privilegeId}-run`)}
                      aria-disabled={runBusy || !!mismatchReason || !!runGate.disabledReason ? "true" : "false"}
                    >
                      {selecting ? "Run selection" : "Run"}
                    </ActionButton>
                  }
                >
                  <div className="toolbar-group run-settings">
                    <label className="inline-field" title="Rows kept per result set; the statement is never rewritten">
                      Rows
                      <NumberField
                        label="Row limit"
                        min={1}
                        max={MAX_ROW_LIMIT}
                        value={tab.limit ?? DEFAULT_ROW_LIMIT}
                        onChange={(v) => {
                          const n = Math.round(Number(v));
                          if (Number.isFinite(n) && n >= 1) changeTab({ limit: Math.min(MAX_ROW_LIMIT, n) === DEFAULT_ROW_LIMIT ? undefined : Math.min(MAX_ROW_LIMIT, n) });
                        }}
                      />
                    </label>
                    <label className="inline-field" title="Stop a run after this many seconds with KILL QUERY; 0 for no limit">
                      Time limit
                      <NumberField
                        label="Run time limit in seconds, 0 for none"
                        min={0}
                        max={86400}
                        unit="s"
                        value={layout.run_timeout}
                        onChange={(v) => {
                          const n = Math.round(Number(v));
                          if (Number.isFinite(n) && n >= 0) setLayout((l) => ({ ...l, run_timeout: Math.min(86400, n) }));
                        }}
                      />
                    </label>
                    <div className="safe-updates-row">
                      <Checkbox
                        className="safe-updates"
                        checked={!!safeChecked}
                        disabled={running}
                        onChange={setSafeUpdates}
                        title="sql_safe_updates for this tab's session"
                      >
                        Safe updates
                      </Checkbox>
                      <SafeUpdatesInfo />
                    </div>
                    <div className="safe-updates-row">
                      <Segmented
                        label="On error"
                        value={tab.on_error ?? "stop"}
                        options={[
                          { value: "stop" as const, label: "Stop", title: "Run all stops at the first statement that fails" },
                          { value: "continue" as const, label: "Continue", title: "Run all runs every statement and counts the failures" },
                        ]}
                        onChange={(v) => changeTab({ on_error: v === "continue" ? "continue" : undefined })}
                      />
                      <InfoPopover label="On error">
                        <p>
                          Stop ends Run all at the first statement that fails. Continue runs every
                          statement; Messages lists each failure with its statement number, and the
                          summary counts what succeeded and what failed.
                        </p>
                        <p>
                          By default, an InnoDB error rolls back only the failed statement. A deadlock,
                          or a lock wait timeout with innodb_rollback_on_timeout ON, rolls back the
                          whole transaction.
                        </p>
                      </InfoPopover>
                    </div>
                  </div>
                </EditorSplitButton>
                <ActionButton
                  icon="runAll"
                  data-action="run-all"
                  className="privilege-action"
                  busy={runBusy} busyLabel="Running…"
                      disabled={!runBusy && (busy || running || sessionBusy || mismatch)}
                  data-privilege-state={allGate.state}
                  aria-keyshortcuts={ctrl(true, "Enter")}
                  {...privilegeButtonProps(allGate, () => runSql(true), mismatchReason, privilegeNoticeId || `${privilegeId}-run-all`)}
                  aria-disabled={runBusy || !!mismatchReason || !!allGate.disabledReason ? "true" : "false"}
                >
                  Run all
                </ActionButton>
                <ActionButton
                  icon="stop"
                  data-action="stop"
                  busy={rt.stopping} busyLabel="Stopping…"
                  disabled={!rt.stopping && (!running || rt.stopGen === rt.gen)}
                  onClick={() => void stop()}
                  title="Stop the running statement with KILL QUERY (Escape); an open transaction stays open"
                >
                  Stop
                </ActionButton>
              </div>
              <div className="toolbar-group editor-database">
                <label className="inline-field"
                  title="The default database of this tab's session: choosing one sends USE in it, and a new session starts in it">
                  Database
                  <select
                    aria-label="Default database"
                    data-action="database"
                    value={databaseShown}
                    disabled={running || busy || rt.fileBusy || !!rt.operation}
                    onFocus={() => { if (!groupMemberProblem(tab)) reloadStaleSchemaCatalog(CATALOG_STALE_MS); }}
                    onChange={(e) => void chooseDatabase(e.target.value)}
                  >
                    <option value="">None</option>
                    {databaseOptions.map((name) => (
                      <option key={name} value={name}>{name}</option>
                    ))}
                  </select>
                </label>
              </div>
              {!privilegeNotice && <>
                <span id={`${privilegeId}-run`} className="account-sr">{runGate.reason}</span>
                <span id={`${privilegeId}-run-all`} className="account-sr">{allGate.reason}</span>
                <span id={`${privilegeId}-analysis`} className="account-sr">{analysisGate.reason}</span>
              </>}
              <div className="editor-actions toolbar-group">
                <ActionButton
                  data-action="explain" busy={running && rt.explaining} busyLabel="Explaining…"
                  disabled={busy || mismatch || !!rt.operation || rt.stopping}
                  title={mismatchReason || `Explain ${scopePhrase} (${keys(false, "E")})`}
                  aria-keyshortcuts={ctrl(false, "E")}
                  onClick={() => void capture("explain")}
                  aria-disabled={running || busy || !!mismatchReason ? "true" : "false"}
                >
                  {selecting ? "Explain selection" : "Explain"}
                </ActionButton>
                <select aria-label="Explain format" value={effectiveExplainFormat}
                  title="Explain output format; Tree needs MySQL 8.0.16 or later"
                  disabled={running || busy} onChange={e => {
                    const format = e.target.value as ExplainFormat;
                    void setUiPreference("explain_format", format).catch(() => {});
                  }}>
                  <option value="table">Table</option>
                  <option value="tree" disabled={!treeAvailable}>Tree</option>
                  <option value="json">JSON</option>
                </select>
                <EditorSplitButton
                  label="Run analysis options"
                  open={showAnalysisOptions}
                  onOpen={toggleAnalysisOptions}
                  action={
                    <ActionButton
                      data-action="analyze" busy={analysisBusy} busyLabel="Analyzing…"
                      disabled={!analysisBusy && (busy || mismatch || running || !!rt.operation)}
                      title={`Run analysis on ${scopePhrase} (${keys(true, "E")}): it executes the SELECT and captures measured timings`}
                      aria-keyshortcuts={ctrl(true, "E")}
                      data-privilege-state={analysisGate.state}
                      {...privilegeButtonProps(analysisGate, () => void capture("analyze"), mismatchReason, `${privilegeNoticeId || `${privilegeId}-analysis`}${scopeLabel ? " run-scope" : ""}`)}
                      aria-disabled={jobBusy || !!mismatchReason || !!analysisGate.disabledReason ? "true" : "false"}
                    >
                      {selecting ? "Run analysis on selection" : "Run analysis"}
                    </ActionButton>
                  }
                >
                  <div className="execution-options plan-options" aria-label="Run analysis settings" role="group">
                    <span className="plan-options-label">Run analysis</span>
                    <p className="hint">Executes the SELECT {repeat} time(s) in a read-only transaction; each run is limited to {timeout} s.</p>
                    <label className="field inline-field">
                      Time limit per run
                      <NumberField
                        min={1}
                        max={300}
                        unit="s"
                        value={timeout}
                        onChange={(v) => setTimeoutSeconds(+v)}
                      />
                    </label>
                    <label className="field inline-field">
                      Analysis runs
                      <NumberField
                        min={1}
                        max={10}
                        value={repeat}
                        onChange={(v) => setRepeat(+v)}
                      />
                    </label>
                    <Checkbox className="check-field" checked={trace} onChange={setTrace}>
                      Capture optimizer trace
                    </Checkbox>
                    <details
                      className="editor-parameters"
                      open={parametersOpen}
                      onToggle={(e) => setParametersOpen(e.currentTarget.open)}
                    >
                      <summary>Parameters and execution conditions</summary>
                      <label className="field">
                        Named parameters · JSON object
                        <textarea
                          ref={parametersField}
                          aria-label="Query parameters"
                          value={tab.parameters}
                          onChange={(e) => changeTab({ parameters: e.target.value })}
                        />
                      </label>
                      <p className="hint">
                        Explain and Run analysis substitute :name values, for example WHERE id = :id with {`{"id": 42}`}. Run sends SQL as written.
                      </p>
                      <label className="field">
                        Conditions
                        <input
                          placeholder="Warm cache, local dataset, new index…"
                          value={conditions}
                          onChange={(e) => setConditions(e.target.value)}
                        />
                      </label>
                    </details>
                  </div>
                </EditorSplitButton>
                {jobBusy && (
                  <button className="button danger" onClick={() => void cancelCapture()}>
                    Cancel capture
                  </button>
                )}
              </div>
              <div className="toolbar-group format-tools">
                <IconButton
                  icon="search"
                  label="Find"
                  title={`Find (${FIND_KEYS})`}
                  aria-keyshortcuts={editorActionShortcuts("find").map(editorAriaKeys).join(" ")}
                  data-action="find"
                  onClick={() => editor.current?.openFind("find")}
                />
                <IconButton
                  icon="replace"
                  label="Find and replace"
                  title={`Find and replace (${REPLACE_KEYS})`}
                  aria-keyshortcuts={editorActionShortcuts("replace").map(editorAriaKeys).join(" ")}
                  data-action="replace"
                  onClick={() => editor.current?.openFind("replace")}
                />
                <IconButton
                  icon="format"
                  label="Format SQL"
                  title={`Format SQL: ${selecting ? "the selection" : "the whole tab"} (${FORMAT_KEYS}). Changes only spaces, line breaks and keyword case; nothing runs.`}
                  aria-keyshortcuts="Shift+Alt+F"
                  aria-busy={formatting || undefined}
                  aria-disabled={formatting || undefined}
                  className={formatting ? "is-busy" : undefined}
                  data-action="format"
                  onClick={() => void formatSql()}
                />
                <IconButton
                  icon="more"
                  label="Editing actions"
                  aria-haspopup="menu"
                  aria-expanded={!!editingMenu}
                  data-action="editing-actions"
                  onPointerDown={event => {
                    if (editingMenu) {
                      // Keep menu focus until the click toggles it closed.
                      event.preventDefault();
                      event.stopPropagation();
                    }
                  }}
                  onClick={event => {
                    const opener = event.currentTarget, rect = opener.getBoundingClientRect();
                    setEditingMenu(previous => previous ? null : { x: rect.left, y: rect.bottom + 4, opener });
                  }}
                />
                {editingMenu && <ContextMenu {...editingMenu} portal closeOnBlur label="Editing actions" className="editor-editing-menu"
                  onClose={() => setEditingMenu(null)} actions={EDITOR_ACTIONS.map(row => ({
                    label: row.label,
                    shortcut: editorActionShortcuts(row.id).map(editorKeyText).join(", "),
                    disabled: !!row.setting && !editorSettings[row.setting],
                    hint: row.setting && !editorSettings[row.setting]
                      ? `Turn on ${EDITOR_SETTING_LABELS[row.setting]} in Settings > Query editor` : undefined,
                    onClick: () => { editor.current?.runCommand(row.id); },
                  }))} />}
              </div>
              <div className="toolbar-group pane-toggles">
                <IconButton
                  icon="browser"
                  label={showBrowser ? "Hide the schema browser" : "Show the schema browser"}
                  aria-pressed={showBrowser}
                  onClick={event => togglePane("browser", event.currentTarget)}
                />
                <IconButton
                  icon="history"
                  label={showHistory ? "Hide the run history" : "Show the run history"}
                  aria-pressed={showHistory}
                  onClick={event => togglePane("history", event.currentTarget)}
                />
                <InfoPopover label="Query editor shortcuts">
                  <p>
                    {keys(false)} runs the selection or the statement at the cursor; {keys(true)} runs every
                    statement. {keys(false, "E")} explains; {keys(true, "E")} runs analysis. <kbd>Esc</kbd> stops a run.
                    {" "}{FORMAT_KEYS} formats the selection or the whole tab.
                  </p>
                  <ul className="editor-shortcut-list">
                    {EDITOR_SHORTCUTS.filter((row) => !row.setting || editorSettings[row.setting]).map((row) => {
                      const name = MAC ? row.mac : row.other;
                      return name ? <li key={row.id}><kbd>{editorKeyText(name)}</kbd> {row.label}</li> : null;
                    })}
                  </ul>
                  <p>Right-click a tab, or press Shift+F10 on it, to rename, duplicate, save or close it.</p>
                  <p>
                    In a result, click a cell; Shift+click or Shift+arrows select a range, a row number selects its row,
                    and {MAC ? "Option" : "Alt"}+click on a column header selects the column. The line under the result
                    totals the selection; {MAC ? "⌘C" : "Ctrl+C"} copies it. Double-click a row, or press {MAC ? "Return" : "Enter"} on it, to show it.
                  </p>
                  <p>Each tab keeps one session, so SET SESSION, temporary tables and transactions last until the tab closes.</p>
                </InfoPopover>
              </div>
              {scopeLabel && (
                <span id="run-scope" className={`run-scope${lit ? " is-lit" : ""}`} title={scopeLabel}>
                  {scopeLabel}
                </span>
              )}
              {/* The window status bar carries connection, session, last run and
                  rows; the editor keeps only this tab's transaction state. */}
              {session && !session.ended && (
                <span className="tx-state" title={tx[2]} data-state={tx[1]}>
                  {txOpen ? <Popover label={transactionLabel} className="editor-transaction-popover"
                    buttonClassName="button small ghost editor-transaction-trigger"
                    buttonTitle={transactionLabel}
                    trigger={<StatusPill tone={session.transaction_meter?.blocking ? "watch" : session.in_transaction === true ? "info" : tx[0]} label={transactionLabel} />}>
                    <p><bdi>{session.transaction_meter?.note || tx[2]}</bdi></p>
                    {session.transaction_meter?.age_source === "observed" && <p>Age is a lower bound from the first observation in this tab.</p>}
                    {session.transaction_meter?.rows_locked != null && <p>
                      {BigInt(session.transaction_meter.rows_locked).toLocaleString()} rows locked · {BigInt(session.transaction_meter.lock_structs || "0").toLocaleString()} lock structures
                    </p>}
                    <p>Blocking counts waiting InnoDB transactions (row and table locks), up to 500; metadata-lock waits are in Server &gt; Locks.</p>
                    <div className="editor-transaction-actions">
                      <ActionButton small data-action="commit" busy={rt.operation === "commit"} busyLabel="Committing…"
                        disabled={rt.operation !== "commit" && (running || sessionBusy || transactionUnknown)} onClick={() => commit()}>Commit</ActionButton>
                      <ActionButton small data-action="rollback" busy={rt.operation === "rollback"} busyLabel="Rolling back…"
                        disabled={rt.operation !== "rollback" && (running || sessionBusy || transactionUnknown)} onClick={() => rollback()}>Roll back</ActionButton>
                      {onOpenLocks && <button className="button small" disabled={mismatch} onClick={onOpenLocks}>Show who waits</button>}
                    </div>
                    <ActionStatus state={rt.sessionFeedback} />
                  </Popover> : <StatusPill
                    tone={tx[1] === "Auto-commit" && session.autocommit === null ? "unknown" : tx[0]}
                    label={tx[1] === "Auto-commit" && session.autocommit === null ? "Autocommit unknown" : tx[1]}
                  />}
                </span>
              )}
              {/* In the toolbar row, so a late preview never pushes the editor down. */}
              {privilegeNotice && <p id={privilegeNoticeId} title={privilegeNotice}
                className={`editor-privilege-note${previewWarning || privilegeReasons.length ? " is-warning" : ""}`}
                role="status">{privilegeNotice}</p>}
            </div>
            <ActionStatus className="editor-stop-status" state={rt.stopFeedback}
              slowAfter={10_000} slowText="No reply to Stop yet; the statement may still be running." />
            {tab.source && (
              <p className="hint tab-source">
                From Top statements, digest{" "}
                <code title={tab.source.digest}>
                  {shortDigest(tab.source.digest)}
                </code>
                {tabSchemaNote}
                {tab.target && !unbound && ` · on ${tab.target.label}`}
              </p>
            )}
            {unbound && (
              <div className="notice tab-target" role="status">
                <span>{unbound}</span>
                <button className="text-button" onClick={bindToConnection}>
                  Use this connection
                </button>
              </div>
            )}
            {pendingFill.length > 0 && (
              <div className="notice fill-notice" role="status">
                <span>
                  {tab.fill_note ||
                    `Fill in ${listNames(pendingFill)} under Parameters with real values, then run Explain.`}
                </span>
                <button type="button" className="text-button" data-action="fill-values" onClick={() => setFillFormTab(tab.id)}>
                  Fill in values…
                </button>
              </div>
            )}
            {mismatch && session && (
              <div className="editor-banner is-warn" data-banner="mismatch">
                <span>
                  This tab is still connected to {sessionName(session)}. Close its session to run on {connectionShortName(tabConnection)}.
                </span>
                <ActionButton small primary busy={rt.operation === "close"} busyLabel="Closing…"
                  disabled={rt.operation !== "close" && (running || sessionBusy)} onClick={() => void requestClose(tab.id, true)}>
                  Close session
                </ActionButton>
              </div>
            )}
            {session?.ended && (
              <div className="editor-banner is-danger" data-banner="ended">
                <span>
                  Session ended{session.ended_reason ? `: ${ENDED[session.ended_reason] || session.ended_reason}` : ""}. Session settings
                  were lost and any open transaction was rolled back.
                </span>
                <ActionButton small busy={rt.operation === "open"} busyLabel="Reconnecting…"
                  disabled={rt.operation !== "open" && (running || sessionBusy)} onClick={() => reconnect()}>
                  Reconnect
                </ActionButton>
              </div>
            )}
            {rt.sessionFeedback.phase === "failed" && (rt.sessionFeedback.code === "client-missing" || rt.sessionFeedback.code === "client-auth-plugin") ? (
              <ClientMissingNotice className="editor-session-status" text={rt.sessionFeedback.code === "client-auth-plugin" ? rt.sessionFeedback.message : ""} onRetry={() => reconnect()} />
            ) : (
              <ActionStatus className="editor-session-status" state={rt.sessionFeedback} />
            )}
            {importCsv.element}
            {batchRunning && (
              <p className="hint">
                Explain and Run analysis wait while Explain top N runs on Server.
              </p>
            )}
            {analysisWarning?.tab === tab.id && (
              <Notice variant="note" role="alert">{analysisWarning.text}</Notice>
            )}
            {status && (
              <Notice variant="note">{status}</Notice>
            )}
            {runClientMissing && runClientMissing.tabId === tab.id && runClientMissing.connection === connectionKey(tabConnection) ? (
              <ClientMissingNotice text={runClientMissing.text} onRetry={() => {
                const retry = runClientMissing.retry;
                setRunClientMissing(null);
                // The tab still uses the connection the run was for (checked above).
                retry();
              }} />
            ) : error && (
              <Notice variant="error" noticeKey={error}>
                {error}
                {sessionFallback && sessionFallback.tabId === tab.id && (
                  <>
                    {" "}
                    <button
                      type="button"
                      className="notice-action"
                      onClick={() => {
                        const fb = sessionFallback;
                        setSessionFallback(null);
                        void startCapture(tab, fb.sql, fb.parameters, fb.mode, fb.label, true);
                      }}
                    >
                      Run in this tab's session
                    </button>
                    {" — its temporary tables are visible there."}
                  </>
                )}
              </Notice>
            )}
            <SqlEditor
              ref={editor}
              docId={tab.id}
              docIds={docIds}
              value={tab.sql}
              completionConnection={connectionFor(tab)}
              engine={engine === "mysql" || engine === "mariadb" ? engine : undefined}
              serverVersion={version}
              sessionTempTables={rt.sessionTempTables}
              backslashEscapes={rt.session?.backslash_escapes ?? true}
              onChange={(sql, sel) => {
                // The editor reports an edit in the same task, not through a
                // React event: hand the tabs over now, so the beforeunload
                // check (main.tsx's tabsNow ref) sees the edit even when the
                // render this queues has not run yet.
                tabsNowRef.current?.(tabsRef.current.map((t) => (t.id === tabRef.current ? { ...t, sql } : t)));
                changeTab({ sql });
                setCursor((c) => (c.start === sel.start && c.end === sel.end ? c : { start: sel.start, end: sel.end }));
              }}
              onSelectionChange={(sel, count) => {
                setCursor((c) => (c.start === sel.start && c.end === sel.end ? c : { start: sel.start, end: sel.end }));
                setCursorCount(count);
              }}
              onFocus={() => {
                touched.current = true;
              }}
              initialSelection={tab.selection}
              statement={statementMark}
              flash={lit}
              error={errorMark}
              height={editorHeight}
              describedBy={scopeLabel ? "run-scope" : undefined}
              placeholder="Write SQL. Run sends the statement at the cursor."
              settings={editorSettings}
              onKeyDown={(e) => {
                if (e.isComposing) return;
                const enter = (e.metaKey || e.ctrlKey) && !e.altKey && e.key === "Enter";
                const explain = (MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey) && !e.altKey && e.key.toLowerCase() === "e";
                if (enter) {
                  e.preventDefault();
                  if (!rt.running) runSql(e.shiftKey);
                } else if (explain) {
                  e.preventDefault();
                  if (!busy) void capture(e.shiftKey ? "analyze" : "explain");
                }
              }}
            />
            {ddlEligible && <div className="ddl-editor" aria-label="DDL progress">
              <div className="action-status">
                <span className="action-status-text" role="status" aria-live="polite">
                  {ddlItem ? `${ddlItem.stage_label}. ${ddlLeft(ddlItem)}` : ""}
                </span>
                {ddlItem && <DdlMeter item={ddlItem} />}
              </div>
              {ddlShown && <DdlUnavailable progress={ddlShown}
                onOpenInstruments={rt.sessionKey === connectionKey(tabConnection) ? onOpenInstruments : undefined} />}
            </div>}
            <div
              className="editor-splitter"
              role="separator"
              aria-orientation="horizontal"
              aria-label="Height of the SQL editor"
              aria-valuemin={Math.min(120, editorHeight)}
              aria-valuemax={Math.max(120, Math.min(2000, editorCap))}
              aria-valuenow={editorHeight}
              tabIndex={0}
              title="Drag to resize the editor and the results"
              onPointerDown={(e) => {
                e.preventDefault();
                const from = editorHeight;
                const y = e.clientY;
                const handle = e.currentTarget;
                handle.setPointerCapture?.(e.pointerId);
                const move = (m: PointerEvent) => setHeight(from + m.clientY - y);
                const up = () => {
                  handle.removeEventListener("pointermove", move);
                  handle.removeEventListener("pointerup", up);
                  handle.removeEventListener("pointercancel", up);
                  handle.removeEventListener("lostpointercapture", up);
                };
                handle.addEventListener("pointermove", move);
                handle.addEventListener("pointerup", up);
                handle.addEventListener("pointercancel", up);
                handle.addEventListener("lostpointercapture", up);
              }}
              onKeyDown={(e) => {
                const step = e.shiftKey ? 64 : 16;
                if (e.key === "ArrowDown") setHeight(editorHeight + step);
                else if (e.key === "ArrowUp") setHeight(editorHeight - step);
                else return;
                e.preventDefault();
              }}
            />
            {rt.indexOrigin && <div className="editor-insights" aria-label="Suggested index measurement">
              <div className="editor-insight"><DigestBefore before={rt.indexOrigin.before} />
                {indexApplied && <ActionButton small data-action="measure-again" busy={analysisBusy} busyLabel="Analyzing…"
                  disabled={!canMeasure || busy || running || !!rt.operation || rt.stopping || !!analysisGate.disabledReason}
                  title={!canMeasure ? "Switch back to the connection where the index was created." : analysisGate.disabledReason || undefined}
                  onClick={measureAgain}>Measure the statement again</ActionButton>}
              </div>
            </div>}
            {!running && rt.run?.kind === "run" && tab.result_tab !== "plan" && <div className="editor-insights" aria-label="Statement server stats">
              {insightStatements.map(insightStatement =>
              <StatementInsight key={insightStatement.index} statement={insightStatement} numbered={insightStatements.length > 1}
                disabled={busy || !canReload(tab.id) || !!analysisGate.disabledReason || !!rt.operation || rt.stopping}
                busy={analysisBusy}
                onOpenInstruments={onOpenInstruments && rt.runKey === connectionKey(tabConnection)
                  ? () => onOpenInstruments(insightStatement.server_stats?.available === false &&
                    insightStatement.server_stats.reason.startsWith("Performance Schema is off") ? "ps" : "consumer:events_statements_history") : undefined}
                onAnalyze={() => {
                  setError(""); setStatus("");
                  setAnalysisReview({ tab: { ...tab }, sql: rt.explain ? insightStatement.text : insightStatement.sent || insightStatement.text,
                    parameters: rt.explain ? rt.explainParameters || {} : {}, label: `statement ${insightStatement.index + 1}`, binding: analysisBinding });
                }} />)}</div>}
            <ResultsPane
              rowPanel={{ narrow: narrow && onScreen && !tableData, blocked: narrow && sheet !== "", onOpen: () => setSheet("") }}
              run={rt.run}
              running={running}
              base={0}
              stale={stale}
              fromConnection={
                rt.run && rt.runKey && rt.runKey !== connectionKey(tabConnection)
                  ? rt.runConnectionName || (session ? sessionName(session) : "") || undefined
                  : undefined
              }
              selected={tab.result_tab || ""}
              onSelect={(key) => changeTab({ result_tab: key })}
              engine={session?.engine || null}
              backslash={rt.runBackslash}
              columnTypes={columnTypesFor}
              plan={planPane}
              explain={rt.explain}
              explaining={rt.explaining}
              onExplore={exploreExplain}
              exploreBusy={jobBusy && captureOwner?.mode === "explain" && captureOwner.tabId === tab.id}
              exploreDisabled={busy || !canReload(tab.id)}
              onJump={jump}
              onLoadMore={loadMore}
              canLoadMore={(r) => rt.run?.statements[r.statement]?.kind === "read" && canReload(tab.id)}
              binary={rt.binary}
              onBinary={(result, column, on) =>
                patchRuntime(tab.id, (r) => {
                  const cols = new Set(r.binary[result] || []);
                  if (on) cols.add(column);
                  else cols.delete(column);
                  return { binary: { ...r.binary, [result]: [...cols].sort((a, b) => a - b) } };
                })
              }
              reloaded={rt.reloaded}
              onNote={(text, isError) => note(tab.id, text, isError)}
              empty={
                <EmptyState icon="run" title="No results yet">
                  {keys(false)} runs the statement at the cursor; {keys(true)} runs them all.
                </EmptyState>
              }
            />
            {/* Operation feedback is unique to the editor and remains visible
                when the compact layout uses only the window's status bar. */}
            <p className={`editor-feedback statusbar-note${rt.noteError ? " is-error" : ""}`}
              role="status" hidden={!rt.note}>{rt.note}</p>
          </div>
        </div>
        {showHistory && <div ref={historySheet} className="history-panel-slot"
          role={narrow ? "dialog" : undefined} aria-label="History and saved queries" tabIndex={-1}>
          <HistoryPanel refresh={historyTick} savedRefresh={savedTick} tab={sideTab} onTab={setSideTab}
            onInsert={insertAtCursor} onOpenSaved={openSaved} onClose={() => togglePane("history")} />
        </div>}
      </div>
      {analysisReview && analysisReview.binding === analysisBinding && <ConfirmSheet
        title={`Run analysis on ${connectionLabel(tabConnection)}?`}
        description={/^\s*(?:\(|SELECT\b|WITH\b|TABLE\b)/i.test(analysisReview.sql.replace(/^(?:\s+|\/\*[\s\S]*?\*\/|(?:--\s|#)[^\n]*(?:\n|$))*/, ""))
          ? `Run analysis runs the statement ${runtimeRef.current[analysisReview.tab.id]?.measureBefore ? 1 : repeat} time(s) in a read-only transaction, with a ${timeout} s time limit per run.`
          : "This is not a SELECT, so Run analysis does not run it. It prepares a SELECT that reads the same rows, when the statement allows one, in a new tab for you to review first. Nothing runs now."}
        confirmLabel="Run analysis" confirmTone="primary" busy={starting}
        onCancel={() => setAnalysisReview(null)} onConfirm={() => {
          if (analysisReview.binding !== analysisBinding) return;
          const review = analysisReview;
          setAnalysisReview(null);
          void startCapture(review.tab, review.sql, review.parameters, "analyze", review.label);
        }}><pre className="confirm-sql">{analysisReview.sql}</pre></ConfirmSheet>}
      {saveFor && (() => {
        const t = tabs.find((x) => x.id === saveFor);
        if (!t) return null;
        return (
          <SaveQuerySheet
            key={t.id}
            name={t.name}
            sql={t.sql}
            database={connectionFor(t).database || null}
            onClose={() => setSaveFor(null)}
            onSaved={(saved, redacted) => {
              setSaveFor(null);
              setSavedTick((n) => n + 1);
              note(t.id, `Saved “${saved.name}”${saved.folder ? ` in ${saved.folder}` : ""}. It is under Saved in the side panel.${
                redacted ? " Credentials in it were replaced by [redacted]." : ""}`);
            }}
          />
        );
      })()}
      {fillFormTab && (() => {
        const t = tabs.find((x) => x.id === fillFormTab);
        if (!t?.fill?.length) return null;
        return (
          <FillParametersSheet
            key={t.id}
            tabName={t.name}
            fields={fillFields(t.sql, t.fill, t.fill_lists)}
            parameters={t.parameters}
            returnFocus={() => editor.current?.element ?? null}
            onUse={(parameters, filled, remaining) => applyFillValues(t.id, parameters, filled, remaining)}
            onLater={editParametersByHand}
            onClose={() => setFillFormTab(null)}
          />
        );
      })()}
      {pending && (
        <ConfirmSheet
          className="editor-confirm"
          band={
            <ConnectionTypeBand
              connection={(pending.payload.connection as Connection | undefined) || pending.confirmation.connection}
              type={pending.confirmation.connection_type}
            />
          }
          eyebrow={
            pending.confirmation.alter_preflight
              ? "ALTER TABLE"
              : pending.path === "editor/commit"
              ? "COMMIT"
              : pending.path === "editor/open"
                ? "SET SESSION"
                : pending.confirmation.count > 1
                  ? `${pending.confirmation.count} statements`
                  : (pending.confirmation.statements[0] || "").trim().split(/\s+/)[0]?.toUpperCase() || "SQL"
          }
          title={
            pending.path === "editor/commit"
              ? `Commit the transaction on ${pending.confirmation.connection.name}?`
              : pending.path === "editor/open"
                ? `Turn safe updates off on ${pending.confirmation.connection.name}?`
                : `Run ${pending.confirmation.count === 1 ? "this statement" : `these ${pending.confirmation.count} statements`} on ${pending.confirmation.connection.name}?`
          }
          description={pending.confirmation.text}
          busy={pendingBusy}
          confirmTone={pending.confirmation.tone}
          confirmDisabled={pending.confirmation.typed_target != null && pending.typedTarget !== pending.confirmation.typed_target}
          confirmLabel={
            pending.confirmation.typed_target != null
              ? `Drop database ${pending.confirmation.typed_target}`
              : pending.confirmation.alter_preflight
                ? "Run ALTER TABLE"
                : pending.path === "editor/commit"
              ? "Commit"
              : pending.path === "editor/open"
                ? "Turn safe updates off"
                : pending.confirmation.count === 1
                  ? "Run statement"
                  : `Run ${pending.confirmation.count} statements`
          }
          onCancel={declinePending}
          onConfirm={() => void confirmPending()}
        >
          {pending.again && (
            <p className="notice sheet-again" role="status">
              {pending.again}
            </p>
          )}
          <dl className="sheet-facts">
            <div>
              <dt>Connection</dt>
              <dd>
                {pending.confirmation.connection.name} · {pending.confirmation.connection.user ? `${pending.confirmation.connection.user}@` : ""}{pending.confirmation.connection.host}:
                {pending.confirmation.connection.port}
              </dd>
            </div>
            <div>
              <dt>Type</dt>
              <dd>{TYPE_LABEL[pending.confirmation.connection_type] || pending.confirmation.connection_type}</dd>
            </div>
            <div>
              <dt>Transaction</dt>
              <dd>
                {pending.confirmation.in_transaction === true
                  ? "open"
                  : pending.confirmation.in_transaction === null
                    ? "unknown (may be open)"
                    : "none"}
              </dd>
            </div>
            <div>
              <dt>Valid for</dt>
              <dd>{Math.round(pending.confirmation.expires_in / 60)} minutes, these statements only</dd>
            </div>
          </dl>
          {pending.confirmation.typed_target != null && (
            <label className="field">
              Type the database name <strong>{pending.confirmation.typed_target}</strong> to drop it. This cannot be undone.
              <input aria-label="Database name to confirm DROP" autoComplete="off" spellCheck={false}
                value={pending.typedTarget} disabled={pendingBusy}
                onChange={(e) => { const value = e.target.value; setPending((p) => p ? { ...p, typedTarget: value } : p); }} />
            </label>
          )}
          {pending.confirmation.alter_preflight && (() => {
            const review = pending.confirmation.alter_preflight;
            const prediction = review.prediction;
            return <section aria-label="ALTER TABLE preflight">
              <dl className="sheet-facts">
                {prediction && <div><dt>Predicted</dt><dd className="alter-prediction"
                  data-algorithm={prediction.predicted ? prediction.algorithm ?? "" : "none"}>
                  {prediction.predicted
                    ? <><strong>{prediction.summary}</strong>. {prediction.text}</>
                    : <>Not predicted. {notPredictedReason(prediction.reason)}</>}
                </dd></div>}
                <div><dt>Table</dt><dd>{review.schema || "(no database)"}.{review.table} · {review.table_engine || "engine unknown"}</dd></div>
                <div><dt>Server</dt><dd>{engineName(review.engine, review.version)}</dd></div>
                <div><dt>Approximate size</dt><dd title={review.approx_bytes == null ? undefined : `${review.approx_bytes.toLocaleString()} bytes (data and indexes)`}>{review.approx_bytes == null ? "Unknown" : `${bytes(review.approx_bytes)} (data and indexes)`}</dd></div>
                <div><dt>Metadata locks</dt><dd>{review.metadata_known ? `${review.metadata_locks.length} observed` : "Unknown"}{review.truncated ? " (incomplete snapshot)" : ""}</dd></div>
                <div><dt>Open transactions on table</dt><dd>{review.metadata_known && review.transactions_known ? `${review.transactions.length} observed` : "Unknown"}</dd></div>
                <div><dt>Lock wait limit</dt><dd>5 seconds per metadata-lock wait; retained in this tab's session</dd></div>
              </dl>
              <details><summary>Lock and transaction snapshot</summary>
                <ul>
                  {review.metadata_locks.map((lock, i) => <li key={`m${i}`}>Connection {lock.connection_id ?? "unknown"}: {lock.lock_type}, {lock.status}</li>)}
                  {review.transactions.map((trx, i) => <li key={`t${i}`}>Connection {trx.connection_id ?? "unknown"}: transaction open {trx.age_s == null ? "?" : durationText(trx.age_s)}{trx.query ? `; ${trx.query}` : ""}</li>)}
                  {review.sessions?.map((session, i) => <li key={`s${i}`}>Connection {session.connection_id ?? "unknown"}: {session.query || "No current statement reported"}</li>)}
                </ul>
                <p>{review.unattributed_transactions?.length || 0} other open transactions; table ownership not established. This snapshot cannot rule out new or uninstrumented locks. Check Server → Locks while ALTER runs.</p>
              </details>
              {prediction && <AlterPredictionWhy prediction={prediction} />}
              {review.suggestion && <details><summary>Optional SQL with explicit algorithm and lock</summary>
                <p>Copy this into the editor and review it again to use it. The server checks support; INPLACE can still rebuild the table.</p>
                <pre className="sheet-statements" aria-label="Suggested ALTER SQL">{review.suggestion};</pre>
              </details>}
            </section>;
          })()}
          {pending.confirmation.warnings.length > 0 && (
            <ul className="sheet-warnings" role="alert">
              {pending.confirmation.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}
          {pending.confirmation.reasons.length > 0 && (
            <ul className="sheet-reasons">
              {pending.confirmation.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          )}
          <pre className="sheet-statements" aria-label="Statements that will be sent">
            {pending.confirmation.statements.join(";\n\n")}
            {pending.confirmation.statements.length ? ";" : ""}
          </pre>
        </ConfirmSheet>
      )}
      {closing && (
        <ConfirmSheet
          band={
            <ConnectionTypeBand
              connection={sessionConnectionOf(closing.tabId) || runtime[closing.tabId]?.session?.connection || connection}
            />
          }
          eyebrow="Transaction"
          title={
            closing.keepTab
              ? `Close this session with a transaction ${closing.known ? "open" : "that may be open"}?`
              : `Close ${tabs.find((t) => t.id === closing.tabId)?.name || "this tab"} with a transaction ${closing.known ? "open" : "that may be open"}?`
          }
          description={
            closing.known
              ? "Closing ends the tab's session. Commit keeps the changes; Roll back undoes them."
              : "myflames cannot tell whether a transaction is open in this session. Closing ends it; anything not committed is rolled back."
          }
          busy={closingBusy}
          confirmTone="primary"
          confirmLabel="Commit and close"
          secondary={{ label: "Roll back and close", onClick: () => void closeDiscarding() }}
          onCancel={() => setClosing(null)}
          onConfirm={commitAndClose}
        />
      )}
      {closingFile && (
        <ConfirmSheet
          eyebrow="Unsaved changes"
          title={`Save ${tabs.find((t) => t.id === closingFile.tabId)?.name || "this tab"} before closing it?`}
          description={`${runtime[closingFile.tabId]?.file?.name || "The file"} changed since the last save.`}
          busy={!!runtime[closingFile.tabId]?.fileBusy}
          confirmTone="primary"
          confirmLabel="Save"
          secondary={{
            label: "Don't save",
            onClick: () => {
              const c = closingFile;
              setClosingFile(null);
              void requestClose(c.tabId, c.keepTab, true);
            },
          }}
          onCancel={() => setClosingFile(null)}
          onConfirm={() => {
            const c = closingFile;
            void saveTab(c.tabId).then((saved) => {
              if (!saved) return;
              setClosingFile(null);
              void requestClose(c.tabId, c.keepTab);
            });
          }}
        />
      )}
    </section>
  );
});
