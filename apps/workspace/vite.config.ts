import { THEME_IDS } from "./src/themes";
import { defineConfig } from "vite";
export default defineConfig({
  base: "/",
  plugins: [{ name: "myflames-theme-ids", transformIndexHtml(html) {
    return html.replace("__MYFLAMES_THEME_IDS__", JSON.stringify(THEME_IDS));
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
