// The meaning check behind Format SQL (sqlFormat.ts). Pure functions, no DOM,
// no dependency: the browser suite and test/sql-format.mjs import it.
//
// Formatting may change only white space and the letter case of words whose
// case the server ignores in that place. `compareSql` tokenizes the text
// before and after the way the MySQL and MariaDB lexers split it, under every
// reading a session may use (`READINGS`: with and without
// NO_BACKSLASH_ESCAPES, and with MariaDB's sql_mode=MSSQL names in square
// brackets, so text a session reads differently is still compared), and
// returns the first token that differs:
//
// - strings, quoted identifiers (backticks, and `[name]` under MSSQL),
//   numbers, variables, operators, executable comments (`/*!`, `/*M!`) and
//   optimizer hints (`/*+`) must be identical;
// - plain block comments may differ only in white space, line comments only in
//   trailing white space;
// - a word may change case only where `caseSafe` allows it (a word reserved in
//   both MySQL and MariaDB that is not part of a qualified name, a statement's
//   first word, or a keyword of a known statement opening such as SHOW FULL
//   PROCESSLIST); a table or alias name can be case sensitive on the server,
//   so every other word keeps its spelling;
// - the white space between a built-in function that IGNORE_SPACE governs
//   (COUNT, SUM, NOW, ...) and its parenthesis must stay: without IGNORE_SPACE
//   `COUNT (*)` and `COUNT(*)` are read differently.
//
// The word lists were computed from the server sources, not the manuals:
// MySQL sql/lex.h symbols whose token is not declared `%token<lexer.keyword>`
// in sql/sql_yacc.yy (how sql/gen_keyword_list.cc decides "reserved"), and
// MariaDB sql/lex.h symbols[] whose token appears in no keyword_* rule of
// sql/sql_yacc.yy; RESERVED is their intersection. IGNORE_SPACE_FUNCTIONS is
// the union of MySQL's SYM_FN entries and MariaDB's sql_functions[].

export type TokenKind =
  | "word"
  | "number"
  | "string"
  | "ident"
  | "variable"
  | "param"
  | "comment"
  | "exec"
  | "line"
  | "op"
  | "unclosed";
export type SqlToken = { kind: TokenKind; text: string; start: number; end: number };

/** Words reserved in both MySQL and MariaDB: never an unquoted name. */
export const RESERVED = new Set(
  (
    "ADD ALL ALTER ANALYZE AND AS ASC ASENSITIVE BEFORE BETWEEN BIGINT BINARY BLOB BOTH BY CALL CASCADE CASE CHANGE " +
    "CHAR CHARACTER CHECK COLLATE COLUMN CONDITION CONSTRAINT CONTINUE CONVERT CREATE CROSS CURRENT_DATE " +
    "CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATABASES DAY_HOUR DAY_MICROSECOND DAY_MINUTE DAY_SECOND " +
    "DEC DECIMAL DECLARE DEFAULT DELAYED DELETE DESC DESCRIBE DETERMINISTIC DISTINCT DISTINCTROW DIV DOUBLE DROP " +
    "DUAL EACH ELSE ELSEIF ENCLOSED ESCAPED EXCEPT EXISTS EXIT EXPLAIN FALSE FETCH FLOAT FLOAT4 FLOAT8 FOR FORCE " +
    "FOREIGN FROM FULLTEXT GRANT GROUP HAVING HIGH_PRIORITY HOUR_MICROSECOND HOUR_MINUTE HOUR_SECOND IF IGNORE IN " +
    "INDEX INFILE INNER INOUT INSENSITIVE INSERT INT INT1 INT2 INT3 INT4 INT8 INTEGER INTERSECT INTERVAL INTO IS " +
    "ITERATE JOIN KEY KEYS KILL LEADING LEAVE LEFT LIKE LIMIT LINEAR LINES LOAD LOCALTIME LOCALTIMESTAMP LOCK LONG " +
    "LONGBLOB LONGTEXT LOOP LOW_PRIORITY MATCH MAXVALUE MEDIUMBLOB MEDIUMINT MEDIUMTEXT MIDDLEINT " +
    "MINUTE_MICROSECOND MINUTE_SECOND MOD MODIFIES NATURAL NOT NO_WRITE_TO_BINLOG NULL NUMERIC ON OPTIMIZE " +
    "OPTIONALLY OR ORDER OUT OUTER OUTFILE OVER PARTITION PRECISION PRIMARY PROCEDURE PURGE RANGE READ READS " +
    "READ_WRITE REAL RECURSIVE REFERENCES REGEXP RELEASE RENAME REPEAT REPLACE REQUIRE RESIGNAL RESTRICT RETURN " +
    "REVOKE RIGHT RLIKE ROWS ROW_NUMBER SCHEMAS SECOND_MICROSECOND SELECT SENSITIVE SEPARATOR SET SHOW SIGNAL " +
    "SMALLINT SPATIAL SPECIFIC SQL SQLEXCEPTION SQLSTATE SQLWARNING SQL_BIG_RESULT SQL_CALC_FOUND_ROWS " +
    "SQL_SMALL_RESULT SSL STARTING STRAIGHT_JOIN TABLE TERMINATED THEN TINYBLOB TINYINT TINYTEXT TO TRAILING " +
    "TRIGGER TRUE UNDO UNION UNIQUE UNLOCK UNSIGNED UPDATE USAGE USE USING UTC_DATE UTC_TIME UTC_TIMESTAMP VALUES " +
    "VARBINARY VARCHAR VARCHARACTER VARYING WHEN WHERE WHILE WITH WRITE XOR YEAR_MONTH ZEROFILL"
  ).split(" "),
);

/** Built-in functions read as functions only when `(` follows at once
 * (unless sql_mode has IGNORE_SPACE). */
export const IGNORE_SPACE_FUNCTIONS = new Set(
  (
    "ADDDATE BIT_AND BIT_OR BIT_XOR CAST COUNT CUME_DIST CURDATE CURTIME DATE_ADD DATE_SUB DENSE_RANK EXTRACT " +
    "FIRST_VALUE GROUP_CONCAT JSON_ARRAYAGG JSON_DUALITY_OBJECT JSON_OBJECTAGG LAG LEAD MAX MEDIAN MID MIN NOW " +
    "NTH_VALUE NTILE PERCENTILE_CONT PERCENTILE_DISC PERCENT_RANK POSITION RANK SESSION_USER STD STDDEV STDDEV_POP " +
    "STDDEV_SAMP ST_COLLECT SUBDATE SUBSTR SUBSTRING SUM SYSDATE SYSTEM_USER TRIM TRIM_ORACLE VARIANCE VAR_POP " +
    "VAR_SAMP"
  ).split(" "),
);

/** Keywords that may follow a statement's first word (the word after it is
 * never a name there): SHOW STATUS, START TRANSACTION, KILL QUERY, ... */
const LEAD: Record<string, string[]> = {
  SHOW: ["VARIABLES", "STATUS", "PROCESSLIST", "TABLES", "COLUMNS", "FIELDS", "INDEXES", "ENGINES", "ENGINE",
    "GRANTS", "PRIVILEGES", "PLUGINS", "WARNINGS", "ERRORS", "EVENTS", "TRIGGERS", "REPLICA", "REPLICAS", "SLAVE",
    "MASTER", "BINARY", "OPEN", "PROFILE", "PROFILES", "CHARSET", "COLLATION", "FULL", "GLOBAL", "SESSION",
    "STORAGE", "FUNCTION", "TABLE"],
  START: ["TRANSACTION", "REPLICA", "SLAVE", "GROUP_REPLICATION"],
  STOP: ["REPLICA", "SLAVE", "GROUP_REPLICATION"],
  BEGIN: ["WORK"],
  COMMIT: ["WORK"],
  ROLLBACK: ["WORK"],
  RELEASE: ["SAVEPOINT"],
  KILL: ["QUERY", "CONNECTION"],
  LOCK: ["TABLES", "INSTANCE"],
  UNLOCK: ["TABLES", "INSTANCE"],
  FLUSH: ["PRIVILEGES", "LOGS", "STATUS", "TABLES", "HOSTS", "BINARY", "ENGINE", "ERROR", "GENERAL", "RELAY",
    "SLOW", "OPTIMIZER_COSTS", "USER_RESOURCES", "LOCAL"],
  SET: ["NAMES", "SESSION", "GLOBAL", "PERSIST", "PERSIST_ONLY", "LOCAL", "TRANSACTION", "PASSWORD", "ROLE"],
  RESET: ["MASTER", "REPLICA", "SLAVE", "PERSIST", "QUERY"],
  PURGE: ["BINARY", "MASTER"],
  XA: ["START", "BEGIN", "END", "PREPARE", "COMMIT", "ROLLBACK", "RECOVER"],
  DEALLOCATE: ["PREPARE"],
  INSTALL: ["PLUGIN", "COMPONENT"],
  UNINSTALL: ["PLUGIN", "COMPONENT"],
  LOAD: ["DATA", "XML"],
  CREATE: ["TEMPORARY", "VIEW", "USER", "ROLE", "EVENT", "SERVER", "TABLESPACE", "DATABASE", "SCHEMA", "FUNCTION",
    "AGGREGATE", "DEFINER", "ALGORITHM", "SEQUENCE"],
  ALTER: ["VIEW", "USER", "EVENT", "SERVER", "TABLESPACE", "DATABASE", "SCHEMA", "FUNCTION", "INSTANCE",
    "DEFINER", "ALGORITHM", "ONLINE", "SEQUENCE"],
  DROP: ["TEMPORARY", "VIEW", "USER", "ROLE", "EVENT", "SERVER", "TABLESPACE", "DATABASE", "SCHEMA", "FUNCTION",
    "SEQUENCE"],
};
/** Third keywords after two of them: SHOW GLOBAL STATUS, SHOW FULL PROCESSLIST. */
const LEAD_THIRD: Record<string, string[]> = {
  "SHOW GLOBAL": ["STATUS", "VARIABLES"],
  "SHOW SESSION": ["STATUS", "VARIABLES"],
  "SHOW FULL": ["PROCESSLIST", "TABLES", "COLUMNS", "FIELDS"],
  "SHOW REPLICA": ["STATUS", "HOSTS"],
  "SHOW SLAVE": ["STATUS", "HOSTS"],
  "SHOW MASTER": ["STATUS", "LOGS"],
  "SHOW BINARY": ["LOGS", "LOG"],
  "SHOW TABLE": ["STATUS"],
  "SHOW FUNCTION": ["STATUS", "CODE"],
  "SHOW PROCEDURE": ["STATUS", "CODE"],
  "SHOW OPEN": ["TABLES"],
  "SHOW STORAGE": ["ENGINES"],
  "FLUSH LOCAL": ["PRIVILEGES", "LOGS", "STATUS", "TABLES", "HOSTS"],
  "FLUSH NO_WRITE_TO_BINLOG": ["PRIVILEGES", "LOGS", "STATUS", "TABLES", "HOSTS"],
  "PURGE BINARY": ["LOGS"],
  "PURGE MASTER": ["LOGS"],
  "ROLLBACK TO": ["SAVEPOINT"],
  "LOAD DATA": ["LOCAL"],
};

const isSpace = (c: string | undefined) =>
  c === " " || c === "\t" || c === "\n" || c === "\r" || c === "\f" || c === "\v";
const WORD = /[A-Za-z0-9_$\u0080-￿]/;
const NUMBER = /^(?:0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)/;
const OPERATORS = ["<=>", "->>", "<<", ">>", "<=", ">=", "<>", "!=", ":=", "||", "&&", "->", "=>"];

/** How a session may read the text: backslash escapes in strings (off under
 * NO_BACKSLASH_ESCAPES), and `[name]` as a quoted name (MariaDB 10.4.5+ under
 * sql_mode=MSSQL, MDEV-19142; a syntax error everywhere else). A change must
 * read the same under each. */
export const READINGS: { backslash: boolean; brackets: boolean }[] = [
  { backslash: true, brackets: false },
  { backslash: false, brackets: false },
  { backslash: true, brackets: true },
  { backslash: false, brackets: true },
];

/** The end of the quoted text that starts at `i` (a doubled quote stays
 * inside; a backslash escapes the next character in strings when *backslash*). */
function quotedEnd(sql: string, i: number, backslash: boolean): { end: number; closed: boolean } {
  const q = sql[i];
  let j = i + 1;
  while (j < sql.length) {
    const c = sql[j];
    if (c === "\\" && q !== "`" && backslash) j += 2;
    else if (c !== q) j++;
    else if (sql[j + 1] === q) j += 2;
    else return { end: j + 1, closed: true };
  }
  return { end: sql.length, closed: false };
}

/** Split *sql* into tokens the way the server's lexer does (white space
 * dropped); *brackets* reads `[name]` as a quoted name (see READINGS). */
export function tokenize(sql: string, backslash = true, brackets = false): SqlToken[] {
  const out: SqlToken[] = [];
  const n = sql.length;
  const push = (kind: TokenKind, start: number, end: number) => out.push({ kind, text: sql.slice(start, end), start, end });
  let i = 0;
  while (i < n) {
    const c = sql[i];
    if (isSpace(c)) {
      i++;
      continue;
    }
    const start = i;
    // `#` and `-- ` (the dashes followed by white space or a control character).
    if (c === "#" || (c === "-" && sql[i + 1] === "-" && (i + 2 >= n || sql.charCodeAt(i + 2) <= 32))) {
      let j = i;
      while (j < n && sql[j] !== "\n") j++;
      push("line", start, j);
      i = j;
      continue;
    }
    if (c === "/" && sql[i + 1] === "*") {
      const close = sql.indexOf("*/", i + 2);
      const end = close < 0 ? n : close + 2;
      push(close < 0 ? "unclosed" : /^\/\*(?:M?!|\+)/.test(sql.slice(i, i + 4)) ? "exec" : "comment", start, end);
      i = end;
      continue;
    }
    // X'..', B'..' and N'..' are one token: a space after the letter changes them.
    if ((c === "x" || c === "X" || c === "b" || c === "B" || c === "n" || c === "N") && sql[i + 1] === "'") {
      const q = quotedEnd(sql, i + 1, backslash);
      push(q.closed ? "string" : "unclosed", start, q.end);
      i = q.end;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      const q = quotedEnd(sql, i, backslash);
      push(q.closed ? (c === "`" ? "ident" : "string") : "unclosed", start, q.end);
      i = q.end;
      continue;
    }
    // MariaDB's scan_ident_delimited with ']': `]]` stays inside, no escapes.
    if (brackets && c === "[") {
      let j = i + 1;
      let closed = false;
      while (j < n) {
        if (sql[j] !== "]") j++;
        else if (sql[j + 1] === "]") j += 2;
        else {
          j++;
          closed = true;
          break;
        }
      }
      push(closed ? "ident" : "unclosed", start, j);
      i = j;
      continue;
    }
    // @var, @@global.var, @'quoted', and the @host of 'user'@'host'.
    if (c === "@") {
      let j = i + 1;
      if (sql[j] === "@") j++;
      if (sql[j] === "'" || sql[j] === '"' || sql[j] === "`") {
        const q = quotedEnd(sql, j, backslash);
        push(q.closed ? "variable" : "unclosed", start, q.end);
        i = q.end;
        continue;
      }
      while (j < n && (WORD.test(sql[j]) || sql[j] === ".")) j++;
      push("variable", start, j);
      i = j;
      continue;
    }
    // :name parameters (myflames binds them); := is an operator.
    if (c === ":" && WORD.test(sql[i + 1] || "") && sql[i + 1] !== "=") {
      let j = i + 1;
      while (j < n && WORD.test(sql[j])) j++;
      push("param", start, j);
      i = j;
      continue;
    }
    const last = out[out.length - 1];
    const afterOperand = !!last && last.end === i && (last.kind === "word" || last.kind === "ident" || last.kind === "number");
    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(sql[i + 1] || "") && !afterOperand)) {
      const m = NUMBER.exec(sql.slice(i, i + 400));
      let j = i + (m ? m[0].length : 1);
      // Digits followed by letters are a name (1abc), as the server reads them.
      let word = false;
      while (j < n && WORD.test(sql[j])) {
        j++;
        word = true;
      }
      push(word ? "word" : "number", start, j);
      i = j;
      continue;
    }
    if (WORD.test(c)) {
      let j = i + 1;
      while (j < n && WORD.test(sql[j])) j++;
      push("word", start, j);
      i = j;
      continue;
    }
    const op = OPERATORS.find((o) => sql.startsWith(o, i));
    const end = i + (op ? op.length : 1);
    push("op", start, end);
    i = end;
  }
  return out;
}

// Plain comments and optimizer hints (`/*+ ... */`, read by their own
// grammar) are white space to the statement around them. An executable
// comment (`/*!...*/`, `/*M!...*/`) is code: it ends a statement's opening,
// and a dot at its edge makes the word beside it part of a qualified name.
const isHint = (t: SqlToken) => t.kind === "exec" && t.text.startsWith("/*+");
const isComment = (t: SqlToken) => t.kind === "comment" || t.kind === "line" || isHint(t);
const execBody = (t: SqlToken) => t.text.replace(/^\/\*M?!\d*/, "").replace(/\*\/$/, "").trim();

/**
 * For each token, whether changing its letter case keeps the meaning. With
 * *startsStatement* false the text starts inside a statement, so its first
 * word is not a statement's first word.
 */
export function caseSafe(tokens: SqlToken[], startsStatement = true): boolean[] {
  const safe = tokens.map(() => false);
  const code = tokens.map((t, k) => (isComment(t) ? -1 : k)).filter((k) => k >= 0);
  // The statement's opening words so far, or null once the opening ended.
  let lead: string[] | null = startsStatement ? [] : null;
  for (let c = 0; c < code.length; c++) {
    const k = code[c];
    const t = tokens[k];
    if (t.kind === "op" && t.text === ";") {
      lead = [];
      continue;
    }
    // \g and \G end a statement, as in the mysql client.
    if (t.kind === "word" && (t.text === "g" || t.text === "G") && c > 0 && tokens[code[c - 1]].text === "\\" && tokens[code[c - 1]].end === t.start) {
      lead = [];
      continue;
    }
    if (t.kind !== "word") {
      lead = null;
      continue;
    }
    const prev = c > 0 ? tokens[code[c - 1]] : undefined;
    const next = c + 1 < code.length ? tokens[code[c + 1]] : undefined;
    // Part of a qualified name (the dot may sit inside an executable comment
    // beside it), or not a plain ASCII word (keywords are).
    const qualified = prev?.text === "." || next?.text === "."
      || (prev?.kind === "exec" && execBody(prev).endsWith(".")) || (next?.kind === "exec" && execBody(next).startsWith("."))
      || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(t.text);
    const upper = t.text.toUpperCase();
    let ok = false;
    let opening = false;
    if (!qualified && lead) {
      if (lead.length === 0) opening = true;
      else if (lead.length === 1) opening = LEAD[lead[0]]?.includes(upper) ?? false;
      else if (lead.length === 2) opening = LEAD_THIRD[`${lead[0]} ${lead[1]}`]?.includes(upper) ?? false;
    }
    ok = !qualified && (opening || RESERVED.has(upper));
    safe[k] = ok;
    if (lead) lead = ok && lead.length < 3 ? [...lead, upper] : null;
  }
  return safe;
}

/**
 * *draft* (*before* with only its white space changed) with every word that
 * `caseSafe` allows in upper case, except inside the *keep* ranges (offsets
 * in *draft*). A word is changed only when every reading (READINGS) sees it
 * as the same safe word, so a string the session ends elsewhere, or a name in
 * square brackets, is never touched.
 */
export function upperKeywords(
  before: string,
  draft: string,
  options: { startsStatement?: boolean; keep?: [number, number][] } = {},
): string {
  // Safe words (start offset in draft -> end) under each reading.
  const modes: Map<number, number>[] = [];
  for (const { backslash, brackets } of READINGS) {
    const a = tokenize(before, backslash, brackets);
    const b = tokenize(draft, backslash, brackets);
    if (a.length !== b.length) return draft;
    const safe = caseSafe(a, options.startsStatement ?? true);
    const here = new Map<number, number>();
    b.forEach((t, k) => {
      if (safe[k] && t.kind === "word" && a[k].text.toUpperCase() === t.text.toUpperCase()) here.set(t.start, t.end);
    });
    modes.push(here);
  }
  const chosen = [...modes[0]].filter(([start, end]) => modes.every((m) => m.get(start) === end)).sort((x, y) => x[0] - y[0]);
  const keep = options.keep || [];
  let out = "";
  let at = 0;
  for (const [start, end] of chosen) {
    if (keep.some(([from, to]) => start < to && end > from)) continue;
    out += draft.slice(at, start) + draft.slice(start, end).toUpperCase();
    at = end;
  }
  return out + draft.slice(at);
}

/** Where formatting changed more than white space and keyword case. */
export type FormatDifference = {
  /** Offsets of the differing text in the input. */
  start: number;
  end: number;
  /** 1-based line of `start` in the input. */
  line: number;
  /** The text before and after formatting, cut to one short line. */
  before: string;
  after: string;
};

const collapse = (s: string) => s.replace(/\s+/g, " ").trim();
function sameToken(x: SqlToken, y: SqlToken, caseOk: boolean) {
  if (x.kind !== y.kind) return false;
  if (x.text === y.text) return true;
  if (x.kind === "word") return caseOk && x.text.toUpperCase() === y.text.toUpperCase();
  if (x.kind === "comment") return collapse(x.text) === collapse(y.text);
  if (x.kind === "line") return x.text.trimEnd() === y.text.trimEnd();
  return false;
}
function lineAt(sql: string, offset: number) {
  let line = 1;
  for (let i = 0; i < offset && i < sql.length; i++) if (sql[i] === "\n") line++;
  return line;
}
function short(text: string) {
  const one = text.replace(/\s*\n\s*/g, " ");
  return one.length > 40 ? `${one.slice(0, 39)}…` : one;
}

/**
 * The first place where *after* reads differently from *before*, or null
 * when they differ only in white space and keyword case (see the header).
 * *allowCase* false requires the same letter case everywhere.
 */
export function compareSql(
  before: string,
  after: string,
  options: { startsStatement?: boolean; allowCase?: boolean } = {},
): FormatDifference | null {
  for (const { backslash, brackets } of READINGS) {
    const a = tokenize(before, backslash, brackets);
    const b = tokenize(after, backslash, brackets);
    const safe = options.allowCase === false ? null : caseSafe(a, options.startsStatement ?? true);
    const count = Math.max(a.length, b.length);
    for (let k = 0; k < count; k++) {
      const x = a[k];
      const y = b[k];
      if (!x || !y || !sameToken(x, y, !!safe?.[k])) {
        const from = x || a[a.length - 1];
        const at = from ? from.start : before.length;
        return {
          start: at,
          end: x ? x.end : before.length,
          line: lineAt(before, at),
          before: short(x ? x.text : ""),
          after: short(y ? y.text : ""),
        };
      }
      // COUNT (*) and COUNT(*) are read differently without IGNORE_SPACE.
      const p = a[k - 1];
      const q = b[k - 1];
      if (x.kind === "op" && x.text === "(" && p && q && p.kind === "word" && IGNORE_SPACE_FUNCTIONS.has(p.text.toUpperCase())
        && (p.end === x.start) !== (q.end === y.start)) {
        // Shown with its arguments when they are short: “COUNT (*)” to “COUNT(*)”.
        const call = (text: string, from: number, open: number) => {
          const close = text.indexOf(")", open);
          return short(text.slice(from, close >= 0 && close - open < 24 ? close + 1 : open));
        };
        return {
          start: p.start,
          end: x.end,
          line: lineAt(before, p.start),
          before: call(before, p.start, x.end),
          after: call(after, q.start, y.end),
        };
      }
    }
  }
  return null;
}
