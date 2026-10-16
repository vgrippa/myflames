// The Query editor's text area: a native <textarea> (typing, undo, IME,
// spell check off, screen readers and selection all stay the browser's) over
// a layer that draws the same text with syntax colors, the statement at the
// cursor, a bracket or quote pair, and an error position, plus a line number
// gutter. The layers repeat the textarea's text metrics exactly and follow its
// scroll position with a transform, so nothing re-renders on scroll.
import {
  forwardRef,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
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
};

/** The textarea and its layers; the ref is the textarea itself. */
export const SqlEditor = forwardRef<HTMLTextAreaElement, SqlEditorProps>(function SqlEditor(
  { value, onChange, onSelect, onKeyDown, caret, statement, flash, error, describedBy, height, placeholder },
  ref,
) {
  const area = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => area.current as HTMLTextAreaElement);
  const layer = useRef<HTMLDivElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const [composing, setComposing] = useState(false);
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
          className="sql-editor"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          wrap="off"
          placeholder={placeholder}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onSelect={onSelect}
          onScroll={follow}
          onKeyDown={onKeyDown}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
        />
      </div>
    </div>
  );
});
