// The Files browser for one SSH connection, FileZilla style: This computer on
// the left, the server on the right (stacked in a narrow window), the
// Transfers panel below. It owns the toolbar, what transfers, renames, new
// folders and remote deletes do, their sheets, and the notes they leave.
// The panes (FilePane.tsx) own listing, selection, keys and drag and drop;
// the jobs are polled in Transfers.tsx.
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import {
  ActionButton,
  Checkbox,
  ConfirmSheet,
  FormRow,
  Notice,
  StatusDot,
  ToolbarButton,
  copyToClipboard,
  useAction,
  useFocusScope,
  type Tone,
} from "./kit";
import { bytes } from "./PerfShared";
import { MAC } from "./platform";
import {
  baseName,
  download,
  droppedFilePath,
  joinPath,
  localMkdir,
  localRename,
  remoteMkdir,
  remoteRemove,
  remoteRename,
  upload,
  type FileEntry,
  type OnExists,
  type SshConnection,
  type Transfer,
} from "./filesApi";
import { FileGlyph, FilePane, itemCount, mtimeText, nameStem, storedFolder, usePane, type DragRows, type PaneModel, type Side } from "./FilePane";
import { TransfersPanel, useTransfers, type TransferScope } from "./Transfers";
import "./files.css";

const HIDDEN_KEY = "myflames.files.showHidden";
const PANEL_KEY = "myflames.files.transfersOpen";

function storedFlag(key: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === "1";
  } catch {
    return fallback;
  }
}
function storeFlag(key: string, value: boolean) {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    /* A remembered switch is a convenience; it works for this page without storage. */
  }
}

type Conflict = { name: string; source: FileEntry | null; existing: FileEntry };
type Plan = { direction: "upload" | "download"; paths: string[]; targetDir: string; conflicts: Conflict[] };
type NameTask = { kind: "mkdir" | "rename"; side: Side; dir: string; entry?: FileEntry };

export function FileBrowser(props: {
  connection: SshConnection;
  /** Opens the HostKeySheet; `retry` runs after trust. */
  onHostKeyUnknown: (retry: () => void) => void;
  onEditConnection: () => void;
}) {
  // Another connection is another browser: folders, selection and history never carry over.
  return <Browser key={props.connection.id} {...props} />;
}

function Browser({ connection, onHostKeyUnknown, onEditConnection }: {
  connection: SshConnection;
  onHostKeyUnknown: (retry: () => void) => void;
  onEditConnection: () => void;
}) {
  const id = connection.id;
  const hostKey = useRef(onHostKeyUnknown);
  hostKey.current = onHostKeyUnknown;
  const askHostKey = useCallback((retry: () => void) => hostKey.current(retry), []);
  const local = usePane("local", id);
  const remote = usePane("remote", id, askHostKey);
  const panes = useRef({ local, remote });
  panes.current = { local, remote };
  const dragging = useRef<DragRows | null>(null);
  const [showHidden, setShowHidden] = useState(() => storedFlag(HIDDEN_KEY, false));
  const [panelOpen, setPanelOpen] = useState(() => storedFlag(PANEL_KEY, true));
  const [scope, setScope] = useState<TransferScope>("this");
  const [note, setNote] = useState<{ text: string; variant: "note" | "error"; seq: number } | null>(null);
  const [conflict, setConflict] = useState<Plan | null>(null);
  const [nameTask, setNameTask] = useState<NameTask | null>(null);
  const [removal, setRemoval] = useState<{ dir: string; names: string[] } | null>(null);
  const [startingDirection, setStartingDirection] = useState<"upload" | "download" | null>(null);
  const starting = useAction();
  const refreshing = useAction();
  const removing = useAction();
  const noteSeq = useRef(0);
  const showNote = (variant: "note" | "error", text: string) => setNote({ variant, text, seq: ++noteSeq.current });

  // A job that left the active states refreshes the folder it wrote into, once.
  const onFinished = useCallback((left: Transfer[]) => {
    const { local, remote } = panes.current;
    let toRemote = false;
    let toLocal = false;
    for (const job of left) {
      if (job.ssh === id && job.direction === "upload") toRemote = true;
      if (job.direction === "download" && (job.ssh === id || job.target_dir === local.state.listing?.path)) toLocal = true;
    }
    if (toRemote) void remote.refresh();
    if (toLocal) void local.refresh();
  }, [id]);
  const jobs = useTransfers(onFinished);

  // First listings: the folder last shown here, else the connection's
  // folders ("" is the home folder). A remembered folder that is gone falls
  // back to the connection's.
  useEffect(() => {
    const open = (model: PaneModel, side: Side, fallback: string) => {
      const stored = storedFolder(id, side);
      void model.list(stored ?? fallback, { history: "none" }).then((result) => {
        if (!result.ok && !result.replaced && !result.code && stored !== null && stored !== fallback)
          void model.list(fallback, { history: "none" });
      });
    };
    open(panes.current.local, "local", connection.local_dir || "");
    open(panes.current.remote, "remote", connection.remote_dir || "");
    // Mount only: the browser is keyed by the connection id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  // An edited connection (host, user, key…) lists the server again.
  const updated = useRef(connection.updated);
  useEffect(() => {
    if (updated.current === connection.updated) return;
    updated.current = connection.updated;
    void panes.current.remote.refresh();
  }, [connection.updated]);

  const endpoint = `${connection.user ? `${connection.user}@` : ""}${connection.host}${connection.port !== null ? `:${connection.port}` : ""}`;
  const rs = remote.state;
  const status: { tone: Tone; label: string } = rs.code || (!rs.listing && rs.error)
    ? { tone: "problem", label: "Not connected" }
    : rs.listing ? { tone: "ok", label: "Connected" } : { tone: "info", label: "Connecting…" };

  // ---- Transfers ---------------------------------------------------------------
  async function start(plan: Plan, onExists: OnExists) {
    setPanelOpen(true);
    storeFlag(PANEL_KEY, true);
    setStartingDirection(plan.direction);
    const what = plan.paths.length === 1 ? baseName(plan.paths[0]) : itemCount(plan.paths.length);
    const result = await starting.run(
      () => plan.direction === "upload"
        ? upload(id, plan.paths, plan.targetDir, onExists)
        : download(id, plan.paths, plan.targetDir, onExists),
      { pending: `Starting the ${plan.direction} of ${what}…`, done: "", write: true },
    );
    if (result.ok) jobs.add(result.value.job);
  }
  /** Asks about names that already exist in the destination, else starts. */
  function offer(direction: "upload" | "download", paths: string[], sources: Map<string, FileEntry> | null) {
    if (!paths.length || starting.pending) return;
    const target = direction === "upload" ? remote : local;
    const listing = target.state.listing;
    if (!listing) {
      showNote("error", `${direction === "upload" ? connection.name : "This computer"} has no folder open yet.`);
      return;
    }
    const existing = new Map(listing.entries.map((e) => [e.name, e]));
    const conflicts = paths.map(baseName).filter((name) => existing.has(name))
      .map((name) => ({ name, source: sources?.get(name) ?? null, existing: existing.get(name) as FileEntry }));
    const plan: Plan = { direction, paths, targetDir: listing.path, conflicts };
    if (conflicts.length) setConflict(plan);
    else void start(plan, "fail");
  }
  function transferFrom(side: Side, names: string[], dir?: string) {
    const source = side === "local" ? local : remote;
    const listing = source.state.listing;
    const from = dir ?? listing?.path;
    if (!from || !names.length) return;
    const sources = listing && listing.path === from ? new Map(listing.entries.map((e) => [e.name, e])) : null;
    offer(side === "local" ? "upload" : "download", names.map((n) => joinPath(from, n)), sources);
  }
  function dropFiles(files: File[]) {
    const paths = files.map(droppedFilePath);
    if (!paths.length) return;
    if (paths.some((p) => !p)) {
      showNote("note", `Dropping files from ${MAC ? "Finder" : "File Explorer"} works in the desktop app. Use the This computer pane here.`);
      return;
    }
    offer("upload", paths, null);
  }
  async function copyPaths(paths: string[]) {
    const ok = await copyToClipboard(paths.join("\n"));
    if (!ok) showNote("error", "Could not copy to the clipboard.");
    else showNote("note", paths.length === 1 ? `Path copied: ${paths[0]}` : `${paths.length} paths copied.`);
  }

  // ---- New folder, rename, delete ---------------------------------------------
  function newFolder(side: Side) {
    const listing = (side === "local" ? local : remote).state.listing;
    if (listing) setNameTask({ kind: "mkdir", side, dir: listing.path });
  }
  function rename(side: Side, entry: FileEntry) {
    const listing = (side === "local" ? local : remote).state.listing;
    if (listing) setNameTask({ kind: "rename", side, dir: listing.path, entry });
  }
  async function confirmRemoval() {
    const task = removal;
    if (!task) return;
    const result = await removing.run(() => remoteRemove(id, task.names.map((n) => joinPath(task.dir, n))), {
      pending: "Deleting…", done: "", write: true,
    });
    if (!result.ok && result.reason === "busy") return;
    // Some may be gone even when the request failed: show the folder as it is.
    void remote.refresh();
    if (result.ok) {
      setRemoval(null);
      showNote("note", `Deleted ${itemCount(result.value.removed)}.`);
    }
  }

  const transferFor = (side: Side) => ({
    verb: side === "local" ? ("Upload" as const) : ("Download" as const),
    target: (side === "local" ? remote : local).state.listing?.path ?? "",
    busy: starting.pending && startingDirection === (side === "local" ? "upload" : "download"),
    start: (names: string[]) => transferFrom(side, names),
  });

  return (
    <div className="files-browser">
      <div className="files-toolbar">
        <div className="files-identity">
          <h2 className="files-identity-name"><bdi>{connection.name}</bdi></h2>
          <span className="files-endpoint"><bdi>{endpoint}</bdi></span>
          <StatusDot tone={status.tone} label={status.label} />
        </div>
        <div className="files-toolbar-actions">
          <Checkbox checked={showHidden} onChange={(on) => { setShowHidden(on); storeFlag(HIDDEN_KEY, on); }}>
            Show hidden files
          </Checkbox>
          <ToolbarButton icon="edit" onClick={onEditConnection}>Edit</ToolbarButton>
          <ActionButton
            icon="refresh"
            data-action="refresh"
            busy={refreshing.pending}
            busyLabel="Refreshing…"
            title="List both folders again"
            onClick={() => void refreshing.run(async () => {
              jobs.wake();
              await Promise.all([local.refresh(), remote.refresh()]);
            }, { pending: "Refreshing…", done: "" })}
          >
            Refresh
          </ActionButton>
        </div>
      </div>
      <div className="files-notes">
        {note && (
          <Notice variant={note.variant} noticeKey={`${note.text}#${note.seq}`} onDismiss={() => setNote(null)}>{note.text}</Notice>
        )}
      </div>
      <div className="files-panes">
        <FilePane
          model={local}
          title="This computer"
          showHidden={showHidden}
          dragging={dragging}
          transfer={transferFor("local")}
          onNewFolder={() => newFolder("local")}
          onRename={(entry) => rename("local", entry)}
          onDropRows={(rows) => transferFrom(rows.side, rows.names, rows.dir)}
          onDropFiles={dropFiles}
          onCopyPath={(paths) => void copyPaths(paths)}
        />
        <FilePane
          model={remote}
          title={connection.name}
          showHidden={showHidden}
          dragging={dragging}
          transfer={transferFor("remote")}
          onNewFolder={() => newFolder("remote")}
          onRename={(entry) => rename("remote", entry)}
          onDelete={(names) => {
            const dir = remote.state.listing?.path;
            if (!dir || !names.length) return;
            removing.reset();
            setRemoval({ dir, names });
          }}
          onDropRows={(rows) => transferFrom(rows.side, rows.names, rows.dir)}
          onDropFiles={dropFiles}
          onCopyPath={(paths) => void copyPaths(paths)}
          onTrustHost={() => askHostKey(() => remote.retry())}
          onEditConnection={onEditConnection}
        />
      </div>
      <TransfersPanel
        connection={connection}
        model={jobs}
        open={panelOpen}
        onToggle={(on) => { setPanelOpen(on); storeFlag(PANEL_KEY, on); }}
        scope={scope}
        onScope={setScope}
        starting={starting}
        onStartingDismiss={starting.reset}
        onEditConnection={onEditConnection}
        onTrustHost={onHostKeyUnknown}
      />
      {conflict && (
        <ConflictSheet
          plan={conflict}
          serverName={connection.name}
          onCancel={() => setConflict(null)}
          onChoose={(onExists) => {
            const plan = conflict;
            setConflict(null);
            void start(plan, onExists);
          }}
        />
      )}
      {nameTask && (
        <NameSheet
          task={nameTask}
          place={nameTask.side === "local" ? "This computer" : connection.name}
          existing={new Set(((nameTask.side === "local" ? local : remote).state.listing?.entries || []).map((e) => e.name))}
          submit={(name) => {
            const { side, dir, entry } = nameTask;
            const to = joinPath(dir, name);
            if (nameTask.kind === "mkdir") return side === "remote" ? remoteMkdir(id, to) : localMkdir(to);
            const from = joinPath(dir, entry?.name || "");
            return side === "remote" ? remoteRename(id, from, to) : localRename(from, to);
          }}
          onClose={() => setNameTask(null)}
          onDone={(name) => {
            const side = nameTask.side;
            setNameTask(null);
            void (side === "local" ? local : remote).refresh({ select: [name] });
          }}
        />
      )}
      {removal && (
        <ConfirmSheet
          eyebrow={connection.name}
          title={`Delete ${itemCount(removal.names.length)} from ${connection.name}?`}
          description="Folders are deleted with everything in them. This cannot be undone."
          confirmLabel="Delete"
          busy={removing.pending}
          busyLabel="Deleting…"
          onCancel={() => setRemoval(null)}
          onConfirm={() => void confirmRemoval()}
        >
          <NameList names={removal.names} />
          {(removing.phase === "failed" || removing.phase === "unknown") && (
            <p className="form-error" role="alert">{removing.message}</p>
          )}
        </ConfirmSheet>
      )}
    </div>
  );
}

/** Up to five names, then "and N more". */
function NameList({ names }: { names: string[] }) {
  return (
    <>
      <ul className="files-sheet-names">
        {names.slice(0, 5).map((name) => <li key={name}><bdi>{name}</bdi></li>)}
      </ul>
      {names.length > 5 && <p className="files-sheet-more">and {(names.length - 5).toLocaleString()} more</p>}
    </>
  );
}

const describe = (entry: FileEntry | null) =>
  !entry ? "—" : `${entry.is_dir ? "Folder" : bytes(entry.size)} · ${mtimeText(entry.mtime)}`;

function ConflictSheet({ plan, serverName, onCancel, onChoose }: {
  plan: Plan;
  serverName: string;
  onCancel: () => void;
  onChoose: (onExists: OnExists) => void;
}) {
  const n = plan.conflicts.length;
  const upload = plan.direction === "upload";
  const folder = plan.targetDir === "/" ? "/" : baseName(plan.targetDir);
  const fresh = plan.paths.length - n;
  const folders = plan.conflicts.some((c) => c.existing.is_dir || c.source?.is_dir);
  return (
    <ConfirmSheet
      className="files-conflict-sheet"
      eyebrow={upload ? `Upload to ${serverName}` : "Download to This computer"}
      title={`${itemCount(n)} already ${n === 1 ? "exists" : "exist"} in ${folder}`}
      description={<>In <bdi>{plan.targetDir}</bdi>{fresh > 0 ? `. The other ${itemCount(fresh)} ${fresh === 1 ? "is" : "are"} new.` : "."}</>}
      confirmLabel="Overwrite"
      secondary={{ label: "Skip existing", onClick: () => onChoose("skip") }}
      onCancel={onCancel}
      onConfirm={() => onChoose("overwrite")}
    >
      <ul className="files-conflicts">
        {plan.conflicts.slice(0, 5).map((c) => (
          <li key={c.name}>
            <span className="files-conflict-name">
              <FileGlyph entry={c.existing} />
              <bdi>{c.name}</bdi>
            </span>
            <span className="files-conflict-meta">
              {upload ? "This computer" : serverName}: {describe(c.source)}
            </span>
            <span className="files-conflict-meta">
              {upload ? serverName : "This computer"}: {describe(c.existing)}
            </span>
          </li>
        ))}
      </ul>
      {n > 5 && <p className="files-sheet-more">and {(n - 5).toLocaleString()} more</p>}
      {folders && <p className="sheet-lead">Existing folders are merged: your choice applies to the files inside them.</p>}
      {upload && <p className="sheet-lead">Overwriting removes the server's copy first; if the upload stops, the old file cannot be restored.</p>}
    </ConfirmSheet>
  );
}

/** New folder or Rename: one name field, checked before it is sent; the
 * server's answer stays in the sheet when it refuses. */
function NameSheet({ task, place, existing, submit, onClose, onDone }: {
  task: NameTask;
  /** "This computer" or the connection name. */
  place: string;
  existing: ReadonlySet<string>;
  submit: (name: string) => Promise<unknown>;
  onClose: () => void;
  onDone: (name: string) => void;
}) {
  const rename = task.kind === "rename";
  const initial = rename && task.entry ? task.entry.name : freeName(existing);
  const stem = rename && task.entry ? nameStem(task.entry.name, task.entry.is_dir) : initial.length;
  const [name, setName] = useState(initial);
  const [problem, setProblem] = useState("");
  const action = useAction();
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const selected = useRef(false);
  const titleId = useId();
  const formId = useId();
  const fieldId = useId();
  const busy = action.pending;
  const dismiss = () => {
    if (!busy) onClose();
  };
  useFocusScope(dialog, { open: true, onClose: dismiss, initialFocus: () => input.current });
  const windows = task.side === "local" && task.dir.includes("\\") && !task.dir.includes("/");
  function check(value: string): string {
    if (!value) return "Type a name.";
    if (value === "." || value === "..") return "“.” and “..” cannot be used as names.";
    if (value.includes("/") || (windows && value.includes("\\"))) return `A name cannot contain “${windows ? "\\" : "/"}”.`;
    if (value.includes("\0")) return "A name cannot contain a null character.";
    if (rename && value === task.entry?.name) return "";
    if (existing.has(value)) return `There is already an item named “${value}” here.`;
    return "";
  }
  async function confirm() {
    const value = name.trim();
    const why = check(value);
    if (why) {
      setProblem(why);
      input.current?.focus();
      return;
    }
    if (rename && value === task.entry?.name) {
      onClose();
      return;
    }
    setProblem("");
    const result = await action.run(() => submit(value), {
      pending: rename ? "Renaming…" : "Creating the folder…", done: "", write: true,
    });
    if (result.ok) onDone(value);
  }
  const error = problem || (action.phase === "failed" || action.phase === "unknown" ? action.message : "");
  const title: ReactNode = rename ? <>Rename <bdi>{task.entry?.name}</bdi></> : "New folder";
  return createPortal(
    <dialog
      ref={dialog}
      className="confirm-sheet files-name-sheet"
      role="dialog"
      tabIndex={-1}
      aria-labelledby={titleId}
      aria-busy={busy || undefined}
      onCancel={(e) => { e.preventDefault(); dismiss(); }}
    >
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">{place}</div>
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      <form id={formId} className="sheet-body" noValidate onSubmit={(e) => { e.preventDefault(); if (!busy) void confirm(); }}>
        <p className="sheet-lead">In <bdi>{task.dir}</bdi></p>
        <FormRow label={rename ? "New name" : "Folder name"} htmlFor={fieldId} error={error || undefined}>
          <input
            ref={input}
            id={fieldId}
            value={name}
            maxLength={255}
            spellCheck={false}
            autoCapitalize="off"
            autoComplete="off"
            onFocus={(e) => {
              if (selected.current) return;
              selected.current = true;
              e.currentTarget.setSelectionRange(0, stem);
            }}
            onChange={(e) => { setName(e.target.value); setProblem(""); action.reset(); }}
          />
        </FormRow>
      </form>
      <div className="dialog-actions sheet-actions">
        <button type="button" className="button" disabled={busy} onClick={dismiss}>Cancel</button>
        <ActionButton type="submit" form={formId} primary busy={busy} busyLabel={rename ? "Renaming…" : "Creating…"}>
          {rename ? "Rename" : "Create folder"}
        </ActionButton>
      </div>
    </dialog>,
    document.body,
  );
}

/** "New folder", or "New folder 2" and on when that name is taken. */
function freeName(existing: ReadonlySet<string>): string {
  if (!existing.has("New folder")) return "New folder";
  for (let n = 2; ; n += 1) if (!existing.has(`New folder ${n}`)) return `New folder ${n}`;
}
