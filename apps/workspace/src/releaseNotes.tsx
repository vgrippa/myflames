// Release notes in the Software update dialog: the GitHub release body
// (Markdown) rendered as formatted text. The parser below understands a small
// subset, scanning line by line in linear time, and the component draws the
// block tree with React text children only: no dangerouslySetInnerHTML, no
// innerHTML, and no href, src or style built from the notes, so HTML in the
// notes shows as literal text. Links render their text only; the dialog's
// View on GitHub link is the way to the page.
import { useMemo, type ReactNode } from "react";

const CAP = 100_000;

export type NotesInline =
  | { t: "text"; text: string }
  | { t: "code"; text: string }
  | { t: "strong"; children: NotesInline[] }
  | { t: "em"; children: NotesInline[] };

export type NotesBlock =
  | { kind: "heading"; deep: boolean; inline: NotesInline[] }
  | { kind: "list"; ordered: boolean; start?: number; items: NotesItem[] }
  | { kind: "code"; text: string }
  | { kind: "paragraph"; inline: NotesInline[] };

export type NotesItem = { inline: NotesInline[]; children?: NotesBlock };

/** Record escape parity once, including overlapping closing candidates. */
function escapePositions(s: string): Uint8Array {
  const escaped = new Uint8Array(s.length);
  let slashes = 0;
  for (let i = 0; i < s.length; i++) {
    escaped[i] = slashes % 2;
    slashes = s[i] === "\\" ? slashes + 1 : 0;
  }
  return escaped;
}

/** Hide complete code spans before any emphasis or link searches. The map is
 * keyed by offsets, so literal private-use characters cannot forge a token. */
function codeSpans(raw: string): {
  text: string; codes: Map<number, string>; codeSpaces: Set<number>;
} {
  const escaped = escapePositions(raw);
  const runs: { from: number; end: number; next: number }[] = [];
  for (let i = 0; i < raw.length;) {
    if (raw[i] !== "`") { i++; continue; }
    const from = i;
    while (raw[i] === "`") i++;
    if (!escaped[from]) runs.push({ from, end: i, next: -1 });
  }
  const next = new Map<number, number>();
  for (let i = runs.length - 1; i >= 0; i--) {
    const run = runs[i];
    const length = run.end - run.from;
    run.next = next.get(length) ?? -1;
    next.set(length, i);
  }
  const parts: string[] = [];
  const codes = new Map<number, string>();
  const codeSpaces = new Set<number>();
  let from = 0;
  let length = 0;
  for (let i = 0; i < runs.length; i++) {
    const run = runs[i];
    if (run.next === -1) continue;
    const close = runs[run.next];
    const prefix = raw.slice(from, run.from);
    parts.push(prefix, "\uE000");
    length += prefix.length;
    let content = raw.slice(run.end, close.from);
    if (/\s/.test(content)) codeSpaces.add(length);
    if (content.startsWith(" ") && content.endsWith(" ") && /\S/.test(content))
      content = content.slice(1, -1);
    codes.set(length++, content);
    from = close.end;
    i = run.next;
  }
  parts.push(raw.slice(from));
  return { text: parts.join(""), codes, codeSpaces };
}

export function parseInline(raw: string): NotesInline[] {
  const { text: s, codes, codeSpaces } = codeSpans(raw);
  const escaped = escapePositions(s);
  // Pair parentheses once and count whitespace once. Invalid links with many
  // shared suffixes must never rescan a destination for every '[' opener.
  const parens = new Map<number, number>();
  const stack: number[] = [];
  const spaces = new Uint32Array(s.length + 1);
  for (let i = 0; i < s.length; i++) {
    spaces[i + 1] = spaces[i] + (/\s/.test(s[i]) || codeSpaces.has(i) ? 1 : 0);
    if (escaped[i]) continue;
    if (s[i] === "(") stack.push(i);
    else if (s[i] === ")" && stack.length) parens.set(stack.pop()!, i);
  }
  const links = new Map<number, { labelEnd: number; end: number }>();
  let bracket = -1;
  for (let i = 0; i < s.length; i++) {
    if (escaped[i]) continue;
    if (s[i] === "[") bracket = i;
    else if (s[i] === "]") {
      const end = s[i + 1] === "(" ? parens.get(i + 1) : undefined;
      if (bracket !== -1 && end !== undefined && spaces[end] === spaces[i + 2])
        links.set(bracket, { labelEnd: i, end });
      bracket = -1;
    }
  }
  const searches = new Map<string, { from: number; at: number }>();
  const closing = (from: number, marker: string): number => {
    const cached = searches.get(marker);
    if (cached && from >= cached.from && (cached.at === -1 || from <= cached.at)) return cached.at;
    let at = s.indexOf(marker, from);
    // Advance one character: an escaped '**' may overlap an unescaped '**'.
    while (at !== -1 && escaped[at]) at = s.indexOf(marker, at + 1);
    searches.set(marker, { from, at });
    return at;
  };
  const wordBefore = (i: number) => /[\p{L}\p{N}]$/u.test(s.slice(Math.max(0, i - 2), i));
  const wordAfter = (i: number) => /^[\p{L}\p{N}]/u.test(s.slice(i, i + 2));
  const parse = (start: number, end: number, active: Set<string>): NotesInline[] => {
    const out: NotesInline[] = [];
    let buf = "";
    const flush = () => {
      if (buf) { out.push({ t: "text", text: buf }); buf = ""; }
    };
    let i = start;
    while (i < end) {
      const code = codes.get(i);
      if (code !== undefined) {
        flush();
        out.push({ t: "code", text: code });
        i++;
        continue;
      }
      const c = s[i];
      if (c === "\\" && i + 1 < end && "*_`\\[]()".includes(s[i + 1])) {
        buf += s[i + 1];
        i += 2;
        continue;
      }
      if (c === "*" || c === "_") {
        const double = i + 1 < end && s[i + 1] === c;
        const marker = double ? c + c : c;
        // Fixed marker kinds bound recursion depth. Full CommonMark delimiter
        // runs are outside this subset; intra-word Unicode underscores stay text.
        if (!active.has(marker) && !(c === "_" && wordBefore(i))) {
          const close = closing(i + marker.length, marker);
          if (close !== -1 && close + marker.length <= end) {
            const enclosed = close > i + marker.length &&
              !/\s/.test(s[i + marker.length]) && !/\s/.test(s[close - 1]);
            if (enclosed && !(c === "_" && wordAfter(close + marker.length))) {
              flush();
              out.push({ t: double ? "strong" : "em",
                children: parse(i + marker.length, close, new Set([...active, marker])) });
              i = close + marker.length;
              continue;
            }
          }
        }
      }
      if (c === "[") {
        const link = links.get(i);
        if (link && link.end < end) {
          flush();
          // Text only: no anchor, no href from the notes.
          out.push(...parse(i + 1, link.labelEnd, active));
          i = link.end + 1;
          continue;
        }
      }
      buf += c;
      i++;
    }
    flush();
    return out;
  };
  return parse(0, s.length, new Set());
}

export function parseReleaseNotes(raw: string): NotesBlock[] {
  const truncated = raw.length > CAP;
  // A cap boundary must not leave half an astral character in React text.
  const limit = truncated && /[\uD800-\uDBFF]/.test(raw[CAP - 1]) ? CAP - 1 : CAP;
  const lines = (truncated ? raw.slice(0, limit) : raw).replace(/\r\n?/g, "\n").split("\n");
  const blocks: NotesBlock[] = [];
  let paragraph: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join(" ")) });
      paragraph = [];
    }
  };
  const itemMatch = (line: string) => /^( *)(?:([-*+])|(\d+)\.)[ \t]+(\S.*)$/.exec(line);
  const headingMatch = (line: string) => /^(#{1,6})\s+(.*)$/.exec(line);
  const fenceMatch = (line: string) => /^(`{3,})/.exec(line);
  const rule = (line: string) => line === "---" || line === "***";
  const blockBoundary = (line: string) => !!fenceMatch(line) || !!headingMatch(line) || rule(line);
  type RawItem = { text: string[]; children?: RawList };
  type RawList = { ordered: boolean; start?: number; items: RawItem[] };
  const newList = (item: RegExpExecArray): RawList => ({
    ordered: item[3] !== undefined,
    start: item[3] !== undefined ? Number(item[3]) : undefined,
    items: [],
  });
  const finishList = (list: RawList): NotesBlock => ({
    kind: "list", ordered: list.ordered, start: list.start,
    items: list.items.map((item) => ({
      inline: parseInline(item.text.join(" ")),
      ...(item.children ? { children: finishList(item.children) } : {}),
    })),
  });
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    const fence = fenceMatch(trimmed);
    if (fence) {
      flushParagraph();
      const body: string[] = [];
      const indent = line.length - line.trimStart().length;
      i++;
      while (i < lines.length) {
        const close = /^(`+)\s*$/.exec(lines[i].trimStart());
        if (close && close[1].length >= fence[1].length) { i++; break; }
        // Remove only the opener's indentation, preserving the code's own.
        const leading = lines[i].length - lines[i].trimStart().length;
        body.push(lines[i].slice(Math.min(indent, leading)));
        i++;
      }
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    if (trimmed === "") { flushParagraph(); i++; continue; }
    if (rule(trimmed)) { flushParagraph(); i++; continue; }
    const heading = headingMatch(trimmed);
    if (heading) {
      flushParagraph();
      let title = heading[2].trim();
      let end = title.length;
      while (end > 0 && title[end - 1] === "#") end--;
      if (end < title.length && end > 0 && /\s/.test(title[end - 1]))
        title = title.slice(0, end).trimEnd();
      blocks.push({ kind: "heading", deep: heading[1].length > 2, inline: parseInline(title) });
      i++;
      continue;
    }
    const first = itemMatch(line);
    if (first) {
      flushParagraph();
      const list = newList(first);
      const indent = first[1].length;
      let parent: RawItem | undefined;
      let current: RawItem | undefined;
      while (i < lines.length) {
        const l = lines[i];
        const trimmedLine = l.trim();
        if (trimmedLine === "") {
          // Consume each blank run once. The next nonblank line decides
          // whether this list continues or the outer block parser resumes.
          while (i < lines.length && lines[i].trim() === "") i++;
          continue;
        }
        if (blockBoundary(trimmedLine)) break;
        const item = itemMatch(l);
        if (item) {
          if (parent && item[1].length >= indent + 2) {
            // Every deeper marker flattens into this one nested list.
            if (!parent.children) parent.children = newList(item);
            current = { text: [item[4]] };
            parent.children.items.push(current);
          } else if (item[1].length === indent && (item[3] !== undefined) === list.ordered) {
            parent = current = { text: [item[4]] };
            list.items.push(parent);
          } else break;
          i++;
          continue;
        }
        if (current && l.startsWith(" ".repeat(indent + 2))) {
          current.text.push(trimmedLine);
          i++;
          continue;
        }
        break;
      }
      blocks.push(finishList(list));
      continue;
    }
    paragraph.push(trimmed);
    i++;
  }
  flushParagraph();
  if (truncated)
    blocks.push({ kind: "paragraph", inline: [{ t: "text", text: "The rest of the notes is on GitHub." }] });
  return blocks;
}

function renderInline(nodes: NotesInline[]): ReactNode {
  return nodes.map((node, i) => {
    switch (node.t) {
      case "code": return <code key={i}>{node.text}</code>;
      case "strong": return <strong key={i}>{renderInline(node.children)}</strong>;
      case "em": return <em key={i}>{renderInline(node.children)}</em>;
      default: return node.text;
    }
  });
}

function renderBlock(block: NotesBlock, key: number): ReactNode {
  switch (block.kind) {
    case "heading": {
      const Tag = block.deep ? "h4" : "h3";
      return <Tag key={key}>{renderInline(block.inline)}</Tag>;
    }
    case "list": {
      const items = block.items.map((item, i) => <li key={i}>{renderInline(item.inline)}
        {item.children ? renderBlock(item.children, 0) : null}</li>);
      return block.ordered
        ? <ol key={key} start={block.start !== 1 ? block.start : undefined}>{items}</ol>
        : <ul key={key}>{items}</ul>;
    }
    case "code": return <pre key={key}><code>{block.text}</code></pre>;
    default: return <p key={key}>{renderInline(block.inline)}</p>;
  }
}

export function ReleaseNotes({ text }: { text: string }) {
  const blocks = useMemo(() => parseReleaseNotes(text), [text]);
  return <>{blocks.map(renderBlock)}</>;
}
