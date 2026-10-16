// Unit tests for the restore rules in src/session.ts and the connection
// identity in src/binding.ts: legacy Server view names only through own
// entries, query tab ids cut before they are deduplicated, unload risk for
// unconfirmed saves over the keepalive limit, and a connection key that
// never holds the password. No build or browser needed: the modules are
// bundled with esbuild (a Vite dependency) and imported from a data URL.
// Run: node test/session-rules.mjs
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// binding.ts imports React hooks; only its pure functions run here.
const reactStub = {
  name: "react-stub",
  setup(b) {
    b.onResolve({ filter: /^react$/ }, () => ({ path: "react", namespace: "stub" }));
    b.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
      contents: "export const useCallback = (f) => f; export const useEffect = () => {}; export const useRef = (v) => ({ current: v }); export const useState = (v) => [v, () => {}];",
    }));
  },
};
async function bundle(entry, plugins = []) {
  const result = await build({
    entryPoints: [fileURLToPath(new URL(entry, import.meta.url))],
    bundle: true,
    format: "esm",
    platform: "neutral",
    target: "es2020",
    write: false,
    logLevel: "silent",
    plugins: [...plugins, reactStub],
  });
  return import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64"));
}
// session.ts schedules saves with window.setTimeout.
globalThis.window = globalThis;
globalThis.location = { hash: "", pathname: "/", search: "" };
const { serverKind, normalizeLab, sameLab, sessionEdits: contentEdits, SessionSaver, SERVER_KINDS, columnLayouts, TABLE_IDS } = await bundle("../src/session.ts");
const binding = await bundle("../src/binding.ts");
const { connectionKey, connectionLabel } = binding;

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

test("legacy view names come from own entries only", () => {
  assert.equal(serverKind("running"), "sessions");
  assert.equal(serverKind("processlist"), "sessions");
  assert.equal(serverKind("configuration"), "config");
  for (const name of ["constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"])
    assert.equal(serverKind(name), "health", name);
  assert.equal(serverKind(42), "health");
  for (const kind of SERVER_KINDS) assert.equal(serverKind(kind), kind);
  assert.equal(normalizeLab({ slow: { kind: "constructor" } }).slow.kind, "health");
});

test("tab ids are cut to 100 characters before they are deduplicated", () => {
  const stem = "t".repeat(100);
  const lab = normalizeLab({
    tabs: [
      { id: stem + "-first", name: "First", sql: "SELECT 1", parameters: "{}" },
      { id: stem + "-second", name: "Second", sql: "SELECT 2", parameters: "{}" },
      { id: "other", name: "Other", sql: "SELECT 3", parameters: "{}" },
    ],
    tab_id: stem + "-second",
  });
  assert.deepEqual(lab.tabs.map((t) => t.id), [stem, "other"]);
  assert.equal(new Set(lab.tabs.map((t) => t.id)).size, lab.tabs.length, "every restored id is unique");
  assert.deepEqual(lab.tabs.map((t) => t.name), ["First", "Other"]);
  assert.equal(lab.tab_id, stem, "the selected id is cut the same way");
  assert.equal(normalizeLab({ tabs: lab.tabs, tab_id: "missing" }).tab_id, stem);
});

test("table column layouts are validated on restore", () => {
  const raw = {
    "indexes.unused": { widths: { verdict: 99999, table: "wide", index: -5, "bad key!": 100, ok: 200.6 }, hidden: ["table", "table", 7, "x".repeat(65)] },
    "sessions.sessions": { widths: {}, hidden: [] },
    "not.a.table": { widths: { a: 100 } },
    constructor: { hidden: ["table"] },
    __proto__: { hidden: ["x"] },
  };
  const out = columnLayouts(raw);
  assert.deepEqual(Object.keys(out), ["indexes.unused"], "unknown and empty tables are dropped");
  assert.deepEqual(out["indexes.unused"], { widths: { verdict: 1200, index: 48, ok: 201 }, hidden: ["table"] });
  assert.deepEqual(columnLayouts("nope"), {});
  assert.deepEqual(columnLayouts(null), {});
  const lab = normalizeLab({ slow: { columns_v1: raw } });
  assert.deepEqual(lab.slow.columns_v1, out);
  assert.equal("columns_v1" in normalizeLab({ slow: { columns_v1: { "not.a.table": {} } } }).slow, false, "nothing stored, no key");
  for (const id of TABLE_IDS) assert.match(id, /^[a-z]+\.[a-z]+$/);
});

test("unconfirmed changes over the keepalive limit are at risk on unload", () => {
  const saver = new SessionSaver(() => {}, () => {});
  saver.enable("{}");
  assert.equal(saver.atRisk(), false, "nothing pending");
  saver.update('{"small":true}');
  assert.equal(saver.atRisk(), false, "a small change goes out with the keepalive save");
  saver.update(JSON.stringify({ big: "x".repeat(70 * 1024) }));
  assert.equal(saver.atRisk(), true, "a large change may never arrive");
  saver.update(JSON.stringify({ wide: "é".repeat(35 * 1024) }));
  assert.equal(saver.atRisk(), true, "the limit is in bytes, not characters");
  saver.disable("off");
  assert.equal(saver.atRisk(), false, "autosave off: unload protection uses the unsaved-work rule instead");
});

test("the connection key is the identity without the password", () => {
  const base = { name: "Prod", host: "db.prod", port: 3306, user: "app", database: "shop", ssl_mode: "REQUIRED", ssl_ca: "" };
  const key = connectionKey({ ...base, password: "secret-1" });
  assert.equal(connectionKey({ ...base, password: "secret-2" }), key, "the password is not part of it");
  assert.equal(key.includes("secret"), false);
  assert.equal(connectionKey({ ...base, database: "other" }), key, "the default schema is the same server");
  assert.notEqual(connectionKey({ ...base, host: "db.staging" }), key);
  assert.notEqual(connectionKey({ ...base, port: 3307 }), key);
  assert.notEqual(connectionKey({ ...base, user: "admin" }), key);
  assert.notEqual(connectionKey({ ...base, ssl_mode: "DISABLED" }), key);
  assert.notEqual(connectionKey({ ...base, ssl_ca: "/etc/ca.pem" }), key);
  assert.notEqual(connectionKey({ ...base, profile_id: "p1" }), key, "a saved connection is its own identity");
  assert.equal(connectionLabel(base), "Prod (app@db.prod:3306)");
  assert.equal(connectionLabel({ ...base, name: "" }), "app@db.prod:3306");
});

test("comparing lab states never serializes the query text", () => {
  // Every editor change compares the old and new lab state; with large
  // queries that must not serialize the SQL (review gate: ~66 ms per
  // keystroke for 50 MB). Any JSON.stringify that would carry a tab's text fails.
  const big = (n) => `SELECT ${n} FROM t WHERE x IN (${"1,".repeat(450000)}1)`;
  const tabs = Array.from({ length: 5 }, (_, i) => ({ id: `t${i}`, name: `Query ${i}`, sql: big(i), parameters: "{}" }));
  const a = normalizeLab({ tabs, tab_id: "t0" });
  const b = normalizeLab({ ...a, tabs: a.tabs.map((t) => ({ ...t })) });
  const stringify = JSON.stringify;
  JSON.stringify = (...args) => {
    const out = stringify(...args);
    if (out && out.length > 100000) throw new Error("serialized " + out.length + " characters");
    return out;
  };
  try {
    assert.equal(sameLab(a, b), true);
    const content = contentEdits({ investigation: { notes: "small" }, query_lab: a }, "plan".repeat(500000));
    assert.equal(content.tabs[0].sql, a.tabs[0].sql, "autosave status never serializes the plan or query text");
    assert.equal(sameLab(a, { ...b, tabs: b.tabs.map((t, i) => (i === 3 ? { ...t, sql: t.sql + " " } : t)) }), false);
    assert.equal(sameLab(a, { ...b, tabs: b.tabs.map((t, i) => (i === 1 ? { ...t, name: "Renamed" } : t)) }), false);
    assert.equal(sameLab(a, { ...b, tabs: b.tabs.slice(1) }), false);
    assert.equal(sameLab(a, { ...b, tab_id: "t2" }), false);
    assert.equal(sameLab(a, { ...b, connection: { ...b.connection, read_only: true } }), false);
    const target = { key: "k", label: "Prod" };
    const source = { kind: "digest", digest: "a".repeat(32), schema: "shop" };
    assert.equal(sameLab({ ...a, tabs: [{ ...a.tabs[0], source, target }] }, { ...a, tabs: [{ ...a.tabs[0], source }] }), false);
  } finally {
    JSON.stringify = stringify;
  }
});

// Deterministic timers and transport exercise the real saver without a browser.
const apiStub = {
  name: "save-transport",
  setup(b) {
    b.onResolve({ filter: /^\.\/api$/ }, () => ({ path: "api", namespace: "save" }));
    b.onLoad({ filter: /.*/, namespace: "save" }, () => ({ contents:
      "export class ApiError extends Error {} export const api = (...args) => globalThis.__saveRequest(...args);" }));
  },
};
const saveModule = await bundle("../src/session.ts", [apiStub]);
const { sessionEdits, serializePlans, serializeSession } = saveModule;
const originalTimeout = globalThis.setTimeout;
const originalClear = globalThis.clearTimeout;
const clock = new Map();
let nextTimer = 0;
globalThis.setTimeout = (run, ms) => { const id = ++nextTimer; clock.set(id, { run, ms }); return id; };
globalThis.clearTimeout = id => clock.delete(id);
const runDelay = ms => {
  for (const [id, timer] of [...clock]) if (timer.ms === ms) { clock.delete(id); timer.run(); }
};
const state = (mode = "inspect", note = "") => ({
  schema_version: "session-1.0", mode, plans: [], teach: { lesson: "", return_to_plan: false },
  investigation: { id: "", name: "Untitled investigation", notes: note, tags: "", baseline_id: "", dirty: !!note },
  query_lab: normalizeLab({}),
});
const edits = s => sessionEdits(s, JSON.stringify(s.plans));
const baseline = state();
const statuses = [];
const saver = new saveModule.SessionSaver(s => statuses.push(s.kind), () => statuses.push("conflict"));
let resolveSave;
let rejectSave;
globalThis.__saveRequest = () => new Promise((resolve, reject) => { resolveSave = resolve; rejectSave = reject; });
let revision = 0;
async function finish(request) {
  resolveSave({ revision: ++revision, saved_at: "now" });
  await request;
}
try {
  saver.enable(JSON.stringify(baseline));
  assert.equal(saver.isSaved(JSON.stringify(baseline)), true, "restore establishes a persisted snapshot");
  saver.update(JSON.stringify(baseline), edits(baseline));
  statuses.length = 0;
  const navigation = state("audit");
  saver.update(JSON.stringify(navigation), edits(navigation));
  let request = saver.flush();
  runDelay(400);
  assert.ok(statuses.every(s => s === "saved"), "navigation is silent even for a slow request");
  assert.equal(saver.isSaved(JSON.stringify(navigation)), false, "silent status does not confirm persistence");
  await finish(request);
  assert.equal(statuses.at(-1), "saved");
  assert.equal(saver.isSaved(JSON.stringify(navigation)), true, "only the confirmed snapshot is saved");
  passed++;

  const authored = state("audit", "a note");
  statuses.length = 0;
  saver.update(JSON.stringify(authored), edits(authored));
  request = saver.flush();
  assert.deepEqual(statuses, ["pending"], "fast content save never says Saving…");
  await finish(request);
  runDelay(400);
  assert.deepEqual(statuses, ["pending", "saved"], "completed save cancels the progress timer");
  passed++;

  const another = state("audit", "another note");
  statuses.length = 0;
  saver.update(JSON.stringify(another), edits(another));
  request = saver.flush();
  assert.deepEqual(statuses, ["pending"]);
  runDelay(400);
  assert.deepEqual(statuses, ["pending", "saving"], "slow content save announces progress after 400 ms");
  await finish(request);
  assert.equal(statuses.at(-1), "saved");
  passed++;

  // Navigation during a content save cannot hide still-unconfirmed work.
  const last = state("inspect", "last note");
  saver.update(JSON.stringify(last), edits(last));
  request = saver.flush();
  const moved = { ...last, mode: "server" };
  saver.update(JSON.stringify(moved), edits(moved));
  runDelay(400);
  assert.equal(statuses.at(-1), "saving");
  await finish(request);
  assert.equal(statuses.at(-1), "saved", "content is confirmed while only navigation remains pending");
  assert.equal(saver.isSaved(JSON.stringify(last)), true);
  assert.equal(saver.isSaved(JSON.stringify(moved)), false, "an older completion cannot clear newer unsaved content");
  statuses.length = 0;
  request = saver.flush();
  runDelay(400);
  assert.deepEqual(statuses, [], "follow-up navigation contains no new content");
  await finish(request);
  passed++;

  const failed = state("server", "cannot save");
  saver.update(JSON.stringify(failed), edits(failed));
  request = saver.flush();
  rejectSave(Error("disk full"));
  await request;
  runDelay(400);
  assert.equal(statuses.at(-1), "error", "failure remains visible after the grace period");
  assert.equal(saver.isSaved(JSON.stringify(failed)), false, "a failed save cannot clear unload protection");
  passed++;

  saver.update(JSON.stringify(state("server", "pause")), "changed");
  request = saver.flush();
  saver.pause();
  statuses.length = 0;
  runDelay(400);
  assert.deepEqual(statuses, [], "pause cancels delayed progress");
  await finish(request);
  passed++;

  const sample = { id: "p", name: "sample-plan.json", raw: "{}", origin: "sample" };
  assert.equal(JSON.parse(serializePlans([sample]))[0].origin, "sample");
  assert.equal("origin" in JSON.parse(serializePlans([{ ...sample, origin: undefined }]))[0], false);
  const wire = serializeSession({ ...baseline, plans: undefined }, serializePlans([sample]));
  const saved = JSON.parse(wire);
  const restored = new saveModule.SessionSaver(s => statuses.push(s.kind), () => {});
  restored.enable(wire);
  statuses.length = 0;
  const movedSample = { ...saved, mode: "audit" };
  restored.update(JSON.stringify(movedSample), sessionEdits(movedSample, serializePlans([sample])));
  assert.ok(statuses.every(s => s === "saved"), "restored provenance does not count as a content edit");
  restored.pause();
  passed++;
} finally {
  saver.pause();
  globalThis.setTimeout = originalTimeout;
  globalThis.clearTimeout = originalClear;
  delete globalThis.__saveRequest;
}

console.log(`session rules: ${passed} tests passed`);
