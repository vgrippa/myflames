// Decide which SQL text in the query editor runs: the selection, the
// statement under the cursor, or the editor's only statement.
//
// Pure functions, no DOM. Tokenization follows the capture validator in
// myflames/live.py (TOKEN): `/* */` block comments, `-- ` comments (the
// dashes must be followed by whitespace or the end of text), `#` comments,
// single and double quoted strings with doubled quotes and backslash escapes,
// and backtick identifiers with doubled backticks. A `;` inside any of them
// does not end a statement. `:name` parameters never contain `;`, so they
// need no special handling here; the server binds them against the text
// that is sent.
//
// `DELIMITER x` lines are read like the mysql client (and dbops.split_statements,
// the Python twin): at the start of a line, while the statement so far holds
// only comments or white space, the word DELIMITER (case-insensitive) and an
// argument that is quoted or ends at a space. They change the delimiter and
// are never part of a statement.

/** One statement in the editor. Offsets index into the source text. */
export type Statement = {
  /** First character of the statement, leading comments included. */
  start: number;
  /** Just past its last character, before trailing whitespace and the `;`. */
  end: number;
  /** `source.slice(start, end)`: the text that runs. */
  text: string;
  /** Cursor positions `from` through `to` (inclusive) choose this statement. */
  from: number;
  to: number;
  /** The delimiter that ends it (";" unless a DELIMITER line changed it). */
  delimiter: string;
};

export type RunScope = "selection" | "statement" | "editor";

export type RunTarget = {
  scope: RunScope;
  /** SQL to send as `query`; empty when `problem` is set. */
  text: string;
  /** Offsets of `text` in the editor. */
  start: number;
  end: number;
  /** 1-based statement number and the statement count (scope "statement"). */
  index: number;
  total: number;
  /** Lines the selected text spans (scope "selection"). */
  lines: number;
  /** Why nothing can run; the request is not sent when this is set. */
  problem: string;
};

const isSpace = (c: string | undefined) =>
  c === " " ||
  c === "\t" ||
  c === "\n" ||
  c === "\r" ||
  c === "\f" ||
  c === "\v";

/**
 * The end of a comment, quoted string, or quoted identifier that starts at
 * `i`, or null when none starts there. Unterminated tokens run to the end of
 * the text, so a stray quote never splits a statement.
 */
function tokenAt(
  sql: string,
  i: number,
  backslash = true,
): { end: number; comment: boolean } | null {
  const c = sql[i];
  if (c === "/" && sql[i + 1] === "*") {
    const close = sql.indexOf("*/", i + 2);
    return { end: close < 0 ? sql.length : close + 2, comment: true };
  }
  if (
    c === "#" ||
    (c === "-" &&
      sql[i + 1] === "-" &&
      (i + 2 >= sql.length || isSpace(sql[i + 2])))
  ) {
    let j = i;
    while (j < sql.length && sql[j] !== "\n" && sql[j] !== "\r") j++;
    return { end: j, comment: true };
  }
  if (c === "'" || c === '"' || c === "`") {
    let j = i + 1;
    while (j < sql.length) {
      if (sql[j] === "\\" && c !== "`" && backslash) j += 2;
      else if (sql[j] !== c) j++;
      else if (sql[j + 1] === c) j += 2;
      else return { end: j + 1, comment: false };
    }
    return { end: sql.length, comment: false };
  }
  return null;
}

/**
 * Where the cursor range of a statement ends after its `;` at `after - 1`.
 * Spaces and comments that finish the same line still belong to it
 * (`SELECT 1; -- note`), so the range runs to the line break. When more SQL
 * follows on that line, the range ends right after the `;`.
 */
function tailEnd(sql: string, after: number) {
  let j = after;
  while (j < sql.length) {
    const c = sql[j];
    if (c === "\n" || c === "\r") return j;
    if (c === " " || c === "\t") {
      j++;
      continue;
    }
    const token = tokenAt(sql, j);
    if (!token || !token.comment) return after;
    j = token.end;
  }
  return j;
}

/** The opener of an executable comment (dbops._VERSIONED_OPEN). */
const VERSIONED_OPEN = /^\/\*M?!(?:\d{6}(?=\s)|\d{5})?/;

/**
 * A `DELIMITER x` client command at line start `i`: the new delimiter ("" when
 * the argument is missing or holds a backslash: the client keeps the old one)
 * and the end of its line. Python twin: dbops._DELIMITER_COMMAND and
 * _delimiter_argument (the client compares only the word's first 9 letters).
 */
function delimiterCommand(sql: string, i: number): { value: string; end: number } | null {
  let p = i;
  while (p < sql.length && (sql[p] === " " || sql[p] === "\t" || sql[p] === "\v" || sql[p] === "\f" || sql[p] === "\r")) p++;
  const first = sql.charCodeAt(p);
  if (first !== 68 && first !== 100) return null;
  const command = /^[ \t\v\f\r]*delimiter[^ \t\r\n]*/i.exec(sql.slice(i, i + 4096));
  if (!command) return null;
  const at = i + command[0].length;
  const newline = sql.indexOf("\n", at);
  const stop = newline < 0 ? sql.length : newline;
  const rest = sql.slice(at, stop);
  let k = 0;
  while (k < rest.length && " \t\n\r\f\v".includes(rest[k])) k++;
  let value: string;
  if ("'\"`".includes(rest[k] || "x")) {
    const close = rest.indexOf(rest[k], k + 1);
    value = close < 0 ? rest.slice(k + 1) : rest.slice(k + 1, close);
  } else {
    const space = rest.indexOf(" ", k);
    value = space < 0 ? rest.slice(k) : rest.slice(k, space);
  }
  return { value: value && !value.includes("\\") ? value : "", end: stop };
}

let cachedSplitSql = "";
let cachedSplitBackslash = true;
let cachedSplitStatements: Statement[] | null = null;

/**
 * Split editor text into statements at `;` (or the delimiter a DELIMITER
 * line set) outside quotes and comments. Only statements with SQL beyond
 * comments and whitespace are returned; the cursor ranges of the others
 * merge into a neighbour, so together the ranges cover every position from 0
 * to `sql.length`.
 */
export function splitStatements(sql: string, backslash = true): Statement[] {
  if (cachedSplitStatements !== null && sql === cachedSplitSql && backslash === cachedSplitBackslash) {
    return cachedSplitStatements;
  }
  const segments: {
    from: number;
    begin: number;
    stop: number;
    to: number;
    code: boolean;
    delimiter: string;
  }[] = [];
  let from = 0;
  // Where the statement text may start: after a DELIMITER line, not before.
  let begin = 0;
  let code = false;
  let delimiter = ";";
  // Inside a /*! or /*M! executable comment: its body is code.
  let versioned = false;
  let i = 0;
  const finish = (stop: number, length: number) => {
    const to = tailEnd(sql, stop + length);
    segments.push({ from, begin, stop, to, code, delimiter });
    from = begin = i = to;
    code = false;
  };
  while (i < sql.length) {
    const ch = sql[i];
    if (!code && !versioned && (i === 0 || sql[i - 1] === "\n")) {
      const command = delimiterCommand(sql, i);
      if (command) {
        if (command.value) delimiter = command.value;
        i = begin = command.end;
        continue;
      }
    }
    if (versioned && ch === "*" && sql[i + 1] === "/") {
      versioned = false;
      i += 2;
      continue;
    }
    const opener =
      !versioned && ch === "/" && sql[i + 1] === "*" && (sql[i + 2] === "!" || sql[i + 2] === "M")
        ? VERSIONED_OPEN.exec(sql.slice(i, i + 16))
        : null;
    if (opener) {
      versioned = true;
      i += opener[0].length;
      continue;
    }
    const token =
      ch === "/" || ch === "#" || ch === "-" || ch === "'" || ch === '"' || ch === "`"
        ? tokenAt(sql, i, backslash)
        : null;
    if (token) {
      if (!token.comment) code = true;
      i = token.end;
    } else if (delimiter.length === 1 ? ch === delimiter : sql.startsWith(delimiter, i)) {
      finish(i, delimiter.length);
    } else if (ch === "\\" && (sql[i + 1] === "g" || sql[i + 1] === "G")) {
      // \g and \G end a statement, as in the mysql client.
      finish(i, 2);
    } else {
      if (!isSpace(ch)) code = true;
      i++;
    }
  }
  segments.push({ from, begin, stop: sql.length, to: sql.length, code, delimiter });
  const statements: Statement[] = [];
  for (const segment of segments) {
    if (!segment.code) {
      // Blank or comment-only: its positions choose the statement before it.
      const previous = statements[statements.length - 1];
      if (previous) previous.to = segment.to;
      continue;
    }
    let start = segment.begin;
    let end = segment.stop;
    while (start < end && isSpace(sql[start])) start++;
    while (end > start && isSpace(sql[end - 1])) end--;
    statements.push({
      start,
      end,
      text: sql.slice(start, end),
      from: segment.from,
      to: segment.to,
      delimiter: segment.delimiter,
    });
  }
  // Positions before the first statement choose it.
  if (statements.length) statements[0].from = 0;
  if (sql.length <= 2_000_000) {
    cachedSplitSql = sql;
    cachedSplitBackslash = backslash;
    cachedSplitStatements = statements;
  }
  return statements;
}

/** Index of the statement that contains `cursor`, or -1 when there is none. */
export function statementAt(statements: Statement[], cursor: number) {
  if (!statements.length) return -1;
  let lo = 0;
  let hi = statements.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cursor <= statements[mid].to) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

let cachedLineSql = "";
let cachedLineBreaks: number[] = [];
function getLineBreaks(sql: string): number[] {
  if (sql === cachedLineSql) return cachedLineBreaks;
  const breaks: number[] = [];
  for (let i = 0; i < sql.length; i++) {
    const c = sql.charCodeAt(i);
    if (c === 10 || (c === 13 && sql.charCodeAt(i + 1) !== 10)) breaks.push(i);
  }
  if (sql.length <= 2_000_000) {
    cachedLineSql = sql;
    cachedLineBreaks = breaks;
  }
  return breaks;
}

/** 1-based line number of `offset`. */
export function lineOf(sql: string, offset: number) {
  const limit = Math.min(offset, sql.length);
  if (limit <= 0) return 1;
  const breaks = getLineBreaks(sql);
  let lo = 0;
  let hi = breaks.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (breaks[mid] < limit) lo = mid + 1;
    else hi = mid;
  }
  return 1 + lo;
}

/** Number of lines in `text` (1 for a single line). */
export function lineCount(text: string) {
  return text.split(/\r\n|\r|\n/).length;
}

/**
 * What Explain or Run analysis sends for this editor state.
 *
 * - Selected text (anything beyond whitespace) runs alone: trimmed, without
 *   its trailing `;` or comments after it. A selection with several
 *   statements, or only comments, is a problem instead.
 * - Without a selection, several statements run the one under the cursor.
 * - One statement runs as a whole. An editor with no statement sends its
 *   text unchanged, so the server reports what is missing.
 */
export function runTarget(
  sql: string,
  selectionStart: number,
  selectionEnd: number,
): RunTarget {
  const lo = Math.max(0, Math.min(selectionStart, selectionEnd, sql.length));
  const hi = Math.min(sql.length, Math.max(selectionStart, selectionEnd));
  const selected = sql.slice(lo, hi);
  const none = { index: 0, total: 0, lines: 0, problem: "" };
  if (selected.trim()) {
    const parts = splitStatements(selected);
    const lines = lineCount(selected.trim());
    const base = { ...none, scope: "selection" as const, lines };
    if (!parts.length)
      return {
        ...base,
        text: "",
        start: lo,
        end: hi,
        problem:
          "The selection holds only comments. Select a SELECT statement to run.",
      };
    if (parts.length > 1)
      return {
        ...base,
        text: "",
        start: lo,
        end: hi,
        total: parts.length,
        problem: `The selection holds ${parts.length} statements. Select one statement, or clear the selection to run the statement at the cursor.`,
      };
    return {
      ...base,
      text: parts[0].text,
      start: lo + parts[0].start,
      end: lo + parts[0].end,
    };
  }
  const statements = splitStatements(sql);
  if (statements.length > 1) {
    const k = statementAt(statements, selectionEnd);
    const s = statements[k];
    return {
      ...none,
      scope: "statement",
      text: s.text,
      start: s.start,
      end: s.end,
      index: k + 1,
      total: statements.length,
    };
  }
  const only = statements[0];
  return only
    ? { ...none, scope: "editor", text: only.text, start: only.start, end: only.end, total: 1 }
    : { ...none, scope: "editor", text: sql, start: 0, end: sql.length };
}

/** Short label for the scope chip: "Selection · 3 lines", "Statement 2 of 3 · lines 4–6". */
export function describeTarget(sql: string, target: RunTarget) {
  if (target.scope === "selection")
    return `Selection · ${target.lines} ${target.lines === 1 ? "line" : "lines"}`;
  if (target.scope !== "statement") return "";
  const first = lineOf(sql, target.start);
  const last = lineOf(sql, target.end);
  return `Statement ${target.index} of ${target.total} · ${
    first === last ? `line ${first}` : `lines ${first}–${last}`
  }`;
}

// ---- Run in the Query editor's session (any SQL, /api/editor/run) ----------

/** What Run and Run all send to the tab's session. The server decides what
 * runs: it splits the text itself (myflames/dbops.py, the session's reading
 * of backslashes), so `start`/`end` here only draw the tint and the flash. */
export type SqlRun = {
  scope: "selection" | "statement" | "all";
  /** The part to run (UTF-16 offsets of the whole text). */
  selection?: { start: number; end: number };
  /** The caret: the server runs the one statement it chooses there. */
  cursor?: number;
  /** The part of the editor that is expected to run (cosmetic). */
  start: number;
  end: number;
  /** 1-based statement number and count (scope "statement"). */
  index: number;
  total: number;
  /** Why nothing can run; nothing is sent when set. */
  problem: string;
};

/** The delimiter in effect at `offset` (";" unless a DELIMITER line changed it). */
export function delimiterAt(sql: string, offset: number, backslash = true) {
  const statements = splitStatements(sql, backslash);
  const k = statementAt(statements, offset);
  return k < 0 ? ";" : statements[k].delimiter;
}

/**
 * What Run (the selection, else the statement at the cursor) or Run all (the
 * whole text) sends. Run sends the selection's offsets, or only the caret:
 * the server chooses the statement there with the rules of
 * editor_session.statement_at (the twin of statementAt; shared cases in
 * test/fixtures/sql-split.json).
 */
export function sqlRun(sql: string, selectionStart: number, selectionEnd: number, all = false, backslash = true): SqlRun {
  const none = { index: 0, total: 0, problem: "" };
  if (all)
    return sql.trim()
      ? { ...none, scope: "all", start: 0, end: sql.length }
      : { ...none, scope: "all", start: 0, end: 0, problem: "Write SQL to run." };
  const lo = Math.max(0, Math.min(selectionStart, selectionEnd, sql.length));
  const hi = Math.min(sql.length, Math.max(selectionStart, selectionEnd));
  if (sql.slice(lo, hi).trim()) return { ...none, scope: "selection", selection: { start: lo, end: hi }, start: lo, end: hi };
  const statements = splitStatements(sql, backslash);
  if (!statements.length)
    return { ...none, scope: "statement", start: lo, end: lo, problem: "There is no statement at the cursor. Write SQL to run." };
  const k = statementAt(statements, selectionEnd);
  const s = statements[k];
  return { ...none, scope: "statement", cursor: hi, start: s.start, end: s.end, index: k + 1, total: statements.length };
}
