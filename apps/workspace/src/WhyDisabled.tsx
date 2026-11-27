// The message a disabled button opens on click, Enter or Space: why the
// action is off and what fixes it, anchored to the button. It uses the HTML
// Popover API (popover="auto"): the top layer keeps it out of every scroll
// container, a click outside closes it (light dismiss), and opening another
// closes this one. Escape and Close return focus to the button; Escape is
// consumed before an enclosing inspector can handle the same key.
// The anchor button is wired as the popover's invoker (popovertarget), so a
// click on it does not light-dismiss the message before its toggle handler
// runs. That handler calls showPopover / hidePopover through the ref; the
// open state reaches the owner through toggle events (onOpen / onClose).
// Position is CSS anchor positioning (anchor-name on the button,
// position-anchor here), so the message tracks its button and flips instead
// of leaving the viewport. Contents stay React nodes: server text can never
// become markup (S8, S10). While closed the box renders empty: nothing a
// query for the group's buttons or text can trip over.
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";
import { Icon } from "./components";

/** @types/react 18.3.12 has no popover typings: the attribute goes through a
 * typed record, never an `any` prop. */
const POPOVER_ATTR: Record<string, string> = { popover: "auto" };

/** The message under a disabled button (CatalogActions.tsx wires it to the
 * action buttons of a catalog; any other disabled button can use it the same
 * way). */
export function WhyDisabled({ anchor, open, mayOpen = true, onOpen, onClose, children }: {
  /** The button the message is anchored to (its element or a ref to it). */
  anchor: HTMLElement | RefObject<HTMLElement | null> | null;
  /** The owner's open state, kept in sync with the popover's toggle events. */
  open: boolean;
  /** When false the anchor does not toggle the message (a busy group). */
  mayOpen?: boolean;
  onOpen: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  const pop = useRef<HTMLDivElement | null>(null);
  const plain = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const anchorName = `--why-${plain}`;
  const popId = `why-${plain}`;
  // The latest callbacks: the toggle listener stays attached across renders.
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // Resolved at effect/event time: a ref's .current is empty during the first
  // render and set by the time effects and handlers run.
  const resolve = () => (anchor && "current" in anchor ? anchor.current : anchor);
  const closeToAnchor = () => {
    pop.current?.hidePopover();
    resolve()?.focus({ preventScroll: true });
    onCloseRef.current();
  };

  // Anchor positioning and the invoker relationship: the button carries the
  // anchor name and points at the message while clicks may open it.
  useEffect(() => {
    const button = resolve();
    const el = pop.current;
    if (!button || !el) return;
    button.style.setProperty("anchor-name", anchorName);
    el.style.setProperty("position-anchor", anchorName);
    const toggle = (event: MouseEvent) => {
      // Keep the invoker relationship for pointer light dismiss, but cancel
      // its default toggle because the ref owns opening and closing.
      event.preventDefault();
      if (el.matches(":popover-open")) el.hidePopover();
      else el.showPopover();
    };
    if (mayOpen) {
      button.setAttribute("popovertarget", popId);
      button.addEventListener("click", toggle);
    }
    return () => {
      button.style.removeProperty("anchor-name");
      button.removeAttribute("popovertarget");
      button.removeEventListener("click", toggle);
    };
  }, [anchor, anchorName, popId, mayOpen]);

  // The owner state follows the popover's own openings and closings (the
  // invoker click, Escape, light dismiss, another popover opening).
  useEffect(() => {
    const el = pop.current;
    if (!el) return;
    const sync = (event: Event) => {
      const state = (event as ToggleEvent).newState;
      if (state === "open") onOpenRef.current();
      else if (state === "closed") onCloseRef.current();
    };
    el.addEventListener("toggle", sync);
    return () => el.removeEventListener("toggle", sync);
  }, []);

  // Native Escape handling runs after keydown. Consume the key first so a
  // surrounding inspector or focus scope cannot close as well. Window capture
  // precedes those document listeners, including a narrow inspector's scope.
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !pop.current?.matches(":popover-open")) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      closeToAnchor();
    };
    window.addEventListener("keydown", escape, true);
    return () => window.removeEventListener("keydown", escape, true);
  }, [anchor]);

  // A state change that did not come from the popover (a refresh that made
  // the message stale) drives the element instead.
  useEffect(() => {
    const el = pop.current;
    if (!el) return;
    if (open && !el.matches(":popover-open")) el.showPopover();
    else if (!open && el.matches(":popover-open")) el.hidePopover();
  }, [open]);

  return (
    <div ref={pop} id={popId} {...POPOVER_ATTR} className="cluster-action-why why-disabled">
      {open ? (
        <>
          {children}
          <button type="button" className="icon-button why-disabled-close" aria-label="Close"
            onClick={closeToAnchor}>
            <Icon name="close" size={12} />
          </button>
        </>
      ) : null}
    </div>
  );
}
