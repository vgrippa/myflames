// The message a disabled button opens on click, Enter or Space: why the
// action is off and what fixes it, anchored to the button. It uses the HTML
// Popover API (popover="auto"): the top layer keeps it out of every scroll
// container, a click outside closes it (light dismiss), and opening another
// closes this one. Escape and Close return focus to the button; Escape is
// consumed before an enclosing inspector or dialog can handle the same key.
// The anchor button is wired as the popover's invoker (popovertarget), so a
// click on it does not light-dismiss the message before its toggle handler
// runs. Position is CSS anchor positioning (anchor-name on the button,
// position-anchor here), so the message tracks its button and flips instead
// of leaving the viewport. Contents stay React nodes: server text can never
// become markup (S8, S10). While closed the box renders empty: nothing a
// query for the group's buttons or text can trip over.
//
// WhyDisabledHost renders one shared message near the app root for
// privilege-disabled buttons (privileges.ts: privilegeButtonProps,
// PrivilegeGate, PrivilegeConfirmLabel) via the module-level openWhyDisabled
// function. When the clicked button lives inside a modal <dialog>, the host
// portals the popover into that dialog so modal inertness cannot block Close
// or Copy inside it.
import { useCallback, useEffect, useId, useRef, useState, type PropsWithChildren, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { Commands } from "./Commands";
import { Icon } from "./components";
import "./catalog-actions.css";

/** @types/react 18.3.12 has no popover typings: the attribute goes through a
 * typed record, never an `any` prop. */
const POPOVER_ATTR: Record<string, string> = { popover: "auto" };

/** The message under a disabled button (CatalogActions.tsx wires it to the
 * action buttons of a catalog; WhyDisabledHost wires it to privilege-gated
 * disabled buttons across the workspace). */
export function WhyDisabled({ anchor, open, mayOpen = true, attachClick = true, onOpen, onClose, children }: {
  /** The button the message is anchored to (its element or a ref to it). */
  anchor: HTMLElement | RefObject<HTMLElement | null> | null;
  /** The owner's open state, kept in sync with the popover's toggle events. */
  open: boolean;
  /** When false the anchor does not toggle the message (a busy group). */
  mayOpen?: boolean;
  /** When true (CatalogActions), this component attaches the click toggle
   * handler to the anchor. When false (WhyDisabledHost), the anchor's own
   * click handler opens and toggles the host while this effect only prevents
   * the native popovertarget default toggle. */
  attachClick?: boolean;
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
    try { pop.current?.hidePopover(); } catch { /* Already closed or detached. */ }
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
      // its default toggle because the owner manages opening and closing.
      event.preventDefault();
      if (!attachClick) return;
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
  }, [anchor, anchorName, popId, mayOpen, attachClick]);

  // The owner state follows the popover's own openings and closings (the
  // invoker click, Escape, light dismiss, another popover opening).
  useEffect(() => {
    const el = pop.current;
    if (!el) return;
    const sync = (event: Event) => {
      const state = (event as ToggleEvent).newState;
      if (state === "open" && el.matches(":popover-open")) onOpenRef.current();
      else if (state === "closed" && !el.matches(":popover-open")) onCloseRef.current();
    };
    el.addEventListener("toggle", sync);
    return () => el.removeEventListener("toggle", sync);
  }, []);

  // Native Escape handling runs after keydown. Consume the key first so a
  // surrounding inspector, dialog or focus scope cannot close as well. Window
  // capture precedes those document listeners, including a narrow inspector's scope.
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
  // the message stale, or the host opening for a clicked button) drives the element.
  useEffect(() => {
    const el = pop.current;
    if (!el) return;
    if (open && !el.matches(":popover-open")) {
      try { el.showPopover(); } catch { /* Detached before paint. */ }
    } else if (!open && el.matches(":popover-open")) {
      try { el.hidePopover(); } catch { /* Already closed. */ }
    }
  }, [open, anchor]);

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

// ---- Advisory GRANT builder for missing privileges ------------------------------------

const NON_GRANTABLE = new Set(["GRANT OPTION", "USAGE", "PROXY"]);
const PRIVILEGE_ATOM = /^([A-Z][A-Z0-9_]*(?: [A-Z][A-Z0-9_]*)*)(?:\s+on\s+([^.\s]+)\.(?:(\*)|([^.\s]+)))?$/;

function quoteIdentifier(value: string): string | null {
  if (!value || /[\0\r\n]/.test(value)) return null;
  return "`" + value.replace(/`/g, "``") + "`";
}

function quoteAccount(account: string): string | null {
  const at = account.lastIndexOf("@");
  if (at <= 0 || at === account.length - 1) return null;
  const user = quoteIdentifier(account.slice(0, at));
  const host = quoteIdentifier(account.slice(at + 1));
  return user && host ? `${user}@${host}` : null;
}

/** Advisory GRANT statement for the first missing-privilege alternative that
 * maps cleanly to MySQL/MariaDB GRANT syntax, or null when the missing text is
 * not a plain privilege or the account is unknown. Never executed by myflames. */
export function privilegeGrantSql(missing: string[], account?: string, partialRevokes?: boolean): string | null {
  if (!missing?.length || !account) return null;
  const grantee = quoteAccount(account);
  if (!grantee) return null;
  const alternatives = missing.flatMap((entry) =>
    typeof entry === "string" ? entry.split(/\s+or\s+/).map((part) => part.trim()).filter(Boolean) : [],
  );
  for (const alternative of alternatives) {
    const atoms = alternative.split(/\s+and\s+/).map((part) => part.trim()).filter(Boolean);
    if (!atoms.length) continue;
    const grouped = new Map<string, string[]>();
    let ok = true;
    for (const atom of atoms) {
      const match = atom.match(PRIVILEGE_ATOM);
      if (!match) { ok = false; break; }
      const [, privilege, database, star, table] = match;
      if (NON_GRANTABLE.has(privilege)) { ok = false; break; }
      let scope = "*.*";
      if (database) {
        let db = database;
        if (star) {
          if (/[\\_%]/.test(db)) {
            if (typeof partialRevokes !== "boolean") { ok = false; break; }
            if (!partialRevokes) db = db.replace(/\\/g, "\\\\").replace(/_/g, "\\_").replace(/%/g, "\\%");
          }
          const quotedDb = quoteIdentifier(db);
          if (!quotedDb) { ok = false; break; }
          scope = `${quotedDb}.*`;
        } else if (table) {
          const quotedDb = quoteIdentifier(db);
          const quotedTable = quoteIdentifier(table);
          if (!quotedDb || !quotedTable) { ok = false; break; }
          scope = `${quotedDb}.${quotedTable}`;
        } else {
          ok = false;
          break;
        }
      }
      const list = grouped.get(scope) || [];
      if (!list.includes(privilege)) list.push(privilege);
      grouped.set(scope, list);
    }
    if (ok && grouped.size > 0) {
      return [...grouped.entries()]
        .map(([scope, privs]) => `GRANT ${privs.join(", ")} ON ${scope} TO ${grantee};`)
        .join("\n");
    }
  }
  return null;
}

// ---- App-level host for privilege-disabled buttons ------------------------------------

export type WhyDisabledRequest = {
  anchor: HTMLElement;
  reason: string;
  /** Missing privilege alternatives when the denial is a privilege gap. */
  missing?: string[];
  /** Explicit advisory GRANT text when already computed. */
  grantSql?: string;
  /** Optional account override when outside PrivilegesProvider. */
  account?: string;
  /** Optional label for the GRANT Commands block. */
  label?: string;
};

type ActiveWhy = WhyDisabledRequest & { id: number };
type HostApi = { open: (request: WhyDisabledRequest) => void; close: () => void };

let registeredHost: HostApi | null = null;
let nextWhyId = 0;

/** Open (or toggle closed, when called again for the same button) the single
 * app-level WhyDisabled message anchored to the clicked disabled button. */
export function openWhyDisabled(request: WhyDisabledRequest): void {
  registeredHost?.open(request);
}

/** Close the app-level WhyDisabled message if open. */
export function closeWhyDisabled(): void {
  registeredHost?.close();
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

function anchorStillDisabled(anchor: HTMLElement): boolean {
  if (!anchor.isConnected) return false;
  if (anchor.getClientRects().length === 0) return false;
  if (anchor.getAttribute("aria-disabled") !== "true") return false;
  if ((anchor as HTMLButtonElement).disabled) return false;
  if (anchor.getAttribute("aria-busy") === "true") return false;
  return true;
}

/** Rendered once near the app root. Registers the module-level openWhyDisabled
 * function and renders a single WhyDisabled anchored to the clicked button. */
export function WhyDisabledHost({ account, partialRevokes, resetKey, children }: PropsWithChildren<{
  account?: string;
  partialRevokes?: boolean;
  /** Changes when the active place, view or connection changes, closing any open message. */
  resetKey?: string;
}>) {
  const [active, setActive] = useState<ActiveWhy | null>(null);
  const activeRef = useRef<ActiveWhy | null>(null);
  activeRef.current = active;

  const open = useCallback((request: WhyDisabledRequest) => {
    if (!request.reason || !request.anchor.isConnected) return;
    if (activeRef.current?.anchor === request.anchor) {
      setActive(null);
      return;
    }
    setActive({ ...request, id: ++nextWhyId });
  }, []);

  const close = useCallback(() => {
    setActive(null);
  }, []);

  useEffect(() => {
    const api: HostApi = { open, close };
    registeredHost = api;
    return () => {
      if (registeredHost === api) registeredHost = null;
    };
  }, [open, close]);

  // Close when the active place, view or connection changes.
  useEffect(() => {
    setActive(null);
  }, [resetKey]);

  // Close if the anchored button becomes enabled, disabled natively, busy,
  // hidden (e.g. a retained view switched off-screen), or removed from the DOM.
  useEffect(() => {
    if (!active) return;
    const anchor = active.anchor;
    const activeId = active.id;
    const dismissIfStale = () => {
      if (!anchorStillDisabled(anchor)) {
        setActive((cur) => (cur?.id === activeId ? null : cur));
      }
    };
    const attrObserver = new MutationObserver(dismissIfStale);
    attrObserver.observe(anchor, {
      attributes: true,
      attributeFilter: ["aria-disabled", "disabled", "aria-busy", "data-privilege-state"],
    });
    // Watch ancestor visibility/hidden attributes (retained views hide with [hidden])
    // and removal of the button or its containing panel.
    const ancestorObserver = new MutationObserver(dismissIfStale);
    for (let parent = anchor.parentElement; parent; parent = parent.parentElement) {
      ancestorObserver.observe(parent, {
        attributes: true,
        attributeFilter: ["hidden", "inert", "aria-hidden", "style", "class"],
      });
    }
    const removalRoot = anchor.closest("dialog, .view-inspector, .server-view, main") || document.body;
    const removalObserver = new MutationObserver(dismissIfStale);
    removalObserver.observe(removalRoot, { childList: true, subtree: true });
    let resizeObserver: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(dismissIfStale);
      resizeObserver.observe(anchor);
    }
    return () => {
      attrObserver.disconnect();
      ancestorObserver.disconnect();
      removalObserver.disconnect();
      resizeObserver?.disconnect();
    };
  }, [active]);

  const modal = active?.anchor.isConnected ? (active.anchor.closest("dialog[open]") as HTMLDialogElement | null) : null;
  const grantSql = active
    ? active.grantSql || privilegeGrantSql(active.missing || [], active.account || account, partialRevokes)
    : null;
  const grantLabel = active ? active.label || visibleButtonLabel(active.anchor) : "";
  const message = active ? (
    <WhyDisabled
      key={active.id}
      anchor={active.anchor}
      open={true}
      mayOpen={true}
      attachClick={false}
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
