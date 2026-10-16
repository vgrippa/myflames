// Builds the chart runtime (src/charts) into ../../myflames/assets as one
// IIFE, myflames-charts.js, exposing the global MyflamesCharts. The banner
// records a sha256 of the sources; test/test_chart_data.py recomputes it
// (same algorithm) to catch a stale committed bundle without Node.
import { createHash } from "node:crypto";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const here = fileURLToPath(new URL(".", import.meta.url));

function files(relative: string): string[] {
  return readdirSync(join(here, relative), { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith("."))
    .flatMap((entry) => {
      const path = relative + "/" + entry.name;
      return entry.isDirectory() ? files(path) : [path];
    });
}

/** sha256 over sorted "path\0content\0" records, CRLF normalized to LF. */
export function sourcesSha(): string {
  const hash = createHash("sha256");
  for (const path of [...files("src/charts"), "vite.charts.config.ts"].sort()) {
    const text = readFileSync(join(here, path), "utf8").replace(/\r\n/g, "\n");
    hash.update(path, "utf8").update("\0").update(text, "utf8").update("\0");
  }
  return hash.digest("hex");
}

const sha = sourcesSha();

// Runs after minification, so the banner survives and the inline-safety
// check sees the final text.
const banner = (): Plugin => ({
  name: "myflames-charts-banner",
  enforce: "post",
  generateBundle(_options, bundle) {
    for (const chunk of Object.values(bundle)) {
      if (chunk.type !== "chunk") continue;
      const lowered = chunk.code.toLowerCase();
      if (lowered.includes("</script") || lowered.includes("<!--")) {
        throw new Error("myflames-charts.js must be inlinable in a <script> element.");
      }
      chunk.code = "/*! myflames-charts sources-sha256=" + sha +
        " | generated from apps/workspace/src/charts by vite.charts.config.ts; do not edit */\n" + chunk.code;
    }
  },
});

export default defineConfig({
  publicDir: false,
  define: { __MFC_SOURCES_SHA__: JSON.stringify(sha) },
  plugins: [banner()],
  build: {
    outDir: "../../myflames/assets",
    // anim-runtime.js lives there too.
    emptyOutDir: false,
    copyPublicDir: false,
    target: "es2020",
    minify: "esbuild",
    sourcemap: false,
    reportCompressedSize: false,
    lib: {
      entry: "src/charts/index.ts",
      name: "MyflamesCharts",
      formats: ["iife"],
      fileName: () => "myflames-charts.js",
    },
  },
});
