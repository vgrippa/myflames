import { syntaxTree } from "@codemirror/language";
import { StateEffect, StateField, type EditorState, type Extension } from "@codemirror/state";
import { ViewPlugin, showTooltip, type Tooltip } from "@codemirror/view";
import { HIGHLIGHT_LIMIT, tokenize } from "../sqlHighlight";
import { tokenInfo } from "./sqlLanguage";
import type { EngineFunction } from "./engineWords";
import type { EditorEnv, EditorFeatureSettings } from "./types";

type WordCatalog = typeof import("./engineWords");
let wordCatalog: Promise<WordCatalog> | undefined;
const loadWords = () => wordCatalog ||= import("./engineWords");
const word = /[A-Za-z0-9_$\u0080-\uffff]/;
const nameStart = /[A-Za-z_$\u0080-\uffff]/;
// functionsFor returns a fresh wrapper, but its decoded function array is
// shared by release. Index that array once, including for unknown call names.
const functionIndexes = new WeakMap<EngineFunction[], Map<string, EngineFunction>>();
function functionIndex(functions: EngineFunction[]): Map<string, EngineFunction> {
  let index = functionIndexes.get(functions);
  if (!index) {
    index = new Map(functions.map(fn => [fn.name.toUpperCase(), fn]));
    functionIndexes.set(functions, index);
  }
  return index;
}
type ArgumentHint = Tooltip & { label: string };

/** Counts come only from generated engine facts, with no parameter names. */
export function argumentCount(fn: Pick<EngineFunction, "min" | "max">): string {
  if (fn.min === null || fn.max === null) return "";
  if (fn.max === "unbounded") return `at least ${fn.min} argument${fn.min === 1 ? "" : "s"}`;
  if (fn.min === fn.max) return `${fn.min} argument${fn.min === 1 ? "" : "s"}`;
  return `${fn.min} to ${fn.max} argument${fn.max === 1 ? "" : "s"}`;
}

function hintAt(state: EditorState, env: EditorEnv, words?: WordCatalog, previous?: ArgumentHint | null): ArgumentHint | null {
  const { doc, selection } = state;
  if (!words || !env.engine || !env.serverVersion || doc.length > HIGHLIGHT_LIMIT ||
    selection.ranges.length !== 1 || !selection.main.empty) return null;
  const list = words.functionsFor(env.engine, env.serverVersion);
  if (!list.release) return null;
  const functions = functionIndex(list.functions);
  const pos = selection.main.head, lower = Math.max(0, pos - 4000);
  const text = doc.sliceString(lower, pos);
  const ignored = new Uint8Array(text.length), comments = new Uint8Array(text.length);
  const mark = (kind: string, from: number, to: number) => {
    if (!["string", "ident", "comment", "variable", "param"].includes(kind)) return;
    ignored.fill(1, from, to);
    if (kind === "comment") comments.fill(1, from, to);
  };
  if (env.backslashEscapes) {
    // Reuse the language's tokens, including those starting before this window.
    syntaxTree(state).iterate({ from: lower, to: pos, enter(node) {
      const info = node.type.prop(tokenInfo);
      if (info) mark(info.kind, Math.max(lower, node.from) - lower, Math.min(pos, node.to) - lower);
    } });
  } else {
    // Like hover/completion, mask escapes without changing offsets. Highlighting
    // keeps its existing SQL mode; only hints retokenize this bounded window.
    const scan = text.replace(/\\/g, " ");
    for (const token of tokenize(scan)) mark(token.kind, token.start, token.end);
  }
  let depth = 0, argument = 1;
  for (let at = text.length - 1; at >= 0; at--) {
    if (ignored[at]) continue;
    const char = text[at];
    if (char === ";") return null;
    if (char === ")") { depth++; continue; }
    if (char === "," && depth === 0) argument++;
    if (char !== "(") continue;
    if (depth) { depth--; continue; }
    let start = at;
    while (start > 0 && !ignored[start - 1] && word.test(text[start - 1])) start--;
    let before = start - 1;
    while (before >= 0 && (comments[before] || /\s/.test(text[before]))) before--;
    // At the scan boundary, an unseen dot could still qualify this name.
    const name = text.slice(start, at);
    const fn = start < at && nameStart.test(name[0]) && (before >= 0 || lower === 0) &&
      text[before] !== "." ? functions.get(name.toUpperCase()) : undefined;
    if (fn) {
      const suffix = list.exact ? "" : `, ${env.engine === "mysql" ? "MySQL" : "MariaDB"} ${list.release} list`;
      const label = [`${fn.name}(…)`, argumentCount(fn), `argument ${argument}`].filter(Boolean).join(" · ") + suffix;
      // Anchor to the call, not the moving caret. Preserve the create function
      // while this call and argument label stay the same, so its DOM is reused.
      const anchor = lower + at + 1;
      if (previous?.pos === anchor && previous.label === label) return previous;
      return { pos: anchor, label, above: true, strictSide: true, create() {
        const dom = document.createElement("div");
        dom.className = "sql-arg-hint";
        dom.textContent = label;
        return { dom };
      } };
    }
    // An enclosing call must not count commas inside this unmatched group.
    argument = 1;
  }
  return null;
}

export function argumentHintsExtension(_settings: EditorFeatureSettings, env: { current: EditorEnv }): Extension {
  let words: WordCatalog | undefined;
  const refresh = StateEffect.define<void>();
  const hints = StateField.define<ArgumentHint | null>({
    create: state => hintAt(state, env.current, words),
    update: (value, tr) => hintAt(tr.state, env.current, words, value),
    provide: field => showTooltip.from(field),
  });
  const lifecycle = ViewPlugin.fromClass(class {
    alive = true;
    constructor(view: import("@codemirror/view").EditorView) {
      void loadWords().then(catalog => {
        if (!this.alive) return;
        words = catalog;
        // Never dispatch during an editor update or after its destruction.
        queueMicrotask(() => { if (this.alive) view.dispatch({ effects: refresh.of() }); });
      }).catch(() => {});
    }
    destroy() { this.alive = false; }
  });
  return [hints, lifecycle];
}
