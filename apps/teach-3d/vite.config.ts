// Builds src/main.ts into one IIFE (dist/teach3d.js). scripts/assemble.mjs
// then inlines it, with src/style.css, into src/page.html and writes the
// single offline file myflames/assets/teach3d-btree.html.
import { defineConfig } from "vite";

export default defineConfig({
  publicDir: false,
  build: {
    outDir: "dist",
    emptyOutDir: true,
    copyPublicDir: false,
    target: "es2020",
    minify: "esbuild",
    reportCompressedSize: false,
    lib: {
      entry: "src/main.ts",
      formats: ["iife"],
      name: "Teach3d",
      fileName: () => "teach3d.js",
    },
  },
  esbuild: { legalComments: "none" },
});
