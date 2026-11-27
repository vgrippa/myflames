// Skip-loop polling that does not gate the first screen: the one-time poll of
// a restored workspace's other members, the retry of failed reads, the refresh
// of loops running on other members, and the shell's Stop for those loops.
// The shell (main.tsx) keeps the state and the render; these workers run only
// after a restore with saved loops or while another member's loop is active,
// so they load with their own chunk (docs/DEVELOPER_REFERENCE.md, "Bundle and
// lazy loading"), preloaded beside the first screen so a poll or a Stop never
// waits on it.
import type { Dispatch, SetStateAction } from "react";
import type { Connection, Profile } from "./api";
import { connectionKey, connectionLabel, endpointKey, type Bound } from "./binding";
import { LOOP_POLL_MS, loopActive, readSkipLoops, stopSkipLoop, type SkipLoop } from "./opsApi";

/** The shell's restoredMembers state (main.tsx), published by these polls. */
export type RestoredMembersState = {
  reset: number;
  activeKeys: string[];
  stoppedKeys: string[];
  outsideGroup: { id: string; name: string; group: string; hadGroup: boolean }[];
  failedMembers: { id: string; name: string }[];
};

/** Poll each restored same-group member's loop registry once (no database
 * login). Resolves true when the result was applied; false when the read was
 * aborted or there was nothing to poll, so the caller re-arms on the next
 * change, exactly as the inline effect did. */
export async function pollRestoredMembers(args: {
  resetKey: number;
  members: string[];
  profiles: Profile[];
  activeId: string;
  seenProfileGroups: Set<string>;
  loopMemberRequest: (id: string) => Connection | null;
  restoredLoopForCurrent: boolean;
  publishReplicationLoops: (next: Bound<SkipLoop[]>) => void;
  setRestoredMembers: Dispatch<SetStateAction<RestoredMembersState | null>>;
  syncLoopSession: (active: boolean, members?: string[]) => void;
}, signal: AbortSignal): Promise<boolean> {
  const { resetKey, members, profiles, activeId, seenProfileGroups, loopMemberRequest, restoredLoopForCurrent } = args;
  const currentGroup = (profiles.find((p) => p.id === activeId)?.group || "").trim();
  const toPoll: { id: string; c: Connection; key: string; label: string; name: string }[] = [];
  const outsideGroup: { id: string; name: string; group: string; hadGroup: boolean }[] = [];
  for (const id of members) {
    const target = profiles.find((p) => p.id === id);
    const c = loopMemberRequest(id);
    if (c) {
      toPoll.push({ id, c, key: connectionKey(c), label: connectionLabel(c), name: target?.name || connectionLabel(c) });
      continue;
    }
    if (target) {
      const targetGroup = (target.group || "").trim();
      outsideGroup.push({
        id,
        name: target.name || id,
        group: targetGroup,
        hadGroup: !!targetGroup || !!currentGroup || seenProfileGroups.has(id),
      });
    }
  }
  if (toPoll.length === 0) {
    args.setRestoredMembers({ reset: resetKey, activeKeys: [], stoppedKeys: [], outsideGroup, failedMembers: [] });
    if (!restoredLoopForCurrent) {
      if (outsideGroup.length === 0) args.syncLoopSession(false, []);
      else args.syncLoopSession(true, outsideGroup.map((m) => m.id));
    }
    return false;
  }
  const results = await Promise.allSettled(
    toPoll.map(async (item) => ({ item, loops: (await readSkipLoops(item.c, signal)).loops })),
  );
  if (signal.aborted) return false;
  const activeKeys: string[] = [];
  const stoppedKeys: string[] = [];
  const failedMembers: { id: string; name: string }[] = [];
  for (let i = 0; i < results.length; i++) {
    const r = results[i], item = toPoll[i];
    if (r.status === "fulfilled") {
      args.publishReplicationLoops({ key: item.key, label: item.label, report: r.value.loops });
      if (r.value.loops.some(loopActive)) activeKeys.push(item.key);
      else if (r.value.loops.length === 0) stoppedKeys.push(item.key);
    } else {
      failedMembers.push({ id: item.id, name: item.name });
    }
  }
  args.setRestoredMembers({ reset: resetKey, activeKeys, stoppedKeys, outsideGroup, failedMembers });
  if (!failedMembers.length && !activeKeys.length && !outsideGroup.length && !restoredLoopForCurrent) {
    args.syncLoopSession(false, []);
  }
  return true;
}

/** Re-read restored members whose initial read failed (after LOOP_POLL_MS)
 * and outside-group members that later became reachable (at once, then every
 * LOOP_POLL_MS on failure) through loopMemberRequest. Returns a stop function;
 * the abort signal stops it too. */
export function startRestoreRetry(args: {
  ids: string[];
  failIds: string[];
  reachIds: string[];
  resetKey: number;
  profilesStatus: () => { loaded: boolean; failed: boolean };
  profiles: () => Profile[];
  loopMemberRequest: (id: string) => Connection | null;
  publishReplicationLoops: (next: Bound<SkipLoop[]>) => void;
  setRestoredMembers: Dispatch<SetStateAction<RestoredMembersState | null>>;
}, signal: AbortSignal): () => void {
  const { ids, reachIds, resetKey } = args;
  const failSet = new Set(args.failIds);
  let timer: number | undefined;
  async function tick(batch: string[]) {
    const known = args.profilesStatus().loaded && !args.profilesStatus().failed;
    const toTry: { id: string; c: Connection; key: string; label: string }[] = [];
    const dropped = new Set<string>();
    const movedOutside: { id: string; name: string; group: string; hadGroup: boolean }[] = [];
    for (const id of batch) {
      const c = args.loopMemberRequest(id);
      if (c) toTry.push({ id, c, key: connectionKey(c), label: connectionLabel(c) });
      else if (known && failSet.has(id)) {
        const t = args.profiles().find((p) => p.id === id);
        if (!t) dropped.add(id);
        else movedOutside.push({ id, name: t.name || id, group: (t.group || "").trim(), hadGroup: true });
      }
    }
    const ok = new Set<string>(), newlyActive: string[] = [], newlyStopped: string[] = [];
    if (toTry.length) {
      const results = await Promise.allSettled(toTry.map(async (item) => ({ item, loops: (await readSkipLoops(item.c, signal)).loops })));
      if (signal.aborted) return;
      for (const r of results) {
        if (r.status !== "fulfilled") continue;
        const { item, loops } = r.value;
        ok.add(item.id);
        args.publishReplicationLoops({ key: item.key, label: item.label, report: loops });
        if (loops.some(loopActive)) newlyActive.push(item.key);
        else if (loops.length === 0) newlyStopped.push(item.key);
      }
    }
    if (signal.aborted) return;
    const done = new Set([...dropped, ...ok, ...movedOutside.map((m) => m.id)]);
    if (done.size) {
      args.setRestoredMembers((prev) => {
        if (!prev || prev.reset !== resetKey) return prev;
        const seen = new Set(prev.outsideGroup.map((m) => m.id));
        return {
          ...prev,
          activeKeys: [...new Set([...prev.activeKeys, ...newlyActive])],
          stoppedKeys: [...new Set([...prev.stoppedKeys, ...newlyStopped])],
          outsideGroup: [...prev.outsideGroup.filter((m) => !ok.has(m.id)), ...movedOutside.filter((m) => !seen.has(m.id))],
          failedMembers: prev.failedMembers.filter((m) => !done.has(m.id)),
        };
      });
    }
    const left = ids.filter((id) => !done.has(id));
    if (!signal.aborted && left.length) {
      timer = window.setTimeout(() => { void tick(left); }, LOOP_POLL_MS);
    }
  }
  const stop = () => window.clearTimeout(timer);
  signal.addEventListener("abort", stop);
  if (reachIds.length) void tick(reachIds);
  else timer = window.setTimeout(() => { void tick(ids); }, LOOP_POLL_MS);
  return stop;
}

/** Refresh active skip loops running on other members (ReplicationView polls
 * only its own key) through the shell's endpoint-key Stop resolver. Registry
 * only, no database login; re-arms when a target cannot be resolved and marks
 * unresolved or failed rows "Status not refreshed" until a poll succeeds. */
export function startOtherLoopPoll(
  keys: string[],
  resolve: (boundKey: string) => Connection | null,
  publishReplicationLoops: (next: Bound<SkipLoop[]>) => void,
  setStaleEndpoints: Dispatch<SetStateAction<string[]>>,
  signal: AbortSignal,
): () => void {
  let timer: number | undefined;
  const updateStale = (staleSet: Set<string>) => {
    const next = [...staleSet].sort();
    setStaleEndpoints((prev) =>
      prev.length === next.length && prev.every((v, i) => v === next[i]) ? prev : next,
    );
  };
  async function poll() {
    const resolved = keys.map((boundKey) => ({
      boundKey,
      ep: endpointKey(boundKey),
      c: resolve(boundKey),
    }));
    const targets = resolved.filter((item): item is { boundKey: string; ep: string; c: Connection } => !!item.c);
    const stale = new Set(resolved.filter((item) => !item.c).map((item) => item.ep));
    if (targets.length) {
      const answers = await Promise.allSettled(
        targets.map(async (t) => ({ t, answer: await readSkipLoops(t.c, signal) })),
      );
      if (signal.aborted) return;
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
    if (!signal.aborted) {
      updateStale(stale);
      timer = window.setTimeout(() => { void poll(); }, LOOP_POLL_MS);
    }
  }
  const stop = () => window.clearTimeout(timer);
  signal.addEventListener("abort", stop);
  timer = window.setTimeout(() => { void poll(); }, LOOP_POLL_MS);
  return stop;
}

/** The shell's Stop for another member's loop: stop, then read the registry
 * back so the banner reflects the server. */
export async function stopAndReadSkipLoops(c: Connection, id: string) {
  await stopSkipLoop(c, id);
  return readSkipLoops(c);
}
