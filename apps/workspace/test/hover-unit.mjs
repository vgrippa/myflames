// Real EditorState, hover source and CatalogService. Only api and a minimal
// document.createElement are fake. No browser, network or database is started.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
const bundled = await build({
  stdin: { contents: `
    export { EditorState } from "@codemirror/state";
    export { hoverSource, hoverExtension } from "./src/editor/hover";
    export { catalogFor } from "./src/editor/catalog";
    export { sameConnection } from "./src/editor/types";
  `, resolveDir: fileURLToPath(new URL("..", import.meta.url)) },
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
  plugins: [{ name: "fake-api", setup(b) {
    b.onResolve({ filter: /^(\.\/|\.\.\/)api$/ }, args => ({ path: args.path, namespace: "fake-api" }));
    b.onLoad({ filter: /.*/, namespace: "fake-api" }, () => ({ contents: `
      export class ApiError extends Error {}
      export const api = (path, body, signal) => globalThis.__hoverApi(path, body, signal);
    ` }));
  } }],
});
const cm = await import("data:text/javascript;base64," + Buffer.from(bundled.outputFiles[0].text).toString("base64"));
const classes = new Set(["sql-hover", "sql-hover-heading", "sql-hover-type", "sql-hover-note", "sql-hover-row", "sql-hover-name"]);
// textContent creates text nodes. Markup, arbitrary classes and attribute/style
// APIs fail loudly if the hover callback ever crosses the C1/C2/C3 boundary.
class Element {
  nodeType = 1;
  childNodes = [];
  constructor(tag) { assert.ok(["div", "bdi"].includes(tag)); this.tagName = tag.toUpperCase(); }
  set className(value) { assert.ok(classes.has(value), `fixed app class: ${value}`); this.class = value; }
  get className() { return this.class; }
  set textContent(value) { this.childNodes = value === "" ? [] : [{ nodeType: 3, data: String(value) }]; }
  get textContent() { return this.childNodes.map(n => n.nodeType === 3 ? n.data : n.textContent).join(""); }
  appendChild(child) { this.childNodes.push(child); return child; }
  set innerHTML(_) { assert.fail("innerHTML forbidden"); }
  set outerHTML(_) { assert.fail("outerHTML forbidden"); }
  setAttribute() { assert.fail("no attributes needed"); }
  insertAdjacentHTML() { assert.fail("HTML insertion forbidden"); }
  get style() { assert.fail("no server text in CSS"); }
}
const previousDocument = globalThis.document;
globalThis.document = { createElement: tag => new Element(tag) };
const descendants = dom => dom.childNodes.flatMap(n => n.nodeType === 3 ? [] : [n, ...descendants(n)]);
const texts = dom => dom.childNodes.flatMap(n => n.nodeType === 3 ? [n.data] : texts(n));
const byClass = (dom, name) => descendants(dom).filter(n => n.className === name);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const services = new Set(), allCalls = [];
let calls;
globalThis.__hoverApi = (path, body, signal) => new Promise((resolve, reject) => {
  assert.equal(path, "live/catalog");
  assert.equal(typeof body.table, "string", "hover never reads tables lists or schemas");
  assert.equal("stats" in body, false, "hover never requests statistics");
  const call = { body, signal, resolve, reject };
  calls.push(call); allCalls.push(call);
});
const column = (name, type = "int", key = "PRI", nullable = false) =>
  ({ name, type, key, nullable, default: null, extra: "", generated: false });
const detail = (call, columns = [column("id"), column("email", "varchar(255)", "UNI", true)]) => ({
  schema_version: "catalog-1.0", level: "table", schema: call.body.schema, table: call.body.table, columns, indexes: [],
});
function harness(sql, options = {}) {
  calls = [];
  const env = options.env || { current: { connection: { host: "db", user: "reader", database: "app" }, docId: "tab-1", backslashEscapes: true } };
  if ("database" in options) env.current.connection.database = options.database;
  const catalog = cm.catalogFor(env); services.add(catalog);
  const view = { state: cm.EditorState.create({ doc: sql }), plugin: () => null, closed: 0 };
  const source = options.source || cm.hoverSource(env, v => { v.closed++; });
  const openAt = (pos, side = 1) => {
    const tip = source(view, pos, side);
    return tip ? { tip, ...tip.create(view) } : null;
  };
  const open = (name, start = 0) => {
    const pos = sql.indexOf(name, start); assert.ok(pos >= 0, name);
    return openAt(pos);
  };
  return { env, catalog, view, source, open, openAt };
}
async function answer(call, columns) { call.resolve(detail(call, columns)); await tick(); }
async function ready(h, name, columns) {
  const tip = h.open(name); assert.ok(tip, name); assert.equal(tip.dom.textContent, "Reading…");
  assert.ok(calls.length, "targeted read starts");
  for (const call of calls) call.resolve(detail(call, columns));
  await tick(); return tip;
}
try {
  {
    const h = harness("SELECT * FROM USERS"), peek = h.catalog.peek.bind(h.catalog);
    h.catalog.peek = (kind, params) => kind === "tables"
      ? { failed: false, value: { tables: [{ name: "users", rows: 321 }] } } : peek(kind, params);
    const tip = await ready(h, "USERS");
    assert.ok(tip.dom.textContent.includes("Estimated rows: 321"), "cached table estimates ignore identifier case");
    tip.destroy();
  }
  for (const sql of [
    "WITH summary AS (SELECT 1 AS val) SELECT summary.val FROM summary",
    "WITH summary AS (SELECT 1 AS val) SELECT * FROM (SELECT summary.val FROM summary) t",
    "WITH RECURSIVE summary AS (SELECT summary.val FROM summary) SELECT * FROM summary",
  ]) {
    for (const name of ["summary.val", "val FROM summary", "summary)"]) {
      const pos = sql.indexOf(name);
      if (pos < 0) continue;
      const h = harness(sql);
      assert.equal(h.openAt(pos), null, "CTEs and their columns cannot borrow physical metadata");
      assert.equal(calls.length, 0, sql);
    }
  }
  // Fake catalog counts service reads, including calls that would hit a cache.
  // Every candidate is peeked before the first sequential read is started.
  for (const count of [3, 4, 5]) {
    const names = ["orders", "customers", "order_items", "products", "suppliers"].slice(0, count);
    for (const cached of [[], [names[1]], names]) {
      const h = harness(`SELECT id FROM ${names.join(" JOIN ")}`), events = [], pending = [];
      let active = 0, maximum = 0, reads = 0;
      const result = table => ({ failed: false, value: { schema: "app", table,
        columns: [column(table === "orders" ? "id" : "other")], indexes: [] } });
      h.catalog.peek = (kind, { table }) => {
        if (kind === "tables") return null;
        assert.equal(kind, "columns"); events.push(`peek:${table}`);
        return cached.includes(table) ? result(table) : null;
      };
      h.catalog.read = (kind, { table }) => {
        assert.equal(kind, "columns"); events.push(`read:${table}`); reads++;
        maximum = Math.max(maximum, ++active);
        return new Promise(resolve => pending.push(() => { active--; resolve(result(table)); }));
      };
      const tip = h.open("id");
      await tick();
      if (count >= 4) {
        assert.equal(tip, null, "a capped table list cannot resolve an unqualified column");
        assert.deepEqual(events, [], "a capped table list cannot peek or read column metadata");
        assert.equal(reads, 0);
        continue;
      }
      assert.deepEqual(events.slice(0, count), names.map(table => `peek:${table}`));
      while (pending.length) { pending.shift()(); await tick(); }
      assert.ok(maximum <= 1, "at most one catalog read in flight");
      assert.equal(reads, names.filter(table => !cached.includes(table)).length);
      assert.ok(tip.dom.textContent.includes("app.orders.id"), "only the unique column match is shown");
      tip.destroy();
    }
  }
  {
    const h = harness("SELECT id FROM orders JOIN customers JOIN order_items"), tip = h.open("id");
    assert.equal(calls.length, 1);
    h.view.state = h.view.state.update({ changes: { from: 0, insert: " " } }).state;
    await answer(calls[0]);
    assert.equal(calls.length, 1, "an invalidated hover cannot start the next sequential read");
    assert.equal(tip.dom.textContent, ""); tip.destroy();
  }
  for (const failure of [{ failed: true, value: null }, { failed: false, value: null }]) {
    const h = harness("SELECT id FROM orders JOIN customers"), read = h.catalog.read.bind(h.catalog);
    h.catalog.peek = (kind, { table }) => kind === "columns" && table === "customers" ? failure : null;
    h.catalog.read = (...args) => { assert.equal(args[1].table, "orders"); return read(...args); };
    const tip = h.open("id"); await answer(calls[0]);
    assert.equal(tip.dom.textContent, "", "failed or missing cached metadata prevents a partial answer");
    assert.equal(h.view.closed, 1); tip.destroy();
  }
  for (const [sql, hover, expected, table] of [
    ["SELECT id FROM users", "users", "app.users", true],
    ["SELECT a.id FROM users AS a", "a.id", "app.users", true],
    ["SELECT a.id FROM users AS a", "id", "app.users.id", false],
    ["SELECT id FROM users", "id", "app.users.id", false],
    ["SELECT id FROM users AS a", "a", "app.users", true],
    ["UPDATE users SET email = ''", "users", "app.users", true],
    ["INSERT INTO users VALUES (1)", "users", "app.users", true],
    ["INSERT INTO users (id, email) VALUES (1, '')", "users", "app.users", true],
    ["INSERT INTO users (id, email) VALUES (1, '')", "email", "app.users.email", false],
    ["SELECT b.email FROM users a JOIN contacts b ON a.id=b.id", "email", "app.contacts.email", false],
    ["SELECT sales.users.id FROM sales.users", "id", "sales.users.id", false],
    ["SELECT a.id FROM users a JOIN contacts JOIN orders JOIN products JOIN suppliers", "id", "app.users.id", false],
    ["SELECT a.id FROM users a JOIN contacts JOIN orders JOIN products JOIN suppliers", "users", "app.users", true],
  ]) {
    const h = harness(sql), tip = await ready(h, hover);
    assert.equal(byClass(tip.dom, "sql-hover-heading")[0].textContent, expected);
    assert.equal(byClass(tip.dom, "sql-hover-row").length, table ? 2 : 0);
    assert.ok(texts(tip.dom).includes(table ? "UNI  NULL" : expected.endsWith("email") ? "UNI  NULL" : "PRI  NOT NULL"));
    assert.ok(!tip.dom.textContent.includes("Estimated rows")); tip.destroy();
  }
  {
    const h = harness("SELECT id FROM sales.users", { database: "" });
    const tip = await ready(h, "users"); assert.equal(calls[0].body.schema, "sales"); tip.destroy();
    const bare = harness("SELECT id FROM users", { database: "" });
    assert.equal(bare.open("users"), null); assert.equal(calls.length, 0);
  }
  for (const [sql, name] of [
    ["SELECT 'id' FROM users", "id"], ['SELECT "id" FROM users', "id"],
    ["SELECT /* id */ id FROM users", "id"], ["SELECT /*! id */ id FROM users", "id"],
    ["SELECT id FROM users -- id", "-- id"], ["SELECT id FROM users # id", "# id"],
    ["SELECT 'open id FROM users", "id"], ["SELECT @id FROM users", "@id"],
    ["SELECT :id FROM users", ":id"], ["SELECT COUNT(id) FROM users", "COUNT"],
    ["SELECT `open FROM users", "open"],
  ]) {
    const h = harness(sql);
    const pos = name.startsWith("--") || name.startsWith("#") ? sql.lastIndexOf("id") : sql.indexOf(name);
    assert.equal(h.openAt(pos), null, sql); assert.equal(calls.length, 0, sql);
  }
  for (const sql of ["SELECT missing FROM users", "SELECT id FROM users a JOIN contacts b ON a.id=b.id"]) {
    const h = harness(sql), tip = h.open(sql.includes("missing") ? "missing" : "id");
    assert.ok(tip); for (const call of calls) { call.resolve(detail(call)); await tick(); }
    assert.equal(tip.dom.textContent, ""); assert.equal(h.view.closed, 1); tip.destroy();
  }
  {
    const h = harness("SELECT missing"); assert.equal(h.open("missing"), null); assert.equal(calls.length, 0);
  }
  {
    const h = harness("SELECT a.id, b.email FROM users a JOIN contacts b ON a.id=b.id");
    const first = h.open("id"), before = calls[0], second = h.open("email"), after = calls[1];
    assert.equal(before.signal.aborted, true); first.destroy();
    assert.equal(after.signal.aborted, false, "old tooltip destruction cannot cancel new owner");
    await answer(before); assert.equal(first.dom.textContent, "");
    await answer(after); assert.ok(second.dom.textContent.includes("app.contacts.email")); second.destroy();
  }
  {
    const h = harness("SELECT id FROM users"), second = harness("SELECT id FROM users", { env: h.env, source: h.source });
    const a = h.open("id"), callA = calls[0], b = second.open("id"), callB = calls[1];
    a.destroy(); assert.equal(callA.signal.aborted, true); assert.equal(callB.signal.aborted, false);
    await answer(callB); assert.ok(b.dom.textContent.includes("app.users.id")); b.destroy();
  }
  // Fake transport ignores AbortSignal, proving stale answers cannot paint.
  for (const change of [
    (h, tip) => tip.destroy(),
    h => { h.view.state = h.view.state.update({ changes: { from: 0, insert: " " } }).state; },
    h => { h.env.current = { ...h.env.current, docId: "tab-2" }; },
    h => { h.env.current.connection = { ...h.env.current.connection, host: "elsewhere" }; },
    h => { h.env.current.connection = { ...h.env.current.connection, password: "changed" }; },
    h => { h.env.current = { ...h.env.current, backslashEscapes: false }; },
  ]) {
    const h = harness("SELECT id FROM users"), tip = h.open("id"), call = calls[0];
    change(h, tip); tip.update({ state: h.view.state }); assert.equal(call.signal.aborted, true);
    await answer(call); assert.equal(tip.dom.textContent, ""); tip.destroy();
  }
  {
    const h = harness("SELECT id FROM users"), tip = h.open("id");
    calls[0].reject(new Error("permission denied")); await tick();
    assert.equal(tip.dom.textContent, ""); assert.equal(h.view.closed, 1); tip.destroy();
  }
  {
    const h = harness("SELECT * FROM users"), columns = Array.from({ length: 15 }, (_, i) => column(`col_${i}`));
    const tip = await ready(h, "users", columns);
    assert.deepEqual(byClass(tip.dom, "sql-hover-name").map(n => n.textContent), columns.slice(0, 12).map(c => c.name));
    assert.ok(texts(tip.dom).includes("and 3 more")); tip.destroy();
  }
  // Integration seam for the separate catalog lane's cache-only accessor.
  for (const rows of [1234, 0, null, undefined, NaN, -1]) {
    const h = harness("SELECT * FROM users"); let peeks = 0;
    h.catalog.peek = (kind, params) => {
      if (kind === "columns") return null;
      assert.equal(kind, "tables"); assert.deepEqual(params, { schema: "app" }); peeks++;
      return { failed: false, value: { tables: [{ name: "users", rows }] } };
    };
    const tip = await ready(h, "users");
    assert.equal(tip.dom.textContent.includes("Estimated rows:"), typeof rows === "number" && Number.isFinite(rows) && rows >= 0);
    assert.equal(peeks, 1); assert.equal(calls.length, 1); tip.destroy();
  }
  const hostile = ['<img src=x onerror=1>', '"><svg onload=1>', 'a`b', '</script>', '${x}'];
  const quote = name => "`" + name.replace(/`/g, "``") + "`";
  for (const name of hostile) {
    const type = `enum('${name}')`, sql = `SELECT a.${quote(name)} FROM app.${quote(name)} AS a`;
    const h = harness(sql), tip = await ready(h, quote(name), [column(name, type)]);
    assert.ok(texts(tip.dom).includes(`app.${name}.${name}`)); assert.ok(texts(tip.dom).includes(type));
    assert.ok(descendants(tip.dom).every(n => ["DIV", "BDI"].includes(n.tagName))); tip.destroy();
    const tableTip = h.open(quote(name), sql.indexOf("FROM")); await tick();
    assert.ok(texts(tableTip.dom).includes(name), "raw column name is a text node");
    assert.ok(texts(tableTip.dom).includes(type)); tableTip.destroy();
  }
  {
    const h = harness("SELECT id FROM first_table; SELECT id FROM second_table"), tip = h.open("id", 28);
    assert.equal(calls.length, 1); assert.equal(calls[0].body.table, "second_table"); tip.destroy();
    const edge = harness("SELECT id FROM users");
    assert.ok(edge.openAt(edge.view.state.doc.length, -1));
    assert.equal(edge.openAt(edge.view.state.doc.length, 1), null);
    const large = harness("SELECT id FROM users" + " ".repeat(60001));
    assert.equal(large.open("id"), null); assert.equal(calls.length, 0);
    assert.ok(cm.hoverExtension({ hover_info: true }, edge.env).active, "real hoverTooltip extension");
  }
  assert.ok(allCalls.every(c => typeof c.body.table === "string" && !("stats" in c.body)));
  {
    // The parent rebuilds the connection object on every render; comparing it
    // by identity reconfigured every compartment mid-hover, destroying the
    // hover plugin before its 350 ms delay elapsed (editor-abuse step 5).
    const connection = { name: "n", host: "db", port: 3306, user: "reader", database: "app", ssl_mode: "preferred" };
    assert.ok(cm.sameConnection(connection, { ...connection }), "identical content, fresh object");
    assert.ok(cm.sameConnection(connection, connection));
    assert.ok(!cm.sameConnection(connection, { ...connection, database: "other" }));
    assert.ok(!cm.sameConnection(connection, { ...connection, profile_id: "p1" }));
    assert.ok(!cm.sameConnection({ ...connection, profile_id: "p1" }, connection));
    assert.ok(!cm.sameConnection(connection, undefined));
    assert.ok(!cm.sameConnection(undefined, connection));
    assert.ok(cm.sameConnection(undefined, undefined));
  }
  console.log("Hover: table/alias/column, exclusions, ambiguity, cancellation, 12-column cap, cache-only estimates and hostile text nodes passed (minimal DOM stub).");
} finally {
  for (const service of services) service.dispose();
  if (previousDocument === undefined) delete globalThis.document; else globalThis.document = previousDocument;
  delete globalThis.__hoverApi;
}
