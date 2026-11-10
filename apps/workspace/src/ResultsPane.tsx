// The Query editor's results: one tab per result set, Messages (every
// statement of the run with its outcome, errors with a jump to their
// position), and the last Run analysis, with explicit navigation to Plans. Grids use
// the shared DataTable; values are drawn as text only (NULL as a token, an
// empty string as an empty cell, binary as the client's 0x hex).
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import {
  api,
  download,
  type EditorColumn,
  type EditorError,
  type EditorExport,
  type EditorResult,
  type EditorRows,
  type EditorRun,
  type EditorStatement,
} from "./api";
import { Icon } from "./components";
import { insertDefaults } from "./sqlSelection";
import { prettyJson, totals, totalsText } from "./resultTotals";
import {
  FILTER_DEBOUNCE_MS,
  MAX_PIN_ROWS,
  compareResults,
  defaultKeyColumn,
  exportFilterLabel,
  filterRows,
  filterSummary,
  pinResult,
  pruneClosedTabPins,
  sameColumns,
  upsertChip,
  type CompareDiff,
  type CompareDiffRow,
  type FilterChip,
  type PinnedResult,
} from "./resultFilter";
import { MAC } from "./platform";
import { ACTION_ELAPSED_AFTER_MS, ActionButton, ActionStatus, ContextMenu, DataTable, IconButton, Popover, StatusPill, closePopovers, sortRows,
  useElapsed, useFocusScope, useSince, type Column, type SortState, type Tone } from "./kit";
import "./result-grid.css";

const NUMBER = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/;
const PLAIN = /^(-?)(\d*)(?:\.(\d*))?$/;

/**
 * A key that sorts numbers written as text in numeric order without
 * rounding them (BIGINT past 2^53, DECIMAL with many digits): sign, digit
 * count, digits, fraction. Values with an exponent (FLOAT, DOUBLE) are
 * floating point already and sort as numbers (see numericSort).
 */
export function numberKey(value: string): string {
  const m = PLAIN.exec(value);
  if (!m) return value;
  const int = m[2].replace(/^0+/, "");
  const frac = (m[3] || "").replace(/0+$/, "");
  if (!int && !frac) return "P00000.";
  if (m[1] !== "-") return `P${String(int.length).padStart(5, "0")}${int}.${frac}`;
  // Larger magnitudes first: the length and every digit complemented, and a
  // terminator above every digit so 1.5 sorts after 1.55 (-1.5 > -1.55).
  const flip = (d: string) => d.replace(/\d/g, (c) => String(9 - Number(c)));
  return `N${String(99999 - int.length).padStart(5, "0")}${flip(int)}.${flip(frac)}~`;
}
/** The sort accessor for a column of numbers: exact keys, or doubles when
 * a value has an exponent. */
export function numericSort(values: (string | null)[]): (v: string | null) => string | number | null {
  const exponent = values.some((v) => v !== null && /[eE]/.test(v));
  return exponent ? (v) => (v === null ? null : Number(v)) : (v) => (v === null ? null : numberKey(v));
}
const HEX = /^0x(?:[0-9A-F]{2})*$/;

export function count(n: number) {
  return n.toLocaleString();
}
export function duration(ms: number | null | undefined) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  if (ms >= 60_000) return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}
const rowsText = (n: number) => `${count(n)} ${n === 1 ? "row" : "rows"}`;

/** The rows results of a run, numbered as their tabs are ("Result 1"). */
export function rowResults(run: EditorRun | null) {
  return (run?.results || []).filter((r): r is EditorRows => r.type === "rows");
}

/** "Result 1 · 1,000 rows · 12 ms" */
export function resultLabel(r: EditorRows, n: number) {
  const parts = [`Result ${n}`, r.columns_known ? rowsText(r.row_count) : "No rows"];
  const time = duration(r.duration_ms);
  if (time) parts.push(time);
  return parts.join(" · ");
}

/** Tab-separated text with the CSV rules (NULL is an empty field). */
export function tsv(rows: (string | null)[][]) {
  const field = (v: string | null) =>
    v === null ? "" : /[\t"\r\n]/.test(v) || v === "" ? `"${v.replace(/"/g, '""')}"` : v;
  return rows.map((r) => r.map(field).join("\t")).join("\r\n");
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for pages without clipboard permission.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** One value as text: NULL a token, "" an empty cell, binary hex with its size. */
export function Value({ value, hex }: { value: string | null; hex: boolean }) {
  if (value === null) return <span className="sql-null">NULL</span>;
  if (value === "")
    return (
      <span className="sql-empty" title="Empty string">
        <span className="visually-hidden">empty string</span>
      </span>
    );
  if (hex && HEX.test(value)) {
    const bytes = (value.length - 2) / 2;
    return (
      <span className="sql-hex" title={`${count(bytes)} ${bytes === 1 ? "byte" : "bytes"}, shown as hex`}>
        {value}
      </span>
    );
  }
  return <>{value}</>;
}

type GridRow = { key: string; i: number; v: (string | null)[] };
/** A cell by the row's index as the server sent it (stable across sorting)
 * and its column among the result's columns. */
type CellRef = { i: number; c: number };
/** Cells from the anchor to the focus; *allRows*: whole columns. Only the
 * columns on screen between them count (the Columns menu can hide some).
 * *members* snapshots the original indexes of the rows in the rectangle when
 * it was made: a filter that hides an endpoint must never extend the
 * selection to rows between them that the user never selected. */
type GridSelection = { anchor: CellRef; focus: CellRef; allRows: boolean; members: number[] | null };
/** The cell menu: *row* is the row's index in result.rows, *col* its column. */
type Menu = { x: number; y: number; row: number; col: number; opener: HTMLElement } | null;
/** Option on macOS, Alt elsewhere: with a click on a header it selects the column. */
const ALT = MAC ? "Option" : "Alt";
const COPY_KEYS = MAC ? "⌘C" : "Ctrl+C";

/**
 * A result grid: row numbers, sortable loaded rows, a cell selection with
 * totals in its footer, the row panel, and copy from a context menu.
 * Selection: click a cell; Shift+click or Shift+arrows extend it; click a
 * row number for the whole row; Option+click (Alt+click) a column header for
 * the whole column; ⌘A (Ctrl+A) selects every cell. ⌘C copies the selection
 * as tab-separated text. Double-click a row, or press Return on it, to show
 * it in the row panel.
 */
type RowPanelLayout = { narrow: boolean; blocked: boolean; onOpen: () => void };

function ResultGrid({ result, filteredRows, isFiltered, onAddChip, label, onCopied, rowPanel }: {
  result: EditorRows;
  filteredRows: GridRow[];
  isFiltered: boolean;
  onAddChip: (col: number, mode: "show" | "hide", value: string | null) => void;
  label: string;
  onCopied: (text: string) => void;
  rowPanel?: RowPanelLayout;
}) {
  const [sel, setSel] = useState<GridSelection | null>(null);
  const [sort, setSort] = useState<SortState | null>(null);
  const [panel, setPanel] = useState(false);
  const panelOpener = useRef<HTMLElement | null>(null);
  // The editor owns the other narrow sheets. Closing here also forgets the
  // local open state, so closing Schemas cannot resurrect an old row sheet.
  // Adjusted during render, not in an effect: an effect's update has a lower
  // priority than the Escape that closes Schemas, which could render first
  // and briefly reopen the row sheet.
  const blocked = !!rowPanel?.blocked;
  const [seenBlocked, setSeenBlocked] = useState(blocked);
  if (blocked !== seenBlocked) {
    setSeenBlocked(blocked);
    if (blocked && panel) setPanel(false);
  }
  const [menu, setMenu] = useState<Menu>(null);
  const root = useRef<HTMLDivElement>(null);
  /** The cell a mouse press landed on, applied when the table's row click selects. */
  const pressed = useRef<{ i: number; c: number | "row"; shift: boolean } | null>(null);
  /** Shift was held on the key that moves the row focus: extend, do not collapse. */
  const extendNext = useRef(false);
  const ncol = result.columns.length;
  const allRows = useMemo<GridRow[]>(() => result.rows.map((v, i) => ({ key: String(i), i, v })), [result.rows]);
  const rows = filteredRows;
  // The result's columns on screen, in order: the table reports them, and
  // selections, totals, copied text and Left/Right use only these (a hidden
  // column never enters a selection; the row panel still shows every column).
  const [visibleKeys, setVisibleKeys] = useState<string[] | null>(null);
  const shownCols = useMemo(() => {
    const all = result.columns.map((_, j) => j);
    if (!visibleKeys) return all;
    const on = new Set(visibleKeys);
    return all.filter((j) => on.has(`c${j}`));
  }, [result.columns, visibleKeys]);
  const firstCol = shownCols[0] ?? 0;
  const lastCol = shownCols[shownCols.length - 1] ?? 0;
  /** The column on screen next to *c* to the right (step 1) or left (-1); *c*'s
   * neighbour at the edge stays the edge column. */
  function stepColumn(c: number, step: 1 | -1) {
    if (!shownCols.length) return c;
    if (step > 0) return shownCols.find((j) => j > c) ?? lastCol;
    for (let k = shownCols.length - 1; k >= 0; k--) if (shownCols[k] < c) return shownCols[k];
    return firstCol;
  }
  /** *c* when it is on screen, else the next column on screen (or the last). */
  function onScreenColumn(c: number) {
    if (!shownCols.length || shownCols.includes(c)) return c;
    return shownCols.find((j) => j > c) ?? lastCol;
  }
  /** A row's values in the columns on screen. */
  const visibleValues = (row: (string | null)[]) => shownCols.map((j) => row[j] ?? null);
  const numeric = useMemo(
    () =>
      result.columns.map((_, j) => {
        let seen = false;
        for (const row of result.rows) {
          const v = row[j];
          if (v === null) continue;
          if (!NUMBER.test(v)) return false;
          seen = true;
        }
        return seen;
      }),
    [result],
  );
  const sorters = useMemo(
    () =>
      result.columns.map((_, j) => {
        const key = numericSort(result.rows.map((r) => r[j] ?? null));
        return (r: GridRow) => key(r.v[j] ?? null);
      }),
    [result],
  );
  const accessor = (key: string | undefined): ((r: GridRow) => string | number | null) | undefined => {
    if (key === "#") return (r) => r.i;
    const j = key?.startsWith("c") ? Number(key.slice(1)) : -1;
    if (!(j >= 0 && j < ncol)) return undefined;
    return numeric[j] ? sorters[j] : (r) => r.v[j];
  };
  // The grid sorts (the table only draws): the selection needs the order on screen.
  const sorted = useMemo(() => sortRows(rows, sort, accessor(sort?.key)), [rows, sort, numeric, sorters]);
  const position = useMemo(() => {
    const at = new Map<number, number>();
    sorted.forEach((r, p) => at.set(r.i, p));
    return at;
  }, [sorted]);
  // The selected rectangle in screen order: the member rows still on screen,
  // and the columns on screen from the anchor's column to the focus's (cols,
  // in order). rows === null means every row on screen (whole-column and
  // select-all selections follow the filter).
  const rect = useMemo(() => {
    if (!sel || !sorted.length) return null;
    const lo = Math.min(sel.anchor.c, sel.focus.c);
    const hi = Math.max(sel.anchor.c, sel.focus.c);
    const cols = shownCols.filter((j) => j >= lo && j <= hi);
    if (!cols.length) return null;
    if (sel.allRows) return { rows: null as Set<number> | null, cols };
    const set = new Set<number>();
    for (const i of sel.members || []) {
      const p = position.get(i);
      if (p !== undefined) set.add(p);
    }
    if (!set.size) return null;
    return { rows: set, cols };
  }, [sel, position, sorted, shownCols]);
  const rectRowCount = rect ? (rect.rows ? rect.rows.size : sorted.length) : 0;
  const cells = rectRowCount * (rect ? rect.cols.length : 0);
  const inRect = (p: number) => !!rect && (rect.rows === null || rect.rows.has(p));
  const selectedCols = rect ? rect.cols.join(",") : "";
  function selectedValues(): (string | null)[][] {
    if (!rect) return [];
    const out: (string | null)[][] = [];
    for (let p = 0; p < sorted.length; p++) {
      if (rect.rows === null || rect.rows.has(p)) out.push(rect.cols.map((j) => sorted[p].v[j] ?? null));
    }
    return out;
  }
  const summary = useMemo(() => (rect ? totalsText(totals(selectedValues().flat())) : ""), [rect, sorted]);
  const columns = useMemo<Column<GridRow>[]>(
    () => [
      {
        key: "#",
        label: "#",
        name: "Row number",
        title: "Row number in the order the server sent them. Click to select the row.",
        num: true,
        className: "cell-rownum",
        sort: (r) => r.i,
        render: (r) => r.i + 1,
      },
      ...result.columns.map(
        (c: EditorColumn, j): Column<GridRow> => ({
          key: `c${j}`,
          label: c.name,
          name: c.name,
          title: `${c.hex ? `${c.name}: every value looks like binary (0x hex); a guess` : c.name}\n${ALT}+click selects the column`,
          num: numeric[j],
          className: [
            "cell-value",
            rect && rect.cols.includes(j) ? "is-sel-col" : "",
            sel && sel.focus.c === j ? "is-focus-col" : "",
          ].filter(Boolean).join(" "),
          sort: numeric[j] ? sorters[j] : (r) => r.v[j],
          render: (r) => <Value value={r.v[j]} hex={c.hex} />,
        }),
      ),
    ],
    [result.columns, numeric, sorters, selectedCols, sel?.focus.c],
  );
  // A new result (a run, Load more) starts with nothing selected.
  useEffect(() => {
    setSel(null);
    setMenu(null);
    setPanel(false);
  }, [result]);
  /** A selection with its member rows snapshotted (original indexes), so a
   * later filter intersects it instead of growing it. Whole-column and
   * select-all selections keep no snapshot: they follow the rows on screen. */
  function makeSel(anchor: CellRef, focus: CellRef, allRows: boolean): GridSelection {
    if (allRows) return { anchor, focus, allRows, members: null };
    const a = position.get(anchor.i);
    const f = position.get(focus.i);
    if (a === undefined || f === undefined) return { anchor, focus, allRows, members: [anchor.i, focus.i] };
    const lo = Math.min(a, f);
    const hi = Math.max(a, f);
    const members: number[] = [];
    for (let p = lo; p <= hi; p++) members.push(sorted[p].i);
    return { anchor, focus, allRows, members };
  }
  const one = (c: CellRef): GridSelection => makeSel(c, c, false);
  function selectCell(i: number, c: number, extend: boolean) {
    setSel((s) => (extend && s ? makeSel(s.anchor, { i, c }, false) : one({ i, c })));
  }
  function selectRow(i: number, extend: boolean) {
    setSel((s) =>
      extend && s ? makeSel({ i: s.anchor.i, c: firstCol }, { i, c: lastCol }, false)
        : makeSel({ i, c: firstCol }, { i, c: lastCol }, false));
  }
  function selectColumn(c: number, extend: boolean) {
    if (!sorted.length) return;
    const top = sorted[0].i;
    const bottom = sorted[sorted.length - 1].i;
    setSel((s) => makeSel({ i: top, c: extend && s ? s.anchor.c : c }, { i: bottom, c }, true));
  }
  function selectAll() {
    if (!sorted.length || !shownCols.length) return;
    setSel(makeSel({ i: sorted[0].i, c: firstCol }, { i: sorted[sorted.length - 1].i, c: lastCol }, true));
  }
  /** The table moved its row focus (a click, or an arrow, Home, End, Page key). */
  function onSelect(key: string) {
    const i = Number(key);
    const press = pressed.current;
    pressed.current = null;
    if (press && press.i === i) {
      if (press.c === "row") selectRow(i, press.shift);
      else selectCell(i, press.c, press.shift);
      return;
    }
    const extend = extendNext.current;
    extendNext.current = false;
    setSel((s) => {
      const c = s ? s.focus.c : firstCol;
      return extend && s
        ? makeSel(s.allRows ? { i: sorted[0]?.i ?? i, c: s.anchor.c } : s.anchor, { i, c }, false)
        : one({ i, c });
    });
  }
  /** Bring a row into view in the table (drawn or not: long tables draw a window). */
  function reveal(i: number) {
    const frame = root.current?.querySelector<HTMLElement>(".data-table-wrap");
    if (!frame) return;
    const tr = frame.querySelector<HTMLElement>(`tbody tr[data-row-key="${i}"]`);
    if (tr) {
      tr.scrollIntoView({ block: "nearest" });
      return;
    }
    // The row's top under the sticky header, as the table's own keyboard moves do.
    const sample = frame.querySelector<HTMLElement>("tbody tr[data-row-key]");
    const p = position.get(i);
    if (sample && p !== undefined) frame.scrollTop = p * sample.offsetHeight;
  }
  /** Previous or next row on screen, from the row panel. */
  function move(step: number) {
    if (!sel) return;
    const p = position.get(sel.focus.i);
    if (p === undefined) return;
    const next = sorted[Math.max(0, Math.min(sorted.length - 1, p + step))];
    if (!next || next.i === sel.focus.i) return;
    setSel(one({ i: next.i, c: sel.focus.c }));
    reveal(next.i);
  }
  function showRow(i: number) {
    setSel((s) => (s && s.focus.i === i && !s.allRows ? s : one({ i, c: s ? s.focus.c : 0 })));
    panelOpener.current = menu?.opener || root.current?.querySelector<HTMLElement>(`tbody tr[data-row-key="${i}"]`) || null;
    rowPanel?.onOpen();
    setPanel(true);
  }
  function closePanel() {
    setPanel(false);
    // Back to the row on screen in the panel, which may not be the row it was
    // opened from (Previous and Next row move it). In a narrow window this
    // runs after the sheet's focus scope returned to its opener.
    const i = sel?.focus.i;
    if (i !== undefined) reveal(i);
    window.requestAnimationFrame(() =>
      root.current?.querySelector<HTMLElement>(`tbody tr[data-row-key="${i ?? ""}"]`)?.focus({ preventScroll: true }));
  }
  function cellAt(target: EventTarget | null) {
    const el = target as HTMLElement | null;
    const tr = el?.closest?.("tbody tr[data-row-key]") as HTMLElement | null;
    if (!tr || !root.current?.contains(tr)) return null;
    const td = el?.closest?.("td[data-col]") as HTMLElement | null;
    const i = Number(tr.dataset.rowKey);
    const colKey = td?.dataset.col || "";
    const c: number | "row" | null = colKey === "#" ? "row" : colKey.startsWith("c") ? Number(colKey.slice(1)) : null;
    return { i, c, tr, td };
  }
  function onMouseDown(e: MouseEvent<HTMLDivElement>) {
    if (e.button !== 0) return;
    const at = cellAt(e.target);
    if (!at || at.c === null || (e.target as HTMLElement).closest("button, a, input")) return;
    pressed.current = { i: at.i, c: at.c, shift: e.shiftKey };
    // Shift+click extends the selection, not the page's text selection.
    if (e.shiftKey) e.preventDefault();
  }
  function onClickCapture(e: MouseEvent<HTMLDivElement>) {
    // Option+click (Alt+click) on a header selects the column instead of sorting.
    const th = (e.target as HTMLElement).closest?.("thead th[data-col]") as HTMLElement | null;
    if (!th || !e.altKey || (e.target as HTMLElement).closest(".col-resize")) return;
    const key = th.dataset.col || "";
    if (!key.startsWith("c")) return;
    e.preventDefault();
    e.stopPropagation();
    selectColumn(Number(key.slice(1)), e.shiftKey);
  }
  function openMenu(e: MouseEvent<HTMLDivElement>) {
    const at = cellAt(e.target);
    if (!at) return;
    e.preventDefault();
    const c = typeof at.c === "number" ? at.c : onScreenColumn(sel?.focus.c ?? firstCol);
    // A right click outside the selection selects that cell first.
    const p = position.get(at.i);
    const inside = p !== undefined && inRect(p) && rect!.cols.includes(c);
    if (!inside) {
      if (at.c === "row") selectRow(at.i, false);
      else selectCell(at.i, c, false);
    }
    setMenu({ x: e.clientX, y: e.clientY, row: at.i, col: c, opener: at.tr });
  }
  function onKeyDownCapture(e: KeyboardEvent<HTMLDivElement>) {
    const target = e.target as HTMLElement;
    const at = cellAt(target);
    if (!at || (target !== at.tr && target !== at.td)) return;
    pressed.current = null;
    const focusC = sel && sel.focus.i === at.i ? sel.focus.c : typeof at.c === "number" && target === at.td ? at.c : firstCol;
    if (["ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(e.key)) {
      extendNext.current = e.shiftKey;
      return;
    }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      // Left and Right move the focused cell (the frame scrolls to show it).
      e.preventDefault();
      e.stopPropagation();
      // Hidden columns are stepped over.
      const c = stepColumn(focusC, e.key === "ArrowRight" ? 1 : -1);
      selectCell(at.i, c, e.shiftKey);
      window.requestAnimationFrame(() =>
        at.tr.querySelector<HTMLElement>(`td[data-col="c${c}"]`)?.scrollIntoView({ block: "nearest", inline: "nearest" }));
      return;
    }
    const mod = e.metaKey || e.ctrlKey;
    if (mod && !e.altKey && e.key.toLowerCase() === "a") {
      e.preventDefault();
      selectAll();
      return;
    }
    if (mod && !e.altKey && e.key.toLowerCase() === "c" && !window.getSelection()?.toString()) {
      // The selection as tab-separated text; one cell, its value as is;
      // nothing selected yet, the row.
      e.preventDefault();
      if (rect && cells > 1) void copySelection();
      else if (rect) {
        const p0 = rect.rows ? Math.min(...rect.rows) : 0;
        const value = sorted[p0].v[rect.cols[0]] ?? null;
        void copy(value ?? "", `Value of ${name(rect.cols[0])} copied.`);
      } else void copy(tsv([visibleValues(result.rows[at.i])]), "Row copied as tab-separated text.");
      return;
    }
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      e.stopPropagation();
      const box = (at.td || at.tr).getBoundingClientRect();
      const c = target === at.td && typeof at.c === "number" ? at.c : onScreenColumn(focusC);
      setMenu({ x: box.left + 24, y: box.bottom, row: at.i, col: c, opener: target });
    }
  }
  async function copy(text: string, said: string) {
    setMenu(null);
    if (await copyText(text)) onCopied(said);
    else onCopied("The browser did not allow copying.");
  }
  async function copySelection() {
    const values = selectedValues();
    await copy(tsv(values), `${count(cells)} ${cells === 1 ? "cell" : "cells"} copied as tab-separated text.`);
  }
  const name = (col: number) => result.columns[col]?.name || `column ${col + 1}`;
  const focusRow = panel && !rowPanel?.blocked && sel && position.has(sel.focus.i) ? allRows[sel.focus.i] : undefined;
  return (
    <div ref={root} className={`result-grid${panel ? " has-row-panel" : ""}`} onContextMenu={openMenu}
      onKeyDownCapture={onKeyDownCapture} onMouseDown={onMouseDown} onClickCapture={onClickCapture}>
      <div className="result-grid-body">
        <DataTable<GridRow>
          label={label}
          columns={columns}
          rows={sorted}
          rowKey={(r) => r.key}
          sort={sort}
          onSortChange={setSort}
          selection={{ key: sel ? String(sel.focus.i) : "", select: onSelect }}
          onActivate={(r) => showRow(r.i)}
          rowAttrs={(r) => {
            const p = position.get(r.i) ?? -1;
            return {
              "data-sel": p >= 0 && inRect(p) ? "" : undefined,
              "data-focus": sel && sel.focus.i === r.i ? "" : undefined,
            };
          }}
          // ResultGrid draws its own menu (Show row, Select column, filter chips, Copy).
          nativeRowMenu={false}
          className="result-table"
          onVisibleColumns={setVisibleKeys}
          rowAction={(r) => <button type="button" className="icon-button row-more" aria-label={`More actions for row ${r.i + 1}`}
            title="More actions" aria-haspopup="menu" aria-expanded={menu?.row === r.i}
            onClick={(e) => {
              e.stopPropagation();
              const box = e.currentTarget.getBoundingClientRect();
              const c = sel && sel.focus.i === r.i ? onScreenColumn(sel.focus.c) : firstCol;
              if (!sel || sel.focus.i !== r.i) setSel(one({ i: r.i, c }));
              setMenu({ x: box.left, y: box.bottom, row: r.i, col: c, opener: e.currentTarget });
            }}><Icon name="more" /></button>}
        />
        {focusRow && (
          <RowPanel
            narrow={rowPanel?.narrow || false}
            opener={panelOpener.current}
            result={result}
            row={focusRow}
            at={(position.get(focusRow.i) ?? 0) + 1}
            total={sorted.length}
            onMove={move}
            onClose={closePanel}
            onCopied={onCopied}
          />
        )}
      </div>
      <p className={`result-grid-footer${summary ? "" : " is-empty"}`} data-cells={cells || undefined}
        title={`Click a cell; Shift+click or Shift+arrows select a range; ${ALT}+click a header selects the column; ${COPY_KEYS} copies the selection.`}>
        {summary || "Select cells to see their totals"}
      </p>
      {menu && (
        <ContextMenu
          x={menu.x}
          y={menu.y}
          label="Result actions"
          opener={menu.opener}
          onClose={() => setMenu(null)}
          actions={[
            { label: "Show row", icon: "view", shortcut: MAC ? "Return" : "Enter", onClick: () => showRow(menu.row) },
            { label: `Select column (${name(menu.col)})`, icon: "columns", onClick: () => selectColumn(menu.col, false) },
            {
              label: "Show only this value",
              onClick: () => {
                onAddChip(menu.col, "show", result.rows[menu.row]?.[menu.col] ?? null);
                setMenu(null);
              },
            },
            {
              label: "Hide this value",
              onClick: () => {
                onAddChip(menu.col, "hide", result.rows[menu.row]?.[menu.col] ?? null);
                setMenu(null);
              },
            },
            { divider: true },
            ...(cells > 1
              ? [{ label: `Copy selection (${count(cells)} cells)`, icon: "copy", shortcut: COPY_KEYS, onClick: () => void copySelection() }]
              : []),
            { label: `Copy value (${name(menu.col)})`, icon: "copy", onClick: () => void copy(result.rows[menu.row][menu.col] ?? "", `Value of ${name(menu.col)} copied.`) },
            { label: "Copy row", onClick: () => void copy(tsv([visibleValues(result.rows[menu.row])]), "Row copied as tab-separated text.") },
            {
              label: `Copy column (${name(menu.col)})`,
              onClick: () =>
                void copy(
                  tsv(sorted.map((r) => [r.v[menu.col]])),
                  isFiltered
                    ? `Column ${name(menu.col)} copied (${rowsText(sorted.length)} filtered).`
                    : `Column ${name(menu.col)} copied (${rowsText(result.rows.length)} loaded).`,
                ),
            },
            {
              label: "Copy all rows with header",
              onClick: () =>
                void copy(
                  tsv([shownCols.map((j) => result.columns[j].name), ...sorted.map((r) => visibleValues(r.v))]),
                  isFiltered ? `Filtered result copied with its header (${rowsText(sorted.length)}).` : "Result copied with its header.",
                ),
            },
          ]}
        />
      )}
    </div>
  );
}

/** One value in the row panel: NULL as a muted token, never empty; binary as
 * hex with its size; JSON laid out in a block with Copy; other text as is. */
function RowValue({ value, column, onCopied }: { value: string | null; column: EditorColumn; onCopied: (text: string) => void }) {
  if (value === null) return <span className="sql-null">NULL</span>;
  if (value === "") return <span className="row-panel-empty">empty string</span>;
  if (column.hex && HEX.test(value)) {
    const bytes = (value.length - 2) / 2;
    return (
      <span className="row-panel-hex">
        <span className="row-panel-text">{value}</span>
        <span className="row-panel-size">{count(bytes)} {bytes === 1 ? "byte" : "bytes"}, shown as hex</span>
      </span>
    );
  }
  const json = /json/i.test(column.type || "") || /^\s*[[{]/.test(value) ? prettyJson(value) : null;
  if (json !== null)
    return (
      <div className="row-panel-json-block">
        <pre className="row-panel-json" aria-label={`${column.name} as JSON`}>{json}</pre>
        <button type="button" className="button small row-panel-copy" onClick={async () =>
          onCopied((await copyText(json)) ? `JSON of ${column.name} copied.` : "The browser did not allow copying.")}>
          <Icon name="copy" size={14} />
          Copy JSON
        </button>
      </div>
    );
  return <span className="row-panel-text">{value}</span>;
}

/** The row panel ("Show row"): every column of one row with its type when
 * known. Up and Down (or the buttons) move to the previous or next row on
 * screen; Escape closes it. Below 720 px it is a bottom sheet. */
function RowPanel({
  narrow,
  opener,
  result,
  row,
  at,
  total,
  onMove,
  onClose,
  onCopied,
}: {
  narrow: boolean;
  opener: HTMLElement | null;
  result: EditorRows;
  row: GridRow;
  /** Its place on screen (1-based) and the rows loaded. */
  at: number;
  total: number;
  onMove: (step: number) => void;
  onClose: () => void;
  onCopied: (text: string) => void;
}) {
  const titleId = useId();
  const panel = useRef<HTMLElement>(null);
  useFocusScope(panel, {
    open: narrow, onClose, returnFocus: () => opener,
    initialFocus: () => panel.current?.querySelector<HTMLElement>(".row-panel-fields"),
  });
  function onKey(e: KeyboardEvent<HTMLElement>) {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      onClose();
      return;
    }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && !e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      if ((e.target as HTMLElement).closest("input, textarea, select")) return;
      e.preventDefault();
      e.stopPropagation();
      onMove(e.key === "ArrowUp" ? -1 : 1);
    }
  }
  return (
    <section ref={panel} className="row-panel" role={narrow ? "dialog" : undefined} tabIndex={-1}
      aria-labelledby={titleId} onKeyDown={onKey} data-row={row.i}>
      <div className="row-panel-head">
        <h3 id={titleId} className="row-panel-title">Row {count(row.i + 1)}</h3>
        <span className="row-panel-pos">· {count(at)} of {count(total)}</span>
        <IconButton icon="chevron" className="row-panel-step is-up" label="Previous row" title="Previous row (Up)"
          aria-disabled={at <= 1 || undefined} onClick={() => onMove(-1)} />
        <IconButton icon="chevron" className="row-panel-step is-down" label="Next row" title="Next row (Down)"
          aria-disabled={at >= total || undefined} onClick={() => onMove(1)} />
        <IconButton icon="close" label="Close the row panel" title="Close (Escape)" onClick={onClose} />
      </div>
      <dl className="row-panel-fields" tabIndex={0} aria-label={`Columns of row ${row.i + 1}`}>
        {result.columns.map((c, j) => (
          <div className="row-panel-field" key={j} data-col={`c${j}`}>
            <dt>
              <span className="row-panel-name">{c.name}</span>
              {c.type && <span className="row-panel-type">{c.type}</span>}
            </dt>
            <dd>
              <RowValue value={row.v[j] ?? null} column={c} onCopied={onCopied} />
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

/** Export one result: CSV, JSON, SQL INSERT, Markdown, or the clipboard (TSV). */
function ExportMenu({
  result,
  isFiltered = false,
  loadedCount,
  n,
  engine,
  binary,
  onBinary,
  onDone,
  editorRun,
  backslash,
  columnTypes,
}: {
  result: EditorRows;
  isFiltered?: boolean;
  loadedCount?: number;
  n: number;
  engine: string | null;
  binary: number[];
  onBinary: (column: number, on: boolean) => void;
  onDone: (text: string, error?: boolean) => void;
  /** The run and the session's escapes: the SQL INSERT target starts as the
   * table the result's statement read (still editable). */
  editorRun: EditorRun | null;
  backslash: boolean;
  columnTypes?: (result: EditorRows) => Promise<(string | null)[]>;
}) {
  // Read once per result (the menu is keyed by run and result).
  const [defaults] = useState(() => insertDefaults(editorRun, result, backslash));
  const [table, setTable] = useState(defaults.table);
  const [schema, setSchema] = useState(defaults.schema);
  const [guard, setGuard] = useState(false);
  const [busy, setBusy] = useState("");
  const busySince = useSince(!!busy);
  const hexColumns = result.columns.map((c, j) => [c, j] as const).filter(([c]) => c.hex);
  const filterTitle = isFiltered ? exportFilterLabel(result.rows.length) : undefined;
  async function run(format: "csv" | "json" | "sql" | "markdown" | "tsv") {
    if (busy) return;
    setBusy(format);
    try {
      // Server result types win. SQL alone may fill gaps for proven direct
      // column references; expressions and unreadable metadata stay quoted.
      const types = format === "sql" && result.columns.some(c => !c.type)
        ? await columnTypes?.(result) : undefined;
      const columns = result.columns.map((c, j) => ({ name: c.name, hex: c.hex, binary: binary.includes(j), type: c.type || types?.[j] || null }));
      const reply = await api<EditorExport>("editor/export", {
        format,
        result: {
          columns,
          rows: result.rows,
        },
        engine: engine || "mysql",
        ...(format === "sql" ? { table: table.trim(), schema: schema.trim() || undefined } : {}),
        options: format === "csv" ? { header: true, formula_guard: guard } : {},
      });
      if (format === "tsv") {
        onDone(
          (await copyText(reply.content))
            ? `Result ${n} copied as tab-separated text (${rowsText(result.rows.length)}${isFiltered ? " filtered" : ""}).`
            : "The browser did not allow copying.",
        );
      } else {
        download(reply.content, reply.filename, reply.mime);
        onDone(`Result ${n} exported as ${reply.filename}${isFiltered ? ` (${rowsText(result.rows.length)} filtered)` : ""}.`);
      }
      closePopovers();
    } catch (e) {
      onDone((e as Error).message, true);
    } finally {
      setBusy("");
    }
  }
  return (
    <Popover
      label={`Export result ${n}`}
      className="result-export"
      buttonClassName="button small"
      buttonTitle={filterTitle || "Export or copy this result"}
      wide
      trigger={
        <>
          <Icon name="export" size={14} />
          {filterTitle || "Export"}
        </>
      }
    >
      <h3 className="popover-title">{filterTitle || `Export result ${n}`}</h3>
      <p className="hint">
        {isFiltered
          ? `${rowsText(result.rows.length)} of ${count(loadedCount ?? result.rows.length)} loaded rows matching the filter. NULL is an empty CSV field and JSON null; an empty string is "".`
          : `${rowsText(result.rows.length)} as loaded. NULL is an empty CSV field and JSON null; an empty string is "".`}
      </p>
      {/* The clicked format shows that it works (and keeps focus); the others wait. */}
      <div className="export-actions">
        <ActionButton small busy={busy === "csv"} busyLabel="Exporting…" aria-disabled={(!!busy && busy !== "csv") || undefined} onClick={() => void run("csv")}>
          CSV
        </ActionButton>
        <ActionButton small busy={busy === "json"} busyLabel="Exporting…" aria-disabled={(!!busy && busy !== "json") || undefined} onClick={() => void run("json")}>
          JSON
        </ActionButton>
        <ActionButton small busy={busy === "markdown"} busyLabel="Exporting…" aria-disabled={(!!busy && busy !== "markdown") || undefined} onClick={() => void run("markdown")}>
          Markdown
        </ActionButton>
        <ActionButton small icon="copy" busy={busy === "tsv"} busyLabel="Copying…" aria-disabled={(!!busy && busy !== "tsv") || undefined} onClick={() => void run("tsv")}>
          Copy as TSV
        </ActionButton>
      </div>
      <ActionStatus className="export-status" state={{ phase: busy ? "pending" : "idle", startedAt: busySince,
        message: busy === "tsv" ? `Copying result ${n}…` : `Exporting result ${n} as ${busy === "markdown" ? "Markdown" : busy.toUpperCase()}…` }} />
      <label className="checkbox export-guard" style={{ whiteSpace: "normal", alignItems: "flex-start" }}
        title="Prefix CSV values starting with = + - @ with a single quote to protect spreadsheet formulas">
        <input type="checkbox" checked={guard} onChange={(e) => setGuard(e.target.checked)} />
        <span>Protect spreadsheet formulas (CSV)</span>
      </label>
      <fieldset className="export-sql">
        <legend>SQL INSERT</legend>
        <label className="field">
          Table
          <input value={table} placeholder="Target table name" onChange={(e) => setTable(e.target.value)} />
        </label>
        <label className="field">
          Schema (optional)
          <input value={schema} onChange={(e) => setSchema(e.target.value)} />
        </label>
        <ActionButton small busy={busy === "sql"} busyLabel="Exporting…" disabled={busy !== "sql" && !table.trim()}
          aria-disabled={(!!busy && busy !== "sql") || undefined} onClick={() => void run("sql")}>
          Download SQL
        </ActionButton>
      </fieldset>
      {hexColumns.length > 0 && (
        <fieldset className="export-binary">
          <legend>Binary columns</legend>
          <p className="hint">These look like binary (0x hex), a guess. Marked ones are written as X'..' in SQL; others stay quoted text.</p>
          {hexColumns.map(([c, j]) => (
            <label className="checkbox" key={j}>
              <input type="checkbox" checked={binary.includes(j)} onChange={(e) => onBinary(j, e.target.checked)} />
              <span>Treat {c.name} as binary</span>
            </label>
          ))}
        </fieldset>
      )}
    </Popover>
  );
}

const STATUS: Record<EditorStatement["status"], [Tone, string]> = {
  pending: ["unknown", "Waiting"],
  running: ["info", "Running"],
  ok: ["ok", "Done"],
  error: ["problem", "Error"],
  stopped: ["watch", "Stopped"],
  not_run: ["off", "Not run"],
  session_ended: ["problem", "Outcome unknown"],
};

/** One statement in Messages: its outcome, then any error with a jump to it. */
function StatementMessage({
  statement,
  results,
  base,
  stale,
  onJump,
}: {
  statement: EditorStatement;
  results: EditorResult[];
  base: number;
  stale: boolean;
  onJump: (offset: number) => void;
}) {
  const [tone, word] = STATUS[statement.status] || STATUS.pending;
  const verb = statement.verb ? statement.verb.toUpperCase() : "";
  const facts: string[] = [];
  for (const r of results) {
    if (r.type === "rows") facts.push(r.columns_known ? rowsText(r.row_count) + (r.label ? ` (${r.label})` : "") : "No rows");
    if (r.type === "ok") {
      if (r.affected_rows !== null && r.affected_rows >= 0) facts.push(`${count(r.affected_rows)} ${r.affected_rows === 1 ? "row" : "rows"} affected`);
      if (r.info) facts.push(r.info);
    }
    if (r.type !== "error" && r.warnings) facts.push(`${count(r.warnings)} ${r.warnings === 1 ? "warning" : "warnings"}`);
  }
  const time = duration(statement.duration_ms);
  if (time) facts.push(time);
  const errors = results.filter((r): r is EditorError => r.type === "error");
  const sent = statement.sent || statement.text;
  const first = sent.trim().split("\n")[0];
  return (
    <li className={`message-row is-${statement.status}`} data-statement={statement.index}>
      <div className="message-head">
        <StatusPill tone={tone} label={word} />
        <span className="message-title">
          Statement {statement.index + 1}
          {verb ? ` · ${verb}` : ""}
        </span>
        {facts.length > 0 && <span className="message-facts">{facts.join(" · ")}</span>}
      </div>
      <code className="message-sql" title={sent}>
        {first.length > 160 ? first.slice(0, 160) + "…" : first}
      </code>
      {errors.map((e, k) => (
        <div className="message-error" key={k} role={k === 0 ? "note" : undefined}>
          <span>
            {e.interrupted ? "Stopped: " : `ERROR ${e.code ?? ""}${e.sqlstate ? ` (${e.sqlstate})` : ""}: `}
            {e.message}
          </span>
          {e.position && (
            <button
              type="button"
              className="text-button"
              title={stale ? "The text changed since this run, so the position may have moved" : "Put the cursor at the error"}
              onClick={() => onJump(base + e.position!.offset)}
            >
              Go to line {e.position.line}, column {e.position.column}
            </button>
          )}
        </div>
      ))}
    </li>
  );
}

export type ResultsPaneProps = {
  /** Narrow sheets are mutually exclusive; wide side panels stay independent. */
  rowPanel?: RowPanelLayout;
  run: EditorRun | null;
  running: boolean;
  /** Editor offset of the run's sql[0] (positions in the run are relative to it). */
  base: number;
  /** The editor text changed since the run. */
  stale: boolean;
  /** The connection name when these results came from another connection than the one in use. */
  fromConnection?: string;
  /** "messages", "plan" or "result:N" (0-based among the rows results). */
  selected: string;
  onSelect: (key: string) => void;
  engine: string | null;
  plan: ReactNode | null;
  explain?: boolean;
  explaining?: boolean;
  onExplore?: (result: EditorRows) => void;
  exploreBusy?: boolean;
  exploreDisabled?: boolean;
  onJump: (offset: number) => void;
  onLoadMore: (result: EditorRows) => void;
  /** Columns marked binary for export, per rows result (0-based). */
  binary: Record<number, number[]>;
  onBinary: (result: number, column: number, on: boolean) => void;
  /** Results reloaded with Load more: their note. */
  reloaded: Record<number, string>;
  onNote: (text: string, error?: boolean) => void;
  /** Statements that run read only (Load more re-runs only those). */
  canLoadMore: (result: EditorRows) => boolean;
  empty: ReactNode;
  /** The session reads backslashes in strings as escapes (sql_mode). */
  backslash?: boolean;
  /** Read declared types for direct references in this result's statement. */
  columnTypes?: (result: EditorRows) => Promise<(string | null)[]>;
};

/** The results area: result tabs, Messages, and the plan tab. */
/** What a finished run is announced as. */
function runSummary(run: EditorRun) {
  const c = run.counts;
  if (c.errors) return `Run finished with ${c.errors} ${c.errors === 1 ? "error" : "errors"}.`;
  if (run.state === "stopped") return "Run stopped.";
  return `Run finished: ${c.run} ${c.run === 1 ? "statement" : "statements"}, ${c.rows.toLocaleString()} ${c.rows === 1 ? "row" : "rows"}.`;
}

/** Read the active Query editor tab id from the tablist so pinned results stay scoped to their tab. */
function activeQueryTabId(): string {
  if (typeof document === "undefined") return "default";
  return document.querySelector('.query-tablist [role="tab"][aria-selected="true"]')?.getAttribute("data-tab-id") || "default";
}

export function ResultsPane(props: ResultsPaneProps) {
  const { run, running, selected, onSelect, plan } = props;
  const runningLabel = props.explaining ? "Explaining…" : "Running…";
  // A run reacts at once, also the first one in a tab (which has no results
  // tabs yet): a spinner, "Running…" and its seconds in the results area.
  // One polite region, mounted in both layouts, announces the start and the end.
  const since = useSince(running);
  const seconds = useElapsed(running ? since : null);
  const [announce, setAnnounce] = useState("");
  const wasRunning = useRef(running);
  // The run shown when this run started: a run that ends without a new one
  // (a request refused with an error) announces nothing, never the old run's
  // counts (its error is announced by the editor's alert).
  const runAtStart = useRef<EditorRun | null>(null);
  useEffect(() => {
    if (running && !wasRunning.current) {
      runAtStart.current = run;
      setAnnounce(runningLabel);
    } else if (!running && wasRunning.current) setAnnounce(run && run !== runAtStart.current ? runSummary(run) : "");
    wasRunning.current = running;
  }, [running, run, runningLabel]);
  const live = <span className="visually-hidden" role="status" aria-live="polite">{announce}</span>;
  const elapsed = running && seconds * 1000 >= ACTION_ELAPSED_AFTER_MS
    ? <span className="action-elapsed" aria-hidden="true">{seconds} s so far</span> : null;
  // While a run is out its rows are not sent yet: Messages shows its progress.
  const rows = running ? [] : rowResults(run);
  const errors = run ? run.results.filter((r) => r.type === "error" && !r.interrupted).length : 0;
  const keys: { key: string; label: ReactNode; title: string }[] = rows.map((r, i) => ({
    key: `result:${i}`,
    label: props.explain ? resultLabel(r, i + 1).replace(`Result ${i + 1}`, "Explain") : resultLabel(r, i + 1),
    title: `Statement ${r.statement + 1}`,
  }));
  if (run) keys.push({ key: "messages", label: <>Messages{errors > 0 && <span className="tab-count is-error">{errors}</span>}</>, title: "Every statement of the run" });
  if (plan) keys.push({ key: "plan", label: "Analysis", title: "The last Run analysis of this tab" });
  const active = keys.some((k) => k.key === selected) ? selected : keys[0]?.key || "";
  const list = useRef<HTMLDivElement>(null);

  const tabId = activeQueryTabId();
  const index = active.startsWith("result:") ? Number(active.slice(7)) : -1;
  const result = index >= 0 ? rows[index] : null;
  const resultKey = `${tabId}:${run?.id || ""}:${index}`;

  const [pinsByTab, setPinsByTab] = useState<Record<string, PinnedResult>>({});
  // A pin lives in memory only and dies with its query tab: drop entries whose
  // tab id is no longer in the tablist (a tab close re-renders this pane).
  useEffect(() => {
    const tablist = document.querySelector(".query-tablist");
    if (!tablist) return;
    const openIds = Array.from(tablist.querySelectorAll('[role="tab"][data-tab-id]')).map(
      (el) => el.getAttribute("data-tab-id") || "",
    );
    setPinsByTab((prev) => pruneClosedTabPins(prev, openIds));
  });
  const [seenResult, setSeenResult] = useState<{ key: string; ref: EditorRows | null }>({ key: "", ref: null });
  const [filterText, setFilterText] = useState("");
  const [debouncedText, setDebouncedText] = useState("");
  const [chips, setChips] = useState<FilterChip[]>([]);
  const [comparing, setComparing] = useState(false);
  const [keyCol, setKeyCol] = useState<number>(-1);

  if (seenResult.key !== resultKey || seenResult.ref !== result) {
    setSeenResult({ key: resultKey, ref: result });
    if (filterText !== "") setFilterText("");
    if (debouncedText !== "") setDebouncedText("");
    if (chips.length > 0) setChips([]);
    if (comparing) setComparing(false);
    setKeyCol(result ? defaultKeyColumn(result.columns) : -1);
  }

  useEffect(() => {
    if (filterText === "") {
      setDebouncedText("");
      return;
    }
    const timer = window.setTimeout(() => setDebouncedText(filterText), FILTER_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [filterText]);

  const allGridRows = useMemo<GridRow[]>(
    () => (result ? result.rows.map((v, i) => ({ key: String(i), i, v })) : []),
    [result],
  );
  const isFiltered = debouncedText !== "" || chips.length > 0;
  const filteredGridRows = useMemo<GridRow[]>(
    () => filterRows(allGridRows, (r) => r.v, debouncedText, chips),
    [allGridRows, debouncedText, chips],
  );
  const exportResult = useMemo<EditorRows | null>(
    () => (result ? (isFiltered ? { ...result, rows: filteredGridRows.map((r) => r.v), row_count: filteredGridRows.length } : result) : null),
    [result, isFiltered, filteredGridRows],
  );

  const pinned = pinsByTab[tabId] || null;
  const isPinnedCurrent =
    !!pinned &&
    !!result &&
    pinned.runId === (run?.id || "") &&
    pinned.statement === result.statement &&
    pinned.resultN === index + 1;
  const canCompareWithPinned = !!pinned && !!result && !isPinnedCurrent && sameColumns(pinned.columns, result.columns);

  function clearFilter() {
    setFilterText("");
    setDebouncedText("");
    setChips([]);
  }
  function addChip(col: number, mode: "show" | "hide", value: string | null) {
    if (!result) return;
    const colName = result.columns[col]?.name || `column ${col + 1}`;
    setChips((prev) => upsertChip(prev, col, colName, mode, value));
  }
  function removeChip(id: string) {
    setChips((prev) => prev.filter((c) => c.id !== id));
  }
  function onPin() {
    if (!result) return;
    const next = pinResult(result, run?.id || "", index + 1);
    setPinsByTab((prev) => ({ ...prev, [tabId]: next }));
    setComparing(false);
    setKeyCol(defaultKeyColumn(result.columns));
    // The toolbar's pin badge carries the confirmation; no footer line
    // repeats it.
  }

  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const next =
      e.key === "ArrowRight" ? (i + 1) % keys.length : e.key === "ArrowLeft" ? (i - 1 + keys.length) % keys.length : e.key === "Home" ? 0 : e.key === "End" ? keys.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    onSelect(keys[next].key);
    list.current?.querySelector<HTMLElement>(`[data-key="${keys[next].key}"]`)?.focus();
  }
  if (!keys.length)
    return (
      <div className="results-pane is-empty" aria-label="Results" aria-busy={running || undefined}>
        {live}
        {running ? (
          <p className="results-running results-first-run">
            <span className="spinner" aria-hidden="true" /> {runningLabel}{elapsed}
          </p>
        ) : props.empty}
      </div>
    );
  return (
    <div className="results-pane" aria-busy={running || undefined}>
      {live}
      <div className="results-head">
        <div className="results-tabs" role="tablist" aria-label="Results" ref={list}>
          {keys.map((k, i) => (
            <button
              key={k.key}
              type="button"
              role="tab"
              id={`results-tab-${k.key.replace(":", "-")}`}
              data-key={k.key}
              aria-selected={k.key === active}
              aria-controls="results-panel"
              tabIndex={k.key === active ? 0 : -1}
              title={k.title}
              onClick={() => onSelect(k.key)}
              onKeyDown={(e) => onKey(e, i)}
            >
              {k.label}
            </button>
          ))}
          {running && (
            <span className="results-running">
              <span className="spinner" aria-hidden="true" /> {runningLabel}{elapsed}
            </span>
          )}
        </div>
        {result?.columns_known && exportResult && (
          <div className="results-tabs-actions">
            {props.explain && props.onExplore && <ActionButton small busy={props.exploreBusy} busyLabel="Exploring…"
              disabled={!props.exploreBusy && props.exploreDisabled}
              title="Capture a visual plan in this tab's session; the visual capture supports SELECT only"
              onClick={() => props.onExplore?.(result)}>Explore</ActionButton>}
            <ExportMenu
              // A new result starts from its own statement's table.
              key={`${props.run?.id || ""}:${index}`}
              editorRun={props.run}
              backslash={props.backslash !== false}
              columnTypes={props.columnTypes}
              result={exportResult}
              isFiltered={isFiltered}
              loadedCount={result.rows.length}
              n={index + 1}
              engine={props.engine}
              binary={props.binary[index] || []}
              onBinary={(column, on) => props.onBinary(index, column, on)}
              onDone={props.onNote}
            />
          </div>
        )}
      </div>
      <div
        className="results-panel"
        id="results-panel"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby={`results-tab-${active.replace(":", "-")}`}
      >
        {result && (
          <ResultView
            {...props}
            result={result}
            n={index + 1}
            filterText={filterText}
            onFilterText={setFilterText}
            chips={chips}
            onAddChip={addChip}
            onRemoveChip={removeChip}
            onClearFilter={clearFilter}
            isFiltered={isFiltered}
            filteredRows={filteredGridRows}
            pinned={pinned}
            isPinnedCurrent={isPinnedCurrent}
            canCompareWithPinned={canCompareWithPinned}
            comparing={comparing && canCompareWithPinned}
            onPin={onPin}
            onToggleCompare={() => setComparing((c) => !c)}
            keyCol={keyCol}
            onKeyColChange={setKeyCol}
          />
        )}
        {active === "messages" && run && <Messages {...props} run={run} />}
        {active === "plan" && plan}
      </div>
    </div>
  );
}

function ResultView({
  result,
  n,
  onLoadMore,
  canLoadMore,
  reloaded,
  onNote,
  run,
  fromConnection,
  rowPanel,
  filterText,
  onFilterText,
  chips,
  onAddChip,
  onRemoveChip,
  onClearFilter,
  isFiltered,
  filteredRows,
  pinned,
  isPinnedCurrent,
  canCompareWithPinned,
  comparing,
  onPin,
  onToggleCompare,
  keyCol,
  onKeyColChange,
}: ResultsPaneProps & {
  result: EditorRows;
  n: number;
  filterText: string;
  onFilterText: (text: string) => void;
  chips: FilterChip[];
  onAddChip: (col: number, mode: "show" | "hide", value: string | null) => void;
  onRemoveChip: (id: string) => void;
  onClearFilter: () => void;
  isFiltered: boolean;
  filteredRows: GridRow[];
  pinned: PinnedResult | null;
  isPinnedCurrent: boolean;
  canCompareWithPinned: boolean;
  comparing: boolean;
  onPin: () => void;
  onToggleCompare: () => void;
  keyCol: number;
  onKeyColChange: (col: number) => void;
}) {
  const statement = run?.statements[result.statement];
  const index = rowResults(run).indexOf(result);
  const cut = result.stopped !== null || result.truncated;
  const more = cut && canLoadMore(result) && result.limit < 100_000;
  const filterInputRef = useRef<HTMLInputElement>(null);
  const effectiveKeyCol = keyCol >= 0 && keyCol < result.columns.length ? keyCol : defaultKeyColumn(result.columns);
  // Option values are column indexes, so duplicate column names stay
  // selectable; a repeated name gets its occurrence number ("id (2)").
  const keyColLabels = useMemo(() => {
    const total = new Map<string, number>();
    for (const c of result.columns) total.set(c.name, (total.get(c.name) || 0) + 1);
    const seen = new Map<string, number>();
    return result.columns.map((c) => {
      const n = (seen.get(c.name) || 0) + 1;
      seen.set(c.name, n);
      return (total.get(c.name) || 0) > 1 ? `${c.name} (${n})` : c.name;
    });
  }, [result.columns]);
  const compareDiff = useMemo<CompareDiff | null>(
    () => (comparing && canCompareWithPinned && pinned ? compareResults(pinned, result, effectiveKeyCol) : null),
    [comparing, canCompareWithPinned, pinned, result, effectiveKeyCol],
  );
  if (!result.columns_known)
    return (
      <div className="result-empty">
        <strong>No rows</strong>
        <code title={statement?.text}>{statement?.text.trim().split("\n")[0]}</code>
      </div>
    );
  const metaItems = [
    comparing && compareDiff ? (
      <span key="compare" className="result-compare-summary" role="status">
        {compareDiff.summary}
        {compareDiff.unchanged > 0 ? ` · ${count(compareDiff.unchanged)} unchanged` : ""}
      </span>
    ) : null,
    !comparing && isFiltered ? (
      <span key="filter" className="result-filter-summary" role="status">
        {filterSummary(filteredRows.length, result.rows.length)}
      </span>
    ) : null,
    fromConnection ? <span key="from" className="result-from">From {fromConnection}</span> : null,
    cut ? <span key="cut" className="result-cut">{result.label || `first ${rowsText(result.row_count)}`}</span> : null,
    result.warnings ? <span key="warn">{`${count(result.warnings)} ${result.warnings === 1 ? "warning" : "warnings"}`}</span> : null,
    // Only a cut result has rows the sort cannot see.
    cut && result.row_count > 1 ? (
      <span key="sort" className="result-sortnote" title="Sorting a column reorders the rows loaded here; rows past the limit are not included">
        Sorting applies to loaded rows
      </span>
    ) : null,
  ].filter(Boolean);
  const hasFilterControls = filterText !== "" || chips.length > 0;
  return (
    <div className="result-view">
      <div className="result-bar result-toolbar" role="toolbar" aria-label="Result tools">
        {!comparing && (
          <div className="result-filter-group">
            <label className="toolbar-search result-filter-field">
              <Icon name="search" size={14} />
              <input
                ref={filterInputRef}
                type="search"
                aria-label="Filter loaded rows"
                placeholder="Filter loaded rows…"
                value={filterText}
                spellCheck={false}
                autoCapitalize="off"
                onChange={(e) => onFilterText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.nativeEvent.isComposing) return;
                  if (e.key === "Escape" && (filterText !== "" || chips.length > 0)) {
                    e.preventDefault();
                    e.stopPropagation();
                    onClearFilter();
                  }
                }}
              />
            </label>
            {chips.length > 0 && (
              <div className="result-filter-chips" role="list" aria-label="Active filters">
                {chips.map((chip) => {
                  const valText = chip.value === null ? "NULL" : chip.value === "" ? "empty string" : chip.value;
                  return (
                    <span
                      key={chip.id}
                      role="listitem"
                      className={`result-filter-chip is-${chip.mode}`}
                      data-chip-col={chip.col}
                      data-chip-mode={chip.mode}
                      data-chip-null={chip.value === null ? "true" : undefined}
                    >
                      <span className="result-chip-col">
                        {chip.mode === "hide" ? `not ${chip.colName}` : chip.colName}:
                      </span>
                      {chip.value === null ? (
                        <span className="sql-null">NULL</span>
                      ) : chip.value === "" ? (
                        <span className="result-chip-empty">empty string</span>
                      ) : (
                        <bdi className="result-chip-val">{chip.value}</bdi>
                      )}
                      <button
                        type="button"
                        className="result-chip-remove"
                        aria-label={`Remove filter ${chip.mode === "show" ? "Show only" : "Hide"} ${chip.colName} ${valText}`}
                        title="Remove filter"
                        onClick={() => onRemoveChip(chip.id)}
                      >
                        <Icon name="close" size={12} />
                      </button>
                    </span>
                  );
                })}
              </div>
            )}
            {hasFilterControls && (
              <button
                type="button"
                className="button small result-filter-clear"
                aria-label="Clear filter"
                title="Clear filter (Escape)"
                onClick={() => {
                  onClearFilter();
                  filterInputRef.current?.focus();
                }}
              >
                Clear
              </button>
            )}
          </div>
        )}
        {metaItems.length > 0 && (
          <span className="result-meta result-summary">
            {metaItems.map((item, i) => (
              <span key={i}>
                {i > 0 ? " · " : ""}
                {item}
              </span>
            ))}
          </span>
        )}
        {reloaded[index] && <span className="result-note">{reloaded[index]}</span>}
        <span className="result-actions">
          {comparing && canCompareWithPinned && (
            <label className="toolbar-field result-compare-key">
              <span>Key column</span>
              <select
                className="select"
                aria-label="Key column"
                value={String(effectiveKeyCol)}
                onChange={(e) => onKeyColChange(Number(e.target.value))}
              >
                {result.columns.map((_, j) => (
                  <option key={j} value={String(j)}>
                    {keyColLabels[j]}
                  </option>
                ))}
              </select>
            </label>
          )}
          {pinned && (
            <span
              className="result-pin-badge"
              data-pinned="true"
              title={
                pinned.capped
                  ? `Pinned the first ${count(MAX_PIN_ROWS)} of ${count(pinned.loadedCount)} loaded rows in this tab`
                  : `Pinned ${rowsText(pinned.rows.length)} in this tab`
              }
            >
              Pinned · {count(pinned.rows.length)}
              {pinned.capped ? ` of ${count(pinned.loadedCount)}` : ""} {pinned.rows.length === 1 ? "row" : "rows"}
            </span>
          )}
          <button
            type="button"
            className="button small"
            data-action="pin-result"
            aria-pressed={isPinnedCurrent}
            title="Keep a copy of the loaded rows (at most 5,000) in memory for this tab"
            onClick={onPin}
          >
            <Icon name="baseline" size={14} />
            Pin result
          </button>
          {canCompareWithPinned && (
            <button
              type="button"
              className="button small"
              data-action="compare-pinned"
              aria-pressed={comparing}
              title="Compare this result with the pinned result in this tab"
              onClick={onToggleCompare}
            >
              <Icon name="compare" size={14} />
              Compare with pinned
            </button>
          )}
          {more && (
            <button
              type="button"
              className="button small"
              title="Runs this statement again with a larger row limit; rows may differ from this run"
              onClick={() => onLoadMore(result)}
            >
              Load more
            </button>
          )}
        </span>
      </div>
      {comparing && canCompareWithPinned && compareDiff && pinned ? (
        <ResultCompareView columns={result.columns} diff={compareDiff} pinned={pinned} />
      ) : (
        <ResultGrid
          result={result}
          filteredRows={filteredRows}
          isFiltered={isFiltered}
          onAddChip={onAddChip}
          label={`Result ${n}`}
          onCopied={(text) => onNote(text)}
          rowPanel={rowPanel}
        />
      )}
    </div>
  );
}

function ResultCompareView({
  columns,
  diff,
  pinned,
}: {
  columns: EditorColumn[];
  diff: CompareDiff;
  pinned: PinnedResult;
}) {
  // The shared windowed DataTable: a large pin against a large result mounts
  // only the rows in view, never the whole diff.
  const tableColumns = useMemo<Column<CompareDiffRow>[]>(
    () => [
      {
        key: "status",
        label: "Change",
        name: "Change",
        width: 104,
        minWidth: 88,
        className: "cell-status",
        render: (row) => {
          const statusWord =
            row.status === "added" ? "Added" : row.status === "removed" ? "Removed" : row.status === "changed" ? "Changed" : "Ambiguous";
          const tone: Tone = row.status === "added" ? "ok" : row.status === "removed" ? "problem" : "watch";
          return <StatusPill tone={tone} label={statusWord} />;
        },
      },
      ...columns.map(
        (c, j): Column<CompareDiffRow> => ({
          key: `c${j}`,
          label:
            j === diff.keyCol ? (
              <>
                <span>{c.name}</span> <span className="compare-key-tag">key</span>
              </>
            ) : (
              c.name
            ),
          name: c.name,
          className: j === diff.keyCol ? "cell-value is-key-col" : "cell-value",
          render: (row) => {
            const isChanged = row.status === "changed" && row.changedCols.includes(j);
            if (isChanged) {
              return (
                <span className="compare-cell-diff is-changed-cell" data-changed="true">
                  <span className="compare-cell-before" title="Pinned value">
                    {row.before?.[j] === "" ? (
                      <span className="result-chip-empty">empty string</span>
                    ) : (
                      <Value value={row.before?.[j] ?? null} hex={c.hex} />
                    )}
                  </span>
                  <span className="compare-cell-arrow" aria-hidden="true">→</span>
                  <span className="compare-cell-after" title="Current value">
                    {row.after?.[j] === "" ? (
                      <span className="result-chip-empty">empty string</span>
                    ) : (
                      <Value value={row.after?.[j] ?? null} hex={c.hex} />
                    )}
                  </span>
                </span>
              );
            }
            const val = (row.after ?? row.before)?.[j] ?? null;
            return <Value value={val} hex={c.hex} />;
          },
        }),
      ),
    ],
    [columns, diff.keyCol],
  );
  return (
    <div className="result-compare-view" aria-label="Result comparison">
      {pinned.capped && (
        <p className="result-compare-note hint">
          Comparing against the first {count(MAX_PIN_ROWS)} of {count(pinned.loadedCount)} loaded rows in the pinned result.
        </p>
      )}
      {diff.ambiguous.length > 0 && (
        <p className="result-compare-note hint" role="status">
          Ambiguous key: {rowsText(diff.ambiguous.length)} could not be matched because <bdi>{diff.keyColName}</bdi> has
          duplicate or NULL values. Choose another key column.
        </p>
      )}
      {diff.rows.length === 0 ? (
        <div className="result-empty result-compare-empty">
          <strong>No differences</strong>
          <span>All {rowsText(diff.unchanged)} match the pinned result by {diff.keyColName}.</span>
        </div>
      ) : (
        <DataTable<CompareDiffRow>
          label="Added, removed and changed rows"
          columns={tableColumns}
          rows={diff.rows}
          rowKey={(r) => r.id}
          nativeRowMenu={false}
          className="result-table result-compare-table"
          rowClass={(r) => `compare-diff-row is-${r.status}`}
          rowAttrs={(r) => ({ "data-diff-status": r.status })}
        />
      )}
    </div>
  );
}

function Messages({ run, base, stale, onJump, running, fromConnection }: ResultsPaneProps & { run: EditorRun }) {
  const c = run.counts;
  // Count statement outcomes, never infer successes from planned statements.
  // Stop and a lost connection can leave part of a Continue script unexecuted.
  const continued = run.on_error === "continue" && !running;
  const outcomes = [
    `${count(run.statements.filter((s) => s.status === "ok").length)} succeeded`,
    `${count(run.statements.filter((s) => s.status === "error").length)} failed`,
    ...([
      [["not_run", "pending"], "not run"],
      [["stopped"], "stopped"],
      [["session_ended", "running"], "outcome unknown"],
    ] as const).map(([statuses, label]) => {
        const n = run.statements.filter((s) => statuses.some((status) => s.status === status)).length;
        return n ? `${count(n)} ${label}` : "";
      }).filter(Boolean),
  ].join(", ");
  const deadlock = continued && run.results.some((r) => r.type === "error" && r.code === 1213);
  const lockTimeout = continued && run.results.some((r) => r.type === "error" && r.code === 1205);
  const summary = [
    fromConnection ? `From ${fromConnection}` : "",
    continued
      ? `${count(c.statements)} ${c.statements === 1 ? "statement" : "statements"}: ${outcomes}`
      : `${count(c.statements)} ${c.statements === 1 ? "statement" : "statements"}`,
    !continued && c.run !== c.statements ? `${count(c.run)} run` : "",
    !continued && c.errors ? `${count(c.errors)} ${c.errors === 1 ? "error" : "errors"}` : "",
    // Engine behavior, not a confirmed outcome: a script can SIGNAL either
    // code, and a metadata lock wait also ends with 1205.
    deadlock ? "Error 1213: an InnoDB deadlock rolls back the victim's whole transaction" : "",
    lockTimeout ? "Error 1205: an InnoDB lock wait timeout rolls back the whole transaction only with innodb_rollback_on_timeout ON" : "",
    c.affected_rows ? `${count(c.affected_rows)} rows affected` : "",
    c.warnings ? `${count(c.warnings)} ${c.warnings === 1 ? "warning" : "warnings"}` : "",
    duration(run.duration_ms),
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="messages">
      <p className="messages-summary">
        {running ? "Running: " : ""}
        {summary}
      </p>
      {run.stopped && <p className="messages-note">{run.stopped.message}</p>}
      {run.message && run.state !== "complete" && <p className="messages-note">{run.message}</p>}
      <ol className="message-list">
        {run.statements.map((s) => (
          <StatementMessage
            key={s.index}
            statement={s}
            results={run.results.filter((r) => r.statement === s.index)}
            base={base}
            stale={stale}
            onJump={onJump}
          />
        ))}
      </ol>
    </div>
  );
}
