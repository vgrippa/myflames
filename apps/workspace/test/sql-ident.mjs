// Execute the shared SQL builders without a browser, server or emitted assets.
// Run: node test/sql-ident.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import ts from "typescript";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/sqlIdent.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "sqlIdent.ts", { loader: "ts", format: "esm", target: "es2020" });
const { bt, qualifiedTable, sqlLiteral, indexVisibilitySql, dropIndexSql, tableMaintenanceSql, showIndexSql } =
  await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));

assert.equal(bt("normal"), "`normal`");
assert.equal(bt("a`b``c"), "`a``b````c`");
assert.equal(bt("slash\\name"), "`slash\\name`", "backslashes in identifiers stay literal");
assert.equal(bt("space . ' ü\n--"), "`space . ' ü\n--`");
assert.equal(qualifiedTable("s`x", "t`y"), "`s``x`.`t``y`");
assert.equal(sqlLiteral("end\\"), "'end\\\\'");
assert.equal(sqlLiteral("a'\\'b"), "'a''\\\\''b'");

const schema = "s`'; DROP TABLE secret; --";
const table = "t`\\path";
const index = "i`'\\";
const qualified = "`s``'; DROP TABLE secret; --`.`t``\\path`";
for (const [engine, visible, keyword] of [
  ["mysql", true, "INVISIBLE"], ["mysql", false, "VISIBLE"],
  ["MariaDB", true, "IGNORED"], ["mariadb", false, "NOT IGNORED"],
]) {
  assert.equal(indexVisibilitySql(schema, table, index, engine, visible),
    `ALTER TABLE ${qualified} ALTER INDEX ` + "`i``'\\` " + keyword);
}
assert.equal(dropIndexSql(schema, table, index), `ALTER TABLE ${qualified} DROP INDEX ` + "`i``'\\`");
for (const primary of ["PRIMARY", "primary"]) {
  assert.equal(dropIndexSql(schema, table, primary), `ALTER TABLE ${qualified} DROP PRIMARY KEY`);
}
assert.equal(dropIndexSql(schema, table, "PRIMARY`"), `ALTER TABLE ${qualified} DROP INDEX ` + "`PRIMARY```");
for (const verb of ["ANALYZE", "OPTIMIZE", "CHECK", "CHECKSUM"]) {
  assert.equal(tableMaintenanceSql(verb, schema, table), `${verb} TABLE ${qualified}${verb === "CHECKSUM" ? " EXTENDED" : ""}`);
}
assert.equal(showIndexSql(schema, table, index), `SHOW INDEX FROM ${qualified} WHERE Key_name = ` + "'i`''\\\\';");

// Inspect every view, not only the shared builder: a menu can bypass a safe
// helper accidentally. The first regression was six raw Tables call sites.
const views = await Promise.all(["Tables", "Indexes"].map(async name => [name,
  await fs.readFile(new URL(`../src/${name}.tsx`, import.meta.url), "utf8")]));
for (const [name, source] of views) {
  assert.doesNotMatch(source, /\b(?:ANALYZE|OPTIMIZE|CHECK|CHECKSUM) TABLE\s/, `${name} must use the maintenance builder at every call site`);
}

// Execute the actual SQL expressions passed by TableRowActions and the
// inspector with hostile names. No browser/hooks or copied builder logic.
const tableSource = views.find(([name]) => name === "Tables")[1];
const tree = ts.createSourceFile("Tables.tsx", tableSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function matching(node, predicate) {
  const found = [];
  function visit(child) { if (predicate(child)) found.push(child); ts.forEachChild(child, visit); }
  visit(node);
  return found;
}
const rowActions = matching(tree, node => ts.isFunctionDeclaration(node) && node.name?.text === "TableRowActions")[0];
const inspectorActions = matching(tree, node => ts.isVariableDeclaration(node) && node.name.getText(tree) === "tableInspectorActions")[0];
const rowExpressions = matching(rowActions, node => ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression) && node.expression.name.text === "start")
  .map(node => node.arguments[0].getText(tree));
const inspectorExpressions = matching(inspectorActions, node => ts.isJsxAttribute(node) && node.name.getText(tree) === "sql")
  .map(node => node.initializer.expression.getText(tree));
assert.equal(rowExpressions.length, 3, "the row menu has three maintenance actions");
assert.equal(inspectorExpressions.length, 3, "the inspector has three maintenance actions");
for (const expressions of [rowExpressions, inspectorExpressions]) {
  for (const [i, expression] of expressions.entries()) {
    const { code } = await transformWithEsbuild(`export default (schema, table, r, tableMaintenanceSql) => (${expression});`,
      "table-action.ts", { loader: "ts", format: "esm", target: "es2020" });
    const { default: actual } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
    assert.equal(actual(schema, table, { schema, table }, tableMaintenanceSql), `${["OPTIMIZE", "ANALYZE", "CHECK"][i]} TABLE ${qualified}`);
  }
}
console.log("SQL builders and view integration: quoting, both dialects, every maintenance call site, row-menu and inspector expressions passed");
