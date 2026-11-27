// Code splitting contracts (src/lazy.tsx, docs/DEVELOPER_REFERENCE.md
// "Bundle and lazy loading"). No browser: reads the sources and, when present,
// the committed build in myflames/ui_assets. Run: node test/bundle-split.mjs
// 1. styles.ts lists every stylesheet and main.tsx imports it first, so the
//    cascade order never depends on which chunk loads first.
// 2. The stylesheet checks and source list walk src/ recursively, and only
//    QueryLab.tsx and src/editor/ use value imports into src/editor/.
// 3. The shell does not import a lazily loaded module statically (that would
//    pull it back into the entry chunk).
// 4. The build has one stylesheet and an entry chunk within its budget, keeps
//    CodeMirror out of the entry, and engine words in their own chunk.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.join(here, "../src");
const read = (name) => fs.readFile(path.join(src, name), "utf8");

async function walk(directory, relative = "") {
  const entries = await fs.readdir(directory, { withFileTypes: true });
  const found = [];
  for (const entry of entries) {
    const child = path.posix.join(relative, entry.name);
    if (entry.isDirectory()) found.push(...await walk(path.join(directory, entry.name), child));
    else found.push(child);
  }
  return found;
}

const sourceFiles = await walk(src);
const sources = sourceFiles.filter((f) => /\.(ts|tsx)$/.test(f)).sort();
const stylesheets = sourceFiles.filter((f) => f.endsWith(".css")).sort();
const listed = [...(await read("styles.ts")).matchAll(/^import "\.\/([^"]+\.css)";$/gm)]
  .map((m) => path.posix.normalize(path.posix.join(path.posix.dirname("styles.ts"), m[1])));
assert.deepEqual([...listed].sort(), stylesheets, "styles.ts imports every stylesheet in src/ exactly once");
assert.deepEqual(listed.slice(-2), ["style.css", "themes.css"], "theme colors follow the shared styles");
for (const file of sources) {
  for (const m of (await read(file)).matchAll(/import\s+["']([^"']+\.css)["']/g)) {
    const imported = path.posix.normalize(path.posix.join(path.posix.dirname(file), m[1]));
    assert.ok(listed.includes(imported), `${file} imports ${imported}, which styles.ts does not list`);
  }
}
const main = await read("main.tsx");
const firstImport = main.match(/^import[^;]*;/m)?.[0];
assert.equal(firstImport, 'import "./styles";', "main.tsx imports styles.ts before anything else");
assert.match(main, /^import "\.\/themes";$/m, "the startup theme listener stays eager, including late backend preferences");
let themeListener;
const themeRoot = { dataset: { theme: "dark" } };
const themeCode = ts.transpileModule(await read("themes.ts"), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText;
new Function("require", "exports", "document", themeCode)(() => ({
  subscribeUiPreferences: (listener) => { themeListener = listener; },
}), {}, { documentElement: themeRoot });
assert.equal(typeof themeListener, "function", "eager themes registers its preference listener at startup");
themeListener({ theme: "system" });
assert.equal(themeRoot.dataset.theme, undefined, "the backend's System answer clears a legacy painted theme");
themeListener({ theme: "fire" });
assert.equal(themeRoot.dataset.theme, "fire", "a late backend theme answer paints without loading Settings or the palette");

// CodeMirror's value code is confined to QueryLab and its editor modules.
const editorImports = /^\s*import\s+(type\s+)?([^;]*?)\s+from\s*["']([^"']+)["'];/gm;
const sideEffectImports = /^\s*import\s*["']([^"']+)["'];/gm;
const isValueImport = (typeOnly, clause) => {
  if (typeOnly) return false;
  const named = clause.match(/\{([\s\S]*?)\}/);
  if (!named) return true;
  if (clause.slice(0, named.index).trim()) return true;
  return named[1].split(",").some((item) => item.trim() && !/^type\s+/.test(item.trim()));
};
const resolvesIntoEditor = (file, specifier) =>
  path.posix.normalize(path.posix.join(path.posix.dirname(file), specifier)).startsWith("editor/");
for (const file of sources) {
  const text = await read(file);
  for (const m of text.matchAll(editorImports)) {
    if (resolvesIntoEditor(file, m[3]) && isValueImport(m[1], m[2])) {
      assert.ok(file === "QueryLab.tsx" || file.startsWith("editor/"),
        `${file} value-imports ${m[3]} from src/editor outside QueryLab.tsx and src/editor/`);
    }
  }
  for (const m of text.matchAll(sideEffectImports)) {
    if (resolvesIntoEditor(file, m[1])) {
      assert.ok(file === "QueryLab.tsx" || file.startsWith("editor/"),
        `${file} value-imports ${m[1]} from src/editor outside QueryLab.tsx and src/editor/`);
    }
  }
}

// Inspect every dynamic import, including chart preloads beside PlanScreen.
// Looking only at the first import in a Promise.all loader misses siblings.
const dynamicIn = (text) => [...text.matchAll(/\bimport\s*\(\s*["']\.\/([^"']+)["']\s*\)/g)].map((m) => m[1]);
function assertNoStaticLazyImports(text, file) {
  const dynamic = new Set(dynamicIn(text));
  assert.ok(dynamic.size > 0, `${file} loads parts dynamically`);
  for (const m of text.matchAll(editorImports)) {
    if (m[3].startsWith("./") && isValueImport(m[1], m[2])) {
      assert.ok(!dynamic.has(m[3].slice(2)), `${file} imports ${m[3]} statically and dynamically`);
    }
  }
  for (const m of text.matchAll(sideEffectImports)) {
    assert.ok(!m[1].startsWith("./") || !dynamic.has(m[1].slice(2)),
      `${file} imports ${m[1]} statically and dynamically`);
  }
}
for (const file of ["main.tsx", "Server.tsx", "QueryLab.tsx", "PlanScreen.tsx"])
  assertNoStaticLazyImports(await read(file), file);

// Prove the guard covers all three shell imports, with both value forms,
// while allowing imports that TypeScript erases.
for (const module of ["PlanScreen", "PlanChart", "ContextPanel"]) {
  for (const statement of [`import { ${module} } from "./${module}";`, `import "./${module}";`]) {
    assert.throws(() => assertNoStaticLazyImports(`${main}\n${statement}`, "main.tsx"),
      /statically and dynamically/, `the shell guard rejects a static ${module} import`);
  }
  assertNoStaticLazyImports(`${main}\nimport type { ${module} } from "./${module}";`, "main.tsx");
  assertNoStaticLazyImports(`${main}\nimport { type ${module} } from "./${module}";`, "main.tsx");
}

// Plans and Learn stay behind the same loading boundary as other places.
for (const module of ["PlanScreen", "Teach"]) {
  assert.ok(dynamicIn(main).includes(module), `main.tsx loads ${module} dynamically`);
  assert.match(main, new RegExp(`const ${module} = lazyComponent\\(`),
    `main.tsx loads ${module} through lazyComponent`);
}
// The Import dialog and the skip-loop polling of restored workspaces and other
// members are not first-paint code: both load dynamically (the dialog through
// lazyComponent with a startup preload, the pollers on demand); the shell
// guard above already rejects a static import of either.
for (const module of ["ImportPlanDialog", "loopPolling"]) {
  assert.ok(dynamicIn(main).includes(module), `main.tsx loads ${module} dynamically`);
}
assert.match(main, /const ImportDialog = lazyComponent\(loadImportDialog,/,
  "main.tsx loads the Import dialog through lazyComponent");

// Execute the real shell handlers with a rejected chunk loader. These tests
// cover import failure and abort timing without a server or a browser cache.
const syntax = ts.createSourceFile("main.tsx", main, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function find(node, predicate) {
  if (predicate(node)) return node;
  return ts.forEachChild(node, (child) => find(child, predicate));
}
const effect = (marker) => {
  const call = find(syntax, (node) => ts.isCallExpression(node) && node.expression.getText(syntax) === "useEffect" && node.arguments[0]?.getText(syntax).includes(marker));
  assert.ok(call, `shell effect exists: ${marker}`);
  return call.arguments[0].getText(syntax);
};
const execute = (text, env) => {
  const js = ts.transpileModule(`(${text.replaceAll('import("./loopPolling")', 'loadLoopPolling()')})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.ESNext },
  }).outputText;
  return new Function(...Object.keys(env), `return ${js}`)(...Object.values(env));
};
const settle = () => new Promise((resolve) => setImmediate(resolve));
const cell = (value) => ({ value, set(next) { this.value = typeof next === "function" ? next(this.value) : next; } });
const connection = { profile_id: "p-other" };
const endpoint = "other-endpoint";
const stale = cell([]), loadFailed = cell(false), restored = cell(null), errors = cell({}), stopping = cell(null);
const order = [];
const env = {
  AbortController,
  ready: true, resetKey: 7, profilesLoaded: true, profilesFailed: false,
  restoredOtherMembers: ["p-other"], restoredLoopForCurrent: false,
  restoredMembersPolled: { current: -1 },
  profilesRef: { current: [{ id: "p-other", name: "Other member" }] },
  activeRef: { current: { id: "p-active" } }, seenProfileGroups: { current: new Set() },
  loopMemberRequest: () => connection,
  otherActiveLoopKeys: endpoint,
  endpointKey: (key) => key, connectionKey: () => endpoint, connectionLabel: () => "Other member",
  resolveBoundLoopConnection: () => ({ connection, reason: "" }),
  publishReplicationLoops: () => order.push("publish"), syncLoopSession: () => {},
  setPollerStaleEndpoints: stale.set.bind(stale), setLoopPollingFailed: loadFailed.set.bind(loadFailed),
  setRestoredMembers: restored.set.bind(restored),
  otherLoopStoppingRef: { current: new Set() },
  setOtherLoopErrors: errors.set.bind(errors), setOtherLoopStopping: stopping.set.bind(stopping),
  loadLoopPolling: () => Promise.reject(new Error("Simulated missing polling chunk")),
  stopSkipLoop: async () => { order.push("stop"); },
  readSkipLoops: async () => { order.push("read"); return { loops: [] }; },
};
for (const aborted of [false, true]) {
  stale.value = [];
  loadFailed.value = false;
  const cleanup = execute(effect("m.startOtherLoopPoll"), env)();
  if (aborted) cleanup();
  await settle();
  assert.deepEqual(stale.value, aborted ? [] : [endpoint], "failed polling imports mark affected endpoints stale unless aborted");
  assert.equal(loadFailed.value, !aborted, "a failed import offers visible Reload unless the effect was cancelled");
  cleanup();
}
for (const aborted of [false, true]) {
  restored.value = null;
  loadFailed.value = false;
  env.restoredMembersPolled.current = -1;
  const cleanup = execute(effect("m.pollRestoredMembers"), env)();
  if (aborted) cleanup();
  await settle();
  assert.equal(loadFailed.value, !aborted);
  if (!aborted) {
    assert.deepEqual(restored.value.failedMembers, [{ id: "p-other", name: "Other member" }], "failed restore imports preserve the saved member and expose Status not refreshed");
    assert.equal(restored.value.reset, 7, "a failed import resolves restore pending so later loop changes can sync");
  } else assert.equal(restored.value, null, "an aborted load cannot overwrite a newer restore");
  cleanup();
  assert.equal(env.restoredMembersPolled.current, aborted ? -1 : 7);
}
for (const aborted of [false, true]) {
  loadFailed.value = false;
  const cleanup = execute(effect("m.startRestoreRetry"), { ...env, restoreRetrySpec: "|p-other" })();
  if (aborted) cleanup();
  await settle();
  assert.equal(loadFailed.value, !aborted, "a failed restore-retry import also exposes Reload unless aborted");
  cleanup();
}
const stopNode = find(syntax, (node) => ts.isVariableDeclaration(node) && node.name.getText(syntax) === "stopOtherLoop");
const stopText = stopNode.initializer.arguments[0].getText(syntax);
const stop = execute(stopText, env);
await stop({ key: endpoint }, { job_id: "job" });
assert.deepEqual(order, ["stop", "read", "publish"], "Stop is sent before status and works while the polling chunk is unavailable");
assert.equal(stopping.value, null);
env.stopSkipLoop = async () => { throw new Error("Stop could not be sent"); };
await execute(stopText, env)({ key: endpoint }, { job_id: "job" });
assert.match(errors.value[`${endpoint}:job`], /Could not send Stop/, "a failed Stop is visible on the member's banner");
env.stopSkipLoop = async () => {};
env.readSkipLoops = async () => { throw new Error("Status unavailable"); };
await execute(stopText, env)({ key: endpoint }, { job_id: "job" });
assert.match(errors.value[`${endpoint}:job`], /Stop sent, but status was not refreshed/, "a failed status read does not misreport a successful Stop");

const polling = ts.createSourceFile("loopPolling.ts", await read("loopPolling.ts"), ts.ScriptTarget.Latest, true);
const restoreNode = find(polling, (node) => ts.isFunctionDeclaration(node) && node.name?.text === "pollRestoredMembers");
for (const exists of [false, true]) {
  const calls = [];
  const args = {
    ...env, members: ["p-outside"], profiles: exists ? [{ id: "p-outside", name: "Outside", group: "elsewhere" }] : [],
    activeId: "p-active", seenProfileGroups: new Set(), loopMemberRequest: () => null,
    syncLoopSession: (...values) => calls.push(values),
  };
  const applied = await execute(restoreNode.getText(polling).replace(/^export /, ""), env)(args, new AbortController().signal);
  assert.equal(applied, true, "an empty restore poll is complete and must never re-arm");
  assert.deepEqual(calls, [[exists, exists ? ["p-outside"] : []]]);
}
assert.match(main, /className="notice loop-polling-error" role="alert"/, "polling import failures have a visible shell alert");
assert.match(main, /Status not refreshed: skip-loop polling could not be loaded/);
console.log("Failed polling imports, eager Stop, aborts and empty restores passed.");

// The committed build: one stylesheet, and the entry chunk stays small.
const assets = path.join(here, "../../../myflames/ui_assets");
const html = await fs.readFile(path.join(assets, "index.html"), "utf8").catch(() => "");
if (html) {
  const files = await fs.readdir(path.join(assets, "assets"));
  assert.deepEqual(files.filter((f) => f.endsWith(".css")).length, 1, "one stylesheet: no chunk brings its own");
  const entry = html.match(/<script type="module" crossorigin src="\/assets\/([^"]+\.js)"/)?.[1];
  assert.ok(entry, "index.html loads the entry chunk");
  const size = (await fs.stat(path.join(assets, "assets", entry))).size;
  // The 474,000-byte limit is a ceiling, not a target. Home, the connections
  // manager, startup theme handling and the shell's Stop stay eager; the
  // Import form and loop pollers stay lazy to preserve release headroom.
  // A place or view belongs in a lazy chunk.
  assert.ok(size < 474_000, `entry chunk ${entry} is ${size} bytes; keep places and views out of it (lazy.tsx)`);

  const entryText = await fs.readFile(path.join(assets, "assets", entry), "utf8");
  for (const marker of [".cm-content", ".cm-editor"])
    assert.ok(!entryText.includes(marker), `entry chunk ${entry} includes CodeMirror marker ${marker}`);

  for (const [name, markers] of [
    ["PlanScreen", ["No plan is open", "Comparison measurements"]],
    ["Teach", ["myflames-lesson-focus", "Loading lesson"]],
  ]) {
    const chunk = files.find((f) => f.startsWith(`${name}-`) && f.endsWith(".js"));
    assert.ok(chunk, `build has a ${name} chunk`);
    const chunkText = await fs.readFile(path.join(assets, "assets", chunk), "utf8");
    for (const marker of markers) {
      assert.ok(chunkText.includes(marker), `${chunk} includes ${name} marker ${marker}`);
      assert.ok(!entryText.includes(marker), `entry chunk ${entry} includes ${name} marker ${marker}`);
    }
  }
  console.log(`Entry chunk: ${entry} (${size} bytes); Plans and Learn remain lazy.`);

  const queryLab = files.find((f) => /^QueryLab.*\.js$/.test(f));
  assert.ok(queryLab, "build has a QueryLab chunk");
  const engineWords = files.find((f) => /^engineWords.*\.js$/.test(f));
  assert.ok(engineWords, "build has an engineWords chunk");
  const engineWordsMarker = "mysql-26.10.0";
  const engineWordsText = await fs.readFile(path.join(assets, "assets", engineWords), "utf8");
  assert.ok(engineWordsText.includes(engineWordsMarker), `engineWords chunk ${engineWords} includes generated marker ${engineWordsMarker}`);
  for (const chunk of [entry, queryLab]) {
    const chunkText = chunk === entry ? entryText : await fs.readFile(path.join(assets, "assets", chunk), "utf8");
    assert.ok(!chunkText.includes(engineWordsMarker), `${chunk} includes generated engine words marker ${engineWordsMarker}`);
  }
  console.log(`CodeMirror remains in ${queryLab}; engine words: ${engineWords} (${(await fs.stat(path.join(assets, "assets", engineWords))).size} bytes).`);
}
console.log("Bundle split contracts passed.");
