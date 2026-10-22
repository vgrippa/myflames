// Pan/zoom for a nested <svg> viewport (ported from workbench.js).
// One paint path updates the viewBox, the zoom percentage, and the
// directional button limits together, so buttons, wheel, fit, and reset
// always agree. Wheel deltas are normalized by deltaMode and eased with a
// bounded per-frame step so trackpad momentum cannot jump.

export interface ZoomButtons {
  fit: HTMLButtonElement;
  zoomIn: HTMLButtonElement;
  zoomOut: HTMLButtonElement;
  reset: HTMLButtonElement;
}

export interface ZoomOptions {
  root: SVGSVGElement;
  viewport: SVGSVGElement;
  width: number;
  height: number;
  graphWidth: number;
  graphHeight: number;
  buttons: ZoomButtons;
  /** Workspace canvases fit width at a readable scale; tall trees can pan. */
  fitWidth?: boolean;
}

export interface ZoomController {
  fit(): void;
  reset(): void;
  zoom(factor: number, px: number, py: number): void;
  /** Resize the visible canvas without discarding a user's zoom or pan. */
  resize(width: number, height: number): void;
  /** True when the current pointer gesture moved beyond the drag threshold. */
  dragged(): boolean;
  destroy(): void;
}

interface Box { x: number; y: number; w: number; h: number }

export function createZoom(options: ZoomOptions): ZoomController {
  const { root, viewport, graphWidth, graphHeight, buttons } = options;
  let { width, height } = options;
  const win = root.ownerDocument.defaultView || window;
  let box: Box = { x: 0, y: 0, w: width, h: height };
  let drag: { x: number; y: number; bx: number; by: number; scaleX: number; scaleY: number } | null = null;
  let moved = false;
  let wheelFrame: number | null = null;
  let wheelDelta = 0;
  let wheelPoint = { x: 0, y: 0 };
  let wheelTime: number | null = null;
  let wheelScreen: { left: number; top: number; width: number; height: number; pageHeight: number } | null = null;
  let fitted = false;
  const minimum = () => width / 5;
  const maximum = () => Math.max(width * 8, graphWidth * 2, graphHeight * width / height * 2);
  const clamp = (value: number) => Math.min(maximum(), Math.max(minimum(), value));
  const cleanups: Array<() => void> = [];
  const on = <K extends keyof SVGElementEventMap>(
    target: SVGElement, type: K, listener: (event: SVGElementEventMap[K]) => void,
    opts?: AddEventListenerOptions,
  ) => {
    target.addEventListener(type, listener as EventListener, opts);
    cleanups.push(() => target.removeEventListener(type, listener as EventListener, opts));
  };

  function stopWheel() {
    if (wheelFrame !== null) win.cancelAnimationFrame(wheelFrame);
    wheelFrame = null;
    wheelDelta = 0;
    wheelTime = null;
    wheelScreen = null;
  }

  function paint() {
    viewport.setAttribute("viewBox", [box.x, box.y, box.w, box.h].join(" "));
    const percent = Math.round(width / box.w * 100) + "%";
    buttons.reset.textContent = percent;
    buttons.reset.setAttribute("aria-label", "Zoom " + percent + ". Reset to 100%");
    buttons.reset.setAttribute("title", "Current zoom: " + percent + ". Click to reset to 100%");
    buttons.zoomIn.disabled = box.w <= minimum() + 0.00001;
    buttons.zoomOut.disabled = box.w >= maximum() - 0.00001;
  }

  function fit() {
    stopWheel();
    // A full-tree fit on a phone makes text microscopic. In the workspace,
    // fit width between 75% and 100%, then start at the first input. Keep the
    // report's overview fit, and allow manual zoom out beyond the fit floor.
    const ratio = options.fitWidth
      ? Math.max(1, Math.min(1 / 0.75, (graphWidth + 56) / width))
      : Math.max((graphWidth + 56) / width, (graphHeight + 56) / height);
    box.w = clamp(width * ratio);
    box.h = box.w * height / width;
    box.x = options.fitWidth && graphWidth + 56 > box.w ? -28 : (graphWidth - box.w) / 2;
    box.y = options.fitWidth ? -28 : (graphHeight - box.h) / 2;
    fitted = true;
    paint();
  }

  function reset() {
    stopWheel();
    fitted = false;
    box = { x: (graphWidth - width) / 2, y: -28, w: width, h: height };
    paint();
  }

  function zoom(factor: number, px: number, py: number) {
    fitted = false;
    const next = clamp(box.w * factor);
    const ratio = next / box.w;
    box.x += box.w * px * (1 - ratio);
    box.y += box.h * py * (1 - ratio);
    box.w = next;
    box.h *= ratio;
    paint();
  }

  function resize(nextWidth: number, nextHeight: number) {
    if (!Number.isFinite(nextWidth) || !Number.isFinite(nextHeight) || nextWidth <= 0 || nextHeight <= 0 ||
        (nextWidth === width && nextHeight === height)) return;
    stopWheel();
    const scale = width / box.w;
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    width = nextWidth;
    height = nextHeight;
    if (fitted) { fit(); return; }
    box.w = clamp(width / scale);
    box.h = box.w * height / width;
    box.x = cx - box.w / 2;
    box.y = cy - box.h / 2;
    paint();
  }

  function animateWheel(now: number) {
    // A bounded logarithmic target avoids a backlog from macOS momentum
    // events; elapsed time is capped so a background tab cannot jump.
    const elapsed = wheelTime === null ? 16 : Math.min(32, Math.max(1, now - wheelTime));
    wheelTime = now;
    let step = wheelDelta * (1 - Math.exp(-elapsed / 55));
    step = Math.max(-elapsed * 0.0006, Math.min(elapsed * 0.0006, step));
    if (Math.abs(wheelDelta - step) <= 0.0008) {
      step = wheelDelta;
      wheelDelta = 0;
    } else {
      wheelDelta -= step;
    }
    if (!wheelScreen || !(wheelScreen.width > 0) || !(wheelScreen.height > 0)) {
      stopWheel();
      return;
    }
    const px = (wheelPoint.x - wheelScreen.left) / wheelScreen.width;
    const py = (wheelPoint.y - wheelScreen.top) / wheelScreen.height;
    zoom(Math.exp(step), px, py);
    if (Math.abs(wheelDelta) > 0.00001) {
      wheelFrame = win.requestAnimationFrame(animateWheel);
    } else {
      stopWheel();
    }
  }

  const actions: Array<[HTMLButtonElement, () => void]> = [
    [buttons.fit, fit],
    [buttons.zoomIn, () => zoom(0.8, 0.5, 0.5)],
    [buttons.zoomOut, () => zoom(1.25, 0.5, 0.5)],
    [buttons.reset, reset],
  ];
  for (const [button, action] of actions) {
    let feedback: number | undefined;
    const click = () => {
      if (button.disabled) return;
      win.clearTimeout(feedback);
      button.classList.add("wb-activated");
      feedback = win.setTimeout(() => button.classList.remove("wb-activated"), 450);
      stopWheel();
      action();
    };
    button.addEventListener("click", click);
    cleanups.push(() => { button.removeEventListener("click", click); win.clearTimeout(feedback); });
  }

  on(viewport, "wheel", (event: WheelEvent) => {
    // Pixel deltas from a Magic Mouse/trackpad are not wheel notches:
    // normalize line/page devices, then scale by actual distance.
    if (!Number.isFinite(event.deltaY) || Math.abs(event.deltaY) < 0.01 ||
        Math.abs(event.deltaX) > Math.abs(event.deltaY) || drag) return;
    event.preventDefault();
    // The nested viewport's bounds include the oversized pan background;
    // use its visible height at the parent's screen scale for page units.
    if (!wheelScreen) {
      const matrix = viewport.getScreenCTM();
      if (!matrix) return;
      const parentMatrix = root.getScreenCTM();
      wheelScreen = {
        left: matrix.a * box.x + matrix.e,
        top: matrix.d * box.y + matrix.f,
        width: matrix.a * box.w,
        height: matrix.d * box.h,
        pageHeight: height * (parentMatrix ? Math.abs(parentMatrix.d) : 1),
      };
    }
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? wheelScreen.pageHeight : 1;
    const delta = Math.max(-100, Math.min(100, event.deltaY * unit)) * 0.001;
    // Reversing a gesture responds immediately, without old momentum.
    if (wheelDelta * delta < 0) wheelDelta = 0;
    wheelDelta = Math.max(-0.16, Math.min(0.16, wheelDelta + delta));
    wheelPoint = { x: event.clientX, y: event.clientY };
    if (wheelFrame === null) wheelFrame = win.requestAnimationFrame(animateWheel);
  }, { passive: false });

  on(viewport, "pointerdown", (event: PointerEvent) => {
    if (event.button !== 0) return;
    stopWheel();
    moved = false;
    const matrix = viewport.getScreenCTM();
    if (!matrix || !matrix.a || !matrix.d) return;
    drag = { x: event.clientX, y: event.clientY, bx: box.x, by: box.y, scaleX: matrix.a, scaleY: matrix.d };
  });
  on(viewport, "pointermove", (event: PointerEvent) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 4) {
      fitted = false;
      moved = true;
      if (!viewport.hasPointerCapture(event.pointerId)) viewport.setPointerCapture(event.pointerId);
      box.x = drag.bx - dx / drag.scaleX;
      box.y = drag.by - dy / drag.scaleY;
      paint();
    }
  });
  const release = (event: PointerEvent) => {
    drag = null;
    if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId);
  };
  on(viewport, "pointerup", release);
  on(viewport, "pointercancel", release);
  on(viewport, "pointerleave", () => { if (!moved) drag = null; });
  on(viewport, "dblclick", fit);

  return {
    fit,
    reset,
    zoom,
    resize,
    dragged: () => moved,
    destroy() {
      stopWheel();
      cleanups.splice(0).forEach((cleanup) => cleanup());
    },
  };
}
