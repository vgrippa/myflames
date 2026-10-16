// Syntax colors and bracket matching for the Query editor.
//
// Pure functions, no DOM. The tokenizer reads text the way the mysql client
// and sqlSelection.ts do: `/* */` comments (optimizer hints and `/*! */`
// executable comments included), `-- ` (dashes, then white space or the end)
// and `#` comments, '...' and "..." strings with doubled quotes and backslash
// escapes, backtick identifiers with doubled backticks. A token that is not
// closed runs to the end of the text, as the server would read it. Colors
// are a reading aid only; what runs is decided by sqlSelection.ts and the
// server.

export type TokenKind =
  | "keyword"
  | "string"
  | "ident"
  | "comment"
  | "number"
  | "variable"
  | "param"
  | "bracket"
  | "punct";
export type Token = { kind: TokenKind; start: number; end: number; closed?: boolean };

/** Above this many characters the editor shows plain text: coloring costs a
 * render of every token on each keystroke. */
export const HIGHLIGHT_LIMIT = 60_000;

const WORDS =
  "ACCESSIBLE ACTION ADD AFTER AGAINST AGGREGATE ALGORITHM ALL ALTER ALWAYS ANALYZE AND ANY AS ASC ASCII AUTO_INCREMENT " +
  "AVG_ROW_LENGTH BEFORE BEGIN BETWEEN BIGINT BINARY BINLOG BIT BLOB BOOL BOOLEAN BOTH BTREE BY CACHE CALL CASCADE " +
  "CASCADED CASE CHANGE CHAR CHARACTER CHARSET CHECK CHECKSUM CLOSE COALESCE COLLATE COLLATION COLUMN COLUMNS COMMENT " +
  "COMMIT COMMITTED COMPACT COMPRESSED CONDITION CONSISTENT CONSTRAINT CONTINUE CONVERT COPY COUNT CREATE CROSS CUBE " +
  "CURRENT CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP CURRENT_USER CURSOR DATA DATABASE DATABASES DATE DATETIME DAY " +
  "DEALLOCATE DEC DECIMAL DECLARE DEFAULT DEFINER DELAYED DELETE DENSE_RANK DESC DESCRIBE DETERMINISTIC DISABLE DISCARD " +
  "DISTINCT DISTINCTROW DIV DO DOUBLE DROP DUAL DUPLICATE DYNAMIC EACH ELSE ELSEIF ENABLE ENCLOSED END ENGINE ENGINES " +
  "ENUM ESCAPE ESCAPED EVENT EVENTS EVERY EXCEPT EXCHANGE EXCLUSIVE EXECUTE EXISTS EXIT EXPLAIN EXTENDED FALSE FETCH " +
  "FIELDS FIRST FIXED FLOAT FLUSH FOLLOWING FOR FORCE FOREIGN FORMAT FOUND FROM FULL FULLTEXT FUNCTION GENERATED GET " +
  "GLOBAL GRANT GRANTS GROUP GROUPING HANDLER HASH HAVING HELP HIGH_PRIORITY HOUR IDENTIFIED IF IGNORE IMMEDIATE IN " +
  "INDEX INDEXES INFILE INNER INOUT INPLACE INSERT INSTANT INT INTEGER INTERSECT INTERVAL INTO INVISIBLE INVOKER IS " +
  "ISOLATION ITERATE JOIN JSON JSON_TABLE KEY KEYS KILL LAG LAST LATERAL LEAD LEADING LEAVE LEFT LEVEL LIKE LIMIT " +
  "LINEAR LINES LOAD LOCAL LOCK LOCKED LOGS LONG LONGBLOB LONGTEXT LOOP LOW_PRIORITY MASTER MATCH MAX MAXVALUE " +
  "MEDIUMBLOB MEDIUMINT MEDIUMTEXT MERGE MIN MINUTE MOD MODE MODIFY MONTH NAMES NATIONAL NATURAL NO NONE NOT NOWAIT " +
  "NO_WRITE_TO_BINLOG NULL NUMERIC OF OFF OFFSET ON ONLY OPEN OPTIMIZE OPTION OPTIONALLY OR ORDER OTHERS OUT OUTER " +
  "OUTFILE OVER PARTITION PARTITIONS PASSWORD PERSIST PERSIST_ONLY PRECEDING PRECISION PREPARE PRIMARY PRIVILEGES " +
  "PROCEDURE PROCESS PROCESSLIST PURGE QUERY QUICK RANGE RANK READ READS REAL RECURSIVE REDUNDANT REFERENCES REGEXP " +
  "RELEASE RELOAD RENAME REORGANIZE REPAIR REPEAT REPEATABLE REPLACE REPLICA REPLICAS REPLICATION REQUIRE RESET RESIGNAL " +
  "RESTRICT RETURN RETURNING RETURNS REVOKE RIGHT RLIKE ROLE ROLLBACK ROLLUP ROUTINE ROW ROWS ROW_FORMAT ROW_NUMBER " +
  "SAVEPOINT SCHEMA SCHEMAS SECOND SECURITY SELECT SEPARATOR SEQUENCE SERIALIZABLE SESSION SET SHARE SHARED SHOW " +
  "SHUTDOWN SIGNAL SIGNED SIMPLE SKIP SLAVE SMALLINT SNAPSHOT SOME SPATIAL SQL SQL_BIG_RESULT SQL_BUFFER_RESULT " +
  "SQL_CALC_FOUND_ROWS SQL_NO_CACHE SQL_SMALL_RESULT SQLEXCEPTION SQLSTATE SQLWARNING START STARTING STATEMENT STATUS " +
  "STOP STORAGE STORED STRAIGHT_JOIN SUM SYSTEM TABLE TABLES TABLESPACE TEMPORARY TEMPTABLE TERMINATED TEXT THEN TIES " +
  "TIME TIMESTAMP TINYBLOB TINYINT TINYTEXT TO TRAILING TRANSACTION TRIGGER TRIGGERS TRUE TRUNCATE TYPE UNBOUNDED " +
  "UNCOMMITTED UNDEFINED UNION UNIQUE UNKNOWN UNLOCK UNSIGNED UNTIL UPDATE UPGRADE USAGE USE USER USING UTC_DATE " +
  "UTC_TIME UTC_TIMESTAMP VALUE VALUES VARBINARY VARCHAR VARIABLES VARYING VIEW VIRTUAL VISIBLE WAIT WARNINGS WEEK WHEN " +
  "WHERE WHILE WINDOW WITH WITHOUT WORK WRITE XA XOR YEAR ZEROFILL";
/** Reserved and common words drawn as keywords (upper-cased before the lookup). */
export const KEYWORDS: ReadonlySet<string> = new Set(WORDS.split(" "));

const isSpace = (c: string) => c === " " || c === "\t" || c === "\n" || c === "\r" || c === "\f" || c === "\v";
const isDigit = (c: string | undefined) => !!c && c >= "0" && c <= "9";
/** Letters, digits, `_`, `$` and anything beyond ASCII, as in unquoted MySQL identifiers. */
const isWord = (c: string | undefined) =>
  !!c && ((c >= "a" && c <= "z") || (c >= "A" && c <= "Z") || isDigit(c) || c === "_" || c === "$" || c > "\u007f");

/** The tokens of `sql` in order; white space and plain words are left out (they are drawn as text). */
export function tokenize(sql: string): Token[] {
  const out: Token[] = [];
  const n = sql.length;
  let i = 0;
  while (i < n) {
    const c = sql[i];
    if (isSpace(c)) {
      i++;
      continue;
    }
    if (c === "/" && sql[i + 1] === "*") {
      const close = sql.indexOf("*/", i + 2);
      const end = close < 0 ? n : close + 2;
      out.push({ kind: "comment", start: i, end, closed: close >= 0 });
      i = end;
      continue;
    }
    if (c === "#" || (c === "-" && sql[i + 1] === "-" && (i + 2 >= n || isSpace(sql[i + 2])))) {
      let j = i;
      while (j < n && sql[j] !== "\n" && sql[j] !== "\r") j++;
      out.push({ kind: "comment", start: i, end: j, closed: true });
      i = j;
      continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      let j = i + 1;
      let closed = false;
      while (j < n) {
        if (sql[j] === "\\" && c !== "`") j += 2;
        else if (sql[j] !== c) j++;
        else if (sql[j + 1] === c) j += 2;
        else {
          j++;
          closed = true;
          break;
        }
      }
      out.push({ kind: c === "`" ? "ident" : "string", start: i, end: Math.min(j, n), closed });
      i = Math.min(j, n);
      continue;
    }
    if (c === "@") {
      let j = i + 1;
      if (sql[j] === "@") j++;
      while (isWord(sql[j]) || sql[j] === ".") j++;
      out.push({ kind: "variable", start: i, end: j });
      i = j;
      continue;
    }
    if (c === ":" && isWord(sql[i + 1]) && !isDigit(sql[i + 1]) && sql[i - 1] !== ":") {
      let j = i + 1;
      while (isWord(sql[j])) j++;
      out.push({ kind: "param", start: i, end: j });
      i = j;
      continue;
    }
    if (isDigit(c) || (c === "." && isDigit(sql[i + 1]) && !isWord(sql[i - 1]))) {
      let j = i;
      if (c === "0" && (sql[i + 1] === "x" || sql[i + 1] === "b")) {
        j = i + 2;
        while (isWord(sql[j])) j++;
      } else {
        while (isDigit(sql[j]) || sql[j] === ".") j++;
        if ((sql[j] === "e" || sql[j] === "E") && (isDigit(sql[j + 1]) || ((sql[j + 1] === "+" || sql[j + 1] === "-") && isDigit(sql[j + 2])))) {
          j += 2;
          while (isDigit(sql[j])) j++;
        }
      }
      // 1abc is an identifier in MySQL, not a number.
      if (isWord(sql[j]) && sql[j] !== "$") {
        while (isWord(sql[j])) j++;
        i = j;
        continue;
      }
      out.push({ kind: "number", start: i, end: j });
      i = j;
      continue;
    }
    if (isWord(c)) {
      let j = i + 1;
      while (isWord(sql[j])) j++;
      // A word right after a dot is a name (t.status), never a keyword.
      const qualified = sql[i - 1] === ".";
      if (!qualified && KEYWORDS.has(sql.slice(i, j).toUpperCase())) out.push({ kind: "keyword", start: i, end: j });
      i = j;
      continue;
    }
    if (c === "(" || c === ")" || c === "[" || c === "]" || c === "{" || c === "}") {
      out.push({ kind: "bracket", start: i, end: i + 1 });
      i++;
      continue;
    }
    if (c === ";" || c === "," || c === ".") {
      out.push({ kind: "punct", start: i, end: i + 1 });
      i++;
      continue;
    }
    i++;
  }
  return out;
}

const OPEN: Record<string, string> = { "(": ")", "[": "]", "{": "}" };
const CLOSE: Record<string, string> = { ")": "(", "]": "[", "}": "{" };

/** What the caret touches: a bracket and its partner, or the two quotes of a
 * string or quoted identifier. `partner` is -1 for an unmatched bracket. */
export type Match = { kind: "bracket" | "quote"; at: number; partner: number };

/**
 * The bracket or quote pair next to the caret: the character just before it
 * is checked first, then the one after it (as in most code editors).
 */
export function matchAt(sql: string, tokens: Token[], caret: number): Match | null {
  for (const at of [caret - 1, caret]) {
    if (at < 0 || at >= sql.length) continue;
    const token = tokens.find((t) => t.start <= at && at < t.end);
    if (!token) continue;
    if ((token.kind === "string" || token.kind === "ident") && token.closed && token.end - token.start >= 2) {
      if (at === token.start) return { kind: "quote", at, partner: token.end - 1 };
      if (at === token.end - 1) return { kind: "quote", at, partner: token.start };
      continue;
    }
    if (token.kind !== "bracket") continue;
    const c = sql[at];
    const brackets = tokens.filter((t) => t.kind === "bracket");
    const k = brackets.indexOf(token);
    let depth = 0;
    if (OPEN[c]) {
      for (let j = k; j < brackets.length; j++) {
        const b = sql[brackets[j].start];
        if (OPEN[b]) depth++;
        else if (CLOSE[b]) depth--;
        if (depth === 0) return { kind: "bracket", at, partner: CLOSE[b] === c ? brackets[j].start : -1 };
      }
    } else {
      for (let j = k; j >= 0; j--) {
        const b = sql[brackets[j].start];
        if (CLOSE[b]) depth++;
        else if (OPEN[b]) depth--;
        if (depth === 0) return { kind: "bracket", at, partner: OPEN[b] === c ? brackets[j].start : -1 };
      }
    }
    return { kind: "bracket", at, partner: -1 };
  }
  return null;
}

/** A name for SQL: as is when it is a plain identifier that is not a keyword, else in backticks (backticks doubled). */
export function quoteName(name: string) {
  return /^[A-Za-z_][A-Za-z0-9_$]*$/.test(name) && !KEYWORDS.has(name.toUpperCase())
    ? name
    : "`" + name.replace(/`/g, "``") + "`";
}
