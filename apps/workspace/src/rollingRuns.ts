// The one rolling variable change run, owned outside the sheet so a
// connection change, a group switch or a Parameters report refresh while
// apply runs never loses it: the progress id, the original Group connection
// payload, the reviewed plan, the latest progress and the eventual result all
// live here. The sheet and the Topology place banner read the run through
// useRollingRun; only the sheet starts a run, and only from a reviewed plan.
// A run answers only for its group incarnation (the group name plus the
// sorted member ids with their host:port): another group, or the same name
// after a membership edit, gets the run's banner, never its sheet. Stop
// stays available until the backend reports a terminal state.
import { useSyncExternalStore } from "react";
import { ApiError, type Connection } from "./api";
import { writeFailure } from "./kit";
import {
  applyTopologyChange, rollingChangeProgress, stopTopologyChange,
  type ParameterRow, type RollingChangePlan, type RollingChangeResult, type RollingProgress,
  type TopologyGroupReport,
} from "./topologyApi";

export type RollingRun = {
  progressId: string;
  /** The Group connection the plan was reviewed on; apply and stop keep it. */
  connection: Connection;
  /** The Parameters read and row the sheet opened from; Show reopens them. */
  report: TopologyGroupReport;
  row: ParameterRow;
  plan: RollingChangePlan;
  /** The group incarnation this run answers for (runBinding): another group
   * or an edited membership never attaches it to a sheet. */
  binding: string;
  progress: RollingProgress | null;
  result: RollingChangeResult | null;
  /** The apply's reply never arrived (network error, abort); progress with
   * the same id and the original connection stays the only truth. */
  lostReply: boolean;
  /** A terminal state after a lost reply: progress finished, or answered
   * that it does not know this progress id for the grace period. The last
   * snapshot is kept. */
  lostDone: boolean;
  /** The terminal phase was "unknown": the server does not know this
   * progress id (the apply never started, the server restarted, or the
   * entry expired). */
  lostUnknown: boolean;
  /** After a lost reply, the progress reads are failing. The run keeps
   * polling with backoff; failed polls never end it. */
  progressSilent: boolean;
  /** The apply was answered with an error. */
  applyError: string | null;
  /** The backend acknowledged the Stop; only then is the Stop button off. */
  stopAcked: boolean;
  /** A failed Stop's error, shown beside the still available Stop. */
  stopError: string | null;
};

const POLL_MS = 1000;
/** The slowest progress poll after a lost reply: 1 s, doubling to 10 s. */
const MAX_POLL_MS = 10000;
/** Validation can outlive a lost reply before registering the progress id. */
const UNKNOWN_GRACE_MS = 30000;

/** The group incarnation a run answers for: the group name plus the sorted
 * member ids with their host:port. The same name with another membership (an
 * edit) is another incarnation; another group never attaches the run. */
export function runBinding(groupName: string, members: { profile_id: string; host: string; port: number }[]): string {
  const ids = members.map((m) => `${m.profile_id}@${m.host}:${m.port}`).sort().join(",");
  return `${groupName}|${ids}`;
}

let run: RollingRun | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

export function getRollingRun(): RollingRun | null {
  return run;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useRollingRun(): RollingRun | null {
  return useSyncExternalStore(subscribe, getRollingRun);
}

/** Terminal from the UI's side: an answered result, an answered error, or a
 * lost reply whose progress ended. */
export function runTerminal(current: RollingRun): boolean {
  return !!(current.result || current.applyError || current.lostDone);
}

/** The banner's "server k of n": the member the progress names, else the
 * first in the plan's order. */
export function runMemberSpot(current: RollingRun): { at: number; of: number } {
  const of = current.plan.members.length;
  const id = current.progress?.profile_id;
  const at = id ? current.plan.members.findIndex((m) => m.profile_id === id) + 1 : 0;
  return { at: at || 1, of };
}

/** Drop the run (the result sheet was closed, or a new reviewed run starts). */
export function clearRollingRun(): void {
  run = null;
  emit();
}

function newProgressId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** Start the reviewed plan as the one run. Single flight, synchronous: a
 * second Apply before React commits the busy state finds the accepted run
 * and is refused; an unfinished run of any group is never replaced. A
 * previous terminal run is dropped; the sheet never calls this while an
 * unfinished run exists. */
export function startRollingRun(
  connection: Connection, report: TopologyGroupReport, row: ParameterRow,
  plan: RollingChangePlan, typedName: string | undefined,
): void {
  const binding = runBinding(connection.group_name || report.group_name, report.members);
  if (run && !runTerminal(run)) return;
  run = {
    progressId: newProgressId(),
    connection, report, row, plan, binding,
    progress: null, result: null, lostReply: false, lostDone: false, lostUnknown: false,
    progressSilent: false, applyError: null, stopAcked: false, stopError: null,
  };
  emit();
  void drive(run.progressId, plan, typedName);
}

/** The live run, only when it is still this drive's run. */
function currentRun(progressId: string): RollingRun | null {
  return run && run.progressId === progressId ? run : null;
}

function patch(progressId: string, next: Partial<RollingRun>): void {
  const current = currentRun(progressId);
  if (!current) return;
  run = { ...current, ...next };
  emit();
}

async function drive(progressId: string, plan: RollingChangePlan, typedName: string | undefined): Promise<void> {
  // Start the first unknown window at the lost reply, not at the first poll.
  // A real snapshot resets it, so a later unknown gets its own grace period.
  let unknownSince: number | null = null;
  const poll = async () => {
    // Failed progress reads never end the run: after a lost apply reply the
    // change may still be running, so the polls back off (1 s, doubling to
    // 10 s) and Stop stays available.
    let delay = POLL_MS;
    for (;;) {
      const current = currentRun(progressId);
      if (!current || runTerminal(current)) return;
      await new Promise((resolve) => setTimeout(resolve, delay));
      const waiting = currentRun(progressId);
      if (!waiting || runTerminal(waiting)) return;
      try {
        const snap = await rollingChangeProgress(progressId);
        const latest = currentRun(progressId);
        if (!latest || runTerminal(latest)) return;
        if (latest.lostReply && snap.phase === "unknown") {
          if (unknownSince === null) unknownSince = Date.now();
          if (Date.now() - unknownSince >= UNKNOWN_GRACE_MS) {
            patch(progressId, { lostDone: true, lostUnknown: true, progressSilent: false });
            return;
          }
          // Keep the last real snapshot, the warning and Stop while the
          // server may still be validating the apply before registering it.
          delay = Math.min(delay * 2, MAX_POLL_MS);
          patch(progressId, { progressSilent: true });
          continue;
        }
        delay = POLL_MS;
        unknownSince = null;
        patch(progressId, { progress: snap, progressSilent: false });
        // The reply was lost: the server's own terminal phase ends the run.
        if (latest.lostReply && (snap.phase === "finished" || snap.status)) {
          patch(progressId, { lostDone: true });
          return;
        }
      } catch {
        // Progress only describes the send; the apply's own reply decides.
        // After a lost reply the polls keep trying, slower each time, and
        // the sheet says the server is not answering.
        delay = Math.min(delay * 2, MAX_POLL_MS);
        const latest = currentRun(progressId);
        if (latest && latest.lostReply) patch(progressId, { progressSilent: true });
      }
    }
  };
  void poll();
  try {
    const answer = await applyTopologyChange(currentRun(progressId)!.connection, plan, true,
      plan.confirmation.typed_name !== null ? typedName : undefined, progressId);
    if (!currentRun(progressId)) return;
    patch(progressId, { result: answer });
  } catch (error) {
    if (!currentRun(progressId)) return;
    if (error instanceof ApiError) {
      // The server answered: nothing is running any more.
      patch(progressId, { applyError: writeFailure(error, { audited: true }).message });
    } else {
      // No reply arrived (network error, abort): the change may still be
      // running. Keep polling the same progress id on the original
      // connection; Stop stays available.
      unknownSince = Date.now();
      patch(progressId, { lostReply: true });
    }
  }
}

/** Ask the backend to prevent the next member from starting. A failure is
 * kept on the run (stopError) and Stop stays available for a retry; only an
 * acknowledged Stop sets stopAcked. Bound to the group, not the active
 * member: the original Group connection payload goes with the call. */
export async function stopRollingRun(): Promise<void> {
  const current = run;
  if (!current || runTerminal(current)) return;
  try {
    await stopTopologyChange(current.connection, current.progressId);
    patch(current.progressId, { stopAcked: true, stopError: null });
  } catch (error) {
    patch(current.progressId, { stopError: (error as Error).message || String(error) });
  }
}
