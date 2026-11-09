import type { DdlProgress } from "./api";

/** One read at a time, only in a visible document. The owning editor effect
 * disposes this watcher on Stop, completion, tab changes or leaving the screen.
 * Late replies belong to this watcher and cannot update its replacement. */
export function followDdlProgress(
  read: (signal: AbortSignal) => Promise<DdlProgress>,
  receive: (progress: DdlProgress) => void,
): () => void {
  let disposed = false;
  let timer = 0;
  let request: AbortController | null = null;
  const poll = async () => {
    if (disposed || document.hidden || request) return;
    const controller = new AbortController();
    request = controller;
    try {
      const progress = await read(controller.signal);
      if (!disposed && !controller.signal.aborted) receive(progress);
    } catch (e) {
      if (!disposed && !controller.signal.aborted) receive({
        available: false, reason: `Could not read DDL progress: ${(e as Error).message}`,
        missing: [], source: null, items: [],
      });
    } finally {
      request = null;
      if (!disposed && !document.hidden) timer = window.setTimeout(() => void poll(), 2000);
    }
  };
  const visibility = () => {
    window.clearTimeout(timer);
    request?.abort();
    // An aborted read still owns its slot until finally. Its continuation
    // schedules the next visible read if visibility changed during the await.
    if (!document.hidden && !request) void poll();
  };
  document.addEventListener("visibilitychange", visibility);
  void poll();
  return () => {
    disposed = true;
    window.clearTimeout(timer);
    request?.abort();
    document.removeEventListener("visibilitychange", visibility);
  };
}
