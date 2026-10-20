// Code splitting contracts (src/lazy.tsx, docs/DEVELOPER_REFERENCE.md
// "Bundle and lazy loading"). No browser: reads the sources and, when present,
// the committed build in myflames/ui_assets. Run: node test/bundle-split.mjs
// 1. styles.ts lists every stylesheet and main.tsx imports it first, so the
//    cascade order never depends on which chunk loads first.
// 2. The shell does not import a lazily loaded module statically (that would
//    pull it back into the entry chunk).
// 3. The build has one stylesheet and an entry chunk within its budget.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.join(here, "../src");
const read = (name) => fs.readFile(path.join(src, name), "utf8");

const sources = (await fs.readdir(src)).filter((f) => /\.(ts|tsx)$/.test(f));
const stylesheets = (await fs.readdir(src)).filter((f) => f.endsWith(".css")).sort();
const listed = [...(await read("styles.ts")).matchAll(/^import "\.\/([^"]+\.css)";$/gm)].map((m) => m[1]);
assert.deepEqual([...listed].sort(), stylesheets, "styles.ts imports every stylesheet in src/ exactly once");
assert.equal(listed[listed.length - 1], "style.css", "style.css comes last");
for (const file of sources) {
  for (const m of (await read(file)).matchAll(/import\s+"\.\/([^"]+\.css)"/g))
    assert.ok(listed.includes(m[1]), `${file} imports ${m[1]}, which styles.ts does not list`);
}
const main = await read("main.tsx");
const firstImport = main.match(/^import[^;]*;/m)?.[0];
assert.equal(firstImport, 'import "./styles";', "main.tsx imports styles.ts before anything else");

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
  // About 365 KB after the split (1.1 MB before). Home, the sidebar and the
  // shell only: a place or view belongs in a lazy chunk.
  assert.ok(size < 450_000, `entry chunk ${entry} is ${size} bytes; keep places and views out of it (lazy.tsx)`);
}
console.log("Bundle split contracts passed.");
