// Saved queries (/api/editor/saved/*, saved-query-1.0): SQL the user keeps
// on this computer, in folders, redacted like the history before it is
// stored. The Saved tab of the editor's side panel lists them; opening one
// puts its SQL in a new tab and never runs it. Save query… (the tab menu and
// the command palette) opens SaveQuerySheet.
import { useEffect, useId, useMemo, useRef, useState, type FormEvent, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { deleteSavedQuery, listSavedQueries, moveSavedQuery, renameSavedQuery, saveQuery, type SavedQuery } from "./api";
import { Icon } from "./components";
import { ActionButton, ActionStatus, ConfirmSheet, ContextMenu, FormGrid, FormRow, SearchField, Select, useAction, useFocusScope, useSince } from "./kit";
import "./editor-panels.css";

/** Folder choices: no folder, a new one typed in, or `f:` and an existing name. */
const NO_FOLDER = "none";
const NEW_FOLDER = "new";
const folderChoice = (folder: string) => (folder ? `f:${folder}` : NO_FOLDER);
const MAX_NAME = 200;
const MAX_FOLDER = 200;
const MAX_NOTE = 2000;

/** The folders in use, sorted by name. */
export function foldersOf(items: SavedQuery[]) {
  return [...new Set(items.map((i) => i.folder).filter(Boolean))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}
const byName = (a: SavedQuery, b: SavedQuery) => a.name.localeCompare(b.name, undefined, { numeric: true });
const quoted = (name: string) => `“${name}”`;

/** A small form in a sheet: the first field takes focus, Enter in a field
 * confirms, Escape or Cancel closes (not while the request is out). */
function FormSheet({
  eyebrow,
  title,
  confirmLabel,
  busyLabel,
  busy,
  error,
  initialFocus,
  onCancel,
  onConfirm,
  children,
}: {
  eyebrow: string;
  title: ReactNode;
  confirmLabel: string;
  busyLabel: string;
  busy: boolean;
  /** Why the last attempt failed: once, beside the action. */
  error: string;
  initialFocus: () => HTMLElement | null | undefined;
  onCancel: () => void;
  onConfirm: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const formId = useId();
  const dismiss = () => {
    if (!busy) onCancel();
  };
  useFocusScope(dialog, { open: true, onClose: dismiss, initialFocus });
  return createPortal(
    <dialog ref={dialog} className="confirm-sheet saved-query-sheet" role="dialog" tabIndex={-1} aria-labelledby={titleId}
      aria-busy={busy || undefined} onCancel={(e) => { e.preventDefault(); dismiss(); }}>
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      <form id={formId} className="sheet-body" noValidate onSubmit={(e: FormEvent) => { e.preventDefault(); if (!busy) onConfirm(); }}>
        {children}
        {error && <p className="form-error saved-query-error" role="alert">{error}</p>}
      </form>
      <div className="dialog-actions sheet-actions">
        <button type="button" className="button" disabled={busy} onClick={dismiss}>Cancel</button>
        <ActionButton type="submit" form={formId} primary busy={busy} busyLabel={busyLabel} data-action="saved-confirm">
          {confirmLabel}
        </ActionButton>
      </div>
    </dialog>,
    document.body,
  );
}

/** Folder: an existing folder, no folder, or a new one typed in. */
function FolderField({ id, folders, choice, custom, onChange, current }: {
  id: string;
  folders: string[] | null;
  choice: string;
  custom: string;
  onChange: (choice: string, custom: string) => void;
  /** The folder the query is in now (kept in the list while folders load). */
  current?: string;
}) {
  const names = [...new Set([...(folders || []), ...(current ? [current] : [])])].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const options: [string, string][] = [[NO_FOLDER, "No folder"], ...names.map((f) => [folderChoice(f), f] as [string, string]), [NEW_FOLDER, "New folder…"]];
  return (
    <>
      <FormRow label="Folder" htmlFor={id} hint={folders === null ? "Reading your folders…" : undefined}>
        <Select id={id} label="Folder" value={choice} options={options} onChange={(v) => onChange(v, custom)} />
      </FormRow>
      {choice === NEW_FOLDER && (
        <FormRow label="New folder name" htmlFor={`${id}-new`}>
          <input id={`${id}-new`} value={custom} maxLength={MAX_FOLDER} spellCheck={false} autoComplete="off" autoFocus
            onChange={(e) => onChange(choice, e.target.value.replace(/[\r\n]/g, " "))} />
        </FormRow>
      )}
    </>
  );
}
const folderValue = (choice: string, custom: string) =>
  choice === NEW_FOLDER ? custom.trim() : choice.startsWith("f:") ? choice.slice(2) : "";

/** Reads the folders once for a sheet's Folder choice (a failed read leaves
 * the list empty: a new folder can still be typed). */
function useFolders() {
  const [folders, setFolders] = useState<string[] | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    listSavedQueries(controller.signal).then(
      (r) => setFolders(foldersOf(r.items)),
      () => { if (!controller.signal.aborted) setFolders([]); },
    );
    return () => controller.abort();
  }, []);
  return folders;
}

/** Save query…: the tab's SQL under a name (the tab's, to start with), in a folder. */
export function SaveQuerySheet({ name, sql, database, onClose, onSaved }: {
  name: string;
  sql: string;
  database: string | null;
  onClose: () => void;
  onSaved: (saved: SavedQuery, redacted: boolean) => void;
}) {
  const [title, setTitle] = useState(name.slice(0, MAX_NAME));
  const [note, setNote] = useState("");
  const [folder, setFolder] = useState({ choice: NO_FOLDER, custom: "" });
  const folders = useFolders();
  const saving = useAction();
  const nameField = useRef<HTMLInputElement>(null);
  const id = useId();
  async function confirm() {
    if (!title.trim()) return saving.show("failed", "Type a name for the query.");
    if (folder.choice === NEW_FOLDER && !folder.custom.trim()) return saving.show("failed", "Type the new folder's name, or choose a folder.");
    const result = await saving.run(
      () => saveQuery({ name: title, folder: folderValue(folder.choice, folder.custom), sql, note, database }),
      { pending: "Saving the query…", done: "" },
    );
    if (result.ok) onSaved(result.value.saved, result.value.redacted);
  }
  return (
    <FormSheet eyebrow="Save query" title="Save this query on this computer" confirmLabel="Save query" busyLabel="Saving…"
      busy={saving.pending} error={saving.phase === "failed" ? saving.message : ""}
      initialFocus={() => nameField.current} onCancel={onClose} onConfirm={() => void confirm()}>
      <p className="sheet-lead">
        The SQL is kept with your saved queries, with passwords and other credentials replaced by [redacted]. Opening it
        later puts it in a new tab; nothing runs.
      </p>
      <FormGrid>
        <FormRow label="Name" htmlFor={`${id}-name`}>
          <input ref={nameField} id={`${id}-name`} value={title} maxLength={MAX_NAME} autoComplete="off"
            onFocus={(e) => e.currentTarget.select()}
            onChange={(e) => { setTitle(e.target.value.replace(/[\r\n]/g, " ")); saving.reset(); }} />
        </FormRow>
        <FolderField id={`${id}-folder`} folders={folders} choice={folder.choice} custom={folder.custom}
          onChange={(choice, custom) => { setFolder({ choice, custom }); saving.reset(); }} />
        <FormRow label="Note" htmlFor={`${id}-note`} hint="Optional.">
          <textarea id={`${id}-note`} rows={2} value={note} maxLength={MAX_NOTE} onChange={(e) => setNote(e.target.value)} />
        </FormRow>
      </FormGrid>
    </FormSheet>
  );
}

type Sheet = { kind: "rename" | "move" | "delete"; item: SavedQuery } | null;
type Menu = { item: SavedQuery; x: number; y: number; opener: HTMLElement } | null;

/** The Saved tab: saved queries grouped by folder (collapsible), with a
 * search; a row opens in a new tab; its menu renames, moves or deletes. */
export function SavedQueriesList({ refresh, onOpen, onInsert, onReading }: {
  /** Changes after Save query…, so an open list reads again. */
  refresh: number;
  onOpen: (query: SavedQuery) => void;
  onInsert: (sql: string) => void;
  /** The first read is out (the panel is aria-busy). */
  onReading?: (reading: boolean) => void;
}) {
  const [items, setItems] = useState<SavedQuery[] | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [filter, setFilter] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [menu, setMenu] = useState<Menu>(null);
  const [sheet, setSheet] = useState<Sheet>(null);
  const [sheetName, setSheetName] = useState("");
  const [sheetFolder, setSheetFolder] = useState({ choice: NO_FOLDER, custom: "" });
  const change = useAction();
  const outcome = useAction();
  const reading = items === null && !error;
  const readingSince = useSince(reading);
  const nameField = useRef<HTMLInputElement>(null);
  const folderId = useId();
  const nameId = useId();
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    onReading?.(reading);
    return () => onReading?.(false);
  }, [reading, onReading]);
  useEffect(() => {
    const controller = new AbortController();
    listSavedQueries(controller.signal).then(
      (r) => {
        setItems(r.items);
        setError("");
      },
      (e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      },
    );
    return () => controller.abort();
  }, [refresh, attempt]);
  const needle = filter.trim().toLowerCase();
  const groups = useMemo(() => {
    const shown = (items || []).filter((i) =>
      !needle || [i.name, i.folder, i.sql, i.note, i.database || ""].some((t) => t.toLowerCase().includes(needle)));
    const byFolder = new Map<string, SavedQuery[]>();
    for (const i of shown) byFolder.set(i.folder, [...(byFolder.get(i.folder) || []), i]);
    // Queries without a folder first, then the folders by name.
    return [...byFolder.entries()]
      .sort(([a], [b]) => (a === "" ? -1 : b === "" ? 1 : a.localeCompare(b, undefined, { numeric: true })))
      .map(([folder, queries]) => ({ folder, queries: queries.sort(byName) }));
  }, [items, needle]);
  const folders = useMemo(() => foldersOf(items || []), [items]);
  const onlyUnfiled = groups.length === 1 && groups[0].folder === "";
  function toggle(folder: string) {
    setCollapsed((all) => {
      const next = new Set(all);
      if (next.has(folder)) next.delete(folder);
      else next.add(folder);
      return next;
    });
  }
  function openMenu(item: SavedQuery, x: number, y: number, opener: HTMLElement) {
    setMenu({ item, x, y, opener });
  }
  function onItemKey(e: KeyboardEvent<HTMLButtonElement>, item: SavedQuery) {
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      const box = e.currentTarget.getBoundingClientRect();
      openMenu(item, box.left + 24, box.bottom, e.currentTarget);
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      // Up and Down move between the rows and folder headings on screen.
      const all = [...(list.current?.querySelectorAll<HTMLElement>(".saved-folder, .saved-item") || [])];
      const at = all.indexOf(e.currentTarget);
      const next = all[at + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        e.preventDefault();
        next.focus();
      }
    }
  }
  function startSheet(kind: "rename" | "move" | "delete", item: SavedQuery) {
    change.reset();
    setSheetName(item.name);
    setSheetFolder({ choice: folderChoice(item.folder), custom: "" });
    setSheet({ kind, item });
  }
  function replace(next: SavedQuery) {
    setItems((all) => (all || []).map((i) => (i.id === next.id ? next : i)));
  }
  function gone(id: string) {
    setItems((all) => (all || []).filter((i) => i.id !== id));
  }
  /** Focus the row of *id* again after a sheet closes (or the list when it is gone). */
  function refocus(id: string) {
    window.requestAnimationFrame(() =>
      (list.current?.querySelector<HTMLElement>(`[data-saved-id="${CSS.escape(id)}"]`) ||
        list.current?.closest(".history-panel")?.querySelector<HTMLElement>("input[type=search]"))?.focus());
  }
  async function confirmSheet() {
    if (!sheet) return;
    const { kind, item } = sheet;
    if (kind === "rename") {
      if (!sheetName.trim()) return change.show("failed", "Type a name for the query.");
      const r = await change.run(() => renameSavedQuery(item.id, sheetName), { pending: "Renaming…", done: "" });
      if (!r.ok) return;
      replace(r.value.saved);
      outcome.show("done", `Renamed ${quoted(item.name)} to ${quoted(r.value.saved.name)}.`);
    } else if (kind === "move") {
      if (sheetFolder.choice === NEW_FOLDER && !sheetFolder.custom.trim()) return change.show("failed", "Type the new folder's name, or choose a folder.");
      const r = await change.run(() => moveSavedQuery(item.id, folderValue(sheetFolder.choice, sheetFolder.custom)), { pending: "Moving…", done: "" });
      if (!r.ok) return;
      replace(r.value.saved);
      setCollapsed((all) => {
        const next = new Set(all);
        next.delete(r.value.saved.folder);
        return next;
      });
      outcome.show("done", r.value.saved.folder ? `Moved ${quoted(item.name)} to ${r.value.saved.folder}.` : `Moved ${quoted(item.name)} out of its folder.`);
    } else {
      const r = await change.run(() => deleteSavedQuery(item.id), { pending: "Deleting…", done: "" });
      if (!r.ok) return;
      gone(item.id);
      outcome.show("done", `Deleted ${quoted(item.name)}.`);
    }
    setSheet(null);
    refocus(item.id);
  }
  return (
    <div className="saved-queries" ref={list}>
      <div className="browser-filter">
        <SearchField label="Search saved queries" value={filter} onChange={setFilter} />
      </div>
      <ActionStatus className="browser-status" state={{
        phase: reading ? "pending" : error ? "failed" : "idle",
        message: reading ? "Reading the saved queries…" : error, startedAt: readingSince,
      }} onRetry={error && items === null ? () => { setError(""); setAttempt((n) => n + 1); } : undefined} />
      <ActionStatus className="browser-status saved-outcome" state={outcome} onDismiss={outcome.reset} />
      {items && !groups.length && (
        <p className="browser-status">{items.length ? "Nothing matches." : "Save a query from its tab's menu (Save query…) to keep it here."}</p>
      )}
      <ul className="saved-list" aria-label="Saved queries">
        {groups.map(({ folder, queries }) => {
          const open = !!needle || !collapsed.has(folder);
          const label = folder || "No folder";
          return (
            <li key={folder || "\u0000"} className="saved-group">
              {!onlyUnfiled && (
                <button type="button" className="saved-folder" aria-expanded={open} data-folder={folder}
                  onClick={() => toggle(folder)} onKeyDown={(e) => {
                    if (e.key === "ArrowDown" || e.key === "ArrowUp") onItemKey(e, queries[0]);
                    else if (e.key === "ArrowLeft" && open) { e.preventDefault(); toggle(folder); }
                    else if (e.key === "ArrowRight" && !open) { e.preventDefault(); toggle(folder); }
                  }}>
                  <span className={`saved-folder-mark${open ? " is-open" : ""}`} aria-hidden="true"><Icon name="disclosure" size={12} /></span>
                  <span className="saved-folder-name">{label}</span>
                  <span className="saved-folder-count">{queries.length}</span>
                </button>
              )}
              {open && (
                <ul className="saved-items" aria-label={label}>
                  {queries.map((item) => (
                    <li key={item.id} className="saved-row">
                      <button type="button" className="saved-item" data-saved-id={item.id} aria-haspopup="menu"
                        title={`${item.sql}${item.note ? `\n\n${item.note}` : ""}\n\nClick to open in a new tab (nothing runs); right-click for more`}
                        onClick={() => onOpen(item)}
                        onContextMenu={(e: MouseEvent<HTMLButtonElement>) => { e.preventDefault(); openMenu(item, e.clientX, e.clientY, e.currentTarget); }}
                        onKeyDown={(e) => onItemKey(e, item)}>
                        <span className="saved-name">{item.name}</span>
                        <span className="saved-sql">{item.sql.trim().split("\n").slice(0, 2).join(" ")}</span>
                      </button>
                      <button type="button" className="icon-button row-more" aria-label={`More actions for ${item.name}`} title="More actions"
                        aria-haspopup="menu" aria-expanded={menu?.item.id === item.id}
                        onClick={(e) => { const box = e.currentTarget.getBoundingClientRect(); openMenu(item, box.left, box.bottom, e.currentTarget); }}>
                        <Icon name="more" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
      {menu && (
        <ContextMenu x={menu.x} y={menu.y} label={`Saved query ${menu.item.name}`} opener={menu.opener} onClose={() => setMenu(null)}
          actions={[
            { label: "Open in a new tab", icon: "query", onClick: () => onOpen(menu.item) },
            { label: "Insert at the cursor", icon: "plus", onClick: () => onInsert(menu.item.sql) },
            { divider: true },
            { label: "Rename…", icon: "edit", onClick: () => startSheet("rename", menu.item) },
            { label: "Move to folder…", onClick: () => startSheet("move", menu.item) },
            { divider: true },
            { label: "Delete…", icon: "trash", danger: true, onClick: () => startSheet("delete", menu.item) },
          ]} />
      )}
      {sheet?.kind === "rename" && (
        <FormSheet eyebrow="Rename" title={`Rename ${quoted(sheet.item.name)}`} confirmLabel="Rename" busyLabel="Renaming…"
          busy={change.pending} error={change.phase === "failed" ? change.message : ""} initialFocus={() => nameField.current}
          onCancel={() => { setSheet(null); refocus(sheet.item.id); }} onConfirm={() => void confirmSheet()}>
          <FormGrid>
            <FormRow label="Name" htmlFor={nameId}>
              <input ref={nameField} id={nameId} value={sheetName} maxLength={MAX_NAME} autoComplete="off"
                onFocus={(e) => e.currentTarget.select()}
                onChange={(e) => { setSheetName(e.target.value.replace(/[\r\n]/g, " ")); change.reset(); }} />
            </FormRow>
          </FormGrid>
        </FormSheet>
      )}
      {sheet?.kind === "move" && (
        <FormSheet eyebrow="Move to folder" title={`Move ${quoted(sheet.item.name)}`} confirmLabel="Move" busyLabel="Moving…"
          busy={change.pending} error={change.phase === "failed" ? change.message : ""}
          initialFocus={() => document.getElementById(folderId)} onCancel={() => { setSheet(null); refocus(sheet.item.id); }}
          onConfirm={() => void confirmSheet()}>
          <FormGrid>
            <FolderField id={folderId} folders={folders} current={sheet.item.folder} choice={sheetFolder.choice} custom={sheetFolder.custom}
              onChange={(choice, custom) => { setSheetFolder({ choice, custom }); change.reset(); }} />
          </FormGrid>
        </FormSheet>
      )}
      {sheet?.kind === "delete" && (
        <ConfirmSheet eyebrow="Delete saved query" title={`Delete ${quoted(sheet.item.name)}?`}
          description="It is removed from your saved queries on this computer. Query tabs that have its SQL keep it."
          confirmLabel="Delete" busy={change.pending} busyLabel="Deleting…"
          onCancel={() => { setSheet(null); refocus(sheet.item.id); }} onConfirm={() => void confirmSheet()}>
          {change.phase === "failed" && <p className="form-error" role="alert">{change.message}</p>}
        </ConfirmSheet>
      )}
    </div>
  );
}
