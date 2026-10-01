import { defineConfig } from "vite";
export default defineConfig({
  base: "/",
  build: {
    outDir: "../../myflames/ui_assets",
    emptyOutDir: true,
    target: "es2020",
    rollupOptions: {
      output: {
        // Keep the logo address valid for tabs left open during a rebuild.
        assetFileNames: (asset) =>
          asset.name === "myflames-icon-v2.png"
            ? "assets/myflames-logo[extname]"
            : "assets/[name]-[hash][extname]",
      },
    },
  },
});
