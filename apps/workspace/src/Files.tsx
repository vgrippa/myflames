// The Files view (Server place): FileZilla-style transfer between this
// computer and the connection's SSH host over SFTP. Two panes (local left,
// remote right) with an address field, Up and Refresh each; entries are
// chosen with their checkbox and moved with the Upload/Download buttons or by
// dragging rows onto the other pane. OS files dropped on either pane upload
// into the current remote directory (the desktop app supplies real paths; in
// a plain browser the drop says so and offers Choose files…). Transfers run
// as jobs on the backend, polled while any is queued or running (2 s, paused
// in a hidden tab), and listed under the panes with per-file progress until
// cleared. The SSH password lives in the backend secret store: the chip shows
// only whether one is saved, and the dialog sends it once and forgets it.
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import type { Connection } from "./api";
import { connectionKey } from "./binding";
import { bytes } from "./PerfShared";
import { ConfirmSheet, IconButton, Notice, PrerequisiteGate, ToolbarButton, ViewLayout, errorText } from "./kit";
import { Icon } from "./components";
import { FolderField } from "./FolderField";
import {
  chooseLocalFiles,
  isSshAuthError,
  sftpCancel,
  sftpDownload,
  sftpJobActive,
  sftpJobs,
  sftpList,
  sftpLocalList,
  sftpMkdir,
  sftpRemove,
  sftpUpload,
  sshAuthClear,
  sshAuthSave,
  sshAuthStatus,
  type SftpEntry,
  type SftpJob,
} from "./sftpApi";
import "./files.css";

/** Drag payload of pane rows (never OS files: those carry the Files type). */
const ROWS_MIME = "application/x-myflames-files";

type Side = "local" | "remote";
type Pane = {
  /** The listed directory ("" until the first listing answers). */
  path: string;
  entries: SftpEntry[];
  error: string;
  loading: boolean;
  /** A listing was answered at least once (distinguishes an empty folder). */
  listed: boolean;
};
const EMPTY_PANE: Pane = { path: "", entries: [], error: "", loading: false, listed: false };

type PendingTransfer = {
  direction: "upload" | "download";
  /** Full paths of the sources (local paths for an upload, remote for a download). */
  paths: string[];
  /** Directory the transfer writes into (remote for an upload, local for a download). */
  targetDir: string;
  /** Source names that already exist at the destination. */
  conflicts: string[];
};

/** Remote paths are POSIX; local ones use the separator the path shows. */
function joinPath(dir: string, name: string): string {
  const sep = dir.includes("\\") ? "\\" : "/";
  if (!dir || dir === sep) return sep === "/" ? `/${name}` : name;
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}
function parentPath(path: string): string {
  const sep = path.includes("\\") ? "\\" : "/";
  const trimmed = path.length > 1 ? path.replace(/[/\\]+$/, "") : path;
  const at = trimmed.replace(/[/\\]+$/, "").search(/[/\\][^/\\]+$/);
  if (at < 0) return trimmed === path ? path : sep;
  const parent = trimmed.slice(0, at);
  return parent || sep;
}
function baseName(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}

function fmtMtime(mtime: number | string | null): string {
  if (mtime === null || mtime === undefined || mtime === "") return "—";
  const date = typeof mtime === "number"
    ? new Date(mtime > 1e12 ? mtime : mtime * 1000)
    : new Date(mtime);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
/** "~45 s left", "~2 min left"; null while the backend has no estimate. */
function etaText(eta_s: number | null): string {
  if (eta_s === null || !Number.isFinite(eta_s)) return "—";
  const s = Math.max(0, Math.round(eta_s));
  if (s < 90) return `~${s} s left`;
  if (s < 90 * 60) return `~${Math.round(s / 60)} min left`;
  return `~${(s / 3600).toFixed(1)} h left`;
}
function jobPercent(job: SftpJob): number | null {
  if (!(job.bytes_total > 0)) return null;
  return Math.min(100, Math.round((100 * job.bytes_done) / job.bytes_total));
}
function jobLabel(job: SftpJob): string {
  const first = job.items[0];
  const name = first ? baseName(first.source) : "";
  const more = job.items.length > 1 ? ` and ${job.items.length - 1} more` : "";
  return `${job.direction === "upload" ? "Upload" : "Download"} ${name}${more}`;
}

/** Folder and file glyphs on the 24 px grid (the icon registry has neither). */
function EntryGlyph({ dir }: { dir: boolean }) {
  return dir ? (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="files-glyph is-dir">
      <path d="M2.5 7V17C2.5 18.9 4.1 20.5 6 20.5H18C19.9 20.5 21.5 18.9 21.5 17V9.5C21.5 7.6 19.9 6 18 6H12L10 3.8C9.6 3.3 9.1 3 9 3H6C4.1 3 2.5 4.6 2.5 6.5V7Z" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="files-glyph">
      <path d="M5 3.5H13L19 9.5V20.5H5V3.5Z" />
      <path d="M13 3.5V9.5H19" />
    </svg>
  );
}

/** Jobs are polled like DDL progress (ddlProgressPoll.ts): 2 s, one read at a
 * time, paused in a hidden tab, and only while something is queued or running
 * (wake() restarts the loop when the view starts a transfer). */
function followTransfers(
  read: (signal: AbortSignal) => Promise<SftpJob[]>,
  receive: (jobs: SftpJob[]) => void,
): { wake: () => void; dispose: () => void } {
  let disposed = false;
  let timer = 0;
  let request: AbortController | null = null;
  let active = true; // Until the first answer says otherwise.
  const schedule = () => {
    if (!disposed && !document.hidden && active) timer = window.setTimeout(() => void poll(), 2000);
  };
  const poll = async () => {
    if (disposed || document.hidden || request) return;
    const controller = new AbortController();
    request = controller;
    try {
      const jobs = await read(controller.signal);
      if (!disposed && !controller.signal.aborted) {
        active = jobs.some(sftpJobActive);
        receive(jobs);
      }
    } catch {
      /* The panel keeps the jobs it last showed; the next tick retries. */
    } finally {
      request = null;
      schedule();
    }
  };
  const visibility = () => {
    window.clearTimeout(timer);
    request?.abort();
    if (!document.hidden && !request) void poll();
  };
  document.addEventListener("visibilitychange", visibility);
  void poll();
  return {
    wake: () => {
      if (disposed) return;
      active = true;
      if (!document.hidden && !request) {
        window.clearTimeout(timer);
        void poll();
      }
    },
    dispose: () => {
      disposed = true;
      window.clearTimeout(timer);
      request?.abort();
      document.removeEventListener("visibilitychange", visibility);
    },
  };
}

/** The path of a dropped File where the app supplies one (the desktop shell);
 * "" in a plain browser. */
function droppedPath(file: File): string {
  return (file as File & { path?: string }).path || "";
}

export function FilesView({
  connection,
  disabled = false,
  onBack,
  onOpenConnections,
}: {
  connection: Connection;
  disabled?: boolean;
  /** Narrow layouts: back to the list of views. */
  onBack?: () => void;
  /** Opens Connections (Home), where an SSH host is added to a saved connection. */
  onOpenConnections?: () => void;
}) {
  const key = connectionKey(connection);
  const hasSsh = !!connection.ssh_host;
  const [local, setLocal] = useState<Pane>(EMPTY_PANE);
  const [remote, setRemote] = useState<Pane>(EMPTY_PANE);
  const [localDraft, setLocalDraft] = useState("");
  const [remoteDraft, setRemoteDraft] = useState("");
  const [selected, setSelected] = useState<Record<Side, string[]>>({ local: [], remote: [] });
  const [dropSide, setDropSide] = useState<Side | null>(null);
  const dragDepth = useRef<Record<Side, number>>({ local: 0, remote: 0 });
  const [jobs, setJobs] = useState<SftpJob[]>([]);
  const [cleared, setCleared] = useState<string[]>([]);
  const [transferNote, setTransferNote] = useState("");
  const [transferError, setTransferError] = useState("");
  const [pending, setPending] = useState<PendingTransfer | null>(null);
  const [mkdirOpen, setMkdirOpen] = useState(false);
  const [mkdirName, setMkdirName] = useState("");
  const [mkdirBusy, setMkdirBusy] = useState(false);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeAsk, setRemoveAsk] = useState(false);
  const [hasPassword, setHasPassword] = useState<boolean | null>(null);
  const [sshOpen, setSshOpen] = useState(false);
  const [sshPassword, setSshPassword] = useState("");
  const [sshBusy, setSshBusy] = useState(false);
  const [sshError, setSshError] = useState("");
  const connectionRef = useRef(connection);
  connectionRef.current = connection;

  const pane = (side: Side) => (side === "local" ? local : remote);
  const setPane = (side: Side, next: (pane: Pane) => Pane) =>
    (side === "local" ? setLocal : setRemote)((p) => next(p));

  const listPane = useCallback(async (side: Side, path: string) => {
    setPane(side, (p) => ({ ...p, loading: true, error: "" }));
    try {
      const answer = side === "local"
        ? await sftpLocalList(path)
        : await sftpList(connectionRef.current, path);
      setPane(side, () => ({ path: answer.path, entries: answer.entries || [], error: "", loading: false, listed: true }));
      (side === "local" ? setLocalDraft : setRemoteDraft)(answer.path);
      setSelected((s) => ({ ...s, [side]: [] }));
    } catch (e) {
      const message = errorText(e);
      setPane(side, (p) => ({ ...p, loading: false, error: message }));
      if (side === "remote" && isSshAuthError(message)) setTransferError(message);
    }
  }, []);

  // First listings and the SSH auth state, per connection. The remote home is
  // whatever the backend answers for ""; the local pane lists the same way.
  useEffect(() => {
    setLocal(EMPTY_PANE);
    setRemote(EMPTY_PANE);
    setSelected({ local: [], remote: [] });
    setTransferError("");
    setTransferNote("");
    setHasPassword(null);
    if (!hasSsh) return;
    void listPane("local", "");
    void listPane("remote", "");
    sshAuthStatus(connectionRef.current).then(
      (status) => setHasPassword(status.has_password === true),
      () => setHasPassword(null),
    );
  }, [key, hasSsh, listPane]);

  // The transfer queue: polled while a job is queued or running. A job that
  // leaves the active states refreshes both panes once (its writes landed).
  const transfersRef = useRef<{ wake: () => void; dispose: () => void } | null>(null);
  const jobStates = useRef(new Map<string, string>());
  const panePaths = useRef({ local: "", remote: "" });
  panePaths.current = { local: local.path, remote: remote.path };
  useEffect(() => {
    if (!hasSsh) return;
    const follower = followTransfers(
      (signal) => sftpJobs(signal).then((answer) => answer.jobs || []),
      (fresh) => {
        setJobs(fresh);
        let finished = false;
        const seen = new Map<string, string>();
        for (const job of fresh) {
          seen.set(job.id, job.state);
          const before = jobStates.current.get(job.id);
          if ((before === "queued" || before === "running") && !sftpJobActive(job)) finished = true;
        }
        jobStates.current = seen;
        if (finished) {
          void listPane("local", panePaths.current.local);
          void listPane("remote", panePaths.current.remote);
        }
      },
    );
    transfersRef.current = follower;
    return () => {
      transfersRef.current = null;
      follower.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, hasSsh, listPane]);

  const visibleJobs = jobs.filter((job) => !cleared.includes(job.id));
  const finishedCount = visibleJobs.filter((job) => !sftpJobActive(job)).length;

  function noteFailure(error: unknown) {
    const message = errorText(error);
    if (isSshAuthError(message)) setTransferError(message);
    else setTransferError(message);
  }

  /** Start a transfer (after the overwrite question when conflicts exist). */
  async function startTransfer(transfer: Omit<PendingTransfer, "conflicts">, overwrite: boolean) {
    if (!transfer.paths.length) return;
    const c = connectionRef.current;
    try {
      if (transfer.direction === "upload") await sftpUpload(c, transfer.targetDir, transfer.paths, overwrite);
      else await sftpDownload(c, transfer.targetDir, transfer.paths, overwrite);
      setTransferNote(`${transfer.direction === "upload" ? "Upload" : "Download"} of ${transfer.paths.length} ${transfer.paths.length === 1 ? "item" : "items"} queued.`);
      transfersRef.current?.wake();
    } catch (e) {
      noteFailure(e);
    }
  }

  /** Ask about conflicts when the destination listing names them, else start. */
  function offerTransfer(direction: "upload" | "download", names: string[], sourceSide: Side) {
    const source = pane(sourceSide);
    const target = pane(sourceSide === "local" ? "remote" : "local");
    if (!names.length || !source.path || !target.path) return;
    const paths = names.map((name) => joinPath(source.path, name));
    const existing = new Set(target.entries.map((entry) => entry.name));
    const conflicts = names.filter((name) => existing.has(name));
    if (conflicts.length) setPending({ direction, paths, targetDir: target.path, conflicts });
    else void startTransfer({ direction, paths, targetDir: target.path }, false);
  }

  function uploadPaths(paths: string[]) {
    if (!paths.length || !remote.path) return;
    const existing = new Set(remote.entries.map((entry) => entry.name));
    const conflicts = paths.map(baseName).filter((name) => existing.has(name));
    if (conflicts.length) setPending({ direction: "upload", paths, targetDir: remote.path, conflicts });
    else void startTransfer({ direction: "upload", paths, targetDir: remote.path }, false);
  }

  /** OS files dropped on a pane upload into the current remote directory. */
  function dropFiles(files: FileList | File[]) {
    const list = [...files];
    if (!list.length) return;
    const paths = list.map(droppedPath);
    if (paths.some((p) => !p)) {
      setTransferNote("Drop works fully in the desktop app; here use Choose files…");
      return;
    }
    uploadPaths(paths);
  }

  async function chooseFiles() {
    try {
      const choice = await chooseLocalFiles();
      if (choice.cancelled) return;
      if (choice.path) uploadPaths([choice.path]);
      else setTransferNote(`“${choice.name || "The chosen file"}” cannot be uploaded from here: the picker did not return a path. Drag the file onto a pane in the desktop app instead.`);
    } catch (e) {
      noteFailure(e);
    }
  }

  async function createFolder() {
    const name = mkdirName.trim();
    if (!name || mkdirBusy) return;
    setMkdirBusy(true);
    try {
      await sftpMkdir(connectionRef.current, joinPath(remote.path, name));
      setMkdirOpen(false);
      setMkdirName("");
      await listPane("remote", remote.path);
    } catch (e) {
      noteFailure(e);
      setMkdirOpen(false);
    } finally {
      setMkdirBusy(false);
    }
  }

  async function removeSelected() {
    const names = selected.remote;
    if (!names.length || removeBusy) return;
    setRemoveBusy(true);
    const failed: string[] = [];
    for (const name of names) {
      const entry = remote.entries.find((e) => e.name === name);
      try {
        await sftpRemove(connectionRef.current, joinPath(remote.path, name), entry?.is_dir === true);
      } catch (e) {
        failed.push(`${name}: ${errorText(e)}`);
      }
    }
    setRemoveBusy(false);
    setRemoveAsk(false);
    if (failed.length) setTransferError(`Could not remove ${failed.join("; ")}`);
    await listPane("remote", remote.path);
  }

  async function saveSshPassword() {
    const password = sshPassword;
    if (!password || sshBusy) return;
    setSshBusy(true);
    setSshError("");
    try {
      await sshAuthSave(connectionRef.current, password);
      // The typed password leaves state the moment the save resolves; the
      // backend store keeps it and never returns it.
      setSshPassword("");
      setHasPassword(true);
      setSshOpen(false);
      if (isSshAuthError(transferError)) setTransferError("");
    } catch (e) {
      setSshPassword("");
      setSshError(errorText(e));
    } finally {
      setSshBusy(false);
    }
  }
  async function forgetSshPassword() {
    if (sshBusy) return;
    setSshBusy(true);
    setSshError("");
    try {
      await sshAuthClear(connectionRef.current);
      setHasPassword(false);
      setSshOpen(false);
    } catch (e) {
      setSshError(errorText(e));
    } finally {
      setSshBusy(false);
    }
  }

  function cancelJob(job: SftpJob) {
    sftpCancel(job.id).then(() => transfersRef.current?.wake(), (e) => noteFailure(e));
  }

  // ---- Drag and drop ---------------------------------------------------------
  function rowDragStart(side: Side, name: string, e: DragEvent) {
    const names = selected[side].includes(name) ? selected[side] : [name];
    e.dataTransfer.setData(ROWS_MIME, JSON.stringify({ side, names }));
    e.dataTransfer.effectAllowed = "copy";
  }
  function paneDragOver(e: DragEvent) {
    if (disabled) return;
    if (!e.dataTransfer.types.includes(ROWS_MIME) && !e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  }
  function paneDrop(side: Side, e: DragEvent) {
    dragDepth.current[side] = 0;
    setDropSide(null);
    if (disabled) return;
    const rows = e.dataTransfer.getData(ROWS_MIME);
    if (rows) {
      e.preventDefault();
      try {
        const payload = JSON.parse(rows) as { side: Side; names: string[] };
        if (payload.side !== side && Array.isArray(payload.names) && payload.names.length)
          offerTransfer(side === "remote" ? "upload" : "download", payload.names, payload.side);
      } catch {
        /* Not one of this view's payloads. */
      }
      return;
    }
    if (e.dataTransfer.files.length) {
      e.preventDefault();
      dropFiles(e.dataTransfer.files);
    }
  }

  const actionsOff = disabled || !hasSsh;

  function renderPane(side: Side) {
    const current = pane(side);
    const draft = side === "local" ? localDraft : remoteDraft;
    const setDraft = side === "local" ? setLocalDraft : setRemoteDraft;
    const title = side === "local" ? "Local" : "Remote";
    const sorted = [...current.entries].sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name));
    const chosen = selected[side];
    return (
      <section
        className={`files-pane${dropSide === side ? " is-drop" : ""}`}
        aria-label={`${title} files`}
        onDragOver={(e) => paneDragOver(e)}
        onDragEnter={(e) => {
          if (!e.dataTransfer.types.includes(ROWS_MIME) && !e.dataTransfer.types.includes("Files")) return;
          dragDepth.current[side] += 1;
          setDropSide(side);
        }}
        onDragLeave={() => {
          dragDepth.current[side] = Math.max(0, dragDepth.current[side] - 1);
          if (!dragDepth.current[side]) setDropSide((s) => (s === side ? null : s));
        }}
        onDrop={(e) => paneDrop(side, e)}
      >
        <div className="files-pane-head">
          <h3>{title}</h3>
          <div className="files-path-row">
            {side === "local" ? (
              <div className="files-local-field" onKeyDown={(e) => {
                if (e.key === "Enter" && (e.target as HTMLElement).tagName === "INPUT") {
                  e.preventDefault();
                  void listPane("local", localDraft.trim());
                }
              }}>
                <FolderField
                  label="Local folder"
                  value={localDraft}
                  onChange={setLocalDraft}
                  hint="Listed on this computer."
                  disabled={actionsOff || current.loading}
                />
              </div>
            ) : (
              <input
                className="files-path"
                aria-label="Remote directory"
                value={draft}
                spellCheck={false}
                autoComplete="off"
                disabled={actionsOff || current.loading}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void listPane("remote", remoteDraft.trim());
                  }
                }}
              />
            )}
            <IconButton
              icon="chevron"
              className="files-up"
              label={`${title}: up one directory`}
              disabled={actionsOff || current.loading || !current.path}
              onClick={() => void listPane(side, parentPath(current.path))}
            />
            <IconButton
              icon="refresh"
              label={`Refresh ${title.toLowerCase()} listing`}
              disabled={actionsOff || current.loading}
              onClick={() => void listPane(side, side === "local" ? localDraft.trim() : current.path)}
            />
          </div>
        </div>
        {current.error && (
          <Notice variant="error" noticeKey={current.error}>
            {current.error}
            {side === "remote" && isSshAuthError(current.error) && (
              <>
                {" "}
                <button type="button" className="text-button" onClick={() => setSshOpen(true)}>Save password…</button>
              </>
            )}
          </Notice>
        )}
        <ul className="files-list" aria-label={`${title} directory listing`} data-rows>
          {!current.listed && current.loading && <li className="files-empty-row"><span className="spinner" aria-hidden="true" /> Listing…</li>}
          {current.listed && !sorted.length && <li className="files-empty-row">This folder is empty.</li>}
          {sorted.map((entry) => {
            const isChosen = chosen.includes(entry.name);
            return (
              <li key={entry.name}>
                <div
                  className={`files-row${isChosen ? " is-selected" : ""}`}
                  draggable={!actionsOff}
                  onDragStart={(e) => rowDragStart(side, entry.name, e)}
                  onDoubleClick={() => entry.is_dir && void listPane(side, joinPath(current.path, entry.name))}
                >
                  <input
                    type="checkbox"
                    aria-label={`Select ${entry.name}`}
                    checked={isChosen}
                    disabled={actionsOff}
                    onChange={() =>
                      setSelected((s) => ({
                        ...s,
                        [side]: isChosen ? s[side].filter((n) => n !== entry.name) : [...s[side], entry.name],
                      }))
                    }
                  />
                  <button
                    type="button"
                    className="files-name"
                    title={entry.is_dir ? `Open ${entry.name}` : entry.name}
                    onClick={() =>
                      setSelected((s) => ({
                        ...s,
                        [side]: isChosen ? s[side].filter((n) => n !== entry.name) : [...s[side], entry.name],
                      }))
                    }
                    onDoubleClick={() => entry.is_dir && void listPane(side, joinPath(current.path, entry.name))}
                  >
                    <EntryGlyph dir={entry.is_dir} />
                    <span className="files-name-text">{entry.name}</span>
                  </button>
                  <span className="files-size">{entry.is_dir ? "—" : bytes(entry.size)}</span>
                  <span className="files-mtime">{fmtMtime(entry.mtime)}</span>
                </div>
              </li>
            );
          })}
        </ul>
        <div className="files-pane-foot">
          {side === "local" ? (
            <>
              <ToolbarButton onClick={() => void chooseFiles()} disabled={actionsOff || !remote.path} title="Choose files on this computer to upload">
                Choose files…
              </ToolbarButton>
              <ToolbarButton
                icon="export"
                primary
                disabled={actionsOff || !chosen.length || !remote.path}
                title={chosen.length ? `Upload ${chosen.length} selected to ${remote.path}` : "Select files to upload"}
                onClick={() => offerTransfer("upload", chosen, "local")}
              >
                Upload →
              </ToolbarButton>
            </>
          ) : (
            <>
              <ToolbarButton
                icon="import"
                primary
                disabled={actionsOff || !chosen.length || !local.path}
                title={chosen.length ? `Download ${chosen.length} selected to ${local.path}` : "Select files to download"}
                onClick={() => offerTransfer("download", chosen, "remote")}
              >
                ← Download
              </ToolbarButton>
              <ToolbarButton onClick={() => { setMkdirName(""); setMkdirOpen(true); }} disabled={actionsOff || !remote.path}>
                New folder
              </ToolbarButton>
              <ToolbarButton
                icon="trash"
                disabled={actionsOff || !chosen.length}
                title={chosen.length ? `Remove ${chosen.length} selected on the server` : "Select entries to remove"}
                onClick={() => setRemoveAsk(true)}
              >
                Delete
              </ToolbarButton>
            </>
          )}
        </div>
      </section>
    );
  }

  const sshChip = (
    <button
      type="button"
      className="button files-ssh-chip"
      onClick={() => { setSshError(""); setSshPassword(""); setSshOpen(true); }}
      title="SSH sign-in for transfers"
    >
      <Icon name="key" size={14} />
      {hasPassword ? "SSH: password saved" : "SSH: keys / agent"}
    </button>
  );

  return (
    <ViewLayout
      id="files"
      title="Files"
      className="files-view"
      onBack={onBack}
      info={
        <>
          <p>
            Transfer files between this computer and the server over SFTP, using the connection's SSH
            details. Choose entries with their checkboxes and move them with Upload and Download, or drag
            rows onto the other pane; files dropped from the Finder or Explorer upload into the remote
            directory on screen.
          </p>
          <p>Transfers run in the background and are listed under the panes until you clear them.</p>
        </>
      }
      controls={hasSsh ? sshChip : undefined}
      onRefresh={hasSsh ? () => { void listPane("local", localDraft.trim() || local.path); void listPane("remote", remote.path); transfersRef.current?.wake(); } : undefined}
      loading={local.loading || remote.loading}
    >
      {!hasSsh ? (
        <PrerequisiteGate
          action={
            onOpenConnections ? (
              <ToolbarButton onClick={onOpenConnections}>Open Connections</ToolbarButton>
            ) : undefined
          }
        >
          This connection has no SSH details. Add an SSH host to the saved connection to transfer files.
        </PrerequisiteGate>
      ) : (
        <>
          <div className="files-panes">
            {renderPane("local")}
            {renderPane("remote")}
          </div>
          {transferNote && <Notice variant="note" onDismiss={() => setTransferNote("")}>{transferNote}</Notice>}
          {transferError && (
            <Notice variant="error" noticeKey={transferError} onDismiss={() => setTransferError("")}>
              {transferError}
              {isSshAuthError(transferError) && (
                <>
                  {" "}
                  <button type="button" className="text-button" onClick={() => setSshOpen(true)}>Save password…</button>
                </>
              )}
            </Notice>
          )}
          <section className="files-jobs" aria-label="Transfers">
            <div className="files-jobs-head">
              <h3>Transfers</h3>
              {finishedCount > 0 && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setCleared((ids) => [...ids, ...visibleJobs.filter((j) => !sftpJobActive(j)).map((j) => j.id)])}
                >
                  Clear finished
                </button>
              )}
            </div>
            {!visibleJobs.length && <p className="hint">No transfers yet.</p>}
            {visibleJobs.map((job) => {
              const pct = jobPercent(job);
              return (
                <div key={job.id} className={`files-job is-${job.state}`} data-state={job.state}>
                  <div className="files-job-row">
                    <Icon name={job.direction === "upload" ? "export" : "import"} size={14} />
                    <span className="files-job-label">{jobLabel(job)}</span>
                    <span className="files-job-stats">
                      {bytes(job.bytes_done)}{job.bytes_total > 0 ? ` of ${bytes(job.bytes_total)}` : ""}
                      {pct !== null ? ` · ${pct}%` : ""}
                      {sftpJobActive(job) && job.rate_bps > 0 ? ` · ${bytes(job.rate_bps)}/s` : ""}
                      {sftpJobActive(job) ? ` · ${etaText(job.eta_s)}` : ""}
                    </span>
                    <span className={`files-job-state is-${job.state}`}>{job.state}</span>
                    {sftpJobActive(job) && (
                      <button type="button" className="text-button" onClick={() => cancelJob(job)}>Cancel</button>
                    )}
                  </div>
                  <div
                    className="files-progress"
                    role="progressbar"
                    aria-label={`${jobLabel(job)} progress`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct ?? undefined}
                  >
                    <span style={{ width: `${pct ?? (sftpJobActive(job) ? 15 : 0)}%` }} className={pct === null ? "is-indeterminate" : undefined} />
                  </div>
                  {job.items.length > 0 && (
                    <details className="files-job-items">
                      <summary>{job.items.length} {job.items.length === 1 ? "file" : "files"}</summary>
                      <ul>
                        {job.items.map((item, i) => {
                          const itemPct = item.bytes > 0 ? Math.min(100, Math.round((100 * item.done) / item.bytes)) : item.state === "done" ? 100 : 0;
                          return (
                            <li key={`${item.source}-${i}`} className={`files-job-item is-${item.state}`}>
                              <span className="files-item-name" title={`${item.source} → ${item.target}`}>{baseName(item.source)}</span>
                              <span className="files-item-progress">
                                <span className="files-progress">
                                  <span style={{ width: `${itemPct}%` }} />
                                </span>
                                <span className="files-item-bytes">{bytes(item.done)} of {bytes(item.bytes)}</span>
                              </span>
                              <span className="files-item-state">{item.error || item.state}</span>
                            </li>
                          );
                        })}
                      </ul>
                    </details>
                  )}
                </div>
              );
            })}
          </section>
        </>
      )}
      {pending && (
        <ConfirmSheet
          eyebrow="File transfer"
          title={pending.direction === "upload" ? "Overwrite on the server?" : "Overwrite local files?"}
          description={
            `${pending.conflicts.length} of ${pending.paths.length} ${pending.paths.length === 1 ? "item" : "items"} already ` +
            `${pending.conflicts.length === 1 ? "exists" : "exist"} at the destination: ${pending.conflicts.slice(0, 5).join(", ")}` +
            `${pending.conflicts.length > 5 ? ` and ${pending.conflicts.length - 5} more` : ""}.`
          }
          confirmTone="primary"
          confirmLabel="Overwrite"
          secondary={{
            label: "Skip existing",
            onClick: () => {
              const skipped = new Set(pending.conflicts);
              const paths = pending.paths.filter((p) => !skipped.has(baseName(p)));
              setPending(null);
              void startTransfer({ direction: pending.direction, paths, targetDir: pending.targetDir }, false);
            },
          }}
          onCancel={() => setPending(null)}
          onConfirm={() => {
            const transfer = pending;
            setPending(null);
            void startTransfer({ direction: transfer.direction, paths: transfer.paths, targetDir: transfer.targetDir }, true);
          }}
        />
      )}
      {mkdirOpen && (
        <ConfirmSheet
          eyebrow="Remote folder"
          title="New folder"
          confirmTone="primary"
          confirmLabel="Create folder"
          busy={mkdirBusy}
          busyLabel="Creating…"
          confirmDisabled={!mkdirName.trim()}
          onCancel={() => setMkdirOpen(false)}
          onConfirm={() => void createFolder()}
        >
          <label className="field">
            Folder name
            <input
              value={mkdirName}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setMkdirName(e.target.value)}
              placeholder={`In ${remote.path}`}
            />
          </label>
        </ConfirmSheet>
      )}
      {removeAsk && (
        <ConfirmSheet
          eyebrow="Remote"
          title={`Remove ${selected.remote.length} ${selected.remote.length === 1 ? "entry" : "entries"}?`}
          description={`${selected.remote.slice(0, 5).join(", ")}${selected.remote.length > 5 ? ` and ${selected.remote.length - 5} more` : ""} are removed from the server. This cannot be undone.`}
          confirmLabel="Delete"
          busy={removeBusy}
          busyLabel="Deleting…"
          onCancel={() => setRemoveAsk(false)}
          onConfirm={() => void removeSelected()}
        />
      )}
      {sshOpen && (
        hasPassword ? (
          <ConfirmSheet
            eyebrow="SSH sign-in"
            title="SSH password"
            description="A password is saved for this SSH endpoint."
            confirmLabel="Forget password"
            doneLabel="Close"
            secondary={{ label: "Forget password", onClick: () => void forgetSshPassword() }}
            onCancel={() => setSshOpen(false)}
            onConfirm={() => setSshOpen(false)}
          >
            {sshError && <p role="alert" className="error">{sshError}</p>}
          </ConfirmSheet>
        ) : (
          <ConfirmSheet
            eyebrow="SSH sign-in"
            title="SSH password"
            description="Stored in your OS keychain or the myflames vault, separate from the connection. The app never shows it again."
            confirmTone="primary"
            confirmLabel="Save securely"
            busy={sshBusy}
            busyLabel="Saving…"
            confirmDisabled={!sshPassword}
            onCancel={() => { setSshPassword(""); setSshOpen(false); }}
            onConfirm={() => void saveSshPassword()}
          >
            <label className="field">
              Password
              <input
                type="password"
                autoComplete="off"
                value={sshPassword}
                onChange={(e) => setSshPassword(e.target.value)}
              />
            </label>
            {sshError && <p role="alert" className="error">{sshError}</p>}
          </ConfirmSheet>
        )
      )}
    </ViewLayout>
  );
}
