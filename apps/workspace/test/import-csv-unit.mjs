// Unit tests for src/importCsvRules.ts, Import CSV's pure rules: the column
// type grammar (the same answers as the backend's csv_import.parse_type,
// recorded from myflames/csv_import.py for every case below), CSV-to-table
// column matching, names for a new table, separator and encoding names.
// No build or browser: the module is compiled with Vite's esbuild transform.
// Run: node test/import-csv-unit.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/importCsvRules.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "importCsvRules.ts", { loader: "ts", format: "esm", target: "es2020" });
const rules = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));
const { typeProblem, matchColumns, tableNameFromFile, columnNames, nameProblem, separatorName, encodingValue, encodingLabel, TYPE_CHOICES } = rules;

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

// [type as typed, the backend's refusal ("" when it accepts)], recorded from
// csv_import.parse_type. The wizard must accept and refuse exactly the same.
const BACKEND = [
  ["INT", ""],
  ["int unsigned", ""],
  ["INTEGER", ""],
  ["integer(11)", ""],
  ["BIGINT UNSIGNED", ""],
  ["TINYINT(1)", ""],
  ["tinyint(0)", "TINYINT takes at most one display width."],
  ["INT(256)", "INT takes at most one display width."],
  ["INT(10,2)", "INT takes at most one display width."],
  ["INT ZEROFILL", "INT ZEROFILL is not a type Import CSV can create."],
  ["SMALLINT", ""],
  ["MEDIUMINT(8) UNSIGNED", ""],
  ["DECIMAL", ""],
  ["DECIMAL(10,2)", ""],
  ["decimal(65,30)", ""],
  ["DECIMAL(66,2)", "DECIMAL precision is 1 to 65 and scale 0 to 30, no larger than the precision."],
  ["DECIMAL(10,11)", "DECIMAL precision is 1 to 65 and scale 0 to 30, no larger than the precision."],
  ["DECIMAL(10,2) UNSIGNED", "UNSIGNED is deprecated for DECIMAL; use a CHECK in the Query editor."],
  ["NUMERIC(5)", ""],
  ["DECIMAL(a)", "DECIMAL takes whole numbers in parentheses."],
  ["DECIMAL()", "DECIMAL takes whole numbers in parentheses."],
  ["FLOAT", ""],
  ["FLOAT(7)", "Write FLOAT without a length."],
  ["DOUBLE", ""],
  ["DOUBLE PRECISION", "DOUBLE PRECISION is not a type Import CSV can create."],
  ["DOUBLE UNSIGNED", "UNSIGNED is deprecated for DOUBLE; use a CHECK in the Query editor."],
  ["REAL", "REAL is not a type Import CSV can create."],
  ["DATE", ""],
  ["DATE(3)", "Write DATE without a length."],
  ["DATETIME", ""],
  ["DATETIME(6)", ""],
  ["DATETIME(7)", "DATETIME takes a fractional precision from 0 to 6."],
  ["TIMESTAMP(3)", ""],
  ["TIME(1,2)", "TIME takes a fractional precision from 0 to 6."],
  ["YEAR", ""],
  ["YEAR(4)", "Write YEAR without a length."],
  ["CHAR(0)", ""],
  ["CHAR(255)", ""],
  ["CHAR(256)", "CHAR length must be at most 255."],
  ["CHAR", "CHAR needs a length, like CHAR(64)."],
  ["VARCHAR(255)", ""],
  ["VARCHAR(16383)", ""],
  ["VARCHAR(16384)", "VARCHAR length must be at most 16383."],
  ["VARCHAR(0)", "VARCHAR length must be at most 16383."],
  ["VARCHAR", "VARCHAR needs a length, like VARCHAR(64)."],
  ["BINARY(16)", ""],
  ["VARBINARY(65532)", ""],
  ["VARBINARY(65533)", "VARBINARY length must be at most 65532."],
  ["TEXT", ""],
  ["TEXT(100)", "Write TEXT without a length."],
  ["LONGTEXT", ""],
  ["BLOB", ""],
  ["MEDIUMBLOB", ""],
  ["JSON", ""],
  ["JSON(1)", "Write JSON without a length."],
  ["BIT", ""],
  ["BIT(64)", ""],
  ["BIT(65)", "BIT takes a width from 1 to 64."],
  ["BIT(0)", "BIT takes a width from 1 to 64."],
  ["BOOL", ""],
  ["BOOLEAN", ""],
  ["BOOLEAN(1)", "BOOLEAN takes no length."],
  ["BOOL UNSIGNED", "BOOLEAN takes no length."],
  ["TEXT UNSIGNED", "UNSIGNED applies to numbers only."],
  ["CHAR(10) UNSIGNED", "UNSIGNED applies to numbers only."],
  ["ENUM('a','b')", ""],
  ["ENUM('a','A')", "ENUM members must be distinct (at most 65,535)."],
  ["ENUM", "ENUM needs its members, like ENUM('a','b')."],
  ["ENUM()", "ENUM needs its members, like ENUM('a','b')."],
  ["ENUM('a\\\\b')", "ENUM members must be quoted text without backslashes or control characters."],
  ["SET('x','y')", ""],
  ["ENUM('it''s')", ""],
  ["ENUM(a)", "The ENUM or SET members could not be read."],
  ["ENUM('a'", "ENUM('a' is not a type Import CSV can create."],
  ["ENUM('a', 'b')", ""],
  ["GEOMETRY", "GEOMETRY is not a type Import CSV can create."],
  ["UUID", "UUID is not a type Import CSV can create."],
  ["VARCHAR(40) CHARACTER SET latin1", "VARCHAR(40) CHARACTER SET latin1 is not a type Import CSV can create."],
  ["  int  ", ""],
  ["VARCHAR( 40 )", ""],
  ["xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx is not a type Import CSV can create."],
  ["DECIMAL(10, 2)", ""],
  ["ENUM('a\tb')", "ENUM members must be quoted text without backslashes or control characters."],
];

test("the type grammar answers like the backend for every recorded case", () => {
  for (const [text, expected] of BACKEND) assert.equal(typeProblem(text), expected, `typeProblem(${JSON.stringify(text)})`);
});
test("an empty type asks for one (the backend refuses it too)", () => {
  assert.equal(typeProblem(""), "Choose a column type.");
  assert.equal(typeProblem("   "), "Choose a column type.");
  assert.equal(typeProblem("INT" + " ".repeat(4000)), "Choose a column type.");
});
test("every offered type is accepted", () => {
  for (const type of TYPE_CHOICES) assert.equal(typeProblem(type), "", type);
});
test("columns match by header name, then ignoring case and punctuation, then by position", () => {
  const csv = [{ index: 0, header: "Order ID" }, { index: 1, header: "customer" }, { index: 2, header: "Extra" }, { index: 3, header: "Amount" }];
  assert.deepEqual(matchColumns(csv, ["order_id", "Customer", "amount", "note"], true), ["order_id", "Customer", null, "amount"],
    "Extra falls back to position 3, amount, which Amount already took");
  assert.deepEqual(matchColumns(csv, ["order_id", "Customer", "note", "amount"], true), ["order_id", "Customer", "note", "amount"],
    "a free column at the same position is used");
  assert.deepEqual(matchColumns(csv, ["a", "b"], false), ["a", "b", null, null], "without a header only position counts");
  assert.deepEqual(matchColumns([{ index: 0, header: "x" }, { index: 1, header: "x" }], ["x"], true), ["x", null], "a table column is used once");
});
test("a new table's names come from the file and the headers", () => {
  assert.equal(tableNameFromFile("Orders 2026.csv"), "orders_2026");
  assert.equal(tableNameFromFile("Café-Ventes.TSV"), "cafe_ventes");
  assert.equal(tableNameFromFile("2026.csv"), "imported_2026");
  assert.equal(tableNameFromFile("....txt"), "imported");
  assert.equal(tableNameFromFile("x".repeat(80) + ".csv").length, 64);
  assert.deepEqual(columnNames(["Order ID", "order id", "", "Amount (EUR)"]), ["order_id", "order_id_2", "column_3", "amount_eur"]);
  assert.equal(nameProblem(""), "Enter a name.");
  assert.equal(nameProblem("a.b"), "No dots, slashes or NUL characters.");
  assert.equal(nameProblem("x".repeat(65)), "At most 64 characters.");
  assert.equal(nameProblem("orders"), "");
});
test("separators and encodings are named", () => {
  assert.equal(separatorName(";"), "semicolon");
  assert.equal(separatorName("\t"), "tab");
  assert.equal(separatorName(" "), "space");
  assert.equal(separatorName("~"), "“~”");
  assert.equal(encodingValue("UTF_8"), "utf-8");
  assert.equal(encodingValue("windows-1252"), "cp1252");
  assert.equal(encodingLabel("utf-8-sig"), "UTF-8 with BOM");
});
console.log(`import-csv-unit: ${passed} groups passed (${BACKEND.length} type cases match the backend)`);
