import type { EditorColumn, EditorRows } from "./api";

export const FILTER_DEBOUNCE_MS = 120;
export const MAX_PIN_ROWS = 5000;

export type FilterChip = {
  id: string;
  col: number;
  colName: string;
  mode: "show" | "hide";
  value: string | null;
};

export type PinnedResult = {
  runId: string;
  statement: number;
  resultN: number;
  columns: EditorColumn[];
  rows: (string | null)[][];
  loadedCount: number;
  capped: boolean;
};

export type CompareRowStatus = "added" | "removed" | "changed" | "ambiguous";

export type CompareDiffRow = {
  id: string;
  status: CompareRowStatus;
  keyValue: string | null;
  beforeIndex: number | null;
  afterIndex: number | null;
  before: (string | null)[] | null;
  after: (string | null)[] | null;
  changedCols: number[];
};

export type CompareDiff = {
  keyCol: number;
  keyColName: string;
  added: CompareDiffRow[];
  removed: CompareDiffRow[];
  changed: CompareDiffRow[];
  /** Rows whose key repeats on either side, or is NULL, so no pair can be told apart. */
  ambiguous: CompareDiffRow[];
  rows: CompareDiffRow[];
  unchanged: number;
  summary: string;
};

const count = (n: number) => n.toLocaleString("en-US");

export function chipId(col: number, mode: "show" | "hide", value: string | null): string {
  return `${col}:${mode}:${value === null ? "NULL" : `S:${value}`}`;
}

/** Add a column-scoped filter chip, or flip its mode if the same column and value already have one. */
export function upsertChip(
  chips: FilterChip[],
  col: number,
  colName: string,
  mode: "show" | "hide",
  value: string | null,
): FilterChip[] {
  const id = chipId(col, mode, value);
  const existingIdx = chips.findIndex((c) => c.col === col && c.value === value);
  if (existingIdx >= 0) {
    if (chips[existingIdx].mode === mode) return chips;
    const next = [...chips];
    next[existingIdx] = { id, col, colName, mode, value };
    return next;
  }
  return [...chips, { id, col, colName, mode, value }];
}

/** Whether a single row matches all column-scoped chips and the case-insensitive substring query.
 * SQL NULL (null) and empty string ("") stay distinct: NULL matches only NULL. */
export function matchesFilter(row: (string | null)[], query: string, chips: FilterChip[]): boolean {
  for (let k = 0; k < chips.length; k++) {
    const chip = chips[k];
    const cell = row[chip.col] ?? null;
    const same = cell === chip.value;
    if (chip.mode === "show" && !same) return false;
    if (chip.mode === "hide" && same) return false;
  }
  if (query === "") return true;
  const needle = query.toLowerCase();
  for (let j = 0; j < row.length; j++) {
    const cell = row[j];
    if (cell !== null && cell !== undefined && cell.toLowerCase().includes(needle)) return true;
  }
  return false;
}

/** Filter loaded rows across all columns (case-insensitive substring) and column chips. */
export function filterRows<T>(
  rows: T[],
  getValues: (row: T) => (string | null)[],
  query: string,
  chips: FilterChip[],
): T[] {
  if (query === "" && chips.length === 0) return rows;
  return rows.filter((row) => matchesFilter(getValues(row), query, chips));
}

/** Honest loaded-row filter summary: never implies the whole table on the server. */
export function filterSummary(matchedCount: number, loadedCount: number): string {
  return `${count(matchedCount)} of ${count(loadedCount)} loaded rows`;
}

/** Export menu label when a filter is active. */
export function exportFilterLabel(matchedCount: number): string {
  return `Export ${count(matchedCount)} filtered ${matchedCount === 1 ? "row" : "rows"}`;
}

/** Snapshot up to 5,000 loaded rows of a result in memory for the current tab.
 * Only the capped copy plus scalar identity (run id, statement, column names,
 * loaded row count) is kept: the live result object is never retained. */
export function pinResult(result: EditorRows, runId = "", resultN = 1): PinnedResult {
  const capped = result.rows.length > MAX_PIN_ROWS;
  const sliced = capped ? result.rows.slice(0, MAX_PIN_ROWS) : result.rows;
  return {
    runId,
    statement: result.statement,
    resultN,
    columns: result.columns.map((c) => ({ ...c })),
    rows: sliced.map((r) => [...r]),
    loadedCount: result.rows.length,
    capped,
  };
}

/** Drop the pins whose query tab is no longer open. */
export function pruneClosedTabPins(
  pins: Record<string, PinnedResult>,
  openTabIds: string[],
): Record<string, PinnedResult> {
  const open = new Set(openTabIds);
  let changed = false;
  const next: Record<string, PinnedResult> = {};
  for (const id of Object.keys(pins)) {
    if (open.has(id)) next[id] = pins[id];
    else changed = true;
  }
  return changed ? next : pins;
}

/** Whether two results have the same column names in the same order. */
export function sameColumns(a: EditorColumn[], b: EditorColumn[]): boolean {
  if (a.length === 0 || a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    if (a[i].name !== b[i].name) return false;
  }
  return true;
}

/** Preselect a column named id (exact first, then case-insensitive), else the first column (0). */
export function defaultKeyColumn(columns: EditorColumn[]): number {
  if (columns.length === 0) return -1;
  const exact = columns.findIndex((c) => c.name === "id");
  if (exact >= 0) return exact;
  const lower = columns.findIndex((c) => c.name.toLowerCase() === "id");
  return lower >= 0 ? lower : 0;
}

function keyToken(value: string | null): string {
  return value === null ? "\0NULL" : `S:${value}`;
}

type KeyedRow = { index: number; row: (string | null)[] };

const rowSignature = (row: (string | null)[]) => JSON.stringify(row.map((v) => v ?? null));

/** Compare a pinned baseline result with a later result of the same columns by a key column.
 * A key that repeats on either side is ambiguous: identical rows inside the group pair off
 * as unchanged, the rest are reported as ambiguous. NULL keys never match each other. */
export function compareResults(
  pinned: Pick<PinnedResult, "columns" | "rows">,
  current: Pick<EditorRows, "columns" | "rows">,
  keyCol?: number,
): CompareDiff {
  const ncol = current.columns.length;
  const col = keyCol !== undefined && keyCol >= 0 && keyCol < ncol ? keyCol : Math.max(0, defaultKeyColumn(current.columns));
  const keyColName = current.columns[col]?.name || `column ${col + 1}`;

  const buckets = new Map<string, KeyedRow[]>();
  const pinnedCounts = new Map<string, number>();
  const pinnedNull: KeyedRow[] = [];
  for (let i = 0; i < pinned.rows.length; i++) {
    const row = pinned.rows[i];
    const kv = row[col] ?? null;
    if (kv === null) {
      pinnedNull.push({ index: i, row });
      continue;
    }
    const token = keyToken(kv);
    pinnedCounts.set(token, (pinnedCounts.get(token) || 0) + 1);
    const list = buckets.get(token);
    if (list) list.push({ index: i, row });
    else buckets.set(token, [{ index: i, row }]);
  }
  const currentCounts = new Map<string, number>();
  for (let i = 0; i < current.rows.length; i++) {
    const kv = current.rows[i][col] ?? null;
    if (kv === null) continue;
    const token = keyToken(kv);
    currentCounts.set(token, (currentCounts.get(token) || 0) + 1);
  }
  const isAmbiguousKey = (token: string) => (pinnedCounts.get(token) || 0) > 1 || (currentCounts.get(token) || 0) > 1;

  const added: CompareDiffRow[] = [];
  const removed: CompareDiffRow[] = [];
  const changed: CompareDiffRow[] = [];
  const ambiguous: CompareDiffRow[] = [];
  let unchanged = 0;
  const groups = new Map<string, { before: KeyedRow[]; after: KeyedRow[] }>();
  const groupFor = (token: string) => {
    let g = groups.get(token);
    if (!g) {
      g = { before: [], after: [] };
      groups.set(token, g);
    }
    return g;
  };

  for (let i = 0; i < current.rows.length; i++) {
    const currRow = current.rows[i];
    const kv = currRow[col] ?? null;
    if (kv === null) {
      ambiguous.push({
        id: `amb-a${i}`,
        status: "ambiguous",
        keyValue: null,
        beforeIndex: null,
        afterIndex: i,
        before: null,
        after: currRow,
        changedCols: [],
      });
      continue;
    }
    const token = keyToken(kv);
    if (isAmbiguousKey(token)) {
      groupFor(token).after.push({ index: i, row: currRow });
      continue;
    }
    const list = buckets.get(token);
    const match = list && list.length > 0 ? list.shift()! : null;
    if (!match) {
      added.push({
        id: `add-${i}`,
        status: "added",
        keyValue: kv,
        beforeIndex: null,
        afterIndex: i,
        before: null,
        after: currRow,
        changedCols: [],
      });
      continue;
    }
    const changedCols: number[] = [];
    for (let j = 0; j < ncol; j++) {
      if ((match.row[j] ?? null) !== (currRow[j] ?? null)) {
        changedCols.push(j);
      }
    }
    if (changedCols.length > 0) {
      changed.push({
        id: `chg-${match.index}-${i}`,
        status: "changed",
        keyValue: kv,
        beforeIndex: match.index,
        afterIndex: i,
        before: match.row,
        after: currRow,
        changedCols,
      });
    } else {
      unchanged++;
    }
  }

  const remaining: KeyedRow[] = [];
  buckets.forEach((list, token) => {
    if (!list.length) return;
    if (isAmbiguousKey(token)) groupFor(token).before.push(...list);
    else remaining.push(...list);
  });
  remaining.sort((a, b) => a.index - b.index);
  for (let k = 0; k < remaining.length; k++) {
    const item = remaining[k];
    const kv = item.row[col] ?? null;
    removed.push({
      id: `rem-${item.index}`,
      status: "removed",
      keyValue: kv,
      beforeIndex: item.index,
      afterIndex: null,
      before: item.row,
      after: null,
      changedCols: [],
    });
  }

  // Within one repeated key, identical rows still pair off; the rest cannot be
  // told apart and are reported as ambiguous instead of as false changes.
  groups.forEach((g) => {
    const bySignature = new Map<string, KeyedRow[]>();
    for (const a of g.after) {
      const sig = rowSignature(a.row);
      const list = bySignature.get(sig);
      if (list) list.push(a);
      else bySignature.set(sig, [a]);
    }
    const leftoverBefore: KeyedRow[] = [];
    for (const b of g.before) {
      const list = bySignature.get(rowSignature(b.row));
      if (list && list.length > 0) {
        list.shift();
        unchanged++;
      } else {
        leftoverBefore.push(b);
      }
    }
    const leftoverAfter: KeyedRow[] = [];
    bySignature.forEach((list) => leftoverAfter.push(...list));
    for (const b of leftoverBefore) {
      ambiguous.push({
        id: `amb-b${b.index}`,
        status: "ambiguous",
        keyValue: b.row[col] ?? null,
        beforeIndex: b.index,
        afterIndex: null,
        before: b.row,
        after: null,
        changedCols: [],
      });
    }
    for (const a of leftoverAfter) {
      ambiguous.push({
        id: `amb-a${a.index}`,
        status: "ambiguous",
        keyValue: a.row[col] ?? null,
        beforeIndex: null,
        afterIndex: a.index,
        before: null,
        after: a.row,
        changedCols: [],
      });
    }
  });
  for (const b of pinnedNull) {
    ambiguous.push({
      id: `amb-b${b.index}`,
      status: "ambiguous",
      keyValue: null,
      beforeIndex: b.index,
      afterIndex: null,
      before: b.row,
      after: null,
      changedCols: [],
    });
  }
  ambiguous.sort((a, b) => (a.beforeIndex ?? a.afterIndex ?? 0) - (b.beforeIndex ?? b.afterIndex ?? 0));

  const rows: CompareDiffRow[] = [...changed, ...added, ...removed, ...ambiguous];
  let summary = `${count(added.length)} added · ${count(removed.length)} removed · ${count(changed.length)} changed`;
  if (ambiguous.length > 0) summary += ` · ${count(ambiguous.length)} ambiguous`;

  return {
    keyCol: col,
    keyColName,
    added,
    removed,
    changed,
    ambiguous,
    rows,
    unchanged,
    summary,
  };
}
