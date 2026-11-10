// No DOM: compare the CodeMirror tree with the existing tokenizer byte for byte.
// Run from apps/workspace: node test/sql-tree-parity.mjs
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const result = await build({
  stdin: { contents: `
    export { EditorState } from "@codemirror/state";
    export { syntaxTree, highlightingFor, matchBrackets } from "@codemirror/language";
    export { getStyleTags } from "@lezer/highlight";
    export * from "./src/editor/sqlLanguage";
    export { tokenize, HIGHLIGHT_LIMIT } from "./src/sqlHighlight";
  `, resolveDir: fileURLToPath(new URL("..", import.meta.url)) },
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
});
const cm = await import("data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64"));
const makeState = sql => cm.EditorState.create({ doc: sql, extensions: [cm.sqlLanguage, cm.sqlHighlighting] });
let checked = 0;
function parity(sql, label) {
  const state = makeState(sql);
  assert.deepEqual(cm.treeTokens(state), sql.length > cm.HIGHLIGHT_LIMIT ? [] : cm.tokenize(sql), label);
  assert.equal(cm.syntaxTree(state).length, sql.length, `${label}: complete tree`);
  checked++;
  return state;
}

const fixtures = JSON.parse(await readFile(new URL("./fixtures/sql-split.json", import.meta.url), "utf8"));
let fixtureCount = 0;
function walk(value) {
  if (Array.isArray(value)) return value.forEach(walk);
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (key === "sql" && typeof child === "string") {
      parity(child, `fixture ${++fixtureCount}`);
      if (child.includes("|")) parity(child.replaceAll("|", ""), `fixture ${fixtureCount}: caret removed`);
    } else if (key === "texts" && Array.isArray(child)) {
      child.forEach((sql, i) => parity(sql, `expected statement ${fixtureCount}/${i}`));
    } else walk(child);
  }
}
walk(fixtures);
assert.ok(fixtureCount > 0);

let seed = 0x5e1ec7;
function random(n) {
  seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return seed % n;
}
const alphabet = ["'", '"', "`", "\\", "--", "#", "/*", "*/", "/*!", "\n", "\r", "\t", "@", ":", "0", "1", "9", "a", "Z", "SELECT", " ", "(", ")", "[", "]", "{", "}", ".", ";", ",", "é", "🐬"];
for (let n = 0; n < 2000; n++) {
  let sql = "";
  const length = random(400);
  for (let i = 0; i < length; i++) sql += alphabet[random(alphabet.length)];
  parity(sql, `seeded case ${n}`);
}
const longLine = "SELECT 'x', 12, `y`, @z; ".repeat(2500).slice(0, 50000);
assert.equal(longLine.length, 50000);
assert.ok(!longLine.includes("\n"));
parity(longLine, "single 50000-character line");
parity("SELECT 1; ".repeat(6000), "exactly 60000 characters");
parity("'" + "a".repeat(60000), "60001 characters");
parity("", "empty document");

// Large documents must not even be read into a string by the parser.
const empty = cm.sqlLanguage.parser.parse({ length: 60001, lineChunks: false,
  read() { assert.fail("oversized input was read"); }, chunk() { assert.fail("oversized input was read"); } });
assert.equal(empty.length, 60001);
assert.equal(empty.topNode.firstChild, null);

// Every leaf receives the existing CSS class, including unclosed token types.
const colors = parity("SELECT 'x', 12, `name`, @v, :p, (1); /* comment */ 'unfinished", "highlight classes");
cm.syntaxTree(colors).iterate({ enter(node) {
  const token = node.type.prop(cm.tokenInfo);
  if (token) assert.equal(cm.highlightingFor(colors, cm.getStyleTags(node).tags), `tok-${token.kind}`);
} });

// The plain bracket matcher compares node types and skips quoted/commented brackets.
const bracketSql = "( ')' /* ) */ `)` # )\n 1 )";
const brackets = makeState(bracketSql);
const match = cm.matchBrackets(brackets, 0, 1, { maxScanDistance: cm.HIGHLIGHT_LIMIT });
assert.equal(match.matched, true);
assert.equal(match.end.from, bracketSql.length - 1);

// Replacements across multiline lexical boundaries rebuild the full tree.
let edited = makeState("SELECT /* comment\n with ) */ 'x'");
edited = edited.update({ changes: { from: 7, to: 9, insert: "'" } }).state;
assert.deepEqual(cm.treeTokens(edited), cm.tokenize(edited.doc.toString()));
let boundary = makeState("SELECT 1" + " ".repeat(59992));
boundary = boundary.update({ changes: { from: 60000, insert: " " } }).state;
assert.deepEqual(cm.treeTokens(boundary), []);
boundary = boundary.update({ changes: { from: 60000, to: 60001 } }).state;
assert.deepEqual(cm.treeTokens(boundary), cm.tokenize(boundary.doc.toString()));
console.log(`SQL tree parity: ${checked} cases passed (${fixtureCount} fixture SQL strings, 2000 seeded cases, long line, limit, classes and bracket scan).`);
