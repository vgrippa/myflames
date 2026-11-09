// Unit tests for Format SQL: src/sqlFormatCheck.ts (the token check that
// refuses any change beyond white space and keyword case) and src/sqlFormat.ts
// (statement by statement layout with sql-formatter). No build or browser:
// the modules are compiled with Vite's esbuild transform (sql-format-load.mjs).
// Run: node test/sql-format.mjs
import assert from "node:assert/strict";
import { check, format, cleanup } from "./sql-format-load.mjs";

const { tokenize, compareSql, caseSafe, upperKeywords, RESERVED, IGNORE_SPACE_FUNCTIONS } = check;
const { formatSql, formatEditorText, mapOffset, describeKept } = format;
let passed = 0;
function test(name, body) {
  try {
    body();
    passed++;
  } catch (error) {
    console.error("FAIL " + name);
    throw error;
  }
}
const kinds = (sql, backslash = true, brackets = false) => tokenize(sql, backslash, brackets).map((t) => `${t.kind}:${t.text}`);
/** The difference compareSql reports, as "before -> after", or null. */
const diff = (before, after, options) => {
  const d = compareSql(before, after, options);
  return d ? `${d.before} -> ${d.after}` : null;
};

try {
  // ---- The tokenizer -------------------------------------------------------
  test("strings: doubled quotes and backslash escapes stay inside", () => {
    assert.deepEqual(kinds(`'a''b' 'c\\'d' "e""f"`), ["string:'a''b'", "string:'c\\'d'", 'string:"e""f"']);
  });
  test("NO_BACKSLASH_ESCAPES: a backslash ends nothing", () => {
    assert.deepEqual(kinds(`'c\\' , 'd'`, false), ["string:'c\\'", "op:,", "string:'d'"]);
    assert.deepEqual(kinds(`'c\\' , 'd'`, true), ["string:'c\\' , '", "word:d", "unclosed:'"]);
  });
  test("quoted identifiers with doubled backticks, never escaped", () => {
    assert.deepEqual(kinds("`a``b` `c\\`"), ["ident:`a``b`", "ident:`c\\`"]);
  });
  test("comments: #, -- with white space, block, executable, hints", () => {
    assert.deepEqual(kinds("a # x\n-- y\n--z /* c */ /*!80000 d */ /*M!100100 e */ /*+ BKA(t) */"), [
      "word:a", "line:# x", "line:-- y", "op:-", "op:-", "word:z", "comment:/* c */",
      "exec:/*!80000 d */", "exec:/*M!100100 e */", "exec:/*+ BKA(t) */",
    ]);
    assert.deepEqual(kinds("--\tx\n--"), ["line:--\tx", "line:--"]);
    assert.deepEqual(kinds("/* open"), ["unclosed:/* open"]);
  });
  test("MariaDB sql_mode=MSSQL: a name in square brackets is one token", () => {
    // Review finding: MariaDB 10.4.5+ (MDEV-19142) reads [..] as a quoted
    // name, ]] is a ] inside it, and a backslash escapes nothing there.
    assert.deepEqual(kinds("select [a,b], [x]]y], [c\\] from t", true, true), [
      "word:select", "ident:[a,b]", "op:,", "ident:[x]]y]", "op:,", "ident:[c\\]", "word:from", "word:t",
    ]);
    assert.deepEqual(kinds("select [a", true, true), ["word:select", "unclosed:[a"]);
    assert.deepEqual(kinds("select '[a' /* [b */ from t", true, true), ["word:select", "string:'[a'", "comment:/* [b */", "word:from", "word:t"]);
    // In every other reading a bracket is a character (a syntax error).
    assert.deepEqual(kinds("select [a]"), ["word:select", "op:[", "word:a", "op:]"]);
  });
  test("prefixed literals are one token; an introducer is a word", () => {
    assert.deepEqual(kinds("X'41' x'41' B'1' N'é' _utf8mb4'a' 0x41 0b1"), [
      "string:X'41'", "string:x'41'", "string:B'1'", "string:N'é'", "word:_utf8mb4", "string:'a'", "number:0x41", "number:0b1",
    ]);
  });
  test("variables, parameters and multi-character operators", () => {
    assert.deepEqual(kinds("@a := @@global.max_connections <=> @'x y' ->> :id ? 'u'@'h' a->'$' a || b"), [
      "variable:@a", "op::=", "variable:@@global.max_connections", "op:<=>", "variable:@'x y'", "op:->>", "param::id",
      "op:?", "string:'u'", "variable:@'h'", "word:a", "op:->", "string:'$'", "word:a", "op:||", "word:b",
    ]);
  });
  test("numbers, and names that start with digits", () => {
    assert.deepEqual(kinds("1 1.5 1. .5 1e-3 1.e3 1abc t.5"), [
      "number:1", "number:1.5", "number:1.", "number:.5", "number:1e-3", "number:1.e3", "word:1abc", "word:t", "op:.", "number:5",
    ]);
  });
  test("the word lists come from both servers", () => {
    for (const w of ["SELECT", "FROM", "WHERE", "KEY", "INT", "VARCHAR", "IS", "NULL", "LIMIT", "ORDER"]) assert.ok(RESERVED.has(w), w);
    // Names a table can have: reserved in one server only, or in neither.
    for (const w of ["STATUS", "USER", "DATE", "TEXT", "END", "DATABASE", "FUNCTION", "WINDOW", "RANK", "RETURNING", "COUNT"])
      assert.ok(!RESERVED.has(w), w);
    for (const w of ["COUNT", "SUM", "NOW", "SUBSTRING", "GROUP_CONCAT", "TRIM"]) assert.ok(IGNORE_SPACE_FUNCTIONS.has(w), w);
    assert.ok(!IGNORE_SPACE_FUNCTIONS.has("VALUES") && !IGNORE_SPACE_FUNCTIONS.has("IF"));
  });

  // ---- What compareSql accepts ----------------------------------------------
  test("white space anywhere outside tokens", () => {
    assert.equal(diff("select a,b from t where x=1", "select\n  a,\n  b\nfrom\n  t\nwhere\n  x = 1"), null);
    assert.equal(diff("insert into t(a) values(1)", "insert into t (a) values (1)"), null, "only built-in functions care about the space");
  });
  test("keyword case where the server ignores it", () => {
    assert.equal(diff("select a from t where b is not null order by a desc", "SELECT a FROM t WHERE b IS NOT NULL ORDER BY a DESC"), null);
    assert.equal(diff("show full processlist; start transaction; kill query 5", "SHOW FULL PROCESSLIST; START TRANSACTION; KILL QUERY 5"), null);
    assert.equal(diff("show global status like 'x'", "SHOW GLOBAL STATUS LIKE 'x'"), null);
  });
  test("plain comment white space, line comment trailing space", () => {
    assert.equal(diff("select 1 /* a\n     b */", "select 1 /* a\n  b */"), null);
    assert.equal(diff("select 1 -- c   \nfrom t", "select 1 -- c\nfrom t"), null);
  });
  test("a minus before a negative number is not a comment", () => {
    assert.equal(diff("a - -1", "a --1"), null);
    assert.equal(diff("a - - 1", "a -- 1"), "- -> -- 1");
  });

  // ---- What compareSql refuses ----------------------------------------------
  test("strings, quoted identifiers and literals byte for byte", () => {
    assert.equal(diff("select 'a  b'", "select 'a b'"), "'a  b' -> 'a b'");
    assert.equal(diff("select 'a''b'", "select 'a' 'b'"), "'a''b' -> 'a'");
    assert.equal(diff("select `My  Col`", "select `My Col`"), "`My  Col` -> `My Col`");
    assert.equal(diff("select X'41'", "select X '41'"), "X'41' -> X");
    assert.equal(diff("select 1.5", "select 1. 5"), "1.5 -> 1.");
    assert.equal(diff("select @a", "select @ a"), "@a -> @");
    assert.equal(diff("where id = :id", "where id = : id"), ":id -> :");
    assert.equal(diff("select a <=> b", "select a <= > b"), "<=> -> <=");
  });
  test("case of names: tables, aliases, qualified names, non-ASCII words", () => {
    assert.equal(diff("select a from status", "select a from STATUS"), "status -> STATUS");
    assert.equal(diff("select t.order from t", "select t.ORDER from t"), "order -> ORDER");
    assert.equal(diff("select a from db . select", "select a from db . SELECT"), "select -> SELECT");
    assert.equal(diff("select séance from t", "select SÉANCE from t"), "séance -> SÉANCE");
    assert.equal(diff("show create table status", "SHOW CREATE TABLE STATUS"), "status -> STATUS");
    assert.equal(diff("describe format", "DESCRIBE FORMAT"), "format -> FORMAT");
    assert.equal(diff("lock tables status read", "LOCK TABLES STATUS READ"), "status -> STATUS");
    assert.equal(diff("use select_db", "USE SELECT_DB"), "select_db -> SELECT_DB");
  });
  test("an executable comment may hide what makes a word a name", () => {
    // Review finding: the comment's body is code (CREATE VIEW, a dot).
    assert.equal(diff("/*!50000 create view */ status as select 1", "/*!50000 create view */ STATUS as select 1"), "status -> STATUS");
    assert.equal(diff("select * from db /*!50000 . */ order", "select * from db /*!50000 . */ ORDER"), "order -> ORDER");
    assert.equal(diff("select /*!50000 1 */ from t", "select /*!50000 1 */ FROM t"), null, "a reserved word beside one, without a dot, is still a keyword");
    assert.equal(diff("select * from db /*!50000 .*/ order", "select * from db /*!50000 .*/ ORDER"), "order -> ORDER");
    assert.equal(diff("select a /*!50000 .b */ from t", "SELECT a /*!50000 .b */ FROM t"), null);
    assert.equal(diff("select /*+ BKA(t) */ distinct a from t", "SELECT /*+ BKA(t) */ DISTINCT a FROM t"), null, "a hint is not code of the statement");
    assert.equal(diff("select 1 /* plain */ from t", "select 1 /* plain */ FROM t"), null, "a plain comment is white space");
  });
  test("a selection inside a statement has no statement start", () => {
    assert.equal(diff("status from t", "STATUS from t", { startsStatement: false }), "status -> STATUS");
    assert.equal(diff("status from t; select 1", "status FROM t; SELECT 1", { startsStatement: false }), null);
    assert.equal(diff("select 1", "SELECT 1", { allowCase: false }), "select -> SELECT");
  });
  test("a line comment that would swallow code", () => {
    assert.equal(diff("select a -- c\nfrom t", "select a -- c from t"), "-- c -> -- c from t");
    assert.equal(diff("select a # c\nfrom t", "select a # c from t"), "# c -> # c from t");
  });
  test("executable comments and hints byte for byte", () => {
    assert.equal(diff("select /*!80000 force  index (a) */ 1", "select /*!80000 force index (a) */ 1"),
      "/*!80000 force  index (a) */ -> /*!80000 force index (a) */");
    assert.equal(diff("select /*+ BKA(t)  */ 1", "select /*+ BKA(t) */ 1"), "/*+ BKA(t)  */ -> /*+ BKA(t) */");
    assert.equal(diff("select /*M!100100 1 */", "select /*M!100100  1 */"), "/*M!100100 1 */ -> /*M!100100  1 */");
  });
  test("the space between a built-in function and its parenthesis", () => {
    assert.equal(diff("select count (*) from t", "select count(*) from t"), "count (*) -> count(*)");
    assert.equal(diff("select sum(a) from t", "select sum (a) from t"), "sum(a) -> sum (a)");
    assert.equal(diff("select myfunc (a) from t", "select myfunc(a) from t"), null, "a stored function is not affected");
  });
  test("a string the session ends at a backslash (NO_BACKSLASH_ESCAPES)", () => {
    // With backslash escapes the change is inside one string; without them it
    // is inside the second string, so it is refused.
    assert.equal(diff("select 'a\\' , 'b  c'", "select 'a\\' , 'b c'"), "'b  c' -> 'b c'");
  });
  test("a name in square brackets byte for byte (MariaDB sql_mode=MSSQL)", () => {
    assert.equal(diff("select [a,b] from t", "select [a,\nb] from t"), "[a,b] -> [a, b]");
    assert.equal(diff("select [select] from t", "SELECT [SELECT] FROM t"), "[select] -> [SELECT]");
    assert.equal(diff("select [a b] from t", "select\n  [a b]\nfrom\n  t"), null, "white space around it is fine");
  });
  test("added or removed tokens", () => {
    assert.equal(diff("select 1", "select 1;"), " -> ;");
    assert.equal(diff("select 1, 2", "select 1 2"), ", -> 2");
  });
  test("DELIMITER lines are compared like any text", () => {
    const sql = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//\nDELIMITER ;";
    assert.equal(diff(sql, sql), null);
    assert.equal(diff(sql, sql.replace("END//", "END //")), null, "white space only");
    assert.equal(diff(sql, sql.replace("DELIMITER //", "DELIMITER / /")), null, "tokens alone cannot tell; formatSql never touches DELIMITER lines");
  });

  // ---- caseSafe and upperKeywords -----------------------------------------
  test("caseSafe marks keywords, never names", () => {
    const tokens = tokenize("select t.select, status from status where a = 1");
    const safe = caseSafe(tokens).map((ok, k) => (ok ? tokens[k].text : null)).filter(Boolean);
    assert.deepEqual(safe, ["select", "from", "where"]);
  });
  test("upperKeywords leaves names, kept ranges and strings alone", () => {
    const before = "select a, `from` from status where b = 'select' and c is null";
    assert.equal(upperKeywords(before, before), "SELECT a, `from` FROM status WHERE b = 'select' AND c IS NULL");
    assert.equal(upperKeywords(before, before, { keep: [[0, 6]] }), "select a, `from` FROM status WHERE b = 'select' AND c IS NULL");
    assert.equal(upperKeywords("status from t", "status from t", { startsStatement: false }), "status FROM t");
    assert.equal(upperKeywords("select [select] from t", "select [select] from t"), "SELECT [select] FROM t", "a bracketed name keeps its case");
  });

  // ---- formatSql -------------------------------------------------------------
  test("formats one statement: upper case keywords, two-space indent", () => {
    const r = formatSql("select a, b from t where x = 1 and y in (1,2) order by a", { dialect: "mysql" });
    assert.equal(r.status, "formatted");
    assert.equal(r.text, "SELECT\n  a,\n  b\nFROM\n  t\nWHERE\n  x = 1\n  AND y IN (1, 2)\nORDER BY\n  a");
    assert.equal(r.formatted, 1);
  });
  test("several statements: one blank line between, the final white space kept", () => {
    const r = formatSql("select 1;select 2;\n", { dialect: "mysql" });
    assert.equal(r.text, "SELECT\n  1;\n\nSELECT\n  2;\n");
  });
  test("names keep their case; MariaDB dialect", () => {
    const r = formatSql("select Status, `Name` from Users u join status s on s.id = u.id", { dialect: "mariadb" });
    assert.equal(r.text, "SELECT\n  Status,\n  `Name`\nFROM\n  Users u\n  JOIN status s ON s.id = u.id");
  });
  test("a DELIMITER block and a stored program are left as written", () => {
    const sql = "DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n  select 1;\nEND//\nDELIMITER ;\nselect 2;";
    const r = formatSql(sql, { dialect: "mysql" });
    assert.equal(r.status, "formatted");
    assert.equal(r.text, "DELIMITER //\nCREATE PROCEDURE p()\nBEGIN\n  select 1;\nEND//\nDELIMITER ;\nSELECT\n  2;");
    assert.deepEqual(r.kept, [{ line: 2, reason: "inside a DELIMITER block" }]);
    const program = formatSql("create definer = root@localhost function f() returns int return 1;\nselect 1", { dialect: "mysql" });
    assert.deepEqual(program.kept, [{ line: 1, reason: "a stored program body" }]);
    assert.ok(program.text.startsWith("create definer = root@localhost function f() returns int return 1;\n\nSELECT"));
    const block = formatSql("begin not atomic select 1; end", { dialect: "mariadb" });
    assert.equal(block.kept[0].reason, "a stored program body");
  });
  test("executable comments and hints stay byte for byte", () => {
    const r = formatSql("select /*+ MAX_EXECUTION_TIME(1000) */ * from t /*!80000 force index (primary) */ where a=1", { dialect: "mysql" });
    assert.equal(r.text, "SELECT\n  /*+ MAX_EXECUTION_TIME(1000) */\n  *\nFROM\n  t /*!80000 force index (primary) */\nWHERE\n  a = 1");
    // A multi-line executable comment would be indented by the formatter: kept.
    const multi = formatSql("select 1 /*!50001\n , 2 */ from t", { dialect: "mysql" });
    assert.equal(multi.status, "unchanged");
    assert.match(multi.message, /executable comment or optimizer hint/);
  });
  test("refuses when formatting would change meaning, naming the place", () => {
    const r = formatSql("select 1;\nselect count (*) from t", { dialect: "mysql" });
    assert.equal(r.status, "refused");
    assert.equal(r.line, 2);
    assert.match(r.message, /^Format SQL changed nothing: formatting would have changed “count \(\*\)” to “count\(\*\)” on line 2/);
    const later = formatSql("select count (*) from t", { dialect: "mysql", firstLine: 7 });
    assert.equal(later.line, 7, "lines count from where the selection starts");
  });
  test("executable comments: the words around them keep their case", () => {
    const view = formatSql("/*!50000 create view */ status as select 1", { dialect: "mysql" });
    assert.ok(view.text.includes("*/ status"), view.text);
    const dotted = formatSql("select * from db /*!50000 . */ order", { dialect: "mysql" });
    assert.match(dotted.text, /\*\/ order$/);
  });
  test("formatEditorText: a selection inside a string or a word is refused", () => {
    // Review finding: the fragment alone looked like SQL.
    const quoted = "select 'a,b from t'";
    const inside = formatEditorText(quoted, quoted.indexOf("a,b"), quoted.length - 1, { dialect: "mysql" });
    assert.equal(inside.status, "refused");
    assert.equal(inside.message, "Format SQL changed nothing: the selection starts inside a quoted string on line 1. Select whole statements or clauses, or nothing to format the whole tab.");
    const word = "select myselect from t";
    const part = formatEditorText(word, word.indexOf("select from"), word.indexOf(" from"), { dialect: "mysql" });
    assert.equal(part.status, "refused");
    assert.match(part.message, /starts inside a word on line 1/);
    const comment = "select 1 /* a comment */ from t";
    assert.match(formatEditorText(comment, 0, comment.indexOf("comment */"), { dialect: "mysql" }).message, /ends inside a comment/);
  });
  test("square brackets: a selection inside them is refused, the statement kept", () => {
    // Review finding: selecting `a,b` in `[a,b]` and formatting gave `[a,\nb]`.
    const sql = "select [a,b] from t";
    const inside = formatEditorText(sql, 8, 11, { dialect: "mariadb" });
    assert.equal(inside.status, "refused");
    assert.equal(inside.message, "Format SQL changed nothing: the selection starts inside a quoted name on line 1. Select whole statements or clauses, or nothing to format the whole tab.");
    assert.equal(formatEditorText(sql, 7, 10, { dialect: "mysql" }).status, "refused", "in any dialect");
    const whole = formatSql("select [Order Id] from t;\nselect a from t", { dialect: "mariadb" });
    assert.deepEqual(whole.kept, [{ line: 1, reason: "a name in square brackets" }]);
    assert.equal(whole.text, "select [Order Id] from t;\n\nSELECT\n  a\nFROM\n  t");
    // Brackets split by a ; (the client ends the statement there) are kept too.
    const split = formatEditorText("select [a;b] from t", 0, 0, { dialect: "mariadb" });
    assert.notEqual(split.status, "formatted", split.text);
  });
  test("formatEditorText: whole text, a selection, and the check on the whole text", () => {
    const sql = "select 1;\nselect a,b from t where c=1;\nselect 3";
    const all = formatEditorText(sql, 5, 5, { dialect: "mysql" });
    assert.deepEqual([all.status, all.start, all.end], ["formatted", 0, sql.length]);
    const from = sql.indexOf("select a");
    const to = sql.indexOf(";", from);
    const one = formatEditorText(sql, to, from, { dialect: "mysql" });
    assert.deepEqual([one.status, one.start, one.end, one.text], ["formatted", from, to, "SELECT\n  a,\n  b\nFROM\n  t\nWHERE\n  c = 1"]);
    // Inside a DELIMITER block a selection is left alone.
    const block = "DELIMITER //\nselect 1//\nDELIMITER ;";
    assert.equal(formatEditorText(block, block.indexOf("select"), block.indexOf("//\nDELIMITER ;"), { dialect: "mysql" }).status, "unchanged");
    // A selection starting mid-statement: its first word keeps its case.
    const tail = "select status from t";
    const rest = formatEditorText(tail, tail.indexOf("status"), tail.length, { dialect: "mysql" });
    assert.ok(rest.text.startsWith("status"), rest.text);
  });
  test("a selection inside a statement keeps its first word's case", () => {
    const r = formatSql("status, name from t", { dialect: "mysql", startsStatement: false });
    assert.equal(r.status, "formatted");
    assert.ok(r.text.startsWith("status,"), r.text);
    assert.match(r.text, /FROM/);
  });
  test("inside a DELIMITER block a selection is left alone", () => {
    const r = formatSql("select 1", { dialect: "mysql", delimiter: "//" });
    assert.equal(r.status, "unchanged");
    assert.match(r.message, /DELIMITER block/);
  });
  test("a line comment before the delimiter keeps the ; on its own line", () => {
    const r = formatSql("select a -- c\n;", { dialect: "mysql" });
    assert.equal(r.text, "SELECT\n  a -- c\n;");
  });
  test("client commands, unclosed quotes and comments are left as written", () => {
    assert.match(formatSql("select 'open from t", { dialect: "mysql" }).message, /unclosed quote or comment/);
    const g = formatSql("select a from t\\G\nselect b from u;", { dialect: "mysql" });
    assert.equal(g.text, "SELECT\n  a\nFROM\n  t\\G\nSELECT\n  b\nFROM\n  u;");
  });
  test("comments between statements stay where they are", () => {
    const r = formatSql("-- header\nselect 1; -- note\nselect 2;", { dialect: "mysql" });
    assert.equal(r.text, "-- header\nSELECT\n  1; -- note\nSELECT\n  2;");
  });
  test("parameters, placeholders and variables survive", () => {
    const r = formatSql("insert into t (a, b) values (:a, ?); set @x := 1", { dialect: "mysql" });
    assert.equal(r.text, "INSERT INTO\n  t (a, b)\nVALUES\n  (:a, ?);\n\nSET\n  @x := 1");
  });
  test("nothing to format, already formatted", () => {
    assert.match(formatSql("  -- only a comment\n", { dialect: "mysql" }).message, /^Nothing to format/);
    assert.equal(formatSql("SELECT\n  1;", { dialect: "mysql" }).message, "Already formatted: nothing changed.");
  });
  test("the result always passes the token check", () => {
    const samples = [
      "select a.b, c->>'$.d', e <=> f, g || h from t1 left join t2 using (k) where m regexp '^x' limit 5 offset 2",
      "update t set a = a - -1, b = default where id in (select id from u) order by id limit 3",
      "with recursive c(n) as (select 1 union all select n + 1 from c where n < 10) select * from c",
      "create table t (id bigint unsigned not null auto_increment primary key, j json, key k (j)) engine=innodb",
      "select _utf8mb4'abc' collate utf8mb4_bin, X'41', N'x', 1.5e3, 0x1f from dual",
      "select a from t where b = 'it''s' and c = \"q\\\"q\" -- trailing\n",
      "show global status like 'Innodb%'; show full processlist; start transaction read only; commit",
    ];
    for (const sql of samples) {
      const r = formatSql(sql, { dialect: "mysql" });
      assert.notEqual(r.status, "refused", `${sql}: ${r.message}`);
      if (r.status === "formatted") assert.equal(compareSql(sql, r.text), null, sql);
    }
  });
  test("describeKept and mapOffset", () => {
    assert.equal(describeKept([{ line: 4, reason: "a stored program body" }]), "left line 4 as written (a stored program body)");
    assert.equal(describeKept([{ line: 1, reason: "a" }, { line: 3, reason: "a" }]), "left 2 statements as written (lines 1 and 3: a)");
    const before = "select a from t";
    const after = "SELECT\n  a\nFROM\n  t";
    assert.equal(after.slice(0, mapOffset(before, after, before.indexOf("from"))), "SELECT\n  a\n", "before a word: before it");
    assert.equal(after.slice(0, mapOffset(before, after, before.indexOf(" from"))), "SELECT\n  a", "after a word: after it");
    assert.equal(mapOffset(before, after, 0), 0);
    assert.equal(mapOffset(before, after, before.length), after.length);
  });

  console.log(`Format SQL: ${passed} tests passed (token check and formatter).`);
} finally {
  await cleanup();
}
