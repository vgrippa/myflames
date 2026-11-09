// One pane of the File transfer browser: a folder on this computer or on the
// SSH connection, with its path bar, history, filter, sortable grid,
// Finder-style selection and keys, drag and drop, and the row menu. usePane
// owns the listing requests (latest wins, one request per folder at a time,
// Stop) and the selection; FileBrowser decides what transfers, renames and
// deletes do.
import {
  Fragment,
  createElement,
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type MouseEvent,
  type MutableRefObject,
} from "react";
import { Icon } from "./components";
import { fileGlyphs } from "./fileGlyphs.generated";
import {
  ActionButton,
  ActionStatus,
  ContextMenu,
  IconButton,
  Notice,
  SearchField,
  ViewStatus,
  errorText,
  useAction,
  type TableContextAction,
} from "./kit";
import { bytes } from "./PerfShared";
import { MAC, WINDOWS, shortcut } from "./platform";
import { chooseBackupDirectory } from "./opsApi";
import {
  baseName,
  joinPath,
  localList,
  localReveal,
  parentPath,
  remoteList,
  sshErrorCode,
  type FileEntry,
  type SshErrorCode,
} from "./filesApi";

export type Side = "local" | "remote";
export type Listing = { path: string; home: string; entries: FileEntry[]; transfers?: boolean };
export const REVEAL_LABEL = MAC ? "Reveal in Finder" : WINDOWS ? "Show in File Explorer" : "Show in folder";
const REVEAL_PENDING = MAC ? "Opening in Finder…" : WINDOWS ? "Opening in File Explorer…" : "Opening folder…";
const revealDone = (shown: string) =>
  MAC ? `Revealed ${shown} in Finder.` : WINDOWS ? `Shown in File Explorer: ${shown}.` : `Opened folder for ${shown}.`;
export type PaneState = {
  /** The last folder listed; kept (greyed) while a newer listing fails. */
  listing: Listing | null;
  /** The folder being listed, or the last one asked for (Try again). */
  target: string;
  loading: boolean;
  /** Date.now() when the running listing was sent. */
  since: number | null;
  error: string;
  code: SshErrorCode | "";
  /** Counts failures, so the same error after Try again shows again. */
  failures: number;
  back: string[];
  forward: string[];
};
export type PaneSelection = {
  names: ReadonlySet<string>;
  anchor: string;
  active: string;
  /** Bumped when the active row must scroll into view (the row chosen after a listing). */
  reveal: number;
};
export type ListResult = { ok: boolean; code: SshErrorCode | ""; replaced: boolean };
type ListOptions = { select?: string[]; force?: boolean; history?: "push" | "back" | "forward" | "none" };
type Flight = { path: string; controller: AbortController; select?: string[]; promise: Promise<ListResult> };

const INITIAL: PaneState = {
  listing: null, target: "", loading: false, since: null, error: "", code: "", failures: 0, back: [], forward: [],
};
const NO_SELECTION: PaneSelection = { names: new Set(), anchor: "", active: "", reveal: 0 };
const REPLACED: ListResult = { ok: false, code: "", replaced: true };
const HISTORY_MAX = 50;

/** Drag payload of pane rows (OS files carry the Files type instead). */
export const ROWS_MIME = "application/x-myflames-files";
export type DragRows = { ssh: string; side: Side; dir: string; names: string[] };

export const folderKey = (ssh: string, side: Side) => `myflames.files.${ssh}.${side}`;
export function storedFolder(ssh: string, side: Side): string | null {
  try {
    return localStorage.getItem(folderKey(ssh, side));
  } catch {
    return null;
  }
}
function rememberFolder(ssh: string, side: Side, path: string) {
  try {
    localStorage.setItem(folderKey(ssh, side), path);
  } catch {
    /* Remembering the folder is a convenience; the pane works without it. */
  }
}

/** One pane's listing and selection. *onHostKeyUnknown* gets a retry that
 * lists the same folder again once the host is trusted. */
export function usePane(side: Side, ssh: string, onHostKeyUnknown?: (retry: () => void) => void) {
  const [state, setState] = useState<PaneState>(INITIAL);
  const [selection, setSelection] = useState<PaneSelection>(NO_SELECTION);
  const flight = useRef<Flight | null>(null);
  const current = useRef(state);
  current.current = state;
  const hostKey = useRef(onHostKeyUnknown);
  hostKey.current = onHostKeyUnknown;
  useEffect(() => () => {
    flight.current?.controller.abort();
    flight.current = null;
  }, []);

  const listRef = useRef<(path: string, options?: ListOptions) => Promise<ListResult>>(() => Promise.resolve(REPLACED));
  const list = useCallback((path: string, options: ListOptions = {}): Promise<ListResult> => {
    const running = flight.current;
    // A second click on the same folder joins the request already out.
    if (running && running.path === path && !options.force) {
      if (options.select) running.select = options.select;
      return running.promise;
    }
    running?.controller.abort();
    const before = current.current;
    const from = before.listing?.path ?? null;
    const how = options.history ?? "push";
    const history =
      how === "back" ? { back: before.back.slice(0, -1), forward: from !== null ? [...before.forward, from] : before.forward }
      : how === "forward" ? { back: from !== null ? [...before.back, from] : before.back, forward: before.forward.slice(0, -1) }
      : how === "push" ? { back: from !== null ? [...before.back, from].slice(-HISTORY_MAX) : before.back, forward: [] as string[] }
      : null;
    const controller = new AbortController();
    const entry: Flight = { path, controller, select: options.select, promise: Promise.resolve(REPLACED) };
    flight.current = entry;
    setState((s) => ({ ...s, loading: true, since: Date.now(), target: path }));
    entry.promise = (async (): Promise<ListResult> => {
      try {
        const answer = side === "remote"
          ? await remoteList(ssh, path, controller.signal)
          : await localList(path, controller.signal);
        if (flight.current !== entry) return REPLACED;
        flight.current = null;
        const listing: Listing = { path: answer.path, home: answer.home, entries: answer.entries || [], transfers: answer.transfers };
        const same = listing.path === current.current.listing?.path;
        setState((s) => ({
          ...s,
          ...(history && !(how === "push" && (same || from === null)) ? history : {}),
          listing,
          target: listing.path,
          loading: false,
          since: null,
          error: "",
          code: "",
        }));
        const names = new Set(listing.entries.map((e) => e.name));
        const wanted = entry.select;
        setSelection((sel) => {
          if (wanted) {
            const keep = wanted.filter((n) => names.has(n));
            return { names: new Set(keep), anchor: keep[0] || "", active: keep[0] || "", reveal: sel.reveal + 1 };
          }
          if (!same) return { ...NO_SELECTION, reveal: sel.reveal };
          // A refresh keeps the names that still exist.
          const keep = [...sel.names].filter((n) => names.has(n));
          return {
            names: new Set(keep),
            anchor: names.has(sel.anchor) ? sel.anchor : keep[0] || "",
            active: names.has(sel.active) ? sel.active : keep[0] || "",
            reveal: sel.reveal,
          };
        });
        rememberFolder(ssh, side, listing.path);
        return { ok: true, code: "", replaced: false };
      } catch (error) {
        if (flight.current !== entry) return REPLACED;
        flight.current = null;
        const code = sshErrorCode(error);
        setState((s) => ({ ...s, loading: false, since: null, error: errorText(error), code, failures: s.failures + 1 }));
        if (code === "host_key_unknown") hostKey.current?.(() => void listRef.current(path, { force: true, history: how }));
        return { ok: false, code, replaced: false };
      }
    })();
    return entry.promise;
  }, [side, ssh]);
  listRef.current = list;

  /** Lists the folder on screen again (or joins the listing already out). */
  const refresh = useCallback((options: { select?: string[] } = {}) => {
    const s = current.current;
    const path = flight.current?.path ?? s.listing?.path ?? s.target;
    return list(path, { select: options.select, force: !!options.select, history: "none" });
  }, [list]);
  const retry = useCallback(() => void list(current.current.target, { force: true }), [list]);
  /** Stop waiting: the request is aborted and the last listing stays. */
  const stop = useCallback(() => {
    const running = flight.current;
    if (!running) return;
    flight.current = null;
    running.controller.abort();
    setState((s) => ({ ...s, loading: false, since: null, target: s.listing?.path ?? s.target }));
  }, []);
  const back = useCallback(() => {
    const s = current.current;
    if (s.back.length) void list(s.back[s.back.length - 1], { history: "back" });
  }, [list]);
  const forward = useCallback(() => {
    const s = current.current;
    if (s.forward.length) void list(s.forward[s.forward.length - 1], { history: "forward" });
  }, [list]);
  return { side, ssh, state, selection, select: setSelection, list, refresh, retry, stop, back, forward };
}
export type PaneModel = ReturnType<typeof usePane>;

// ---- Formatting ----------------------------------------------------------------

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const dateTime = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const fullDate = new Intl.DateTimeFormat(undefined, { dateStyle: "full", timeStyle: "medium" });

export function mtimeText(mtime: number | null): string {
  return mtime === null || !Number.isFinite(mtime) ? "—" : dateTime.format(new Date(mtime * 1000));
}
function mtimeTitle(mtime: number | null): string | undefined {
  return mtime === null || !Number.isFinite(mtime) ? undefined : fullDate.format(new Date(mtime * 1000));
}

/** st_mode as `ls -l` shows it: type, then rwx for owner, group and others. */
export function permissions(mode: number | null): string {
  if (mode === null || !Number.isFinite(mode)) return "";
  const type = mode & 0o170000;
  const kind = type === 0o040000 ? "d" : type === 0o120000 ? "l" : type === 0o020000 ? "c" : type === 0o060000 ? "b"
    : type === 0o010000 ? "p" : type === 0o140000 ? "s" : "-";
  const bits = "rwxrwxrwx".split("").map((c, i) => (mode & (1 << (8 - i)) ? c : "-"));
  if (mode & 0o4000) bits[2] = bits[2] === "x" ? "s" : "S";
  if (mode & 0o2000) bits[5] = bits[5] === "x" ? "s" : "S";
  if (mode & 0o1000) bits[8] = bits[8] === "x" ? "t" : "T";
  return kind + bits.join("");
}

/** The part of a name that Rename selects: up to the last dot, unless the
 * name is a folder or only starts with a dot. */
export function nameStem(name: string, isDir: boolean): number {
  const dot = name.lastIndexOf(".");
  return isDir || dot <= 0 ? name.length : dot;
}

export const itemCount = (n: number) => `${n.toLocaleString()} ${n === 1 ? "item" : "items"}`;

type SortKey = "name" | "size" | "mtime" | "perm";
type Sort = { key: SortKey; dir: "asc" | "desc" };
const COLUMNS: [SortKey, string, string][] = [
  ["name", "Name", "files-cell-name"],
  ["size", "Size", "files-cell-size"],
  ["mtime", "Modified", "files-cell-mtime"],
  ["perm", "Permissions", "files-cell-perm"],
];

/** Folders first, then the chosen column; ties by name. */
function sortEntries(entries: FileEntry[], sort: Sort): FileEntry[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  const value = (e: FileEntry): string | number | null =>
    sort.key === "size" ? (e.is_dir ? null : e.size)
    : sort.key === "mtime" ? e.mtime
    : sort.key === "perm" ? (e.mode === null ? null : permissions(e.mode))
    : null;
  const keyed = entries.map((entry) => ({ entry, key: sort.key === "name" ? null : value(entry) }));
  keyed.sort((a, b) => {
    if (a.entry.is_dir !== b.entry.is_dir) return a.entry.is_dir ? -1 : 1;
    if (sort.key !== "name" && a.key !== b.key) {
      if (a.key === null) return 1;
      if (b.key === null) return -1;
      return (a.key < b.key ? -1 : 1) * sign;
    }
    return collator.compare(a.entry.name, b.entry.name) * (sort.key === "name" ? sign : 1);
  });
  return keyed.map((k) => k.entry);
}

/** Path bar segments: "/" then each folder (POSIX), or "C:\" then each folder. */
function crumbs(path: string): { label: string; path: string }[] {
  if (!path) return [];
  const windows = /^[A-Za-z]:[\\/]?/.test(path) || (path.includes("\\") && !path.includes("/"));
  if (windows) {
    const unc = path.match(/^\\\\[^\\]+\\[^\\]+\\?/);
    const drive = path.match(/^[A-Za-z]:/);
    const root = unc ? unc[0].replace(/\\?$/, "\\") : drive ? `${drive[0]}\\` : "\\";
    const out = [{ label: root, path: root }];
    let acc = root;
    for (const part of path.slice(root.length).split(/[\\/]+/).filter(Boolean)) {
      acc = joinPath(acc, part);
      out.push({ label: part, path: acc });
    }
    return out;
  }
  const out = [{ label: "/", path: "/" }];
  let acc = "";
  for (const part of path.split("/").filter(Boolean)) {
    acc += `/${part}`;
    out.push({ label: part, path: acc });
  }
  return out;
}
const isRoot = (path: string) => path === "/" || /^[A-Za-z]:\\?$/.test(path) || /^\\\\[^\\]+\\[^\\]+\\?$/.test(path);

const LINK_TITLE = "Symbolic link: opens if it points to a folder; skipped in transfers";
function entryGlyph(entry: Pick<FileEntry, "kind" | "is_dir">): FileGlyphName {
  if (entry.kind === "link") return entry.is_dir ? "folder-link" : "file-link";
  return entry.is_dir ? "folder" : "file";
}
export type FileGlyphName = keyof typeof fileGlyphs;
/** A file or folder glyph, drawn like components.tsx Icon at 16 px. */
export function FileGlyph({ entry }: { entry: Pick<FileEntry, "kind" | "is_dir"> }) {
  return (
    <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.75}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {fileGlyphs[entryGlyph(entry)].map(([tag, attrs], i) => createElement(tag, { key: i, ...attrs }))}
    </svg>
  );
}

// ---- Rows ------------------------------------------------------------------------

const OVERSCAN = 12;

const FileRow = memo(function FileRow({ entry, index, rowId, selected, active, remote }: {
  entry: FileEntry;
  index: number;
  rowId: string;
  selected: boolean;
  active: boolean;
  remote: boolean;
}) {
  const link = entry.kind === "link";
  return (
    <div
      role="row"
      id={rowId}
      aria-rowindex={index + 2}
      aria-selected={selected}
      data-name={entry.name}
      draggable
      className={`files-row${selected ? " is-selected" : ""}${active ? " is-active" : ""}${entry.is_dir ? " is-dir" : ""}`}
    >
      <div role="gridcell" className="files-cell-name" title={link ? `${entry.name}\n${LINK_TITLE}` : entry.name}>
        <span className={`files-entry-icon${link ? " is-link" : ""}`}>
          <FileGlyph entry={entry} />
        </span>
        <bdi className="files-name-text">{entry.name}</bdi>
        {link && <span className="visually-hidden">, symbolic link</span>}
      </div>
      <div role="gridcell" className="files-cell-size">{entry.is_dir ? "—" : bytes(entry.size)}</div>
      <div role="gridcell" className="files-cell-mtime" title={mtimeTitle(entry.mtime)}>{mtimeText(entry.mtime)}</div>
      {remote && <div role="gridcell" className="files-cell-perm">{permissions(entry.mode)}</div>}
      <div role="gridcell" className="files-cell-more">
        <button type="button" className="files-row-more" tabIndex={-1} data-more aria-label={`Actions for ${entry.name}`} title="Actions">
          <Icon name="more" size={16} />
        </button>
      </div>
    </div>
  );
});

// ---- Path bar --------------------------------------------------------------------

function PathBar({ path, title, editing, onEditing, onList, onDone }: {
  path: string;
  title: string;
  editing: boolean;
  onEditing: (editing: boolean) => void;
  onList: (path: string) => void;
  /** Focus goes back to the listing. */
  onDone: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const strip = useRef<HTMLDivElement>(null);
  // The field starts with the path on screen, all selected; a listing that
  // answers while typing never replaces the typed text (uncontrolled).
  useLayoutEffect(() => {
    if (!editing) return;
    input.current?.focus();
    input.current?.select();
  }, [editing]);
  // Long paths keep their end (the folder on screen) in view, including after
  // the strip changes width (for example switching between 720 px and 1440 px).
  useLayoutEffect(() => {
    const el = strip.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [path, editing]);
  useLayoutEffect(() => {
    const el = strip.current;
    if (!el || editing || typeof ResizeObserver === "undefined") return;
    let lastWidth = el.clientWidth;
    const observer = new ResizeObserver(() => {
      if (el.clientWidth !== lastWidth) {
        lastWidth = el.clientWidth;
        el.scrollLeft = el.scrollWidth;
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, [editing]);
  if (editing) {
    return (
      <div className="files-path is-editing">
        <input
          ref={input}
          className="files-path-input"
          aria-label={`${title} path`}
          defaultValue={path}
          spellCheck={false}
          autoCapitalize="off"
          autoComplete="off"
          onBlur={() => onEditing(false)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              onList(e.currentTarget.value.trim());
              onEditing(false);
              onDone();
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              onEditing(false);
              onDone();
            }
          }}
        />
      </div>
    );
  }
  const parts = crumbs(path);
  return (
    <div className="files-path">
      <div ref={strip} className="files-crumbs" role="navigation" aria-label={`${title} path`} data-path={path}>
        {parts.map((part, i) => {
          const current = i === parts.length - 1;
          return (
            <Fragment key={part.path}>
              {i > 1 && <span className="files-crumb-sep" aria-hidden="true"><Icon name="chevron" size={12} /></span>}
              <span className={`files-crumb${current ? " is-current" : ""}`}>
                <button
                  type="button"
                  className="files-crumb-button"
                  aria-current={current ? "location" : undefined}
                  title={part.path}
                  onClick={() => onList(part.path)}
                >
                  <bdi>{part.label}</bdi>
                </button>
              </span>
            </Fragment>
          );
        })}
        <button type="button" className="files-path-end" tabIndex={-1} aria-hidden="true" onClick={() => onEditing(true)} />
      </div>
      <IconButton icon="edit" size={14} label="Edit path" title={`Edit path (${shortcut("L")})`} onClick={() => onEditing(true)} />
    </div>
  );
}

// ---- The pane --------------------------------------------------------------------

export type PaneTransfer = {
  verb: "Upload" | "Download";
  /** The other pane's folder ("" while it has none). */
  target: string;
  /** Set when transfers are unavailable on this platform (Windows). */
  blocked?: string;
  busy: boolean;
  start: (names: string[]) => void;
};

export function FilePane({
  model,
  title,
  active,
  onActivate,
  showHidden,
  dragging,
  transfer,
  onNewFolder,
  onRename,
  onDelete,
  onDropRows,
  onDropFiles,
  onCopyPath,
  onTrustHost,
  onEditConnection,
}: {
  model: PaneModel;
  title: string;
  active: boolean;
  onActivate: () => void;
  showHidden: boolean;
  /** The rows being dragged out of either pane (dragover cannot read the payload). */
  dragging: MutableRefObject<DragRows | null>;
  transfer: PaneTransfer;
  onNewFolder: () => void;
  onRename: (entry: FileEntry) => void;
  /** Remote only: delete these names in the folder on screen. */
  onDelete?: (names: string[]) => void;
  onDropRows: (rows: DragRows) => void;
  onDropFiles: (files: File[]) => void;
  onCopyPath: (paths: string[]) => void;
  onTrustHost?: () => void;
  onEditConnection?: () => void;
}) {
  const { side, state, selection, select } = model;
  const listing = state.listing;
  const shownPath = listing?.path ?? "";
  const remote = side === "remote";
  const gridId = useId();
  const scroller = useRef<HTMLDivElement>(null);
  const head = useRef<HTMLDivElement>(null);
  const [sort, setSort] = useState<Sort>({ key: "name", dir: "asc" });
  // The filter belongs to the folder it was typed in: another folder starts unfiltered.
  const [filter, setFilter] = useState({ text: "", path: "" });
  const needle = filter.path === shownPath ? filter.text : "";
  const [editing, setEditing] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number; targets: string[]; focus?: string } | null>(null);
  const [dropping, setDropping] = useState(false);
  const depth = useRef(0);
  const collapse = useRef<string | null>(null);
  const typed = useRef({ text: "", at: 0 });
  const [rowH, setRowH] = useState(28);
  const [view, setView] = useState({ first: 0, capacity: 30 });

  const entries = listing?.entries;
  const byName = useMemo(() => new Map((entries || []).map((e) => [e.name, e])), [entries]);
  const rows = useMemo(() => {
    const text = needle.trim().toLowerCase();
    const shown = (entries || []).filter((e) => (showHidden || !e.name.startsWith(".")) && (!text || e.name.toLowerCase().includes(text)));
    return sortEntries(shown, sort);
  }, [entries, showHidden, needle, sort]);
  const indexOf = useMemo(() => new Map(rows.map((e, i) => [e.name, i])), [rows]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  const ordered = useCallback(
    (names: ReadonlySet<string>) => rowsRef.current.filter((e) => names.has(e.name)).map((e) => e.name),
    [],
  );

  // A filter or the hidden-files switch drops selected rows it hides, so an
  // action never touches what is not on screen.
  useEffect(() => {
    select((sel) => {
      const visible = new Set(rowsRef.current.map((e) => e.name));
      if ([...sel.names].every((n) => visible.has(n))) return sel;
      const keep = [...sel.names].filter((n) => visible.has(n));
      return { ...sel, names: new Set(keep), anchor: visible.has(sel.anchor) ? sel.anchor : keep[0] || "", active: visible.has(sel.active) ? sel.active : keep[0] || "" };
    });
  }, [needle, showHidden, select]);

  // Row height follows the control height token (taller on touch-size windows).
  const measure = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const h = parseFloat(getComputedStyle(el).getPropertyValue("--files-row-h"));
    const height = h > 0 ? h : 28;
    setRowH(height);
    setView((v) => {
      const capacity = Math.ceil(el.clientHeight / height) + 1;
      return v.capacity === capacity ? v : { ...v, capacity };
    });
  }, []);
  useLayoutEffect(() => {
    measure();
    const el = scroller.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => measure());
    observer.observe(el);
    return () => observer.disconnect();
  }, [measure]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const first = Math.max(0, Math.floor((el.scrollTop - (head.current?.offsetHeight || 0)) / rowH));
    setView((v) => (v.first === first ? v : { ...v, first }));
  };
  // Another folder starts at the top; a refresh of the same folder keeps its place.
  const lastPath = useRef(shownPath);
  useLayoutEffect(() => {
    if (lastPath.current === shownPath) return;
    lastPath.current = shownPath;
    if (scroller.current) scroller.current.scrollTop = 0;
    setView((v) => ({ ...v, first: 0 }));
  }, [shownPath]);

  const scrollToIndex = useCallback((index: number) => {
    const el = scroller.current;
    if (!el || index < 0) return;
    const headH = head.current?.offsetHeight || 0;
    const top = headH + index * rowH;
    if (top < el.scrollTop + headH) el.scrollTop = top - headH;
    else if (top + rowH > el.scrollTop + el.clientHeight) el.scrollTop = top + rowH - el.clientHeight;
    const first = Math.max(0, Math.floor((el.scrollTop - headH) / rowH));
    setView((v) => (v.first === first ? v : { ...v, first }));
  }, [rowH]);
  useLayoutEffect(() => {
    if (selection.reveal) scrollToIndex(indexOf.get(selection.active) ?? -1);
    // Only a new reveal request scrolls; later row changes keep the user's place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selection.reveal]);

  const focusGrid = useCallback(() => scroller.current?.focus({ preventScroll: true }), []);

  // ---- Selection --------------------------------------------------------------
  const toggleKey = (e: { metaKey: boolean; ctrlKey: boolean }) => (MAC ? e.metaKey : e.ctrlKey);
  const selectOne = (name: string) =>
    select((sel) => ({ names: new Set([name]), anchor: name, active: name, reveal: sel.reveal }));
  const range = (from: string, to: string) => {
    const a = indexOf.get(from) ?? indexOf.get(to) ?? 0;
    const b = indexOf.get(to) ?? 0;
    return rows.slice(Math.min(a, b), Math.max(a, b) + 1).map((e) => e.name);
  };

  function open(name: string) {
    const entry = byName.get(name);
    if (!entry || !listing) return;
    if (entry.is_dir) void model.list(joinPath(listing.path, name));
    else if (!transfer.blocked) transfer.start([name]);
  }
  function goUp() {
    if (!listing || isRoot(listing.path)) return;
    // Like Finder, the folder you came from is selected in its parent.
    void model.list(parentPath(listing.path), { select: [baseName(listing.path)] });
  }
  function goHome() {
    void model.list(listing?.home || "");
  }
  function deleteSelection() {
    if (!onDelete) return;
    const names = ordered(selection.names);
    if (names.length) onDelete(names);
  }
  function renameActive() {
    const name = selection.names.has(selection.active) ? selection.active : selection.names.size === 1 ? [...selection.names][0] : "";
    const entry = byName.get(name);
    if (entry) onRename(entry);
  }
  function openMenuFor(targets: string[], x: number, y: number, focus?: string) {
    setMenu({ x, y, targets, focus });
  }
  function menuForActive() {
    const index = indexOf.get(selection.active) ?? -1;
    const row = index >= 0 ? scroller.current?.querySelector<HTMLElement>(`[aria-rowindex="${index + 2}"]`) : null;
    const box = (row || scroller.current)?.getBoundingClientRect();
    if (!box) return;
    openMenuFor(
      row ? ordered(selection.names.size ? selection.names : new Set([selection.active])) : [],
      box.left + 24,
      box.top + (row ? box.height : 8),
      row ? selection.active : undefined,
    );
  }

  function onKeyDownGrid(e: KeyboardEvent<HTMLDivElement>) {
    if (e.target !== e.currentTarget) return;
    e.currentTarget.setAttribute("data-keyboard", "");
    const mod = e.metaKey || e.ctrlKey;
    const key = e.key;
    if (mod && key.toLowerCase() === "a") {
      e.preventDefault();
      const all = rows.map((r) => r.name);
      select((sel) => ({ names: new Set(all), anchor: all[0] || "", active: sel.active && indexOf.has(sel.active) ? sel.active : all[0] || "", reveal: sel.reveal }));
      return;
    }
    if (mod && key === "ArrowUp") { e.preventDefault(); goUp(); return; }
    if (mod && key === "ArrowDown") { e.preventDefault(); if (selection.active) open(selection.active); return; }
    if (mod && key === "[") { e.preventDefault(); model.back(); return; }
    if (mod && key === "]") { e.preventDefault(); model.forward(); return; }
    if (key === "Enter") {
      e.preventDefault();
      if (selection.active && indexOf.has(selection.active)) open(selection.active);
      return;
    }
    if (key === "Backspace" && !mod) { e.preventDefault(); goUp(); return; }
    if (key === "Delete" || (key === "Backspace" && mod)) {
      if (onDelete) { e.preventDefault(); deleteSelection(); }
      return;
    }
    if (key === "F2") { e.preventDefault(); renameActive(); return; }
    if (key === "ContextMenu" || (key === "F10" && e.shiftKey)) { e.preventDefault(); menuForActive(); return; }
    if (["ArrowDown", "ArrowUp", "Home", "End", "PageDown", "PageUp"].includes(key) && !mod && !e.altKey) {
      e.preventDefault();
      const n = rows.length;
      if (!n) return;
      const at = indexOf.get(selection.active) ?? -1;
      const page = Math.max(1, view.capacity - 2);
      const next =
        key === "ArrowDown" ? (at < 0 ? 0 : Math.min(n - 1, at + 1))
        : key === "ArrowUp" ? (at < 0 ? n - 1 : Math.max(0, at - 1))
        : key === "Home" ? 0
        : key === "End" ? n - 1
        : key === "PageDown" ? Math.min(n - 1, Math.max(at, 0) + page)
        : Math.max(0, at - page);
      const name = rows[next].name;
      if (e.shiftKey) {
        const anchor = selection.anchor && indexOf.has(selection.anchor) ? selection.anchor : name;
        select((sel) => ({ names: new Set(range(anchor, name)), anchor, active: name, reveal: sel.reveal }));
      } else selectOne(name);
      scrollToIndex(next);
      return;
    }
    // Type the start of a name to jump to it.
    if (key.length === 1 && key !== " " && !mod && !e.altKey) {
      const now = Date.now();
      const text = (now - typed.current.at < 700 ? typed.current.text : "") + key.toLowerCase();
      typed.current = { text, at: now };
      const hit = rows.findIndex((r) => r.name.toLowerCase().startsWith(text));
      if (hit >= 0) {
        selectOne(rows[hit].name);
        scrollToIndex(hit);
      }
    }
  }

  function onMouseDown(e: MouseEvent<HTMLDivElement>) {
    e.currentTarget.removeAttribute("data-keyboard");
    if (e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest(".files-grid-head")) return;
    const row = target.closest<HTMLElement>("[data-name]");
    const name = row?.dataset.name ?? "";
    if (target.closest("[data-more]")) {
      // The row button keeps focus on the listing (it is not a tab stop).
      e.preventDefault();
      focusGrid();
      if (name && !selection.names.has(name)) selectOne(name);
      return;
    }
    if (!row) {
      // Empty space clears the selection; the scroll bar does not.
      const box = e.currentTarget.getBoundingClientRect();
      const onBar = e.clientX - box.left >= e.currentTarget.clientWidth || e.clientY - box.top >= e.currentTarget.clientHeight;
      if (!onBar && !e.shiftKey && !toggleKey(e)) select((sel) => ({ ...NO_SELECTION, reveal: sel.reveal }));
      return;
    }
    if (e.shiftKey) {
      const anchor = selection.anchor && indexOf.has(selection.anchor) ? selection.anchor : name;
      const span = range(anchor, name);
      select((sel) => ({
        names: new Set(toggleKey(e) ? [...sel.names, ...span] : span),
        anchor,
        active: name,
        reveal: sel.reveal,
      }));
      return;
    }
    if (toggleKey(e)) {
      select((sel) => {
        const names = new Set(sel.names);
        if (names.has(name)) names.delete(name);
        else names.add(name);
        return { names, anchor: name, active: name, reveal: sel.reveal };
      });
      return;
    }
    // A press on a selected row keeps the selection, so it can be dragged;
    // the click (no drag) then selects that row alone.
    if (selection.names.has(name)) collapse.current = name;
    else selectOne(name);
  }
  function onClick(e: MouseEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement;
    const more = target.closest<HTMLElement>("[data-more]");
    if (more) {
      const name = target.closest<HTMLElement>("[data-name]")?.dataset.name ?? "";
      const box = more.getBoundingClientRect();
      openMenuFor(selection.names.has(name) ? ordered(selection.names) : [name], box.left, box.bottom + 2, name);
      return;
    }
    const name = collapse.current;
    collapse.current = null;
    if (name && !e.shiftKey && !toggleKey(e) && selection.names.size > 1) selectOne(name);
  }
  function onDoubleClick(e: MouseEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement;
    if (target.closest("[data-more]") || target.closest(".files-grid-head")) return;
    const name = target.closest<HTMLElement>("[data-name]")?.dataset.name;
    if (name) open(name);
  }
  function onContextMenu(e: MouseEvent<HTMLDivElement>) {
    e.preventDefault();
    const name = (e.target as HTMLElement).closest<HTMLElement>("[data-name]")?.dataset.name ?? "";
    if (!name) {
      openMenuFor([], e.clientX, e.clientY);
      return;
    }
    if (!selection.names.has(name)) selectOne(name);
    openMenuFor(selection.names.has(name) ? ordered(selection.names) : [name], e.clientX, e.clientY, name);
  }

  // ---- Drag and drop -------------------------------------------------------------
  function onDragStart(e: DragEvent<HTMLDivElement>) {
    const name = (e.target as HTMLElement).closest?.<HTMLElement>("[data-name]")?.dataset.name;
    if (!name || !listing) {
      e.preventDefault();
      return;
    }
    collapse.current = null;
    const names = selection.names.has(name) ? ordered(selection.names) : [name];
    if (!selection.names.has(name)) selectOne(name);
    const payload: DragRows = { ssh: model.ssh, side, dir: listing.path, names };
    e.dataTransfer.setData(ROWS_MIME, JSON.stringify(payload));
    e.dataTransfer.effectAllowed = "copy";
    if (names.length > 1) {
      const ghost = document.createElement("div");
      ghost.className = "files-drag-ghost";
      ghost.textContent = itemCount(names.length);
      document.body.appendChild(ghost);
      e.dataTransfer.setDragImage(ghost, -10, -10);
      window.requestAnimationFrame(() => ghost.remove());
    }
    dragging.current = payload;
  }
  const accepts = (e: DragEvent) => {
    const types = Array.from(e.dataTransfer.types);
    if (types.includes(ROWS_MIME)) return !dragging.current || dragging.current.side !== side;
    return types.includes("Files");
  };
  function onDragEnter(e: DragEvent<HTMLElement>) {
    if (!accepts(e)) return;
    e.preventDefault();
    depth.current += 1;
    if (!transfer.blocked) setDropping(true);
  }
  function onDragOver(e: DragEvent<HTMLElement>) {
    if (!accepts(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }
  function onDragLeave() {
    depth.current = Math.max(0, depth.current - 1);
    if (!depth.current) setDropping(false);
  }
  function onDrop(e: DragEvent<HTMLElement>) {
    depth.current = 0;
    setDropping(false);
    const raw = e.dataTransfer.getData(ROWS_MIME);
    if (raw) {
      let payload: DragRows | null = null;
      try {
        payload = JSON.parse(raw) as DragRows;
      } catch {
        payload = null;
      }
      if (payload && payload.side !== side && payload.ssh === model.ssh && Array.isArray(payload.names) && payload.names.length) {
        e.preventDefault();
        onDropRows(payload);
      }
      dragging.current = null;
      return;
    }
    if (e.dataTransfer.files.length) {
      e.preventDefault();
      onDropFiles(Array.from(e.dataTransfer.files));
    }
  }

  // ---- Reveal in file manager & Browse (This computer) ------------------------
  const reveal = useAction(shownPath);
  async function revealPath(targetPath: string) {
    if (!targetPath) return;
    await reveal.run(() => localReveal(targetPath), {
      pending: REVEAL_PENDING,
      done: (res) => revealDone(baseName(res.revealed || targetPath) || targetPath),
    });
  }
  const browse = useAction();
  async function chooseFolder() {
    const result = await browse.run(() => chooseBackupDirectory(), { pending: "Choose a folder in the system dialog.", done: "" });
    if (result.ok && !result.value.cancelled && result.value.path) void model.list(result.value.path);
  }

  // ---- Menu ------------------------------------------------------------------------
  function menuActions(targets: string[], focus?: string): TableContextAction[] {
    const dir = listing?.path ?? "";
    const refreshItem: TableContextAction = {
      label: "Refresh",
      icon: "refresh",
      shortcut: "F5",
      disabled: state.loading,
      onClick: () => void model.refresh(),
    };
    const newFolder: TableContextAction = { label: "New folder…", icon: "folder-add", disabled: !listing, onClick: onNewFolder };
    const folderGlyph = <FileGlyph entry={{ kind: "dir", is_dir: true }} />;
    if (!targets.length) {
      const items: TableContextAction[] = [newFolder];
      if (!remote) {
        items.push({
          label: REVEAL_LABEL,
          icon: folderGlyph,
          disabled: !dir || reveal.pending,
          hint: reveal.pending ? REVEAL_PENDING : undefined,
          onClick: () => void revealPath(dir),
        });
      }
      items.push({ label: "Copy path", icon: "copy", disabled: !dir, onClick: () => onCopyPath([dir]) });
      items.push({ divider: true }, refreshItem);
      return items;
    }
    const single = targets.length === 1 ? byName.get(targets[0]) : undefined;
    const transferDisabled = !!transfer.blocked || !transfer.target;
    const items: TableContextAction[] = [
      {
        label: transfer.verb,
        icon: remote ? "import" : "export",
        disabled: transferDisabled,
        hint: transfer.blocked || (transfer.target ? undefined : "The other pane has no folder open."),
        onClick: () => { if (!transferDisabled) transfer.start(targets); },
      },
    ];
    if (single?.is_dir) items.push({ label: "Open", icon: "chevron", onClick: () => open(single.name) });
    if (!remote) {
      const chosen = (focus && targets.includes(focus) ? focus : selection.active && targets.includes(selection.active) ? selection.active : targets[0]) || "";
      items.push({
        label: REVEAL_LABEL,
        icon: folderGlyph,
        disabled: !dir || !chosen || reveal.pending,
        hint: reveal.pending ? REVEAL_PENDING : undefined,
        onClick: () => void revealPath(joinPath(dir, chosen)),
      });
    }
    items.push({ divider: true });
    if (single) items.push({ label: "Rename…", icon: "edit", shortcut: "F2", onClick: () => onRename(single) });
    items.push(newFolder);
    if (onDelete) items.push({ label: "Delete…", icon: "trash", danger: true, onClick: () => onDelete(targets) });
    items.push({ divider: true });
    items.push({ label: targets.length > 1 ? "Copy paths" : "Copy path", icon: "copy", onClick: () => onCopyPath(targets.map((n) => joinPath(dir, n))) });
    return items;
  }

  // ---- Footer -----------------------------------------------------------------------
  const selectedNames = ordered(selection.names);
  let summary: string;
  if (selectedNames.length) {
    const files = selectedNames.map((n) => byName.get(n)).filter((e): e is FileEntry => !!e && !e.is_dir);
    const size = files.reduce((sum, e) => sum + (e.size || 0), 0);
    summary = `${selectedNames.length.toLocaleString()} selected${files.length ? ` · ${bytes(size)}` : ""}`;
  } else if (listing) {
    const hidden = showHidden ? 0 : listing.entries.filter((e) => e.name.startsWith(".")).length;
    const all = listing.entries.length - hidden;
    summary = needle ? `${rows.length.toLocaleString()} of ${itemCount(all)}` : `${itemCount(all)}${hidden ? ` · ${hidden.toLocaleString()} hidden` : ""}`;
  } else summary = "";
  const otherSide = remote ? "this computer" : "the server";
  const transferTitle = transfer.blocked
    ? transfer.blocked
    : !transfer.target
      ? `Waiting for the folder on ${otherSide}`
      : selectedNames.length
        ? `${transfer.verb} ${selectedNames.length === 1 ? selectedNames[0] : itemCount(selectedNames.length)} to ${transfer.target}`
        : `Select files or folders to ${transfer.verb.toLowerCase()}`;
  const transferOff = !!transfer.blocked || !selectedNames.length || !transfer.target;

  const start = Math.max(0, view.first - OVERSCAN);
  const end = Math.min(rows.length, view.first + view.capacity + OVERSCAN);
  const activeIndex = indexOf.get(selection.active);
  const loadingText = state.loading
    ? remote && !listing ? `Connecting to ${title}…` : `Listing ${state.target || "the home folder"}…`
    : undefined;

  return (
    <section
      className={`files-pane is-${side}${dropping ? " is-drop" : ""}`}
      aria-label={title}
      data-side={side}
      onPointerDownCapture={onActivate}
      onFocusCapture={onActivate}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "l") {
          e.preventDefault();
          setEditing(true);
          return;
        }
        if (e.key === "F5" && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey && !editing) {
          const target = e.target as HTMLElement | null;
          const tag = target?.tagName;
          if (tag !== "INPUT" && tag !== "TEXTAREA" && !target?.isContentEditable) {
            e.preventDefault();
            void model.refresh();
          }
        }
      }}
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <header className="files-pane-head">
        <div className="files-pane-titlebar">
          <h3 className="files-pane-title"><bdi>{title}</bdi></h3>
          <span className="files-pane-titlebar-actions">
            <SearchField label={`Filter ${title}`} placeholder="Filter" value={needle} onChange={(text) => setFilter({ text, path: shownPath })} />
            {!remote && (
              <ActionButton small busy={browse.pending} busyLabel="Choosing…" title="Open a folder from the system dialog (another disk or volume)" onClick={() => void chooseFolder()}>
                Browse…
              </ActionButton>
            )}
          </span>
        </div>
        <div className="files-pane-nav">
          <span className="files-nav-buttons">
            <IconButton icon="back" label="Back" title={`Back (${shortcut("[")})`} disabled={!state.back.length} onClick={model.back} />
            <IconButton icon="chevron" label="Forward" title={`Forward (${shortcut("]")})`} disabled={!state.forward.length} onClick={model.forward} />
            <IconButton icon="up" label="Up one folder" title={`Up one folder (${shortcut("↑")})`} disabled={!listing || isRoot(listing.path)} onClick={goUp} />
            <IconButton icon="home" label="Home folder" onClick={goHome} />
          </span>
          <PathBar path={shownPath} title={title} editing={editing} onEditing={setEditing} onList={(p) => void model.list(p)} onDone={focusGrid} />
          <span className="files-nav-buttons">
            <IconButton
              icon="refresh"
              label={`Refresh ${title}`}
              title="List this folder again (F5)"
              data-action="refresh"
              className={state.loading ? "is-busy" : ""}
              aria-busy={state.loading || undefined}
              aria-disabled={state.loading || undefined}
              onClick={() => { if (!state.loading) void model.refresh(); }}
            />
            <IconButton icon="folder-add" label="New folder" disabled={!listing} onClick={onNewFolder} />
          </span>
        </div>
      </header>
      {!remote && <ActionStatus className="files-pane-action-status" state={reveal} onDismiss={reveal.reset} />}
      {browse.phase === "failed" && (
        <Notice variant="error" noticeKey={`${browse.message}#${browse.endedAt}`} onDismiss={browse.reset}>{browse.message}</Notice>
      )}
      <div className="files-pane-error">
        <ViewStatus error={state.error ? state.error : undefined} onRetry={model.retry}>
          {state.code === "host_key_unknown" && onTrustHost && (
            <> <button type="button" className="text-button" onClick={onTrustHost}>Trust host…</button></>
          )}
          {state.code === "auth_failed" && onEditConnection && (
            <> <button type="button" className="text-button" onClick={onEditConnection}>Edit connection</button></>
          )}
        </ViewStatus>
      </div>
      <div className="files-grid-wrap">
        <div className={`files-loading-bar${state.loading ? " is-on" : ""}`} aria-hidden="true" />
        <div
          ref={scroller}
          className={`files-grid${state.error && listing ? " is-stale" : ""}${state.loading ? " is-loading" : ""}`}
          role="grid"
          aria-label={`Files in ${title}`}
          aria-multiselectable="true"
          aria-rowcount={rows.length + 1}
          aria-busy={state.loading || undefined}
          aria-activedescendant={activeIndex !== undefined ? `${gridId}-row-${activeIndex}` : undefined}
          tabIndex={0}
          onKeyDown={onKeyDownGrid}
          onScroll={onScroll}
          onMouseDown={onMouseDown}
          onClick={onClick}
          onDoubleClick={onDoubleClick}
          onContextMenu={onContextMenu}
          onDragStart={onDragStart}
          onDragEnd={() => { dragging.current = null; }}
        >
          <div ref={head} role="rowgroup" className="files-grid-head">
            <div role="row" aria-rowindex={1} className="files-row is-head">
              {COLUMNS.filter((c) => remote || c[0] !== "perm").map(([key, label, className]) => {
                const on = sort.key === key;
                return (
                  <div key={key} role="columnheader" className={className} aria-sort={on ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}>
                    <button
                      type="button"
                      className="sort-button"
                      onClick={() => setSort((s) => (s.key === key
                        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
                        : { key, dir: key === "size" || key === "mtime" ? "desc" : "asc" }))}
                    >
                      <span className="th-label">{label}</span>
                      <span className="sort-mark" aria-hidden="true">
                        {on && <span className={sort.dir === "asc" ? "sort-up" : "sort-down"}><Icon name="disclosure" size={12} /></span>}
                      </span>
                    </button>
                  </div>
                );
              })}
              <div role="columnheader" className="files-cell-more"><span className="visually-hidden">Actions</span></div>
            </div>
          </div>
          <div role="rowgroup" className="files-grid-body" style={{ height: rows.length * rowH, paddingTop: start * rowH }}>
            {rows.slice(start, end).map((entry, i) => (
              <FileRow
                key={entry.name}
                entry={entry}
                index={start + i}
                rowId={`${gridId}-row-${start + i}`}
                selected={selection.names.has(entry.name)}
                active={selection.active === entry.name}
                remote={remote}
              />
            ))}
          </div>
        </div>
        {listing && !rows.length && (
          <p className="files-empty">
            {needle ? "No names match the filter." : listing.entries.length ? "Only hidden files. Turn on Show hidden files to see them." : "This folder is empty."}
          </p>
        )}
        {!listing && state.loading && (
          <p className="files-empty" aria-hidden="true"><span className="spinner" /> {loadingText}</p>
        )}
      </div>
      <footer className="files-pane-foot">
        <div className="files-pane-status">
          <ViewStatus loading={loadingText} startedAt={state.since ?? undefined} slowAfter={10_000} onStop={model.stop} />
          {!state.loading && <span className="files-pane-summary">{summary}</span>}
        </div>
        <ActionButton
          className="files-transfer-button"
          primary={active}
          icon={remote ? "import" : "export"}
          busy={transfer.busy}
          busyLabel="Starting…"
          aria-disabled={transferOff || undefined}
          title={transferTitle}
          onClick={() => { if (!transferOff) transfer.start(selectedNames); }}
        >
          {remote ? "Download" : "Upload"}
        </ActionButton>
      </footer>
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          label={menu.targets.length ? `Actions for ${menu.targets.length === 1 ? menu.targets[0] : itemCount(menu.targets.length)}` : `Actions for ${title}`}
          opener={scroller.current}
          actions={menuActions(menu.targets, menu.focus)}
          onClose={() => setMenu(null)}
        />
      )}
    </section>
  );
}
