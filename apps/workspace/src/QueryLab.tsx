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
  ApiError,
  type Capture,
  type Connection,
  type EditorClose,
  type EditorConfirmation,
  type EditorReply,
  type EditorRows,
  type EditorRun,
  type EditorSession,
  type EditorStop,
  type Plan,
  type TopStatement,
} from "./api";
import type { DigestAction } from "./SlowQueries";
import { delimiterAt, describeTarget, runTarget, sqlRun, type RunTarget } from "./sqlSelection";
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
import { Icon } from "./components";
import { Checkbox, ConfirmSheet, EmptyState, IconButton, InfoPopover, NumberField, StatusPill, ToolbarButton, useWindowStatus, type Tone } from "./kit";
import { useWritePolicy, TYPE_LABEL } from "./Connections";
import { SqlEditor, type Range } from "./SqlEditor";
import { ResultsPane, rowResults } from "./ResultsPane";
import { SchemaBrowser } from "./SchemaBrowser";
import { TableData } from "./TableData";
import { HistoryPanel } from "./HistoryPanel";
import { editorPrivilegeGate, isPrivilegeDenial, privilegeButtonProps, usePrivilege, usePrivileges, type EditorPrivilegePreview } from "./privileges";
import { QueryDigestView } from "./QueryDigest";
import "./editor.css";

const MAC =
  typeof navigator !== "undefined" &&
  /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent);
/** Visible shortcut text: "⌘Return" on macOS, "Ctrl+Enter" elsewhere. */
function keys(shift: boolean, key: "Enter" | "E" = "Enter") {
  if (key === "E") return MAC ? `${shift ? "⇧" : ""}⌘E` : `Ctrl+${shift ? "Shift+" : ""}E`;
  return MAC ? `${shift ? "⇧" : ""}⌘Return` : `Ctrl+${shift ? "Shift+" : ""}Enter`;
}
/** How long the statement that runs stays highlighted in the editor. */
const FLASH_MS = 1600;
/** Below this width the schema browser and history open as sheets. */
const NARROW = 720;
type Tab = QueryTab;
/** Options for adding a captured plan: `open: false` keeps the current screen. */
export type CaptureOptions = { open?: boolean };
/** The editor part of the restorable lab state (the connection and the
 * Server tabs are reported by their own owners). */
export type EditorState = Pick<
  LabState,
  "tabs" | "tab_id" | "timeout" | "repeat" | "trace" | "conditions" | "editor_layout"
>;
/** What the Server place asks of the editor. */
export type QueryLabHandle = {
  /** Open a Top statements row in a new tab and, for Explain and Run
   * analysis, capture it with the editor's settings. With *expect* (a
   * connectionKey), nothing opens or runs unless that is still the
   * connection in use. */
  openDigest: (row: TopStatement, rank: number, action: DigestAction, expect?: string) => void;
  /** Switch to the Query Digest (pt-query-digest) view */
  openDigestView?: () => void;
  /** Add an empty query tab and put the cursor in it (Home's New query tab). */
  newTab: () => void;
  /** Make an existing tab the current one (Home's Recent work); unknown ids do nothing. */
  openTab: (id: string) => void;
  /** Add a statement at the end of the current tab, selected and not run
   * (the SET SESSION from Parameters: it changes only this tab's session). */
  openSql: (sql: string) => void;
  /** Request closing a query tab, verifying uncommitted transactions first. */
  closeTab: (id: string) => void;
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
type Runtime = {
  session: EditorSession | null;
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
  /** Requests started for the tab; an answer to an older probe is dropped. */
  gen: number;
  /** The last status poll failed: the run may still be going, retrying. */
  pollTrouble: boolean;
  run: EditorRun | null;
  /** The editor text when the run started (positions are stale after an edit). */
  runSql: string;
  running: boolean;
  stopping: boolean;
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
};
const blankRuntime = (): Runtime => ({
  session: null,
  sessionKey: "",
  sessionConnection: null,
  execConnection: null,
  runKey: "",
  runSessionId: null,
  gen: 0,
  pollTrouble: false,
  run: null,
  runSql: "",
  running: false,
  stopping: false,
  note: "",
  noteError: false,
  binary: {},
  reloaded: {},
  safe: null,
  probed: false,
});
type SendMeta = {
  runSql: string;
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
      const below = b.bottom + 4;
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
      if ((e as CustomEvent).detail !== id) onOpen(false);
    };
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!button.current?.contains(target) && !panel.current?.contains(target)) onOpen(false);
    };
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
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
    /** Open Indexes or Tables on one table (the schema browser's menu); *from*
     * is the key of the connection the name was read from. */
    onOpenTable?: (view: "indexes" | "tables", schema: string, table: string, from: string) => void;
  }
>(function QueryLab(
  {
    onCapture,
    connection,
    picker,
    batchRunning,
    initial,
    onStateChange,
    onTabsNow,
    onBusyChange,
    onConnected,
    onOpenTable,
  },
  ref,
) {
  // Read once on mount: App remounts the editor (new key) to restore again.
  const [start] = useState(() => normalizeLab(initial));
  const [tabs, setTabs] = useState<Tab[]>(start.tabs);
  const [tabId, setTabId] = useState(start.tab_id);
  const tab = tabs.find((t) => t.id === tabId) || tabs[0];
  const [timeout, setTimeoutSeconds] = useState(start.timeout);
  const [repeat, setRepeat] = useState(start.repeat);
  const [trace, setTrace] = useState(start.trace);
  const [conditions, setConditions] = useState(start.conditions);
  const [layout, setLayout] = useState<EditorLayout>(() => normalizeEditorLayout(start.editor_layout ?? defaultEditorLayout()));
  const privileges = usePrivileges();
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
  const [workspaceMode, setWorkspaceMode] = useState<"editor" | "digest">("editor");
  // A staged table draft remains bound to the connection it was opened from.
  const [tableData, setTableData] = useState<{ connection: Connection; schema: string; table: string } | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  // The connection the capture was started on: the server answers its status
  // and Cancel only for that connection.
  const jobConnection = useRef<Connection | null>(null);
  const [starting, setStarting] = useState(false);
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
  const editor = useRef<HTMLTextAreaElement>(null);
  const parametersField = useRef<HTMLTextAreaElement>(null);
  const [parametersOpen, setParametersOpen] = useState(false);
  const [analysisOptionsOpen, setAnalysisOptionsOpen] = useState(false);
  const [runOptionsOpen, setRunOptionsOpen] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; name: string } | null>(null);
  function finishRename(save: boolean, restoreFocus = true) {
    if (!renaming) return;
    if (save && renaming.name.trim()) changeTab({ name: renaming.name.trim() }, renaming.id);
    const id = renaming.id;
    setRenaming(null);
    if (restoreFocus) window.setTimeout(() => document.getElementById(`query-tab-${tabs.findIndex((t) => t.id === id)}`)?.focus());
  }
  const showAnalysisOptions = analysisOptionsOpen || parametersOpen;
  const toggleAnalysisOptions = useCallback((open: boolean) => {
    setAnalysisOptionsOpen(open);
    if (!open) setParametersOpen(false);
  }, []);
  // Editor selection, for the run scope (selection / statement at cursor).
  const [cursor, setCursor] = useState({ start: 0, end: 0 });
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
  const [runtime, setRuntime] = useState<Record<string, Runtime>>({});
  // The latest runtime, updated in the same task as every change (a
  // continuation such as Commit and close reads it before React renders).
  const runtimeRef = useRef(runtime);
  const rt = runtime[tab.id] || blankRuntime();
  const [pending, setPending] = useState<Pending | null>(null);
  const [pendingBusy, setPendingBusy] = useState(false);
  const [closing, setClosing] = useState<{ tabId: string; known: boolean; keepTab: boolean } | null>(null);
  const [closingBusy, setClosingBusy] = useState(false);
  const [historyTick, setHistoryTick] = useState(0);
  const narrow = useNarrow();
  // Below NARROW px the panes are sheets, closed until asked for in this page.
  const [sheet, setSheet] = useState<"" | "browser" | "history">("");
  const showBrowser = narrow ? sheet === "browser" : layout.browser;
  const showHistory = narrow ? sheet === "history" : layout.history;
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
    runtimeRef.current = { ...all, [id]: { ...current, ...next } };
    setRuntime(runtimeRef.current);
  }
  function note(id: string, text: string, isError = false) {
    patchRuntime(id, { note: text, noteError: isError });
  }

  useImperativeHandle(ref, () => ({
    openDigest,
    openDigestView: () => setWorkspaceMode("digest"),
    newTab: () => addTab(true),
    openTab: (id: string) => {
      if (tabs.some((t) => t.id === id)) setTabId(id);
    },
    openSql: appendStatement,
    closeTab: (id: string) => {
      void requestClose(id);
    },
  }));
  /** A new tab after the others; *focus* puts the cursor in the editor. */
  function addTab(focus = false) {
    const id = crypto.randomUUID();
    setTabs((ts) => [
      ...ts,
      {
        id,
        name: `Query ${ts.length + 1}`,
        sql: "SELECT 1",
        parameters: "{}",
      },
    ]);
    setTabId(id);
    if (focus) window.setTimeout(() => editor.current?.focus());
  }
  /**
   * Open a Top statements row in a new tab and, for Explain and Run
   * analysis, capture it right away. A statement that only has normalized
   * text opens with a parameters skeleton and waits for values.
   */
  function openDigest(row: TopStatement, rank: number, action: DigestAction, expect?: string) {
    if (expect !== undefined && expect !== connectionKey(connection)) {
      setStatus("");
      setError("The connection in use changed after you chose this statement, so nothing ran. Refresh Top statements and choose it again.");
      return;
    }
    // A statement capture refuses (not a SELECT) opens as recorded: there is
    // nothing to fill in for a run that cannot happen.
    const refused = row.capture_problem || "";
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
    };
    if (source) {
      next.source = source;
      // The tab stays bound to the server the statement came from, also for
      // a run deferred until its parameters are filled in, and after restore.
      next.target = { key: connectionKey(connection), label: connectionLabel(connection) };
    }
    if (q.names.length && !refused) {
      next.fill = q.names;
      next.fill_note = fillNotice(q);
    }
    setTabs((ts) => [...ts, next]);
    setTabId(next.id);
    setError("");
    setStatus("");
    if (refused) {
      setStatus(`Loaded the statement into a new tab. Explain and Run analysis cannot run it: ${refused}`);
      window.setTimeout(() => editor.current?.focus());
      return;
    }
    if (row.analysis_warning) {
      setAnalysisWarning({ tab: next.id, text: row.analysis_warning });
      if (q.names.length) setParametersOpen(true);
      window.setTimeout(() => editor.current?.focus());
      return;
    }
    if (q.names.length) {
      // Nothing runs until the values are filled in.
      setParametersOpen(true);
      window.setTimeout(() => {
        parametersField.current?.focus();
        parametersField.current?.scrollIntoView({ block: "nearest" });
      });
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
        editor.current?.scrollIntoView({ block: "nearest" });
      });
      return;
    }
    window.setTimeout(() =>
      editor.current?.scrollIntoView({ block: "nearest" }),
    );
    void startCapture(next, q.sql, {}, action, `Top statements #${rank}`);
  }
  const received = useRef("");
  const reportRef = useRef(onStateChange);
  reportRef.current = onStateChange;
  const tabsNowRef = useRef(onTabsNow);
  tabsNowRef.current = onTabsNow;
  // App's beforeunload check (unsaved query tabs when autosave is off) must
  // see an edit as soon as it is made: a layout effect hands it the tabs in
  // the same task as the keystroke. The full report below stays a passive
  // effect, because App normalizes and serializes the whole workspace from
  // it; a reload between the two once checked the tabs before the edit.
  useLayoutEffect(() => {
    tabsNowRef.current?.(tabs);
  }, [tabs]);
  // The pane layout is reported once it differs from the defaults (or was
  // restored), so opening the workspace alone never changes the session.
  const layoutChanged = useRef(!!start.editor_layout);
  if (!layoutChanged.current && JSON.stringify(layout) !== JSON.stringify(defaultEditorLayout())) layoutChanged.current = true;
  const reportedLayout = layoutChanged.current ? layout : undefined;
  useEffect(() => {
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
          setStatus("Capture complete. The plan is open in Plans.");
          await onCapture(next.result, captureName.current);
        }
        if (next.error) { setError(next.error); denied(next.error, jobConnection.current); }
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
  const sqlNow = useRef(tab.sql);
  sqlNow.current = tab.sql;
  function syncCursor() {
    const el = editor.current;
    // Offsets only describe the text in state. When the field holds other
    // text, an input event is pending and the effect below syncs after it;
    // re-rendering now would put the old text back into the field.
    if (!el || el.value !== sqlNow.current) return;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    setCursor((c) => (c.start === start && c.end === end ? c : { start, end }));
  }
  // A tab switch (or the first render) puts back the caret the tab was left with.
  useLayoutEffect(() => {
    const el = editor.current;
    const saved = tab.selection;
    if (!el || !saved || el.value !== tab.sql) return;
    const length = tab.sql.length;
    el.setSelectionRange(Math.min(saved.start, length), Math.min(saved.end, length));
    // Only on a tab switch, never while typing.
  }, [tabId]);
  // A tab switch or a loaded query replaces the text, and the browser moves
  // the caret without a select event.
  useLayoutEffect(syncCursor, [tabId, tab.sql]);
  useEffect(() => {
    // React's onSelect misses selections made without a key or mouse event
    // (context menu Select All, assistive technology, setSelectionRange).
    const el = editor.current;
    el?.addEventListener("select", syncCursor);
    el?.addEventListener("selectionchange", syncCursor);
    document.addEventListener("selectionchange", syncCursor);
    return () => {
      el?.removeEventListener("select", syncCursor);
      el?.removeEventListener("selectionchange", syncCursor);
      document.removeEventListener("selectionchange", syncCursor);
      window.clearTimeout(flashTimer.current);
    };
  }, []);
  // The caret is saved with the tab a moment after it stops moving, once the
  // user has worked in the editor (opening the page saves nothing).
  const touched = useRef(false);
  useEffect(() => {
    const el = editor.current;
    const mark = () => (touched.current = true);
    el?.addEventListener("focus", mark);
    return () => el?.removeEventListener("focus", mark);
  }, []);
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
    if (at?.probed) return;
    // Marked only once answered: a probe cut short by a tab switch or a
    // failure is asked again the next time the tab is shown.
    const gen = at?.gen ?? 0;
    const controller = new AbortController();
    api<EditorReply>("editor/status", { tab_id: id, wait: 0, connection: connectionRef.current }, controller.signal).then(
      (reply) => {
        const now = runtimeRef.current[id] || blankRuntime();
        // A run started meanwhile knows better than this answer.
        if (now.gen !== gen || now.running) return;
        patchRuntime(id, { probed: true });
        if (!reply.session) return;
        const session = reply.session;
        const key = sessionKeyFor(session, connectionRef.current);
        // Bound to the connection in use only when the session says it is that one.
        const verified = key === connectionKey(connectionRef.current) ? connectionRef.current : null;
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
  }, [tab.id]);
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
  function previewKey(payload: Record<string, unknown>) {
    const via = payload.connection as Connection;
    return JSON.stringify([payload.tab_id, connectionKey(via), via.database || "", payload.sql,
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
    [scopedKey, allKey, previewEpoch, privileges?.report, connection.password]);
  const [previews, setPreviews] = useState<{
    generation: symbol;
    values: Record<string, EditorPrivilegePreview>;
  } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    if (!tab.sql.trim() || rt.running) return () => controller.abort();
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
  // One explanation for the run group. A pending preview is quiet; only
  // its current answer can add the unknown note. Keep mixed batches runnable.
  const privilegeReasons = [...new Set(
    [runGate, allGate, explainGate, analysisGate].map(gate => gate.disabledReason)
      .filter(reason => reason && !previewWarning?.includes(reason)),
  )];
  const privilegeNotice = [previewWarning, ...privilegeReasons].filter(Boolean).join(" ") ||
    (runPreview?.aggregate.state === "unknown" ? "Privileges not verified for this statement" : "");
  const privilegeNoticeId = privilegeNotice ? `${privilegeId}-notice` : undefined;
  const scopeLabel = describeTarget(tab.sql, target);
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
    if (!t.source) return "";
    if (!t.target)
      return `This statement came from Top statements, and the workspace did not record which server it was read from. Choose Use this connection to run it on ${connectionLabel(connection)}.`;
    if (t.target.key !== connectionKey(connection))
      return `This statement came from Top statements on ${t.target.label || "another connection"}, not the connection in use. Switch back, or choose Use this connection to run it on ${connectionLabel(connection)}.`;
    return "";
  }
  /** Move a Top statements tab to the connection in use (an explicit choice). */
  function bindToConnection() {
    changeTab({ target: { key: connectionKey(connection), label: connectionLabel(connection) } });
    setError("");
    setStatus(`This tab now runs on ${connectionLabel(connection)}.`);
  }
  /** The connection a run of *t* uses: a Top statements tab runs in the
   * schema it was recorded in when the connection names none. */
  function connectionFor(t: Tab): Connection {
    const database = connection.database || t.source?.schema || "";
    return database === connection.database ? connection : { ...connection, database };
  }
  /** Start one capture of *sql* for tab *t* with the lab's settings. */
  async function startCapture(
    t: Tab,
    sql: string,
    parameters: unknown,
    mode: string,
    label: string,
  ) {
    // Every capture path (buttons, shortcuts, a row's Explain) passes here.
    if (busy || pendingStart.current) return;
    const gate = mode === "explain" ? explainGate : analysisGate;
    const problem = bindingProblem(t) || sessionProblem(t.id) || gate.disabledReason;
    if (problem) {
      setStatus("");
      setError(problem);
      return;
    }
    const pending = { cancelled: false };
    pendingStart.current = pending;
    setStarting(true);
    changeTab({ result_tab: "plan" }, t.id);
    try {
      const expectedConnection = connectionKey(connectionRef.current);
      const prepared = await api<{ sql: string; converted: boolean; warning: string; problem: string }>(
        "analysis/prepare-query", { query: sql });
      if (pending.cancelled) return;
      if (expectedConnection !== connectionKey(connectionRef.current))
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
        setStatus("Review the converted SELECT, then choose Explain or Run analysis.");
        return;
      }
      sql = prepared.sql;
      captureName.current = t.name;
      setLastRun({ tabId: t.id, label, sql });
      const via = connectionFor(t);
      jobConnection.current = via;
      const result = await api<{ id: string }>("live/start", {
        connection: via,
        query: sql,
        parameters,
        mode,
        timeout,
        repeat,
        trace,
        conditions,
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
        repeat: mode === "explain" ? 1 : repeat,
        error: "",
        result: null,
      });
    } catch (e) {
      denied(e, jobConnection.current);
      setError((e as Error).message);
    } finally {
      if (pendingStart.current === pending) pendingStart.current = null;
      setStarting(false);
    }
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
  /** Explain or Run analysis: one SELECT, captured as a plan. */
  async function capture(mode: string) {
    setError("");
    setStatus("");
    // Read the selection now: a click on a button keeps the editor's
    // selection, and a shortcut runs before any select event.
    const el = editor.current;
    const chosen = el
      ? runTarget(tab.sql, el.selectionStart, el.selectionEnd)
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
    await startCapture(tab, chosen.text, parameters, mode, sentLabel(chosen));
  }
  function flashRange(startAt: number, endAt: number, sql = tab.sql) {
    window.clearTimeout(flashTimer.current);
    setFlash({ tabId: tab.id, sql, start: startAt, end: endAt });
    flashTimer.current = window.setTimeout(() => setFlash(null), FLASH_MS);
  }

  // ---- Run SQL in the tab's session -------------------------------------------

  /** The session of the tab is on another connection than the one in use. */
  const mismatch = !!rt.session && !rt.session.ended && !!rt.sessionKey && rt.sessionKey !== connectionKey(connection);
  /** Why tab *id* may not run on the connection in use now: its session is
   * on another connection. Every execution path checks it (Run, Run all,
   * Explain, Run analysis, their shortcuts, Load more). */
  function sessionProblem(id: string) {
    const now = runtimeRef.current[id] || blankRuntime();
    if (now.session && !now.session.ended && now.sessionKey && now.sessionKey !== connectionKey(connectionRef.current))
      return `This tab's session is on ${sessionName(now.session)}, not the connection in use. Close the session to run on ${connectionLabel(connectionRef.current)}.`;
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
  function runSql(all: boolean, text?: string, caret?: number) {
    setError("");
    setStatus("");
    const t = tab;
    const sql = text ?? t.sql;
    const el = editor.current;
    const lo = caret ?? (el ? el.selectionStart : cursor.start);
    const hi = caret ?? (el ? el.selectionEnd : cursor.end);
    const now = runtimeRef.current[t.id] || blankRuntime();
    const part = sqlRun(sql, lo, hi, all, now.session?.backslash_escapes ?? true);
    if (part.problem) {
      setError(part.problem);
      return;
    }
    const problem = bindingProblem(t) || sessionProblem(t.id);
    if (problem) {
      setError(problem);
      return;
    }
    if (now.running) return;
    if (now.session?.ended) {
      setError("This tab's session ended. Choose Reconnect to start a new session.");
      return;
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
      wait: 2,
      ...(now.safe === true ? { safe_updates: true } : {}),
    };
    const gate = editorPrivilegeGate(currentPreview(previewKey(payload)));
    if (gate.disabledReason) {
      setError(gate.disabledReason);
      return;
    }
    void send(t.id, "editor/run", payload, { runSql: sql });
  }
  async function send(id: string, path: string, payload: Record<string, unknown>, meta: SendMeta) {
    const isRun = path !== "editor/open";
    const gen = (runtimeRef.current[id]?.gen ?? 0) + 1;
    // The connection of this request, before any reply: Stop uses it even
    // when the picker changes while the request is on its way.
    patchRuntime(id, { running: isRun, note: "", noteError: false, gen, execConnection: payload.connection as Connection });
    let reply: EditorReply;
    try {
      reply = await api<EditorReply>(path, payload);
    } catch (e) {
      patchRuntime(id, { running: false, execConnection: null });
      const err = e as ApiError;
      denied(err, payload.connection as Connection);
      setError(err.message);
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
    const done = { running: false, execConnection: null };
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
        patchRuntime(id, { run, runSql: meta.runSql, binary: {}, reloaded: {}, ...bound(id, reply.session) });
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
    return { runKey: now.sessionKey, runSessionId: (session ?? now.session)?.connection_id ?? null };
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
          { tab_id: id, run_id: runId, wait: 5, connection: sessionConnectionOf(id) || connectionRef.current },
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
        patchRuntime(id, { running: false, execConnection: null });
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
    patchRuntime(id, { running: false, stopping: false, execConnection: null, pollTrouble: false });
    setHistoryTick((n) => n + 1);
    if (meta.action) {
      const word = meta.action === "commit" ? "Committed" : "Rolled back";
      if (run.state === "complete") {
        note(id, `${word}.`);
        meta.after?.();
      } else {
        const err = run.error || run.results.find((r) => r.type === "error");
        note(id, `${meta.action === "commit" ? "Commit" : "Roll back"} failed: ${(err && "message" in err && err.message) || run.message || run.state}`, true);
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
          s.index === merge.statement ? { ...s, status: run.statements[0]?.status ?? s.status, duration_ms: run.statements[0]?.duration_ms ?? s.duration_ms } : s,
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
    patchRuntime(id, { run, runSql: meta.runSql, binary: {}, reloaded: {}, ...bound(id, session) });
    const rows = rowResults(run);
    changeTab({ result_tab: run.error || !rows.length || run.state === "session_ended" ? "messages" : "result:0" }, id);
    if (run.state === "stopped") note(id, run.stopped?.message || "Stopped.");
  }
  async function stop(id = tab.id) {
    const now = runtimeRef.current[id];
    if (!now?.running || now.stopping) return;
    const via = sessionConnectionOf(id);
    if (!via) {
      setError(noSessionConnection(id));
      return;
    }
    patchRuntime(id, { stopping: true });
    try {
      const reply = await api<EditorStop>("editor/stop", { tab_id: id, connection: via });
      note(id, reply.message);
      if (reply.session !== undefined) patchRuntime(id, { session: reply.session });
    } catch (e) {
      setError((e as Error).message);
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
    const now = runtimeRef.current[id] || blankRuntime();
    setError("");
    patchRuntime(id, { session: null, sessionKey: "", sessionConnection: null });
    void send(id, "editor/open", { tab_id: id, connection: connectionFor(tab), ...(now.safe === true ? { safe_updates: true } : {}) }, { runSql: "" });
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
      now.runKey === connectionKey(connectionRef.current)
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
        limit,
        timeout: layout.run_timeout || 0,
        wait: 2,
      },
      { runSql: now.runSql, merge: { statement: result.statement, result: index, limit } },
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
          fresh = await policy.check();
        } catch (e) {
          setPending(null);
          setError(`Could not read this connection's settings (${(e as Error).message}), so nothing was sent.`);
          return;
        }
        if (connectionKey(fresh) !== p.key) {
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
        }];
      }
      if (id === tabId && remainingTabs.length) setTabId(remainingTabs[Math.max(0, index - 1)].id);
      return remainingTabs;
    });
  }
  /** Close a tab (or only its session): a transaction that is open, or may
   * be, asks Commit, Roll back or Cancel first. */
  async function requestClose(id: string, keepTab = false) {
    const now = runtimeRef.current[id];
    if (now?.running) {
      setError("A statement is running in this tab. Stop it before closing.");
      return;
    }
    try {
      const reply = await api<EditorClose>("editor/close", { tab_id: id, connection: sessionConnectionOf(id) || connectionRef.current });
      if (reply.closed) {
        if (keepTab) {
          patchRuntime(id, { session: null, sessionKey: "", sessionConnection: null });
          note(id, "Session closed. The next run opens a new one on the connection in use.");
        } else removeTab(id);
        return;
      }
      if (reply.session) adoptSession(id, reply.session, {});
      setClosing({ tabId: id, known: reply.reason === "transaction_open", keepTab });
    } catch (e) {
      setError((e as Error).message);
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
        connection: sessionConnectionOf(c.tabId) || connectionRef.current,
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
    commit(c.tabId, () => void requestClose(c.tabId, c.keepTab));
  }

  // ---- Editing helpers (schema browser, history, error positions) ----------

  /** Insert *text* at the caret, keeping the browser's undo history. */
  function insertAtCursor(text: string) {
    const el = editor.current;
    if (!el) return;
    el.focus();
    if (!document.execCommand("insertText", false, text)) {
      const s = el.selectionStart;
      const e = el.selectionEnd;
      changeTab({ sql: tab.sql.slice(0, s) + text + tab.sql.slice(e) });
      window.requestAnimationFrame(() => el.setSelectionRange(s + text.length, s + text.length));
    }
    if (narrow) setSheet("");
  }
  /** Add *statement* at the end of the tab, selected, without running it. */
  function appendStatement(statement: string) {
    const sql = tab.sql;
    const d = sql.trim() ? delimiterAt(sql, sql.length, runtimeRef.current[tab.id]?.session?.backslash_escapes ?? true) : ";";
    const body = statement.trim().replace(/;$/, "");
    const trimmed = sql.replace(/\s+$/, "");
    const lead = !trimmed ? "" : !trimmed.endsWith(d) ? `${d}\n\n` : sql.endsWith("\n\n") ? "" : sql.endsWith("\n") ? "\n" : "\n\n";
    const at = sql.length + lead.length;
    changeTab({ sql: sql + lead + body + d });
    window.requestAnimationFrame(() => {
      const el = editor.current;
      if (!el) return;
      el.focus();
      el.setSelectionRange(at, at + body.length);
    });
  }
  /** Add *statement* at the end of the tab and run it. */
  function appendAndRun(statement: string) {
    const el = editor.current;
    if (!el) return;
    const sql = tab.sql;
    const d = sql.trim() ? delimiterAt(sql, sql.length, runtimeRef.current[tab.id]?.session?.backslash_escapes ?? true) : ";";
    const trimmed = sql.replace(/\s+$/, "");
    const lead = !trimmed ? "" : !trimmed.endsWith(d) ? `${d}\n\n` : sql.endsWith("\n\n") ? "" : sql.endsWith("\n") ? "\n" : "\n\n";
    const addition = lead + statement + d;
    el.focus();
    el.setSelectionRange(sql.length, sql.length);
    if (!document.execCommand("insertText", false, addition)) changeTab({ sql: sql + addition });
    const at = sql.length + lead.length;
    el.setSelectionRange(at, at);
    if (narrow) setSheet("");
    runSql(false, sql + addition, at);
  }
  function jump(offset: number) {
    const el = editor.current;
    if (!el) return;
    const at = Math.max(0, Math.min(offset, tab.sql.length));
    el.focus();
    el.setSelectionRange(at, at);
    let line = 0;
    for (let i = 0; i < at; i++) if (tab.sql[i] === "\n") line++;
    el.scrollTop = Math.max(0, (line - 3) * 20);
    syncCursor();
  }

  // ---- Layout: splitter and panes -------------------------------------------

  function setHeight(height: number) {
    setLayout((l) => ({ ...l, height: Math.round(Math.max(120, Math.min(2000, height))) }));
  }
  function togglePane(pane: "browser" | "history") {
    if (narrow) setSheet((s) => (s === pane ? "" : pane));
    else setLayout((l) => ({ ...l, [pane]: !l[pane] }));
  }

  const pendingFill = unfilled(tab, tab.sql, parseParameters(tab.parameters));
  const unbound = bindingProblem(tab);
  const tabSchema = tab.source
    ? connection.database || tab.source.schema || ""
    : "";
  const session = rt.session;
  const running = rt.running;
  const stale = !!rt.run && rt.runSql !== tab.sql;
  const errorPosition = rt.run?.error?.position;
  const errorMark = !stale && errorPosition ? wordAt(tab.sql, errorPosition.offset) : null;
  const statementMark = runPart.scope === "statement" && runPart.total > 1 && !runPart.problem ? { start: runPart.start, end: runPart.end } : null;
  const safeChecked = session && !session.ended && session.safe_updates !== null ? session.safe_updates : rt.safe ?? connection.type !== "development";
  const txOpen = !!session && !session.ended && session.in_transaction !== false;
  let tx: [Tone, string, string] = ["off", "No session", "The first run opens this tab's session: one server connection kept until the tab closes."];
  if (session?.ended) tx = ["problem", "Session ended", session.message || ""];
  else if (session && session.in_transaction === true) tx = ["watch", "Transaction open", "Commit or roll it back; closing the tab asks first."];
  else if (session && session.in_transaction === null)
    tx = ["unknown", "Transaction unknown", session.transaction_note || "This server cannot tell whether a transaction is open."];
  else if (session) tx = ["ok", session.autocommit === false ? "Manual commit" : "Auto-commit", session.autocommit === false ? "autocommit is off: changes wait for Commit" : "Each statement commits on its own."];
  const runLabel = rt.run && !running ? rt.run : null;
  // A tab may retain a session/results from another connection. Publish only
  // evidence belonging to the connection named by the shell's active scope.
  const statusKey = connectionKey(connection);
  const statusSession = rt.sessionKey === statusKey ? session : null;
  const statusRun = rt.runKey === statusKey ? runLabel : null;
  const transaction = statusSession && !statusSession.ended
    ? tx[1] === "Auto-commit" && statusSession.autocommit === null ? "Autocommit unknown" : tx[1]
    : undefined;
  useWindowStatus(`query:${statusKey}:${tab.id}`, {
    connectionState: connectionLabel(connection),
    engineVersion: statusSession?.server_version ? `${statusSession.engine === "mariadb" ? "MariaDB" : statusSession.engine === "mysql" ? "MySQL" : "Server"} ${statusSession.server_version}` : undefined,
    sessionState: statusSession
      ? statusSession.ended ? "Session ended"
        : `${running ? "Running" : statusSession.state === "starting" ? "Session starting" : "Session open"}${statusSession.connection_id !== null ? ` · connection ${statusSession.connection_id}` : ""}${statusSession.read_only_transactions ? " · read-only" : ""}`
      : rt.probed && !session ? "No session" : undefined,
    transaction,
    lastRunAt: statusRun?.finished_at ? Date.parse(statusRun.finished_at) : undefined,
    durationMs: statusRun?.duration_ms,
    rows: statusRun?.counts.rows,
  });
  const planPane =
    lastRun?.tabId === tab.id || (jobBusy && lastRun?.tabId === tab.id) ? (
      <div className="plan-pane">
        {jobBusy && (
          <p role="status" className="capture-status">
            <span className="spinner" /> Capturing {job?.progress || 0} /{" "}
            {job?.repeat || 1} runs…
          </p>
        )}
        {!jobBusy && <p className="hint">Each capture opens as a plan in Plans.</p>}
        {lastRun && lastRun.tabId === tab.id && (
          <details className="sent-sql">
            <summary>SQL sent: {lastRun.label}</summary>
            <pre>{lastRun.sql}</pre>
          </details>
        )}
      </div>
    ) : null;
  const ctrl = (shift: boolean, key: "Enter" | "E") =>
    key === "E" ? (MAC ? `Meta+${shift ? "Shift+" : ""}E` : `Control+${shift ? "Shift+" : ""}E`) : `Control+${shift ? "Shift+" : ""}Enter Meta+${shift ? "Shift+" : ""}Enter`;
  return (
    <section className="query-lab" aria-labelledby="query-editor-title">
      <h1 id="query-editor-title" className="visually-hidden">Query editor</h1>
      {tableData && <TableData connection={tableData.connection} currentConnectionKey={connectionKey(connection)}
        schema={tableData.schema} table={tableData.table} onClose={() => {
          setTableData(null);
          window.requestAnimationFrame(() => editor.current?.focus());
        }} /> }
      <div hidden={!!tableData} className={`editor-workbench${showBrowser ? " has-browser" : ""}${showHistory ? " has-history" : ""}${narrow ? " is-narrow" : ""}`}>
        {/* Kept mounted while hidden, so what was read stays when it opens again. */}
        <div className="schema-browser-slot" hidden={!showBrowser}>
          <SchemaBrowser
            connection={connection}
            width={narrow ? 0 : layout.browser_width}
            onWidth={(w) => setLayout((l) => ({ ...l, browser_width: w }))}
            onClose={() => togglePane("browser")}
            onInsert={insertAtCursor}
            onQuery={appendAndRun}
            onOpenTable={onOpenTable}
            onOpenData={tableData ? undefined : (schema, table, from) => {
              if (from !== connectionKey(connection)) {
                setError("The connection changed. Refresh the schema browser before opening table data.");
                return;
              }
              // A second click cannot replace a draft before the grid's own
              // discard confirmation has called onClose.
              setTableData(current => current || { connection: { ...connection }, schema, table });
            }}
          />
        </div>
        <div className="editor-panel card">
          <div className="query-tabs">
            <div className="query-mode-switch segmented-control" role="tablist" aria-label="Query workspace view">
              <button
                type="button"
                role="tab"
                aria-selected={workspaceMode === "editor"}
                className={`mode-tab ${workspaceMode === "editor" ? "is-active" : ""}`}
                onClick={() => setWorkspaceMode("editor")}
              >
                <Icon name="query" size={13} />
                <span>SQL Editor</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={workspaceMode === "digest"}
                className={`mode-tab ${workspaceMode === "digest" ? "is-active" : ""}`}
                onClick={() => setWorkspaceMode("digest")}
                title="pt-query-digest workload analysis report and slow query inspector"
              >
                <Icon name="statements" size={13} />
                <span>Query Digest (pt-query-digest)</span>
              </button>
            </div>
            {workspaceMode === "editor" && (
              <>
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
                          autoFocus onFocus={(e) => e.currentTarget.select()}
                          onChange={(e) => setRenaming({ id: t.id, name: e.target.value })}
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
                        title="Double-click or press F2 to rename"
                        onDoubleClick={() => setRenaming({ id: t.id, name: t.name })}
                        onClick={() => setTabId(t.id)}
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
                        {t.name}
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
              </>
            )}
            <div className="editor-connection" style={workspaceMode === "digest" ? { marginLeft: "auto" } : undefined}>{picker}</div>
          </div>
          {workspaceMode === "digest" && (
            <div style={{ flex: 1, minHeight: 0, display: "flex", overflow: "hidden" }}>
              <QueryDigestView
                connection={connection}
                onOpenInEditor={(row, rank, from) => {
                  openDigest(row, rank, "open", from);
                  setWorkspaceMode("editor");
                }}
                onExplain={(row, rank, from) => {
                  openDigest(row, rank, "explain", from);
                  setWorkspaceMode("editor");
                }}
              />
            </div>
          )}
          <div
            role="tabpanel" tabIndex={0}
            id="query-tab-panel"
            className="query-tab-panel"
            hidden={workspaceMode !== "editor"}
            aria-labelledby={`query-tab-${Math.max(0, tabs.indexOf(tab))}`}
            onKeyDown={(e) => {
              // Escape that ends an input method's composition is not Stop.
              if (e.key === "Escape" && !e.nativeEvent.isComposing && e.keyCode !== 229 && rt.running && !pending && !closing) {
                e.preventDefault();
                void stop();
              }
            }}
          >
            <div className="editor-toolbar" role="toolbar" aria-label="Run and settings">
              <div className="toolbar-group run-actions">
                <EditorSplitButton
                  label="Run options"
                  open={runOptionsOpen}
                  onOpen={setRunOptionsOpen}
                  action={
                    <ToolbarButton
                      icon="run"
                      primary={!runGate.disabledReason}
                      className="privilege-action"
                      data-action="run"
                      disabled={running}
                      data-privilege-state={runGate.state}
                      aria-keyshortcuts={ctrl(false, "Enter")}
                      {...privilegeButtonProps(runGate, () => runSql(false), "", privilegeNoticeId || `${privilegeId}-run`)}
                    >
                      {selecting ? "Run selection" : "Run"}
                    </ToolbarButton>
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
                    <Checkbox
                      className="safe-updates"
                      checked={!!safeChecked}
                      disabled={running}
                      onChange={setSafeUpdates}
                      title="sql_safe_updates for this tab's session: the server refuses UPDATE or DELETE without a key in WHERE or a LIMIT. Turning it off on a Production connection asks first."
                    >
                      Safe updates
                    </Checkbox>
                  </div>
                </EditorSplitButton>
                <ToolbarButton
                  icon="runAll"
                  data-action="run-all"
                  className="privilege-action"
                  disabled={running}
                  data-privilege-state={allGate.state}
                  aria-keyshortcuts={ctrl(true, "Enter")}
                  {...privilegeButtonProps(allGate, () => runSql(true), "", privilegeNoticeId || `${privilegeId}-run-all`)}
                >
                  Run all
                </ToolbarButton>
                <ToolbarButton
                  icon="stop"
                  data-action="stop"
                  disabled={!running || rt.stopping}
                  onClick={() => void stop()}
                  title="Stop the running statement with KILL QUERY (Escape); an open transaction stays open"
                >
                  {rt.stopping ? "Stopping…" : "Stop"}
                </ToolbarButton>
              </div>
              {!privilegeNotice && <>
                <span id={`${privilegeId}-run`} className="account-sr">{runGate.reason}</span>
                <span id={`${privilegeId}-run-all`} className="account-sr">{allGate.reason}</span>
                <span id={`${privilegeId}-explain`} className="account-sr">{explainGate.reason}</span>
                <span id={`${privilegeId}-analysis`} className="account-sr">{analysisGate.reason}</span>
              </>}
              <div className="editor-actions toolbar-group">
                <button
                  className="button"
                  data-action="explain"
                  disabled={busy}
                  title={`Explain ${scopePhrase} (${keys(false, "E")})`}
                  aria-keyshortcuts={ctrl(false, "E")}
                  data-privilege-state={explainGate.state}
                  {...privilegeButtonProps(explainGate, () => void capture("explain"), "", `${privilegeNoticeId || `${privilegeId}-explain`}${scopeLabel ? " run-scope" : ""}`)}
                >
                  {selecting ? "Explain selection" : "Explain"}
                </button>
                <EditorSplitButton
                  label="Explain and Run analysis options"
                  open={showAnalysisOptions}
                  onOpen={toggleAnalysisOptions}
                  action={
                    <button
                      className="button"
                      data-action="analyze"
                      disabled={busy}
                      title={`Run analysis on ${scopePhrase} (${keys(true, "E")}): it executes the SELECT and captures measured timings`}
                      aria-keyshortcuts={ctrl(true, "E")}
                      data-privilege-state={analysisGate.state}
                      {...privilegeButtonProps(analysisGate, () => void capture("analyze"), "", `${privilegeNoticeId || `${privilegeId}-analysis`}${scopeLabel ? " run-scope" : ""}`)}
                    >
                      {selecting ? "Analyze selection" : "Run analysis"}
                    </button>
                  }
                >
                  <div className="execution-options plan-options" aria-label="Explain and Run analysis settings" role="group">
                    <span className="plan-options-label">Explain and Run analysis</span>
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
                        Use :name in SQL, for example WHERE id = :id with {`{"id": 42}`}.
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
              <div className="toolbar-group tx-group" aria-label="Transaction" hidden={!txOpen}>
                {txOpen && (
                  <>
                    <button type="button" className="button small" data-action="commit" disabled={running} onClick={() => commit()}>
                      Commit
                    </button>
                    <button type="button" className="button small" data-action="rollback" disabled={running} onClick={() => rollback()}>
                      Roll back
                    </button>
                  </>
                )}
              </div>
              <div className="toolbar-group pane-toggles">
                <IconButton
                  icon="browser"
                  label={showBrowser ? "Hide the schema browser" : "Show the schema browser"}
                  aria-pressed={showBrowser}
                  onClick={() => togglePane("browser")}
                />
                <IconButton
                  icon="history"
                  label={showHistory ? "Hide the run history" : "Show the run history"}
                  aria-pressed={showHistory}
                  onClick={() => togglePane("history")}
                />
                <InfoPopover label="Query editor shortcuts">
                  <p>
                    {keys(false)} runs the selection or the statement at the cursor; {keys(true)} runs every
                    statement. {keys(false, "E")} explains; {keys(true, "E")} runs analysis. Escape stops a run.
                    Ctrl+Space opens SQL suggestions; Ctrl+Shift+Space also works when the system reserves Ctrl+Space.
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
                  <StatusPill
                    tone={tx[1] === "Auto-commit" && session.autocommit === null ? "unknown" : tx[0]}
                    label={tx[1] === "Auto-commit" && session.autocommit === null ? "Autocommit unknown" : tx[1]}
                  />
                </span>
              )}
            </div>
            {privilegeNotice && <p id={privilegeNoticeId}
              className={`editor-privilege-note${previewWarning || privilegeReasons.length ? " is-warning" : ""}`}
              role="status">{privilegeNotice}</p>}
            {tab.source && (
              <p className="hint tab-source">
                From Top statements, digest{" "}
                <code title={tab.source.digest}>
                  {shortDigest(tab.source.digest)}
                </code>
                {tabSchema &&
                  (connection.database
                    ? ` · runs in ${tabSchema}, the connection's schema`
                    : ` · runs in ${tabSchema}, the schema it was recorded in`)}
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
              <p className="notice fill-notice" role="status">
                {tab.fill_note ||
                  `Fill in ${listNames(pendingFill)} under Parameters with real values, then run Explain.`}
              </p>
            )}
            {mismatch && session && (
              <div className="editor-banner is-warn" data-banner="mismatch">
                <span>
                  This tab's session is on {sessionName(session)}, not {connectionLabel(connection)}. Runs wait until you
                  close it{session.in_transaction !== false ? "; its transaction is open or unknown" : ""}.
                </span>
                <button type="button" className="button small" onClick={() => void requestClose(tab.id, true)}>
                  Close session
                </button>
              </div>
            )}
            {session?.ended && (
              <div className="editor-banner is-danger" data-banner="ended">
                <span>
                  Session ended{session.ended_reason ? `: ${ENDED[session.ended_reason] || session.ended_reason}` : ""}. Session settings
                  were lost and any open transaction was rolled back.
                </span>
                <button type="button" className="button small" onClick={() => reconnect()}>
                  Reconnect
                </button>
              </div>
            )}
            {batchRunning && (
              <p className="hint">
                Explain and Run analysis wait while Explain top N runs on Server.
              </p>
            )}
            {analysisWarning?.tab === tab.id && (
              <p role="alert" className="notice">{analysisWarning.text}</p>
            )}
            {status && (
              <p role="status" className="notice">
                {status}
              </p>
            )}
            {error && (
              <p role="alert" className="error">
                {error}
              </p>
            )}
            <SqlEditor
              ref={editor}
              value={tab.sql}
              completionConnection={connectionFor(tab)}
              completionScope={tab.id}
              backslashEscapes={rt.session?.backslash_escapes ?? true}
              onChange={(sql) => changeTab({ sql })}
              onSelect={syncCursor}
              caret={cursor}
              statement={statementMark}
              flash={lit}
              error={errorMark}
              height={layout.height}
              describedBy={scopeLabel ? "run-scope" : undefined}
              placeholder="Write SQL. Run sends the statement at the cursor."
              onKeyDown={(e) => {
                if (e.nativeEvent.isComposing) return;
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
            <div
              className="editor-splitter"
              role="separator"
              aria-orientation="horizontal"
              aria-label="Height of the SQL editor"
              aria-valuemin={120}
              aria-valuemax={2000}
              aria-valuenow={layout.height}
              tabIndex={0}
              title="Drag to resize the editor and the results"
              onPointerDown={(e) => {
                e.preventDefault();
                const from = layout.height;
                const y = e.clientY;
                const handle = e.currentTarget;
                handle.setPointerCapture?.(e.pointerId);
                const move = (m: PointerEvent) => setHeight(from + m.clientY - y);
                const up = () => {
                  handle.removeEventListener("pointermove", move);
                  handle.removeEventListener("pointerup", up);
                };
                handle.addEventListener("pointermove", move);
                handle.addEventListener("pointerup", up);
              }}
              onKeyDown={(e) => {
                const step = e.shiftKey ? 64 : 16;
                if (e.key === "ArrowDown") setHeight(layout.height + step);
                else if (e.key === "ArrowUp") setHeight(layout.height - step);
                else return;
                e.preventDefault();
              }}
            />
            <ResultsPane
              run={rt.run}
              running={running}
              base={0}
              stale={stale}
              selected={tab.result_tab || ""}
              onSelect={(key) => changeTab({ result_tab: key })}
              engine={session?.engine || null}
              plan={planPane}
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
        {showHistory && <HistoryPanel refresh={historyTick} onInsert={insertAtCursor} onClose={() => togglePane("history")} />}
      </div>
      {pending && (
        <ConfirmSheet
          className="editor-confirm"
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
                {pending.confirmation.connection.name} · {pending.confirmation.connection.user}@{pending.confirmation.connection.host}:
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
            return <section aria-label="ALTER TABLE preflight">
              <dl className="sheet-facts">
                <div><dt>Table</dt><dd>{review.schema || "(no database)"}.{review.table} · {review.table_engine || "engine unknown"}</dd></div>
                <div><dt>Server</dt><dd>{review.engine} {review.version}</dd></div>
                <div><dt>Approximate size</dt><dd>{review.approx_bytes == null ? "Unknown" : `${review.approx_bytes.toLocaleString()} bytes (data and indexes)`}</dd></div>
                <div><dt>Metadata locks</dt><dd>{review.metadata_known ? `${review.metadata_locks.length} observed` : "Unknown"}{review.truncated ? " (incomplete snapshot)" : ""}</dd></div>
                <div><dt>Open transactions on table</dt><dd>{review.metadata_known && review.transactions_known ? `${review.transactions.length} observed` : "Unknown"}</dd></div>
                <div><dt>Lock wait limit</dt><dd>5 seconds per metadata-lock wait; retained in this tab's session</dd></div>
              </dl>
              <details><summary>Lock and transaction snapshot</summary>
                <ul>
                  {review.metadata_locks.map((lock, i) => <li key={`m${i}`}>Connection {lock.connection_id ?? "unknown"}: {lock.lock_type}, {lock.status}</li>)}
                  {review.transactions.map((trx, i) => <li key={`t${i}`}>Connection {trx.connection_id ?? "unknown"}: transaction open {trx.age_s ?? "?"} seconds{trx.query ? `; ${trx.query}` : ""}</li>)}
                  {review.sessions?.map((session, i) => <li key={`s${i}`}>Connection {session.connection_id ?? "unknown"}: {session.query || "No current statement reported"}</li>)}
                </ul>
                <p>{review.unattributed_transactions?.length || 0} other open transactions; table ownership not established. This snapshot cannot rule out new or uninstrumented locks. Check Server → Locks while ALTER runs.</p>
              </details>
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
    </section>
  );
});
