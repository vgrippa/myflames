// The Query editor settings store in src/editorSettings.ts, shared by the
// Settings popover, the command palette and the editor: the defaults show
// until the first read lands, one read is shared, writes go out one at a time
// in the order they were made, only the latest answer is published, a failed
// latest write reads the stored value again, and a change made while the
// first read is on its way wins over that read. No build or browser: the
// module is bundled with esbuild against an in-memory /api/settings/editor.
// Run: node test/editor-settings.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
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
  entryPoints: [fileURLToPath(new URL("../src/editorSettings.ts", import.meta.url))],
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent", plugins: [stubs],
});

// The backend: answers are released by the test, in any order. `stored` is
// what the workspace store holds; a {set: {...}} body applies to it.
const DEFAULTS = { multi_cursor: true, completion_auto: true, close_brackets: true, soft_wrap: false, problem_warnings: true, hover_info: true };
const server = { stored: { ...DEFAULTS }, sent: [], pending: [] };
globalThis.__api = (path, body) => new Promise((resolve, reject) => {
  assert.equal(path, "settings/editor");
  server.sent.push(body);
  server.pending.push({ body, resolve, reject });
});
const tick = () => new Promise((r) => setTimeout(r, 0));
async function answer(index = 0, fail = false) {
  const [call] = server.pending.splice(index, 1);
  if (fail) return call.reject(new Error("offline"));
  if (call.body.set) Object.assign(server.stored, call.body.set);
  call.resolve({ schema_version: "editor-settings-1.0", settings: { ...server.stored }, defaults: { ...DEFAULTS } });
  await tick();
}
const load = async () => import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64") + "#" + Math.random());
const reset = () => { server.stored = { ...DEFAULTS }; server.sent.length = 0; server.pending.length = 0; };

// The key list is the backend's: EDITOR_SETTING_DEFAULTS in workspace_store.py.
{
  const mod = await load();
  const python = await fs.readFile(new URL("../../../myflames/workspace_store.py", import.meta.url), "utf8");
  const block = python.match(/EDITOR_SETTING_DEFAULTS = \{([^}]*)\}/s)?.[1] || "";
  const keys = [...block.matchAll(/'([a-z_]+)':/g)].map((m) => m[1]);
  assert.deepEqual([...mod.EDITOR_SETTING_KEYS], keys, "EDITOR_SETTING_KEYS match EDITOR_SETTING_DEFAULTS");
  assert.deepEqual(mod.EDITOR_SETTING_DEFAULTS, DEFAULTS, "the defaults match the backend's");
  assert.deepEqual(mod.EDITOR_SETTING_KEYS.map((k) => mod.EDITOR_SETTING_LABELS[k]), [
    "Multiple cursors", "Suggestions while typing", "Close brackets and quotes",
    "Wrap long lines", "Problem warnings", "Details on hover",
  ], "the labels the Settings popover and the palette show");
}

// The defaults show until the first read lands; one read is shared.
{
  reset();
  const mod = await load();
  assert.deepEqual(mod.currentEditorSettings(), DEFAULTS, "defaults before the first read");
  assert.equal(mod.editorSettingsLoaded(), false);
  const first = mod.loadEditorSettings();
  const second = mod.loadEditorSettings();
  await tick();
  assert.equal(server.sent.length, 1, "one shared read");
  server.stored.soft_wrap = true;
  await answer();
  assert.equal(await first, await second);
  assert.equal(mod.currentEditorSettings().soft_wrap, true);
  assert.equal(mod.editorSettingsLoaded(), true);
  const seen = [];
  const off = mod.subscribeEditorSettings((s) => seen.push(s.soft_wrap));
  await mod.loadEditorSettings();
  assert.deepEqual(seen, [], "a second read is the one already known");
  off();
}

// Writes are sent one at a time, in order: the stored value is the last choice.
{
  reset();
  const mod = await load();
  const writes = [
    mod.setEditorSetting("soft_wrap", true),
    mod.setEditorSetting("soft_wrap", false),
    mod.setEditorSetting("multi_cursor", false),
  ];
  for (const w of writes) w.catch(() => {});
  await tick();
  assert.deepEqual(server.sent, [{ set: { soft_wrap: true } }], "the second write waits for the first answer");
  await answer();
  assert.deepEqual(server.sent.slice(1), [{ set: { soft_wrap: false } }]);
  await answer();
  assert.deepEqual(server.sent.slice(2), [{ set: { multi_cursor: false } }]);
  await answer();
  await Promise.all(writes);
  assert.deepEqual(server.stored, { ...DEFAULTS, multi_cursor: false });
  assert.equal(mod.currentEditorSettings().multi_cursor, false);
  assert.equal(mod.currentEditorSettings().soft_wrap, false);
}

// A failed latest write reads the stored value again.
{
  reset();
  server.stored.soft_wrap = true;
  const mod = await load();
  const failing = mod.setEditorSetting("soft_wrap", false);
  await tick();
  await answer(0, true);
  await assert.rejects(failing, /offline/);
  await tick();
  assert.deepEqual(server.sent.at(-1), {}, "the stored value is read again");
  await answer();
  assert.equal(mod.currentEditorSettings().soft_wrap, true, "the stored value wins back");
}

// A change made while the first read is on its way wins over that read.
{
  reset();
  const mod = await load();
  const reading = mod.loadEditorSettings();
  const write = mod.setEditorSetting("soft_wrap", true);
  await tick();
  assert.deepEqual(server.sent, [{}, { set: { soft_wrap: true } }]);
  await answer(0); // the read answers with the stale store (soft_wrap still off)
  await reading;
  assert.equal(mod.currentEditorSettings().soft_wrap, false, "the stale read is not published");
  await answer();
  await write;
  assert.equal(mod.currentEditorSettings().soft_wrap, true, "the change made during the read wins");
}
console.log("Editor settings store: defaults, shared read, ordered writes, re-read after a failed write, change during read passed.");

// Render the real popover callbacks with stubbed hooks and controls. This
// checks overlapping saves without a browser, transport or running service.
{
  const root = fileURLToPath(new URL("..", import.meta.url));
  const ui = await build({
    entryPoints: [fileURLToPath(new URL("../src/QueryEditorSettings.tsx", import.meta.url))],
    bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
    define: { "process.env.NODE_ENV": '"production"' },
    plugins: [{ name: "settings-ui", setup(b) {
      b.onResolve({ filter: /^react$|^\.\/kit$|^\.\/editorSettings$/ }, args =>
        args.importer.endsWith("/QueryEditorSettings.tsx")
          ? { path: args.path, namespace: "settings-ui" } : undefined);
      b.onLoad({ filter: /.*/, namespace: "settings-ui" }, args => ({ resolveDir: root, contents:
        args.path === "./kit" ? 'export const Checkbox = "checkbox";' : args.path === "./editorSettings" ? `
          export { EDITOR_SETTING_KEYS, EDITOR_SETTING_LABELS } from "./src/editorSettings";
          export const useEditorSettings = () => globalThis.__settingsUi.values;
          export const setEditorSetting = (key, on) => globalThis.__settingsUi.save(key, on);
        ` : `
          export { Fragment } from "react";
          export const useState = value => globalThis.__settingsUi.useState(value);
          export const useRef = value => globalThis.__settingsUi.useRef(value);
          export const useEffect = (effect, deps) => globalThis.__settingsUi.useEffect(effect, deps);
        ` }));
    } }],
  });
  const mod = await import("data:text/javascript;base64," + Buffer.from(ui.outputFiles[0].text).toString("base64"));
  const hooks = [], pending = [], effects = [];
  let index = 0, updates = 0;
  const useRef = value => hooks[index++] ??= { current: value };
  globalThis.__settingsUi = {
    values: DEFAULTS, useRef,
    useState(value) {
      const slot = useRef(value);
      return [slot.current, update => {
        updates++; slot.current = typeof update === "function" ? update(slot.current) : update;
      }];
    },
    useEffect(effect) {
      const slot = useRef(null);
      if (!slot.current) effects.push(() => { slot.current = { cleanup: effect() }; });
    },
    save: (key, on) => new Promise((resolve, reject) => pending.push({ key, on, resolve, reject })),
  };
  const render = () => {
    index = 0; effects.length = 0;
    const tree = mod.QueryEditorSettings();
    for (const effect of effects) effect();
    return tree;
  };
  const unmount = () => { for (const slot of hooks) slot.current?.cleanup?.(); };
  const row = (tree, key) => tree.props.children[1].find(node => node.key === key);
  const checkbox = (tree, key) => row(tree, key).props.children[0].props.children.props;
  const alert = (tree, key) => row(tree, key).props.children.at(-1);
  const change = (key, on) => checkbox(render(), key).onChange(on);
  try {
    let tree = render();
    for (const key of Object.keys(DEFAULTS)) {
      assert.equal(alert(tree, key).props.role, "alert", "every error live region is pre-mounted");
      assert.equal(alert(tree, key).props.children, "", "idle live regions are empty");
    }
    const errorId = alert(tree, "soft_wrap").props.id;
    change("soft_wrap", true); change("multi_cursor", false);
    tree = render();
    assert.equal(checkbox(tree, "soft_wrap").busy, true);
    assert.equal(checkbox(tree, "multi_cursor").busy, true);
    pending[0].reject(Error("A failed")); await tick();
    tree = render();
    assert.equal(checkbox(tree, "soft_wrap").busy, false);
    assert.equal(checkbox(tree, "multi_cursor").busy, true, "A's finally cannot clear B's busy state");
    assert.equal(alert(tree, "soft_wrap").props.children, "Could not save this setting. Try again.");
    assert.equal(alert(tree, "soft_wrap").props.id, errorId, "the live region keeps its identity");
    assert.equal(checkbox(tree, "soft_wrap").describedBy, errorId);
    pending[1].reject(Error("B failed")); await tick();
    change("multi_cursor", true);
    tree = render();
    assert.equal(alert(tree, "soft_wrap").props.children, "Could not save this setting. Try again.", "B's retry keeps A's error");
    assert.equal(alert(tree, "multi_cursor").props.children, "");
    pending[2].resolve(); await tick();

    // Two callbacks before the busy checkbox rerenders must still be ordered.
    change("soft_wrap", true); change("soft_wrap", false);
    pending[3].reject(Error("stale failure")); await tick();
    tree = render();
    assert.equal(checkbox(tree, "soft_wrap").busy, true, "a stale save cannot clear a newer busy state");
    assert.equal(alert(tree, "soft_wrap").props.children, "", "a stale save cannot publish its error");
    pending[4].resolve(); await tick();
    assert.equal(checkbox(render(), "soft_wrap").busy, false);

    change("hover_info", false);
    unmount(); const before = updates;
    pending[5].reject(Error("unmounted")); await tick();
    assert.equal(updates, before, "settled saves cannot update an unmounted popover");
  } finally {
    unmount(); delete globalThis.__settingsUi;
  }
  console.log("Editor settings popover: concurrent keys, independent errors, stale saves, mounted alerts and unmount passed.");
}
