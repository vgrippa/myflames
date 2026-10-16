// Instance-scoped search toolbar for the tree, bar chart and flame graph.
// It lives in HTML above the chart's own SVG, so "Export SVG" (the first
// <svg> in the panel) carries the chart without the search box. Replaces
// the old prompt() search: no modal dialogs, so sandboxed frames need no
// allow-modals.
import { html } from "./dom";

export interface Toolbar {
  element: HTMLDivElement;
  input: HTMLInputElement;
  status: HTMLSpanElement;
  /** Optional "Ignore case" checkbox (flame graph). */
  ignoreCase: HTMLInputElement | null;
  focus(): void;
}

export function createToolbar(doc: Document, parent: HTMLElement, label: string,
                              withIgnoreCase = false): Toolbar {
  const element = html(doc, "div", { class: "mfc-toolbar", role: "search" }, parent);
  const input = html(doc, "input", {
    type: "search", "aria-label": label, placeholder: label + " · Ctrl+F", spellcheck: "false",
  }, element);
  let ignoreCase: HTMLInputElement | null = null;
  if (withIgnoreCase) {
    const wrap = html(doc, "label", { class: "mfc-toggle" }, element);
    ignoreCase = html(doc, "input", { type: "checkbox" }, wrap);
    wrap.appendChild(doc.createTextNode(" Ignore case"));
  }
  const status = html(doc, "span", { class: "mfc-search-status", "aria-live": "polite" }, element);
  return {
    element, input, status, ignoreCase,
    focus() {
      input.focus();
      input.select();
    },
  };
}

/** Compile a user regex; null for invalid input (reported, never thrown). */
export function compile(term: string, flags: string): RegExp | null {
  try {
    return new RegExp(term, flags);
  } catch {
    return null;
  }
}

export const isTyping = (target: EventTarget | null) => {
  const tag = target && (target as Element).tagName;
  return tag === "INPUT" || tag === "TEXTAREA";
};

/**
 * Keyboard shortcuts scoped to one chart: the listener sits on the chart's
 * container, so Ctrl+F / F3 are captured only while focus is inside it and
 * browser find keeps working everywhere else. *key* may handle further keys
 * and returns true when it consumed the event.
 */
export function bindKeys(container: HTMLElement, find: () => void,
                         key?: (event: KeyboardEvent) => boolean): () => void {
  // Focusable without a tab stop, so clicking the chart background gives
  // the shortcuts a focus target.
  if (!container.hasAttribute("tabindex")) container.setAttribute("tabindex", "-1");
  const listener = (event: KeyboardEvent) => {
    const findKey = (event.ctrlKey && !event.metaKey && !event.altKey && (event.key === "f" || event.key === "F"))
      || event.key === "F3";
    if (findKey) {
      event.preventDefault();
      find();
      return;
    }
    if (key && key(event)) event.preventDefault();
  };
  container.addEventListener("keydown", listener);
  return () => container.removeEventListener("keydown", listener);
}
