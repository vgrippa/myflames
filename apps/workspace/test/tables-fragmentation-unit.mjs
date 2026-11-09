// Exercise the actual Rebuild component and ownership projection without a
// socket or browser. The browser suite owns layout, filtering and sort state.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import React from "react";
import ts from "typescript";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/Tables.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("Tables.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const functions = tree.statements.filter(node => ts.isFunctionDeclaration(node) && ["Rebuild", "reclaimable"].includes(node.name?.text));
assert.equal(functions.length, 2);
const { code } = await transformWithEsbuild(
  `export default (React, qualifiedTable, tableMaintenanceSql, CopySql, InspectorSection, bytes) => {
    ${functions.map(node => node.getText(tree)).join("\n")}
    return { Rebuild, reclaimable };
  };`, "tables-fragmentation.tsx", { loader: "tsx", format: "esm", target: "es2020", jsx: "transform" });
const { default: make } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const identSource = await fs.readFile(new URL("../src/sqlIdent.ts", import.meta.url), "utf8");
const ident = await transformWithEsbuild(identSource, "sqlIdent.ts", { loader: "ts", format: "esm", target: "es2020" });
const { qualifiedTable, tableMaintenanceSql } = await import("data:text/javascript;base64," + Buffer.from(ident.code).toString("base64"));
const CopySql = () => null;
const InspectorSection = () => null;
const { Rebuild, reclaimable } = make(React, qualifiedTable, tableMaintenanceSql, CopySql, InspectorSection, value => value === null ? "Unknown" : `${value} bytes`);

function elements(element) {
  if (!React.isValidElement(element)) return [];
  return [element, ...React.Children.toArray(element.props.children).flatMap(elements)];
}
function text(element) {
  if (typeof element === "string" || typeof element === "number") return String(element);
  return React.isValidElement(element) ? React.Children.toArray(element.props.children).map(text).join("") : "";
}
const row = { schema: "s.`name", table: "t.`name", data_bytes: 200, index_bytes: 100, free_bytes: 100, status: "info" };
const report = { engine: "mysql", notes: [], shared_tablespaces: [] };
for (const [status, expected] of [["info", 100], ["ok", 100], ["shared", null], ["unknown", null]]) {
  assert.equal(reclaimable({ ...row, status }), expected, `${status}: only attributable bytes are reclaimable`);
}
assert.equal(reclaimable({ ...row, free_bytes: null }), null);
assert.equal(reclaimable({ ...row, free_bytes: 0 }), 0);

const opened = [];
const component = Rebuild({ row, report, onOpenSql: sql => opened.push(sql), disabled: false });
assert.equal(component.type, InspectorSection);
assert.equal(component.props.title, "Rebuild");
const all = elements(component);
const statements = all.filter(element => element.type === CopySql).map(element => element.props.sql);
assert.deepEqual(statements, ["OPTIMIZE TABLE `s.``name`.`t.``name`;", "ALTER TABLE `s.``name`.`t.``name` ENGINE=InnoDB;"]);
const buttons = all.filter(element => element.type === "button");
assert.equal(buttons.length, 2, "Rebuild exposes only the two editor handoffs");
assert.deepEqual(opened, [], "rendering never opens or executes a statement");
for (const button of buttons) {
  assert.equal(button.props.disabled, false);
  assert.equal(button.props.children, "Open in Query editor");
  button.props.onClick();
}
assert.deepEqual(opened, statements, "the editor receives exactly the SQL shown for copying");
for (const pattern of [/recreate \+ analyze/, /without the separate ANALYZE TABLE step/, /concurrent reads and writes/,
  /FULLTEXT or SPATIAL/, /writes wait until the rebuild ends/, /300 bytes/, /replicas rebuild too/, /NO_WRITE_TO_BINLOG/, /partly filled pages/,
  /Fresh numbers and refresh/]) assert.match(text(component), pattern);
assert.equal(elements(Rebuild({ row, report, disabled: false })).filter(element => element.type === "button").length, 0,
  "without the callback, only copying is offered");
assert.ok(elements(Rebuild({ row, report, onOpenSql() {}, disabled: true })).filter(element => element.type === "button")
  .every(element => element.props.disabled), "a list from another connection disables editor handoffs");
const maria = { ...report, engine: "mariadb", notes: ["innodb_defragment is ON: OPTIMIZE TABLE defragments instead of rebuilding."] };
const mariaText = text(Rebuild({ row, report: maria, disabled: false }));
assert.match(mariaText, /innodb_defragment is ON/);
assert.match(mariaText, /or an index on a virtual column/);
assert.doesNotMatch(mariaText, /information_schema_stats_expiry|OPTIMIZE_LOCAL_TABLE/);
assert.match(text(Rebuild({ row: { ...row, status: "shared" }, report, disabled: false })), /the file does not shrink/);
assert.match(text(Rebuild({ row: { ...row, status: "unknown" }, report, disabled: false })), /Reclaimable bytes are unknown/);
console.log("Tables fragmentation: ownership, quoted statements, editor handoffs, no execute action and engine caveats passed");
