// Unit tests for src/sqlSelection.ts: which SQL Explain and Run analysis send.
// No build or browser needed: the module is compiled with Vite's esbuild
// transform and imported from a data URL. Run: node test/sql-selection.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(
  new URL("../src/sqlSelection.ts", import.meta.url),
  "utf8",
);
const { code } = await transformWithEsbuild(source, "sqlSelection.ts", {
  loader: "ts",
  format: "esm",
  target: "es2020",
});
const {
  splitStatements,
  statementAt,
  runTarget,
  describeTarget,
  lineOf,
  lineCount,
  sqlRun,
  delimiterAt,
} = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);

const shared = JSON.parse(await fs.readFile(new URL("./fixtures/sql-split.json", import.meta.url), "utf8"));
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
const texts = (sql) => splitStatements(sql).map((s) => s.text);
// The cursor position of the "|" marker, and the text without it.
function at(marked) {
  const cursor = marked.indexOf("|");
  assert.ok(cursor >= 0, "test text needs a | cursor marker");
  return [marked.slice(0, cursor) + marked.slice(cursor + 1), cursor];
}
function cursorTarget(marked) {
  const [sql, cursor] = at(marked);
  return runTarget(sql, cursor, cursor);
}

test("one statement, with and without the trailing semicolon", () => {
  assert.deepEqual(texts("SELECT 1"), ["SELECT 1"]);
  assert.deepEqual(texts("  SELECT 1 ;  \n"), ["SELECT 1"]);
  assert.deepEqual(texts("SELECT 1;;"), ["SELECT 1"]);
  assert.deepEqual(texts(""), []);
  assert.deepEqual(texts(" \n\t "), []);
  assert.deepEqual(texts(";;"), []);
});

test("splits at semicolons outside quotes, identifiers, and comments", () => {
  assert.deepEqual(texts("SELECT 1; SELECT 2;\nSELECT 3"), [
    "SELECT 1",
    "SELECT 2",
    "SELECT 3",
  ]);
  assert.deepEqual(
    texts(
      "SELECT 'a;b', \"c;d\", `e;f` FROM t; SELECT 'it''s;', 'x\\';y', `g``;h`",
    ),
    [
      "SELECT 'a;b', \"c;d\", `e;f` FROM t",
      "SELECT 'it''s;', 'x\\';y', `g``;h`",
    ],
  );
  assert.deepEqual(
    texts("SELECT 1 /* a; b */ + 2; SELECT 3 -- c; d\n; SELECT 4 # e; f\n"),
    ["SELECT 1 /* a; b */ + 2", "SELECT 3 -- c; d", "SELECT 4 # e; f"],
  );
  assert.deepEqual(texts('SELECT "a""b;c"; SELECT 2'), [
    'SELECT "a""b;c"',
    "SELECT 2",
  ]);
});

test("-- needs whitespace after it, as in MySQL and the server validator", () => {
  assert.deepEqual(texts("SELECT 5 --1; SELECT 2"), ["SELECT 5 --1", "SELECT 2"]);
  assert.deepEqual(texts("SELECT 5 --\t; x\nSELECT 2"), ["SELECT 5 --\t; x\nSELECT 2"]);
  assert.deepEqual(texts("SELECT 5 --"), ["SELECT 5 --"]);
});

test("unterminated quotes and comments never split", () => {
  assert.deepEqual(texts("SELECT 'abc; SELECT 2"), ["SELECT 'abc; SELECT 2"]);
  assert.deepEqual(texts("SELECT `abc; SELECT 2"), ["SELECT `abc; SELECT 2"]);
  assert.deepEqual(texts("SELECT 1 /* open; SELECT 2"), [
    "SELECT 1 /* open; SELECT 2",
  ]);
  assert.deepEqual(texts("/* only; a comment"), []);
});

test("parameters stay in the statement text and do not affect splitting", () => {
  assert.deepEqual(
    texts("SELECT :a;SELECT * FROM t WHERE x = :b AND y = ':c;'"),
    ["SELECT :a", "SELECT * FROM t WHERE x = :b AND y = ':c;'"],
  );
});

test("comment-only parts are not statements; leading comments are kept", () => {
  assert.deepEqual(texts("-- just a note\n/* and another */"), []);
  assert.deepEqual(texts("SELECT 1; -- trailing note"), ["SELECT 1"]);
  assert.deepEqual(texts("SELECT 1;\n-- trailing line\n"), ["SELECT 1"]);
  assert.deepEqual(texts("-- top customers\nSELECT 1;\n\n# second\nSELECT 2"), [
    "-- top customers\nSELECT 1",
    "# second\nSELECT 2",
  ]);
  // A comment after the ; on the same line belongs to the statement before it.
  assert.deepEqual(texts("SELECT 1; -- first\nSELECT 2; /* second */\n"), [
    "SELECT 1",
    "SELECT 2",
  ]);
});

test("cursor ranges cover the whole text without gaps", () => {
  for (const sql of [
    "SELECT 1; SELECT 2",
    "-- head\n;\nSELECT 1;\n\n;-- x\nSELECT 2; -- t\n\n",
    "SELECT 1;\r\nSELECT 2;\r\n",
  ]) {
    const statements = splitStatements(sql);
    assert.equal(statements[0].from, 0);
    assert.equal(statements.at(-1).to, sql.length);
    for (let k = 1; k < statements.length; k++)
      assert.equal(statements[k].from, statements[k - 1].to);
    for (const s of statements) assert.equal(sql.slice(s.start, s.end), s.text);
  }
});

test("statement under the cursor", () => {
  const sql = "SELECT 1; -- one\n\nSELECT 2;\nSELECT 3";
  const statements = splitStatements(sql);
  const index = (marked) => {
    const [text, cursor] = at(marked);
    assert.equal(text, sql);
    return statementAt(statements, cursor);
  };
  assert.equal(index("|SELECT 1; -- one\n\nSELECT 2;\nSELECT 3"), 0);
  assert.equal(index("SELECT 1;| -- one\n\nSELECT 2;\nSELECT 3"), 0);
  assert.equal(index("SELECT 1; -- one|\n\nSELECT 2;\nSELECT 3"), 0);
  assert.equal(index("SELECT 1; -- one\n|\nSELECT 2;\nSELECT 3"), 1);
  assert.equal(index("SELECT 1; -- one\n\nSELECT 2;|\nSELECT 3"), 1);
  assert.equal(index("SELECT 1; -- one\n\nSELECT 2;\n|SELECT 3"), 2);
  assert.equal(index("SELECT 1; -- one\n\nSELECT 2;\nSELECT 3|"), 2);
  assert.equal(statementAt([], 0), -1);
  // Right after a ; that has more SQL on its line: the statement it ends.
  assert.equal(cursorTarget("SELECT 1;| SELECT 2").index, 1);
  assert.equal(cursorTarget("SELECT 1; |SELECT 2").index, 2);
  // Below the last statement: the last statement.
  assert.equal(cursorTarget("SELECT 1;\nSELECT 2;\n\n|").index, 2);
  // Above the first statement: the first statement.
  assert.equal(cursorTarget("|\n-- intro\n\nSELECT 1;\nSELECT 2;").index, 1);
});

test("runTarget: no selection, several statements runs the one at the cursor", () => {
  const target = cursorTarget("SELECT 1;\nSELECT * FROM t\n  WHERE id = :id|;\nSELECT 3;");
  assert.equal(target.scope, "statement");
  assert.equal(target.text, "SELECT * FROM t\n  WHERE id = :id");
  assert.equal(target.index, 2);
  assert.equal(target.total, 3);
  assert.equal(target.problem, "");
  const [sql] = at("SELECT 1;\nSELECT * FROM t\n  WHERE id = :id|;\nSELECT 3;");
  assert.equal(sql.slice(target.start, target.end), target.text);
  assert.equal(describeTarget(sql, target), "Statement 2 of 3 · lines 2–3");
  assert.equal(
    describeTarget("SELECT 1; SELECT 2", runTarget("SELECT 1; SELECT 2", 15, 15)),
    "Statement 2 of 2 · line 1",
  );
});

test("runTarget: one statement runs as a whole", () => {
  const target = cursorTarget("-- note\nSELECT 1|;\n-- end");
  assert.equal(target.scope, "editor");
  assert.equal(target.text, "-- note\nSELECT 1");
  assert.equal(target.total, 1);
  assert.equal(describeTarget("", target), "");
  // Nothing runnable: the editor text goes to the server unchanged.
  for (const sql of ["", "  ", "-- only a comment"]) {
    const empty = runTarget(sql, 0, 0);
    assert.equal(empty.scope, "editor");
    assert.equal(empty.text, sql);
    assert.equal(empty.total, 0);
  }
});

test("runTarget: a selection runs alone, trimmed and without its ;", () => {
  const sql = "SELECT 1;\nSELECT a, b\nFROM t WHERE x = :x;\nSELECT 3;";
  const start = sql.indexOf("SELECT a");
  const end = sql.indexOf(";", start);
  for (const [lo, hi] of [
    [start, end],
    [start, end + 1],
    [start - 1, end + 2],
    [end + 1, start - 1],
  ]) {
    const target = runTarget(sql, lo, hi);
    assert.equal(target.scope, "selection");
    assert.equal(target.text, "SELECT a, b\nFROM t WHERE x = :x");
    assert.equal(sql.slice(target.start, target.end), target.text);
    assert.equal(target.lines, 2);
    assert.equal(target.problem, "");
  }
  assert.equal(describeTarget(sql, runTarget(sql, start, end)), "Selection · 2 lines");
  // Part of a statement, even inside one line.
  const part = runTarget("SELECT * FROM t WHERE id = 3", 0, 15);
  assert.equal(part.text, "SELECT * FROM t");
  assert.equal(describeTarget("", part), "Selection · 1 line");
  // Leading comments stay; a comment after the final ; is dropped.
  assert.equal(
    runTarget("-- why\nSELECT 1; -- note", 0, 22).text,
    "-- why\nSELECT 1",
  );
  // A whitespace-only selection is no selection.
  const blank = "SELECT 1;\n\n\nSELECT 2;";
  const caret = blank.indexOf("\n\n") + 1;
  assert.equal(runTarget(blank, caret, caret + 1).scope, "statement");
});

test("runTarget: a selection with several statements or no SQL is a problem", () => {
  const sql = "SELECT 1; SELECT 2;";
  const several = runTarget(sql, 0, sql.length);
  assert.equal(several.scope, "selection");
  assert.equal(several.text, "");
  assert.equal(several.total, 2);
  assert.match(several.problem, /holds 2 statements/);
  const comments = runTarget("-- a\n/* b */\nSELECT 1", 0, 12);
  assert.equal(comments.text, "");
  assert.match(comments.problem, /only comments/);
});

test("DELIMITER lines change the delimiter and are not statements", () => {
  const sql = [
    "SELECT 1;",
    "DELIMITER //",
    "CREATE PROCEDURE p()",
    "BEGIN",
    "  SELECT 'a;b'; SELECT 2;",
    "END//",
    "delimiter ;",
    "SELECT 3;",
  ].join("\n");
  const statements = splitStatements(sql);
  assert.deepEqual(statements.map((s) => s.text), [
    "SELECT 1",
    "CREATE PROCEDURE p()\nBEGIN\n  SELECT 'a;b'; SELECT 2;\nEND",
    "SELECT 3",
  ]);
  assert.deepEqual(statements.map((s) => s.delimiter), [";", "//", ";"]);
  for (const s of statements) assert.equal(sql.slice(s.start, s.end), s.text);
  assert.equal(statements[0].from, 0);
  assert.equal(statements.at(-1).to, sql.length);
  for (let k = 1; k < statements.length; k++) assert.equal(statements[k].from, statements[k - 1].to);
  // The cursor inside the procedure body chooses the whole procedure.
  assert.equal(statementAt(statements, sql.indexOf("SELECT 2")), 1);
  // Quoted arguments, a missing argument (the old delimiter stays), and a
  // DELIMITER word after SQL on the same statement is SQL, not a command.
  assert.deepEqual(texts("DELIMITER '$$'\nSELECT 1$$ SELECT 2$$"), ["SELECT 1", "SELECT 2"]);
  assert.deepEqual(texts("DELIMITER\nSELECT 1; SELECT 2"), ["SELECT 1", "SELECT 2"]);
  assert.deepEqual(texts("SELECT 1\nDELIMITER //\n;"), ["SELECT 1\nDELIMITER //"]);
  // A comment before the command still lets it count.
  assert.deepEqual(texts("-- set up\n  delimiter ;;\nSELECT 1;;SELECT 2"), ["SELECT 1", "SELECT 2"]);
});

test("sqlRun: Run sends the selection or the caret, Run all everything", () => {
  const sql = "SELECT 1;\nUPDATE t SET a = 1 WHERE id = 2;\nSELECT 3";
  const all = sqlRun(sql, 3, 3, true);
  assert.deepEqual([all.scope, all.selection, all.cursor], ["all", undefined, undefined]);
  const at = sqlRun(sql, sql.indexOf("SET"), sql.indexOf("SET"));
  assert.equal(at.scope, "statement");
  assert.deepEqual([at.index, at.total, at.cursor, at.selection], [2, 3, sql.indexOf("SET"), undefined]);
  assert.equal(sql.slice(at.start, at.end), "UPDATE t SET a = 1 WHERE id = 2");
  // A selection may hold several statements: the server splits and runs them in order.
  const both = sqlRun(sql, 0, sql.indexOf("SELECT 3"));
  assert.equal(both.scope, "selection");
  assert.deepEqual(both.selection, { start: 0, end: sql.indexOf("SELECT 3") });
  assert.match(sqlRun("  \n-- only", 0, 0).problem, /no statement at the cursor/);
  assert.match(sqlRun(" ", 0, 0, true).problem, /Write SQL/);
  // The tint follows the session's reading of backslashes.
  const nb = "SELECT 'a\\'; INSERT INTO t VALUES (1); SELECT 'c';";
  assert.equal(sqlRun(nb, 2, 2, false, false).end, nb.indexOf(";"));
  const proc = "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END//\nDELIMITER ;\nCALL p();";
  assert.equal(delimiterAt(proc, proc.indexOf("BEGIN")), "//");
  const inside = sqlRun(proc, proc.indexOf("BEGIN"), proc.indexOf("BEGIN"));
  assert.equal(proc.slice(inside.start, inside.end), "CREATE PROCEDURE p() BEGIN SELECT 1; END");
});

test("shared cases with the server's splitter (test/fixtures/sql-split.json)", () => {
  const strip = (text) => {
    for (;;) {
      const m = /^(?:\s+|--(?=\s|$)[^\n]*|#[^\n]*|\/\*(?!M?!)[\s\S]*?\*\/)/.exec(text);
      if (!m || !m[0]) return text;
      text = text.slice(m[0].length);
    }
  };
  for (const c of shared.statements)
    assert.deepEqual(splitStatements(c.sql, c.backslash ?? true).map((s) => strip(s.text)), c.texts, c.sql);
  for (const c of shared.cursors) {
    const cursor = c.sql.indexOf("|");
    const sql = c.sql.replace("|", "");
    assert.equal(statementAt(splitStatements(sql, c.backslash ?? true), cursor), c.index, c.sql);
  }
});

test("line helpers", () => {
  assert.equal(lineOf("a\nb\r\nc\rd", 0), 1);
  assert.equal(lineOf("a\nb\r\nc\rd", 2), 2);
  assert.equal(lineOf("a\nb\r\nc\rd", 5), 3);
  assert.equal(lineOf("a\nb\r\nc\rd", 7), 4);
  assert.equal(lineCount("one"), 1);
  assert.equal(lineCount("one\r\ntwo\nthree"), 3);
});

console.log(`SQL selection: ${passed} tests passed.`);
