// Application updates, shared by the badge beside the logo, Home's version
// footer, About myflames and the Software update dialog. GitHub is asked by
// checkForUpdates: from a click, or from the automatic check (at start and
// once a day) while "Check for updates automatically" is on. Update downloads
// the release's update file through the backend (checked against its
// published SHA-256) and hands it to the desktop app (window.myflamesDesktop
// .update, desktop/updater.cjs), which installs it at restart or quit.
import { useSyncExternalStore } from "react";
import {
  appUpdateSettings, appUpdateStatus, appVersion, checkAppUpdate, stageAppUpdate,
  type AppUpdateInfo, type AppUpdateLastCheck, type AppUpdateStaging,
} from "./api";

/** What the desktop app answers about updating this copy in place. */
export type DesktopUpdateSupport = { ok: boolean; supported: boolean; reason: string | null; ready: string | null };
type DesktopUpdateResult = { ok: boolean; reason?: string; version?: string };
type DesktopUpdateBridge = {
  support(): Promise<DesktopUpdateSupport>;
  prepare(): Promise<DesktopUpdateResult>;
  restart(): Promise<DesktopUpdateResult>;
};

/** The desktop app's update bridge (preload.cjs), or null in a browser. */
export function desktopUpdate(): DesktopUpdateBridge | null {
  const bridge = (window as unknown as { myflamesDesktop?: { update?: DesktopUpdateBridge } }).myflamesDesktop?.update;
  return bridge && typeof bridge.prepare === "function" ? bridge : null;
}

export type UpdateState = {
  /** The stored last check has been read. */
  loaded: boolean;
  /** Reading the stored last check failed: its outcome is unknown (retried on the next mount). */
  loadFailed: boolean;
  checking: boolean;
  last: AppUpdateLastCheck | null;
  /** The full answer of a check made in this window (needed to download). */
  info: AppUpdateInfo | null;
  /** "Check for updates automatically" (null until read). */
  autoCheck: boolean | null;
  /** Whether this copy can update itself; null outside the desktop app or until asked. */
  support: DesktopUpdateSupport | null;
  /** The backend's download of the update file. */
  staging: AppUpdateStaging | null;
  /** Update is running: checking, downloading or handing the file to the installer. */
  installing: boolean;
  /** The version installed at restart or quit, once the desktop app has it. */
  ready: string | null;
  installError: string | null;
  /** The Software update dialog is open, with the check answer captured when it opened. */
  dialog: { info: AppUpdateInfo | null } | null;
};

/** A result older than this is shown as a past result, never as current. */
export const STALE_AFTER_MS = 24 * 60 * 60 * 1000;
/** The automatic check runs again after a failure once this much time has passed. */
const RETRY_FAILED_AFTER_MS = 60 * 60 * 1000;

let state: UpdateState = {
  loaded: false, loadFailed: false, checking: false, last: null, info: null, autoCheck: null,
  support: null, staging: null, installing: false, ready: null, installError: null, dialog: null,
};
const listeners = new Set<() => void>();
let loading = false;
let inFlight: Promise<AppUpdateInfo | null> | null = null;
let installing: Promise<void> | null = null;
let returnFocus: HTMLElement | null = null;

function set(patch: Partial<UpdateState>) {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useUpdateStatus(): UpdateState {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

const plain = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback);
const delay = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

/** Read the stored last check, the setting and any download once per page
 * (again after a failed read). Local only: no GitHub request. */
export function loadUpdateStatus() {
  if (loading || state.loaded) return;
  loading = true;
  appUpdateStatus()
    .then((r) => {
      set({ loaded: true, loadFailed: false, last: state.last ?? r.last_check ?? null,
        autoCheck: state.autoCheck ?? r.auto_check ?? true, staging: state.staging ?? r.staging ?? null });
      void resumeInstall(r.staging ?? null, !!r.staged);
    })
    .catch(() => set({ loadFailed: true }))
    .finally(() => { loading = false; });
}

/** After a reload: follow a download still running, and give an update that
 * finished downloading to the desktop app again (no network). */
async function resumeInstall(staging: AppUpdateStaging | null, staged: boolean) {
  const bridge = desktopUpdate();
  if (!bridge) return;
  let support: DesktopUpdateSupport;
  try { support = await bridge.support(); } catch { return; }
  set({ support, ready: state.ready ?? support.ready ?? null });
  if (!support.supported || state.ready || installing) return;
  if (staging?.state === "running" || staged) void run(() => finishInstall(bridge, staging));
}

/** Ask the desktop app whether this copy can update in place (no network).
 * A failed answer reads as "no", so Download update stays available. */
export async function readUpdateSupport() {
  const bridge = desktopUpdate();
  if (!bridge) return;
  try {
    const support = await bridge.support();
    set({ support, ready: state.ready ?? support.ready ?? null });
  } catch {
    set({ support: { ok: false, supported: false, reason: null, ready: null } });
  }
}

/** Ask GitHub for the latest release: a click, or the automatic check. */
export function checkForUpdates(): Promise<AppUpdateInfo | null> {
  if (inFlight) return inFlight;
  set({ checking: true });
  inFlight = checkAppUpdate(true)
    .then((info) => {
      const checkedAt = info.checked_at || new Date().toISOString();
      const last: AppUpdateLastCheck = info.last_check ?? (info.error
        ? { checked_at: checkedAt, outcome: "failed", current_version: info.current_version, latest_version: null,
            reason: info.reason || info.error }
        : { checked_at: checkedAt, outcome: info.update_available ? "available" : "up_to_date",
            current_version: info.current_version, latest_version: info.latest_version, reason: null });
      const usable = info.error ? null : info;
      set({ loaded: true, loadFailed: false, last, info: usable });
      return usable;
    })
    .catch((e: unknown) => {
      // The workspace itself did not answer: shown for this window only.
      set({ loaded: true, loadFailed: false, info: null, last: { checked_at: new Date().toISOString(), outcome: "failed",
        current_version: appVersion(), latest_version: null,
        reason: e instanceof Error ? e.message.replace(/\.$/, "") : "the request failed" } });
      return null;
    })
    .finally(() => {
      inFlight = null;
      set({ checking: false });
    });
  return inFlight;
}

/** Whether the automatic check is due: never checked, a result older than a
 * day, or a failure more than an hour old. */
export function automaticCheckDue(last: AppUpdateLastCheck | null, now = Date.now()): boolean {
  if (!last) return true;
  const at = Date.parse(last.checked_at);
  if (Number.isNaN(at)) return true;
  const age = now - at;
  return age > STALE_AFTER_MS || (last.outcome === "failed" && age > RETRY_FAILED_AFTER_MS);
}

let automatic: number | null = null;
/** At start (after 10 s) and every hour, check when due and the setting is on. */
export function startAutomaticChecks() {
  if (automatic !== null) return;
  loadUpdateStatus();
  const tick = () => {
    if (state.autoCheck === true && state.loaded && !state.checking && !state.installing && !state.ready
        && automaticCheckDue(state.last)) void checkForUpdates();
  };
  window.setTimeout(tick, 10_000);
  automatic = window.setInterval(tick, 60 * 60 * 1000);
}

let settingWrites = 0;
/** Turn the automatic check on or off (stored in the workspace store). */
export async function setAutoCheck(on: boolean): Promise<void> {
  const mine = ++settingWrites;
  const r = await appUpdateSettings(on);
  if (mine === settingWrites) set({ autoCheck: r.auto_check === true });
}

function run(work: () => Promise<void>): Promise<void> {
  if (!installing) installing = work().finally(() => { installing = null; });
  return installing;
}

/** Update: check again when needed, download and verify the update file, then
 * hand it to the desktop app. Progress and errors land in the shared state. */
export function installUpdate(): Promise<void> {
  return run(async () => {
    const bridge = desktopUpdate();
    if (!bridge || state.ready) return;
    set({ installing: true, installError: null });
    try {
      const support = await bridge.support();
      set({ support });
      if (!support.supported) throw new Error(support.reason || "This copy of myflames cannot update itself.");
      // The backend downloads only the file its last check found, so a result
      // read from the store (after a restart) is checked again first.
      const info = state.info?.update_available ? state.info : await checkForUpdates();
      if (!info?.update_available) {
        throw new Error(state.last?.outcome === "failed" ? `Could not check: ${state.last.reason}` : "No newer version was found.");
      }
      const staging = await stageAppUpdate();
      set({ staging });
      await finishInstall(bridge, staging);
    } catch (e) {
      set({ installError: plain(e, "The update could not be installed."), installing: false });
    }
  });
}

async function finishInstall(bridge: DesktopUpdateBridge, started: AppUpdateStaging | null) {
  set({ installing: true, installError: null });
  try {
    let staging = started;
    let misses = 0;
    while (staging?.state === "running") {
      await delay(750);
      try {
        const status = await appUpdateStatus();
        staging = status.staging ?? null;
        misses = 0;
      } catch (e) {
        if (++misses >= 5) throw e;
      }
      set({ staging });
    }
    if (staging && staging.state === "failed") throw new Error(staging.reason || "The download failed.");
    const prepared = await bridge.prepare();
    if (!prepared.ok) throw new Error(prepared.reason || "The update could not be installed.");
    set({ ready: prepared.version || staging?.version || null, installing: false });
  } catch (e) {
    set({ installError: plain(e, "The update could not be installed."), installing: false });
  }
}

/** Restart to update: the desktop app stops the backend, installs and reopens. */
export async function restartToUpdate(): Promise<void> {
  const bridge = desktopUpdate();
  if (!bridge || !state.ready) return;
  set({ installError: null });
  try {
    const result = await bridge.restart();
    if (!result.ok) set({ installError: result.reason || "myflames could not restart." });
  } catch (e) {
    set({ installError: plain(e, "myflames could not restart.") });
  }
}

/** Open Software update. Without a fresh answer, an available result is checked again first (it holds the download). */
export async function openUpdateDialog(from?: HTMLElement | null) {
  returnFocus = from ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
  const info = state.info?.update_available || state.ready || state.installing ? state.info : await checkForUpdates();
  set({ dialog: { info: info ?? state.info } });
}
export function closeUpdateDialog() {
  set({ dialog: null });
}
export const updateDialogReturnFocus = () => returnFocus;

export type UpdatePhase = "available" | "downloading" | "preparing" | "ready";

/** What the badge beside the logo shows, or null when there is nothing to do. */
export function updatePhase(s: UpdateState, now = new Date()): UpdatePhase | null {
  if (s.ready) return "ready";
  if (s.installing) return s.staging?.state === "running" ? "downloading" : "preparing";
  if (s.last?.outcome !== "available") return null;
  const when = checkedWhen(s.last.checked_at, now);
  return when && !when.stale ? "available" : null;
}

/** 0-100 while downloading, when the size is known. */
export function stagingPercent(staging: AppUpdateStaging | null): number | null {
  if (!staging || !staging.total || staging.total <= 0) return null;
  return Math.max(0, Math.min(100, Math.floor((staging.received / staging.total) * 100)));
}

export function updatePhaseLabel(s: UpdateState, phase: UpdatePhase): string {
  const version = s.ready || s.staging?.version || s.last?.latest_version || s.info?.latest_version || "";
  const name = version ? `myflames ${version}` : "The update";
  if (phase === "ready") return `${name} is ready: restart to update`;
  if (phase === "preparing") return `Preparing ${name}…`;
  if (phase === "downloading") {
    const percent = stagingPercent(s.staging);
    return percent === null ? `Downloading ${name}…` : `Downloading ${name}: ${percent}%`;
  }
  return `${name} is available`;
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** "today at 10:42", "yesterday", "Oct 18" (with the year when it differs), in local time. */
export function checkedWhen(iso: string, now = new Date()): { text: string; title: string; stale: boolean } | null {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const text = sameDay(at, now)
    ? `today at ${at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`
    : sameDay(at, yesterday)
      ? "yesterday"
      : at.toLocaleDateString(undefined, at.getFullYear() === now.getFullYear()
        ? { month: "short", day: "numeric" }
        : { month: "short", day: "numeric", year: "numeric" });
  return {
    text,
    title: at.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" }),
    stale: now.getTime() - at.getTime() > STALE_AFTER_MS,
  };
}

/** The status words for a last check. A stale result names itself as the last result. */
export function updateStatusText(last: AppUpdateLastCheck | null, stale: boolean, loadFailed = false): string {
  if (!last) return loadFailed ? "Last check unknown" : "Not checked yet";
  if (last.outcome === "failed") return `Could not check: ${last.reason || "unknown reason"}`;
  const found = last.outcome === "available" ? `${last.latest_version} available` : "Up to date";
  return stale ? `Last result: ${found.charAt(0).toLowerCase()}${found.slice(1)}` : found;
}
