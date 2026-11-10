// Pure parsing and real export handlers, no browser or socket needed.
// Run: node apps/workspace/test/sql-select-unit.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/sqlSelection.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "sqlSelection.ts", { loader: "ts", format: "esm", target: "es2020" });
const { selectColumnReferences, insertDefaults } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
let passed = 0;
async function test(name, body) {
  try { await body(); passed++; }
  catch (error) { console.error(`FAIL ${name}`); throw error; }
}

for (const [sql, columns, expected] of [
  ["SELECT id, qty FROM t", ["id", "qty"], ["id", "qty"]],
  ["SELECT t.id AS k FROM t", ["k"], ["id"]],
  ["SELECT `id` FROM `s`.`t`", ["id"], ["id"]],
  ["SELECT * FROM t", ["id", "qty"], ["id", "qty"]],
  ["SELECT t.* FROM t", ["id", "qty"], ["id", "qty"]],
  ["SELECT LPAD(id, 5, '0') AS id, qty FROM t", ["id", "qty"], [null, "qty"]],
  ["SELECT id + 0 FROM t", ["id + 0"], [null]],
  ["SELECT DISTINCT id FROM t", ["id"], ["id"]],
  ["SELECT 'a,b' AS x, id FROM t", ["x", "id"], [null, "id"]],
  ["SELECT /* , */ id FROM t", ["id"], ["id"]],
  ["SELECT a FROM t UNION SELECT b FROM u", ["a"], null],
  ["SELECT COUNT(*) FROM t", ["COUNT(*)"], [null]],
  ["SELECT `s`.`t`.`ID` `key`, qty AS quantity FROM s.t", ["key", "quantity"], ["ID", "qty"]],
  ["SELECT `odd,``name` FROM t", ["odd,`name"], ["odd,`name"]],
  ["SELECT id # ,\n, qty -- ,\nFROM t", ["id", "qty"], ["id", "qty"]],
  ["SELECT CONCAT('a,''b,c', \"x,y\"), EXTRACT(YEAR FROM created), qty FROM t", ["x", "y", "qty"], [null, null, "qty"]],
  ["SELECT NULL AS id, TRUE AS qty, 0x123 AS hex, 1 AS n, @id AS x, CURRENT_TIMESTAMP AS d FROM t", ["id", "qty", "hex", "n", "x", "d"], [null, null, null, null, null, null]],
  ["SELECT CASE WHEN id > 0 THEN id END AS id, id COLLATE utf8mb4_bin AS x, CAST(id AS CHAR) AS y FROM t", ["id", "x", "y"], [null, null, null]],
  ["SELECT id, qty FROM t", ["id"], null],
  ["SELECT *, id FROM t", ["id", "qty", "id"], null],
  ["SELECT t.*, id FROM t", ["id", "id"], null],
  ["SELECT t.* FROM t", [], []],
  ["SELECT id FROM t WHERE id = 1; SELECT qty FROM t", ["id"], null],
  ["SELECT id FROM t JOIN u ON t.id = u.id", ["id"], null],
  ["SELECT id FROM t, u", ["id"], null],
  ["SELECT (SELECT id FROM u) FROM t", ["id"], null],
  ["SELECT id FROM t WHERE id IN (SELECT id FROM u)", ["id"], null],
  ["WITH t AS (SELECT id FROM u) SELECT id FROM t", ["id"], null],
  ["SELECT id", ["id"], null],
  ["TABLE t", ["id"], null],
  ["SELECT /*!80000 DISTINCT */ id FROM t", ["id"], null],
  ["SELECT id AS FROM t", ["id"], [null]],
  ["SELECT id, FROM t", ["id"], null],
  ["SELECT (id FROM t", ["id"], null],
]) await test(sql, () => assert.deepEqual(selectColumnReferences(sql, columns), expected));
for (const modifier of ["ALL", "DISTINCT", "DISTINCTROW", "HIGH_PRIORITY", "STRAIGHT_JOIN", "SQL_SMALL_RESULT", "SQL_BIG_RESULT", "SQL_BUFFER_RESULT", "SQL_NO_CACHE", "SQL_CALC_FOUND_ROWS"]) {
  await test(modifier, () => assert.deepEqual(selectColumnReferences(`SELECT ${modifier} id FROM t`, ["id"]), ["id"]));
}
await test("session backslash mode controls strings", () => {
  const sql = "SELECT 'a\\' AS x, id FROM t";
  assert.deepEqual(selectColumnReferences(sql, ["x", "id"], false), [null, "id"]);
  assert.equal(selectColumnReferences(sql, ["x", "id"], true), null);
});
await test("bounded parser", () => assert.equal(selectColumnReferences("SELECT " + " ".repeat(65536) + "id FROM t", ["id"]), null));

// Execute the actual nested handlers, like editor-files-unit.mjs. This checks
// the async request/type wiring without opening a local HTTP server.
function find(node, predicate) {
  if (predicate(node)) return node;
  return ts.forEachChild(node, child => find(child, predicate));
}
async function handler(file, name, env, parent) {
  const text = await fs.readFile(new URL(`../src/${file}`, import.meta.url), "utf8");
  const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const named = n => ts.isFunctionDeclaration(n) && n.name?.text === name;
  const scope = parent ? find(tree, n => ts.isFunctionDeclaration(n) && n.name?.text === parent) : tree;
  const node = find(scope, named);
  assert.ok(node, name);
  const js = ts.transpileModule(node.getText(tree), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText;
  return new Function(...Object.keys(env), `${js}\nreturn ${name};`)(...Object.values(env));
}
const sourceConnection = { host: "source", user: "app", password: "source-only" };
await test("result binding retains its own connection and escape mode after session changes", async () => {
  const now = { sessionKey: "source", sessionConnection: sourceConnection,
    session: { connection_id: 42, backslash_escapes: false } };
  const bound = await handler("QueryLab.tsx", "bound", {
    runtimeRef: { current: { source: now } }, sessionName: () => "Source",
  });
  const binding = bound("source");
  now.sessionConnection = { host: "new-session" };
  now.session.backslash_escapes = true;
  assert.equal(binding.runConnection, sourceConnection);
  assert.equal(binding.runBackslash, false);
});
function fixture(sql = "SELECT id, qty FROM shop.users", types = [null, null]) {
  const result = { statement: 0, columns: types.map((type, i) => ({ name: ["id", "qty"][i], type, hex: false })), rows: [["1", "7"]] };
  const rt = { run: { statements: [{ index: 0, text: sql }] }, runConnection: sourceConnection, runBackslash: true };
  const calls = [];
  const catalog = { schema: "shop", table: "users", columns: [{ name: "ID", type: "int" }, { name: "qty", type: "int" }] };
  const api = async (path, body) => { calls.push({ path, body }); return catalog; };
  return { result, rt, calls, catalog, env: { rt, selectColumnReferences, insertDefaults, api } };
}
await test("declared types fill gaps by referenced name, once on the source connection", async () => {
  const h = fixture("SELECT ID AS id, qty FROM shop.users");
  const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
  assert.deepEqual(await read(h.result), ["int", "int"]);
  assert.deepEqual(h.calls, [{ path: "live/catalog", body: { connection: sourceConnection, schema: "shop", table: "users" } }]);
  assert.deepEqual(h.result.columns.map(c => c.type), [null, null], "export does not mutate the original result");
});
await test("LPAD alias is untyped; server type wins over the table", async () => {
  const h = fixture("SELECT LPAD(id, 5, '0') AS id, qty FROM shop.users", [null, "decimal(10,2)"]);
  const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
  assert.deepEqual(await read(h.result), [null, "decimal(10,2)"]);
  assert.equal(h.calls.length, 1);
});
await test("alias maps by position to its source, not its output name", async () => {
  const h = fixture("SELECT qty AS id, id AS qty FROM shop.users");
  h.catalog.columns[0].type = "bigint";
  const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
  assert.deepEqual(await read(h.result), ["int", "bigint"]);
});
await test("a USE in the same run resolves an unqualified table's schema", async () => {
  const h = fixture("SELECT id, qty FROM users");
  h.result.statement = 1;
  h.rt.run.statements = [{ index: 0, text: "USE shop", status: "ok" },
    { index: 1, text: "SELECT id, qty FROM users" }];
  const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
  assert.deepEqual(await read(h.result), ["int", "int"]);
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].body.schema, "shop");
});
await test("missing columns stay untyped without blocking known references", async () => {
  const h = fixture();
  h.catalog.columns = [{ name: "ID", type: "int" }];
  const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
  assert.deepEqual(await read(h.result), ["int", null]);
});
for (const sql of ["SELECT id, qty FROM t", "SELECT id, qty FROM shop.users UNION SELECT id, qty FROM shop.other", "SELECT COUNT(*), MAX(qty) FROM shop.users"]) {
  await test(`no catalog for ${sql}`, async () => {
    const h = fixture(sql);
    const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
    assert.deepEqual(await read(h.result), [null, null]);
    assert.equal(h.calls.length, 0);
  });
}
for (const reason of ["missing connection", "server types known", "read failed", "table missing", "wrong table"]) {
  await test(reason, async () => {
    const h = fixture();
    if (reason === "missing connection") h.rt.runConnection = null;
    if (reason === "server types known") h.result.columns.forEach(c => { c.type = "bigint"; });
    if (reason === "read failed") h.env.api = async () => { throw Error("permission denied"); };
    if (reason === "table missing") h.catalog.columns = [];
    if (reason === "wrong table") h.catalog.table = "other";
    const read = await handler("QueryLab.tsx", "columnTypesFor", h.env);
    assert.deepEqual(await read(h.result), h.result.columns.map(c => c.type));
    if (["missing connection", "server types known"].includes(reason)) assert.equal(h.calls.length, 0);
  });
}
for (const format of ["csv", "json", "markdown", "tsv", "sql"]) {
  await test(`export format ${format} gates type lookup`, async () => {
    const h = fixture();
    let reads = 0;
    const exports = [];
    const run = await handler("ResultsPane.tsx", "run", {
      result: h.result, busy: "", setBusy: () => {}, columnTypes: async () => { reads++; return ["int", "int"]; },
      binary: [], engine: "mysql", table: "copy", schema: "archive", guard: false,
      api: async (_, body) => { exports.push(body); return { content: "", filename: "x", mime: "text/plain" }; },
      onDone: () => {}, copyText: async () => true, download: () => {}, closePopovers: () => {}, n: 1, isFiltered: false, rowsText: String,
    }, "ExportMenu");
    await run(format);
    assert.equal(reads, format === "sql" ? 1 : 0);
    assert.equal(exports.length, 1);
    assert.deepEqual(exports[0].result.columns.map(c => c.type), format === "sql" ? ["int", "int"] : [null, null]);
  });
}
console.log(`SQL SELECT/export: ${passed} tests passed.`);
