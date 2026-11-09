// One selection per mounted chart, shared by the chart marks and any panel.
// Every registered element gets aria-pressed and .is-selected; views add
// their own styling and details through listeners.

export interface Selection {
  register(id: string, element: Element): void;
  known(id: string): boolean;
  current(): string;
  select(id: string, notify?: boolean): boolean;
  /** Drop the selection locally (listeners receive ""); never notifies. */
  clear(): void;
  listen(listener: (id: string) => void): void;
}

export function createSelection(notify: (id: string) => void): Selection {
  const elements = new Map<string, Element[]>();
  const listeners: Array<(id: string) => void> = [];
  let current = "";
  const release = () => {
    for (const element of elements.get(current) || []) {
      element.classList.remove("is-selected");
      element.setAttribute("aria-pressed", "false");
    }
  };
  return {
    register(id, element) {
      element.setAttribute("aria-pressed", "false");
      const list = elements.get(id);
      if (list) list.push(element); else elements.set(id, [element]);
    },
    known: (id) => typeof id === "string" && elements.has(id),
    current: () => current,
    select(id, shouldNotify = false) {
      if (typeof id !== "string" || !elements.has(id)) return false;
      release();
      current = id;
      for (const element of elements.get(id) || []) {
        element.classList.add("is-selected");
        element.setAttribute("aria-pressed", "true");
      }
      listeners.forEach((listener) => listener(id));
      if (shouldNotify) notify(id);
      return true;
    },
    clear() {
      if (!current) return;
      release();
      current = "";
      listeners.forEach((listener) => listener(""));
    },
    listen(listener) {
      listeners.push(listener);
    },
  };
}
