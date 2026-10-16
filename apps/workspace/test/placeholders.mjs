// Unit tests for src/placeholders.ts: digest text to named parameters.
// No build or browser needed: the module is compiled with Vite's esbuild
// transform and imported from a data URL. Run: node test/placeholders.mjs
// The shared cases in fixtures/digest-placeholders.json also run in
// test/test_digest_capture.py against myflames/discover.py.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(
  new URL("../src/placeholders.ts", import.meta.url),
  "utf8",
);
const { code } = await transformWithEsbuild(source, "placeholders.ts", {
  loader: "ts",
  format: "esm",
  target: "es2020",
});
const {
  parameterize,
  joinFunctionCalls,
  skeleton,
  digestQuery,
  shortDigest,
  firstTable,
  digestName,
  fillNotice,
} = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);
const shared = JSON.parse(
  await fs.readFile(
    new URL("./fixtures/digest-placeholders.json", import.meta.url),
    "utf8",
  ),
);

let passed = 0;
function test(name, body) {
  try {
    body();
    passed++;
  } catch (error) {
    console.error("FAIL " + name);
    throw error;
  }
}

for (const c of shared.parameterize)
  test(`shared parameterize: ${c.name}`, () => {
    assert.deepEqual(parameterize(c.text), {
      sql: c.sql,
      names: c.names,
      lists: c.lists,
    });
  });

for (const c of shared.digest_query)
  test(`shared digestQuery: ${c.name}`, () => {
    const q = digestQuery({ digest: "a".repeat(64), schema: "shop", ...c.row });
    assert.equal(q.sql, c.sql);
    assert.deepEqual(q.names, c.names);
    assert.equal(q.fromSample, c.from_sample);
    assert.equal(q.reason, c.reason);
  });

for (const c of shared.join_function_calls)
  test(`shared joinFunctionCalls: ${c.name}`, () => {
    assert.equal(joinFunctionCalls(c.text), c.sql);
  });

test("numbering restarts per call and is stable", () => {
  const a = parameterize("SELECT ? , ?");
  const b = parameterize("SELECT ? , ?");
  assert.deepEqual(a, b);
  assert.deepEqual(a.names, ["p1", "p2"]);
});

test("converted SQL keeps every quoted span byte for byte", () => {
  const text =
    "SELECT \"a\"\"?\" , 'b\\\\' , ? FROM `t` /* multi\nline ? */ WHERE c = ?";
  const { sql, names } = parameterize(text);
  assert.equal(
    sql,
    "SELECT \"a\"\"?\" , 'b\\\\' , :p1 FROM `t` /* multi\nline ? */ WHERE c = :p2",
  );
  assert.deepEqual(names, ["p1", "p2"]);
});

test("an unterminated comment hides the rest", () => {
  assert.deepEqual(parameterize("SELECT ? /* ? ?").names, ["p1"]);
});

test("skeleton is valid JSON with null values in order", () => {
  assert.equal(skeleton([]), "{}");
  const text = skeleton(["p1", "p2", "p3"]);
  assert.deepEqual(Object.keys(JSON.parse(text)), ["p1", "p2", "p3"]);
  assert.ok(Object.values(JSON.parse(text)).every((v) => v === null));
  assert.match(text, /^\{\n {2}"p1": null,\n/);
});

test("short digest is the first 12 characters", () => {
  assert.equal(shortDigest("028d37443170c0c809af33862240c71d"), "028d37443170");
  assert.equal(shortDigest(null), "");
});

test("first table after FROM, with or without schema and backticks", () => {
  assert.equal(firstTable("SELECT * FROM `orders` WHERE a = ?"), "orders");
  assert.equal(firstTable("SELECT COUNT ( * ) FROM `shop` . `orders`"), "orders");
  assert.equal(firstTable("select * from shop.order_items o"), "order_items");
  assert.equal(firstTable("SELECT `a``b` FROM `we``ird`"), "we`ird");
  assert.equal(firstTable("SELECT 'FROM nope' , x FROM real_t"), "real_t");
  assert.equal(firstTable("SELECT /* FROM nope */ 1 FROM t2"), "t2");
  assert.equal(firstTable("SELECT * FROM ( SELECT 1 ) d"), "");
  assert.equal(firstTable("SELECT 1"), "");
});

test("tab names come from the rank and the first table, else the schema", () => {
  const row = (text, schema = "shop") => ({
    digest: "a".repeat(64),
    digest_text: text,
    schema,
    sample_text: null,
    sample_truncated: false,
  });
  assert.equal(digestName(3, row("SELECT * FROM `orders`")), "Top #3 · orders");
  assert.equal(digestName(1, row("SELECT ?")), "Top #1 · shop");
  assert.equal(digestName(2, row("SELECT ?", null)), "Top #2");
});

test("fill notice names the parameters and the collapsed lists", () => {
  assert.equal(fillNotice({ names: [], lists: [], reason: null }), "");
  const one = fillNotice({ names: ["p1"], lists: [], reason: "normalized" });
  assert.match(one, /only the normalized text/);
  assert.match(one, /Fill in p1 under Parameters/);
  const many = fillNotice({
    names: ["p1", "p2", "p3"],
    lists: ["p2"],
    reason: "truncated",
  });
  assert.match(many, /cut off/);
  assert.match(many, /p1 to p3/);
  assert.match(many, /p2 replaces a collapsed list/);
  assert.match(
    fillNotice({ names: ["p1", "p2"], lists: [], reason: "placeholders" }),
    /p1 and p2/,
  );
});

console.log(`placeholders: ${passed} tests passed`);
