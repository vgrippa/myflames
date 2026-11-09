// Execute the shipped callback bodies without sockets. This follows
// review-read-hooks.mjs; history-nav.mjs owns React/DOM integration.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";

const source = (file) => ts.createSourceFile(file,
  fs.readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, (node) => find(node, predicate));
}
const main = source("main.tsx"), schema = source("SchemaBrowser.tsx");
const named = (root, name) => find(root, (n) => ts.isFunctionDeclaration(n) && n.name?.text === name);
const variable = (root, name) => find(root, (n) => ts.isVariableDeclaration(n) && n.name.getText() === name);
const effect = (root, text) => find(root, (n) => ts.isCallExpression(n) && n.expression.getText() === "useEffect" && n.arguments[0].getText().includes(text)).arguments[0].getText();
function run(text, context) {
  return vm.runInNewContext(ts.transpileModule(text, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None, jsx: ts.JsxEmit.React,
  } }).outputText, context);
}
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const flush = () => new Promise(setImmediate);

function history() {
  let actualQuery = { tabId: "q1", reveal: null }, requestedQuery;
  const ctx = {
    ready: true, mode: "server", serverViewNow: "health", navKey: "A", navStop: 0,
    activeId: "p1", beforeId: "p1", afterId: "p2", plans: [{ id: "p1" }, { id: "p2" }],
    pendingObjectLink: { current: null }, pendingServerView: { current: null }, observedNav: { current: "" }, traversing: { current: null },
    nextNavStop: { current: 0 }, navHistory: { current: { entries: [], index: -1 } },
    navStateRef: { current: { key: "A", name: "A" } }, connectedRequest: { current: { host: "A" } },
    navEntryLabel: (mode, view) => ({ label: `${mode}:${view || ""}`, icon: "query" }),
    connectionLabel: (c) => c.host, placeOf: (mode) => mode,
    labState: { tab_id: "q1" }, labRef: { current: { navigationSelection: () => actualQuery } },
    setNavStop: (value) => { ctx.navStop = value; }, setNavRevision() {},
    setLinkBack: (value) => { ctx.linkBack = value; }, setNotice: (value) => { ctx.notice = value; }, setAuditFocus() {},
    setMode: (mode) => { ctx.mode = mode; ctx.serverViewNow = null; },
    go: (mode) => { ctx.mode = mode; ctx.serverViewNow = null; },
    setServerNavigation: (update) => { ctx.requestedView = update({ n: 0 }).view; },
    setActiveId: (id) => { ctx.activeId = id; }, setBeforeId: (id) => { ctx.beforeId = id; }, setAfterId: (id) => { ctx.afterId = id; },
    withLab: (callback) => callback({ restoreNavigation(selection) { requestedQuery = plain(selection); return selection; } }),
  };
  for (const name of ["navSelection", "linkOrigin", "finishLink", "applyNavEntry", "navMove"])
    ctx[name] = run(`(${named(main, name).getText()})`, ctx);
  const record = run(`(${effect(main, "h.entries.push")})`, ctx);
  const go = (mode, connection = ctx.navKey, tabId = actualQuery.tabId) => {
    ctx.mode = mode; ctx.serverViewNow = mode === "server" ? "health" : null;
    ctx.navKey = connection; ctx.navStateRef.current = { key: connection, name: connection };
    ctx.connectedRequest.current = { host: connection };
    actualQuery = { tabId, reveal: null }; record();
  };
  const settle = () => {
    if (requestedQuery) { actualQuery = requestedQuery; requestedQuery = null; }
    if (ctx.requestedView) { ctx.serverViewNow = ctx.requestedView; ctx.requestedView = null; }
    record(); record();
  };
  return { ctx, record, go, settle, query: () => actualQuery };
}

test("cross-connection traversal retains Forward after restoration and repeated effects", () => {
  const h = history();
  h.go("server", "A"); h.go("query", "A"); h.go("query", "B"); h.go("server", "B");
  const entries = h.ctx.navHistory.current.entries.slice();
  h.ctx.navMove(-1); h.settle();
  h.ctx.navMove(-1); h.settle();
  assert.match(h.ctx.notice, /was open on A/);
  assert.equal(h.ctx.navKey, "B");
  assert.deepEqual(h.ctx.navHistory.current.entries, entries);
  assert.equal(h.ctx.navHistory.current.index, 1);
  h.ctx.navMove(1); h.settle(); h.ctx.navMove(1); h.settle();
  assert.equal(h.ctx.mode, "server");
  assert.equal(h.ctx.navHistory.current.index, 3);
});

test("same-place cross-links record tab identities and the chip restores its captured origin", () => {
  const h = history();
  h.go("server"); h.go("query", "A", "q1");
  const origin = h.ctx.linkOrigin();
  h.ctx.finishLink(origin); h.go("query", "A", "q2");
  assert.equal(h.ctx.navHistory.current.entries.length, 3);
  assert.equal(h.ctx.navHistory.current.entries[2].selection.query.tabId, "q2");
  h.go("query", "A", "q3"); // the visible chip still refers to q1
  const chip = find(main, (n) => ts.isJsxAttribute(n) && n.name.text === "onClick" && n.getText().includes("applyNavEntry(linkBack"));
  run(`(${chip.initializer.expression.getText()})()`, h.ctx);
  h.record(); // q3 is still active until the child reports q1
  assert.equal(h.ctx.navHistory.current.index, 1);
  assert.equal(h.ctx.navHistory.current.entries.length, 4);
  h.settle();
  assert.equal(h.query().tabId, "q1");
  h.ctx.navMove(1); h.settle();
  assert.equal(h.query().tabId, "q2");
  assert.equal(h.ctx.navHistory.current.entries.length, 4);
});

test("a cross-link without a document change is still a stop and new navigation truncates Forward", () => {
  const h = history(); h.go("query");
  h.ctx.finishLink(h.ctx.linkOrigin()); h.record();
  assert.equal(h.ctx.navHistory.current.entries.length, 2);
  h.ctx.navMove(-1); h.settle(); h.go("server");
  assert.equal(h.ctx.navHistory.current.entries.length, 2);
  assert.equal(h.ctx.navHistory.current.entries[1].mode, "server");
});

test("palette results are hidden during another connection's debounce and late replies cannot win", async () => {
  const tasks = [], pending = [], opened = [];
  let state;
  const ctx = {
    AbortController, paletteSearchRef: { current: { key: "A", text: "race", open: true } },
    connectedRequest: { current: { host: "A" } },
    window: { setTimeout: (fn) => { tasks.push(fn); return tasks.length; }, clearTimeout() {} },
    searchObjects: (connection, text, signal) => { const task = deferred(); pending.push({ ...task, connection, signal }); return task.promise; },
    setObjectSearch: (value) => { state = typeof value === "function" ? value(state) : value; },
    message: (error) => error.message,
    OBJECT_ICONS: { procedure: "query" }, OBJECT_GROUP_LABELS: { procedure: "Procedure" },
    openObject: (...args) => opened.push(args),
  };
  const search = run(`(navKey, paletteQuery, commandPaletteOpen, paletteConnected) => (${effect(main, "searchObjects(")})()`, ctx);
  const commands = run(`(navKey, paletteQuery, objectSearch, objectSearchHere) => {
    const objectCatalog = {catalog: null}, paletteConnected = true;
    return (${variable(main, "objectCommands").initializer.arguments[0].getText()})();
  }`, ctx);
  const start = (key) => {
    ctx.paletteSearchRef.current = { key, text: "race", open: true }; ctx.connectedRequest.current = { host: key };
    const cleanup = search(key, "race", true, true); tasks.shift()(); return cleanup;
  };
  const result = (name) => ({ objects: [{ kind: "procedure", schema: "shop", name }], truncated: false });
  let cleanup = start("A"); pending[0].resolve(result("race_A")); await flush();
  assert.equal(commands("A", "race", state, true).length, 1);
  assert.equal(commands("B", "race", state, false).length, 0, "B render suppresses A before its effect or debounce");
  cleanup(); cleanup = start("A"); // old A remains pending
  cleanup(); cleanup = start("B");
  cleanup(); cleanup = start("A");
  pending[3].resolve(result("race_current")); await flush();
  pending[1].resolve(result("race_old_A")); pending[2].reject(Error("late_B_error")); await flush();
  assert.deepEqual(plain(state.matches), result("race_current").objects);
  assert.equal(state.error, "");
  cleanup();
});

test("palette selection rechecks identity before navigation and after a lazy editor mount", () => {
  const calls = [], waiting = [];
  const ctx = {
    pendingObjectLink: { current: null }, setNavRevision() {},
    navStateRef: { current: { key: "B" } }, linkOrigin: () => ({}), go: () => calls.push("go"),
    withLab: (fn) => waiting.push(fn), finishLink: () => calls.push("chip"), showCreateSql: () => "SHOW CREATE PROCEDURE shop.race",
  };
  const open = run(`(${named(main, "openObject").getText()})`, ctx);
  const object = { kind: "procedure", schema: "shop", name: "race" };
  open(object, "A"); assert.deepEqual(calls, []); assert.equal(waiting.length, 0);
  ctx.navStateRef.current.key = "A"; open(object, "A");
  ctx.navStateRef.current.key = "B";
  waiting.shift()({ openSql: () => calls.push("sql") });
  assert.deepEqual(calls, ["go"], "an editor loaded later cannot open another connection's object");
});

test("expanded groups read the new connection once and discard cancelled success and failure", async () => {
  const pending = [];
  const ctx = {
    enabled: true, visible: true, key: "A", catalog: { catalog: { schemas: [{ name: "shop" }] } },
    open: new Set(["s:shop", "s:shop/routines"]), groups: {}, OBJECT_GROUPS: [["routines"]],
    groupsKey: (key, schema, group) => `${key}/${schema}/${group}`,
    groupRequests: { current: new Map() }, connectionRef: { current: { host: "A" } },
    AbortController, connectionKey: (c) => c.host,
    setGroups: (fn) => { ctx.groups = fn(ctx.groups); },
    api: (_path, body, signal) => { const task = deferred(); pending.push({ ...task, body, signal }); return task.promise; },
  };
  const loader = run(`(key) => (${named(schema, "loadGroup").getText()})`, ctx);
  ctx.loadGroupRef = { current: loader("A") };
  const loadExpanded = run(`(${effect(schema, "loadGroupRef.current")})`, ctx);
  loadExpanded(); loadExpanded(); assert.equal(pending.length, 1);
  ctx.key = "B"; ctx.connectionRef.current = { host: "B" }; ctx.loadGroupRef.current = loader("B");
  loadExpanded(); loadExpanded(); assert.equal(pending.length, 2);
  assert.equal(pending[1].body.connection.host, "B");
  pending[1].resolve({ objects: [{ name: "B_proc" }] });
  pending[0].resolve({ objects: [{ name: "A_proc" }] }); await flush();
  assert.equal(ctx.groups["B/shop/routines"].data.objects[0].name, "B_proc");
  assert.equal(ctx.groups["A/shop/routines"].data, null, "late A data is not accepted even before cleanup");
  // Cancellation also protects A -> B -> A, where identity alone would pass.
  ctx.key = "A"; ctx.connectionRef.current = { host: "A" }; ctx.loadGroupRef.current = loader("A");
  delete ctx.groups["A/shop/routines"]; loadExpanded();
  ctx.groupRequests.current.get("A/shop/routines").abort();
  pending[2].reject(Error("cancelled group")); await flush();
  assert.equal(ctx.groups["A/shop/routines"].error, "");
});

test("reveal targets the full name past page 200, reports absence and ignores another connection", () => {
  const ctx = {
    key: "A", visible: true, enabled: true, revealId: "target", revealDone: { current: "" },
    reveal: { connection: "A", schema: "shop", table: "table_201", n: 1 },
    open: new Set(), filter: "", nodes: [], tables: {},
    catalog: { loading: false, catalog: { schemas: [{ name: "shop" }] } },
    tablesKey: (key, schema, prefix) => `${key}/${schema}/${prefix}`,
    setFilter: (value) => { ctx.filter = value; }, setOpen: (fn) => { ctx.open = fn(ctx.open); },
    setRevealError: (value) => { ctx.error = value; }, moveTo: (id) => { ctx.focus = id; },
  };
  const start = run(`(${effect(schema, "setFilter(reveal.table")})`, ctx);
  const finish = run(`(${effect(schema, "const wanted = reveal.table")})`, ctx);
  start(); assert.equal(ctx.filter, "table_201");
  ctx.tables["A/shop/table_201"] = { loading: false, error: "", data: { tables: [{ name: "table_201" }], truncated: false } };
  ctx.nodes = [{ id: "target-row", kind: "table", schema: "shop", table: "table_201" }];
  finish(); assert.equal(ctx.focus, "target-row");
  ctx.revealId = "missing"; ctx.reveal = { ...ctx.reveal, table: "missing", n: 2 }; ctx.nodes = [];
  start(); ctx.tables["A/shop/missing"] = { loading: false, error: "", data: { tables: [], truncated: false } };
  finish(); assert.match(ctx.error.message, /no longer visible/);
  ctx.key = "B"; ctx.filter = "B_filter"; ctx.revealDone.current = "";
  start(); finish(); assert.equal(ctx.filter, "B_filter");
});


test("editor history restores only existing tab IDs and never reveals another connection's object", () => {
  const editor = source("QueryLab.tsx");
  const property = find(editor, (n) => ts.isPropertyAssignment(n) && n.name.getText() === "restoreNavigation");
  const ctx = {
    tabs: [{ id: "q1" }, { id: "q2" }], tabId: "q1", connection: { host: "B" }, narrow: false,
    connectionKey: (c) => c.host, browserRevealSerial: { current: 0 },
    setTabId: (id) => { ctx.selected = id; },
    setBrowserReveal: (value) => { ctx.reveal = value; },
    setSheet: (value) => { ctx.sheet = value; }, setLayout: (fn) => { ctx.layout = fn({ browser: false }); },
  };
  const restore = run(`(${property.initializer.getText()})`, ctx);
  let restored = restore({ tabId: "q2", reveal: { connection: "A", schema: "shop", table: "orders" } });
  assert.equal(ctx.selected, "q2"); assert.equal(ctx.reveal, null); assert.equal(restored.reveal, null);
  restored = restore({ tabId: "closed", reveal: { connection: "B", schema: "shop", table: "orders" } });
  assert.equal(ctx.selected, "q1", "a closed tab is not recreated or run");
  assert.equal(ctx.reveal.table, "orders"); assert.equal(ctx.layout.browser, true);
  const first = ctx.reveal.n;
  restore({ tabId: "q1", reveal: null });
  restore(restored); assert.equal(ctx.reveal.n, first + 1, "a repeated target after clearing still makes a fresh reveal request");
});

test("a lazy object destination records only its settled tab and connection switches cancel the wait", () => {
  const h = history(); h.go("server");
  const origin = h.ctx.linkOrigin();
  h.ctx.pendingObjectLink.current = { connection: "A" };
  h.go("query", "A", "q1");
  assert.equal(h.ctx.navHistory.current.entries.length, 1, "loading the editor is not a history stop");
  h.ctx.pendingObjectLink.current = null;
  h.ctx.finishLink(origin); h.go("query", "A", "q2");
  assert.equal(h.ctx.navHistory.current.entries.length, 2);
  assert.equal(h.ctx.navHistory.current.entries[1].selection.query.tabId, "q2");
  h.ctx.pendingObjectLink.current = { connection: "A" };
  h.go("query", "B", "q2");
  assert.equal(h.ctx.pendingObjectLink.current, null);
  assert.equal(h.ctx.navHistory.current.entries.at(-1).connection, "B");
});
