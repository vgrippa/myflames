// The Files place API: saved SSH connections, host-key trust, both panes'
// listings and file operations, and the transfer queue. Every call is a POST
// with a JSON body through api() (api.ts). A secret (SSH password or key
// passphrase) is sent on save or on a draft test and never comes back: the
// answers say only whether one is saved (has_secret).
import { api, ApiError } from "./api";

export type SshAuth = "agent" | "key" | "password";
export type SshConnection = {
  id: string;
  name: string;
  host: string;
  port: number | null;
  user: string;
  auth: SshAuth;
  /** The private key file; "" unless auth is "key". */
  key_path: string;
  has_secret: boolean;
  /** Folder opened first on the server; "" opens the remote home. */
  remote_dir: string;
  /** Folder opened first on this computer; "" opens the home folder. */
  local_dir: string;
  updated: string;
};
/** What the connection sheet sends: no id for a new connection. */
export type SshConnectionDraft = Omit<SshConnection, "id" | "has_secret" | "updated"> & { id?: string };

export type FileEntry = {
  name: string;
  kind: "file" | "dir" | "link" | "other";
  /** True for a folder and for a link to a folder (it opens). */
  is_dir: boolean;
  size: number | null;
  /** Epoch seconds. */
  mtime: number | null;
  /** Full st_mode, type bits included. */
  mode: number | null;
};
export type FileListing = { path: string; home: string; entries: FileEntry[] };

export type OnExists = "fail" | "skip" | "overwrite";
export type TransferItemState = "queued" | "running" | "finished" | "failed" | "cancelled" | "skipped";
export type TransferItem = {
  source: string;
  target: string;
  bytes: number;
  done: number;
  state: TransferItemState;
  error: string | null;
};
export type TransferState = "queued" | "running" | "finished" | "failed" | "cancelled";
export type Transfer = {
  id: string;
  ssh: string;
  ssh_name: string;
  direction: "upload" | "download";
  state: TransferState;
  phase: "scanning" | "transferring" | null;
  on_exists: OnExists;
  target_dir: string;
  sources: string[];
  files_total: number;
  files_done: number;
  files_skipped: number;
  files_failed: number;
  bytes_total: number;
  bytes_done: number;
  rate_bps: number;
  eta_s: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  /** The SSH reason of a job that failed as a whole (sign-in, host key), else null. */
  error_code: SshErrorCode | null;
  current: TransferItem | null;
  /** At most 200, in order; items_more counts the rest. */
  items: TransferItem[];
  items_more: number;
  failed_items: TransferItem[];
};
export const transferActive = (job: Transfer) => job.state === "queued" || job.state === "running";

export type HostKeyScan = {
  host: string;
  keys: { type: string; bits: number; fingerprint: string }[];
  token: string;
};
/** A saved connection by id, or a draft from the connection sheet. */
export type SshTarget = { id: string } | { connection: SshConnectionDraft; secret?: string };

// ---- SSH connections --------------------------------------------------------
export const sshConnections = (signal?: AbortSignal) =>
  api<{ connections: SshConnection[]; known_hosts_file: string }>("ssh/connections/list", {}, signal);
export const sshConnectionSave = (connection: SshConnectionDraft, secret?: string, forget_secret?: boolean) =>
  api<{ connection: SshConnection }>("ssh/connections/save", {
    connection,
    ...(secret ? { secret } : {}),
    ...(forget_secret ? { forget_secret: true } : {}),
  });
export const sshConnectionDelete = (id: string) => api<{ deleted: true }>("ssh/connections/delete", { id });
export const sshConnectionTest = (target: SshTarget, signal?: AbortSignal) =>
  api<{ ok: true; home: string; elapsed_ms: number }>("ssh/connections/test", target, signal);
export const sshHostKeyScan = (target: SshTarget) =>
  api<HostKeyScan>("ssh/hostkey/scan", "connection" in target ? { connection: target.connection } : { id: target.id });
export const sshHostKeyTrust = (token: string) => api<{ trusted: true; host: string }>("ssh/hostkey/trust", { token });
/** Private keys in ~/.ssh with a matching .pub: the Key file pick list. */
export const sshKeys = () => api<{ keys: { path: string; type: string; comment: string }[] }>("ssh/keys", {});
/** Host aliases from ~/.ssh/config (ssh-hosts-1.0). */
export const sshConfigHosts = () =>
  api<{ hosts: string[]; ssh_found: boolean; message: string }>("ssh/hosts", {});

// ---- Listings and file operations --------------------------------------------
export const remoteList = (ssh: string, path: string, signal?: AbortSignal) =>
  api<FileListing>("files/remote/list", { ssh, path }, signal);
export const localList = (path: string, signal?: AbortSignal) =>
  api<FileListing>("files/local/list", { path }, signal);
export const remoteMkdir = (ssh: string, path: string) => api<{ path: string }>("files/remote/mkdir", { ssh, path });
export const remoteRename = (ssh: string, from: string, to: string) =>
  api<{ path: string }>("files/remote/rename", { ssh, from, to });
export const remoteRemove = (ssh: string, paths: string[]) =>
  api<{ removed: number }>("files/remote/remove", { ssh, paths });
export const localMkdir = (path: string) => api<{ path: string }>("files/local/mkdir", { path });
export const localRename = (from: string, to: string) => api<{ path: string }>("files/local/rename", { from, to });

// ---- Transfers ---------------------------------------------------------------
export const upload = (ssh: string, local_paths: string[], remote_dir: string, on_exists: OnExists) =>
  api<{ job: Transfer }>("files/upload", { ssh, local_paths, remote_dir, on_exists });
export const download = (ssh: string, remote_paths: string[], local_dir: string, on_exists: OnExists) =>
  api<{ job: Transfer }>("files/download", { ssh, remote_paths, local_dir, on_exists });
export const transfers = (ssh?: string, signal?: AbortSignal) =>
  api<{ jobs: Transfer[] }>("files/jobs", ssh ? { ssh } : {}, signal);
export const transferCancel = (job_id: string) => api<{ job: Transfer }>("files/cancel", { job_id });
export const transferRetry = (job_id: string) => api<{ job: Transfer }>("files/retry", { job_id });
export const transfersClear = (ssh?: string) => api<{ cleared: number }>("files/clear", ssh ? { ssh } : {});

// ---- Errors ------------------------------------------------------------------
export type SshErrorCode = "host_key_unknown" | "host_key_changed" | "auth_failed" | "ssh_missing";
/** The machine-readable reason of a failed SSH call, or "" for any other failure. */
export function sshErrorCode(error: unknown): SshErrorCode | "" {
  const code = error instanceof ApiError ? error.code : "";
  return code === "host_key_unknown" || code === "host_key_changed" || code === "auth_failed" || code === "ssh_missing"
    ? code
    : "";
}

/** The desktop app's dropped-file paths (preload: webUtils.getPathForFile);
 * "" in a plain browser or for a file without a path. */
export function droppedFilePath(file: File): string {
  const bridge = (window as unknown as { myflamesDesktop?: { pathForFile?: (file: File) => string } }).myflamesDesktop;
  try {
    return bridge?.pathForFile?.(file) || "";
  } catch {
    return "";
  }
}

// ---- Paths -------------------------------------------------------------------
/** Remote paths are POSIX; local ones use the separator the path shows. */
export function joinPath(dir: string, name: string): string {
  const sep = dir.includes("\\") && !dir.includes("/") ? "\\" : "/";
  if (!dir) return name;
  return dir.endsWith(sep) ? dir + name : dir + sep + name;
}
export function parentPath(path: string): string {
  const sep = path.includes("\\") && !path.includes("/") ? "\\" : "/";
  const trimmed = path.length > 1 ? path.replace(/[/\\]+$/, "") : path;
  const at = trimmed.lastIndexOf(sep);
  if (at < 0) return path;
  if (at === 0) return sep;
  const parent = trimmed.slice(0, at);
  // "C:" alone is not a folder on Windows: keep its root separator.
  return /^[A-Za-z]:$/.test(parent) ? parent + sep : parent;
}
export function baseName(path: string): string {
  const parts = path.split(/[/\\]/).filter(Boolean);
  return parts[parts.length - 1] || path;
}
