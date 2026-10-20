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

export function CommandPalette({ commands, onClose }: { commands: WorkspaceCommand[]; onClose(): void }) {
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
  const index = Math.min(selected, Math.max(0, matches.length - 1));
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
  }, [matches.map((command) => command.id).join("\u0000")]);
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
        aria-activedescendant={matches.length ? `${listId}-${index}` : undefined}
        onChange={event => { setQuery(event.target.value); setSelected(0); }}
        onKeyDown={event => {
          if (event.key === "ArrowDown") { event.preventDefault(); setSelected((index + 1) % Math.max(matches.length, 1)); }
          else if (event.key === "ArrowUp") { event.preventDefault(); setSelected((index - 1 + matches.length) % Math.max(matches.length, 1)); }
          else if (event.key === "Enter") { event.preventDefault(); choose(matches[index]); }
        }} />
      <button type="button" className="text-button" onClick={onClose} aria-label="Close commands">Esc</button>
    </div>
    <div ref={results} className="command-results" id={listId} role="listbox" aria-label="Matching commands">
      {matches.map((command, position) => <button type="button" key={command.id} id={`${listId}-${position}`}
        className="command-option" role="option" aria-selected={position === index}
        aria-disabled={command.disabled || undefined} tabIndex={-1}
        onMouseEnter={() => setSelected(position)} onClick={() => choose(command)}>
        <Icon name={command.icon} /><span>{command.label}</span><small>{command.group}</small>
      </button>)}
      {!matches.length && <p className="command-empty" role="status">No commands match “{query}”.</p>}
    </div>
    <footer className="command-footer"><span>↑ ↓ to move</span><span>Return to open</span></footer>
  </dialog>;
}
