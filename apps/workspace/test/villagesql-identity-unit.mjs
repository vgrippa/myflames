// Compile the production identity helpers without mounting views or opening a server.
// Run from apps/workspace: node test/villagesql-identity-unit.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

function declarations(file, names, dependencies = {}) {
  const source = ts.createSourceFile(file, readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, file.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const found = source.statements.filter(statement =>
    (ts.isFunctionDeclaration(statement) && names.includes(statement.name?.text)) ||
    (ts.isVariableStatement(statement) && statement.declarationList.declarations.some(d => names.includes(d.name.getText(source)))));
  assert.equal(found.length, names.length, `${file}: every production declaration found`);
  const code = found.map(statement => statement.getText(source)).join("\n")
    + `\nexport { ${names.join(", ")} };`;
  const compiled = ts.transpileModule(code, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.React, jsxFactory: "createElement" },
  }).outputText;
  const out = {};
  new Function("exports", ...Object.keys(dependencies), compiled)(out, ...Object.values(dependencies));
  return out;
}

const identity = declarations("serverIdentity.ts", ["hasPerconaFeatures", "villageSQLLabel"]);
// The Python release parser reads these exact vectors too. For malformed
// releases, null proves that no partial or unsafe suffix infers a product.
const releaseVectors = JSON.parse(readFileSync(
  new URL("../../../test/fixtures/villagesql/version-labels.json", import.meta.url), "utf8"));
assert.ok(releaseVectors.length >= 40, "the shared release grammar includes hostile and boundary cases");
for (const vector of releaseVectors) {
  assert.equal(identity.villageSQLLabel(vector.version, vector.base), vector.expected_label, vector.name);
  if (vector.expected_release !== null)
    assert.ok(vector.expected_label.startsWith(`VillageSQL ${vector.expected_release} (`), vector.name);
  else {
    const upstream = vector.version.match(/^[ \t\r\n\f\v]*([0-9]+\.[0-9]+\.[0-9]+)/)?.[1] || "?";
    const codebase = vector.base === "percona" ? "Percona Server " : vector.base === "mysql" ? "MySQL " : "";
    assert.equal(identity.villageSQLLabel(vector.version, vector.base, "VillageSQL"),
      vector.expected_explicit_label || `VillageSQL ? (${codebase}${upstream})`, vector.name);
  }
}

const { engineName } = declarations("OpsShared.tsx", ["engineName"], identity);
const { engineLabel } = declarations("PerfShared.tsx", ["engineLabel"], identity);
const { testedServer } = declarations("Connections.tsx", ["testedServer"], identity);
const { memberEngine } = declarations("ClusterTopology.tsx", ["memberEngine"], { engineName });
const { documentationLink } = declarations("Config.tsx", ["DOC_HOSTS", "documentationLink"]);
const { readinessLine, factsOf } = declarations("readiness.tsx", ["readinessLine", "factsOf"], identity);
const { engineText } = declarations("TopologyPlace.tsx", ["engineText"], identity);
const { itemDetail } = declarations("Baselines.tsx", ["itemDetail"], identity);
const { serverName } = declarations("baselineCompare.ts", ["serverName"], identity);
const { productText } = declarations("schemaVersionsApi.ts", ["productText"], identity);
const { engineName: planEngineName } = declarations("planDisplay.ts", ["engineName"]);
const { captureServerLabel } = declarations("planDisplay.ts", ["captureServerLabel"], { ...identity, engineName: planEngineName });

// Exercise the exact production expressions at the two JSX call sites too.
// The shell must keep its small, eager identity import instead of OpsShared.
function expression(file, matches, dependencies) {
  const source = ts.createSourceFile(file, readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found = [];
  function visit(node) {
    if (matches(node)) found.push(node.initializer.getText(source));
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(found.length, 1, `${file}: exactly one production label expression`);
  return context => new Function(...Object.keys(dependencies), ...Object.keys(context), `return (${found[0]});`)(
    ...Object.values(dependencies), ...Object.values(context));
}
const mapEngine = expression("TopologyMap.tsx", node => ts.isVariableDeclaration(node) && node.name.getText() === "engine", identity);
const captureEngine = expression("main.tsx", node => ts.isPropertyAssignment(node) && node.name.getText() === "engineVersion",
  { captureServerLabel });

function labelSurfaces(engine, version, product, base, expected) {
  const server = { engine, version, product, server_base: base };
  const report = { engine, server_version: version, server_product: product, server_base: base };
  const facts = { ...server, performance_schema: true };
  assert.ok(readinessLine(facts).startsWith(expected + " · "));
  assert.equal(engineText(server), expected);
  assert.equal(mapEngine({ peer: false, node: server }), expected);
  assert.equal(mapEngine({ peer: true, node: server }), "Reported peer");
  assert.equal(itemDetail({ kind: "health", server }), expected + " · All OK");
  assert.equal(itemDetail({ kind: "digest", server, statements: 2 }), expected + " · 2 statements");
  assert.equal(serverName(server), expected + " at ?:?");
  assert.equal(serverName({ ...server, server_uuid: "one" }), expected + " (server_uuid one)");
  assert.equal(productText(report), expected);
}

for (const [version, base, expected] of [
  ["8.4.11-villagesql-0.0.8", "mysql", "VillageSQL 0.0.8 (MySQL 8.4.11)"],
  ["8.4.11-villagesql-0.0.8", "percona", "VillageSQL 0.0.8 (Percona Server 8.4.11)"],
  ["8.4.11-villagesql-0.0.8", null, "VillageSQL 0.0.8 (8.4.11)"],
  ["9.7.2-villagesql-0.0.8", "mysql", "VillageSQL 0.0.8 (MySQL 9.7.2)"],
  ["9.7.2-villagesql-0.0.9-dev-543f42e0601", "mysql", "VillageSQL 0.0.9-dev (MySQL 9.7.2)"],
  ["9.7.2-villagesql-0.0.9-dev", undefined, "VillageSQL 0.0.9-dev (9.7.2)"],
  ["8.4.11-villagesql-0.0.9-rc.1-543f42e0601", "mysql", "VillageSQL 0.0.9-rc.1 (MySQL 8.4.11)"],
  ["8.4.11-villagesql-0.0.9-alpha.2", "mysql", "VillageSQL 0.0.9-alpha.2 (MySQL 8.4.11)"],
  ["8.4.11-villagesql-0.0.9-Beta.1-aBcDeF0", "percona", "VillageSQL 0.0.9-Beta.1 (Percona Server 8.4.11)"],
  ["8.4.11-villagesql-0.0.9-deadbee", "mysql", "VillageSQL 0.0.9-deadbee (MySQL 8.4.11)"],
  ["8.4.11-villagesql-0.0.9-" + "a".repeat(40), null, "VillageSQL 0.0.9-" + "a".repeat(40) + " (8.4.11)"],
  ["8.4.11-villagesql-0.0.9-deadbee-543f42e0601", "mysql", "VillageSQL 0.0.9-deadbee (MySQL 8.4.11)"],
]) {
  const report = { engine: "mysql", flavor: "villagesql", server_version: version, server_product: "VillageSQL", server_base: base };
  assert.equal(engineName("mysql", version, "VillageSQL", base), expected);
  assert.equal(engineName("mysql", version, undefined, base), expected, "version-only identity retains VillageSQL");
  assert.equal(engineLabel(report), expected);
  assert.equal(testedServer({ version, flavor: "villagesql", product: "VillageSQL", server_base: base }), expected);
  assert.equal(memberEngine({ ...report, kind: "innodb_cluster" }, { version, local: true }), expected);
  labelSurfaces("mysql", version, "VillageSQL", base, expected);
  assert.equal(captureEngine({ mode: "inspect", active: { capture: { engine: "mysql", server_version: version } } }),
    identity.villageSQLLabel(version), "capture labels retain version-only identity without another read");
}
for (const suffix of ["-rc.1\u001b[31m", "-<b>rc.1</b>", "-rc.1-other", "-rc.1-123456", "-rc.1-" + "a".repeat(41)]) {
  const version = "8.4.11-villagesql-0.0.9" + suffix;
  assert.equal(identity.villageSQLLabel(version), null, "malformed suffix cannot produce a partial release label");
  assert.equal(identity.villageSQLLabel(version, "mysql", "VillageSQL"), "VillageSQL ? (MySQL 8.4.11)",
    "untrusted suffixes never enter the product label");
}
assert.equal(engineName("mysql", undefined, "VillageSQL"), "VillageSQL ? (?)");
assert.equal(engineLabel({ engine: "mysql", server_version: null, server_product: "VillageSQL" }), "VillageSQL ? (?)");
assert.equal(identity.villageSQLLabel("8.4.11", "percona", "MySQL"), null);

for (const [engine, version, product, short, full] of [
  ["mysql", "8.4.11", "MySQL", "MySQL 8.4.11", "MySQL 8.4.11"],
  ["mysql", "8.4.11-11", "Percona Server", "Percona Server 8.4.11-11", "Percona Server 8.4.11-11"],
  ["mysql", "8.4.11-11.1", "Percona XtraDB Cluster", "Percona XtraDB Cluster 8.4.11-11.1", "Percona XtraDB Cluster 8.4.11-11.1"],
  ["mariadb", "11.4.13-MariaDB-log", "MariaDB", "MariaDB 11.4.13", "MariaDB 11.4.13-MariaDB-log"],
  ["mysql", "8.0.26", "Readyset", "Readyset 8.0.26", "Readyset 8.0.26"],
]) {
  assert.equal(engineName(engine, version, product), short);
  assert.equal(engineLabel({ engine, server_version: version, server_product: product }), full);
  labelSurfaces(engine, version, product, undefined, full);
  assert.equal(captureEngine({ mode: "inspect", active: { capture: { engine, server_version: version } } }),
    `${planEngineName(engine)} ${version}`, "other captured products retain the existing label");
}
assert.equal(captureEngine({ mode: "compare", active: null }), undefined);
assert.equal(engineText({ engine: null, version: null, product: null }), "");
assert.equal(productText({ engine: "mysql" }), "");
assert.equal(serverName(null), "? ? at ?:?");
assert.equal(engineName(null), "Server");
assert.equal(engineLabel({ engine: "mysql", server_version: null }), "MySQL ?");
assert.equal(testedServer({ version: "8.4.11", flavor: "mysql", product: "MySQL" }), "8.4.11");
assert.equal(testedServer({ version: "8.4.11-11", flavor: "percona", product: "Percona Server" }), "Percona Server 8.4.11-11");
for (const flavor of [undefined, null, "mysql", "mariadb", "readyset", "villagesql", "percona"]) {
  for (const base of [undefined, null, "mysql", "percona", "unknown"])
    assert.equal(identity.hasPerconaFeatures(flavor, base), flavor === "percona" || (flavor === "villagesql" && base === "percona"));
}

for (const hostile of ["percona-" + "x".repeat(100000), "percona-8.4_0.0.8\0\n\u202e",
  "percona-mysql-8.4_0.0.8", '<img src=x onerror="alert(1)">']) {
  labelSurfaces("mysql", "8.4.11-villagesql-0.0.8", "VillageSQL", hostile, "VillageSQL 0.0.8 (8.4.11)");
  assert.equal(factsOf({ product: "VillageSQL", version: "8.4.11-villagesql-0.0.8", server_base: hostile }).server_base, null);
}
assert.equal(factsOf({ product: "VillageSQL", version: "8.4.11-villagesql-0.0.8", server_base: "percona" }).server_base, "percona");
for (const server of [
  { product: "VillageSQL", version: "8.4.11-villagesql-0.0.8", server_base: '<img src=x onerror="alert(1)">' },
  { product: '<img src=x onerror="alert(1)">', version: "8.4.11" },
]) {
  const { ReadinessFacts } = declarations("readiness.tsx", ["ReadinessFacts"], {
    createElement, readinessLine, InfoPopover: "section", relativeTime: () => "just now",
    useReadiness: () => ({ facts: { ...server, checked_at: "2026-11-27T12:00:00Z" } }),
  });
  const markup = renderToStaticMarkup(createElement(ReadinessFacts, { connection: {} }));
  assert.ok(!markup.includes("<img"), "server metadata never becomes an HTML element");
  if (server.product === "VillageSQL") assert.ok(!markup.includes("onerror"), "raw codebase metadata never enters markup");
  else assert.ok(markup.includes("&lt;img"), "ordinary server text is escaped by the production JSX");
}

const documentation = "https://villagesql.com/docs/en/8.4/stable/reference.md";
const { externalLink } = createRequire(import.meta.url)("../../desktop/backend.cjs");
assert.deepEqual(documentationLink({ url: documentation, source: "VillageSQL" }), { url: documentation, source: "VillageSQL" });
assert.equal(externalLink(documentation), documentation);
for (const url of [
  "http://villagesql.com/docs/en/8.4/stable/reference.md",
  "https://villagesql.com.evil.invalid/reference.md",
  "https://villagesql.com@evil.invalid/reference.md",
  "https://user:secret@villagesql.com/reference.md",
  "https://villagesql.com:444/reference.md",
  "javascript:alert(1)",
]) {
  assert.equal(documentationLink({ url, source: "VillageSQL" }), null, url);
  assert.equal(externalLink(url), null, url);
}
// Execute the production receipt effect with a fixed clock. New identity
// evidence must win even when receipts share a millisecond or clocks go back.
function receiptEffect() {
  const file = "Server.tsx";
  const source = ts.createSourceFile(file, readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const refs = [], effects = [];
  function visit(node) {
    if (ts.isVariableStatement(node) && node.declarationList.declarations.some(d =>
      ["received", "receiptSequence"].includes(d.name.getText(source)))) refs.push(node.getText(source));
    if (ts.isCallExpression(node) && node.expression.getText(source) === "useEffect"
      && node.arguments[0]?.getText(source).includes("setServerFlavor(")) effects.push(node.arguments[0].getText(source));
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(effects.length, 1, "execute the production identity receipt effect");
  let clock = 1000, lastRead, serverFlavor;
  const code = refs.join("\n") + `\nreturn context => { const { activeKey, readiness, health, innodb, errorlog,
    sessions, memory, waits, io, locks, indexes, tables, config, instruments, extensions, replication, cluster,
    accounts, diagram, maintenance, analysis, topStatements } = context; return (${effects[0]})(); };`;
  const compiled = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  const step = new Function("useRef", "Date", "setLastRead", "setServerFlavor", "villageSQLLabel", compiled)(
    current => ({ current }), { now: () => clock },
    update => { lastRead = update(lastRead || {}); }, update => { serverFlavor = update(serverFlavor || {}); }, identity.villageSQLLabel);
  const context = { activeKey: "current", readiness: {}, health: {}, maintenance: {}, topStatements: {} };
  for (const name of ["innodb", "errorlog", "sessions", "memory", "waits", "io", "locks", "indexes", "tables", "config",
    "instruments", "extensions", "replication", "cluster", "accounts", "diagram", "analysis"]) context[name] = null;
  return { context, step: () => { step(context); return { lastRead, serverFlavor }; }, setClock: value => { clock = value; } };
}
const receipts = receiptEffect();
const oldVillage = { engine: "mysql", flavor: "villagesql", server_version: "8.4.11-villagesql-0.0.8" };
receipts.context.extensions = { key: "current", report: oldVillage };
assert.equal(receipts.step().serverFlavor.flavor, "villagesql");
receipts.context.readiness = { facts: { flavor: "mysql", product: "MySQL", version: "8.4.11" } };
assert.equal(receipts.step().serverFlavor.flavor, "mysql", "new readiness wins within the same millisecond");
assert.equal(receipts.step().serverFlavor.flavor, "mysql", "retained old reports never become new receipts");
assert.equal(receipts.step().lastRead.engineVersion, "MySQL 8.4.11", "readiness version follows the same receipt order");
receipts.context.accounts = { key: "current", report: { engine: "mysql", version: "8.4.10" } };
assert.equal(receipts.step().lastRead.engineVersion, "MySQL 8.4.10");
receipts.context.health = { key: "current", report: { engine: "mysql", flavor: "mysql", server_version: "8.4.12" } };
assert.equal(receipts.step().lastRead.engineVersion, "MySQL 8.4.12", "new version wins over an old report later in the array");
assert.equal(receipts.step().lastRead.engineVersion, "MySQL 8.4.12", "retained account reports keep a stable receipt");
receipts.setClock(999);
receipts.context.health.report = { ...oldVillage, server_version: "8.4.12-villagesql-0.0.9" };
assert.equal(receipts.step().serverFlavor.flavor, "villagesql", "identity receipt order survives a backwards wall clock");
receipts.context.readiness = { facts: { flavor: "", product: "MySQL", version: "8.4.12" } };
assert.equal(receipts.step().serverFlavor.flavor, "villagesql", "empty identity does not override known identity");
receipts.context.extensions = { key: "other", report: { flavor: "mysql", server_version: "9.7.0" } };
assert.equal(receipts.step().serverFlavor.flavor, "villagesql", "another connection cannot relabel this server");
receipts.context.readiness.stale = true;
assert.equal(receipts.step().serverFlavor.flavor, "villagesql", "failed readiness retains the accepted identity");
receipts.context.activeKey = "unknown";
assert.equal(receipts.step().serverFlavor.flavor, null, "a connection without identity stays unknown");
console.log("VillageSQL identity unit tests passed: all label surfaces, capability gates, hostile metadata and escaped JSX, legacy products, and documentation boundaries.");
