// Socket-free regression tests of the actual components. Like
// reviewed-actions-unit.mjs, hooks preserve render order and JSX children are
// inspectable nodes; network responses are explicitly released, never timed.
import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs/promises";
import vm from "node:vm";
import { transformWithEsbuild } from "vite";

const deferred = () => {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
};
const flush = () => new Promise(setImmediate);

async function component(file, mocks) {
  const source = await fs.readFile(new URL(`../src/${file}`, import.meta.url), "utf8");
  const { code } = await transformWithEsbuild(source.replace(/^import[\s\S]*?;\n/gm, ""), file, {
    loader: file.endsWith("tsx") ? "tsx" : "ts", format: "cjs", target: "es2020",
    jsx: "transform", jsxFactory: "jsx", jsxFragment: "Fragment",
    tsconfigRaw: { compilerOptions: { jsx: "react" } },
  });
  const slots = [], effects = [];
  let cursor = 0;
  const node = (type, props, ...children) => ({ type, props: { ...props, children } });
  const context = {
    module: { exports: {} }, exports: {}, Date, AbortController,
    jsx: node, createElement: node, Fragment: "Fragment",
    createContext: () => ({ Provider: "Provider" }),
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], value => { slots[i] = typeof value === "function" ? value(slots[i]) : value; }];
    },
    useRef(initial) { const i = cursor++; return slots[i] || (slots[i] = { current: initial }); },
    useCallback(fn, deps) {
      const i = cursor++;
      if (!slots[i] || deps.some((v, n) => v !== slots[i].deps[n])) slots[i] = { deps, fn };
      return slots[i].fn;
    },
    useEffect(effect, deps) {
      const i = cursor++;
      if (!slots[i] || deps.some((v, n) => v !== slots[i].deps[n])) {
        const previous = slots[i]; slots[i] = { deps };
        effects.push(() => { previous?.cleanup?.(); slots[i].cleanup = effect(); });
      }
    },
    connectionKey: c => c.host, connectionLabel: c => c.host,
    ...mocks,
  };
  vm.runInNewContext(code, context);
  return (name, props) => {
    cursor = 0;
    const result = context.module.exports[name](props);
    while (effects.length) effects.shift()();
    return result;
  };
}

function nodes(tree, name) {
  if (Array.isArray(tree)) return tree.flatMap(n => nodes(n, name));
  if (!tree || typeof tree !== "object" || !tree.props) return [];
  return [ ...(tree.type === name || tree.type?.name === name ? [tree] : []),
    ...Object.values(tree.props).flatMap(n => nodes(n, name)) ];
}

test("late reconnect refreshes status without committing the password being typed", async () => {
  const requests = [], focus = {};
  const renderComponent = await component("privileges.ts", {
    window: { addEventListener: (name, fn) => { focus[name] = fn; }, removeEventListener: name => { delete focus[name]; } },
    document: { activeElement: { matches: () => false } },
    sharedRead: (_key, _params, read, options) => read(options.signal),
    readPrivileges: async c => { requests.push({ ...c }); return { account: "actor@%", actions: {} }; },
  });
  let connection = { host: "A", password: "committed" }, connectRevision = 0, passwordRevision = 0;
  const render = () => renderComponent("PrivilegesProvider", { connection, connectRevision, passwordRevision }).props.value;
  render(); await flush();
  assert.equal(requests.length, 1);
  connection = { ...connection, password: "part" }; render();
  connectRevision++; // Reconnect was sent before the picker started editing.
  let value = render(); await flush();
  assert.equal(requests.length, 1, "a reconnect response must not authenticate with a partial password");
  await value.refresh(true); await value.accountChanged(); focus.focus(); await flush();
  assert.equal(requests.length, 1, "incidental reads also wait for the explicit password commit");
  connection = { ...connection, password: "replacement" }; render();
  passwordRevision++; value = render(); await flush();
  assert.equal(requests.length, 2);
  assert.equal(requests.at(-1).password, "replacement");
  render(); await flush(); assert.equal(requests.length, 2, "a rerender is not another commit");
  connectRevision++; render(); await flush();
  assert.equal(requests.length, 3, "reconnect still refreshes when the credential is committed");
  connection = { host: "B", password: "B-password" }; render(); await flush();
  assert.equal(requests.at(-1).host, "B", "a new connection retains its own credentials");
});

test("explicit Use connection commits new credentials even for the same identity", async () => {
  const requests = [];
  let connection = { host: "A", password: "old" }, passwordRevision = 0;
  const renderComponent = await component("privileges.ts", {
    window: { addEventListener() {}, removeEventListener() {} },
    sharedRead: (_key, _params, read, options) => read(options.signal),
    readPrivileges: async c => { requests.push({ ...c }); return { actions: {} }; },
  });
  const render = () => renderComponent("PrivilegesProvider", { connection, passwordRevision });
  render(); await flush();
  const source = await fs.readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
  const callback = source.slice(source.indexOf("  function applyConnection("), source.indexOf("  function switchConnection("));
  const { code } = await transformWithEsbuild(callback + "\nmodule.exports = applyConnection;", "selection.ts", { loader: "ts", format: "cjs" });
  const context = { module: { exports: {} },
    recordConnectionUse() {}, patchLab: ({ connection: fields }) => { connection = { ...connection, ...fields }; },
    setActivePassword: password => { connection = { ...connection, password }; },
    setPasswordRevision: update => { passwordRevision = update(passwordRevision); }, setConnectionNote() {},
  };
  vm.runInNewContext(code, context);
  context.module.exports("profile-A", { host: "A" }, "explicit-new-password");
  render(); await flush();
  assert.deepEqual(requests.map(r => r.password), ["old", "explicit-new-password"]);
});

async function accountHarness() {
  let connection = { host: "A" }, reply, changed = null;
  const sends = [], refreshes = [], declines = [];
  const target = { name: "app", host: "%" };
  const groups = [{ target, edit: { operation: "password", random_password: true }, plan: {} }];
  const renderComponent = await component("Accounts.tsx", {
    ...Object.fromEntries(["ViewLayout", "ViewStatus", "SearchField", "EmptyState", "ActionButton", "ActionStatus", "SummaryLine", "DataTable"].map(n => [n, n])),
    useWritePolicy: () => ({ blocked: "", check: async () => ({ ...connection }) }),
    usePrivileges: () => ({ accountChanged: async () => { refreshes.push(["privileges", connection.host]); if (changed) await changed.promise; } }),
    usePrivilege: () => ({ available: true }), useSince: () => 0, useOpenRead: () => {},
    useConnectionReads: () => ({ key: connection.host, currentKey: () => connection.host, loading: "", running: () => false,
      run: async params => { refreshes.push([params, connection.host]); } }),
    accountKey: a => `${a.name}@${a.host}`, accountLabel: a => `${a.name}@${a.host}`,
    accountPlanProblem: () => "", planAccountGroups: async () => groups,
    declineAccountGroups: async () => { declines.push(1); },
    applyAccountGroups: async (via, _groups, _typed, check) => { await check(); sends.push(via.host); return reply.promise; },
    listAccounts: () => {}, readAccount: () => {},
  });
  const render = () => renderComponent("AccountsView", { connection, report: null, selection: { key: "", select() {} }, onReport() {} });
  const one = (tree, name) => { const found = nodes(tree, name); assert.equal(found.length, 1, name); return found[0]; };
  const start = async () => {
    reply = deferred();
    nodes(render(), "AccountGate")[0].props.onClick();
    await one(render(), "AccountForm").props.onPrepare(target, groups.map(g => g.edit));
    const task = one(render(), "ReviewSheet").props.onApply({});
    await flush();
    one(render(), "ReviewSheet").props.onLeave();
    assert.equal(nodes(render(), "ReviewSheet").length, 0, "Close hides the pending sheet");
    assert.equal(declines.length, 0, "Close is not a decline");
    return { task };
  };
  render();
  return { render, start, sends, refreshes, declines,
    switchTo(host) { connection = { host }; render(); },
    delayPrivileges() { changed = deferred(); return changed; },
    finish(status, secret) {
      reply.resolve([{ group: groups[0], status: status === "applied" ? "applied" : "failed", message: status,
        result: { status, ...(secret ? { generated_password: secret } : {}) } }]);
    },
  };
}

test("account results and one-time passwords survive switching connections until individually dismissed", async () => {
  const h = await accountHarness();
  const first = await h.start();
  h.switchTo("B"); h.finish("applied", "secret-A"); await first.task;
  assert.equal(nodes(h.render(), "GeneratedPassword").length, 0, "A's secret is hidden on B");
  assert.equal(nodes(h.render(), "BatchResult").length, 0, "A's result is hidden on B");
  assert.equal(h.refreshes.length, 0, "a late outcome must not refresh B");
  const second = await h.start(); h.finish("applied", "secret-B"); await second.task;
  assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "secret-B");
  h.switchTo("A");
  assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "secret-A", "A's late secret must remain recoverable");
  const results = nodes(h.render(), "BatchResult");
  assert.equal(results.length, 1, "A's outcome remains with its identity");
  assert.ok(!JSON.stringify(results[0].props.results).includes("secret-A"), "ordinary results contain no password");
  results[0].props.onDismiss();
  assert.equal(nodes(h.render(), "GeneratedPassword").length, 1, "dismissing the result does not discard the secret");
  // A later operation, even without a password, must not overwrite an
  // unacknowledged one-time secret on this same connection.
  const third = await h.start(); h.finish("unknown"); await third.task;
  assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "secret-A");
  const fourth = await h.start(); h.finish("applied", "secret-A2"); await fourth.task;
  assert.deepEqual(nodes(h.render(), "GeneratedPassword").map(n => n.props.password), ["secret-A", "secret-A2"]);
  nodes(h.render(), "GeneratedPassword")[0].props.onDismiss();
  assert.deepEqual(nodes(h.render(), "GeneratedPassword").map(n => n.props.password), ["secret-A2"]);
  h.switchTo("B"); assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "secret-B");
  h.switchTo("A"); assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "secret-A2");
  assert.deepEqual(h.sends, ["A", "B", "A", "A"], "each confirmed write is dispatched once");
  assert.equal(h.declines.length, 0);
});

test("follow-up account reads recheck identity after an awaited privilege refresh", async () => {
  const h = await accountHarness();
  const wait = h.delayPrivileges(), run = await h.start();
  h.finish("applied", "retained-secret"); await flush();
  assert.deepEqual(h.refreshes, [["privileges", "A"]]);
  h.switchTo("B"); wait.resolve(); await run.task;
  assert.deepEqual(h.refreshes, [["privileges", "A"]], "no account read may run against B after A's privilege refresh");
  h.switchTo("A"); assert.equal(nodes(h.render(), "GeneratedPassword")[0]?.props.password, "retained-secret");
});
