// The message a disabled button opens on click, Enter or Space: why the
// action is off and what fixes it, anchored to the button. It uses the HTML
// Popover API (popover="auto"): the top layer keeps it out of every scroll
// container, a click outside closes it (light dismiss), and opening another
// closes this one. It joins the workspace's shared popover and focus-scope
// coordination (kit.tsx) so the newest surface owns Escape and focus, consumes
// Escape only while focus is on its own button or inside it, closes when its
// anchor or place leaves the screen, and places itself inside the safe
// viewport so it never covers the fixed status bar or a sticky toolbar.
// Contents stay React nodes: server text can never become markup (S8, S10).
// Never build SQL text in the browser: any advisory GRANT comes verbatim from
// the backend's grant_sql field.
//
// WhyDisabledHost renders one shared message near the app root for
// privilege-disabled buttons (privileges.ts: privilegeButtonProps,
// PrivilegeGate, PrivilegeConfirmLabel) via the module-level openWhyDisabled
// function. When the clicked button lives inside a modal <dialog>, the host
// portals the popover into that dialog so modal inertness cannot block Close
// or Copy inside it.
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type PropsWithChildren,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";
import { Commands } from "./Commands";
import { Icon } from "./components";
import { POPOVER_EVENT, registerFocusScope } from "./kit";
import { useViewOnScreen } from "./viewOnScreen";
import {
  closeWhyDisabled,
  openWhyDisabled,
  registerWhyHost,
  type WhyDisabledRequest,
} from "./whyDisabledBus";
import "./catalog-actions.css";
export { closeWhyDisabled, openWhyDisabled, type WhyDisabledRequest };

/** @types/react 18.3.12 has no popover typings: the attribute goes through a
 * typed record, never an `any` prop. */
const POPOVER_ATTR: Record<string, string> = { popover: "auto" };
const FALLBACK_FOCUSABLE = 'button, [href], input, select, textarea, summary, [tabindex]';
const STICKY_TOPBAR_SELECTOR =
  ".topbar-stack, .view-toolbar, .place-toolbar, .editor-toolbar, .teach-toolbar, .connection-group-bar";
const ANCESTOR_ATTRS = ["hidden", "inert", "aria-hidden", "open", "style", "class"];
const CANCEL_LABEL = /^(cancel|close)$/i;

function restoreAttr(el: HTMLElement, attr: string, expected: string | null, prev: string | null) {
  if (expected !== null && el.getAttribute(attr) === expected) {
    if (prev === null) el.removeAttribute(attr);
    else el.setAttribute(attr, prev);
  }
}

function observeAncestors(el: HTMLElement, onChange: () => void): MutationObserver {
  const observer = new MutationObserver(onChange);
  for (let parent = el.parentElement; parent; parent = parent.parentElement) {
    observer.observe(parent, { attributes: true, attributeFilter: ANCESTOR_ATTRS });
  }
  return observer;
}

function isElementVisible(el: HTMLElement | null): el is HTMLElement {
  if (!el || !el.isConnected) return false;
  if (el.closest('[hidden], [inert], [aria-hidden="true"], dialog:not([open])')) return false;
  if (el.getClientRects().length === 0) return false;
  if (getComputedStyle(el).visibility === "hidden") return false;
  return true;
}

function isFocusableVisible(el: HTMLElement | null): el is HTMLElement {
  return isElementVisible(el) && !el.matches(":disabled");
}

function isAnchorDisabled(el: HTMLElement | null): boolean {
  return !!el && el.isConnected && (el.getAttribute("aria-disabled") === "true" || el.matches(":disabled"));
}

function isExcludedNode(node: HTMLElement, exclude: (HTMLElement | null)[]): boolean {
  return exclude.some((ex) => ex && (ex === node || ex.contains(node)));
}

function findDialogFallback(dialog: HTMLDialogElement, exclude: (HTMLElement | null)[]): HTMLElement | null {
  const candidates = [
    ...dialog.querySelectorAll<HTMLElement>(".sheet-actions button, .dialog-actions button"),
    ...dialog.querySelectorAll<HTMLElement>("button"),
  ];
  for (const cand of candidates) {
    if (isExcludedNode(cand, exclude) || !isFocusableVisible(cand)) continue;
    const text = (cand.textContent || "").replace(/\s+/g, " ").trim();
    const label = (cand.getAttribute("aria-label") || "").trim();
    if (CANCEL_LABEL.test(text) || CANCEL_LABEL.test(label)) return cand;
  }
  return isElementVisible(dialog) ? dialog : null;
}

function findVisibleFallback(containers: HTMLElement[], exclude: (HTMLElement | null)[]): HTMLElement | null {
  const candidates: (HTMLElement | null)[] = [
    ...containers,
    document.querySelector<HTMLElement>(".server-view:not([hidden])"),
    document.querySelector<HTMLElement>("main"),
    document.querySelector<HTMLElement>(".main-nav button.active"),
  ];
  const seen = new Set<HTMLElement>();
  for (const container of candidates) {
    if (!container || seen.has(container) || !isElementVisible(container)) continue;
    seen.add(container);
    if (container.matches(FALLBACK_FOCUSABLE) && container.tabIndex >= 0 && isFocusableVisible(container) && !isExcludedNode(container, exclude)) {
      return container;
    }
    const preferred = container.querySelector<HTMLElement>(".detail-back, .inspector-close, .view-switcher");
    if (preferred && isFocusableVisible(preferred) && !isExcludedNode(preferred, exclude)) return preferred;
    for (const node of container.querySelectorAll<HTMLElement>(FALLBACK_FOCUSABLE)) {
      if (node.tabIndex < 0 || isExcludedNode(node, exclude)) continue;
      if (isFocusableVisible(node)) return node;
    }
    const heading = container.querySelector<HTMLElement>('h2[tabindex="-1"]');
    if (heading && isFocusableVisible(heading) && !isExcludedNode(heading, exclude)) return heading;
  }
  return null;
}

function measureSafeViewport(anchor: HTMLElement, popover: HTMLElement) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const inModal = !!popover.closest("dialog[open]");
  let safeLeft = 8;
  let safeRight = Math.max(safeLeft + 120, vw - 8);
  let safeTop = 8;
  let safeBottom = Math.max(safeTop + 48, vh - 8);
  if (!inModal) {
    const statusBar = document.querySelector<HTMLElement>(".window-statusbar");
    if (statusBar && isElementVisible(statusBar)) {
      const r = statusBar.getBoundingClientRect();
      if (r.height > 0 && r.top > 0 && r.top < vh) {
        safeBottom = Math.min(safeBottom, Math.floor(r.top) - 4);
      }
    }
    const anchorRect = anchor.getBoundingClientRect();
    const anchorView = anchor.closest(".server-view, .workspace");
    for (const bar of document.querySelectorAll<HTMLElement>(STICKY_TOPBAR_SELECTOR)) {
      if (!isElementVisible(bar)) continue;
      const pos = getComputedStyle(bar).position;
      if (pos !== "sticky" && pos !== "fixed") continue;
      // Ignore sticky toolbars inside other views that are not sharing this view.
      const barView = bar.closest(".server-view");
      if (barView && anchorView && barView !== anchorView && !barView.contains(anchor)) continue;
      const r = bar.getBoundingClientRect();
      if (r.height <= 0 || r.bottom <= 0 || r.bottom > vh * 0.65) continue;
      if (r.right <= anchorRect.left - 8 && r.right < vw * 0.5) continue;
      if (r.left >= anchorRect.right + 8 && r.left > vw * 0.5) continue;
      safeTop = Math.max(safeTop, Math.ceil(r.bottom) + 4);
    }
    // Keep a usable band even on an unusually short viewport.
    if (safeBottom - safeTop < 48) {
      safeTop = Math.max(8, Math.min(safeTop, safeBottom - 48));
    }
  }
  return { vw, vh, safeLeft, safeRight, safeTop, safeBottom };
}

function placePopover(anchor: HTMLElement, el: HTMLElement) {
  const { vh, safeLeft, safeRight, safeTop, safeBottom } = measureSafeViewport(anchor, el);
  const availH = Math.max(48, safeBottom - safeTop);
  const maxCap = Math.max(48, Math.min(Math.floor(vh * 0.6), availH));
  const a = anchor.getBoundingClientRect();

  // Place with fixed viewport coordinates inside the safe band so the top-layer
  // popover never overlaps the fixed status bar or a sticky toolbar.
  el.style.setProperty("position-area", "none");
  el.style.position = "fixed";
  el.style.right = "auto";
  el.style.bottom = "auto";
  el.style.left = `${safeLeft}px`;
  el.style.top = `${safeTop}px`;
  el.style.maxHeight = `${maxCap}px`;

  const width = el.offsetWidth;
  const borderY = Math.max(0, el.offsetHeight - el.clientHeight);
  const naturalHeight = Math.min(el.scrollHeight + borderY, maxCap);
  const spaceBelow = safeBottom - a.bottom;
  const spaceAbove = a.top - safeTop;
  const placeBelow = naturalHeight <= spaceBelow || (spaceBelow >= spaceAbove && spaceBelow >= 48);

  if (placeBelow) {
    const top = Math.max(safeTop, Math.min(a.bottom, safeBottom - 48));
    const maxH = Math.max(48, Math.min(maxCap, safeBottom - top));
    el.style.top = `${Math.round(top)}px`;
    el.style.maxHeight = `${Math.floor(maxH)}px`;
  } else {
    const maxH = Math.max(48, Math.min(maxCap, spaceAbove));
    const height = Math.min(naturalHeight, maxH);
    const top = Math.max(safeTop, Math.min(a.top - height, safeBottom - height));
    el.style.top = `${Math.round(top)}px`;
    el.style.maxHeight = `${Math.floor(maxH)}px`;
  }

  const left = Math.max(safeLeft, Math.min(a.left, safeRight - width));
  el.style.left = `${Math.round(left)}px`;

  // Final post-clamp check in case wrapping or scrollbars changed height.
  const rect = el.getBoundingClientRect();
  if (rect.bottom > safeBottom) {
    const nudgedTop = Math.max(safeTop, Math.floor(safeBottom - rect.height));
    el.style.top = `${nudgedTop}px`;
    el.style.maxHeight = `${Math.max(48, Math.floor(safeBottom - nudgedTop))}px`;
  }
  if (rect.top < safeTop) {
    el.style.top = `${safeTop}px`;
    el.style.maxHeight = `${Math.max(48, Math.floor(safeBottom - safeTop))}px`;
  }
  const adjusted = el.getBoundingClientRect();
  if (adjusted.right > safeRight) {
    el.style.left = `${Math.max(safeLeft, Math.floor(safeRight - adjusted.width))}px`;
  }
}

function visibleButtonLabel(button: HTMLElement): string {
  const clone = button.cloneNode(true) as HTMLElement;
  clone.querySelectorAll(".account-sr, .visually-hidden, .privilege-unverified").forEach((n) => n.remove());
  const text = (clone.textContent || button.getAttribute("aria-label") || "this action")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[….]+$/, "");
  return text || "this action";
}

/** The message under a disabled button (CatalogActions.tsx wires it to the
 * action buttons of a catalog; ReadysetPlace wires it to disabled allowlist
 * Remove chips; WhyDisabledHost wires it to privilege-gated disabled buttons
 * across the workspace). */
export function WhyDisabled({ anchor, open, mayOpen = true, attachClick = true, label, onOpen, onClose, children }: {
  /** The button the message is anchored to (its element or a ref to it). */
  anchor: HTMLElement | RefObject<HTMLElement | null> | null;
  /** The owner's open state, kept in sync with the popover's toggle events. */
  open: boolean;
  /** When false the anchor does not toggle the message (a busy group). */
  mayOpen?: boolean;
  /** When true (CatalogActions, Readyset chips), this component attaches the
   * click toggle handler to the anchor. When false (WhyDisabledHost), the
   * anchor's own click handler opens and toggles the host while this effect
   * only prevents the native popovertarget default toggle. */
  attachClick?: boolean;
  /** Accessible name of the non-modal dialog (defaults to "Why <button> is unavailable"). */
  label?: string;
  onOpen: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  const pop = useRef<HTMLDivElement | null>(null);
  const plain = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const anchorName = `--why-${plain}`;
  const popId = `why-${plain}`;
  const onScreen = useViewOnScreen();

  // Latest callbacks and state for listeners that stay attached across renders.
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const openRef = useRef(open);
  openRef.current = open;
  const onScreenRef = useRef(onScreen);
  onScreenRef.current = onScreen;

  // Focus restoration tracking for unmount/close while focus is inside (W3, S-F1).
  const focusInsideRef = useRef(false);
  const outsidePointerRef = useRef(false);
  const explicitCloseRef = useRef(false);
  const hidingRef = useRef(false);
  const fallbackContainersRef = useRef<HTMLElement[]>([]);
  const appliedExpandedRef = useRef<{ button: HTMLElement; value: string } | null>(null);

  // Resolved at effect/event time: a ref's .current is empty during the first
  // render and set by the time effects and handlers run.
  const resolve = () => (anchor && "current" in anchor ? anchor.current : anchor);

  const hidePopover = (el: HTMLElement | null) => {
    if (!el) return;
    hidingRef.current = true;
    try {
      if (el.matches(":popover-open")) el.hidePopover();
    } catch { /* Already closed or detached. */ } finally {
      hidingRef.current = false;
    }
  };

  // S-F1: return focus to the anchor ONLY while it is still disabled. If the
  // gate opened (enabled), focus the open dialog's Cancel button (or dialog)
  // or a visible non-anchor fallback so a pending Enter/Space never confirms.
  const restoreFocus = (el: HTMLElement | null, button: HTMLElement | null) => {
    if (onScreenRef.current && isAnchorDisabled(button) && isFocusableVisible(button)) {
      button.focus({ preventScroll: true });
      return;
    }
    const dialog =
      (button?.isConnected ? button.closest<HTMLDialogElement>("dialog[open]") : null) ||
      (el?.isConnected ? el.closest<HTMLDialogElement>("dialog[open]") : null) ||
      fallbackContainersRef.current.find(
        (c): c is HTMLDialogElement => c.matches("dialog[open]") && isElementVisible(c),
      ) ||
      null;
    if (dialog && isElementVisible(dialog)) {
      findDialogFallback(dialog, [el, button])?.focus({ preventScroll: true });
      return;
    }
    findVisibleFallback(fallbackContainersRef.current, [el, button])?.focus({ preventScroll: true });
  };

  const closeToAnchor = () => {
    explicitCloseRef.current = true;
    const el = pop.current;
    const button = resolve();
    hidePopover(el);
    restoreFocus(el, button);
    onCloseRef.current();
  };

  // Anchor naming and invoker relationship: the button carries the anchor name
  // and popovertarget while clicks may open the message. Snapshot any prior
  // aria-haspopup/aria-expanded values React set and restore them on cleanup (V8).
  useEffect(() => {
    const button = resolve();
    const el = pop.current;
    if (!button || !el) return;
    const prevAnchorName = button.style.getPropertyValue("anchor-name");
    const prevPopoverTarget = button.getAttribute("popovertarget");
    const prevHasPopup = button.getAttribute("aria-haspopup");
    const prevExpanded = button.getAttribute("aria-expanded");
    button.style.setProperty("anchor-name", anchorName);
    el.style.setProperty("position-anchor", anchorName);
    const toggle = (event: MouseEvent) => {
      // Keep the invoker relationship for pointer light dismiss, but cancel
      // its default toggle because the owner manages opening and closing.
      event.preventDefault();
      if (!attachClick) return;
      if (openRef.current || el.matches(":popover-open")) {
        hidePopover(el);
        onCloseRef.current();
      } else {
        onOpenRef.current();
      }
    };
    if (mayOpen) {
      button.setAttribute("popovertarget", popId);
      button.setAttribute("aria-haspopup", "dialog");
      const expanded = String(openRef.current);
      button.setAttribute("aria-expanded", expanded);
      appliedExpandedRef.current = { button, value: expanded };
      button.addEventListener("click", toggle);
    }
    return () => {
      if (prevAnchorName) button.style.setProperty("anchor-name", prevAnchorName);
      else button.style.removeProperty("anchor-name");
      if (mayOpen) {
        restoreAttr(button, "popovertarget", popId, prevPopoverTarget);
        restoreAttr(button, "aria-haspopup", "dialog", prevHasPopup);
        const appliedExpanded = appliedExpandedRef.current;
        if (appliedExpanded?.button === button) {
          restoreAttr(button, "aria-expanded", appliedExpanded.value, prevExpanded);
          appliedExpandedRef.current = null;
        }
        button.removeEventListener("click", toggle);
      }
    };
  }, [anchor, anchorName, popId, mayOpen, attachClick]);

  // Keep aria-expanded in sync with open state without re-binding click handlers.
  useEffect(() => {
    const button = resolve();
    if (!button || !mayOpen) return;
    const expanded = String(open);
    button.setAttribute("aria-expanded", expanded);
    appliedExpandedRef.current = { button, value: expanded };
  }, [anchor, mayOpen, open]);

  // Close immediately when the enclosing view/place leaves the screen (W1).
  useEffect(() => {
    if (!onScreen && open) {
      hidePopover(pop.current);
      onCloseRef.current();
    }
  }, [onScreen, open]);

  // The owner state follows the popover's native light-dismiss closings.
  useEffect(() => {
    const el = pop.current;
    if (!el) return;
    const sync = (event: Event) => {
      const state = (event as ToggleEvent).newState;
      if (state === "open" && el.matches(":popover-open") && !openRef.current) onOpenRef.current();
      else if (state === "closed" && !el.matches(":popover-open") && openRef.current) onCloseRef.current();
    };
    el.addEventListener("toggle", sync);
    return () => el.removeEventListener("toggle", sync);
  }, []);

  // Show/hide the top-layer popover and keep it inside the safe viewport
  // (never covering the fixed status bar or a sticky toolbar, W6), closing
  // automatically if the anchor becomes hidden (W1, V2).
  useLayoutEffect(() => {
    const el = pop.current;
    const button = resolve();
    if (!el) return;
    if (!open) {
      if (el.matches(":popover-open")) {
        const hadFocus = el.contains(document.activeElement);
        if (hadFocus) focusInsideRef.current = true;
        hidePopover(el);
        if (hadFocus && button && document.activeElement === button && !isAnchorDisabled(button)) {
          restoreFocus(el, button);
        }
      }
      return;
    }
    if (!button || !onScreenRef.current || !isElementVisible(button)) {
      hidePopover(el);
      onCloseRef.current();
      return;
    }
    if (!el.matches(":popover-open")) {
      try { el.showPopover(); } catch { return; }
    }
    const update = () => {
      if (!onScreenRef.current || !isElementVisible(button)) {
        hidePopover(el);
        onCloseRef.current();
        return;
      }
      if (el.matches(":popover-open")) placePopover(button, el);
    };
    update();
    const ancestorObserver = observeAncestors(button, update);
    let resizeObserver: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(update);
      resizeObserver.observe(el);
      resizeObserver.observe(button);
    }
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      ancestorObserver.disconnect();
      resizeObserver?.disconnect();
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
      if (el.contains(document.activeElement)) {
        focusInsideRef.current = true;
      }
      hidePopover(el);
    };
  }, [open, anchor]);

  // Shared popover coordination (myflames-popover), focus-scope ordering
  // (registerFocusScope), scoped Escape handling (W1, W2), and focus
  // restoration when unmounting or closing with focus inside (W3, S-F1).
  useEffect(() => {
    if (!open) return;
    const el = pop.current;
    const button = resolve();
    if (!el || !button) return;

    fallbackContainersRef.current = [
      button.closest<HTMLElement>("dialog[open]"),
      button.closest<HTMLElement>(".view-inspector"),
      button.closest<HTMLElement>(".review-actions"),
      button.closest<HTMLElement>(".readyset-allowlist-chips"),
      button.closest<HTMLElement>(".server-view"),
      button.closest<HTMLElement>("main"),
    ].filter((n): n is HTMLElement => !!n);

    explicitCloseRef.current = false;
    outsidePointerRef.current = false;
    focusInsideRef.current = el.contains(document.activeElement);

    // Close any older workspace popover (About, Columns, Export, ViewSwitcher)
    // and close this message if a newer workspace popover opens.
    window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: popId }));
    const onOtherPopover = (event: Event) => {
      const detail = (event as CustomEvent).detail;
      if (detail === popId) return;
      const opened = typeof detail === "string" && detail ? document.getElementById(detail) : null;
      if (opened && el.contains(opened)) return;
      outsidePointerRef.current = true;
      hidePopover(el);
      onCloseRef.current();
    };
    window.addEventListener(POPOVER_EVENT, onOtherPopover);

    const ownsEscape = () => {
      if (!onScreenRef.current) return false;
      if (!el.matches(":popover-open")) return false;
      if (!isElementVisible(button)) return false;
      const active = document.activeElement;
      if (!(active instanceof Node)) return false;
      const native = active instanceof Element ? active.closest("dialog[open]") : null;
      if (native && native !== el && !native.contains(el)) return false;
      return button.contains(active) || el.contains(active);
    };
    const scope = registerFocusScope(el, { opener: button, ownsEscape });

    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (!ownsEscape() || !scope.isTopForEscape()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      closeToAnchor();
    };
    window.addEventListener("keydown", onEscape, true);

    const onFocusIn = () => {
      focusInsideRef.current = true;
      outsidePointerRef.current = false;
    };
    const onFocusOut = (event: FocusEvent) => {
      if (hidingRef.current) return;
      const next = event.relatedTarget as Node | null;
      if (next && el.contains(next)) return;
      if (next) focusInsideRef.current = false;
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && !el.contains(target) && !button.contains(target)) {
        outsidePointerRef.current = true;
      }
    };
    el.addEventListener("focusin", onFocusIn);
    el.addEventListener("focusout", onFocusOut);
    document.addEventListener("pointerdown", onPointerDown, true);

    return () => {
      scope.release();
      window.removeEventListener(POPOVER_EVENT, onOtherPopover);
      window.removeEventListener("keydown", onEscape, true);
      el.removeEventListener("focusin", onFocusIn);
      el.removeEventListener("focusout", onFocusOut);
      document.removeEventListener("pointerdown", onPointerDown, true);

      const wasExplicit = explicitCloseRef.current;
      explicitCloseRef.current = false;
      const hadFocus = focusInsideRef.current || el.contains(document.activeElement);
      focusInsideRef.current = false;
      const dismissedByOutside = outsidePointerRef.current;
      outsidePointerRef.current = false;

      hidePopover(el);

      if (wasExplicit || !hadFocus || dismissedByOutside) return;
      const active = document.activeElement;
      const focusOnEnabledAnchor = active === button && !isAnchorDisabled(button);
      const focusLost = !active || active === document.body || active === document.documentElement ||
        !active.isConnected || el.contains(active) || focusOnEnabledAnchor;
      if (!focusLost) return;
      restoreFocus(el, button);
    };
  }, [open, anchor, popId]);

  const anchorEl = resolve();
  const dialogLabel = label || (anchorEl ? `Why ${visibleButtonLabel(anchorEl)} is unavailable` : "Why this action is unavailable");

  return (
    <div
      ref={pop}
      id={popId}
      {...POPOVER_ATTR}
      role="dialog"
      aria-label={dialogLabel}
      tabIndex={-1}
      className="cluster-action-why why-disabled"
    >
      {open ? (
        <>
          {children}
          <button
            type="button"
            className="icon-button why-disabled-close"
            aria-label="Close"
            onClick={closeToAnchor}
          >
            <Icon name="close" size={12} />
          </button>
        </>
      ) : null}
    </div>
  );
}

// ---- App-level host for privilege-disabled buttons ------------------------------------

type ActiveWhy = WhyDisabledRequest & { id: number };
let nextWhyId = 0;

function anchorStillDisabled(anchor: HTMLElement): boolean {
  if (!isElementVisible(anchor)) return false;
  if (anchor.getAttribute("aria-disabled") !== "true") return false;
  if ((anchor as HTMLButtonElement).disabled) return false;
  if (anchor.getAttribute("aria-busy") === "true") return false;
  return true;
}

/** Rendered once near the app root. Registers the module-level openWhyDisabled
 * function and renders a single WhyDisabled anchored to the clicked button. */
export function WhyDisabledHost({ resetKey, children }: PropsWithChildren<{
  /** Changes when the active place, view or connection changes, closing any open message. */
  resetKey?: string;
}>) {
  const [active, setActive] = useState<ActiveWhy | null>(null);
  const activeRef = useRef<ActiveWhy | null>(null);
  activeRef.current = active;

  const open = useCallback((request: WhyDisabledRequest) => {
    if (!request.reason || !anchorStillDisabled(request.anchor)) return;
    if (activeRef.current?.anchor === request.anchor) {
      setActive(null);
      return;
    }
    setActive({ ...request, id: ++nextWhyId });
  }, []);

  const close = useCallback(() => {
    setActive(null);
  }, []);

  useEffect(() => registerWhyHost({ open, close }), [open, close]);

  // Close when the active place, view or connection changes.
  const prevReset = useRef(resetKey);
  useEffect(() => {
    if (prevReset.current !== resetKey) {
      prevReset.current = resetKey;
      setActive(null);
    }
  }, [resetKey]);

  // Close if the anchored button becomes enabled, disabled natively, busy,
  // hidden (e.g. a retained view switched off-screen or a dialog closed), or
  // removed from the DOM; update reason and GRANT live while it stays disabled (V1, V2).
  const activeId = active?.id ?? 0;
  const activeAnchor = active?.anchor ?? null;
  useEffect(() => {
    if (!activeId || !activeAnchor) return;
    const anchor = activeAnchor;
    const tracksData = anchor.hasAttribute("data-why-reason");
    const syncOrDismiss = () => {
      if (!anchorStillDisabled(anchor)) {
        setActive((cur) => (cur?.id === activeId ? null : cur));
        return;
      }
      if (anchor.hasAttribute("data-why-reason")) {
        const nextReason = anchor.getAttribute("data-why-reason") || "";
        const nextGrant = anchor.getAttribute("data-why-grant");
        if (!nextReason) {
          setActive((cur) => (cur?.id === activeId ? null : cur));
          return;
        }
        setActive((cur) => {
          if (!cur || cur.id !== activeId) return cur;
          if (cur.reason === nextReason && (cur.grantSql ?? null) === (nextGrant ?? null)) return cur;
          return { ...cur, reason: nextReason, grantSql: nextGrant };
        });
      } else if (tracksData) {
        setActive((cur) => (cur?.id === activeId ? null : cur));
      }
    };
    const attrObserver = new MutationObserver(syncOrDismiss);
    attrObserver.observe(anchor, {
      attributes: true,
      attributeFilter: ["aria-disabled", "disabled", "aria-busy", "data-privilege-state", "data-why-reason", "data-why-grant"],
    });
    const ancestorObserver = observeAncestors(anchor, syncOrDismiss);
    const removalRoot = anchor.closest("dialog, .view-inspector, .server-view, main") || document.body;
    const removalTarget = removalRoot === document.body ? document.body : removalRoot.parentElement || document.body;
    const removalObserver = new MutationObserver(syncOrDismiss);
    removalObserver.observe(removalTarget, { childList: true, subtree: true });
    let resizeObserver: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(syncOrDismiss);
      resizeObserver.observe(anchor);
    }
    return () => {
      attrObserver.disconnect();
      ancestorObserver.disconnect();
      removalObserver.disconnect();
      resizeObserver?.disconnect();
    };
  }, [activeId, activeAnchor]);

  const modal = active?.anchor.isConnected ? (active.anchor.closest("dialog[open]") as HTMLDialogElement | null) : null;
  const grantSql = active && typeof active.grantSql === "string" && active.grantSql.length > 0 ? active.grantSql : null;
  const grantLabel = active ? active.label || visibleButtonLabel(active.anchor) : "";
  const message = active ? (
    <WhyDisabled
      key={active.id}
      anchor={active.anchor}
      open={true}
      mayOpen={true}
      attachClick={false}
      label={`Why ${grantLabel} is unavailable`}
      onOpen={() => {}}
      onClose={() => setActive((cur) => (cur?.id === active.id ? null : cur))}
    >
      <p>{active.reason}</p>
      {grantSql && (
        <>
          <Commands text={grantSql} label={`Grant for ${grantLabel}`} />
          <p className="hint">Run it as an administrator, then Refresh.</p>
        </>
      )}
    </WhyDisabled>
  ) : null;

  return (
    <>
      {children}
      {message && (modal ? createPortal(message, modal) : message)}
    </>
  );
}
