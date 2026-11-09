// Socket-free checks of production inverse selection, API and text rendering.
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { build } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const workspace = fileURLToPath(new URL("../", import.meta.url));
const id = "\0object-changes-unit";
const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
  plugins: [{ name: "object-changes-unit", resolveId(value) { if (value === id) return id; }, load(value) {
    if (value === id) return `export { parameterUndo, relativeChangeTime, operationLabel, ObjectChanges, StoredStatement } from ${JSON.stringify(path.join(workspace, "src/ObjectChanges.tsx"))};
      export { readObjectAudit } from ${JSON.stringify(path.join(workspace, "src/api.ts"))};`;
  }, transform(code, filename) {
    if (filename.endsWith("/src/ObjectChanges.tsx")) return code.replace("function StoredStatement(", "export function StoredStatement(");
  } }], build: { ssr: true, write: false, rollupOptions: { input: id, output: { inlineDynamicImports: true } } } });
const directory = await fs.mkdtemp(path.join(workspace, ".object-changes-unit-"));
try {
  const modulePath = path.join(directory, "unit.mjs");
  await fs.writeFile(modulePath, compiled.output.find(f => f.type === "chunk").code);
  const { parameterUndo, relativeChangeTime, operationLabel, readObjectAudit, StoredStatement } = await import(pathToFileURL(modulePath));
  const html = renderToStaticMarkup(createElement(StoredStatement, { statement: "</script><img src=x onerror=alert(1)>\nSET GLOBAL x='RTL'" }));
  assert.ok(html.includes("&lt;/script&gt;&lt;img"), "SQL is escaped text");
  assert.ok(!html.includes("<img") && !html.includes("<script"));
  assert.match(html, /<pre class="inspector-sql"[^>]*aria-label="Statement as stored"/);
  assert.match(html, /<code><bdi>&lt;\/script/);
  const labels = {
    editor_run: "Run SQL", set_global: "Set global parameter", set_persist: "Set and persist parameter",
    set_persist_only: "Persist parameter for restart", set_session: "Set session parameter", reset_persist: "Reset persisted parameter",
    "accounts.create": "Create account", "accounts.drop": "Drop account", "accounts.rename": "Rename account",
    "accounts.lock": "Lock account", "accounts.unlock": "Unlock account", "accounts.expire": "Expire password",
    "accounts.password": "Change password", "accounts.grant": "Grant privileges", "accounts.revoke": "Revoke privileges",
    "accounts.roles": "Change roles", "accounts.options": "Change account options",
  };
  for (const [key, label] of Object.entries(labels)) assert.equal(operationLabel(key), label, key);
  assert.equal(operationLabel('accounts.future'), 'accounts.future', 'unknown keys retain their diagnostic text');
  assert.equal(operationLabel('toString'), 'toString', 'prototype names are untrusted operation keys too');
  const record = { status: "ok", operation: "set_global", target: { mode: "global", global_value: "151" } };
  assert.deepEqual(parameterUndo(record), { mode: "global", value: "151" });
  assert.deepEqual(parameterUndo({ ...record, target: { mode: "global", global_value: "" } }), { mode: "global", value: "" });
  for (const status of ["unknown", "error", "pending", "declined", "refused"])
    assert.equal(parameterUndo({ ...record, status }), null, status);
  assert.deepEqual(parameterUndo({ ...record, target: { mode: "global", global_value: null } }), { mode: "global", value: null });
  assert.equal(parameterUndo({ ...record, target: { mode: "global" } }), null, "a missing before-value is not SQL NULL");
  for (const value of [undefined, [], {}, "[redacted]", "x".repeat(4097)])
    assert.equal(parameterUndo({ ...record, target: { mode: "global", global_value: value } }), null);
  for (const mode of ["session", "persist", "persist_only", "reset_persist"])
    assert.equal(parameterUndo({ ...record, target: { ...record.target, mode } }), null, "mode/operation must agree");
  for (const operation of ["set_persist_only", "reset_persist"]) {
    const mode = operation === "set_persist_only" ? "persist_only" : "reset_persist";
    const old = { status: "ok", operation, target: { mode, persisted_state: { state: "present" }, persisted_value: "300" } };
    assert.deepEqual(parameterUndo(old), { mode: "persist_only", value: "300" });
    assert.deepEqual(parameterUndo({ ...old, target: { ...old.target, persisted_value: null } }), { mode: "persist_only", value: null });
    assert.equal(parameterUndo({ ...old, target: { mode, persisted_state: { state: "present" } } }), null);
    assert.equal(parameterUndo({ ...old, target: { mode, persisted_state: { state: "unreadable" }, persisted_value: "300" } }), null);
  }
  assert.deepEqual(parameterUndo({ status: "ok", operation: "set_persist_only", target: { mode: "persist_only", persisted_state: { state: "absent" } } }), { mode: "reset_persist", value: "" });
  assert.equal(parameterUndo({ ...record, operation: "set_persist", target: { mode: "persist", global_value: "151" } }), null, "compound undo is not a one-step inverse");
  const now = Date.parse("2026-11-08T12:00:00Z");
  assert.equal(relativeChangeTime("2026-11-08T11:59:01Z", now), "Just now");
  assert.equal(relativeChangeTime("2026-11-08T11:59:00Z", now), "1 min ago");
  assert.equal(relativeChangeTime("2026-11-08T10:00:00Z", now), "2 h ago");
  assert.equal(relativeChangeTime("2026-11-06T12:00:00Z", now), "2 d ago");
  assert.equal(relativeChangeTime("invalid", now), "invalid");
  const calls = [];
  globalThis.fetch = async (url, options) => {
    if (url === "/api/auth/session") return { ok: true, json: async () => ({ token: "fixture-token" }) };
    calls.push({ url, body: JSON.parse(options.body), signal: options.signal, token: options.headers["X-Myflames-Token"] });
    return { ok: true, json: async () => ({ schema_version: "audit-1.0", records: [record] }) };
  };
  const controller = new AbortController();
  assert.equal((await readObjectAudit("db:3306", { kind: "parameter", key: "max_connections" }, controller.signal)).records.length, 1);
  assert.deepEqual(calls, [{ url: "/api/audit/object", body: { server: "db:3306", kind: "parameter", key: "max_connections" }, signal: controller.signal, token: "fixture-token" }]);
  for (const host of ["bastion-a", "bastion-b"]) {
    const ssh = { host, user: "deploy", port: 2222 };
    await readObjectAudit("127.0.0.1:3306", { kind: "parameter", key: "max_connections" }, controller.signal, ssh);
    assert.deepEqual(calls.at(-1).body, { server: "127.0.0.1:3306", ssh, kind: "parameter", key: "max_connections" });
  }
  console.log("object changes unit: inverse modes, refusal paths, time boundaries, escaped SQL and authenticated local API passed");
} finally { await fs.rm(directory, { recursive: true, force: true }); }
