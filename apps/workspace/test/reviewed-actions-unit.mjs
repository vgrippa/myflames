// Exercise the actual hook without a DOM or a listening socket. State and
// effects follow render order; child components remain inspectable JSX nodes.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import vm from "node:vm";
import { webcrypto } from "node:crypto";
import { transformWithEsbuild } from "vite";

export async function reviewedActionsUnit() {
  const source = await fs.readFile(new URL("../src/ActionReview.tsx", import.meta.url), "utf8");
  const ops = await fs.readFile(new URL("../src/opsApi.ts", import.meta.url), "utf8");
  const scope = ops.slice(ops.indexOf("export function useOpsScope("), ops.indexOf("/** Engine-specific data"));
  const { code } = await transformWithEsbuild(scope + source.replace(/^import[\s\S]*?;\n/gm, ""), "ActionReview.tsx", {
    loader: "tsx", format: "cjs", target: "es2020", jsx: "transform", jsxFactory: "jsx", jsxFragment: "Fragment",
    tsconfigRaw: { compilerOptions: { jsx: "react" } },
  });
  for (const outcome of ["done", "partial", "unknown", "refused", "error", "lost"]) {
    for (const switchTo of [null, "B", "ABA"]) {
      const slots = [], effects = [];
      let cursor = 0, resolve, reject, sent = 0, refreshed = 0, followed = 0;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      class ApiError extends Error { constructor(message, status) { super(message); this.status = status; } }
      const context = {
        module: { exports: {} }, exports: {}, crypto: webcrypto, Date,
        // Polling is deliberately parked: the send's own response decides.
        window: { setTimeout: () => 0 },
        jsx: (type, props, ...children) => ({ type, props: { ...props, children } }), Fragment: "fragment",
        useState(initial) {
          const i = cursor++;
          if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
          return [slots[i], (value) => { slots[i] = typeof value === "function" ? value(slots[i]) : value; }];
        },
        useRef(initial) { const i = cursor++; return slots[i] || (slots[i] = { current: initial }); },
        useEffect(effect, deps) {
          const i = cursor++;
          if (!slots[i] || deps.some((v, n) => v !== slots[i].deps[n])) {
            const previous = slots[i];
            slots[i] = { deps };
            effects.push(() => { previous?.cleanup?.(); slots[i].cleanup = effect(); });
          }
        },
        connectionKey: (c) => c.host, connectionLabel: (c) => c.host,
        useWritePolicy: () => null, recordDeclined: async () => {}, api: async () => ({}), ApiError,
      };
      vm.runInNewContext(code, context);
      const hook = context.module.exports.useReviewedActions;
      let connection = { host: "A" };
      function render() {
        cursor = 0;
        const result = hook({ connection, operation: "proxysql_action",
          send: () => { sent++; return promise; }, onSent: () => { refreshed++; }, onNext: () => { followed++; } });
        while (effects.length) effects.shift()();
        return result;
      }
      const reviewSheet = (value) => value.sheet.props.children.find((n) => n?.type?.name === "ReviewSheet");
      let value = render();
      const plan = { title: "Change?", action: "servers_save", statements: ["SAVE MYSQL SERVERS TO DISK"], typed_guard: null };
      await value.review("save", async () => plan);
      value = render();
      reviewSheet(value).props.onConfirm();
      render();
      assert.equal(sent, 1);
      if (switchTo) {
        connection = { host: "B" }; render();
        if (switchTo === "ABA") { connection = { host: "A" }; render(); }
      }
      if (outcome === "error") reject(new ApiError("Nothing was sent.", 400));
      else if (outcome === "lost") reject(new Error("Reply lost"));
      else resolve({ status: outcome, next: { action: "servers_save" } });
      await new Promise(setImmediate);
      value = render();
      const sheet = reviewSheet(value);
      if (switchTo) {
        assert.equal(sheet, undefined, `${outcome}/${switchTo}: old send must close after completion`);
        assert.equal(value.result, null, `${outcome}/${switchTo}: no old result or follow-up on this connection`);
        assert.equal(value.error, "");
        assert.equal(refreshed, 0, "no refresh using the replacement connection");
        assert.equal(value.busy, false, "a stale completed send must release the review flow");
      } else {
        assert.ok(sheet?.props.run.result, `${outcome}: current connection keeps its answered sheet`);
        assert.equal(!!sheet.props.onNext, outcome === "done");
      }
      assert.equal(followed, 0);
      assert.equal(sent, 1, "no retry");
    }
  }
  console.log("proxysql: reviewed action hook regressions ok");
}
