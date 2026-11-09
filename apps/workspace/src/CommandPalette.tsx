import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Icon, type IconName } from "./components";
import { useFocusScope } from "./kit";
import "./command-palette.css";

/** Only existing navigation and local document actions enter this palette.
 * Execution and consequential changes keep their original review surfaces. */
export type WorkspaceCommand = {
  id: string; label: string; group: string; icon: IconName;
  keywords?: string; disabled?: boolean; run(): void;
};
/** The object search's state while a query is typed (2+ characters). */
export type PaletteObjectStatus = { searching: boolean; error: string; truncated: boolean };

export function CommandPalette({ commands, recent = [], objects = [], objectStatus, onQueryChange, onClose }: {
  commands: WorkspaceCommand[];
  /** The last views visited, first while the search is empty. */
  recent?: WorkspaceCommand[];
  /** Schema objects matching the typed text (cached catalog, then the server). */
  objects?: WorkspaceCommand[];
  objectStatus?: PaletteObjectStatus;
  onQueryChange?: (query: string) => void;
  onClose(): void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const listId = useId();
  const words = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  // Commands whose label holds every word come first ("innodb" opens the
  // InnoDB view before the Cluster place, which only has it as a keyword);
  // otherwise the order stays as declared.
  const labelMatch = (command: WorkspaceCommand) => words.every(word => command.label.toLocaleLowerCase().includes(word));
  const matches = commands.filter(command => words.every(word =>
    `${command.label} ${command.group} ${command.keywords || ""}`.toLocaleLowerCase().includes(word)))
    .map((command, order) => ({ command, order, rank: labelMatch(command) ? 0 : 1 }))
    .sort((a, b) => a.rank - b.rank || a.order - b.order)
    .map(({ command }) => command);
  // Recent views lead the empty palette; a typed search is a search, and
  // objects join it from two characters up.
  const recents = words.length ? [] : recent;
  const objectItems = words.length
    ? objects.filter(command => words.every(word =>
        `${command.label} ${command.group}`.toLocaleLowerCase().includes(word)))
    : [];
  const items = [...recents, ...matches, ...objectItems];
  const index = Math.min(selected, Math.max(0, items.length - 1));
  const searching = !!objectStatus?.searching && words.length > 0;
  const objectNote = searching ? "Searching objects…"
    : objectStatus?.error && words.length ? `Object search: ${objectStatus.error}`
    : objectStatus?.truncated && objectItems.length ? "More objects match; keep typing to narrow the search."
    : "";
  useFocusScope(dialog, { open: true, onClose, initialFocus: () => input.current });
  useEffect(() => { document.getElementById(`${listId}-${index}`)?.scrollIntoView({ block: "nearest" }); }, [index, listId, query]);
  // A list taller than the palette ends on a whole command: its height is
  // trimmed to the last command that fits, so none is cut in half above the
  // footer. Recomputed when the matches or the window change.
  const results = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = results.current;
    if (!el) return;
    const fit = () => {
      el.style.maxHeight = "";
      // Not laid out yet (the dialog opens after this effect): the observer
      // below calls again once the list has a size.
      if (!el.clientHeight || el.scrollHeight <= el.clientHeight + 1) return;
      let bottom = 0;
      for (const option of el.querySelectorAll<HTMLElement>(".command-option")) {
        const end = option.offsetTop + option.offsetHeight;
        if (end > el.clientHeight) break;
        bottom = end;
      }
      if (bottom > 0) el.style.maxHeight = `${bottom}px`;
    };
    fit();
    const observer = new ResizeObserver(() => { if (!el.style.maxHeight) fit(); });
    observer.observe(el);
    window.addEventListener("resize", fit);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", fit);
    };
    // Recomputed for every new set of matches (a filter can keep the count
    // but change which commands, and so their wrapped heights).
  }, [items.map((command) => command.id).join("\u0000") + objectNote]);
  function choose(command: WorkspaceCommand | undefined) {
    if (!command || command.disabled) return;
    onClose();
    // Let the palette return focus before the destination takes ownership.
    window.setTimeout(() => command.run());
  }
  return <dialog ref={dialog} className="command-palette" aria-label="Command palette" onCancel={event => { event.preventDefault(); onClose(); }}>
    <div className="command-search">
      <Icon name="search" size={18} />
      <input ref={input} value={query} type="search" autoComplete="off" spellCheck={false}
        aria-label="Search commands" placeholder="Search or jump to…" role="combobox"
        aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={items.length ? `${listId}-${index}` : undefined}
        onChange={event => { setQuery(event.target.value); setSelected(0); onQueryChange?.(event.target.value); }}
        onKeyDown={event => {
          if (event.key === "ArrowDown") { event.preventDefault(); setSelected((index + 1) % Math.max(items.length, 1)); }
          else if (event.key === "ArrowUp") { event.preventDefault(); setSelected((index - 1 + items.length) % Math.max(items.length, 1)); }
          else if (event.key === "Enter") { event.preventDefault(); choose(items[index]); }
        }} />
      <button type="button" className="text-button command-esc" onClick={onClose} aria-label="Close commands"><kbd>Esc</kbd></button>
    </div>
    <div ref={results} className="command-results" id={listId} role="listbox" aria-label="Matching commands">
      {items.map((command, position) => <button type="button" key={command.id} id={`${listId}-${position}`}
        className="command-option" role="option" aria-selected={position === index}
        aria-disabled={command.disabled || undefined} tabIndex={-1}
        onMouseEnter={() => setSelected(position)} onClick={() => choose(command)}>
        <Icon name={command.icon} /><span><bdi>{command.label}</bdi></span><small>{command.group}</small>
      </button>)}
      {objectNote && <p className="command-note" role="status">{objectNote}</p>}
      {!items.length && !searching && <p className="command-empty" role="status">No commands match “{query}”.</p>}
    </div>
    <footer className="command-footer"><span><kbd>↑</kbd> <kbd>↓</kbd> to move</span><span><kbd>Return</kbd> to open</span><span><kbd>Esc</kbd> to close</span></footer>
  </dialog>;
}
