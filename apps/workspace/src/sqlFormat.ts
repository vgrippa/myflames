// Format SQL in the Query editor. Loaded on first use (QueryLab imports it
// with import()), so sql-formatter stays out of the editor's chunk.
//
// The text is split into statements with the editor's own splitter
// (sqlSelection.ts). Each statement is laid out by sql-formatter (MIT,
// MySQL or MariaDB dialect, two-space indent, letter case kept), then the
// keywords whose case the server ignores are put in upper case
// (sqlFormatCheck.ts `upperKeywords`). Left exactly as written: statements in
// a DELIMITER block, stored program bodies (CREATE PROCEDURE, FUNCTION,
// TRIGGER, EVENT, compound statements), statements with a client command
// (a backslash), an unclosed quote or comment, a name in square brackets
// (MariaDB sql_mode=MSSQL), an executable comment or optimizer hint the
// formatter would change, and statements the formatter cannot read. Comments and blank lines between statements stay as they are.
//
// Nothing is ever run. The whole result is compared with the input token by
// token (`compareSql`); any difference beyond white space and keyword case
// refuses the format and nothing changes.
import { formatDialect, mariadb, mysql } from "sql-formatter";
import { delimiterAt, lineOf, splitStatements } from "./sqlSelection";
import { READINGS, compareSql, tokenize, upperKeywords, type SqlToken } from "./sqlFormatCheck";

export type FormatDialect = "mysql" | "mariadb";
export type KeptStatement = { line: number; reason: string };
export type FormatOutcome =
  | { status: "formatted"; text: string; formatted: number; kept: KeptStatement[] }
  | { status: "unchanged"; message: string; kept: KeptStatement[] }
  | { status: "refused"; message: string; line: number };

const COMPOUND = new Set(["IF", "WHILE", "LOOP", "REPEAT", "CASE", "DECLARE", "RETURN", "LEAVE", "ITERATE"]);
const PROGRAM = new Set(["PROCEDURE", "FUNCTION", "TRIGGER", "EVENT", "PACKAGE"]);
const NOT_PROGRAM = new Set(["TABLE", "VIEW", "INDEX", "DATABASE", "SCHEMA", "USER", "ROLE", "SERVER", "TABLESPACE",
  "LOGFILE", "RESOURCE", "SEQUENCE", "SPATIAL", "UNIQUE", "FULLTEXT"]);

/** A stored program body or compound statement: its layout is the author's. */
function storedProgram(tokens: SqlToken[]) {
  const code = tokens.filter((t) => t.kind !== "comment" && t.kind !== "line" && t.kind !== "exec");
  const word = (k: number) => (code[k]?.kind === "word" ? code[k].text.toUpperCase() : "");
  const first = word(0);
  // A label (`name: BEGIN`), BEGIN NOT ATOMIC, or a compound statement.
  if (code[0]?.kind === "word" && code[1]?.text === ":") return true;
  if (first === "BEGIN" && word(1) === "NOT") return true;
  if (COMPOUND.has(first)) return true;
  if (first !== "CREATE" && first !== "ALTER") return false;
  for (let k = 1; k < Math.min(code.length, 16); k++) {
    const w = word(k);
    if (PROGRAM.has(w)) return true;
    if (NOT_PROGRAM.has(w) || code[k].text === "(") return false;
  }
  return false;
}

function lineAt(sql: string, offset: number) {
  let line = 1;
  for (let i = 0; i < offset && i < sql.length; i++) if (sql[i] === "\n") line++;
  return line;
}

/** Why a statement is left as written, or "" when it can be formatted. */
function keepReason(text: string, delimiter: string, tokens: SqlToken[]) {
  if (delimiter !== ";") return "inside a DELIMITER block";
  if (tokens.some((t) => t.kind === "unclosed")) return "an unclosed quote or comment";
  if (tokens.some((t) => t.kind === "op" && t.text === "\\")) return "a client command";
  // A quoted name under MariaDB's sql_mode=MSSQL, a syntax error elsewhere.
  if (tokens.some((t) => t.kind === "op" && t.text === "[")) return "a name in square brackets";
  if (storedProgram(tokens)) return "a stored program body";
  return text.trim() ? "" : "nothing to format";
}

/**
 * Format *text* (the whole tab or the selection). *firstLine* is the
 * editor line *text* starts on (for messages); *startsStatement* is false
 * when a selection starts inside a statement; *delimiter* is the delimiter
 * in force where the text starts.
 */
export function formatSql(
  text: string,
  options: { dialect: FormatDialect; startsStatement?: boolean; firstLine?: number; delimiter?: string },
): FormatOutcome {
  const firstLine = options.firstLine ?? 1;
  const startsStatement = options.startsStatement ?? true;
  const editorLine = (offset: number) => firstLine + lineAt(text, offset) - 1;
  if ((options.delimiter ?? ";") !== ";")
    return { status: "unchanged", kept: [], message: "Format SQL leaves a DELIMITER block as written, so nothing changed." };
  const statements = splitStatements(text);
  if (!statements.length) return { status: "unchanged", kept: [], message: "Nothing to format: there is no SQL statement here." };
  const dialect = options.dialect === "mariadb" ? mariadb : mysql;
  const kept: KeptStatement[] = [];
  const keep: [number, number][] = [];
  let formatted = 0;
  // The text before the first statement (white space, comments, DELIMITER lines) stays.
  let draft = text.slice(0, statements[0].start);
  keep.push([0, draft.length]);
  for (let k = 0; k < statements.length; k++) {
    const s = statements[k];
    const next = statements[k + 1];
    const tokens = tokenize(s.text);
    let reason = keepReason(s.text, s.delimiter, tokens);
    let out = s.text;
    if (!reason) {
      try {
        out = formatDialect(s.text, {
          dialect,
          tabWidth: 2,
          useTabs: false,
          keywordCase: "preserve",
          paramTypes: { positional: true, named: [":"] },
        }).trim();
      } catch {
        reason = "the formatter could not read it";
      }
    }
    if (!reason) {
      // Executable comments and optimizer hints are code: they stay byte for byte.
      const execs = (t: SqlToken[]) => t.filter((x) => x.kind === "exec").map((x) => x.text).join("\u0000");
      if (execs(tokenize(out)) !== execs(tokens)) reason = "an executable comment or optimizer hint the formatter would change";
    }
    if (!reason) {
      // The formatter changed more than white space: refuse everything.
      const difference = compareSql(s.text, out, { allowCase: false });
      if (difference) {
        const line = editorLine(s.start + difference.start);
        return {
          status: "refused",
          line,
          message: `Format SQL changed nothing: formatting would have changed “${difference.before}” to “${difference.after}” on line ${line}, which the server can read differently.`,
        };
      }
      formatted++;
    } else {
      out = s.text;
      kept.push({ line: editorLine(s.start), reason });
      keep.push([draft.length, draft.length + out.length]);
    }
    draft += out;
    // What follows the statement: its delimiter and the white space after it
    // become one blank line between statements; anything else stays as it is.
    const gap = text.slice(s.end, next ? next.start : text.length);
    const plain = /^(\s*);(\s*)$/.exec(gap);
    let tail: string;
    if (plain && s.delimiter === ";" && (!next || next.delimiter === ";")) {
      // A statement that ends with a line comment keeps its ; on the next line.
      const semicolon = tokenize(out).pop()?.kind === "line" ? "\n;" : ";";
      tail = semicolon + (next ? "\n\n" : plain[2]);
    } else tail = gap;
    keep.push([draft.length, draft.length + tail.length]);
    draft += tail;
  }
  // Statements after a DELIMITER line keep their text; the whole result is
  // then checked against the input, case changes included.
  const result = upperKeywords(text, draft, { startsStatement, keep });
  const difference = compareSql(text, result, { startsStatement });
  if (difference) {
    const line = editorLine(difference.start);
    return {
      status: "refused",
      line,
      message: `Format SQL changed nothing: formatting would have changed “${difference.before}” to “${difference.after}” on line ${line}, which the server can read differently.`,
    };
  }
  if (result === text) {
    return {
      status: "unchanged",
      kept,
      message: kept.length && !formatted ? `Nothing to format: ${describeKept(kept)}.` : "Already formatted: nothing changed.",
    };
  }
  return { status: "formatted", text: result, formatted, kept };
}

/** What a selection boundary cut, for the refusal. */
const INSIDE: Record<SqlToken["kind"], string> = {
  word: "a word", number: "a number", param: "a parameter", variable: "a variable", op: "an operator",
  string: "a quoted string", ident: "a quoted name", comment: "a comment", line: "a comment",
  exec: "an executable comment or hint", unclosed: "an unclosed quote or comment",
};

/**
 * Format SQL in the editor: the selection *from*..*to* of *sql*, or all of it
 * when nothing is selected. A selection may not start or end inside a word, a
 * quoted string or name, or a comment. The whole editor text after the change
 * is compared with the text before it (a fragment alone cannot show that it
 * sat inside a string), and any difference beyond white space and keyword
 * case refuses. On success `text` replaces `start`..`end`.
 */
export function formatEditorText(
  sql: string,
  from: number,
  to: number,
  options: { dialect: FormatDialect; backslash?: boolean },
): FormatOutcome & { start: number; end: number } {
  const whole = from === to;
  const start = whole ? 0 : Math.min(from, to);
  const end = whole ? sql.length : Math.max(from, to);
  const backslash = options.backslash ?? true;
  if (!whole) {
    for (const reading of READINGS) {
      const cut = tokenize(sql, reading.backslash, reading.brackets).find((t) => (t.start < start && start < t.end) || (t.start < end && end < t.end));
      if (cut) {
        const line = lineOf(sql, cut.start);
        return {
          status: "refused", line, start, end,
          message: `Format SQL changed nothing: the selection ${cut.start < start ? "starts" : "ends"} inside ${INSIDE[cut.kind]} on line ${line}. Select whole statements or clauses, or nothing to format the whole tab.`,
        };
      }
    }
  }
  const statements = whole ? [] : splitStatements(sql, backslash);
  const outcome = formatSql(sql.slice(start, end), {
    dialect: options.dialect,
    startsStatement: whole || !statements.some((s) => s.start < start && start < s.end),
    firstLine: lineOf(sql, start),
    delimiter: whole ? ";" : delimiterAt(sql, start, backslash),
  });
  if (outcome.status !== "formatted") return { ...outcome, start, end };
  const next = sql.slice(0, start) + outcome.text + sql.slice(end);
  const difference = compareSql(sql, next);
  if (difference) {
    return {
      status: "refused", line: difference.line, start, end,
      message: `Format SQL changed nothing: formatting would have changed “${difference.before}” to “${difference.after}” on line ${difference.line}, which the server can read differently.`,
    };
  }
  return { ...outcome, start, end };
}

/** "line 4 is a stored program body" / "lines 4 and 9 are kept as written". */
export function describeKept(kept: KeptStatement[]) {
  if (kept.length === 1) return `left line ${kept[0].line} as written (${kept[0].reason})`;
  const reasons = [...new Set(kept.map((k) => k.reason))];
  const lines = kept.map((k) => k.line);
  const list = lines.length > 4 ? `${lines.slice(0, 4).join(", ")} and ${lines.length - 4} more` : `${lines.slice(0, -1).join(", ")} and ${lines[lines.length - 1]}`;
  return `left ${kept.length} statements as written (lines ${list}: ${reasons.join("; ")})`;
}

/** Where *offset* in *before* lands in *after* when only white space and
 * letter case changed: next to the same character. A caret before a word
 * stays before it; one after a word (in white space) stays after it. */
export function mapOffset(before: string, after: string, offset: number) {
  let count = 0;
  for (let i = 0; i < offset && i < before.length; i++) if (!/\s/.test(before[i])) count++;
  const beforeToken = offset < before.length && !/\s/.test(before[offset]);
  let seen = 0;
  for (let j = 0; j < after.length; j++) {
    if (/\s/.test(after[j])) continue;
    if (beforeToken && seen === count) return j;
    seen++;
    if (!beforeToken && seen === count) return j + 1;
  }
  return count ? after.length : 0;
}
