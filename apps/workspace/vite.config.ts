import { THEME_IDS } from "./src/themes";
import { defineConfig } from "vite";
export function validateThemeIds(ids: readonly string[]): void {
  if (ids.some(id => !/^[a-z]+(-[a-z]+)*$/.test(id))) {
    throw new Error("Theme ids must contain lowercase words separated by hyphens.");
  }
}
export default defineConfig({
  base: "/",
  plugins: [{ name: "myflames-theme-ids", transformIndexHtml(html) {
    validateThemeIds(THEME_IDS);
    return html.replace("__MYFLAMES_THEME_IDS__", () => JSON.stringify(THEME_IDS));
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
