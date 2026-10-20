// The B+tree numbers, mirrored from myflames/teach/_cost_model.py
// (innodb_fanout, innodb_leaf_capacity, innodb_btree_shape,
// innodb_index_shapes, leaf_range_pages). Python is the source of truth:
// scripts/export-btree-shape.py writes test/fixtures/btree-shape.json and
// test/model.test.mjs checks every case against these functions.
// Keep this file free of imports so node can load it directly in the test.

export const PAGE_OVERHEAD_BYTES = 120;
export const CHILD_POINTER_BYTES = 9;
export const RECORD_HEADER_BYTES = 5;
export const CLUSTERED_SYSTEM_BYTES = 13;
export const PK_BYTES = 8;
/** Clustered leaves filled in key order keep page size / 16 free for updates. */
export const CLUSTERED_LEAF_RESERVE_DIVISOR = 16;

/** The values the controls offer; the fixture covers every combination. */
export const OPTIONS = {
  rows: [100, 1000, 10000, 100000, 1000000, 10000000, 100000000, 1000000000],
  rowBytes: [50, 100, 200, 500, 1000],
  emailBytes: [16, 24, 32, 64],
  pageKib: [4, 8, 16, 32, 64],
};

export const DEFAULTS = { rows: 1000000, rowBytes: 100, emailBytes: 24, pageKib: 16 };

/** The range scan chapter reads id BETWEEN 42 AND 4000, capped at the row count. */
export const RANGE_FIRST = 42;
export const RANGE_LAST = 4000;
/** The row both lookups find. */
export const TARGET_ID = 42;

export interface Inputs {
  rows: number;
  rowBytes: number;
  emailBytes: number;
  pageKib: number;
}

export interface Shape {
  /** Page count of every level, root first, leaf last. */
  levels: number[];
  fanOut: number;
  leafCapacity: number;
  leafEntryBytes: number;
  leafReserveBytes: number;
  pages: number;
}

export function fanout(keyBytes: number, pageSize: number): number {
  return Math.max(2, Math.floor((pageSize - PAGE_OVERHEAD_BYTES) / (keyBytes + CHILD_POINTER_BYTES)));
}

export function leafCapacity(entryBytes: number, pageSize: number, reserveBytes = 0): number {
  return Math.max(1, Math.floor((pageSize - PAGE_OVERHEAD_BYTES - reserveBytes) / entryBytes));
}

export function btreeShape(
  rows: number,
  leafEntryBytes: number,
  nodeKeyBytes: number,
  pageSize: number,
  leafReserveBytes = 0,
): number[] {
  const capacity = leafCapacity(leafEntryBytes, pageSize, leafReserveBytes);
  const fan = fanout(nodeKeyBytes, pageSize);
  const levels = [Math.max(1, Math.ceil(rows / capacity))];
  while (levels[levels.length - 1] > 1) levels.push(Math.ceil(levels[levels.length - 1] / fan));
  return levels.reverse();
}

function shapeOf(rows: number, leafBytes: number, nodeKey: number, pageSize: number, reserve: number): Shape {
  const levels = btreeShape(rows, leafBytes, nodeKey, pageSize, reserve);
  return {
    levels,
    fanOut: fanout(nodeKey, pageSize),
    leafCapacity: leafCapacity(leafBytes, pageSize, reserve),
    leafEntryBytes: leafBytes,
    leafReserveBytes: reserve,
    pages: levels.reduce((a, b) => a + b, 0),
  };
}

/** users (clustered on id BIGINT) and idx_users_email, as in _cost_model.innodb_index_shapes. */
export function indexShapes(input: Inputs): { clustered: Shape; secondary: Shape } {
  const pageSize = input.pageKib * 1024;
  const clusteredLeaf = input.rowBytes + RECORD_HEADER_BYTES + CLUSTERED_SYSTEM_BYTES;
  const secondaryLeaf = input.emailBytes + PK_BYTES + RECORD_HEADER_BYTES;
  const reserve = Math.floor(pageSize / CLUSTERED_LEAF_RESERVE_DIVISOR);
  return {
    clustered: shapeOf(input.rows, clusteredLeaf, PK_BYTES, pageSize, reserve),
    secondary: shapeOf(input.rows, secondaryLeaf, input.emailBytes + PK_BYTES, pageSize, 0),
  };
}

/** Leaf pages the primary key range first..last spans (ids 1..N packed in order). */
export function leafRangePages(first: number, last: number, capacity: number): number {
  return Math.floor((last - 1) / capacity) - Math.floor((first - 1) / capacity) + 1;
}

export function rangeLast(rows: number): number {
  return Math.min(RANGE_LAST, rows);
}

/** Records under one page of each level when every page is full (leaf = capacity). */
export function spans(shape: Shape): number[] {
  const out: number[] = [];
  let span = shape.leafCapacity;
  for (let level = shape.levels.length - 1; level >= 0; level--) {
    out[level] = span;
    span *= level === 0 ? 1 : shape.fanOut;
  }
  return out;
}

/** Page index at each level on the path to record number `ordinal` (1-based). */
export function pathTo(shape: Shape, ordinal: number): number[] {
  return spans(shape).map((span) => Math.floor((ordinal - 1) / span));
}

/** Where alice@example.com sits in A to Z email order: near the start. */
export function aliceOrdinal(rows: number): number {
  return Math.max(1, Math.min(rows, Math.ceil(rows * 0.03)));
}
