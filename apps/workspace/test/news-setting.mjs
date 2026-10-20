// The Home news switch in src/newsApi.ts, shared by Home and the Settings
// popover: writes go out one at a time in the order they were made, only the
// latest answer is shown, a failed latest write reads the stored value again,
// and a change made while the first read is on its way wins over that read.
// No build or browser: the module is bundled with esbuild against an
// in-memory /api/news/settings. Run: node test/news-setting.mjs
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const stubs = {
  name: "stubs",
  setup(b) {
    b.onResolve({ filter: /^(react|\.\/api)$/ }, (args) => ({ path: args.path, namespace: "stub" }));
    b.onLoad({ filter: /.*/, namespace: "stub" }, (args) => ({
      contents: args.path === "react"
        ? "export const useEffect = () => {}; export const useState = (v) => [v, () => {}];"
        : "export const api = (path, body) => globalThis.__api(path, body);",
    }));
  },
};
const result = await build({
  entryPoints: [fileURLToPath(new URL("../src/newsApi.ts", import.meta.url))],
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent", plugins: [stubs],
});

// The backend: answers are released by the test, in any order.
const server = { stored: true, sent: [], pending: [] };
globalThis.__api = (path, body) => new Promise((resolve, reject) => {
  assert.equal(path, "news/settings");
  server.sent.push(body);
  server.pending.push({ body, resolve, reject });
});
const tick = () => new Promise((r) => setTimeout(r, 0));
async function answer(index = 0, fail = false) {
  const [call] = server.pending.splice(index, 1);
  if (fail) return call.reject(new Error("offline"));
  if ("enabled" in call.body) server.stored = call.body.enabled;
  call.resolve({ enabled: server.stored });
  await tick();
}
const load = async () => import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64") + "#" + Math.random());

// Writes are sent one at a time, in order: the stored value is the last choice.
{
  const news = await load();
  const shown = [];
  const writes = [news.setNewsEnabled(false), news.setNewsEnabled(false), news.setNewsEnabled(true)];
  for (const w of writes) w.catch(() => {});
  await tick();
  assert.deepEqual(server.sent, [{ enabled: false }], "the second write waits for the first answer");
  await answer();
  assert.deepEqual(server.sent.slice(1), [{ enabled: false }]);
  await answer();
  assert.deepEqual(server.sent.slice(2), [{ enabled: true }]);
  await answer();
  await Promise.all(writes);
  assert.equal(server.stored, true);
  // useNewsEnabled reads the published value through its state initializer.
  shown.push(news.useNewsEnabled());
  assert.deepEqual(shown, [true]);
}
// A failed latest write reads the stored value again.
{
  server.sent.length = 0;
  server.stored = false;
  const news = await load();
  const first = news.setNewsEnabled(false);
  await tick();
  await answer();
  await first;
  assert.equal(news.useNewsEnabled(), false);
  const failing = news.setNewsEnabled(true);
  await tick();
  await answer(0, true);
  await assert.rejects(failing, /offline/);
  await tick();
  assert.deepEqual(server.sent.at(-1), {}, "the stored value is read again");
  await answer();
  assert.equal(news.useNewsEnabled(), false);
}
console.log("Home news switch: ordered writes, latest answer only, re-read after a failed write passed.");
