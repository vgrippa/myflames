// Production Sessions navigation hint, no DOM, network or database.
// Run from apps/workspace: node test/sessions-ddl-unit.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { performance } from "node:perf_hooks";
import ts from "typescript";

// Compile the production declarations, like problems-unit's runtime harness,
// without importing unrelated component modules or their browser side effects.
const source = ts.createSourceFile("Sessions.tsx", readFileSync(new URL("../src/Sessions.tsx", import.meta.url), "utf8"),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const declarations = [];
for (const statement of source.statements) {
  if (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(d => d.name.getText(source) === "DDL_START")) {
    declarations.push(statement.getText(source));
  }
  if (ts.isFunctionDeclaration(statement) && statement.name?.text === "startsDdl") declarations.push(statement.getText(source));
}
assert.equal(declarations.length, 2, "test the actual DDL regex and exported helper");
const compiled = ts.transpileModule(declarations.join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText;
const out = {};
new Function("exports", compiled)(out);
const { startsDdl } = out;

for (const prefix of ["/**/", "/**/ ", "-- \n", "# \n"]) {
  const sql = prefix.repeat(5000) + "SELECT 1";
  const started = performance.now();
  const result = startsDdl(sql);
  const elapsed = performance.now() - started;
  assert.equal(result, false, JSON.stringify(prefix));
  assert.ok(elapsed < 50, `${JSON.stringify(prefix)} took ${elapsed.toFixed(3)} ms`);
  console.log(`Sessions DDL ${JSON.stringify(prefix)} x5000: ${elapsed.toFixed(3)} ms`);
}
for (const sql of ["/* x */ ALTER TABLE t ADD c INT", "-- note\nDROP TABLE t", "# n\nCREATE INDEX i ON t (c)",
  "/* a ** b * c */REPAIR TABLE t", "/**/-- note\n# note\r\nOPTIMIZE TABLE t", "\nTRUNCATE TABLE t", "RENAME TABLE t TO u"]) {
  assert.equal(startsDdl(sql), true, sql);
}
for (const sql of ["SELECT 'ALTER'", "-- DROP TABLE t", "# CREATE TABLE t", "/* ALTER TABLE t */ SELECT 1",
  "/**/-- CREATE TABLE t", "CREATEd", "/* unclosed CREATE TABLE t"]) {
  assert.equal(startsDdl(sql), false, sql);
}
console.log("Sessions DDL unit tests passed.");
