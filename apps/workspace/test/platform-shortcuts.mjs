// Unit tests for platform.ts: shortcut labels name ⌘ and ⌥ on macOS and
// Ctrl and Alt on Windows and Linux. Run: node test/platform-shortcuts.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/platform.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "platform.ts", { loader: "ts", format: "esm", target: "es2020" });

// MAC is decided when the module loads, so load one copy per platform.
async function load(platform, userAgent) {
  Object.defineProperty(globalThis, "navigator", { value: { platform, userAgent }, configurable: true });
  const tagged = code + `\n// ${platform}`;
  return import("data:text/javascript;base64," + Buffer.from(tagged).toString("base64"));
}

const mac = await load("MacIntel", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)");
assert.equal(mac.MAC, true);
assert.equal(mac.shortcut("K"), "⌘K");
assert.equal(mac.shortcut("R", true), "⌥⌘R");

const windows = await load("Win32", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) myflames/3.2.0 Electron/44.8.0");
assert.equal(windows.MAC, false);
assert.equal(windows.shortcut("K"), "Ctrl+K");
assert.equal(windows.shortcut("R", true), "Ctrl+Alt+R");
assert.equal(windows.shortcut("I", true), "Ctrl+Alt+I");

const linux = await load("Linux x86_64", "Mozilla/5.0 (X11; Linux x86_64)");
assert.equal(linux.shortcut("K"), "Ctrl+K");

// An empty platform falls back to the user agent.
const ipad = await load("", "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)");
assert.equal(ipad.MAC, true);

// No visible label in the shell, Server or kit spells a Mac-only shortcut.
for (const file of ["main.tsx", "Server.tsx", "kit.tsx"]) {
  const text = await fs.readFile(new URL(`../src/${file}`, import.meta.url), "utf8");
  const labels = text.split("\n").filter((line) => !line.trim().startsWith("//") && /⌘|Cmd\+/.test(line) && !/MAC \?/.test(line));
  assert.deepEqual(labels, [], `${file} has a Mac-only shortcut label`);
}
console.log("platform shortcut labels: ok");
