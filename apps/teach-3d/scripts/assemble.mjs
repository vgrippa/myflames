// Inlines dist/teach3d.js and src/style.css into src/page.html and writes
// myflames/assets/teach3d-btree.html: one file that works offline, from
// file://, and inside the workspace's sandboxed srcdoc iframe (CSP
// default-src 'none'; script-src 'unsafe-inline'). The page records a sha256
// of its sources; test/test_teach.py recomputes it to catch a stale build.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL("..", import.meta.url));
const OUT = join(here, "../../myflames/assets/teach3d-btree.html");

function files(relative) {
  return readdirSync(join(here, relative), { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith("."))
    .flatMap((entry) => {
      const path = relative + "/" + entry.name;
      return entry.isDirectory() ? files(path) : [path];
    });
}

/** sha256 over sorted "path\0content\0" records, CRLF normalized to LF. */
export function sourcesSha() {
  const hash = createHash("sha256");
  const paths = [...files("src"), "package.json", "vite.config.ts", "scripts/assemble.mjs"].sort();
  for (const path of paths) {
    const text = readFileSync(join(here, path), "utf8").replace(/\r\n/g, "\n");
    hash.update(path, "utf8").update("\0").update(text, "utf8").update("\0");
  }
  return hash.digest("hex");
}

const three = JSON.parse(readFileSync(join(here, "node_modules/three/package.json"), "utf8")).version;
const license = readFileSync(join(here, "node_modules/three/LICENSE"), "utf8").split("\n")[2].trim();
const js =
  `/*! three.js ${three} | MIT License | ${license} | https://github.com/mrdoob/three.js/blob/dev/LICENSE */\n` +
  readFileSync(join(here, "dist/teach3d.js"), "utf8");
const css = readFileSync(join(here, "src/style.css"), "utf8");
for (const [name, text] of [["script", js], ["style", css]]) {
  const lowered = text.toLowerCase();
  if (lowered.includes("</script") || lowered.includes("</style") || lowered.includes("<!--")) {
    throw new Error(`the ${name} cannot be inlined: it contains a closing tag or comment opener`);
  }
}
const page = readFileSync(join(here, "src/page.html"), "utf8")
  .replace("__SOURCES_SHA__", () => sourcesSha())
  .replace("/*__CSS__*/", () => css)
  .replace("/*__JS__*/", () => js);
writeFileSync(OUT, page);
console.log(`wrote myflames/assets/teach3d-btree.html (${(page.length / 1024).toFixed(0)} KiB)`);
