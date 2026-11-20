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
  | { kind: "list"; ordered: boolean; items: NotesInline[][] }
  | { kind: "code"; text: string }
  | { kind: "paragraph"; inline: NotesInline[] };

const wordChar = (c: string | undefined) => !!c && /[A-Za-z0-9]/.test(c);
/** The next unescaped position of a marker, or -1 when it never closes. */
function findClosing(s: string, from: number, marker: string): number {
  let at = s.indexOf(marker, from);
  // Advance past each escaped candidate once; never restart this scan.
  while (at > 0 && s[at - 1] === "\\") at = s.indexOf(marker, at + marker.length);
  return at;
}

export function parseInline(s: string): NotesInline[] {
  const out: NotesInline[] = [];
  const searches = new Map<string, { from: number; at: number }>();
  const closing = (from: number, marker: string, escaped = true): number => {
    const cached = searches.get(marker);
    // A failed suffix stays failed for later openers. Keep successful results
    // too: many '[' openers can share one '](' when its ')' is missing.
    if (cached && from >= cached.from && (cached.at === -1 || from <= cached.at)) return cached.at;
    const at = escaped ? findClosing(s, from, marker) : s.indexOf(marker, from);
    searches.set(marker, { from, at });
    return at;
  };
  let buf = "";
  const flush = () => {
    if (buf) { out.push({ t: "text", text: buf }); buf = ""; }
  };
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === "\\" && i + 1 < s.length && "*_`\\".includes(s[i + 1])) {
      buf += s[i + 1];
      i += 2;
      continue;
    }
    if (c === "`") {
      const close = closing(i + 1, "`");
      if (close !== -1) {
        flush();
        out.push({ t: "code", text: s.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
      buf += c;
      i++;
      continue;
    }
    if (c === "*" || c === "_") {
      const double = s[i + 1] === c;
      const marker = double ? c + c : c;
      // Underscore markers never act inside a word (snake_case stays text).
      if (c === "_" && wordChar(s[i - 1])) { buf += c; i++; continue; }
      const close = closing(i + marker.length, marker);
      if (close !== -1) {
        const inner = s.slice(i + marker.length, close);
        const enclosed = inner.length > 0 && !/^\s|\s$/.test(inner);
        if (enclosed && !(c === "_" && wordChar(s[close + marker.length]))) {
          flush();
          // Consume the inner span, so the caller never scans it again.
          // Nesting cannot repeat an unescaped closing marker, bounding depth
          // by the fixed marker kinds and keeping total work linear.
          out.push({ t: double ? "strong" : "em", children: parseInline(inner) });
          i = close + marker.length;
          continue;
        }
      }
      buf += c;
      i++;
      continue;
    }
    if (c === "[") {
      const closeBracket = closing(i + 1, "](", false);
      if (closeBracket !== -1) {
        const closeParen = closing(closeBracket + 2, ")", false);
        if (closeParen !== -1) {
          flush();
          // Text only: no anchor, no href from the notes.
          out.push(...parseInline(s.slice(i + 1, closeBracket)));
          i = closeParen + 1;
          continue;
        }
      }
      buf += c;
      i++;
      continue;
    }
    buf += c;
    i++;
  }
  flush();
  return out;
}

export function parseReleaseNotes(raw: string): NotesBlock[] {
  const truncated = raw.length > CAP;
  const lines = (truncated ? raw.slice(0, CAP) : raw).replace(/\r\n?/g, "\n").split("\n");
  const blocks: NotesBlock[] = [];
  let paragraph: string[] = [];
  const flushParagraph = () => {
    if (paragraph.length) {
      blocks.push({ kind: "paragraph", inline: parseInline(paragraph.join(" ")) });
      paragraph = [];
    }
  };
  const itemMatch = (line: string) => /^\s*(?:([-*+])|(\d+)\.)\s+(.*)$/.exec(line);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (trimmed.startsWith("```")) {
      flushParagraph();
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) { body.push(lines[i]); i++; }
      if (i < lines.length) i++;
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    if (trimmed === "") { flushParagraph(); i++; continue; }
    if (trimmed === "---" || trimmed === "***") { flushParagraph(); i++; continue; }
    const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      blocks.push({ kind: "heading", deep: heading[1].length > 2, inline: parseInline(heading[2].trim()) });
      i++;
      continue;
    }
    const first = itemMatch(line);
    if (first) {
      flushParagraph();
      const ordered = first[2] !== undefined;
      const items: NotesInline[][] = [];
      let current: string[] = [];
      while (i < lines.length) {
        const l = lines[i];
        if (l.trim() === "") {
          // A blank line never splits the list; it splits only when what
          // follows is not another item of this list.
          let j = i;
          while (j < lines.length && lines[j].trim() === "") j++;
          const next = j < lines.length ? itemMatch(lines[j]) : null;
          if (next && (next[2] !== undefined) === ordered) { i = j; continue; }
          break;
        }
        const item = itemMatch(l);
        if (item && (item[2] !== undefined) === ordered) {
          if (current.length) items.push(parseInline(current.join(" ")));
          current = [item[3]];
          i++;
          continue;
        }
        if (/^\s+\S/.test(l) && current.length) { current.push(l.trim()); i++; continue; }
        break;
      }
      if (current.length) items.push(parseInline(current.join(" ")));
      blocks.push({ kind: "list", ordered, items });
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

export function ReleaseNotes({ text }: { text: string }) {
  const blocks = useMemo(() => parseReleaseNotes(text), [text]);
  return (
    <>
      {blocks.map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const Tag = block.deep ? "h4" : "h3";
            return <Tag key={i}>{renderInline(block.inline)}</Tag>;
          }
          case "list": {
            const items = block.items.map((item, j) => <li key={j}>{renderInline(item)}</li>);
            return block.ordered ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>;
          }
          case "code": return <pre key={i}><code>{block.text}</code></pre>;
          default: return <p key={i}>{renderInline(block.inline)}</p>;
        }
      })}
    </>
  );
}
