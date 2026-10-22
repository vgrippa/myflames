"use strict";
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(
  // If the importer is in node compatibility mode or this is not an ESM
  // file that has been converted to a CommonJS file using a Babel-
  // compatible transform (i.e. "__esModule" has not been set), then set
  // "default" to the CommonJS "module.exports" for node compatibility.
  isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target,
  mod
));
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// src/Tuning.tsx
var Tuning_exports = {};
__export(Tuning_exports, {
  TuningView: () => TuningView
});
module.exports = __toCommonJS(Tuning_exports);
var import_react12 = require("react");

// src/api.ts
var ApiError = class extends Error {
  constructor(message, status, data2) {
    super(message);
    this.status = status;
    this.code = typeof data2.code === "string" ? data2.code : "";
    this.data = data2;
  }
};
var launchSecret = "";
var authentication = null;
var SESSION_PROOF = "myflames.session-proof";
function sessionProof() {
  try {
    return window.localStorage.getItem(SESSION_PROOF) || "";
  } catch {
    return "";
  }
}
function captureLaunchSecret() {
  if (typeof window === "undefined") return;
  const secret = new URLSearchParams(window.location.hash.slice(1)).get("launch");
  if (secret) {
    launchSecret = secret;
    authentication = null;
  }
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
}
captureLaunchSecret();
function authenticate() {
  captureLaunchSecret();
  if (!authentication) {
    const body = JSON.stringify(launchSecret ? { secret: launchSecret } : {});
    launchSecret = "";
    authentication = fetch("/api/auth/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": sessionProof() },
      body
    }).then(async (response) => {
      const result = await response.json();
      if (!response.ok || typeof result.token !== "string" || !result.token)
        throw new ApiError(result.error || "Open the launch URL printed by myflames ui.", response.status, result);
      try {
        window.localStorage.setItem(SESSION_PROOF, result.token);
      } catch {
      }
      return result.token;
    });
  }
  return authentication;
}
async function api(path, body, signal, keepalive = false) {
  const csrf = await authenticate();
  const response = await fetch(`/api/${path}`, {
    credentials: "same-origin",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Myflames-Token": csrf
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
    signal,
    keepalive
  });
  let result = {};
  try {
    result = await response.json();
  } catch {
  }
  if (!response.ok)
    throw new ApiError(
      typeof result.error === "string" ? result.error : "The request failed. Try again.",
      response.status,
      result
    );
  return result;
}
function download(text, filename, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1e3);
}
var numberFormats = /* @__PURE__ */ new Map();
function formatNumber(value, options) {
  const key = JSON.stringify(options);
  let format = numberFormats.get(key);
  if (!format) numberFormats.set(key, format = new Intl.NumberFormat(void 0, options));
  return format.format(value);
}
function time(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "\u2014";
  if (ms >= 1e3) return `${(ms / 1e3).toFixed(2)} s`;
  if (ms > 0 && ms < 1) return `${(ms * 1e3).toFixed(1)} \xB5s`;
  return `${formatNumber(ms, { maximumFractionDigits: 2 })} ms`;
}
function number(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "\u2014";
  return formatNumber(value, { maximumFractionDigits: 0 });
}

// src/binding.ts
var import_react = require("react");
function connectionKey(c) {
  const key = [c.profile_id || "", c.host || "", Number(c.port) || 0, c.user || "", c.ssl_mode || "", c.ssl_ca || ""];
  const type = c.type === "development" ? "development" : "production";
  if (type !== "production" || c.read_only === true) key.push(type, c.read_only === true);
  return JSON.stringify(key);
}
function connectionLabel(c) {
  const target = `${c.user ? `${c.user}@` : ""}${c.host}:${c.port}`;
  return c.name && c.name !== target ? `${c.name} (${target})` : target;
}
var inflight = /* @__PURE__ */ new Map();
var generations = /* @__PURE__ */ new Map();
function readView(params) {
  const at = params.indexOf(":");
  return at < 0 ? params : params.slice(0, at);
}
function nextGeneration(view) {
  const next = (generations.get(view) || 0) + 1;
  generations.set(view, next);
  return next;
}
function latestGeneration(view) {
  return generations.get(view) || 0;
}
var viewReads = /* @__PURE__ */ new Map();
function sharedRead(key, params, start, options = {}) {
  const id = `${key}|${params}`;
  let entry = options.fresh ? void 0 : inflight.get(id);
  if (!entry) {
    const controller = new AbortController();
    const created = { promise: start(controller.signal), controller, users: 0 };
    const drop = () => {
      if (inflight.get(id) === created) inflight.delete(id);
    };
    created.promise.then(drop, drop);
    inflight.set(id, created);
    entry = created;
  }
  const shared = entry;
  shared.users += 1;
  const signal = options.signal;
  return new Promise((resolve, reject) => {
    let settled = false;
    const release = () => {
      settled = true;
      shared.users -= 1;
      signal?.removeEventListener("abort", stop);
    };
    function stop() {
      if (settled) return;
      release();
      if (shared.users <= 0) {
        shared.controller.abort();
        if (inflight.get(id) === shared) inflight.delete(id);
      }
      reject(new DOMException("The read was stopped.", "AbortError"));
    }
    if (signal?.aborted) return stop();
    signal?.addEventListener("abort", stop);
    shared.promise.then(
      (value) => {
        if (settled) return;
        release();
        resolve(value);
      },
      (error) => {
        if (settled) return;
        release();
        reject(error);
      }
    );
  });
}
function useConnectionReads(connection) {
  const key = connectionKey(connection);
  const label = connectionLabel(connection);
  const latest = (0, import_react.useRef)({ key, label, connection });
  latest.current = { key, label, connection };
  const current = (0, import_react.useRef)(null);
  const seq = (0, import_react.useRef)(0);
  const [pending, setPending] = (0, import_react.useState)(null);
  const [failure, setFailure] = (0, import_react.useState)(null);
  const [done, setDone] = (0, import_react.useState)(null);
  const drop = (0, import_react.useCallback)((t) => {
    if (!t) return;
    if (current.current?.id === t.id) {
      current.current = null;
      setPending(null);
    }
    t.controller.abort();
  }, []);
  (0, import_react.useEffect)(() => {
    const t = current.current;
    if (t && t.key !== key) drop(t);
  }, [key, drop]);
  (0, import_react.useEffect)(() => () => drop(current.current), [drop]);
  const live = (0, import_react.useCallback)(
    (t) => current.current?.id === t.id && latest.current.key === t.key && latestGeneration(t.view) === t.gen,
    []
  );
  const begin = (0, import_react.useCallback)((params, message, force = false) => {
    const running = current.current;
    if (!force && running && running.key === latest.current.key && running.params === params) return null;
    const view = readView(params);
    const t = {
      id: ++seq.current,
      params,
      message,
      ...latest.current,
      view,
      gen: nextGeneration(view),
      controller: new AbortController()
    };
    running?.controller.abort();
    current.current = t;
    setPending(t);
    setFailure(null);
    return t;
  }, []);
  const finish = (0, import_react.useCallback)((t) => {
    if (current.current?.id !== t.id) return false;
    current.current = null;
    setPending(null);
    return true;
  }, []);
  const run = (0, import_react.useCallback)(
    async (params, message, request, accept, options = {}) => {
      const t = begin(params, message, options.force);
      if (!t) return null;
      try {
        viewReads.set(t.view, { key: t.key, params, request: (c, signal) => request(c, signal) });
        const result = await sharedRead(t.key, params, (signal) => request(t.connection, signal), {
          fresh: options.force,
          signal: t.controller.signal
        });
        if (!live(t)) return null;
        const b = { key: t.key, label: t.label, report: result };
        accept(b);
        setDone({ key: t.key, at: Date.now() });
        return b;
      } catch (e) {
        if (live(t)) {
          const text = e.message;
          setFailure({ key: t.key, message: text });
          options.onError?.(text);
        }
        return null;
      } finally {
        finish(t);
      }
    },
    [begin, finish, live]
  );
  return {
    /** connectionKey() of the connection in use. */
    key,
    label,
    run,
    begin,
    finish,
    live,
    /** The loading text of the read running for the connection in use. */
    loading: pending && pending.key === key ? pending.message : "",
    /** The last error of a read for the connection in use. */
    error: failure && failure.key === key ? failure.message : "",
    /** When the last read for the connection in use was shown (Date.now()), or 0. */
    doneAt: done && done.key === key ? done.at : 0,
    /** Stop waiting for the read running now: nothing is shown from it, the
     * view keeps its report, and the request is aborted unless Refresh all
     * waits for the same read. */
    stop: (0, import_react.useCallback)(() => drop(current.current), [drop]),
    clearError: (0, import_react.useCallback)(() => setFailure(null), []),
    /** connectionKey() of the connection in use now (after an await, not the render's). */
    currentKey: (0, import_react.useCallback)(() => latest.current.key, []),
    /** A read for the connection in use is running (automatic refresh skips a tick). */
    running: (0, import_react.useCallback)(() => !!current.current && current.current.key === latest.current.key, [])
  };
}

// src/components.tsx
var import_react3 = __toESM(require("react"), 1);

// src/kit.tsx
var import_react2 = require("react");
var import_react_dom = require("react-dom");
var import_jsx_runtime = require("react/jsx-runtime");
var focusScopes = [];
var FOCUSABLE = "button, [href], input, select, textarea, summary, [tabindex]";
function useFocusScope(ref, options) {
  const latest = (0, import_react2.useRef)(options);
  latest.current = options;
  (0, import_react2.useEffect)(() => {
    const root = ref.current;
    if (!options.open || !root) return;
    const opener = latest.current.returnFocus?.() || (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const dialog = root instanceof HTMLDialogElement ? root : null;
    if (dialog && !dialog.open) dialog.showModal();
    focusScopes.push(root);
    const visible = (el) => !el.matches(":disabled") && !el.closest('[hidden], [inert], [aria-hidden="true"]') && !!el.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
    const items = () => Array.from(root.querySelectorAll(latest.current.menu ? '[role="menuitem"]' : FOCUSABLE)).filter((el) => (latest.current.menu || el.tabIndex >= 0) && visible(el));
    const focusFirst = () => {
      const preferred = latest.current.initialFocus?.();
      (preferred && visible(preferred) ? preferred : items()[0] || root).focus({ preventScroll: true });
    };
    focusFirst();
    const frame = requestAnimationFrame(() => {
      if (focusScopes[focusScopes.length - 1] === root && (!root.contains(document.activeElement) || document.activeElement === root)) focusFirst();
    });
    const key = (event) => {
      if (focusScopes[focusScopes.length - 1] !== root) return;
      const native = document.activeElement?.closest("dialog[open]");
      if (native && native !== root && !native.contains(root)) return;
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopImmediatePropagation();
        latest.current.onClose();
        return;
      }
      const nodes = items();
      const at = nodes.indexOf(document.activeElement);
      if (latest.current.menu && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        event.stopImmediatePropagation();
        const next = event.key === "Home" ? 0 : event.key === "End" ? nodes.length - 1 : event.key === "ArrowDown" ? (at + 1) % nodes.length : at <= 0 ? nodes.length - 1 : at - 1;
        (nodes[next] || root).focus();
      } else if (event.key === "Tab") {
        if (!nodes.length || at < 0 || (event.shiftKey ? at === 0 : at === nodes.length - 1)) {
          event.preventDefault();
          (event.shiftKey ? nodes[nodes.length - 1] || root : nodes[0] || root).focus();
        }
        event.stopPropagation();
      }
    };
    document.addEventListener("keydown", key, true);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", key, true);
      const wasTop = focusScopes[focusScopes.length - 1] === root;
      const at = focusScopes.lastIndexOf(root);
      if (at >= 0) focusScopes.splice(at, 1);
      const focused = document.activeElement;
      const restore = wasTop && (root.contains(focused) || focused === document.body || !focused?.isConnected);
      if (dialog?.open) dialog.close();
      if (restore && opener?.isConnected && visible(opener)) opener.focus({ preventScroll: true });
    };
  }, [ref, options.open]);
}
var TONE_TEXT = {
  ok: "OK",
  watch: "Watch",
  problem: "Problem",
  info: "No threshold",
  unknown: "Unknown",
  off: "Off"
};
function StatusPill({ tone, label, title, quiet = false }) {
  const word = label || TONE_TEXT[tone];
  if (quiet && (tone === "ok" || tone === "info") && word === TONE_TEXT[tone]) {
    return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: `status-pill is-${tone} is-quiet`, title: title || word, children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "visually-hidden", children: word }) });
  }
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: `status-pill is-${tone}`, title, children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "status-dot", "aria-hidden": "true" }),
    label || TONE_TEXT[tone]
  ] });
}
var POPOVER_EVENT = "myflames-popover";
function Popover({
  label,
  trigger,
  className = "",
  buttonClassName = "info-button",
  wide,
  buttonTitle,
  disabled = false,
  menu = false,
  width: panelWidth,
  children
}) {
  const id = (0, import_react2.useId)();
  const [open, setOpen] = (0, import_react2.useState)(false);
  const [style, setStyle] = (0, import_react2.useState)({ visibility: "hidden" });
  const button = (0, import_react2.useRef)(null);
  const panel = (0, import_react2.useRef)(null);
  useFocusScope(panel, { open, onClose: () => setOpen(false), returnFocus: () => button.current, menu });
  (0, import_react2.useEffect)(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: id }));
    const other = (e) => {
      if (e.detail !== id) setOpen(false);
    };
    const outside = (e) => {
      const target = e.target;
      if (!panel.current?.contains(target) && !button.current?.contains(target)) setOpen(false);
    };
    window.addEventListener(POPOVER_EVENT, other);
    document.addEventListener("mousedown", outside);
    return () => {
      window.removeEventListener(POPOVER_EVENT, other);
      document.removeEventListener("mousedown", outside);
    };
  }, [open, id]);
  (0, import_react2.useEffect)(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  (0, import_react2.useLayoutEffect)(() => {
    if (!open) return;
    const place = () => {
      const b = button.current?.getBoundingClientRect();
      const p = panel.current;
      if (!b || !p) return;
      const width = Math.min(panelWidth || (wide ? 400 : 320), window.innerWidth - 16);
      const left = Math.max(8, Math.min(b.left - 12, window.innerWidth - width - 8));
      const height = p.offsetHeight;
      const footer = document.querySelector(".window-statusbar")?.getBoundingClientRect().top;
      const floor = footer !== void 0 && footer >= b.bottom ? Math.min(footer, window.innerHeight) : window.innerHeight;
      const below = b.bottom + 6;
      const top = below + height > floor - 8 && b.top - height - 6 > 8 ? b.top - height - 6 : below;
      const boundedTop = Math.max(8, Math.min(top, floor - Math.min(height, floor - 16) - 8));
      p.style.visibility = "visible";
      setStyle({ left, top: boundedTop, width, maxHeight: floor - boundedTop - 8 });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, wide, panelWidth]);
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: `popover-anchor ${className}`, children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
      "button",
      {
        ref: button,
        type: "button",
        className: buttonClassName,
        "aria-label": label,
        "aria-haspopup": menu ? "menu" : "dialog",
        disabled,
        "aria-expanded": open,
        "aria-controls": open ? id : void 0,
        title: trigger ? buttonTitle : label,
        onClick: () => {
          setStyle({ visibility: "hidden" });
          setOpen((o) => !o);
        },
        children: trigger || /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "info", size: 15 })
      }
    ),
    open && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { ref: panel, id, className: `popover${wide ? " is-wide" : ""}`, tabIndex: -1, role: menu ? "menu" : "dialog", "aria-label": label, style, children })
  ] });
}
function ExportMenu({ label, formats, disabled = false, className = "", title }) {
  const face = /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "export", size: 14 }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { children: "Export" })
  ] });
  if (formats.length === 1) {
    const only = formats[0];
    return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
      "button",
      {
        type: "button",
        className: `button export-button ${className}`,
        "aria-label": label,
        title: title || label,
        disabled: disabled || only.disabled,
        onClick: only.onSelect,
        children: face
      }
    );
  }
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
    Popover,
    {
      menu: true,
      width: 200,
      label,
      buttonTitle: title || label,
      disabled,
      className: `export-menu ${className}`,
      buttonClassName: "button export-button",
      trigger: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
        face,
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "chevron-down", size: 12 })
      ] }),
      children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "export-menu-items", children: formats.map((format) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
        "button",
        {
          type: "button",
          role: "menuitem",
          "data-format": format.key,
          disabled: format.disabled,
          onClick: () => {
            closePopovers();
            format.onSelect();
          },
          children: format.label
        },
        format.key
      )) })
    }
  );
}
function closePopovers() {
  window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: "" }));
}
function InfoPopover({ label, children }) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Popover, { label, children });
}
function NotesButton({ notes, label = "Notes about this read", title }) {
  if (!notes.length) return null;
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
    Popover,
    {
      label,
      buttonTitle: title,
      wide: true,
      className: "notes-anchor",
      buttonClassName: "notes-button",
      trigger: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "info", size: 14 }),
        notes.length,
        " ",
        notes.length === 1 ? "note" : "notes"
      ] }),
      children: [
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)("h3", { className: "popover-title", children: label }),
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", { className: "popover-notes", children: notes.map((n, i) => /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { children: n }, i)) })
      ]
    }
  );
}
function Segmented({
  label,
  value,
  options,
  onChange,
  className = "",
  tabs
}) {
  const group = (0, import_react2.useRef)(null);
  function onKey(event, index) {
    const enabled = options.map((o, i) => o.disabled ? -1 : i).filter((i) => i >= 0);
    const at = enabled.indexOf(index);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const next = step ? enabled[(at + step + enabled.length) % enabled.length] : tabs && event.key === "Home" ? enabled[0] : tabs && event.key === "End" ? enabled[enabled.length - 1] : void 0;
    if (next === void 0) return;
    event.preventDefault();
    onChange(options[next].value);
    group.current?.querySelectorAll("button")[next]?.focus();
  }
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { ref: group, className: `segmented ${className}`, role: tabs ? "tablist" : "group", "aria-label": label, children: options.map((o, i) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
    "button",
    {
      type: "button",
      ...tabs ? {
        role: "tab",
        id: `${tabs}-tab-${o.value}`,
        "aria-selected": o.value === value,
        "aria-controls": `${tabs}-panel-${o.value}`,
        tabIndex: o.value === value ? 0 : -1
      } : { "aria-pressed": o.value === value },
      disabled: o.disabled,
      title: o.title,
      "data-value": o.value,
      onClick: () => onChange(o.value),
      onKeyDown: (e) => onKey(e, i),
      children: [
        o.label,
        o.count !== void 0 && o.count !== null && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "segment-count", children: o.count.toLocaleString() })
      ]
    },
    o.value
  )) });
}
function IconButton({
  icon,
  label,
  size = 16,
  className = "",
  title,
  ...rest
}) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", { type: "button", className: `icon-button ${className}`.trim(), "aria-label": label, title: title ?? label, ...rest, children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: icon, size }) });
}
var ACTION_ELAPSED_AFTER_MS = 1e3;
function useSince(active) {
  const since = (0, import_react2.useRef)(null);
  if (active && since.current === null) since.current = Date.now();
  if (!active) since.current = null;
  return since.current;
}
function useElapsed(since) {
  const [now, setNow] = (0, import_react2.useState)(() => Date.now());
  (0, import_react2.useEffect)(() => {
    if (since == null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1e3);
    return () => window.clearInterval(timer);
  }, [since]);
  return since == null ? 0 : Math.max(0, Math.floor((now - since) / 1e3));
}
function ActionButton({
  busy = false,
  busyLabel,
  icon,
  primary,
  danger,
  small,
  className = "",
  children,
  onClick,
  labelClassName = "",
  ...rest
}) {
  const idle = typeof children === "string" ? children : void 0;
  const reserve = busy ? idle : busyLabel;
  const kind = primary ? " primary" : danger ? " danger" : "";
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
    "button",
    {
      type: "button",
      ...rest,
      className: `button action-button${kind}${small ? " small" : ""}${busy ? " is-pending" : ""}${icon ? " has-icon" : ""} ${className}`.trim(),
      "aria-busy": busy || void 0,
      "aria-disabled": busy || rest["aria-disabled"] || void 0,
      onClick: (e) => {
        if (busy || rest["aria-disabled"] === true || rest["aria-disabled"] === "true") {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      },
      children: [
        icon && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "action-button-icon", "aria-hidden": "true", children: busy ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "spinner" }) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: icon, size: 15 }) }),
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: `action-button-label${busy ? " is-busy" : ""} ${labelClassName}`.trim(), "data-reserve": reserve || void 0, children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { children: [
          !icon && busy && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "spinner", "aria-hidden": "true" }),
          busy && busyLabel ? busyLabel : children
        ] }) })
      ]
    }
  );
}
function restoreFocus(line, near) {
  if (!line || !line.isConnected) return;
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected) return;
  let scope = line.parentElement;
  for (let depth = 0; scope && depth < 3; depth++, scope = scope.parentElement) {
    const target = scope.querySelector(near);
    if (target && target.getClientRects().length) {
      target.focus({ preventScroll: true });
      return;
    }
  }
  line.focus({ preventScroll: true });
}
var WindowStatusContext = (0, import_react2.createContext)(null);
var ViewActivityContext = (0, import_react2.createContext)(null);
var ViewBackLabelContext = (0, import_react2.createContext)("Server");
var ViewSwitcherContext = (0, import_react2.createContext)(null);
function ViewSwitcher({ id, title, shared }) {
  const button = (0, import_react2.useRef)(null);
  const popover = (0, import_react2.useRef)(null);
  const open = shared.open && shared.active;
  const latest = (0, import_react2.useRef)(shared);
  latest.current = shared;
  const [maxHeight, setMaxHeight] = (0, import_react2.useState)(void 0);
  (0, import_react2.useLayoutEffect)(() => {
    if (!open) return;
    const measure = () => {
      const top = button.current?.getBoundingClientRect().bottom ?? 0;
      const footer = document.querySelector(".window-statusbar")?.getBoundingClientRect().height ?? 0;
      setMaxHeight(Math.max(160, Math.floor(window.innerHeight - top - footer - 16)));
    };
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [open]);
  useFocusScope(popover, {
    open,
    onClose: () => latest.current.setOpen(false),
    returnFocus: () => button.current,
    initialFocus: () => popover.current?.querySelector('[aria-current="page"]')
  });
  (0, import_react2.useEffect)(() => {
    if (!open) return;
    window.dispatchEvent(new CustomEvent(POPOVER_EVENT, { detail: `${id}-views` }));
    const other = (event) => {
      if (event.detail !== `${id}-views`) latest.current.setOpen(false);
    };
    const onDown = (event) => {
      const target = event.target;
      if (popover.current?.contains(target) || button.current?.contains(target)) return;
      latest.current.setOpen(false);
      requestAnimationFrame(() => {
        const focused = document.activeElement;
        if (!focused || focused === document.body || !focused.isConnected) button.current?.focus({ preventScroll: true });
      });
    };
    window.addEventListener(POPOVER_EVENT, other);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener(POPOVER_EVENT, other);
      document.removeEventListener("mousedown", onDown);
    };
  }, [open, id]);
  (0, import_react2.useEffect)(() => {
    if (!shared.active || !latest.current.takeFocus()) return;
    const focused = document.activeElement;
    if (!focused || focused === document.body || !focused.isConnected) button.current?.focus({ preventScroll: true });
  }, [shared.active]);
  const others = shared.others;
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("h2", { id: `${id}-title`, tabIndex: -1, className: "view-switcher-heading", children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
      "button",
      {
        ref: button,
        type: "button",
        id: `${id}-switcher`,
        className: `view-switcher${open ? " is-open" : ""}`,
        "aria-haspopup": "dialog",
        "aria-expanded": open,
        "aria-controls": open ? `${id}-views` : void 0,
        title: others ? `${title}: ${others.text}` : `Switch ${shared.label.replace(/ views$/, "")} view`,
        onClick: () => shared.setOpen(!open),
        onKeyDown: (event) => {
          if (event.key === "ArrowDown" && !open) {
            event.preventDefault();
            shared.setOpen(true);
          }
        },
        children: [
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { id: `${id}-title-name`, className: "view-switcher-name", children: title }),
          others && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: `status-dot is-${others.tone} view-switcher-marker`, "aria-hidden": "true" }),
          others && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: "visually-hidden", children: [
            ", ",
            others.text
          ] }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "view-switcher-chevron", "aria-hidden": "true", children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "chevron-down", size: 12 }) })
        ]
      }
    ) }),
    open && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
      "div",
      {
        ref: popover,
        id: `${id}-views`,
        role: "dialog",
        "aria-label": shared.label,
        tabIndex: -1,
        className: "view-switcher-popover",
        style: maxHeight ? { maxHeight: `${maxHeight}px` } : void 0,
        children: shared.list
      }
    )
  ] });
}
function moveRowFocus(event, items_selector = "[data-row-key]", scope_selector = "[data-rows]") {
  const keys = ["ArrowDown", "ArrowUp", "Home", "End"];
  const horizontal = event.currentTarget.dataset.rowAxis === "both";
  if (horizontal) keys.push("ArrowRight", "ArrowLeft");
  if (!keys.includes(event.key)) return null;
  const scope = event.currentTarget.closest(scope_selector);
  if (!scope) return null;
  const items = [...scope.querySelectorAll(items_selector)].filter(
    (e) => (
      // Rows inside a closed disclosure are laid out but not visible.
      typeof e.checkVisibility === "function" ? e.checkVisibility() : e.offsetParent !== null
    )
  );
  const at = items.indexOf(event.currentTarget);
  if (at < 0) return null;
  const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : event.key === "ArrowDown" || event.key === "ArrowRight" ? Math.min(items.length - 1, at + 1) : Math.max(0, at - 1);
  event.preventDefault();
  const target = items[next];
  target.focus();
  target.scrollIntoView({ block: "nearest" });
  return target;
}
var ColumnStoreContext = (0, import_react2.createContext)(null);
function GroupedList({
  id,
  title,
  count: count2,
  info,
  aside,
  footer,
  children,
  className = ""
}) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", { className: `grouped ${className}`, "aria-labelledby": id, children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { className: "grouped-head", children: [
      /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("h3", { id, children: [
        title,
        count2 !== void 0 && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "count", children: count2.toLocaleString() })
      ] }),
      info && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(InfoPopover, { label: `About ${typeof title === "string" ? title : "this group"}`, children: info }),
      aside && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "grouped-aside", children: aside })
    ] }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("ul", { className: "grouped-rows", children }),
    footer && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", { className: "grouped-footer", children: footer })
  ] });
}
function GroupedRow({
  rowKey,
  selection,
  tone,
  toneLabel,
  label,
  detail,
  value,
  secondary,
  attrs,
  depth = 0,
  wrap = false,
  className = ""
}) {
  const selected = !!selection && selection.key === rowKey;
  const body = /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
    tone && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(StatusPill, { tone, label: toneLabel, quiet: true }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: "row-label", children: [
      /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "row-title", title: typeof label === "string" ? label : void 0, children: label }),
      detail && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "row-detail", children: detail })
    ] }),
    (value !== void 0 || secondary) && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: "row-value", children: [
      value,
      secondary && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "row-secondary", children: secondary })
    ] }),
    selection && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "disclosure", size: 14 })
  ] });
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("li", { className: `grouped-item${depth ? ` depth-${Math.min(depth, 3)}` : ""} ${className}`, ...attrs || {}, children: selection ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
    "button",
    {
      type: "button",
      className: `grouped-row${selected ? " is-selected" : ""}${wrap ? " is-wrapping" : ""}`,
      "data-row-key": rowKey,
      "aria-current": selected ? "true" : void 0,
      onClick: () => selection.select(rowKey, true),
      onKeyDown: (e) => {
        const next = moveRowFocus(e, "button.grouped-row[data-row-key]", ".view-main, [data-rows]");
        if (next?.dataset.rowKey) selection.select(next.dataset.rowKey, false);
      },
      children: body
    }
  ) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: `grouped-row is-static${wrap ? " is-wrapping" : ""}`, children: body }) });
}
function PrerequisiteGate({ children, action }) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "gate", role: "note", children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "warning", size: 18 }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", { children }),
    action && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "gate-action", children: action })
  ] });
}
function EmptyState({
  icon,
  title,
  children,
  action
}) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "view-empty", children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: icon, size: 22 }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("h3", { children: title }),
    children && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", { children }),
    action
  ] });
}
function SummaryLine({ tone, toneLabel, children, notes, notesTitle, extra }) {
  const text = (0, import_react2.useRef)(null);
  (0, import_react2.useLayoutEffect)(() => {
    const el = text.current;
    if (!el) return;
    if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1) el.title = (el.textContent || "").trim();
    else el.removeAttribute("title");
  });
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "summary-line", children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { children: [
      tone && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(StatusPill, { tone, label: toneLabel }),
      /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "summary-text", ref: text, children })
    ] }),
    extra,
    notes && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(NotesButton, { notes, title: notesTitle })
  ] });
}
function FactStrip({ label, facts }) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dl", { className: "fact-strip", "aria-label": label, children: facts.map(([dt, dd]) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dt", { children: dt }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dd", { children: dd })
  ] }, dt)) });
}
function Disclosure({
  title,
  count: count2,
  open,
  onToggle,
  children,
  className = "",
  id
}) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
    "details",
    {
      id,
      className: `disclosure ${className}`,
      open,
      onToggle: onToggle ? (e) => onToggle(e.currentTarget.open) : void 0,
      children: [
        /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("summary", { children: [
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "disclosure", size: 14 }),
          title,
          count2 !== void 0 && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "count", children: count2.toLocaleString() })
        ] }),
        /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "disclosure-body", children })
      ]
    }
  );
}
var INSPECTOR_MIN = 280;
var INSPECTOR_MAX = 560;
var INSPECTOR_DEFAULT = 340;
var InspectorContext = (0, import_react2.createContext)(null);
function useInspector() {
  return (0, import_react2.useContext)(InspectorContext);
}
function InspectorSection({ title, children, info }) {
  const id = (0, import_react2.useId)();
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("section", { className: "inspector-block", "aria-labelledby": id, children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("h4", { id, children: [
      title,
      info && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(InfoPopover, { label: `About ${title}`, children: info })
    ] }),
    children
  ] });
}
var IDENTIFIER = /^[A-Za-z_$][\w$]*(?:\.[\w$*]+)*$/;
function isIdentifier(text) {
  return typeof text === "string" && /[_.]/.test(text) && IDENTIFIER.test(text);
}
function nodeText(node) {
  return import_react2.Children.toArray(node).map((child) => (0, import_react2.isValidElement)(child) ? nodeText(child.props.children) : typeof child === "string" || typeof child === "number" ? String(child) : "").join("");
}
function Fields({ label, rows }) {
  const shown = rows.filter(Boolean);
  if (!shown.length) return null;
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dl", { className: "inspector-fields", "aria-label": label, children: shown.map(([dt, dd], i) => /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dt", { className: (isIdentifier(dt) ? dt.length > 20 : (0, import_react2.isValidElement)(dt) && dt.type === "code" && nodeText(dt.props.children).length > 20) ? "is-long-label" : void 0, children: isIdentifier(dt) ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)("code", { children: dt }) : dt }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("dd", { children: dd })
  ] }, i)) });
}
function Inspector({
  content,
  empty,
  onClose,
  detailBackLabel
}) {
  const shared = useInspector();
  const titleId = (0, import_react2.useId)();
  const body = (0, import_react2.useRef)(null);
  const root = (0, import_react2.useRef)(null);
  const sheet = !detailBackLabel && !!shared?.sheet;
  const width = shared?.width ?? INSPECTOR_DEFAULT;
  (0, import_react2.useEffect)(() => {
    body.current?.scrollTo?.({ top: 0 });
    if (detailBackLabel) root.current?.querySelector(".detail-back")?.focus();
  }, [content?.key]);
  useFocusScope(root, { open: sheet, onClose });
  const drag = (0, import_react2.useRef)(null);
  const latest = (0, import_react2.useRef)(width);
  latest.current = width;
  function resize(next) {
    const max = Math.min(INSPECTOR_MAX, Math.max(INSPECTOR_MIN, window.innerWidth - 640));
    const value = Math.max(INSPECTOR_MIN, Math.min(max, Math.round(next)));
    latest.current = value;
    shared?.setWidth(value);
  }
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(import_jsx_runtime.Fragment, { children: [
    sheet && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "sheet-scrim", "aria-hidden": "true", onClick: onClose }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
      "aside",
      {
        ref: root,
        className: `view-inspector${detailBackLabel ? " is-detail" : sheet ? " is-sheet" : ""}`,
        "aria-labelledby": titleId,
        role: detailBackLabel ? "region" : sheet ? "dialog" : "complementary",
        "aria-modal": sheet ? true : void 0,
        style: sheet || detailBackLabel ? void 0 : { width },
        tabIndex: sheet ? -1 : void 0,
        children: [
          !sheet && !detailBackLabel && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
            "div",
            {
              className: "inspector-resize",
              role: "separator",
              "aria-orientation": "vertical",
              "aria-label": "Inspector width",
              "aria-valuemin": INSPECTOR_MIN,
              "aria-valuemax": INSPECTOR_MAX,
              "aria-valuenow": width,
              tabIndex: 0,
              onPointerDown: (e) => {
                drag.current = { x: e.clientX, width };
                e.currentTarget.setPointerCapture(e.pointerId);
              },
              onPointerMove: (e) => {
                if (drag.current) resize(drag.current.width + drag.current.x - e.clientX);
              },
              onPointerUp: (e) => {
                if (drag.current) resize(drag.current.width + drag.current.x - e.clientX);
                drag.current = null;
              },
              onPointerCancel: () => {
                drag.current = null;
              },
              onLostPointerCapture: () => {
                drag.current = null;
              },
              onKeyDown: (e) => {
                const step = e.shiftKey ? 64 : 16;
                if (e.key === "ArrowLeft") resize(latest.current + step);
                else if (e.key === "ArrowRight") resize(latest.current - step);
                else if (e.key === "Home") resize(INSPECTOR_MIN);
                else if (e.key === "End") resize(INSPECTOR_MAX);
                else return;
                e.preventDefault();
              }
            }
          ),
          /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { className: "inspector-head", children: [
            detailBackLabel && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("button", { type: "button", className: "button ghost detail-back inspector-close", onClick: onClose, children: [
              /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "back", size: 14 }),
              detailBackLabel
            ] }),
            /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "inspector-title", children: [
              /* @__PURE__ */ (0, import_jsx_runtime.jsx)("h3", { id: titleId, children: content ? content.title : "Inspector" }),
              content?.meta && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "inspector-meta", children: content.meta })
            ] }),
            !detailBackLabel && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(IconButton, { icon: "close", size: 15, className: "inspector-close", label: "Close inspector", title: "Close inspector (Escape)", onClick: onClose })
          ] }),
          content?.actions && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "inspector-actions-bar", children: content.actions }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "inspector-scroll", ref: body, children: content ? content.body : /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "inspector-placeholder", children: empty }) })
        ]
      }
    )
  ] });
}
function ViewLayout({
  id,
  title,
  hideTitle = false,
  info,
  controls,
  onRefresh,
  refreshDisabled,
  loading,
  onBack,
  inspector,
  inspectorEmpty,
  detailBackLabel = `Back to ${title.toLocaleLowerCase()}`,
  status,
  children,
  className = "",
  otherConnection,
  otherAction
}) {
  const reportActivity = (0, import_react2.useContext)(ViewActivityContext);
  const backLabel = (0, import_react2.useContext)(ViewBackLabelContext);
  const offered = (0, import_react2.useContext)(ViewSwitcherContext);
  const switcher = offered && !onBack && !hideTitle ? offered : null;
  (0, import_react2.useEffect)(() => {
    reportActivity?.(id, !!loading);
    return () => reportActivity?.(id, false);
  }, [reportActivity, id, loading]);
  const shared = useInspector();
  const main = (0, import_react2.useRef)(null);
  const open = !!shared?.open && (!shared.sheet && !detailBackLabel || !!inspector);
  const detailOpen = !!detailBackLabel && open;
  function close() {
    shared?.setOpen(false);
    window.setTimeout(() => {
      const split = main.current?.parentElement;
      if (!split || split.querySelector(".view-inspector")) return;
      const active = document.activeElement;
      if (active && active !== document.body && split.contains(active)) return;
      const row = main.current?.querySelector('.is-selected[data-row-key], [aria-current="true"][data-row-key]');
      row?.focus({ preventScroll: false });
    });
  }
  return /* @__PURE__ */ (0, import_jsx_runtime.jsx)(ViewSwitcherContext.Provider, { value: null, children: /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
    "section",
    {
      className: `server-view ${className}${otherConnection ? " is-other-connection" : ""}`,
      "aria-labelledby": switcher ? `${id}-title-name` : `${id}-title`,
      "data-view": id,
      "data-other-connection": otherConnection ? "true" : void 0,
      onKeyDown: (e) => {
        if (e.target.closest?.("dialog")) return;
        if (e.key === "Escape" && open && !e.defaultPrevented) {
          e.preventDefault();
          close();
        }
      },
      children: [
        /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("header", { className: "view-toolbar", children: [
          onBack && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("button", { type: "button", className: "server-back", onClick: onBack, children: [
            /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "back", size: 14 }),
            backLabel
          ] }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: `view-title${switcher ? " has-switcher" : ""}`, children: [
            switcher ? /* @__PURE__ */ (0, import_jsx_runtime.jsx)(ViewSwitcher, { id, title, shared: switcher }) : /* @__PURE__ */ (0, import_jsx_runtime.jsx)("h2", { id: `${id}-title`, tabIndex: -1, className: hideTitle ? "visually-hidden" : void 0, children: title }),
            info && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(InfoPopover, { label: `About ${title}`, children: info })
          ] }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)("div", { className: "toolbar-controls", hidden: detailOpen, children: controls }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "toolbar-end", children: [
            onRefresh && // While it reads: a spinner and "Refreshing…", aria-busy, clicks
            // ignored, and focus stays on it (aria-disabled, not disabled).
            /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
              ActionButton,
              {
                className: "toolbar-refresh",
                labelClassName: "toolbar-refresh-label",
                "aria-label": "Refresh",
                "data-action": "refresh",
                icon: "refresh",
                busy: !!loading,
                busyLabel: "Refreshing\u2026",
                disabled: !loading && !!refreshDisabled,
                onClick: onRefresh,
                children: "Refresh"
              }
            ),
            shared && !shared.sheet && !detailBackLabel && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
              IconButton,
              {
                icon: "inspector",
                className: "inspector-toggle",
                "aria-pressed": shared.open,
                label: shared.open ? "Hide inspector" : "Show inspector",
                title: `${shared.open ? "Hide" : "Show"} inspector (\u2325\u2318I)`,
                onClick: () => shared.open ? close() : shared.setOpen(true)
              }
            )
          ] })
        ] }),
        status,
        detailOpen && otherConnection && (otherAction ? /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "gate other-connection", role: "note", children: [
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "connection", size: 18 }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { children: [
            "From ",
            otherConnection,
            ". ",
            otherAction.label,
            " to load this server."
          ] }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "gate-action", children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", { type: "button", className: "button", disabled: otherAction.disabled || loading, onClick: otherAction.onClick, children: otherAction.label }) })
        ] }) : /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "gate other-connection", role: "note", children: [
          /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "connection", size: 18 }),
          /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { children: [
            "From ",
            otherConnection,
            ", refresh to load this server."
          ] })
        ] })),
        /* @__PURE__ */ (0, import_jsx_runtime.jsxs)(
          "div",
          {
            className: `view-split${open && !shared?.sheet && !detailOpen ? " has-inspector" : ""}`,
            style: open && !shared?.sheet && !detailOpen ? { "--inspector-w": `${shared?.width ?? INSPECTOR_DEFAULT}px` } : void 0,
            children: [
              /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "view-main", ref: main, hidden: detailOpen, children: [
                otherConnection && otherAction ? /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "gate other-connection", role: "note", children: [
                  /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "connection", size: 18 }),
                  /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { children: [
                    "From ",
                    otherConnection,
                    ". ",
                    otherAction.label,
                    " to load this server."
                  ] }),
                  /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "gate-action", children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", { type: "button", className: "button", disabled: otherAction.disabled || loading, onClick: otherAction.onClick, children: otherAction.label }) })
                ] }) : otherConnection && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: "gate other-connection", role: "note", children: [
                  /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "connection", size: 18 }),
                  /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { children: [
                    "From ",
                    otherConnection,
                    ", refresh to load this server."
                  ] }),
                  onRefresh && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "gate-action", children: /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", { type: "button", className: "button", disabled: refreshDisabled || loading, onClick: onRefresh, children: "Refresh" }) })
                ] }),
                children
              ] }),
              open && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
                Inspector,
                {
                  content: inspector || null,
                  empty: inspectorEmpty || /* @__PURE__ */ (0, import_jsx_runtime.jsx)("p", { children: "Select a row to see its details here." }),
                  onClose: close,
                  detailBackLabel
                }
              )
            ]
          }
        )
      ]
    }
  ) });
}
function ViewStatus({
  loading,
  error,
  children,
  onStop,
  onRetry,
  doneAt,
  startedAt,
  elapsed: countSeconds = true,
  slowAfter = 2e4,
  slowText = "Still waiting for the server."
}) {
  const since = useSince(!!loading);
  const seconds = useElapsed(loading ? startedAt ?? since : null);
  const slow = !!loading && seconds * 1e3 >= slowAfter;
  const root = (0, import_react2.useRef)(null);
  const refocus = () => window.requestAnimationFrame(() => restoreFocus(root.current, "[data-action=refresh]:not(:disabled)"));
  const [announce, setAnnounce] = (0, import_react2.useState)("");
  const previous = (0, import_react2.useRef)({ loading, doneAt });
  const stopped = (0, import_react2.useRef)(false);
  (0, import_react2.useEffect)(() => {
    const was = previous.current;
    previous.current = { loading, doneAt };
    if (loading && loading !== was.loading) {
      stopped.current = false;
      setAnnounce(loading);
    } else if (doneAt && doneAt !== was.doneAt) setAnnounce(`Updated at ${new Date(doneAt).toLocaleTimeString()}.`);
    else if (!loading && was.loading && !stopped.current) setAnnounce("");
  }, [loading, doneAt]);
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { ref: root, tabIndex: -1, className: `view-status${!loading && !error ? " is-idle" : ""}`, children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "visually-hidden", role: "status", "aria-live": "polite", children: announce }),
    loading && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { className: "capture-status", children: [
      /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "spinner", "aria-hidden": "true" }),
      " ",
      loading,
      countSeconds && seconds * 1e3 >= ACTION_ELAPSED_AFTER_MS && /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("span", { className: "action-elapsed", "aria-hidden": "true", children: [
        seconds,
        " s so far"
      ] }),
      slow && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("span", { className: "hint", children: slowText }),
      slow && onStop && /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
        "button",
        {
          type: "button",
          className: "text-button view-status-stop",
          onClick: () => {
            stopped.current = true;
            setAnnounce("Stopped waiting. The view keeps what it showed before.");
            onStop();
            refocus();
          },
          children: "Stop"
        }
      )
    ] }),
    error && // The alert holds the error alone; Try again sits beside it in the same box.
    /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("div", { className: `error view-status-error${onRetry ? " has-retry" : ""}`, children: [
      /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("p", { role: "alert", children: [
        error,
        children
      ] }),
      onRetry && /* @__PURE__ */ (0, import_jsx_runtime.jsx)("button", { type: "button", className: "text-button view-status-retry", onClick: () => {
        onRetry();
        refocus();
      }, children: "Try again" })
    ] })
  ] });
}
function SearchField({
  label,
  value,
  onChange,
  placeholder,
  id
}) {
  return /* @__PURE__ */ (0, import_jsx_runtime.jsxs)("label", { className: "toolbar-search", children: [
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(Icon, { name: "search", size: 14 }),
    /* @__PURE__ */ (0, import_jsx_runtime.jsx)(
      "input",
      {
        id,
        type: "search",
        "aria-label": label,
        value,
        placeholder: placeholder || label,
        spellCheck: false,
        autoCapitalize: "off",
        onChange: (e) => onChange(e.target.value)
      }
    )
  ] });
}

// src/icons.generated.ts
var icons = {
  "home": [["path", { "d": "M3 11.9896V14.5C3 17.7998 3 19.4497 4.02513 20.4749C5.05025 21.5 6.70017 21.5 10 21.5H14C17.2998 21.5 18.9497 21.5 19.9749 20.4749C21 19.4497 21 17.7998 21 14.5V11.9896C21 10.3083 21 9.46773 20.6441 8.74005C20.2882 8.01237 19.6247 7.49628 18.2976 6.46411L16.2976 4.90855C14.2331 3.30285 13.2009 2.5 12 2.5C10.7991 2.5 9.76689 3.30285 7.70242 4.90855L5.70241 6.46411C4.37533 7.49628 3.71179 8.01237 3.3559 8.74005C3 9.46773 3 10.3083 3 11.9896Z" }]],
  // Home01
  "plan": [["path", { "d": "M9.89128 7.55559C9.34109 7.8715 7.89853 8.51654 8.77715 9.32371C9.20634 9.71801 9.68436 10 10.2853 10H13.7147C14.3156 10 14.7937 9.71801 15.2229 9.32371C16.1015 8.51654 14.6589 7.8715 14.1087 7.55559C12.8185 6.8148 11.1815 6.8148 9.89128 7.55559Z" }], ["path", { "d": "M13.5 3.5C13.5 4.32843 12.8284 5 12 5C11.1716 5 10.5 4.32843 10.5 3.5C10.5 2.67157 11.1716 2 12 2C12.8284 2 13.5 2.67157 13.5 3.5Z" }], ["path", { "d": "M16.3913 19.5556C15.8411 19.8715 14.3985 20.5165 15.2771 21.3237C15.7063 21.718 16.1844 22 16.7853 22H20.2147C20.8156 22 21.2937 21.718 21.7229 21.3237C22.6015 20.5165 21.1589 19.8715 20.6087 19.5556C19.3185 18.8148 17.6815 18.8148 16.3913 19.5556Z" }], ["path", { "d": "M20 15.5C20 16.3284 19.3284 17 18.5 17C17.6716 17 17 16.3284 17 15.5C17 14.6716 17.6716 14 18.5 14C19.3284 14 20 14.6716 20 15.5Z" }], ["path", { "d": "M3.39128 19.5556C2.84109 19.8715 1.39853 20.5165 2.27715 21.3237C2.70634 21.718 3.18436 22 3.78534 22H7.21466C7.81564 22 8.29366 21.718 8.72285 21.3237C9.60147 20.5165 8.15891 19.8715 7.60872 19.5556C6.31854 18.8148 4.68146 18.8148 3.39128 19.5556Z" }], ["path", { "d": "M7 15.5C7 16.3284 6.32843 17 5.5 17C4.67157 17 4 16.3284 4 15.5C4 14.6716 4.67157 14 5.5 14C6.32843 14 7 14.6716 7 15.5Z" }], ["path", { "d": "M12 13V15.5M12 15.5L14.5 17M12 15.5L9.5 17" }]],
  // Hierarchy
  "query": [["path", { "d": "M17 8L18.8398 9.85008C19.6133 10.6279 20 11.0168 20 11.5C20 11.9832 19.6133 12.3721 18.8398 13.1499L17 15" }], ["path", { "d": "M7 8L5.16019 9.85008C4.38673 10.6279 4 11.0168 4 11.5C4 11.9832 4.38673 12.3721 5.16019 13.1499L7 15" }], ["path", { "d": "M14.5 4L9.5 20" }]],
  // SourceCode
  "server": [["path", { "d": "M19 4H5C4.06812 4 3.60218 4 3.23463 4.15224C2.74458 4.35523 2.35523 4.74458 2.15224 5.23463C2 5.60218 2 6.06812 2 7C2 7.93188 2 8.39782 2.15224 8.76537C2.35523 9.25542 2.74458 9.64477 3.23463 9.84776C3.60218 10 4.06812 10 5 10H19C19.9319 10 20.3978 10 20.7654 9.84776C21.2554 9.64477 21.6448 9.25542 21.8478 8.76537C22 8.39782 22 7.93188 22 7C22 6.06812 22 5.60218 21.8478 5.23463C21.6448 4.74458 21.2554 4.35523 20.7654 4.15224C20.3978 4 19.9319 4 19 4Z" }], ["path", { "d": "M19 14H5C4.06812 14 3.60218 14 3.23463 14.1522C2.74458 14.3552 2.35523 14.7446 2.15224 15.2346C2 15.6022 2 16.0681 2 17C2 17.9319 2 18.3978 2.15224 18.7654C2.35523 19.2554 2.74458 19.6448 3.23463 19.8478C3.60218 20 4.06812 20 5 20H19C19.9319 20 20.3978 20 20.7654 19.8478C21.2554 19.6448 21.6448 19.2554 21.8478 18.7654C22 18.3978 22 17.9319 22 17C22 16.0681 22 15.6022 21.8478 15.2346C21.6448 14.7446 21.2554 14.3552 20.7654 14.1522C20.3978 14 19.9319 14 19 14Z" }], ["path", { "d": "M6.125 7H6M6.25 7C6.25 7.13807 6.13807 7.25 6 7.25C5.86193 7.25 5.75 7.13807 5.75 7C5.75 6.86193 5.86193 6.75 6 6.75C6.13807 6.75 6.25 6.86193 6.25 7Z" }], ["path", { "d": "M10.125 7H10M10.25 7C10.25 7.13807 10.1381 7.25 10 7.25C9.86193 7.25 9.75 7.13807 9.75 7C9.75 6.86193 9.86193 6.75 10 6.75C10.1381 6.75 10.25 6.86193 10.25 7Z" }], ["path", { "d": "M6.125 17H6M6.25 17C6.25 17.1381 6.13807 17.25 6 17.25C5.86193 17.25 5.75 17.1381 5.75 17C5.75 16.8619 5.86193 16.75 6 16.75C6.13807 16.75 6.25 16.8619 6.25 17Z" }], ["path", { "d": "M10.125 17H10M10.25 17C10.25 17.1381 10.1381 17.25 10 17.25C9.86193 17.25 9.75 17.1381 9.75 17C9.75 16.8619 9.86193 16.75 10 16.75C10.1381 16.75 10.25 16.8619 10.25 17Z" }]],
  // ServerStack01
  "database": [["ellipse", { "cx": "12", "cy": "5", "rx": "8", "ry": "3" }], ["path", { "d": "M7 10.842C7.60158 11.0229 8.27434 11.1718 9 11.282" }], ["path", { "d": "M20 12C20 13.6569 16.4183 15 12 15C7.58172 15 4 13.6569 4 12" }], ["path", { "d": "M7 17.842C7.60158 18.0229 8.27434 18.1718 9 18.282" }], ["path", { "d": "M20 5V19C20 20.6569 16.4183 22 12 22C7.58172 22 4 20.6569 4 19V5" }]],
  // Database
  "investigation": [["path", { "d": "M8 7H16.75C18.8567 7 19.91 7 20.6667 7.50559C20.9943 7.72447 21.2755 8.00572 21.4944 8.33329C22 9.08996 22 10.1433 22 12.25C22 15.7612 22 17.5167 21.1573 18.7779C20.7926 19.3238 20.3238 19.7926 19.7779 20.1573C18.5167 21 16.7612 21 13.25 21H12C7.28595 21 4.92893 21 3.46447 19.5355C2 18.0711 2 15.714 2 11V7.94427C2 6.1278 2 5.21956 2.38032 4.53806C2.65142 4.05227 3.05227 3.65142 3.53806 3.38032C4.21956 3 5.1278 3 6.94427 3C8.10802 3 8.6899 3 9.19926 3.19101C10.3622 3.62712 10.8418 4.68358 11.3666 5.73313L12 7" }]],
  // Folder01
  "learn": [["path", { "d": "M7.99978 3.5H6.60021C4.43183 3.5 3.34764 3.5 2.67399 4.17362C2.00034 4.84724 2.00029 5.93144 2.00021 8.09982L2 13.3998C1.99992 15.5684 1.99987 16.6526 2.67353 17.3263C3.34719 18 4.43146 18 6.6 18H8.95042C10.4329 18 11.7092 19.0464 11.9999 20.5V5.5C11.0556 4.24097 9.99989 3.5 7.99978 3.5Z" }], ["path", { "d": "M16.0001 3.5H17.3997C19.5681 3.5 20.6523 3.5 21.3259 4.17362C21.9996 4.84724 21.9996 5.93144 21.9997 8.09982L21.9999 13.3998C22 15.5684 22 16.6526 21.3264 17.3263C20.6527 18 19.5684 18 17.3999 18H15.0495C13.567 18 12.2907 19.0464 12 20.5V5.5C12.9443 4.24097 14 3.5 16.0001 3.5Z" }]],
  // BookOpen01
  "teach": [["path", { "d": "M1.99805 7.99928C1.99805 9.34126 10.0943 13 11.9857 13C13.8772 13 21.9734 9.34126 21.9734 7.99928C21.9734 6.6573 13.8772 2.99854 11.9857 2.99854C10.0943 2.99854 1.99805 6.6573 1.99805 7.99928Z" }], ["path", { "d": "M5.99219 11L7.24348 16.8002C7.32919 17.1975 7.52703 17.5687 7.85696 17.8054C10.0787 19.3998 13.8908 19.3998 16.1126 17.8054C16.4426 17.5687 16.6404 17.1975 16.7261 16.8002L17.9774 11" }], ["path", { "d": "M20.4774 9.49951V16.5006M20.4774 16.5006C19.6864 17.9471 19.3366 18.7221 18.9813 20.0011C18.9042 20.4562 18.9654 20.6855 19.2786 20.8891C19.4059 20.9718 19.5588 21.0012 19.7104 21.0012H21.229C21.3904 21.0012 21.5533 20.9675 21.6863 20.8757C21.9774 20.6747 22.0523 20.4541 21.9734 20.0011C21.662 18.8135 21.2653 18.0016 20.4774 16.5006Z" }]],
  // Mortarboard01
  "connection": [["path", { "d": "M15.5 2V6M8.5 6V2" }], ["path", { "d": "M6.00446 7.61331C5.93719 6.74273 6.63957 6 7.53014 6H16.4699C17.3604 6 18.0628 6.74273 17.9955 7.61331L17.8117 9.99197C17.6796 11.7019 17.1011 13.3498 16.132 14.7773L15.5312 15.6622C14.9638 16.4979 14.0077 17 12.9838 17H11.0162C9.99228 17 9.03617 16.4979 8.46881 15.6622L7.86803 14.7773C6.89885 13.3498 6.32041 11.7019 6.18827 9.99197L6.00446 7.61331Z" }], ["path", { "d": "M12 17V22" }], ["path", { "d": "M11 9H13" }]],
  // Plug01
  "key": [["path", { "d": "M15.5 14.5C18.8137 14.5 21.5 11.8137 21.5 8.5C21.5 5.18629 18.8137 2.5 15.5 2.5C12.1863 2.5 9.5 5.18629 9.5 8.5C9.5 9.38041 9.68962 10.2165 10.0303 10.9697L2.5 18.5V21.5H5.5V19.5H7.5V17.5H9.5L13.0303 13.9697C13.7835 14.3104 14.6196 14.5 15.5 14.5Z" }], ["path", { "d": "M17.5 6.5L16.5 7.5" }]],
  // Key01
  "settings": [["path", { "d": "M15.5 12C15.5 13.933 13.933 15.5 12 15.5C10.067 15.5 8.5 13.933 8.5 12C8.5 10.067 10.067 8.5 12 8.5C13.933 8.5 15.5 10.067 15.5 12Z" }], ["path", { "d": "M21.011 14.0965C21.5329 13.9558 21.7939 13.8854 21.8969 13.7508C22 13.6163 22 13.3998 22 12.9669V11.0332C22 10.6003 22 10.3838 21.8969 10.2493C21.7938 10.1147 21.5329 10.0443 21.011 9.90358C19.0606 9.37759 17.8399 7.33851 18.3433 5.40087C18.4817 4.86799 18.5509 4.60156 18.4848 4.44529C18.4187 4.28902 18.2291 4.18134 17.8497 3.96596L16.125 2.98673C15.7528 2.77539 15.5667 2.66972 15.3997 2.69222C15.2326 2.71472 15.0442 2.90273 14.6672 3.27873C13.208 4.73448 10.7936 4.73442 9.33434 3.27864C8.95743 2.90263 8.76898 2.71463 8.60193 2.69212C8.43489 2.66962 8.24877 2.77529 7.87653 2.98663L6.15184 3.96587C5.77253 4.18123 5.58287 4.28891 5.51678 4.44515C5.45068 4.6014 5.51987 4.86787 5.65825 5.4008C6.16137 7.3385 4.93972 9.37763 2.98902 9.9036C2.46712 10.0443 2.20617 10.1147 2.10308 10.2492C2 10.3838 2 10.6003 2 11.0332V12.9669C2 13.3998 2 13.6163 2.10308 13.7508C2.20615 13.8854 2.46711 13.9558 2.98902 14.0965C4.9394 14.6225 6.16008 16.6616 5.65672 18.5992C5.51829 19.1321 5.44907 19.3985 5.51516 19.5548C5.58126 19.7111 5.77092 19.8188 6.15025 20.0341L7.87495 21.0134C8.24721 21.2247 8.43334 21.3304 8.6004 21.3079C8.76746 21.2854 8.95588 21.0973 9.33271 20.7213C10.7927 19.2644 13.2088 19.2643 14.6689 20.7212C15.0457 21.0973 15.2341 21.2853 15.4012 21.3078C15.5682 21.3303 15.7544 21.2246 16.1266 21.0133L17.8513 20.034C18.2307 19.8187 18.4204 19.711 18.4864 19.5547C18.5525 19.3984 18.4833 19.132 18.3448 18.5991C17.8412 16.6616 19.0609 14.6226 21.011 14.0965Z" }]],
  // Settings02
  "menu": [["path", { "d": "M4 5L20 5" }], ["path", { "d": "M4 12L20 12" }], ["path", { "d": "M4 19L20 19" }]],
  // Menu01
  "more": [["path", { "d": "M6.00449 12.5V12M18.0045 12.5V12M12.0045 12.5V12M7.00449 12.5C7.00449 11.9477 6.55677 11.5 6.00449 11.5C5.4522 11.5 5.00449 11.9477 5.00449 12.5C5.00449 13.0523 5.4522 13.5 6.00449 13.5C6.55677 13.5 7.00449 13.0523 7.00449 12.5ZM19.0045 12.5C19.0045 11.9477 18.5568 11.5 18.0045 11.5C17.4522 11.5 17.0045 11.9477 17.0045 12.5C17.0045 13.0523 17.4522 13.5 18.0045 13.5C18.5568 13.5 19.0045 13.0523 19.0045 12.5ZM13.0045 12.5C13.0045 11.9477 12.5568 11.5 12.0045 11.5C11.4522 11.5 11.0045 11.9477 11.0045 12.5C11.0045 13.0523 11.4522 13.5 12.0045 13.5C12.5568 13.5 13.0045 13.0523 13.0045 12.5Z" }]],
  // MoreHorizontal
  "import": [["path", { "d": "M2.99969 17.0002C2.99969 17.9302 2.99969 18.3952 3.10192 18.7767C3.37932 19.8119 4.18796 20.6206 5.22324 20.898C5.60474 21.0002 6.06972 21.0002 6.99969 21.0002L16.9997 21.0002C17.9297 21.0002 18.3947 21.0002 18.7762 20.898C19.8114 20.6206 20.6201 19.8119 20.8975 18.7767C20.9997 18.3952 20.9997 17.9302 20.9997 17.0002" }], ["path", { "d": "M16.4998 11.5002C16.4998 11.5002 13.1856 16.0002 11.9997 16.0002C10.8139 16.0002 7.49976 11.5002 7.49976 11.5002M11.9997 15.0002V3.00016" }]],
  // Download01
  "export": [["path", { "d": "M2.99994 17C2.99994 17.93 2.99994 18.395 3.10216 18.7765C3.37956 19.8117 4.18821 20.6204 5.22348 20.8978C5.60498 21 6.06997 21 6.99994 21L16.9999 21C17.9299 21 18.3949 21 18.7764 20.8978C19.8117 20.6204 20.6203 19.8117 20.8977 18.7765C20.9999 18.395 20.9999 17.93 20.9999 17" }], ["path", { "d": "M16.5 7.49993C16.5 7.49993 13.1858 2.99997 12 2.99996C10.8141 2.99995 7.50002 7.49996 7.50002 7.49996M12 3.99996V16" }]],
  // Upload01
  "compare": [["path", { "d": "M19.9999 17L3.99994 17" }], ["path", { "d": "M17 14C17 14 19.9999 16.2095 19.9999 17C19.9999 17.7906 16.9999 20 16.9999 20" }], ["path", { "d": "M4.99994 7L19.9999 7" }], ["path", { "d": "M6.99992 4C6.99992 4 3.99995 6.20947 3.99994 7.00002C3.99993 7.79058 6.99994 10 6.99994 10" }]],
  // ArrowLeftRight
  "plus": [["path", { "d": "M12.001 5.00003V19.002" }], ["path", { "d": "M19.002 12.002L4.99998 12.002" }]],
  // Add01
  "close": [["path", { "d": "M18 6L6.00081 17.9992M17.9992 18L6 6.00085" }]],
  // Cancel01
  "chevron": [["path", { "d": "M9.00005 6C9.00005 6 15 10.4189 15 12C15 13.5812 9 18 9 18" }]],
  // ArrowRight01
  "chevron-down": [["path", { "d": "M18 9.00005C18 9.00005 13.5811 15 12 15C10.4188 15 6 9 6 9" }]],
  // ArrowDown01
  "disclosure": [["path", { "d": "M9.00005 6C9.00005 6 15 10.4189 15 12C15 13.5812 9 18 9 18" }]],
  // ArrowRight01
  "back": [["path", { "d": "M15 6C15 6 9.00001 10.4189 9 12C8.99999 13.5812 15 18 15 18" }]],
  // ArrowLeft01
  "search": [["path", { "d": "M17 17L21 21" }], ["path", { "d": "M19 11C19 6.58172 15.4183 3 11 3C6.58172 3 3 6.58172 3 11C3 15.4183 6.58172 19 11 19C15.4183 19 19 15.4183 19 11Z" }]],
  // Search01
  "check": [["path", { "d": "M5 14L8.5 17.5L19 6.5" }]],
  // Tick02
  "health": [["path", { "d": "M2 12H6L7.5 8L9.5 15L13 6L15.5 18L18 12H22" }]],
  // Pulse01
  "engine": [["path", { "d": "M4 12C4 8.22876 4 6.34315 5.17157 5.17157C6.34315 4 8.22876 4 12 4C15.7712 4 17.6569 4 18.8284 5.17157C20 6.34315 20 8.22876 20 12C20 15.7712 20 17.6569 18.8284 18.8284C17.6569 20 15.7712 20 12 20C8.22876 20 6.34315 20 5.17157 18.8284C4 17.6569 4 15.7712 4 12Z" }], ["path", { "d": "M9.5 2V4" }], ["path", { "d": "M14.5 2V4" }], ["path", { "d": "M9.5 20V22" }], ["path", { "d": "M14.5 20V22" }], ["path", { "d": "M13 9L9 13" }], ["path", { "d": "M15 13L13 15" }], ["path", { "d": "M22 14.5L20 14.5" }], ["path", { "d": "M4 9.5L2 9.5" }], ["path", { "d": "M4 14.5L2 14.5" }], ["path", { "d": "M22 9.5L20 9.5" }]],
  // Cpu
  "analysis": [["path", { "d": "M7 17L7 13" }], ["path", { "d": "M12 17L12 7" }], ["path", { "d": "M17 17L17 11" }], ["path", { "d": "M2.5 12C2.5 7.52166 2.5 5.28249 3.89124 3.89124C5.28249 2.5 7.52166 2.5 12 2.5C16.4783 2.5 18.7175 2.5 20.1088 3.89124C21.5 5.28249 21.5 7.52166 21.5 12C21.5 16.4783 21.5 18.7175 20.1088 20.1088C18.7175 21.5 16.4783 21.5 12 21.5C7.52166 21.5 5.28249 21.5 3.89124 20.1088C2.5 18.7175 2.5 16.4783 2.5 12Z" }]],
  // Analytics01
  "tuning": [["circle", { "cx": "12", "cy": "18", "r": "3" }], ["path", { "d": "M12 15V10" }], ["path", { "d": "M22 13C22 7.47715 17.5228 3 12 3C6.47715 3 2 7.47715 2 13" }]],
  // DashboardSpeed02
  "statements": [["path", { "d": "M7 6H11M7 11H15M7 16H19" }], ["path", { "d": "M3 3V13C3 16.7712 3 18.6569 4.17157 19.8284C5.34315 21 7.22876 21 11 21H21" }]],
  // ChartBarIncreasing
  "sessions": [["path", { "d": "M18 21.0001C17.713 17.269 14.7289 14.3151 10.995 14.0662L10 13.9999C9.64458 14.0096 9.31335 14.0225 9.00082 14.0378C5.3 14.2192 2.28417 17.3057 2 21.0001" }], ["path", { "d": "M18 6.49988H22" }], ["path", { "d": "M18 9.99988H22" }], ["path", { "d": "M20 13.4999H22" }], ["circle", { "cx": "10", "cy": "6.99988", "r": "4" }]],
  // UserList
  "waits": [["path", { "d": "M4 3H20" }], ["path", { "d": "M5.5 3V5.03039C5.5 6.27227 6.07682 7.4437 7.06116 8.20089L12 12L16.9388 8.20089C17.9232 7.44371 18.5 6.27227 18.5 5.03039V3" }], ["path", { "d": "M5.5 21V18.9696C5.5 17.7277 6.07682 16.5563 7.06116 15.7991L12 12L16.9388 15.7991C17.9232 16.5563 18.5 17.7277 18.5 18.9696V21" }], ["path", { "d": "M4 21H20" }]],
  // Hourglass
  "io": [["path", { "d": "M7 4V20" }], ["path", { "d": "M17 19L17 4" }], ["path", { "d": "M10 6.99998C10 6.99998 7.79053 4.00001 6.99998 4C6.20942 3.99999 4 7 4 7" }], ["path", { "d": "M20 17C20 17 17.7905 20 17 20C16.2094 20 14 17 14 17" }]],
  // ArrowUpDown
  "lock": [["path", { "d": "M12 14V17.0023" }], ["path", { "d": "M16.5 9V6.5C16.5 4.01472 14.4853 2 12 2C9.51472 2 7.5 4.01472 7.5 6.5V9" }], ["path", { "d": "M4.26781 18.8447C4.49269 20.515 5.87613 21.8235 7.55966 21.9009C8.97627 21.966 10.4153 22 12 22C13.5847 22 15.0237 21.966 16.4403 21.9009C18.1239 21.8235 19.5073 20.515 19.7322 18.8447C19.879 17.7547 20 16.6376 20 15.5C20 14.3624 19.879 13.2453 19.7322 12.1553C19.5073 10.485 18.1239 9.17649 16.4403 9.09909C15.0237 9.03397 13.5847 9 12 9C10.4153 9 8.97627 9.03397 7.55966 9.09909C5.87613 9.17649 4.49269 10.485 4.26781 12.1553C4.12104 13.2453 4 14.3624 4 15.5C4 16.6376 4.12104 17.7547 4.26781 18.8447Z" }]],
  // SquareLock01
  "index": [["path", { "d": "M8 5.5L20 5.5" }], ["path", { "d": "M8 12.5L20 12.5" }], ["path", { "d": "M8 19.5L20 19.5" }], ["path", { "d": "M4.375 5.5H4.25M4.5 5.5C4.5 5.63807 4.38807 5.75 4.25 5.75C4.11193 5.75 4 5.63807 4 5.5C4 5.36193 4.11193 5.25 4.25 5.25C4.38807 5.25 4.5 5.36193 4.5 5.5Z" }], ["path", { "d": "M4.375 12.5H4.25M4.5 12.5C4.5 12.6381 4.38807 12.75 4.25 12.75C4.11193 12.75 4 12.6381 4 12.5C4 12.3619 4.11193 12.25 4.25 12.25C4.38807 12.25 4.5 12.3619 4.5 12.5Z" }], ["path", { "d": "M4.375 19.5H4.25M4.5 19.5C4.5 19.6381 4.38807 19.75 4.25 19.75C4.11193 19.75 4 19.6381 4 19.5C4 19.3619 4.11193 19.25 4.25 19.25C4.38807 19.25 4.5 19.3619 4.5 19.5Z" }]],
  // LeftToRightListBullet
  "table": [["path", { "d": "M3.89124 20.1088C2.5 18.7175 2.5 16.4783 2.5 12C2.5 7.52166 2.5 5.28249 3.89124 3.89124C5.28248 2.5 7.52166 2.5 12 2.5C16.4783 2.5 18.7175 2.5 20.1088 3.89124C21.5 5.28248 21.5 7.52166 21.5 12C21.5 16.4783 21.5 18.7175 20.1088 20.1088C18.7175 21.5 16.4783 21.5 12 21.5C7.52166 21.5 5.28249 21.5 3.89124 20.1088Z" }], ["path", { "d": "M2.5 9L21.5 9" }], ["path", { "d": "M16 21.5L16 2.5" }], ["path", { "d": "M8 21.4998L8 2.49976" }]],
  // LayoutTable01
  "diagram": [["path", { "d": "M15 12C15 13.4142 15 14.1213 15.5126 14.5607C16.0251 15 16.8501 15 18.5 15C20.1499 15 20.9749 15 21.4874 14.5607C22 14.1213 22 13.4142 22 12C22 10.5858 22 9.87868 21.4874 9.43934C20.9749 9 20.1499 9 18.5 9C16.8501 9 16.0251 9 15.5126 9.43934C15 9.87868 15 10.5858 15 12Z" }], ["path", { "d": "M2 19C2 20.4142 2 21.1213 2.41005 21.5607C2.8201 22 3.48007 22 4.8 22H6.2C7.51993 22 8.1799 22 8.58995 21.5607C9 21.1213 9 20.4142 9 19C9 17.5858 9 16.8787 8.58995 16.4393C8.1799 16 7.51993 16 6.2 16H4.8C3.48007 16 2.8201 16 2.41005 16.4393C2 16.8787 2 17.5858 2 19Z" }], ["path", { "d": "M2 5C2 6.41421 2 7.12132 2.41005 7.56066C2.8201 8 3.48007 8 4.8 8L6.2 8C7.51993 8 8.1799 8 8.58995 7.56066C9 7.12132 9 6.41421 9 5C9 3.58579 9 2.87868 8.58995 2.43934C8.1799 2 7.51993 2 6.2 2L4.8 2C3.48007 2 2.8201 2 2.41005 2.43934C2 2.87868 2 3.58579 2 5Z" }], ["path", { "d": "M12 12.001L12 7.95383C12 6.07993 11.0828 5.23846 9 5M12 12.001L12 16.0481C12 17.8155 11.221 18.7415 9 19M12 12.001L15 12.001" }]],
  // HierarchySquare05
  "configuration": [["path", { "d": "M3.99963 5.00055L9.99963 5.00031" }], ["path", { "d": "M12.9996 5.00031L19.9996 5.00031" }], ["path", { "d": "M15.9996 9.00031L15.9996 15.0003" }], ["path", { "d": "M9.99963 2.00031L9.99963 8.00031" }], ["path", { "d": "M11.9996 16.0003L11.9996 22.0003" }], ["path", { "d": "M15.9996 12.0001L19.9996 12.0003" }], ["path", { "d": "M3.99963 12.0005L12.9996 12.0003" }], ["path", { "d": "M11.9996 19.0003L19.9996 19.0003" }], ["path", { "d": "M3.99963 19.0005L8.99963 19.0003" }]],
  // SlidersHorizontal
  "instruments": [["path", { "d": "M4.25273 20.302C5.65537 21.5 7.77024 21.5 12 21.5C16.2297 21.5 18.3446 21.5 19.7472 20.302C19.9464 20.1319 20.1319 19.9464 20.302 19.7472C21.5 18.3446 21.5 16.2297 21.5 12C21.5 7.77027 21.5 5.6554 20.302 4.25276C20.1319 4.05358 19.9464 3.86808 19.7472 3.69797C18.3446 2.5 16.2297 2.5 12 2.5C7.77024 2.5 5.65537 2.5 4.25273 3.69797C4.05355 3.86808 3.86805 4.05358 3.69794 4.25276C2.49997 5.6554 2.49997 7.77027 2.49997 12C2.49997 16.2297 2.49997 18.3446 3.69794 19.7472C3.86805 19.9464 4.05355 20.1319 4.25273 20.302Z" }], ["path", { "d": "M3.00003 15.5H21" }], ["path", { "d": "M7.125 18.5H7M7.25 18.5C7.25 18.6381 7.13807 18.75 7 18.75C6.86193 18.75 6.75 18.6381 6.75 18.5C6.75 18.3619 6.86193 18.25 7 18.25C7.13807 18.25 7.25 18.3619 7.25 18.5Z" }], ["path", { "d": "M11.125 18.5H11M11.25 18.5C11.25 18.6381 11.1381 18.75 11 18.75C10.8619 18.75 10.75 18.6381 10.75 18.5C10.75 18.3619 10.8619 18.25 11 18.25C11.1381 18.25 11.25 18.3619 11.25 18.5Z" }], ["path", { "d": "M17 9L15.5 9L13.5 6.5L10.5 11.5L8.5 9H7" }]],
  // Cardiogram01
  "replication": [["path", { "d": "M10.9999 20C6.7573 20 4.63598 20 3.31796 18.682C1.99994 17.364 1.99994 15.2426 1.99994 11C1.99994 6.75736 1.99994 4.63604 3.31796 3.31802C4.63598 2 6.7573 2 10.9999 2C15.2426 2 17.3639 2 18.6819 3.31802C19.892 4.52814 19.9911 6.41543 19.9992 10" }], ["path", { "d": "M1.99994 11H10.9999" }], ["path", { "d": "M9.99994 6.5L15.9999 6.5" }], ["path", { "d": "M6.12494 6.5H5.99994M6.24994 6.5C6.24994 6.63807 6.13801 6.75 5.99994 6.75C5.86187 6.75 5.74994 6.63807 5.74994 6.5C5.74994 6.36193 5.86187 6.25 5.99994 6.25C6.13801 6.25 6.24994 6.36193 6.24994 6.5Z" }], ["path", { "d": "M6.12494 15.5H5.99994M6.24994 15.5C6.24994 15.6381 6.13801 15.75 5.99994 15.75C5.86187 15.75 5.74994 15.6381 5.74994 15.5C5.74994 15.3619 5.86187 15.25 5.99994 15.25C6.13801 15.25 6.24994 15.3619 6.24994 15.5Z" }], ["path", { "d": "M14.2907 16C14.8841 14.5341 16.3212 13.5 17.9999 13.5C19.4805 13.5 20.7732 14.3044 21.4648 15.5M21.7092 19C21.1158 20.4659 19.6786 21.5 17.9999 21.5C16.5194 21.5 15.2267 20.6956 14.5351 19.5M16.9999 16H13.9999V13M18.9999 19H21.9999V22" }]],
  // DatabaseSync
  "maintenance": [["path", { "d": "M13 11L18 6" }], ["path", { "d": "M19 7L17 5L19.5 3.5L20.5 4.5L19 7Z" }], ["path", { "d": "M4.02513 8.97487C3.01416 7.96391 2.75095 6.48836 3.23548 5.23548L4.65748 6.65748H6.65748V4.65748L5.23548 3.23548C6.48836 2.75095 7.96391 3.01416 8.97487 4.02513C9.98621 5.03647 10.2493 6.51274 9.76398 7.76593L16.2341 14.236C17.4873 13.7507 18.9635 14.0138 19.9749 15.0251C20.9858 16.0361 21.2491 17.5116 20.7645 18.7645L19.3425 17.3425L17.3425 17.3425V19.3425L18.7645 20.7645C17.5116 21.2491 16.0361 20.9858 15.0251 19.9749C14.0145 18.9643 13.7511 17.4895 14.2349 16.2369L7.76312 9.76507C6.51053 10.2489 5.03571 9.98546 4.02513 8.97487Z" }], ["path", { "d": "M12.203 14.5L6.59897 20.1041C6.07115 20.6319 5.2154 20.6319 4.68758 20.1041L3.89586 19.3124C3.36805 18.7846 3.36805 17.9288 3.89586 17.401L9.49994 11.7969" }]],
  // Tools
  "wrench": [["path", { "d": "M20.3584 13.3567C19.1689 14.546 16.9308 14.4998 13.4992 14.4998C11.2914 14.4998 9.50138 12.7071 9.50024 10.4993C9.50024 7.07001 9.454 4.83065 10.6435 3.64138C11.8329 2.45212 12.3583 2.50027 17.6274 2.50027C18.1366 2.49809 18.3929 3.11389 18.0329 3.47394L15.3199 6.18714C14.6313 6.87582 14.6294 7.99233 15.3181 8.68092C16.0068 9.36952 17.1234 9.36959 17.8122 8.68109L20.5259 5.96855C20.886 5.60859 21.5019 5.86483 21.4997 6.37395C21.4997 11.6422 21.5479 12.1675 20.3584 13.3567Z" }], ["path", { "d": "M13.5 14.5L7.32842 20.6716C6.22386 21.7761 4.433 21.7761 3.32843 20.6716C2.22386 19.567 2.22386 17.7761 3.32843 16.6716L9.5 10.5" }], ["path", { "d": "M5.50896 18.5H5.5" }]],
  // Wrench01
  "users": [["path", { "d": "M18.4995 20.5C18.2663 17.5685 15.8417 15.2477 12.808 15.0521L11.9995 15C11.7107 15.0076 11.4416 15.0178 11.1877 15.0298C8.18075 15.1723 5.7304 17.5974 5.49951 20.5" }], ["path", { "d": "M15.2495 9.25C15.2495 11.0449 13.7944 12.5 11.9995 12.5C10.2046 12.5 8.74952 11.0449 8.74952 9.25C8.74952 7.45507 10.2046 6 11.9995 6C13.7944 6 15.2495 7.45507 15.2495 9.25Z" }], ["path", { "d": "M5.50249 8.5C5.17908 7.99485 4.99158 7.39432 4.99158 6.75C4.99158 4.95507 6.44665 3.5 8.24157 3.5C8.68752 3.5 9.1125 3.58982 9.49939 3.75235" }], ["path", { "d": "M18.4963 8.5C18.8197 7.99485 19.0072 7.39432 19.0072 6.75C19.0072 4.95507 17.5521 3.5 15.7572 3.5C15.3113 3.5 14.8863 3.58982 14.4994 3.75235" }], ["path", { "d": "M22.0007 17.9996C21.8208 15.7374 19.9995 13.5 17.9995 13" }], ["path", { "d": "M1.99927 17.9996C2.17923 15.7374 4.00049 13.5 6.00049 13" }]],
  // UserGroup
  "audit": [["path", { "d": "M19 10.5V10C19 6.22876 19 4.34315 17.8284 3.17157C16.6569 2 14.7712 2 11 2C7.22876 2 5.34315 2 4.17157 3.17157C3 4.34315 3 6.22876 3 10V16C3 17.8638 3 18.7956 3.30448 19.5307C3.71046 20.5108 4.48915 21.2895 5.46927 21.6955C6.20435 22 7.13623 22 9 22" }], ["path", { "d": "M7 7H15M7 11H11" }], ["path", { "d": "M15.2825 19.0044C15.2235 18.1157 15.118 17.1658 14.6817 16.0917C14.3095 15.1756 14.4132 13.0205 16.5 13.0205C18.5868 13.0205 18.6664 15.1756 18.2942 16.0917C17.8578 17.1658 17.7765 18.1157 17.7175 19.0044M21 22H12V20.7543C12 20.3078 12.2664 19.9154 12.6528 19.7928L14.9076 19.077C15.0684 19.0259 15.2348 19 15.4021 19H17.5979C17.7652 19 17.9316 19.0259 18.0924 19.077L20.3472 19.7928C20.7336 19.9154 21 20.3078 21 20.7543V22Z" }]],
  // Audit01
  "errorlog": [["path", { "d": "M11.9922 8L11.9922 12" }], ["path", { "d": "M12.1172 15.75L11.9922 15.75M12.2422 15.75C12.2422 15.8881 12.1303 16 11.9922 16C11.8541 16 11.7422 15.8881 11.7422 15.75C11.7422 15.6119 11.8541 15.5 11.9922 15.5C12.1303 15.5 12.2422 15.6119 12.2422 15.75Z" }], ["path", { "d": "M13.5067 2C14.733 2 15.3461 2 15.8974 2.22836C16.4487 2.45672 16.8823 2.89027 17.7494 3.75736L20.2346 6.24264C21.1017 7.10973 21.5353 7.54328 21.7636 8.09459C21.992 8.6459 21.992 9.25903 21.992 10.4853V13.5147C21.992 14.741 21.992 15.3541 21.7636 15.9054C21.5353 16.4567 21.1017 16.8903 20.2346 17.7574L17.7494 20.2426C16.8823 21.1097 16.4487 21.5433 15.8974 21.7716C15.3461 22 14.733 22 13.5067 22H10.4773C9.25105 22 8.63792 22 8.08659 21.7716C7.53527 21.5433 7.10172 21.1097 6.23462 20.2426L3.74948 17.7573C2.88242 16.8903 2.44889 16.4567 2.22054 15.9054C1.99219 15.3541 1.99219 14.741 1.99219 13.5148V10.4852C1.99219 9.259 1.99219 8.64589 2.22054 8.09458C2.44889 7.54328 2.88242 7.10974 3.74948 6.24265L6.23463 3.75743C7.10172 2.8903 7.53527 2.45674 8.08659 2.22837C8.63792 2 9.25105 2 10.4773 2H13.5067Z" }]],
  // OctagonAlert
  "warning": [["path", { "d": "M13.9248 21H10.0752C5.44476 21 3.12955 21 2.27636 19.4939C1.42317 17.9879 2.60736 15.9914 4.97574 11.9985L6.90057 8.75333C9.17559 4.91778 10.3131 3 12 3C13.6869 3 14.8244 4.91777 17.0994 8.75332L19.0243 11.9985C21.3926 15.9914 22.5768 17.9879 21.7236 19.4939C20.8704 21 18.5552 21 13.9248 21Z" }], ["path", { "d": "M12 9V13" }], ["path", { "d": "M12.125 16.75H12M12.25 16.75C12.25 16.8881 12.1381 17 12 17C11.8619 17 11.75 16.8881 11.75 16.75C11.75 16.6119 11.8619 16.5 12 16.5C12.1381 16.5 12.25 16.6119 12.25 16.75Z" }]],
  // Alert02
  "info": [["circle", { "cx": "12", "cy": "12", "r": "10" }], ["path", { "d": "M12 16V12" }], ["path", { "d": "M12.125 8.25H12M12.25 8.25C12.25 8.11193 12.1381 8 12 8C11.8619 8 11.75 8.11193 11.75 8.25C11.75 8.38807 11.8619 8.5 12 8.5C12.1381 8.5 12.25 8.38807 12.25 8.25Z" }]],
  // InformationCircle
  "run": [["path", { "d": "M18.8906 12.846C18.5371 14.189 16.8667 15.138 13.5257 17.0361C10.296 18.8709 8.6812 19.7884 7.37983 19.4196C6.8418 19.2671 6.35159 18.9776 5.95624 18.5787C5 17.6139 5 15.7426 5 12C5 8.2574 5 6.3861 5.95624 5.42132C6.35159 5.02245 6.8418 4.73288 7.37983 4.58042C8.6812 4.21165 10.296 5.12907 13.5257 6.96393C16.8667 8.86197 18.5371 9.811 18.8906 11.154C19.0365 11.7084 19.0365 12.2916 18.8906 12.846Z" }]],
  // Play
  "runAll": [["path", { "d": "M8.19067 16.2995C5.99362 17.9404 4.89509 18.7608 4.07255 18.34C3.25 17.9192 3.25 16.5502 3.25 13.8124V10.1873C3.25 7.4495 3.25 6.08062 4.07255 5.6598C4.8951 5.23897 5.99359 6.05938 8.19057 7.7002C9.20752 8.45971 10.2263 9.21653 11.2386 9.9805C11.2386 7.36072 11.2386 6.05082 12.0453 5.62582C12.0628 5.61659 12.0805 5.60771 12.0984 5.59918C12.921 5.20683 13.9497 6.00067 16.007 7.58835L18.5961 9.5863C20.032 10.6944 20.75 11.2485 20.75 11.9999C20.75 12.7513 20.032 13.3054 18.5961 14.4136L16.0072 16.4115C13.9497 17.9993 12.921 18.7932 12.0983 18.4008C12.0805 18.3923 12.0628 18.3834 12.0454 18.3742C11.2386 17.9493 11.2386 16.6393 11.2386 14.0193C10.2263 14.7832 9.20759 15.54 8.19067 16.2995Z" }]],
  // FastForward
  "stop": [["path", { "d": "M4 12C4 8.72077 4 7.08116 4.81382 5.91891C5.1149 5.48891 5.48891 5.1149 5.91891 4.81382C7.08116 4 8.72077 4 12 4C15.2792 4 16.9188 4 18.0811 4.81382C18.5111 5.1149 18.8851 5.48891 19.1862 5.91891C20 7.08116 20 8.72077 20 12C20 15.2792 20 16.9188 19.1862 18.0811C18.8851 18.5111 18.5111 18.8851 18.0811 19.1862C16.9188 20 15.2792 20 12 20C8.72077 20 7.08116 20 5.91891 19.1862C5.48891 18.8851 5.1149 18.5111 4.81382 18.0811C4 16.9188 4 15.2792 4 12Z" }]],
  // Stop
  "refresh": [["path", { "d": "M3.04712 13C3.01082 12.6717 2.99219 12.338 2.99219 12C2.99219 7.02944 7.02162 3 11.9922 3C14.8195 3 17.3422 4.30367 18.9922 6.34267M20.9373 11C20.9736 11.3283 20.9922 11.662 20.9922 12C20.9922 16.9706 16.9628 21 11.9922 21C9.16492 21 6.64214 19.6963 4.99219 17.6573" }], ["path", { "d": "M15.9922 7H16.9922C18.4064 7 19.1135 7 19.5528 6.56066C19.9922 6.12132 19.9922 5.41421 19.9922 4V3" }], ["path", { "d": "M7.99219 17H6.99219C5.57797 17 4.87087 17 4.43153 17.4393C3.99219 17.8787 3.99219 18.5858 3.99219 20V21" }]],
  // RefreshCw
  "history": [["path", { "d": "M3.49902 14.9656C4.72475 18.4791 8.06749 21 11.999 21C16.9696 21 20.999 16.9706 20.999 12C20.999 7.02944 16.9696 3 11.999 3C8.29827 3 4.8984 5.6756 3.68943 8.5" }], ["path", { "d": "M11.999 7V12L14.999 14" }], ["path", { "d": "M7.49751 8.74363C7.49751 8.74363 3.81388 9.3026 3.25487 8.7436C2.69585 8.1846 3.25488 4.50098 3.25488 4.50098" }]],
  // History
  "copy": [["path", { "d": "M7.5 14.5C7.5 11.2002 7.5 9.55025 8.52513 8.52513C9.55025 7.5 11.2002 7.5 14.5 7.5C17.7998 7.5 19.4497 7.5 20.4749 8.52513C21.5 9.55025 21.5 11.2002 21.5 14.5C21.5 17.7998 21.5 19.4497 20.4749 20.4749C19.4497 21.5 17.7998 21.5 14.5 21.5C11.2002 21.5 9.55025 21.5 8.52513 20.4749C7.5 19.4497 7.5 17.7998 7.5 14.5Z" }], ["path", { "d": "M7.5 16.5C6.10355 16.5 5.40533 16.5 4.84402 16.3036C3.83866 15.9518 3.0482 15.1613 2.69641 14.156C2.5 13.5947 2.5 12.8964 2.5 11.5V9.5C2.5 6.20017 2.5 4.55025 3.52513 3.52513C4.55025 2.5 6.20017 2.5 9.5 2.5H11.5C12.8964 2.5 13.5947 2.5 14.156 2.69641C15.1613 3.0482 15.9518 3.83866 16.3036 4.84402C16.5 5.40533 16.5 6.10355 16.5 7.5" }]],
  // Copy01
  "columns": [["path", { "d": "M20.1088 3.89124C21.5 5.28249 21.5 7.52166 21.5 12C21.5 16.4783 21.5 18.7175 20.1088 20.1088C18.7175 21.5 16.4783 21.5 12 21.5C7.52166 21.5 5.28249 21.5 3.89124 20.1088C2.5 18.7175 2.5 16.4783 2.5 12C2.5 7.52166 2.5 5.28249 3.89124 3.89124C5.28249 2.5 7.52166 2.5 12 2.5C16.4783 2.5 18.7175 2.5 20.1088 3.89124Z" }], ["path", { "d": "M8.5 2.5V21.5" }], ["path", { "d": "M15.5 2.5V21.5" }]],
  // Layout3Column
  "inspector": [["path", { "d": "M2 12C2 8.3109 2 6.46633 2.81382 5.1588C3.1149 4.67505 3.48891 4.2543 3.91891 3.91557C5.08116 3.00003 6.72077 3.00003 10 3.00003H14C17.2792 3.00003 18.9188 3.00003 20.0811 3.91557C20.5111 4.2543 20.8851 4.67505 21.1862 5.1588C22 6.46633 22 8.3109 22 12C22 15.6892 22 17.5337 21.1862 18.8413C20.8851 19.325 20.5111 19.7458 20.0811 20.0845C18.9188 21 17.2792 21 14 21H10C6.72077 21 5.08116 21 3.91891 20.0845C3.48891 19.7458 3.1149 19.325 2.81382 18.8413C2 17.5337 2 15.6892 2 12Z" }], ["path", { "d": "M14.5 3.00003L14.5 21" }], ["path", { "d": "M18 7.00006H19M18 10.0001H19" }]],
  // SidebarRight
  "split-pane": [["path", { "d": "M3.89124 3.89124C5.28249 2.5 7.52166 2.5 12 2.5C16.4783 2.5 18.7175 2.5 20.1088 3.89124C21.5 5.28249 21.5 7.52166 21.5 12C21.5 16.4783 21.5 18.7175 20.1088 20.1088C18.7175 21.5 16.4783 21.5 12 21.5C7.52166 21.5 5.28249 21.5 3.89124 20.1088C2.5 18.7175 2.5 16.4783 2.5 12C2.5 7.52166 2.5 5.28249 3.89124 3.89124Z" }], ["path", { "d": "M12 2.5V21.5" }]],
  // LayoutTwoColumn
  "browser": [["path", { "d": "M2 12C2 8.31087 2 6.4663 2.81382 5.15877C3.1149 4.67502 3.48891 4.25427 3.91891 3.91554C5.08116 3 6.72077 3 10 3H14C17.2792 3 18.9188 3 20.0811 3.91554C20.5111 4.25427 20.8851 4.67502 21.1862 5.15877C22 6.4663 22 8.31087 22 12C22 15.6891 22 17.5337 21.1862 18.8412C20.8851 19.325 20.5111 19.7457 20.0811 20.0845C18.9188 21 17.2792 21 14 21H10C6.72077 21 5.08116 21 3.91891 20.0845C3.48891 19.7457 3.1149 19.325 2.81382 18.8412C2 17.5337 2 15.6891 2 12Z" }], ["path", { "d": "M9.5 3L9.5 21" }], ["path", { "d": "M5 7H6M5 10H6" }]],
  // SidebarLeft
  "view": [["path", { "d": "M21.544 11.045C21.848 11.4713 22 11.6845 22 12C22 12.3155 21.848 12.5287 21.544 12.955C20.1779 14.8706 16.6892 19 12 19C7.31078 19 3.8221 14.8706 2.45604 12.955C2.15201 12.5287 2 12.3155 2 12C2 11.6845 2.15201 11.4713 2.45604 11.045C3.8221 9.12944 7.31078 5 12 5C16.6892 5 20.1779 9.12944 21.544 11.045Z" }], ["path", { "d": "M15 12C15 10.3431 13.6569 9 12 9C10.3431 9 9 10.3431 9 12C9 13.6569 10.3431 15 12 15C13.6569 15 15 13.6569 15 12Z" }]],
  // View
  "baseline": [["path", { "d": "M5.0249 21C5.04385 19.2643 5.04366 17.5541 5.0366 15.9209M5.0366 15.9209C5.01301 10.4614 4.91276 5.86186 5.19475 4.04271C5.5611 1.67939 9.39301 3.82993 13.9703 5.59842L16.0328 6.48729C17.5508 7.1415 19.7187 8.30352 18.7662 9.66084C18.3738 10.22 17.56 10.8596 16.0575 11.567L5.0366 15.9209Z" }]],
  // Flag01
  "branches": [["path", { "d": "M7 19H13C15.8284 19 17.2426 19 18.1213 18.1213C19 17.2426 19 15.8284 19 13V10M19 10C19.7002 10 21.0085 11.9943 21.5 12.5M19 10C18.2998 10 16.9915 11.9943 16.5 12.5" }], ["path", { "d": "M5 7L5 17" }], ["circle", { "cx": "5", "cy": "5", "r": "2" }], ["circle", { "cx": "19", "cy": "5", "r": "2" }], ["circle", { "cx": "5", "cy": "19", "r": "2" }]],
  // GitBranch
  "edit": [["path", { "d": "M14.0737 3.88545C14.8189 3.07808 15.1915 2.6744 15.5874 2.43893C16.5427 1.87076 17.7191 1.85309 18.6904 2.39232C19.0929 2.6158 19.4769 3.00812 20.245 3.79276C21.0131 4.5774 21.3972 4.96972 21.6159 5.38093C22.1438 6.37312 22.1265 7.57479 21.5703 8.5507C21.3398 8.95516 20.9446 9.33578 20.1543 10.097L10.7506 19.1543C9.25288 20.5969 8.504 21.3182 7.56806 21.6837C6.63212 22.0493 5.6032 22.0224 3.54536 21.9686L3.26538 21.9613C2.63891 21.9449 2.32567 21.9367 2.14359 21.73C1.9615 21.5234 1.98636 21.2043 2.03608 20.5662L2.06308 20.2197C2.20301 18.4235 2.27297 17.5255 2.62371 16.7182C2.97444 15.9109 3.57944 15.2555 4.78943 13.9445L14.0737 3.88545Z" }], ["path", { "d": "M13 4L20 11" }], ["path", { "d": "M14 22L22 22" }]],
  // Edit02
  "trash": [["path", { "d": "M19.5 5.5L18.8803 15.5251C18.7219 18.0864 18.6428 19.3671 18.0008 20.2879C17.6833 20.7431 17.2747 21.1273 16.8007 21.416C15.8421 22 14.559 22 11.9927 22C9.42312 22 8.1383 22 7.17905 21.4149C6.7048 21.1257 6.296 20.7408 5.97868 20.2848C5.33688 19.3626 5.25945 18.0801 5.10461 15.5152L4.5 5.5" }], ["path", { "d": "M3 5.5H21M16.0557 5.5L15.3731 4.09173C14.9196 3.15626 14.6928 2.68852 14.3017 2.39681C14.215 2.3321 14.1231 2.27454 14.027 2.2247C13.5939 2 13.0741 2 12.0345 2C10.9688 2 10.436 2 9.99568 2.23412C9.8981 2.28601 9.80498 2.3459 9.71729 2.41317C9.32164 2.7167 9.10063 3.20155 8.65861 4.17126L8.05292 5.5" }], ["path", { "d": "M9.5 16.5L9.5 10.5" }], ["path", { "d": "M14.5 16.5L14.5 10.5" }]],
  // Delete02
  "globe": [["path", { "d": "M12.5 19L12.5 22" }], ["path", { "d": "M10.5 22H14.5" }], ["circle", { "cx": "7", "cy": "7", "r": "7", "transform": "matrix(-1 0 0 1 20.5 2)" }], ["path", { "d": "M8.5 4C9.15431 4.0385 9.49236 4.35899 10.0735 4.97301C11.1231 6.08206 12.1727 6.1746 12.8724 5.80492C13.922 5.2504 13.04 4.35221 14.2719 3.86409C15.0748 3.54595 15.1868 2.68026 14.7399 2" }], ["path", { "d": "M20 10C18.5 10 18.2338 11.2468 17 11C14.5 10.5 13.7916 11.0589 13.7916 12.2511C13.7916 13.4432 13.7916 13.4432 13.2717 14.3373C12.9335 14.9189 12.8153 15.5004 13.4894 16" }], ["path", { "d": "M6.5 2C4.64864 3.79995 3.5 6.3082 3.5 9.08251C3.5 14.5598 7.97715 19 13.5 19C16.2255 19 18.6962 17.9187 20.5 16.165" }]],
  // Globe
  "optimize": [["path", { "d": "M5.22576 11.3294L12.224 2.34651C12.7713 1.64397 13.7972 2.08124 13.7972 3.01707V9.96994C13.7972 10.5305 14.1995 10.985 14.6958 10.985H18.0996C18.8729 10.985 19.2851 12.0149 18.7742 12.6706L11.776 21.6535C11.2287 22.356 10.2028 21.9188 10.2028 20.9829V14.0301C10.2028 13.4695 9.80048 13.015 9.3042 13.015H5.90035C5.12711 13.015 4.71494 11.9851 5.22576 11.3294Z" }]],
  // Flash
  "shield": [["path", { "d": "M20.9922 11.1833V8.28029C20.9922 6.64029 20.9922 5.82028 20.5881 5.28529C20.184 4.75029 19.2703 4.49056 17.4429 3.9711C16.1944 3.6162 15.0938 3.18863 14.2145 2.79829C13.0156 2.2661 12.4161 2 11.9922 2C11.5682 2 10.9688 2.2661 9.7699 2.79829C8.89057 3.18863 7.79002 3.61619 6.54152 3.9711C4.71411 4.49056 3.80041 4.75029 3.3963 5.28529C2.99219 5.82028 2.99219 6.64029 2.99219 8.28029V11.1833C2.99219 16.8085 8.05496 20.1835 10.5861 21.5194C11.1932 21.8398 11.4968 22 11.9922 22C12.4876 22 12.7911 21.8398 13.3982 21.5194C15.9294 20.1835 20.9922 16.8085 20.9922 11.1833Z" }], ["path", { "d": "M8.49219 11.8333C8.49219 11.8333 9.36719 11.8333 10.2422 13.5C10.2422 13.5 13.0216 9.33333 15.4922 8.5" }]],
  // ShieldCheck
  "minus": [["path", { "d": "M20 12L4 12" }]],
  // MinusSign
  "block": [["path", { "d": "M22 12C22 6.47715 17.5228 2 12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22C17.5228 22 22 17.5228 22 12Z" }], ["path", { "d": "M14.9994 15L9 9M9.00064 15L15 9" }]],
  // CancelCircle
  "clean": [["path", { "d": "M14.25 16L14.6279 19.0232C14.8255 20.6038 13.593 22 12 22C10.407 22 9.17452 20.6038 9.3721 19.0232L9.75 16C9.85134 15.1892 9.90202 14.7839 9.72689 14.4892C9.663 14.3816 9.57951 14.2871 9.48074 14.2103C9.21003 14 8.8015 14 7.98444 14H7.83046C6.10918 14 5.24854 14 4.68301 13.5052C4.61617 13.4467 4.55331 13.3838 4.49483 13.317C4 12.7515 4 11.8908 4 10.1695V10H20V10.1695C20 11.8908 20 12.7515 19.5052 13.317C19.4467 13.3838 19.3838 13.4467 19.317 13.5052C18.7515 14 17.8908 14 16.1695 14H16.0156C15.1985 14 14.79 14 14.5193 14.2103C14.4205 14.2871 14.337 14.3816 14.2731 14.4892C14.098 14.7839 14.1487 15.1892 14.25 16Z" }], ["path", { "d": "M4 10V5.51472C4 5.03662 4 4.79756 4.0272 4.59696C4.20846 3.25997 5.25997 2.20846 6.59696 2.0272C6.79756 2 7.03662 2 7.51472 2C7.58456 2 7.61949 2 7.65311 2.00227C7.87143 2.017 8.07889 2.10293 8.24368 2.24689C8.26906 2.26906 8.29376 2.29376 8.34315 2.34315L8.58579 2.58579C9.25245 3.25245 9.58579 3.58579 10 3.58579C10.4142 3.58579 10.7475 3.25245 11.4142 2.58579C11.7032 2.29676 11.8478 2.15224 12.0315 2.07612C12.2153 2 12.4197 2 12.8284 2H14C16.8284 2 18.2426 2 19.1213 2.87868C20 3.75736 20 5.17157 20 8V10" }]],
  // BrushCleaning
  "undo": [["path", { "d": "M12 21C16.9706 21 21 16.9706 21 12C21 7.02944 16.9706 3 12 3C8.66873 3 5.76018 4.80989 4.20404 7.5" }], ["path", { "d": "M3 3V4.27816C3 6.47004 3 7.56599 3.70725 8.16512C4.4145 8.76425 5.49553 8.58408 7.6576 8.22373L9 8" }]],
  // Undo02
  "cluster": [["path", { "d": "M8.99219 6C8.99219 4.58579 8.99219 3.87868 9.43153 3.43934C9.87087 3 10.578 3 11.9922 3C13.4064 3 14.1135 3 14.5528 3.43934C14.9922 3.87868 14.9922 4.58579 14.9922 6C14.9922 7.41421 14.9922 8.12132 14.5528 8.56066C14.1135 9 13.4064 9 11.9922 9C10.578 9 9.87087 9 9.43153 8.56066C8.99219 8.12132 8.99219 7.41421 8.99219 6Z" }], ["path", { "d": "M2.99219 18C2.99219 16.5858 2.99219 15.8787 3.43153 15.4393C3.87087 15 4.57797 15 5.99219 15C7.4064 15 8.11351 15 8.55285 15.4393C8.99219 15.8787 8.99219 16.5858 8.99219 18C8.99219 19.4142 8.99219 20.1213 8.55285 20.5607C8.11351 21 7.4064 21 5.99219 21C4.57797 21 3.87087 21 3.43153 20.5607C2.99219 20.1213 2.99219 19.4142 2.99219 18Z" }], ["path", { "d": "M14.9922 18C14.9922 16.5858 14.9922 15.8787 15.4315 15.4393C15.8709 15 16.578 15 17.9922 15C19.4064 15 20.1135 15 20.5528 15.4393C20.9922 15.8787 20.9922 16.5858 20.9922 18C20.9922 19.4142 20.9922 20.1213 20.5528 20.5607C20.1135 21 19.4064 21 17.9922 21C16.578 21 15.8709 21 15.4315 20.5607C14.9922 20.1213 14.9922 19.4142 14.9922 18Z" }], ["path", { "d": "M11.9922 9V12.0002" }], ["path", { "d": "M17.9922 15C17.9922 14.0681 17.9922 13.6022 17.8399 13.2346C17.637 12.7446 17.2476 12.3552 16.7576 12.1522C16.39 12 15.9241 12 14.9922 12H8.99219C8.0603 12 7.59436 12 7.22682 12.1522C6.73676 12.3552 6.34742 12.7446 6.14443 13.2346C5.99219 13.6022 5.99219 14.0681 5.99219 15" }]],
  // Network
  "proxy": [["path", { "d": "M21 8.5V6.6C21 4.90294 21 4.05442 20.4728 3.52721C19.9456 3 19.0971 3 17.4 3H15.5M20 4L14.5 9.5" }], ["path", { "d": "M3 8.5V6.6C3 4.90294 3 4.05442 3.52721 3.52721C4.05442 3 4.90294 3 6.6 3H8.5M4 4L9.65686 9.65686C10.813 10.813 11.391 11.391 11.6955 12.1261C12 12.8612 12 13.6787 12 15.3137V21" }]],
  // Split
  "format": [["path", { "d": "M11 3H21M11 9H21" }], ["path", { "d": "M3 15H21" }], ["path", { "d": "M3 21H21" }], ["path", { "d": "M3 3L4.54218 4.24227C5.84739 5.29366 6.5 5.81935 6.5 6.5C6.5 7.18065 5.84739 7.70635 4.54218 8.75773L3 10" }]]
  // TextIndent
};

// src/components.tsx
var import_jsx_runtime2 = require("react/jsx-runtime");
var data = icons;
function Icon({ name, size }) {
  const inline = ["chevron", "disclosure", "back", "check", "warning"].includes(name);
  const pixels = size && size > 16 ? size : inline || size === 12 ? 12 : 16;
  return /* @__PURE__ */ (0, import_jsx_runtime2.jsx)(
    "svg",
    {
      width: pixels,
      height: pixels,
      viewBox: "0 0 24 24",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: pixels <= 12 ? 2 : 1.75,
      strokeLinecap: "round",
      strokeLinejoin: "round",
      "aria-hidden": "true",
      children: data[name].map(([tag, attrs], i) => import_react3.default.createElement(tag, { key: i, ...attrs }))
    }
  );
}

// src/PerfShared.tsx
var import_react4 = require("react");
var import_jsx_runtime3 = require("react/jsx-runtime");
function engineLabel(report) {
  return `${report.server_product || (report.engine === "mariadb" ? "MariaDB" : "MySQL")} ${report.server_version || "?"}`;
}
function CopySql({ sql, label }) {
  const [copied, setCopied] = (0, import_react4.useState)("");
  return /* @__PURE__ */ (0, import_jsx_runtime3.jsxs)("div", { className: "perf-sql", children: [
    /* @__PURE__ */ (0, import_jsx_runtime3.jsx)("pre", { tabIndex: 0, "aria-label": label, children: /* @__PURE__ */ (0, import_jsx_runtime3.jsx)("code", { children: sql }) }),
    /* @__PURE__ */ (0, import_jsx_runtime3.jsx)(
      "button",
      {
        type: "button",
        className: "text-button",
        "aria-label": `Copy: ${label}`,
        onClick: () => {
          navigator.clipboard.writeText(sql).then(() => setCopied("Copied. Run it yourself in a SQL client if you decide to.")).catch(() => setCopied("Select the text to copy it."));
        },
        children: "Copy"
      }
    ),
    copied && /* @__PURE__ */ (0, import_jsx_runtime3.jsx)("span", { className: "hint", role: "status", children: copied })
  ] });
}

// src/Health.tsx
var import_react11 = require("react");

// src/viewOnScreen.ts
var import_react5 = require("react");
var ViewOnScreenContext = (0, import_react5.createContext)(true);

// src/session.ts
var MAX_SESSION_BYTES = 64 * 1024 * 1024;
var KEEPALIVE_LIMIT = 60 * 1024;
var utf8 = new TextEncoder();

// src/SlowQueries.tsx
var import_react10 = require("react");

// src/Instruments.tsx
var import_react9 = require("react");

// src/ReviewedSqlAction.tsx
var import_react8 = require("react");

// src/privileges.ts
var import_react7 = require("react");

// src/Connections.tsx
var import_react6 = require("react");
var import_react_dom2 = require("react-dom");
var import_jsx_runtime4 = require("react/jsx-runtime");
var WritePolicyContext = (0, import_react6.createContext)(null);

// src/privileges.ts
var PrivilegesContext = (0, import_react7.createContext)(null);
function usePrivileges() {
  return (0, import_react7.useContext)(PrivilegesContext);
}
function privilegeResult(answer, account = "") {
  const missing = [...new Set((answer?.missing || []).flat())];
  const state = answer?.state || "unknown";
  const available = answer?.available !== false;
  const reason = state === "unknown" ? `Unverified. ${answer?.reason || "SHOW GRANTS cannot tell; the server decides."}` : (
    // The server's reason usually says both already ("Needs X; app@% has
    // none of them."): add what it does not say, never repeat it.
    state === "denied" ? [
      answer?.reason,
      missing.length && !/\bNeeds\b/.test(answer?.reason || "") ? `Needs ${answer?.need || missing.join(" or ")}.` : "",
      account && !(answer?.reason || "").includes(account) ? `Connected as ${account}.` : ""
    ].filter(Boolean).join(" ") : answer?.reason || "Required privileges are present."
  );
  return {
    state,
    missing,
    reason,
    available,
    disabledReason: !available ? answer?.note || "Unavailable on this server." : state === "denied" && answer?.grey_out !== false ? reason : ""
  };
}
function usePrivilege(actionKey) {
  const context = usePrivileges();
  return privilegeResult(actionKey ? context?.report?.actions[actionKey] : void 0, context?.report?.account);
}
function privilegeButtonProps(gate, onClick, blocked = "", describedBy) {
  const reason = blocked || gate.disabledReason;
  return {
    type: "button",
    "aria-disabled": !!reason,
    ...reason ? { title: reason } : {},
    "aria-describedby": describedBy,
    onClick: (e) => {
      if (reason) {
        e.preventDefault();
        e.stopPropagation();
      } else onClick?.(e);
    }
  };
}

// src/ReviewedSqlAction.tsx
var import_jsx_runtime5 = require("react/jsx-runtime");
var Context = (0, import_react8.createContext)(null);

// src/Instruments.tsx
var import_jsx_runtime6 = require("react/jsx-runtime");
var InstrumentsLinkContext = (0, import_react9.createContext)(null);
function InstrumentsLink({ row, children = "How to turn it on" }) {
  const open = (0, import_react9.useContext)(InstrumentsLinkContext);
  if (!open) return null;
  return /* @__PURE__ */ (0, import_jsx_runtime6.jsx)("button", { type: "button", className: "text-button instr-link", "data-instruments-row": row, onClick: () => open(row), children });
}

// src/placeholders.ts
var SKIP = /(\/\*[\s\S]*?(?:\*\/|$)|--(?=\s|$)[^\r\n]*|#[^\r\n]*|'(?:''|\\[\s\S]|[^'\\])*'?|"(?:""|\\[\s\S]|[^"\\])*"?|`(?:``|[^`])*`?)/.source;
var TOKEN = new RegExp(
  `${SKIP}|(\\(\\s*\\.\\.\\.\\s*\\)|\\?\\s*,\\s*\\.\\.\\.)|(\\?)`,
  "g"
);
var FUNCTION_ONLY = [
  "ADDDATE",
  "BIT_AND",
  "BIT_OR",
  "BIT_XOR",
  "CAST",
  "COUNT",
  "CUME_DIST",
  "CURDATE",
  "CURTIME",
  "DATE_ADD",
  "DATE_SUB",
  "DENSE_RANK",
  "EXTRACT",
  "FIRST_VALUE",
  "GROUP_CONCAT",
  "JSON_ARRAYAGG",
  "JSON_DUALITY_OBJECT",
  "JSON_OBJECTAGG",
  "LAG",
  "LEAD",
  "MAX",
  "MEDIAN",
  "MID",
  "MIN",
  "NOW",
  "NTH_VALUE",
  "NTILE",
  "PERCENT_RANK",
  "PERCENTILE_CONT",
  "PERCENTILE_DISC",
  "POSITION",
  "RANK",
  "SESSION_USER",
  "STD",
  "STDDEV",
  "STDDEV_POP",
  "STDDEV_SAMP",
  "ST_COLLECT",
  "SUBDATE",
  "SUBSTR",
  "SUBSTRING",
  "SUM",
  "SYSDATE",
  "SYSTEM_USER",
  "TRIM",
  "TRIM_ORACLE",
  "VARIANCE",
  "VAR_POP",
  "VAR_SAMP"
];
var FUNCTION_SPACE = new RegExp(
  `${SKIP}|(?<![\\w.$@\`])(${FUNCTION_ONLY.join("|")})\\s+\\(`,
  "gi"
);
function joinFunctionCalls(sql) {
  return (sql || "").replace(
    FUNCTION_SPACE,
    (whole, skip, name) => skip !== void 0 ? whole : `${name}(`
  );
}
function parameterize(text) {
  const names = [];
  const lists = [];
  const sql = (text || "").replace(
    TOKEN,
    (whole, skip, list) => {
      if (skip !== void 0) return whole;
      const name = `p${names.length + 1}`;
      names.push(name);
      if (list === void 0) return `:${name}`;
      lists.push(name);
      return list.startsWith("(") ? `(:${name})` : `:${name}`;
    }
  );
  return { sql, names, lists };
}
function digestQuery(row) {
  const recorded = row.sample_text || "";
  const sample = recorded && !row.sample_truncated ? recorded : "";
  const text = sample || row.digest_text || "";
  const p = parameterize(row.analysis_sql || text);
  if (!sample) p.sql = joinFunctionCalls(p.sql);
  let reason = null;
  if (!text.trim()) reason = "missing";
  else if (p.names.length)
    reason = sample ? "placeholders" : recorded ? "truncated" : "normalized";
  return { ...p, sql: p.sql.trim(), text, fromSample: !!sample, reason };
}
var IDENT = "(`(?:``|[^`])+`|[A-Za-z0-9_$]+)";
var FROM = new RegExp(`\\bFROM\\s+${IDENT}(?:\\s*\\.\\s*${IDENT})?`, "i");

// src/SlowQueries.tsx
var import_jsx_runtime7 = require("react/jsx-runtime");
function statementKey(row, rank) {
  return JSON.stringify([row.schema || "", row.digest || `#${rank}`]);
}
var ORDER_COUNTERS = {
  full_join: ["joins without an index", (r) => (r.select_full_join || 0) + (r.select_range_check || 0)],
  scan: ["first-table scans", (r) => r.select_scan || 0],
  no_index: ["calls without an index", (r) => r.no_index_used]
};
function tableName(table) {
  return `${table.schema}.${table.table}`;
}
function duration(seconds) {
  if (seconds === null || !Number.isFinite(seconds)) return "unknown";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor(seconds % 86400 / 3600);
  const m = Math.floor(seconds % 3600 / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min`;
  return `${Math.round(seconds)} s`;
}
function coverageNotes(coverage) {
  if (!coverage) return [];
  const out = [
    `Server uptime ${duration(coverage.uptime_s)}` + (coverage.first_seen ? `; the oldest statement listed was first seen ${coverage.first_seen}.` : ".")
  ];
  if (coverage.digest_lost)
    out.push(
      `Performance_schema_digest_lost is ${number(coverage.digest_lost)}: those statements were not recorded at all.`
    );
  return out;
}
function digestGate(error) {
  if (/Performance Schema is off/.test(error)) return { text: "Performance Schema is off, so there are no statement digests.", row: "ps" };
  if (/consumer disabled/.test(error)) {
    const consumer = (error.match(/consumer disabled: ([a-z_]+)/) || [])[1] || "statements_digest";
    return { text: `Statement digests are not being collected: the ${consumer} consumer is off.`, row: `consumer:${consumer}` };
  }
  return null;
}
function flat(text) {
  return text.replace(/\s+/g, " ").trim();
}
function preview(text, width = 140) {
  const f = flat(text);
  return f.length > width ? f.slice(0, width - 1) + "\u2026" : f;
}
function topRead(order, schema, table, limit = 25) {
  const target = schema.trim();
  return {
    params: `top:${order}:${target}${table ? `:${JSON.stringify([table.schema, table.table])}` : ""}${limit === 25 ? "" : `:${limit}`}`,
    body: (c) => ({
      connection: c,
      order,
      limit,
      ...target ? { schema: target } : {},
      ...table ? { table: { schema: table.schema, table: table.table } } : {}
    })
  };
}
function TopActions({
  row,
  rank,
  disabled,
  settings,
  connection,
  onAction,
  compact = false,
  idPrefix = "top-",
  name = ""
}) {
  const explainGate = usePrivilege("explain");
  const analysisGate = usePrivilege("run_analysis");
  const privilegeId = (0, import_react10.useId)();
  const text = row.sample_text || row.digest_text || "";
  const q = digestQuery(row);
  const problem = row.capture_problem || (q.reason === "missing" ? "No statement text was recorded." : "");
  const schema = connection.database || row.schema || "";
  const identity = JSON.stringify([
    connectionKey(connection),
    row.schema || "",
    row.digest || "",
    q.sql,
    schema,
    settings.timeout,
    settings.repeat
  ]);
  const statement = statementKey(row, rank);
  const [confirming, setConfirming] = (0, import_react10.useState)(null);
  const [voided, setVoided] = (0, import_react10.useState)(false);
  const confirmButton = (0, import_react10.useRef)(null);
  const analyzeButton = (0, import_react10.useRef)(null);
  (0, import_react10.useEffect)(() => setVoided(false), [statement]);
  (0, import_react10.useEffect)(() => {
    if (confirming && confirming.identity !== identity) {
      setConfirming(null);
      setVoided(confirming.statement === statement);
    }
  }, [identity, confirming, statement]);
  (0, import_react10.useEffect)(() => {
    if (confirming) confirmButton.current?.focus();
  }, [confirming]);
  const reasonId = `${idPrefix}reason-${rank}`;
  const confirmId = `${idPrefix}confirm-${rank}`;
  const label = `statement ${rank}${name ? ` ${name}` : ""}`;
  const needsValues = q.names.length > 0;
  function closeConfirm() {
    setConfirming(null);
    window.setTimeout(() => analyzeButton.current?.focus());
  }
  function openConfirm() {
    if (disabled || problem || analysisGate.disabledReason) return;
    setVoided(false);
    setConfirming({
      identity,
      statement,
      row,
      rank,
      sql: q.sql,
      server: `${connection.host}:${connection.port}`,
      schema,
      timeout: settings.timeout,
      repeat: settings.repeat
    });
  }
  function execute() {
    const c = confirming;
    setConfirming(null);
    if (!c || c.identity !== identity || disabled || problem || analysisGate.disabledReason) {
      setVoided(true);
      return;
    }
    onAction("analyze", c.row, c.rank);
  }
  const runs = (n) => n === 1 ? "1 run" : `${n} runs`;
  return /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "top-actions", children: [
    /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "button-row", children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
        "button",
        {
          className: "button",
          disabled: disabled || !!problem,
          "aria-label": `Explain ${label}`,
          "aria-describedby": problem ? reasonId : void 0,
          title: needsValues ? "Open in a new tab with parameters to fill in. Nothing runs until you do." : "Open in a new tab and capture the estimated plan (EXPLAIN only)",
          "data-privilege-state": explainGate.state,
          ...privilegeButtonProps(explainGate, () => onAction("explain", row, rank), "", `${privilegeId}-explain${problem ? ` ${reasonId}` : ""}`),
          children: "Explain"
        }
      ),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
        "button",
        {
          ref: analyzeButton,
          className: "button",
          disabled: disabled || !!problem,
          "aria-label": `Run analysis on ${label}`,
          "aria-describedby": problem ? reasonId : void 0,
          "aria-expanded": needsValues ? void 0 : !!confirming,
          title: needsValues ? "Open in a new tab with parameters to fill in. Nothing runs until you do." : `Executes the statement in a read-only transaction, ${settings.timeout} s limit per run`,
          "data-privilege-state": analysisGate.state,
          ...privilegeButtonProps(analysisGate, () => needsValues ? onAction("analyze", row, rank) : openConfirm(), "", `${privilegeId}-analysis${problem ? ` ${reasonId}` : ""}`),
          children: "Run analysis"
        }
      ),
      !compact && /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
        "button",
        {
          className: "button",
          disabled: disabled || !text,
          "aria-label": `Use ${label} in the editor`,
          onClick: () => onAction("open", row, rank),
          children: "Use this query"
        }
      )
    ] }),
    /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("span", { id: `${privilegeId}-explain`, className: explainGate.state === "allowed" ? "account-sr" : "account-note", children: [
      explainGate.state !== "allowed" && "Explain: ",
      explainGate.reason
    ] }),
    " ",
    /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("span", { id: `${privilegeId}-analysis`, className: analysisGate.state === "allowed" ? "account-sr" : "account-note", children: [
      analysisGate.state !== "allowed" && "Run analysis: ",
      analysisGate.reason
    ] }),
    row.analysis_warning && !problem && /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "notice", role: "status", children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("p", { children: row.analysis_warning }),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("pre", { className: "sql", children: q.sql })
    ] }),
    problem && /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("p", { className: "hint top-reason", id: reasonId, children: [
      "Explain and Run analysis are off for this statement: ",
      problem
    ] }),
    voided && !confirming && /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("p", { className: "hint top-voided", role: "status", children: "The statement, the connection or the settings changed, so nothing ran. Choose Run analysis again to confirm." }),
    confirming && /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)(
      "div",
      {
        className: "run-confirm",
        role: "group",
        "aria-labelledby": confirmId,
        onKeyDown: (e) => {
          if (e.key === "Escape") {
            e.preventDefault();
            closeConfirm();
          }
        },
        children: [
          /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("p", { id: confirmId, children: [
            "Run analysis executes this statement on ",
            confirming.server,
            ": ",
            runs(confirming.repeat),
            " inside a read-only transaction that is rolled back, each stopped after ",
            confirming.timeout,
            " s.",
            confirming.schema && ` Schema: ${confirming.schema}.`
          ] }),
          /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("p", { className: "hint", children: analysisGate.reason }),
          /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "button-row", children: [
            /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("button", { ref: confirmButton, className: "button primary", disabled, ...privilegeButtonProps(analysisGate, execute, "", `${privilegeId}-analysis`), children: "Execute and analyze" }),
            /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("button", { className: "button", onClick: closeConfirm, children: "Cancel" })
          ] })
        ]
      }
    )
  ] });
}
function StatementRows({
  statements,
  order,
  table = null,
  connection,
  disabled,
  settings,
  onAction
}) {
  if (!statements.length) return null;
  const counter = ORDER_COUNTERS[order];
  const idPrefix = `behind-${order}-${table ? tableName(table).replace(/[^A-Za-z0-9]+/g, "-") : "all"}-`;
  const name = table ? `that names ${tableName(table)}` : counter ? `with ${counter[0]}` : "";
  return /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("ol", { className: "statements-behind-list", "aria-label": `Statements ${name}`.trim(), children: statements.map((row, i) => {
    const text = row.sample_text || row.digest_text || "";
    return /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("li", { "data-digest": row.digest || void 0, children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("code", { className: "cell-sql statements-behind-sql", title: text, children: text ? preview(text, 180) : "No statement text" }),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("span", { className: "hint statements-behind-meta", children: [
        number(row.count),
        " calls \xB7 ",
        time(row.total_ms),
        " total",
        counter && ` \xB7 ${number(counter[1](row))} ${counter[0]}`,
        row.schema && ` \xB7 ${row.schema}`
      ] }),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
        TopActions,
        {
          compact: true,
          row,
          rank: i + 1,
          disabled,
          settings,
          connection,
          idPrefix,
          name,
          onAction
        }
      )
    ] }, statementKey(row, i + 1));
  }) });
}
var behindCache = /* @__PURE__ */ new Map();
var BEHIND_CACHE_LIMIT = 50;
var BEHIND_LIMIT = 5;
function StatementsBehind({
  connection,
  disabled,
  other,
  from,
  order,
  table = null,
  settings,
  onDigest,
  onOpenTop
}) {
  const reads = useConnectionReads(connection);
  const tableKey = table ? JSON.stringify([table.schema, table.table]) : "";
  const [, setVersion] = (0, import_react10.useState)(0);
  const bound = other ? null : behindCache.get(`${reads.key}|${order}|${tableKey}`) || null;
  const report = bound?.report ?? null;
  const statements = report?.statements || [];
  const counter = ORDER_COUNTERS[order];
  const busy = disabled || other || !!reads.loading;
  const gate = digestGate(reads.error);
  async function show() {
    if (other) return;
    const read = topRead(order, "", table, BEHIND_LIMIT);
    await reads.run(
      `statements:${order}:${tableKey}`,
      table ? `Reading statements that name ${tableName(table)}\u2026` : "Reading statement digests\u2026",
      (c, signal) => api("live/top", read.body(c), signal),
      (b) => {
        behindCache.delete(`${b.key}|${order}|${tableKey}`);
        behindCache.set(`${b.key}|${order}|${tableKey}`, b);
        while (behindCache.size > BEHIND_CACHE_LIMIT) behindCache.delete(behindCache.keys().next().value);
        setVersion((v) => v + 1);
      }
    );
  }
  return /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "statements-behind", "data-order": order, "data-table": table ? tableName(table) : void 0, children: [
    /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("div", { className: "button-row", children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("button", { type: "button", className: "button", disabled: busy, onClick: () => void show(), children: report ? "Show again" : "Show statements" }),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("button", { type: "button", className: "button", disabled: disabled || other, onClick: () => onOpenTop(order, table, from), children: "Open in Top statements" })
    ] }),
    !!reads.loading && /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("p", { role: "status", className: "capture-status", children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("span", { className: "spinner" }),
      " ",
      reads.loading
    ] }),
    gate ? /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(PrerequisiteGate, { action: /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(InstrumentsLink, { row: gate.row }), children: gate.text }) : !!reads.error && /* @__PURE__ */ (0, import_jsx_runtime7.jsx)("p", { role: "alert", className: "error", children: reads.error }),
    report && /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)(import_jsx_runtime7.Fragment, { children: [
      /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("p", { className: "hint statements-behind-summary", children: [
        /* @__PURE__ */ (0, import_jsx_runtime7.jsxs)("span", { children: [
          statements.length ? `${statements.length === 1 ? "1 statement" : `Top ${statements.length} statements`}${counter ? ` by ${counter[0]}` : ""}, since ${report.coverage?.first_seen || "the server started"}` : table ? `No recorded statement names ${tableName(table)} and reads without an index.` : `No recorded statement has ${counter ? counter[0] : "this counter"}.`,
          table && ` \xB7 ${report.match?.label || "text match, not proof"}`
        ] }),
        /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
          NotesButton,
          {
            label: "Notes about these statements",
            notes: [...report.notes, ...coverageNotes(report.coverage), ...report.match?.caveats || []]
          }
        )
      ] }),
      /* @__PURE__ */ (0, import_jsx_runtime7.jsx)(
        StatementRows,
        {
          statements,
          order,
          table,
          connection,
          disabled: busy,
          settings,
          onAction: (action, r, rank) => {
            if (bound && bound.key === connectionKey(connection)) onDigest(r, rank, action, bound.key);
          }
        }
      )
    ] })
  ] });
}

// src/Health.tsx
var import_jsx_runtime8 = require("react/jsx-runtime");
var LESSONS = {
  lru: "How the buffer pool keeps pages",
  buffer_pool_warmup: "Cold and warm buffer pools",
  tmp: "Temporary tables",
  filesort: "Filesort",
  join: "Block nested loop and hash join",
  full_scan: "Full table scan"
};
function uptime(seconds) {
  if (seconds === null || !Number.isFinite(seconds)) return "\u2014";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor(seconds % 86400 / 3600);
  const m = Math.floor(seconds % 3600 / 60);
  if (d) return `${d} d ${h} h`;
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min`;
  return `${Math.round(seconds)} s`;
}

// src/Report.tsx
var import_jsx_runtime9 = require("react/jsx-runtime");
var REPORT_ESTIMATE = "about 10 to 20 s";
var STATUS_WORD = {
  problem: "Problem",
  watch: "Watch",
  unknown: "Not judged",
  info: "No threshold",
  ok: "OK"
};
function plural(n, one, many) {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
function clock(iso) {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}
function attributionNotes(report) {
  const a = report.attribution;
  if (!a) return [];
  if (!a.available) return [a.gate || "Statements could not be linked to these numbers.", ...a.fix && a.fix !== a.gate ? [a.fix] : []];
  const out = coverageNotes(a.coverage);
  out.unshift("Statement counters are cumulative since the server started or the digest table was truncated, not over Health's window.");
  const w = a.health_window;
  if (w && w.elapsed_s !== null && w.select_full_join !== null)
    out.push(`Health counted ${w.select_full_join.toLocaleString()} joins without an index in its ${Math.round(w.elapsed_s)} s window.`);
  if (a.coverage?.overflow)
    out.push(
      `The digest table is full: ${a.coverage.overflow.executions.toLocaleString()} executions were summed into its overflow row and are not attributed.`
    );
  for (const t of a.full_scans || []) if (t.capped) out.push(`For ${t.schema}.${t.table} only the heaviest digests that name it were checked.`);
  return [...out, ...a.caveats || []];
}
function ReportDiagnosticBody({
  report,
  hostLabel,
  selection
}) {
  const findings = report.findings;
  const byId = new Map(findings.map((f) => [f.id, f]));
  const first = report.look_first ? byId.get(report.look_first) || null : null;
  const look = findings.filter((f) => f.id !== first?.id && (f.status === "problem" || f.status === "watch"));
  const info = findings.filter((f) => f.status === "info");
  const unknown = findings.filter((f) => f.status === "unknown");
  const failures = report.header.failures || [];
  const s = report.summary;
  const notes = [
    ...report.notes,
    ...attributionNotes(report),
    ...report.checklist.not_checked.map((n) => `Not checked: ${n.label}. ${n.reason}`)
  ];
  const row = (f, prefix = "finding", detail) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
    GroupedRow,
    {
      rowKey: `${prefix}:${f.id}`,
      selection,
      tone: f.status,
      toneLabel: STATUS_WORD[f.status],
      label: f.title,
      detail,
      value: f.headline ? /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { title: f.headline, children: f.headline }) : void 0,
      attrs: { "data-finding": f.id },
      className: "report-finding",
      wrap: true
    },
    `${prefix}:${f.id}`
  );
  return /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
    /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(SummaryLine, { tone: s.status, toneLabel: STATUS_WORD[s.status], notes, children: s.text }),
    /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
      FactStrip,
      {
        label: "Full analysis",
        facts: [
          ["Server", engineLabel({ engine: report.engine, server_version: report.server_version, server_product: report.server_product })],
          ["Host", hostLabel || ""],
          ["Uptime", uptime(report.uptime_s)],
          ["Taken", `${clock(report.taken_at)}, in ${(report.header.elapsed_s ?? 0).toFixed(1)} s`],
          ["Databases", report.header.scope.text],
          ["Views read", failures.length ? `${report.header.views.length - failures.length} of ${report.header.views.length}` : `all ${report.header.views.length}`]
        ]
      }
    ),
    !report.attribution.available && report.attribution.gate && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(PrerequisiteGate, { children: report.attribution.gate }),
    first && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(GroupedList, { id: "report-first-h", title: "Look at first", className: "report-first", children: row(first, "first") }),
    /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
      GroupedList,
      {
        id: "report-findings-h",
        title: "To look at",
        count: look.length,
        className: "report-look",
        aside: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("span", { children: [
          plural(look.filter((f) => f.status === "problem").length, "problem", "problems"),
          ", ",
          look.filter((f) => f.status === "watch").length,
          " to watch"
        ] }),
        children: look.length ? look.map((f) => row(f)) : /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(GroupedRow, { rowKey: "none", tone: first ? "info" : "ok", label: first ? "No other problem or watch finding" : "Nothing to look at: no problem or watch finding" })
      }
    ),
    (unknown.length > 0 || failures.length > 0) && /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(
      GroupedList,
      {
        id: "report-unknown-h",
        title: "Not judged",
        count: unknown.length + failures.length,
        className: "report-unknown",
        info: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: "Checks that could not run, with the reason: a missing privilege, Performance Schema off, or a view that failed. They never count as OK." }),
        children: [
          failures.map((x) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
            GroupedRow,
            {
              rowKey: `view:${x.view}`,
              selection,
              tone: "unknown",
              toneLabel: x.status === "skipped" ? "Skipped" : "Failed",
              label: x.label,
              detail: x.error,
              attrs: { "data-view-failure": x.view },
              className: "report-finding"
            },
            `view:${x.view}`
          )),
          unknown.map((f) => row(f, "finding", f.verdict))
        ]
      }
    ),
    /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(GroupedList, { id: "report-sections-h", title: "Sections", count: report.sections.length, className: "report-sections", children: report.sections.map((section) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
      GroupedRow,
      {
        rowKey: `section:${section.key}`,
        selection,
        tone: section.status,
        toneLabel: STATUS_WORD[section.status],
        label: section.title,
        value: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { title: section.summary, children: section.summary }),
        wrap: true,
        attrs: { "data-section": section.key },
        className: "report-section"
      },
      section.key
    )) }),
    info.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(Disclosure, { title: "For information, no rule applies", count: info.length, className: "report-info", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("ul", { className: "grouped-rows", "data-rows": true, children: info.map((f) => row(f)) }) })
  ] });
}
function buildReportInspector({
  report,
  selectionKey,
  onSelectKey,
  boundKey,
  actionsOff = false,
  connection,
  settings,
  onDigest,
  onOpenLink,
  onOpenLesson
}) {
  const byId = new Map(report.findings.map((f) => [f.id, f]));
  const failures = report.header.failures || [];
  const [type, name] = selectionKey.split(/:(.*)/s);
  function openFromInspector(key) {
    onSelectKey(key, true);
    window.setTimeout(() => {
      const title = document.querySelector(".view-inspector .inspector-title h3");
      if (!title) return;
      title.tabIndex = -1;
      title.focus();
    });
  }
  function attribution(finding) {
    const a = report.attribution;
    const act = (action, r, rank) => {
      if (boundKey && onDigest) onDigest(r, rank, action, boundKey);
    };
    const about = /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: "Statements Performance Schema counted the same events for, read with the analysis. Their counters run since the server started or the digest table was truncated, not over Health's window." });
    if (!a.available)
      return /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Statements behind it", info: about, children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint", children: a.gate || "Statements could not be linked to this number." }) });
    if (finding.attribution === "full_joins") {
      const statements = a.full_joins?.statements || [];
      return /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Statements behind it", info: about, children: statements.length && connection && settings ? /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
        StatementRows,
        {
          statements,
          order: "full_join",
          connection,
          disabled: actionsOff,
          settings,
          onAction: act
        }
      ) : /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint", children: "No recorded statement has joins without an index." }) });
    }
    return /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Statements that name each table", info: about, children: (a.full_scans || []).map((t) => {
      const table = { schema: t.schema, table: t.table };
      return /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("div", { className: "report-scan-table", "data-table": `${t.schema}.${t.table}`, children: [
        /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("h5", { children: [
          /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("code", { children: [
            t.schema,
            ".",
            t.table
          ] }),
          " ",
          /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { className: "hint", children: t.label })
        ] }),
        t.error ? /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("p", { className: "error", children: [
          "Could not read: ",
          t.error
        ] }) : t.statements.length && connection && settings ? /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
          StatementRows,
          {
            statements: t.statements,
            order: "no_index",
            table,
            connection,
            disabled: actionsOff,
            settings,
            onAction: act
          }
        ) : /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint", children: "No recorded statement names it and reads without an index." })
      ] }, `${t.schema}.${t.table}`);
    }) });
  }
  function findingInspector(f) {
    const lesson = f.learn && LESSONS[f.learn] && onOpenLesson ? f.learn : "";
    const action = f.action;
    return {
      key: selectionKey,
      title: f.title,
      meta: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(StatusPill, { tone: f.status, label: STATUS_WORD[f.status] }),
        f.headline && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { children: f.headline })
      ] }),
      actions: f.links.length && onOpenLink || lesson ? /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("div", { className: "inspector-links", children: [
        onOpenLink && f.links.map((link, i) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
          "button",
          {
            type: "button",
            className: "button",
            title: link.label,
            disabled: link.view === "top" && actionsOff,
            onClick: () => onOpenLink(link, f, boundKey || ""),
            children: link.label
          },
          i
        )),
        lesson && /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("button", { type: "button", className: "button", onClick: () => onOpenLesson?.(lesson), children: [
          "Lesson: ",
          LESSONS[lesson]
        ] })
      ] }) : void 0,
      body: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: f.status === "unknown" ? "Why it was not judged" : "Verdict", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: f.verdict || "No verdict." }) }),
        f.numbers.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Numbers", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(Fields, { label: `${f.title}: numbers`, rows: f.numbers.map((n) => [isIdentifier(n.label) ? /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("code", { children: n.label }) : n.label, n.text]) }) }),
        f.settings.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Settings", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(
          Fields,
          {
            label: `${f.title}: settings`,
            rows: f.settings.map((v) => [
              /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("code", { children: v.name }, "n"),
              /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
                v.display ?? v.value ?? "?",
                v.default !== null && v.default !== void 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("span", { className: "hint", children: [
                  " \xB7 default ",
                  v.default
                ] })
              ] })
            ])
          }
        ) }),
        f.rule && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Rule", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("p", { children: [
          "Rule of thumb: ",
          f.rule
        ] }) }),
        f.why && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Why it matters", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: f.why }) }),
        action && (action.text || action.sql || action.config) && /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(InspectorSection, { title: "What to do", children: [
          action.text && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: action.text }),
          action.sql && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(CopySql, { sql: action.sql, label: `${f.title}: statement` }),
          action.config && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(CopySql, { sql: action.config, label: `${f.title}: my.cnf line` }),
          (action.sql || action.config) && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint", children: "Suggested only: myflames never runs or applies it." }),
          f.caveat && /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("p", { className: "hint perf-caveat", children: [
            "Caveat: ",
            f.caveat
          ] })
        ] }),
        !action && f.caveat && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Caveat", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint perf-caveat", children: f.caveat }) }),
        f.attribution && attribution(f)
      ] })
    };
  }
  if (type === "finding" || type === "first") {
    const f = byId.get(name);
    if (f) return findingInspector(f);
  } else if (type === "section") {
    const section = report.sections.find((x) => x.key === name);
    if (section) {
      const own = section.findings.map((id) => byId.get(id)).filter((f) => !!f);
      return {
        key: selectionKey,
        title: section.title,
        meta: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(StatusPill, { tone: section.status, label: STATUS_WORD[section.status] }),
        body: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
          /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(InspectorSection, { title: "Summary", children: [
            /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: section.summary }),
            section.notes.length > 0 && section.notes.join(" ") !== section.summary && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("ul", { className: "report-notes", children: section.notes.map((n, i) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("li", { children: n }, i)) })
          ] }),
          own.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: `Findings (${own.length})`, children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("ul", { className: "report-section-findings", children: own.map((f) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("li", { "data-finding": f.id, children: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("button", { type: "button", className: "report-section-link", onClick: () => openFromInspector(`finding:${f.id}`), children: [
            /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(StatusPill, { tone: f.status, label: STATUS_WORD[f.status] }),
            /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { className: "report-section-title", children: f.title }),
            f.headline && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("span", { className: "report-section-value", children: f.headline })
          ] }) }, f.id)) }) }),
          /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)("p", { className: "hint", children: [
            "From ",
            section.views.join(", ")
          ] })
        ] })
      };
    }
  } else if (type === "view") {
    const failure = failures.find((x) => x.view === name);
    const view = report.header.views.find((v) => v.key === name);
    if (failure) {
      return {
        key: selectionKey,
        title: failure.label,
        meta: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(StatusPill, { tone: "unknown", label: failure.status === "skipped" ? "Skipped" : "Failed" }),
        body: /* @__PURE__ */ (0, import_jsx_runtime9.jsxs)(import_jsx_runtime9.Fragment, { children: [
          /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: failure.status === "skipped" ? "Why it was skipped" : "Why it failed", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { children: failure.error }) }),
          /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("p", { className: "hint", children: "The sections this view feeds say what is missing; nothing else stopped." }),
          view && view.notes.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime9.jsx)(InspectorSection, { title: "Notes", children: /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("ul", { className: "report-notes", children: view.notes.map((n, i) => /* @__PURE__ */ (0, import_jsx_runtime9.jsx)("li", { children: n }, i)) }) })
        ] })
      };
    }
  }
  return null;
}

// src/tuningRules.ts
var TUNING_CATEGORIES = {
  innodb: {
    key: "innodb",
    name: "Buffer pool and redo",
    weight: 0.3,
    description: "Buffer pool pressure, dirty pages, waits for a free page and the redo log size."
  },
  queries: {
    key: "queries",
    name: "Queries",
    weight: 0.25,
    description: "Joins without an index, temporary tables on disk, sort merge passes and the slow query log."
  },
  connections: {
    key: "connections",
    name: "Connections and threads",
    weight: 0.2,
    description: "Connection limit, thread cache and connections that ended without closing."
  },
  tables: {
    key: "tables",
    name: "Tables and caches",
    weight: 0.15,
    description: "Table open cache, table definition cache, tables without a primary key and MyISAM tables."
  },
  security: {
    key: "security",
    name: "Security",
    weight: 0.1,
    description: "Accounts without a password, anonymous accounts, root from any host and encrypted transport."
  }
};
var CATEGORY_ORDER = ["innodb", "queries", "connections", "tables", "security"];
var POINTS = { ok: 100, watch: 50, problem: 0 };
var GRADE_SCALE = [
  { grade: "A", min: 90 },
  { grade: "B", min: 80 },
  { grade: "C", min: 70 },
  { grade: "D", min: 50 },
  { grade: "F", min: 0 }
];
var PROBLEM_GRADE_CAP = "C";
function gradeRangeText(grade) {
  const i = GRADE_SCALE.findIndex((g) => g.grade === grade);
  const { min } = GRADE_SCALE[i];
  if (i === 0) return `${min} and above`;
  const max = GRADE_SCALE[i - 1].min - 1;
  if (i === GRADE_SCALE.length - 1) return `below ${max + 1}`;
  return `${min} to ${max}`;
}
function gradeScaleText() {
  return GRADE_SCALE.map((g) => `${g.grade} ${gradeRangeText(g.grade)}`).join(", ");
}
var SCORE_RULE = "Each measured check scores 100 when OK, 50 to watch and 0 for a problem. A category scores the mean of its measured checks, and the overall score weights the categories that have one (Buffer pool and redo 30%, Queries 25%, Connections and threads 20%, Tables and caches 15%, Security 10%). Checks that were not measured are left out.";
var GRADE_RULE = `Grades: ${gradeScaleText()}. A problem caps the grade at ${PROBLEM_GRADE_CAP}. The score is provisional, with no grade, until at least half the checks are measured and every category has one.`;
var SCORING_RULE = `${SCORE_RULE} ${GRADE_RULE}`;
function formatCount(n) {
  if (n === null || n === void 0 || !Number.isFinite(n)) return "?";
  const rounded = Math.abs(n) >= 100 || Number.isInteger(n) ? Math.round(n) : Math.round(n * 100) / 100;
  return rounded.toLocaleString("en-US");
}
function formatBytes(bytes2) {
  if (bytes2 === null || bytes2 === void 0 || !Number.isFinite(bytes2)) return "?";
  if (bytes2 < 1024) return `${Math.max(0, Math.round(bytes2))} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = bytes2 / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}
function formatMetric(m) {
  const { value, unit } = m;
  if (value === null || value === void 0) return "\u2014";
  if (unit === "%") return `${value.toFixed(1)}%`;
  if (unit === "B/s") return `${formatBytes(value)}/s`;
  if (unit === "/s") return `${formatCount(value)}/s`;
  if (unit === "pages/s" || unit === "ms/s") return `${formatCount(value)} ${unit}`;
  if (unit === "ms") return `${formatCount(value)} ms`;
  return formatCount(value);
}
function plural2(n, one, many) {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}
function observation(text, rule) {
  const t = (text || "").trim();
  const at = t.indexOf(" Rule of thumb: ");
  if (at > 0 && (!rule || t.slice(at).includes(rule.slice(0, 20)))) return t.slice(0, at).trim();
  return t;
}
function versionTuple(version) {
  if (!version) return null;
  const text = version.replace(/^5\.5\.5-/, "");
  const match = text.match(/^(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3] || 0)];
}
function atLeast(tuple, want) {
  if (!tuple) return null;
  for (let i = 0; i < want.length; i++) {
    const a = tuple[i] || 0;
    if (a !== want[i]) return a > want[i];
  }
  return true;
}
var PY_STATUSES = ["ok", "watch", "problem", "info", "unknown"];
function pyStatus(s) {
  return PY_STATUSES.includes(s || "") ? s : "unknown";
}
function timeOf(iso) {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}
function sourceOf(view, takenAt) {
  const names = {
    health: "Health",
    config: "Parameters",
    tables: "Tables",
    report: "Full analysis"
  };
  return { view, label: names[view], takenAt: takenAt || null };
}
function settingOf(s) {
  return { name: s.name, value: String(s.display ?? s.value ?? ""), default: s.default ?? null };
}
function metricItem(m, source) {
  const value = formatMetric(m);
  return {
    status: pyStatus(m.status),
    value,
    verdict: observation(m.why, m.rule) || value,
    rule: m.rule || null,
    why: m.explain || "",
    caveat: null,
    advice: null,
    actionSql: null,
    actionConfig: null,
    numbers: [{ label: m.label || m.key, text: value }, ...m.detail ? [{ label: "Detail", text: m.detail }] : []],
    settings: [],
    state: null,
    inputs: m.inputs || {},
    value_number: m.value ?? null,
    source
  };
}
function diagItem(d, source) {
  return {
    status: pyStatus(d.status),
    value: d.headline || "",
    verdict: d.verdict || d.headline || "",
    rule: d.rule || null,
    why: d.why || "",
    caveat: d.recommendation?.caveat || null,
    advice: d.recommendation?.text || null,
    actionSql: null,
    actionConfig: null,
    numbers: (d.numbers || []).filter(Boolean).map((n) => ({ label: n.label, text: n.text })),
    settings: (d.settings || []).filter(Boolean).map(settingOf),
    state: d.state ?? null,
    inputs: Object.fromEntries((d.numbers || []).filter(Boolean).map((n) => [n.key, n.value ?? null])),
    value_number: d.value ?? null,
    source
  };
}
function findingItem(f, source) {
  return {
    status: pyStatus(f.status),
    value: f.headline || "",
    verdict: f.verdict || f.headline || "",
    rule: f.rule || null,
    why: f.why || "",
    caveat: f.caveat || null,
    advice: f.action?.text || null,
    actionSql: f.action?.sql || null,
    actionConfig: f.action?.config || null,
    numbers: (f.numbers || []).map((n) => ({ label: n.label, text: n.text })),
    settings: (f.settings || []).filter((s) => s && s.name).map((s) => settingOf({ ...s, display: s.display ?? s.value })),
    state: f.state ?? null,
    inputs: Object.fromEntries((f.numbers || []).filter((n) => n.value !== void 0).map((n) => [n.label, n.value ?? null])),
    value_number: f.impact ?? null,
    source
  };
}
var Ctx = class {
  constructor(input) {
    this.findings = /* @__PURE__ */ new Map();
    this.report = input.report ?? null;
    this.health = input.health ?? null;
    this.config = input.config ?? null;
    this.tables = input.tables ?? null;
    const engine = this.report?.header?.server?.engine || this.report?.engine || this.health?.server?.engine || this.health?.engine || this.config?.engine || this.tables?.engine || "mysql";
    this.engine = engine.toLowerCase().includes("mariadb") ? "mariadb" : "mysql";
    this.version = this.report?.header?.server?.version || this.report?.server_version || this.health?.server?.version || this.health?.server_version || this.config?.server_version || this.tables?.server_version || null;
    this.product = this.report?.header?.server?.product || this.report?.server_product || this.health?.server_product || this.config?.server_product || this.tables?.server_product || null;
    this.tuple = versionTuple(this.version);
    for (const f of this.report?.findings || []) this.findings.set(f.id, f);
  }
  get mariadb() {
    return this.engine === "mariadb";
  }
  atLeast(want) {
    return atLeast(this.tuple, want);
  }
  /** When the Full analysis read a view (its own taken_at), else the report's. */
  reportTaken(view) {
    const v = this.report?.header?.views?.find((x) => x.key === view);
    if (v && (v.status === "failed" || v.status === "skipped")) return null;
    return v?.taken_at || this.report?.taken_at || null;
  }
  reportRan(view) {
    const v = this.report?.header?.views?.find((x) => x.key === view);
    return !!this.report && (!v || v.status !== "failed" && v.status !== "skipped");
  }
  finding(id) {
    return this.findings.get(id) ?? null;
  }
  /** The newer of the view's own item and the Full analysis's (the view wins a tie). */
  newer(own, fromReport) {
    if (!own) return fromReport;
    if (!fromReport) return own;
    return timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own;
  }
  metric(key) {
    const m = this.health?.metrics?.find((x) => x.key === key);
    const own = m ? metricItem(m, sourceOf("health", this.health?.taken_at)) : null;
    let fromReport = null;
    if (this.reportRan("health")) {
      const src = sourceOf("report", this.reportTaken("health"));
      const listed = this.report?.health_metrics?.find((x) => x.key === key);
      if (listed) fromReport = metricItem(listed, src);
      else {
        const f = this.finding(`health.${key}`);
        if (f) fromReport = findingItem(f, src);
      }
    }
    return this.newer(own, fromReport);
  }
  diag(key) {
    const d = this.health?.diagnostics?.find((x) => x.key === key);
    const own = d ? diagItem(d, sourceOf("health", this.health?.taken_at)) : null;
    const f = this.reportRan("health") ? this.finding(`health.${key}`) : null;
    return this.newer(own, f ? findingItem(f, sourceOf("report", this.reportTaken("health"))) : null);
  }
  configCheck(key) {
    const c = this.config?.checks?.find((x) => x.key === key);
    const own = c ? {
      status: pyStatus(c.status),
      value: c.value || "",
      verdict: observation(c.why, c.rule) || c.value || "",
      rule: c.rule || null,
      why: c.explain || "",
      caveat: c.caveat || null,
      advice: c.action?.text || null,
      actionSql: c.action?.sql || null,
      actionConfig: c.action?.config || null,
      numbers: [],
      settings: (c.settings || []).filter(Boolean).map(settingOf),
      state: null,
      inputs: {},
      value_number: null,
      source: sourceOf("config", this.config?.taken_at)
    } : null;
    const f = this.reportRan("config") ? this.finding(`config.${key}`) : null;
    return this.newer(own, f ? findingItem(f, sourceOf("report", this.reportTaken("config"))) : null);
  }
  serverCheck(key) {
    const f = this.reportRan("checks") ? this.finding(`checks.${key}`) : null;
    return f ? findingItem(f, sourceOf("report", this.reportTaken("checks"))) : null;
  }
  /** A variable's value from Parameters (every global variable), the checks' settings, or Health. */
  variable(name) {
    const lower = name.toLowerCase();
    const p = this.config?.parameters?.items?.find((x) => x.name.toLowerCase() === lower);
    if (p && p.value !== null) return p.value;
    for (const c of this.config?.checks || [])
      for (const s of c.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    for (const d of this.health?.diagnostics || [])
      for (const s of d.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    for (const f of this.report?.findings || [])
      for (const s of f.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    return null;
  }
  /** Parameters was read (config-1.0 with the parameters-1.0 catalog). */
  get parameters() {
    return this.config?.parameters?.items ? this.config.parameters : null;
  }
};
function globalChange(engine, name, value, dynamic = true) {
  const line = `${name} = ${value}  # under [mysqld]`;
  if (!dynamic) return { optionFile: line };
  if (engine === "mysql") return { sql: `SET PERSIST ${name} = ${value};` };
  return { sql: `SET GLOBAL ${name} = ${value};`, optionFile: line };
}
function joinChanges(changes) {
  const sql = changes.map((c) => c.sql).filter(Boolean).join("\n");
  const optionFile = changes.map((c) => c.optionFile).filter(Boolean).join("\n");
  return { ...sql ? { sql } : {}, ...optionFile ? { optionFile } : {} };
}
function assigned(sql, name) {
  if (!sql) return null;
  const m = sql.match(new RegExp(`${name}\\s*=\\s*(\\d+)`));
  return m ? Number(m[1]) : null;
}
var RANK = { problem: 0, watch: 1, ok: 2, unknown: 3 };
function mapStatus(item, info) {
  if (item.status === "info") {
    if (typeof info === "function") return info(item);
    return info === "ok" ? "ok" : "unknown";
  }
  return item.status;
}
function build(spec) {
  const items = spec.items.filter((x) => !!x);
  const base = {
    key: spec.key,
    title: spec.title,
    category: spec.category,
    measureWith: spec.measureWith,
    ...spec.docs ? { docs: spec.docs } : {}
  };
  if (!items.length) {
    return {
      ...base,
      status: "unknown",
      applies: true,
      value: "Not measured",
      verdict: "",
      unknownReason: spec.missing,
      rule: null,
      why: spec.why || "",
      evidence: [],
      settings: [],
      source: null
    };
  }
  if (spec.notApplicable && items.every((i) => spec.notApplicable(i))) {
    const item2 = items[0];
    return {
      ...base,
      status: "unknown",
      applies: false,
      value: "Does not apply",
      verdict: item2.verdict,
      unknownReason: item2.verdict,
      rule: item2.rule,
      why: spec.why || item2.why,
      evidence: item2.numbers,
      settings: item2.settings,
      source: item2.source
    };
  }
  const ranked = items.map((item2) => ({ item: item2, status: mapStatus(item2, spec.info) })).sort((a, b) => RANK[a.status] - RANK[b.status]);
  const { item, status } = ranked[0];
  const ordered = ranked.map((r) => r.item);
  const evidence = spec.evidence ? spec.evidence(item, ordered) : item.numbers;
  const settings = [];
  for (const i of ordered) for (const s of i.settings) if (!settings.some((x) => x.name === s.name)) settings.push(s);
  return {
    ...base,
    status,
    applies: true,
    value: status === "unknown" ? "Not measured" : spec.value ? spec.value(item, ordered) : item.value,
    verdict: spec.verdict ? spec.verdict(item, ordered) : item.verdict,
    ...status === "unknown" ? { unknownReason: item.verdict || `${item.source.label} did not judge this.` } : {},
    rule: item.rule,
    why: spec.why || item.why,
    evidence,
    settings,
    ...status === "problem" || status === "watch" ? { fix: spec.fix?.(item, status, ordered) } : {},
    source: item.source
  };
}
var LOAD_HEALTH = "Load Health, or run the full analysis, to measure this.";
var LOAD_CONFIG = "Open Parameters, or run the full analysis, to read this setting.";
var LOAD_TABLES = "Load Tables, or run the full analysis, to check the tables.";
var RUN_ANALYSIS = "Run the full analysis to read the accounts (it needs SELECT on mysql.user).";
function bufferPool(ctx) {
  const diag = ctx.diag("buffer_pool");
  const hit = ctx.metric("bp_hit_ratio");
  return build({
    key: "buffer_pool",
    title: "Buffer pool",
    category: "innodb",
    items: [diag],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Python's info here is "not full": with free pages left nothing is evicted
    // to make room (state "room"), so that is ok; "warming" cannot be judged yet
    // (health.py: "Reads from disk are expected while it warms").
    info: (i) => i.state === "room" ? "ok" : "unknown",
    evidence: (i) => hit && hit.status !== "unknown" && hit.value_number !== null ? [{ label: "Hit ratio in the window", text: hit.value }, ...i.numbers] : i.numbers,
    fix: (i) => {
      const change = globalChange(ctx.engine, "innodb_buffer_pool_size", "<bytes>");
      const cap = ctx.mariadb && ctx.variable("innodb_buffer_pool_size_max") !== null ? " MariaDB grows it online only up to innodb_buffer_pool_size_max (0 means the startup size); beyond that it needs a restart." : "";
      return {
        text: (i.advice || "Look for queries that scan large tables first (Top statements); then consider a larger innodb_buffer_pool_size.") + cap,
        ...change,
        caveat: i.caveat || void 0
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-resize.html"
  });
}
function dirtyPages(ctx) {
  return build({
    key: "dirty_pages",
    title: "Dirty pages",
    category: "innodb",
    items: [ctx.metric("bp_dirty")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // No io_capacity advice: MySQL 8.4's innodb_io_capacity default is 10000
    // (M/storage/innobase/handler/ha_innodb.cc, mysql-8.4.10 tag), so a fixed
    // "2000" would cut flushing; MariaDB's lwm is 0, so sitting near the maximum
    // is normal there (Ma/storage/innobase/handler/ha_innodb.cc:19284-19290).
    fix: () => ({
      text: "A high share alone is not a stall. Check Waits for a free page and Redo log size: when both are OK, InnoDB is keeping up."
    })
  });
}
function freePageWaits(ctx) {
  const depthDefault = ctx.mariadb ? "1536" : "1024 per buffer pool instance";
  return build({
    key: "free_page_waits",
    title: "Waits for a free page",
    category: "innodb",
    items: [ctx.metric("bp_wait_free")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    fix: () => {
      const current = ctx.variable("innodb_lru_scan_depth");
      return {
        text: `Sessions waited for a free page: the page cleaner did not keep enough free pages ahead of reads. A larger buffer pool helps when Buffer pool shows it is full; a deeper innodb_lru_scan_depth (now ${current ?? "unknown"}, default ${depthDefault}) makes the page cleaner keep more free pages.`,
        ...globalChange(ctx.engine, "innodb_lru_scan_depth", "<pages>"),
        caveat: "Raise it only when the storage has spare I/O capacity: a deeper scan makes the page cleaner read and write more."
      };
    }
  });
}
function redoLog(ctx) {
  const diag = ctx.diag("redo");
  const cfg = ctx.configCheck("redo");
  return build({
    key: "redo_log",
    title: "Redo log size",
    category: "innodb",
    items: [diag, cfg],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    fix: (_item, _status, all) => {
      const name = ctx.mariadb ? "innodb_log_file_size" : "innodb_redo_log_capacity";
      let target = null;
      for (const i of all) {
        const fromPython = assigned(i.actionSql, name) ?? assigned(i.advice, name);
        if (fromPython) target = Math.max(target || 0, fromPython);
      }
      const capacityNumber = all.map((i) => i.inputs.capacity).find((c) => typeof c === "number" && c > 0);
      const capacity = (typeof capacityNumber === "number" ? capacityNumber : null) ?? ctx.health?.server?.redo_capacity_bytes ?? (Number(ctx.variable(name)) || null);
      if (target !== null && capacity !== null && target <= capacity) target = null;
      const caveat = "A short window may be a burst or a lull; compare several refreshes. A larger redo log uses more disk and makes crash recovery longer.";
      const legacy = !ctx.mariadb && ctx.atLeast([8, 0, 30]) === false;
      const legacySized = legacy && all.some((i) => /innodb_log_file_size x innodb_log_files_in_group/.test(i.advice || ""));
      if (target === null && !legacySized) {
        const logWaits = all.some(
          (i) => typeof i.inputs.log_waits === "number" && i.inputs.log_waits > 0 || /waits for space in the log buffer/.test(i.verdict)
        );
        return {
          text: logWaits ? "Waits for space in the log buffer raised the status. They point at innodb_log_buffer_size, not the redo log size." : "The checkpoint age raised the status, while the redo capacity already covers the measured rate. InnoDB flushed harder to free redo space; compare several refreshes before changing the size.",
          caveat
        };
      }
      const value = target ? String(target) : "<bytes>";
      const size = target ? ` (${formatBytes(target)})` : "";
      if (!ctx.mariadb) {
        if (legacy)
          return {
            text: `About an hour of redo at the measured rate${size}. This MySQL has no innodb_redo_log_capacity: set the log files in the option file and restart.`,
            optionFile: `innodb_log_file_size = <bytes per file>  # under [mysqld]
innodb_log_files_in_group = 2`,
            caveat
          };
        return {
          text: `About an hour of redo at the measured rate${size}, rounded up to 256 MiB. It resizes online.`,
          ...globalChange("mysql", "innodb_redo_log_capacity", value),
          caveat
        };
      }
      if (ctx.atLeast([10, 9, 0]) === false)
        return {
          text: `About an hour of redo at the measured rate${size}. On MariaDB before 10.9 innodb_log_file_size is read-only: set it in the option file and restart.`,
          optionFile: `innodb_log_file_size = ${value}  # under [mysqld]`,
          caveat
        };
      return {
        text: `About an hour of redo at the measured rate${size}, rounded up to 256 MiB. SET GLOBAL resizes it online and waits until the resize finishes.`,
        ...globalChange("mariadb", "innodb_log_file_size", value),
        caveat
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html"
  });
}
function joinsWithoutIndex(ctx) {
  return build({
    key: "joins_without_index",
    title: "Joins without an index",
    category: "queries",
    items: [ctx.metric("select_full_join")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Select_full_join counts a full scan of a later table in a join; on MySQL a
    // hash join's inner table counts too and is read once, not once per row
    // (M/sql/join_optimizer/access_path.cc:1906-1925; Ma/sql/sql_select.cc:16495-16565).
    why: "Select_full_join counts joins where a later table was read by a full scan because no index matched the join. On MySQL a hash join's inner table counts too; it is read once rather than once per row. Without an index the server reads every row of that table for the join.",
    fix: () => ({
      text: "Find the statements in Top statements ordered by joins without an index, then add an index on the join columns of the table read by full scan. Explain the statement before and after.",
      caveat: "Every index adds work to writes on that table. Small lookup tables are often fine to scan."
    })
  });
}
function tempTables(ctx) {
  return build({
    key: "temp_tables",
    title: "Temporary tables on disk",
    category: "queries",
    items: [ctx.diag("tmp_tables"), ctx.configCheck("tmp_tables")],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    fix: (item, _status, all) => {
      const findFirst = "Find the statements with the temporary-table-on-disk flag in Top statements; an index that avoids the temporary table helps more than a bigger limit.";
      const storage = ctx.mariadb ? null : ctx.variable("internal_tmp_mem_storage_engine");
      const temptable = !!storage && storage.toLowerCase() === "temptable";
      const shared = "Every TempTable table shares temptable_max_ram: it is one memory budget for the whole server, so raise it only with memory to spare.";
      if (ctx.mariadb)
        return {
          text: item.advice || findFirst,
          sql: "SET SESSION tmp_memory_table_size = <bytes>;\nSET SESSION max_heap_table_size = <bytes>;",
          caveat: item.caveat || void 0
        };
      if (!storage)
        return {
          text: `${item.advice || findFirst} Read internal_tmp_mem_storage_engine first (Parameters): it decides which limit applies.`,
          caveat: item.caveat || void 0
        };
      if (!temptable)
        return {
          text: item.advice || findFirst,
          sql: "SET SESSION tmp_table_size = <bytes>;\nSET SESSION max_heap_table_size = <bytes>;",
          caveat: item.caveat || void 0
        };
      const hits = (i, key, label) => {
        const v = i.inputs[key] ?? i.inputs[label];
        return typeof v === "number" ? v : null;
      };
      const ramOnly = all.some((i) => {
        const ram = hits(i, "hit_max_ram", "Hit temptable_max_ram (all TempTable memory)");
        const perTable = hits(i, "hit_tmp_table_size", "Hit tmp_table_size (per table)");
        return ram !== null && ram > 0 && !(perTable !== null && perTable > 0);
      }) || all.some((i) => /temptable_max_ram \(global\) is the limit/.test(i.advice || ""));
      if (ramOnly)
        return {
          text: `${findFirst} Here TempTable's total memory ran out, so temptable_max_ram (global) is the limit to look at, not tmp_table_size.`,
          ...globalChange("mysql", "temptable_max_ram", "<bytes>"),
          caveat: shared
        };
      if (ctx.atLeast([8, 0, 28]) === false)
        return {
          text: `${findFirst} Before MySQL 8.0.28 TempTable has no per-table limit; temptable_max_ram (global) decides when its tables move to disk.`,
          ...globalChange("mysql", "temptable_max_ram", "<bytes>"),
          caveat: shared
        };
      return {
        text: item.advice || findFirst,
        sql: "SET SESSION tmp_table_size = <bytes>;",
        caveat: item.caveat || void 0
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/internal-temporary-tables.html"
  });
}
function sortMergePasses(ctx) {
  return build({
    key: "sort_merge_passes",
    title: "Sort merge passes",
    category: "queries",
    items: [ctx.diag("sorts")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    fix: (item) => ({
      text: item.advice || "Find the sorting statements in Top statements first: an index in ORDER BY or GROUP BY order removes the sort.",
      sql: "SET SESSION sort_buffer_size = <bytes>;",
      caveat: item.caveat || "A large global sort_buffer_size lets every concurrent sort use up to that much memory."
    })
  });
}
function slowLog(ctx) {
  return build({
    key: "slow_log",
    title: "Slow query log",
    category: "queries",
    items: [ctx.configCheck("slow_log")],
    measureWith: ["config", "report"],
    missing: LOAD_CONFIG,
    // On with a 10 s threshold is still on: Python only declines to add a rule.
    info: "ok",
    why: "The slow query log records statements that ran longer than long_query_time. Slow_queries counts them even when the log is off, but only the log records each slow statement with its own text and timing.",
    fix: () => {
      const changes = joinChanges([
        globalChange(ctx.engine, "slow_query_log", "ON"),
        globalChange(ctx.engine, "long_query_time", "<seconds>")
      ]);
      return {
        text: "The slow query log is off, so slow statements are not written to a log. Turn it on with a threshold that suits this server (long_query_time defaults to 10 s).",
        ...changes,
        caveat: "A low threshold on a busy server writes a lot of log. A new long_query_time applies to new connections only" + (ctx.mariadb ? ", and so does turning the log on: each MariaDB connection copies the setting when it connects, so pooled connections keep the old one." : ".")
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html"
  });
}
function connections(ctx) {
  const now = ctx.metric("connections_used");
  const peak = ctx.configCheck("max_connections");
  return build({
    key: "connections",
    title: "Connection limit",
    category: "connections",
    items: [now, peak],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    value: () => {
      if (now && now.status !== "unknown") {
        const { Threads_connected: open, max_connections: limit, Max_used_connections: top } = now.inputs;
        if (typeof open === "number" && typeof limit === "number")
          return `${formatCount(open)} of ${formatCount(limit)} open${typeof top === "number" ? `, peak ${formatCount(top)}` : ""}`;
        const detail = now.numbers.find((n) => n.label === "Detail")?.text || "";
        const m = detail.match(/^([\d,]+) of max_connections ([\d,]+)(?:; peak ([\d,]+))?/);
        if (m) return `${m[1]} of ${m[2]} open${m[3] ? `, peak ${m[3]}` : ""}`;
        return `${now.value} in use`;
      }
      return peak?.verdict || "Measured";
    },
    evidence: (_item, all) => {
      const out = [];
      for (const i of all) {
        if (i.status === "unknown") continue;
        out.push({ label: i.source.view === "config" || i === peak ? "Since startup" : "Now", text: i.verdict });
      }
      return out;
    },
    verdict: (_i, all) => all.filter((i) => i.status !== "unknown").map((i) => i.verdict).join(" ") || _i.verdict,
    fix: (_item, status, all) => {
      const change = globalChange(ctx.engine, "max_connections", "<n>");
      const refusals = all.some(
        (i) => typeof i.inputs.Connection_errors_max_connections === "number" && i.inputs.Connection_errors_max_connections > 0 || /refused/i.test(i.verdict)
      );
      return {
        text: (refusals ? "Clients were refused at max_connections. " : status === "problem" ? "Open connections reached 90% of max_connections. " : "") + "Find out why connections pile up first (Sessions: idle transactions, lock waits, a pool that grows). A connection pool in the application caps them; raise the limit only with memory for the extra sessions.",
        ...change,
        caveat: "Each connection can allocate its own sort, join and temporary-table memory when a statement needs it, so a higher limit raises the worst case." + (ctx.mariadb ? "" : " MySQL may lower max_connections at startup to fit open_files_limit.")
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/too-many-connections.html"
  });
}
function threadCache(ctx) {
  return build({
    key: "thread_cache",
    title: "Thread cache",
    category: "connections",
    items: [ctx.diag("thread_cache")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    notApplicable: (i) => i.state === "not_applicable",
    fix: (item) => {
      return {
        text: item.advice || `Raise thread_cache_size toward the number of connections that arrive in a burst (default ${ctx.mariadb ? "256, at most max_connections" : "8 + max_connections / 100, at most 100"}).`,
        ...globalChange(ctx.engine, "thread_cache_size", "<n>"),
        caveat: item.caveat || "Creating a thread costs little, so the gain is modest."
      };
    }
  });
}
function droppedConnections(ctx) {
  return build({
    key: "dropped_connections",
    title: "Connections dropped",
    category: "connections",
    items: [ctx.metric("aborted_clients")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // No wait_timeout or max_allowed_packet values: 28800 is already the default on both
    // engines, and max_allowed_packet defaults differ (64 MiB MySQL, 16 MiB MariaDB).
    fix: () => ({
      text: "Look at the client side first: applications that exit or are killed without closing, pools that keep idle connections longer than wait_timeout, network drops, or packets larger than max_allowed_packet."
    })
  });
}
function tableOpenCache(ctx) {
  return build({
    key: "table_open_cache",
    title: "Table open cache",
    category: "tables",
    items: [ctx.diag("table_cache"), ctx.configCheck("table_open_cache")],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    // Table_open_cache_overflows is about table_open_cache per instance
    // (M/sql/table_cache.h:381-391), not the table definition cache.
    fix: (item) => ({
      text: "Raise table_open_cache so the tables in use fit, and check that open_files_limit covers it.",
      ...globalChange(ctx.engine, "table_open_cache", "<n>"),
      caveat: item.caveat || "Each open table uses memory and may hold a file descriptor."
    })
  });
}
function tableDefinitionCache(ctx) {
  return build({
    key: "table_definition_cache",
    title: "Table definition cache",
    category: "tables",
    items: [ctx.diag("table_definitions")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Opened_table_definitions counts new shares (M/sql/dd_table_share.cc:2392, Ma/sql/table.cc:3580);
    // defaults: MySQL min(400 + table_open_cache / 2, 2000) (M/sql/mysqld.cc:10815-10825), MariaDB 400.
    fix: (item) => ({
      text: "Raise table_definition_cache to cover the tables in use.",
      ...globalChange(ctx.engine, "table_definition_cache", "<n>"),
      caveat: item.caveat || "Each cached definition uses memory."
    })
  });
}
function scopeText(t) {
  if (!t?.scope) return "the databases read";
  if (t.scope.schemas && t.scope.schemas.length) return t.scope.schemas.join(", ");
  return "every user database";
}
function noTables(reason, source) {
  return {
    status: "unknown",
    value: "",
    verdict: reason,
    rule: null,
    why: "",
    caveat: null,
    advice: null,
    actionSql: null,
    actionConfig: null,
    numbers: [],
    settings: [],
    state: null,
    inputs: {},
    value_number: null,
    source
  };
}
function reportHasNoTables(ctx) {
  return (ctx.finding("tables.engines")?.headline || "").trim().toLowerCase() === "no tables";
}
function primaryKeys(ctx) {
  const t = ctx.tables;
  const own = t ? (() => {
    const s = t.summary;
    if (!s.tables) return noTables(`No tables were read in ${scopeText(t)}, so there is nothing to judge.`, sourceOf("tables", t.taken_at));
    const noKey = s.no_usable_key || 0;
    const standIn = s.not_null_unique || 0;
    const status = noKey ? "problem" : standIn ? "watch" : "ok";
    const why = (t.why || []).filter((w) => w.key === "clustering" || w.key === "replication").map((w) => w.text);
    const listed = (t.missing_primary_keys || []).slice(0, 5);
    return {
      status,
      value: noKey || standIn ? [noKey ? plural2(noKey, "table without a key", "tables without a key") : "", standIn ? plural2(standIn, "on a unique key", "on a unique key") : ""].filter(Boolean).join(", ") : "All have a key",
      verdict: noKey || standIn ? `In ${scopeText(t)}: ${listed.map((m) => `${m.schema}.${m.table}`).join(", ")}${noKey + standIn > listed.length ? ", ..." : ""}.` : `Every table in ${scopeText(t)} has a primary key or a unique key on NOT NULL columns.`,
      rule: t.rules?.no_usable_key ? `${t.rules.no_usable_key}; ${t.rules.not_null_unique || ""}`.replace(/; $/, "") : null,
      why: why.join(" "),
      caveat: listed[0]?.caveat || null,
      advice: null,
      actionSql: listed[0]?.fixes?.[0]?.sql || null,
      actionConfig: null,
      numbers: listed.map((m) => ({ label: `${m.schema}.${m.table}`, text: m.clustered_on || m.kind })),
      settings: [],
      state: null,
      inputs: { no_usable_key: noKey, not_null_unique: standIn },
      value_number: noKey,
      source: sourceOf("tables", t.taken_at)
    };
  })() : null;
  let fromReport = null;
  if (ctx.reportRan("tables")) {
    const noKey = ctx.finding("tables.no_primary_key");
    const standIn = ctx.finding("tables.unique_stand_in");
    const src = sourceOf("report", ctx.reportTaken("tables"));
    const items = [noKey, standIn].filter((f) => !!f).map((f) => findingItem(f, src));
    items.sort((a, b) => RANK[a.status] - RANK[b.status]);
    if (items.length && reportHasNoTables(ctx)) {
      fromReport = noTables("The Full analysis read no tables in the databases it covered, so there is nothing to judge.", src);
    } else if (items.length) {
      const worst = items[0];
      const counts = [noKey, standIn].map((f) => f && f.status !== "ok" ? f.impact || 0 : 0);
      fromReport = {
        ...worst,
        value: counts[0] || counts[1] ? [counts[0] ? plural2(counts[0], "table without a key", "tables without a key") : "", counts[1] ? plural2(counts[1], "on a unique key", "on a unique key") : ""].filter(Boolean).join(", ") : "All have a key",
        advice: null,
        actionSql: null
      };
    }
  }
  const item = own && fromReport ? timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own : own || fromReport;
  return build({
    key: "primary_keys",
    title: "Tables without a primary key",
    category: "tables",
    items: [item],
    measureWith: ["tables", "report"],
    missing: LOAD_TABLES,
    fix: (i) => ({
      text: "Add a primary key. Tables lists each table with the checks to run first and the statement for it.",
      ...i.actionSql ? { sql: `-- ${i.numbers[0]?.label || "first table"} (template: check the columns first)
${i.actionSql}` } : {},
      caveat: i.caveat || "Adding a primary key rebuilds the table; adding an AUTO_INCREMENT column blocks writes for the whole rebuild."
    })
  });
}
function myisamTables(ctx) {
  const t = ctx.tables;
  let own = null;
  const engines = t?.summary?.engines;
  const total = t ? (engines || []).reduce((n, e) => n + (e.tables || 0), 0) : 0;
  if (t && (!t.summary?.tables || engines && total === 0)) {
    own = noTables(`No tables were read in ${scopeText(t)}, so there is nothing to judge.`, sourceOf("tables", t.taken_at));
  } else if (t && engines) {
    const myisam = engines.filter((e) => (e.engine || "").toLowerCase() === "myisam").reduce((n, e) => n + (e.tables || 0), 0);
    const names = (t.tables || []).filter((r) => (r.engine || "").toLowerCase() === "myisam").slice(0, 5);
    own = {
      status: myisam ? "watch" : "info",
      value: myisam ? plural2(myisam, "MyISAM table", "MyISAM tables") : "None",
      verdict: `${engines.map((e) => `${e.engine || "unknown"} ${formatCount(e.tables)}`).join(", ") || "No tables"} in ${scopeText(t)}.`,
      rule: "watch when any user table uses MyISAM",
      why: "MyISAM has no transactions, locks the whole table for each write, and after a crash a table may need a repair.",
      caveat: null,
      advice: null,
      actionSql: names[0] ? `ALTER TABLE \`${names[0].schema.replace(/`/g, "``")}\`.\`${names[0].table.replace(/`/g, "``")}\` ENGINE=InnoDB;` : null,
      actionConfig: null,
      numbers: names.map((r) => ({ label: `${r.schema}.${r.table}`, text: r.size_bytes != null ? formatBytes(r.size_bytes) : "MyISAM" })),
      settings: [],
      state: null,
      inputs: { myisam },
      value_number: myisam,
      source: sourceOf("tables", t.taken_at)
    };
  }
  const f = ctx.reportRan("tables") ? ctx.finding("tables.engines") : null;
  const fromReport = !f ? null : reportHasNoTables(ctx) ? noTables("The Full analysis read no tables in the databases it covered, so there is nothing to judge.", sourceOf("report", ctx.reportTaken("tables"))) : { ...findingItem(f, sourceOf("report", ctx.reportTaken("tables"))), value: f.status === "watch" ? "MyISAM tables" : "None", rule: "watch when any user table uses MyISAM" };
  const item = own && fromReport ? timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own : own || fromReport;
  return build({
    key: "myisam_tables",
    title: "MyISAM tables",
    category: "tables",
    items: [item],
    measureWith: ["tables", "report"],
    missing: LOAD_TABLES,
    // Python's info here means "no MyISAM": the engines were read and nothing matched.
    info: "ok",
    fix: (i) => ({
      text: "Convert them to InnoDB after checking for code that relies on MyISAM behavior, such as an instant COUNT(*) without WHERE. FULLTEXT and SPATIAL indexes work in InnoDB too.",
      sql: i.actionSql || "ALTER TABLE <schema>.<table> ENGINE=InnoDB;",
      caveat: "Changing the engine copies the table: it blocks writes while it runs and needs free disk space for the copy."
    })
  });
}
function accountCount(i) {
  const n = Number(i.value);
  return Number.isFinite(n) ? n : null;
}
function accounts(ctx, key, title) {
  return build({
    key,
    title,
    category: "security",
    items: [ctx.serverCheck(key)],
    measureWith: ["report"],
    missing: RUN_ANALYSIS,
    value: (i) => {
      const n = accountCount(i);
      if (n === null) return i.value;
      return n === 0 ? "None" : plural2(n, "account", "accounts");
    },
    evidence: (i) => i.numbers.filter((n) => n.label === "Account" || n.label === "More"),
    fix: (i) => ({
      text: i.advice || "Review the accounts listed.",
      ...i.actionSql ? { sql: i.actionSql } : {},
      caveat: i.caveat || void 0
    }),
    docs: "https://dev.mysql.com/doc/refman/8.4/en/alter-user.html"
  });
}
function secureTransport(ctx) {
  const params = ctx.parameters;
  const require2 = params ? ctx.variable("require_secure_transport") : null;
  const haveSsl = params ? ctx.variable("have_ssl") : null;
  let item = null;
  if (params) {
    const on = require2 !== null && ["ON", "1", "YES", "TRUE"].includes(require2.toUpperCase());
    const base = {
      rule: "watch when require_secure_transport is OFF",
      why: "With require_secure_transport OFF, a client may connect over TCP without TLS, so the login handshake and every query and result cross the network unencrypted.",
      caveat: null,
      advice: null,
      actionSql: null,
      actionConfig: null,
      numbers: [],
      settings: [
        ...require2 !== null ? [{ name: "require_secure_transport", value: require2, default: "OFF" }] : [],
        ...haveSsl !== null ? [{ name: "have_ssl", value: haveSsl }] : []
      ],
      state: null,
      inputs: {},
      value_number: null,
      source: sourceOf("config", ctx.config?.taken_at)
    };
    item = require2 === null ? { ...base, status: "unknown", value: "", verdict: "This server does not report require_secure_transport." } : on ? { ...base, status: "ok", value: "Required", verdict: "require_secure_transport=ON: clients must use TLS or a local socket." } : {
      ...base,
      status: "watch",
      value: "Not required",
      verdict: "require_secure_transport=OFF: clients may connect over TCP without TLS." + (haveSsl && haveSsl.toUpperCase() !== "YES" ? ` TLS is not set up on this server (have_ssl=${haveSsl}).` : "")
    };
  }
  return build({
    key: "secure_transport",
    title: "Encrypted connections",
    category: "security",
    items: [item],
    measureWith: ["config"],
    missing: "Open Parameters to read require_secure_transport (the full analysis does not collect it).",
    fix: () => ({
      text: (haveSsl && haveSsl.toUpperCase() !== "YES" ? "Set up TLS first (ssl_cert, ssl_key), then check that every application connects with TLS. " : "Check that every application connects with TLS first. ") + "Then require it.",
      ...globalChange(ctx.engine, "require_secure_transport", "ON"),
      caveat: "From then on TCP connections without TLS are refused at login; local socket connections still work. Except on Windows, the server accepts the change even when TLS is not set up."
    }),
    docs: "https://dev.mysql.com/doc/refman/8.4/en/using-encrypted-connections.html"
  });
}
function categoryScore(checks) {
  const measured = checks.filter((c) => c.applies && c.status !== "unknown");
  if (!measured.length) return null;
  const total = measured.reduce((sum, c) => sum + POINTS[c.status], 0);
  return Math.round(total / measured.length);
}
function worstStatus(checks) {
  const measured = checks.filter((c) => c.applies && c.status !== "unknown");
  if (!measured.length) return "unknown";
  return measured.reduce((w, c) => RANK[c.status] < RANK[w] ? c.status : w, "ok");
}
function overallScore(categories) {
  let sum = 0;
  let weight = 0;
  for (const c of categories) {
    if (c.score === null) continue;
    sum += c.score * c.weight;
    weight += c.weight;
  }
  return weight > 0 ? Math.round(sum / weight) : null;
}
function letterGrade(score) {
  return (GRADE_SCALE.find((g) => score >= g.min) || GRADE_SCALE[GRADE_SCALE.length - 1]).grade;
}
var gradeRank = (g) => GRADE_SCALE.findIndex((x) => x.grade === g);
function gradeReason(r) {
  if (r.score === null || !r.grade) return "";
  const earned = letterGrade(r.score);
  if (earned !== r.grade) {
    const n = r.counts.problem;
    return `Capped at ${r.grade}: ${n.toLocaleString("en-US")} ${n === 1 ? "check has" : "checks have"} a problem (${r.score} is in the ${earned} range).`;
  }
  return `Score ${r.score} is in the ${r.grade} range, ${gradeRangeText(r.grade)}.`;
}
function evaluateTuning(input) {
  const ctx = new Ctx(input);
  const checks = [
    bufferPool(ctx),
    dirtyPages(ctx),
    freePageWaits(ctx),
    redoLog(ctx),
    joinsWithoutIndex(ctx),
    tempTables(ctx),
    sortMergePasses(ctx),
    slowLog(ctx),
    connections(ctx),
    threadCache(ctx),
    droppedConnections(ctx),
    tableOpenCache(ctx),
    tableDefinitionCache(ctx),
    primaryKeys(ctx),
    myisamTables(ctx),
    accounts(ctx, "no_password", "Accounts without a password"),
    accounts(ctx, "anonymous", "Anonymous accounts"),
    accounts(ctx, "root_any_host", "root from any host"),
    secureTransport(ctx)
  ];
  const categories = CATEGORY_ORDER.map((key) => {
    const own = checks.filter((c) => c.category === key);
    const applicable2 = own.filter((c) => c.applies);
    return {
      key,
      name: TUNING_CATEGORIES[key].name,
      weight: TUNING_CATEGORIES[key].weight,
      score: categoryScore(own),
      status: worstStatus(own),
      measured: applicable2.filter((c) => c.status !== "unknown").length,
      total: applicable2.length,
      checks: own
    };
  });
  const applicable = checks.filter((c) => c.applies);
  const measured = applicable.filter((c) => c.status !== "unknown").length;
  const total = applicable.length;
  const counts = { problem: 0, watch: 0, ok: 0, unknown: 0 };
  for (const c of applicable) counts[c.status]++;
  const score = overallScore(categories);
  const provisional = score === null || measured * 2 < total || categories.some((c) => c.total > 0 && c.measured === 0);
  let grade = null;
  if (score !== null && !provisional) {
    grade = letterGrade(score);
    if (counts.problem > 0 && gradeRank(grade) < gradeRank(PROBLEM_GRADE_CAP)) grade = PROBLEM_GRADE_CAP;
  }
  const label = counts.problem > 0 ? "Problems found" : counts.watch > 0 ? "Needs attention" : provisional ? "Not enough data" : "Good";
  const weight = (c) => TUNING_CATEGORIES[c.category].weight;
  const recommendations = checks.map((c, i) => ({ c, i })).filter(({ c }) => c.applies && (c.status === "problem" || c.status === "watch")).sort((a, b) => RANK[a.c.status] - RANK[b.c.status] || weight(b.c) - weight(a.c) || a.i - b.i).map(({ c }) => c);
  return {
    schema_version: "tuning-1.0",
    score,
    provisional,
    grade,
    label,
    status: worstStatus(checks),
    measured,
    total,
    counts,
    categories,
    checks,
    recommendations,
    server: { engine: ctx.engine, version: ctx.version, product: ctx.product },
    scoring: SCORING_RULE,
    generatedAt: input.now || (/* @__PURE__ */ new Date()).toISOString()
  };
}
function tuningInputs({
  activeKey,
  report,
  health,
  config,
  tables,
  legacy
}) {
  const bound = [
    ["report", report],
    ["health", health],
    ["config", config],
    ["tables", tables]
  ];
  const present = bound.filter(([, b]) => b && b.report);
  const current = present.filter(([, b]) => b.key === activeKey);
  const pick = (key) => {
    const input = {};
    for (const [name, b] of present)
      if (b.key === key) input[name] = b.report;
    return input;
  };
  const withLegacy = (input) => ({
    ...input,
    ...health === void 0 && legacy?.health ? { health: legacy.health } : {},
    ...config === void 0 && legacy?.config ? { config: legacy.config } : {},
    ...tables === void 0 && legacy?.tables ? { tables: legacy.tables } : {}
  });
  if (current.length) return { input: withLegacy(pick(activeKey)), other: null, key: activeKey };
  const legacyOnly = withLegacy({});
  if (Object.keys(legacyOnly).length) return { input: legacyOnly, other: null, key: activeKey };
  if (present.length) {
    const [, first] = present[0];
    return { input: pick(first.key), other: first.label, key: first.key };
  }
  return { input: {}, other: null, key: activeKey };
}
var STATUS_TEXT = {
  problem: "Problem",
  watch: "Watch",
  ok: "OK",
  unknown: "Not measured"
};
function coverageText(r) {
  return `${r.measured} of ${r.total} checks measured`;
}
function summaryText(r) {
  const head = r.score === null ? `${r.label}: no check measured yet` : `${r.label}: ${r.score}%${r.provisional ? " (provisional)" : r.grade ? `, grade ${r.grade}` : ""}, ${coverageText(r)}`;
  const parts = [];
  if (r.counts.problem) parts.push(plural2(r.counts.problem, "problem", "problems"));
  if (r.counts.watch) parts.push(`${r.counts.watch} to watch`);
  return parts.length ? `${head}; ${parts.join(", ")}.` : `${head}.`;
}
function escapeHtml(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function escapeMarkdown(value) {
  return String(value ?? "").replace(/\r?\n/g, " ").replace(/([\\`*_{}[\]()#+!|<>~])/g, "\\$1");
}
function fence(text, lang = "sql") {
  const longest = Math.max(2, ...(text.match(/`+/g) || []).map((m) => m.length));
  const ticks = "`".repeat(longest + 1);
  return [`${ticks}${lang}`, text, ticks];
}
var CREDIT = "Made with myflames. Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.";
function serverText(r) {
  return `${r.server.product || (r.server.engine === "mariadb" ? "MariaDB" : "MySQL")} ${r.server.version || ""}`.trim();
}
function scoreText(r) {
  if (r.score === null) return "No score: no check was measured.";
  return `${r.score}%${r.provisional ? " (provisional, no grade)" : r.grade ? ` (grade ${r.grade})` : ""}`;
}
function tuningMarkdown(r, connectionLabel2) {
  const md = escapeMarkdown;
  const lines = ["# Tuning scorecard", ""];
  lines.push(`${md(serverText(r))}${connectionLabel2 ? ` on ${md(connectionLabel2)}` : ""}, ${md(r.generatedAt)}`, "");
  lines.push(`**${md(r.label)}.** Score ${md(scoreText(r))}. ${md(coverageText(r))}.`, "");
  lines.push(md(r.scoring), "");
  lines.push("| Category | Weight | Score | Measured |", "| :--- | ---: | ---: | ---: |");
  for (const c of r.categories)
    lines.push(`| ${md(c.name)} | ${Math.round(c.weight * 100)}% | ${c.score === null ? "not measured" : `${c.score}%`} | ${c.measured} of ${c.total} |`);
  lines.push("", "## Needs attention", "");
  if (!r.recommendations.length) lines.push("No problem or watch among the measured checks.", "");
  for (const c of r.recommendations) {
    lines.push(`### ${md(STATUS_TEXT[c.status])}: ${md(c.title)}`, "");
    lines.push(`- Measured: ${md(c.value)}`);
    if (c.verdict) lines.push(`- What this server shows: ${md(c.verdict)}`);
    if (c.rule) lines.push(`- Rule: ${md(c.rule)}`);
    if (c.source) lines.push(`- From: ${md(c.source.label)}${c.source.takenAt ? `, ${md(c.source.takenAt)}` : ""}`);
    if (c.fix) {
      lines.push(`- What to do: ${md(c.fix.text)}`);
      if (c.fix.sql) lines.push("", ...fence(c.fix.sql));
      if (c.fix.optionFile) lines.push("", "Option file (copy it; myflames does not edit files):", "", ...fence(c.fix.optionFile, "ini"));
      if (c.fix.caveat) lines.push("", `Caveat: ${md(c.fix.caveat)}`);
    }
    lines.push("");
  }
  const unknown = r.checks.filter((c) => c.status === "unknown");
  if (unknown.length) {
    lines.push("## Not measured", "");
    for (const c of unknown) lines.push(`- ${md(c.title)}: ${md(c.unknownReason || "")}`);
    lines.push("");
  }
  const ok = r.checks.filter((c) => c.applies && c.status === "ok");
  if (ok.length) {
    lines.push("## OK", "");
    for (const c of ok) lines.push(`- ${md(c.title)}: ${md(c.value)}`);
    lines.push("");
  }
  lines.push("Statements are for you to review and run yourself; myflames never runs them from this page.", "", CREDIT, "");
  return lines.join("\n");
}
function tuningHtml(r, connectionLabel2) {
  const e = escapeHtml;
  const tone = (s) => `<span class="badge ${e(s)}">${e(STATUS_TEXT[s])}</span>`;
  const rows = r.categories.map(
    (c) => `<tr><td>${e(c.name)}</td><td>${e(Math.round(c.weight * 100))}%</td><td>${c.score === null ? "not measured" : `${e(c.score)}%`}</td><td>${e(c.measured)} of ${e(c.total)}</td></tr>`
  ).join("");
  const card = (c) => `
    <section class="check ${e(c.status)}">
      <h3>${tone(c.status)} ${e(c.title)}</h3>
      <p><strong>Measured:</strong> ${e(c.value)}</p>
      ${c.verdict ? `<p>${e(c.verdict)}</p>` : ""}
      ${c.rule ? `<p class="muted"><strong>Rule:</strong> ${e(c.rule)}</p>` : ""}
      ${c.source ? `<p class="muted">From ${e(c.source.label)}${c.source.takenAt ? `, ${e(c.source.takenAt)}` : ""}</p>` : ""}
      ${c.fix ? `<p><strong>What to do:</strong> ${e(c.fix.text)}</p>` : ""}
      ${c.fix?.sql ? `<pre><code>${e(c.fix.sql)}</code></pre>` : ""}
      ${c.fix?.optionFile ? `<p class="muted">Option file (copy it; myflames does not edit files):</p><pre><code>${e(c.fix.optionFile)}</code></pre>` : ""}
      ${c.fix?.caveat ? `<p class="muted">Caveat: ${e(c.fix.caveat)}</p>` : ""}
    </section>`;
  const unknown = r.checks.filter((c) => c.status === "unknown");
  const ok = r.checks.filter((c) => c.applies && c.status === "ok");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tuning scorecard</title>
<style>
:root { color-scheme: light dark; --bg: #ffffff; --ink: #1d1d1f; --muted: #6e6e73; --line: rgba(0,0,0,0.1);
  --ok: #1b7f3b; --warn: #a15c00; --danger: #c4271c; --info: #0b5cad; }
@media (prefers-color-scheme: dark) { :root { --bg: #1c1c1e; --ink: #f5f5f7; --muted: #a1a1a6; --line: rgba(255,255,255,0.14);
  --ok: #4cd964; --warn: #ffb340; --danger: #ff6961; --info: #64a8ff; } }
body { margin: 0; padding: 32px 16px; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; } h3 { font-size: 14px; margin: 0 0 6px; }
.muted { color: var(--muted); }
.score { font-size: 28px; font-weight: 600; margin: 16px 0 4px; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; } th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
.check { border-top: 1px solid var(--line); padding: 12px 0; } .check p { margin: 4px 0; }
.badge { display: inline-block; font-size: 12px; font-weight: 600; margin-right: 4px; }
.badge.problem { color: var(--danger); } .badge.watch { color: var(--warn); } .badge.ok { color: var(--ok); } .badge.unknown { color: var(--muted); }
pre { background: rgba(127,127,127,0.12); padding: 8px 10px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
footer { margin-top: 32px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
<h1>Tuning scorecard</h1>
<p class="muted">${e(serverText(r))}${connectionLabel2 ? ` on ${e(connectionLabel2)}` : ""}, ${e(r.generatedAt)}</p>
<p class="score">${e(r.label)}: ${e(scoreText(r))}</p>
<p>${e(coverageText(r))}.</p>
<p class="muted">${e(r.scoring)}</p>
<h2>Categories</h2>
<table><thead><tr><th>Category</th><th>Weight</th><th>Score</th><th>Measured</th></tr></thead><tbody>${rows}</tbody></table>
<h2>Needs attention (${e(r.recommendations.length)})</h2>
${r.recommendations.length ? r.recommendations.map(card).join("") : "<p>No problem or watch among the measured checks.</p>"}
${unknown.length ? `<h2>Not measured (${e(unknown.length)})</h2><ul>${unknown.map((c) => `<li>${e(c.title)}: ${e(c.unknownReason || "")}</li>`).join("")}</ul>` : ""}
${ok.length ? `<h2>OK (${e(ok.length)})</h2><ul>${ok.map((c) => `<li>${e(c.title)}: ${e(c.value)}</li>`).join("")}</ul>` : ""}
<footer><p>Statements are for you to review and run yourself; myflames never runs them from this page.</p><p>${e(CREDIT)}</p></footer>
</main>
</body>
</html>
`;
}

// src/Tuning.tsx
var import_jsx_runtime10 = require("react/jsx-runtime");
var REPORT_USUAL_MAX_S = 20;
var ANALYSIS_FORMATS = [
  ["html", "HTML"],
  ["markdown", "Markdown"],
  ["json", "JSON"],
  ["text", "Text"]
];
var MEASURE_VIEW = {
  health: { view: "health", label: "Open Health" },
  config: { view: "config", label: "Open Parameters" },
  tables: { view: "tables", label: "Open Tables" },
  report: { view: "report", label: "Run full analysis" }
};
function TuningView({
  connection,
  disabled,
  runBlocked,
  onBack,
  selection,
  report: boundReport,
  health,
  config,
  tables,
  healthReport,
  configReport,
  tablesReport,
  source,
  reads,
  refreshRunning,
  startedAt,
  wasRun,
  settings,
  onDigest,
  onOpenLink,
  onOpenLesson,
  onRunReport,
  onOpenSql,
  onOpenView,
  onOpenTop,
  tabRequest
}) {
  const [tab, setTab] = (0, import_react12.useState)(tabRequest?.tab || "scorecard");
  const [category, setCategory] = (0, import_react12.useState)("all");
  const [filter, setFilter] = (0, import_react12.useState)("attention");
  const [query, setQuery] = (0, import_react12.useState)("");
  const [exporting, setExporting] = (0, import_react12.useState)("");
  const [exported, setExported] = (0, import_react12.useState)("");
  const [exportError, setExportError] = (0, import_react12.useState)("");
  (0, import_react12.useEffect)(() => {
    if (tabRequest) setTab(tabRequest.tab);
  }, [tabRequest?.tab, tabRequest?.n]);
  const activeKey = connection ? connectionKey(connection) : reads?.key || "";
  const { input, other } = (0, import_react12.useMemo)(
    () => tuningInputs({
      activeKey,
      report: boundReport,
      health,
      config,
      tables,
      legacy: { health: healthReport, config: configReport, tables: tablesReport }
    }),
    [activeKey, boundReport, health, config, tables, healthReport, configReport, tablesReport]
  );
  const tuning = (0, import_react12.useMemo)(() => evaluateTuning(input), [input]);
  const analysis = boundReport?.report ?? null;
  const analysisOther = boundReport && boundReport.key !== activeKey ? boundReport.label : null;
  const loading = reads?.loading || "";
  const running = !!loading || !!refreshRunning;
  const selectRef = (0, import_react12.useRef)(selection.select);
  selectRef.current = selection.select;
  const hasSelection = !!selection.key;
  (0, import_react12.useEffect)(() => {
    if (tab === "analysis" && analysis?.look_first && !hasSelection) selectRef.current(`first:${analysis.look_first}`, false);
  }, [analysis, tab]);
  const visible = (0, import_react12.useMemo)(() => {
    const q = query.trim().toLowerCase();
    return tuning.checks.filter((c) => {
      if (category !== "all" && c.category !== category) return false;
      if (filter === "attention" && !(c.applies && (c.status === "problem" || c.status === "watch"))) return false;
      if (filter === "unknown" && !(c.status === "unknown")) return false;
      if (!q) return true;
      return [c.title, c.value, c.verdict, c.unknownReason || "", c.fix?.sql || "", ...c.settings.map((s) => s.name)].join(" ").toLowerCase().includes(q);
    });
  }, [tuning.checks, category, filter, query]);
  const inCategory = (c) => category === "all" || c.category === category;
  const counts = {
    attention: tuning.checks.filter((c) => inCategory(c) && c.applies && (c.status === "problem" || c.status === "watch")).length,
    unknown: tuning.checks.filter((c) => inCategory(c) && c.status === "unknown").length,
    all: tuning.checks.filter(inCategory).length
  };
  const run = () => {
    if (disabled || runBlocked || running || !onRunReport) return;
    setExported("");
    setExportError("");
    void onRunReport();
  };
  const open = (view) => {
    if (view === "report") run();
    else onOpenView?.(MEASURE_VIEW[view].view);
  };
  function exportScorecard(format) {
    const label = other || void 0;
    const date = tuning.generatedAt.slice(0, 10);
    if (format === "json") download(JSON.stringify(tuning, null, 2), `tuning-${date}.json`, "application/json");
    else if (format === "markdown") download(tuningMarkdown(tuning, label), `tuning-${date}.md`, "text/markdown");
    else download(tuningHtml(tuning, label), `tuning-${date}.html`, "text/html");
    setExported(`tuning-${date}.${format === "markdown" ? "md" : format}`);
  }
  async function exportAnalysis(format) {
    if (!analysis || exporting) return;
    setExporting(format);
    setExported("");
    setExportError("");
    try {
      const file = await api("report/export", { report: analysis, format, ...source ? { source } : {} });
      download(file.content, file.filename, file.mime);
      setExported(file.filename);
    } catch (e) {
      setExportError(e.message);
    } finally {
      setExporting("");
    }
  }
  let inspector = null;
  const key = selection.key || "";
  if (tab === "scorecard" && key.startsWith("check:")) {
    const check = tuning.checks.find((c) => c.key === key.slice(6));
    if (check)
      inspector = checkInspector(check, {
        actionsOff: disabled || !!other,
        other: !!other,
        // Statements behind a number: only from a Full analysis of the connection in use.
        analysis: boundReport && boundReport.key === activeKey ? boundReport.report : null,
        activeKey,
        connection,
        settings,
        onDigest,
        onOpenTop,
        onOpenSql,
        onMeasure: open,
        canMeasure: (v) => v === "report" ? !!onRunReport : !!onOpenView
      });
  } else if (tab === "analysis" && analysis && /^(finding|first|section|view):/.test(key)) {
    inspector = buildReportInspector({
      report: analysis,
      selectionKey: key,
      onSelectKey: selection.select,
      boundKey: boundReport?.key,
      actionsOff: disabled || !!analysisOther,
      connection,
      settings,
      onDigest,
      onOpenLink,
      onOpenLesson
    });
  }
  const exportMenu = tab === "scorecard" ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
    ExportMenu,
    {
      label: "Export the tuning scorecard",
      className: "report-export",
      disabled: !!other,
      title: other ? `The scorecard is from ${other}: run the full analysis to load this server` : `The scorecard as shown: ${coverageText(tuning)}; nothing is read again.`,
      formats: ["html", "markdown", "json"].map((format) => ({
        key: format,
        label: format === "html" ? "HTML" : format === "markdown" ? "Markdown" : "JSON",
        onSelect: () => exportScorecard(format)
      }))
    }
  ) : analysis ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
    ExportMenu,
    {
      label: "Export the full analysis",
      className: "report-export",
      disabled: !!exporting,
      title: `Made from the report as read${analysisOther ? ` on ${analysisOther}` : ""}; nothing is read again.`,
      formats: ANALYSIS_FORMATS.map(([format, label]) => ({
        key: format,
        label,
        onSelect: () => void exportAnalysis(format)
      }))
    }
  ) : null;
  const tabs = /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
    Segmented,
    {
      label: "Tuning",
      tabs: "tuning",
      className: "tuning-tabs",
      value: tab,
      onChange: setTab,
      options: [
        { value: "scorecard", label: "Scorecard" },
        { value: "analysis", label: "Full analysis" }
      ]
    }
  );
  return /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
    ViewLayout,
    {
      id: "tuning",
      title: "Tuning",
      className: "tuning-panel server-aligned-values",
      onBack,
      info: tab === "scorecard" ? /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: "A score from the checks the Server views already judge: Health, Parameters, Tables and the Full analysis. Each check shows what was measured, the rule and what to do." }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: SCORE_RULE }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: GRADE_RULE }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: "Suggested statements are for you to copy or open in the Query editor; nothing here runs them." })
      ] }) : /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { children: [
          "Runs every Server view once on the connection in use, ",
          REPORT_ESTIMATE,
          ", and ranks what it finds: problems first, then what to watch, with the rule each one uses."
        ] }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: "It only reads. Suggested statements and my.cnf lines are for you to copy; myflames never applies them." })
      ] }),
      controls: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
        tabs,
        exportMenu,
        tab === "scorecard" && onRunReport && // While it runs: a spinner and "Running…", aria-busy, clicks
        // ignored, and focus stays on it (aria-disabled, not disabled).
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
          ActionButton,
          {
            "data-action": "run-analysis",
            icon: "analysis",
            busy: running,
            busyLabel: "Running\u2026",
            disabled: !running && (disabled || !!runBlocked),
            title: `Runs every Server view once, ${REPORT_ESTIMATE}. It only reads.`,
            onClick: run,
            children: "Run full analysis"
          }
        )
      ] }),
      onRefresh: tab === "analysis" && onRunReport ? run : void 0,
      refreshDisabled: disabled || !!runBlocked,
      loading: running,
      otherConnection: tab === "scorecard" ? other : analysisOther,
      otherAction: tab === "scorecard" && onRunReport ? { label: "Run full analysis", onClick: run, disabled: disabled || !!runBlocked || running } : void 0,
      inspector,
      inspectorEmpty: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: tab === "scorecard" ? "Select a check to see what was measured, the rule and what to do." : "Select a finding or a section to see the verdict, the numbers, the rule and what to do." }),
      status: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
          ViewStatus,
          {
            loading: running ? `Running every Server view once, ${REPORT_ESTIMATE} in all. It only reads.` : "",
            startedAt,
            slowAfter: REPORT_USUAL_MAX_S * 1e3,
            slowText: `Longer than the usual ${REPORT_ESTIMATE.replace(/^about /, "")}.`,
            onStop: reads?.stop,
            onRetry: reads?.error ? run : void 0,
            doneAt: reads?.doneAt,
            error: reads?.error || exportError
          }
        ),
        exported && /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { className: "hint", role: "status", children: [
          "Saved ",
          exported
        ] })
      ] }),
      children: tab === "scorecard" ? /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("div", { className: "tuning tuning-view", id: "tuning-panel-scorecard", role: "tabpanel", "aria-labelledby": "tuning-tab-scorecard", children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "visually-hidden", role: "status", children: summaryText(tuning) }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(ScoreHeader, { tuning, running: running || !!runBlocked, disabled: disabled || !!other, onMeasure: open, canRun: !!onRunReport }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("ul", { className: "tuning-categories", "aria-label": "Categories", children: tuning.categories.map((c) => {
          const active = category === c.key;
          return /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("li", { children: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(
            "button",
            {
              type: "button",
              className: `tuning-category is-${c.status}${active ? " is-active" : ""}`,
              "aria-pressed": active,
              "data-category": c.key,
              title: `${TUNING_CATEGORIES[c.key].description} ${Math.round(c.weight * 100)}% of the score.`,
              onClick: () => setCategory(active ? "all" : c.key),
              children: [
                /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-category-name", children: c.name }),
                /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-category-score", children: c.score === null ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-category-none", children: "Not measured" }) : `${c.score}%` }),
                /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-meter", "aria-hidden": "true", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { style: { width: `${c.score ?? 0}%` } }) }),
                /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("span", { className: "tuning-category-foot", children: [
                  c.measured,
                  " of ",
                  c.total,
                  " measured"
                ] })
              ]
            }
          ) }, c.key);
        }) }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("div", { className: "tuning-filters", children: [
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
            Segmented,
            {
              label: "Show",
              value: filter,
              onChange: setFilter,
              options: [
                { value: "attention", label: "Needs attention", count: counts.attention },
                { value: "unknown", label: "Not measured", count: counts.unknown },
                { value: "all", label: "All checks", count: counts.all }
              ]
            }
          ),
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(SearchField, { label: "Search checks", value: query, onChange: setQuery, placeholder: "Search checks or SQL" })
        ] }),
        visible.length === 0 ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "tuning-empty", role: "status", children: query ? "No check matches the search." : filter === "attention" ? tuning.measured ? `No problem or watch among the ${tuning.measured} measured checks${category !== "all" ? " in this category" : ""}.` : "Nothing measured yet, so nothing to flag." : filter === "unknown" ? "Every check here was measured." : "No check in this category." }) : CATEGORY_ORDER.filter((k) => visible.some((c) => c.category === k)).map((k) => /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(GroupedList, { id: `tuning-group-${k}`, title: TUNING_CATEGORIES[k].name, className: "tuning-group", children: visible.filter((c) => c.category === k).map((c) => /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
          GroupedRow,
          {
            rowKey: `check:${c.key}`,
            selection,
            tone: c.applies ? c.status : "off",
            toneLabel: c.applies ? STATUS_TEXT[c.status] : "Does not apply",
            label: c.title,
            detail: c.status === "unknown" ? c.source ? c.unknownReason : void 0 : c.verdict,
            value: c.status === "unknown" ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-needs", title: c.unknownReason, children: needsText(c) }) : /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { title: c.value, children: c.value }),
            attrs: { "data-check": c.key, "data-status": c.applies ? c.status : "not-applicable" },
            className: "tuning-check",
            wrap: true
          },
          c.key
        )) }, k))
      ] }) : /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("div", { className: "report-panel tuning-analysis tuning-view", id: "tuning-panel-analysis", role: "tabpanel", "aria-labelledby": "tuning-tab-analysis", children: analysis ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(ReportDiagnosticBody, { report: analysis, hostLabel: boundReport?.label, selection }) : !running && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
        EmptyState,
        {
          icon: "analysis",
          title: "No full analysis yet",
          action: onRunReport && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("button", { type: "button", className: "button primary", disabled: disabled || !!runBlocked, onClick: run, children: "Run full analysis" }),
          children: wasRun ? `A full analysis ran in your last session. Reports are not saved: run it again, ${REPORT_ESTIMATE}. It only reads.` : `Runs every Server view once and ranks what to look at first, ${REPORT_ESTIMATE}. It only reads.`
        }
      ) })
    }
  );
}
var MEASURE_NAME = {
  health: "Health",
  config: "Parameters",
  tables: "Tables",
  report: "Full analysis"
};
function needsText(c) {
  if (!c.applies) return "Does not apply";
  if (c.source) return "Not judged";
  return `Needs ${c.measureWith.map((v) => MEASURE_NAME[v]).join(" or ")}`;
}
function ScoreHeader({
  tuning,
  running,
  disabled,
  canRun,
  onMeasure
}) {
  const unknown = tuning.checks.filter((c) => c.applies && c.status === "unknown");
  const needsAnalysis = unknown.some((c) => c.measureWith.includes("report"));
  const onlyParameters = unknown.length > 0 && unknown.every((c) => c.measureWith.length === 1 && c.measureWith[0] === "config");
  const reason = gradeReason(tuning);
  const capped = tuning.grade !== null && tuning.score !== null && letterGrade(tuning.score) !== tuning.grade;
  return /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("section", { className: `tuning-score is-${tuning.status}`, "aria-labelledby": "tuning-score-label", children: [
    /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("div", { className: "tuning-score-figure", children: [
      /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(
        "span",
        {
          className: `tuning-score-number${tuning.provisional ? " is-provisional" : ""}`,
          "data-score": tuning.score ?? "",
          "aria-hidden": tuning.score === null ? "true" : void 0,
          children: [
            tuning.score === null ? "\u2014" : tuning.score,
            tuning.score !== null && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-score-unit", children: "%" })
          ]
        }
      ),
      /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("span", { className: "tuning-grade-line", children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-grade", "data-grade": tuning.grade || "", children: tuning.grade ? `Grade ${tuning.grade}` : tuning.score === null ? "No score" : "Provisional" }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(GradeLegend, { grade: tuning.grade })
      ] })
    ] }),
    /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("div", { className: "tuning-score-text", children: [
      /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("h3", { id: "tuning-score-label", className: "tuning-score-label", children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: `status-dot is-${tuning.status}`, "aria-hidden": "true" }),
        tuning.label
      ] }),
      reason && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "tuning-grade-reason", "data-capped": capped ? "true" : "false", children: reason }),
      /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { className: "tuning-score-coverage", children: [
        coverageText(tuning),
        tuning.counts.problem > 0 && ` \xB7 ${tuning.counts.problem} problem${tuning.counts.problem === 1 ? "" : "s"}`,
        tuning.counts.watch > 0 && ` \xB7 ${tuning.counts.watch} to watch`,
        tuning.counts.ok > 0 && ` \xB7 ${tuning.counts.ok} OK`
      ] }),
      unknown.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { className: "tuning-score-hint", children: [
        tuning.provisional ? "Not graded until at least half the checks, and one in each category, are measured." : `${unknown.length} not measured; they are left out of the score.`,
        " ",
        needsAnalysis && canRun && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("button", { type: "button", className: "text-button", disabled: disabled || running, onClick: () => onMeasure("report"), children: "Run full analysis" }),
        onlyParameters && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("button", { type: "button", className: "text-button", disabled, onClick: () => onMeasure("config"), children: "Open Parameters" })
      ] })
    ] })
  ] });
}
function GradeLegend({ grade }) {
  return /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(Popover, { label: "How the grade works", className: "tuning-grade-help", width: 300, children: [
    /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("h3", { className: "popover-title", children: "How the grade works" }),
    /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("table", { className: "tuning-grade-legend", children: [
      /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("caption", { className: "visually-hidden", children: "Grades and the scores they cover" }),
      /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("thead", { children: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("tr", { children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("th", { scope: "col", children: "Grade" }),
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("th", { scope: "col", children: "Score" })
      ] }) }),
      /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("tbody", { children: GRADE_SCALE.map((g) => /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(
        "tr",
        {
          "data-grade": g.grade,
          className: g.grade === grade ? "is-current" : void 0,
          "aria-current": g.grade === grade ? "true" : void 0,
          children: [
            /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("th", { scope: "row", children: g.grade }),
            /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("td", { children: [
              gradeRangeText(g.grade),
              g.grade === grade && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "tuning-grade-current", children: " \xB7 this server" })
            ] })
          ]
        },
        g.grade
      )) })
    ] }),
    /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { children: [
      "A problem caps the grade at ",
      PROBLEM_GRADE_CAP,
      ", whatever the score."
    ] }),
    /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: "No grade (provisional) until at least half the checks, and one in each category, are measured." })
  ] });
}
function checkInspector(check, ctx) {
  const fix = check.fix;
  const measured = check.status !== "unknown";
  const attributed = ctx.analysis?.attribution?.full_joins?.statements || [];
  const joins = check.key === "joins_without_index" && measured && (check.status === "problem" || check.status === "watch");
  return {
    key: `check:${check.key}`,
    title: check.title,
    meta: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
      /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(StatusPill, { tone: check.applies ? check.status : "off", label: check.applies ? STATUS_TEXT[check.status] : "Does not apply" }),
      /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("span", { className: "hint", children: TUNING_CATEGORIES[check.category].name })
    ] }),
    actions: fix?.sql && ctx.onOpenSql ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("div", { className: "inspector-links", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(
      "button",
      {
        type: "button",
        className: "button",
        disabled: ctx.actionsOff,
        title: "Opens the statements in a Query editor tab; they run only when you run them there",
        onClick: () => ctx.onOpenSql?.(fix.sql),
        children: [
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(Icon, { name: "query", size: 14 }),
          " Open in Query editor"
        ]
      }
    ) }) : void 0,
    body: /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
      /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(InspectorSection, { title: measured ? "Measured" : check.applies ? "Not measured" : "Does not apply", children: [
        measured ? /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "tuning-measured-value", children: check.value }),
          check.verdict && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: check.verdict })
        ] }) : /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: check.unknownReason }),
          check.applies && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("div", { className: "button-row", children: check.measureWith.filter((v) => ctx.canMeasure(v)).map((v) => /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("button", { type: "button", className: "button", disabled: ctx.actionsOff, onClick: () => ctx.onMeasure(v), children: MEASURE_VIEW[v].label }, v)) })
        ] }),
        check.evidence.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(Fields, { label: "Numbers", rows: check.evidence.map((n) => [n.label, n.text]) }),
        check.source && /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)("p", { className: "hint", children: [
          "From ",
          check.source.label,
          check.source.takenAt ? `, read at ${new Date(check.source.takenAt).toLocaleTimeString()}` : "",
          "."
        ] })
      ] }),
      fix && /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(InspectorSection, { title: "What to do", children: [
        /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: fix.text }),
        fix.sql && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(CopySql, { sql: fix.sql, label: `Statements for ${check.title}` }),
        fix.optionFile && /* @__PURE__ */ (0, import_jsx_runtime10.jsxs)(import_jsx_runtime10.Fragment, { children: [
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "hint", children: "Option file line, to copy (myflames does not edit files):" }),
          /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(CopySql, { sql: fix.optionFile, label: `Option file line for ${check.title}` })
        ] }),
        fix.caveat && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "hint", children: fix.caveat })
      ] }),
      check.rule && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(InspectorSection, { title: "Rule", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: check.rule }) }),
      check.why && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(InspectorSection, { title: "Why it matters", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { children: check.why }) }),
      joins && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(InspectorSection, { title: "Statements behind it", children: attributed.length && ctx.connection && ctx.settings ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
        StatementRows,
        {
          statements: attributed,
          order: "full_join",
          connection: ctx.connection,
          disabled: ctx.actionsOff,
          settings: ctx.settings,
          onAction: (action, row, rank) => ctx.onDigest?.(row, rank, action, ctx.activeKey)
        }
      ) : ctx.connection && ctx.settings && ctx.onDigest && ctx.onOpenTop ? /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
        StatementsBehind,
        {
          connection: ctx.connection,
          disabled: ctx.actionsOff,
          other: ctx.other,
          from: ctx.activeKey,
          order: "full_join",
          settings: ctx.settings,
          onDigest: ctx.onDigest,
          onOpenTop: ctx.onOpenTop
        }
      ) : /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "hint", children: "Top statements ordered by joins without an index lists them." }) }),
      check.settings.length > 0 && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(InspectorSection, { title: "Settings", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)(
        Fields,
        {
          label: "Settings",
          rows: check.settings.map((s) => [
            s.name,
            s.default && s.default !== s.value ? `${s.value} (default ${s.default})` : s.value
          ])
        }
      ) }),
      check.docs && /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("p", { className: "hint", children: /* @__PURE__ */ (0, import_jsx_runtime10.jsx)("a", { href: check.docs, target: "_blank", rel: "noreferrer noopener", children: "Reference manual" }) })
    ] })
  };
}
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  TuningView
});
