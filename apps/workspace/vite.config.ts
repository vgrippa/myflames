import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { THEME_IDS } from "./src/themes";
import { defineConfig } from "vite";

const repo = fileURLToPath(new URL("../..", import.meta.url));

function files(relative: string, root: string): string[] {
  return readdirSync(join(root, relative), { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith("."))
    .flatMap((entry) => {
      const path = relative + "/" + entry.name;
      return entry.isDirectory() ? files(path, root) : [path];
    });
}

/** Same byte-based path\0content\0 algorithm as test/test_workspace_assets.py. */
export function sourcesSha(root: string = repo): string {
  const hash = createHash("sha256");
  const paths = [...files("apps/workspace/src", root),
    "apps/workspace/index.html", "apps/workspace/vite.config.ts",
    "apps/workspace/tsconfig.json", "apps/workspace/package-lock.json",
    "docs/brand/myflames-app-logo.png"];
  // UTF-8 byte order agrees with Python even for supplementary Unicode names.
  paths.sort((a, b) => Buffer.compare(Buffer.from(a, "utf8"), Buffer.from(b, "utf8")));
  for (const path of paths) {
    const bytes = readFileSync(join(root, path));
    hash.update(path, "utf8").update("\0");
    // Drop only CR bytes immediately before LF; never decode binary inputs.
    let start = 0;
    for (let i = 0; i + 1 < bytes.length; i++) {
      if (bytes[i] === 13 && bytes[i + 1] === 10) {
        hash.update(bytes.subarray(start, i));
        start = i + 1;
      }
    }
    hash.update(bytes.subarray(start)).update("\0");
  }
  return hash.digest("hex");
}

export function validateThemeIds(ids: readonly string[]): void {
  if (ids.some(id => !/^[a-z]+(-[a-z]+)*$/.test(id))) {
    throw new Error("Theme ids must contain lowercase words separated by hyphens.");
  }
}
export default defineConfig({
  base: "/",
  plugins: [{ name: "myflames-theme-ids", transformIndexHtml(html) {
    validateThemeIds(THEME_IDS);
    return {
      html: html.replace("__MYFLAMES_THEME_IDS__", () => JSON.stringify(THEME_IDS)),
      tags: [{ tag: "meta", attrs: {
        name: "myflames-sources-sha256", content: sourcesSha(),
      }, injectTo: "head" }],
    };
  } }],
  build: {
    outDir: "../../myflames/ui_assets",
    emptyOutDir: true,
    target: "es2020",
    rollupOptions: {
      output: {
        // Keep the logo address valid for tabs left open during a rebuild.
        assetFileNames: (asset) =>
          asset.name === "myflames-app-logo.png"
            ? "assets/myflames-logo[extname]"
            : "assets/[name]-[hash][extname]",
      },
    },
  },
});
