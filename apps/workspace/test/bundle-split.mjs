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
assert.equal(listed[listed.length - 1], "style.css", "style.css comes last");
for (const file of sources) {
  for (const m of (await read(file)).matchAll(/import\s+["']([^"']+\.css)["']/g)) {
    const imported = path.posix.normalize(path.posix.join(path.posix.dirname(file), m[1]));
    assert.ok(listed.includes(imported), `${file} imports ${imported}, which styles.ts does not list`);
  }
}
const main = await read("main.tsx");
const firstImport = main.match(/^import[^;]*;/m)?.[0];
assert.equal(firstImport, 'import "./styles";', "main.tsx imports styles.ts before anything else");

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

// Modules loaded through lazyComponent must not be imported statically by the
// shell or by Server (type-only imports are erased and fine).
const lazyIn = (text) => [...text.matchAll(/lazyComponent\(\(\) => import\("\.\/([^"]+)"\)/g)].map((m) => m[1]);
for (const file of ["main.tsx", "Server.tsx", "QueryLab.tsx"]) {
  const text = await read(file);
  const lazy = lazyIn(text);
  assert.ok(lazy.length > 0, `${file} loads parts lazily`);
  for (const module of lazy) {
    const statics = [...text.matchAll(new RegExp(`^import\\s+(?!type\\b)([^;]*?)\\s+from\\s+"\\./${module}";`, "gms"))]
      .filter((m) => !/^\{\s*(type\s+\w+\s*,?\s*)+\}$/.test(m[1]));
    assert.equal(statics.length, 0, `${file} imports ./${module} statically and lazily`);
  }
}

// The committed build: one stylesheet, and the entry chunk stays small.
const assets = path.join(here, "../../../myflames/ui_assets");
const html = await fs.readFile(path.join(assets, "index.html"), "utf8").catch(() => "");
if (html) {
  const files = await fs.readdir(path.join(assets, "assets"));
  assert.deepEqual(files.filter((f) => f.endsWith(".css")).length, 1, "one stylesheet: no chunk brings its own");
  const entry = html.match(/<script type="module" crossorigin src="\/assets\/([^"]+\.js)"/)?.[1];
  assert.ok(entry, "index.html loads the entry chunk");
  const size = (await fs.stat(path.join(assets, "assets", entry))).size;
  // About 365 KB after the split (1.1 MB before), 442 KB by 3.3.2 and 475 KB
  // in 3.3.3, where the shell gained Back and Forward history, Cmd+K object
  // search and recents, and readiness facts on Home. Home, the sidebar and
  // the shell only: a place or view belongs in a lazy chunk. Next step to win
  // room back: move main.tsx's plan screen into a lazy chunk.
  assert.ok(size < 500_000, `entry chunk ${entry} is ${size} bytes; keep places and views out of it (lazy.tsx)`);

  const entryText = await fs.readFile(path.join(assets, "assets", entry), "utf8");
  for (const marker of [".cm-content", ".cm-editor"])
    assert.ok(!entryText.includes(marker), `entry chunk ${entry} includes CodeMirror marker ${marker}`);

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
