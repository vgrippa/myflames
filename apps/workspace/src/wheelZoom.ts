import { useEffect, useRef, type RefObject } from "react";

/** Logarithmic step, so several small trackpad deltas equal one wheel step. */
export function wheelZoomStep(
  event: { deltaX: number; deltaY: number; deltaMode: number },
  speedPercent: number,
  pageHeight: number,
): number | null {
  if (!Number.isFinite(event.deltaY) || Math.abs(event.deltaY) < 0.01 ||
      Math.abs(event.deltaX) > Math.abs(event.deltaY)) return null;
  // Mirror charts/core/zoom.ts: 16 px per line, visible page height, 100 px cap, 0.001 per px.
  const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? pageHeight : 1;
  const px = Math.max(-100, Math.min(100, event.deltaY * unit));
  return -px * 0.001 * speedPercent / 100;
}

/** Use a native non-passive listener and apply at most one zoom per frame. */
export function useWheelZoom<T extends HTMLElement>(
  ref: RefObject<T>,
  apply: (factor: number, clientX: number, clientY: number) => void,
  speedPercent: number,
) {
  const latest = useRef({ apply, speedPercent });
  latest.current = { apply, speedPercent };
  const binding = useRef<{ element: T; dispose: () => void } | null>(null);
  const pending = useRef({ frame: null as number | null, sum: 0, clientX: 0, clientY: 0 });

  // The canvas is conditional (empty state, Diagram/Mermaid switch). Check
  // its identity after each commit, retaining the listener on ordinary renders.
  useEffect(() => {
    const element = ref.current;
    if (binding.current?.element === element) return;
    binding.current?.dispose();
    binding.current = null;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      const step = wheelZoomStep(event, latest.current.speedPercent, element.clientHeight);
      if (step === null) return;
      event.preventDefault();
      const state = pending.current;
      state.sum += step;
      state.clientX = event.clientX; state.clientY = event.clientY;
      if (state.frame === null) state.frame = requestAnimationFrame(() => {
        state.frame = null;
        const factor = Math.exp(state.sum);
        state.sum = 0;
        latest.current.apply(factor, state.clientX, state.clientY);
      });
    };
    element.addEventListener("wheel", wheel, { passive: false });
    binding.current = { element, dispose: () => {
      element.removeEventListener("wheel", wheel);
      if (pending.current.frame !== null) cancelAnimationFrame(pending.current.frame);
      pending.current = { frame: null, sum: 0, clientX: 0, clientY: 0 };
    } };
  });
  useEffect(() => () => {
    binding.current?.dispose();
    binding.current = null;
  }, []);
}
