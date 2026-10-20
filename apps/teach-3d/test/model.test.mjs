// Checks src/model.ts against the Python model (fixtures/btree-shape.json,
// written by scripts/export-btree-shape.py). Run with `npm test`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as model from "../src/model.ts";

const fixture = JSON.parse(readFileSync(new URL("./fixtures/btree-shape.json", import.meta.url), "utf8"));

test("constants match _cost_model.py", () => {
  assert.equal(model.PAGE_OVERHEAD_BYTES, fixture.constants.page_overhead_bytes);
  assert.equal(model.CHILD_POINTER_BYTES, fixture.constants.child_pointer_bytes);
  assert.equal(model.RECORD_HEADER_BYTES, fixture.constants.record_header_bytes);
  assert.equal(model.CLUSTERED_SYSTEM_BYTES, fixture.constants.clustered_system_bytes);
  assert.equal(model.PK_BYTES, fixture.constants.pk_bytes);
  assert.equal(model.CLUSTERED_LEAF_RESERVE_DIVISOR, fixture.constants.clustered_leaf_reserve_divisor);
  assert.deepEqual([model.RANGE_FIRST, model.RANGE_LAST], fixture.range);
});

test("control options match the fixture grid", () => {
  assert.deepEqual(model.OPTIONS.rows, fixture.options.rows);
  assert.deepEqual(model.OPTIONS.rowBytes, fixture.options.row_bytes);
  assert.deepEqual(model.OPTIONS.emailBytes, fixture.options.email_bytes);
  assert.deepEqual(model.OPTIONS.pageKib, fixture.options.page_kib);
});

test("every case matches the Python model", () => {
  for (const c of fixture.cases) {
    const input = { rows: c.rows, rowBytes: c.row_bytes, emailBytes: c.email_bytes, pageKib: c.page_size / 1024 };
    const got = model.indexShapes(input);
    for (const name of ["clustered", "secondary"]) {
      const want = c[name];
      const shape = got[name];
      const label = `${name} ${JSON.stringify(input)}`;
      assert.deepEqual(shape.levels, want.levels, label);
      assert.equal(shape.fanOut, want.fan_out, label);
      assert.equal(shape.leafCapacity, want.leaf_capacity, label);
      assert.equal(shape.leafEntryBytes, want.leaf_entry_bytes, label);
      assert.equal(shape.leafReserveBytes, want.leaf_reserve_bytes, label);
      assert.equal(shape.pages, want.pages, label);
    }
    const leaves = model.leafRangePages(model.RANGE_FIRST, model.rangeLast(c.rows), got.clustered.leafCapacity);
    assert.equal(leaves, c.range_leaves, `range ${JSON.stringify(input)}`);
  }
});

test("paths land on the page that holds the record", () => {
  const shape = model.indexShapes({ rows: 1000000, rowBytes: 100, emailBytes: 24, pageKib: 16 }).clustered;
  assert.deepEqual(shape.levels, [1, 9, 7752]);
  assert.deepEqual(model.pathTo(shape, 42), [0, 0, 0]);
  const last = model.pathTo(shape, 1000000);
  assert.equal(last[2], shape.levels[2] - 1);
  assert.equal(last[1], shape.levels[1] - 1);
});
