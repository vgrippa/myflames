// The Query editor's text area: a native <textarea> (typing, undo, IME,
// spell check off, screen readers and selection all stay the browser's) over
// a layer that draws the same text with syntax colors, the statement at the
// cursor, a bracket or quote pair, and an error position, plus a line number
// gutter. The layers repeat the textarea's text metrics exactly and follow its
// scroll position with a transform, so nothing re-renders on scroll.
import {
  forwardRef,
  useImperativeHandle,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import type { Connection } from "./api";
import { useSqlCompletion } from "./useSqlCompletion";
import "./completion.css";
import { HIGHLIGHT_LIMIT, matchAt, tokenize, type Token } from "./sqlHighlight";

export type Range = { start: number; end: number };
/** Lines numbered one by one up to here; a longer text gets a plain column. */
const GUTTER_LINE_LIMIT = 5000;

type Mark = Range & { cls: string };

/** Text pieces with their classes: token kinds plus the marks over them. */
function pieces(sql: string, tokens: Token[], marks: Mark[]): ReactNode[] {
  const cuts = new Set<number>([0, sql.length]);
  for (const t of tokens) {
    cuts.add(t.start);
    cuts.add(t.end);
  }
  for (const m of marks) {
    cuts.add(Math.max(0, Math.min(sql.length, m.start)));
    cuts.add(Math.max(0, Math.min(sql.length, m.end)));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const out: ReactNode[] = [];
  let k = 0;
  let plain = "";
  for (let p = 0; p < points.length - 1; p++) {
    const start = points[p];
    const end = points[p + 1];
    if (end <= start) continue;
    while (k < tokens.length && tokens[k].end <= start) k++;
    const token = k < tokens.length && tokens[k].start <= start && start < tokens[k].end ? tokens[k] : null;
    const classes: string[] = [];
    if (token) classes.push(`tok-${token.kind}`);
    for (const m of marks) if (m.start <= start && end <= m.end && m.end > m.start) classes.push(m.cls);
    const text = sql.slice(start, end);
    if (!classes.length) {
      plain += text;
      continue;
    }
    if (plain) {
      out.push(plain);
      plain = "";
    }
    out.push(
      <span key={start} className={classes.join(" ")}>
        {text}
      </span>,
    );
  }
  if (plain) out.push(plain);
  return out;
}

/** 0-based line of an offset. */
function lineAt(sql: string, offset: number) {
  let line = 0;
  for (let i = 0; i < offset && i < sql.length; i++) if (sql[i] === "\n") line++;
  return line;
}

export type SqlEditorProps = {
  value: string;
  onChange: (value: string) => void;
  onSelect?: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  /** The caret or selection, to find the bracket pair next to it. */
  caret: Range;
  /** The statement Run would send (tinted while the caret is in it). */
  statement?: Range | null;
  /** The statement that just ran, highlighted briefly (`.sql-backdrop mark`). */
  flash?: Range | null;
  /** Where the last error points (a wavy underline). */
  error?: Range | null;
  describedBy?: string;
  /** Editor height in px (the splitter below sets it). */
  height?: number;
  placeholder?: string;
  completionConnection?: Connection;
  completionScope?: string;
  backslashEscapes?: boolean;
};

/** The textarea and its layers; the ref is the textarea itself. */
export const SqlEditor = forwardRef<HTMLTextAreaElement, SqlEditorProps>(function SqlEditor(
  { value, onChange, onSelect, onKeyDown, caret, statement, flash, error, describedBy, height, placeholder, completionConnection, completionScope = "", backslashEscapes = true },
  ref,
) {
  const area = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => area.current as HTMLTextAreaElement);
  const layer = useRef<HTMLDivElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const [composing, setComposing] = useState(false);
  const focusState = useRef(false);
  const focused = focusState.current;
  const [completionMode, setCompletionMode] = useState<false | "auto" | "manual">(false);
  const [choice, setChoice] = useState(0);
  const [completionRequest, setCompletionRequest] = useState(0);
  const listId = useId();
  const completion = useSqlCompletion(completionConnection, value, caret.start, caret.end,
    focused && !composing && !!completionMode, completionMode === "manual", completionScope, backslashEscapes, completionRequest);
  const showing = focused && !composing && !!completionMode &&
    (completion.items.length > 0 || completion.loading || !!completion.note);
  const selected = Math.min(choice, Math.max(0, completion.items.length - 1));
  const popup = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  useEffect(() => { setCompletionMode(false); }, [completionScope]);
  useEffect(() => { setChoice(0); }, [completion.key]);
  useLayoutEffect(() => {
    if (showing) document.getElementById(`${listId}-${selected}`)?.scrollIntoView({ block: "nearest" });
  }, [selected, showing, completion.items]);
  function positionCompletion() {
    const el = area.current;
    if (!showing || !el) return;
    // Measure a plain-text mirror, including tabs and non-ASCII names. The
    // popup is a portal so editor splitters and overflow never clip it.
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    const mirror = document.createElement("div");
    Object.assign(mirror.style, { position: "fixed", left: "0", top: "0", visibility: "hidden",
      whiteSpace: "pre", font: style.font, lineHeight: style.lineHeight, tabSize: style.tabSize,
      letterSpacing: style.letterSpacing, fontVariantLigatures: "none", padding: style.padding });
    mirror.textContent = value.slice(0, caret.start);
    const marker = document.createElement("span"); marker.textContent = "\u200b";
    mirror.append(marker); document.body.append(mirror);
    const point = marker.getBoundingClientRect(); mirror.remove();
    const width = popup.current?.offsetWidth || 320;
    const height = popup.current?.offsetHeight || 180;
    const line = parseFloat(style.lineHeight) || 20;
    const x = rect.left + point.left - el.scrollLeft;
    const y = rect.top + point.top - el.scrollTop;
    const left = Math.max(8, Math.min(x, window.innerWidth - width - 8));
    const top = Math.max(8, Math.min(y + line + height > window.innerHeight - 8 ? y - height : y + line, window.innerHeight - height - 8));
    setPosition(previous => previous.left === left && previous.top === top ? previous : { left, top });
  }
  useLayoutEffect(positionCompletion);
  useEffect(() => {
    if (!showing) return;
    window.addEventListener("resize", positionCompletion);
    return () => window.removeEventListener("resize", positionCompletion);
  }, [showing, value, caret.start]);
  function acceptCompletion(index: number) {
    const el = area.current, item = completion.items[index], context = completion.context;
    if (!el || !item || !context || el.value !== value || el.selectionStart !== caret.start || el.selectionEnd !== caret.end) return;
    el.focus();
    el.setSelectionRange(context.start, context.end);
    // Native insertText joins the browser's undo stack. Never replace .value
    // or synthesize a React edit, which discards native undo history.
    if (!document.execCommand("insertText", false, item.insert)) el.setSelectionRange(caret.start, caret.end);
    setCompletionMode(false);
    onSelect?.();
  }
  function completionKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (composing || e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.ctrlKey && !e.metaKey && !e.altKey && (e.code === "Space" || e.key === " ")) {
      e.preventDefault(); e.stopPropagation(); setCompletionMode("manual"); setCompletionRequest(n => n + 1); setChoice(0); return;
    }
    if (showing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setCompletionMode(false); return; }
      if (completion.items.length && !e.shiftKey && ["ArrowDown", "ArrowUp", "Enter", "Tab"].includes(e.key)) {
        e.preventDefault(); e.stopPropagation();
        if (e.key === "ArrowDown") setChoice((selected + 1) % completion.items.length);
        else if (e.key === "ArrowUp") setChoice((selected + completion.items.length - 1) % completion.items.length);
        else acceptCompletion(selected);
        return;
      }
    }
    if (["ArrowLeft", "ArrowRight", "Home", "End", "Tab"].includes(e.key) || e.ctrlKey || e.metaKey) setCompletionMode(false);
    onKeyDown?.(e);
  }

  const colored = value.length <= HIGHLIGHT_LIMIT;
  const tokens = useMemo(() => (colored ? tokenize(value) : []), [value, colored]);
  const match = colored && caret.start === caret.end ? matchAt(value, tokens, caret.start) : null;
  const statementStart = statement?.start ?? -1;
  const statementEnd = statement?.end ?? -1;
  const errorStart = error?.start ?? -1;
  const errorEnd = error?.end ?? -1;
  const matchKey = match ? `${match.kind}:${match.at}:${match.partner}` : "";
  const drawn = useMemo(() => {
    if (!colored) return null;
    const marks: Mark[] = [];
    if (statementEnd > statementStart) marks.push({ start: statementStart, end: statementEnd, cls: "sql-current" });
    if (errorEnd > errorStart) marks.push({ start: errorStart, end: errorEnd, cls: "sql-error-mark" });
    if (match) {
      const cls = match.partner < 0 ? "sql-unmatched" : "sql-match";
      marks.push({ start: match.at, end: match.at + 1, cls });
      if (match.partner >= 0) marks.push({ start: match.partner, end: match.partner + 1, cls });
    }
    return pieces(value, tokens, marks);
    // matchKey stands for match (a new object on every render).
  }, [value, tokens, colored, statementStart, statementEnd, errorStart, errorEnd, matchKey]);

  // Line numbers, with the statement's lines and the error line marked.
  const lineCount = useMemo(() => {
    let n = 1;
    for (let i = 0; i < value.length; i++) if (value[i] === "\n") n++;
    return n;
  }, [value]);
  const firstLine = statement && statementEnd > statementStart ? lineAt(value, statementStart) : -1;
  const lastLine = statement && statementEnd > statementStart ? lineAt(value, statementEnd) : -1;
  const errorLine = errorEnd > errorStart ? lineAt(value, errorStart) : -1;
  const numbers = useMemo(() => {
    if (lineCount > GUTTER_LINE_LIMIT)
      return Array.from({ length: lineCount }, (_, i) => i + 1).join("\n");
    return Array.from({ length: lineCount }, (_, i) => (
      <span
        key={i}
        className={[i >= firstLine && i <= lastLine ? "is-current" : "", i === errorLine ? "is-error" : ""].join(" ").trim() || undefined}
      >
        {i + 1}
      </span>
    ));
  }, [lineCount, firstLine, lastLine, errorLine]);

  // The layers follow the textarea's scroll position.
  function follow() {
    const el = area.current;
    if (!el) return;
    const shift = `translate(${-el.scrollLeft}px, ${-el.scrollTop}px)`;
    if (layer.current) layer.current.style.transform = shift;
    if (backdrop.current) backdrop.current.style.transform = shift;
    if (gutter.current) gutter.current.style.transform = `translateY(${-el.scrollTop}px)`;
  }
  useLayoutEffect(follow);

  const flashLit = flash && flash.end > flash.start ? flash : null;
  return (
    <div
      className={`sql-editor-wrap${colored ? " is-colored" : ""}${composing ? " is-composing" : ""}`}
      style={height ? { height } : undefined}
    >
      <div className="sql-gutter" aria-hidden="true">
        <div ref={gutter} className="sql-gutter-lines">
          {numbers}
        </div>
      </div>
      <div className="sql-surface">
        {colored && (
          <div className="sql-highlight" aria-hidden="true">
            <div ref={layer} className="sql-layer">
              {drawn}
              {"\n"}
            </div>
          </div>
        )}
        {flashLit && (
          // The statement that runs, highlighted without touching the caret or selection.
          <div className="sql-backdrop" aria-hidden="true">
            <div ref={backdrop} className="sql-layer">
              {value.slice(0, flashLit.start)}
              <mark>{value.slice(flashLit.start, flashLit.end)}</mark>
              {value.slice(flashLit.end)}
              {"\n"}
            </div>
          </div>
        )}
        <textarea
          ref={area}
          aria-label="SQL editor"
          aria-describedby={describedBy}
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={showing}
          aria-controls={showing ? listId : undefined}
          aria-activedescendant={showing && completion.items.length ? `${listId}-${selected}` : undefined}
          className="sql-editor"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          wrap="off"
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            const input = e.nativeEvent as InputEvent;
            setCompletionMode(composing || input.isComposing || input.inputType?.startsWith("history") ? false : "auto");
            onChange(e.target.value);
          }}
          onFocus={() => { focusState.current = true; }}
          onBlur={() => { focusState.current = false; setCompletionMode(false); }}
          onPointerDown={() => setCompletionMode(false)}
          onSelect={onSelect}
          onScroll={() => { follow(); positionCompletion(); }}
          onKeyDown={completionKeyDown}
          onCompositionStart={() => { setComposing(true); setCompletionMode(false); }}
          onCompositionEnd={() => { setComposing(false); setCompletionMode(false); }}
        />
        {showing && createPortal(
          <div ref={popup} className="sql-completion" style={position}>
            <div id={listId} role="listbox" aria-label="SQL suggestions" className="sql-completion-list">
              {completion.items.map((item, index) => (
                <div key={`${item.kind}:${item.insert}`} id={`${listId}-${index}`} role="option"
                  aria-selected={index === selected} className="sql-completion-option"
                  onMouseDown={(e) => { e.preventDefault(); acceptCompletion(index); }}
                  onMouseMove={() => setChoice(index)}>
                  <span className="sql-completion-label">{item.label}</span>
                  <span className="sql-completion-kind">{item.kind}</span>
                  <span className="sql-completion-detail">{item.detail}</span>
                </div>
              ))}
            </div>
            <div className="sql-completion-hint" role="status">
              {completion.loading ? "Reading catalog…" : completion.note || "Enter or Tab to insert · Esc to dismiss"}
            </div>
          </div>, document.body,
        )}
      </div>
    </div>
  );
});
