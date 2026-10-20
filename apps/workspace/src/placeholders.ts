// Turn a Performance Schema digest row into SQL the Query editor can capture.
//
// Pure functions, no DOM: test/placeholders.mjs runs them in Node. The
// Python side (myflames/discover.py: parameterize, digest_query) follows the
// same rules, and both run the shared cases in
// test/fixtures/digest-placeholders.json.
//
// Normalized digest text replaces every literal with `?` and collapses value
// lists: `(...)` (MariaDB and MySQL, e.g. `IN (...)`) and `?, ...` (MySQL
// sql/gen_lex_token.cc:351-369, MariaDB sql/gen_lex_token.cc:190-212).

/** Quoted text and comments, copied unchanged. */
const SKIP =
  /(\/\*[\s\S]*?(?:\*\/|$)|--(?=\s|$)[^\r\n]*|#[^\r\n]*|'(?:''|\\[\s\S]|[^'\\])*'?|"(?:""|\\[\s\S]|[^"\\])*"?|`(?:``|[^`])*`?)/
    .source;
/** Skipped spans, then collapsed lists, then a lone `?`. */
const TOKEN = new RegExp(
  `${SKIP}|(\\(\\s*\\.\\.\\.\\s*\\)|\\?\\s*,\\s*\\.\\.\\.)|(\\?)`,
  "g",
);
/**
 * Names the lexer reads as a function only when "(" follows with no space
 * (SYM_FN in MySQL sql/lex.h:847-882; sql_functions[] in MariaDB
 * sql/lex.h). Digest text prints "COUNT ( * )", which is a syntax error.
 */
const FUNCTION_ONLY = [
  "ADDDATE", "BIT_AND", "BIT_OR", "BIT_XOR", "CAST", "COUNT", "CUME_DIST",
  "CURDATE", "CURTIME", "DATE_ADD", "DATE_SUB", "DENSE_RANK", "EXTRACT",
  "FIRST_VALUE", "GROUP_CONCAT", "JSON_ARRAYAGG", "JSON_DUALITY_OBJECT",
  "JSON_OBJECTAGG", "LAG", "LEAD", "MAX", "MEDIAN", "MID", "MIN", "NOW",
  "NTH_VALUE", "NTILE", "PERCENT_RANK", "PERCENTILE_CONT", "PERCENTILE_DISC",
  "POSITION", "RANK", "SESSION_USER", "STD", "STDDEV", "STDDEV_POP",
  "STDDEV_SAMP", "ST_COLLECT", "SUBDATE", "SUBSTR", "SUBSTRING", "SUM",
  "SYSDATE", "SYSTEM_USER", "TRIM", "TRIM_ORACLE", "VARIANCE", "VAR_POP",
  "VAR_SAMP",
];
const FUNCTION_SPACE = new RegExp(
  `${SKIP}|(?<![\\w.$@\`])(${FUNCTION_ONLY.join("|")})\\s+\\(`,
  "gi",
);

/**
 * Drop the space digest text puts between a built-in function name and its
 * "(" (`COUNT ( * )` becomes `COUNT( * )`), outside quotes and comments.
 */
export function joinFunctionCalls(sql: string) {
  return (sql || "").replace(
    FUNCTION_SPACE,
    (whole, skip?: string, name?: string) =>
      skip !== undefined ? whole : `${name}(`,
  );
}

export type Parameterized = {
  /** The SQL with `:p1`, `:p2`, … in place of each placeholder. */
  sql: string;
  /** Parameter names in order of appearance. */
  names: string[];
  /** The names that stand for a collapsed list of values. */
  lists: string[];
};

/** Replace each `?` outside quotes, backticks and comments with `:pN`. */
export function parameterize(text: string): Parameterized {
  const names: string[] = [];
  const lists: string[] = [];
  const sql = (text || "").replace(
    TOKEN,
    (whole, skip?: string, list?: string) => {
      if (skip !== undefined) return whole;
      const name = `p${names.length + 1}`;
      names.push(name);
      if (list === undefined) return `:${name}`;
      lists.push(name);
      return list.startsWith("(") ? `(:${name})` : `:${name}`;
    },
  );
  return { sql, names, lists };
}

/** `{"p1": null, …}`: the parameters JSON a new tab starts with. */
export function skeleton(names: string[]) {
  if (!names.length) return "{}";
  return `{\n${names.map((n) => `  ${JSON.stringify(n)}: null`).join(",\n")}\n}`;
}

/** The fields of a `top-1.0` statement that decide what runs. */
export type DigestRow = {
  analysis_sql?: string | null;
  digest: string | null;
  digest_text: string | null;
  schema: string | null;
  sample_text: string | null;
  sample_truncated: boolean;
};

/**
 * Why a statement needs values before it can run, or null when it runs as
 * recorded. `normalized`: the server keeps no sample statement (MariaDB);
 * `truncated`: the MySQL sample was cut off; `placeholders`: the sample has
 * `?` markers (a prepared statement); `missing`: no text at all.
 */
export type FillReason = "normalized" | "truncated" | "placeholders" | "missing";

export type DigestQuery = Parameterized & {
  /** The recorded text the SQL was built from. */
  text: string;
  fromSample: boolean;
  reason: FillReason | null;
};

/**
 * The sample text unless missing or cut off; otherwise the digest text with
 * built-in function calls rejoined.
 */
export function digestQuery(row: DigestRow): DigestQuery {
  const recorded = row.sample_text || "";
  const sample = recorded && !row.sample_truncated ? recorded : "";
  const text = sample || row.digest_text || "";
  const p = parameterize(row.analysis_sql || text);
  if (!sample) p.sql = joinFunctionCalls(p.sql);
  let reason: FillReason | null = null;
  if (!text.trim()) reason = "missing";
  else if (p.names.length)
    reason = sample ? "placeholders" : recorded ? "truncated" : "normalized";
  return { ...p, sql: p.sql.trim(), text, fromSample: !!sample, reason };
}

/** The first 12 hex characters of a digest. */
export function shortDigest(digest: string | null | undefined) {
  return (digest || "").slice(0, 12);
}

const IDENT = "(`(?:``|[^`])+`|[A-Za-z0-9_$]+)";
const FROM = new RegExp(`\\bFROM\\s+${IDENT}(?:\\s*\\.\\s*${IDENT})?`, "i");

/** The first table after FROM, without quotes, or "". */
export function firstTable(text: string) {
  const masked = (text || "").replace(TOKEN, (whole, skip?: string) =>
    skip !== undefined && !skip.startsWith("`") ? " " : whole,
  );
  const match = FROM.exec(masked);
  if (!match) return "";
  const name = match[2] || match[1];
  return name.startsWith("`") ? name.slice(1, -1).replace(/``/g, "`") : name;
}

/** Tab and plan name for a ranked digest row: "Top #3 · orders". */
export function digestName(rank: number, row: DigestRow) {
  const table = firstTable(row.sample_text || row.digest_text || "");
  const what = table || row.schema || "";
  return what ? `Top #${rank} · ${what}` : `Top #${rank}`;
}

/** One sentence that tells the user which values to fill in. */
export function fillNotice(q: Pick<DigestQuery, "names" | "lists" | "reason">) {
  if (!q.names.length) return "";
  const why =
    q.reason === "truncated"
      ? "The sample statement was cut off, so this is the normalized text."
      : q.reason === "placeholders"
        ? "The sample statement has ? markers."
        : "This server keeps only the normalized text.";
  const range =
    q.names.length === 1
      ? q.names[0]
      : q.names.length === 2
        ? `${q.names[0]} and ${q.names[1]}`
        : `${q.names[0]} to ${q.names[q.names.length - 1]}`;
  const lists = q.lists.length
    ? ` ${q.lists.join(", ")} ${q.lists.length === 1 ? "replaces" : "replace"} a collapsed list of values (such as IN (...)); bind one value or write the list into the SQL.`
    : "";
  return `${why} Fill in ${range} under Parameters with real values, then run Explain.${lists}`;
}
