import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { Icon } from "./components";
import { useFocusScope } from "./kit";

/** Longest query tab name (the session keeps up to 200 characters). */
export const MAX_TAB_NAME = 64;

/** *text* cut to at most *max* UTF-16 units without splitting a character
 * written as a surrogate pair (an emoji), which would leave invalid text. */
export function clipName(text: string, max = MAX_TAB_NAME) {
  if (text.length <= max) return text;
  const lead = text.charCodeAt(max - 1);
  return text.slice(0, lead >= 0xd800 && lead <= 0xdbff ? max - 1 : max);
}

/** "Orders copy", then "Orders copy 2", … within MAX_TAB_NAME. */
export function copyName(name: string, taken: string[]) {
  const used = new Set(taken);
  for (let n = 1; ; n++) {
    const suffix = n === 1 ? " copy" : ` copy ${n}`;
    const candidate = `${clipName(name, MAX_TAB_NAME - suffix.length).trimEnd()}${suffix}`;
    if (!used.has(candidate)) return candidate;
  }
}

/**
 * The menu of one query tab (right-click, Shift+F10 or the context-menu key
 * on the tab): Rename…, Duplicate, Close, Close others. Arrow keys move,
 * Escape or a click elsewhere closes it and returns focus to the tab.
 */
export function QueryTabMenu({ x, y, opener, name, single, onRename, onDuplicate, onClose, onCloseOthers, onDismiss }: {
  x: number;
  y: number;
  opener: HTMLElement;
  name: string;
  /** The only tab: Close others has nothing to close. */
  single: boolean;
  onRename: () => void;
  onDuplicate: () => void;
  onClose: () => void;
  onCloseOthers: () => void;
  onDismiss: () => void;
}) {
  const menu = useRef<HTMLDivElement>(null);
  // Stays visible while measured (the focus scope only focuses visible items);
  // the layout effect moves it before the first paint.
  const [position, setPosition] = useState<CSSProperties>({ left: x, top: y });
  useFocusScope(menu, { open: true, onClose: onDismiss, menu: true, returnFocus: () => opener });
  // Inside the window, below the pointer or the tab; above it when there is no room.
  useLayoutEffect(() => {
    const box = menu.current?.getBoundingClientRect();
    if (!box) return;
    const left = Math.max(8, Math.min(x, window.innerWidth - box.width - 8));
    const top = y + box.height + 8 > window.innerHeight ? Math.max(8, y - box.height - 4) : y;
    setPosition({ left, top });
  }, [x, y]);
  useEffect(() => {
    const outside = (e: MouseEvent) => {
      if (!menu.current?.contains(e.target as Node)) onDismiss();
    };
    document.addEventListener("mousedown", outside);
    window.addEventListener("resize", onDismiss);
    window.addEventListener("blur", onDismiss);
    return () => {
      document.removeEventListener("mousedown", outside);
      window.removeEventListener("resize", onDismiss);
      window.removeEventListener("blur", onDismiss);
    };
  }, [onDismiss]);
  return (
    <div ref={menu} className="context-menu query-tab-menu" role="menu" aria-label={`Query tab ${name}`} style={position} tabIndex={-1}>
      <button type="button" role="menuitem" onClick={onRename}>
        <span className="context-menu-icon"><Icon name="edit" /></span>
        <span>Rename…</span>
        <span className="context-menu-shortcut">F2</span>
      </button>
      <button type="button" role="menuitem" onClick={onDuplicate}>
        <span className="context-menu-icon"><Icon name="copy" /></span>
        <span>Duplicate</span>
      </button>
      <div className="context-menu-divider" role="separator" />
      <button type="button" role="menuitem" onClick={onClose}>
        <span className="context-menu-icon"><Icon name="close" /></span>
        <span>Close</span>
      </button>
      <button type="button" role="menuitem" disabled={single} onClick={onCloseOthers}
        title={single ? "This is the only tab" : undefined}>
        <span className="context-menu-icon" />
        <span>Close others</span>
      </button>
    </div>
  );
}
