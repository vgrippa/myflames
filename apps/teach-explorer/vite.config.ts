import { defineConfig, type PluginOption } from "vite";
import react from "@vitejs/plugin-react";
import { resolve, normalize, join } from "node:path";
import { readFile, stat } from "node:fs/promises";

const DOCS_TEACH = resolve(__dirname, "../../docs/teach");

// In dev, mount the existing rendered teach lessons under /teach/ so
// the "Open full lesson" links resolve. In prod, the built app lives at
// docs/teach-explorer/, so "../teach/..." works naturally.
const serveTeachDir = (): PluginOption => ({
  name: "serve-teach-dir",
  configureServer(server) {
    server.middlewares.use(async (req, res, next) => {
      const url = req.url || "";
      if (!url.startsWith("/teach/") && url !== "/teach") return next();
      const rel = decodeURIComponent(url.replace(/^\/teach\/?/, "").split("?")[0]) || "index.html";
      const filePath = normalize(join(DOCS_TEACH, rel));
      if (!filePath.startsWith(DOCS_TEACH)) {
        res.statusCode = 403;
        return res.end("forbidden");
      }
      try {
        const s = await stat(filePath);
        if (s.isDirectory()) {
          const buf = await readFile(join(filePath, "index.html"));
          res.setHeader("Content-Type", "text/html; charset=utf-8");
          return res.end(buf);
        }
        const buf = await readFile(filePath);
        const ext = filePath.split(".").pop() || "";
        const mime: Record<string, string> = {
          html: "text/html; charset=utf-8",
          js: "application/javascript",
          css: "text/css",
          svg: "image/svg+xml",
          json: "application/json",
          png: "image/png",
          jpg: "image/jpeg",
        };
        res.setHeader("Content-Type", mime[ext] || "application/octet-stream");
        return res.end(buf);
      } catch {
        return next();
      }
    });
  },
});

export default defineConfig({
  plugins: [react(), serveTeachDir()],
  base: "./",
  build: {
    outDir: resolve(__dirname, "../../docs/teach-explorer"),
    emptyOutDir: true,
  },
  server: {
    port: 5174,
    fs: {
      allow: [resolve(__dirname, "../..")],
    },
  },
});
