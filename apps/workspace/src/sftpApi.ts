// The SSH/SFTP file transfer API of the Files view (Server place). Every call
// is a POST with a JSON body through api() (api.ts); the backend answers JSON.
// The SSH password is a secret of the backend store: it is sent once on save
// and never comes back (status answers only whether one exists).
import { api, type Connection } from "./api";

export type SftpEntry = {
  name: string;
  size: number;
  /** Epoch seconds (or milliseconds) or an ISO string, as the backend sends it. */
  mtime: number | string | null;
  is_dir: boolean;
  mode: string | number | null;
};
export type SftpList = { path: string; entries: SftpEntry[] };

export type SftpJobItem = {
  source: string;
  target: string;
  bytes: number;
  done: number;
  state: string;
  error: string | null;
};
export type SftpJobState = "queued" | "running" | "done" | "failed" | "cancelled";
export type SftpJob = {
  id: string;
  direction: "upload" | "download";
  state: SftpJobState;
  items: SftpJobItem[];
  bytes_done: number;
  bytes_total: number;
  rate_bps: number;
  eta_s: number | null;
  started_at: string | number | null;
};
export const sftpJobActive = (job: SftpJob) => job.state === "queued" || job.state === "running";

export const sftpList = (connection: Connection, path: string, signal?: AbortSignal) =>
  api<SftpList>("live/sftp/list", { connection, path }, signal);
/** The local pane's listing (this computer, no connection involved). */
export const sftpLocalList = (path: string, signal?: AbortSignal) =>
  api<SftpList>("live/sftp/local-list", { path }, signal);
export const sftpMkdir = (connection: Connection, path: string) =>
  api<{ path: string }>("live/sftp/mkdir", { connection, path });
export const sftpRemove = (connection: Connection, path: string, is_dir: boolean) =>
  api<{ removed: string }>("live/sftp/remove", { connection, path, is_dir });
export const sftpUpload = (connection: Connection, remote_dir: string, local_paths: string[], overwrite: boolean) =>
  api<{ job: SftpJob }>("live/sftp/upload", { connection, remote_dir, local_paths, overwrite });
export const sftpDownload = (connection: Connection, local_dir: string, remote_paths: string[], overwrite: boolean) =>
  api<{ job: SftpJob }>("live/sftp/download", { connection, local_dir, remote_paths, overwrite });
export const sftpJobs = (signal?: AbortSignal) =>
  api<{ jobs: SftpJob[] }>("live/sftp/jobs", {}, signal);
export const sftpCancel = (job_id: string) =>
  api<{ cancelled: true }>("live/sftp/cancel", { job_id });

export const sshAuthStatus = (connection: Connection, signal?: AbortSignal) =>
  api<{ has_password: boolean }>("live/sftp/ssh-auth/status", { connection }, signal);
export const sshAuthSave = (connection: Connection, password: string) =>
  api<{ saved: true }>("live/sftp/ssh-auth/save", { connection, password });
export const sshAuthClear = (connection: Connection) =>
  api<{ cleared: true }>("live/sftp/ssh-auth/clear", { connection });

/** The native file chooser Import CSV already uses (import/csv/choose). It
 * answers {file_id, name, size, modified} today: no path, so a choice made
 * here can start an upload only where the reply grows a `path`. */
export type ChosenFile = { path?: string; name?: string; cancelled?: boolean };
export const chooseLocalFiles = () => api<ChosenFile>("import/csv/choose", {});

/** A list or transfer failure the SSH password dialog can fix. */
export function isSshAuthError(message: string) {
  return message.startsWith("SSH sign-in failed");
}
